from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .state import StateStore

SPAM_TRAP_SECTION = "spam_trap_configs"
EVIDENCE_BACKGROUND = (3, 3, 4)
EVIDENCE_PANEL = (16, 16, 19)
EVIDENCE_BORDER = (52, 53, 61)
EVIDENCE_TEXT = (248, 248, 250)
EVIDENCE_MUTED = (174, 177, 186)
EVIDENCE_RED = (237, 66, 69)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpamTrapConfig:
    guild_id: int
    enabled: bool = False
    channel_id: int | None = None
    log_channel_id: int | None = None
    history_scan_limit: int = 100
    history_window_hours: int = 24

    def to_dict(self) -> dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "enabled": self.enabled,
            "channel_id": self.channel_id,
            "log_channel_id": self.log_channel_id,
            "history_scan_limit": self.history_scan_limit,
            "history_window_hours": self.history_window_hours,
        }

    @classmethod
    def from_dict(cls, guild_id: int, value: Any) -> "SpamTrapConfig":
        data = value if isinstance(value, dict) else {}
        return cls(
            guild_id=guild_id,
            enabled=bool(data.get("enabled", False)),
            channel_id=_optional_int(data.get("channel_id")),
            log_channel_id=_optional_int(data.get("log_channel_id")),
            history_scan_limit=max(
                25,
                min(300, _integer(data.get("history_scan_limit"), 100)),
            ),
            history_window_hours=max(
                1,
                min(168, _integer(data.get("history_window_hours"), 24)),
            ),
        )


@dataclass(frozen=True)
class SpamTrapEvent:
    guild_id: int
    user_id: int
    user_mention: str
    channel_id: int
    message_id: int
    jump_url: str
    deleted_messages: int
    affected_channel_ids: tuple[int, ...]
    ban_applied: bool
    reason: str
    occurred_at: datetime
    errors: tuple[str, ...] = ()
    is_test: bool = False


@dataclass
class _RecentMessage:
    message: discord.Message
    signature: str
    observed_at: datetime


class SpamTrapLogView(discord.ui.LayoutView):
    def __init__(
        self,
        event: SpamTrapEvent,
        *,
        evidence_image: bytes | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.evidence_file: discord.File | None = None
        channels = (
            ", ".join(f"<#{channel_id}>" for channel_id in event.affected_channel_ids)
            or "Nenhum canal adicional"
        )
        if event.is_test:
            result = "Teste de entrega concluído"
        else:
            result = (
                "Banimento permanente aplicado"
                if event.ban_applied
                else "Banimento falhou"
            )
        occurred_at = event.occurred_at.astimezone().strftime("%d/%m/%Y às %H:%M:%S")
        container = discord.ui.Container(accent_color=discord.Color.red())
        container.add_item(
            discord.ui.TextDisplay(
                (
                    "## Teste do canal-isca\n"
                    "A entrega de relatórios está funcionando."
                    if event.is_test
                    else (
                        "## Segurança automática\n"
                        "Uma mensagem foi publicada no canal-isca configurado."
                    )
                )
            )
        )
        container.add_item(
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
        )
        if evidence_image:
            self.evidence_file = discord.File(
                io.BytesIO(evidence_image),
                filename=f"evidencia-canal-isca-{event.message_id}.png",
                description="Captura visual da mensagem que acionou o canal-isca",
            )
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        self.evidence_file,
                        description=(
                            "Evidência preservada antes da exclusão da mensagem"
                        ),
                    )
                )
            )
            container.add_item(
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
            )
        container.add_item(
            discord.ui.TextDisplay(
                f"**Usuário**\n{event.user_mention} (`{event.user_id}`)\n\n"
                f"**Canal de detecção**\n<#{event.channel_id}>\n\n"
                f"**Motivo**\n{event.reason}"
            )
        )
        container.add_item(
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
        )
        container.add_item(
            discord.ui.TextDisplay(
                f"**Ação aplicada**\n{result}\n\n"
                f"**Mensagens idênticas removidas**\n{event.deleted_messages}\n\n"
                f"**Canais afetados**\n{channels}"
            )
        )
        container.add_item(
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
        )
        details = (
            "\n".join(f"- {error}" for error in event.errors[:5])
            if event.errors
            else "Nenhuma falha operacional registrada."
        )
        container.add_item(
            discord.ui.TextDisplay(
                f"**Mensagem original**\n{event.jump_url}\n\n"
                f"**Horário**\n{occurred_at}\n\n"
                f"**Diagnóstico**\n{details}"
            )
        )
        container.add_item(
            discord.ui.TextDisplay(
                "-# Bella Security Center • Evento processado por lógica local"
            )
        )
        self.add_item(container)


