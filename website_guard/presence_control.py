from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from .state import StateStore

LOGGER = logging.getLogger(__name__)

PRESENCE_CONTROL_SECTION = "presence_control"
PRESENCE_PANEL_SECTION = "presence_control_panel"
DEFAULT_TIMEZONE = "America/Sao_Paulo"
DEFAULT_NIGHT_TEXT = "Zzz... tô descansando um pouquinho. Volto às 07h"
LEGACY_NIGHT_TEXT = "😴 Zzz... tô descansando um pouquinho. Volto às 07h, hihihi"
DEFAULT_DAY_TEXT = "Tô por aqui cuidando de tudo :3"
PANEL_COLOR = 0xC27C2B

STATUS_LABELS = {
    "online": "Online",
    "idle": "Ausente",
    "dnd": "Não perturbe",
    "invisible": "Invisível",
}
STATUS_EMOJIS = {
    "online": "🟢",
    "idle": "🟡",
    "dnd": "🔴",
    "invisible": "⚫",
}
ACTIVITY_LABELS = {
    "custom": "Status personalizado",
    "playing": "Jogando",
    "watching": "Assistindo",
    "listening": "Ouvindo",
    "competing": "Competindo",
    "streaming": "Transmitindo",
}
EXTERNAL_MODE_LABELS = {
    "neffex": "NEFFEX aleatório",
    "squarecloud": "Hospedagem SquareCloud",
    "dynamic": "Rotação dinâmica",
    "streamer": "Modo streamer",
    "default": "Presença padrão",
}


def default_presence_config() -> dict[str, Any]:
    return {
        "mode": "automatic",
        "timezone": DEFAULT_TIMEZONE,
        "night_start": "21:00",
        "day_start": "07:00",
        "night_status": "idle",
        "night_activity_type": "custom",
        "night_text": DEFAULT_NIGHT_TEXT,
        "day_status": "dnd",
        "day_activity_type": "custom",
        "day_text": DEFAULT_DAY_TEXT,
        "manual_status": "dnd",
        "manual_activity_type": "custom",
        "manual_text": DEFAULT_DAY_TEXT,
        "manual_url": "",
        "external_mode": "",
        "external_guild_id": None,
        "updated_by": None,
        "updated_at": None,
    }


def _parse_clock(value: Any, fallback: str) -> time:
    candidate = str(value or fallback).strip()
    try:
        hour_text, minute_text = candidate.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
    except (TypeError, ValueError):
        hour_text, minute_text = fallback.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    return time(hour=hour, minute=minute)


def validate_clock(value: str) -> str:
    candidate = value.strip()
    try:
        hour_text, minute_text = candidate.split(":", 1)
        if len(hour_text) != 2 or len(minute_text) != 2:
            raise ValueError
        hour, minute = int(hour_text), int(minute_text)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("Use o formato HH:MM, por exemplo 21:00.") from exc
    if candidate != f"{hour:02d}:{minute:02d}":
        raise ValueError("Use o formato HH:MM, por exemplo 21:00.")
    return candidate


def validate_timezone(value: str) -> str:
    candidate = value.strip()
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Fuso horário inválido. Exemplo: America/Sao_Paulo.") from exc
    return candidate


def normalize_presence_config(value: Any) -> dict[str, Any]:
    defaults = default_presence_config()
    source = value if isinstance(value, dict) else {}
    config = {**defaults, **source}
    if config["mode"] not in {"automatic", "manual", "disabled"}:
        config["mode"] = "automatic"
    for key in ("night_status", "day_status", "manual_status"):
        if config[key] not in STATUS_LABELS:
            config[key] = defaults[key]
    for key in ("night_activity_type", "day_activity_type", "manual_activity_type"):
        if config[key] not in ACTIVITY_LABELS:
            config[key] = defaults[key]
    config["night_start"] = _parse_clock(config["night_start"], "21:00").strftime("%H:%M")
    config["day_start"] = _parse_clock(config["day_start"], "07:00").strftime("%H:%M")
    try:
        config["timezone"] = validate_timezone(str(config["timezone"]))
    except ValueError:
        config["timezone"] = DEFAULT_TIMEZONE
    for key in ("night_text", "day_text", "manual_text"):
        config[key] = str(config.get(key) or defaults[key]).strip()[:128] or defaults[key]
    if config["night_text"] == LEGACY_NIGHT_TEXT:
        config["night_text"] = DEFAULT_NIGHT_TEXT
    config["manual_url"] = str(config.get("manual_url") or "").strip()[:500]
    if config["external_mode"] not in EXTERNAL_MODE_LABELS:
        config["external_mode"] = ""
    try:
        config["external_guild_id"] = int(config["external_guild_id"] or 0) or None
    except (TypeError, ValueError):
        config["external_guild_id"] = None
    return config


