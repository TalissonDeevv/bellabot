from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable

import discord

from .state import StateStore

LOGGER = logging.getLogger(__name__)

LOG_CONFIG_SECTION = "guild_log_config"
LOG_CHANNELS_SECTION = "guild_log_channels"
PANEL_CHANNELS_SECTION = "guild_log_panel_channels"
LOG_EVENTS_SECTION = "guild_log_events"
LOG_SCHEMA_VERSION = 6
SCHEMA_2_MODULES = {"critical_changes", "panel_repair", "giveaways", "satisfaction"}
SCHEMA_3_MODULES = {"identity", "site_feedbacks"}
SCHEMA_4_MODULES = {"activity"}
SCHEMA_5_MODULES = {"errors"}
SCHEMA_6_MODULES = {"maintenance"}


@dataclass(frozen=True)
class LogModuleDefinition:
    identifier: str
    display_name: str
    default_channel_name: str
    emoji: str
    event_types: tuple[str, ...]
    enabled: bool = True
    alert_capable: bool = False


DEFAULT_LOG_MODULES: tuple[LogModuleDefinition, ...] = (
    LogModuleDefinition("site", "Site", "site", "🌐", ("monitor", "alteração", "falha"), True, True),
    LogModuleDefinition("tickets", "Tickets", "tickets", "🎫", ("abertura", "fechamento", "escalonamento"), True, True),
    LogModuleDefinition("automod", "AutoMod", "automod", "🛡️", ("bloqueio", "regra", "exceção"), True, True),
    LogModuleDefinition("security", "Segurança", "seguranca", "🔐", ("scan", "finding", "mudança"), True, True),
    LogModuleDefinition("critical_changes", "Alterações Críticas", "alteracoes-criticas", "⚠️", ("cargo", "canal", "automod", "webhook"), True, True),
    LogModuleDefinition("panel_repair", "Reparador de Painéis", "reparo-paineis", "🧰", ("apagado", "inconsistente", "recriado", "relink"), True, True),
    LogModuleDefinition("backups", "Backups", "backups", "💾", ("criação", "restauração", "falha"), True, True),
    LogModuleDefinition("giveaways", "Sorteios", "sorteios", "🎁", ("criação", "entrada", "sorteio", "reroll"), True, True),
    LogModuleDefinition("satisfaction", "Satisfação", "satisfacao", "⭐", ("pesquisa", "avaliação", "métrica", "crítica"), True, True),
    LogModuleDefinition("site_feedbacks", "Feedbacks do Site", "feedbacks-site", "💬", ("sincronização", "novo feedback", "falha"), True, True),
    LogModuleDefinition("identity", "Identidades", "identidades", "👤", ("nickname", "biografia", "isenção"), True, True),
    LogModuleDefinition("activity", "Atividade", "atividade", "📋", ("entrada", "saída", "primeira mensagem", "voz", "comando", "cargo", "canal", "mensagem"), True, True),
    LogModuleDefinition("errors", "Erros", "erros", "❌", ("comando", "evento", "worker", "integração"), True, True),
    LogModuleDefinition("maintenance", "Manutenção", "manutencao", "🛠️", ("início", "alteração", "bloqueio", "conclusão"), True, True),
    LogModuleDefinition("training", "Treinamentos", "treinamentos", "🎓", ("aula", "progresso", "administração")),
    LogModuleDefinition("partners", "Parcerias", "parcerias", "🤝", ("inscrição", "aprovação", "pagamento", "relatório")),
    LogModuleDefinition("letters", "Cartas", "cartas", "📨", ("envio", "falha", "administração")),
    LogModuleDefinition("shopping", "Achadinhos", "achadinhos", "🛍️", ("produto", "painel", "publicação")),
    LogModuleDefinition("hosting", "Hospedagem", "hospedagem", "☁️", ("deploy", "health", "latência", "falha"), True, True),
    LogModuleDefinition("bella", "Bella", "bella", "🤖", ("comando", "IA", "administração"), True, True),
    LogModuleDefinition("alerts", "Alertas", "alertas", "🚨", ("crítico", "atenção imediata"), True, True),
    LogModuleDefinition("system", "Sistema", "sistema", "⚙️", ("inicialização", "job", "erro interno"), True, True),
)


@dataclass(frozen=True)
class PanelChannelDefinition:
    identifier: str
    display_name: str
    default_channel_name: str
    emoji: str


