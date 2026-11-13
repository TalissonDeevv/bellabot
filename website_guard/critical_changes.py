from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable

import discord

from .central_logs import CentralLogService
from .state import StateStore

LOGGER = logging.getLogger(__name__)

CONFIG_SECTION = "critical_change_config"
EVENTS_SECTION = "critical_change_events"
BASELINES_SECTION = "critical_change_baselines"
MAX_EVENTS_PER_GUILD = 300

AuthorizationCallback = Callable[[discord.Member], bool]
EventSink = Callable[[int, dict[str, Any]], Awaitable[None]]

SENSITIVE_PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("administrator", "Administrator", "CRITICAL"),
    ("manage_guild", "Gerenciar Servidor", "HIGH"),
    ("manage_roles", "Gerenciar Cargos", "HIGH"),
    ("manage_channels", "Gerenciar Canais", "HIGH"),
    ("manage_webhooks", "Gerenciar Webhooks", "HIGH"),
    ("ban_members", "Banir Membros", "HIGH"),
    ("kick_members", "Expulsar Membros", "MEDIUM"),
    ("mention_everyone", "Mencionar Everyone/Here", "MEDIUM"),
    ("manage_messages", "Gerenciar Mensagens", "MEDIUM"),
)

SENSITIVE_CHANNEL_TERMS = (
    "admin",
    "staff",
    "diretoria",
    "moderacao",
    "moderação",
    "seguranca",
    "segurança",
    "logs",
    "backup",
    "ticket",
    "financeiro",
)