def is_night_period(current: time, night_start: time, day_start: time) -> bool:
    if night_start == day_start:
        return True
    if night_start > day_start:
        return current >= night_start or current < day_start
    return night_start <= current < day_start


@dataclass(frozen=True, slots=True)
class EffectivePresence:
    status: str
    activity_type: str
    text: str
    url: str
    source: str
    local_now: datetime

    def to_override(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "status": self.status,
            "activity_type": self.activity_type,
            "text": self.text,
            "url": self.url,
            "source": self.source,
        }


def resolve_presence(config_value: Any, now: datetime | None = None) -> EffectivePresence | None:
    config = normalize_presence_config(config_value)
    if config["mode"] == "disabled":
        return None
    zone = ZoneInfo(config["timezone"])
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_now = current.astimezone(zone)
    if config["mode"] == "manual":
        return EffectivePresence(
            status=config["manual_status"],
            activity_type=config["manual_activity_type"],
            text=config["manual_text"],
            url=config["manual_url"],
            source="manual",
            local_now=local_now,
        )
    night = is_night_period(
        local_now.timetz().replace(tzinfo=None),
        _parse_clock(config["night_start"], "21:00"),
        _parse_clock(config["day_start"], "07:00"),
    )
    prefix = "night" if night else "day"
    return EffectivePresence(
        status=config[f"{prefix}_status"],
        activity_type=config[f"{prefix}_activity_type"],
        text=config[f"{prefix}_text"],
        url="",
        source="automatic-night" if night else "automatic-day",
        local_now=local_now,
    )


def next_transition(config_value: Any, now: datetime | None = None) -> datetime:
    config = normalize_presence_config(config_value)
    zone = ZoneInfo(config["timezone"])
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_now = current.astimezone(zone)
    candidates: list[datetime] = []
    for offset in (0, 1, 2):
        target_date = (local_now + timedelta(days=offset)).date()
        for value in (config["night_start"], config["day_start"]):
            candidate = datetime.combine(target_date, _parse_clock(value, "00:00"), zone)
            if candidate > local_now:
                candidates.append(candidate)
    return min(candidates)


def _discord_status(value: str) -> discord.Status:
    return {
        "online": discord.Status.online,
        "idle": discord.Status.idle,
        "dnd": discord.Status.dnd,
        "invisible": discord.Status.invisible,
    }.get(value, discord.Status.online)


def _discord_activity(presence: EffectivePresence) -> discord.BaseActivity:
    text = presence.text[:128]
    if presence.activity_type == "custom":
        return discord.CustomActivity(name=text)
    if presence.activity_type == "playing":
        return discord.Game(name=text)
    if presence.activity_type == "streaming" and presence.url.startswith("https://"):
        return discord.Streaming(name=text, url=presence.url)
    activity_type = {
        "watching": discord.ActivityType.watching,
        "listening": discord.ActivityType.listening,
        "competing": discord.ActivityType.competing,
    }.get(presence.activity_type, discord.ActivityType.playing)
    return discord.Activity(type=activity_type, name=text)