DEFAULT_PANEL_CHANNELS: tuple[PanelChannelDefinition, ...] = (
    PanelChannelDefinition("backup_admin", "Central de Backups", "backups-painel", "💾"),
)


@dataclass
class GuildLogConfig:
    guild_id: int
    category_name: str = "BELLA • LOGS"
    category_id: int | None = None
    use_emojis: bool = True
    divider: str = "╺╸"
    default_emoji: str = ""
    viewer_role_ids: list[int] = field(default_factory=list)
    enabled_modules: list[str] = field(
        default_factory=lambda: [item.identifier for item in DEFAULT_LOG_MODULES if item.enabled]
    )
    alert_module_id: str = "alerts"
    schema_version: int = LOG_SCHEMA_VERSION
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, guild_id: int, value: Any) -> "GuildLogConfig":
        data = value if isinstance(value, dict) else {}
        valid_modules = {item.identifier for item in DEFAULT_LOG_MODULES}
        raw_modules = data.get("enabled_modules")
        modules = [
            str(item)
            for item in (raw_modules if isinstance(raw_modules, list) else [])
            if str(item) in valid_modules
        ]
        if not modules:
            modules = [item.identifier for item in DEFAULT_LOG_MODULES if item.enabled]
        elif int(data.get("schema_version") or 1) < 2:
            modules.extend(
                item.identifier
                for item in DEFAULT_LOG_MODULES
                if item.identifier in SCHEMA_2_MODULES and item.identifier not in modules
            )
        if int(data.get("schema_version") or 1) < 3:
            modules.extend(
                item.identifier
                for item in DEFAULT_LOG_MODULES
                if item.identifier in SCHEMA_3_MODULES and item.identifier not in modules
            )
        if int(data.get("schema_version") or 1) < 4:
            modules.extend(
                item.identifier
                for item in DEFAULT_LOG_MODULES
                if item.identifier in SCHEMA_4_MODULES and item.identifier not in modules
            )
        if int(data.get("schema_version") or 1) < 5:
            modules.extend(
                item.identifier
                for item in DEFAULT_LOG_MODULES
                if item.identifier in SCHEMA_5_MODULES and item.identifier not in modules
            )
        if int(data.get("schema_version") or 1) < 6:
            modules.extend(
                item.identifier
                for item in DEFAULT_LOG_MODULES
                if item.identifier in SCHEMA_6_MODULES and item.identifier not in modules
            )
        return cls(
            guild_id=guild_id,
            category_name=_clean_category_name(data.get("category_name") or "BELLA • LOGS"),
            category_id=_optional_int(data.get("category_id")),
            use_emojis=bool(data.get("use_emojis", True)),
            divider=_clean_divider(data.get("divider") or "╺╸"),
            default_emoji=str(data.get("default_emoji") or "").strip()[:32],
            viewer_role_ids=sorted(set(_int_values(data.get("viewer_role_ids", [])))),
            enabled_modules=modules,
            alert_module_id=(
                str(data.get("alert_module_id") or "alerts")
                if str(data.get("alert_module_id") or "alerts") in valid_modules
                else "alerts"
            ),
            schema_version=LOG_SCHEMA_VERSION,
            updated_at=str(data.get("updated_at") or ""),
        )


@dataclass(frozen=True)
class LogProvisionResult:
    category_id: int
    created_category: bool
    created_channels: tuple[str, ...]
    reused_channels: tuple[str, ...]
    missing_permissions: tuple[str, ...]
    channel_ids: dict[str, int]
    panel_channel_ids: dict[str, int] = field(default_factory=dict)
    created_panel_channels: tuple[str, ...] = ()
    reused_panel_channels: tuple[str, ...] = ()


class LogModuleRegistry:
    def __init__(self, definitions: Iterable[LogModuleDefinition] = DEFAULT_LOG_MODULES) -> None:
        self._definitions = {item.identifier: item for item in definitions}

    def all(self) -> tuple[LogModuleDefinition, ...]:
        return tuple(self._definitions.values())

    def get(self, identifier: str) -> LogModuleDefinition | None:
        return self._definitions.get(identifier)

    def enabled(self, identifiers: Iterable[str]) -> tuple[LogModuleDefinition, ...]:
        selected = set(identifiers)
        return tuple(item for item in self._definitions.values() if item.identifier in selected)


