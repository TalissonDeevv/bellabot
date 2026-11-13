from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Awaitable, Callable, Iterable

import discord

from .central_logs import CentralLogService
from .state import StateStore

LOGGER = logging.getLogger(__name__)

BACKUPS_SECTION = "server_backups"
BACKUP_RESOURCES_SECTION = "server_backup_resources"
RESTORE_JOBS_SECTION = "backup_restore_jobs"
RESTORED_MESSAGES_SECTION = "restored_backup_messages"
BACKUP_SCHEMA_VERSION = 2
MIN_SUPPORTED_BACKUP_SCHEMA_VERSION = 1
EMBED_SCAN_MESSAGES_PER_CHANNEL = 5000
EMBED_SCAN_TOTAL_MESSAGES = 30000


class BackupJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class BackupMetadata:
    backup_id: str
    guild_id: int
    guild_name: str
    actor_id: int
    created_at: str
    status: BackupJobStatus
    schema_version: int
    automatic: bool = False
    reason: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["warnings"] = list(self.warnings)
        return payload


@dataclass(frozen=True)
class RestorePlan:
    backup_id: str
    guild_id: int
    create_roles: int
    reuse_roles: int
    skip_managed_roles: int
    create_categories: int
    reuse_categories: int
    create_channels: int
    reuse_channels: int
    permission_overwrites: int
    automod_create: int
    automod_update: int
    bella_panels: int
    bella_embeds: int
    unsupported: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    @property
    def high_impact(self) -> bool:
        return bool(
            self.create_roles
            or self.create_categories
            or self.create_channels
            or self.permission_overwrites
            or self.automod_create
            or self.automod_update
        )


@dataclass(frozen=True)
class RestoreResult:
    job_id: str
    backup_id: str
    safety_backup_id: str
    status: BackupJobStatus
    role_map: dict[int, int]
    category_map: dict[int, int]
    channel_map: dict[int, int]
    created: tuple[str, ...]
    reused: tuple[str, ...]
    failures: tuple[str, ...]
    started_at: str
    finished_at: str


PanelRefreshCallback = Callable[[], Awaitable[int]]
SecurityScanCallback = Callable[[discord.Guild, int], Awaitable[Any]]


class ServerBackupRepository:
    def __init__(self, state: StateStore, *, retention_per_guild: int = 15) -> None:
        self.state = state
        self.retention_per_guild = max(3, retention_per_guild)

    async def save_backup(self, metadata: BackupMetadata, payload: dict[str, Any]) -> None:
        await self.state.update_section(BACKUPS_SECTION, metadata.backup_id, metadata.to_dict())
        await self.state.update_section(BACKUP_RESOURCES_SECTION, metadata.backup_id, payload)
        await self._prune(metadata.guild_id)

    async def metadata(self, backup_id: str) -> dict[str, Any] | None:
        values = await self.state.get_section(BACKUPS_SECTION)
        raw = values.get(backup_id)
        return dict(raw) if isinstance(raw, dict) else None

    async def payload(self, backup_id: str) -> dict[str, Any] | None:
        values = await self.state.get_section(BACKUP_RESOURCES_SECTION)
        raw = values.get(backup_id)
        return json.loads(json.dumps(raw, ensure_ascii=False)) if isinstance(raw, dict) else None

    async def list_for_guild(self, guild_id: int, *, limit: int = 15) -> list[dict[str, Any]]:
        values = await self.state.get_section(BACKUPS_SECTION)
        items = [
            dict(value)
            for value in values.values()
            if isinstance(value, dict) and int(value.get("guild_id", 0) or 0) == guild_id
        ]
        return sorted(items, key=lambda item: str(item.get("created_at", "")), reverse=True)[:limit]

    async def save_job(self, job_id: str, payload: dict[str, Any]) -> None:
        await self.state.update_section(RESTORE_JOBS_SECTION, job_id, payload)

    async def job(self, job_id: str) -> dict[str, Any] | None:
        values = await self.state.get_section(RESTORE_JOBS_SECTION)
        raw = values.get(job_id)
        return dict(raw) if isinstance(raw, dict) else None

    async def list_jobs_for_guild(self, guild_id: int, *, limit: int = 15) -> list[dict[str, Any]]:
        values = await self.state.get_section(RESTORE_JOBS_SECTION)
        jobs = [
            dict(value)
            for value in values.values()
            if isinstance(value, dict) and int(value.get("guild_id", 0) or 0) == guild_id
        ]
        return sorted(jobs, key=lambda item: str(item.get("started_at", "")), reverse=True)[:limit]

    async def _prune(self, guild_id: int) -> None:
        items = await self.list_for_guild(guild_id, limit=10_000)
        removable = items[self.retention_per_guild :]
        if not removable:
            return
        remove_ids = {str(item.get("backup_id")) for item in removable}

        def remove_metadata(section: dict[str, Any]) -> None:
            for backup_id in remove_ids:
                section.pop(backup_id, None)

        await self.state.mutate_section(BACKUPS_SECTION, remove_metadata)
        await self.state.mutate_section(BACKUP_RESOURCES_SECTION, remove_metadata)


