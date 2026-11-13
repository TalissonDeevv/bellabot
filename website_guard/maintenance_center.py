from __future__ import annotations

import asyncio
import logging
import os
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import discord

from .central_logs import CentralLogService, LogModuleDefinition
from .presence_control import PresenceControlManager
from .state import StateStore

LOGGER = logging.getLogger(__name__)

MAINTENANCE_SECTION = "global_maintenance"
MAINTENANCE_PANEL_SECTION = "global_maintenance_panel"
DEFAULT_PANEL_CHANNEL_ID = 1561342090238234654
PANEL_COLOR = 0xD88728

MODE_PRESETS: dict[str, tuple[str, str]] = {
    "maintenance": (
        "Manutenção em andamento",
        "A Bella está passando por uma manutenção programada. Os sistemas públicos ficam temporariamente indisponíveis para que tudo volte a funcionar com segurança.",
    ),
    "improvements": (
        "Melhorias em andamento",
        "A Bella está recebendo melhorias. Durante esse período, os sistemas públicos ficam pausados para evitar operações incompletas.",
    ),
    "update": (
        "Atualização em andamento",
        "Uma atualização da Bella está sendo aplicada. Os recursos públicos voltarão assim que a verificação terminar.",
    ),
    "incident": (
        "Manutenção emergencial",
        "A Bella identificou uma condição que exige atenção técnica. Os sistemas públicos foram pausados preventivamente.",
    ),
}

_controller: "MaintenanceCenter | None" = None


def configure_public_maintenance_guard(controller: "MaintenanceCenter") -> None:
    global _controller
    _controller = controller


async def guard_public_interaction(
    interaction: discord.Interaction,
    system: str,
) -> bool:
    if _controller is None:
        return False
    return await _controller.block_interaction(interaction, system=system)


def default_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "mode": "maintenance",
        "title": MODE_PRESETS["maintenance"][0],
        "message": MODE_PRESETS["maintenance"][1],
        "status": "dnd",
        "activity_type": "watching",
        "activity_text": "Manutenção e melhorias em andamento",
        "estimated_return": "",
        "started_at": "",
        "started_by": None,
        "updated_at": "",
        "updated_by": None,
        "previous_presence": None,
        "block_public_systems": True,
        "block_public_chat": True,
    }