class SpamTrapMonitor:
    def __init__(
        self,
        bot: discord.Client,
        state: StateStore,
        *,
        owner_id: int,
        recent_capacity: int = 2500,
    ) -> None:
        self.bot = bot
        self.state = state
        self.owner_id = owner_id
        self._config_cache: dict[int, SpamTrapConfig] = {}
        self._recent: deque[_RecentMessage] = deque(
            maxlen=max(250, min(10000, recent_capacity))
        )

    async def config(self, guild_id: int) -> SpamTrapConfig:
        cached = self._config_cache.get(guild_id)
        if cached is not None:
            return cached
        section = await self.state.get_section(SPAM_TRAP_SECTION)
        config = SpamTrapConfig.from_dict(guild_id, section.get(str(guild_id)))
        self._config_cache[guild_id] = config
        return config

    async def configure(
        self,
        guild_id: int,
        *,
        channel_id: int,
        log_channel_id: int,
        history_scan_limit: int = 100,
        history_window_hours: int = 24,
    ) -> SpamTrapConfig:
        config = SpamTrapConfig(
            guild_id=guild_id,
            enabled=True,
            channel_id=channel_id,
            log_channel_id=log_channel_id,
            history_scan_limit=max(25, min(300, history_scan_limit)),
            history_window_hours=max(1, min(168, history_window_hours)),
        )
        await self._save(config)
        return config

    async def set_enabled(self, guild_id: int, enabled: bool) -> SpamTrapConfig:
        current = await self.config(guild_id)
        config = SpamTrapConfig(
            guild_id=current.guild_id,
            enabled=enabled,
            channel_id=current.channel_id,
            log_channel_id=current.log_channel_id,
            history_scan_limit=current.history_scan_limit,
            history_window_hours=current.history_window_hours,
        )
        await self._save(config)
        return config

    def observe(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        now = datetime.now(timezone.utc)
        self._prune_recent(now - timedelta(hours=24))
        self._recent.append(
            _RecentMessage(
                message=message,
                signature=message_signature(message),
                observed_at=now,
            )
        )

    async def handle_message(self, message: discord.Message) -> bool:
        if message.guild is None or message.author.bot:
            return False
        config = await self.config(message.guild.id)
        if (
            not config.enabled
            or config.channel_id is None
            or message.channel.id != config.channel_id
            or message.author.id == self.owner_id
            or message.author.id == message.guild.owner_id
        ):
            return False

        evidence_image: bytes | None = None
        evidence_error = ""
        try:
            evidence_image = await asyncio.wait_for(
                self._capture_evidence(message),
                timeout=6,
            )
        except (TimeoutError, OSError, ValueError) as exc:
            evidence_error = f"Falha ao gerar evidência: {type(exc).__name__}"
        signature = message_signature(message)
        deleted_ids, channel_ids, errors = await self._delete_matching_messages(
            message,
            signature,
            config,
        )
        reason = (
            "Envio de conteúdo no canal-isca de segurança, configurado como área "
            "sem interação de membros."
        )
        ban_applied = False
        try:
            await message.guild.ban(
                message.author,
                reason=reason,
                delete_message_seconds=0,
            )
            ban_applied = True
        except TypeError:
            try:
                await message.guild.ban(
                    message.author,
                    reason=reason,
                    delete_message_days=0,
                )
                ban_applied = True
            except (discord.Forbidden, discord.HTTPException) as exc:
                errors.append(f"Falha ao banir: {type(exc).__name__}: {exc}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            errors.append(f"Falha ao banir: {type(exc).__name__}: {exc}")
        if evidence_error:
            errors.append(evidence_error)

        event = SpamTrapEvent(
            guild_id=message.guild.id,
            user_id=message.author.id,
            user_mention=message.author.mention,
            channel_id=message.channel.id,
            message_id=message.id,
            jump_url=message.jump_url,
            deleted_messages=len(deleted_ids),
            affected_channel_ids=tuple(sorted(channel_ids)),
            ban_applied=ban_applied,
            reason=reason,
            occurred_at=datetime.now(timezone.utc),
            errors=tuple(errors),
        )
        log_delivered, log_detail = await self._send_log(
            config,
            event,
            evidence_image=evidence_image,
        )
        await self.state.update_section(
            "spam_trap_events",
            f"{message.guild.id}:{message.id}",
            {
                "guild_id": event.guild_id,
                "user_id": event.user_id,
                "channel_id": event.channel_id,
                "message_id": event.message_id,
                "jump_url": event.jump_url,
                "deleted_messages": event.deleted_messages,
                "affected_channel_ids": list(event.affected_channel_ids),
                "ban_applied": event.ban_applied,
                "reason": event.reason,
                "occurred_at": event.occurred_at.isoformat(),
                "errors": list(event.errors),
                "log_delivered": log_delivered,
                "log_detail": log_detail,
            },
        )
        await self._store_log_delivery(
            guild_id=message.guild.id,
            channel_id=config.log_channel_id,
            delivered=log_delivered,
            detail=log_detail,
        )
        return True

    async def send_test_log(
        self,
        guild: discord.Guild,
        requester: discord.abc.User,
        *,
        source_channel_id: int,
    ) -> tuple[bool, str]:
        config = await self.config(guild.id)
        if not config.log_channel_id:
            return False, "O canal de logs não está configurado."
        avatar_bytes = await _read_avatar(requester)
        evidence_image = render_spam_trap_evidence(
            display_name=str(
                getattr(requester, "display_name", "")
                or getattr(requester, "name", "")
                or requester.id
            ),
            username=str(
                getattr(requester, "name", "")
                or getattr(requester, "display_name", "")
                or requester.id
            ),
            user_id=requester.id,
            channel_name="teste-canal-isca",
            channel_id=source_channel_id,
            created_at=datetime.now(timezone.utc),
            content=(
                "Este é um teste de entrega do relatório do canal-isca. "
                "Nenhuma punição foi aplicada."
            ),
            attachment_names=[],
            avatar_bytes=avatar_bytes,
            image_bytes=None,
        )
        event = SpamTrapEvent(
            guild_id=guild.id,
            user_id=requester.id,
            user_mention=getattr(requester, "mention", f"<@{requester.id}>"),
            channel_id=source_channel_id,
            message_id=int(datetime.now(timezone.utc).timestamp()),
            jump_url="Teste manual sem mensagem de origem.",
            deleted_messages=0,
            affected_channel_ids=(),
            ban_applied=False,
            reason="Validação manual da entrega de relatórios do canal-isca.",
            occurred_at=datetime.now(timezone.utc),
            is_test=True,
        )
        delivered, detail = await self._send_log(
            config,
            event,
            evidence_image=evidence_image,
        )
        await self._store_log_delivery(
            guild_id=guild.id,
            channel_id=config.log_channel_id,
            delivered=delivered,
            detail=detail,
        )
        return delivered, detail

    async def last_log_delivery(self, guild_id: int) -> dict[str, Any]:
        section = await self.state.get_section("spam_trap_log_delivery")
        value = section.get(str(guild_id), {})
        return value if isinstance(value, dict) else {}

    async def _delete_matching_messages(
        self,
        trigger: discord.Message,
        signature: str,
        config: SpamTrapConfig,
    ) -> tuple[set[int], set[int], list[str]]:
        deleted_ids: set[int] = set()
        channel_ids: set[int] = set()
        errors: list[str] = []
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=config.history_window_hours
        )
        self._prune_recent(cutoff)

        indexed = [
            item.message
            for item in self._recent
            if item.message.guild is not None
            and item.message.guild.id == trigger.guild.id
            and item.message.author.id == trigger.author.id
            and item.signature == signature
            and item.observed_at >= cutoff
        ]
        for candidate in indexed:
            await self._delete_candidate(
                candidate,
                deleted_ids,
                channel_ids,
                errors,
            )

        channels: list[Any] = list(getattr(trigger.guild, "text_channels", ()))
        channels.extend(getattr(trigger.guild, "threads", ()))
        for channel in channels:
            if not hasattr(channel, "history"):
                continue
            try:
                async for candidate in channel.history(
                    limit=config.history_scan_limit,
                    after=cutoff,
                    oldest_first=False,
                ):
                    if (
                        candidate.id in deleted_ids
                        or candidate.author.id != trigger.author.id
                        or message_signature(candidate) != signature
                    ):
                        continue
                    await self._delete_candidate(
                        candidate,
                        deleted_ids,
                        channel_ids,
                        errors,
                    )
            except (discord.Forbidden, discord.HTTPException) as exc:
                errors.append(
                    f"Histórico indisponível em #{getattr(channel, 'name', channel.id)}: "
                    f"{type(exc).__name__}"
                )
        return deleted_ids, channel_ids, errors

    @staticmethod
    async def _delete_candidate(
        candidate: discord.Message,
        deleted_ids: set[int],
        channel_ids: set[int],
        errors: list[str],
    ) -> None:
        if candidate.id in deleted_ids:
            return
        try:
            await candidate.delete()
            deleted_ids.add(candidate.id)
            channel_ids.add(candidate.channel.id)
        except (discord.NotFound,):
            deleted_ids.add(candidate.id)
        except (discord.Forbidden, discord.HTTPException) as exc:
            errors.append(
                f"Falha ao apagar mensagem {candidate.id}: {type(exc).__name__}"
            )

    async def _send_log(
        self,
        config: SpamTrapConfig,
        event: SpamTrapEvent,
        *,
        evidence_image: bytes | None,
    ) -> tuple[bool, str]:
        if not config.log_channel_id:
            return False, "Canal de logs não configurado."
        channel = self.bot.get_channel(config.log_channel_id)
        if channel is None:
            guild = self.bot.get_guild(event.guild_id)
            if guild is not None:
                channel = guild.get_channel(config.log_channel_id)
        if channel is None:
            fetch_channel = getattr(self.bot, "fetch_channel", None)
            if callable(fetch_channel):
                try:
                    channel = await fetch_channel(config.log_channel_id)
                except Exception as exc:
                    LOGGER.warning(
                        "Canal-isca: %s (guild=%s canal=%s)",
                        _delivery_error("buscar o canal", exc),
                        event.guild_id,
                        config.log_channel_id,
                    )
        if channel is None or not hasattr(channel, "send"):
            return (
                False,
                f"Canal de logs {config.log_channel_id} não encontrado ou incompatível.",
            )

        failures: list[str] = []
        attempts: list[tuple[str, bytes | None]] = []
        if evidence_image:
            attempts.append(("Components V2 com evidência", evidence_image))
        attempts.append(("Components V2 sem evidência", None))
        for label, image in attempts:
            try:
                await channel.send(
                    view=SpamTrapLogView(
                        event,
                        evidence_image=image,
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                LOGGER.info(
                    "Log do canal-isca entregue: guild=%s canal=%s formato=%s",
                    event.guild_id,
                    config.log_channel_id,
                    label,
                )
                return True, f"Enviado como {label}."
            except Exception as exc:
                failures.append(_delivery_error(label, exc))
                LOGGER.warning(
                    "Falha no log do canal-isca: guild=%s canal=%s formato=%s erro=%s",
                    event.guild_id,
                    config.log_channel_id,
                    label,
                    failures[-1],
                )
                await asyncio.sleep(0.35)

        try:
            await channel.send(
                content=_plain_log_message(event),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            LOGGER.info(
                "Log do canal-isca entregue com fallback: guild=%s canal=%s",
                event.guild_id,
                config.log_channel_id,
            )
            return True, "Enviado como texto simples após falha no Components V2."
        except Exception as exc:
            failures.append(_delivery_error("texto simples", exc))
            detail = " | ".join(failures)[-1800:]
            LOGGER.error(
                "Não foi possível entregar o log do canal-isca: "
                "guild=%s canal=%s erros=%s",
                event.guild_id,
                config.log_channel_id,
                detail,
            )
            return False, detail

    async def _store_log_delivery(
        self,
        *,
        guild_id: int,
        channel_id: int | None,
        delivered: bool,
        detail: str,
    ) -> None:
        await self.state.update_section(
            "spam_trap_log_delivery",
            str(guild_id),
            {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "delivered": delivered,
                "detail": detail,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def _capture_evidence(
        self,
        message: discord.Message,
    ) -> bytes:
        avatar_bytes = await _read_avatar(message.author)
        image_bytes: bytes | None = None
        attachment_names: list[str] = []
        for attachment in getattr(message, "attachments", ()):
            filename = str(getattr(attachment, "filename", "arquivo"))[:120]
            attachment_names.append(filename)
            if image_bytes is not None or not _attachment_is_image(attachment):
                continue
            size = int(getattr(attachment, "size", 0) or 0)
            if size > 12 * 1024 * 1024:
                continue
            image_bytes = await _read_attachment(attachment)
        created_at = getattr(message, "created_at", None)
        if not isinstance(created_at, datetime):
            created_at = datetime.now(timezone.utc)
        author_name = str(
            getattr(message.author, "display_name", "")
            or getattr(message.author, "name", "")
            or f"Usuário {message.author.id}"
        )
        username = str(
            getattr(message.author, "name", "")
            or getattr(message.author, "display_name", "")
            or message.author.id
        )
        content = str(
            getattr(message, "clean_content", "")
            or getattr(message, "content", "")
        )
        return render_spam_trap_evidence(
            display_name=author_name,
            username=username,
            user_id=message.author.id,
            channel_name=str(
                getattr(message.channel, "name", message.channel.id)
            ),
            channel_id=message.channel.id,
            created_at=created_at,
            content=content,
            attachment_names=attachment_names,
            avatar_bytes=avatar_bytes,
            image_bytes=image_bytes,
        )

    async def _save(self, config: SpamTrapConfig) -> None:
        await self.state.update_section(
            SPAM_TRAP_SECTION,
            str(config.guild_id),
            config.to_dict(),
        )
        self._config_cache[config.guild_id] = config

    def _prune_recent(self, cutoff: datetime) -> None:
        while self._recent and self._recent[0].observed_at < cutoff:
            self._recent.popleft()


def message_signature(message: discord.Message) -> str:
    content = " ".join(str(getattr(message, "content", "")).casefold().split())
    attachments = sorted(
        (
            str(getattr(item, "filename", "")).casefold(),
            int(getattr(item, "size", 0) or 0),
            str(getattr(item, "content_type", "") or "").casefold(),
        )
        for item in getattr(message, "attachments", ())
    )
    stickers = sorted(
        str(getattr(item, "id", ""))
        for item in getattr(message, "stickers", ())
    )
    payload = repr((content, attachments, stickers)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _plain_log_message(event: SpamTrapEvent) -> str:
    occurred_at = event.occurred_at.astimezone().strftime("%d/%m/%Y às %H:%M:%S")
    if event.is_test:
        return (
            "**Teste do canal-isca**\n"
            "A entrega de relatórios está funcionando.\n\n"
            f"Canal configurado: <#{event.channel_id}>\n"
            f"Horário: {occurred_at}"
        )
    result = (
        "Banimento permanente aplicado"
        if event.ban_applied
        else "Não foi possível aplicar o banimento"
    )
    affected_channels = (
        ", ".join(f"<#{channel_id}>" for channel_id in event.affected_channel_ids)
        or "Nenhum canal adicional"
    )
    diagnostics = (
        "\n".join(f"- {item}" for item in event.errors[:5])
        if event.errors
        else "Nenhuma falha operacional registrada."
    )
    return (
        "**Segurança automática — canal-isca**\n\n"
        f"Usuário: {event.user_mention} (`{event.user_id}`)\n"
        f"Canal: <#{event.channel_id}>\n"
        f"Motivo: {event.reason}\n"
        f"Ação: {result}\n"
        f"Mensagens idênticas removidas: {event.deleted_messages}\n"
        f"Canais afetados: {affected_channels}\n"
        f"Mensagem original: {event.jump_url}\n"
        f"Horário: {occurred_at}\n"
        f"Diagnóstico:\n{diagnostics}"
    )[:1990]


def _delivery_error(stage: str, exc: Exception) -> str:
    text = " ".join(str(exc).split())
    if len(text) > 320:
        text = f"{text[:317]}..."
    return f"{stage}: {type(exc).__name__}: {text or 'sem detalhes'}"


def render_spam_trap_evidence(
    *,
    display_name: str,
    username: str,
    user_id: int,
    channel_name: str,
    channel_id: int,
    created_at: datetime,
    content: str,
    attachment_names: list[str],
    avatar_bytes: bytes | None,
    image_bytes: bytes | None,
) -> bytes:
    width = 1200
    title_font = _evidence_font(30, bold=True)
    body_font = _evidence_font(24)
    label_font = _evidence_font(15, bold=True)
    meta_font = _evidence_font(17)
    draft = Image.new("RGB", (width, 200), EVIDENCE_BACKGROUND)
    draft_draw = ImageDraw.Draw(draft)
    evidence_image = _load_evidence_image(image_bytes)
    lines = _wrap_evidence_text(
        draft_draw,
        content.strip() or "Mensagem sem conteúdo textual.",
        body_font,
        1040,
        max_lines=10 if evidence_image is not None else 18,
    )
    text_height = max(46, len(lines) * 34)
    attachment_text = ", ".join(attachment_names[:5])
    if len(attachment_names) > 5:
        attachment_text += f" e mais {len(attachment_names) - 5}"
    attachment_height = 42 if attachment_text else 0
    image_height = 0
    if evidence_image is not None:
        evidence_image.thumbnail((1040, 650), Image.Resampling.LANCZOS)
        image_height = evidence_image.height + 50
    content_top = 220
    content_height = 78 + text_height + attachment_height + image_height
    height = max(520, min(1500, content_top + content_height + 80))
    image = Image.new("RGB", (width, height), EVIDENCE_BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 8), fill=EVIDENCE_RED)
    draw.text(
        (50, 30),
        "EVIDÊNCIA DE MODERAÇÃO",
        font=label_font,
        fill=EVIDENCE_RED,
    )
    draw.text(
        (50, 58),
        "Conteúdo preservado antes da remoção automática",
        font=meta_font,
        fill=EVIDENCE_MUTED,
    )

    _draw_evidence_avatar(
        image,
        avatar_bytes,
        bounds=(50, 105, 126, 181),
    )
    draw.text(
        (150, 105),
        _truncate_evidence(display_name, 38),
        font=title_font,
        fill=EVIDENCE_TEXT,
    )
    draw.text(
        (152, 145),
        f"@{_truncate_evidence(username, 28)}  •  ID {user_id}",
        font=meta_font,
        fill=EVIDENCE_MUTED,
    )
    local_time = created_at.astimezone()
    time_text = local_time.strftime("%d/%m/%Y às %H:%M:%S")
    time_box = draw.textbbox((0, 0), time_text, font=meta_font)
    draw.text(
        (1150 - (time_box[2] - time_box[0]), 115),
        time_text,
        font=meta_font,
        fill=EVIDENCE_MUTED,
    )
    channel_text = f"#{channel_name}  •  Canal {channel_id}"
    channel_box = draw.textbbox((0, 0), channel_text, font=meta_font)
    draw.text(
        (1150 - (channel_box[2] - channel_box[0]), 148),
        channel_text,
        font=meta_font,
        fill=EVIDENCE_MUTED,
    )

    panel_bottom = min(height - 55, content_top + content_height)
    draw.rounded_rectangle(
        (40, content_top, 1160, panel_bottom),
        radius=24,
        fill=EVIDENCE_PANEL,
        outline=EVIDENCE_BORDER,
        width=2,
    )
    draw.text(
        (75, content_top + 30),
        "MENSAGEM ENVIADA",
        font=label_font,
        fill=EVIDENCE_MUTED,
    )
    text_y = content_top + 66
    for line in lines:
        draw.text(
            (75, text_y),
            line,
            font=body_font,
            fill=EVIDENCE_TEXT,
        )
        text_y += 34
    if attachment_text:
        text_y += 12
        draw.text(
            (75, text_y),
            f"Anexos: {_truncate_evidence(attachment_text, 105)}",
            font=meta_font,
            fill=EVIDENCE_MUTED,
        )
        text_y += 42
    if evidence_image is not None and text_y + evidence_image.height + 25 <= panel_bottom:
        image_x = 75 + (1040 - evidence_image.width) // 2
        draw.rounded_rectangle(
            (
                image_x - 3,
                text_y - 3,
                image_x + evidence_image.width + 3,
                text_y + evidence_image.height + 3,
            ),
            radius=14,
            fill=EVIDENCE_BORDER,
        )
        mask = Image.new("L", evidence_image.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, evidence_image.width, evidence_image.height),
            radius=12,
            fill=255,
        )
        image.paste(evidence_image, (image_x, text_y), mask)
    footer = "BELLA SECURITY CENTER  •  REGISTRO LOCAL"
    draw.text(
        (50, height - 35),
        footer,
        font=label_font,
        fill=EVIDENCE_MUTED,
    )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def _read_avatar(author: discord.abc.User) -> bytes | None:
    asset = getattr(author, "display_avatar", None)
    if asset is None or not hasattr(asset, "read"):
        return None
    try:
        return await asset.read()
    except (discord.HTTPException, OSError):
        return None


async def _read_attachment(attachment: discord.Attachment) -> bytes | None:
    try:
        return await attachment.read(use_cached=True)
    except TypeError:
        try:
            return await attachment.read()
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
            OSError,
        ):
            return None
    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException,
        OSError,
    ):
        return None


def _attachment_is_image(attachment: discord.Attachment) -> bool:
    content_type = str(getattr(attachment, "content_type", "") or "").casefold()
    filename = str(getattr(attachment, "filename", "") or "").casefold()
    return content_type.startswith("image/") or filename.endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif")
    )


def _load_evidence_image(data: bytes | None) -> Image.Image | None:
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as source:
            return ImageOps.exif_transpose(source).convert("RGB")
    except (OSError, ValueError):
        return None


def _draw_evidence_avatar(
    image: Image.Image,
    avatar_bytes: bytes | None,
    *,
    bounds: tuple[int, int, int, int],
) -> None:
    left, top, right, bottom = bounds
    size = min(right - left, bottom - top)
    avatar = _load_evidence_image(avatar_bytes)
    if avatar is None:
        avatar = Image.new("RGB", (size, size), (46, 47, 54))
    else:
        avatar = ImageOps.fit(
            avatar,
            (size, size),
            method=Image.Resampling.LANCZOS,
        )
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    image.paste(avatar, (left, top), mask)
    draw = ImageDraw.Draw(image)
    draw.ellipse(
        (left - 2, top - 2, left + size + 1, top + size + 1),
        outline=EVIDENCE_BORDER,
        width=2,
    )


def _wrap_evidence_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    *,
    max_lines: int,
) -> list[str]:
    lines: list[str] = []
    paragraphs = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for paragraph in paragraphs:
        words = paragraph.split()
        if not words:
            if lines and lines[-1]:
                lines.append("")
            continue
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            width = draw.textbbox((0, 0), candidate, font=font)[2]
            if current and width > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
            if len(lines) >= max_lines:
                break
        if len(lines) >= max_lines:
            break
        if current:
            lines.append(current)
    if not lines:
        return ["Mensagem sem conteúdo textual."]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if len(lines) == max_lines:
        lines[-1] = _truncate_evidence(lines[-1], 75).rstrip(".") + "..."
    return lines


def _evidence_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        Path(
            "C:/Windows/Fonts/seguisb.ttf"
            if bold
            else "C:/Windows/Fonts/segoeui.ttf"
        ),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        if bold
        else Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for path in candidates:
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _truncate_evidence(value: str, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
