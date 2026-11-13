from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import discord

from .central_logs import CentralLogService
from .state import StateStore

LOGGER = logging.getLogger(__name__)

FIXED_PANELS_SECTION = "fixed_admin_panels"
CONFIG_SECTION = "panel_repair_config"
ISSUES_SECTION = "panel_repair_issues"
HISTORY_SECTION = "panel_repair_history"
MAX_ISSUES_PER_GUILD = 200
BACKUP_RESOURCES_SECTION = "server_backup_resources"

AuthorizationCallback = Callable[[discord.Member], bool]
RecreateCallback = Callable[[str, discord.Guild, discord.TextChannel], Awaitable[discord.Message]]


class PanelRepairService:
    def __init__(
        self,
        bot: discord.Client,
        state: StateStore,
        logs: CentralLogService,
        *,
        authorize: AuthorizationCallback,
    ) -> None:
        self.bot = bot
        self.state = state
        self.logs = logs
        self.authorize = authorize
        self._recreate_callback: RecreateCallback | None = None

    def set_recreate_callback(self, callback: RecreateCallback) -> None:
        self._recreate_callback = callback

    async def enabled(self, guild_id: int) -> bool:
        section = await self.state.get_section(CONFIG_SECTION)
        payload = section.get(str(guild_id), {})
        return bool(payload.get("enabled", True)) if isinstance(payload, dict) else True

    async def set_enabled(self, guild_id: int, enabled: bool) -> None:
        await self.state.update_section(
            CONFIG_SECTION,
            str(guild_id),
            {"enabled": bool(enabled), "updated_at": _now()},
        )

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> bool:
        if payload.guild_id is None or not await self.enabled(payload.guild_id):
            return False
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return False
        cached = getattr(payload, "cached_message", None)
        snapshot = _serialize_deleted_message(cached)
        if (
            cached is not None
            and self.bot.user is not None
            and cached.author.id != self.bot.user.id
        ):
            snapshot = None
        if snapshot is None:
            snapshot = await self._message_snapshot_from_backups(
                payload.guild_id,
                payload.channel_id,
                payload.message_id,
            )
        for kind, reference in await self.references(payload.guild_id):
            if int(reference.get("message_id") or 0) != payload.message_id:
                continue
            await self._open_issue(
                guild,
                kind=kind,
                channel_id=payload.channel_id,
                message_id=payload.message_id,
                problem="missing_message",
                detail="O Discord informou que a mensagem registrada do painel foi apagada.",
                resource_type="panel",
                resource_id=payload.message_id,
                snapshot=snapshot,
            )
            return True
        if snapshot is not None and snapshot.get("embeds"):
            await self._open_issue(
                guild,
                kind="bella_embed",
                channel_id=payload.channel_id,
                message_id=payload.message_id,
                problem="deleted_embed",
                detail="Uma mensagem com embed enviada pela Bella foi apagada.",
                resource_type="embed",
                resource_id=payload.message_id,
                snapshot=snapshot,
            )
            return True
        return False

    async def on_role_delete(self, role: discord.Role) -> dict[str, Any] | None:
        if not await self.enabled(role.guild.id) or role.managed or role.is_default():
            return None
        return await self._open_issue(
            role.guild,
            kind="deleted_role",
            channel_id=0,
            message_id=0,
            problem="deleted_role",
            detail=f"O cargo **{role.name}** foi apagado e pode ser recriado seletivamente.",
            resource_type="role",
            resource_id=role.id,
            snapshot=_serialize_role(role),
        )

    async def on_channel_delete(
        self,
        channel: discord.abc.GuildChannel,
    ) -> dict[str, Any] | None:
        if not await self.enabled(channel.guild.id):
            return None
        return await self._open_issue(
            channel.guild,
            kind="deleted_channel",
            channel_id=channel.id,
            message_id=0,
            problem="deleted_channel",
            detail=f"O canal **{channel.name}** foi apagado e pode ser recriado seletivamente.",
            resource_type="channel",
            resource_id=channel.id,
            snapshot=_serialize_channel(channel),
        )

    async def audit_guild(self, guild: discord.Guild) -> dict[str, int]:
        if not await self.enabled(guild.id):
            return {"checked": 0, "opened": 0, "healthy": 0}
        checked = opened = healthy = 0
        for kind, reference in await self.references(guild.id):
            checked += 1
            channel_id = _int(reference.get("channel_id"))
            message_id = _int(reference.get("message_id"))
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                await self._open_issue(
                    guild,
                    kind=kind,
                    channel_id=channel_id,
                    message_id=message_id,
                    problem="missing_channel",
                    detail="O canal registrado não existe mais ou não é um canal de texto.",
                )
                opened += 1
                continue
            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                await self._open_issue(
                    guild,
                    kind=kind,
                    channel_id=channel_id,
                    message_id=message_id,
                    problem="missing_message",
                    detail="A mensagem registrada não foi encontrada no canal.",
                )
                opened += 1
                continue
            except (discord.Forbidden, discord.HTTPException) as exc:
                LOGGER.warning("Falha ao verificar painel %s/%s: %s", guild.id, message_id, exc)
                continue
            if self.bot.user is not None and message.author.id != self.bot.user.id:
                await self._open_issue(
                    guild,
                    kind=kind,
                    channel_id=channel_id,
                    message_id=message_id,
                    problem="foreign_author",
                    detail="A mensagem registrada não pertence à conta atual da Bella.",
                )
                opened += 1
                continue
            if not message.embeds and not message.components:
                await self._open_issue(
                    guild,
                    kind=kind,
                    channel_id=channel_id,
                    message_id=message_id,
                    problem="empty_panel",
                    detail="A mensagem existe, mas não contém embed nem componentes de painel.",
                )
                opened += 1
                continue
            healthy += 1
            await self._resolve_matching_issue(guild.id, kind, message_id, "HEALTHY")
        return {"checked": checked, "opened": opened, "healthy": healthy}

    async def references(self, guild_id: int) -> list[tuple[str, dict[str, Any]]]:
        section = await self.state.get_section(FIXED_PANELS_SECTION)
        guilds = section.get("guilds", {}) if isinstance(section, dict) else {}
        panels = guilds.get(str(guild_id), {}) if isinstance(guilds, dict) else {}
        if not isinstance(panels, dict):
            return []
        return [
            (str(kind), dict(item))
            for kind, entries in panels.items()
            if isinstance(entries, list)
            for item in entries
            if isinstance(item, dict)
            and _int(item.get("channel_id"))
            and _int(item.get("message_id"))
        ]

    async def issues(
        self,
        guild_id: int,
        *,
        status: str | None = "OPEN",
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        section = await self.state.get_section(ISSUES_SECTION)
        values = section.get(str(guild_id), [])
        if not isinstance(values, list):
            return []
        result = [
            dict(item)
            for item in values
            if isinstance(item, dict) and (status is None or item.get("status") == status)
        ]
        return result[-limit:][::-1]

    async def recreate(self, guild: discord.Guild, issue_id: str, actor_id: int) -> discord.Message:
        issue = await self._issue(guild.id, issue_id)
        if issue is None or issue.get("status") != "OPEN":
            raise ValueError("Essa pendência não está mais aberta.")
        if self._recreate_callback is None:
            raise RuntimeError("O recriador de painéis não foi configurado.")
        channel = await self._resolve_issue_text_channel(guild, issue)
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("O canal original não existe. Selecione também a pendência do canal para restaurá-lo primeiro.")
        message = await self._recreate_callback(str(issue.get("kind")), guild, channel)
        await self._replace_reference(
            guild.id,
            str(issue.get("kind")),
            old_message_id=_int(issue.get("message_id")),
            channel_id=channel.id,
            message_id=message.id,
        )
        await self._finish_issue(guild, issue_id, "RECREATED", actor_id, new_message_id=message.id)
        return message

    async def relink(self, guild: discord.Guild, issue_id: str, actor_id: int) -> discord.Message:
        issue = await self._issue(guild.id, issue_id)
        if issue is None or issue.get("status") != "OPEN":
            raise ValueError("Essa pendência não está mais aberta.")
        channel = await self._resolve_issue_text_channel(guild, issue)
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("O canal original não existe; publique um novo painel e use Recriar.")
        async for message in channel.history(limit=50):
            if self.bot.user is None or message.author.id != self.bot.user.id:
                continue
            if message.id == _int(issue.get("message_id")):
                continue
            if not _message_matches_kind(message, str(issue.get("kind"))):
                continue
            await self._replace_reference(
                guild.id,
                str(issue.get("kind")),
                old_message_id=_int(issue.get("message_id")),
                channel_id=channel.id,
                message_id=message.id,
            )
            await self._finish_issue(guild, issue_id, "RELINKED", actor_id, new_message_id=message.id)
            return message
        raise ValueError("Não encontrei um painel compatível entre as 50 mensagens mais recentes deste canal.")

    async def _resolve_issue_text_channel(
        self,
        guild: discord.Guild,
        issue: dict[str, Any],
    ) -> discord.TextChannel | None:
        old_channel_id = _int(issue.get("channel_id"))
        channel = guild.get_channel(old_channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel

        for candidate in await self.issues(guild.id, status=None, limit=MAX_ISSUES_PER_GUILD):
            if (
                str(candidate.get("resource_type")) != "channel"
                or _int(candidate.get("resource_id")) != old_channel_id
            ):
                continue
            channel = guild.get_channel(_int(candidate.get("new_resource_id")))
            if isinstance(channel, discord.TextChannel):
                return channel

        snapshot = issue.get("snapshot")
        if isinstance(snapshot, dict):
            channel = _find_text_channel(guild, snapshot)
            if isinstance(channel, discord.TextChannel):
                return channel
        return None

    async def ignore(self, guild: discord.Guild, issue_id: str, actor_id: int) -> None:
        issue = await self._issue(guild.id, issue_id)
        if issue is None or issue.get("status") != "OPEN":
            raise ValueError("Essa pendência não está mais aberta.")
        await self._finish_issue(guild, issue_id, "IGNORED", actor_id)

    async def restore_issue(self, guild: discord.Guild, issue_id: str, actor_id: int) -> str:
        issue = await self._issue(guild.id, issue_id)
        if issue is None or issue.get("status") != "OPEN":
            raise ValueError("Essa pendência não está mais aberta.")
        resource_type = str(issue.get("resource_type") or "panel")
        if resource_type == "panel":
            message = await self.recreate(guild, issue_id, actor_id)
            return f"Painel recriado: {message.jump_url}"
        snapshot = issue.get("snapshot")
        if not isinstance(snapshot, dict):
            raise ValueError("Essa pendência não possui snapshot restaurável.")
        if resource_type == "role":
            role = await self._restore_role(guild, snapshot)
            await self._finish_issue(
                guild,
                issue_id,
                "RESTORED",
                actor_id,
                new_resource_id=role.id,
            )
            return f"Cargo restaurado: {role.mention}"
        if resource_type == "channel":
            channel = await self._restore_channel(guild, snapshot)
            await self._finish_issue(
                guild,
                issue_id,
                "RESTORED",
                actor_id,
                new_resource_id=channel.id,
            )
            return f"Canal restaurado: {getattr(channel, 'mention', channel.name)}"
        if resource_type == "embed":
            message = await self._restore_embed(guild, snapshot)
            await self._finish_issue(
                guild,
                issue_id,
                "RESTORED",
                actor_id,
                new_message_id=message.id,
                new_resource_id=message.id,
            )
            return f"Embed restaurada: {message.jump_url}"
        raise ValueError(f"Tipo de recurso não suportado: {resource_type}.")

    async def restore_selected(
        self,
        guild: discord.Guild,
        issue_ids: list[str],
        actor_id: int,
    ) -> tuple[list[str], list[str]]:
        issues = [await self._issue(guild.id, issue_id) for issue_id in issue_ids]
        order = {"role": 0, "channel": 1, "panel": 2, "embed": 3}
        selected = sorted(
            [item for item in issues if isinstance(item, dict)],
            key=lambda item: order.get(str(item.get("resource_type") or "panel"), 9),
        )
        restored: list[str] = []
        failures: list[str] = []
        for issue in selected:
            issue_id = str(issue.get("id"))
            try:
                restored.append(await self.restore_issue(guild, issue_id, actor_id))
            except (ValueError, RuntimeError, discord.Forbidden, discord.HTTPException) as exc:
                failures.append(f"{issue_id}: {exc}")
        return restored, failures

    async def ignore_selected(self, guild: discord.Guild, issue_ids: list[str], actor_id: int) -> int:
        ignored = 0
        for issue_id in issue_ids:
            try:
                await self.ignore(guild, issue_id, actor_id)
                ignored += 1
            except ValueError:
                continue
        return ignored

    async def _restore_role(self, guild: discord.Guild, snapshot: dict[str, Any]) -> discord.Role:
        name = str(snapshot.get("name") or "cargo-restaurado")[:100]
        existing = discord.utils.find(lambda role: role.name == name and not role.managed, guild.roles)
        if existing is not None:
            return existing
        role = await guild.create_role(
            name=name,
            permissions=discord.Permissions(_int(snapshot.get("permissions"))),
            colour=discord.Colour(_int(snapshot.get("color"))),
            hoist=bool(snapshot.get("hoist", False)),
            mentionable=bool(snapshot.get("mentionable", False)),
            reason="Restauração seletiva pelo Reparador da Bella",
        )
        position = _int(snapshot.get("position"))
        if position:
            try:
                await role.edit(position=position, reason="Reposicionamento do cargo restaurado")
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Não foi possível reposicionar o cargo restaurado %s.", role.id)
        return role

    async def _restore_channel(
        self,
        guild: discord.Guild,
        snapshot: dict[str, Any],
    ) -> discord.abc.GuildChannel:
        name = str(snapshot.get("name") or "canal-restaurado")[:100]
        channel_type = str(snapshot.get("type") or "text")
        existing = discord.utils.find(
            lambda item: item.name == name and str(item.type) == channel_type,
            guild.channels,
        )
        if existing is not None:
            return existing
        category = _find_category(guild, snapshot)
        reason = "Restauração seletiva pelo Reparador da Bella"
        if channel_type == "category":
            channel = await guild.create_category(name, reason=reason)
        elif channel_type == "voice":
            voice_kwargs: dict[str, Any] = {
                "category": category,
                "user_limit": _int(snapshot.get("user_limit")),
                "reason": reason,
            }
            if _int(snapshot.get("bitrate")):
                voice_kwargs["bitrate"] = _int(snapshot.get("bitrate"))
            channel = await guild.create_voice_channel(name, **voice_kwargs)
        elif channel_type == "stage_voice":
            channel = await guild.create_stage_channel(name, category=category, reason=reason)
        elif channel_type == "forum" and hasattr(guild, "create_forum"):
            channel = await guild.create_forum(
                name,
                category=category,
                topic=str(snapshot.get("topic") or "")[:4096] or None,
                nsfw=bool(snapshot.get("nsfw", False)),
                slowmode_delay=_int(snapshot.get("slowmode_delay")),
                reason=reason,
            )
        else:
            channel = await guild.create_text_channel(
                name,
                category=category,
                topic=str(snapshot.get("topic") or "")[:1024] or None,
                nsfw=bool(snapshot.get("nsfw", False)),
                slowmode_delay=_int(snapshot.get("slowmode_delay")),
                news=channel_type == "news",
                reason=reason,
            )
        await _restore_channel_overwrites(channel, guild, snapshot.get("overwrites", []))
        return channel

    async def _restore_embed(self, guild: discord.Guild, snapshot: dict[str, Any]) -> discord.Message:
        channel = guild.get_channel(_int(snapshot.get("channel_id")))
        if not isinstance(channel, discord.TextChannel):
            channel = _find_text_channel(guild, snapshot)
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("O canal original da embed não existe; restaure o canal primeiro.")
        embeds: list[discord.Embed] = []
        for value in snapshot.get("embeds", [])[:10]:
            if not isinstance(value, dict):
                continue
            try:
                embeds.append(discord.Embed.from_dict(value))
            except (TypeError, ValueError):
                continue
        if not embeds:
            raise ValueError("O snapshot não contém uma embed válida.")
        return await channel.send(
            str(snapshot.get("content") or "")[:2000] or None,
            embeds=embeds,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _message_snapshot_from_backups(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> dict[str, Any] | None:
        resources = await self.state.get_section(BACKUP_RESOURCES_SECTION)
        payloads = [item for item in resources.values() if isinstance(item, dict)]
        payloads.sort(
            key=lambda item: str(item.get("METADATA", {}).get("created_at", "")),
            reverse=True,
        )
        for payload in payloads:
            metadata = payload.get("METADATA", {})
            if _int(metadata.get("source_guild_id")) != guild_id:
                continue
            for section_name in ("BELLA_PANELS", "EMBEDS"):
                for item in payload.get(section_name, []):
                    if not isinstance(item, dict):
                        continue
                    if _int(item.get("channel_id")) == channel_id and _int(item.get("message_id")) == message_id:
                        snapshot = dict(item)
                        channel_snapshot = next(
                            (
                                channel
                                for channel in payload.get("CHANNELS", [])
                                if isinstance(channel, dict) and _int(channel.get("id")) == channel_id
                            ),
                            None,
                        )
                        if isinstance(channel_snapshot, dict):
                            snapshot.setdefault("channel_name", channel_snapshot.get("name"))
                            snapshot.setdefault("category_id", channel_snapshot.get("category_id"))
                        return snapshot
        return None

    async def status_embed(self, guild: discord.Guild) -> discord.Embed:
        open_issues = await self.issues(guild.id, status="OPEN", limit=200)
        all_issues = await self.issues(guild.id, status=None, limit=200)
        references = await self.references(guild.id)
        by_type: dict[str, int] = {}
        for issue in open_issues:
            resource_type = str(issue.get("resource_type") or "panel")
            by_type[resource_type] = by_type.get(resource_type, 0) + 1
        embed = discord.Embed(
            title="Reparador de Recursos da Bella",
            description=(
                "Acompanha painéis e snapshots de cargos, canais e embeds apagados enquanto a Bella está online."
            ),
            color=discord.Color.from_rgb(255, 80, 120),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Painéis registrados", value=str(len(references)), inline=True)
        embed.add_field(name="Pendências", value=str(len(open_issues)), inline=True)
        embed.add_field(name="Histórico", value=str(len(all_issues)), inline=True)
        embed.add_field(
            name="Pendências por tipo",
            value="\n".join(f"{key}: **{value}**" for key, value in sorted(by_type.items())) or "Nenhuma",
            inline=False,
        )
        embed.add_field(
            name="Ações seguras",
            value="Selecione um ou vários recursos. Restaurar só cria o que estiver ausente; Ignorar mantém o registro para auditoria.",
            inline=False,
        )
        return embed

    def panel_view(self) -> "PanelRepairAdminView":
        return PanelRepairAdminView(self)

    async def _open_issue(
        self,
        guild: discord.Guild,
        *,
        kind: str,
        channel_id: int,
        message_id: int,
        problem: str,
        detail: str,
        resource_type: str = "panel",
        resource_id: int = 0,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        issue_id = hashlib.sha256(
            f"{guild.id}:{resource_type}:{kind}:{resource_id or message_id}:{problem}".encode("utf-8")
        ).hexdigest()[:16]
        created = False
        issue: dict[str, Any] = {}

        def mutate(section: dict[str, Any]) -> None:
            nonlocal created, issue
            values = section.setdefault(str(guild.id), [])
            if not isinstance(values, list):
                values = []
            existing = next(
                (item for item in values if isinstance(item, dict) and item.get("id") == issue_id),
                None,
            )
            if existing is not None and existing.get("status") == "OPEN":
                existing["last_seen_at"] = _now()
                issue = dict(existing)
            else:
                issue = {
                    "id": issue_id,
                    "kind": kind,
                    "resource_type": resource_type,
                    "resource_id": resource_id or message_id,
                    "channel_id": channel_id,
                    "message_id": message_id,
                    "problem": problem,
                    "detail": detail,
                    "status": "OPEN",
                    "created_at": _now(),
                    "last_seen_at": _now(),
                    "snapshot": snapshot or {},
                }
                values.append(issue)
                created = True
            section[str(guild.id)] = values[-MAX_ISSUES_PER_GUILD:]

        await self.state.mutate_section(ISSUES_SECTION, mutate)
        if created:
            await self.logs.send_event(
                guild,
                "panel_repair",
                title="Recurso da Bella precisa de atenção",
                description=detail,
                severity=(
                    "HIGH"
                    if problem in {"missing_message", "missing_channel", "deleted_channel", "deleted_role"}
                    else "MEDIUM"
                ),
                critical=False,
                fields=(
                    ("Tipo", kind, True),
                    ("Recurso", resource_type, True),
                    ("Problema", problem, True),
                    ("Canal", f"<#{channel_id}>" if channel_id else "removido", True),
                    ("Pendência", issue_id, False),
                ),
            )
        return issue

    async def _resolve_matching_issue(
        self,
        guild_id: int,
        kind: str,
        message_id: int,
        status: str,
    ) -> None:
        def mutate(section: dict[str, Any]) -> None:
            values = section.get(str(guild_id), [])
            if not isinstance(values, list):
                return
            for item in values:
                if (
                    isinstance(item, dict)
                    and item.get("kind") == kind
                    and _int(item.get("message_id")) == message_id
                    and item.get("status") == "OPEN"
                ):
                    item["status"] = status
                    item["resolved_at"] = _now()

        await self.state.mutate_section(ISSUES_SECTION, mutate)

    async def _issue(self, guild_id: int, issue_id: str) -> dict[str, Any] | None:
        issues = await self.issues(guild_id, status=None, limit=MAX_ISSUES_PER_GUILD)
        return next((item for item in issues if item.get("id") == issue_id), None)

    async def _finish_issue(
        self,
        guild: discord.Guild,
        issue_id: str,
        status: str,
        actor_id: int,
        *,
        new_message_id: int | None = None,
        new_resource_id: int | None = None,
    ) -> None:
        updated: dict[str, Any] = {}

        def mutate(section: dict[str, Any]) -> None:
            nonlocal updated
            values = section.get(str(guild.id), [])
            if not isinstance(values, list):
                return
            for item in values:
                if isinstance(item, dict) and item.get("id") == issue_id:
                    item.update(
                        {
                            "status": status,
                            "resolved_at": _now(),
                            "resolved_by": actor_id,
                            "new_message_id": new_message_id,
                            "new_resource_id": new_resource_id,
                        }
                    )
                    updated = dict(item)
                    break

        await self.state.mutate_section(ISSUES_SECTION, mutate)
        if not updated:
            return
        await self.state.update_section(
            HISTORY_SECTION,
            f"{guild.id}:{issue_id}:{int(datetime.now(timezone.utc).timestamp())}",
            updated,
        )
        await self.logs.send_event(
            guild,
            "panel_repair",
            title=f"Recurso {status.casefold()}",
            description=f"A pendência **{issue_id}** foi finalizada por <@{actor_id}>.",
            severity="INFO",
            fields=(("Tipo", str(updated.get("kind")), True), ("Estado", status, True)),
        )

    async def _replace_reference(
        self,
        guild_id: int,
        kind: str,
        *,
        old_message_id: int,
        channel_id: int,
        message_id: int,
    ) -> None:
        def mutate(section: dict[str, Any]) -> None:
            guilds = section.setdefault("guilds", {})
            if not isinstance(guilds, dict):
                guilds = {}
                section["guilds"] = guilds
            panels = guilds.setdefault(str(guild_id), {})
            if not isinstance(panels, dict):
                panels = {}
                guilds[str(guild_id)] = panels
            entries = panels.setdefault(kind, [])
            if not isinstance(entries, list):
                entries = []
            replaced = False
            for item in entries:
                if isinstance(item, dict) and _int(item.get("message_id")) == old_message_id:
                    item.update(
                        {
                            "channel_id": channel_id,
                            "message_id": message_id,
                            "updated_at": _now(),
                        }
                    )
                    replaced = True
            if not replaced:
                entries.append(
                    {
                        "channel_id": channel_id,
                        "message_id": message_id,
                        "updated_at": _now(),
                    }
                )
            panels[kind] = entries[-10:]

        await self.state.mutate_section(FIXED_PANELS_SECTION, mutate)


class PanelRepairAdminView(discord.ui.View):
    def __init__(self, service: PanelRepairService) -> None:
        super().__init__(timeout=None)
        self.service = service

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = isinstance(interaction.user, discord.Member) and self.service.authorize(interaction.user)
        if not allowed:
            await interaction.response.send_message("Você não possui acesso a este painel.", ephemeral=True)
        return allowed

    @discord.ui.button(label="Auditar agora", style=discord.ButtonStyle.primary, custom_id="bella:panel_repair:audit:v1")
    async def audit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.service.audit_guild(interaction.guild)
        await interaction.followup.send(
            f"Auditoria concluída: **{result['checked']}** verificados, **{result['healthy']}** saudáveis e **{result['opened']}** com pendência.",
            ephemeral=True,
        )

    @discord.ui.button(label="Pendências", style=discord.ButtonStyle.secondary, custom_id="bella:panel_repair:issues:v1")
    async def issues(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        issues = await self.service.issues(interaction.guild.id, status="OPEN")
        if not issues:
            await interaction.response.send_message("Nenhum recurso precisa de reparo agora.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Escolha a pendência que deseja tratar:",
            view=PanelIssuePicker(self.service, issues, interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(label="Status", style=discord.ButtonStyle.secondary, custom_id="bella:panel_repair:status:v1")
    async def status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.send_message(embed=await self.service.status_embed(interaction.guild), ephemeral=True)

    @discord.ui.button(label="Ativar / pausar", style=discord.ButtonStyle.danger, custom_id="bella:panel_repair:toggle:v1")
    async def toggle(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        enabled = not await self.service.enabled(interaction.guild.id)
        await self.service.set_enabled(interaction.guild.id, enabled)
        await interaction.response.send_message(
            f"Reparador {'ativado' if enabled else 'pausado'} neste servidor.",
            ephemeral=True,
        )


class PanelIssuePicker(discord.ui.View):
    def __init__(self, service: PanelRepairService, issues: list[dict[str, Any]], owner_id: int) -> None:
        super().__init__(timeout=600)
        self.service = service
        self.owner_id = owner_id
        select = discord.ui.Select(
            placeholder="Selecione um ou mais recursos",
            min_values=1,
            max_values=min(25, len(issues)),
            options=[
                discord.SelectOption(
                    label=(
                        f"{item.get('resource_type', 'panel')} • "
                        f"{item.get('snapshot', {}).get('name') or item.get('kind', 'recurso')}"
                    )[:100],
                    value=str(item.get("id")),
                    description=f"{item.get('problem', 'problema')} • {item.get('id')}"[:100],
                )
                for item in issues[:25]
            ],
        )
        select.callback = self._select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = interaction.user.id == self.owner_id
        if not allowed:
            await interaction.response.send_message("Somente quem abriu esta lista pode usá-la.", ephemeral=True)
        return allowed

    async def _select(self, interaction: discord.Interaction) -> None:
        select = self.children[0]
        issue_ids = list(select.values) if isinstance(select, discord.ui.Select) else []
        await interaction.response.send_message(
            f"**{len(issue_ids)}** recurso(s) selecionado(s). Escolha uma ação segura.",
            view=PanelIssueSelectionActionView(self.service, issue_ids, self.owner_id),
            ephemeral=True,
        )


class PanelIssueSelectionActionView(discord.ui.View):
    def __init__(self, service: PanelRepairService, issue_ids: list[str], owner_id: int) -> None:
        super().__init__(timeout=600)
        self.service = service
        self.issue_ids = issue_ids
        self.owner_id = owner_id

        restore = discord.ui.Button(label="Restaurar selecionados", style=discord.ButtonStyle.success)
        restore.callback = self._restore
        self.add_item(restore)

        relink = discord.ui.Button(
            label="Relincar painel",
            style=discord.ButtonStyle.primary,
            disabled=len(issue_ids) != 1,
        )
        relink.callback = self._relink
        self.add_item(relink)

        ignore = discord.ui.Button(label="Ignorar selecionados", style=discord.ButtonStyle.danger)
        ignore.callback = self._ignore
        self.add_item(ignore)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Somente quem abriu esta ação pode usá-la.", ephemeral=True)
            return False
        return True

    async def _restore(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        restored, failures = await self.service.restore_selected(
            interaction.guild,
            self.issue_ids,
            interaction.user.id,
        )
        lines = [f"Concluídos: **{len(restored)}**", f"Falhas: **{len(failures)}**"]
        lines.extend(f"• {item}" for item in restored[:15])
        lines.extend(f"• Falha: {item}" for item in failures[:10])
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)

    async def _relink(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or len(self.issue_ids) != 1:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await self.service.relink(
                interaction.guild,
                self.issue_ids[0],
                interaction.user.id,
            )
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(f"Não consegui relincar: {exc}", ephemeral=True)
            return
        await interaction.followup.send(f"Painel relincado: {message.jump_url}", ephemeral=True)

    async def _ignore(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        ignored = await self.service.ignore_selected(
            interaction.guild,
            self.issue_ids,
            interaction.user.id,
        )
        await interaction.response.send_message(
            f"**{ignored}** pendência(s) ignorada(s) e mantida(s) no histórico.",
            ephemeral=True,
        )


class PanelIssueActionView(discord.ui.View):
    def __init__(self, service: PanelRepairService, issue_id: str, owner_id: int) -> None:
        super().__init__(timeout=600)
        self.service = service
        self.issue_id = issue_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Somente quem abriu esta ação pode usá-la.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Recriar", style=discord.ButtonStyle.success)
    async def recreate(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await self.service.recreate(interaction.guild, self.issue_id, interaction.user.id)
        except (ValueError, RuntimeError, discord.HTTPException) as exc:
            await interaction.followup.send(f"Não consegui recriar: {exc}", ephemeral=True)
            return
        await interaction.followup.send(f"Painel recriado: {message.jump_url}", ephemeral=True)

    @discord.ui.button(label="Relincar", style=discord.ButtonStyle.primary)
    async def relink(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await self.service.relink(interaction.guild, self.issue_id, interaction.user.id)
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(f"Não consegui relincar: {exc}", ephemeral=True)
            return
        await interaction.followup.send(f"Painel relincado: {message.jump_url}", ephemeral=True)

    @discord.ui.button(label="Ignorar", style=discord.ButtonStyle.danger)
    async def ignore(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        try:
            await self.service.ignore(interaction.guild, self.issue_id, interaction.user.id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message("Pendência ignorada e mantida no histórico.", ephemeral=True)


def panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Reparador de Recursos",
        description=(
            "A Bella acompanha painéis e registra snapshots de cargos, canais e embeds apagados enquanto está online."
        ),
        color=discord.Color.from_rgb(255, 80, 120),
    )
    embed.add_field(
        name="Restauração seletiva",
        value="Escolha um ou vários recursos e restaure somente os itens desejados.",
        inline=False,
    )
    embed.add_field(
        name="Painéis",
        value="Painéis registrados podem ser recriados ou relincados no canal correspondente.",
        inline=False,
    )
    embed.add_field(
        name="Limite seguro",
        value="Recursos apagados enquanto a Bella estava offline dependem de um backup estrutural existente.",
        inline=False,
    )
    return embed


def _message_matches_kind(message: discord.Message, kind: str) -> bool:
    aliases = {
        "ticket_admin": ("ticket", "atendimento"),
        "english_admin": ("inglês", "ingles", "english"),
        "site_management": ("site", "monitoramento"),
        "embed_generator": ("embed", "gerador"),
        "critical_changes_admin": ("alterações críticas", "alteracoes criticas"),
        "panel_repair_admin": ("reparador", "painéis", "paineis"),
        "giveaways_admin": ("sorteio", "giveaway"),
        "satisfaction_admin": ("satisfação", "satisfacao", "feedback"),
    }
    text = " ".join(
        [message.content]
        + [embed.title or "" for embed in message.embeds]
        + [embed.description or "" for embed in message.embeds]
        + [str(message.components)]
    ).casefold()
    terms = aliases.get(kind, (kind.replace("_", " "),))
    return bool(message.components) and any(term in text for term in terms)


def _serialize_role(role: discord.Role) -> dict[str, Any]:
    return {
        "id": role.id,
        "name": role.name,
        "permissions": role.permissions.value,
        "color": role.colour.value,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "position": role.position,
    }


def _serialize_channel(channel: discord.abc.GuildChannel) -> dict[str, Any]:
    category = getattr(channel, "category", None)
    overwrites: list[dict[str, Any]] = []
    for target, overwrite in getattr(channel, "overwrites", {}).items():
        allow, deny = overwrite.pair()
        overwrites.append(
            {
                "target_id": target.id,
                "target_type": "role" if isinstance(target, discord.Role) else "member",
                "target_name": getattr(target, "name", ""),
                "allow": allow.value,
                "deny": deny.value,
            }
        )
    return {
        "id": channel.id,
        "name": channel.name,
        "type": str(channel.type),
        "category_id": category.id if category else 0,
        "category_name": category.name if category else "",
        "topic": getattr(channel, "topic", None),
        "nsfw": bool(getattr(channel, "nsfw", False)),
        "slowmode_delay": _int(getattr(channel, "slowmode_delay", 0)),
        "bitrate": _int(getattr(channel, "bitrate", 0)),
        "user_limit": _int(getattr(channel, "user_limit", 0)),
        "position": _int(getattr(channel, "position", 0)),
        "overwrites": overwrites,
    }


def _serialize_deleted_message(message: discord.Message | None) -> dict[str, Any] | None:
    if message is None or not message.embeds:
        return None
    return {
        "channel_id": message.channel.id,
        "channel_name": getattr(message.channel, "name", ""),
        "category_id": getattr(getattr(message.channel, "category", None), "id", 0),
        "message_id": message.id,
        "content": message.content,
        "embeds": [embed.to_dict() for embed in message.embeds],
        "components": [component.to_dict() for component in message.components],
        "created_at": message.created_at.isoformat(),
    }


def _find_category(guild: discord.Guild, snapshot: dict[str, Any]) -> discord.CategoryChannel | None:
    category = guild.get_channel(_int(snapshot.get("category_id")))
    if isinstance(category, discord.CategoryChannel):
        return category
    name = str(snapshot.get("category_name") or "")
    if not name:
        return None
    return discord.utils.find(lambda item: item.name == name, guild.categories)


def _find_text_channel(guild: discord.Guild, snapshot: dict[str, Any]) -> discord.TextChannel | None:
    name = str(snapshot.get("channel_name") or "")
    if not name:
        return None
    return discord.utils.find(lambda item: item.name == name, guild.text_channels)


async def _restore_channel_overwrites(
    channel: discord.abc.GuildChannel,
    guild: discord.Guild,
    values: Any,
) -> None:
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        target = None
        if item.get("target_type") == "role":
            target = guild.get_role(_int(item.get("target_id")))
            if target is None and item.get("target_name"):
                target = discord.utils.find(
                    lambda role: role.name == str(item.get("target_name")),
                    guild.roles,
                )
        else:
            target = guild.get_member(_int(item.get("target_id")))
        if target is None:
            continue
        overwrite = discord.PermissionOverwrite.from_pair(
            discord.Permissions(_int(item.get("allow"))),
            discord.Permissions(_int(item.get("deny"))),
        )
        try:
            await channel.set_permissions(
                target,
                overwrite=overwrite,
                reason="Restauração seletiva pelo Reparador da Bella",
            )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Não foi possível restaurar overwrite de %s em %s.", target.id, channel.id)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