BindingCallback = Callable[[discord.Guild, dict[str, int]], Awaitable[None]]
PanelBindingCallback = Callable[[discord.Guild, dict[str, int]], Awaitable[None]]
AuthorizationCallback = Callable[[discord.Member], bool]


class CentralLogService:
    def __init__(
        self,
        state: StateStore,
        *,
        registry: LogModuleRegistry | None = None,
        binding_callback: BindingCallback | None = None,
        panel_binding_callback: PanelBindingCallback | None = None,
    ) -> None:
        self.state = state
        self.registry = registry or LogModuleRegistry()
        self.binding_callback = binding_callback
        self.panel_binding_callback = panel_binding_callback
        self._locks: dict[int, asyncio.Lock] = {}

    def set_binding_callback(self, callback: BindingCallback) -> None:
        self.binding_callback = callback

    def set_panel_binding_callback(self, callback: PanelBindingCallback) -> None:
        self.panel_binding_callback = callback

    async def config(self, guild_id: int) -> GuildLogConfig:
        values = await self.state.get_section(LOG_CONFIG_SECTION)
        return GuildLogConfig.from_dict(guild_id, values.get(str(guild_id)))

    async def save_config(self, config: GuildLogConfig) -> None:
        config.updated_at = datetime.now(timezone.utc).isoformat()
        await self.state.update_section(LOG_CONFIG_SECTION, str(config.guild_id), config.to_dict())

    async def channel_ids(self, guild_id: int) -> dict[str, int]:
        values = await self.state.get_section(LOG_CHANNELS_SECTION)
        raw = values.get(str(guild_id), {})
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): int(value)
            for key, value in raw.items()
            if str(value).isdigit()
        }

    async def panel_channel_ids(self, guild_id: int) -> dict[str, int]:
        values = await self.state.get_section(PANEL_CHANNELS_SECTION)
        raw = values.get(str(guild_id), {})
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): int(value)
            for key, value in raw.items()
            if str(value).isdigit()
        }

    async def ensure_panel_channel(
        self,
        guild: discord.Guild,
        definition: PanelChannelDefinition,
        *,
        topic: str,
        reason: str,
    ) -> discord.TextChannel:
        lock = self._locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            bot_member = guild.me
            if bot_member is None or not bot_member.guild_permissions.manage_channels:
                raise PermissionError(
                    "A Bella precisa da permissão Gerenciar Canais para criar o canal de Analytics."
                )
            config = await self.config(guild.id)
            category, _ = await self._resolve_category(guild, config, reason=reason)
            stored = await self.panel_channel_ids(guild.id)
            channel = _stored_text_channel(guild, stored.get(definition.identifier))
            expected_name = panel_channel_name(definition, config)
            if channel is None:
                channel = discord.utils.find(
                    lambda item: item.name == expected_name,
                    category.text_channels,
                )
            if channel is None:
                channel = await guild.create_text_channel(
                    expected_name,
                    category=category,
                    topic=topic[:1024],
                    reason=reason,
                )
            else:
                await channel.edit(
                    name=expected_name,
                    category=category,
                    topic=topic[:1024],
                    sync_permissions=True,
                    reason=reason,
                )
            config.category_id = category.id
            await self.save_config(config)
            stored[definition.identifier] = channel.id
            await self.state.update_section(PANEL_CHANNELS_SECTION, str(guild.id), stored)
            return channel

    async def ensure_log_channel(
        self,
        guild: discord.Guild,
        definition: LogModuleDefinition,
        *,
        reason: str,
    ) -> discord.TextChannel:
        lock = self._locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            bot_member = guild.me
            if bot_member is None or not bot_member.guild_permissions.manage_channels:
                raise PermissionError(
                    "A Bella precisa da permissão Gerenciar Canais para criar o canal de logs."
                )
            config = await self.config(guild.id)
            category, _ = await self._resolve_category(guild, config, reason=reason)
            stored = await self.channel_ids(guild.id)
            channel = _stored_text_channel(guild, stored.get(definition.identifier))
            expected_name = channel_name(definition, config)
            if channel is None:
                channel = discord.utils.find(
                    lambda item: item.name == expected_name,
                    category.text_channels,
                )
            if channel is None:
                channel = await guild.create_text_channel(
                    expected_name,
                    category=category,
                    topic=(
                        f"Logs de {definition.display_name} da Bella. "
                        f"Eventos: {', '.join(definition.event_types)}."
                    )[:1024],
                    reason=reason,
                )
            stored[definition.identifier] = channel.id
            config.category_id = category.id
            if definition.identifier not in config.enabled_modules:
                config.enabled_modules.append(definition.identifier)
            await self.save_config(config)
            await self.state.update_section(LOG_CHANNELS_SECTION, str(guild.id), stored)
            return channel

    async def provision(
        self,
        guild: discord.Guild,
        config: GuildLogConfig,
        *,
        reason: str,
    ) -> LogProvisionResult:
        lock = self._locks.setdefault(guild.id, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("Já existe uma criação/sincronização de logs em andamento neste servidor.")
        async with lock:
            bot_member = guild.me
            if bot_member is None or not bot_member.guild_permissions.manage_channels:
                raise PermissionError("A Bella precisa da permissão Gerenciar Canais para provisionar os logs.")
            category, created_category = await self._resolve_category(guild, config, reason=reason)
            stored = await self.channel_ids(guild.id)
            created: list[str] = []
            reused: list[str] = []
            missing_permissions: list[str] = []
            resolved: dict[str, int] = {}
            for definition in self.registry.enabled(config.enabled_modules):
                channel = _stored_text_channel(guild, stored.get(definition.identifier))
                expected_name = channel_name(definition, config)
                if channel is None:
                    channel = discord.utils.find(
                        lambda item: item.name == expected_name,
                        category.text_channels,
                    )
                if channel is None:
                    try:
                        channel = await guild.create_text_channel(
                            expected_name,
                            category=category,
                            topic=(
                                f"Logs de {definition.display_name} da Bella. "
                                f"Módulo: {definition.identifier}. Eventos: {', '.join(definition.event_types)}."
                            )[:1024],
                            reason=reason,
                        )
                        created.append(definition.identifier)
                    except discord.Forbidden as exc:
                        missing_permissions.append(definition.identifier)
                        LOGGER.warning("Sem permissão para criar log %s: %s", definition.identifier, exc)
                        continue
                else:
                    reused.append(definition.identifier)
                resolved[definition.identifier] = channel.id

            stored_panels = await self.panel_channel_ids(guild.id)
            default_panel_ids = {item.identifier for item in DEFAULT_PANEL_CHANNELS}
            resolved_panels: dict[str, int] = {
                identifier: channel_id
                for identifier, channel_id in stored_panels.items()
                if identifier not in default_panel_ids
                and _stored_text_channel(guild, channel_id) is not None
            }
            created_panels: list[str] = []
            reused_panels: list[str] = []
            for definition in DEFAULT_PANEL_CHANNELS:
                channel = _stored_text_channel(guild, stored_panels.get(definition.identifier))
                expected_name = panel_channel_name(definition, config)
                if channel is None:
                    channel = discord.utils.find(
                        lambda item: item.name == expected_name,
                        category.text_channels,
                    )
                if channel is None:
                    try:
                        channel = await guild.create_text_channel(
                            expected_name,
                            category=category,
                            topic=(
                                f"Painel administrativo de {definition.display_name} da Bella. "
                                "Gerenciado pela Central de Logs."
                            )[:1024],
                            reason=reason,
                        )
                        created_panels.append(definition.identifier)
                    except discord.Forbidden:
                        missing_permissions.append(f"painel:{definition.identifier}")
                        continue
                else:
                    reused_panels.append(definition.identifier)
                resolved_panels[definition.identifier] = channel.id

            config.category_id = category.id
            await self.save_config(config)
            await self.state.update_section(LOG_CHANNELS_SECTION, str(guild.id), resolved)
            await self.state.update_section(PANEL_CHANNELS_SECTION, str(guild.id), resolved_panels)
            if self.binding_callback is not None:
                await self.binding_callback(guild, resolved)
            if self.panel_binding_callback is not None:
                await self.panel_binding_callback(guild, resolved_panels)
            await self._record_event(
                guild.id,
                "system",
                "logs_provisioned",
                {
                    "created_category": created_category,
                    "created_channels": created,
                    "reused_channels": reused,
                    "created_panel_channels": created_panels,
                    "reused_panel_channels": reused_panels,
                    "missing_permissions": missing_permissions,
                },
            )
            return LogProvisionResult(
                category_id=category.id,
                created_category=created_category,
                created_channels=tuple(created),
                reused_channels=tuple(reused),
                missing_permissions=tuple(missing_permissions),
                channel_ids=resolved,
                panel_channel_ids=resolved_panels,
                created_panel_channels=tuple(created_panels),
                reused_panel_channels=tuple(reused_panels),
            )

    async def synchronize(self, guild: discord.Guild, *, reason: str) -> LogProvisionResult:
        return await self.provision(guild, await self.config(guild.id), reason=reason)

    async def send_event(
        self,
        guild: discord.Guild,
        module_id: str,
        *,
        title: str,
        description: str,
        severity: str = "INFO",
        fields: Iterable[tuple[str, str, bool]] = (),
        critical: bool = False,
    ) -> None:
        channel_ids = await self.channel_ids(guild.id)
        targets = [channel_ids.get(module_id)]
        if critical:
            config = await self.config(guild.id)
            targets.append(channel_ids.get(config.alert_module_id))
        embed = discord.Embed(
            title=title[:256],
            description=description[:4096],
            color=_severity_color(severity),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Bella Logs • {module_id} • {severity.upper()}")
        for name, value, inline in fields:
            embed.add_field(name=str(name)[:256], value=str(value)[:1024], inline=inline)
        sent_to: set[int] = set()
        for channel_id in targets:
            if not channel_id or int(channel_id) in sent_to:
                continue
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                sent_to.add(channel.id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                LOGGER.exception("Falha ao enviar evento %s para o canal %s", module_id, channel_id)
        await self._record_event(
            guild.id,
            module_id,
            title,
            {"severity": severity, "critical": critical, "channels": sorted(sent_to)},
        )

    async def status_embed(self, guild: discord.Guild) -> discord.Embed:
        config = await self.config(guild.id)
        channels = await self.channel_ids(guild.id)
        lines: list[str] = []
        missing = 0
        for definition in self.registry.enabled(config.enabled_modules):
            channel_id = channels.get(definition.identifier)
            channel = guild.get_channel(channel_id or 0)
            if isinstance(channel, discord.TextChannel):
                state = channel.mention
            else:
                state = "Não configurado"
                missing += 1
            lines.append(f"{definition.emoji} **{definition.display_name}:** {state}")
        category = guild.get_channel(config.category_id or 0)
        embed = discord.Embed(
            title="Central de Logs da Bella",
            description="\n".join(lines)[:4000] or "Nenhum módulo de log habilitado.",
            color=discord.Color.from_rgb(255, 80, 120),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Categoria",
            value=category.name if isinstance(category, discord.CategoryChannel) else "Não criada",
            inline=True,
        )
        embed.add_field(name="Módulos", value=str(len(config.enabled_modules)), inline=True)
        embed.add_field(name="Pendentes", value=str(missing), inline=True)
        embed.set_footer(text=f"Schema {config.schema_version} • separador {config.divider}")
        return embed

    async def _resolve_category(
        self,
        guild: discord.Guild,
        config: GuildLogConfig,
        *,
        reason: str,
    ) -> tuple[discord.CategoryChannel, bool]:
        category = guild.get_channel(config.category_id or 0)
        if not isinstance(category, discord.CategoryChannel):
            category = discord.utils.find(
                lambda item: item.name.casefold() == config.category_name.casefold(),
                guild.categories,
            )
        overwrites = _private_category_overwrites(guild, config.viewer_role_ids)
        if category is None:
            return (
                await guild.create_category(
                    config.category_name,
                    overwrites=overwrites,
                    reason=reason,
                ),
                True,
            )
        try:
            await category.edit(overwrites=overwrites, reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Não foi possível sincronizar overwrites da categoria de logs %s.", category.id)
        return category, False

    async def _record_event(
        self,
        guild_id: int,
        module_id: str,
        event: str,
        payload: dict[str, Any],
    ) -> None:
        timestamp = datetime.now(timezone.utc)
        key = f"{int(timestamp.timestamp() * 1000)}:{guild_id}:{module_id}"

        def mutate(section: dict[str, Any]) -> None:
            section[key] = {
                "guild_id": guild_id,
                "module_id": module_id,
                "event": event,
                "payload": payload,
                "timestamp": timestamp.isoformat(),
            }
            if len(section) > 3000:
                for old_key in sorted(section)[: len(section) - 3000]:
                    section.pop(old_key, None)

        await self.state.mutate_section(LOG_EVENTS_SECTION, mutate)


class LogSetupView(discord.ui.LayoutView):
    def __init__(
        self,
        service: CentralLogService,
        guild: discord.Guild,
        owner_id: int,
        authorize: AuthorizationCallback,
        config: GuildLogConfig,
    ) -> None:
        super().__init__(timeout=900)
        self.service = service
        self.guild = guild
        self.owner_id = owner_id
        self.authorize = authorize
        self.config = config
        self._render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = (
            interaction.user.id == self.owner_id
            and isinstance(interaction.user, discord.Member)
            and self.authorize(interaction.user)
        )
        if not allowed:
            await interaction.response.send_message(
                "Somente quem abriu esta configuração pode usá-la.",
                ephemeral=True,
            )
        return allowed

    def _render(self) -> None:
        self.clear_items()
        self.add_item(
            discord.ui.TextDisplay(
                "# Central de Logs da Bella\n"
                "Escolha os módulos, os cargos leitores e a aparência. A sincronização "
                "reaproveita canais existentes e cria somente o que estiver faltando."
            )
        )
        self.add_item(
            discord.ui.TextDisplay(
                f"**Categoria:** {self.config.category_name}\n"
                f"**Separador:** `{self.config.divider}`\n"
                f"**Emojis:** {'ativos' if self.config.use_emojis else 'desativados'}\n"
                f"**Módulos selecionados:** {len(self.config.enabled_modules)}\n"
                f"**Cargos leitores:** {len(self.config.viewer_role_ids)}"
            )
        )
        module_select = discord.ui.Select(
            placeholder="Módulos com canal próprio",
            min_values=1,
            max_values=len(self.service.registry.all()),
            options=[
                discord.SelectOption(
                    label=item.display_name,
                    value=item.identifier,
                    emoji=item.emoji,
                    description=", ".join(item.event_types)[:100],
                    default=item.identifier in self.config.enabled_modules,
                )
                for item in self.service.registry.all()
            ],
        )

        async def update_modules(interaction: discord.Interaction) -> None:
            self.config.enabled_modules = list(module_select.values)
            self._render()
            await interaction.response.edit_message(view=self)

        module_select.callback = update_modules
        self.add_item(discord.ui.ActionRow(module_select))

        role_select = discord.ui.RoleSelect(
            placeholder="Cargos que podem visualizar os logs",
            min_values=0,
            max_values=10,
            default_values=[
                discord.Object(id=role_id, type=discord.Role)
                for role_id in self.config.viewer_role_ids
                if self.guild.get_role(role_id) is not None
            ],
        )

        async def update_roles(interaction: discord.Interaction) -> None:
            self.config.viewer_role_ids = sorted({role.id for role in role_select.values})
            self._render()
            await interaction.response.edit_message(view=self)

        role_select.callback = update_roles
        self.add_item(discord.ui.ActionRow(role_select))

        actions = discord.ui.ActionRow()
        appearance = discord.ui.Button(label="Aparência", style=discord.ButtonStyle.secondary)
        create = discord.ui.Button(label="Salvar configuração", style=discord.ButtonStyle.success)
        cancel = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.danger)
        appearance.callback = lambda interaction: interaction.response.send_modal(LogAppearanceModal(self))
        create.callback = self._provision
        cancel.callback = self._cancel
        actions.add_item(appearance)
        actions.add_item(create)
        actions.add_item(cancel)
        self.add_item(actions)

    async def _provision(self, interaction: discord.Interaction) -> None:
        await self.service.save_config(self.config)
        await interaction.response.send_message(
            "Configuração salva. Use `/logs-sincronizar` para criar somente os canais que estiverem faltando.",
            ephemeral=True,
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        for item in self.walk_children():
            if isinstance(item, (discord.ui.Button, discord.ui.Select, discord.ui.RoleSelect)):
                item.disabled = True
        await interaction.response.edit_message(view=self)


class LogAppearanceModal(discord.ui.Modal, title="Aparência da Central de Logs"):
    def __init__(self, view: LogSetupView) -> None:
        super().__init__()
        self.view = view
        self.category_name = discord.ui.TextInput(
            label="Nome da categoria",
            default=view.config.category_name,
            max_length=100,
        )
        self.divider = discord.ui.TextInput(
            label="Divisor dos canais",
            default=view.config.divider,
            max_length=8,
        )
        self.use_emojis = discord.ui.TextInput(
            label="Usar emojis? (sim/não)",
            default="sim" if view.config.use_emojis else "não",
            max_length=5,
        )
        self.default_emoji = discord.ui.TextInput(
            label="Emoji único opcional",
            default=view.config.default_emoji,
            required=False,
            max_length=32,
        )
        for item in (self.category_name, self.divider, self.use_emojis, self.default_emoji):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        enabled = str(self.use_emojis.value).strip().casefold() in {"sim", "s", "1", "true", "yes"}
        self.view.config.category_name = _clean_category_name(str(self.category_name.value))
        self.view.config.divider = _clean_divider(str(self.divider.value))
        self.view.config.use_emojis = enabled
        self.view.config.default_emoji = str(self.default_emoji.value or "").strip()[:32]
        self.view._render()
        await interaction.response.edit_message(view=self.view)


def channel_name(definition: LogModuleDefinition, config: GuildLogConfig) -> str:
    base = _slug_channel_name(definition.default_channel_name)
    if not config.use_emojis:
        return base[:100]
    emoji = config.default_emoji or definition.emoji
    return f"{emoji}{config.divider}{base}"[:100]


def panel_channel_name(definition: PanelChannelDefinition, config: GuildLogConfig) -> str:
    base = _slug_channel_name(definition.default_channel_name)
    if not config.use_emojis:
        return base[:100]
    emoji = config.default_emoji or definition.emoji
    return f"{emoji}{config.divider}{base}"[:100]


def _private_category_overwrites(
    guild: discord.Guild,
    viewer_role_ids: Iterable[int],
) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
    overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    if guild.me is not None:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            embed_links=True,
            attach_files=True,
            manage_messages=True,
        )
    for role_id in viewer_role_ids:
        role = guild.get_role(int(role_id))
        if role is not None and role != guild.default_role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                send_messages=False,
            )
    return overwrites


def _stored_text_channel(guild: discord.Guild, channel_id: int | None) -> discord.TextChannel | None:
    channel = guild.get_channel(channel_id or 0)
    return channel if isinstance(channel, discord.TextChannel) else None


def _clean_category_name(value: Any) -> str:
    return " ".join(str(value or "BELLA • LOGS").split())[:100] or "BELLA • LOGS"


def _clean_divider(value: Any) -> str:
    text = "".join(str(value or "╺╸").split())[:8]
    return text or "-"


def _slug_channel_name(value: str) -> str:
    text = value.strip().casefold().replace("_", "-").replace(" ", "-")
    text = re.sub(r"[^a-z0-9áàâãéêíóôõúç-]+", "", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "logs"


def _int_values(value: Any) -> list[int]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[int] = []
    for item in value:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed or None


def _severity_color(severity: str) -> discord.Color:
    return {
        "CRITICAL": discord.Color.from_rgb(150, 0, 24),
        "HIGH": discord.Color.from_rgb(218, 54, 51),
        "MEDIUM": discord.Color.from_rgb(210, 153, 34),
        "LOW": discord.Color.from_rgb(24, 119, 242),
        "INFO": discord.Color.from_rgb(110, 118, 129),
    }.get(severity.upper(), discord.Color.from_rgb(110, 118, 129))


def _provision_embed(result: LogProvisionResult) -> discord.Embed:
    embed = discord.Embed(
        title="Central de Logs sincronizada",
        description="Os módulos foram vinculados aos canais sem recriar destinos válidos.",
        color=discord.Color.from_rgb(46, 160, 67),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Categoria criada", value="sim" if result.created_category else "não", inline=True)
    embed.add_field(name="Canais criados", value=str(len(result.created_channels)), inline=True)
    embed.add_field(name="Canais reutilizados", value=str(len(result.reused_channels)), inline=True)
    embed.add_field(name="Painéis criados", value=str(len(result.created_panel_channels)), inline=True)
    embed.add_field(name="Painéis reutilizados", value=str(len(result.reused_panel_channels)), inline=True)
    if result.created_channels:
        embed.add_field(name="Novos módulos", value=", ".join(result.created_channels)[:1024], inline=False)
    if result.missing_permissions:
        embed.add_field(
            name="Não criados",
            value=", ".join(result.missing_permissions)[:1024],
            inline=False,
        )
    return embed