class PresenceControlManager:
    def __init__(
        self,
        bot: discord.Client,
        state: StateStore,
        *,
        authorize: Callable[[discord.Member], bool],
    ) -> None:
        self.bot = bot
        self.state = state
        self.authorize = authorize
        self._last_signature: str | None = None
        self._external_action: Callable[[discord.Interaction, str], Awaitable[None]] | None = None
        self._external_describer: Callable[[dict[str, Any]], Awaitable[dict[str, str]]] | None = None

    def configure_external_modes(
        self,
        *,
        action: Callable[[discord.Interaction, str], Awaitable[None]],
        describe: Callable[[dict[str, Any]], Awaitable[dict[str, str]]],
    ) -> None:
        self._external_action = action
        self._external_describer = describe

    async def ensure_defaults(self) -> bool:
        stored = await self.state.get(PRESENCE_CONTROL_SECTION)
        if isinstance(stored, dict):
            normalized = normalize_presence_config(stored)
            if normalized != stored:
                await self.state.set(PRESENCE_CONTROL_SECTION, normalized)
            return False
        await self.state.set(PRESENCE_CONTROL_SECTION, default_presence_config())
        return True

    async def config(self) -> dict[str, Any]:
        return normalize_presence_config(await self.state.get(PRESENCE_CONTROL_SECTION))

    async def save(self, config: dict[str, Any], *, actor_id: int | None = None) -> dict[str, Any]:
        normalized = normalize_presence_config(config)
        normalized["updated_by"] = actor_id
        normalized["updated_at"] = datetime.now(timezone.utc).isoformat()
        await self.state.set(PRESENCE_CONTROL_SECTION, normalized)
        self._last_signature = None
        return normalized

    async def set_mode(self, mode: str, *, actor_id: int | None = None) -> dict[str, Any]:
        config = await self.config()
        config["mode"] = mode if mode in {"automatic", "manual", "disabled"} else "automatic"
        if config["mode"] != "disabled":
            config["external_mode"] = ""
            config["external_guild_id"] = None
        return await self.save(config, actor_id=actor_id)

    async def set_external_mode(
        self,
        mode: str,
        *,
        guild_id: int,
        actor_id: int | None = None,
    ) -> dict[str, Any]:
        if mode not in EXTERNAL_MODE_LABELS:
            raise ValueError("Modo de presença externo inválido.")
        config = await self.config()
        config.update(mode="disabled", external_mode=mode, external_guild_id=guild_id)
        return await self.save(config, actor_id=actor_id)

    async def set_manual_status(self, status: str, *, actor_id: int) -> dict[str, Any]:
        config = await self.config()
        config.update(mode="manual", manual_status=status, external_mode="", external_guild_id=None)
        return await self.save(config, actor_id=actor_id)

    async def set_manual_activity_type(self, activity_type: str, *, actor_id: int) -> dict[str, Any]:
        config = await self.config()
        config.update(
            mode="manual",
            manual_activity_type=activity_type,
            external_mode="",
            external_guild_id=None,
        )
        return await self.save(config, actor_id=actor_id)

    async def set_manual_content(
        self,
        text: str,
        url: str,
        *,
        actor_id: int,
    ) -> dict[str, Any]:
        config = await self.config()
        if config["manual_activity_type"] == "streaming" and not url.startswith("https://"):
            raise ValueError("No modo Transmitindo, informe uma URL HTTPS válida.")
        config.update(
            mode="manual",
            manual_text=text.strip()[:128],
            manual_url=url.strip()[:500],
            external_mode="",
            external_guild_id=None,
        )
        return await self.save(config, actor_id=actor_id)

    async def update_schedule(
        self,
        *,
        night_start: str,
        day_start: str,
        timezone_name: str,
        night_text: str,
        day_text: str,
        actor_id: int,
    ) -> dict[str, Any]:
        config = await self.config()
        config.update(
            mode="automatic",
            night_start=validate_clock(night_start),
            day_start=validate_clock(day_start),
            timezone=validate_timezone(timezone_name),
            night_text=night_text.strip()[:128] or DEFAULT_NIGHT_TEXT,
            day_text=day_text.strip()[:128] or DEFAULT_DAY_TEXT,
            external_mode="",
            external_guild_id=None,
        )
        return await self.save(config, actor_id=actor_id)

    async def reset_defaults(self, *, actor_id: int | None = None) -> dict[str, Any]:
        return await self.save(default_presence_config(), actor_id=actor_id)

    async def effective(self, now: datetime | None = None) -> EffectivePresence | None:
        return resolve_presence(await self.config(), now)

    async def effective_override(self, now: datetime | None = None) -> dict[str, Any] | None:
        presence = await self.effective(now)
        return presence.to_override() if presence else None

    async def apply(self, *, force: bool = False, now: datetime | None = None) -> bool:
        presence = await self.effective(now)
        if presence is None:
            self._last_signature = None
            return False
        signature = "|".join((presence.status, presence.activity_type, presence.text, presence.url, presence.source))
        if not force and signature == self._last_signature:
            return False
        await self.bot.change_presence(
            status=_discord_status(presence.status),
            activity=_discord_activity(presence),
        )
        self._last_signature = signature
        return True

    async def build_panel_view(self) -> "PresenceControlPanelView":
        config = await self.config()
        external_details: dict[str, str] = {}
        if config["mode"] == "disabled" and self._external_describer is not None:
            try:
                external_details = await self._external_describer(config)
            except Exception:
                LOGGER.exception("Falha ao descrever o modo externo da presença.")
        return PresenceControlPanelView(self, config, external_details=external_details)

    def persistent_view(self) -> "PresenceControlPanelView":
        return PresenceControlPanelView(self, default_presence_config())

    async def activate_external(self, interaction: discord.Interaction, mode: str) -> None:
        if self._external_action is None:
            await interaction.response.send_message(
                "Este modo ainda não está disponível nesta inicialização.",
                ephemeral=True,
            )
            return
        await self._external_action(interaction, mode)

    async def publish_panel(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        *,
        force_recreate: bool = False,
    ) -> tuple[discord.Message, bool]:
        record = await self.state.get(PRESENCE_PANEL_SECTION, {})
        existing: discord.Message | None = None
        if isinstance(record, dict) and int(record.get("channel_id") or 0) == channel.id:
            message_id = int(record.get("message_id") or 0)
            if message_id:
                try:
                    existing = await channel.fetch_message(message_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    existing = None
        if existing is not None and not force_recreate:
            await existing.edit(view=await self.build_panel_view())
            return existing, False
        if existing is not None and force_recreate:
            try:
                await existing.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        message = await channel.send(view=await self.build_panel_view())
        await self.state.set(
            PRESENCE_PANEL_SECTION,
            {
                "guild_id": guild.id,
                "channel_id": channel.id,
                "message_id": message.id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return message, True

    async def refresh_stored_panel(self) -> bool:
        record = await self.state.get(PRESENCE_PANEL_SECTION, {})
        if not isinstance(record, dict):
            return False
        guild = self.bot.get_guild(int(record.get("guild_id") or 0))
        if guild is None:
            return False
        channel = guild.get_channel(int(record.get("channel_id") or 0))
        if not isinstance(channel, discord.TextChannel):
            return False
        try:
            message = await channel.fetch_message(int(record.get("message_id") or 0))
            await message.edit(view=await self.build_panel_view())
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False


class PresenceControlPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        manager: PresenceControlManager,
        config: dict[str, Any],
        *,
        external_details: dict[str, str] | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.manager = manager
        self.config = normalize_presence_config(config)
        self.external_details = external_details or {}
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = isinstance(interaction.user, discord.Member) and self.manager.authorize(interaction.user)
        if not allowed:
            await interaction.response.send_message(
                "Você não possui permissão para gerenciar a presença da Bella.",
                ephemeral=True,
            )
        return allowed

    def _render(self) -> None:
        effective = resolve_presence(self.config)
        mode_label = {
            "automatic": "Agenda automática",
            "manual": "Controle manual",
            "disabled": "Pausado para outro modo",
        }[self.config["mode"]]
        if effective is None:
            external_mode = self.config["external_mode"]
            external_label = EXTERNAL_MODE_LABELS.get(external_mode, "Outro sistema")
            current = self.external_details.get(
                "current",
                f"**{external_label}** está controlando a presença da Bella.",
            )
            transition = "sem transição programada"
        else:
            current = (
                f"{STATUS_EMOJIS[effective.status]} **{STATUS_LABELS[effective.status]}**  •  "
                f"**{ACTIVITY_LABELS[effective.activity_type]}**\n{effective.text}"
            )
            if self.config["mode"] == "automatic":
                transition_at = next_transition(self.config)
                transition = f"<t:{int(transition_at.timestamp())}:R>"
            else:
                transition = "pausada no modo manual"
        container = discord.ui.Container(accent_color=discord.Color(PANEL_COLOR))
        container.add_item(
            discord.ui.TextDisplay(
                "# Central de Presença\n"
                "Gerencie o estado e a atividade da Bella sem precisar reiniciar o bot."
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(
            discord.ui.TextDisplay(
                f"### Agora\n{current}\n"
                f"**Modo:** {mode_label}  •  **Próxima troca:** {transition}"
            )
        )
        container.add_item(
            discord.ui.TextDisplay(
                "### Agenda diária\n"
                f"🌙 **{self.config['night_start']}–{self.config['day_start']}**  •  Ausente\n"
                f"{self.config['night_text']}\n"
                f"☀️ **{self.config['day_start']}–{self.config['night_start']}**  •  Não perturbe\n"
                f"{self.config['day_text']}\n"
                f"-# Fuso horário: {self.config['timezone']}"
            )
        )

        status_select = discord.ui.Select(
            custom_id="bella:presence:status:v2",
            placeholder="Definir estado manual",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=label,
                    value=value,
                    emoji=STATUS_EMOJIS[value],
                    default=self.config["mode"] == "manual" and self.config["manual_status"] == value,
                )
                for value, label in STATUS_LABELS.items()
            ],
        )
        status_select.callback = self._select_status
        container.add_item(discord.ui.ActionRow(status_select))

        activity_select = discord.ui.Select(
            custom_id="bella:presence:activity:v2",
            placeholder="Definir tipo de atividade manual",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=label,
                    value=value,
                    default=self.config["mode"] == "manual" and self.config["manual_activity_type"] == value,
                )
                for value, label in ACTIVITY_LABELS.items()
            ],
        )
        activity_select.callback = self._select_activity
        container.add_item(discord.ui.ActionRow(activity_select))

        systems = discord.ui.ActionRow()
        system_buttons = (
            ("Agenda", discord.ButtonStyle.success, "automatic"),
            ("NEFFEX", discord.ButtonStyle.secondary, "neffex"),
            ("Hospedagem", discord.ButtonStyle.secondary, "squarecloud"),
            ("Dinâmico", discord.ButtonStyle.secondary, "dynamic"),
            ("Streamer", discord.ButtonStyle.secondary, "streamer"),
        )
        for label, style, mode in system_buttons:
            button = discord.ui.Button(
                label=label,
                style=style,
                custom_id=f"bella:presence:system:{mode}:v2",
            )
            button.callback = self._external_callback(mode)
            systems.add_item(button)
        container.add_item(systems)

        actions = discord.ui.ActionRow()
        edit_text = discord.ui.Button(
            label="Criar personalizado",
            style=discord.ButtonStyle.primary,
            custom_id="bella:presence:edit-activity:v2",
        )
        edit_schedule = discord.ui.Button(
            label="Editar agenda",
            style=discord.ButtonStyle.primary,
            custom_id="bella:presence:edit-schedule:v2",
        )
        apply_now = discord.ui.Button(
            label="Aplicar agora",
            style=discord.ButtonStyle.secondary,
            custom_id="bella:presence:apply:v2",
        )
        reset = discord.ui.Button(
            label="Restaurar padrão",
            style=discord.ButtonStyle.danger,
            custom_id="bella:presence:reset:v2",
        )
        edit_text.callback = self._edit_activity
        edit_schedule.callback = self._edit_schedule
        apply_now.callback = self._apply_now
        reset.callback = self._reset
        for item in (edit_text, edit_schedule, apply_now, reset):
            actions.add_item(item)
        container.add_item(actions)
        container.add_item(
            discord.ui.TextDisplay(
                "-# Os modos são exclusivos. Alterações manuais pausam os sistemas automáticos até você escolher outro modo."
            )
        )
        self.add_item(container)

    def _external_callback(
        self,
        mode: str,
    ) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def callback(interaction: discord.Interaction) -> None:
            await self.manager.activate_external(interaction, mode)

        return callback

    async def _finish(self, interaction: discord.Interaction, message: str) -> None:
        try:
            await self.manager.apply(force=True)
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"A configuração foi salva, mas o Discord recusou a presença: {exc}",
                ephemeral=True,
            )
            return
        view = await self.manager.build_panel_view()
        await interaction.response.edit_message(view=view)
        await interaction.followup.send(message, ephemeral=True)

    async def _select_status(self, interaction: discord.Interaction) -> None:
        selected = interaction.data.get("values", ["dnd"])[0] if interaction.data else "dnd"
        await self.manager.set_manual_status(str(selected), actor_id=interaction.user.id)
        await self._finish(interaction, "Estado manual aplicado.")

    async def _select_activity(self, interaction: discord.Interaction) -> None:
        selected = interaction.data.get("values", ["custom"])[0] if interaction.data else "custom"
        config = await self.manager.set_manual_activity_type(str(selected), actor_id=interaction.user.id)
        if selected == "streaming" and not config["manual_url"].startswith("https://"):
            await interaction.response.send_modal(PresenceActivityModal(self.manager, config))
            return
        await self._finish(interaction, "Tipo de atividade manual aplicado.")

    async def _edit_activity(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PresenceActivityModal(self.manager, self.config))

    async def _edit_schedule(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PresenceScheduleModal(self.manager, self.config))

    async def _apply_now(self, interaction: discord.Interaction) -> None:
        await self._finish(interaction, "Presença reaplicada no Discord.")

    async def _reset(self, interaction: discord.Interaction) -> None:
        if self.manager._external_action is not None:
            await self.manager.activate_external(interaction, "reset")
            return
        await self.manager.reset_defaults(actor_id=interaction.user.id)
        await self._finish(interaction, "Padrão diário restaurado.")


