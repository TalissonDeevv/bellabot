from __future__ import annotations

import re
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from .identity import OWNER_DISCORD_ID
from .state import StateStore

STATUS_KEY = "talisson_availability_status"
CONFIG_SECTION = "talisson_reminder_configs"
PENDING_SECTION = "talisson_pending_reminders"
REMINDER_SECTION = "talisson_reminders"
PERSONAL_REMINDER_SECTION = "talisson_personal_reminders"
DEFAULT_PENDING_TTL_SECONDS = 30 * 60
DEFAULT_NOTIFICATION_OFFSETS_MINUTES = (24 * 60, 120, 30)
PERSONAL_REMINDER_PENDING = "pendente"
PERSONAL_REMINDER_SENT = "enviado"
PERSONAL_REMINDER_CANCELLED = "cancelado"
REMINDER_STATUS_PENDING = "pendente"
REMINDER_STATUS_RESPONDED = "respondido"
REMINDER_STATUS_CLOSED = "encerrado"
WEEKDAY_NAMES = {
    "segunda": 0,
    "segunda feira": 0,
    "terca": 1,
    "terca feira": 1,
    "terça": 1,
    "terça feira": 1,
    "quarta": 2,
    "quarta feira": 2,
    "quinta": 3,
    "quinta feira": 3,
    "sexta": 4,
    "sexta feira": 4,
    "sabado": 5,
    "sábado": 5,
    "domingo": 6,
}


