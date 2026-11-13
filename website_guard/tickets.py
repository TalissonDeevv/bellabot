from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from unicodedata import combining, normalize
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from .maintenance_center import guard_public_interaction

from src.ai.groq_service import GroqService, MediaInput
from src.ai.official_knowledge import curated_entries
from src.ai.personality import BELLA_PERSONA_RUNTIME
from src.ai.personality_engine import CommunityPersonalityEngine

from .embed_builder import DraftField, EmbedDraft, V2ContainerDraft, V2ItemDraft
from .identity import OWNER_DISCORD_ID
from .quote_intake import IntakeState, QuoteIntake, is_quote_category
from .state import StateStore

LOGGER = logging.getLogger(__name__)
TICKET_OWNER_ID = OWNER_DISCORD_ID
TICKET_CONFIG_SECTION = "ticket_configs"
TICKET_RECORD_SECTION = "smart_tickets"
TICKET_METRICS_SECTION = "ticket_metrics"
ACTIVE_TICKET_STATUSES = {
    "open",
    "in_progress",
    "waiting_user",
    "escalated",
    "human_assumed",
}
SMART_TICKET_ACTIONS = {
    "smart_ticket_rename",
    "smart_ticket_status",
    "smart_ticket_priority",
    "smart_ticket_assign",
    "smart_ticket_escalate",
    "smart_ticket_close",
    "smart_ticket_reopen",
}
DEFAULT_TICKET_DOUBT_CHANNEL_ID = 1514971351427977316
LEGACY_TICKET_CLOSED_TITLE = "Atendimento fechado no momento"
PREVIOUS_TICKET_CLOSED_TITLE = (
    "<:Icon_Moderator_Alumni:1252078187849060453>  Atendimento fechado no momento"
)
DEFAULT_TICKET_CLOSED_TITLE = "Atendimento fechado no momento"
LEGACY_TICKET_CLOSED_TEMPLATE = (
    "{motivo}\n\nHorários de atendimento:\n{horarios}\n\n"
    "Se for uma dúvida geral, envie no canal {canal_duvidas}."
)
DEFAULT_TICKET_CLOSED_TEMPLATE = (
    "Nossa Central de Atendimento está fora do horário de funcionamento e, "
    "por isso, não está recebendo novos tickets. Para garantir que cada "
    "solicitação receba a atenção necessária, pedimos que retorne durante um "
    "dos horários abaixo.\n\n"
    "Horários de atendimento:\n{horarios}\n\n"
    "Se tiver alguma dúvida geral, você também pode utilizar o canal "
    "{canal_duvidas}, onde outros membros poderão ajudar."
)
LEGACY_TICKET_CLOSED_SCHEDULE = (
    "Segunda a sexta: 07:00 às 18:00\n"
    "Sábado: 07:00 às 12:00\n"
    "Domingo: fechado"
)
DEFAULT_TICKET_CLOSED_SCHEDULE = (
    "- Segunda a sexta: 07h às 18h\n"
    "- Sábado: 07h às 12h\n"
    "- Domingo: fechado"
)
DEFAULT_TICKET_OPENING_HOURS = {
    "0": [{"start": "07:00", "end": "18:00"}],
    "1": [{"start": "07:00", "end": "18:00"}],
    "2": [{"start": "07:00", "end": "18:00"}],
    "3": [{"start": "07:00", "end": "18:00"}],
    "4": [{"start": "07:00", "end": "18:00"}],
    "5": [{"start": "07:00", "end": "12:00"}],
    "6": [],
}

TICKET_CATEGORIES = (
    "Dúvida simples",
    "Suporte",
    "Feedback",
    "Sugestão",
    "Problema técnico",
    "Orçamento",
    "Parceria",
    "Comercial",
    "Reunião",
    "Solicitação ao Talisson",
    "Assunto administrativo",
    "Outro",
)

STAFF_CENTER_HELPER_ALLOWED_CATEGORIES = frozenset(
    {
        "duvida-simples",
        "suporte",
        "feedback",
        "sugestao",
        "problema-tecnico",
        "denuncia-de-membro",
        "solicitacao-geral",
        "outro",
    }
)
STAFF_CENTER_RESTRICTED_CATEGORIES = frozenset(
    {
        "orcamento",
        "financeiro",
        "comercial",
        "parceria",
        "reuniao",
        "solicitacao-ao-talisson",
        "assunto-administrativo",
    }
)


def ticket_category_key(value: str) -> str:
    normalized = _normalize(value or "")
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")


def helper_can_access_ticket_category(
    category: str,
    *,
    allowed: list[str] | tuple[str, ...] | set[str] | None = None,
    restricted: list[str] | tuple[str, ...] | set[str] | None = None,
) -> bool:
    key = ticket_category_key(category)
    denied = set(restricted or STAFF_CENTER_RESTRICTED_CATEGORIES)
    permitted = set(allowed or STAFF_CENTER_HELPER_ALLOWED_CATEGORIES)
    if not key or key in denied:
        return False
    return key in permitted