class CriticalChangeDetector:
    def __init__(
        self,
        state: StateStore,
        logs: CentralLogService,
        *,
        authorize: AuthorizationCallback,
        event_sink: EventSink | None = None,
    ) -> None:
        self.state = state
        self.logs = logs
        self.authorize = authorize
        self.event_sink = event_sink
        self._recent_signatures: dict[str, float] = {}

    def set_event_sink(self, event_sink: EventSink) -> None:
        self.event_sink = event_sink

    async def enabled(self, guild_id: int) -> bool:
        section = await self.state.get_section(CONFIG_SECTION)
        payload = section.get(str(guild_id), {})
        return bool(payload.get("enabled", True)) if isinstance(payload, dict) else True

    async def set_enabled(self, guild_id: int, enabled: bool) -> None:
        await self.state.update_section(
            CONFIG_SECTION,
            str(guild_id),
            {
                "enabled": bool(enabled),
                "updated_at": _now(),
            },
        )

    async def initialize_guild(self, guild: discord.Guild) -> None:
        await self.refresh_webhook_baseline(guild, emit=False)

    async def refresh_webhook_baseline(
        self,
        guild: discord.Guild,
        *,
        emit: bool = True,
    ) -> int:
        inventory = await self._webhook_inventory(guild)
        if inventory is None:
            return 0
        await self._save_baseline(guild.id, "webhooks", inventory)
        if emit:
            await self.logs.send_event(
                guild,
                "critical_changes",
                title="Baseline de webhooks atualizada",
                description=f"A Bella registrou **{len(inventory)}** webhook(s) visível(is) para futuras comparações.",
                severity="INFO",
            )
        return len(inventory)

    async def on_role_create(self, role: discord.Role) -> None:
        if not await self.enabled(role.guild.id):
            return
        gained = _enabled_sensitive_permissions(role.permissions)
        if not gained:
            return
        severity = _highest_severity(item[2] for item in gained)
        await self._emit(
            role.guild,
            rule_id="role.created_sensitive",
            category="CARGOS",
            severity=severity,
            title="Cargo criado com permissões sensíveis",
            description=(
                f"O cargo **{role.name}** foi criado já contendo permissões administrativas. "
                "Revise se esse acesso é realmente necessário."
            ),
            evidence=f"Permissões: {', '.join(item[1] for item in gained)}",
            resource=f"role:{role.id}",
        )

    async def on_role_update(self, before: discord.Role, after: discord.Role) -> None:
        if not await self.enabled(after.guild.id):
            return
        gained = [
            item
            for item in SENSITIVE_PERMISSIONS
            if not bool(getattr(before.permissions, item[0], False))
            and bool(getattr(after.permissions, item[0], False))
        ]
        if not gained:
            return
        severity = _highest_severity(item[2] for item in gained)
        affected = len(after.members)
        await self._emit(
            after.guild,
            rule_id="role.permissions_gained",
            category="CARGOS",
            severity=severity,
            title="Cargo recebeu permissões sensíveis",
            description=(
                f"O cargo **{after.name}** passou a ter acesso administrativo adicional. "
                "A alteração foi detectada no momento em que ocorreu."
            ),
            evidence=(
                f"Novas permissões: {', '.join(item[1] for item in gained)}\n"
                f"Membros afetados: {affected}\nPosição do cargo: {after.position}"
            ),
            resource=f"role:{after.id}",
        )

    async def on_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        if not await self.enabled(after.guild.id):
            return
        before_overwrites = _overwrite_fingerprint(before)
        after_overwrites = _overwrite_fingerprint(after)
        if before_overwrites == after_overwrites:
            return
        sensitive = await self._is_sensitive_channel(after)
        if not sensitive:
            return
        public_before = _default_role_can_view(before)
        public_after = _default_role_can_view(after)
        exposed = not public_before and public_after
        await self._emit(
            after.guild,
            rule_id="channel.sensitive_overwrites_changed",
            category="CANAIS",
            severity="CRITICAL" if exposed else "HIGH",
            title=(
                "Canal administrativo potencialmente exposto"
                if exposed
                else "Permissões de canal sensível alteradas"
            ),
            description=(
                f"As permissões de **{after.name}** mudaram. "
                "A Bella não reverteu a alteração automaticamente."
            ),
            evidence=(
                f"Visível para @everyone antes: {'sim' if public_before else 'não'}\n"
                f"Visível para @everyone agora: {'sim' if public_after else 'não'}\n"
                f"Overwrites antes/agora: {len(before_overwrites)}/{len(after_overwrites)}"
            ),
            resource=f"channel:{after.id}",
        )

    async def on_automod_rule_update(
        self,
        before: discord.AutoModRule,
        after: discord.AutoModRule,
    ) -> None:
        if not await self.enabled(after.guild.id):
            return
        if bool(before.enabled) and not bool(after.enabled):
            await self._emit(
                after.guild,
                rule_id="automod.rule_disabled",
                category="AUTOMOD",
                severity="HIGH",
                title="Regra de AutoMod desativada",
                description=f"A regra **{after.name}** foi desativada e deixou de aplicar suas ações.",
                evidence=f"Regra: {after.id}\nCriador: {after.creator_id}",
                resource=f"automod:{after.id}",
            )

    async def on_automod_rule_delete(self, rule: discord.AutoModRule) -> None:
        if not await self.enabled(rule.guild.id):
            return
        await self._emit(
            rule.guild,
            rule_id="automod.rule_deleted",
            category="AUTOMOD",
            severity="HIGH",
            title="Regra de AutoMod removida",
            description=f"A regra **{rule.name}** foi removida do servidor.",
            evidence=f"Regra: {rule.id}\nEstava ativa: {'sim' if rule.enabled else 'não'}",
            resource=f"automod:{rule.id}",
        )

    async def on_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild
        if not await self.enabled(guild.id):
            return
        previous = await self._baseline(guild.id, "webhooks")
        current = await self._webhook_inventory(guild)
        if current is None:
            return
        if not previous:
            await self._save_baseline(guild.id, "webhooks", current)
            return
        previous_ids = set(previous)
        current_ids = set(current)
        for webhook_id in sorted(current_ids - previous_ids):
            payload = current[webhook_id]
            await self._emit(
                guild,
                rule_id="webhook.created",
                category="WEBHOOKS",
                severity="HIGH",
                title="Novo webhook detectado",
                description="Um webhook novo apareceu no servidor e merece revisão administrativa.",
                evidence=(
                    f"Nome: {payload.get('name') or 'Sem nome'}\n"
                    f"Canal: <#{payload.get('channel_id')}>\n"
                    f"Criador: {payload.get('user_id') or 'não disponível'}"
                ),
                resource=f"webhook:{webhook_id}",
            )
        for webhook_id in sorted(previous_ids - current_ids):
            payload = previous[webhook_id]
            await self._emit(
                guild,
                rule_id="webhook.deleted",
                category="WEBHOOKS",
                severity="MEDIUM",
                title="Webhook removido",
                description="Um webhook anteriormente conhecido não está mais disponível.",
                evidence=f"Nome: {payload.get('name') or 'Sem nome'}\nCanal: <#{payload.get('channel_id')}>",
                resource=f"webhook:{webhook_id}",
            )
        await self._save_baseline(guild.id, "webhooks", current)

    async def recent_events(self, guild_id: int, *, limit: int = 10) -> list[dict[str, Any]]:
        section = await self.state.get_section(EVENTS_SECTION)
        values = section.get(str(guild_id), [])
        if not isinstance(values, list):
            return []
        return [item for item in values[-limit:] if isinstance(item, dict)][::-1]

    async def status_embed(self, guild: discord.Guild) -> discord.Embed:
        enabled = await self.enabled(guild.id)
        events = await self.recent_events(guild.id, limit=100)
        embed = discord.Embed(
            title="Detector de Alterações Críticas",
            description=(
                "Monitoramento em tempo real de cargos, canais administrativos, AutoMod e webhooks. "
                "A Bella alerta, mas não desfaz mudanças automaticamente."
            ),
            color=discord.Color.from_rgb(255, 80, 120),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Estado", value="Ativo" if enabled else "Pausado", inline=True)
        embed.add_field(name="Eventos registrados", value=str(len(events)), inline=True)
        embed.add_field(name="Últimas 24h", value=str(_events_last_day(events)), inline=True)
        embed.set_footer(text="Use o painel para consultar eventos e atualizar a baseline.")
        return embed

    async def recent_embed(self, guild: discord.Guild) -> discord.Embed:
        events = await self.recent_events(guild.id, limit=10)
        lines = [
            f"`{item.get('severity', 'INFO')}` **{item.get('title', 'Evento')}**\n{item.get('resource', 'recurso')}"
            for item in events
        ]
        return discord.Embed(
            title="Alterações críticas recentes",
            description="\n\n".join(lines)[:4000] or "Nenhuma alteração crítica registrada.",
            color=discord.Color.from_rgb(210, 153, 34),
            timestamp=datetime.now(timezone.utc),
        )

    def panel_view(self) -> "CriticalChangesPanelView":
        return CriticalChangesPanelView(self)

    async def _is_sensitive_channel(self, channel: discord.abc.GuildChannel) -> bool:
        name = str(getattr(channel, "name", "")).casefold()
        if any(term in name for term in SENSITIVE_CHANNEL_TERMS):
            return True
        log_channels = await self.logs.channel_ids(channel.guild.id)
        if channel.id in set(log_channels.values()):
            return True
        fixed = await self.state.get_section("fixed_admin_panels")
        guilds = fixed.get("guilds", {}) if isinstance(fixed, dict) else {}
        panels = guilds.get(str(channel.guild.id), {}) if isinstance(guilds, dict) else {}
        if not isinstance(panels, dict):
            return False
        return any(
            int(item.get("channel_id") or 0) == channel.id
            for entries in panels.values()
            if isinstance(entries, list)
            for item in entries
            if isinstance(item, dict)
        )

    async def _webhook_inventory(self, guild: discord.Guild) -> dict[str, dict[str, Any]] | None:
        inventory: dict[str, dict[str, Any]] = {}
        try:
            webhooks = await guild.webhooks()
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Não foi possível consultar webhooks do servidor %s.", guild.id)
            return None
        for webhook in webhooks:
            inventory[str(webhook.id)] = {
                "id": webhook.id,
                "name": webhook.name,
                "channel_id": webhook.channel_id,
                "user_id": webhook.user.id if webhook.user else None,
                "type": str(webhook.type),
            }
        return inventory

    async def _baseline(self, guild_id: int, key: str) -> dict[str, Any]:
        section = await self.state.get_section(BASELINES_SECTION)
        guild_data = section.get(str(guild_id), {})
        value = guild_data.get(key, {}) if isinstance(guild_data, dict) else {}
        return value if isinstance(value, dict) else {}

    async def _save_baseline(self, guild_id: int, key: str, value: dict[str, Any]) -> None:
        def mutate(section: dict[str, Any]) -> None:
            guild_data = section.setdefault(str(guild_id), {})
            if not isinstance(guild_data, dict):
                guild_data = {}
                section[str(guild_id)] = guild_data
            guild_data[key] = value
            guild_data["updated_at"] = _now()

        await self.state.mutate_section(BASELINES_SECTION, mutate)

    async def _emit(
        self,
        guild: discord.Guild,
        *,
        rule_id: str,
        category: str,
        severity: str,
        title: str,
        description: str,
        evidence: str,
        resource: str,
    ) -> None:
        signature = hashlib.sha256(
            f"{guild.id}:{rule_id}:{resource}:{evidence}".encode("utf-8")
        ).hexdigest()
        now_monotonic = time.monotonic()
        if now_monotonic - self._recent_signatures.get(signature, 0.0) < 20:
            return
        self._recent_signatures[signature] = now_monotonic
        event = {
            "id": signature[:16],
            "rule_id": rule_id,
            "category": category,
            "severity": severity,
            "title": title,
            "description": description,
            "evidence": evidence,
            "resource": resource,
            "timestamp": _now(),
            "status": "OPEN",
        }

        def mutate(section: dict[str, Any]) -> None:
            entries = section.setdefault(str(guild.id), [])
            if not isinstance(entries, list):
                entries = []
            entries.append(event)
            section[str(guild.id)] = entries[-MAX_EVENTS_PER_GUILD:]

        await self.state.mutate_section(EVENTS_SECTION, mutate)
        if self.event_sink is not None:
            await self.event_sink(guild.id, event)
        await self.logs.send_event(
            guild,
            "critical_changes",
            title=title,
            description=description,
            severity=severity,
            critical=severity in {"CRITICAL", "HIGH"},
            fields=(
                ("Regra", rule_id, True),
                ("Categoria", category, True),
                ("Recurso", resource, True),
                ("Evidência", evidence, False),
                ("Recomendação", "Revise a alteração no Discord e confirme se ela foi autorizada.", False),
            ),
        )


class CriticalChangesPanelView(discord.ui.View):
    def __init__(self, service: CriticalChangeDetector) -> None:
        super().__init__(timeout=None)
        self.service = service

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = isinstance(interaction.user, discord.Member) and self.service.authorize(interaction.user)
        if not allowed:
            await interaction.response.send_message("Você não possui acesso a este painel.", ephemeral=True)
        return allowed

    @discord.ui.button(
        label="Status",
        style=discord.ButtonStyle.primary,
        custom_id="bella:critical_changes:status:v1",
    )
    async def status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.send_message(embed=await self.service.status_embed(interaction.guild), ephemeral=True)

    @discord.ui.button(
        label="Eventos recentes",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:critical_changes:recent:v1",
    )
    async def recent(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.send_message(embed=await self.service.recent_embed(interaction.guild), ephemeral=True)

    @discord.ui.button(
        label="Atualizar baseline",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:critical_changes:baseline:v1",
    )
    async def baseline(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        count = await self.service.refresh_webhook_baseline(interaction.guild)
        await interaction.followup.send(f"Baseline atualizada com **{count}** webhook(s).", ephemeral=True)

    @discord.ui.button(
        label="Ativar / pausar",
        style=discord.ButtonStyle.danger,
        custom_id="bella:critical_changes:toggle:v1",
    )
    async def toggle(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        enabled = not await self.service.enabled(interaction.guild.id)
        await self.service.set_enabled(interaction.guild.id, enabled)
        await interaction.response.send_message(
            f"Detector {'ativado' if enabled else 'pausado'} neste servidor.",
            ephemeral=True,
        )


def panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Detector de Alterações Críticas",
        description=(
            "Acompanha mudanças sensíveis no momento em que acontecem: permissões de cargos, "
            "canais administrativos, regras de AutoMod e webhooks."
        ),
        color=discord.Color.from_rgb(255, 80, 120),
    )
    embed.add_field(
        name="Como funciona",
        value="As alterações são registradas no canal próprio da Central de Logs. Casos altos e críticos também chegam aos alertas.",
        inline=False,
    )
    embed.add_field(
        name="Segurança",
        value="O detector não desfaz alterações sozinho e nunca expõe tokens de webhook.",
        inline=False,
    )
    return embed


def _enabled_sensitive_permissions(
    permissions: discord.Permissions,
) -> list[tuple[str, str, str]]:
    return [item for item in SENSITIVE_PERMISSIONS if bool(getattr(permissions, item[0], False))]


def _highest_severity(values: Iterable[str]) -> str:
    order = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
    return max(values, key=lambda value: order.get(value, 0), default="INFO")


def _overwrite_fingerprint(channel: discord.abc.GuildChannel) -> str:
    payload: list[dict[str, Any]] = []
    for target, overwrite in sorted(channel.overwrites.items(), key=lambda item: item[0].id):
        allow, deny = overwrite.pair()
        payload.append(
            {
                "target_id": target.id,
                "target_type": "role" if isinstance(target, discord.Role) else "member",
                "allow": allow.value,
                "deny": deny.value,
            }
        )
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _default_role_can_view(channel: discord.abc.GuildChannel) -> bool:
    permissions_for = getattr(channel, "permissions_for", None)
    if callable(permissions_for):
        try:
            return bool(permissions_for(channel.guild.default_role).view_channel)
        except (AttributeError, TypeError):
            pass
    overwrite = channel.overwrites_for(channel.guild.default_role)
    return overwrite.view_channel is not False


def _events_last_day(events: Iterable[dict[str, Any]]) -> int:
    cutoff = datetime.now(timezone.utc).timestamp() - 86400
    total = 0
    for item in events:
        try:
            if datetime.fromisoformat(str(item.get("timestamp"))).timestamp() >= cutoff:
                total += 1
        except (TypeError, ValueError):
            continue
    return total


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