class ServerBackupService:
    def __init__(
        self,
        bot: discord.Client,
        state: StateStore,
        *,
        logs: CentralLogService | None = None,
        panel_refresh_callback: PanelRefreshCallback | None = None,
        security_scan_callback: SecurityScanCallback | None = None,
    ) -> None:
        self.bot = bot
        self.state = state
        self.logs = logs
        self.repository = ServerBackupRepository(state)
        self.panel_refresh_callback = panel_refresh_callback
        self.security_scan_callback = security_scan_callback
        self._locks: dict[int, asyncio.Lock] = {}

    def set_panel_refresh_callback(self, callback: PanelRefreshCallback) -> None:
        self.panel_refresh_callback = callback

    def set_security_scan_callback(self, callback: SecurityScanCallback) -> None:
        self.security_scan_callback = callback

    async def create_backup(
        self,
        guild: discord.Guild,
        *,
        actor_id: int,
        reason: str,
        automatic: bool = False,
    ) -> BackupMetadata:
        lock = self._locks.setdefault(guild.id, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("Já existe uma operação de backup/restauração neste servidor.")
        async with lock:
            metadata, payload = await self._collect_and_save(
                guild,
                actor_id=actor_id,
                reason=reason,
                automatic=automatic,
            )
        await self._log_backup(guild, metadata)
        return metadata

    async def preview_restore(self, guild: discord.Guild, backup_id: str) -> RestorePlan:
        payload = await self._required_payload(backup_id, guild.id)
        return build_restore_plan(guild, payload, backup_id=backup_id)

    async def restore_backup(
        self,
        guild: discord.Guild,
        backup_id: str,
        *,
        actor_id: int,
        confirmation_phrase: str,
    ) -> RestoreResult:
        if confirmation_phrase.strip().upper() != "RESTAURAR":
            raise ValueError("A confirmação final deve ser exatamente RESTAURAR.")
        payload = await self._required_payload(backup_id, guild.id)
        lock = self._locks.setdefault(guild.id, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("Já existe uma operação de backup/restauração neste servidor.")

        job_id = uuid.uuid4().hex[:16]
        started_at = _utc_now()
        await self.repository.save_job(
            job_id,
            _job_payload(job_id, backup_id, guild.id, actor_id, BackupJobStatus.PENDING, started_at),
        )
        async with lock:
            await self.repository.save_job(
                job_id,
                _job_payload(job_id, backup_id, guild.id, actor_id, BackupJobStatus.RUNNING, started_at),
            )
            try:
                safety_metadata, _ = await self._collect_and_save(
                    guild,
                    actor_id=actor_id,
                    reason=f"Snapshot automático antes de restaurar {backup_id}",
                    automatic=True,
                )
                integration_failures: list[str] = []
                if self.security_scan_callback is not None:
                    try:
                        await self.security_scan_callback(guild, actor_id)
                    except Exception as exc:
                        integration_failures.append(
                            f"Scan de segurança anterior: {type(exc).__name__}: {exc}"
                        )
                result = await self._restore_payload(
                    guild,
                    payload,
                    backup_id=backup_id,
                    safety_backup_id=safety_metadata.backup_id,
                    job_id=job_id,
                    actor_id=actor_id,
                    started_at=started_at,
                )
                if self.security_scan_callback is not None:
                    try:
                        await self.security_scan_callback(guild, actor_id)
                    except Exception as exc:
                        integration_failures.append(
                            f"Scan de segurança posterior: {type(exc).__name__}: {exc}"
                        )
                if integration_failures:
                    result = replace(
                        result,
                        status=BackupJobStatus.PARTIAL,
                        failures=tuple([*result.failures, *integration_failures]),
                    )
            except Exception as exc:
                await self.repository.save_job(
                    job_id,
                    _job_payload(
                        job_id,
                        backup_id,
                        guild.id,
                        actor_id,
                        BackupJobStatus.FAILED,
                        started_at,
                        finished_at=_utc_now(),
                        error=f"{type(exc).__name__}: {exc}",
                    ),
                )
                await self._log_restore_failure(guild, backup_id, actor_id, exc)
                raise

        await self.repository.save_job(
            job_id,
            _job_payload(
                job_id,
                backup_id,
                guild.id,
                actor_id,
                result.status,
                started_at,
                finished_at=result.finished_at,
                details={
                    "safety_backup_id": result.safety_backup_id,
                    "created": list(result.created),
                    "reused": list(result.reused),
                    "failures": list(result.failures),
                    "role_map": {str(key): value for key, value in result.role_map.items()},
                    "category_map": {str(key): value for key, value in result.category_map.items()},
                    "channel_map": {str(key): value for key, value in result.channel_map.items()},
                },
            ),
        )
        await self._log_restore(guild, result, actor_id)
        return result

    async def _collect_and_save(
        self,
        guild: discord.Guild,
        *,
        actor_id: int,
        reason: str,
        automatic: bool,
    ) -> tuple[BackupMetadata, dict[str, Any]]:
        backup_id = uuid.uuid4().hex[:16]
        created_at = _utc_now()
        warnings: list[str] = []
        automod: list[dict[str, Any]] = []
        try:
            automod = [_serialize_automod_rule(rule) for rule in await guild.fetch_automod_rules()]
        except (discord.Forbidden, discord.HTTPException) as exc:
            warnings.append(f"AutoMod não coletado: {type(exc).__name__}")

        panels, panel_warnings = await self._collect_registered_panels(guild)
        warnings.extend(panel_warnings)
        panel_message_ids = {
            (int(item.get("channel_id", 0) or 0), int(item.get("message_id", 0) or 0))
            for item in panels
        }
        embeds, embed_warnings = await self._collect_bella_embed_messages(
            guild,
            excluded=panel_message_ids,
        )
        warnings.extend(embed_warnings)
        roles = [_serialize_role(role) for role in guild.roles]
        channels = [_serialize_channel(channel) for channel in guild.channels]
        payload = normalize_backup_payload(
            {
                "METADATA": {
                    "backup_schema_version": BACKUP_SCHEMA_VERSION,
                    "backup_id": backup_id,
                    "created_at": created_at,
                    "source_guild_id": guild.id,
                    "source_guild_name": guild.name,
                    "actor_id": actor_id,
                    "automatic": automatic,
                    "reason": reason,
                },
                "STRUCTURE": _serialize_guild(guild),
                "ROLES": roles,
                "CHANNELS": channels,
                "PERMISSIONS": {
                    str(item["id"]): item.get("overwrites", [])
                    for item in channels
                    if item.get("overwrites")
                },
                "AUTOMOD": automod,
                "BELLA_PANELS": panels,
                "EMBEDS": embeds,
            }
        )
        errors = validate_backup_schema(payload)
        if errors:
            raise ValueError("Backup inválido: " + "; ".join(errors))
        counts = {
            "roles": len(roles),
            "channels": len(channels),
            "automod": len(automod),
            "bella_panels": len(panels),
            "embeds": sum(len(item.get("embeds", [])) for item in embeds),
            "embed_messages": len(embeds),
        }
        metadata = BackupMetadata(
            backup_id=backup_id,
            guild_id=guild.id,
            guild_name=guild.name,
            actor_id=actor_id,
            created_at=created_at,
            status=BackupJobStatus.PARTIAL if warnings else BackupJobStatus.COMPLETED,
            schema_version=BACKUP_SCHEMA_VERSION,
            automatic=automatic,
            reason=reason,
            counts=counts,
            warnings=tuple(warnings),
        )
        await self.repository.save_backup(metadata, payload)
        return metadata, payload

    async def _collect_registered_panels(
        self,
        guild: discord.Guild,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        references = await _registered_panel_references(self.state, guild.id)
        panels: list[dict[str, Any]] = []
        warnings: list[str] = []
        seen: set[tuple[int, int]] = set()
        for reference in references:
            channel_id = int(reference.get("channel_id", 0) or 0)
            message_id = int(reference.get("message_id", 0) or 0)
            if not channel_id or not message_id or (channel_id, message_id) in seen:
                continue
            seen.add((channel_id, message_id))
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                warnings.append(f"Painel {message_id}: canal indisponível")
                continue
            try:
                message = await channel.fetch_message(message_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                warnings.append(f"Painel {message_id}: {type(exc).__name__}")
                continue
            if self.bot.user is None or message.author.id != self.bot.user.id:
                warnings.append(f"Mensagem {message_id} ignorada: não foi enviada pela Bella")
                continue
            panels.append(_serialize_panel_message(message, reference))
        return panels, warnings

    async def _collect_bella_embed_messages(
        self,
        guild: discord.Guild,
        *,
        excluded: set[tuple[int, int]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if self.bot.user is None:
            return [], ["Embeds não coletados: identidade da Bella indisponível"]
        collected: list[dict[str, Any]] = []
        warnings: list[str] = []
        scanned_total = 0
        for channel in guild.text_channels:
            if scanned_total >= EMBED_SCAN_TOTAL_MESSAGES:
                warnings.append(
                    f"Varredura de embeds interrompida após {EMBED_SCAN_TOTAL_MESSAGES} mensagens no servidor"
                )
                break
            if guild.me is not None:
                permissions = channel.permissions_for(guild.me)
                if not permissions.view_channel or not permissions.read_message_history:
                    warnings.append(f"Embeds em #{channel.name} não coletados: sem acesso ao histórico")
                    continue
            channel_limit = min(
                EMBED_SCAN_MESSAGES_PER_CHANNEL,
                EMBED_SCAN_TOTAL_MESSAGES - scanned_total,
            )
            scanned_channel = 0
            try:
                async for message in channel.history(limit=channel_limit):
                    scanned_channel += 1
                    if (
                        message.author.id != self.bot.user.id
                        or not message.embeds
                        or (channel.id, message.id) in excluded
                    ):
                        continue
                    collected.append(
                        _serialize_panel_message(
                            message,
                            {
                                "kind": "bella_embed",
                                "source_section": "bella_embed_history_scan",
                            },
                        )
                    )
            except (discord.Forbidden, discord.HTTPException) as exc:
                warnings.append(f"Embeds em #{channel.name}: {type(exc).__name__}")
                continue
            scanned_total += scanned_channel
            if scanned_channel >= channel_limit:
                warnings.append(
                    f"Varredura de #{channel.name} limitada às {channel_limit} mensagens mais recentes"
                )
        return collected, warnings

    async def _required_payload(self, backup_id: str, guild_id: int) -> dict[str, Any]:
        metadata = await self.repository.metadata(backup_id)
        if metadata is None:
            raise LookupError("Backup não encontrado.")
        if int(metadata.get("guild_id", 0) or 0) != guild_id:
            raise PermissionError("Esse backup pertence a outro servidor.")
        payload = await self.repository.payload(backup_id)
        if payload is None:
            raise LookupError("Os recursos desse backup não estão disponíveis.")
        errors = validate_backup_schema(payload)
        if errors:
            raise ValueError("Schema de backup inválido: " + "; ".join(errors))
        return payload

    async def _restore_payload(
        self,
        guild: discord.Guild,
        payload: dict[str, Any],
        *,
        backup_id: str,
        safety_backup_id: str,
        job_id: str,
        actor_id: int,
        started_at: str,
    ) -> RestoreResult:
        reason = f"Restauração Bella {backup_id} por {actor_id}"
        role_map: dict[int, int] = {int(payload["STRUCTURE"]["default_role_id"]): guild.default_role.id}
        category_map: dict[int, int] = {}
        channel_map: dict[int, int] = {}
        created: list[str] = []
        reused: list[str] = []
        failures: list[str] = []

        await self._restore_roles(guild, payload["ROLES"], role_map, created, reused, failures, reason)
        categories = [item for item in payload["CHANNELS"] if item.get("type") == "category"]
        await self._restore_categories(guild, categories, category_map, created, reused, failures, reason)
        channels = [item for item in payload["CHANNELS"] if item.get("type") != "category"]
        await self._restore_channels(
            guild,
            channels,
            category_map,
            channel_map,
            created,
            reused,
            failures,
            reason,
        )
        await self._restore_overwrites(
            guild,
            payload.get("PERMISSIONS", {}),
            role_map,
            category_map,
            channel_map,
            failures,
            reason,
        )
        await self._restore_guild_settings(guild, payload["STRUCTURE"], channel_map, failures, reason)
        await self._restore_automod(guild, payload["AUTOMOD"], role_map, channel_map, created, reused, failures, reason)
        await self._restore_panels(guild, payload["BELLA_PANELS"], channel_map, created, failures)
        await self._restore_embeds(
            guild,
            payload.get("EMBEDS", []),
            channel_map,
            backup_id,
            created,
            reused,
            failures,
            skip_message_ids={
                int(item.get("message_id", 0) or 0)
                for item in payload.get("BELLA_PANELS", [])
                if isinstance(item, dict)
            },
        )
        await _remap_internal_bella_config(self.state, guild.id, role_map, category_map, channel_map)
        if self.panel_refresh_callback is not None:
            try:
                await self.panel_refresh_callback()
            except Exception as exc:
                failures.append(f"Atualização dos painéis: {type(exc).__name__}: {exc}")

        finished_at = _utc_now()
        status = BackupJobStatus.PARTIAL if failures else BackupJobStatus.COMPLETED
        return RestoreResult(
            job_id=job_id,
            backup_id=backup_id,
            safety_backup_id=safety_backup_id,
            status=status,
            role_map=role_map,
            category_map=category_map,
            channel_map=channel_map,
            created=tuple(created),
            reused=tuple(reused),
            failures=tuple(failures),
            started_at=started_at,
            finished_at=finished_at,
        )

    async def _restore_roles(
        self,
        guild: discord.Guild,
        roles: list[dict[str, Any]],
        mapping: dict[int, int],
        created: list[str],
        reused: list[str],
        failures: list[str],
        reason: str,
    ) -> None:
        for item in sorted(roles, key=lambda value: int(value.get("position", 0))):
            old_id = int(item.get("id", 0) or 0)
            if not old_id or item.get("is_default"):
                continue
            if item.get("managed"):
                failures.append(f"Cargo gerenciado não restaurável: {item.get('name', old_id)}")
                continue
            role = discord.utils.find(lambda value: value.name == item.get("name") and not value.managed, guild.roles)
            if role is None:
                try:
                    role = await guild.create_role(
                        name=str(item.get("name") or "cargo-restaurado")[:100],
                        permissions=discord.Permissions(int(item.get("permissions", 0) or 0)),
                        colour=discord.Colour(int(item.get("color", 0) or 0)),
                        hoist=bool(item.get("hoist", False)),
                        mentionable=bool(item.get("mentionable", False)),
                        reason=reason,
                    )
                    created.append(f"role:{role.id}")
                except (discord.Forbidden, discord.HTTPException) as exc:
                    failures.append(f"Cargo {item.get('name')}: {type(exc).__name__}")
                    continue
            else:
                reused.append(f"role:{role.id}")
            mapping[old_id] = role.id

    async def _restore_categories(
        self,
        guild: discord.Guild,
        categories: list[dict[str, Any]],
        mapping: dict[int, int],
        created: list[str],
        reused: list[str],
        failures: list[str],
        reason: str,
    ) -> None:
        for item in sorted(categories, key=lambda value: int(value.get("position", 0))):
            old_id = int(item.get("id", 0) or 0)
            category = discord.utils.find(lambda value: value.name == item.get("name"), guild.categories)
            if category is None:
                try:
                    category = await guild.create_category(str(item.get("name") or "categoria")[:100], reason=reason)
                    created.append(f"category:{category.id}")
                except (discord.Forbidden, discord.HTTPException) as exc:
                    failures.append(f"Categoria {item.get('name')}: {type(exc).__name__}")
                    continue
            else:
                reused.append(f"category:{category.id}")
            mapping[old_id] = category.id

    async def _restore_channels(
        self,
        guild: discord.Guild,
        channels: list[dict[str, Any]],
        category_map: dict[int, int],
        channel_map: dict[int, int],
        created: list[str],
        reused: list[str],
        failures: list[str],
        reason: str,
    ) -> None:
        for item in sorted(channels, key=lambda value: int(value.get("position", 0))):
            old_id = int(item.get("id", 0) or 0)
            old_category_id = int(item.get("category_id", 0) or 0)
            category = guild.get_channel(category_map.get(old_category_id, 0))
            category = category if isinstance(category, discord.CategoryChannel) else None
            expected_type = str(item.get("type") or "text")
            existing = _find_channel(guild, str(item.get("name") or ""), expected_type, category)
            if existing is None:
                try:
                    existing = await _create_channel(guild, item, category=category, reason=reason)
                    created.append(f"channel:{existing.id}")
                except (ValueError, discord.Forbidden, discord.HTTPException) as exc:
                    failures.append(f"Canal {item.get('name')}: {type(exc).__name__}: {exc}")
                    continue
            else:
                reused.append(f"channel:{existing.id}")
            channel_map[old_id] = existing.id

    async def _restore_overwrites(
        self,
        guild: discord.Guild,
        permissions: dict[str, Any],
        role_map: dict[int, int],
        category_map: dict[int, int],
        channel_map: dict[int, int],
        failures: list[str],
        reason: str,
    ) -> None:
        all_channels = {**category_map, **channel_map}
        for old_channel_id, raw_overwrites in permissions.items():
            channel = guild.get_channel(all_channels.get(int(old_channel_id), 0))
            if not isinstance(channel, discord.abc.GuildChannel):
                continue
            mapped, skipped = remap_permission_overwrites(raw_overwrites, role_map, guild)
            failures.extend(f"Overwrite ignorado em {channel.name}: {item}" for item in skipped)
            for target, overwrite in mapped.items():
                try:
                    await channel.set_permissions(target, overwrite=overwrite, reason=reason)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    failures.append(f"Overwrite {channel.name}/{target.id}: {type(exc).__name__}")

    async def _restore_guild_settings(
        self,
        guild: discord.Guild,
        structure: dict[str, Any],
        channel_map: dict[int, int],
        failures: list[str],
        reason: str,
    ) -> None:
        kwargs: dict[str, Any] = {
            "name": str(structure.get("name") or guild.name)[:100],
            "verification_level": discord.VerificationLevel(int(structure.get("verification_level", 0) or 0)),
            "explicit_content_filter": discord.ContentFilter(int(structure.get("explicit_content_filter", 0) or 0)),
            "default_notifications": discord.NotificationLevel(int(structure.get("default_notifications", 0) or 0)),
            "afk_timeout": int(structure.get("afk_timeout", guild.afk_timeout) or guild.afk_timeout),
        }
        for key, expected_type in (
            ("afk_channel_id", (discord.VoiceChannel, discord.StageChannel)),
            ("system_channel_id", discord.TextChannel),
            ("rules_channel_id", discord.TextChannel),
            ("public_updates_channel_id", discord.TextChannel),
        ):
            old_id = int(structure.get(key, 0) or 0)
            channel = guild.get_channel(channel_map.get(old_id, 0))
            if isinstance(channel, expected_type):
                kwargs[key.removesuffix("_id")] = channel
        try:
            await guild.edit(reason=reason, **kwargs)
        except (ValueError, discord.Forbidden, discord.HTTPException) as exc:
            failures.append(f"Configurações do servidor: {type(exc).__name__}: {exc}")

    async def _restore_automod(
        self,
        guild: discord.Guild,
        rules: list[dict[str, Any]],
        role_map: dict[int, int],
        channel_map: dict[int, int],
        created: list[str],
        reused: list[str],
        failures: list[str],
        reason: str,
    ) -> None:
        try:
            existing = {rule.name: rule for rule in await guild.fetch_automod_rules()}
        except (discord.Forbidden, discord.HTTPException) as exc:
            failures.append(f"AutoMod indisponível: {type(exc).__name__}")
            return
        for raw in rules:
            try:
                mapped = remap_automod_rule(raw, role_map, channel_map, guild)
                current = existing.get(mapped["name"])
                if current is None:
                    current = await guild.create_automod_rule(reason=reason, **mapped)
                    created.append(f"automod:{current.id}")
                else:
                    await current.edit(reason=reason, **mapped)
                    reused.append(f"automod:{current.id}")
            except (ValueError, discord.Forbidden, discord.HTTPException) as exc:
                failures.append(f"AutoMod {raw.get('name')}: {type(exc).__name__}: {exc}")

    async def _restore_panels(
        self,
        guild: discord.Guild,
        panels: list[dict[str, Any]],
        channel_map: dict[int, int],
        created: list[str],
        failures: list[str],
    ) -> None:
        for item in panels:
            old_channel_id = int(item.get("channel_id", 0) or 0)
            channel = guild.get_channel(channel_map.get(old_channel_id, 0))
            if not isinstance(channel, discord.TextChannel):
                failures.append(f"Painel {item.get('message_id')}: canal não mapeado")
                continue
            embeds: list[discord.Embed] = []
            for value in item.get("embeds", [])[:10]:
                if isinstance(value, dict):
                    try:
                        embeds.append(discord.Embed.from_dict(value))
                    except Exception:
                        continue
            try:
                message = await channel.send(
                    str(item.get("content") or "")[:2000] or None,
                    embeds=embeds,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                created.append(f"panel:{message.id}")
                await _save_restored_panel_reference(self.state, guild.id, item, channel.id, message.id)
                if item.get("components"):
                    failures.append(
                        f"Painel {item.get('message_id')}: componentes preservados no backup, "
                        "mas aguardam republicação pelo módulo"
                    )
            except (discord.Forbidden, discord.HTTPException) as exc:
                failures.append(f"Painel {item.get('message_id')}: {type(exc).__name__}")

    async def _restore_embeds(
        self,
        guild: discord.Guild,
        messages: list[dict[str, Any]],
        channel_map: dict[int, int],
        backup_id: str,
        created: list[str],
        reused: list[str],
        failures: list[str],
        *,
        skip_message_ids: set[int],
    ) -> None:
        restored = await self.state.get_section(RESTORED_MESSAGES_SECTION)
        for item in messages:
            old_message_id = int(item.get("message_id", 0) or 0)
            if not old_message_id or old_message_id in skip_message_ids:
                continue
            mapping_key = f"{guild.id}:{backup_id}:{old_message_id}"
            previous = restored.get(mapping_key)
            if isinstance(previous, dict):
                previous_channel = guild.get_channel(int(previous.get("channel_id", 0) or 0))
                if isinstance(previous_channel, discord.TextChannel):
                    try:
                        await previous_channel.fetch_message(int(previous.get("message_id", 0) or 0))
                        reused.append(f"embed:{previous.get('message_id')}")
                        continue
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
            old_channel_id = int(item.get("channel_id", 0) or 0)
            channel = guild.get_channel(channel_map.get(old_channel_id, 0))
            if not isinstance(channel, discord.TextChannel):
                failures.append(f"Embed {old_message_id}: canal não mapeado")
                continue
            embeds = _deserialize_embeds(item.get("embeds", []))
            if not embeds:
                failures.append(f"Embed {old_message_id}: conteúdo de embed inválido")
                continue
            try:
                message = await channel.send(
                    str(item.get("content") or "")[:2000] or None,
                    embeds=embeds,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                created.append(f"embed:{message.id}")
                await self.state.update_section(
                    RESTORED_MESSAGES_SECTION,
                    mapping_key,
                    {
                        "guild_id": guild.id,
                        "backup_id": backup_id,
                        "source_channel_id": old_channel_id,
                        "source_message_id": old_message_id,
                        "channel_id": channel.id,
                        "message_id": message.id,
                        "restored_at": _utc_now(),
                    },
                )
                if item.get("components"):
                    failures.append(
                        f"Embed {old_message_id}: componentes interativos não foram recriados"
                    )
            except (discord.Forbidden, discord.HTTPException) as exc:
                failures.append(f"Embed {old_message_id}: {type(exc).__name__}")

    async def _log_backup(self, guild: discord.Guild, metadata: BackupMetadata) -> None:
        if self.logs is None:
            return
        await self.logs.send_event(
            guild,
            "backups",
            title="Backup estrutural criado",
            description=f"Backup `{metadata.backup_id}` concluído como **{metadata.status.value}**.",
            severity="MEDIUM" if metadata.warnings else "INFO",
            fields=[(key.title(), str(value), True) for key, value in metadata.counts.items()],
            critical=False,
        )

    async def _log_restore(self, guild: discord.Guild, result: RestoreResult, actor_id: int) -> None:
        if self.logs is None:
            return
        await self.logs.send_event(
            guild,
            "backups",
            title="Restauração estrutural concluída",
            description=(
                f"Backup `{result.backup_id}` restaurado por <@{actor_id}> como "
                f"**{result.status.value}**. Snapshot de segurança: `{result.safety_backup_id}`."
            ),
            severity="HIGH" if result.failures else "INFO",
            fields=[
                ("Criados", str(len(result.created)), True),
                ("Reutilizados", str(len(result.reused)), True),
                ("Falhas", str(len(result.failures)), True),
            ],
            critical=bool(result.failures),
        )

    async def _log_restore_failure(
        self,
        guild: discord.Guild,
        backup_id: str,
        actor_id: int,
        error: Exception,
    ) -> None:
        if self.logs is None:
            return
        await self.logs.send_event(
            guild,
            "backups",
            title="Restauração falhou",
            description=f"Backup `{backup_id}` por <@{actor_id}>: {type(error).__name__}: {error}",
            severity="CRITICAL",
            critical=True,
        )


class BackupRestoreConfirmView(discord.ui.LayoutView):
    def __init__(
        self,
        service: ServerBackupService,
        guild: discord.Guild,
        backup_id: str,
        owner_id: int,
        authorize: Callable[[discord.Member], bool],
        plan: RestorePlan,
    ) -> None:
        super().__init__(timeout=600)
        self.service = service
        self.guild = guild
        self.backup_id = backup_id
        self.owner_id = owner_id
        self.authorize = authorize
        self.plan = plan
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = (
            interaction.user.id == self.owner_id
            and isinstance(interaction.user, discord.Member)
            and self.authorize(interaction.user)
        )
        if not allowed:
            await interaction.response.send_message("Esta confirmação pertence a outro administrador.", ephemeral=True)
        return allowed

    def _render(self) -> None:
        self.clear_items()
        self.add_item(discord.ui.TextDisplay("# Confirmar restauração\nEsta operação não apaga recursos, mas pode criar e atualizar configurações."))
        self.add_item(
            discord.ui.TextDisplay(
                f"**Backup:** `{self.backup_id}`\n"
                f"**Cargos:** +{self.plan.create_roles} / {self.plan.reuse_roles} reutilizados\n"
                f"**Categorias:** +{self.plan.create_categories} / {self.plan.reuse_categories} reutilizadas\n"
                f"**Canais:** +{self.plan.create_channels} / {self.plan.reuse_channels} reutilizados\n"
                f"**Overwrites:** {self.plan.permission_overwrites}\n"
                f"**AutoMod:** +{self.plan.automod_create} / {self.plan.automod_update} atualizados\n"
                f"**Painéis Bella:** {self.plan.bella_panels}\n"
                f"**Mensagens com embeds da Bella:** {self.plan.bella_embeds}\n\n"
                "Um snapshot automático será criado antes da execução."
            )
        )
        if self.plan.unsupported:
            self.add_item(
                discord.ui.TextDisplay(
                    "**Limitações detectadas**\n" + "\n".join(f"- {item}" for item in self.plan.unsupported)
                )
            )
        row = discord.ui.ActionRow()
        confirm = discord.ui.Button(label="Continuar para confirmação final", style=discord.ButtonStyle.danger)
        cancel = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        confirm.callback = lambda interaction: interaction.response.send_modal(FinalRestoreModal(self))
        cancel.callback = self._cancel
        row.add_item(confirm)
        row.add_item(cancel)
        self.add_item(row)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        for child in self.walk_children():
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.edit_message(view=self)


class FinalRestoreModal(discord.ui.Modal, title="Confirmação final da restauração"):
    def __init__(self, view: BackupRestoreConfirmView) -> None:
        super().__init__()
        self.view = view
        self.confirmation = discord.ui.TextInput(
            label="Digite RESTAURAR",
            placeholder="RESTAURAR",
            min_length=9,
            max_length=9,
        )
        self.add_item(self.confirmation)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.view.service.restore_backup(
                self.view.guild,
                self.view.backup_id,
                actor_id=interaction.user.id,
                confirmation_phrase=str(self.confirmation.value),
            )
        except Exception as exc:
            await interaction.followup.send(f"A restauração falhou: {type(exc).__name__}: {exc}", ephemeral=True)
            return
        await interaction.followup.send(embed=restore_result_embed(result), ephemeral=True)


BackupAuthorizationCallback = Callable[[discord.Member], bool]


class BackupAdminPanelView(discord.ui.View):
    def __init__(
        self,
        service: ServerBackupService,
        authorize: BackupAuthorizationCallback,
    ) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.authorize = authorize

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = isinstance(interaction.user, discord.Member) and self.authorize(interaction.user)
        if not allowed:
            await interaction.response.send_message(
                "Você não possui acesso à Central de Backups.",
                ephemeral=True,
            )
        return allowed

    @discord.ui.button(
        label="Criar backup",
        style=discord.ButtonStyle.success,
        custom_id="bella:backups:create:v1",
        row=0,
    )
    async def create(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(BackupCreateModal(self.service))

    @discord.ui.button(
        label="Backups salvos",
        style=discord.ButtonStyle.primary,
        custom_id="bella:backups:list:v1",
        row=0,
    )
    async def list_backups(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        items = await self.service.repository.list_for_guild(interaction.guild.id)
        await interaction.response.send_message(
            embed=backup_list_embed(items),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Visualizar / restaurar",
        style=discord.ButtonStyle.danger,
        custom_id="bella:backups:restore:v1",
        row=0,
    )
    async def restore(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        items = await self.service.repository.list_for_guild(interaction.guild.id)
        if not items:
            await interaction.response.send_message("Ainda não existe backup neste servidor.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Escolha um backup para visualizar, analisar ou restaurar:",
            view=BackupPickerView(self.service, items, interaction.user.id, self.authorize),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Jobs de restauração",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:backups:jobs:v1",
        row=1,
    )
    async def jobs(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        values = await self.service.repository.list_jobs_for_guild(interaction.guild.id)
        lines = [
            f"`{item.get('job_id', '?')}` • **{item.get('status', '?')}** • backup `{item.get('backup_id', '?')}`"
            for item in values
        ]
        embed = discord.Embed(
            title="Jobs de restauração",
            description="\n".join(lines)[:4000] or "Nenhum job de restauração registrado.",
            color=discord.Color.from_rgb(110, 118, 129),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Atualizar painel",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:backups:refresh:v1",
        row=1,
    )
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.message is None:
            await interaction.response.send_message("Painel atualizado.", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=backup_panel_embed(),
            view=BackupAdminPanelView(self.service, self.authorize),
        )


class BackupCreateModal(discord.ui.Modal, title="Criar backup estrutural"):
    reason = discord.ui.TextInput(
        label="Motivo do backup",
        placeholder="Ex.: antes de reorganizar os canais",
        default="Backup manual criado pelo painel da Bella",
        required=False,
        max_length=300,
    )

    def __init__(self, service: ServerBackupService) -> None:
        super().__init__()
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            metadata = await self.service.create_backup(
                interaction.guild,
                actor_id=interaction.user.id,
                reason=str(self.reason.value or "Backup manual criado pelo painel da Bella"),
            )
        except Exception as exc:
            await interaction.followup.send(
                f"O backup falhou: {type(exc).__name__}: {exc}",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=backup_metadata_embed(metadata.to_dict()),
            ephemeral=True,
        )


class BackupPickerView(discord.ui.View):
    def __init__(
        self,
        service: ServerBackupService,
        backups: list[dict[str, Any]],
        owner_id: int,
        authorize: BackupAuthorizationCallback,
    ) -> None:
        super().__init__(timeout=600)
        self.service = service
        self.backups = backups
        self.owner_id = owner_id
        self.authorize = authorize
        select = discord.ui.Select(
            placeholder="Selecione um backup",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=str(item.get("backup_id", "backup"))[:100],
                    value=str(item.get("backup_id")),
                    description=f"{item.get('status', '?')} • {item.get('created_at', '—')}"[:100],
                )
                for item in backups[:25]
            ],
        )
        select.callback = self._selected
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = interaction.user.id == self.owner_id
        if not allowed:
            await interaction.response.send_message("Somente quem abriu esta lista pode usá-la.", ephemeral=True)
        return allowed

    async def _selected(self, interaction: discord.Interaction) -> None:
        select = self.children[0]
        backup_id = select.values[0] if isinstance(select, discord.ui.Select) else ""
        await interaction.response.send_message(
            f"Backup `{backup_id}` selecionado. Escolha uma ação:",
            view=BackupSelectionView(self.service, backup_id, self.owner_id, self.authorize),
            ephemeral=True,
        )


class BackupSelectionView(discord.ui.View):
    def __init__(
        self,
        service: ServerBackupService,
        backup_id: str,
        owner_id: int,
        authorize: BackupAuthorizationCallback,
    ) -> None:
        super().__init__(timeout=600)
        self.service = service
        self.backup_id = backup_id
        self.owner_id = owner_id
        self.authorize = authorize

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = interaction.user.id == self.owner_id
        if not allowed:
            await interaction.response.send_message("Somente quem abriu esta ação pode usá-la.", ephemeral=True)
        return allowed

    @discord.ui.button(label="Detalhes", style=discord.ButtonStyle.secondary)
    async def details(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        metadata = await self.service.repository.metadata(self.backup_id)
        if metadata is None:
            await interaction.response.send_message("Backup não encontrado.", ephemeral=True)
            return
        await interaction.response.send_message(embed=backup_metadata_embed(metadata), ephemeral=True)

    @discord.ui.button(label="Analisar restauração", style=discord.ButtonStyle.primary)
    async def preview(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        try:
            plan = await self.service.preview_restore(interaction.guild, self.backup_id)
        except Exception as exc:
            await interaction.response.send_message(f"Não consegui analisar: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(embed=restore_plan_embed(plan), ephemeral=True)

    @discord.ui.button(label="Restaurar com confirmação", style=discord.ButtonStyle.danger)
    async def restore(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        try:
            plan = await self.service.preview_restore(interaction.guild, self.backup_id)
        except Exception as exc:
            await interaction.response.send_message(f"Não consegui preparar a restauração: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=restore_plan_embed(plan),
            view=BackupRestoreConfirmView(
                self.service,
                interaction.guild,
                self.backup_id,
                interaction.user.id,
                self.authorize,
                plan,
            ),
            ephemeral=True,
        )


def backup_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Central de Backups da Bella",
        description=(
            "Backup estrutural seguro do servidor. O sistema preserva configurações, cargos, "
            "canais, permissões, AutoMod, painéis Bella e embeds autorizados. Conversas comuns "
            "não entram no backup."
        ),
        color=discord.Color.from_rgb(255, 80, 120),
    )
    embed.add_field(
        name="Criar backup",
        value="Coleta uma fotografia versionada e registra avisos de limitações da API.",
        inline=False,
    )
    embed.add_field(
        name="Visualizar / restaurar",
        value="Escolha um backup, veja os recursos, analise conflitos e só então confirme a restauração.",
        inline=False,
    )
    embed.add_field(
        name="Segurança",
        value="A restauração cria snapshot automático, exige confirmação dupla e pode terminar como parcial.",
        inline=False,
    )
    embed.set_footer(text="Bella Backups • use /logs-sincronizar para provisionar este painel")
    return embed


def normalize_backup_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
    for key in ("ROLES", "CHANNELS", "AUTOMOD", "BELLA_PANELS", "EMBEDS"):
        values = data.get(key, [])
        if isinstance(values, list):
            identity = "message_id" if key in {"BELLA_PANELS", "EMBEDS"} else "id"
            data[key] = sorted((_normalize(item) for item in values), key=lambda item: str(item.get(identity, "")))
    data["PERMISSIONS"] = _normalize(data.get("PERMISSIONS", {}))
    data["STRUCTURE"] = _normalize(data.get("STRUCTURE", {}))
    data["METADATA"] = _normalize(data.get("METADATA", {}))
    return data


def validate_backup_schema(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = {"STRUCTURE", "CHANNELS", "ROLES", "PERMISSIONS", "AUTOMOD", "BELLA_PANELS", "EMBEDS", "METADATA"}
    missing = sorted(required - set(payload))
    if missing:
        errors.append("seções ausentes: " + ", ".join(missing))
    metadata = payload.get("METADATA")
    if not isinstance(metadata, dict):
        errors.append("METADATA deve ser um objeto")
    elif not MIN_SUPPORTED_BACKUP_SCHEMA_VERSION <= int(
        metadata.get("backup_schema_version", 0) or 0
    ) <= BACKUP_SCHEMA_VERSION:
        errors.append("versão de schema não suportada")
    for key in ("CHANNELS", "ROLES", "AUTOMOD", "BELLA_PANELS", "EMBEDS"):
        if key in payload and not isinstance(payload[key], list):
            errors.append(f"{key} deve ser uma lista")
    if "PERMISSIONS" in payload and not isinstance(payload["PERMISSIONS"], dict):
        errors.append("PERMISSIONS deve ser um objeto")
    return errors


def build_restore_plan(guild: discord.Guild, payload: dict[str, Any], *, backup_id: str) -> RestorePlan:
    role_names = {role.name for role in guild.roles if not role.managed}
    categories = {category.name for category in guild.categories}
    channel_keys = {
        (channel.name, _channel_type_name(channel), channel.category.name if channel.category else "")
        for channel in guild.channels
        if not isinstance(channel, discord.CategoryChannel)
    }
    create_roles = reuse_roles = skip_managed = 0
    for role in payload.get("ROLES", []):
        if role.get("is_default"):
            continue
        if role.get("managed"):
            skip_managed += 1
        elif role.get("name") in role_names:
            reuse_roles += 1
        else:
            create_roles += 1
    create_categories = sum(1 for item in payload.get("CHANNELS", []) if item.get("type") == "category" and item.get("name") not in categories)
    reuse_categories = sum(1 for item in payload.get("CHANNELS", []) if item.get("type") == "category" and item.get("name") in categories)
    category_names = {int(item.get("id", 0)): str(item.get("name", "")) for item in payload.get("CHANNELS", []) if item.get("type") == "category"}
    create_channels = reuse_channels = 0
    for item in payload.get("CHANNELS", []):
        if item.get("type") == "category":
            continue
        key = (str(item.get("name", "")), str(item.get("type", "text")), category_names.get(int(item.get("category_id", 0) or 0), ""))
        if key in channel_keys:
            reuse_channels += 1
        else:
            create_channels += 1
    existing_automod_names = set()
    cached_rules = getattr(guild, "auto_moderation_rules", ()) or ()
    existing_automod_names.update(rule.name for rule in cached_rules)
    automod_update = sum(1 for item in payload.get("AUTOMOD", []) if item.get("name") in existing_automod_names)
    automod_create = len(payload.get("AUTOMOD", [])) - automod_update
    unsupported = []
    if any(item.get("components") for item in payload.get("BELLA_PANELS", [])):
        unsupported.append("Components interativos dependem da republicação pelo módulo da Bella.")
    if any(item.get("components") for item in payload.get("EMBEDS", [])):
        unsupported.append("Componentes de mensagens com embed não são recriados sem o módulo original.")
    if any(item.get("attachments") for item in payload.get("EMBEDS", [])):
        unsupported.append("Anexos externos são referenciados, mas não são baixados para o backup.")
    if skip_managed:
        unsupported.append("Cargos gerenciados por bots/integrações não podem ser recriados.")
    return RestorePlan(
        backup_id=backup_id,
        guild_id=guild.id,
        create_roles=create_roles,
        reuse_roles=reuse_roles,
        skip_managed_roles=skip_managed,
        create_categories=create_categories,
        reuse_categories=reuse_categories,
        create_channels=create_channels,
        reuse_channels=reuse_channels,
        permission_overwrites=sum(len(value) for value in payload.get("PERMISSIONS", {}).values() if isinstance(value, list)),
        automod_create=automod_create,
        automod_update=automod_update,
        bella_panels=len(payload.get("BELLA_PANELS", [])),
        bella_embeds=len(payload.get("EMBEDS", [])),
        unsupported=tuple(unsupported),
    )


def remap_permission_overwrites(
    raw_overwrites: Any,
    role_map: dict[int, int],
    guild: discord.Guild,
) -> tuple[dict[discord.Role | discord.Member, discord.PermissionOverwrite], list[str]]:
    result: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {}
    skipped: list[str] = []
    for item in raw_overwrites if isinstance(raw_overwrites, list) else []:
        if not isinstance(item, dict):
            continue
        old_id = int(item.get("target_id", 0) or 0)
        if item.get("target_type") == "role":
            target = guild.get_role(role_map.get(old_id, 0))
        else:
            target = guild.get_member(old_id)
        if target is None:
            skipped.append(f"{item.get('target_type')}:{old_id}")
            continue
        result[target] = discord.PermissionOverwrite.from_pair(
            discord.Permissions(int(item.get("allow", 0) or 0)),
            discord.Permissions(int(item.get("deny", 0) or 0)),
        )
    return result, skipped


def remap_automod_rule(
    raw: dict[str, Any],
    role_map: dict[int, int],
    channel_map: dict[int, int],
    guild: discord.Guild,
) -> dict[str, Any]:
    trigger_type = discord.AutoModRuleTriggerType(int(raw.get("trigger_type", 1) or 1))
    metadata = raw.get("trigger_metadata", {}) if isinstance(raw.get("trigger_metadata"), dict) else {}
    trigger = discord.AutoModTrigger(
        type=trigger_type,
        keyword_filter=list(metadata.get("keyword_filter") or []) or None,
        presets=(
            discord.AutoModPresets._from_value(
                [int(value) for value in metadata.get("presets", [])]
            )
            if isinstance(metadata.get("presets"), list)
            else None
        ),
        allow_list=list(metadata.get("allow_list") or []) or None,
        mention_limit=metadata.get("mention_total_limit"),
        regex_patterns=list(metadata.get("regex_patterns") or []) or None,
        mention_raid_protection=metadata.get("mention_raid_protection_enabled"),
    )
    actions: list[discord.AutoModRuleAction] = []
    for action in raw.get("actions", []):
        if not isinstance(action, dict):
            continue
        action_type = discord.AutoModRuleActionType(int(action.get("type", 1) or 1))
        action_meta = action.get("metadata", {}) if isinstance(action.get("metadata"), dict) else {}
        old_channel_id = int(action_meta.get("channel_id", 0) or 0)
        duration_seconds = int(action_meta.get("duration_seconds", 0) or 0)
        actions.append(
            discord.AutoModRuleAction(
                type=action_type,
                channel_id=channel_map.get(old_channel_id) if old_channel_id else None,
                duration=timedelta(seconds=duration_seconds) if duration_seconds else None,
                custom_message=action_meta.get("custom_message"),
            )
        )
    exempt_roles = [guild.get_role(role_map.get(int(old_id), 0)) for old_id in raw.get("exempt_role_ids", [])]
    exempt_channels = [guild.get_channel(channel_map.get(int(old_id), 0)) for old_id in raw.get("exempt_channel_ids", [])]
    return {
        "name": str(raw.get("name") or "Regra restaurada")[:100],
        "event_type": discord.AutoModRuleEventType(int(raw.get("event_type", 1) or 1)),
        "trigger": trigger,
        "actions": actions,
        "enabled": bool(raw.get("enabled", False)),
        "exempt_roles": [item for item in exempt_roles if item is not None],
        "exempt_channels": [item for item in exempt_channels if item is not None],
    }


def backup_metadata_embed(metadata: dict[str, Any]) -> discord.Embed:
    counts = metadata.get("counts", {}) if isinstance(metadata.get("counts"), dict) else {}
    embed = discord.Embed(
        title=f"Backup estrutural • {metadata.get('backup_id', '?')}",
        description=(
            f"**Servidor:** {metadata.get('guild_name', '—')}\n"
            f"**Data:** {metadata.get('created_at', '—')}\n"
            f"**Status:** {metadata.get('status', '—')}\n"
            f"**Schema:** {metadata.get('schema_version', '—')}"
        ),
        color=discord.Color.from_rgb(255, 80, 120),
    )
    for key in ("roles", "channels", "automod", "bella_panels", "embed_messages", "embeds"):
        embed.add_field(name=key.replace("_", " ").title(), value=str(counts.get(key, 0)), inline=True)
    warnings = metadata.get("warnings", [])
    if warnings:
        embed.add_field(name="Limitações da coleta", value="\n".join(str(item) for item in warnings)[:1024], inline=False)
    return embed


def backup_list_embed(items: Iterable[dict[str, Any]]) -> discord.Embed:
    lines = [
        f"`{item.get('backup_id', '?')}` • **{item.get('status', '?')}** • {item.get('created_at', '—')}"
        for item in items
    ]
    return discord.Embed(
        title="Backups estruturais do servidor",
        description="\n".join(lines)[:4000] or "Nenhum backup criado.",
        color=discord.Color.from_rgb(24, 119, 242),
    )


def restore_plan_embed(plan: RestorePlan) -> discord.Embed:
    embed = discord.Embed(
        title="Prévia de restauração",
        description=f"Backup `{plan.backup_id}`. Nenhuma alteração foi aplicada nesta etapa.",
        color=discord.Color.from_rgb(210, 153, 34),
    )
    embed.add_field(name="Cargos", value=f"Criar {plan.create_roles}\nReutilizar {plan.reuse_roles}\nGerenciados {plan.skip_managed_roles}", inline=True)
    embed.add_field(name="Categorias", value=f"Criar {plan.create_categories}\nReutilizar {plan.reuse_categories}", inline=True)
    embed.add_field(name="Canais", value=f"Criar {plan.create_channels}\nReutilizar {plan.reuse_channels}", inline=True)
    embed.add_field(name="Permissões", value=str(plan.permission_overwrites), inline=True)
    embed.add_field(name="AutoMod", value=f"Criar {plan.automod_create}\nAtualizar {plan.automod_update}", inline=True)
    embed.add_field(name="Painéis Bella", value=str(plan.bella_panels), inline=True)
    embed.add_field(name="Mensagens com embeds", value=str(plan.bella_embeds), inline=True)
    if plan.unsupported:
        embed.add_field(name="Limitações", value="\n".join(plan.unsupported)[:1024], inline=False)
    return embed


def restore_result_embed(result: RestoreResult) -> discord.Embed:
    embed = discord.Embed(
        title="Restauração estrutural finalizada",
        description=(
            f"**Status:** {result.status.value}\n"
            f"**Job:** `{result.job_id}`\n"
            f"**Backup restaurado:** `{result.backup_id}`\n"
            f"**Snapshot anterior:** `{result.safety_backup_id}`"
        ),
        color=discord.Color.from_rgb(210, 153, 34) if result.failures else discord.Color.from_rgb(46, 160, 67),
    )
    embed.add_field(name="Criados", value=str(len(result.created)), inline=True)
    embed.add_field(name="Reutilizados", value=str(len(result.reused)), inline=True)
    embed.add_field(name="Falhas", value=str(len(result.failures)), inline=True)
    if result.failures:
        embed.add_field(name="Resultado parcial", value="\n".join(result.failures[:10])[:1024], inline=False)
    return embed


def _serialize_guild(guild: discord.Guild) -> dict[str, Any]:
    return {
        "id": guild.id,
        "name": guild.name,
        "description": guild.description,
        "owner_id": guild.owner_id,
        "default_role_id": guild.default_role.id,
        "verification_level": int(guild.verification_level.value),
        "explicit_content_filter": int(guild.explicit_content_filter.value),
        "default_notifications": int(guild.default_notifications.value),
        "mfa_level": int(guild.mfa_level.value),
        "preferred_locale": str(guild.preferred_locale),
        "afk_channel_id": guild.afk_channel.id if guild.afk_channel else None,
        "afk_timeout": guild.afk_timeout,
        "system_channel_id": guild.system_channel.id if guild.system_channel else None,
        "rules_channel_id": guild.rules_channel.id if guild.rules_channel else None,
        "public_updates_channel_id": guild.public_updates_channel.id if guild.public_updates_channel else None,
        "system_channel_flags": guild.system_channel_flags.value,
        "features": sorted(guild.features),
    }


def _serialize_role(role: discord.Role) -> dict[str, Any]:
    return {
        "id": role.id,
        "name": role.name,
        "permissions": role.permissions.value,
        "position": role.position,
        "color": role.color.value,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "managed": role.managed,
        "is_default": role.is_default(),
        "tags": {
            "bot_id": getattr(role.tags, "bot_id", None) if role.tags else None,
            "integration_id": getattr(role.tags, "integration_id", None) if role.tags else None,
        },
    }


def _serialize_channel(channel: discord.abc.GuildChannel) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": channel.id,
        "name": channel.name,
        "type": _channel_type_name(channel),
        "position": channel.position,
        "category_id": getattr(channel, "category_id", None),
        "overwrites": _serialize_overwrites(channel),
    }
    for attr in (
        "topic",
        "nsfw",
        "slowmode_delay",
        "default_auto_archive_duration",
        "default_thread_slowmode_delay",
        "bitrate",
        "user_limit",
    ):
        if hasattr(channel, attr):
            result[attr] = getattr(channel, attr)
    rtc_region = getattr(channel, "rtc_region", None)
    if rtc_region is not None:
        result["rtc_region"] = str(rtc_region)
    video_quality_mode = getattr(channel, "video_quality_mode", None)
    if video_quality_mode is not None:
        result["video_quality_mode"] = int(video_quality_mode.value)
    if isinstance(channel, discord.ForumChannel):
        result["default_sort_order"] = int(channel.default_sort_order.value) if channel.default_sort_order else None
        result["default_layout"] = int(channel.default_layout.value)
        result["available_tags"] = [
            {
                "name": tag.name,
                "moderated": tag.moderated,
                "emoji": str(tag.emoji) if tag.emoji else None,
            }
            for tag in channel.available_tags
        ]
    return result


def _serialize_overwrites(channel: discord.abc.GuildChannel) -> list[dict[str, Any]]:
    values = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        values.append(
            {
                "target_id": target.id,
                "target_type": "role" if isinstance(target, discord.Role) else "member",
                "allow": allow.value,
                "deny": deny.value,
            }
        )
    return sorted(values, key=lambda item: (item["target_type"], item["target_id"]))


def _serialize_automod_rule(rule: discord.AutoModRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "name": rule.name,
        "enabled": rule.enabled,
        "event_type": int(rule.event_type.value),
        "trigger_type": int(rule.trigger.type.value) if rule.trigger.type else None,
        "trigger_metadata": rule.trigger.to_metadata_dict(),
        "actions": [action.to_dict() for action in rule.actions],
        "exempt_role_ids": sorted(int(value) for value in rule.exempt_role_ids),
        "exempt_channel_ids": sorted(int(value) for value in rule.exempt_channel_ids),
    }


def _serialize_panel_message(message: discord.Message, reference: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": str(reference.get("kind") or "registered"),
        "source_section": str(reference.get("source_section") or ""),
        "channel_id": message.channel.id,
        "message_id": message.id,
        "content": message.content,
        "embeds": [embed.to_dict() for embed in message.embeds],
        "components": [component.to_dict() for component in message.components],
        "attachments": [
            {
                "id": attachment.id,
                "filename": attachment.filename,
                "url": attachment.url,
                "content_type": attachment.content_type,
                "size": attachment.size,
            }
            for attachment in message.attachments
        ],
        "created_at": message.created_at.isoformat(),
    }


def _deserialize_embeds(values: Any) -> list[discord.Embed]:
    embeds: list[discord.Embed] = []
    for value in values[:10] if isinstance(values, list) else []:
        if not isinstance(value, dict):
            continue
        try:
            embeds.append(discord.Embed.from_dict(value))
        except (TypeError, ValueError):
            continue
    return embeds


async def _registered_panel_references(state: StateStore, guild_id: int) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    fixed = await state.get_section("fixed_admin_panels")
    guilds = fixed.get("guilds", {}) if isinstance(fixed.get("guilds"), dict) else {}
    panels = guilds.get(str(guild_id), {}) if isinstance(guilds, dict) else {}
    if isinstance(panels, dict):
        for kind, entries in panels.items():
            for item in entries if isinstance(entries, list) else []:
                if isinstance(item, dict):
                    references.append({**item, "kind": str(kind), "source_section": "fixed_admin_panels"})
    tickets = await state.get_section("ticket_configs")
    ticket = tickets.get(str(guild_id))
    if isinstance(ticket, dict) and ticket.get("opening_channel_id") and ticket.get("panel_message_id"):
        references.append(
            {
                "channel_id": ticket["opening_channel_id"],
                "message_id": ticket["panel_message_id"],
                "kind": "tickets_public",
                "source_section": "ticket_configs",
            }
        )
    for section_name in ("squarecloud_hosting", "letters_panel", "english_learning_settings"):
        section = await state.get_section(section_name)
        candidates = [section, section.get(str(guild_id)) if isinstance(section, dict) else None]
        for item in candidates:
            if isinstance(item, dict) and item.get("panel_channel_id") and item.get("panel_message_id"):
                references.append(
                    {
                        "channel_id": item["panel_channel_id"],
                        "message_id": item["panel_message_id"],
                        "kind": section_name,
                        "source_section": section_name,
                    }
                )
    return references


async def _save_restored_panel_reference(
    state: StateStore,
    guild_id: int,
    item: dict[str, Any],
    channel_id: int,
    message_id: int,
) -> None:
    kind = str(item.get("kind") or "restored")

    def mutate(section: dict[str, Any]) -> None:
        guilds = section.setdefault("guilds", {})
        guild_panels = guilds.setdefault(str(guild_id), {})
        entries = guild_panels.setdefault(kind, [])
        entries.append(
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "restored_from_message_id": item.get("message_id"),
                "updated_at": _utc_now(),
            }
        )
        guild_panels[kind] = entries[-10:]

    await state.mutate_section("fixed_admin_panels", mutate)


async def _remap_internal_bella_config(
    state: StateStore,
    guild_id: int,
    role_map: dict[int, int],
    category_map: dict[int, int],
    channel_map: dict[int, int],
) -> None:
    replacements = {**role_map, **category_map, **channel_map}
    for section_name in (
        "guild_log_config",
        "guild_log_channels",
        "guild_audit_config",
        "discord_automod_config",
        "ticket_configs",
    ):
        section = await state.get_section(section_name)
        raw = section.get(str(guild_id))
        if not isinstance(raw, dict):
            continue
        await state.update_section(section_name, str(guild_id), _replace_known_ids(raw, replacements))


def _replace_known_ids(value: Any, replacements: dict[int, int], key: str = "") -> Any:
    if isinstance(value, dict):
        return {name: _replace_known_ids(item, replacements, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        if key.endswith("_ids"):
            return [replacements.get(int(item), int(item)) if str(item).isdigit() else item for item in value]
        return [_replace_known_ids(item, replacements, key) for item in value]
    if key.endswith("_id") and str(value).isdigit():
        return replacements.get(int(value), int(value))
    return value


def _find_channel(
    guild: discord.Guild,
    name: str,
    channel_type: str,
    category: discord.CategoryChannel | None,
) -> discord.abc.GuildChannel | None:
    for channel in guild.channels:
        if isinstance(channel, discord.CategoryChannel):
            continue
        if channel.name != name or _channel_type_name(channel) != channel_type:
            continue
        if (channel.category.id if channel.category else None) == (category.id if category else None):
            return channel
    return None


async def _create_channel(
    guild: discord.Guild,
    item: dict[str, Any],
    *,
    category: discord.CategoryChannel | None,
    reason: str,
) -> discord.abc.GuildChannel:
    name = str(item.get("name") or "canal-restaurado")[:100]
    channel_type = str(item.get("type") or "text")
    common = {"category": category, "reason": reason, "position": int(item.get("position", 0) or 0)}
    if channel_type in {"text", "news"}:
        return await guild.create_text_channel(
            name,
            news=channel_type == "news",
            topic=item.get("topic") or None,
            slowmode_delay=int(item.get("slowmode_delay", 0) or 0),
            nsfw=bool(item.get("nsfw", False)),
            default_auto_archive_duration=int(item.get("default_auto_archive_duration", 1440) or 1440),
            default_thread_slowmode_delay=int(item.get("default_thread_slowmode_delay", 0) or 0),
            **common,
        )
    if channel_type in {"voice", "stage_voice"}:
        method = guild.create_stage_channel if channel_type == "stage_voice" else guild.create_voice_channel
        return await method(
            name,
            bitrate=int(item.get("bitrate", 64000) or 64000),
            user_limit=int(item.get("user_limit", 0) or 0),
            video_quality_mode=discord.VideoQualityMode(int(item.get("video_quality_mode", 1) or 1)),
            nsfw=bool(item.get("nsfw", False)),
            **common,
        )
    if channel_type in {"forum", "media"}:
        tags = [
            discord.ForumTag(name=str(tag.get("name") or "tag")[:20], moderated=bool(tag.get("moderated", False)))
            for tag in item.get("available_tags", [])
            if isinstance(tag, dict)
        ]
        return await guild.create_forum(
            name,
            topic=item.get("topic") or None,
            slowmode_delay=int(item.get("slowmode_delay", 0) or 0),
            nsfw=bool(item.get("nsfw", False)),
            media=channel_type == "media",
            default_auto_archive_duration=int(item.get("default_auto_archive_duration", 1440) or 1440),
            default_thread_slowmode_delay=int(item.get("default_thread_slowmode_delay", 0) or 0),
            default_sort_order=(
                discord.ForumOrderType(int(item["default_sort_order"]))
                if item.get("default_sort_order") is not None
                else discord.utils.MISSING
            ),
            default_layout=discord.ForumLayoutType(int(item.get("default_layout", 0) or 0)),
            available_tags=tags,
            **common,
        )
    raise ValueError(f"tipo de canal não suportado: {channel_type}")


def _channel_type_name(channel: discord.abc.GuildChannel) -> str:
    return {
        discord.ChannelType.category: "category",
        discord.ChannelType.text: "text",
        discord.ChannelType.news: "news",
        discord.ChannelType.voice: "voice",
        discord.ChannelType.stage_voice: "stage_voice",
        discord.ChannelType.forum: "forum",
        discord.ChannelType.media: "media",
    }.get(channel.type, str(channel.type))


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list):
        normalized = [_normalize(item) for item in value]
        if all(isinstance(item, (str, int, float, bool, type(None))) for item in normalized):
            return sorted(normalized, key=str)
        return normalized
    return value


def _job_payload(
    job_id: str,
    backup_id: str,
    guild_id: int,
    actor_id: int,
    status: BackupJobStatus,
    started_at: str,
    *,
    finished_at: str = "",
    details: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "backup_id": backup_id,
        "guild_id": guild_id,
        "actor_id": actor_id,
        "status": status.value,
        "started_at": started_at,
        "finished_at": finished_at,
        "details": details or {},
        "error": error[:2000],
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
