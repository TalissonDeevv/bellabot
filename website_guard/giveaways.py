from __future__ import annotations

import asyncio
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import discord

from .embed_builder import (
    DraftField,
    EmbedDraft,
    EmbedEditorSession,
    V2ContainerDraft,
    V2EditorSession,
    V2ItemDraft,
)
from .maintenance_center import guard_public_interaction

from .central_logs import CentralLogService
from .state import StateStore

LOGGER = logging.getLogger(__name__)

CONFIG_SECTION = "giveaway_config"
GIVEAWAYS_SECTION = "giveaways"
ENTRIES_SECTION = "giveaway_entries"
HISTORY_SECTION = "giveaway_history"
MAX_HISTORY_PER_GUILD = 150
MAX_GIVEAWAY_CONTAINERS = 5
GIVEAWAY_TIMEZONE = ZoneInfo("America/Sao_Paulo")

AuthorizationCallback = Callable[[discord.Member], bool]


class GiveawayService:
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
        self._locks: dict[str, asyncio.Lock] = {}
        self._views_registered: set[str] = set()

    async def config(self, guild_id: int) -> dict[str, Any]:
        section = await self.state.get_section(CONFIG_SECTION)
        value = section.get(str(guild_id), {})
        return dict(value) if isinstance(value, dict) else {}

    async def set_channel(self, guild_id: int, channel_id: int) -> None:
        payload = await self.config(guild_id)
        payload.update({"channel_id": channel_id, "updated_at": _now()})
        await self.state.update_section(CONFIG_SECTION, str(guild_id), payload)

    async def create(
        self,
        guild: discord.Guild,
        *,
        prize: str,
        winners_count: int,
        duration_minutes: int,
        required_role_id: int | None,
        minimum_server_days: int,
        start_delay_minutes: int,
        recurrence_hours: int,
        created_by: int,
        details: str = "",
        image_url: str = "",
        required_role_ids: list[int] | None = None,
        mention_role_ids: list[int] | None = None,
        publish_immediately: bool = True,
        exact_ends_at: datetime | None = None,
    ) -> dict[str, Any]:
        config = await self.config(guild.id)
        channel_id = _int(config.get("channel_id"))
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Configure primeiro o canal público dos sorteios pelo painel.")
        now = datetime.now(timezone.utc)
        starts_at = now + timedelta(minutes=max(0, start_delay_minutes))
        ends_at = exact_ends_at.astimezone(timezone.utc) if exact_ends_at is not None else (
            starts_at + timedelta(minutes=max(1, duration_minutes))
        )
        if ends_at <= starts_at:
            raise ValueError("A data final do sorteio precisa ser posterior ao início.")
        calculated_duration = max(1, int((ends_at - starts_at).total_seconds() // 60))
        giveaway_id = uuid.uuid4().hex[:12]
        role_ids = _unique_ids(required_role_ids or ([required_role_id] if required_role_id else []))
        mention_ids = _unique_ids(mention_role_ids or [])
        payload = {
            "id": giveaway_id,
            "guild_id": guild.id,
            "channel_id": channel.id,
            "message_id": 0,
            "prize": str(prize).strip()[:256],
            "details": str(details).strip()[:4000],
            "image_url": _optional_http_url(image_url),
            "winners_count": max(1, min(20, int(winners_count))),
            "minimum_participants": max(1, min(20, int(winners_count))) * 4,
            "required_role_id": role_ids[0] if role_ids else 0,
            "required_role_ids": role_ids,
            "mention_role_ids": mention_ids,
            "minimum_server_days": max(0, int(minimum_server_days)),
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "duration_minutes": calculated_duration,
            "recurrence_hours": max(0, int(recurrence_hours)),
            "created_by": created_by,
            "created_at": now.isoformat(),
            "status": (
                "DRAFT"
                if not publish_immediately
                else "SCHEDULED" if starts_at > now + timedelta(seconds=2) else "PENDING"
            ),
            "winner_ids": [],
            "rerolls": [],
            "bonus_roles": [],
            "visual_mode": "v2",
            "embed_v1_payload": _default_giveaway_v1_payload(),
            "message_content": "",
            "containers_v2": [container.to_dict() for container in _default_giveaway_containers()],
        }
        await self._save(guild.id, giveaway_id, payload)
        if payload["status"] == "PENDING":
            await self.publish(guild, giveaway_id)
            payload = await self.get(guild.id, giveaway_id) or payload
        await self.logs.send_event(
            guild,
            "giveaways",
            title="Sorteio criado",
            description=f"O sorteio **{payload['prize']}** foi configurado.",
            severity="INFO",
            fields=(
                ("ID", giveaway_id, True),
                ("Vencedores", str(payload["winners_count"]), True),
                ("Início", discord.utils.format_dt(starts_at, "R"), True),
                ("Término", discord.utils.format_dt(ends_at, "R"), True),
            ),
        )
        return payload

    async def get(self, guild_id: int, giveaway_id: str) -> dict[str, Any] | None:
        section = await self.state.get_section(GIVEAWAYS_SECTION)
        guild_data = section.get(str(guild_id), {})
        payload = guild_data.get(giveaway_id) if isinstance(guild_data, dict) else None
        return dict(payload) if isinstance(payload, dict) else None

    async def list_for_guild(
        self,
        guild_id: int,
        *,
        statuses: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        section = await self.state.get_section(GIVEAWAYS_SECTION)
        guild_data = section.get(str(guild_id), {})
        if not isinstance(guild_data, dict):
            return []
        values = [dict(item) for item in guild_data.values() if isinstance(item, dict)]
        if statuses is not None:
            values = [item for item in values if str(item.get("status")) in statuses]
        return sorted(values, key=lambda item: str(item.get("created_at", "")), reverse=True)

    async def publish(self, guild: discord.Guild, giveaway_id: str) -> discord.Message | None:
        payload = await self.get(guild.id, giveaway_id)
        if payload is None:
            raise ValueError("Sorteio não encontrado.")
        channel = guild.get_channel(_int(payload.get("channel_id")))
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("O canal público do sorteio não está disponível.")
        if _int(payload.get("message_id")):
            return await self.refresh_public_message(guild, giveaway_id)
        now = datetime.now(timezone.utc)
        if _datetime(payload.get("starts_at")) > now + timedelta(seconds=2):
            payload["status"] = "SCHEDULED"
            await self._save(guild.id, giveaway_id, payload)
            return None
        announcement = _giveaway_announcement_content(payload)
        if announcement:
            if _int(payload.get("announcement_message_id")):
                await self._refresh_announcement(channel, payload)
            else:
                announcement_message = await channel.send(
                    announcement,
                    allowed_mentions=_giveaway_allowed_mentions(),
                )
                payload["announcement_message_id"] = announcement_message.id
        active_payload = {**payload, "status": "ACTIVE"}
        if _giveaway_visual_mode(payload) == "v1":
            participant_ids = await self.entries(giveaway_id)
            message = await channel.send(
                embeds=self.public_v1_embeds(
                    guild,
                    active_payload,
                    len(participant_ids),
                    total_entries=sum(
                        self.entry_weight(guild.get_member(member_id), active_payload)
                        for member_id in participant_ids
                    ),
                ),
                view=GiveawayClassicEntryView(self, giveaway_id),
            )
        else:
            message = await channel.send(view=await self.public_view(guild, active_payload))
        payload.update({"message_id": message.id, "status": "ACTIVE", "published_at": _now()})
        await self._save(guild.id, giveaway_id, payload)
        self._register_view(giveaway_id, message.id, mode=_giveaway_visual_mode(payload))
        return message

    async def enter(self, interaction: discord.Interaction, giveaway_id: str) -> tuple[bool, str]:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False, "Este sorteio só funciona dentro do servidor."
        lock = self._locks.setdefault(giveaway_id, asyncio.Lock())
        async with lock:
            payload = await self.get(interaction.guild.id, giveaway_id)
            if payload is None or payload.get("status") != "ACTIVE":
                return False, "Esse sorteio não está mais recebendo participações."
            now = datetime.now(timezone.utc)
            if now >= _datetime(payload.get("ends_at")):
                return False, "O prazo deste sorteio já terminou."
            required_role_ids = _payload_role_ids(payload, "required_role_ids")
            if required_role_ids and not any(interaction.user.get_role(role_id) for role_id in required_role_ids):
                roles = [interaction.guild.get_role(role_id) for role_id in required_role_ids]
                labels = ", ".join(role.name for role in roles if role is not None)
                return False, f"Você precisa de pelo menos um destes cargos: **{labels or 'cargo configurado'}**."
            minimum_days = _int(payload.get("minimum_server_days"))
            if minimum_days:
                joined_at = interaction.user.joined_at
                if joined_at is None or now - joined_at < timedelta(days=minimum_days):
                    return False, f"É preciso estar no servidor há pelo menos **{minimum_days} dias**."
            entries = await self.entries(giveaway_id)
            if interaction.user.id in entries:
                return False, "Você já está participando desse sorteio."
            entries.append(interaction.user.id)
            await self.state.update_section(ENTRIES_SECTION, giveaway_id, entries)
            return True, f"Participação confirmada. Agora são **{len(entries)}** participante(s)."

    async def entries(self, giveaway_id: str) -> list[int]:
        section = await self.state.get_section(ENTRIES_SECTION)
        values = section.get(giveaway_id, [])
        return sorted({_int(value) for value in values if _int(value)}) if isinstance(values, list) else []

    async def draw(self, guild: discord.Guild, giveaway_id: str, *, reroll: bool = False) -> list[int]:
        lock = self._locks.setdefault(giveaway_id, asyncio.Lock())
        async with lock:
            payload = await self.get(guild.id, giveaway_id)
            if payload is None:
                raise ValueError("Sorteio não encontrado.")
            if not reroll and payload.get("status") not in {"ACTIVE", "DRAWING"}:
                raise ValueError("Esse sorteio não está ativo.")
            if reroll and payload.get("status") != "ENDED":
                raise ValueError("O reroll só pode ser usado em um sorteio encerrado.")
            if reroll and payload.get("ended_reason") == "INSUFFICIENT_PARTICIPANTS":
                raise ValueError("Não é possível fazer reroll: o sorteio terminou sem o mínimo de participantes.")
            eligible = [member_id for member_id in await self.entries(giveaway_id) if guild.get_member(member_id)]
            minimum_participants = _minimum_giveaway_participants(payload)
            previous = {_int(value) for value in payload.get("winner_ids", [])}
            pool = [member_id for member_id in eligible if member_id not in previous] if reroll else eligible
            if not pool and reroll:
                pool = eligible
            insufficient_participants = not reroll and len(eligible) < minimum_participants
            if insufficient_participants:
                winners = []
            else:
                winner_count = min(_int(payload.get("winners_count")) or 1, len(pool))
                weights = {
                    member_id: self.entry_weight(guild.get_member(member_id), payload)
                    for member_id in pool
                }
                winners = _weighted_sample_without_replacement(pool, weights, winner_count)
            if reroll:
                rerolls = payload.get("rerolls", [])
                rerolls = rerolls if isinstance(rerolls, list) else []
                rerolls.append({"winner_ids": winners, "created_at": _now()})
                payload["rerolls"] = rerolls[-20:]
            else:
                payload.update(
                    {
                        "winner_ids": winners,
                        "status": "ENDED",
                        "ended_at": _now(),
                        "ended_reason": (
                            "INSUFFICIENT_PARTICIPANTS" if insufficient_participants else "COMPLETED"
                        ),
                    }
                )
            await self._save(guild.id, giveaway_id, payload)
            await self._finish_public_message(guild, payload, winners, reroll=reroll)
            await self._append_history(guild.id, payload, winners, reroll=reroll)
            await self.logs.send_event(
                guild,
                "giveaways",
                title="Sorteio refeito" if reroll else "Sorteio concluído",
                description=(
                    f"**{payload.get('prize')}** terminou sem vencedores: eram necessários "
                    f"{minimum_participants} participantes, mas houve {len(eligible)}."
                    if insufficient_participants
                    else
                    f"**{payload.get('prize')}** terminou com {len(winners)} vencedor(es)."
                    if winners
                    else f"**{payload.get('prize')}** terminou sem participantes elegíveis."
                ),
                severity="INFO",
                fields=(
                    ("ID", giveaway_id, True),
                    ("Participantes elegíveis", str(len(eligible)), True),
                    ("Mínimo exigido", str(minimum_participants), True),
                    ("Vencedores", ", ".join(f"<@{item}>" for item in winners) or "nenhum", False),
                ),
            )
            if not reroll and _int(payload.get("recurrence_hours")) > 0:
                await self._schedule_recurrence(guild, payload)
            return winners

    async def reroll_latest(self, guild: discord.Guild) -> tuple[dict[str, Any], list[int]]:
        ended = await self.list_for_guild(guild.id, statuses={"ENDED"})
        if not ended:
            raise ValueError("Ainda não existe sorteio concluído para refazer.")
        payload = ended[0]
        winners = await self.draw(guild, str(payload["id"]), reroll=True)
        return payload, winners

    async def process_due(self) -> dict[str, int]:
        published = ended = failed = 0
        now = datetime.now(timezone.utc)
        for guild in list(self.bot.guilds):
            giveaways = await self.list_for_guild(guild.id, statuses={"SCHEDULED", "PENDING", "ACTIVE"})
            for payload in giveaways:
                try:
                    status = str(payload.get("status"))
                    if status in {"SCHEDULED", "PENDING"} and now >= _datetime(payload.get("starts_at")):
                        await self.publish(guild, str(payload["id"]))
                        published += 1
                    elif status == "ACTIVE" and now >= _datetime(payload.get("ends_at")):
                        await self.draw(guild, str(payload["id"]))
                        ended += 1
                except (ValueError, discord.HTTPException):
                    failed += 1
                    LOGGER.exception("Falha ao processar sorteio %s", payload.get("id"))
        return {"published": published, "ended": ended, "failed": failed}

    async def register_active_views(self) -> None:
        for guild in list(self.bot.guilds):
            for payload in await self.list_for_guild(guild.id, statuses={"ACTIVE"}):
                message_id = _int(payload.get("message_id"))
                if message_id:
                    self._register_view(
                        str(payload["id"]),
                        message_id,
                        mode=_giveaway_visual_mode(payload),
                    )
                    try:
                        await self.refresh_public_message(guild, str(payload["id"]))
                    except discord.NotFound:
                        payload["status"] = "CANCELLED"
                        payload["ended_reason"] = "PUBLIC_MESSAGE_MISSING"
                        payload["cancelled_at"] = _now()
                        await self._save(guild.id, str(payload["id"]), payload)
                        LOGGER.info(
                            "Sorteio %s cancelado porque a mensagem pública foi apagada.",
                            payload.get("id"),
                        )
                    except (ValueError, discord.Forbidden, discord.HTTPException):
                        LOGGER.warning("Não foi possível atualizar o sorteio ativo %s.", payload.get("id"))

    async def active_embed(self, guild: discord.Guild) -> discord.Embed:
        values = await self.list_for_guild(guild.id, statuses={"SCHEDULED", "PENDING", "ACTIVE"})
        lines = [
            f"`{item.get('status')}` **{item.get('prize')}** — termina {discord.utils.format_dt(_datetime(item.get('ends_at')), 'R')}"
            for item in values[:15]
        ]
        return discord.Embed(
            title="Sorteios ativos e agendados",
            description="\n".join(lines)[:4000] or "Nenhum sorteio ativo ou agendado.",
            color=discord.Color.from_rgb(255, 80, 120),
            timestamp=datetime.now(timezone.utc),
        )

    async def history_embed(self, guild: discord.Guild) -> discord.Embed:
        section = await self.state.get_section(HISTORY_SECTION)
        values = section.get(str(guild.id), [])
        entries = [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []
        lines = [
            f"**{item.get('prize')}** — {len(item.get('winner_ids', []))} vencedor(es) • {item.get('action')}"
            for item in entries[-15:][::-1]
        ]
        return discord.Embed(
            title="Histórico de sorteios",
            description="\n".join(lines)[:4000] or "Nenhum sorteio concluído.",
            color=discord.Color.from_rgb(110, 118, 129),
            timestamp=datetime.now(timezone.utc),
        )

    async def detail_embed(self, guild: discord.Guild, giveaway_id: str) -> discord.Embed:
        payload = await self.get(guild.id, giveaway_id)
        if payload is None:
            raise ValueError("Sorteio não encontrado.")
        embed = await self.giveaway_embed(guild, payload)
        embed.title = f"Gerenciar • {payload.get('prize')}"
        embed.add_field(name="Estado", value=str(payload.get("status", "DRAFT")), inline=True)
        embed.add_field(name="ID", value=f"`{giveaway_id}`", inline=True)
        embed.add_field(
            name="Mensagem pública",
            value=(
                f"[Abrir](https://discord.com/channels/{guild.id}/{payload.get('channel_id')}/{payload.get('message_id')})"
                if _int(payload.get("message_id"))
                else "Ainda não publicada"
            ),
            inline=True,
        )
        mention_roles = [guild.get_role(role_id) for role_id in _payload_role_ids(payload, "mention_role_ids")]
        embed.add_field(
            name="Cargos mencionados",
            value=" ".join(role.mention for role in mention_roles if role is not None) or "Nenhum",
            inline=False,
        )
        return embed

    async def participants_embed(self, guild: discord.Guild, giveaway_id: str) -> discord.Embed:
        payload = await self.get(guild.id, giveaway_id)
        if payload is None:
            raise ValueError("Sorteio não encontrado.")
        participant_ids = await self.entries(giveaway_id)
        lines: list[str] = []
        total_entries = 0
        for member_id in participant_ids[:75]:
            member = guild.get_member(member_id)
            weight = self.entry_weight(member, payload)
            total_entries += weight
            lines.append(
                f"• {member.mention if member else f'<@{member_id}>'} (`{member_id}`) • **{weight} entrada(s)**"
            )
        if len(participant_ids) > 75:
            total_entries = sum(
                self.entry_weight(guild.get_member(member_id), payload)
                for member_id in participant_ids
            )
        description = "\n".join(lines) or "Nenhum participante até agora."
        if len(participant_ids) > 75:
            description += f"\n\n... e mais **{len(participant_ids) - 75}** participante(s)."
        embed = discord.Embed(
            title=f"Participantes • {payload.get('prize')}",
            description=description[:4000],
            color=discord.Color.from_rgb(255, 80, 120),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Participantes", value=str(len(participant_ids)), inline=True)
        embed.add_field(name="Entradas", value=str(total_entries), inline=True)
        embed.add_field(name="Estado", value=str(payload.get("status")), inline=True)
        return embed

    async def update_details(
        self,
        guild: discord.Guild,
        giveaway_id: str,
        *,
        prize: str,
        details: str,
        image_url: str,
        duration_minutes: int,
        winners_count: int,
        minimum_server_days: int,
        start_delay_minutes: int,
        recurrence_hours: int,
        exact_ends_at: datetime | None = None,
    ) -> dict[str, Any]:
        payload = await self.get(guild.id, giveaway_id)
        if payload is None:
            raise ValueError("Sorteio não encontrado.")
        if payload.get("status") == "ENDED":
            raise ValueError("Reutilize o sorteio encerrado para criar uma nova edição.")
        now = datetime.now(timezone.utc)
        starts_at = now + timedelta(minutes=max(0, start_delay_minutes))
        payload.update(
            {
                "prize": str(prize).strip()[:256],
                "details": str(details).strip()[:4000],
                "image_url": _optional_http_url(image_url),
                "duration_minutes": max(1, int(duration_minutes)),
                "winners_count": max(1, min(20, int(winners_count))),
                "minimum_participants": max(1, min(20, int(winners_count))) * 4,
                "minimum_server_days": max(0, int(minimum_server_days)),
                "recurrence_hours": max(0, int(recurrence_hours)),
                "updated_at": _now(),
            }
        )
        if exact_ends_at is not None:
            exact_utc = exact_ends_at.astimezone(timezone.utc)
            if exact_utc <= (now if payload.get("status") == "ACTIVE" else starts_at):
                raise ValueError("A data final do sorteio precisa ser posterior ao início.")
            if payload.get("status") != "ACTIVE":
                payload["starts_at"] = starts_at.isoformat()
            payload["ends_at"] = exact_utc.isoformat()
            payload["duration_minutes"] = max(
                1,
                int((exact_utc - (now if payload.get("status") == "ACTIVE" else starts_at)).total_seconds() // 60),
            )
        elif payload.get("status") != "ACTIVE":
            payload["starts_at"] = starts_at.isoformat()
            payload["ends_at"] = (starts_at + timedelta(minutes=payload["duration_minutes"])).isoformat()
        else:
            payload["ends_at"] = (now + timedelta(minutes=payload["duration_minutes"])).isoformat()
        await self._save(guild.id, giveaway_id, payload)
        if payload.get("status") == "ACTIVE":
            await self.refresh_public_message(guild, giveaway_id)
        return payload

    async def set_required_roles(
        self,
        guild: discord.Guild,
        giveaway_id: str,
        role_ids: list[int],
    ) -> dict[str, Any]:
        payload = await self.get(guild.id, giveaway_id)
        if payload is None:
            raise ValueError("Sorteio não encontrado.")
        values = _unique_ids(role_ids)[:5]
        payload["required_role_ids"] = values
        payload["required_role_id"] = values[0] if values else 0
        payload["updated_at"] = _now()
        await self._save(guild.id, giveaway_id, payload)
        if payload.get("status") == "ACTIVE":
            await self.refresh_public_message(guild, giveaway_id)
        return payload

    async def set_mention_roles(
        self,
        guild: discord.Guild,
        giveaway_id: str,
        role_ids: list[int],
    ) -> dict[str, Any]:
        payload = await self.get(guild.id, giveaway_id)
        if payload is None:
            raise ValueError("Sorteio não encontrado.")
        payload["mention_role_ids"] = _unique_ids(role_ids)[:25]
        payload["updated_at"] = _now()
        await self._save(guild.id, giveaway_id, payload)
        return payload

    async def set_bonus_roles(
        self,
        guild: discord.Guild,
        giveaway_id: str,
        role_ids: list[int],
        extra_entries: int,
    ) -> dict[str, Any]:
        payload = await self.get(guild.id, giveaway_id)
        if payload is None:
            raise ValueError("Sorteio não encontrado.")
        amount = _bounded_int(extra_entries, 1, 100, "entradas extras")
        configured = {
            _int(item.get("role_id")): _int(item.get("extra_entries"))
            for item in payload.get("bonus_roles", [])
            if isinstance(item, dict) and _int(item.get("role_id"))
        }
        for role_id in _unique_ids(role_ids)[:25]:
            configured[role_id] = amount
        payload["bonus_roles"] = [
            {"role_id": role_id, "extra_entries": entries}
            for role_id, entries in configured.items()
        ][:25]
        payload["updated_at"] = _now()
        await self._save(guild.id, giveaway_id, payload)
        if payload.get("status") == "ACTIVE":
            await self.refresh_public_message(guild, giveaway_id)
        return payload

    async def clear_bonus_roles(self, guild: discord.Guild, giveaway_id: str) -> dict[str, Any]:
        payload = await self.get(guild.id, giveaway_id)
        if payload is None:
            raise ValueError("Sorteio não encontrado.")
        payload["bonus_roles"] = []
        payload["updated_at"] = _now()
        await self._save(guild.id, giveaway_id, payload)
        if payload.get("status") == "ACTIVE":
            await self.refresh_public_message(guild, giveaway_id)
        return payload

    async def remove_bonus_role(
        self,
        guild: discord.Guild,
        giveaway_id: str,
        role_id: int,
    ) -> dict[str, Any]:
        payload = await self.get(guild.id, giveaway_id)
        if payload is None:
            raise ValueError("Sorteio não encontrado.")
        payload["bonus_roles"] = [
            item
            for item in payload.get("bonus_roles", [])
            if isinstance(item, dict) and _int(item.get("role_id")) != _int(role_id)
        ]
        payload["updated_at"] = _now()
        await self._save(guild.id, giveaway_id, payload)
        if payload.get("status") == "ACTIVE":
            await self.refresh_public_message(guild, giveaway_id)
        return payload

    def entry_weight(self, member: discord.Member | None, payload: dict[str, Any]) -> int:
        if member is None:
            return 1
        member_role_ids = {role.id for role in getattr(member, "roles", [])}
        extra = sum(
            max(0, min(100, _int(item.get("extra_entries"))))
            for item in payload.get("bonus_roles", [])
            if isinstance(item, dict) and _int(item.get("role_id")) in member_role_ids
        )
        return 1 + extra

    async def refresh_public_message(self, guild: discord.Guild, giveaway_id: str) -> discord.Message:
        payload = await self.get(guild.id, giveaway_id)
        if payload is None:
            raise ValueError("Sorteio não encontrado.")
        channel = guild.get_channel(_int(payload.get("channel_id")))
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("O canal público do sorteio não está disponível.")
        message_id = _int(payload.get("message_id"))
        if not message_id:
            raise ValueError("Esse sorteio ainda não foi publicado.")
        await self._refresh_announcement(channel, payload)
        message = await channel.fetch_message(message_id)
        if _giveaway_visual_mode(payload) == "v1":
            entries = await self.entries(giveaway_id)
            total_entries = sum(
                self.entry_weight(guild.get_member(member_id), payload)
                for member_id in entries
            )
            await message.edit(
                content=None,
                embeds=self.public_v1_embeds(
                    guild,
                    payload,
                    len(entries),
                    total_entries=total_entries,
                ),
                view=GiveawayClassicEntryView(
                    self,
                    giveaway_id,
                    active=payload.get("status") == "ACTIVE",
                ),
            )
        else:
            await message.edit(content=None, embeds=[], view=await self.public_view(guild, payload))
        return message

    async def v1_editor_session(
        self,
        interaction: discord.Interaction,
        giveaway_id: str,
    ) -> EmbedEditorSession:
        if interaction.guild is None or interaction.channel is None:
            raise ValueError("Este editor só pode ser aberto dentro do servidor.")
        payload = await self.get(interaction.guild.id, giveaway_id)
        if payload is None:
            raise ValueError("Sorteio não encontrado.")

        async def save_visual(
            save_interaction: discord.Interaction,
            session: EmbedEditorSession,
        ) -> str:
            if save_interaction.guild is None or save_interaction.guild.id != interaction.guild.id:
                raise ValueError("O servidor deste editor não está mais disponível.")
            current = await self.get(save_interaction.guild.id, giveaway_id)
            if current is None:
                raise ValueError("Sorteio não encontrado.")
            old_mode = _giveaway_visual_mode(current)
            exported = session.export_payload()
            for embed_data in exported.get("embeds", []):
                if isinstance(embed_data, dict):
                    embed_data["buttons"] = []
            current["embed_v1_payload"] = exported
            current["message_content"] = _safe_giveaway_content(session.message_content)
            current["visual_mode"] = "v1"
            current["updated_at"] = _now()
            await self._save(save_interaction.guild.id, giveaway_id, current)
            if _int(current.get("message_id")):
                await self._refresh_or_republish_after_mode_change(
                    save_interaction.guild,
                    giveaway_id,
                    old_mode=old_mode,
                    new_mode="v1",
                )
            return "Embed V1 salva no sorteio. Use **Enviar sorteio** no estúdio para publicar o rascunho."

        session = EmbedEditorSession(
            interaction.user.id,
            interaction.channel,
            save_callback=save_visual,
            allow_link_buttons=False,
        )
        session.apply_payload(
            payload.get("embed_v1_payload") or _default_giveaway_v1_payload(),
            fallback_prompt="Sorteio",
        )
        return session

    async def visual_editor_session(
        self,
        interaction: discord.Interaction,
        giveaway_id: str,
    ) -> V2EditorSession:
        if interaction.guild is None or interaction.channel is None:
            raise ValueError("Este editor só pode ser aberto dentro do servidor.")
        payload = await self.get(interaction.guild.id, giveaway_id)
        if payload is None:
            raise ValueError("Sorteio não encontrado.")

        async def save_visual(
            save_interaction: discord.Interaction,
            session: V2EditorSession,
        ) -> str:
            if save_interaction.guild is None or save_interaction.guild.id != interaction.guild.id:
                raise ValueError("O servidor deste editor não está mais disponível.")
            current = await self.get(save_interaction.guild.id, giveaway_id)
            if current is None:
                raise ValueError("Sorteio não encontrado.")
            old_mode = _giveaway_visual_mode(current)
            clean_containers = _protected_giveaway_containers(session.containers)
            current["containers_v2"] = [container.to_dict() for container in clean_containers]
            current["message_content"] = _safe_giveaway_content(session.message_content)
            current["visual_mode"] = "v2"
            current["updated_at"] = _now()
            await self._save(save_interaction.guild.id, giveaway_id, current)
            if _int(current.get("message_id")):
                await self._refresh_or_republish_after_mode_change(
                    save_interaction.guild,
                    giveaway_id,
                    old_mode=old_mode,
                    new_mode="v2",
                )
            return "Visual V2 do sorteio salvo. Os botões oficiais permaneceram protegidos e fora dos containers."

        session = V2EditorSession(
            interaction.user.id,
            interaction.channel,
            save_callback=save_visual,
            allow_component_buttons=False,
            allow_message_content=True,
        )
        session.apply_payload(
            {
                "content": str(payload.get("message_content") or ""),
                "containers": payload.get("containers_v2") or [
                    container.to_dict() for container in _default_giveaway_containers()
                ],
            }
        )
        return session

    def public_v1_embeds(
        self,
        guild: discord.Guild,
        payload: dict[str, Any],
        participant_count: int,
        *,
        total_entries: int | None = None,
    ) -> list[discord.Embed]:
        raw = payload.get("embed_v1_payload")
        raw = raw if isinstance(raw, dict) else _default_giveaway_v1_payload()
        context = _giveaway_placeholder_context(
            guild,
            payload,
            participant_count,
            total_entries=total_entries,
        )
        rendered = _replace_placeholders_in_data(raw, context)
        drafts = [
            EmbedDraft.from_dict(item)
            for item in rendered.get("embeds", [])
            if isinstance(item, dict)
        ][:10]
        embeds: list[discord.Embed] = []
        for draft in drafts or [EmbedDraft(description="\u200b")]:
            draft.buttons = []
            embeds.extend(draft.to_embeds())
        return embeds[:10]

    async def _refresh_or_republish_after_mode_change(
        self,
        guild: discord.Guild,
        giveaway_id: str,
        *,
        old_mode: str,
        new_mode: str,
    ) -> None:
        if old_mode == new_mode:
            await self.refresh_public_message(guild, giveaway_id)
            return
        payload = await self.get(guild.id, giveaway_id)
        if payload is None:
            return
        channel = guild.get_channel(_int(payload.get("channel_id")))
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            message = await channel.fetch_message(_int(payload.get("message_id")))
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        payload["message_id"] = 0
        await self._save(guild.id, giveaway_id, payload)
        await self.publish(guild, giveaway_id)

    async def public_view(
        self,
        guild: discord.Guild,
        payload: dict[str, Any],
    ) -> "GiveawayEntryView":
        entries = await self.entries(str(payload.get("id") or ""))
        total_entries = sum(self.entry_weight(guild.get_member(member_id), payload) for member_id in entries)
        containers = _render_giveaway_containers(
            guild,
            payload,
            len(entries),
            total_entries=total_entries,
        )
        return GiveawayEntryView(
            self,
            str(payload.get("id") or ""),
            containers=containers,
            active=payload.get("status") == "ACTIVE",
        )

    async def _refresh_announcement(
        self,
        channel: discord.TextChannel,
        payload: dict[str, Any],
    ) -> None:
        content = _giveaway_announcement_content(payload)
        announcement_id = _int(payload.get("announcement_message_id"))
        announcement = None
        if announcement_id:
            try:
                announcement = await channel.fetch_message(announcement_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                announcement = None
        if content:
            if announcement is not None:
                await announcement.edit(content=content, allowed_mentions=_giveaway_allowed_mentions())
            else:
                announcement = await channel.send(content, allowed_mentions=_giveaway_allowed_mentions())
                payload["announcement_message_id"] = announcement.id
                await self._save(_int(payload.get("guild_id")), str(payload.get("id")), payload)
        elif announcement is not None:
            await announcement.delete()
            payload["announcement_message_id"] = 0
            await self._save(_int(payload.get("guild_id")), str(payload.get("id")), payload)

    async def reuse(
        self,
        guild: discord.Guild,
        giveaway_id: str,
        *,
        created_by: int,
    ) -> dict[str, Any]:
        source = await self.get(guild.id, giveaway_id)
        if source is None:
            raise ValueError("Sorteio não encontrado.")
        now = datetime.now(timezone.utc)
        duration = max(1, _int(source.get("duration_minutes")) or 60)
        new_id = uuid.uuid4().hex[:12]
        payload = {
            **source,
            "id": new_id,
            "message_id": 0,
            "announcement_message_id": 0,
            "created_by": created_by,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "starts_at": now.isoformat(),
            "ends_at": (now + timedelta(minutes=duration)).isoformat(),
            "status": "DRAFT",
            "winner_ids": [],
            "rerolls": [],
            "ended_reason": "",
            "reused_from": giveaway_id,
        }
        await self._save(guild.id, new_id, payload)
        await self.state.update_section(ENTRIES_SECTION, new_id, [])
        return payload

    async def cancel(self, guild: discord.Guild, giveaway_id: str) -> dict[str, Any]:
        payload = await self.get(guild.id, giveaway_id)
        if payload is None:
            raise ValueError("Sorteio não encontrado.")
        if payload.get("status") == "ENDED":
            raise ValueError("Esse sorteio já foi encerrado.")
        payload.update({"status": "CANCELLED", "cancelled_at": _now()})
        await self._save(guild.id, giveaway_id, payload)
        if _int(payload.get("message_id")):
            try:
                await self.refresh_public_message(guild, giveaway_id)
            except (ValueError, discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Não foi possível atualizar o sorteio cancelado %s.", giveaway_id)
        return payload

    async def giveaway_embed(self, guild: discord.Guild, payload: dict[str, Any]) -> discord.Embed:
        entries = await self.entries(str(payload["id"]))
        required_roles = [guild.get_role(role_id) for role_id in _payload_role_ids(payload, "required_role_ids")]
        details = str(payload.get("details") or "").strip()
        embed = discord.Embed(
            title=f"Sorteio • {payload.get('prize')}",
            description=(
                details
                or "Clique em **Participar** para entrar. A Bella valida os requisitos no momento da inscrição."
            )[:4096],
            color=discord.Color.from_rgb(255, 80, 120),
            timestamp=_datetime(payload.get("ends_at")),
        )
        embed.add_field(name="Vencedores", value=str(payload.get("winners_count", 1)), inline=True)
        embed.add_field(name="Participantes", value=str(len(entries)), inline=True)
        embed.add_field(
            name="Mínimo para concluir",
            value=str(_minimum_giveaway_participants(payload)),
            inline=True,
        )
        embed.add_field(name="Termina", value=discord.utils.format_dt(_datetime(payload.get("ends_at")), "R"), inline=True)
        requirements = []
        role_mentions = [role.mention for role in required_roles if role is not None]
        if role_mentions:
            requirements.append(f"Cargo (um deles): {' '.join(role_mentions)}")
        if _int(payload.get("minimum_server_days")):
            requirements.append(f"Tempo no servidor: {_int(payload.get('minimum_server_days'))} dias")
        embed.add_field(name="Requisitos", value="\n".join(requirements) or "Sem requisitos adicionais", inline=False)
        image_url = str(payload.get("image_url") or "")
        if image_url:
            embed.set_image(url=image_url)
        embed.set_footer(text=f"ID {payload.get('id')} • Bella Giveaways")
        return embed

    def panel_view(self) -> "GiveawayAdminView":
        return GiveawayAdminView(self)

    async def _save(self, guild_id: int, giveaway_id: str, payload: dict[str, Any]) -> None:
        def mutate(section: dict[str, Any]) -> None:
            guild_data = section.setdefault(str(guild_id), {})
            if not isinstance(guild_data, dict):
                guild_data = {}
                section[str(guild_id)] = guild_data
            guild_data[giveaway_id] = payload

        await self.state.mutate_section(GIVEAWAYS_SECTION, mutate)

    async def _finish_public_message(
        self,
        guild: discord.Guild,
        payload: dict[str, Any],
        winners: list[int],
        *,
        reroll: bool,
    ) -> None:
        channel = guild.get_channel(_int(payload.get("channel_id")))
        if not isinstance(channel, discord.TextChannel):
            return
        mentions = " ".join(f"<@{winner_id}>" for winner_id in winners)
        insufficient = payload.get("ended_reason") == "INSUFFICIENT_PARTICIPANTS"
        minimum_participants = _minimum_giveaway_participants(payload)
        participant_count = len(await self.entries(str(payload.get("id"))))
        if reroll:
            await channel.send(
                f"Novo resultado de **{payload.get('prize')}**: {mentions or 'nenhum participante elegível'}",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            return
        message_id = _int(payload.get("message_id"))
        try:
            message = await channel.fetch_message(message_id)
            if _giveaway_visual_mode(payload) == "v1":
                entries = await self.entries(str(payload.get("id")))
                await message.edit(
                    content=None,
                    embeds=self.public_v1_embeds(
                        guild,
                        payload,
                        len(entries),
                        total_entries=sum(
                            self.entry_weight(guild.get_member(member_id), payload)
                            for member_id in entries
                        ),
                    ),
                    view=GiveawayClassicEntryView(self, str(payload.get("id")), active=False),
                )
            else:
                await message.edit(content=None, embeds=[], view=await self.public_view(guild, payload))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Não foi possível atualizar a mensagem do sorteio %s.", payload.get("id"))
        if insufficient:
            result_message = (
                f"Sorteio **{payload.get('prize')}** encerrado sem vencedores. "
                f"Foram registrados **{participant_count} participante(s)**, mas o mínimo necessário era "
                f"**{minimum_participants}** para {_int(payload.get('winners_count')) or 1} vencedor(es)."
            )
        else:
            result_message = (
                f"Sorteio **{payload.get('prize')}** concluído. "
                f"{mentions or 'Não houve participante elegível.'}"
            )
        await channel.send(
            result_message,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )

    async def _append_history(
        self,
        guild_id: int,
        payload: dict[str, Any],
        winners: list[int],
        *,
        reroll: bool,
    ) -> None:
        record = {
            "giveaway_id": payload.get("id"),
            "prize": payload.get("prize"),
            "winner_ids": winners,
            "action": "REROLL" if reroll else "DRAW",
            "ended_reason": payload.get("ended_reason"),
            "minimum_participants": _minimum_giveaway_participants(payload),
            "created_at": _now(),
        }

        def mutate(section: dict[str, Any]) -> None:
            values = section.setdefault(str(guild_id), [])
            if not isinstance(values, list):
                values = []
            values.append(record)
            section[str(guild_id)] = values[-MAX_HISTORY_PER_GUILD:]

        await self.state.mutate_section(HISTORY_SECTION, mutate)

    async def _schedule_recurrence(self, guild: discord.Guild, payload: dict[str, Any]) -> None:
        recurrence_hours = _int(payload.get("recurrence_hours"))
        if recurrence_hours <= 0:
            return
        next_start = datetime.now(timezone.utc) + timedelta(hours=recurrence_hours)
        giveaway_id = uuid.uuid4().hex[:12]
        next_payload = {
            **payload,
            "id": giveaway_id,
            "message_id": 0,
            "announcement_message_id": 0,
            "starts_at": next_start.isoformat(),
            "ends_at": (next_start + timedelta(minutes=_int(payload.get("duration_minutes")) or 1)).isoformat(),
            "created_at": _now(),
            "status": "SCHEDULED",
            "winner_ids": [],
            "rerolls": [],
            "ended_reason": "",
            "parent_giveaway_id": payload.get("id"),
        }
        await self._save(guild.id, giveaway_id, next_payload)

    def _register_view(self, giveaway_id: str, message_id: int, *, mode: str = "v2") -> None:
        if giveaway_id in self._views_registered or not hasattr(self.bot, "add_view"):
            return
        view: discord.ui.View | discord.ui.LayoutView
        if mode == "v1":
            view = GiveawayClassicEntryView(self, giveaway_id)
        else:
            view = GiveawayEntryView(self, giveaway_id)
        self.bot.add_view(view, message_id=message_id)
        self._views_registered.add(giveaway_id)


class GiveawayEntryView(discord.ui.LayoutView):
    def __init__(
        self,
        service: GiveawayService,
        giveaway_id: str,
        *,
        containers: list[V2ContainerDraft] | None = None,
        active: bool = True,
    ) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.giveaway_id = giveaway_id
        for container in (containers or [])[:MAX_GIVEAWAY_CONTAINERS]:
            self.add_item(container.to_container(preview=False))
        if not active:
            return
        button = discord.ui.Button(
            label="Participar",
            style=discord.ButtonStyle.success,
            custom_id=f"bella:giveaway:enter:{giveaway_id}",
        )
        button.callback = self._enter
        self.add_item(discord.ui.ActionRow(button))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return not await guard_public_interaction(interaction, "sorteios")

    async def _enter(self, interaction: discord.Interaction) -> None:
        accepted, message = await self.service.enter(interaction, self.giveaway_id)
        await interaction.response.send_message(message, ephemeral=True)
        if accepted and interaction.guild is not None and interaction.message is not None:
            try:
                await self.service.refresh_public_message(interaction.guild, self.giveaway_id)
            except (ValueError, discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class GiveawayClassicEntryView(discord.ui.View):
    def __init__(self, service: GiveawayService, giveaway_id: str, *, active: bool = True) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.giveaway_id = giveaway_id
        if not active:
            return
        button = discord.ui.Button(
            label="Participar",
            style=discord.ButtonStyle.success,
            custom_id=f"bella:giveaway:enter:{giveaway_id}",
        )
        button.callback = self._enter
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return not await guard_public_interaction(interaction, "sorteios")

    async def _enter(self, interaction: discord.Interaction) -> None:
        accepted, message = await self.service.enter(interaction, self.giveaway_id)
        await interaction.response.send_message(message, ephemeral=True)
        if accepted and interaction.guild is not None:
            try:
                await self.service.refresh_public_message(interaction.guild, self.giveaway_id)
            except (ValueError, discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class GiveawayAdminView(discord.ui.View):
    def __init__(self, service: GiveawayService) -> None:
        super().__init__(timeout=None)
        self.service = service

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = isinstance(interaction.user, discord.Member) and self.service.authorize(interaction.user)
        if not allowed:
            await interaction.response.send_message("Você não possui acesso a este painel.", ephemeral=True)
        return allowed

    @discord.ui.button(label="Canal", style=discord.ButtonStyle.secondary, custom_id="bella:giveaways:channel:v1")
    async def channel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Escolha o canal onde os sorteios públicos serão enviados:",
            view=GiveawayChannelView(self.service, interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(label="Criar sorteio", style=discord.ButtonStyle.success, custom_id="bella:giveaways:create:v1")
    async def create(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            payload = await self.service.create(
                interaction.guild,
                prize="Novo sorteio",
                winners_count=1,
                duration_minutes=1,
                required_role_id=None,
                minimum_server_days=0,
                start_delay_minutes=0,
                recurrence_hours=0,
                created_by=interaction.user.id,
                details="Personalize esta mensagem no gerador de embeds da Bella.",
                publish_immediately=False,
                exact_ends_at=datetime.now(timezone.utc) + timedelta(days=7),
            )
            session = await self.service.visual_editor_session(interaction, str(payload.get("id")))
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(f"Não consegui criar o rascunho: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"## Estúdio do Sorteio\nRascunho `{payload.get('id')}` criado. "
            "O gerador V2 está aberto abaixo; use **Configurar dados** para definir prêmio, data e vencedores.",
            view=GiveawayCreationStudioView(
                self.service,
                str(payload.get("id")),
                interaction.user.id,
            ),
            ephemeral=True,
        )
        await interaction.followup.send(view=session.editor_view(), ephemeral=True)

    @discord.ui.button(label="Gerenciar", style=discord.ButtonStyle.primary, custom_id="bella:giveaways:manage:v2")
    async def manage(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        giveaways = await self.service.list_for_guild(interaction.guild.id)
        if not giveaways:
            await interaction.response.send_message("Ainda não existe sorteio para gerenciar.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Selecione o sorteio que deseja administrar:",
            view=GiveawayPickerView(self.service, giveaways, interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(label="Ativos", style=discord.ButtonStyle.primary, custom_id="bella:giveaways:active:v1")
    async def active(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.send_message(embed=await self.service.active_embed(interaction.guild), ephemeral=True)

    @discord.ui.button(label="Processar agora", style=discord.ButtonStyle.secondary, custom_id="bella:giveaways:process:v1")
    async def process(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.service.process_due()
        await interaction.followup.send(
            f"Processamento concluído: {result['published']} publicados, {result['ended']} finalizados, {result['failed']} falhas.",
            ephemeral=True,
        )

    @discord.ui.button(label="Reroll recente", style=discord.ButtonStyle.danger, custom_id="bella:giveaways:reroll:v1")
    async def reroll(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            payload, winners = await self.service.reroll_latest(interaction.guild)
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"Reroll de **{payload.get('prize')}**: {', '.join(f'<@{item}>' for item in winners) or 'nenhum vencedor'}.",
            ephemeral=True,
        )

    @discord.ui.button(label="Histórico", style=discord.ButtonStyle.secondary, custom_id="bella:giveaways:history:v1")
    async def history(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.send_message(embed=await self.service.history_embed(interaction.guild), ephemeral=True)

    @discord.ui.button(
        label="Gerador de embeds",
        style=discord.ButtonStyle.primary,
        custom_id="bella:giveaways:customize:v1",
    )
    async def customize(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._open_picker(interaction, "studio")

    @discord.ui.button(
        label="Bônus por cargo",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:giveaways:bonus-roles:v1",
    )
    async def bonus_roles(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._open_picker(interaction, "bonus_roles")

    async def _open_picker(self, interaction: discord.Interaction, action: str) -> None:
        if interaction.guild is None:
            return
        giveaways = await self.service.list_for_guild(interaction.guild.id)
        if not giveaways:
            await interaction.response.send_message("Ainda não existe sorteio para configurar.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Selecione o sorteio que deseja configurar:",
            view=GiveawayPickerView(self.service, giveaways, interaction.user.id, action=action),
            ephemeral=True,
        )


class GiveawayChannelView(discord.ui.View):
    def __init__(self, service: GiveawayService, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.owner_id = owner_id
        select = discord.ui.ChannelSelect(
            placeholder="Canal de sorteios",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        select.callback = self._selected
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Somente quem abriu esta configuração pode usá-la.", ephemeral=True)
            return False
        return True

    async def _selected(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        select = self.children[0]
        selected = select.values[0] if isinstance(select, discord.ui.ChannelSelect) else None
        channel_id = _int(getattr(selected, "id", 0))
        if not channel_id:
            await interaction.response.send_message("Canal inválido.", ephemeral=True)
            return
        await self.service.set_channel(interaction.guild.id, channel_id)
        await interaction.response.send_message(f"Canal de sorteios definido para <#{channel_id}>.", ephemeral=True)


class GiveawayPickerView(discord.ui.View):
    def __init__(
        self,
        service: GiveawayService,
        giveaways: list[dict[str, Any]],
        owner_id: int,
        *,
        action: str = "manage",
    ) -> None:
        super().__init__(timeout=600)
        self.service = service
        self.owner_id = owner_id
        self.action = action
        select = discord.ui.Select(
            placeholder="Escolha um sorteio",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=str(item.get("prize") or "Sorteio")[:100],
                    value=str(item.get("id")),
                    description=f"{item.get('status', 'DRAFT')} • {item.get('id')}"[:100],
                )
                for item in giveaways[:25]
            ],
        )
        select.callback = self._selected
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Somente quem abriu esta lista pode usá-la.", ephemeral=True)
            return False
        return True

    async def _selected(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        select = self.children[0]
        giveaway_id = select.values[0] if isinstance(select, discord.ui.Select) else ""
        if self.action == "studio":
            await interaction.response.send_message(
                "Abra o mesmo gerador de embeds da Bella em V1 ou V2 e publique quando estiver pronto.",
                view=GiveawayCreationStudioView(
                    self.service,
                    giveaway_id,
                    self.owner_id,
                ),
                ephemeral=True,
            )
            return
        if self.action == "bonus_roles":
            payload = await self.service.get(interaction.guild.id, giveaway_id)
            if payload is None:
                await interaction.response.send_message("Sorteio não encontrado.", ephemeral=True)
                return
            await interaction.response.send_message(
                f"**Bônus atuais**\n{_bonus_roles_text(interaction.guild, payload)}\n\n"
                "Cadastre cada cargo separadamente usando o ID ou a menção dele.",
                view=GiveawayBonusRoleView(self.service, giveaway_id, self.owner_id),
                ephemeral=True,
            )
            return
        try:
            embed = await self.service.detail_embed(interaction.guild, giveaway_id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=embed,
            view=GiveawayManageView(self.service, giveaway_id, self.owner_id),
            ephemeral=True,
        )


class GiveawayManageView(discord.ui.View):
    def __init__(self, service: GiveawayService, giveaway_id: str, owner_id: int) -> None:
        super().__init__(timeout=900)
        self.service = service
        self.giveaway_id = giveaway_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Somente quem abriu este gerenciador pode usá-lo.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Detalhes", style=discord.ButtonStyle.primary, row=0)
    async def details(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.send_message(
            embed=await self.service.detail_embed(interaction.guild, self.giveaway_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Editar", style=discord.ButtonStyle.secondary, row=0)
    async def edit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        payload = await self.service.get(interaction.guild.id, self.giveaway_id)
        if payload is None:
            await interaction.response.send_message("Sorteio não encontrado.", ephemeral=True)
            return
        await interaction.response.send_modal(GiveawayEditModal(self.service, payload))

    @discord.ui.button(label="Cargos exigidos", style=discord.ButtonStyle.secondary, row=0)
    async def required_roles(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Selecione até cinco cargos. Ter qualquer um deles libera a participação.",
            view=GiveawayRoleSelectView(self.service, self.giveaway_id, self.owner_id, "required"),
            ephemeral=True,
        )

    @discord.ui.button(label="Cargos para mencionar", style=discord.ButtonStyle.secondary, row=0)
    async def mention_roles(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Selecione qualquer cargo do servidor para mencionar na publicação (até 25 por sorteio).",
            view=GiveawayRoleSelectView(self.service, self.giveaway_id, self.owner_id, "mention"),
            ephemeral=True,
        )

    @discord.ui.button(label="Participantes", style=discord.ButtonStyle.secondary, row=0)
    async def participants(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.send_message(
            embed=await self.service.participants_embed(interaction.guild, self.giveaway_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Publicar / atualizar", style=discord.ButtonStyle.success, row=1)
    async def publish(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.send_message(
            "Escolha os cargos que receberão o ping junto da publicação ou envie explicitamente sem ping:",
            view=GiveawayPublishRoleView(self.service, self.giveaway_id, self.owner_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Encerrar agora", style=discord.ButtonStyle.danger, row=1)
    async def end_now(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            winners = await self.service.draw(interaction.guild, self.giveaway_id)
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"Sorteio encerrado. Vencedores: {' '.join(f'<@{item}>' for item in winners) or 'nenhum'}.",
            ephemeral=True,
        )

    @discord.ui.button(label="Reroll", style=discord.ButtonStyle.danger, row=1)
    async def reroll(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            winners = await self.service.draw(interaction.guild, self.giveaway_id, reroll=True)
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"Novo resultado: {' '.join(f'<@{item}>' for item in winners) or 'nenhum'}.",
            ephemeral=True,
        )

    @discord.ui.button(label="Reutilizar", style=discord.ButtonStyle.primary, row=1)
    async def reuse(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        payload = await self.service.reuse(
            interaction.guild,
            self.giveaway_id,
            created_by=interaction.user.id,
        )
        await interaction.response.send_message(
            f"Nova cópia criada como rascunho: `{payload.get('id')}`. Abra **Gerenciar** para editar e publicar.",
            ephemeral=True,
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        try:
            await self.service.cancel(interaction.guild, self.giveaway_id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message("Sorteio cancelado.", ephemeral=True)

    @discord.ui.button(label="Editar V2", style=discord.ButtonStyle.primary, row=2)
    async def customize_panel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            session = await self.service.visual_editor_session(interaction, self.giveaway_id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            "Use placeholders como `{premio}`, `{detalhes}`, `{participantes}`, `{vencedores}`, "
            "`{entradas_totais}`, `{termina}`, `{requisitos}`, `{bonus_cargos}`, `{status}`, `{imagem}` e `{id}`. "
            "O botão **Participar** é adicionado pela Bella fora dos containers e não pode ser alterado.",
            ephemeral=True,
        )
        await interaction.followup.send(view=session.editor_view(), ephemeral=True)

    @discord.ui.button(label="Editar V1", style=discord.ButtonStyle.primary, row=2)
    async def customize_v1(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            session = await self.service.v1_editor_session(interaction, self.giveaway_id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            "Gerenciador V1 aberto. Todos os elementos da embed podem ser personalizados; "
            "o botão **Participar** permanece protegido pela Bella.",
            embeds=session.editor_embeds(),
            view=session.preview_view(),
            ephemeral=True,
        )

    @discord.ui.button(label="Bônus por cargo", style=discord.ButtonStyle.secondary, row=2)
    async def bonus_roles(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        payload = await self.service.get(interaction.guild.id, self.giveaway_id)
        if payload is None:
            await interaction.response.send_message("Sorteio não encontrado.", ephemeral=True)
            return
        current = _bonus_roles_text(interaction.guild, payload)
        await interaction.response.send_message(
            f"**Bônus atuais**\n{current}\n\nCadastre um cargo por vez usando o ID ou a menção dele.",
            view=GiveawayBonusRoleView(self.service, self.giveaway_id, self.owner_id),
            ephemeral=True,
        )


class GiveawayCreationStudioView(discord.ui.View):
    def __init__(self, service: GiveawayService, giveaway_id: str, owner_id: int) -> None:
        super().__init__(timeout=1800)
        self.service = service
        self.giveaway_id = giveaway_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Somente quem criou este rascunho pode usar o estúdio.", ephemeral=True)
        return False

    @discord.ui.button(label="Gerenciador V1", style=discord.ButtonStyle.primary, row=0)
    async def edit_v1(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            session = await self.service.v1_editor_session(interaction, self.giveaway_id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            "Personalize a embed V1. O botão de participação não faz parte do editor e continuará protegido.",
            embeds=session.editor_embeds(),
            view=session.preview_view(),
            ephemeral=True,
        )

    @discord.ui.button(label="Gerenciador V2", style=discord.ButtonStyle.primary, row=0)
    async def edit_v2(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            session = await self.service.visual_editor_session(interaction, self.giveaway_id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            "Personalize os containers V2. O botão de participação ficará fora deles e continuará protegido.",
            ephemeral=True,
        )
        await interaction.followup.send(view=session.editor_view(), ephemeral=True)

    @discord.ui.button(label="Cargos e menções", style=discord.ButtonStyle.secondary, row=0)
    async def roles(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Escolha o que deseja configurar:",
            view=GiveawayCreationRolesView(self.service, self.giveaway_id, self.owner_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Bônus por cargo", style=discord.ButtonStyle.secondary, row=0)
    async def bonuses(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        payload = await self.service.get(interaction.guild.id, self.giveaway_id)
        if payload is None:
            await interaction.response.send_message("Sorteio não encontrado.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"**Bônus atuais**\n{_bonus_roles_text(interaction.guild, payload)}",
            view=GiveawayBonusRoleView(self.service, self.giveaway_id, self.owner_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Configurar dados", style=discord.ButtonStyle.secondary, row=0)
    async def configure(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        payload = await self.service.get(interaction.guild.id, self.giveaway_id)
        if payload is None:
            await interaction.response.send_message("Sorteio não encontrado.", ephemeral=True)
            return
        await interaction.response.send_modal(GiveawayEditModal(self.service, payload))

    @discord.ui.button(label="Enviar sorteio", style=discord.ButtonStyle.success, row=1)
    async def publish(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.send_message(
            "Selecione os cargos que serão mencionados ao enviar o sorteio. "
            "Se não quiser mencionar ninguém, use **Enviar sem ping**.",
            view=GiveawayPublishRoleView(self.service, self.giveaway_id, self.owner_id),
            ephemeral=True,
        )


class GiveawayPublishRoleView(discord.ui.View):
    def __init__(self, service: GiveawayService, giveaway_id: str, owner_id: int) -> None:
        super().__init__(timeout=600)
        self.service = service
        self.giveaway_id = giveaway_id
        self.owner_id = owner_id
        select = discord.ui.RoleSelect(
            placeholder="Cargos que receberão o ping",
            min_values=1,
            max_values=25,
        )
        select.callback = self._selected
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Somente quem abriu esta publicação pode usá-la.", ephemeral=True)
        return False

    async def _selected(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        select = self.children[0]
        role_ids = [
            _int(getattr(role, "id", 0))
            for role in (select.values if isinstance(select, discord.ui.RoleSelect) else [])
        ]
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.service.set_mention_roles(interaction.guild, self.giveaway_id, role_ids)
        except (ValueError, discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            await interaction.followup.send(f"Não consegui configurar os cargos do ping: {exc}", ephemeral=True)
            return
        await self._publish(interaction, ping_count=len(role_ids))

    @discord.ui.button(label="Enviar sem ping", style=discord.ButtonStyle.secondary, row=1)
    async def without_ping(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.service.set_mention_roles(interaction.guild, self.giveaway_id, [])
        except (ValueError, discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            await interaction.followup.send(f"Não consegui remover os cargos do ping: {exc}", ephemeral=True)
            return
        await self._publish(interaction, ping_count=0)

    async def _publish(self, interaction: discord.Interaction, *, ping_count: int) -> None:
        if interaction.guild is None:
            return
        try:
            payload = await self.service.get(interaction.guild.id, self.giveaway_id)
            if payload is None:
                raise ValueError("Sorteio não encontrado.")
            if payload.get("status") in {"ENDED", "CANCELLED"}:
                raise ValueError("Reutilize este sorteio para criar uma nova edição.")
            message = await self.service.publish(interaction.guild, self.giveaway_id)
        except (ValueError, discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            await interaction.followup.send(f"Não consegui publicar o sorteio: {exc}", ephemeral=True)
            return
        state = "publicado/atualizado" if message else "agendado"
        ping_text = f"com **{ping_count} cargo(s)** mencionado(s)" if ping_count else "sem ping"
        await interaction.followup.send(f"Sorteio {state} {ping_text}.", ephemeral=True)


class GiveawayCreationRolesView(discord.ui.View):
    def __init__(self, service: GiveawayService, giveaway_id: str, owner_id: int) -> None:
        super().__init__(timeout=600)
        self.service = service
        self.giveaway_id = giveaway_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Somente quem criou este rascunho pode configurar os cargos.", ephemeral=True)
        return False

    @discord.ui.button(label="Cargos exigidos", style=discord.ButtonStyle.secondary)
    async def required(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Selecione os cargos aceitos para participar:",
            view=GiveawayRoleSelectView(self.service, self.giveaway_id, self.owner_id, "required"),
            ephemeral=True,
        )

    @discord.ui.button(label="Cargos para mencionar", style=discord.ButtonStyle.secondary)
    async def mentions(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Selecione até 25 cargos que serão mencionados na publicação:",
            view=GiveawayRoleSelectView(self.service, self.giveaway_id, self.owner_id, "mention"),
            ephemeral=True,
        )


class GiveawayBonusRoleView(discord.ui.View):
    def __init__(self, service: GiveawayService, giveaway_id: str, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.giveaway_id = giveaway_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Somente quem abriu esta configuração pode usá-la.", ephemeral=True)
        return False

    @discord.ui.button(label="Adicionar/editar cargo", style=discord.ButtonStyle.success)
    async def add(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            GiveawayBonusEntriesModal(self.service, self.giveaway_id)
        )

    @discord.ui.button(label="Remover um cargo", style=discord.ButtonStyle.secondary)
    async def remove(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            GiveawayBonusRemoveModal(self.service, self.giveaway_id)
        )

    @discord.ui.button(label="Remover todos os bônus", style=discord.ButtonStyle.danger)
    async def clear(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await self.service.clear_bonus_roles(interaction.guild, self.giveaway_id)
        await interaction.response.send_message("Todos os bônus por cargo foram removidos.", ephemeral=True)


class GiveawayBonusEntriesModal(discord.ui.Modal, title="Entradas extras por cargo"):
    def __init__(self, service: GiveawayService, giveaway_id: str) -> None:
        super().__init__()
        self.service = service
        self.giveaway_id = giveaway_id
        self.role_id = discord.ui.TextInput(
            label="ID ou menção do cargo",
            placeholder="Ex.: 1529974341872128070 ou <@&...>",
            min_length=1,
            max_length=32,
        )
        self.extra_entries = discord.ui.TextInput(
            label="Entradas extras deste cargo",
            placeholder="Ex.: 2",
            default="1",
            min_length=1,
            max_length=3,
        )
        self.add_item(self.role_id)
        self.add_item(self.extra_entries)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        try:
            role_id = _parse_role_id(str(self.role_id))
            role = interaction.guild.get_role(role_id)
            if role is None:
                raise ValueError("Esse cargo não existe neste servidor.")
            amount = _bounded_int(str(self.extra_entries), 1, 100, "entradas extras")
            await self.service.set_bonus_roles(
                interaction.guild,
                self.giveaway_id,
                [role_id],
                amount,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Bônus individual configurado: <@&{role_id}> recebe **+{amount} entrada(s)**.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class GiveawayBonusRemoveModal(discord.ui.Modal, title="Remover bônus de um cargo"):
    def __init__(self, service: GiveawayService, giveaway_id: str) -> None:
        super().__init__()
        self.service = service
        self.giveaway_id = giveaway_id
        self.role_id = discord.ui.TextInput(
            label="ID ou menção do cargo",
            placeholder="Ex.: 1529974341872128070 ou <@&...>",
            min_length=1,
            max_length=32,
        )
        self.add_item(self.role_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        try:
            role_id = _parse_role_id(str(self.role_id))
            await self.service.remove_bonus_role(interaction.guild, self.giveaway_id, role_id)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"O bônus de <@&{role_id}> foi removido.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class GiveawayRoleSelectView(discord.ui.View):
    def __init__(
        self,
        service: GiveawayService,
        giveaway_id: str,
        owner_id: int,
        mode: str,
    ) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.giveaway_id = giveaway_id
        self.owner_id = owner_id
        self.mode = mode
        maximum = 5 if mode == "required" else 25
        select = discord.ui.RoleSelect(
            placeholder=f"Selecione de 1 a {maximum} cargos",
            min_values=1,
            max_values=maximum,
        )
        select.callback = self._selected
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Somente quem abriu esta seleção pode usá-la.", ephemeral=True)
            return False
        return True

    async def _selected(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        select = self.children[0]
        role_ids = [int(role.id) for role in select.values] if isinstance(select, discord.ui.RoleSelect) else []
        if self.mode == "required":
            await self.service.set_required_roles(interaction.guild, self.giveaway_id, role_ids)
        else:
            await self.service.set_mention_roles(interaction.guild, self.giveaway_id, role_ids)
        await interaction.response.send_message("Cargos atualizados.", ephemeral=True)

    @discord.ui.button(label="Remover todos", style=discord.ButtonStyle.danger)
    async def clear(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        if self.mode == "required":
            await self.service.set_required_roles(interaction.guild, self.giveaway_id, [])
        else:
            await self.service.set_mention_roles(interaction.guild, self.giveaway_id, [])
        await interaction.response.send_message("Todos os cargos desta configuração foram removidos.", ephemeral=True)


class GiveawayCreateModal(discord.ui.Modal, title="Novo sorteio"):
    def __init__(self, service: GiveawayService) -> None:
        super().__init__()
        self.service = service
        default_end = datetime.now(GIVEAWAY_TIMEZONE) + timedelta(days=7)
        self.prize = discord.ui.TextInput(label="Prêmio", max_length=256)
        self.details = discord.ui.TextInput(
            label="Detalhes do sorteio",
            style=discord.TextStyle.paragraph,
            placeholder="Explique prêmio, regras e observações.",
            required=False,
            max_length=4000,
        )
        self.image_url = discord.ui.TextInput(label="URL da imagem", required=False, max_length=1000)
        self.timing = discord.ui.TextInput(
            label="Data final | vencedores | dias mínimos",
            default=f"{default_end:%d/%m/%Y} | 1 | 0",
            placeholder="10/11/2026 | 1 | 0",
            max_length=60,
        )
        self.schedule = discord.ui.TextInput(
            label="Atraso min | recorrência horas",
            placeholder="0 | 0",
            default="0 | 0",
            required=False,
            max_length=40,
        )
        for item in (self.prize, self.details, self.image_url, self.timing, self.schedule):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        try:
            exact_ends_at, winners_count, minimum_days = _parse_giveaway_timing(str(self.timing.value))
            schedule = _split_ints(str(self.schedule.value), 2)
            payload = await self.service.create(
                interaction.guild,
                prize=str(self.prize.value),
                winners_count=winners_count,
                duration_minutes=1,
                required_role_id=None,
                minimum_server_days=minimum_days,
                start_delay_minutes=max(0, schedule[0]),
                recurrence_hours=max(0, schedule[1]),
                created_by=interaction.user.id,
                details=str(self.details.value or ""),
                image_url=str(self.image_url.value or ""),
                publish_immediately=False,
                exact_ends_at=exact_ends_at,
            )
        except (ValueError, discord.HTTPException) as exc:
            await interaction.response.send_message(f"Não consegui criar o sorteio: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"## Estúdio do Sorteio\nRascunho **{payload.get('prize')}** criado com ID `{payload.get('id')}`.\n"
            "Escolha o gerenciador completo **V1** ou **V2**, configure os cargos e publique somente quando estiver pronto.",
            view=GiveawayCreationStudioView(
                self.service,
                str(payload.get("id")),
                interaction.user.id,
            ),
            ephemeral=True,
        )


class GiveawayEditModal(discord.ui.Modal, title="Editar sorteio"):
    def __init__(self, service: GiveawayService, payload: dict[str, Any]) -> None:
        super().__init__()
        self.service = service
        self.giveaway_id = str(payload.get("id"))
        self.prize = discord.ui.TextInput(label="Prêmio", default=str(payload.get("prize") or "")[:256], max_length=256)
        self.details = discord.ui.TextInput(
            label="Detalhes",
            style=discord.TextStyle.paragraph,
            default=str(payload.get("details") or "")[:4000],
            required=False,
            max_length=4000,
        )
        self.image_url = discord.ui.TextInput(
            label="URL da imagem",
            default=str(payload.get("image_url") or "")[:1000],
            required=False,
            max_length=1000,
        )
        self.timing = discord.ui.TextInput(
            label="Data final | vencedores | dias mínimos",
            default=(
                f"{_datetime(payload.get('ends_at')).astimezone(GIVEAWAY_TIMEZONE):%d/%m/%Y %H:%M} | "
                f"{_int(payload.get('winners_count')) or 1} | "
                f"{_int(payload.get('minimum_server_days'))}"
            ),
            max_length=60,
        )
        self.schedule = discord.ui.TextInput(
            label="Atraso min | recorrência horas",
            default=f"0 | {_int(payload.get('recurrence_hours'))}",
            max_length=40,
        )
        for item in (self.prize, self.details, self.image_url, self.timing, self.schedule):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        try:
            exact_ends_at, winners_count, minimum_days = _parse_giveaway_timing(str(self.timing.value))
            schedule = _split_ints(str(self.schedule.value), 2)
            await self.service.update_details(
                interaction.guild,
                self.giveaway_id,
                prize=str(self.prize.value),
                details=str(self.details.value or ""),
                image_url=str(self.image_url.value or ""),
                duration_minutes=1,
                winners_count=winners_count,
                minimum_server_days=minimum_days,
                start_delay_minutes=max(0, schedule[0]),
                recurrence_hours=max(0, schedule[1]),
                exact_ends_at=exact_ends_at,
            )
        except (ValueError, discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            await interaction.response.send_message(f"Não consegui editar: {exc}", ephemeral=True)
            return
        await interaction.response.send_message("Sorteio atualizado.", ephemeral=True)


def panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Bella Giveaways",
        description=(
            "Crie rascunhos e gerencie cada sorteio pelo painel: conteúdo, imagem, "
            "containers V2, cargos, bônus de entradas, participantes, publicação, encerramento, reroll e reutilização."
        ),
        color=discord.Color.from_rgb(255, 80, 120),
    )
    embed.add_field(name="Requisitos", value="Cargo obrigatório e tempo mínimo no servidor.", inline=True)
    embed.add_field(name="Resultados", value="Múltiplos vencedores, participantes visíveis e reroll por sorteio.", inline=True)
    embed.add_field(name="Automação", value="Agendamento, recorrência e reutilização de edições antigas.", inline=True)
    embed.set_footer(text="As ações administrativas exigem autorização da Bella.")
    return embed


def _default_giveaway_v1_payload() -> dict[str, Any]:
    draft = EmbedDraft(
        title="🎉 {premio}",
        description="{detalhes}",
        color=0xFF5078,
        image_url="{imagem}",
        timestamp=True,
        footer="Sorteio {id} • Bella Giveaways",
        fields=[
            DraftField("Status", "{status}", True),
            DraftField("Participantes", "{participantes}", True),
            DraftField("Entradas válidas", "{entradas_totais}", True),
            DraftField("Mínimo necessário", "{minimo_participantes}", True),
            DraftField("Vencedores", "{vencedores}", True),
            DraftField("Término", "{termina}\n{termina_em}", True),
            DraftField("Resultado", "{resultado}", False),
            DraftField("Requisitos", "{requisitos}\n\n**Bônus por cargo**\n{bonus_cargos}", False),
        ],
    )
    return {"content": "", "embeds": [draft.to_dict()]}


def _giveaway_visual_mode(payload: dict[str, Any]) -> str:
    return "v1" if str(payload.get("visual_mode")).casefold() == "v1" else "v2"


def _replace_placeholders_in_data(value: Any, context: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _replace_giveaway_placeholders(value, context)
    if isinstance(value, list):
        return [_replace_placeholders_in_data(item, context) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_placeholders_in_data(item, context)
            for key, item in value.items()
        }
    return value


def _default_giveaway_containers() -> list[V2ContainerDraft]:
    return [
        V2ContainerDraft(
            title="🎉 {premio}",
            description="{detalhes}",
            color=0xFF5078,
            items=[
                V2ItemDraft(
                    kind="text",
                    content=(
                        "### Informações do sorteio\n"
                        "**Status:** {status}\n"
                        "**Vencedores:** {vencedores}\n"
                        "**Participantes:** {participantes}\n"
                        "**Entradas válidas:** {entradas_totais}\n"
                        "**Mínimo necessário:** {minimo_participantes}\n"
                        "**Término:** {termina} ({termina_em})"
                    ),
                ),
                V2ItemDraft(
                    kind="text",
                    content=(
                        "### Resultado\n{resultado}\n\n"
                        "### Requisitos\n{requisitos}\n\n### Entradas extras\n{bonus_cargos}"
                    ),
                ),
                V2ItemDraft(kind="media", url="{imagem}", content="Imagem do sorteio"),
                V2ItemDraft(kind="text", content="-# Sorteio `{id}` • Bella Giveaways"),
            ],
        )
    ]


def _protected_giveaway_containers(containers: list[V2ContainerDraft]) -> list[V2ContainerDraft]:
    cleaned: list[V2ContainerDraft] = []
    for source in containers[:MAX_GIVEAWAY_CONTAINERS]:
        container = V2ContainerDraft.from_dict(source.to_dict())
        container.items = [
            item
            for item in container.items
            if item.kind not in {"link_button", "interactive_button"}
        ][:20]
        cleaned.append(container)
    return cleaned or _default_giveaway_containers()


def _render_giveaway_containers(
    guild: discord.Guild,
    payload: dict[str, Any],
    participant_count: int,
    *,
    total_entries: int | None = None,
) -> list[V2ContainerDraft]:
    raw = payload.get("containers_v2")
    if isinstance(raw, list):
        drafts = [V2ContainerDraft.from_dict(item) for item in raw if isinstance(item, dict)]
    else:
        drafts = []
    drafts = _protected_giveaway_containers(drafts or _default_giveaway_containers())
    context = _giveaway_placeholder_context(
        guild,
        payload,
        participant_count,
        total_entries=total_entries,
    )
    rendered: list[V2ContainerDraft] = []
    for source in drafts:
        container = V2ContainerDraft.from_dict(source.to_dict())
        container.title = _replace_giveaway_placeholders(container.title, context)
        container.description = _replace_giveaway_placeholders(container.description, context)
        container.thumbnail_url = _replace_giveaway_placeholders(container.thumbnail_url, context)
        for item in container.items:
            item.content = _replace_giveaway_placeholders(item.content, context)
            item.url = _replace_giveaway_placeholders(item.url, context)
            item.label = _replace_giveaway_placeholders(item.label, context)
        rendered.append(container)
    return rendered


def _giveaway_placeholder_context(
    guild: discord.Guild,
    payload: dict[str, Any],
    participant_count: int,
    *,
    total_entries: int | None = None,
) -> dict[str, str]:
    ends_at = _datetime(payload.get("ends_at"))
    starts_at = _datetime(payload.get("starts_at"))
    required_roles = [guild.get_role(role_id) for role_id in _payload_role_ids(payload, "required_role_ids")]
    required_mentions = [role.mention for role in required_roles if role is not None]
    requirements: list[str] = []
    if required_mentions:
        requirements.append(f"Cargo (um deles): {' '.join(required_mentions)}")
    minimum_days = _int(payload.get("minimum_server_days"))
    if minimum_days:
        requirements.append(f"Tempo mínimo no servidor: **{minimum_days} dia(s)**")
    winner_ids = [_int(value) for value in payload.get("winner_ids", []) if _int(value)]
    minimum_participants = _minimum_giveaway_participants(payload)
    if payload.get("ended_reason") == "INSUFFICIENT_PARTICIPANTS":
        result = (
            f"Encerrado sem vencedores: **{participant_count}/{minimum_participants}** "
            "participantes registrados em relação ao mínimo exigido."
        )
    elif str(payload.get("status")) == "ENDED":
        result = "Sorteio concluído com resultado válido."
    else:
        result = f"O sorteio precisa de pelo menos **{minimum_participants} participantes** para gerar vencedores."
    status = {
        "DRAFT": "Rascunho",
        "PENDING": "Pendente",
        "SCHEDULED": "Agendado",
        "ACTIVE": "Ativo",
        "ENDED": "Encerrado",
        "CANCELLED": "Cancelado",
    }.get(str(payload.get("status")), str(payload.get("status") or "Indefinido"))
    return {
        "premio": str(payload.get("prize") or "Sorteio"),
        "detalhes": str(payload.get("details") or "Clique em **Participar** para entrar no sorteio."),
        "participantes": str(participant_count),
        "entradas_totais": str(total_entries if total_entries is not None else participant_count),
        "minimo_participantes": str(minimum_participants),
        "resultado": result,
        "vencedores": " ".join(f"<@{winner_id}>" for winner_id in winner_ids)
        or str(_int(payload.get("winners_count")) or 1),
        "termina": discord.utils.format_dt(ends_at, "F"),
        "termina_em": discord.utils.format_dt(ends_at, "R"),
        "inicio": discord.utils.format_dt(starts_at, "F"),
        "inicio_em": discord.utils.format_dt(starts_at, "R"),
        "requisitos": "\n".join(requirements) or "Sem requisitos adicionais.",
        "bonus_cargos": _bonus_roles_text(guild, payload),
        "status": status,
        "imagem": str(payload.get("image_url") or ""),
        "id": str(payload.get("id") or ""),
    }


def _replace_giveaway_placeholders(value: str, context: dict[str, str]) -> str:
    rendered = str(value or "")
    for key, replacement in context.items():
        rendered = rendered.replace("{" + key + "}", replacement)
    return rendered


def _bonus_roles_text(guild: discord.Guild, payload: dict[str, Any]) -> str:
    lines: list[str] = []
    for item in payload.get("bonus_roles", []):
        if not isinstance(item, dict):
            continue
        role_id = _int(item.get("role_id"))
        extra = _int(item.get("extra_entries"))
        if not role_id or extra <= 0:
            continue
        role = guild.get_role(role_id)
        lines.append(f"{role.mention if role is not None else f'<@&{role_id}>'}: **+{extra}**")
    return "\n".join(lines) or "Nenhum bônus por cargo configurado."


def _safe_giveaway_content(value: str) -> str:
    text = re.sub(r"@everyone", "@\u200beveryone", str(value or ""), flags=re.IGNORECASE)
    text = re.sub(r"@here", "@\u200bhere", text, flags=re.IGNORECASE)
    return text.strip()[:1800]


def _giveaway_announcement_content(payload: dict[str, Any]) -> str:
    parts = [
        " ".join(f"<@&{role_id}>" for role_id in _payload_role_ids(payload, "mention_role_ids")),
        _safe_giveaway_content(str(payload.get("message_content") or "")),
    ]
    return "\n".join(part for part in parts if part).strip()[:2000]


def _giveaway_allowed_mentions() -> discord.AllowedMentions:
    return discord.AllowedMentions(users=False, roles=True, everyone=False, replied_user=False)


def _weighted_sample_without_replacement(
    member_ids: list[int],
    weights: dict[int, int],
    count: int,
) -> list[int]:
    if count <= 0 or not member_ids:
        return []
    random_source = secrets.SystemRandom()
    ranked = sorted(
        member_ids,
        key=lambda member_id: random_source.random() ** (1 / max(1, weights.get(member_id, 1))),
        reverse=True,
    )
    return ranked[: min(count, len(ranked))]


def _split_ints(value: str, length: int) -> list[int]:
    parts = [part.strip() for part in str(value or "").split("|")]
    result: list[int] = []
    for index in range(length):
        raw = parts[index] if index < len(parts) else "0"
        if raw and not raw.isdigit():
            raise ValueError("Use apenas números separados por `|` nos campos de requisitos e agenda.")
        result.append(int(raw or 0))
    return result


def _parse_giveaway_timing(value: str) -> tuple[datetime, int, int]:
    parts = [part.strip() for part in str(value or "").split("|")]
    if len(parts) < 3:
        raise ValueError(
            "Use `data final | vencedores | dias mínimos`, por exemplo `10/11/2026 | 1 | 0`."
        )
    raw_date, raw_winners, raw_days = parts[:3]
    parsed_date: datetime | None = None
    for date_format in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            parsed_date = datetime.strptime(raw_date, date_format)
            if date_format == "%d/%m/%Y":
                parsed_date = parsed_date.replace(hour=23, minute=59, second=59)
            break
        except ValueError:
            continue
    if parsed_date is None:
        raise ValueError("Use a data como `10/11/2026` ou `10/11/2026 18:30`.")
    exact_ends_at = parsed_date.replace(tzinfo=GIVEAWAY_TIMEZONE).astimezone(timezone.utc)
    if exact_ends_at <= datetime.now(timezone.utc):
        raise ValueError("A data final precisa estar no futuro.")
    winners = _bounded_int(raw_winners, 1, 20, "vencedores")
    minimum_days = _bounded_int(raw_days, 0, 3650, "dias mínimos")
    return exact_ends_at, winners, minimum_days


def _bounded_int(value: Any, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"O campo {label} precisa ser um número inteiro.") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"O campo {label} deve ficar entre {minimum} e {maximum}.")
    return parsed


def _unique_ids(values: list[int]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        parsed = _int(value)
        if not parsed or parsed in seen:
            continue
        seen.add(parsed)
        result.append(parsed)
    return result


def _minimum_giveaway_participants(payload: dict[str, Any]) -> int:
    winners_count = max(1, min(20, _int(payload.get("winners_count")) or 1))
    configured = _int(payload.get("minimum_participants"))
    return max(winners_count * 4, configured)


def _payload_role_ids(payload: dict[str, Any], key: str) -> list[int]:
    raw = payload.get(key, [])
    values = _unique_ids(list(raw) if isinstance(raw, list) else [])
    if key == "required_role_ids" and not values:
        legacy = _int(payload.get("required_role_id"))
        if legacy:
            values.append(legacy)
    return values


def _optional_http_url(value: Any) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    parsed = urlparse(clean)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("A imagem precisa usar uma URL HTTP ou HTTPS válida.")
    return clean[:1000]


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_role_id(value: str) -> int:
    match = re.search(r"\d{15,22}", str(value or ""))
    if match is None:
        raise ValueError("Informe o ID do cargo ou cole a menção dele.")
    return int(match.group(0))


def _datetime(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