@dataclass
class TicketConfig:
    guild_id: int
    opening_channel_id: int
    category_id: int | None = None
    logs_channel_id: int | None = None
    support_role_id: int | None = None
    admin_role_id: int | None = None
    extra_staff_role_ids: list[int] = field(default_factory=list)
    staff_center_helper_role_id: int | None = None
    staff_center_allowed_category_keys: list[str] = field(
        default_factory=lambda: sorted(STAFF_CENTER_HELPER_ALLOWED_CATEGORIES)
    )
    staff_center_restricted_category_keys: list[str] = field(
        default_factory=lambda: sorted(STAFF_CENTER_RESTRICTED_CATEGORIES)
    )
    color: int = 0x176B87
    author_name: str = "Bella Intelligence Center"
    author_icon_url: str = ""
    title: str = "Central de Atendimento"
    description: str = (
        "Inicie um atendimento privado para receber orientação da Bella "
        "ou encaminhamento à equipe responsável."
    )
    fields: list[dict[str, Any]] = field(default_factory=list)
    footer: str = "Atendimento inteligente e confidencial"
    footer_icon_url: str = ""
    thumbnail_url: str = ""
    banner_url: str = ""
    banner_position: str = "end"
    timestamp: bool = True
    open_button_label: str = "Abrir atendimento"
    open_button_emoji: str = ""
    panel_mode: str = "select_only"
    panel_buttons: list[dict[str, str]] = field(
        default_factory=lambda: [
            {
                "label": "Abrir atendimento",
                "category": "Outro",
                "emoji": "",
                "style": "primary",
            }
        ]
    )
    close_button_label: str = "Encerrar"
    close_button_emoji: str = ""
    escalate_button_label: str = "Solicitar equipe"
    escalate_button_emoji: str = ""
    select_placeholder: str = "Selecione o assunto do atendimento"
    select_options: list[dict[str, str]] = field(
        default_factory=lambda: [
            {"label": value, "value": _slug(value), "description": "", "emoji": ""}
            for value in TICKET_CATEGORIES
        ]
    )
    opening_instruction: str = (
        "Receba o usuário de forma elegante e peça uma explicação objetiva "
        "sobre o que ele precisa."
    )
    closing_instruction: str = (
        "Encerre de forma natural, considerando se o assunto foi resolvido "
        "ou encaminhado à equipe."
    )
    panel_message_id: int | None = None
    panel_notice_message_id: int | None = None
    panel_render_mode: str = "v1"
    panel_v1_payload: dict[str, Any] = field(default_factory=dict)
    panel_v2_payload: dict[str, Any] = field(default_factory=dict)
    system_enabled: bool = True
    auto_attendance_enabled: bool = True
    closed_reason: str = "Central temporariamente em manutenção."
    closed_block_title: str = DEFAULT_TICKET_CLOSED_TITLE
    closed_block_template: str = DEFAULT_TICKET_CLOSED_TEMPLATE
    closed_schedule_text: str = DEFAULT_TICKET_CLOSED_SCHEDULE
    opening_hours_enabled: bool = True
    opening_hours_timezone: str = "America/Sao_Paulo"
    opening_hours: dict[str, list[dict[str, str]]] = field(
        default_factory=lambda: {
            key: [dict(item) for item in value]
            for key, value in DEFAULT_TICKET_OPENING_HOURS.items()
        }
    )
    fallback_doubt_channel_id: int = DEFAULT_TICKET_DOUBT_CHANNEL_ID

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TicketConfig":
        valid = {item.name for item in cls.__dataclass_fields__.values()}
        payload = {key: item for key, item in value.items() if key in valid}
        payload["guild_id"] = int(payload.get("guild_id", 0))
        payload["opening_channel_id"] = int(payload.get("opening_channel_id", 0))
        payload["color"] = _safe_color(payload.get("color", 0x176B87))
        payload["fields"] = list(payload.get("fields") or [])
        payload["panel_v1_payload"] = (
            dict(payload.get("panel_v1_payload") or {})
            if isinstance(payload.get("panel_v1_payload"), dict)
            else {}
        )
        payload["panel_v2_payload"] = (
            dict(payload.get("panel_v2_payload") or {})
            if isinstance(payload.get("panel_v2_payload"), dict)
            else {}
        )
        payload["extra_staff_role_ids"] = [
            int(role_id)
            for role_id in (payload.get("extra_staff_role_ids") or [])
            if str(role_id).isdigit()
        ]
        helper_role_id = payload.get("staff_center_helper_role_id")
        payload["staff_center_helper_role_id"] = (
            int(helper_role_id) if str(helper_role_id or "").isdigit() else None
        )
        payload["staff_center_allowed_category_keys"] = [
            ticket_category_key(str(item))
            for item in (payload.get("staff_center_allowed_category_keys") or [])
            if ticket_category_key(str(item))
        ] or sorted(STAFF_CENTER_HELPER_ALLOWED_CATEGORIES)
        payload["staff_center_restricted_category_keys"] = [
            ticket_category_key(str(item))
            for item in (payload.get("staff_center_restricted_category_keys") or [])
            if ticket_category_key(str(item))
        ] or sorted(STAFF_CENTER_RESTRICTED_CATEGORIES)
        payload["select_options"] = list(payload.get("select_options") or [])
        if "panel_buttons" not in payload:
            payload["panel_buttons"] = [
                {
                    "label": str(
                        payload.get("open_button_label", "Abrir atendimento")
                    ),
                    "category": "Outro",
                    "emoji": str(payload.get("open_button_emoji", "")),
                    "style": "primary",
                }
            ]
        else:
            payload["panel_buttons"] = list(payload.get("panel_buttons") or [])
        payload["panel_mode"] = _ticket_panel_mode(
            str(payload.get("panel_mode", "select_only"))
        )
        payload["panel_render_mode"] = _ticket_render_mode(
                str(payload.get("panel_render_mode", "v1"))
        )
        if payload.get("closed_block_title") in {
            LEGACY_TICKET_CLOSED_TITLE,
            PREVIOUS_TICKET_CLOSED_TITLE,
        }:
            payload["closed_block_title"] = DEFAULT_TICKET_CLOSED_TITLE
        if payload.get("closed_block_template") == LEGACY_TICKET_CLOSED_TEMPLATE:
            payload["closed_block_template"] = DEFAULT_TICKET_CLOSED_TEMPLATE
        if payload.get("closed_schedule_text") == LEGACY_TICKET_CLOSED_SCHEDULE:
            payload["closed_schedule_text"] = DEFAULT_TICKET_CLOSED_SCHEDULE
        payload["banner_position"] = _ticket_image_position(
            str(payload.get("banner_position", "end"))
        )
        raw_hours = payload.get("opening_hours")
        if not isinstance(raw_hours, dict):
            payload["opening_hours"] = {
                key: [dict(item) for item in value]
                for key, value in DEFAULT_TICKET_OPENING_HOURS.items()
            }
        else:
            normalized_hours: dict[str, list[dict[str, str]]] = {}
            for day, windows in raw_hours.items():
                if not isinstance(windows, list):
                    continue
                normalized_hours[str(day)] = [
                    {
                        "start": str(item.get("start", "")),
                        "end": str(item.get("end", "")),
                    }
                    for item in windows
                    if isinstance(item, dict)
                ]
            payload["opening_hours"] = normalized_hours
        try:
            payload["fallback_doubt_channel_id"] = int(
                payload.get("fallback_doubt_channel_id")
                or DEFAULT_TICKET_DOUBT_CHANNEL_ID
            )
        except (TypeError, ValueError):
            payload["fallback_doubt_channel_id"] = DEFAULT_TICKET_DOUBT_CHANNEL_ID
        return cls(**payload)

    def panel_embed(self, *, include_banner: bool = True, include_fields: bool = True, include_footer: bool = True) -> discord.Embed:
        embed = discord.Embed(
            title=self.title[:256] or None,
            description=self.description[:4096] or None,
            color=discord.Color(self.color),
            timestamp=datetime.now(timezone.utc) if self.timestamp else None,
        )
        if self.author_name:
            kwargs: dict[str, Any] = {"name": self.author_name[:256]}
            if _valid_url(self.author_icon_url):
                kwargs["icon_url"] = self.author_icon_url
            embed.set_author(**kwargs)
        if include_fields:
            for item in self.fields[:25]:
                embed.add_field(
                    name=str(item.get("name", ""))[:256] or "\u200b",
                    value=str(item.get("value", ""))[:1024] or "\u200b",
                    inline=bool(item.get("inline", False)),
                )
        if include_footer and self.footer:
            kwargs = {"text": self.footer[:2048]}
            if _valid_url(self.footer_icon_url):
                kwargs["icon_url"] = self.footer_icon_url
            embed.set_footer(**kwargs)
        if _valid_url(self.thumbnail_url):
            embed.set_thumbnail(url=self.thumbnail_url)
        if include_banner and _valid_url(self.banner_url):
            embed.set_image(url=self.banner_url)
        return embed

    def panel_embeds(self) -> list[discord.Embed]:
        raw_embeds = self.panel_v1_payload.get("embeds")
        if isinstance(raw_embeds, list) and raw_embeds:
            embeds: list[discord.Embed] = []
            for raw in raw_embeds[:10]:
                if not isinstance(raw, dict):
                    continue
                draft = EmbedDraft.from_dict(raw)
                draft.buttons = []
                embeds.extend(draft.to_embeds())
                if len(embeds) >= 10:
                    break
            if embeds:
                return embeds[:10]
        if not _valid_url(self.banner_url):
            return [self.panel_embed(include_banner=False)]
        position = _ticket_image_position(self.banner_position)
        if position == "top":
            return [_ticket_banner_embed(self), self.panel_embed(include_banner=False)]
        if position == "middle":
            if self.fields:
                return [
                    self.panel_embed(include_banner=False, include_fields=False, include_footer=False),
                    _ticket_banner_embed(self),
                    self.panel_embed(include_banner=False, include_fields=True, include_footer=True),
                ]
            return [self.panel_embed(include_banner=False), _ticket_banner_embed(self)]
        return [self.panel_embed(include_banner=True)]

    def panel_message_content(self) -> str:
        return str(self.panel_v1_payload.get("content") or "")[:2000].strip()

    def v2_containers(self) -> list[V2ContainerDraft]:
        raw_containers = self.panel_v2_payload.get("containers")
        if isinstance(raw_containers, list) and raw_containers:
            containers = [
                V2ContainerDraft.from_dict(item)
                for item in raw_containers[:5]
                if isinstance(item, dict)
            ]
            for container in containers:
                container.items = [
                    item
                    for item in container.items
                    if item.kind not in {"link_button", "interactive_button"}
                ]
            if containers:
                return containers
        items: list[V2ItemDraft] = []
        for item in self.fields[:10]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "-").strip() or "-"
            items.append(
                V2ItemDraft(
                    kind="text",
                    content=f"### {name}\n{value}" if name else value,
                )
            )
        if _valid_url(self.banner_url):
            media = V2ItemDraft(kind="media", url=self.banner_url)
            position = _ticket_image_position(self.banner_position)
            if position == "top":
                items.insert(0, media)
            elif position == "middle":
                items.insert(max(0, len(items) // 2), media)
            else:
                items.append(media)
        if self.footer:
            items.append(V2ItemDraft(kind="text", content=f"-# {self.footer}"))
        return [
            V2ContainerDraft(
                title=self.title,
                description=self.description,
                color=int(self.color),
                thumbnail_url=self.thumbnail_url,
                items=items,
            )
        ]

    def v2_message_content(self) -> str:
        return str(self.panel_v2_payload.get("content") or "")[:2000].strip()


@dataclass(frozen=True)
class TicketOrderOutcome:
    success: bool
    public_message: str
    audit_reason: str
    target: discord.Member | None = None
    roles: tuple[discord.Role, ...] = ()
    notes: str = ""


class TicketStore:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    async def config(self, guild_id: int) -> TicketConfig | None:
        values = await self.state.get_section(TICKET_CONFIG_SECTION)
        raw = values.get(str(guild_id))
        if not isinstance(raw, dict):
            return None
        try:
            config = TicketConfig.from_dict(raw)
        except (TypeError, ValueError):
            return None
        return config if config.opening_channel_id else None

    async def save_config(self, config: TicketConfig) -> None:
        await self.state.update_section(
            TICKET_CONFIG_SECTION,
            str(config.guild_id),
            config.to_dict(),
        )

    async def ticket(self, thread_id: int) -> dict[str, Any] | None:
        values = await self.state.get_section(TICKET_RECORD_SECTION)
        raw = values.get(str(thread_id))
        return dict(raw) if isinstance(raw, dict) else None

    async def save_ticket(self, thread_id: int, record: dict[str, Any]) -> None:
        await self.state.update_section(
            TICKET_RECORD_SECTION,
            str(thread_id),
            record,
        )

    async def open_for_user(
        self,
        guild_id: int,
        user_id: int,
    ) -> tuple[int, dict[str, Any]] | None:
        values = await self.state.get_section(TICKET_RECORD_SECTION)
        for thread_id, record in values.items():
            if not isinstance(record, dict):
                continue
            if (
                int(record.get("guild_id", 0)) == guild_id
                and int(record.get("creator_id", 0)) == user_id
                and record.get("status") in ACTIVE_TICKET_STATUSES
            ):
                try:
                    return int(thread_id), dict(record)
                except ValueError:
                    continue
        return None

    async def metrics(self, guild_id: int) -> dict[str, Any]:
        values = await self.state.get_section(TICKET_METRICS_SECTION)
        raw = values.get(str(guild_id))
        return dict(raw) if isinstance(raw, dict) else {}

    async def mutate_metrics(
        self,
        guild_id: int,
        *,
        opened: int = 0,
        closed: int = 0,
        ai_resolved: int = 0,
        escalated: int = 0,
        response_seconds: float | None = None,
        duration_seconds: float | None = None,
        category: str = "",
    ) -> dict[str, Any]:
        key = str(guild_id)

        def mutate(section: dict[str, Any]) -> None:
            metrics = section.setdefault(key, {})
            if not isinstance(metrics, dict):
                metrics = {}
                section[key] = metrics
            metrics["opened"] = int(metrics.get("opened", 0)) + opened
            metrics["closed"] = int(metrics.get("closed", 0)) + closed
            metrics["ai_resolved"] = int(metrics.get("ai_resolved", 0)) + ai_resolved
            metrics["escalated"] = int(metrics.get("escalated", 0)) + escalated
            if response_seconds is not None:
                total = float(metrics.get("response_total_seconds", 0))
                count = int(metrics.get("response_count", 0))
                metrics["response_total_seconds"] = total + max(0, response_seconds)
                metrics["response_count"] = count + 1
            if duration_seconds is not None:
                total = float(metrics.get("duration_total_seconds", 0))
                count = int(metrics.get("duration_count", 0))
                metrics["duration_total_seconds"] = total + max(0, duration_seconds)
                metrics["duration_count"] = count + 1
            if category:
                categories = metrics.setdefault("categories", {})
                if isinstance(categories, dict):
                    categories[category] = int(categories.get(category, 0)) + 1

        section = await self.state.mutate_section(TICKET_METRICS_SECTION, mutate)
        raw = section.get(key, {})
        return dict(raw) if isinstance(raw, dict) else {}


class SmartTicketManager:
    def __init__(
        self,
        bot: discord.Client,
        service: GroqService,
        state: StateStore,
    ) -> None:
        self.bot = bot
        self.service = service
        self.store = TicketStore(state)
        self.personality = CommunityPersonalityEngine()
        self.quote_intake = QuoteIntake(service)
        self._views_registered = False
        self._thread_locks: dict[int, Any] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._order_handler: Callable[[discord.Message], Awaitable[bool]] | None = None
        self._ticket_open_handler: Callable[..., Awaitable[Any]] | None = None
        self._ticket_close_handler: Callable[..., Awaitable[Any]] | None = None

    def set_order_handler(
        self,
        handler: Callable[[discord.Message], Awaitable[bool]],
    ) -> None:
        self._order_handler = handler

    def set_ticket_open_handler(
        self,
        handler: Callable[..., Awaitable[Any]],
    ) -> None:
        self._ticket_open_handler = handler

    def set_ticket_close_handler(
        self,
        handler: Callable[..., Awaitable[Any]],
    ) -> None:
        self._ticket_close_handler = handler

    def register_persistent_views(self) -> None:
        if self._views_registered or not hasattr(self.bot, "add_view"):
            return
        self.bot.add_view(TicketPanelPersistentView(self))
        self.bot.add_view(TicketControlView(self))
        self._views_registered = True

    async def refresh_public_panels(self) -> int:
        values = await self.store.state.get_section(TICKET_CONFIG_SECTION)
        refreshed = 0
        for raw in values.values():
            if not isinstance(raw, dict):
                continue
            try:
                config = TicketConfig.from_dict(raw)
            except (TypeError, ValueError):
                continue
            if not config.opening_channel_id or not config.panel_message_id:
                continue
            channel = self.bot.get_channel(config.opening_channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                message = await channel.fetch_message(config.panel_message_id)
                await send_or_edit_ticket_panel(self, config, channel, message)
                refreshed += 1
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
                TypeError,
                ValueError,
            ):
                continue
        return refreshed

    async def create_ticket_from_selection(
        self,
        interaction: discord.Interaction,
        selected_value: str,
    ) -> None:
        if await guard_public_interaction(interaction, "tickets"):
            return
        label = selected_value.replace("-", " ").title()
        if interaction.guild is not None:
            config = await self.store.config(interaction.guild.id)
            if config is not None:
                label = next(
                    (
                        str(option.get("label", label))
                        for option in config.select_options
                        if str(option.get("value", "")) == selected_value
                    ),
                    label,
                )
        await self.create_ticket(
            interaction,
            selected_category=label,
        )

    async def handle_message(self, message: discord.Message) -> bool:
        if (
            message.guild is None
            or not isinstance(message.channel, discord.Thread)
            or message.author.bot
        ):
            return False
        record = await self.store.ticket(message.channel.id)
        if not record:
            return False
        config = await self.store.config(message.guild.id)
        if config is None:
            return True
        if (
            self._order_handler is not None
            and _bot_invoked(message, self.bot.user)
            and await self._order_handler(message)
        ):
            return True
        if record.get("status") not in ACTIVE_TICKET_STATUSES:
            return False
        creator_id = int(record.get("creator_id", 0))
        is_creator = message.author.id == creator_id
        is_team_member = self.member_can_manage_ticket(message.author, config, record)
        if is_team_member and not is_creator:
            if _is_close_ticket_request(message.content):
                await self._close(
                    message.channel,
                    record,
                    config,
                    closed_by=message.author.id,
                    ai_resolved=False,
                )
                return True
            if not record.get("human_first_response_at"):
                record["human_first_response_at"] = datetime.now(timezone.utc).isoformat()
                record["human_first_responder"] = message.author.id
                await self.store.save_ticket(message.channel.id, record)
            if not record.get("human_assumed"):
                record.update(
                    {
                        "human_assumed": True,
                        "assigned_to": message.author.id,
                        "status": "human_assumed",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                await self.store.save_ticket(message.channel.id, record)
            return True
        if (
            is_creator
            and record.get("human_assumed")
            and int(record.get("assigned_to", 0) or 0) == creator_id
        ):
            record.update(
                {
                    "human_assumed": False,
                    "assigned_to": None,
                    "status": "in_progress",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            await self.store.save_ticket(message.channel.id, record)
        if record.get("human_assumed"):
            return True
        if is_creator and _quote_intake_active(record):
            await self._quote_turn(message, config)
            return True
        if not config.auto_attendance_enabled and not record.get("force_auto_attendance"):
            return True
        if not is_creator:
            return True
        lock = self._thread_locks.setdefault(message.channel.id, asyncio.Lock())
        async with lock:
            record = await self.store.ticket(message.channel.id)
            if not record or record.get("status") not in ACTIVE_TICKET_STATUSES:
                return True
            if int(record.get("last_processed_message_id", 0)) == message.id:
                return True
            record["last_processed_message_id"] = message.id
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
            await self.store.save_ticket(message.channel.id, record)
            async with message.channel.typing():
                await self._triage(message, record)
        return True

    async def create_ticket(
        self,
        interaction: discord.Interaction,
        *,
        selected_category: str = "Outro",
        opening_channel: discord.TextChannel | None = None,
        context_instruction: str = "",
        allow_outside_hours: bool = False,
        force_auto_attendance: bool = False,
    ) -> None:
        if await guard_public_interaction(interaction, "tickets"):
            return
        if interaction.guild is None or not isinstance(
            interaction.user,
            discord.Member,
        ):
            await _interaction_reply(
                interaction,
                "O atendimento precisa ser aberto dentro de um servidor.",
            )
            return
        config = await self.store.config(interaction.guild.id)
        if config is None:
            await _interaction_reply(
                interaction,
                "A central de atendimento ainda não foi configurada.",
            )
            return
        available, unavailable_message = _ticket_availability(config)
        outside_hours_override = bool(
            allow_outside_hours
            and config.system_enabled
            and config.opening_hours_enabled
        )
        if not available and not outside_hours_override:
            await _interaction_reply(interaction, unavailable_message)
            return
        existing = await self.store.open_for_user(
            interaction.guild.id,
            interaction.user.id,
        )
        if existing:
            thread = interaction.guild.get_thread(existing[0])
            if thread is not None:
                await _interaction_reply(
                    interaction,
                    f"Seu atendimento já está em andamento em {thread.mention}.",
                )
                return
        channel = opening_channel or interaction.guild.get_channel(config.opening_channel_id)
        if not isinstance(channel, discord.TextChannel):
            await _interaction_reply(
                interaction,
                "O canal de abertura configurado não está mais disponível.",
            )
            return
        bot_member = interaction.guild.me
        if bot_member is None:
            await _interaction_reply(interaction, "A Bella ainda está iniciando.")
            return
        permissions = channel.permissions_for(bot_member)
        if not permissions.create_private_threads or not permissions.send_messages_in_threads:
            await _interaction_reply(
                interaction,
                "A Bella precisa das permissões Criar Threads Privadas e "
                "Enviar Mensagens em Threads.",
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        suffix = random.randint(1000, 9999)
        thread = await channel.create_thread(
            name=f"ticket-{_slug(interaction.user.display_name)[:50]}-{suffix}",
            type=discord.ChannelType.private_thread,
            invitable=False,
            reason=f"Ticket aberto por {interaction.user} ({interaction.user.id})",
        )
        await thread.add_user(interaction.user)
        quote_ticket = is_quote_category(selected_category)
        if not quote_ticket:
            # Orçamento: a equipe só entra depois da entrevista, com o resumo pronto.
            await self._add_team_members(thread, config, selected_category)
        now = datetime.now(timezone.utc)
        record = {
            "guild_id": interaction.guild.id,
            "creator_id": interaction.user.id,
            "creator_name": interaction.user.display_name,
            "status": "open",
            "category": selected_category or "Outro",
            "priority": "Normal",
            "summary": "",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "first_response_at": None,
            "closed_at": None,
            "closed_by": None,
            "ai_resolved": False,
            "escalated": False,
            "human_assumed": False,
            "assigned_to": None,
            "triage_complete": False,
            "context_instruction": context_instruction.strip()[:2000],
            "opening_channel_id": channel.id,
            "force_auto_attendance": bool(force_auto_attendance),
        }
        intake_state = IntakeState(started_at=now.isoformat()) if quote_ticket else None
        if intake_state is not None:
            record["quote_intake"] = intake_state.to_dict()
        await self.store.save_ticket(thread.id, record)
        await self.store.mutate_metrics(interaction.guild.id, opened=1)
        welcome = (
            self.quote_intake.opening(interaction.user.display_name, intake_state)
            if intake_state is not None
            else self._welcome_fallback(interaction.user.display_name, selected_category)
        )
        embed = discord.Embed(
            title=f"Atendimento • {selected_category}",
            description=welcome,
            color=discord.Color(config.color),
            timestamp=now,
        )
        embed.set_footer(text="Conversa privada acompanhada pela Bella")
        welcome_message = await thread.send(
            content=interaction.user.mention,
            embed=embed,
            view=TicketControlView(self, config),
            allowed_mentions=discord.AllowedMentions(
                users=[interaction.user],
                roles=False,
                everyone=False,
            ),
        )
        task = None if intake_state is not None else asyncio.create_task(
            self._refine_welcome(
                welcome_message,
                interaction.user.display_name,
                selected_category,
                config,
                context_instruction=context_instruction,
            )
        )
        if task is not None:
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        if self._ticket_open_handler is not None:
            try:
                await self._ticket_open_handler(
                    guild=interaction.guild,
                    member=interaction.user,
                    thread=thread,
                    category=selected_category,
                )
            except Exception as exc:
                LOGGER.warning(
                    "Tarefa local de abertura de ticket falhou: %s: %s",
                    type(exc).__name__,
                    exc,
                )
        await interaction.followup.send(
            f"Atendimento criado com sucesso em {thread.mention}.",
            ephemeral=True,
        )

    async def close_ticket(
        self,
        interaction: discord.Interaction,
        *,
        ai_resolved: bool = False,
    ) -> None:
        thread, record, config = await self._interaction_ticket(interaction)
        if thread is None or record is None or config is None:
            return
        if not ai_resolved and not self.member_can_manage_ticket(interaction.user, config, record):
            await _interaction_reply(
                interaction,
                "Somente a equipe configurada pode encerrar este atendimento.",
            )
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        await self._close(
            thread,
            record,
            config,
            closed_by=interaction.user.id,
            ai_resolved=ai_resolved,
        )
        await interaction.followup.send(
            "Atendimento encerrado e transcript enviado aos logs.",
            ephemeral=True,
        )

    async def reopen_ticket(self, interaction: discord.Interaction) -> None:
        thread, record, config = await self._interaction_ticket(interaction)
        if thread is None or record is None or config is None:
            return
        if not self.member_can_manage_ticket(interaction.user, config, record):
            await _interaction_reply(
                interaction,
                "Somente a equipe configurada pode reabrir este atendimento.",
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await thread.edit(archived=False, locked=False, reason="Ticket reaberto")
        record.update(
            {
                "status": "open",
                "closed_at": None,
                "closed_by": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        await self.store.save_ticket(thread.id, record)
        await thread.send(
            "O atendimento foi reaberto pela equipe.",
            view=TicketControlView(self, config),
        )
        await interaction.followup.send("Atendimento reaberto.", ephemeral=True)

    async def escalate_ticket(
        self,
        interaction: discord.Interaction,
    ) -> None:
        thread, record, config = await self._interaction_ticket(interaction)
        if thread is None or record is None or config is None:
            return
        is_team = self.member_can_manage_ticket(interaction.user, config, record)
        if _quote_intake_active(record) and not is_team:
            await interaction.response.send_message(
                "Assim que você terminar as perguntas do orçamento eu já chamo a equipe "
                "com o resumo pronto. Falta pouco! ♡",
                ephemeral=True,
            )
            return
        if _quote_intake_active(record):
            # Alguém da equipe pediu: encerra a entrevista e chama todo mundo.
            record.setdefault("quote_intake", {})["finished"] = True
            await self._add_team_members(thread, config, str(record.get("category", "")))
        if not is_team and not _record_has_minimum_context(record):
            await interaction.response.send_message(
                (
                    "Antes de encaminhar você para nossa equipe, preciso entender "
                    "o motivo do contato. Explique o que você precisa ou qual "
                    "problema está enfrentando."
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._escalate(thread, record, config, requested=True)
        await interaction.followup.send(
            "A equipe responsável foi acionada.",
            ephemeral=True,
        )

    async def metrics_embed(self, guild_id: int) -> discord.Embed:
        metrics = await self.store.metrics(guild_id)
        responses = int(metrics.get("response_count", 0))
        durations = int(metrics.get("duration_count", 0))
        average_response = (
            float(metrics.get("response_total_seconds", 0)) / responses
            if responses
            else 0
        )
        average_duration = (
            float(metrics.get("duration_total_seconds", 0)) / durations
            if durations
            else 0
        )
        categories = metrics.get("categories", {})
        most_common = "Sem dados"
        if isinstance(categories, dict) and categories:
            most_common = Counter(categories).most_common(1)[0][0]
        embed = discord.Embed(
            title="Métricas da Central de Atendimento",
            color=discord.Color(0x176B87),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Abertos", value=str(metrics.get("opened", 0)))
        embed.add_field(name="Fechados", value=str(metrics.get("closed", 0)))
        embed.add_field(
            name="Resolvidos pela Bella",
            value=str(metrics.get("ai_resolved", 0)),
        )
        embed.add_field(
            name="Encaminhados",
            value=str(metrics.get("escalated", 0)),
        )
        embed.add_field(
            name="Resposta média",
            value=_duration(average_response),
        )
        embed.add_field(
            name="Atendimento médio",
            value=_duration(average_duration),
        )
        embed.add_field(name="Categoria recorrente", value=most_common, inline=False)
        return embed

    async def update_ticket_metadata(
        self,
        interaction: discord.Interaction,
        *,
        field_name: str,
        value: str,
    ) -> None:
        thread, record, config = await self._interaction_ticket(interaction)
        if thread is None or record is None or config is None:
            return
        if not self.member_can_manage_ticket(interaction.user, config, record):
            await _interaction_reply(
                interaction,
                "Somente a equipe configurada pode alterar este atendimento.",
            )
            return
        record[field_name] = value
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        await self.store.save_ticket(thread.id, record)
        label = "Status" if field_name == "status" else "Prioridade"
        await _interaction_reply(
            interaction,
            f"{label} atualizado para **{value}**.",
        )

    @staticmethod
    def member_is_team(
        member: discord.abc.User,
        config: TicketConfig,
    ) -> bool:
        if member.id == TICKET_OWNER_ID:
            return True
        roles = getattr(member, "roles", None)
        if roles is None:
            return False
        role_ids = {role.id for role in roles}
        role_names = {_normalize(str(getattr(role, "name", ""))) for role in roles}
        allowed = {
            value
            for value in (config.support_role_id, config.admin_role_id)
            if value
        }
        allowed.update(int(role_id) for role_id in getattr(config, "extra_staff_role_ids", []) if role_id)
        if role_ids & allowed:
            return True
        staff_names = {
            "suporte",
            "administracao",
            "administração",
            "diretoria executiva",
            "diretoria",
            "proprietario",
            "proprietário",
            "staff",
        }
        return bool(role_names & {_normalize(name) for name in staff_names})

    @classmethod
    def member_can_manage_ticket(
        cls,
        member: discord.abc.User,
        config: TicketConfig,
        record: dict[str, Any],
    ) -> bool:
        helper_role_id = getattr(config, "staff_center_helper_role_id", None)
        roles = getattr(member, "roles", None)
        role_ids = {role.id for role in roles} if roles is not None else set()
        if helper_role_id and int(helper_role_id) in role_ids:
            return helper_can_access_ticket_category(
                str(record.get("category") or ""),
                allowed=getattr(config, "staff_center_allowed_category_keys", None),
                restricted=getattr(config, "staff_center_restricted_category_keys", None),
            )
        return cls.member_is_team(member, config)

    async def execute_order(
        self,
        message: discord.Message,
        request: Any,
    ) -> TicketOrderOutcome:
        if (
            message.guild is None
            or not isinstance(message.channel, discord.Thread)
            or not isinstance(message.author, discord.Member)
        ):
            return TicketOrderOutcome(
                success=False,
                public_message="Esta ordem precisa ser executada dentro de um ticket.",
                audit_reason="A ação de ticket foi solicitada fora de uma thread.",
            )
        record = await self.store.ticket(message.channel.id)
        config = await self.store.config(message.guild.id)
        if record is None or config is None:
            return TicketOrderOutcome(
                success=False,
                public_message="Esta thread não está registrada como atendimento.",
                audit_reason="Thread sem registro de ticket ou configuração ativa.",
            )
        if not self.member_can_manage_ticket(message.author, config, record):
            return TicketOrderOutcome(
                success=False,
                public_message="Somente a equipe pode administrar este atendimento.",
                audit_reason="Usuário sem permissão para administrar o ticket.",
            )
        action = str(getattr(request, "action", ""))
        now = datetime.now(timezone.utc)
        reason = (
            "Ordem administrativa em linguagem natural por "
            f"{message.author} ({message.author.id})"
        )

        if action == "smart_ticket_rename":
            name = _clean_ticket_name(str(getattr(request, "name", "")))
            if not name:
                return self._order_failure(
                    message,
                    "Informe o novo nome do ticket.",
                    "Nome de ticket ausente ou inválido.",
                )
            previous_name = message.channel.name
            await message.channel.edit(name=name, reason=reason)
            record.update(
                {
                    "display_name": name,
                    "updated_at": now.isoformat(),
                    "updated_by": message.author.id,
                }
            )
            await self.store.save_ticket(message.channel.id, record)
            return self._order_success(
                message,
                f"Pronto. Este ticket agora se chama **{name}**.",
                "Ticket renomeado com sucesso.",
                notes=f"Ticket: {previous_name} ({message.channel.id}) -> {name}.",
            )

        if action == "smart_ticket_status":
            value = _clean_ticket_label(
                str(getattr(request, "status_value", ""))
                or str(getattr(request, "name", ""))
            )
            if not value:
                return self._order_failure(
                    message,
                    "Informe o novo status do ticket.",
                    "Status de ticket ausente ou inválido.",
                )
            canonical = _canonical_ticket_status(value)
            if canonical is not None:
                record["status"] = canonical
            record.update(
                {
                    "workflow_status": value,
                    "updated_at": now.isoformat(),
                    "updated_by": message.author.id,
                }
            )
            await self.store.save_ticket(message.channel.id, record)
            return self._order_success(
                message,
                f"Pronto. O status deste ticket agora é **{value}**.",
                "Status do ticket atualizado com sucesso.",
                notes=f"Ticket: {message.channel.id}; status: {value}.",
            )

        if action == "smart_ticket_priority":
            value = _clean_ticket_label(
                str(getattr(request, "priority_value", ""))
                or str(getattr(request, "name", ""))
            )
            if not value:
                return self._order_failure(
                    message,
                    "Informe a nova prioridade do ticket.",
                    "Prioridade de ticket ausente ou inválida.",
                )
            record.update(
                {
                    "priority": value,
                    "updated_at": now.isoformat(),
                    "updated_by": message.author.id,
                }
            )
            await self.store.save_ticket(message.channel.id, record)
            return self._order_success(
                message,
                f"Pronto. A prioridade deste ticket agora é **{value}**.",
                "Prioridade do ticket atualizada com sucesso.",
                notes=f"Ticket: {message.channel.id}; prioridade: {value}.",
            )

        if action == "smart_ticket_assign":
            target_id = getattr(request, "target_id", None)
            target = (
                message.guild.get_member(int(target_id))
                if target_id is not None
                else message.author
            )
            if target is None or not self.member_can_manage_ticket(target, config, record):
                return self._order_failure(
                    message,
                    "O responsável precisa ser um membro da equipe configurada.",
                    "Responsável ausente ou sem permissão de equipe.",
                )
            record.update(
                {
                    "human_assumed": True,
                    "assigned_to": target.id,
                    "status": "human_assumed",
                    "workflow_status": "Em atendimento",
                    "updated_at": now.isoformat(),
                    "updated_by": message.author.id,
                }
            )
            await self.store.save_ticket(message.channel.id, record)
            return self._order_success(
                message,
                f"Pronto. Este ticket foi atribuído a {target.mention}.",
                "Ticket atribuído com sucesso.",
                target=target,
                notes=f"Ticket: {message.channel.id}; responsável: {target.id}.",
            )

        if action == "smart_ticket_escalate":
            await self._escalate(
                message.channel,
                record,
                config,
                requested=True,
            )
            return self._order_success(
                message,
                "Pronto. O atendimento foi encaminhado à equipe responsável.",
                "Ticket encaminhado com sucesso.",
                notes=f"Ticket: {message.channel.id}.",
            )

        if action == "smart_ticket_close":
            await self._close(
                message.channel,
                record,
                config,
                closed_by=message.author.id,
                ai_resolved=False,
            )
            return self._order_success(
                message,
                "Atendimento encerrado e histórico registrado.",
                "Ticket encerrado com sucesso.",
                notes=f"Ticket: {message.channel.id}.",
            )

        if action == "smart_ticket_reopen":
            await message.channel.edit(
                archived=False,
                locked=False,
                reason=reason,
            )
            record.update(
                {
                    "status": "open",
                    "workflow_status": "Aberto",
                    "closed_at": None,
                    "closed_by": None,
                    "human_assumed": False,
                    "assigned_to": None,
                    "updated_at": now.isoformat(),
                    "updated_by": message.author.id,
                }
            )
            await self.store.save_ticket(message.channel.id, record)
            return self._order_success(
                message,
                "Pronto. O atendimento foi reaberto.",
                "Ticket reaberto com sucesso.",
                notes=f"Ticket: {message.channel.id}.",
            )

        return self._order_failure(
            message,
            "A ordem de ticket foi reconhecida, mas ainda não possui executor.",
            f"Ação de ticket sem executor: {action}.",
        )

    @staticmethod
    def _order_success(
        message: discord.Message,
        public_message: str,
        reason: str,
        *,
        target: discord.Member | None = None,
        notes: str = "",
    ) -> TicketOrderOutcome:
        return TicketOrderOutcome(
            success=True,
            public_message=public_message,
            audit_reason=reason,
            target=target,
            notes=notes,
        )

    @staticmethod
    def _order_failure(
        message: discord.Message,
        public_message: str,
        reason: str,
    ) -> TicketOrderOutcome:
        del message
        return TicketOrderOutcome(
            success=False,
            public_message=public_message,
            audit_reason=reason,
        )

    async def _triage(
        self,
        message: discord.Message,
        record: dict[str, Any],
    ) -> None:
        config = await self.store.config(message.guild.id)
        if config is None:
            return
        started = time.perf_counter()
        message_text, media = await self._message_payload(message)
        history: list[dict[str, str]] = []
        data = self.fast_ticket_decision(
            message_text,
            display_name=message.author.display_name,
            current_category=str(record.get("category", "Outro")),
        )
        if data is None:
            history = await self._history(
                message.channel,
                limit=12,
                exclude_message_id=message.id,
            )
            data = await self._ticket_analysis(
                message_text,
                history[-12:],
                record,
                media=media,
            )
        category = str(data.get("category", record.get("category", "Outro")))
        priority = str(data.get("priority", "Normal"))
        summary = str(data.get("summary", ""))[:1000]
        action = str(data.get("action", "ask"))
        reply = str(data.get("reply", "")).strip()
        confidence = _score(data.get("resolution_confidence"))
        if not reply:
            reply = self._natural_clarification(message.author.display_name)
        style = self.personality.analyze(
            message_text,
            recent_context=history,
            humor_allowed=False,
            context_hint="ticket",
        )
        reply = self.personality.adapt(
            reply,
            user_message=message_text,
            style=style,
        )

        first_response = record.get("first_response_at")
        now = datetime.now(timezone.utc)
        record.update(
            {
                "category": category,
                "priority": priority,
                "summary": summary,
                "updated_at": now.isoformat(),
                "triage_complete": bool(
                    record.get("triage_complete")
                    or _message_has_useful_context(message_text)
                ),
            }
        )
        if not first_response:
            record["first_response_at"] = now.isoformat()
        await self.store.save_ticket(message.channel.id, record)
        await self.store.mutate_metrics(
            message.guild.id,
            response_seconds=time.perf_counter() - started,
            category=category if not first_response else "",
        )

        if action == "escalate":
            if not _message_has_useful_context(message_text) and not _record_has_minimum_context(record):
                await message.reply(
                    (
                        "Antes de encaminhar você para nossa equipe, preciso "
                        "entender melhor o motivo do contato. O que exatamente "
                        "aconteceu ou qual resultado você espera?"
                    ),
                    mention_author=False,
                )
                return
            await message.reply(reply, mention_author=False)
            await self._escalate(message.channel, record, config)
            return
        await message.reply(reply, mention_author=False)
        if (
            action == "resolve"
            and confidence >= 0.90
            and bool(data.get("user_satisfied"))
        ):
            await self._close(
                message.channel,
                record,
                config,
                closed_by=self.bot.user.id if self.bot.user else 0,
                ai_resolved=True,
            )

    async def _ticket_analysis(
        self,
        message: str,
        history: list[dict[str, str]],
        record: dict[str, Any],
        *,
        media: list[MediaInput] | None = None,
    ) -> dict[str, Any]:
        if not self.service.available:
            return self._ticket_fallback(message, record)
        resources = [asdict(item) for item in curated_entries(message)]
        style = self.personality.analyze(
            message,
            recent_context=history,
            humor_allowed=False,
            context_hint="ticket",
        )
        prompt = (
            "Atue como primeiro nível de atendimento da Bella. Seja direto e use "
            "no máximo três frases curtas. Não repita saudações, não ecoe a mensagem "
            "do usuário e não faça duas perguntas na mesma resposta. Nunca mencione "
            "IA, prompt, sistema interno ou falta de contexto. Se faltar informação, "
            "faça somente a pergunta objetiva necessária. "
            "Use action=answer quando puder orientar, ask quando precisar esclarecer, "
            "resolve apenas quando o usuário confirmar satisfação e o caso estiver "
            "concluído, ou escalate para orçamento, proposta, reunião, negociação, "
            "contratação, projeto personalizado, qualquer pedido para falar com "
            "Talisson ou "
            "caso que exija intervenção humana. Não invente fatos nem alegue sucesso "
            "sem evidência. Se houver recursos oficiais relevantes, responda primeiro "
            "e recomende no máximo três, de forma natural.\n\n"
            f"Persona oficial da Bella:\n{BELLA_PERSONA_RUNTIME}\n\n"
            f"Camada local de personalidade: {style.instruction}\n"
            f"Contexto específico deste atendimento: "
            f"{str(record.get('context_instruction') or 'Atendimento geral.')}\n"
            f"Categorias permitidas: {json.dumps(TICKET_CATEGORIES, ensure_ascii=False)}\n"
            f"Registro atual: {json.dumps(record, ensure_ascii=False)}\n"
            f"Histórico: {json.dumps(history, ensure_ascii=False)}\n"
            f"Mensagem atual: {message}\n"
            f"Recursos oficiais: {json.dumps(resources, ensure_ascii=False)}"
        )
        try:
            return await self.service.generate_json(
                prompt,
                _ticket_schema(),
                max_output_tokens=380,
                timeout_seconds=6,
                media=media,
            )
        except Exception as exc:
            LOGGER.warning("Triagem por IA indisponível: %s: %s", type(exc).__name__, exc)
            return self._ticket_fallback(message, record)

    async def _refine_welcome(
        self,
        target_message: discord.Message,
        display_name: str,
        category: str,
        config: TicketConfig,
        *,
        context_instruction: str = "",
    ) -> None:
        style = self.personality.analyze(
            f"Abertura de ticket: {category}",
            recent_context=[],
            humor_allowed=False,
            context_hint="ticket",
        )
        prompt = (
            "Gere uma recepção exclusiva para um ticket da Bella em português. "
            "Use no máximo duas frases curtas, com tom elegante, natural e "
            "prestativo. Não diga que é IA, sistema automatizado ou chatbot. Não use "
            "emoji. Peça ao usuário para explicar o necessário sem parecer template.\n\n"
            f"Persona oficial da Bella:\n{BELLA_PERSONA_RUNTIME}\n\n"
            f"Camada local de personalidade: {style.instruction}\n"
            f"Usuário: {display_name}\nCategoria inicial: {category}\n"
            f"Contexto específico: {context_instruction.strip() or 'Atendimento geral.'}\n"
            f"Orientação configurada: {config.opening_instruction}"
        )
        refined = await self._dynamic_text(
            prompt,
            "",
            max_tokens=110,
        )
        if not refined or not target_message.embeds:
            return
        refined = self.personality.adapt(
            refined,
            user_message=f"Abertura de ticket: {category}",
            style=style,
        )
        embed = target_message.embeds[0].copy()
        embed.description = refined[:1000]
        try:
            await target_message.edit(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Não foi possível atualizar a recepção do ticket")

    async def _closing(
        self,
        record: dict[str, Any],
        history: list[dict[str, str]],
        config: TicketConfig,
    ) -> str:
        style = self.personality.analyze(
            "Encerramento de ticket",
            recent_context=history,
            humor_allowed=False,
            context_hint="ticket",
        )
        prompt = (
            "Gere uma despedida contextual e exclusiva para o ticket. Considere se "
            "foi resolvido pela Bella ou encaminhado à equipe. Use duas ou três "
            "frases, tom elegante e natural, sem emoji, sem mencionar IA ou "
            "automação.\n\n"
            f"Persona oficial da Bella:\n{BELLA_PERSONA_RUNTIME}\n\n"
            f"Camada local de personalidade: {style.instruction}\n"
            f"Registro: {json.dumps(record, ensure_ascii=False)}\n"
            f"Histórico: {json.dumps(history[-20:], ensure_ascii=False)}\n"
            f"Orientação configurada: {config.closing_instruction}"
        )
        fallback = "Fechamos por aqui e o histórico ficou salvo. Se precisar de novo, pode chamar."
        if record.get("escalated"):
            fallback = (
                "Seu pedido foi encaminhado pra equipe. Eles continuam por aqui "
                "usando o histórico deste atendimento."
            )
        result = await self._dynamic_text(prompt, fallback, max_tokens=180)
        return self.personality.adapt(
            result,
            user_message="Encerramento de ticket",
            style=style,
        )

    async def _dynamic_text(
        self,
        prompt: str,
        fallback: str,
        *,
        max_tokens: int,
    ) -> str:
        if self.service.available:
            try:
                text = await self.service.generate_text(
                    prompt,
                    max_output_tokens=max_tokens,
                    timeout_seconds=5,
                )
                if text.strip():
                    return text.strip()[:1500]
            except Exception as exc:
                LOGGER.warning(
                    "Texto dinâmico de ticket indisponível: %s: %s",
                    type(exc).__name__,
                    exc,
                )
        return fallback

    async def _quote_turn(self, message: discord.Message, config: TicketConfig) -> None:
        """Uma resposta do cliente na entrevista de orçamento."""
        lock = self._thread_locks.setdefault(message.channel.id, asyncio.Lock())
        async with lock:
            record = await self.store.ticket(message.channel.id)
            if not record or not _quote_intake_active(record):
                return
            if int(record.get("last_processed_message_id", 0)) == message.id:
                return
            state = IntakeState.from_dict(record.get("quote_intake"))
            text = message.content or ""
            if message.attachments:
                text += "\n" + "\n".join(f"(anexo: {a.filename} {a.url})" for a in message.attachments[:5])
            async with message.channel.typing():
                result = await self.quote_intake.handle(text, state)
            now = datetime.now(timezone.utc)
            record["quote_intake"] = state.to_dict()
            record["last_processed_message_id"] = message.id
            record["updated_at"] = now.isoformat()
            if not record.get("first_response_at"):
                record["first_response_at"] = now.isoformat()
            await self.store.save_ticket(message.channel.id, record)
            if result.reply:
                for part in _split_text(result.reply, 1900):
                    await message.channel.send(part, allowed_mentions=discord.AllowedMentions.none())
            if result.finished:
                await self._finish_quote(message.channel, record, config, state, message.author.display_name)

    async def _finish_quote(
        self,
        thread: discord.Thread,
        record: dict[str, Any],
        config: TicketConfig,
        state: IntakeState,
        client_name: str,
    ) -> None:
        summary = await self.quote_intake.summary(state, client_name)
        document = self.quote_intake.answers_document(state, client_name)
        embed = discord.Embed(
            title=f"Resumo administrativo • {str(summary.get('titulo', 'Orçamento'))[:200]}",
            description=str(summary.get("resumo", ""))[:3500],
            color=discord.Color(config.color),
            timestamp=datetime.now(timezone.utc),
        )
        for name, key in (("Escopo", "escopo"), ("Orçamento e prazo", "orcamento_prazo"),
                          ("Técnico", "tecnico"), ("Complexidade", "complexidade")):
            if summary.get(key):
                embed.add_field(name=name, value=str(summary[key])[:1024], inline=key == "complexidade")
        for name, key in (("Pontos de atenção", "pontos_de_atencao"), ("Próximos passos", "proximos_passos")):
            items = [str(item) for item in summary.get(key) or [] if str(item).strip()]
            if items:
                embed.add_field(name=name, value="\n".join(f"• {item}" for item in items)[:1024], inline=False)
        if state.client_questions:
            embed.add_field(
                name="Dúvidas do cliente",
                value="\n".join(f"• {item}" for item in state.client_questions)[:1024],
                inline=False,
            )
        embed.set_footer(text="Respostas completas no arquivo anexado")
        file = discord.File(io.BytesIO(document.encode("utf-8")), filename="respostas-orcamento.md")
        await thread.send(embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none())
        record["summary"] = str(summary.get("resumo", ""))[:1000]
        record["quote_summary"] = summary
        record["triage_complete"] = True
        await self.store.save_ticket(thread.id, record)
        await self._add_team_members(thread, config, str(record.get("category", "")))
        await self._escalate(thread, record, config, requested=True)

    async def _escalate(
        self,
        thread: discord.Thread,
        record: dict[str, Any],
        config: TicketConfig,
        *,
        requested: bool = False,
    ) -> None:
        if record.get("escalated") and not requested:
            return
        record.update(
            {
                "status": "escalated",
                "escalated": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        await self.store.save_ticket(thread.id, record)
        await self.store.mutate_metrics(thread.guild.id, escalated=1)
        mentions = [f"<@{TICKET_OWNER_ID}>"]
        for role_id in (config.support_role_id, config.admin_role_id, *getattr(config, "extra_staff_role_ids", [])):
            if role_id:
                mentions.append(f"<@&{role_id}>")
        embed = discord.Embed(
            title="Atendimento encaminhado",
            description=(
                "Este assunto exige análise direta da equipe responsável. "
                "O histórico e o resumo executivo já estão disponíveis nesta thread."
            ),
            color=discord.Color(config.color),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Assunto",
            value=str(record.get("category", "Outro"))[:1024],
        )
        embed.add_field(
            name="Prioridade",
            value=str(record.get("priority", "Normal"))[:1024],
        )
        embed.add_field(
            name="Status",
            value="Aguardando equipe",
        )
        embed.add_field(
            name="Resumo executivo",
            value=str(record.get("summary", "Em análise."))[:1024],
            inline=False,
        )
        await thread.send(
            content=" ".join(dict.fromkeys(mentions)),
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=True,
                everyone=False,
            ),
        )

    async def _close(
        self,
        thread: discord.Thread,
        record: dict[str, Any],
        config: TicketConfig,
        *,
        closed_by: int,
        ai_resolved: bool,
    ) -> None:
        if record.get("status") == "closed":
            return
        history = await self._history(thread, limit=500, include_bot=True)
        now = datetime.now(timezone.utc)
        created_at = _datetime(record.get("created_at")) or now
        record.update(
            {
                "status": "closed",
                "closed_at": now.isoformat(),
                "closed_by": closed_by,
                "ai_resolved": ai_resolved,
                "updated_at": now.isoformat(),
            }
        )
        farewell = await self._closing(record, history, config)
        await thread.send(farewell)
        await self.store.save_ticket(thread.id, record)
        await self.store.mutate_metrics(
            thread.guild.id,
            closed=1,
            ai_resolved=1 if ai_resolved else 0,
            duration_seconds=(now - created_at).total_seconds(),
        )
        await self._send_transcript(thread, record, config, history)
        if self._ticket_close_handler is not None:
            try:
                await self._ticket_close_handler(
                    guild=thread.guild,
                    user_id=int(record.get("creator_id") or 0),
                    ticket_id=thread.id,
                )
            except Exception:
                LOGGER.exception("Falha ao executar integração pós-fechamento do ticket %s", thread.id)
        try:
            await thread.edit(
                archived=True,
                locked=True,
                reason=f"Ticket encerrado por {closed_by}",
            )
        except discord.HTTPException:
            LOGGER.exception("Não foi possível arquivar a thread %s", thread.id)

    async def _send_transcript(
        self,
        thread: discord.Thread,
        record: dict[str, Any],
        config: TicketConfig,
        history: list[dict[str, str]],
    ) -> None:
        channel = thread.guild.get_channel(config.logs_channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            return
        created_at = _datetime(record.get("created_at"))
        closed_at = _datetime(record.get("closed_at"))
        participants = sorted(
            {
                item.get("author", "Desconhecido")
                for item in history
                if item.get("author")
            }
        )
        lines = [
            "BELLA INTELLIGENCE CENTER — TRANSCRIPT",
            f"Servidor: {thread.guild.name} ({thread.guild.id})",
            f"Thread: {thread.name} ({thread.id})",
            f"Participantes: {', '.join(participants) or 'Não identificado'}",
            f"Abertura: {_format_datetime(created_at)}",
            f"Fechamento: {_format_datetime(closed_at)}",
            f"Duração: {_duration((closed_at - created_at).total_seconds()) if created_at and closed_at else 'Não calculada'}",
            f"Categoria: {record.get('category', 'Outro')}",
            f"Prioridade: {record.get('priority', 'Normal')}",
            f"Resumo IA: {record.get('summary', 'Não disponível')}",
            "",
            "CONVERSA COMPLETA",
        ]
        for item in history:
            lines.append(
                f"[{item.get('timestamp', '')}] {item.get('author', 'Usuário')}: "
                f"{item.get('content', '')}"
            )
        payload = "\n".join(lines).encode("utf-8")
        file = discord.File(
            io.BytesIO(payload),
            filename=f"transcript-{thread.id}.txt",
        )
        embed = discord.Embed(
            title="Atendimento encerrado",
            color=discord.Color(config.color),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Usuário",
            value=f"<@{record.get('creator_id', 0)}>",
        )
        embed.add_field(
            name="Categoria",
            value=str(record.get("category", "Outro"))[:1024],
        )
        embed.add_field(
            name="Destino",
            value="Equipe" if record.get("escalated") else "Resolvido pela Bella",
        )
        embed.add_field(
            name="Resumo executivo",
            value=str(record.get("summary", "Não disponível"))[:1024],
            inline=False,
        )
        await channel.send(embed=embed, file=file)

    async def _history(
        self,
        thread: discord.Thread,
        *,
        limit: int,
        include_bot: bool = True,
        exclude_message_id: int | None = None,
    ) -> list[dict[str, str]]:
        values: list[dict[str, str]] = []
        try:
            async for item in thread.history(limit=limit, oldest_first=True):
                if exclude_message_id is not None and item.id == exclude_message_id:
                    continue
                if not include_bot and item.author.bot:
                    continue
                content = item.content.strip() or _attachment_summary(item)
                if not content:
                    continue
                values.append(
                    {
                        "author": item.author.display_name,
                        "content": content[:700],
                        "timestamp": item.created_at.isoformat(),
                    }
                )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Não foi possível ler o histórico da thread %s", thread.id)
        return values

    async def _message_payload(
        self,
        message: discord.Message,
    ) -> tuple[str, list[MediaInput]]:
        parts = [message.content.strip()] if message.content.strip() else []
        media: list[MediaInput] = []
        total_bytes = 0
        for attachment in message.attachments[:4]:
            mime_type = (
                attachment.content_type
                or _attachment_mime_type(attachment.filename)
            )
            parts.append(
                f"Anexo: {attachment.filename} ({mime_type or 'tipo desconhecido'})"
            )
            if attachment.size > 20 * 1024 * 1024:
                parts.append("O arquivo excede o limite de leitura de 20 MB.")
                continue
            if mime_type.startswith("image/"):
                media.append(
                    MediaInput(
                        data=b"",
                        mime_type=mime_type,
                        url=str(attachment.url),
                    )
                )
                continue
            if total_bytes + attachment.size > 12 * 1024 * 1024:
                break
            try:
                data = await attachment.read(use_cached=True)
            except (discord.Forbidden, discord.HTTPException):
                continue
            total_bytes += len(data)
            if mime_type.startswith("text/") or mime_type in {
                "application/json",
                "application/xml",
            }:
                parts.append(data.decode("utf-8", errors="replace")[:6000])
        return "\n".join(parts).strip() or "Arquivo enviado pelo usuário.", media

    async def _interaction_ticket(
        self,
        interaction: discord.Interaction,
    ) -> tuple[
        discord.Thread | None,
        dict[str, Any] | None,
        TicketConfig | None,
    ]:
        if interaction.guild is None or not isinstance(
            interaction.channel,
            discord.Thread,
        ):
            await _interaction_reply(
                interaction,
                "Este controle só funciona dentro de um atendimento.",
            )
            return None, None, None
        record = await self.store.ticket(interaction.channel.id)
        config = await self.store.config(interaction.guild.id)
        if record is None or config is None:
            await _interaction_reply(
                interaction,
                "Este atendimento não possui uma configuração ativa.",
            )
            return None, None, None
        return interaction.channel, record, config

    async def _add_team_members(
        self,
        thread: discord.Thread,
        config: TicketConfig,
        category: str,
    ) -> None:
        role_ids = {
            item
            for item in (config.support_role_id, config.admin_role_id)
            if item
        }
        helper_role_id = getattr(config, "staff_center_helper_role_id", None)
        if helper_role_id and helper_can_access_ticket_category(
            category,
            allowed=getattr(config, "staff_center_allowed_category_keys", None),
            restricted=getattr(config, "staff_center_restricted_category_keys", None),
        ):
            role_ids.add(int(helper_role_id))
        added = 0
        for member in thread.guild.members:
            if added >= 40:
                break
            if member.bot or not role_ids.intersection(
                role.id for role in member.roles
            ):
                continue
            try:
                await thread.add_user(member)
                added += 1
            except (discord.Forbidden, discord.HTTPException):
                continue

    @staticmethod
    def _welcome_fallback(display_name: str, category: str) -> str:
        options = (
            f"Bem-vindo, {display_name}. Vou acompanhar seu atendimento sobre "
            f"{category.lower()}; conte o que aconteceu e seguimos a partir daí.",
            f"Olá, {display_name}. Este atendimento já está reservado para você. "
            "Explique sua necessidade com os detalhes que considerar importantes.",
            f"Obrigado por iniciar o atendimento, {display_name}. Descreva o que "
            "você precisa e vou organizar a melhor orientação possível.",
        )
        return random.choice(options)

    @staticmethod
    def fast_ticket_decision(
        message: str,
        *,
        display_name: str,
        current_category: str,
    ) -> dict[str, Any] | None:
        text = _normalize(message)
        compact = text.strip(" ?!.,")
        if compact in {
            "oi",
            "ola",
            "opa",
            "bom dia",
            "boa tarde",
            "boa noite",
        }:
            return _ticket_result(
                category=current_category,
                action="answer",
                summary="Usuário iniciou o atendimento.",
                reply="Olá. Pode me contar, de forma direta, o que você precisa?",
            )
        if compact in {
            "tudo bem",
            "tudo bom",
            "tudo bao",
            "como voce esta",
            "como vai",
        }:
            return _ticket_result(
                category=current_category,
                action="answer",
                summary="Usuário iniciou uma conversa cordial.",
                reply="Tudo certo. Como posso ajudar neste atendimento?",
            )
        if _requests_human_attention(text) and not _message_has_useful_context(message):
            return _ticket_result(
                category=current_category,
                action="ask",
                summary=f"{display_name} pediu atendimento humano sem descrever o motivo.",
                reply=(
                    "Antes de encaminhar você para nossa equipe, preciso entender "
                    "o motivo do contato. Pode me explicar o que você precisa?"
                ),
            )
        if _requests_talisson(text):
            return _ticket_result(
                category="Solicitação ao Talisson",
                action="escalate",
                priority="Média",
                summary=(
                    f"{display_name} solicitou atendimento direto com Talisson."
                ),
                reply=(
                    "Entendi. Vou encaminhar este atendimento ao Talisson com o "
                    "histórico da conversa."
                ),
            )
        if _is_satisfied(text):
            return _ticket_result(
                category=current_category,
                action="resolve",
                summary=f"{display_name} confirmou que a solicitação foi atendida.",
                reply=(
                    "Perfeito. Vou encerrar este atendimento e registrar o histórico."
                ),
                confidence=1.0,
                user_satisfied=True,
            )
        escalation_category = _commercial_category(text)
        if escalation_category:
            return _ticket_result(
                category=escalation_category,
                action="escalate",
                priority="Média",
                summary=(
                    f"{display_name} apresentou uma solicitação de "
                    f"{escalation_category.lower()}."
                ),
                reply=(
                    "Entendi a solicitação. Este assunto precisa da equipe "
                    "responsável, então vou encaminhá-lo agora."
                ),
            )
        if _is_technical_issue(text):
            return _ticket_result(
                category="Problema técnico",
                action="ask",
                priority="Média",
                summary=f"{display_name} relatou um problema técnico.",
                reply=(
                    "Envie o erro exibido, a página afetada e, se possível, uma "
                    "captura de tela. Com isso eu consigo iniciar o diagnóstico."
                ),
            )
        return None

    def _ticket_fallback(
        self,
        message: str,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        fast = self.fast_ticket_decision(
            message,
            display_name=str(record.get("creator_name", "Usuário")),
            current_category=str(record.get("category", "Outro")),
        )
        if fast:
            return fast
        return _ticket_result(
            category=str(record.get("category", "Outro")),
            action="ask",
            summary=message[:500],
            reply=(
                "Para eu direcionar corretamente, descreva em uma frase o problema "
                "ou o resultado que você deseja."
            ),
            confidence=0,
        )

    @staticmethod
    def _natural_clarification(display_name: str) -> str:
        prefix = f"{display_name}, " if display_name else ""
        options = (
            f"{prefix}quero entender corretamente antes de avançar. Qual resultado "
            "você espera obter com esta solicitação?",
            f"{prefix}pode detalhar o ponto principal e o que já tentou? Assim eu "
            "consigo orientar o próximo passo sem presumir informações.",
            f"{prefix}faltou apenas um detalhe para eu seguir com segurança: o que "
            "exatamente precisa ser resolvido neste atendimento?",
        )
        return random.choice(options)


class TicketPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        manager: SmartTicketManager,
        config: TicketConfig | None = None,
        *,
        preview: bool = False,
        notice: str | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.manager = manager
        selected = config or TicketConfig(guild_id=0, opening_channel_id=0)
        self.config = selected
        available, unavailable_message = _ticket_availability(selected)
        closed = preview or not available
        message_content = selected.v2_message_content()
        if message_content:
            self.add_item(discord.ui.TextDisplay(message_content))
        for draft in selected.v2_containers():
            self.add_item(draft.to_container(preview=False))
        mode = _ticket_panel_mode(selected.panel_mode)
        if mode in {"select_only", "button_and_select"}:
            select = discord.ui.Select(
                custom_id="jarvis:ticket:subject",
                placeholder=(
                    selected.select_placeholder[:150]
                    or "Selecione o assunto do atendimento"
                ),
                min_values=1,
                max_values=1,
                options=_ticket_select_options(selected),
                disabled=closed,
            )

            async def select_callback(
                interaction: discord.Interaction,
            ) -> None:
                await self.manager.create_ticket_from_selection(
                    interaction,
                    select.values[0],
                )

            select.callback = select_callback
            self.add_item(discord.ui.ActionRow(select))
        if mode in {"button_only", "button_and_select"}:
            configured_buttons = _ticket_panel_buttons(selected)
            for row_start in range(0, len(configured_buttons), 5):
                row_buttons: list[discord.ui.Button[Any]] = []
                for index, item in enumerate(
                    configured_buttons[row_start : row_start + 5],
                    start=row_start,
                ):
                    category = item["category"]
                    button_kwargs: dict[str, Any] = {
                        "label": item["label"],
                        "style": _ticket_button_style(item["style"]),
                        "custom_id": (
                            "jarvis:ticket:open"
                            if index == 0
                            else f"jarvis:ticket:open:{index}"
                        ),
                        "disabled": closed,
                    }
                    if item["emoji"]:
                        button_kwargs["emoji"] = item["emoji"]
                    try:
                        button = discord.ui.Button(**button_kwargs)
                    except (TypeError, ValueError):
                        button_kwargs.pop("emoji", None)
                        button = discord.ui.Button(**button_kwargs)

                    async def button_callback(
                        interaction: discord.Interaction,
                        selected_category: str = category,
                    ) -> None:
                        await self.manager.create_ticket(
                            interaction,
                            selected_category=selected_category,
                        )

                    button.callback = button_callback
                    row_buttons.append(button)
                if row_buttons:
                    self.add_item(discord.ui.ActionRow(*row_buttons))


class TicketPanelPersistentView(discord.ui.View):
    def __init__(self, manager: SmartTicketManager) -> None:
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(
        label="Abrir atendimento",
        style=discord.ButtonStyle.primary,
        custom_id="jarvis:ticket:open",
        row=0,
    )
    async def open_ticket(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.manager.create_ticket(interaction)

    @discord.ui.select(
        placeholder="Selecione o assunto",
        options=[discord.SelectOption(label="Outro", value="outro")],
        custom_id="jarvis:ticket:subject",
        min_values=1,
        max_values=1,
        row=1,
    )
    async def subject(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select,
    ) -> None:
        await self.manager.create_ticket_from_selection(
            interaction,
            select.values[0],
        )


class TicketControlView(discord.ui.View):
    def __init__(
        self,
        manager: SmartTicketManager,
        config: TicketConfig | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.manager = manager
        if config:
            self.close.label = config.close_button_label[:80]
            self.escalate.label = config.escalate_button_label[:80]
            if config.close_button_emoji:
                self.close.emoji = config.close_button_emoji
            if config.escalate_button_emoji:
                self.escalate.emoji = config.escalate_button_emoji

    @discord.ui.button(
        label="Encerrar",
        style=discord.ButtonStyle.secondary,
        custom_id="jarvis:ticket:close",
    )
    async def close(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.manager.close_ticket(interaction)

    @discord.ui.button(
        label="Solicitar equipe",
        style=discord.ButtonStyle.primary,
        custom_id="jarvis:ticket:escalate",
    )
    async def escalate(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.manager.escalate_ticket(interaction)


class TicketSetupSession:
    def __init__(
        self,
        manager: SmartTicketManager,
        owner_id: int,
        config: TicketConfig,
    ) -> None:
        self.manager = manager
        self.owner_id = owner_id
        self.config = config

    async def save(self) -> None:
        await self.manager.store.save_config(self.config)

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Editor da Central de Atendimento",
            description=(
                "Configure o painel por partes. Cada controle abaixo abre somente "
                "o editor daquela área, e todas as alterações são salvas "
                "automaticamente."
            ),
            color=discord.Color(self.config.color),
        )
        embed.add_field(
            name="Identidade visual",
            value=(
                f"**Título:** {self.config.title or 'Sem título'}\n"
                f"**Cor:** `#{self.config.color:06X}`\n"
                f"**Banner:** {'Configurado' if self.config.banner_url else 'Não definido'}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Abertura de tickets",
            value=(
                f"**Modo:** {_ticket_panel_mode_label(self.config.panel_mode)}\n"
                f"**Botões:** {len(_ticket_panel_buttons(self.config))}\n"
                f"**Opções no menu:** {len(self.config.select_options[:25])}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Estrutura e destino",
            value=(
                f"**Campos informativos:** {len(self.config.fields[:25])}\n"
                f"**Canal público:** <#{self.config.opening_channel_id}>\n"
                f"**Logs:** "
                f"{f'<#{self.config.logs_channel_id}>' if self.config.logs_channel_id else 'Não definido'}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Fluxo recomendado",
            value=(
                "Edite o visual, organize os campos e gerencie botões ou opções "
                "individualmente. Depois use **Prévia** e finalize em "
                "**Publicar painel**."
            ),
            inline=False,
        )
        embed.set_footer(text="Alterações salvas automaticamente • Publicação manual")
        return embed

    def buttons_embed(self, selected_index: int | None = None) -> discord.Embed:
        buttons = _ticket_panel_buttons(self.config)
        embed = discord.Embed(
            title="Botões de abertura",
            description=(
                "Cada botão pode abrir um assunto diferente. Selecione um item "
                "para editar ou remover, ou crie um novo botão."
            ),
            color=discord.Color(self.config.color),
        )
        lines = []
        for index, item in enumerate(buttons):
            marker = "→" if selected_index == index else "•"
            emoji = f"{item['emoji']} " if item["emoji"] else ""
            lines.append(
                f"{marker} **{emoji}{item['label']}** — {item['category']} "
                f"(`{_ticket_button_style_label(item['style'])}`)"
            )
        embed.add_field(
            name=f"Configurados ({len(buttons)}/20)",
            value="\n".join(lines)[:4000] or "Nenhum botão configurado.",
            inline=False,
        )
        embed.set_footer(text="Selecione um botão antes de editar ou remover")
        return embed

    def menu_embed(self, selected_index: int | None = None) -> discord.Embed:
        embed = discord.Embed(
            title="Menu de assuntos",
            description=(
                "Gerencie as opções exibidas no menu público. Cada opção abre "
                "o ticket já identificado com o assunto escolhido."
            ),
            color=discord.Color(self.config.color),
        )
        embed.add_field(
            name="Texto do menu",
            value=self.config.select_placeholder or "Sem texto definido",
            inline=False,
        )
        lines = []
        for index, item in enumerate(self.config.select_options[:25]):
            marker = "→" if selected_index == index else "•"
            emoji = f"{item.get('emoji', '')} " if item.get("emoji") else ""
            description = str(item.get("description", "")).strip()
            suffix = f" — {description}" if description else ""
            lines.append(
                f"{marker} **{emoji}{item.get('label', 'Outro')}**{suffix}"
            )
        embed.add_field(
            name=f"Opções ({len(self.config.select_options[:25])}/25)",
            value="\n".join(lines)[:4000] or "Nenhuma opção configurada.",
            inline=False,
        )
        embed.set_footer(text="Selecione uma opção antes de editar ou remover")
        return embed

    def fields_embed(self, selected_index: int | None = None) -> discord.Embed:
        embed = discord.Embed(
            title="Campos informativos",
            description=(
                "Campos são blocos exibidos no painel, como horários, orientações "
                "ou links úteis."
            ),
            color=discord.Color(self.config.color),
        )
        lines = []
        for index, item in enumerate(self.config.fields[:25]):
            marker = "→" if selected_index == index else "•"
            name = str(item.get("name", "")).strip() or "Sem título"
            layout = "lado a lado" if item.get("inline") else "linha inteira"
            lines.append(f"{marker} **{name}** — {layout}")
        embed.add_field(
            name=f"Campos ({len(self.config.fields[:25])}/25)",
            value="\n".join(lines)[:4000] or "Nenhum campo configurado.",
            inline=False,
        )
        embed.set_footer(text="Selecione um campo antes de editar ou remover")
        return embed


class _TicketSetupOwnedView(discord.ui.View):
    def __init__(self, session: TicketSetupSession) -> None:
        super().__init__(timeout=900)
        self.session = session

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.session.owner_id:
            return True
        await interaction.response.send_message(
            "Somente quem abriu o configurador pode usar estes controles.",
            ephemeral=True,
        )
        return False


class TicketSetupView(_TicketSetupOwnedView):
    def __init__(self, session: TicketSetupSession) -> None:
        super().__init__(session)
        self.panel_mode.label = (
            f"Modo: {_ticket_panel_mode_label(session.config.panel_mode)}"
        )[:80]

    @discord.ui.button(label="Conteúdo", style=discord.ButtonStyle.secondary, row=0)
    async def content(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await interaction.response.send_modal(TicketContentModal(self.session))

    @discord.ui.button(label="Aparência", style=discord.ButtonStyle.secondary, row=0)
    async def appearance(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await interaction.response.send_modal(TicketAppearanceModal(self.session))

    @discord.ui.button(label="Mídia", style=discord.ButtonStyle.secondary, row=0)
    async def media(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await interaction.response.send_modal(TicketMediaModal(self.session))

    @discord.ui.button(label="Campos", style=discord.ButtonStyle.secondary, row=1)
    async def fields(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await interaction.response.edit_message(
            embed=self.session.fields_embed(),
            view=TicketFieldManagerView(self.session),
        )

    @discord.ui.button(
        label="Botões de abertura",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def opening_buttons(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await interaction.response.edit_message(
            embed=self.session.buttons_embed(),
            view=TicketButtonManagerView(self.session),
        )

    @discord.ui.button(
        label="Menu de assuntos",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def select_menu(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await interaction.response.edit_message(
            embed=self.session.menu_embed(),
            view=TicketMenuManagerView(self.session),
        )

    @discord.ui.button(
        label="Modo: Somente menu",
        style=discord.ButtonStyle.primary,
        row=1,
    )
    async def panel_mode(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        modes = ("select_only", "button_only", "button_and_select")
        current = _ticket_panel_mode(self.session.config.panel_mode)
        index = (modes.index(current) + 1) % len(modes)
        next_mode = modes[index]
        if next_mode == "button_only" and not _ticket_panel_buttons(
            self.session.config
        ):
            await interaction.response.send_message(
                "Adicione pelo menos um botão de abertura antes de usar esse modo.",
                ephemeral=True,
            )
            return
        self.session.config.panel_mode = next_mode
        await self.session.save()
        await interaction.response.edit_message(
            embed=self.session.embed(),
            view=TicketSetupView(self.session),
        )

    @discord.ui.button(
        label="Mensagens do atendimento",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def messages(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await interaction.response.send_modal(TicketMessagesModal(self.session))

    @discord.ui.button(
        label="Botões internos",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def internal_buttons(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await interaction.response.send_modal(
            TicketInternalButtonsModal(self.session)
        )

    @discord.ui.button(label="Prévia", style=discord.ButtonStyle.secondary, row=2)
    async def preview(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        kwargs = ticket_panel_message_kwargs(
            self.session.manager,
            self.session.config,
            preview=True,
        )
        try:
            await interaction.response.send_message(**kwargs, ephemeral=True)
        except (discord.HTTPException, TypeError, ValueError):
            await interaction.response.send_message(
                **ticket_panel_message_kwargs(
                    self.session.manager,
                    self.session.config,
                    preview=True,
                    force_render_mode="v1",
                ),
                ephemeral=True,
            )

    @discord.ui.button(label="Publicar painel", style=discord.ButtonStyle.success, row=3)
    async def publish(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        channel = (
            interaction.guild.get_channel(self.session.config.opening_channel_id)
            if interaction.guild
            else None
        )
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "O canal de abertura não está disponível.",
                ephemeral=True,
            )
            return
        if (
            _ticket_panel_mode(self.session.config.panel_mode) == "button_only"
            and not _ticket_panel_buttons(self.session.config)
        ):
            await interaction.response.send_message(
                "Adicione pelo menos um botão antes de publicar no modo Somente botão.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        message: discord.Message | None = None
        if self.session.config.panel_message_id:
            try:
                message = await channel.fetch_message(
                    self.session.config.panel_message_id
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        message = await send_or_edit_ticket_panel(
            self.session.manager,
            self.session.config,
            channel,
            message,
        )
        await interaction.followup.send(
            f"Painel publicado em {channel.mention}.",
            ephemeral=True,
        )


class _TicketButtonSelect(discord.ui.Select):
    def __init__(
        self,
        session: TicketSetupSession,
        selected_index: int | None,
    ) -> None:
        self.session = session
        buttons = _ticket_panel_buttons(session.config)
        options = []
        for index, item in enumerate(buttons):
            kwargs: dict[str, Any] = {
                "label": item["label"][:100],
                "value": str(index),
                "description": f"Abre como: {item['category']}"[:100],
                "default": index == selected_index,
            }
            if item["emoji"]:
                kwargs["emoji"] = item["emoji"]
            try:
                options.append(discord.SelectOption(**kwargs))
            except (TypeError, ValueError):
                kwargs.pop("emoji", None)
                options.append(discord.SelectOption(**kwargs))
        if not options:
            options = [
                discord.SelectOption(
                    label="Nenhum botão configurado",
                    value="none",
                )
            ]
        super().__init__(
            placeholder="Selecione um botão para editar",
            min_values=1,
            max_values=1,
            options=options,
            disabled=not buttons,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = int(self.values[0])
        await interaction.response.edit_message(
            embed=self.session.buttons_embed(selected),
            view=TicketButtonManagerView(self.session, selected),
        )


class TicketButtonManagerView(_TicketSetupOwnedView):
    def __init__(
        self,
        session: TicketSetupSession,
        selected_index: int | None = None,
    ) -> None:
        super().__init__(session)
        buttons = _ticket_panel_buttons(session.config)
        self.selected_index = (
            selected_index
            if selected_index is not None and 0 <= selected_index < len(buttons)
            else None
        )
        self.add_item(_TicketButtonSelect(session, self.selected_index))

    @discord.ui.button(label="Adicionar botão", style=discord.ButtonStyle.success, row=1)
    async def add_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        if len(_ticket_panel_buttons(self.session.config)) >= 20:
            await interaction.response.send_message(
                "O painel já atingiu o limite de 20 botões.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            TicketOpeningButtonModal(self.session)
        )

    @discord.ui.button(label="Editar selecionado", style=discord.ButtonStyle.primary, row=1)
    async def edit_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        if self.selected_index is None:
            await interaction.response.send_message(
                "Selecione um botão no menu acima.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            TicketOpeningButtonModal(
                self.session,
                index=self.selected_index,
            )
        )

    @discord.ui.button(label="Remover selecionado", style=discord.ButtonStyle.danger, row=1)
    async def remove_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        if self.selected_index is None:
            await interaction.response.send_message(
                "Selecione um botão no menu acima.",
                ephemeral=True,
            )
            return
        self.session.config.panel_buttons.pop(self.selected_index)
        if (
            not self.session.config.panel_buttons
            and _ticket_panel_mode(self.session.config.panel_mode) == "button_only"
        ):
            self.session.config.panel_mode = "select_only"
        await self.session.save()
        await interaction.response.edit_message(
            embed=self.session.buttons_embed(),
            view=TicketButtonManagerView(self.session),
        )

    @discord.ui.button(label="Voltar ao editor", style=discord.ButtonStyle.secondary, row=2)
    async def back(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await interaction.response.edit_message(
            embed=self.session.embed(),
            view=TicketSetupView(self.session),
        )


class _TicketMenuOptionSelect(discord.ui.Select):
    def __init__(
        self,
        session: TicketSetupSession,
        selected_index: int | None,
    ) -> None:
        self.session = session
        items = session.config.select_options[:25]
        options = []
        for index, item in enumerate(items):
            kwargs: dict[str, Any] = {
                "label": str(item.get("label", "Outro"))[:100] or "Outro",
                "value": str(index),
                "description": (
                    str(item.get("description", "")).strip()[:100] or None
                ),
                "default": index == selected_index,
            }
            emoji = str(item.get("emoji", "")).strip()
            if emoji:
                kwargs["emoji"] = emoji
            try:
                options.append(discord.SelectOption(**kwargs))
            except (TypeError, ValueError):
                kwargs.pop("emoji", None)
                options.append(discord.SelectOption(**kwargs))
        if not options:
            options = [
                discord.SelectOption(
                    label="Nenhuma opção configurada",
                    value="none",
                )
            ]
        super().__init__(
            placeholder="Selecione uma opção do menu",
            min_values=1,
            max_values=1,
            options=options,
            disabled=not items,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = int(self.values[0])
        await interaction.response.edit_message(
            embed=self.session.menu_embed(selected),
            view=TicketMenuManagerView(self.session, selected),
        )


class TicketMenuManagerView(_TicketSetupOwnedView):
    def __init__(
        self,
        session: TicketSetupSession,
        selected_index: int | None = None,
    ) -> None:
        super().__init__(session)
        items = session.config.select_options[:25]
        self.selected_index = (
            selected_index
            if selected_index is not None and 0 <= selected_index < len(items)
            else None
        )
        self.add_item(_TicketMenuOptionSelect(session, self.selected_index))

    @discord.ui.button(label="Adicionar opção", style=discord.ButtonStyle.success, row=1)
    async def add_option(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        if len(self.session.config.select_options) >= 25:
            await interaction.response.send_message(
                "O menu já atingiu o limite de 25 opções.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(TicketMenuOptionModal(self.session))

    @discord.ui.button(label="Editar selecionada", style=discord.ButtonStyle.primary, row=1)
    async def edit_option(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        if self.selected_index is None:
            await interaction.response.send_message(
                "Selecione uma opção no menu acima.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            TicketMenuOptionModal(
                self.session,
                index=self.selected_index,
            )
        )

    @discord.ui.button(label="Remover selecionada", style=discord.ButtonStyle.danger, row=1)
    async def remove_option(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        if self.selected_index is None:
            await interaction.response.send_message(
                "Selecione uma opção no menu acima.",
                ephemeral=True,
            )
            return
        if len(self.session.config.select_options) <= 1:
            await interaction.response.send_message(
                "O menu precisa manter ao menos uma opção. Se não quiser usar o "
                "menu, altere o modo do painel para Somente botão.",
                ephemeral=True,
            )
            return
        self.session.config.select_options.pop(self.selected_index)
        await self.session.save()
        await interaction.response.edit_message(
            embed=self.session.menu_embed(),
            view=TicketMenuManagerView(self.session),
        )

    @discord.ui.button(label="Editar texto do menu", style=discord.ButtonStyle.secondary, row=2)
    async def edit_placeholder(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await interaction.response.send_modal(
            TicketMenuPlaceholderModal(self.session)
        )

    @discord.ui.button(label="Voltar ao editor", style=discord.ButtonStyle.secondary, row=2)
    async def back(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await interaction.response.edit_message(
            embed=self.session.embed(),
            view=TicketSetupView(self.session),
        )


class _TicketFieldSelect(discord.ui.Select):
    def __init__(
        self,
        session: TicketSetupSession,
        selected_index: int | None,
    ) -> None:
        self.session = session
        items = session.config.fields[:25]
        options = [
            discord.SelectOption(
                label=str(item.get("name", "Sem título"))[:100] or "Sem título",
                value=str(index),
                description=(
                    "Exibido lado a lado"
                    if item.get("inline")
                    else "Exibido em linha inteira"
                ),
                default=index == selected_index,
            )
            for index, item in enumerate(items)
        ]
        if not options:
            options = [
                discord.SelectOption(
                    label="Nenhum campo configurado",
                    value="none",
                )
            ]
        super().__init__(
            placeholder="Selecione um campo para editar",
            min_values=1,
            max_values=1,
            options=options,
            disabled=not items,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = int(self.values[0])
        await interaction.response.edit_message(
            embed=self.session.fields_embed(selected),
            view=TicketFieldManagerView(self.session, selected),
        )


class TicketFieldManagerView(_TicketSetupOwnedView):
    def __init__(
        self,
        session: TicketSetupSession,
        selected_index: int | None = None,
    ) -> None:
        super().__init__(session)
        items = session.config.fields[:25]
        self.selected_index = (
            selected_index
            if selected_index is not None and 0 <= selected_index < len(items)
            else None
        )
        self.add_item(_TicketFieldSelect(session, self.selected_index))

    @discord.ui.button(label="Adicionar campo", style=discord.ButtonStyle.success, row=1)
    async def add_field(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        if len(self.session.config.fields) >= 25:
            await interaction.response.send_message(
                "O painel já atingiu o limite de 25 campos.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(TicketFieldItemModal(self.session))

    @discord.ui.button(label="Editar selecionado", style=discord.ButtonStyle.primary, row=1)
    async def edit_field(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        if self.selected_index is None:
            await interaction.response.send_message(
                "Selecione um campo no menu acima.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            TicketFieldItemModal(
                self.session,
                index=self.selected_index,
            )
        )

    @discord.ui.button(label="Remover selecionado", style=discord.ButtonStyle.danger, row=1)
    async def remove_field(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        if self.selected_index is None:
            await interaction.response.send_message(
                "Selecione um campo no menu acima.",
                ephemeral=True,
            )
            return
        self.session.config.fields.pop(self.selected_index)
        await self.session.save()
        await interaction.response.edit_message(
            embed=self.session.fields_embed(),
            view=TicketFieldManagerView(self.session),
        )

    @discord.ui.button(label="Voltar ao editor", style=discord.ButtonStyle.secondary, row=2)
    async def back(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await interaction.response.edit_message(
            embed=self.session.embed(),
            view=TicketSetupView(self.session),
        )


class TicketContentModal(discord.ui.Modal):
    def __init__(self, session: TicketSetupSession) -> None:
        super().__init__(title="Conteúdo do painel")
        self.session = session
        config = session.config
        self.author = discord.ui.TextInput(
            label="Author",
            default=config.author_name[:256],
            required=False,
            max_length=256,
        )
        self.title_text = discord.ui.TextInput(
            label="Título",
            default=config.title[:256],
            required=False,
            max_length=256,
        )
        self.description = discord.ui.TextInput(
            label="Descrição",
            default=config.description[:4000],
            required=False,
            max_length=4000,
            style=discord.TextStyle.paragraph,
        )
        self.footer = discord.ui.TextInput(
            label="Footer",
            default=config.footer[:2048],
            required=False,
            max_length=2048,
        )
        for item in (
            self.author,
            self.title_text,
            self.description,
            self.footer,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.session.config.author_name = str(self.author).strip()
        self.session.config.title = str(self.title_text).strip()
        self.session.config.description = str(self.description).strip()
        self.session.config.footer = str(self.footer).strip()
        await self.session.save()
        await interaction.response.edit_message(
            embed=self.session.embed(),
            view=TicketSetupView(self.session),
        )


class TicketAppearanceModal(discord.ui.Modal):
    def __init__(self, session: TicketSetupSession) -> None:
        super().__init__(title="Aparência do painel")
        self.session = session
        self.color = discord.ui.TextInput(
            label="Cor hexadecimal",
            default=f"#{session.config.color:06X}",
            placeholder="#176B87",
            max_length=7,
        )
        self.timestamp = discord.ui.TextInput(
            label="Exibir data e horário?",
            default="Sim" if session.config.timestamp else "Não",
            placeholder="Sim ou Não",
            max_length=3,
        )
        self.add_item(self.color)
        self.add_item(self.timestamp)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            self.session.config.color = _parse_color(str(self.color))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        timestamp = _normalize(str(self.timestamp))
        if timestamp not in {"sim", "s", "nao", "n", "true", "false", "1", "0"}:
            await interaction.response.send_message(
                "No campo de data e horário, use somente `Sim` ou `Não`.",
                ephemeral=True,
            )
            return
        self.session.config.timestamp = timestamp in {"sim", "s", "true", "1"}
        await self.session.save()
        await interaction.response.edit_message(
            embed=self.session.embed(),
            view=TicketSetupView(self.session),
        )


class TicketMediaModal(discord.ui.Modal):
    def __init__(self, session: TicketSetupSession) -> None:
        super().__init__(title="Mídia do painel")
        self.session = session
        config = session.config
        self.author_icon = _url_input("Author Icon", config.author_icon_url)
        self.footer_icon = _url_input("Footer Icon", config.footer_icon_url)
        self.thumbnail = _url_input("Thumbnail", config.thumbnail_url)
        self.banner = _url_input("Image Banner", config.banner_url)
        for item in (
            self.author_icon,
            self.footer_icon,
            self.thumbnail,
            self.banner,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        values = [
            str(self.author_icon).strip(),
            str(self.footer_icon).strip(),
            str(self.thumbnail).strip(),
            str(self.banner).strip(),
        ]
        if any(value and not _valid_url(value) for value in values):
            await interaction.response.send_message(
                "Todas as mídias precisam usar URLs http:// ou https://.",
                ephemeral=True,
            )
            return
        (
            self.session.config.author_icon_url,
            self.session.config.footer_icon_url,
            self.session.config.thumbnail_url,
            self.session.config.banner_url,
        ) = values
        await self.session.save()
        await interaction.response.edit_message(
            embed=self.session.embed(),
            view=TicketSetupView(self.session),
        )


class TicketOpeningButtonModal(discord.ui.Modal):
    def __init__(
        self,
        session: TicketSetupSession,
        *,
        index: int | None = None,
    ) -> None:
        super().__init__(
            title="Editar botão" if index is not None else "Adicionar botão"
        )
        self.session = session
        self.index = index
        buttons = _ticket_panel_buttons(session.config)
        current = (
            buttons[index]
            if index is not None and 0 <= index < len(buttons)
            else {
                "label": "",
                "category": "Outro",
                "emoji": "",
                "style": "primary",
            }
        )
        self.label_text = discord.ui.TextInput(
            label="Texto do botão",
            default=current["label"],
            placeholder="Ex.: Solicitar orçamento",
            max_length=80,
        )
        self.category = discord.ui.TextInput(
            label="Assunto aberto pelo botão",
            default=current["category"],
            placeholder="Ex.: Orçamento",
            max_length=100,
        )
        self.emoji = discord.ui.TextInput(
            label="Emoji opcional",
            default=current["emoji"],
            placeholder="Ex.: 💼 ou <:ticket:123456789>",
            required=False,
            max_length=100,
        )
        self.style = discord.ui.TextInput(
            label="Cor do botão",
            default=_ticket_button_style_label(current["style"]),
            placeholder="Azul, Cinza, Verde ou Vermelho",
            max_length=10,
        )
        for item in (self.label_text, self.category, self.emoji, self.style):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        style = _ticket_button_style_name(str(self.style))
        if style is None:
            await interaction.response.send_message(
                "Use uma destas cores: Azul, Cinza, Verde ou Vermelho.",
                ephemeral=True,
            )
            return
        item = {
            "label": str(self.label_text).strip()[:80],
            "category": str(self.category).strip()[:100] or "Outro",
            "emoji": str(self.emoji).strip(),
            "style": style,
        }
        if not item["label"]:
            await interaction.response.send_message(
                "Informe o texto que aparecerá no botão.",
                ephemeral=True,
            )
            return
        if self.index is None:
            if len(self.session.config.panel_buttons) >= 20:
                await interaction.response.send_message(
                    "O painel já atingiu o limite de 20 botões.",
                    ephemeral=True,
                )
                return
            self.session.config.panel_buttons.append(item)
            selected_index = len(self.session.config.panel_buttons) - 1
        else:
            if not 0 <= self.index < len(self.session.config.panel_buttons):
                await interaction.response.send_message(
                    "Esse botão não existe mais. Abra novamente o gerenciador.",
                    ephemeral=True,
                )
                return
            self.session.config.panel_buttons[self.index] = item
            selected_index = self.index
        first = self.session.config.panel_buttons[0]
        self.session.config.open_button_label = str(first.get("label", ""))
        self.session.config.open_button_emoji = str(first.get("emoji", ""))
        await self.session.save()
        await interaction.response.edit_message(
            embed=self.session.buttons_embed(selected_index),
            view=TicketButtonManagerView(self.session, selected_index),
        )


class TicketMenuOptionModal(discord.ui.Modal):
    def __init__(
        self,
        session: TicketSetupSession,
        *,
        index: int | None = None,
    ) -> None:
        super().__init__(
            title="Editar opção" if index is not None else "Adicionar opção"
        )
        self.session = session
        self.index = index
        current = (
            session.config.select_options[index]
            if index is not None
            and 0 <= index < len(session.config.select_options)
            else {}
        )
        self.label_text = discord.ui.TextInput(
            label="Nome exibido",
            default=str(current.get("label", "")),
            placeholder="Ex.: Solicitar orçamento",
            max_length=100,
        )
        self.description = discord.ui.TextInput(
            label="Descrição curta",
            default=str(current.get("description", "")),
            placeholder="Ex.: Projetos, sistemas e consultoria",
            required=False,
            max_length=100,
        )
        self.value = discord.ui.TextInput(
            label="Identificador interno (opcional)",
            default=str(current.get("value", "")),
            placeholder="Gerado automaticamente quando vazio",
            required=False,
            max_length=100,
        )
        self.emoji = discord.ui.TextInput(
            label="Emoji opcional",
            default=str(current.get("emoji", "")),
            placeholder="Ex.: 💼 ou <:ticket:123456789>",
            required=False,
            max_length=100,
        )
        for item in (self.label_text, self.description, self.value, self.emoji):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        label = str(self.label_text).strip()[:100]
        if not label:
            await interaction.response.send_message(
                "Informe o nome exibido no menu.",
                ephemeral=True,
            )
            return
        value = _unique_ticket_option_value(
            self.session.config.select_options,
            str(self.value).strip() or label,
            ignore_index=self.index,
        )
        item = {
            "label": label,
            "value": value,
            "description": str(self.description).strip()[:100],
            "emoji": str(self.emoji).strip(),
        }
        if self.index is None:
            if len(self.session.config.select_options) >= 25:
                await interaction.response.send_message(
                    "O menu já atingiu o limite de 25 opções.",
                    ephemeral=True,
                )
                return
            self.session.config.select_options.append(item)
            selected_index = len(self.session.config.select_options) - 1
        else:
            if not 0 <= self.index < len(self.session.config.select_options):
                await interaction.response.send_message(
                    "Essa opção não existe mais. Abra novamente o gerenciador.",
                    ephemeral=True,
                )
                return
            self.session.config.select_options[self.index] = item
            selected_index = self.index
        await self.session.save()
        await interaction.response.edit_message(
            embed=self.session.menu_embed(selected_index),
            view=TicketMenuManagerView(self.session, selected_index),
        )


class TicketMenuPlaceholderModal(discord.ui.Modal):
    def __init__(self, session: TicketSetupSession) -> None:
        super().__init__(title="Texto do menu")
        self.session = session
        self.placeholder = discord.ui.TextInput(
            label="Texto exibido antes da seleção",
            default=session.config.select_placeholder[:150],
            placeholder="Selecione o assunto do atendimento",
            max_length=150,
        )
        self.add_item(self.placeholder)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.session.config.select_placeholder = (
            str(self.placeholder).strip()[:150]
            or "Selecione o assunto do atendimento"
        )
        await self.session.save()
        await interaction.response.edit_message(
            embed=self.session.menu_embed(),
            view=TicketMenuManagerView(self.session),
        )


class TicketFieldItemModal(discord.ui.Modal):
    def __init__(
        self,
        session: TicketSetupSession,
        *,
        index: int | None = None,
    ) -> None:
        super().__init__(
            title="Editar campo" if index is not None else "Adicionar campo"
        )
        self.session = session
        self.index = index
        current = (
            session.config.fields[index]
            if index is not None and 0 <= index < len(session.config.fields)
            else {}
        )
        self.name = discord.ui.TextInput(
            label="Título do campo",
            default=str(current.get("name", "")),
            placeholder="Ex.: Horário de atendimento",
            max_length=256,
        )
        self.value = discord.ui.TextInput(
            label="Conteúdo",
            default=str(current.get("value", "")),
            placeholder="Informe o texto exibido neste bloco",
            max_length=1024,
            style=discord.TextStyle.paragraph,
        )
        self.inline = discord.ui.TextInput(
            label="Exibir lado a lado?",
            default="Sim" if current.get("inline") else "Não",
            placeholder="Sim ou Não",
            max_length=3,
        )
        for item in (self.name, self.value, self.inline):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        inline = _normalize(str(self.inline))
        if inline not in {"sim", "s", "nao", "n", "true", "false", "1", "0"}:
            await interaction.response.send_message(
                "No campo de layout, use somente `Sim` ou `Não`.",
                ephemeral=True,
            )
            return
        item = {
            "name": str(self.name).strip()[:256],
            "value": str(self.value).strip()[:1024],
            "inline": inline in {"sim", "s", "true", "1"},
        }
        if not item["name"] or not item["value"]:
            await interaction.response.send_message(
                "Preencha o título e o conteúdo do campo.",
                ephemeral=True,
            )
            return
        if self.index is None:
            if len(self.session.config.fields) >= 25:
                await interaction.response.send_message(
                    "O painel já atingiu o limite de 25 campos.",
                    ephemeral=True,
                )
                return
            self.session.config.fields.append(item)
            selected_index = len(self.session.config.fields) - 1
        else:
            if not 0 <= self.index < len(self.session.config.fields):
                await interaction.response.send_message(
                    "Esse campo não existe mais. Abra novamente o gerenciador.",
                    ephemeral=True,
                )
                return
            self.session.config.fields[self.index] = item
            selected_index = self.index
        await self.session.save()
        await interaction.response.edit_message(
            embed=self.session.fields_embed(selected_index),
            view=TicketFieldManagerView(self.session, selected_index),
        )


class TicketInternalButtonsModal(discord.ui.Modal):
    def __init__(self, session: TicketSetupSession) -> None:
        super().__init__(title="Botões dentro do ticket")
        self.session = session
        config = session.config
        self.close_label = discord.ui.TextInput(
            label="Texto para encerrar",
            default=config.close_button_label[:80],
            max_length=80,
        )
        self.close_emoji = discord.ui.TextInput(
            label="Emoji para encerrar (opcional)",
            default=config.close_button_emoji,
            required=False,
            max_length=100,
        )
        self.escalate_label = discord.ui.TextInput(
            label="Texto para solicitar equipe",
            default=config.escalate_button_label[:80],
            max_length=80,
        )
        self.escalate_emoji = discord.ui.TextInput(
            label="Emoji para equipe (opcional)",
            default=config.escalate_button_emoji,
            required=False,
            max_length=100,
        )
        for item in (
            self.close_label,
            self.close_emoji,
            self.escalate_label,
            self.escalate_emoji,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.session.config.close_button_label = (
            str(self.close_label).strip()[:80] or "Encerrar"
        )
        self.session.config.close_button_emoji = str(self.close_emoji).strip()
        self.session.config.escalate_button_label = (
            str(self.escalate_label).strip()[:80] or "Solicitar equipe"
        )
        self.session.config.escalate_button_emoji = str(
            self.escalate_emoji
        ).strip()
        await self.session.save()
        await interaction.response.edit_message(
            embed=self.session.embed(),
            view=TicketSetupView(self.session),
        )


class TicketFieldsModal(discord.ui.Modal):
    def __init__(self, session: TicketSetupSession) -> None:
        super().__init__(title="Campos do painel")
        self.session = session
        current = "\n".join(
            f"{item.get('name', '')}|{item.get('value', '')}|"
            f"{'sim' if item.get('inline') else 'não'}"
            for item in session.config.fields
        )
        self.fields_text = discord.ui.TextInput(
            label="Um campo por linha: nome|valor|inline",
            default=current[:4000],
            required=False,
            max_length=4000,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.fields_text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        fields: list[dict[str, Any]] = []
        for line in str(self.fields_text).splitlines()[:25]:
            name, separator, remainder = line.partition("|")
            if not separator:
                continue
            value, _, inline = remainder.rpartition("|")
            fields.append(
                {
                    "name": name.strip()[:256],
                    "value": value.strip()[:1024],
                    "inline": inline.strip().lower() in {"sim", "s", "true", "1"},
                }
            )
        self.session.config.fields = fields
        await self.session.save()
        await interaction.response.edit_message(
            embed=self.session.embed(),
            view=TicketSetupView(self.session),
        )


class TicketComponentsModal(discord.ui.Modal):
    def __init__(self, session: TicketSetupSession) -> None:
        super().__init__(title="Botões e menu")
        self.session = session
        config = session.config
        self.open_button = discord.ui.TextInput(
            label="Botão abrir: texto|emoji",
            default=f"{config.open_button_label}|{config.open_button_emoji}",
            max_length=120,
        )
        self.close_button = discord.ui.TextInput(
            label="Botão encerrar: texto|emoji",
            default=f"{config.close_button_label}|{config.close_button_emoji}",
            max_length=120,
        )
        self.escalate_button = discord.ui.TextInput(
            label="Botão equipe: texto|emoji",
            default=f"{config.escalate_button_label}|{config.escalate_button_emoji}",
            max_length=120,
        )
        self.placeholder = discord.ui.TextInput(
            label="Placeholder do menu",
            default=config.select_placeholder[:150],
            max_length=150,
        )
        options = "\n".join(
            "|".join(
                (
                    item.get("label", ""),
                    item.get("value", ""),
                    item.get("description", ""),
                    item.get("emoji", ""),
                )
            )
            for item in config.select_options
        )
        self.options = discord.ui.TextInput(
            label="Opções: label|valor|descrição|emoji",
            default=options[:4000],
            required=False,
            max_length=4000,
            style=discord.TextStyle.paragraph,
        )
        for item in (
            self.open_button,
            self.close_button,
            self.escalate_button,
            self.placeholder,
            self.options,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        (
            self.session.config.open_button_label,
            self.session.config.open_button_emoji,
        ) = _component_parts(str(self.open_button))
        (
            self.session.config.close_button_label,
            self.session.config.close_button_emoji,
        ) = _component_parts(str(self.close_button))
        (
            self.session.config.escalate_button_label,
            self.session.config.escalate_button_emoji,
        ) = _component_parts(str(self.escalate_button))
        self.session.config.select_placeholder = str(self.placeholder).strip()
        options: list[dict[str, str]] = []
        for line in str(self.options).splitlines()[:25]:
            parts = [item.strip() for item in line.split("|", 3)]
            while len(parts) < 4:
                parts.append("")
            label, value, description, emoji = parts
            if label:
                options.append(
                    {
                        "label": label[:100],
                        "value": (value or _slug(label))[:100],
                        "description": description[:100],
                        "emoji": emoji,
                    }
                )
        self.session.config.select_options = options
        await self.session.save()
        await interaction.response.edit_message(
            embed=self.session.embed(),
            view=TicketSetupView(self.session),
        )


class TicketMessagesModal(discord.ui.Modal):
    def __init__(self, session: TicketSetupSession) -> None:
        super().__init__(title="Personalidade do atendimento")
        self.session = session
        self.opening = discord.ui.TextInput(
            label="Orientação da recepção",
            default=session.config.opening_instruction[:1000],
            max_length=1000,
            style=discord.TextStyle.paragraph,
        )
        self.closing = discord.ui.TextInput(
            label="Orientação do encerramento",
            default=session.config.closing_instruction[:1000],
            max_length=1000,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.opening)
        self.add_item(self.closing)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.session.config.opening_instruction = str(self.opening).strip()
        self.session.config.closing_instruction = str(self.closing).strip()
        await self.session.save()
        await interaction.response.edit_message(
            embed=self.session.embed(),
            view=TicketSetupView(self.session),
        )


def _ticket_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": list(TICKET_CATEGORIES)},
            "priority": {
                "type": "string",
                "enum": ["Baixa", "Normal", "Média", "Alta", "Crítica"],
            },
            "action": {
                "type": "string",
                "enum": ["answer", "ask", "resolve", "escalate"],
            },
            "summary": {"type": "string"},
            "reply": {"type": "string"},
            "resolution_confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
            "user_satisfied": {"type": "boolean"},
        },
        "required": [
            "category",
            "priority",
            "action",
            "summary",
            "reply",
            "resolution_confidence",
            "user_satisfied",
        ],
    }


def _ticket_result(
    *,
    category: str,
    action: str,
    summary: str,
    reply: str,
    priority: str = "Normal",
    confidence: float = 1.0,
    user_satisfied: bool = False,
) -> dict[str, Any]:
    return {
        "category": category,
        "priority": priority,
        "action": action,
        "summary": summary,
        "reply": reply,
        "resolution_confidence": confidence,
        "user_satisfied": user_satisfied,
    }


def _bot_invoked(
    message: discord.Message,
    bot_user: discord.abc.User | None,
) -> bool:
    if bot_user and any(user.id == bot_user.id for user in message.mentions):
        return True
    return re.match(
        r"^\s*(?:senhor\s+)?jarvis\b",
        message.content,
        flags=re.IGNORECASE,
    ) is not None


def _clean_ticket_name(value: str) -> str:
    cleaned = re.sub(r"<(?:@!?|@&|#)\d+>", " ", value)
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned)
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned[:100]


def _clean_ticket_label(value: str) -> str:
    return _clean_ticket_name(value)[:100]


def _canonical_ticket_status(value: str) -> str | None:
    normalized = _normalize(value)
    aliases = {
        "aberto": "open",
        "reaberto": "open",
        "em atendimento": "in_progress",
        "em andamento": "in_progress",
        "andamento": "in_progress",
        "aguardando usuario": "waiting_user",
        "pendente usuario": "waiting_user",
        "aguardando cliente": "waiting_user",
        "pendente cliente": "waiting_user",
        "aguardando equipe": "escalated",
        "pendente equipe": "escalated",
        "encaminhado": "escalated",
        "escalado": "escalated",
    }
    return aliases.get(normalized)


def _normalize(value: str) -> str:
    decomposed = normalize("NFKD", value.casefold())
    ascii_text = "".join(char for char in decomposed if not combining(char))
    return " ".join(ascii_text.split())


def _requests_talisson(text: str) -> bool:
    if not any(
        name in text
        for name in ("talisson", "talinho", "talinhodev", "senhor talisson")
    ):
        return False
    return any(
        expression in text
        for expression in (
            "falar com",
            "conversar com",
            "contato com",
            "chamar",
            "quero falar",
            "preciso falar",
            "assunto com",
            "reuniao com",
        )
    )


def _requests_human_attention(text: str) -> bool:
    text = _normalize(text)
    return any(
        expression in text
        for expression in (
            "falar com humano",
            "falar com um humano",
            "falar com alguem",
            "falar com alguém",
            "chamar suporte",
            "chamar equipe",
            "solicitar equipe",
            "solicitar atendente",
            "quero suporte",
            "quero humano",
            "quero atendente",
            "administracao",
            "administração",
            "diretoria",
            "responsavel",
            "responsável",
        )
    ) or _requests_talisson(text)


def _message_has_useful_context(message: str) -> bool:
    text = _normalize(message)
    compact = text.strip(" ?!.,")
    invalid = {
        "oi",
        "ola",
        "opa",
        "ajuda",
        "suporte",
        "equipe",
        "humano",
        "atendente",
        "administracao",
        "administração",
        "preciso falar com alguem",
        "preciso falar com um humano",
        "preciso falar com alguém",
        "quero falar com alguem",
        "quero falar com um humano",
        "quero falar com alguém",
        "preciso de ajuda",
    }
    if compact in invalid:
        return False
    words = [word for word in re.findall(r"[a-z0-9]+", text) if len(word) > 2]
    if len(words) >= 4:
        return True
    if "pagamento" in text and "recusado" in text:
        return True
    return any(
        term in text
        for term in (
            "pagamento recusado",
            "servidor",
            "orcamento",
            "orçamento",
            "servico",
            "serviço",
            "erro",
            "bug",
            "site",
            "plataforma",
            "projeto",
            "parceria",
            "reuniao",
            "reunião",
            "contratar",
            "proposta",
        )
    )


def _split_text(text: str, limit: int) -> list[str]:
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return [part for part in parts if part.strip()]


def _quote_intake_active(record: dict[str, Any]) -> bool:
    intake = record.get("quote_intake")
    return isinstance(intake, dict) and not intake.get("finished")


def _record_has_minimum_context(record: dict[str, Any]) -> bool:
    if bool(record.get("triage_complete")):
        return True
    summary = str(record.get("summary", ""))
    return _message_has_useful_context(summary)


def _is_close_ticket_request(content: str) -> bool:
    text = _normalize(content)
    if "ticket" not in text and "atendimento" not in text:
        return False
    return any(
        term in text
        for term in (
            "encerrar",
            "encerre",
            "fechar",
            "feche",
            "finalizar",
            "finalize",
            "concluir",
            "conclua",
            "arquivar",
            "arquive",
        )
    )


def _commercial_category(text: str) -> str:
    categories = (
        (
            "Orçamento",
            (
                "orcamento",
                "quanto custa",
                "contratar um servico",
                "contratar o talisson",
            ),
        ),
        (
            "Reunião",
            (
                "marcar reuniao",
                "agendar reuniao",
                "reuniao comercial",
            ),
        ),
        (
            "Parceria",
            (
                "parceria",
                "colaboracao comercial",
            ),
        ),
        (
            "Comercial",
            (
                "proposta comercial",
                "negociacao",
                "contratacao",
                "projeto personalizado",
            ),
        ),
    )
    for category, expressions in categories:
        if any(expression in text for expression in expressions):
            return category
    return ""


def _is_satisfied(text: str) -> bool:
    return any(
        expression in text
        for expression in (
            "resolveu obrigado",
            "resolvido obrigado",
            "era isso",
            "deu certo",
            "ja resolveu",
            "pode fechar",
            "pode encerrar",
        )
    )


def _is_technical_issue(text: str) -> bool:
    return any(
        expression in text
        for expression in (
            "erro 404",
            "erro 500",
            "erro 502",
            "erro 503",
            "site fora do ar",
            "site nao abre",
            "site nao funciona",
            "pagina quebrada",
            "formulario nao envia",
            "deu erro no site",
            "problema tecnico",
        )
    )


def _ticket_panel_mode(value: str) -> str:
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "select": "select_only",
        "menu": "select_only",
        "somente_menu": "select_only",
        "select_only": "select_only",
        "button": "button_only",
        "botao": "button_only",
        "somente_botao": "button_only",
        "button_only": "button_only",
        "ambos": "button_and_select",
        "menu_e_botao": "button_and_select",
        "button_and_select": "button_and_select",
    }
    return aliases.get(normalized, "select_only")


def _ticket_render_mode(value: str) -> str:
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized in {"v1", "embed", "embed_v1", "classico", "clássico"}:
        return "v1"
    return "v2"


def _ticket_image_position(value: str) -> str:
    normalized = (
        str(value or "")
        .strip()
        .casefold()
        .replace("í", "i")
        .replace("ó", "o")
        .replace("é", "e")
        .replace("ã", "a")
        .replace("ç", "c")
    )
    if normalized in {"top", "topo", "inicio", "início", "antes"}:
        return "top"
    if normalized in {"middle", "meio", "centro"}:
        return "middle"
    return "end"


def _ticket_banner_embed(config: TicketConfig) -> discord.Embed:
    embed = discord.Embed(color=discord.Color(config.color))
    embed.set_image(url=config.banner_url)
    return embed


def _ticket_timezone(config: TicketConfig) -> ZoneInfo:
    try:
        return ZoneInfo(config.opening_hours_timezone or "America/Sao_Paulo")
    except ZoneInfoNotFoundError:
        return ZoneInfo("America/Sao_Paulo")


def _parse_ticket_time(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value or "")
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour, minute


def _minutes_of_day(moment: datetime) -> int:
    return moment.hour * 60 + moment.minute


def _ticket_schedule_text(config: TicketConfig) -> str:
    return str(config.closed_schedule_text or "").strip()


def _ticket_unavailable_message(config: TicketConfig, reason: str) -> str:
    doubt_channel_id = int(
        config.fallback_doubt_channel_id or DEFAULT_TICKET_DOUBT_CHANNEL_ID
    )
    template = str(config.closed_block_template or "").strip()
    rendered = (
        template.replace("{motivo}", reason.strip())
        .replace("{horarios}", _ticket_schedule_text(config))
        .replace("{canal_duvidas}", f"<#{doubt_channel_id}>")
    )
    rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()
    return rendered or reason.strip() or "A central está indisponível no momento."


def _ticket_closed_panel_message(config: TicketConfig, details: str) -> str:
    title = str(config.closed_block_title or "").strip()
    heading = f"# {title}" if title else "# Atendimento indisponível"
    return "\n\n".join(
        value for value in (heading, details.strip()) if value
    )[:4000]


def _merge_ticket_message_content(
    current: str,
    addition: str,
    *,
    limit: int,
) -> str:
    current = str(current or "").strip()
    addition = str(addition or "").strip()
    if not addition:
        return current[:limit]
    if not current:
        return addition[:limit]
    available = max(0, limit - len(addition) - 2)
    if not available:
        return addition[:limit]
    return f"{current[:available].rstrip()}\n\n{addition}"[:limit]


def _ticket_panel_reply_content(
    config: TicketConfig,
    *,
    notice: str | None = None,
) -> str:
    available, unavailable_message = _ticket_availability(config)
    content = (
        _ticket_closed_panel_message(config, unavailable_message)
        if not available
        else ""
    )
    clean_notice = str(notice or "").strip()
    if clean_notice:
        content = _merge_ticket_message_content(
            content,
            f"**Aviso:** {clean_notice[:1500]}",
            limit=2000,
        )
    return content[:2000]


async def _sync_ticket_panel_reply(
    manager: SmartTicketManager,
    config: TicketConfig,
    channel: discord.TextChannel,
    panel_message: discord.Message,
    *,
    notice: str | None = None,
) -> None:
    content = _ticket_panel_reply_content(config, notice=notice)
    reply: discord.Message | None = None
    changed = False
    if config.panel_notice_message_id:
        try:
            reply = await channel.fetch_message(config.panel_notice_message_id)
        except discord.NotFound:
            config.panel_notice_message_id = None
            changed = True
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning(
                "Não foi possível consultar a resposta do painel de tickets %s.",
                config.panel_notice_message_id,
            )
            return
    if reply is not None:
        referenced_id = getattr(getattr(reply, "reference", None), "message_id", None)
        if referenced_id != panel_message.id:
            try:
                await reply.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            reply = None
            config.panel_notice_message_id = None
            changed = True
    if not content:
        if reply is not None:
            try:
                await reply.delete()
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.warning(
                    "Não foi possível remover a resposta do painel de tickets %s.",
                    reply.id,
                )
                return
            config.panel_notice_message_id = None
            changed = True
        if changed:
            await manager.store.save_config(config)
        return
    if reply is not None:
        try:
            await reply.edit(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            reply = None
            config.panel_notice_message_id = None
            changed = True
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning(
                "Não foi possível atualizar a resposta do painel de tickets %s.",
                reply.id,
            )
            return
    if reply is None:
        try:
            reply = await channel.send(
                content,
                reference=panel_message,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning(
                "Não foi possível responder ao painel de tickets %s.",
                panel_message.id,
            )
            if changed:
                await manager.store.save_config(config)
            return
        config.panel_notice_message_id = reply.id
        changed = True
    if changed:
        await manager.store.save_config(config)


def _ticket_is_inside_hours(config: TicketConfig, now: datetime) -> bool:
    windows = config.opening_hours.get(str(now.weekday()), [])
    if not isinstance(windows, list):
        return False
    current = _minutes_of_day(now)
    for window in windows:
        if not isinstance(window, dict):
            continue
        start = _parse_ticket_time(str(window.get("start", "")))
        end = _parse_ticket_time(str(window.get("end", "")))
        if start is None or end is None:
            continue
        start_minutes = start[0] * 60 + start[1]
        end_minutes = end[0] * 60 + end[1]
        if start_minutes <= current < end_minutes:
            return True
    return False


def _ticket_availability(config: TicketConfig) -> tuple[bool, str]:
    if not config.system_enabled:
        reason = (config.closed_reason or "Central temporariamente em manutenção.").strip()
        return False, _ticket_unavailable_message(config, reason)
    if not config.opening_hours_enabled:
        return True, ""
    now = datetime.now(_ticket_timezone(config))
    if _ticket_is_inside_hours(config, now):
        return True, ""
    return False, _ticket_unavailable_message(
        config,
        "A central de atendimento está fechada neste horário.",
    )


def _ticket_panel_mode_label(value: str) -> str:
    return {
        "select_only": "Somente menu",
        "button_only": "Somente botão",
        "button_and_select": "Menu + botão",
    }[_ticket_panel_mode(value)]


def _ticket_panel_buttons(config: TicketConfig) -> list[dict[str, str]]:
    buttons: list[dict[str, str]] = []
    maximum = 20 if _ticket_panel_mode(config.panel_mode) == "button_and_select" else 25
    for item in config.panel_buttons[:maximum]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()[:80]
        if not label:
            continue
        buttons.append(
            {
                "label": label,
                "category": str(item.get("category", "Outro")).strip()[:100]
                or "Outro",
                "emoji": str(item.get("emoji", "")).strip(),
                "style": str(item.get("style", "primary")).strip().casefold(),
            }
        )
    return buttons


def _ticket_button_style(value: str) -> discord.ButtonStyle:
    return {
        "primary": discord.ButtonStyle.primary,
        "azul": discord.ButtonStyle.primary,
        "secondary": discord.ButtonStyle.secondary,
        "cinza": discord.ButtonStyle.secondary,
        "success": discord.ButtonStyle.success,
        "verde": discord.ButtonStyle.success,
        "danger": discord.ButtonStyle.danger,
        "vermelho": discord.ButtonStyle.danger,
    }.get(value.strip().casefold(), discord.ButtonStyle.primary)


def _ticket_button_style_name(value: str) -> str | None:
    normalized = _normalize(value)
    return {
        "primary": "primary",
        "azul": "primary",
        "secondary": "secondary",
        "cinza": "secondary",
        "cinzento": "secondary",
        "success": "success",
        "verde": "success",
        "danger": "danger",
        "vermelho": "danger",
    }.get(normalized)


def _ticket_button_style_label(value: str) -> str:
    return {
        "primary": "Azul",
        "secondary": "Cinza",
        "success": "Verde",
        "danger": "Vermelho",
    }.get(
        _ticket_button_style_name(value) or "primary",
        "Azul",
    )


def _unique_ticket_option_value(
    options: list[dict[str, str]],
    source: str,
    *,
    ignore_index: int | None = None,
) -> str:
    base = _slug(source).strip("-")[:90] or "assunto"
    used = {
        str(item.get("value", "")).casefold()
        for index, item in enumerate(options)
        if index != ignore_index
    }
    candidate = base
    suffix = 2
    while candidate.casefold() in used:
        ending = f"-{suffix}"
        candidate = f"{base[:100 - len(ending)]}{ending}"
        suffix += 1
    return candidate[:100]


def _ticket_panel_header(config: TicketConfig) -> str:
    values: list[str] = []
    if config.author_name.strip():
        values.append(f"-# {config.author_name.strip()[:180]}")
    if config.title.strip():
        values.append(f"## {config.title.strip()[:240]}")
    if config.description.strip():
        values.append(config.description.strip()[:3300])
    return "\n".join(values)[:4000] or "## Central de Atendimento"


def _ticket_panel_fields(config: TicketConfig) -> str:
    blocks: list[str] = []
    size = 0
    for item in config.fields[:12]:
        name = str(item.get("name", "")).strip()[:120]
        value = str(item.get("value", "")).strip()[:700]
        if not name and not value:
            continue
        if name and not name.casefold().startswith("informa"):
            block = f"### {name}\n{value or 'N?o informado'}"
        else:
            block = value or "N?o informado"
        if size + len(block) + 2 > 3900:
            break
        blocks.append(block)
        size += len(block) + 2
    return "\n\n".join(blocks)


def _ticket_panel_footer(config: TicketConfig) -> str:
    values: list[str] = []
    if config.footer.strip():
        values.append(config.footer.strip()[:500])
    if config.timestamp:
        values.append(f"<t:{int(datetime.now(timezone.utc).timestamp())}:f>")
    return f"-# {'  •  '.join(values)}" if values else ""


def _ticket_select_options(config: TicketConfig) -> list[discord.SelectOption]:
    options: list[discord.SelectOption] = []
    source = config.select_options[:25] or [
        {
            "label": "Outro",
            "value": "outro",
            "description": "Assunto não listado",
            "emoji": "",
        }
    ]
    for item in source:
        kwargs: dict[str, Any] = {
            "label": str(item.get("label", "Outro"))[:100] or "Outro",
            "value": str(item.get("value", "outro"))[:100] or "outro",
        }
        description = str(item.get("description", "")).strip()
        emoji = str(item.get("emoji", "")).strip()
        if description:
            kwargs["description"] = description[:100]
        if emoji:
            kwargs["emoji"] = emoji
        try:
            options.append(discord.SelectOption(**kwargs))
        except (TypeError, ValueError):
            kwargs.pop("emoji", None)
            options.append(discord.SelectOption(**kwargs))
    return options


class TicketClassicPanelView(discord.ui.View):
    def __init__(
        self,
        manager: SmartTicketManager,
        config: TicketConfig,
        *,
        preview: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.manager = manager
        self.config = config
        available, _ = _ticket_availability(config)
        closed = preview or not available
        mode = _ticket_panel_mode(config.panel_mode)
        if mode in {"select_only", "button_and_select"}:
            select = discord.ui.Select(
                custom_id="jarvis:ticket:subject",
                placeholder=config.select_placeholder[:150]
                or "Selecione o assunto do atendimento",
                min_values=1,
                max_values=1,
                options=_ticket_select_options(config),
                disabled=closed,
            )

            async def select_callback(interaction: discord.Interaction) -> None:
                await self.manager.create_ticket_from_selection(
                    interaction,
                    select.values[0],
                )

            select.callback = select_callback
            self.add_item(select)
        if mode in {"button_only", "button_and_select"}:
            for index, item in enumerate(_ticket_panel_buttons(config)[:25]):
                kwargs: dict[str, Any] = {
                    "label": item["label"],
                    "style": _ticket_button_style(item["style"]),
                    "custom_id": (
                        "jarvis:ticket:open"
                        if index == 0
                        else f"jarvis:ticket:open:{index}"
                    ),
                    "disabled": closed,
                }
                if item["emoji"]:
                    kwargs["emoji"] = item["emoji"]
                try:
                    button = discord.ui.Button(**kwargs)
                except (TypeError, ValueError):
                    kwargs.pop("emoji", None)
                    button = discord.ui.Button(**kwargs)

                async def button_callback(
                    interaction: discord.Interaction,
                    selected_category: str = item["category"],
                ) -> None:
                    await self.manager.create_ticket(
                        interaction,
                        selected_category=selected_category,
                    )

                button.callback = button_callback
                self.add_item(button)


def ticket_panel_message_kwargs(
    manager: SmartTicketManager,
    config: TicketConfig,
    *,
    preview: bool = False,
    force_render_mode: str | None = None,
    notice: str | None = None,
) -> dict[str, Any]:
    render_mode = _ticket_render_mode(force_render_mode or config.panel_render_mode)
    if render_mode == "v1":
        embeds = config.panel_embeds()
        return {
            "content": config.panel_message_content() or None,
            "embeds": embeds[:10],
            "view": TicketClassicPanelView(manager, config, preview=preview),
            "allowed_mentions": discord.AllowedMentions.none(),
        }
    return {
        "content": None,
        "embeds": [],
        "view": TicketPanelView(manager, config, preview=preview, notice=notice),
        "allowed_mentions": discord.AllowedMentions.none(),
    }


async def send_or_edit_ticket_panel(
    manager: SmartTicketManager,
    config: TicketConfig,
    channel: discord.TextChannel,
    message: discord.Message | None = None,
    *,
    preview: bool = False,
    notice: str | None = None,
) -> discord.Message:
    kwargs = ticket_panel_message_kwargs(manager, config, preview=preview, notice=notice)
    try:
        if message is None:
            sent = await channel.send(**kwargs)
            config.panel_message_id = sent.id
            await manager.store.save_config(config)
        else:
            await message.edit(**kwargs)
            sent = message
    except (discord.HTTPException, TypeError, ValueError):
        if _ticket_render_mode(config.panel_render_mode) != "v2":
            raise
        fallback_kwargs = ticket_panel_message_kwargs(
            manager,
            config,
            preview=preview,
            force_render_mode="v2",
            notice=notice,
        )
        if message is not None:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        sent = await channel.send(**fallback_kwargs)
        config.panel_message_id = sent.id
        await manager.store.save_config(config)
    if not preview:
        await _sync_ticket_panel_reply(
            manager,
            config,
            channel,
            sent,
            notice=notice,
        )
    return sent


async def _interaction_reply(
    interaction: discord.Interaction,
    message: str,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def _component_parts(value: str) -> tuple[str, str]:
    label, _, emoji = value.partition("|")
    return label.strip()[:80] or "Atendimento", emoji.strip()


def _url_input(label: str, default: str) -> discord.ui.TextInput:
    return discord.ui.TextInput(
        label=label,
        default=default[:4000],
        required=False,
        max_length=4000,
        placeholder="https://...",
    )


def _valid_url(value: str) -> bool:
    return value.startswith(("https://", "http://"))


def _safe_color(value: Any) -> int:
    try:
        return max(0, min(0xFFFFFF, int(value)))
    except (TypeError, ValueError):
        return 0x176B87


def _parse_color(value: str) -> int:
    cleaned = value.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", cleaned):
        raise ValueError("Use uma cor hexadecimal no formato #176B87.")
    return int(cleaned, 16)


def _slug(value: str) -> str:
    normalized = value.lower().strip()
    normalized = re.sub(r"[^a-z0-9áàâãéêíóôõúç]+", "-", normalized)
    return normalized.strip("-") or "ticket"


def _score(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _format_datetime(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).isoformat() if value else "Não registrado"


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}min"
    if minutes:
        return f"{minutes}min {seconds}s"
    return f"{seconds}s"


def _attachment_summary(message: discord.Message) -> str:
    if not message.attachments:
        return ""
    names = ", ".join(item.filename for item in message.attachments[:5])
    return f"Arquivos enviados: {names}"


def _attachment_mime_type(filename: str) -> str:
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif": "image/gif",
        "pdf": "application/pdf",
        "txt": "text/plain",
        "md": "text/markdown",
        "csv": "text/csv",
        "json": "application/json",
        "xml": "application/xml",
    }.get(extension, "application/octet-stream")