class TalissonAvailabilityCenter:
    def __init__(
        self,
        bot: discord.Client,
        state: StateStore,
        *,
        service: Any | None = None,
        owner_id: int = OWNER_DISCORD_ID,
        pending_ttl_seconds: int = DEFAULT_PENDING_TTL_SECONDS,
    ) -> None:
        self.bot = bot
        self.state = state
        self.owner_id = int(owner_id)
        self.pending_ttl_seconds = max(60, int(pending_ttl_seconds))

    async def handle_message(
        self,
        message: discord.Message,
        *,
        invoked: bool,
        content: str,
    ) -> bool:
        if message.guild is None:
            return False
        text = content.strip()
        if not text:
            return False

        if message.author.id == self.owner_id:
            if await self._handle_owner_reminder_response(message, text):
                return True
            if invoked and self._looks_like_reminder_channel_config(message, text):
                return await self._handle_natural_channel_config(message)
            if invoked and self._is_personal_schedule_query(text):
                await self._reply(message, await self._agenda_summary(text))
                return True
            if (invoked or self._looks_like_personal_reminder(text)) and self._looks_like_personal_reminder(text):
                parsed = self._parse_personal_reminder(text)
                if parsed is not None:
                    record = await self.create_personal_reminder(
                        guild_id=message.guild.id,
                        channel_id=message.channel.id,
                        message_id=message.id,
                        creator_id=message.author.id,
                        title=str(parsed["title"]),
                        description=str(parsed["description"]),
                        scheduled_at=str(parsed["scheduled_at"]),
                        priority=str(parsed["priority"]),
                    )
                    reply = "Perfeito. Vou lembrar você desse compromisso antes do horário marcado."
                    if self._could_mark_absence(text):
                        reply += " Se quiser, também posso tratar esse período como indisponibilidade."
                    await self._reply(message, reply)
                    return True
            if (invoked or self._is_strong_return(text)) and self._looks_like_return(text):
                await self.clear_status()
                await self._reply(
                    message,
                    "Bem-vindo de volta, Talisson. Vou considerar que você está disponível novamente.",
                )
                return True
            if (invoked or self._is_strong_absence(text)) and self._looks_like_absence(text):
                reason = self._extract_reason(text)
                await self.set_status(
                    reason=reason,
                    original_text=text,
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    message_id=message.id,
                )
                await self._reply(
                    message,
                    (
                        "Pode deixar. Vou avisar as pessoas que você não estará "
                        f"disponível devido à {reason}. Quando voltar, basta me avisar."
                    ),
                )
                return True

        if await self._handle_member_reminder_reply(message, text):
            return True

        if self._is_availability_question(text) and self._mentions_owner(message, text):
            await self._reply(message, await self._availability_answer())
            return True

        if self._is_scheduling_request(text) and self._mentions_owner(message, text):
            await self._save_pending(
                message.guild.id,
                message.author.id,
                text,
                source="agendamento",
            )
            await self._reply(
                message,
                "Claro. Me informe o assunto e sua disponibilidade, e registrarei a solicitação para análise.",
            )
            return True

        pending = await self._pending_for(message.guild.id, message.author.id)
        if pending is not None and message.author.id != self.owner_id:
            source = str(pending.get("source") or "continuação")
            record = await self._save_reminder_from_message(
                message,
                text,
                source=source,
            )
            await self._clear_pending(message.guild.id, message.author.id)
            await self._reply(
                message,
                _reminder_ack(record, direct=False),
            )
            return True

        if self._is_invite_request(text) and self._mentions_owner(message, text):
            record = await self._save_reminder_from_message(
                message,
                text,
                source="convite",
            )
            await self._reply(message, _reminder_ack(record, direct=True))
            return True

        if self._is_direct_reminder_request(text) and self._mentions_owner(message, text):
            record = await self._save_reminder_from_message(
                message,
                text,
                source="recado",
            )
            await self._reply(message, _reminder_ack(record, direct=True))
            return True

        if self._is_contact_request(text) and self._mentions_owner(message, text):
            await self._save_pending(message.guild.id, message.author.id, text)
            await self._reply(
                message,
                "Sem problemas. O que você gostaria que eu avisasse a ele?",
            )
            return True

        status = await self.status()
        if status and self._mentions_owner(message, text):
            reason = str(status.get("reason") or "não está disponível agora")
            phrase = _availability_phrase(reason)
            await self._save_pending(message.guild.id, message.author.id, text)
            await self._reply(
                message,
                (
                    f"O Talisson não está disponível neste momento porque está {phrase}.\n\n"
                    "Se desejar, descreva o assunto e eu registrarei um recado para ele."
                ),
            )
            return True

        return False

    async def configure_reminder_channel(
        self,
        guild_id: int,
        channel_id: int,
    ) -> None:
        await self.state.mutate_section(
            CONFIG_SECTION,
            lambda values: values.__setitem__(
                str(guild_id),
                {"channel_id": int(channel_id), "updated_at": _now_iso()},
            ),
        )

    async def reminder_channel_id(self, guild_id: int) -> int | None:
        configs = await self.state.get_section(CONFIG_SECTION)
        value = configs.get(str(guild_id), {})
        if not isinstance(value, dict):
            return None
        try:
            channel_id = int(value.get("channel_id") or 0)
        except (TypeError, ValueError):
            return None
        return channel_id or None

    async def set_status(
        self,
        *,
        reason: str,
        original_text: str,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> dict[str, Any]:
        status = {
            "active": True,
            "reason": reason,
            "original_text": original_text[:500],
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "message_id": int(message_id),
            "created_at": _now_iso(),
        }
        await self.state.set(STATUS_KEY, status)
        await self.state.set(
            "talisson_availability_last_event",
            {
                "type": "set",
                "reason": reason,
                "guild_id": int(guild_id),
                "channel_id": int(channel_id),
                "message_id": int(message_id),
                "created_at": status["created_at"],
            },
        )
        return status

    async def clear_status(self) -> None:
        await self.state.remove(STATUS_KEY)
        await self.state.set(
            "talisson_availability_last_event",
            {"type": "clear", "created_at": _now_iso()},
        )

    async def status(self) -> dict[str, Any] | None:
        value = await self.state.get(STATUS_KEY)
        if not isinstance(value, dict) or not value.get("active"):
            return None
        return value

    async def reminder_count(self, guild_id: int | None = None) -> int:
        reminders = await self.state.get_section(REMINDER_SECTION)
        if guild_id is None:
            return len(reminders)
        return sum(
            1
            for value in reminders.values()
            if isinstance(value, dict) and int(value.get("guild_id") or 0) == guild_id
        )

    def build_status_embed(
        self,
        *,
        guild_id: int,
        channel_id: int | None,
        active_status: dict[str, Any] | None,
        reminder_count: int,
    ) -> discord.Embed:
        embed = discord.Embed(
            title="Central de lembretes da Bella",
            color=discord.Color.from_rgb(24, 119, 242),
            timestamp=_now_dt(),
        )
        embed.add_field(
            name="Canal de lembretes",
            value=f"<#{channel_id}>" if channel_id else "Ainda não configurado",
            inline=False,
        )
        if active_status:
            embed.add_field(
                name="Disponibilidade de Talisson",
                value=f"Indisponível: {active_status.get('reason')}",
                inline=False,
            )
        else:
            embed.add_field(
                name="Disponibilidade de Talisson",
                value="Disponível no momento",
                inline=False,
            )
        embed.add_field(
            name="Recados registrados",
            value=str(reminder_count),
            inline=True,
        )
        embed.set_footer(text="Bella | assistente executivo")
        return embed

    async def create_personal_reminder(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        creator_id: int,
        title: str,
        description: str,
        scheduled_at: str,
        priority: str,
    ) -> dict[str, Any]:
        record_id = f"{int(guild_id)}:{int(time.time() * 1000)}:{int(message_id)}"
        record = {
            "id": record_id,
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "message_id": int(message_id),
            "creator_id": int(creator_id),
            "title": title[:160] or "Compromisso",
            "description": description[:1200] or title[:1200],
            "scheduled_at": scheduled_at,
            "priority": priority,
            "message_original": description[:1200] or title[:1200],
            "antecedencia_minutos": None,
            "notification_offsets_minutes": list(
                DEFAULT_NOTIFICATION_OFFSETS_MINUTES
            ),
            "status": PERSONAL_REMINDER_PENDING,
            "created_at": _now_iso(),
            "notifications_sent": [],
        }
        await self.state.update_section(PERSONAL_REMINDER_SECTION, record_id, record)
        await self.state.set(
            "talisson_personal_reminders_last_event",
            {
                "type": "created",
                "record_id": record_id,
                "scheduled_at": scheduled_at,
                "created_at": _now_iso(),
            },
        )
        return record

    async def create_deterministic_reminder(
        self,
        *,
        guild_id: int,
        channel_id: int,
        creator_id: int,
        message: str,
        scheduled_at: str,
        antecedence_minutes: int = 0,
    ) -> dict[str, Any]:
        original_message = str(message)
        offset = max(0, int(antecedence_minutes))
        record_id = f"{int(guild_id)}:{int(time.time() * 1000)}:{int(creator_id)}"
        record = {
            "id": record_id,
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "message_id": 0,
            "creator_id": int(creator_id),
            "title": "Lembrete",
            "description": original_message,
            "message_original": original_message,
            "scheduled_at": scheduled_at,
            "priority": "normal",
            "antecedencia_minutos": offset,
            "notification_offsets_minutes": [offset],
            "status": PERSONAL_REMINDER_PENDING,
            "created_at": _now_iso(),
            "notifications_sent": [],
            "deterministic": True,
        }
        await self.state.update_section(PERSONAL_REMINDER_SECTION, record_id, record)
        await self.state.set(
            "talisson_personal_reminders_last_event",
            {
                "type": "created",
                "record_id": record_id,
                "scheduled_at": scheduled_at,
                "created_at": _now_iso(),
            },
        )
        return record

    async def dispatch_due_personal_reminders(self) -> int:
        now = _now_dt()
        records = await self.state.get_section(PERSONAL_REMINDER_SECTION)
        sent = 0
        changed = False
        for key, record in list(records.items()):
            if not isinstance(record, dict):
                continue
            status = str(record.get("status") or "")
            if status not in {"scheduled", PERSONAL_REMINDER_PENDING}:
                continue
            scheduled_at = _parse_iso_datetime(str(record.get("scheduled_at") or ""))
            if scheduled_at is None:
                record["status"] = PERSONAL_REMINDER_CANCELLED
                changed = True
                continue
            sent_offsets = {
                int(value)
                for value in record.get("notifications_sent", [])
                if str(value).lstrip("-").isdigit()
            }
            configured_offsets = record.get("notification_offsets_minutes")
            if not isinstance(configured_offsets, list) or not configured_offsets:
                configured_offsets = list(DEFAULT_NOTIFICATION_OFFSETS_MINUTES)
            offsets = sorted(
                {
                    max(0, int(value))
                    for value in configured_offsets
                    if str(value).lstrip("-").isdigit()
                }
            )
            if now >= scheduled_at:
                if not sent_offsets:
                    if await self._send_personal_reminder_notification(record, 0):
                        record.setdefault("notifications_sent", []).append(0)
                        record["last_notification_at"] = now.isoformat()
                        sent += 1
                record["status"] = PERSONAL_REMINDER_SENT
                record["sent_at"] = now.isoformat()
                changed = True
                continue
            for offset in offsets:
                if offset in sent_offsets:
                    continue
                notify_at = scheduled_at - timedelta(minutes=offset)
                if now >= notify_at:
                    if await self._send_personal_reminder_notification(record, offset):
                        record.setdefault("notifications_sent", []).append(offset)
                        record["last_notification_at"] = now.isoformat()
                        sent += 1
                        changed = True
                        if record.get("deterministic"):
                            record["status"] = PERSONAL_REMINDER_SENT
                            record["sent_at"] = now.isoformat()
                    break
        if changed:
            await self.state.mutate_section(
                PERSONAL_REMINDER_SECTION,
                lambda values: values.update(records),
            )
        return sent

    async def _send_personal_reminder_notification(
        self,
        record: dict[str, Any],
        offset_minutes: int,
    ) -> bool:
        guild_id = int(record.get("guild_id") or 0)
        creator_id = int(record.get("creator_id") or self.owner_id)
        channel_id = (
            await self.reminder_channel_id(guild_id)
            if creator_id == self.owner_id
            else None
        )
        if channel_id is None:
            channel_id = int(record.get("channel_id") or 0) or None
        if channel_id is None:
            return False
        channel = self.bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "send"):
            return False
        text = await self._notification_text(record, offset_minutes)
        try:
            await channel.send(
                content=f"<@{creator_id}>\n{text}",
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=False,
                    everyone=False,
                ),
            )
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _notification_text(
        self,
        record: dict[str, Any],
        offset_minutes: int,
    ) -> str:
        original = str(
            record.get("message_original")
            or record.get("description")
            or record.get("title")
            or ""
        )
        return f"Lembrete: {original}"

    async def _agenda_summary(self, text: str) -> str:
        now = _now_dt()
        start, end, label = _query_window(text, now)
        records = await self.state.get_section(PERSONAL_REMINDER_SECTION)
        items: list[tuple[datetime, dict[str, Any]]] = []
        for record in records.values():
            if not isinstance(record, dict):
                continue
            if str(record.get("status") or "") not in {
                "scheduled",
                PERSONAL_REMINDER_PENDING,
            }:
                continue
            scheduled_at = _parse_iso_datetime(str(record.get("scheduled_at") or ""))
            if scheduled_at is None:
                continue
            if start <= scheduled_at < end:
                items.append((scheduled_at, record))
        items.sort(key=lambda item: item[0])
        if not items:
            return f"Você não tem compromissos marcados para {label}."
        lines = [
            f"Sua agenda para {label}:",
            "",
        ]
        for scheduled_at, record in items[:8]:
            lines.append(
                f"- {scheduled_at.strftime('%d/%m às %H:%M')} — "
                f"{record.get('title') or 'Compromisso'}"
            )
        if len(items) > 8:
            lines.append(f"- E mais {len(items) - 8} compromisso(s).")
        return "\n".join(lines)

    async def _availability_answer(self) -> str:
        status = await self.status()
        if status:
            reason = str(status.get("reason") or "um compromisso informado")
            return (
                f"Talisson está ausente no momento devido a {_availability_phrase(reason)}. "
                "Assim que retornar poderá analisar novas solicitações.\n\n"
                "Normalmente, ele atende de segunda a sexta, das 09h às 18h, sempre com horário marcado."
            )
        return (
            "Atualmente o Talisson atende de segunda a sexta, das 09h às 18h, "
            "sempre mediante agendamento prévio. Aos sábados e domingos ele não "
            "realiza atendimentos.\n\n"
            "Se quiser marcar um horário, me informe o assunto e sua disponibilidade."
        )

    async def _handle_natural_channel_config(
        self,
        message: discord.Message,
    ) -> bool:
        channel_id = self._first_channel_id(message)
        if channel_id is None:
            await self._reply(
                message,
                "Claro, mas me diga qual canal devo usar para centralizar os recados.",
            )
            return True
        await self.configure_reminder_channel(message.guild.id, channel_id)
        await self._reply(
            message,
            f"Perfeito. Vou centralizar os recados e lembretes em <#{channel_id}>.",
        )
        return True

    async def _save_reminder_from_message(
        self,
        message: discord.Message,
        subject: str,
        *,
        source: str,
    ) -> dict[str, Any]:
        record_id = f"{message.guild.id}:{int(time.time() * 1000)}:{message.id}"
        category = categorize_reminder(subject)
        record = {
            "id": record_id,
            "guild_id": message.guild.id,
            "channel_id": message.channel.id,
            "channel_name": getattr(message.channel, "name", str(message.channel.id)),
            "message_id": message.id,
            "message_url": getattr(message, "jump_url", ""),
            "author_id": message.author.id,
            "author_name": getattr(message.author, "display_name", str(message.author)),
            "subject": subject[:1500],
            "category": category,
            "source": source,
            "status": REMINDER_STATUS_PENDING,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "history": [
                {
                    "from": "member",
                    "user_id": int(message.author.id),
                    "channel_id": int(message.channel.id),
                    "message_id": int(message.id),
                    "message_url": getattr(message, "jump_url", ""),
                    "text": subject[:1500],
                    "created_at": _now_iso(),
                }
            ],
        }
        delivered = await self._send_reminder_report(message.guild.id, record)
        record["delivered"] = delivered
        await self.state.update_section(REMINDER_SECTION, record_id, record)
        return record

    async def _send_reminder_report(
        self,
        guild_id: int,
        record: dict[str, Any],
    ) -> bool:
        channel_id = await self.reminder_channel_id(guild_id)
        if channel_id is None:
            await self.state.set(
                "talisson_reminders_last_delivery",
                {
                    "ok": False,
                    "reason": "reminder_channel_not_configured",
                    "guild_id": int(guild_id),
                    "record_id": record.get("id"),
                    "created_at": _now_iso(),
                },
            )
            return False
        channel = self.bot.get_channel(channel_id)
        if channel is None or not hasattr(channel, "send"):
            await self.state.set(
                "talisson_reminders_last_delivery",
                {
                    "ok": False,
                    "reason": "reminder_channel_unavailable",
                    "guild_id": int(guild_id),
                    "channel_id": int(channel_id),
                    "record_id": record.get("id"),
                    "created_at": _now_iso(),
                },
            )
            return False
        text = await self._reminder_report_text(record)
        try:
            sent = await channel.send(
                content=text,
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=False,
                    everyone=False,
                ),
            )
            record["delivery_channel_id"] = int(channel_id)
            message_id = getattr(sent, "id", None)
            if message_id is not None:
                record["delivery_message_id"] = int(message_id)
                record["last_owner_delivery_message_id"] = int(message_id)
            record["delivered_at"] = _now_iso()
            record["updated_at"] = _now_iso()
            await self.state.set(
                "talisson_reminders_last_delivery",
                {
                    "ok": True,
                    "guild_id": int(guild_id),
                    "channel_id": int(channel_id),
                    "record_id": record.get("id"),
                    "created_at": _now_iso(),
                },
            )
            return True
        except (discord.Forbidden, discord.HTTPException):
            await self.state.set(
                "talisson_reminders_last_delivery",
                {
                    "ok": False,
                    "reason": "reminder_channel_send_failed",
                    "guild_id": int(guild_id),
                    "channel_id": int(channel_id),
                    "record_id": record.get("id"),
                    "created_at": _now_iso(),
                },
            )
            return False

    async def _handle_owner_reminder_response(
        self,
        message: discord.Message,
        text: str,
    ) -> bool:
        found = await self._reminder_from_reference(message, side="owner")
        if found is None:
            return False
        key, record = found
        if str(record.get("status") or "") == REMINDER_STATUS_CLOSED:
            await self._reply(
                message,
                "Esse recado já está encerrado. Se precisar, peça para a pessoa abrir um novo assunto comigo.",
            )
            return True
        if _is_reminder_close_request(text):
            self._append_history(record, message, text, sender="owner")
            record["status"] = REMINDER_STATUS_CLOSED
            record["closed_at"] = _now_iso()
            record["updated_at"] = _now_iso()
            await self.state.update_section(REMINDER_SECTION, key, record)
            await self._reply(
                message,
                "Fechado. Vou considerar esse recado encerrado.",
            )
            return True

        sent = await self._send_owner_reply_to_member(message, record, text)
        self._append_history(
            record,
            message,
            text,
            sender="owner",
            delivered_channel_id=(
                int(getattr(sent, "channel", None).id)
                if getattr(sent, "channel", None) is not None
                else int(record.get("channel_id") or 0)
            )
            if sent is not None
            else None,
            delivered_message_id=(
                int(getattr(sent, "id"))
                if sent is not None and getattr(sent, "id", None) is not None
                else None
            ),
            delivered_to="member",
        )
        if sent is not None and getattr(sent, "id", None) is not None:
            record["last_user_delivery_message_id"] = int(getattr(sent, "id"))
            record["last_user_delivery_channel_id"] = int(record.get("channel_id") or 0)
        record["status"] = REMINDER_STATUS_RESPONDED
        record["responded_at"] = _now_iso()
        record["updated_at"] = _now_iso()
        await self.state.update_section(REMINDER_SECTION, key, record)
        if sent is None:
            await self._reply(
                message,
                "Entendi sua resposta, mas não consegui entregar ao usuário agora. Mantive tudo registrado no recado.",
            )
        else:
            await self._reply(message, "Pode deixar. Encaminhei sua resposta.")
        return True

    async def _handle_member_reminder_reply(
        self,
        message: discord.Message,
        text: str,
    ) -> bool:
        if message.author.id == self.owner_id:
            return False
        found = await self._reminder_from_reference(message, side="member")
        if found is None:
            return False
        key, record = found
        if str(record.get("status") or "") == REMINDER_STATUS_CLOSED:
            await self._reply(
                message,
                "Esse assunto já foi encerrado por aqui. Se precisar, me envie um novo recado para o Talisson.",
            )
            return True
        if int(record.get("author_id") or 0) != int(message.author.id):
            return False
        sent = await self._send_member_reply_to_owner(message, record, text)
        self._append_history(
            record,
            message,
            text,
            sender="member",
            delivered_channel_id=(
                int(getattr(sent, "channel", None).id)
                if getattr(sent, "channel", None) is not None
                else int(record.get("delivery_channel_id") or 0)
            )
            if sent is not None
            else None,
            delivered_message_id=(
                int(getattr(sent, "id"))
                if sent is not None and getattr(sent, "id", None) is not None
                else None
            ),
            delivered_to="owner",
        )
        if sent is not None and getattr(sent, "id", None) is not None:
            record["last_owner_delivery_message_id"] = int(getattr(sent, "id"))
            record["delivery_channel_id"] = int(record.get("delivery_channel_id") or 0)
        record["status"] = REMINDER_STATUS_PENDING
        record["updated_at"] = _now_iso()
        await self.state.update_section(REMINDER_SECTION, key, record)
        if sent is None:
            await self._reply(
                message,
                "Eu entendi sua resposta e mantive o recado atualizado, mas não consegui encaminhar ao Talisson agora.",
            )
        else:
            await self._reply(message, "Certo. Vou levar essa resposta ao Talisson.")
        return True

    async def _send_owner_reply_to_member(
        self,
        message: discord.Message,
        record: dict[str, Any],
        response: str,
    ) -> Any | None:
        channel_id = int(record.get("channel_id") or 0)
        channel = self._channel_for(message.guild, channel_id)
        if channel is None or not hasattr(channel, "send"):
            return None
        text = await self._owner_reply_text(record, response)
        try:
            return await channel.send(
                content=text,
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=False,
                    everyone=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            return None

    async def _send_member_reply_to_owner(
        self,
        message: discord.Message,
        record: dict[str, Any],
        response: str,
    ) -> Any | None:
        channel_id = int(record.get("delivery_channel_id") or 0)
        if not channel_id:
            channel_id = await self.reminder_channel_id(message.guild.id) or 0
        channel = self._channel_for(message.guild, channel_id)
        if channel is None or not hasattr(channel, "send"):
            return None
        text = await self._member_follow_up_text(record, response)
        try:
            return await channel.send(
                content=text,
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=False,
                    everyone=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            return None

    async def _reminder_report_text(self, record: dict[str, Any]) -> str:
        return _natural_reminder_report_text(self.owner_id, record)

    async def _owner_reply_text(
        self,
        record: dict[str, Any],
        response: str,
    ) -> str:
        return _natural_owner_reply_text(record, response)

    async def _member_follow_up_text(
        self,
        record: dict[str, Any],
        response: str,
    ) -> str:
        return _natural_member_follow_up_text(self.owner_id, record, response)

    async def _reminder_from_reference(
        self,
        message: discord.Message,
        *,
        side: str,
    ) -> tuple[str, dict[str, Any]] | None:
        reference_id = _referenced_message_id(message)
        if reference_id is None:
            return None
        channel_id = int(getattr(message.channel, "id", 0) or 0)
        records = await self.state.get_section(REMINDER_SECTION)
        for key, record in records.items():
            if not isinstance(record, dict):
                continue
            if int(record.get("guild_id") or 0) != int(message.guild.id):
                continue
            if side == "owner" and message.author.id != self.owner_id:
                continue
            if side == "member" and int(record.get("author_id") or 0) != int(message.author.id):
                continue
            if _record_has_delivery_reference(record, reference_id, channel_id, side):
                return str(key), record
        return None

    def _append_history(
        self,
        record: dict[str, Any],
        message: discord.Message,
        text: str,
        *,
        sender: str,
        delivered_channel_id: int | None = None,
        delivered_message_id: int | None = None,
        delivered_to: str | None = None,
    ) -> None:
        entry = {
            "from": sender,
            "user_id": int(message.author.id),
            "channel_id": int(getattr(message.channel, "id", 0) or 0),
            "message_id": int(getattr(message, "id", 0) or 0),
            "message_url": getattr(message, "jump_url", ""),
            "text": text[:1500],
            "created_at": _now_iso(),
        }
        if delivered_channel_id is not None:
            entry["delivered_channel_id"] = int(delivered_channel_id)
        if delivered_message_id is not None:
            entry["delivered_message_id"] = int(delivered_message_id)
        if delivered_to is not None:
            entry["delivered_to"] = delivered_to
        history = record.setdefault("history", [])
        if isinstance(history, list):
            history.append(entry)
            del history[:-20]

    async def _save_pending(
        self,
        guild_id: int,
        user_id: int,
        prompt: str,
        *,
        source: str = "recado",
    ) -> None:
        key = _pending_key(guild_id, user_id)
        await self.state.mutate_section(
            PENDING_SECTION,
            lambda values: values.__setitem__(
                key,
                {
                    "guild_id": int(guild_id),
                    "user_id": int(user_id),
                    "prompt": prompt[:500],
                    "source": source,
                    "expires_at": time.time() + self.pending_ttl_seconds,
                    "created_at": _now_iso(),
                },
            ),
        )

    async def _pending_for(
        self,
        guild_id: int,
        user_id: int,
    ) -> dict[str, Any] | None:
        key = _pending_key(guild_id, user_id)
        values = await self.state.get_section(PENDING_SECTION)
        pending = values.get(key)
        if not isinstance(pending, dict):
            return None
        if float(pending.get("expires_at") or 0) < time.time():
            await self._clear_pending(guild_id, user_id)
            return None
        return pending

    async def _clear_pending(self, guild_id: int, user_id: int) -> None:
        key = _pending_key(guild_id, user_id)

        def remove(values: dict[str, Any]) -> None:
            values.pop(key, None)

        await self.state.mutate_section(PENDING_SECTION, remove)

    async def _reply(self, message: discord.Message, text: str) -> None:
        try:
            await message.reply(
                text,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions(
                    users=[message.author],
                    roles=False,
                    everyone=False,
                    replied_user=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            return

    def _mentions_owner(self, message: discord.Message, text: str) -> bool:
        if re.search(rf"<@!?{self.owner_id}>", text):
            return True
        for member in getattr(message, "mentions", []) or []:
            if getattr(member, "id", None) == self.owner_id:
                return True
        normalized = _normalize(text)
        if re.search(r"\b(talisson|talinho|tali)\b", normalized):
            return True
        return bool(
            re.search(
                r"\b(cade|cadê|chama|chamar|falar|avisa|avisar|esperando)\s+"
                r"(o\s+)?(ele|talisson|talinho|tali)\b",
                normalized,
            )
        )

    def _looks_like_return(self, text: str) -> bool:
        normalized = _normalize(text)
        return bool(
            re.search(
                r"\b(cheguei|voltei|retornei|retornei|estou disponivel novamente|"
                r"estou disponível novamente|to de volta|tô de volta|ja voltei|já voltei|"
                r"ja estou de volta|já estou de volta|estou de volta|terminei|"
                r"terminei a reuniao|terminei a reunião|pode falar comigo|"
                r"estou livre|estou liberado|disponivel de novo|disponível de novo)\b",
                normalized,
            )
        )

    def _looks_like_absence(self, text: str) -> bool:
        normalized = _normalize(text)
        if self._looks_like_return(text):
            return False
        return bool(
            re.search(
                r"\b(nao vou estar disponivel|não vou estar disponível|"
                r"nao estarei disponivel|não estarei disponível|"
                r"estarei ocupado|estou ocupado|to ocupado|tô ocupado|vou ficar ocupado|"
                r"vou ficar indisponivel|vou ficar indisponível|"
                r"estou em reuniao|estou em reunião|vou viajar|"
                r"estarei viajando|consulta medica|consulta médica|"
                r"tenho consulta|vou estar em consulta|vou estar fora|"
                r"nao vou estar|não vou estar|indisponivel|indisponível|ausente|"
                r"vou ficar ausente|vou me ausentar|ficarei ausente|afk|"
                r"fora do pc|sem acesso|nao consigo responder|não consigo responder|"
                r"nao vou responder|não vou responder|vou demorar pra responder|"
                r"vou demorar para responder|estou em aula|estarei em aula|"
                r"estou no trabalho|estou em compromisso|compromisso pessoal|"
                r"nao posso atender|não posso atender|hospital|medico|médico)\b",
                normalized,
            )
        )

    def _is_strong_return(self, text: str) -> bool:
        normalized = _normalize(text)
        return bool(
            re.fullmatch(
                r"(cheguei|voltei|retornei|ja voltei|já voltei|estou de volta|"
                r"estou disponivel novamente|estou disponível novamente|"
                r"disponivel de novo|disponível de novo)",
                normalized,
            )
        )

    def _is_strong_absence(self, text: str) -> bool:
        normalized = _normalize(text)
        return bool(
            re.search(
                r"\b(eu\s+)?(vou|estou|estarei|ficarei|vou ficar|vou estar|"
                r"tenho|nao vou|não vou|nao consigo|não consigo|nao posso|não posso)\b"
                r".{0,80}\b(consulta|medico|médico|hospital|reuniao|reunião|"
                r"ocupado|indisponivel|indisponível|ausente|viajar|viagem|fora|"
                r"afk|aula|trabalho|compromisso|responder|atender)\b",
                normalized,
            )
        )

    def _looks_like_reminder_channel_config(
        self,
        message: discord.Message,
        text: str,
    ) -> bool:
        if self._first_channel_id(message) is None:
            return False
        normalized = _normalize(text)
        if re.search(
            r"\b(anuncio|anunciar|anuncie|anuncia|publicar|publique|divulgar|divulgue|comunicado|aviso|vaga)\b",
            normalized,
        ):
            return False
        config_words = r"(configura|configure|defina|define|seta|centraliza|centralize|usar|use)"
        target_words = r"(canal\s+(?:de\s+)?(?:lembretes|recados)|(?:lembretes|recados))"
        return bool(
            re.search(rf"\b{config_words}\b.{0,50}\b{target_words}\b", normalized)
            or re.search(rf"\b{target_words}\b.{0,50}\b{config_words}\b", normalized)
        )

    def _is_availability_question(self, text: str) -> bool:
        normalized = _normalize(text)
        return bool(
            re.search(
                r"\b(talisson|talinho|tali|ele)\b.*\b(disponivel|disponível|atende|atendimento|horario|horário)\b",
                normalized,
            )
            or re.search(
                r"\b(posso falar com|qual o horario|qual o horário|quando .*atende|"
                r"como faco para marcar|como faço para marcar|horario de atendimento|"
                r"horário de atendimento)\b",
                normalized,
            )
        )

    def _is_scheduling_request(self, text: str) -> bool:
        normalized = _normalize(text)
        return bool(
            re.search(
                r"\b(quero|queria|gostaria|preciso|posso)\b.{0,40}"
                r"\b(marcar|agendar|reuniao|reunião|horario|horário|conversar|falar)\b",
                normalized,
            )
            or re.search(
                r"\b(como posso agendar|como agendar|marcar um horario|marcar um horário|"
                r"agendar uma reuniao|agendar uma reunião)\b",
                normalized,
            )
        )

    def _is_personal_schedule_query(self, text: str) -> bool:
        normalized = _normalize(text)
        return bool(
            re.search(
                r"\b(minha agenda|agenda de hoje|agenda de amanha|agenda de amanhã|"
                r"quais compromissos|compromissos tenho|tenho algo marcado|"
                r"tenho alguma coisa marcada|o que tenho marcado)\b",
                normalized,
            )
        )

    def _looks_like_personal_reminder(self, text: str) -> bool:
        normalized = _normalize(text)
        return bool(
            _extract_time(normalized)
            and re.search(
                r"\b(me lembre|lembrar|tenho|compromisso|reuniao|reunião|consulta|"
                r"ligar|call|evento|agenda|marcado|marcada|cliente|parceiro|parceria)\b",
                normalized,
            )
        )

    def _parse_personal_reminder(self, text: str) -> dict[str, Any] | None:
        normalized = _normalize(text)
        parsed_time = _extract_time(normalized)
        if parsed_time is None:
            return None
        hour, minute = parsed_time
        scheduled_date = _extract_date(normalized, hour, minute)
        scheduled_at = scheduled_date.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
        description = _clean_personal_reminder_description(text)
        title = _title_from_description(description)
        priority = "alta" if "urgente" in normalized or "importante" in normalized else "normal"
        return {
            "title": title,
            "description": description,
            "scheduled_at": scheduled_at.isoformat(),
            "priority": priority,
        }

    def _could_mark_absence(self, text: str) -> bool:
        normalized = _normalize(text)
        return bool(
            re.search(
                r"\b(consulta|medico|médico|hospital|reuniao|reunião|viagem|"
                r"compromisso|aula|trabalho)\b",
                normalized,
            )
        )

    def _is_contact_request(self, text: str) -> bool:
        normalized = _normalize(text)
        return bool(
            re.search(
                r"\b(preciso falar|quero falar|falar com|fala para|avisa ele|"
                r"avisa o talisson|diz para ele|entrar em contato|me chamar|me chama)\b",
                normalized,
            )
        )

    def _is_direct_reminder_request(self, text: str) -> bool:
        normalized = _normalize(text)
        owner_target = r"(?:o\s+)?(?:talisson|talinho|tali|ele)"
        return bool(
            re.search(
                rf"\b(?:diz|fala|avisa|manda|pede|lembra|comenta)\s+"
                rf"(?:pro|pra|para\s+o|para|ao|a)\s+{owner_target}\s+.+",
                normalized,
            )
            or re.search(
                rf"\b(?:diz|fala|avisa|manda|pede|lembra|comenta)\s+"
                rf"{owner_target}\s+(?:que|pra|para|sobre|de|do|da)\s+.+",
                normalized,
            )
        )

    def _is_invite_request(self, text: str) -> bool:
        normalized = _normalize(text)
        return bool(
            re.search(
                r"\b(convida|convide|party|call|entrar na call|entra na call|"
                r"estamos esperando|esperando ele|esperando o talisson|chama ele pra call)\b",
                normalized,
            )
        )

    def _extract_reason(self, text: str) -> str:
        normalized = _normalize(text)
        if "consulta" in normalized or "medic" in normalized:
            return "consulta médica"
        if "hospital" in normalized:
            return "compromisso médico"
        if "reuniao" in normalized:
            return "reunião"
        if "viaj" in normalized:
            return "viagem"
        if "aula" in normalized:
            return "aula"
        if "trabalho" in normalized:
            return "trabalho"
        if "compromisso" in normalized:
            return "compromissos pessoais"
        if "ocupad" in normalized:
            return "compromissos pessoais"
        if "indispon" in normalized:
            return "indisponibilidade"
        if "ausente" in normalized or "fora" in normalized or "afk" in normalized:
            return "ausência temporária"
        if "responder" in normalized or "atender" in normalized:
            return "indisponibilidade temporária"
        cleaned = re.sub(
            r"\b(hoje|amanha|amanhã|agora|vou|estar|estarei|estou|ficar|em|por|porque)\b",
            " ",
            text,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,!?:;-")
        return cleaned[:80] or "indisponibilidade temporária"

    def _first_channel_id(self, message: discord.Message) -> int | None:
        for channel in getattr(message, "channel_mentions", []) or []:
            channel_id = getattr(channel, "id", None)
            if channel_id:
                return int(channel_id)
        match = re.search(r"<#(\d+)>", message.content)
        if match:
            return int(match.group(1))
        return None

    def _channel_for(
        self,
        guild: discord.Guild,
        channel_id: int,
    ) -> Any | None:
        channel = self.bot.get_channel(channel_id)
        if channel is None and hasattr(guild, "get_channel"):
            channel = guild.get_channel(channel_id)
        return channel


def categorize_reminder(text: str) -> str:
    normalized = _normalize(text)
    rules = [
        ("Urgente", ("urgente", "emergencia", "emergência", "agora", "imediato")),
        ("Agendamento", ("agendar", "agenda", "horario", "horário", "reuniao", "reunião", "marcar")),
        ("Parceria", ("parceria", "partner", "colaboracao", "colaboração")),
        ("Bug", ("bug", "erro", "falha", "quebrou", "problema tecnico", "problema técnico")),
        ("Suporte", ("suporte", "ajuda", "duvida", "dúvida", "atendimento")),
        ("Convite", ("convite", "convida", "party", "call", "reuniao", "reunião")),
        ("Ideia", ("ideia", "idéia", "insight")),
        ("Sugestão", ("sugestao", "sugestão", "sugerir", "melhoria")),
        ("Reclamação", ("reclamacao", "reclamação", "insatisfeito", "ruim")),
        ("Contato", ("contato", "falar", "chamar", "me chama", "me chamar")),
    ]
    for category, keywords in rules:
        if any(keyword in normalized for keyword in keywords):
            return category
    return "Contato"


def _reminder_ack(record: dict[str, Any], *, direct: bool) -> str:
    if bool(record.get("delivered")):
        if direct:
            return "Claro. Vou avisar o Talisson."
        return "Perfeito. Vou deixar esse recado registrado para ele."
    if direct:
        return (
            "Claro. Eu guardei esse recado para o Talisson, mas ainda não consegui "
            "enviar no canal de lembretes."
        )
    return (
        "Perfeito. Eu guardei esse recado para ele, mas ainda não consegui enviar "
        "no canal de lembretes."
    )


def _reminder_report_text(owner_id: int, record: dict[str, Any]) -> str:
    title = (
        "📅 Nova solicitação de agendamento"
        if str(record.get("source") or "") == "agendamento"
        else "📌 Novo recado recebido"
    )
    author_id = int(record.get("author_id") or 0)
    channel_id = int(record.get("channel_id") or 0)
    category = str(record.get("category") or "Contato").strip() or "Contato"
    subject = str(record.get("subject") or "Sem conteúdo informado").strip()
    message_url = str(record.get("message_url") or "").strip()
    if not message_url:
        guild_id = int(record.get("guild_id") or 0)
        message_id = int(record.get("message_id") or 0)
        if guild_id and channel_id and message_id:
            message_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    return (
        f"<@{int(owner_id)}>\n\n"
        f"{title}\n\n"
        "👤 Usuário:\n"
        f"<@{author_id}>\n\n"
        "📂 Categoria:\n"
        f"{category}\n\n"
        "💬 Mensagem:\n"
        f"{subject}\n\n"
        "📍 Canal:\n"
        f"<#{channel_id}>\n\n"
        "🕒 Horário:\n"
        f"{_format_datetime(str(record.get('created_at') or ''))}\n\n"
        "🔗 Mensagem original:\n"
        f"{message_url}"
    )


def _text_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }


def _natural_reminder_report_text(owner_id: int, record: dict[str, Any]) -> str:
    author_id = int(record.get("author_id") or 0)
    channel_id = int(record.get("channel_id") or 0)
    subject = str(record.get("subject") or "Sem conteúdo informado").strip()
    source = str(record.get("source") or "recado")
    variant = _variant(str(record.get("id") or subject), 3)
    if source == "agendamento":
        lines = [
            f"<@{int(owner_id)}>",
            "",
            f"<@{author_id}> quer marcar um horário com você.",
            "",
            f"Ele deixou este contexto: {subject}",
        ]
    elif source == "convite":
        lines = [
            f"<@{int(owner_id)}>",
            "",
            f"<@{author_id}> pediu para te chamar.",
            "",
            subject,
        ]
    elif variant == 0:
        lines = [
            f"<@{int(owner_id)}>",
            "",
            "Recebi uma mensagem para você.",
            "",
            f"<@{author_id}> pediu para avisar: {subject}",
        ]
    elif variant == 1:
        lines = [
            f"<@{int(owner_id)}>",
            "",
            f"<@{author_id}> deixou um recado para quando você estiver disponível.",
            "",
            subject,
        ]
    else:
        lines = [
            f"<@{int(owner_id)}>",
            "",
            f"<@{author_id}> gostaria que você visse este recado:",
            "",
            subject,
        ]
    if channel_id:
        lines.extend(["", f"A mensagem veio do canal <#{channel_id}>."])
    return "\n".join(lines).strip()


def _natural_owner_reply_text(record: dict[str, Any], response: str) -> str:
    author_id = int(record.get("author_id") or 0)
    subject = str(record.get("subject") or "seu recado").strip()
    variant = _variant(str(record.get("id") or response) + response, 3)
    if variant == 0:
        return (
            f"Olá <@{author_id}>. O Talisson recebeu seu recado e pediu para te avisar: "
            f"{response.strip()}"
        )
    if variant == 1:
        return (
            f"<@{author_id}>, o Talisson viu sua mensagem sobre {subject} e retornou: "
            f"{response.strip()}"
        )
    return f"Passando o retorno do Talisson, <@{author_id}>: {response.strip()}"


def _natural_member_follow_up_text(
    owner_id: int,
    record: dict[str, Any],
    response: str,
) -> str:
    author_id = int(record.get("author_id") or 0)
    variant = _variant(str(record.get("id") or response) + response, 3)
    if variant == 0:
        return (
            f"<@{int(owner_id)}>\n\n"
            f"<@{author_id}> respondeu ao seu retorno.\n\n"
            f"Ele disse: {response.strip()}"
        )
    if variant == 1:
        return (
            f"<@{int(owner_id)}>\n\n"
            f"Chegou uma nova resposta de <@{author_id}> sobre aquele recado.\n\n"
            f"{response.strip()}"
        )
    return (
        f"<@{int(owner_id)}>\n\n"
        f"<@{author_id}> continuou a conversa e deixou esta resposta:\n\n"
        f"{response.strip()}"
    )


def _reminder_report_prompt(owner_id: int, record: dict[str, Any]) -> str:
    return (
        "Você é Bella, assistente pessoal do Talisson. Gere um recado natural, "
        "curto e conversacional para Talisson. Não use formato de formulário, "
        "não use campos, não cite categoria, horário, sistema, ticket ou banco de dados. "
        "Mencione Talisson e o usuário. Pode citar o canal em uma frase natural. "
        "Não invente dados.\n\n"
        f"Menção do Talisson: <@{int(owner_id)}>\n"
        f"Usuário: <@{int(record.get('author_id') or 0)}>\n"
        f"Canal: <#{int(record.get('channel_id') or 0)}>\n"
        f"Tipo interno: {record.get('source') or 'recado'}\n"
        f"Mensagem do usuário: {record.get('subject') or ''}"
    )


def _owner_reply_prompt(record: dict[str, Any], response: str) -> str:
    history = _history_excerpt(record)
    return (
        "Você é Bella intermediando um recado entre Talisson e um membro do Discord. "
        "Transforme a resposta de Talisson em uma mensagem natural para o usuário. "
        "Mantenha o sentido, não invente, não use formato de formulário, não diga que é sistema. "
        "A mensagem deve mencionar o usuário.\n\n"
        f"Usuário: <@{int(record.get('author_id') or 0)}>\n"
        f"Recado original: {record.get('subject') or ''}\n"
        f"Histórico recente:\n{history}\n\n"
        f"Resposta de Talisson: {response}"
    )


def _member_follow_up_prompt(
    owner_id: int,
    record: dict[str, Any],
    response: str,
) -> str:
    history = _history_excerpt(record)
    return (
        "Você é Bella intermediando uma conversa assíncrona. Gere uma mensagem natural "
        "para Talisson com a nova resposta do usuário. Não use campos, não use layout "
        "de ticket e não cite sistema. Mencione Talisson e o usuário.\n\n"
        f"Talisson: <@{int(owner_id)}>\n"
        f"Usuário: <@{int(record.get('author_id') or 0)}>\n"
        f"Recado original: {record.get('subject') or ''}\n"
        f"Histórico recente:\n{history}\n\n"
        f"Nova resposta do usuário: {response}"
    )


def _history_excerpt(record: dict[str, Any]) -> str:
    history = record.get("history", [])
    if not isinstance(history, list):
        return "Sem histórico anterior."
    lines = []
    for item in history[-6:]:
        if not isinstance(item, dict):
            continue
        sender = "Talisson" if item.get("from") == "owner" else "Usuário"
        text = str(item.get("text") or "").strip()
        if text:
            lines.append(f"{sender}: {text[:240]}")
    return "\n".join(lines) or "Sem histórico anterior."


def _ensure_owner_mention(owner_id: int, text: str) -> str:
    mention = f"<@{int(owner_id)}>"
    return text if mention in text else f"{mention}\n\n{text}"


def _ensure_member_mention(record: dict[str, Any], text: str) -> str:
    mention = f"<@{int(record.get('author_id') or 0)}>"
    return text if mention in text else f"{mention}, {text}"


def _referenced_message_id(message: discord.Message) -> int | None:
    reference = getattr(message, "reference", None)
    if reference is None:
        return None
    message_id = getattr(reference, "message_id", None)
    if message_id is None:
        resolved = getattr(reference, "resolved", None)
        message_id = getattr(resolved, "id", None)
    try:
        return int(message_id) if message_id is not None else None
    except (TypeError, ValueError):
        return None


def _record_has_delivery_reference(
    record: dict[str, Any],
    message_id: int,
    channel_id: int,
    side: str,
) -> bool:
    direct_fields = (
        (
            ("delivery_message_id", "delivery_channel_id"),
            ("last_owner_delivery_message_id", "delivery_channel_id"),
        )
        if side == "owner"
        else (("last_user_delivery_message_id", "last_user_delivery_channel_id"),)
    )
    for message_field, channel_field in direct_fields:
        if _same_message_reference(record, message_field, channel_field, message_id, channel_id):
            return True
    history = record.get("history", [])
    if not isinstance(history, list):
        return False
    expected_to = "owner" if side == "owner" else "member"
    for item in history:
        if not isinstance(item, dict):
            continue
        if item.get("delivered_to") != expected_to:
            continue
        try:
            delivered_message_id = int(item.get("delivered_message_id") or 0)
            delivered_channel_id = int(item.get("delivered_channel_id") or 0)
        except (TypeError, ValueError):
            continue
        if delivered_message_id == int(message_id) and (
            not delivered_channel_id or delivered_channel_id == int(channel_id)
        ):
            return True
    return False


def _same_message_reference(
    record: dict[str, Any],
    message_field: str,
    channel_field: str,
    message_id: int,
    channel_id: int,
) -> bool:
    try:
        stored_message_id = int(record.get(message_field) or 0)
        stored_channel_id = int(record.get(channel_field) or 0)
    except (TypeError, ValueError):
        return False
    return stored_message_id == int(message_id) and (
        not stored_channel_id or stored_channel_id == int(channel_id)
    )


def _is_reminder_close_request(text: str) -> bool:
    normalized = _normalize(text)
    return bool(
        re.search(
            r"\b(pode encerrar|problema resolvido|recado resolvido|encerrar recado|"
            r"encerra esse recado|encerre esse recado|finaliza|finalize|resolvido)\b",
            normalized,
        )
    )


def _variant(seed: str, size: int) -> int:
    return sum(ord(char) for char in seed) % max(1, size)


def _extract_time(normalized: str) -> tuple[int, int] | None:
    match = re.search(
        r"(?<![#/@&\w])(?:\b(?:as|às|a)\s*)?(\d{1,2})(?:[:h](\d{2}))?\s*h?(?!\d)\b",
        normalized,
    )
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _extract_date(normalized: str, hour: int, minute: int) -> datetime:
    now = _now_dt()
    if "depois de amanha" in normalized or "depois de amanhã" in normalized:
        return now + timedelta(days=2)
    if "amanha" in normalized or "amanhã" in normalized:
        return now + timedelta(days=1)
    if "hoje" in normalized:
        return now

    next_week = "proxima" in normalized or "próxima" in normalized or "proximo" in normalized or "próximo" in normalized
    for name, weekday in WEEKDAY_NAMES.items():
        if re.search(rf"\b{name}\b", normalized):
            days = (weekday - now.weekday()) % 7
            if next_week or days == 0:
                days = 7 if days == 0 else days
            candidate = now + timedelta(days=days)
            if days == 0:
                scheduled = candidate.replace(
                    hour=hour,
                    minute=minute,
                    second=0,
                    microsecond=0,
                )
                if scheduled <= now:
                    candidate += timedelta(days=7)
            return candidate

    candidate = now
    scheduled = candidate.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )
    if scheduled <= now:
        candidate += timedelta(days=1)
    return candidate


def _clean_personal_reminder_description(text: str) -> str:
    cleaned = re.sub(
        r"^\s*jarvis\s*[,:\-]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(me lembre|lembre-me|tenho|de|que eu tenho|vou ter)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(hoje|amanh[ãa]|depois de amanh[ãa]|na pr[óo]xima|no pr[óo]ximo|"
        r"pr[óo]xima|pr[óo]ximo|segunda(?:-feira)?|ter[çc]a(?:-feira)?|"
        r"quarta(?:-feira)?|quinta(?:-feira)?|sexta(?:-feira)?|"
        r"s[áa]bado|domingo)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"(?:\b(?:as|às|a)\s*)?\d{1,2}(?:[:h]\d{2})?\s*h?\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,!?:;-")
    return cleaned or "compromisso"


def _title_from_description(description: str) -> str:
    value = description.strip(" .")
    if not value:
        return "Compromisso"
    return value[:1].upper() + value[1:120]


def _parse_iso_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_local_tz())
    return parsed.astimezone(_local_tz())


def _offset_label(offset_minutes: int) -> str:
    if offset_minutes >= 24 * 60:
        days = offset_minutes // (24 * 60)
        return f"em cerca de {days} dia" if days == 1 else f"em cerca de {days} dias"
    if offset_minutes >= 60:
        hours = offset_minutes // 60
        return f"em cerca de {hours} hora" if hours == 1 else f"em cerca de {hours} horas"
    return f"em {offset_minutes} minutos"


def _query_window(text: str, now: datetime) -> tuple[datetime, datetime, str]:
    normalized = _normalize(text)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "amanha" in normalized or "amanhã" in normalized:
        start = day_start + timedelta(days=1)
        return start, start + timedelta(days=1), "amanhã"
    if "semana" in normalized:
        start = day_start
        return start, start + timedelta(days=7), "esta semana"
    return day_start, day_start + timedelta(days=1), "hoje"


def _pending_key(guild_id: int, user_id: int) -> str:
    return f"{int(guild_id)}:{int(user_id)}"


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKD", text.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("-", " ")
    return re.sub(r"\s+", " ", value).strip()


def _now_dt() -> datetime:
    return datetime.now(_local_tz())


def _now_iso() -> str:
    return _now_dt().isoformat()


def _local_tz() -> ZoneInfo:
    try:
        return ZoneInfo("America/Sao_Paulo")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _format_datetime(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value or "Agora"
    return parsed.astimezone(_local_tz()).strftime("%d/%m/%Y %H:%M")


def _availability_phrase(reason: str) -> str:
    normalized = _normalize(reason)
    if "consulta" in normalized:
        return "em uma consulta médica"
    if "reuniao" in normalized:
        return "em reunião"
    if "viagem" in normalized or "viaj" in normalized:
        return "em viagem"
    if "compromisso" in normalized:
        return "com compromissos pessoais"
    if "ausencia" in normalized:
        return "ausente temporariamente"
    if "indispon" in normalized:
        return "indisponível"
    return f"com {reason}"