class PresenceActivityModal(discord.ui.Modal, title="Atividade manual da Bella"):
    def __init__(self, manager: PresenceControlManager, config: dict[str, Any]) -> None:
        super().__init__()
        self.manager = manager
        self.text = discord.ui.TextInput(
            label="Texto da atividade",
            default=str(config["manual_text"])[:128],
            max_length=128,
        )
        self.url = discord.ui.TextInput(
            label="URL HTTPS (somente Transmitindo)",
            default=str(config["manual_url"])[:500],
            required=False,
            max_length=500,
        )
        self.add_item(self.text)
        self.add_item(self.url)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            await self.manager.set_manual_content(
                str(self.text),
                str(self.url),
                actor_id=interaction.user.id,
            )
            await self.manager.apply(force=True)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"A configuração foi salva, mas o Discord recusou a presença: {exc}",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(view=await self.manager.build_panel_view())
        await interaction.followup.send("Texto da atividade atualizado.", ephemeral=True)


class PresenceScheduleModal(discord.ui.Modal, title="Agenda automática da Bella"):
    def __init__(self, manager: PresenceControlManager, config: dict[str, Any]) -> None:
        super().__init__()
        self.manager = manager
        self.night_start = discord.ui.TextInput(
            label="Início do descanso (HH:MM)", default=config["night_start"], max_length=5
        )
        self.day_start = discord.ui.TextInput(
            label="Retorno diário (HH:MM)", default=config["day_start"], max_length=5
        )
        self.timezone_name = discord.ui.TextInput(
            label="Fuso horário", default=config["timezone"], max_length=64
        )
        self.night_text = discord.ui.TextInput(
            label="Texto durante o descanso", default=config["night_text"], max_length=128
        )
        self.day_text = discord.ui.TextInput(
            label="Texto durante o dia", default=config["day_text"], max_length=128
        )
        for item in (
            self.night_start,
            self.day_start,
            self.timezone_name,
            self.night_text,
            self.day_text,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            await self.manager.update_schedule(
                night_start=str(self.night_start),
                day_start=str(self.day_start),
                timezone_name=str(self.timezone_name),
                night_text=str(self.night_text),
                day_text=str(self.day_text),
                actor_id=interaction.user.id,
            )
            await self.manager.apply(force=True)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"A agenda foi salva, mas o Discord recusou a presença: {exc}",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(view=await self.manager.build_panel_view())
        await interaction.followup.send("Agenda atualizada e ativada.", ephemeral=True)