def normalize_config(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    mode = str(raw.get("mode") or "maintenance")
    if mode not in {*MODE_PRESETS, "custom"}:
        mode = "maintenance"
    preset = MODE_PRESETS.get(mode, MODE_PRESETS["maintenance"])
    status = str(raw.get("status") or "dnd")
    if status not in {"online", "idle", "dnd", "invisible"}:
        status = "dnd"
    activity_type = str(raw.get("activity_type") or "watching")
    if activity_type not in {"playing", "watching", "listening", "competing", "custom"}:
        activity_type = "watching"
    previous = raw.get("previous_presence")
    return {
        "enabled": bool(raw.get("enabled", False)),
        "mode": mode,
        "title": str(raw.get("title") or preset[0]).strip()[:120],
        "message": str(raw.get("message") or preset[1]).strip()[:1800],
        "status": status,
        "activity_type": activity_type,
        "activity_text": str(raw.get("activity_text") or "Manutenção e melhorias em andamento").strip()[:128],
        "estimated_return": str(raw.get("estimated_return") or "").strip()[:120],
        "started_at": str(raw.get("started_at") or ""),
        "started_by": _optional_int(raw.get("started_by")),
        "updated_at": str(raw.get("updated_at") or ""),
        "updated_by": _optional_int(raw.get("updated_by")),
        "previous_presence": deepcopy(previous) if isinstance(previous, dict) else None,
        "block_public_systems": bool(raw.get("block_public_systems", True)),
        "block_public_chat": bool(raw.get("block_public_chat", True)),
    }


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class MaintenanceCenter:
    def __init__(
        self,
        bot: discord.Client,
        state: StateStore,
        presence: PresenceControlManager,
        logs: CentralLogService,
        *,
        authorize: Callable[[discord.Member], bool],
    ) -> None:
        self.bot = bot
        self.state = state
        self.presence = presence
        self.logs = logs
        self.authorize = authorize
        self._lock = asyncio.Lock()
        self._blocked_log_at: dict[tuple[int, int, str], float] = {}

    async def config(self) -> dict[str, Any]:
        return normalize_config(await self.state.get(MAINTENANCE_SECTION, {}))

    async def is_active(self) -> bool:
        return bool((await self.config())["enabled"])

    async def activate(
        self,
        *,
        mode: str,
        actor_id: int,
        title: str | None = None,
        message: str | None = None,
        estimated_return: str = "",
        status: str = "dnd",
        activity_type: str = "watching",
        activity_text: str = "Manutenção e melhorias em andamento",
    ) -> dict[str, Any]:
        async with self._lock:
            current = await self.config()
            previous_presence = current.get("previous_presence")
            if not current["enabled"] or not isinstance(previous_presence, dict):
                previous_presence = await self.presence.config()
            preset = MODE_PRESETS.get(mode, MODE_PRESETS["maintenance"])
            now = datetime.now(timezone.utc).isoformat()
            updated = normalize_config(
                {
                    **current,
                    "enabled": True,
                    "mode": mode if mode in {*MODE_PRESETS, "custom"} else "maintenance",
                    "title": title or preset[0],
                    "message": message or preset[1],
                    "estimated_return": estimated_return,
                    "status": status,
                    "activity_type": activity_type,
                    "activity_text": activity_text,
                    "started_at": current["started_at"] if current["enabled"] else now,
                    "started_by": current["started_by"] if current["enabled"] else actor_id,
                    "updated_at": now,
                    "updated_by": actor_id,
                    "previous_presence": previous_presence,
                    "block_public_systems": True,
                    "block_public_chat": True,
                }
            )
            await self.state.set(MAINTENANCE_SECTION, updated)
            await self._apply_maintenance_presence(updated, actor_id=actor_id)
            return updated

    async def deactivate(self, *, actor_id: int) -> dict[str, Any]:
        async with self._lock:
            current = await self.config()
            previous = current.get("previous_presence")
            now = datetime.now(timezone.utc).isoformat()
            updated = normalize_config(
                {
                    **current,
                    "enabled": False,
                    "updated_at": now,
                    "updated_by": actor_id,
                    "previous_presence": None,
                }
            )
            await self.state.set(MAINTENANCE_SECTION, updated)
            if isinstance(previous, dict):
                await self.presence.save(previous, actor_id=actor_id)
            else:
                await self.presence.reset_defaults(actor_id=actor_id)
            await self.presence.apply(force=True)
            return updated

    async def reapply(self) -> None:
        config = await self.config()
        if config["enabled"]:
            await self._apply_maintenance_presence(config, actor_id=config["updated_by"] or 0)

    async def _apply_maintenance_presence(self, config: dict[str, Any], *, actor_id: int) -> None:
        presence = await self.presence.config()
        presence.update(
            mode="manual",
            manual_status=config["status"],
            manual_activity_type=config["activity_type"],
            manual_text=config["activity_text"],
            manual_url="",
            external_mode="",
            external_guild_id=None,
        )
        await self.presence.save(presence, actor_id=actor_id)
        await self.presence.apply(force=True)

    async def block_interaction(self, interaction: discord.Interaction, *, system: str) -> bool:
        config = await self.config()
        if not config["enabled"] or not config["block_public_systems"]:
            return False
        content = self.public_notice(config)
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
        await self._log_blocked(interaction, system)
        return True

    async def reply_to_message(self, message: discord.Message) -> bool:
        config = await self.config()
        if not config["enabled"] or not config["block_public_chat"]:
            return False
        await message.reply(
            self.public_notice(config),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    def public_notice(self, config: dict[str, Any]) -> str:
        estimate = (
            f"\n**Previsão informada:** {config['estimated_return']}"
            if config["estimated_return"]
            else ""
        )
        return (
            f"## 🛠️ {config['title']}\n"
            f"{config['message']}{estimate}\n\n"
            "Tente novamente quando a manutenção for concluída."
        )

    async def _log_blocked(self, interaction: discord.Interaction, system: str) -> None:
        if interaction.guild is None:
            return
        loop_time = asyncio.get_running_loop().time()
        key = (interaction.guild.id, interaction.user.id, system)
        if loop_time - self._blocked_log_at.get(key, 0.0) < 300:
            return
        self._blocked_log_at[key] = loop_time
        await self.logs.send_event(
            interaction.guild,
            "maintenance",
            title="Acesso público bloqueado",
            description=f"Um acesso ao sistema **{system}** foi bloqueado pelo modo de manutenção.",
            severity="INFO",
            fields=(("Usuário", f"<@{interaction.user.id}> (`{interaction.user.id}`)", True),),
        )

    def persistent_view(self) -> "MaintenanceAdminPanelView":
        return MaintenanceAdminPanelView(self, default_config())

    async def build_panel_view(self) -> "MaintenanceAdminPanelView":
        return MaintenanceAdminPanelView(self, await self.config())

    async def publish_fixed_panel(self, guild: discord.Guild) -> tuple[discord.Message, bool]:
        channel_id = _optional_int(os.getenv("MAINTENANCE_PANEL_CHANNEL_ID")) or DEFAULT_PANEL_CHANNEL_ID
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(f"Canal fixo de manutenção não encontrado: {channel_id}.")
        record = await self.state.get(MAINTENANCE_PANEL_SECTION, {})
        existing: discord.Message | None = None
        if isinstance(record, dict) and int(record.get("channel_id") or 0) == channel.id:
            message_id = int(record.get("message_id") or 0)
            if message_id:
                try:
                    existing = await channel.fetch_message(message_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    existing = None
        view = await self.build_panel_view()
        if existing is not None:
            await existing.edit(view=view)
            return existing, False
        message = await channel.send(view=view)
        await self.state.set(
            MAINTENANCE_PANEL_SECTION,
            {
                "guild_id": guild.id,
                "channel_id": channel.id,
                "message_id": message.id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return message, True

    async def refresh_panel(self) -> bool:
        record = await self.state.get(MAINTENANCE_PANEL_SECTION, {})
        if not isinstance(record, dict):
            return False
        channel = self.bot.get_channel(int(record.get("channel_id") or 0))
        if not isinstance(channel, discord.TextChannel):
            return False
        try:
            message = await channel.fetch_message(int(record.get("message_id") or 0))
            await message.edit(view=await self.build_panel_view())
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False

    async def ensure_log_channel(self, guild: discord.Guild) -> discord.TextChannel:
        return await self.logs.ensure_log_channel(
            guild,
            LogModuleDefinition(
                "maintenance",
                "Manutenção",
                "manutencao",
                "🛠️",
                ("início", "alteração", "bloqueio", "conclusão"),
                True,
                True,
            ),
            reason="Configuração da Central de Manutenção da Bella",
        )

    async def log_state_change(
        self,
        guild: discord.Guild,
        *,
        actor: discord.abc.User,
        enabled: bool,
        config: dict[str, Any],
    ) -> None:
        await self.logs.send_event(
            guild,
            "maintenance",
            title="Manutenção iniciada" if enabled else "Manutenção concluída",
            description=(
                f"**{config['title']}**\n{config['message']}"
                if enabled
                else "Os sistemas públicos e as conversas com a Bella foram liberados novamente."
            ),
            severity="HIGH" if enabled else "INFO",
            fields=(("Responsável", f"<@{actor.id}> (`{actor.id}`)", True),),
            critical=enabled and config["mode"] == "incident",
        )


class MaintenanceAdminPanelView(discord.ui.LayoutView):
    def __init__(self, center: MaintenanceCenter, config: dict[str, Any]) -> None:
        super().__init__(timeout=None)
        self.center = center
        self.config = normalize_config(config)
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = isinstance(interaction.user, discord.Member) and self.center.authorize(interaction.user)
        if not allowed:
            await interaction.response.send_message(
                "Você não possui permissão para gerenciar a manutenção da Bella.",
                ephemeral=True,
            )
        return allowed

    def _render(self) -> None:
        active = self.config["enabled"]
        state = "🟠 **EM MANUTENÇÃO**" if active else "🟢 **OPERACIONAL**"
        started = self.config["started_at"]
        started_line = ""
        if active and started:
            try:
                timestamp = int(datetime.fromisoformat(started).timestamp())
                started_line = f"\n**Iniciada:** <t:{timestamp}:F> • <t:{timestamp}:R>"
            except ValueError:
                started_line = ""
        container = discord.ui.Container(accent_color=discord.Color(PANEL_COLOR))
        container.add_item(
            discord.ui.TextDisplay(
                "# Central de Manutenção\n"
                "Controle a disponibilidade pública da Bella sem desligar o bot."
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(
            discord.ui.TextDisplay(
                f"### Estado atual\n{state}{started_line}\n"
                f"**Motivo:** {self.config['title']}\n"
                f"**Sistemas públicos:** {'bloqueados' if active else 'liberados'} • "
                f"**Conversas:** {'bloqueadas' if active else 'liberadas'}"
            )
        )
        if active:
            estimate = self.config["estimated_return"] or "sem previsão informada"
            container.add_item(
                discord.ui.TextDisplay(
                    f"### Aviso público\n{self.config['message']}\n-# Previsão: {estimate}"
                )
            )

        status_select = discord.ui.Select(
            custom_id="bella:maintenance:status:v1",
            placeholder="Alterar status durante a manutenção",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Online", value="online", emoji="🟢", default=self.config["status"] == "online"),
                discord.SelectOption(label="Ausente", value="idle", emoji="🌙", default=self.config["status"] == "idle"),
                discord.SelectOption(label="Não perturbe", value="dnd", emoji="⛔", default=self.config["status"] == "dnd"),
                discord.SelectOption(label="Invisível", value="invisible", emoji="⚫", default=self.config["status"] == "invisible"),
            ],
        )
        status_select.callback = self._select_status
        container.add_item(discord.ui.ActionRow(status_select))

        quick = discord.ui.ActionRow()
        for label, mode, style in (
            ("Manutenção", "maintenance", discord.ButtonStyle.primary),
            ("Melhorias", "improvements", discord.ButtonStyle.primary),
            ("Atualização", "update", discord.ButtonStyle.secondary),
            ("Emergência", "incident", discord.ButtonStyle.danger),
        ):
            button = discord.ui.Button(
                label=label,
                style=style,
                custom_id=f"bella:maintenance:start:{mode}:v1",
            )
            button.callback = self._quick_callback(mode)
            quick.add_item(button)
        container.add_item(quick)

        actions = discord.ui.ActionRow()
        custom = discord.ui.Button(
            label="Personalizar",
            style=discord.ButtonStyle.secondary,
            custom_id="bella:maintenance:custom:v1",
        )
        reapply = discord.ui.Button(
            label="Reaplicar status",
            style=discord.ButtonStyle.secondary,
            custom_id="bella:maintenance:reapply:v1",
        )
        finish = discord.ui.Button(
            label="Finalizar manutenção",
            style=discord.ButtonStyle.success,
            custom_id="bella:maintenance:finish:v1",
            disabled=not active,
        )
        custom.callback = self._custom
        reapply.callback = self._reapply
        finish.callback = self._finish
        for item in (custom, reapply, finish):
            actions.add_item(item)
        container.add_item(actions)
        container.add_item(
            discord.ui.TextDisplay(
                "-# A moderação, os logs e este painel continuam ativos. O estado anterior da presença é restaurado ao finalizar."
            )
        )
        self.add_item(container)

    def _quick_callback(self, mode: str) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def callback(interaction: discord.Interaction) -> None:
            config = await self.center.activate(mode=mode, actor_id=interaction.user.id)
            if interaction.guild is not None:
                await self.center.log_state_change(
                    interaction.guild,
                    actor=interaction.user,
                    enabled=True,
                    config=config,
                )
            await interaction.response.edit_message(view=await self.center.build_panel_view())
            await interaction.followup.send("Modo de manutenção aplicado.", ephemeral=True)

        return callback

    async def _custom(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(MaintenanceConfigModal(self.center, self.config))

    async def _select_status(self, interaction: discord.Interaction) -> None:
        selected = str((interaction.data or {}).get("values", ["dnd"])[0])
        config = await self.center.activate(
            mode=self.config["mode"],
            actor_id=interaction.user.id,
            title=self.config["title"],
            message=self.config["message"],
            estimated_return=self.config["estimated_return"],
            status=selected,
            activity_type=self.config["activity_type"],
            activity_text=self.config["activity_text"],
        )
        if interaction.guild is not None:
            await self.center.log_state_change(
                interaction.guild,
                actor=interaction.user,
                enabled=True,
                config=config,
            )
        await interaction.response.edit_message(view=await self.center.build_panel_view())
        await interaction.followup.send("Status da manutenção atualizado.", ephemeral=True)

    async def _reapply(self, interaction: discord.Interaction) -> None:
        await self.center.reapply()
        await interaction.response.edit_message(view=await self.center.build_panel_view())
        await interaction.followup.send("Status e painel reaplicados.", ephemeral=True)

    async def _finish(self, interaction: discord.Interaction) -> None:
        config = await self.center.deactivate(actor_id=interaction.user.id)
        if interaction.guild is not None:
            await self.center.log_state_change(
                interaction.guild,
                actor=interaction.user,
                enabled=False,
                config=config,
            )
        await interaction.response.edit_message(view=await self.center.build_panel_view())
        await interaction.followup.send("Bella voltou ao modo operacional.", ephemeral=True)


class MaintenanceConfigModal(discord.ui.Modal, title="Configurar manutenção"):
    def __init__(self, center: MaintenanceCenter, config: dict[str, Any]) -> None:
        super().__init__()
        self.center = center
        self.title_input = discord.ui.TextInput(
            label="Título público",
            default=str(config["title"])[:120],
            max_length=120,
        )
        self.message_input = discord.ui.TextInput(
            label="Mensagem pública",
            style=discord.TextStyle.paragraph,
            default=str(config["message"])[:1800],
            max_length=1800,
        )
        self.estimate_input = discord.ui.TextInput(
            label="Previsão (opcional)",
            default=str(config["estimated_return"])[:120],
            required=False,
            max_length=120,
            placeholder="Ex.: hoje às 18h",
        )
        self.activity_input = discord.ui.TextInput(
            label="Texto do status da Bella",
            default=str(config["activity_text"])[:128],
            max_length=128,
        )
        for item in (
            self.title_input,
            self.message_input,
            self.estimate_input,
            self.activity_input,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        config = await self.center.activate(
            mode="custom",
            actor_id=interaction.user.id,
            title=str(self.title_input.value),
            message=str(self.message_input.value),
            estimated_return=str(self.estimate_input.value),
            activity_text=str(self.activity_input.value),
        )
        if interaction.guild is not None:
            await self.center.log_state_change(
                interaction.guild,
                actor=interaction.user,
                enabled=True,
                config=config,
            )
        await interaction.response.edit_message(view=await self.center.build_panel_view())
        await interaction.followup.send("Manutenção personalizada aplicada.", ephemeral=True)
