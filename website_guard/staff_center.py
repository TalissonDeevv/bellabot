from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import discord

from .central_logs import CentralLogService
from .state import StateStore
from .tickets import (
    STAFF_CENTER_HELPER_ALLOWED_CATEGORIES,
    STAFF_CENTER_RESTRICTED_CATEGORIES,
    SmartTicketManager,
)

LOGGER = logging.getLogger(__name__)

STAFF_CONFIG_SECTION = "staff_center_configs"
STAFF_OCCURRENCES_SECTION = "staff_center_occurrences"
STAFF_WARNINGS_SECTION = "staff_center_warnings"
STAFF_INTERVENTIONS_SECTION = "staff_center_interventions"
STAFF_TRAINING_SECTION = "staff_center_training"
STAFF_TRAINING_TICKETS_SECTION = "staff_center_training_tickets"
STAFF_ACTIVITY_SECTION = "staff_center_activity"
STAFF_MODERATION_SECTION = "staff_center_moderation_actions"
STAFF_SUGGESTIONS_SECTION = "staff_center_suggestions"
STAFF_SUPREME_SECTION = "staff_center_supreme"

LOGS_CATEGORY_ID = 1551649692125896744
ADMIN_CATEGORY_ID = 1563248235274047649
SUGGESTION_REVIEW_CHANNEL_ID = 1563301109550817400
SUGGESTION_SUBMISSION_CHANNEL_ID = 1563301545091534868
COMMAND_CHANNEL_KEY = "central-comandos"
MODERATION_LOG_CHANNEL_KEY = "moderacao-staff"
COMMAND_CHANNEL_NAME = "🛡️╺╸central-comandos"
MODERATION_LOG_CHANNEL_NAME = "⚖️╺╸moderacao-staff"
TRAINING_REPORT_CHANNEL_NAME = "📚╺╸relatorios-treinamento"
SUPREME_LOG_CHANNEL_NAME = "🚨╺╸comando-supremo"
SUPREME_PANEL_KEY = "supreme-admin-panel"

ROLE_NAMES = {
    "president": "Presidente",
    "vice_president": "Vice-Presidente",
    "helper": "Ajudante",
}
CATEGORY_NAME = "🛡️ Administração"
OFFICIAL_ROLE_IDS = {
    "president": 1467982180469833979,
    "vice_president": 1504605819923206204,
    "helper": 1563200377095061534,
}
SAFE_HELPER_PERMISSION_NAMES = frozenset(
    {
        "view_channel",
        "send_messages",
        "send_messages_in_threads",
        "read_message_history",
        "add_reactions",
        "embed_links",
        "attach_files",
        "use_external_emojis",
        "use_external_stickers",
        "use_application_commands",
    }
)
DANGEROUS_HELPER_PERMISSION_NAMES = frozenset(
    {
        "administrator",
        "manage_guild",
        "manage_roles",
        "manage_channels",
        "manage_webhooks",
        "ban_members",
        "kick_members",
        "moderate_members",
        "mention_everyone",
    }
)
SHARED_CHANNELS = (
    "central-administrativa",
    "comunicados-internos",
    "ocorrencias",
    "solicitacoes",
    "treinamento-staff",
    "relatorios-staff",
)
LEADERSHIP_CHANNELS = (
    "presidencia",
    "vice-presidencia",
    "decisoes-administrativas",
    "auditoria-staff",
)
CHANNEL_DISPLAY_NAMES = {
    "central-administrativa": "🏛️╺╸central-administrativa",
    "comunicados-internos": "📢╺╸comunicados-internos",
    "ocorrencias": "🚨╺╸ocorrencias",
    "solicitacoes": "📥╺╸solicitacoes",
    "treinamento-staff": "🎓╺╸treinamento-staff",
    "relatorios-staff": "📊╺╸relatorios-staff",
    "presidencia": "👑╺╸presidencia",
    "vice-presidencia": "🛡️╺╸vice-presidencia",
    "decisoes-administrativas": "⚖️╺╸decisoes-administrativas",
    "auditoria-staff": "📋╺╸auditoria-staff",
}

TRAINING_ARTICLES = {
    "fundamentos": "https://www.talissonsouza.com.br/artigos/moderacao-discord",
    "decisao": "https://www.talissonsouza.com.br/artigos/treinamento-moderacao-discord",
    "aprendizado": "https://www.talissonsouza.com.br/artigos/por-que-discord-pode-ser-ambiente-de-aprendizado-serio",
}
TRAINING_MODULES = {
    "fundamentos": {
        "title": "Fundamentos da moderação responsável",
        "summary": (
            "Moderar não é demonstrar poder. É proteger a cultura, aplicar regras com consistência, "
            "prevenir conflitos e respeitar os limites da própria função."
        ),
        "points": (
            "Responsabilidade antes de autoridade",
            "Hierarquia com limites e escalação clara",
            "Regras explicáveis e aplicação consistente",
            "Prevenção de conflitos e segurança da comunidade",
        ),
    },
    "decisao": {
        "title": "Decisão, proporcionalidade e resposta a crises",
        "summary": (
            "Saber a regra não basta. O Ajudante precisa separar fato de opinião, avaliar contexto, "
            "risco, histórico e proporcionalidade antes de agir ou escalar."
        ),
        "points": (
            "Registrar o fato e identificar a regra",
            "Avaliar contexto, impacto e risco ainda ativo",
            "Escolher a medida mínima suficiente",
            "Escalar golpes, ameaças, exposição e casos permanentes",
            "Registrar e revisar o que aconteceu depois",
        ),
    },
    "aprendizado": {
        "title": "Comunidade como ambiente de aprendizado",
        "summary": (
            "Canais claros, dúvidas com contexto, histórico útil e prática acompanhada transformam "
            "o Discord em um espaço de evolução real, não apenas movimento no chat."
        ),
        "points": (
            "Pedir e oferecer contexto",
            "Preservar decisões e evidências úteis",
            "Praticar com constância e feedback",
            "Converter leitura em mudança observável",
        ),
    },
}
LEVEL_WEIGHT = {"none": 0, "helper": 1, "vice_president": 2, "president": 3}
LOCKDOWN_PERMISSION_NAMES = (
    "send_messages",
    "send_messages_in_threads",
    "create_public_threads",
    "create_private_threads",
    "use_application_commands",
    "add_reactions",
    "attach_files",
    "embed_links",
    "send_voice_messages",
    "connect",
    "speak",
    "stream",
    "request_to_speak",
    "use_external_apps",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StaffConfig:
    guild_id: int
    configured: bool = False
    category_id: int | None = None
    president_role_id: int | None = OFFICIAL_ROLE_IDS["president"]
    vice_president_role_id: int | None = OFFICIAL_ROLE_IDS["vice_president"]
    helper_role_id: int | None = OFFICIAL_ROLE_IDS["helper"]
    panel_channel_id: int | None = None
    panel_message_id: int | None = None
    channel_ids: dict[str, int] = field(default_factory=dict)
    functional_message_ids: dict[str, int] = field(default_factory=dict)
    command_channel_id: int | None = None
    moderation_log_channel_id: int | None = None
    training_report_channel_id: int | None = None
    supreme_log_channel_id: int | None = None
    suggestion_submission_channel_id: int | None = SUGGESTION_SUBMISSION_CHANNEL_ID
    suggestion_review_channel_id: int | None = SUGGESTION_REVIEW_CHANNEL_ID
    configured_by: int | None = None
    configured_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StaffConfig":
        valid = cls.__dataclass_fields__
        payload = {key: value for key, value in raw.items() if key in valid}
        payload["guild_id"] = int(payload.get("guild_id") or 0)
        payload["channel_ids"] = {
            str(key): int(value)
            for key, value in dict(payload.get("channel_ids") or {}).items()
            if str(value).isdigit()
        }
        payload["functional_message_ids"] = {
            str(key): int(value)
            for key, value in dict(payload.get("functional_message_ids") or {}).items()
            if str(value).isdigit()
        }
        for key in (
            "category_id",
            "president_role_id",
            "vice_president_role_id",
            "helper_role_id",
            "panel_channel_id",
            "panel_message_id",
            "configured_by",
            "command_channel_id",
            "moderation_log_channel_id",
            "training_report_channel_id",
            "supreme_log_channel_id",
            "suggestion_submission_channel_id",
            "suggestion_review_channel_id",
        ):
            value = payload.get(key)
            payload[key] = int(value) if str(value or "").isdigit() else None
        return cls(**payload)


def staff_level(member: discord.abc.User, config: StaffConfig) -> str:
    roles = getattr(member, "roles", None)
    if roles is None:
        return "none"
    role_ids = {int(role.id) for role in roles}
    if config.president_role_id in role_ids:
        return "president"
    if config.vice_president_role_id in role_ids:
        return "vice_president"
    if config.helper_role_id in role_ids:
        return "helper"
    return "none"


def can_manage_staff(actor_level: str, target_level: str) -> bool:
    if actor_level not in {"president", "vice_president"}:
        return False
    return LEVEL_WEIGHT[actor_level] > LEVEL_WEIGHT[target_level]


def warning_escalation(count: int) -> tuple[str, int | None]:
    if count == 3:
        return "timeout", 60
    if count == 6:
        return "kick", None
    if count == 12:
        return "ban", None
    return "warning", None


def serialize_overwrites(
    overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite],
) -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {}
    for target, overwrite in overwrites.items():
        allow, deny = overwrite.pair()
        payload[str(target.id)] = {
            "target_type": "role" if isinstance(target, discord.Role) else "member",
            "allow": allow.value,
            "deny": deny.value,
        }
    return payload


def mass_ban_candidates(
    members: list[discord.Member],
    *,
    role_id: int,
    owner_id: int,
    bot_user_id: int,
) -> list[discord.Member]:
    president_role_id = OFFICIAL_ROLE_IDS["president"]
    return [
        member
        for member in members
        if role_id in {role.id for role in member.roles}
        and member.id not in {owner_id, bot_user_id}
        and not member.bot
        and president_role_id not in {role.id for role in member.roles}
    ]


class StaffCenter:
    def __init__(
        self,
        bot: discord.Client,
        state: StateStore,
        tickets: SmartTicketManager,
        logs: CentralLogService,
        *,
        authorize: Callable[[discord.Member], bool],
        ai_explainer: Callable[[str], Awaitable[str]] | None = None,
        maintenance: Any | None = None,
    ) -> None:
        self.bot = bot
        self.state = state
        self.tickets = tickets
        self.logs = logs
        self.authorize = authorize
        self.ai_explainer = ai_explainer
        self.maintenance = maintenance
        self._setup_locks: dict[int, asyncio.Lock] = {}
        self._supreme_locks: dict[int, asyncio.Lock] = {}
        self._views_registered = False

    async def config(self, guild_id: int) -> StaffConfig:
        values = await self.state.get_section(STAFF_CONFIG_SECTION)
        raw = values.get(str(guild_id))
        if isinstance(raw, dict):
            config = StaffConfig.from_dict(raw)
            for key, role_id in OFFICIAL_ROLE_IDS.items():
                if not getattr(config, f"{key}_role_id"):
                    setattr(config, f"{key}_role_id", role_id)
            return config
        return StaffConfig(guild_id=guild_id)

    async def save_config(self, config: StaffConfig) -> None:
        await self.state.update_section(STAFF_CONFIG_SECTION, str(config.guild_id), config.to_dict())

    def register_persistent_view(self) -> None:
        if self._views_registered or not hasattr(self.bot, "add_view"):
            return
        self.bot.add_view(StaffPanelView(self))
        self.bot.add_view(StaffTrainingPanelView(self))
        self.bot.add_view(TrainingTicketControlsView(self))
        self.bot.add_view(StaffReportsPanelView(self))
        self.bot.add_view(StaffAuditPanelView(self))
        self.bot.add_view(StaffRequestsPanelView(self))
        self.bot.add_view(StaffOccurrencesPanelView(self))
        self.bot.add_view(StaffCommandPanelView(self))
        self.bot.add_view(StaffSuggestionPanelView(self))
        self.bot.add_view(StaffSuggestionReviewView(self))
        self.bot.add_view(SupremeAdminPanelView(self))
        self._views_registered = True

    async def is_authorized(self, member: discord.Member, *, minimum: str = "helper") -> bool:
        if self.authorize(member):
            return True
        config = await self.config(member.guild.id)
        return LEVEL_WEIGHT[staff_level(member, config)] >= LEVEL_WEIGHT[minimum]

    async def require(
        self,
        interaction: discord.Interaction,
        *,
        minimum: str = "helper",
    ) -> bool:
        if not isinstance(interaction.user, discord.Member) or not await self.is_authorized(
            interaction.user, minimum=minimum
        ):
            await _reply(interaction, "Você não possui autorização para usar esta área.")
            return False
        return True

    async def require_president(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            await _reply(interaction, "Esta central só funciona dentro do servidor.")
            return False
        role_ids = {role.id for role in interaction.user.roles}
        if OFFICIAL_ROLE_IDS["president"] not in role_ids:
            await _reply(
                interaction,
                "A Central Suprema é exclusiva para o cargo oficial de Presidente.",
            )
            return False
        return True

    async def setup_preview(self, guild: discord.Guild) -> discord.Embed:
        config = await self.config(guild.id)
        existing = sum(
            1
            for name in (*SHARED_CHANNELS, *LEADERSHIP_CHANNELS)
            if guild.get_channel(config.channel_ids.get(name, 0)) is not None
        )
        embed = discord.Embed(
            title="Bella Staff Center • Prévia da configuração",
            description=(
                f"A confirmação cria ou reutiliza a categoria **{CATEGORY_NAME}**, três cargos "
                "administrativos e os canais oficiais abaixo. Nenhuma alteração é feita nesta prévia."
            ),
            color=0xE39A32,
        )
        embed.add_field(name="Cargos", value="Presidente\nVice-Presidente\nAjudante", inline=True)
        embed.add_field(
            name="Canais compartilhados",
            value="\n".join(CHANNEL_DISPLAY_NAMES[name] for name in SHARED_CHANNELS),
            inline=True,
        )
        embed.add_field(
            name="Canais da liderança",
            value="\n".join(CHANNEL_DISPLAY_NAMES[name] for name in LEADERSHIP_CHANNELS),
            inline=True,
        )
        embed.add_field(
            name="Integração com tickets",
            value=(
                "Ajudantes entram somente em dúvidas, suporte, comunidade, solicitações gerais e "
                "denúncias. Orçamento, financeiro, comercial e categorias desconhecidas permanecem restritos."
            ),
            inline=False,
        )
        embed.set_footer(text=f"Recursos já reconhecidos: {existing} • Confirmação explícita obrigatória")
        return embed

    async def configure(self, guild: discord.Guild, actor: discord.Member) -> StaffConfig:
        lock = self._setup_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            config = await self.config(guild.id)
            bot_member = guild.me
            if bot_member is None:
                raise RuntimeError("A Bella ainda não está disponível neste servidor.")
            category = guild.get_channel(config.category_id or 0)
            if not isinstance(category, discord.CategoryChannel):
                category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
                if category is None:
                    category = discord.utils.get(guild.categories, name="ADMINISTRAÇÃO")
            roles: dict[str, discord.Role] = {}
            for key, name in ROLE_NAMES.items():
                saved_id = getattr(config, f"{key}_role_id")
                role = (
                    guild.get_role(saved_id or 0)
                    or guild.get_role(OFFICIAL_ROLE_IDS[key])
                    or discord.utils.get(guild.roles, name=name)
                )
                if role is None:
                    role = await guild.create_role(name=name, reason=f"Bella Staff Center por {actor}")
                roles[key] = role
                setattr(config, f"{key}_role_id", role.id)
            safe_helper_permissions = discord.Permissions.none()
            for permission_name in SAFE_HELPER_PERMISSION_NAMES:
                if hasattr(safe_helper_permissions, permission_name):
                    setattr(safe_helper_permissions, permission_name, True)
            await roles["helper"].edit(
                permissions=safe_helper_permissions,
                reason="Permissões operacionais seguras do Bella Staff Center",
            )
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                bot_member: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    create_private_threads=True,
                    send_messages_in_threads=True,
                    manage_threads=True,
                    manage_channels=True,
                    manage_messages=True,
                    read_message_history=True,
                ),
                roles["president"]: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                roles["vice_president"]: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                roles["helper"]: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    send_messages_in_threads=True,
                    read_message_history=True,
                    add_reactions=True,
                    embed_links=True,
                    attach_files=True,
                    use_application_commands=True,
                    manage_channels=False,
                    manage_roles=False,
                    manage_webhooks=False,
                    administrator=False,
                    ban_members=False,
                    kick_members=False,
                ),
            }
            if category is None:
                category = await guild.create_category(
                    CATEGORY_NAME, overwrites=overwrites, reason=f"Bella Staff Center por {actor}"
                )
            else:
                await category.edit(
                    name=CATEGORY_NAME,
                    overwrites=overwrites,
                    reason="Sincronização Bella Staff Center",
                )
            config.category_id = category.id
            for name in (*SHARED_CHANNELS, *LEADERSHIP_CHANNELS):
                display_name = CHANNEL_DISPLAY_NAMES[name]
                channel = guild.get_channel(config.channel_ids.get(name, 0))
                if not isinstance(channel, discord.TextChannel):
                    channel = discord.utils.get(category.text_channels, name=display_name)
                if not isinstance(channel, discord.TextChannel):
                    channel = discord.utils.get(category.text_channels, name=name)
                channel_overwrites = dict(overwrites)
                if name in LEADERSHIP_CHANNELS:
                    channel_overwrites[roles["helper"]] = discord.PermissionOverwrite(view_channel=False)
                if channel is None:
                    channel = await guild.create_text_channel(
                        display_name,
                        category=category,
                        overwrites=channel_overwrites,
                        reason=f"Bella Staff Center por {actor}",
                    )
                else:
                    await channel.edit(
                        name=display_name,
                        category=category,
                        overwrites=channel_overwrites,
                        reason="Sincronização visual do Bella Staff Center",
                    )
                config.channel_ids[name] = channel.id
            config.panel_channel_id = config.channel_ids["central-administrativa"]
            config.configured = True
            config.configured_by = actor.id
            config.configured_at = utc_now()
            await self.save_config(config)
            await self._integrate_tickets(config)
            await self.publish_panel(guild, channel_id=config.panel_channel_id)
            await self.publish_functional_panels(guild)
            await self.ensure_command_and_suggestion_channels(guild)
            await self._activity(guild.id, actor.id, "staff_configured", {"category_id": category.id})
            await self.logs.send_event(
                guild,
                "system",
                title="Bella Staff Center configurado",
                description=f"Estrutura sincronizada por {actor.mention}.",
                severity="INFO",
            )
            return config

    async def _integrate_tickets(self, config: StaffConfig) -> None:
        ticket_config = await self.tickets.store.config(config.guild_id)
        if ticket_config is None:
            return
        ticket_config.staff_center_helper_role_id = config.helper_role_id
        ticket_config.staff_center_allowed_category_keys = sorted(STAFF_CENTER_HELPER_ALLOWED_CATEGORIES)
        ticket_config.staff_center_restricted_category_keys = sorted(STAFF_CENTER_RESTRICTED_CATEGORIES)
        await self.tickets.store.save_config(ticket_config)

    async def panel_embed(self, guild: discord.Guild) -> discord.Embed:
        config = await self.config(guild.id)
        occurrences = await self._guild_records(STAFF_OCCURRENCES_SECTION, guild.id)
        interventions = await self._guild_records(STAFF_INTERVENTIONS_SECTION, guild.id)
        open_occurrences = sum(item.get("status") == "open" for item in occurrences)
        pending = sum(item.get("status") == "pending" for item in interventions)
        embed = discord.Embed(
            title="Bella Staff Center",
            description="Central administrativa, moderação, solicitações e acompanhamento da equipe.",
            color=0xE39A32,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Estado", value="Operacional" if config.configured else "Não configurado")
        embed.add_field(name="Ocorrências abertas", value=str(open_occurrences))
        embed.add_field(name="Solicitações pendentes", value=str(pending))
        embed.add_field(
            name="Hierarquia",
            value="Presidente → Vice-Presidente → Ajudante",
            inline=False,
        )
        embed.set_footer(text="Bella Staff Center • ações auditadas e persistentes")
        return embed

    async def publish_panel(self, guild: discord.Guild, *, channel_id: int | None = None) -> discord.Message:
        config = await self.config(guild.id)
        channel = guild.get_channel(channel_id or config.panel_channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("O canal central-administrativa ainda não está disponível.")
        embed = await self.panel_embed(guild)
        message: discord.Message | None = None
        if config.panel_message_id:
            try:
                message = await channel.fetch_message(config.panel_message_id)
                await message.edit(embed=embed, view=StaffPanelView(self))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        if message is None:
            message = await channel.send(embed=embed, view=StaffPanelView(self))
        config.panel_channel_id = channel.id
        config.panel_message_id = message.id
        await self.save_config(config)
        return message

    async def sync_visual_names(self, guild: discord.Guild) -> int:
        """Renames only persisted Staff Center resources; it never creates new ones."""
        config = await self.config(guild.id)
        changed = 0
        category = guild.get_channel(config.category_id or 0)
        if isinstance(category, discord.CategoryChannel) and category.name != CATEGORY_NAME:
            await category.edit(name=CATEGORY_NAME, reason="Identidade visual do Bella Staff Center")
            changed += 1
        for key, display_name in CHANNEL_DISPLAY_NAMES.items():
            channel = guild.get_channel(config.channel_ids.get(key, 0))
            if isinstance(channel, discord.TextChannel) and channel.name != display_name:
                await channel.edit(name=display_name, reason="Identidade visual do Bella Staff Center")
                changed += 1
        return changed

    async def ensure_command_and_suggestion_channels(self, guild: discord.Guild) -> StaffConfig:
        config = await self.config(guild.id)
        president = guild.get_role(config.president_role_id or 0)
        vice = guild.get_role(config.vice_president_role_id or 0)
        helper = guild.get_role(config.helper_role_id or 0)
        bot_member = guild.me
        if not all((president, vice, helper, bot_member)):
            raise RuntimeError("Os cargos oficiais ou a Bella não estão disponíveis.")

        admin_category = guild.get_channel(ADMIN_CATEGORY_ID)
        if not isinstance(admin_category, discord.CategoryChannel):
            admin_category = guild.get_channel(config.category_id or 0)
        logs_category = guild.get_channel(LOGS_CATEGORY_ID)
        if not isinstance(admin_category, discord.CategoryChannel):
            raise RuntimeError("A categoria administrativa configurada não foi encontrada.")
        if not isinstance(logs_category, discord.CategoryChannel):
            raise RuntimeError("A categoria Bella Logs configurada não foi encontrada.")

        command_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
            president: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
            vice: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
            helper: discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                send_messages=False,
                manage_messages=False,
                create_public_threads=False,
                create_private_threads=False,
                send_messages_in_threads=False,
                use_application_commands=False,
            ),
        }
        command_channel = guild.get_channel(config.command_channel_id or 0)
        if not isinstance(command_channel, discord.TextChannel):
            command_channel = discord.utils.get(admin_category.text_channels, name=COMMAND_CHANNEL_NAME)
        if command_channel is None:
            command_channel = await guild.create_text_channel(
                COMMAND_CHANNEL_NAME,
                category=admin_category,
                overwrites=command_overwrites,
                reason="Central de Comandos do Bella Staff Center",
            )
        else:
            await command_channel.edit(
                name=COMMAND_CHANNEL_NAME,
                category=admin_category,
                overwrites=command_overwrites,
                reason="Sincronização da Central de Comandos",
            )
        config.command_channel_id = command_channel.id

        leadership_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
            president: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            vice: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            helper: discord.PermissionOverwrite(view_channel=False),
        }
        moderation_log = guild.get_channel(config.moderation_log_channel_id or 0)
        if not isinstance(moderation_log, discord.TextChannel):
            moderation_log = discord.utils.get(logs_category.text_channels, name=MODERATION_LOG_CHANNEL_NAME)
        if moderation_log is None:
            moderation_log = await guild.create_text_channel(
                MODERATION_LOG_CHANNEL_NAME,
                category=logs_category,
                overwrites=leadership_overwrites,
                reason="Logs privados de moderação da Staff",
            )
        else:
            await moderation_log.edit(category=logs_category, overwrites=leadership_overwrites)
        config.moderation_log_channel_id = moderation_log.id

        training_report_channel = guild.get_channel(config.training_report_channel_id or 0)
        if not isinstance(training_report_channel, discord.TextChannel):
            training_report_channel = discord.utils.get(
                logs_category.text_channels,
                name=TRAINING_REPORT_CHANNEL_NAME,
            )
        if training_report_channel is None:
            training_report_channel = await guild.create_text_channel(
                TRAINING_REPORT_CHANNEL_NAME,
                category=logs_category,
                overwrites=leadership_overwrites,
                reason="Relatórios privados de aprendizagem da Staff",
            )
        else:
            await training_report_channel.edit(
                name=TRAINING_REPORT_CHANNEL_NAME,
                category=logs_category,
                overwrites=leadership_overwrites,
                reason="Sincronização dos relatórios privados de aprendizagem",
            )
        config.training_report_channel_id = training_report_channel.id

        supreme_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
            ),
            president: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            ),
            vice: discord.PermissionOverwrite(view_channel=False),
            helper: discord.PermissionOverwrite(view_channel=False),
        }
        supreme_log = guild.get_channel(config.supreme_log_channel_id or 0)
        if not isinstance(supreme_log, discord.TextChannel):
            supreme_log = discord.utils.get(logs_category.text_channels, name=SUPREME_LOG_CHANNEL_NAME)
        if supreme_log is None:
            supreme_log = await guild.create_text_channel(
                SUPREME_LOG_CHANNEL_NAME,
                category=logs_category,
                overwrites=supreme_overwrites,
                reason="Auditoria exclusiva da Central Suprema da Bella",
            )
        else:
            await supreme_log.edit(
                name=SUPREME_LOG_CHANNEL_NAME,
                category=logs_category,
                overwrites=supreme_overwrites,
                reason="Sincronização dos logs da Central Suprema",
            )
        config.supreme_log_channel_id = supreme_log.id

        submission_channel = guild.get_channel(SUGGESTION_SUBMISSION_CHANNEL_ID)
        review_channel = guild.get_channel(SUGGESTION_REVIEW_CHANNEL_ID)
        if isinstance(submission_channel, discord.TextChannel):
            await submission_channel.edit(
                name="💡╺╸sugestoes-staff",
                category=admin_category,
                overwrites=command_overwrites,
                reason="Painel de sugestões da Staff",
            )
            config.suggestion_submission_channel_id = submission_channel.id
        if isinstance(review_channel, discord.TextChannel):
            await review_channel.edit(
                name="🔔╺╸notificacoes-sugestoes",
                category=logs_category,
                overwrites=leadership_overwrites,
                reason="Revisão privada de sugestões da Staff",
            )
            config.suggestion_review_channel_id = review_channel.id
        await self.sync_category_dividers(admin_category)
        await self.sync_category_dividers(logs_category)
        await self.save_config(config)
        await self._publish_single_panel(
            command_channel,
            "command-panel",
            self.command_panel_embed(),
            StaffCommandPanelView(self),
            config,
        )
        if isinstance(submission_channel, discord.TextChannel):
            await self._publish_single_panel(
                submission_channel,
                "suggestion-panel",
                self.suggestion_panel_embed(),
                StaffSuggestionPanelView(self),
                config,
            )
        await self.save_config(config)
        return config

    @staticmethod
    async def sync_category_dividers(category: discord.CategoryChannel) -> int:
        changed = 0
        for channel in category.text_channels:
            normalized = _channel_name_with_divider(channel.name)
            if normalized != channel.name:
                await channel.edit(name=normalized, reason="Padronização visual Bella ╺╸")
                changed += 1
        return changed

    async def _publish_single_panel(
        self,
        channel: discord.TextChannel,
        key: str,
        embed: discord.Embed,
        view: discord.ui.View,
        config: StaffConfig,
    ) -> discord.Message:
        message: discord.Message | None = None
        message_id = config.functional_message_ids.get(key)
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embed, view=view)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        if message is None:
            message = await channel.send(embed=embed, view=view)
        config.functional_message_ids[key] = message.id
        return message

    @staticmethod
    def command_panel_embed() -> discord.Embed:
        embed = discord.Embed(
            title="Central de Comandos da Staff",
            description=(
                "Execute ações moderativas com evidência obrigatória. A Bella valida hierarquia, "
                "registra o resultado e nunca concede permissões perigosas diretamente ao Ajudante."
            ),
            color=0xE39A32,
        )
        embed.add_field(
            name="Progressão de advertências",
            value="3 advertências → mute de 1 hora\n6 advertências → kick\n12 advertências → banimento permanente",
            inline=False,
        )
        embed.set_footer(text="Toda ação exige motivo e imagem • logs privados para Presidente e Vice-Presidente")
        return embed

    @staticmethod
    def suggestion_panel_embed() -> discord.Embed:
        return discord.Embed(
            title="Sugestões da Administração",
            description="Envie uma proposta de melhoria para análise do Presidente e da Vice-Presidência.",
            color=0xE39A32,
        )

    async def publish_functional_panels(self, guild: discord.Guild) -> int:
        config = await self.config(guild.id)
        definitions: dict[str, tuple[discord.Embed, discord.ui.View]] = {
            "treinamento-staff": (self.training_panel_embed(), StaffTrainingPanelView(self)),
            "relatorios-staff": (self.reports_panel_embed(), StaffReportsPanelView(self)),
            "auditoria-staff": (self.audit_panel_embed(), StaffAuditPanelView(self)),
            "solicitacoes": (self.requests_panel_embed(), StaffRequestsPanelView(self)),
            "ocorrencias": (self.occurrences_panel_embed(), StaffOccurrencesPanelView(self)),
        }
        published = 0
        for key, (embed, view) in definitions.items():
            channel = guild.get_channel(config.channel_ids.get(key, 0))
            if not isinstance(channel, discord.TextChannel):
                continue
            message: discord.Message | None = None
            message_id = config.functional_message_ids.get(key)
            if message_id:
                try:
                    message = await channel.fetch_message(message_id)
                    await message.edit(embed=embed, view=view)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    message = None
            if message is None:
                message = await channel.send(embed=embed, view=view)
            config.functional_message_ids[key] = message.id
            published += 1
        presidency_channel = guild.get_channel(config.channel_ids.get("presidencia", 0))
        if isinstance(presidency_channel, discord.TextChannel):
            await self._publish_single_panel(
                presidency_channel,
                SUPREME_PANEL_KEY,
                await self.supreme_panel_embed(guild),
                SupremeAdminPanelView(self),
                config,
            )
            published += 1
        await self.save_config(config)
        return published

    @staticmethod
    def training_panel_embed() -> discord.Embed:
        embed = discord.Embed(
            title="Formação prática para Ajudantes",
            description=(
                "Uma trilha curta para desenvolver critério, postura e segurança antes de ganhar "
                "mais autonomia. Estude os módulos, resolva situações e acompanhe sua evolução."
            ),
            color=0xE39A32,
        )
        embed.add_field(
            name="Trilha",
            value="1. Moderação responsável\n2. Decisão e proporcionalidade\n3. Aprendizado aplicado",
            inline=False,
        )
        embed.add_field(
            name="Como funciona",
            value=(
                "O Quiz abre um atendimento privado com tutoria da IA da Bella, milhares de combinações "
                "baseadas nos artigos e relatório final reservado à Presidência e Vice-Presidência."
            ),
            inline=False,
        )
        embed.set_footer(text="Conteúdo baseado nos artigos de Talisson Souza • sem punições automáticas")
        return embed

    @staticmethod
    def reports_panel_embed() -> discord.Embed:
        return discord.Embed(
            title="Relatórios da Staff",
            description="Gere um resumo atualizado de ocorrências, solicitações, advertências e treinamento.",
            color=0xE39A32,
        )

    @staticmethod
    def audit_panel_embed() -> discord.Embed:
        return discord.Embed(
            title="Auditoria da Staff",
            description="Consulte ações administrativas registradas pela Bella com autor e data.",
            color=0xE39A32,
        )

    @staticmethod
    def requests_panel_embed() -> discord.Embed:
        return discord.Embed(
            title="Solicitações à liderança",
            description="Envie uma situação que precise de decisão, apoio ou intervenção da liderança.",
            color=0xE39A32,
        )

    @staticmethod
    def occurrences_panel_embed() -> discord.Embed:
        return discord.Embed(
            title="Registro de ocorrências",
            description="Registre fatos, contexto e evidências para revisão. Evite julgamentos ou exposição desnecessária.",
            color=0xE39A32,
        )

    async def diagnose(self, guild: discord.Guild) -> discord.Embed:
        config = await self.config(guild.id)
        missing: list[str] = []
        for key in ("president", "vice_president", "helper"):
            if guild.get_role(getattr(config, f"{key}_role_id") or 0) is None:
                missing.append(f"cargo {ROLE_NAMES[key]}")
        for name in (*SHARED_CHANNELS, *LEADERSHIP_CHANNELS):
            if guild.get_channel(config.channel_ids.get(name, 0)) is None:
                missing.append(f"canal {name}")
        ticket_config = await self.tickets.store.config(guild.id)
        integration = bool(
            ticket_config
            and ticket_config.staff_center_helper_role_id
            and ticket_config.staff_center_helper_role_id == config.helper_role_id
        )
        embed = discord.Embed(title="Diagnóstico do Bella Staff Center", color=0xE39A32)
        embed.add_field(name="Configuração", value="Ativa" if config.configured else "Pendente")
        embed.add_field(name="Integração com tickets", value="Ativa" if integration else "Pendente")
        embed.add_field(name="Painel persistente", value="Registrado" if config.panel_message_id else "Pendente")
        embed.add_field(
            name="Recursos ausentes",
            value="\n".join(f"• {item}" for item in missing[:15]) or "Nenhum",
            inline=False,
        )
        return embed

    async def create_occurrence(
        self, guild_id: int, actor_id: int, target_id: int, title: str, details: str
    ) -> dict[str, Any]:
        record = {
            "id": await self._next_id(STAFF_OCCURRENCES_SECTION, guild_id),
            "guild_id": guild_id,
            "actor_id": actor_id,
            "target_id": target_id,
            "title": title[:120],
            "details": details[:2000],
            "status": "open",
            "created_at": utc_now(),
        }
        await self.state.update_section(STAFF_OCCURRENCES_SECTION, f"{guild_id}:{record['id']}", record)
        await self._activity(guild_id, actor_id, "occurrence_created", {"occurrence_id": record["id"], "target_id": target_id})
        return record

    async def create_warning(
        self, guild_id: int, actor_id: int, target_id: int, reason: str
    ) -> dict[str, Any]:
        record = {
            "id": await self._next_id(STAFF_WARNINGS_SECTION, guild_id),
            "guild_id": guild_id,
            "actor_id": actor_id,
            "target_id": target_id,
            "reason": reason[:1000],
            "created_at": utc_now(),
        }
        await self.state.update_section(STAFF_WARNINGS_SECTION, f"{guild_id}:{record['id']}", record)
        await self._activity(guild_id, actor_id, "warning_created", {"warning_id": record["id"], "target_id": target_id})
        return record

    async def create_intervention(
        self, guild_id: int, actor_id: int, subject: str, details: str
    ) -> dict[str, Any]:
        record = {
            "id": await self._next_id(STAFF_INTERVENTIONS_SECTION, guild_id),
            "guild_id": guild_id,
            "actor_id": actor_id,
            "subject": subject[:120],
            "details": details[:2000],
            "status": "pending",
            "created_at": utc_now(),
        }
        await self.state.update_section(STAFF_INTERVENTIONS_SECTION, f"{guild_id}:{record['id']}", record)
        await self._activity(guild_id, actor_id, "intervention_requested", {"intervention_id": record["id"]})
        return record

    async def set_training_progress(self, guild_id: int, user_id: int, progress: int) -> None:
        profile = await self.training_profile(guild_id, user_id)
        profile["progress"] = max(0, min(100, progress))
        profile["updated_at"] = utc_now()
        await self.state.update_section(STAFF_TRAINING_SECTION, f"{guild_id}:{user_id}", profile)

    async def training_profile(self, guild_id: int, user_id: int) -> dict[str, Any]:
        values = await self.state.get_section(STAFF_TRAINING_SECTION)
        raw = values.get(f"{guild_id}:{user_id}")
        profile = dict(raw) if isinstance(raw, dict) else {}
        profile.setdefault("guild_id", guild_id)
        profile.setdefault("user_id", user_id)
        profile.setdefault("completed_modules", [])
        profile.setdefault("passed_quizzes", [])
        profile.setdefault("scenario_reviews", 0)
        profile.setdefault("xp", 0)
        profile.setdefault("quiz_attempts", 0)
        profile.setdefault("quiz_correct", 0)
        profile.setdefault("quiz_wrong", 0)
        profile.setdefault("quiz_correct_by_module", {})
        profile.setdefault("answered_question_ids", [])
        profile.setdefault("tutor_messages", 0)
        completed = set(str(item) for item in profile["completed_modules"])
        quizzes = set(str(item) for item in profile["passed_quizzes"])
        earned = len(completed) + len(quizzes) + min(int(profile["scenario_reviews"]), 3)
        profile["progress"] = max(int(profile.get("progress") or 0), round(earned / 9 * 100))
        return profile

    async def complete_training_module(self, guild_id: int, user_id: int, module_id: str) -> dict[str, Any]:
        if module_id not in TRAINING_MODULES:
            raise ValueError("Módulo de treinamento inválido.")
        profile = await self.training_profile(guild_id, user_id)
        completed = set(str(item) for item in profile["completed_modules"])
        if module_id not in completed:
            completed.add(module_id)
            profile["xp"] = int(profile["xp"]) + 100
        profile["completed_modules"] = sorted(completed)
        profile["updated_at"] = utc_now()
        await self.state.update_section(STAFF_TRAINING_SECTION, f"{guild_id}:{user_id}", profile)
        await self._activity(guild_id, user_id, "training_module_completed", {"module_id": module_id})
        return await self.training_profile(guild_id, user_id)

    async def record_quiz_result(
        self,
        guild_id: int,
        user_id: int,
        module_id: str,
        passed: bool,
        *,
        question_id: str | None = None,
    ) -> dict[str, Any]:
        profile = await self.training_profile(guild_id, user_id)
        profile["quiz_attempts"] = int(profile["quiz_attempts"]) + 1
        profile["quiz_correct" if passed else "quiz_wrong"] = int(
            profile["quiz_correct" if passed else "quiz_wrong"]
        ) + 1
        if passed:
            quizzes = set(str(item) for item in profile["passed_quizzes"])
            if question_id is None:
                if module_id not in quizzes:
                    quizzes.add(module_id)
                    profile["xp"] = int(profile["xp"]) + 150
                profile["passed_quizzes"] = sorted(quizzes)
                profile["updated_at"] = utc_now()
                await self.state.update_section(STAFF_TRAINING_SECTION, f"{guild_id}:{user_id}", profile)
                await self._activity(
                    guild_id,
                    user_id,
                    "training_quiz_answered",
                    {"module_id": module_id, "passed": passed, "question_id": None},
                )
                return await self.training_profile(guild_id, user_id)
            answered = list(dict.fromkeys(str(item) for item in profile["answered_question_ids"]))
            is_new = question_id is None or question_id not in answered
            if question_id and is_new:
                answered.append(question_id)
                profile["answered_question_ids"] = answered[-500:]
            correct_by_module = {
                str(key): int(value)
                for key, value in dict(profile["quiz_correct_by_module"]).items()
            }
            if is_new:
                correct_by_module[module_id] = correct_by_module.get(module_id, 0) + 1
                profile["xp"] = int(profile["xp"]) + 25
            profile["quiz_correct_by_module"] = correct_by_module
            if correct_by_module.get(module_id, 0) >= 5 and module_id not in quizzes:
                quizzes.add(module_id)
                profile["xp"] = int(profile["xp"]) + 100
            profile["passed_quizzes"] = sorted(quizzes)
        profile["updated_at"] = utc_now()
        await self.state.update_section(STAFF_TRAINING_SECTION, f"{guild_id}:{user_id}", profile)
        await self._activity(
            guild_id,
            user_id,
            "training_quiz_answered",
            {"module_id": module_id, "passed": passed, "question_id": question_id},
        )
        return await self.training_profile(guild_id, user_id)

    async def record_scenario_review(self, guild_id: int, user_id: int, successful: bool) -> dict[str, Any]:
        profile = await self.training_profile(guild_id, user_id)
        profile["scenario_reviews"] = int(profile["scenario_reviews"]) + 1
        if successful:
            profile["xp"] = int(profile["xp"]) + 75
        profile["updated_at"] = utc_now()
        await self.state.update_section(STAFF_TRAINING_SECTION, f"{guild_id}:{user_id}", profile)
        await self._activity(guild_id, user_id, "training_scenario_reviewed", {"successful": successful})
        return await self.training_profile(guild_id, user_id)

    async def training_progress_embed(self, guild: discord.Guild, member: discord.Member) -> discord.Embed:
        profile = await self.training_profile(guild.id, member.id)
        xp = int(profile["xp"])
        level = "Em formação" if xp < 300 else "Ajudante preparado" if xp < 700 else "Ajudante experiente"
        embed = discord.Embed(title=f"Progresso de treinamento • {member.display_name}", color=0xE39A32)
        embed.add_field(name="Progresso", value=f"{int(profile['progress'])}%")
        embed.add_field(name="Experiência", value=f"{xp} XP")
        embed.add_field(name="Nível", value=level)
        embed.add_field(name="Módulos concluídos", value=f"{len(profile['completed_modules'])}/3")
        embed.add_field(name="Quizzes aprovados", value=f"{len(profile['passed_quizzes'])}/3")
        embed.add_field(name="Cenários revisados", value=str(profile["scenario_reviews"]))
        attempts = int(profile["quiz_attempts"])
        accuracy = (int(profile["quiz_correct"]) / attempts * 100) if attempts else 0.0
        embed.add_field(
            name="Banco de questões",
            value=(
                f"{attempts} respondidas • {profile['quiz_correct']} acertos • "
                f"{accuracy:.1f}% de aproveitamento"
            ),
            inline=False,
        )
        embed.add_field(name="Conversas com a tutora", value=str(profile["tutor_messages"]))
        embed.set_footer(text="Experiência indica prática na trilha; não concede permissões automaticamente")
        return embed

    async def training_ticket(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        values = await self.state.get_section(STAFF_TRAINING_TICKETS_SECTION)
        raw = values.get(f"{guild_id}:{user_id}")
        return dict(raw) if isinstance(raw, dict) else None

    async def create_training_ticket(self, interaction: discord.Interaction) -> discord.Thread | None:
        if (
            interaction.guild is None
            or not isinstance(interaction.guild, discord.Guild)
            or not isinstance(interaction.user, discord.Member)
            or not isinstance(interaction.channel, discord.TextChannel)
        ):
            await _reply(interaction, "Abra o treinamento por um canal de texto do servidor.")
            return None
        existing = await self.training_ticket(interaction.guild.id, interaction.user.id)
        if existing and str(existing.get("status")) == "open":
            thread = interaction.guild.get_thread(int(existing.get("thread_id") or 0))
            if thread is not None and not thread.archived:
                await _reply(interaction, f"Seu atendimento de treinamento já está aberto em {thread.mention}.")
                return thread
        bot_member = interaction.guild.me
        if bot_member is None:
            await _reply(interaction, "A Bella ainda está iniciando. Tente novamente em alguns segundos.")
            return None
        permissions = interaction.channel.permissions_for(bot_member)
        if not permissions.create_private_threads or not permissions.send_messages_in_threads:
            await _reply(
                interaction,
                "Preciso das permissões **Criar Threads Privadas** e **Enviar Mensagens em Threads** neste canal.",
            )
            return None
        await interaction.response.defer(ephemeral=True, thinking=True)
        profile = await self.training_profile(interaction.guild.id, interaction.user.id)
        safe_name = "".join(
            character.lower() if character.isalnum() else "-"
            for character in interaction.user.display_name
        ).strip("-")[:40] or str(interaction.user.id)
        thread = await interaction.channel.create_thread(
            name=f"treinamento-{safe_name}-{interaction.user.id % 10000:04d}",
            type=discord.ChannelType.private_thread,
            invitable=False,
            reason=f"Atendimento de treinamento da Staff criado por {interaction.user}",
        )
        await thread.add_user(interaction.user)
        record = {
            "guild_id": interaction.guild.id,
            "user_id": interaction.user.id,
            "thread_id": thread.id,
            "channel_id": interaction.channel.id,
            "status": "open",
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        await self.state.update_section(
            STAFF_TRAINING_TICKETS_SECTION,
            f"{interaction.guild.id}:{interaction.user.id}",
            record,
        )
        item = build_quiz_item(interaction.user.id, int(profile["quiz_attempts"]))
        welcome = discord.Embed(
            title="Atendimento de Treinamento da Staff",
            description=(
                f"{interaction.user.mention}, este é o seu espaço privado de formação com a Bella. "
                "Responda às questões, explique suas decisões e converse comigo sobre casos reais ou dúvidas."
            ),
            color=0xE39A32,
        )
        welcome.add_field(
            name="Como funciona",
            value=(
                "• O banco possui milhares de combinações baseadas nos três artigos oficiais.\n"
                "• A Bella explica erros e adapta o treinamento ao seu progresso.\n"
                "• Escreva normalmente nesta thread para receber orientação da IA.\n"
                "• Cinco acertos inéditos em cada módulo concluem a etapa de quiz."
            ),
            inline=False,
        )
        welcome.set_footer(text="Treinamento formativo • decisões administrativas continuam sob revisão humana")
        await thread.send(
            content=interaction.user.mention,
            embed=welcome,
            view=TrainingTicketControlsView(self),
            allowed_mentions=discord.AllowedMentions(users=[interaction.user], roles=False, everyone=False),
        )
        await thread.send(embed=TrainingQuizView.question_embed(item), view=TrainingQuizView(self, interaction.user.id, item))
        await self._activity(
            interaction.guild.id,
            interaction.user.id,
            "training_ticket_opened",
            {"thread_id": thread.id},
        )
        await interaction.followup.send(f"Abri seu atendimento de treinamento em {thread.mention}.", ephemeral=True)
        return thread

    async def close_training_ticket(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.Thread):
            await _reply(interaction, "Este botão funciona dentro do atendimento de treinamento.")
            return
        record = await self._training_ticket_by_thread(interaction.guild.id, interaction.channel.id)
        if record is None:
            await _reply(interaction, "Este atendimento não está registrado.")
            return
        owner_id = int(record.get("user_id") or 0)
        if interaction.user.id != owner_id and not (
            isinstance(interaction.user, discord.Member)
            and await self.is_authorized(interaction.user, minimum="vice_president")
        ):
            await _reply(interaction, "Somente o aluno ou a liderança pode encerrar este atendimento.")
            return
        await interaction.response.send_message("Treinamento encerrado. Seu progresso foi salvo.")
        record["status"] = "closed"
        record["closed_at"] = utc_now()
        record["updated_at"] = utc_now()
        await self.state.update_section(
            STAFF_TRAINING_TICKETS_SECTION,
            f"{interaction.guild.id}:{owner_id}",
            record,
        )
        report_message: discord.Message | None = None
        try:
            report_message = await self.publish_training_report(interaction.guild, owner_id, record)
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.exception("Falha ao publicar relatório de aprendizagem: %s", exc)
        if report_message is not None:
            record["report_channel_id"] = report_message.channel.id
            record["report_message_id"] = report_message.id
            await self.state.update_section(
                STAFF_TRAINING_TICKETS_SECTION,
                f"{interaction.guild.id}:{owner_id}",
                record,
            )
        await self._activity(interaction.guild.id, interaction.user.id, "training_ticket_closed", {"thread_id": interaction.channel.id})
        await interaction.channel.edit(archived=True, locked=True, reason="Atendimento de treinamento encerrado")

    async def publish_training_report(
        self,
        guild: discord.Guild,
        user_id: int,
        ticket: dict[str, Any],
    ) -> discord.Message | None:
        config = await self.config(guild.id)
        channel = guild.get_channel(config.training_report_channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            LOGGER.error("Canal de relatórios de treinamento não encontrado no servidor %s", guild.id)
            return None
        profile = await self.training_profile(guild.id, user_id)
        attempts = int(profile["quiz_attempts"])
        correct = int(profile["quiz_correct"])
        accuracy = correct / attempts * 100 if attempts else 0.0
        counts = {
            str(key): int(value)
            for key, value in dict(profile["quiz_correct_by_module"]).items()
        }
        strongest = max(counts, key=counts.get) if counts else None
        pending = [module_id for module_id in TRAINING_MODULES if counts.get(module_id, 0) < 5]
        member = guild.get_member(user_id)
        display_name = member.display_name if member is not None else str(user_id)
        deterministic_summary = (
            f"{display_name} respondeu {attempts} questões, acertou {correct} e alcançou "
            f"{accuracy:.1f}% de aproveitamento. "
            f"Concluiu {len(profile['completed_modules'])} de 3 módulos e possui {profile['xp']} XP."
        )
        summary = deterministic_summary
        if self.ai_explainer is not None:
            try:
                generated = (
                    await self.ai_explainer(
                        "Produza um resumo técnico de aprendizagem em até 6 linhas para Presidente e Vice-Presidente. "
                        "Use somente os números fornecidos, não diagnostique personalidade e não invente fatos. "
                        "Indique avanço e próximos pontos de estudo.\n"
                        f"Aluno: {display_name} ({user_id})\n"
                        f"Módulos concluídos: {len(profile['completed_modules'])}/3\n"
                        f"Questões: {attempts}; acertos: {correct}; erros: {profile['quiz_wrong']}; "
                        f"aproveitamento: {accuracy:.1f}%\n"
                        f"Acertos por módulo: {counts}\n"
                        f"Cenários revisados: {profile['scenario_reviews']}\n"
                        f"Interações com tutora: {profile['tutor_messages']}\n"
                        f"XP: {profile['xp']}"
                    )
                ).strip()
                if generated:
                    summary = generated[:1800]
            except Exception as exc:
                LOGGER.warning("Resumo de aprendizagem por IA indisponível: %s: %s", type(exc).__name__, exc)
        embed = discord.Embed(
            title="Relatório de Aprendizagem da Staff",
            description=summary,
            color=0xE39A32,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Ajudante", value=f"<@{user_id}> (`{user_id}`)", inline=False)
        embed.add_field(name="Progresso", value=f"{profile['progress']}% • {profile['xp']} XP")
        embed.add_field(name="Quiz", value=f"{correct}/{attempts} • {accuracy:.1f}%")
        embed.add_field(name="Tutoria", value=f"{profile['tutor_messages']} mensagens")
        module_lines = [
            f"• {TRAINING_MODULES[module_id]['title']}: {counts.get(module_id, 0)} acertos inéditos"
            for module_id in TRAINING_MODULES
        ]
        embed.add_field(name="Desempenho por módulo", value="\n".join(module_lines), inline=False)
        embed.add_field(
            name="Ponto forte observado",
            value=(str(TRAINING_MODULES[strongest]["title"]) if strongest else "Ainda sem base suficiente"),
            inline=False,
        )
        embed.add_field(
            name="Próximos estudos",
            value=(
                "\n".join(f"• {TRAINING_MODULES[module_id]['title']}" for module_id in pending)
                or "Todos os módulos atingiram o mínimo de acertos inéditos."
            ),
            inline=False,
        )
        embed.add_field(
            name="Período do atendimento",
            value=f"{_discord_timestamp(str(ticket.get('created_at') or ''))} até <t:{int(datetime.now(timezone.utc).timestamp())}:f>",
            inline=False,
        )
        embed.set_footer(text="Relatório privado • conversas completas não são copiadas para os logs")
        return await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _training_ticket_by_thread(self, guild_id: int, thread_id: int) -> dict[str, Any] | None:
        values = await self.state.get_section(STAFF_TRAINING_TICKETS_SECTION)
        for raw in values.values():
            if (
                isinstance(raw, dict)
                and int(raw.get("guild_id") or 0) == guild_id
                and int(raw.get("thread_id") or 0) == thread_id
                and str(raw.get("status")) == "open"
            ):
                return dict(raw)
        return None

    async def handle_training_ticket_message(self, message: discord.Message) -> bool:
        if (
            message.guild is None
            or not isinstance(message.channel, discord.Thread)
            or message.author.bot
            or not message.content.strip()
        ):
            return False
        record = await self._training_ticket_by_thread(message.guild.id, message.channel.id)
        if record is None or int(record.get("user_id") or 0) != message.author.id:
            return False
        profile = await self.training_profile(message.guild.id, message.author.id)
        history: list[str] = []
        try:
            async for item in message.channel.history(limit=8, before=message):
                text = item.content.strip()
                if text:
                    history.append(f"{item.author.display_name}: {text[:400]}")
        except (discord.Forbidden, discord.HTTPException):
            history = []
        history.reverse()
        prompt = (
            "Você está em um atendimento privado de treinamento da Staff da Bella. "
            "Ensine moderação responsável usando exclusivamente os princípios abaixo, sem inventar regras do servidor.\n\n"
            "Base oficial: autoridade é responsabilidade; decisões consideram fato, regra, contexto, risco, "
            "proporcionalidade, escalação e registro; golpes, ameaças, exposição e punições permanentes exigem "
            "revisão; aprendizagem real produz evidência e mudança observável; dúvidas precisam de contexto.\n\n"
            "Conduza como tutora: responda a dúvida, avalie o raciocínio sem humilhar, corrija no máximo dois "
            "pontos por vez e termine com uma pergunta prática curta. Não execute punições nem afirme fatos não fornecidos.\n\n"
            f"Progresso: {profile['progress']}%, XP {profile['xp']}, {profile['quiz_correct']} acertos em "
            f"{profile['quiz_attempts']} questões.\n"
            f"Histórico recente:\n{chr(10).join(history[-6:]) or 'Sem histórico.'}\n\n"
            f"Mensagem do Ajudante:\n{message.content.strip()}"
        )
        fallback = (
            "Entendi seu ponto. Para analisar com segurança, separe o fato observado da interpretação, "
            "identifique a regra aplicável e avalie contexto, risco e proporcionalidade. "
            "Qual evidência objetiva você registraria antes de decidir?"
        )
        async with message.channel.typing():
            answer = fallback
            if self.ai_explainer is not None:
                try:
                    generated = (await self.ai_explainer(prompt)).strip()
                    if generated:
                        answer = generated[:3800]
                except Exception as exc:
                    LOGGER.warning("Tutoria de treinamento indisponível: %s: %s", type(exc).__name__, exc)
        profile["tutor_messages"] = int(profile["tutor_messages"]) + 1
        profile["updated_at"] = utc_now()
        await self.state.update_section(STAFF_TRAINING_SECTION, f"{message.guild.id}:{message.author.id}", profile)
        record["updated_at"] = utc_now()
        await self.state.update_section(
            STAFF_TRAINING_TICKETS_SECTION,
            f"{message.guild.id}:{message.author.id}",
            record,
        )
        for start in range(0, len(answer), 1900):
            await message.channel.send(answer[start : start + 1900])
        return True

    async def supreme_state(self, guild_id: int) -> dict[str, Any]:
        values = await self.state.get_section(STAFF_SUPREME_SECTION)
        raw = values.get(str(guild_id))
        return dict(raw) if isinstance(raw, dict) else {"guild_id": guild_id, "lockdown_active": False}

    async def supreme_panel_embed(self, guild: discord.Guild) -> discord.Embed:
        state = await self.supreme_state(guild.id)
        maintenance_active = bool(self.maintenance and await self.maintenance.is_active())
        embed = discord.Embed(
            title="Central Administrativa Suprema da Bella",
            description=(
                "Controle emergencial reservado exclusivamente ao Presidente. Todas as ações exigem "
                "confirmação, são auditadas e preservam um caminho de restauração."
            ),
            color=0xED4245 if state.get("lockdown_active") or maintenance_active else 0xE39A32,
        )
        embed.add_field(
            name="Estado de contenção",
            value=(
                f"Canais: **{'TRANCADOS' if state.get('lockdown_active') else 'operacionais'}**\n"
                f"Sistemas públicos: **{'BLOQUEADOS' if maintenance_active else 'liberados'}**"
            ),
            inline=False,
        )
        if state.get("lockdown_active"):
            embed.add_field(
                name="Lockdown atual",
                value=(
                    f"Aplicado por: <@{state.get('lockdown_by')}>\n"
                    f"Início: {_discord_timestamp(str(state.get('lockdown_at') or ''))}\n"
                    f"Canais protegidos: **{len(dict(state.get('channels') or {}))}**"
                ),
                inline=False,
            )
        embed.add_field(
            name="Ações disponíveis",
            value=(
                "• Banir membros de um cargo com prévia e confirmação.\n"
                "• Trancar/restaurar mensagens, comandos, anexos, links, threads e voz.\n"
                "• Bloquear/liberar tickets, parcerias, inglês e demais sistemas públicos.\n"
                "• Aplicar contenção total e consultar o diagnóstico emergencial."
            ),
            inline=False,
        )
        embed.set_footer(text="Cargo autorizado: Presidente • ações críticas possuem auditoria exclusiva")
        return embed

    async def refresh_supreme_panel(self, guild: discord.Guild) -> None:
        config = await self.config(guild.id)
        channel = guild.get_channel(config.channel_ids.get("presidencia", 0))
        message_id = config.functional_message_ids.get(SUPREME_PANEL_KEY)
        if not isinstance(channel, discord.TextChannel) or not message_id:
            return
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=await self.supreme_panel_embed(guild), view=SupremeAdminPanelView(self))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    async def _send_supreme_log(
        self,
        guild: discord.Guild,
        *,
        actor_id: int,
        title: str,
        description: str,
        color: int,
        transcript: str | None = None,
    ) -> None:
        config = await self.config(guild.id)
        channel = guild.get_channel(config.supreme_log_channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            LOGGER.error("Canal da Central Suprema não encontrado no servidor %s", guild.id)
            return
        embed = discord.Embed(
            title=title,
            description=description[:4000],
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Executado por", value=f"<@{actor_id}> (`{actor_id}`)", inline=False)
        embed.set_footer(text="Bella Supreme Center • auditoria crítica")
        kwargs: dict[str, Any] = {
            "embed": embed,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if transcript:
            import io

            kwargs["file"] = discord.File(
                io.BytesIO(transcript.encode("utf-8")),
                filename=f"supreme-{int(datetime.now(timezone.utc).timestamp())}.txt",
            )
        await channel.send(**kwargs)

    async def set_public_emergency(
        self,
        guild: discord.Guild,
        *,
        actor: discord.Member,
        enabled: bool,
        reason: str,
    ) -> None:
        if self.maintenance is None:
            raise RuntimeError("A Central de Manutenção não está conectada.")
        if enabled:
            config = await self.maintenance.activate(
                mode="incident",
                actor_id=actor.id,
                title="Contenção emergencial",
                message=(
                    "Os sistemas públicos da Bella estão temporariamente bloqueados por segurança. "
                    "A Presidência está verificando a situação."
                ),
                status="dnd",
                activity_type="watching",
                activity_text="Contenção de segurança em andamento",
            )
        else:
            config = await self.maintenance.deactivate(actor_id=actor.id)
        await self.maintenance.log_state_change(guild, actor=actor, enabled=enabled, config=config)
        await self._send_supreme_log(
            guild,
            actor_id=actor.id,
            title="Sistemas públicos bloqueados" if enabled else "Sistemas públicos liberados",
            description=reason,
            color=0xED4245 if enabled else 0x57F287,
        )
        await self.refresh_supreme_panel(guild)

    @staticmethod
    def _lockdown_overwrites(
        channel: discord.abc.GuildChannel,
        guild: discord.Guild,
        president: discord.Role,
        bot_member: discord.Member,
    ) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
        overwrites = dict(channel.overwrites)
        protected_ids = {president.id, bot_member.id}
        for target, overwrite in list(overwrites.items()):
            if target.id in protected_ids:
                continue
            for permission_name in LOCKDOWN_PERMISSION_NAMES:
                if hasattr(overwrite, permission_name):
                    setattr(overwrite, permission_name, False)
            overwrites[target] = overwrite
        everyone = overwrites.get(guild.default_role, discord.PermissionOverwrite())
        for permission_name in LOCKDOWN_PERMISSION_NAMES:
            if hasattr(everyone, permission_name):
                setattr(everyone, permission_name, False)
        overwrites[guild.default_role] = everyone
        president_overwrite = overwrites.get(president, discord.PermissionOverwrite())
        for permission_name in LOCKDOWN_PERMISSION_NAMES:
            if hasattr(president_overwrite, permission_name):
                setattr(president_overwrite, permission_name, True)
        president_overwrite.view_channel = True
        overwrites[president] = president_overwrite
        bot_overwrite = overwrites.get(bot_member, discord.PermissionOverwrite())
        bot_overwrite.view_channel = True
        bot_overwrite.send_messages = True
        bot_overwrite.send_messages_in_threads = True
        bot_overwrite.mention_everyone = True
        bot_overwrite.manage_channels = True
        bot_overwrite.manage_threads = True
        overwrites[bot_member] = bot_overwrite
        return overwrites

    async def lockdown_server(self, guild: discord.Guild, *, actor: discord.Member, reason: str) -> dict[str, Any]:
        lock = self._supreme_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            current = await self.supreme_state(guild.id)
            if current.get("lockdown_active"):
                raise RuntimeError("O servidor já está em lockdown.")
            president = guild.get_role(OFFICIAL_ROLE_IDS["president"])
            bot_member = guild.me
            if president is None or bot_member is None:
                raise RuntimeError("Cargo Presidente ou membro da Bella indisponível.")
            channels: dict[str, Any] = {}
            snapshot = {
                "guild_id": guild.id,
                "lockdown_active": True,
                "lockdown_status": "applying",
                "lockdown_by": actor.id,
                "lockdown_at": utc_now(),
                "reason": reason[:1000],
                "channels": channels,
            }
            await self.state.update_section(STAFF_SUPREME_SECTION, str(guild.id), snapshot)
            changed = 0
            failures: list[str] = []
            for channel in guild.channels:
                if isinstance(channel, (discord.CategoryChannel, discord.Thread)) or not hasattr(channel, "overwrites"):
                    continue
                channels[str(channel.id)] = {
                    "name": channel.name,
                    "permissions_synced": bool(getattr(channel, "permissions_synced", False)),
                    "overwrites": serialize_overwrites(dict(channel.overwrites)),
                }
                snapshot["channels"] = channels
                await self.state.update_section(STAFF_SUPREME_SECTION, str(guild.id), snapshot)
                try:
                    await channel.edit(
                        overwrites=self._lockdown_overwrites(channel, guild, president, bot_member),
                        reason=f"Lockdown da Central Suprema • {actor} • {reason[:300]}",
                    )
                    changed += 1
                except (discord.Forbidden, discord.HTTPException) as exc:
                    failures.append(f"{channel.id} ({channel.name}): {type(exc).__name__}")
            snapshot["lockdown_status"] = "partial" if failures else "active"
            snapshot["changed_channels"] = changed
            snapshot["failures"] = failures[:100]
            await self.state.update_section(STAFF_SUPREME_SECTION, str(guild.id), snapshot)
            await self._send_supreme_log(
                guild,
                actor_id=actor.id,
                title="Lockdown aplicado",
                description=(
                    f"Motivo: {reason}\nCanais alterados: **{changed}**\nFalhas: **{len(failures)}**"
                ),
                color=0xED4245,
                transcript="\n".join(failures) if failures else None,
            )
            await self.refresh_supreme_panel(guild)
            return snapshot

    async def send_lockdown_announcement(
        self,
        guild: discord.Guild,
        *,
        actor: discord.Member,
        channel_id: int,
        announcement: str,
    ) -> discord.Message:
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("O canal escolhido para o anúncio não é um canal de texto válido.")
        bot_member = guild.me
        permissions = channel.permissions_for(bot_member) if bot_member is not None else None
        if permissions is None or not permissions.send_messages:
            raise RuntimeError("A Bella não consegue enviar mensagens no canal escolhido.")
        if not permissions.mention_everyone:
            raise RuntimeError("A Bella não possui permissão para mencionar @everyone e @here nesse canal.")
        embed = discord.Embed(
            title="Servidor em lockdown de segurança",
            description=announcement[:4000],
            color=0xED4245,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Estado",
            value=(
                "Mensagens, comandos, anexos, links, threads e comunicação por voz foram "
                "temporariamente restringidos enquanto a Presidência verifica a situação."
            ),
            inline=False,
        )
        embed.set_footer(text="Bella Security • aguarde a liberação oficial")
        message = await channel.send(
            content="@everyone @here",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                everyone=True,
                users=False,
                roles=False,
                replied_user=False,
            ),
        )
        await self._send_supreme_log(
            guild,
            actor_id=actor.id,
            title="Anúncio de lockdown publicado",
            description=(
                f"Canal: {channel.mention} (`{channel.id}`)\n"
                f"Mensagem: `{message.id}`\n\n{announcement[:3000]}"
            ),
            color=0xED4245,
        )
        return message

    async def restore_lockdown(self, guild: discord.Guild, *, actor: discord.Member, reason: str) -> dict[str, Any]:
        lock = self._supreme_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            snapshot = await self.supreme_state(guild.id)
            if not snapshot.get("lockdown_active"):
                raise RuntimeError("Não existe lockdown ativo para restaurar.")
            restored = 0
            failures: list[str] = []
            for channel_id, channel_state in dict(snapshot.get("channels") or {}).items():
                channel = guild.get_channel(int(channel_id))
                if channel is None or not hasattr(channel, "edit"):
                    failures.append(f"{channel_id}: canal não encontrado")
                    continue
                try:
                    if channel_state.get("permissions_synced") and getattr(channel, "category", None) is not None:
                        await channel.edit(sync_permissions=True, reason=f"Restauração do lockdown • {actor}")
                    else:
                        restored_overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {}
                        for target_id, raw in dict(channel_state.get("overwrites") or {}).items():
                            target = (
                                guild.get_role(int(target_id))
                                if raw.get("target_type") == "role"
                                else guild.get_member(int(target_id))
                            )
                            if target is None:
                                continue
                            restored_overwrites[target] = discord.PermissionOverwrite.from_pair(
                                discord.Permissions(int(raw.get("allow") or 0)),
                                discord.Permissions(int(raw.get("deny") or 0)),
                            )
                        await channel.edit(
                            overwrites=restored_overwrites,
                            reason=f"Restauração do lockdown • {actor} • {reason[:300]}",
                        )
                    restored += 1
                except (discord.Forbidden, discord.HTTPException) as exc:
                    failures.append(f"{channel_id}: {type(exc).__name__}")
            snapshot["lockdown_active"] = False
            snapshot["lockdown_status"] = "restore_partial" if failures else "restored"
            snapshot["restored_by"] = actor.id
            snapshot["restored_at"] = utc_now()
            snapshot["restore_reason"] = reason[:1000]
            snapshot["restore_failures"] = failures[:100]
            await self.state.update_section(STAFF_SUPREME_SECTION, str(guild.id), snapshot)
            await self._send_supreme_log(
                guild,
                actor_id=actor.id,
                title="Lockdown restaurado",
                description=f"Motivo: {reason}\nCanais restaurados: **{restored}**\nFalhas: **{len(failures)}**",
                color=0x57F287 if not failures else 0xFEE75C,
                transcript="\n".join(failures) if failures else None,
            )
            await self.refresh_supreme_panel(guild)
            return snapshot

    async def mass_ban_role(
        self,
        guild: discord.Guild,
        *,
        actor: discord.Member,
        role: discord.Role,
        reason: str,
        approved_member_ids: set[int] | None = None,
    ) -> dict[str, Any]:
        try:
            await guild.chunk(cache=True)
        except (discord.ClientException, discord.HTTPException):
            pass
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.ban_members:
            raise RuntimeError("A Bella não possui permissão para banir membros.")
        candidates = mass_ban_candidates(
            list(role.members),
            role_id=role.id,
            owner_id=guild.owner_id,
            bot_user_id=bot_member.id,
        )
        if approved_member_ids is not None:
            candidates = [member for member in candidates if member.id in approved_member_ids]
        banned: list[str] = []
        skipped: list[str] = []
        for member in candidates:
            if member.top_role >= bot_member.top_role:
                skipped.append(f"{member.id} ({member}): hierarquia acima da Bella")
                continue
            try:
                await guild.ban(
                    member,
                    reason=f"Central Suprema • {actor} • cargo {role.name} • {reason}",
                    delete_message_seconds=0,
                )
                banned.append(f"{member.id} ({member})")
            except (discord.Forbidden, discord.HTTPException) as exc:
                skipped.append(f"{member.id} ({member}): {type(exc).__name__}")
        record = {
            "guild_id": guild.id,
            "action": "mass_ban_role",
            "actor_id": actor.id,
            "role_id": role.id,
            "role_name": role.name,
            "reason": reason[:1000],
            "banned_count": len(banned),
            "skipped_count": len(skipped),
            "created_at": utc_now(),
        }
        await self.state.update_section(
            STAFF_SUPREME_SECTION,
            f"{guild.id}:mass-ban:{int(datetime.now(timezone.utc).timestamp())}",
            record,
        )
        transcript = (
            "BANIDOS\n" + ("\n".join(banned) or "nenhum") + "\n\nNÃO BANIDOS\n" + ("\n".join(skipped) or "nenhum")
        )
        await self._send_supreme_log(
            guild,
            actor_id=actor.id,
            title="Banimento em massa por cargo concluído",
            description=(
                f"Cargo: {role.mention} (`{role.id}`)\nMotivo: {reason}\n"
                f"Banidos: **{len(banned)}** • Não banidos: **{len(skipped)}**"
            ),
            color=0xED4245,
            transcript=transcript,
        )
        return record

    async def supreme_diagnostics_embed(self, guild: discord.Guild) -> discord.Embed:
        state = await self.supreme_state(guild.id)
        maintenance_active = bool(self.maintenance and await self.maintenance.is_active())
        bot_member = guild.me
        embed = discord.Embed(title="Diagnóstico emergencial", color=0xE39A32)
        embed.add_field(name="Lockdown", value=str(state.get("lockdown_status") or "inativo"))
        embed.add_field(name="Bella pública", value="bloqueada" if maintenance_active else "liberada")
        embed.add_field(name="Canais", value=str(len(guild.channels)))
        embed.add_field(name="Membros", value=str(guild.member_count or len(guild.members)))
        embed.add_field(name="Cargos", value=str(len(guild.roles)))
        embed.add_field(
            name="Permissões da Bella",
            value=(
                f"Gerenciar canais: **{bool(bot_member and bot_member.guild_permissions.manage_channels)}**\n"
                f"Banir membros: **{bool(bot_member and bot_member.guild_permissions.ban_members)}**\n"
                f"Gerenciar cargos: **{bool(bot_member and bot_member.guild_permissions.manage_roles)}**"
            ),
            inline=False,
        )
        embed.set_footer(text="Diagnóstico local • nenhuma alteração foi executada")
        return embed

    async def audit_embed(self, guild_id: int) -> discord.Embed:
        records = await self._guild_records(STAFF_ACTIVITY_SECTION, guild_id)
        records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        lines = []
        labels = {
            "staff_configured": "Staff Center configurado",
            "occurrence_created": "Ocorrência registrada",
            "warning_created": "Advertência registrada",
            "intervention_requested": "Intervenção solicitada",
            "training_module_completed": "Módulo concluído",
            "training_quiz_answered": "Quiz respondido",
            "training_scenario_reviewed": "Cenário revisado",
            "moderation_action": "Ação moderativa executada",
            "suggestion_created": "Sugestão enviada",
            "suggestion_reviewed": "Sugestão revisada",
        }
        for item in records[:15]:
            timestamp = _discord_timestamp(str(item.get("created_at") or ""))
            lines.append(f"{timestamp} • <@{item.get('actor_id')}> • {labels.get(str(item.get('action')), str(item.get('action')))}")
        embed = discord.Embed(title="Auditoria recente da Staff", color=0xE39A32)
        embed.description = "\n".join(lines) or "Nenhuma ação registrada."
        return embed

    async def member_embed(self, guild: discord.Guild, member: discord.Member) -> discord.Embed:
        config = await self.config(guild.id)
        warnings = [
            item
            for item in await self._guild_records(STAFF_WARNINGS_SECTION, guild.id)
            if int(item.get("target_id") or 0) == member.id
        ]
        training = await self.state.get_section(STAFF_TRAINING_SECTION)
        progress = dict(training.get(f"{guild.id}:{member.id}") or {}).get("progress", 0)
        embed = discord.Embed(title=f"Ficha administrativa • {member.display_name}", color=0xE39A32)
        embed.add_field(name="Nível Staff", value=staff_level(member, config).replace("_", " ").title())
        embed.add_field(name="Advertências", value=str(len(warnings)))
        embed.add_field(name="Treinamento", value=f"{int(progress)}%")
        actions = [
            item
            for item in await self._guild_records(STAFF_MODERATION_SECTION, guild.id)
            if int(item.get("target_id") or 0) == member.id
        ]
        embed.add_field(name="Ações moderativas", value=str(len(actions)))
        embed.add_field(name="ID", value=str(member.id), inline=False)
        return embed

    async def execute_moderation_action(
        self,
        interaction: discord.Interaction,
        *,
        action: str,
        target_id: int,
        reason: str,
        evidence: discord.Attachment,
        duration_minutes: int = 60,
    ) -> dict[str, Any]:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            raise ValueError("A ação precisa ser executada dentro do servidor.")
        guild = interaction.guild
        actor = interaction.user
        target = guild.get_member(target_id)
        if target is None:
            raise ValueError("O membro não está disponível no servidor.")
        if target.id == actor.id:
            raise ValueError("Você não pode aplicar uma ação em si mesmo.")
        if target.id == guild.owner_id or target.bot:
            raise ValueError("Este alvo não pode ser moderado por este painel.")
        config = await self.config(guild.id)
        actor_level = staff_level(actor, config)
        target_level = staff_level(target, config)
        if not self.authorize(actor) and actor_level == "none":
            raise PermissionError("Você não pertence à equipe autorizada.")
        if target_level != "none" and LEVEL_WEIGHT[actor_level] <= LEVEL_WEIGHT[target_level]:
            raise PermissionError("A hierarquia da Staff não permite essa ação.")
        if actor.top_role <= target.top_role and actor.id != guild.owner_id:
            raise PermissionError("Seu cargo não está acima do cargo do membro.")
        bot_member = guild.me
        if bot_member is None or bot_member.top_role <= target.top_role:
            raise PermissionError("O cargo da Bella não está acima do cargo do membro.")
        if not evidence.content_type or not evidence.content_type.startswith("image/"):
            raise ValueError("A evidência precisa ser uma imagem válida.")
        reason = reason.strip()
        if len(reason) < 8:
            raise ValueError("Explique o motivo com pelo menos 8 caracteres.")

        warning_count: int | None = None
        effective_action = action
        if action == "warning":
            warning = await self.create_warning(guild.id, actor.id, target.id, reason)
            warning_count = len(
                [
                    item
                    for item in await self._guild_records(STAFF_WARNINGS_SECTION, guild.id)
                    if int(item.get("target_id") or 0) == target.id
                ]
            )
            effective_action, automatic_duration = warning_escalation(warning_count)
            if automatic_duration is not None:
                duration_minutes = automatic_duration
            warning["count_after"] = warning_count

        result = "success"
        error_text = ""
        try:
            if effective_action == "ban":
                await guild.ban(target, reason=f"Staff Center • {actor} • {reason}", delete_message_seconds=0)
            elif effective_action == "kick":
                await target.kick(reason=f"Staff Center • {actor} • {reason}")
            elif effective_action == "timeout":
                minutes = max(1, min(int(duration_minutes), 40320))
                await target.timeout(timedelta(minutes=minutes), reason=f"Staff Center • {actor} • {reason}")
            elif effective_action != "warning":
                raise ValueError("Ação moderativa inválida.")
        except (discord.Forbidden, discord.HTTPException) as exc:
            result = "failed"
            error_text = f"{type(exc).__name__}: {exc}"[:500]

        record = {
            "id": await self._next_id(STAFF_MODERATION_SECTION, guild.id),
            "guild_id": guild.id,
            "actor_id": actor.id,
            "actor_name": actor.display_name,
            "target_id": target.id,
            "target_name": target.display_name,
            "applied_by_id": guild.me.id if guild.me else None,
            "requested_action": action,
            "effective_action": effective_action,
            "warning_count": warning_count,
            "duration_minutes": duration_minutes if effective_action == "timeout" else None,
            "reason": reason[:1500],
            "result": result,
            "error": error_text,
            "created_at": utc_now(),
        }
        await self.state.update_section(STAFF_MODERATION_SECTION, f"{guild.id}:{record['id']}", record)
        await self._activity(guild.id, actor.id, "moderation_action", {"action_id": record["id"], "target_id": target.id, "result": result})
        await self._send_moderation_log(guild, record, evidence)
        if result == "failed":
            raise RuntimeError("A ação foi registrada, mas o Discord recusou sua execução. A liderança pode revisar o log.")
        return record

    async def _send_moderation_log(
        self,
        guild: discord.Guild,
        record: dict[str, Any],
        evidence: discord.Attachment,
    ) -> None:
        config = await self.config(guild.id)
        channel = guild.get_channel(config.moderation_log_channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            LOGGER.error("Canal de logs moderativos não encontrado no servidor %s", guild.id)
            return
        file = await evidence.to_file(use_cached=True, spoiler=True)
        embed = discord.Embed(
            title="Ação moderativa da Staff",
            description=str(record["reason"]),
            color=0x43B581 if record["result"] == "success" else 0xED4245,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Ação solicitada", value=str(record["requested_action"]).upper())
        embed.add_field(name="Ação aplicada", value=str(record["effective_action"]).upper())
        embed.add_field(name="Resultado", value=str(record["result"]).upper())
        embed.add_field(name="Executado por", value=f"{record['actor_name']} (`{record['actor_id']}`)", inline=False)
        embed.add_field(name="Aplicado por", value=f"Bella (`{record.get('applied_by_id')}`)", inline=False)
        embed.add_field(name="Membro afetado", value=f"{record['target_name']} (`{record['target_id']}`)", inline=False)
        if record.get("warning_count") is not None:
            embed.add_field(name="Advertências acumuladas", value=str(record["warning_count"]))
        if record.get("duration_minutes"):
            embed.add_field(name="Duração", value=f"{record['duration_minutes']} minutos")
        if record.get("error"):
            embed.add_field(name="Erro", value=str(record["error"]), inline=False)
        embed.set_image(url=f"attachment://{file.filename}")
        embed.set_footer(text=f"Bella Staff Center • Registro #{record['id']}")
        await channel.send(embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none())

    async def create_suggestion(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        title: str,
        description: str,
        benefit: str,
    ) -> dict[str, Any]:
        config = await self.config(guild.id)
        review_channel = guild.get_channel(config.suggestion_review_channel_id or SUGGESTION_REVIEW_CHANNEL_ID)
        if not isinstance(review_channel, discord.TextChannel):
            raise RuntimeError("O canal privado de revisão de sugestões não foi encontrado.")
        record = {
            "id": await self._next_id(STAFF_SUGGESTIONS_SECTION, guild.id),
            "guild_id": guild.id,
            "author_id": actor.id,
            "author_name": actor.display_name,
            "title": title.strip()[:120],
            "description": description.strip()[:2000],
            "benefit": benefit.strip()[:1000],
            "status": "pending",
            "reviewed_by": None,
            "review_reason": "",
            "created_at": utc_now(),
        }
        view = StaffSuggestionReviewView(self, record)
        message = await review_channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        record["message_id"] = message.id
        record["channel_id"] = review_channel.id
        await self.state.update_section(STAFF_SUGGESTIONS_SECTION, f"{guild.id}:{record['id']}", record)
        await self._activity(guild.id, actor.id, "suggestion_created", {"suggestion_id": record["id"]})
        return record

    async def suggestion_for_message(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        records = await self._guild_records(STAFF_SUGGESTIONS_SECTION, guild_id)
        return next((item for item in records if int(item.get("message_id") or 0) == message_id), None)

    async def review_suggestion(
        self,
        guild: discord.Guild,
        reviewer: discord.Member,
        message_id: int,
        *,
        status: str,
        reason: str = "",
    ) -> dict[str, Any]:
        record = await self.suggestion_for_message(guild.id, message_id)
        if record is None:
            raise ValueError("Não encontrei esta sugestão no histórico.")
        if record.get("status") != "pending":
            raise ValueError("Esta sugestão já foi revisada.")
        record["status"] = status
        record["reviewed_by"] = reviewer.id
        record["reviewer_name"] = reviewer.display_name
        record["review_reason"] = reason.strip()[:1000]
        record["reviewed_at"] = utc_now()
        await self.state.update_section(STAFF_SUGGESTIONS_SECTION, f"{guild.id}:{record['id']}", record)
        await self._activity(guild.id, reviewer.id, "suggestion_reviewed", {"suggestion_id": record["id"], "status": status})
        author = guild.get_member(int(record["author_id"]))
        if author is not None:
            try:
                await author.send(
                    f"Sua sugestão **{record['title']}** foi **{'aprovada' if status == 'approved' else 'recusada'}**."
                    + (f"\nMotivo: {record['review_reason']}" if record["review_reason"] else "")
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        return record

    async def report_embed(self, guild: discord.Guild) -> discord.Embed:
        occurrences = await self._guild_records(STAFF_OCCURRENCES_SECTION, guild.id)
        warnings = await self._guild_records(STAFF_WARNINGS_SECTION, guild.id)
        interventions = await self._guild_records(STAFF_INTERVENTIONS_SECTION, guild.id)
        training = await self._guild_records(STAFF_TRAINING_SECTION, guild.id)
        moderation = await self._guild_records(STAFF_MODERATION_SECTION, guild.id)
        suggestions = await self._guild_records(STAFF_SUGGESTIONS_SECTION, guild.id)
        embed = discord.Embed(
            title="Relatório administrativo da Staff",
            description="Resumo determinístico com dados persistidos pela Bella.",
            color=0xE39A32,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Ocorrências", value=str(len(occurrences)))
        embed.add_field(name="Advertências", value=str(len(warnings)))
        embed.add_field(name="Solicitações", value=str(len(interventions)))
        embed.add_field(name="Em treinamento", value=str(len(training)))
        embed.add_field(name="Ações moderativas", value=str(len(moderation)))
        embed.add_field(name="Sugestões", value=str(len(suggestions)))
        embed.add_field(
            name="Pendências",
            value=(
                f"{sum(item.get('status') == 'open' for item in occurrences)} ocorrências abertas • "
                f"{sum(item.get('status') == 'pending' for item in interventions)} solicitações pendentes"
            ),
            inline=False,
        )
        return embed

    async def copilot(self, question: str, guild: discord.Guild, actor: discord.Member) -> str:
        if self.ai_explainer is None:
            return "O Copiloto está temporariamente indisponível. Os registros administrativos continuam funcionando."
        prompt = (
            "Você é o Copiloto administrativo da Bella. Oriente sem executar punições, sem inventar regras, "
            "sem expor dados privados e sempre recomende revisão humana.\n"
            f"Servidor: {guild.name}\nSolicitante: {actor.display_name}\nPergunta: {question[:2500]}"
        )
        try:
            answer = await self.ai_explainer(prompt)
        except Exception:
            LOGGER.exception("Falha no Copiloto do Staff Center")
            return "Não consegui consultar a IA agora. Registre a situação em Ocorrências ou Solicitações para revisão humana."
        return answer[:1900] if answer.strip() else "A IA não retornou uma orientação válida."

    async def _guild_records(self, section: str, guild_id: int) -> list[dict[str, Any]]:
        values = await self.state.get_section(section)
        return [dict(item) for item in values.values() if isinstance(item, dict) and int(item.get("guild_id") or 0) == guild_id]

    async def _next_id(self, section: str, guild_id: int) -> int:
        records = await self._guild_records(section, guild_id)
        return max((int(item.get("id") or 0) for item in records), default=0) + 1

    async def _activity(self, guild_id: int, actor_id: int, action: str, details: dict[str, Any]) -> None:
        identifier = f"{guild_id}:{datetime.now(timezone.utc).timestamp()}:{actor_id}"
        await self.state.update_section(
            STAFF_ACTIVITY_SECTION,
            identifier,
            {"guild_id": guild_id, "actor_id": actor_id, "action": action, "details": details, "created_at": utc_now()},
        )


class StaffSetupConfirmView(discord.ui.View):
    def __init__(self, center: StaffCenter, actor_id: int) -> None:
        super().__init__(timeout=120)
        self.center = center
        self.actor_id = actor_id

    @discord.ui.button(label="Confirmar configuração", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.actor_id:
            await _reply(interaction, "Somente quem abriu esta prévia pode confirmá-la.")
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await _reply(interaction, "Esta ação precisa ser executada em um servidor.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            config = await self.center.configure(interaction.guild, interaction.user)
        except (discord.Forbidden, discord.HTTPException, RuntimeError) as exc:
            await interaction.followup.send(f"Não consegui concluir a configuração: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Staff Center sincronizado em <#{config.panel_channel_id}>.", ephemeral=True
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Configuração cancelada.", embed=None, view=None)


class StaffPanelView(discord.ui.View):
    def __init__(self, center: StaffCenter) -> None:
        super().__init__(timeout=None)
        self.center = center

    @discord.ui.button(label="Ocorrência", style=discord.ButtonStyle.danger, custom_id="bella:staff:occurrence")
    async def occurrence(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.center.require(interaction):
            await interaction.response.send_modal(OccurrenceModal(self.center))

    @discord.ui.button(label="Consultar membro", style=discord.ButtonStyle.primary, custom_id="bella:staff:member")
    async def member(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.center.require(interaction):
            await interaction.response.send_modal(MemberLookupModal(self.center))

    @discord.ui.button(label="Solicitar intervenção", style=discord.ButtonStyle.secondary, custom_id="bella:staff:request")
    async def request(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.center.require(interaction):
            await interaction.response.send_modal(InterventionModal(self.center))

    @discord.ui.button(label="Treinamento", style=discord.ButtonStyle.success, custom_id="bella:staff:training")
    async def training(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.center.require(interaction):
            return
        await _reply(
            interaction,
            "## Treinamento da Staff\n1. Leia as regras oficiais.\n2. Revise o fluxo de tickets.\n3. Registre ocorrências com evidências.\n4. Nunca aplique punições sem contexto e autorização.",
        )

    @discord.ui.button(label="Relatório", style=discord.ButtonStyle.primary, custom_id="bella:staff:report")
    async def report(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.center.require(interaction, minimum="vice_president") or interaction.guild is None:
            return
        await _reply(interaction, embed=await self.center.report_embed(interaction.guild))

    @discord.ui.button(label="Copiloto", style=discord.ButtonStyle.secondary, custom_id="bella:staff:copilot")
    async def copilot(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.center.require(interaction):
            await interaction.response.send_modal(StaffCopilotModal(self.center))

    @discord.ui.button(label="Diagnóstico", style=discord.ButtonStyle.secondary, custom_id="bella:staff:diagnostics")
    async def diagnostics(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.center.require(interaction, minimum="vice_president") or interaction.guild is None:
            return
        await _reply(interaction, embed=await self.center.diagnose(interaction.guild))


class StaffTrainingPanelView(discord.ui.View):
    def __init__(self, center: StaffCenter) -> None:
        super().__init__(timeout=None)
        self.center = center

    @discord.ui.button(label="Estudar módulos", style=discord.ButtonStyle.primary, custom_id="bella:staff:training:modules")
    async def modules(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.center.require(interaction):
            return
        await interaction.response.send_message(
            "Escolha o módulo que deseja estudar:", view=TrainingModulesView(self.center), ephemeral=True
        )

    @discord.ui.button(label="Quiz", style=discord.ButtonStyle.success, custom_id="bella:staff:training:quiz")
    async def quiz(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.center.require(interaction):
            return
        await self.center.create_training_ticket(interaction)

    @discord.ui.button(label="Situação prática", style=discord.ButtonStyle.secondary, custom_id="bella:staff:training:scenario")
    async def scenario(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.center.require(interaction):
            return
        view = TrainingScenarioView(self.center, interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @discord.ui.button(label="Meu progresso", style=discord.ButtonStyle.secondary, custom_id="bella:staff:training:progress")
    async def progress(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return
        if not await self.center.require(interaction):
            return
        await interaction.response.send_message(
            embed=await self.center.training_progress_embed(interaction.guild, interaction.user), ephemeral=True
        )


class TrainingModulesView(discord.ui.View):
    def __init__(self, center: StaffCenter) -> None:
        super().__init__(timeout=600)
        self.center = center
        for module_id, module in TRAINING_MODULES.items():
            button = discord.ui.Button(label=str(module["title"])[:80], style=discord.ButtonStyle.secondary)

            async def callback(interaction: discord.Interaction, selected: str = module_id) -> None:
                data = TRAINING_MODULES[selected]
                embed = discord.Embed(title=str(data["title"]), description=str(data["summary"]), color=0xE39A32)
                embed.add_field(
                    name="Pontos essenciais",
                    value="\n".join(f"• {point}" for point in data["points"]),
                    inline=False,
                )
                embed.add_field(name="Leitura completa", value=TRAINING_ARTICLES[selected], inline=False)
                embed.set_footer(text="Concluir registra 100 XP; leia e reflita antes de marcar")
                await interaction.response.edit_message(
                    content=None,
                    embed=embed,
                    view=TrainingModuleCompletionView(self.center, selected),
                )

            button.callback = callback
            self.add_item(button)


class TrainingModuleCompletionView(discord.ui.View):
    def __init__(self, center: StaffCenter, module_id: str) -> None:
        super().__init__(timeout=600)
        self.center = center
        self.module_id = module_id

    @discord.ui.button(label="Concluir módulo", style=discord.ButtonStyle.success)
    async def complete(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not await self.center.require(interaction):
            return
        profile = await self.center.complete_training_module(interaction.guild.id, interaction.user.id, self.module_id)
        await interaction.response.edit_message(
            content=f"Módulo concluído. Você possui **{profile['xp']} XP** e **{profile['progress']}%** de progresso.",
            embed=None,
            view=None,
        )


QUIZ_CONCEPTS = (
    ("fundamentos", "poder e responsabilidade", "Moderação competente usa autoridade como responsabilidade para proteger a cultura."),
    ("fundamentos", "confiança da comunidade", "Aplicar regras com consistência torna o ambiente previsível e fortalece a confiança."),
    ("fundamentos", "hierarquia com propósito", "Cada nível precisa conhecer responsabilidades, limites e quando escalar uma decisão."),
    ("fundamentos", "comunicação interna", "A equipe precisa de espaço interno claro para registrar, revisar e alinhar decisões."),
    ("fundamentos", "regras claras", "Uma boa regra comunica o comportamento esperado, o motivo e as consequências."),
    ("fundamentos", "cultura interna", "A liderança molda a cultura principalmente pelo exemplo e pela consistência."),
    ("fundamentos", "prevenção de conflitos", "Regras claras, canais adequados e diálogo reduzem conflitos antes da punição."),
    ("fundamentos", "gestão de conflitos", "Escuta ativa, investigação dos fatos e comunicação clara devem vir antes da conclusão."),
    ("fundamentos", "segurança do servidor", "Segurança inclui raids, phishing, integrações, backups e monitoramento, não apenas permissões."),
    ("fundamentos", "organização de canais", "Canais devem ter propósito claro e estrutura intuitiva para novos membros."),
    ("fundamentos", "excesso de moderação", "Policiar toda mensagem reduz naturalidade e pode prejudicar o engajamento."),
    ("fundamentos", "favoritismo", "Regras aplicadas de forma diferente para amigos destroem a confiança na gestão."),
    ("fundamentos", "longevidade", "Moderação é função estratégica porque influencia segurança, confiança e permanência dos membros."),
    ("decisao", "treinamento de entrada", "Apresentar regras é diferente de formar alguém para decidir em situações reais."),
    ("decisao", "critério", "Uma decisão deve considerar fato, contexto, histórico, impacto e risco de repetição."),
    ("decisao", "registro do fato", "O primeiro passo é descrever objetivamente o que aconteceu, sem rótulos ou opinião."),
    ("decisao", "identificação da regra", "Relacionar o fato à regra evita punir alguém apenas por antipatia ou tom."),
    ("decisao", "avaliação de contexto", "Avisos anteriores, canal, provocação e exposição mudam a leitura do caso."),
    ("decisao", "risco ativo", "A urgência depende do risco que continua existindo se ninguém agir."),
    ("decisao", "proporcionalidade", "A medida deve ser suficiente para conter o problema sem exceder o necessário."),
    ("decisao", "escalação", "Golpes, ameaças, exposição, acusações graves e punições permanentes pedem revisão adicional."),
    ("decisao", "pós-incidente", "Depois da ação é necessário registrar, comunicar, corrigir erros e revisar prevenção."),
    ("decisao", "prática acompanhada", "O novo moderador deve observar casos, propor decisões e receber feedback antes de maior autonomia."),
    ("decisao", "postura com o cargo", "Acesso interno e autoridade são responsabilidade, não palco ou sinal de superioridade."),
    ("decisao", "capacidade de receber feedback", "Um bom candidato consegue ser corrigido sem transformar feedback em ofensa pessoal."),
    ("decisao", "acolhimento à vítima", "A vítima deve ser acolhida sem promessas sobre resultados que a equipe não controla."),
    ("decisao", "coleta de evidências", "Colete conta, mensagem, link, local, prints e padrão sem agir como investigação policial."),
    ("decisao", "conta comprometida", "Uma conta comprometida precisa ser contida, mas o contexto deve ser preservado no registro."),
    ("decisao", "raid e invasão de bots", "A equipe deve definir antes da crise quem contém, verifica logs, comunica e registra."),
    ("decisao", "revisão após raid", "Após conter o ataque, revise convite, verificação, bots, permissões e tempo de resposta."),
    ("aprendizado", "qualidade da comunidade", "A ferramenta não define qualidade; cultura, regras e pessoas consistentes definem."),
    ("aprendizado", "dúvidas com contexto", "Uma dúvida útil explica objetivo, cenário, tentativa e limitação em vez de pedir resposta pronta."),
    ("aprendizado", "histórico útil", "Decisões e conversas relevantes devem deixar registros que outras pessoas consigam consultar."),
    ("aprendizado", "consistência prática", "Mudanças pequenas repetidas ensinam mais do que esforço intenso seguido de abandono."),
    ("aprendizado", "aprendizado aplicado", "Leitura vira competência quando muda uma decisão ou produz evidência observável."),
    ("aprendizado", "sinais concretos", "Projetos claros, decisões registradas e entregas contínuas demonstram evolução."),
    ("aprendizado", "problema antes da ferramenta", "Entenda o problema e as alternativas antes de escolher ferramenta ou solução."),
    ("aprendizado", "evidência de evolução", "Uma explicação, registro, teste ou processo melhor é sinal mais confiável que sensação de progresso."),
    ("aprendizado", "maturidade", "Maturidade é saber onde o cuidado faz diferença, não transformar tudo em processo pesado."),
    ("aprendizado", "revisão", "Voltar ao conteúdo depois de enfrentar um problema real aprofunda o aprendizado."),
)

QUIZ_PROMPT_TEMPLATES = (
    "Sobre **{topic}**, qual princípio está alinhado ao treinamento?",
    "Qual alternativa demonstra melhor entendimento de **{topic}**?",
    "Em uma situação envolvendo **{topic}**, qual postura é mais responsável?",
    "Qual orientação a Bella deveria reforçar sobre **{topic}**?",
    "Ao revisar um caso de **{topic}**, qual critério deve prevalecer?",
    "Qual prática melhora a atuação da Staff em **{topic}**?",
    "No contexto dos artigos, o que é correto sobre **{topic}**?",
    "Qual decisão evita improviso quando o assunto é **{topic}**?",
    "O que um Ajudante experiente reconhece sobre **{topic}**?",
    "Qual opção representa aprendizado aplicado em **{topic}**?",
)

QUIZ_DISTRACTORS = (
    "Agir pela emoção para demonstrar firmeza rapidamente.",
    "Punir primeiro e investigar apenas se alguém reclamar.",
    "Usar popularidade e amizade como principal critério.",
    "Evitar registros para que o caso seja esquecido mais rápido.",
    "Dar todas as permissões antes de qualquer prática acompanhada.",
    "Tratar volume de mensagens como única medida de qualidade.",
    "Ignorar contexto sempre que alguma regra for mencionada.",
    "Transformar toda situação simples em punição permanente.",
    "Prometer resolver prejuízos externos que a Staff não controla.",
    "Expor denúncias publicamente para pressionar uma resposta.",
    "Tomar a decisão com base em quem é mais conhecido no servidor.",
    "Considerar o cargo uma forma de superioridade sobre membros.",
)


def quiz_catalog_size() -> int:
    return len(QUIZ_CONCEPTS) * len(QUIZ_PROMPT_TEMPLATES) * 24


def build_quiz_item(user_id: int, question_number: int) -> dict[str, Any]:
    seed = (user_id * 1_000_003) ^ (question_number * 97_409)
    rng = random.Random(seed)
    concept_index = (question_number + user_id) % len(QUIZ_CONCEPTS)
    module, topic, correct_answer = QUIZ_CONCEPTS[concept_index]
    template_index = rng.randrange(len(QUIZ_PROMPT_TEMPLATES))
    answers = [correct_answer, *rng.sample(QUIZ_DISTRACTORS, 3)]
    rng.shuffle(answers)
    correct_index = answers.index(correct_answer)
    order_signature = "-".join(str(QUIZ_DISTRACTORS.index(item)) if item in QUIZ_DISTRACTORS else "c" for item in answers)
    return {
        "id": f"{concept_index}:{template_index}:{order_signature}",
        "module": module,
        "question": QUIZ_PROMPT_TEMPLATES[template_index].format(topic=topic),
        "answers": tuple(answers),
        "correct": correct_index,
        "explanation": correct_answer,
        "number": question_number,
    }


class TrainingQuizView(discord.ui.View):
    def __init__(self, center: StaffCenter, user_id: int, item: dict[str, Any]) -> None:
        super().__init__(timeout=300)
        self.center = center
        self.user_id = user_id
        self.item = item
        for index, answer in enumerate(self.item["answers"]):
            button = discord.ui.Button(label=str(answer)[:80], style=discord.ButtonStyle.secondary)

            async def callback(interaction: discord.Interaction, selected: int = index) -> None:
                if interaction.user.id != user_id or interaction.guild is None:
                    await _reply(interaction, "Este quiz pertence a outro Ajudante.")
                    return
                await interaction.response.defer()
                passed = selected == int(self.item["correct"])
                profile = await self.center.record_quiz_result(
                    interaction.guild.id,
                    interaction.user.id,
                    str(self.item["module"]),
                    passed,
                    question_id=str(self.item["id"]),
                )
                ai_feedback = ""
                if self.center.ai_explainer is not None:
                    try:
                        ai_feedback = (
                            await self.center.ai_explainer(
                                "Explique em português brasileiro, em até 5 linhas, por que a resposta correta do "
                                "quiz de formação da Staff faz sentido. Não invente regras do servidor.\n"
                                f"Pergunta: {self.item['question']}\n"
                                f"Resposta escolhida: {self.item['answers'][selected]}\n"
                                f"Resposta correta: {self.item['explanation']}\n"
                                f"O aluno {'acertou' if passed else 'errou'}."
                            )
                        ).strip()[:1200]
                    except Exception as exc:
                        LOGGER.warning("Feedback de quiz por IA indisponível: %s: %s", type(exc).__name__, exc)
                message = (
                    f"**{'Resposta correta' if passed else 'Ainda não'}:** {self.item['explanation']}\n"
                    f"{ai_feedback + chr(10) if ai_feedback else ''}"
                    f"Progresso: **{profile['progress']}%** • XP: **{profile['xp']}**"
                )
                await interaction.edit_original_response(
                    content=message,
                    embed=None,
                    view=TrainingQuizResultView(self.center, user_id),
                )

            button.callback = callback
            self.add_item(button)

    @staticmethod
    def question_embed(item: dict[str, Any]) -> discord.Embed:
        module = TRAINING_MODULES.get(str(item["module"]), {})
        embed = discord.Embed(
            title=f"Quiz da Staff • Pergunta {int(item['number']) + 1}",
            description=str(item["question"]),
            color=0xE39A32,
        )
        embed.add_field(name="Módulo", value=str(module.get("title") or item["module"]), inline=False)
        embed.set_footer(text=f"Banco formativo com {quiz_catalog_size():,} combinações • responda com critério")
        return embed


class TrainingQuizResultView(discord.ui.View):
    def __init__(self, center: StaffCenter, user_id: int) -> None:
        super().__init__(timeout=600)
        self.center = center
        self.user_id = user_id

    @discord.ui.button(label="Próxima pergunta", style=discord.ButtonStyle.success)
    async def next_question(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or interaction.user.id != self.user_id:
            await _reply(interaction, "Este treinamento pertence a outro Ajudante.")
            return
        profile = await self.center.training_profile(interaction.guild.id, self.user_id)
        item = build_quiz_item(self.user_id, int(profile["quiz_attempts"]))
        await interaction.response.edit_message(
            content=None,
            embed=TrainingQuizView.question_embed(item),
            view=TrainingQuizView(self.center, self.user_id, item),
        )


class TrainingTicketControlsView(discord.ui.View):
    def __init__(self, center: StaffCenter) -> None:
        super().__init__(timeout=None)
        self.center = center

    @discord.ui.button(
        label="Nova pergunta",
        style=discord.ButtonStyle.success,
        custom_id="bella:staff:training-ticket:question",
    )
    async def question(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.Thread):
            await _reply(interaction, "Use este botão dentro do atendimento de treinamento.")
            return
        record = await self.center._training_ticket_by_thread(interaction.guild.id, interaction.channel.id)
        if record is None or interaction.user.id != int(record.get("user_id") or 0):
            await _reply(interaction, "Este atendimento pertence a outro Ajudante.")
            return
        profile = await self.center.training_profile(interaction.guild.id, interaction.user.id)
        item = build_quiz_item(interaction.user.id, int(profile["quiz_attempts"]))
        await interaction.response.send_message(
            embed=TrainingQuizView.question_embed(item),
            view=TrainingQuizView(self.center, interaction.user.id, item),
        )

    @discord.ui.button(
        label="Meu progresso",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:staff:training-ticket:progress",
    )
    async def progress(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return
        await interaction.response.send_message(
            embed=await self.center.training_progress_embed(interaction.guild, interaction.user),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Encerrar treinamento",
        style=discord.ButtonStyle.danger,
        custom_id="bella:staff:training-ticket:close",
    )
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.center.close_training_ticket(interaction)


SCENARIOS = (
    {
        "title": "Piada pesada entre membros antigos",
        "description": "Um membro antigo passa do limite em uma piada. A pessoa atingida reclama; a situação já parou.",
        "answers": ("Banir imediatamente", "Ouvir, registrar contexto e aplicar medida proporcional", "Ignorar porque são amigos"),
        "correct": 1,
        "feedback": "A relação entre eles não apaga o impacto, mas a resposta também não deve pular a análise.",
    },
    {
        "title": "Possível golpe por conta comprometida",
        "description": "Uma conta real começou a enviar links suspeitos em massa e uma vítima enviou prints confusos.",
        "answers": ("Conter a conta, preservar evidências, verificar padrão e acolher a vítima", "Culpar a vítima", "Apagar tudo e encerrar"),
        "correct": 0,
        "feedback": "A prioridade é conter o risco, preservar evidências e orientar sem prometer recuperação externa.",
    },
)


class TrainingScenarioView(discord.ui.View):
    def __init__(self, center: StaffCenter, user_id: int) -> None:
        super().__init__(timeout=300)
        self.center = center
        self.user_id = user_id
        self.scenario = SCENARIOS[user_id % len(SCENARIOS)]
        for index, answer in enumerate(self.scenario["answers"]):
            button = discord.ui.Button(label=str(answer)[:80], style=discord.ButtonStyle.secondary)

            async def callback(interaction: discord.Interaction, selected: int = index) -> None:
                if interaction.user.id != self.user_id or interaction.guild is None:
                    await _reply(interaction, "Este cenário pertence a outro Ajudante.")
                    return
                successful = selected == int(self.scenario["correct"])
                profile = await self.center.record_scenario_review(interaction.guild.id, interaction.user.id, successful)
                await interaction.response.edit_message(
                    content=(
                        f"**{'Boa decisão' if successful else 'Revise o critério'}:** {self.scenario['feedback']}\n"
                        f"XP: **{profile['xp']}** • Cenários revisados: **{profile['scenario_reviews']}**"
                    ),
                    embed=None,
                    view=None,
                )

            button.callback = callback
            self.add_item(button)

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title=f"Situação prática • {self.scenario['title']}",
            description=str(self.scenario["description"]),
            color=0xE39A32,
        )


class SupremeAdminPanelView(discord.ui.View):
    def __init__(self, center: StaffCenter) -> None:
        super().__init__(timeout=None)
        self.center = center

    @discord.ui.button(
        label="Banir membros por cargo",
        style=discord.ButtonStyle.danger,
        custom_id="bella:staff:supreme:mass-ban",
        row=0,
    )
    async def mass_ban(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.center.require_president(interaction):
            return
        await interaction.response.send_message(
            "Selecione o cargo. A Bella mostrará a quantidade exata antes de permitir a confirmação.",
            view=SupremeRoleSelectView(self.center),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Lockdown dos canais",
        style=discord.ButtonStyle.danger,
        custom_id="bella:staff:supreme:lockdown",
        row=0,
    )
    async def lockdown(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.center.require_president(interaction):
            await interaction.response.send_modal(
                SupremeCriticalActionModal(self.center, "lockdown", "TRANCAR SERVIDOR")
            )

    @discord.ui.button(
        label="Restaurar canais",
        style=discord.ButtonStyle.success,
        custom_id="bella:staff:supreme:restore-lockdown",
        row=0,
    )
    async def restore_lockdown(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.center.require_president(interaction):
            await interaction.response.send_modal(
                SupremeCriticalActionModal(self.center, "restore_lockdown", "RESTAURAR CANAIS")
            )

    @discord.ui.button(
        label="Bloquear Bella pública",
        style=discord.ButtonStyle.danger,
        custom_id="bella:staff:supreme:block-public",
        row=1,
    )
    async def block_public(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.center.require_president(interaction):
            await interaction.response.send_modal(
                SupremeCriticalActionModal(self.center, "block_public", "BLOQUEAR BELLA")
            )

    @discord.ui.button(
        label="Liberar Bella pública",
        style=discord.ButtonStyle.success,
        custom_id="bella:staff:supreme:release-public",
        row=1,
    )
    async def release_public(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.center.require_president(interaction):
            await interaction.response.send_modal(
                SupremeCriticalActionModal(self.center, "release_public", "LIBERAR BELLA")
            )

    @discord.ui.button(
        label="Contenção total",
        style=discord.ButtonStyle.danger,
        custom_id="bella:staff:supreme:containment",
        row=1,
    )
    async def containment(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.center.require_president(interaction):
            await interaction.response.send_modal(
                SupremeCriticalActionModal(self.center, "containment", "CONTENCAO TOTAL")
            )

    @discord.ui.button(
        label="Restaurar tudo",
        style=discord.ButtonStyle.success,
        custom_id="bella:staff:supreme:restore-all",
        row=1,
    )
    async def restore_all(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.center.require_president(interaction):
            await interaction.response.send_modal(
                SupremeCriticalActionModal(self.center, "restore_all", "RESTAURAR TUDO")
            )

    @discord.ui.button(
        label="Diagnóstico emergencial",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:staff:supreme:diagnostics",
        row=2,
    )
    async def diagnostics(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.center.require_president(interaction) or interaction.guild is None:
            return
        await interaction.response.send_message(
            embed=await self.center.supreme_diagnostics_embed(interaction.guild),
            ephemeral=True,
        )


class SupremeRoleSelect(discord.ui.RoleSelect):
    def __init__(self, center: StaffCenter) -> None:
        super().__init__(placeholder="Selecione o cargo que será removido do servidor", min_values=1, max_values=1)
        self.center = center

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.center.require_president(interaction) or interaction.guild is None:
            return
        role = self.values[0] if self.values else None
        if role is None:
            await interaction.response.send_message("Selecione um cargo válido.", ephemeral=True)
            return
        if role.id in {interaction.guild.id, OFFICIAL_ROLE_IDS["president"]} or role.managed:
            await interaction.response.send_message(
                "O cargo @everyone, o cargo Presidente e cargos gerenciados são protegidos.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await interaction.guild.chunk(cache=True)
        except (discord.ClientException, discord.HTTPException):
            pass
        bot_member = interaction.guild.me
        if bot_member is None:
            await interaction.followup.send("A Bella ainda não está disponível.", ephemeral=True)
            return
        candidates = mass_ban_candidates(
            list(role.members),
            role_id=role.id,
            owner_id=interaction.guild.owner_id,
            bot_user_id=bot_member.id,
        )
        if not candidates:
            await interaction.followup.send(
                f"Nenhum membro elegível foi encontrado no cargo {role.mention}.",
                ephemeral=True,
            )
            return
        protected = len(role.members) - len(candidates)
        preview = discord.Embed(
            title="Prévia do banimento por cargo",
            description=(
                f"Cargo: {role.mention} (`{role.id}`)\n"
                f"Membros que poderão ser banidos: **{len(candidates)}**\n"
                f"Protegidos/excluídos: **{protected}**\n\n"
                "A lista fica congelada nesta confirmação. Novos membros não serão incluídos silenciosamente."
            ),
            color=0xED4245,
        )
        await interaction.followup.send(
            embed=preview,
            view=SupremeMassBanPreviewView(
                self.center,
                interaction.user.id,
                role.id,
                {member.id for member in candidates},
            ),
            ephemeral=True,
        )


class SupremeRoleSelectView(discord.ui.View):
    def __init__(self, center: StaffCenter) -> None:
        super().__init__(timeout=600)
        self.add_item(SupremeRoleSelect(center))


class SupremeMassBanPreviewView(discord.ui.View):
    def __init__(self, center: StaffCenter, owner_id: int, role_id: int, member_ids: set[int]) -> None:
        super().__init__(timeout=600)
        self.center = center
        self.owner_id = owner_id
        self.role_id = role_id
        self.member_ids = set(member_ids)

    @discord.ui.button(label="Prosseguir para confirmação", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.owner_id or not await self.center.require_president(interaction):
            return
        await interaction.response.send_modal(
            SupremeMassBanConfirmModal(self.center, self.role_id, self.member_ids)
        )


class SupremeMassBanConfirmModal(discord.ui.Modal):
    def __init__(self, center: StaffCenter, role_id: int, member_ids: set[int]) -> None:
        super().__init__(title="Confirmar banimento em massa", timeout=600)
        self.center = center
        self.role_id = role_id
        self.member_ids = set(member_ids)
        self.reason_input = discord.ui.TextInput(
            label="Motivo detalhado",
            style=discord.TextStyle.paragraph,
            min_length=8,
            max_length=1000,
        )
        self.confirm_input = discord.ui.TextInput(
            label=f"Digite BANIR {len(member_ids)}",
            max_length=30,
            placeholder=f"BANIR {len(member_ids)}",
        )
        self.add_item(self.reason_input)
        self.add_item(self.confirm_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.center.require_president(interaction) or interaction.guild is None:
            return
        expected = f"BANIR {len(self.member_ids)}"
        if str(self.confirm_input).strip().upper() != expected:
            await interaction.response.send_message(f"Confirmação inválida. Digite exatamente `{expected}`.", ephemeral=True)
            return
        role = interaction.guild.get_role(self.role_id)
        if role is None:
            await interaction.response.send_message("O cargo não existe mais.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(
            f"Banimento iniciado para até **{len(self.member_ids)}** membro(s). O resultado será enviado ao log supremo.",
            ephemeral=True,
        )

        async def runner() -> None:
            try:
                await self.center.mass_ban_role(
                    interaction.guild,
                    actor=interaction.user,
                    role=role,
                    reason=str(self.reason_input),
                    approved_member_ids=self.member_ids,
                )
            except Exception:
                LOGGER.exception("Falha no banimento em massa do cargo %s", role.id)

        asyncio.create_task(runner(), name=f"supreme-mass-ban:{interaction.guild.id}:{role.id}")


class SupremeCriticalActionModal(discord.ui.Modal):
    def __init__(self, center: StaffCenter, action: str, confirmation: str) -> None:
        super().__init__(title="Confirmar ação crítica", timeout=600)
        self.center = center
        self.action = action
        self.confirmation = confirmation
        self.announcement_channel_input: discord.ui.TextInput | None = None
        self.announcement_input: discord.ui.TextInput | None = None
        self.reason_input = discord.ui.TextInput(
            label="Motivo da ação",
            style=discord.TextStyle.paragraph,
            min_length=8,
            max_length=1000,
        )
        self.confirm_input = discord.ui.TextInput(
            label=f"Digite: {confirmation}"[:45],
            placeholder=confirmation,
            max_length=45,
        )
        self.add_item(self.reason_input)
        if action in {"lockdown", "containment"}:
            self.announcement_channel_input = discord.ui.TextInput(
                label="ID do canal público do anúncio",
                placeholder="Ex.: 123456789012345678",
                max_length=20,
            )
            self.announcement_input = discord.ui.TextInput(
                label="Anúncio enviado após o lockdown",
                style=discord.TextStyle.paragraph,
                min_length=10,
                max_length=4000,
                placeholder="Explique o motivo e oriente os membros sobre o que fazer.",
            )
            self.add_item(self.announcement_channel_input)
            self.add_item(self.announcement_input)
        self.add_item(self.confirm_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.center.require_president(interaction) or interaction.guild is None:
            return
        if str(self.confirm_input).strip().upper() != self.confirmation:
            await interaction.response.send_message(
                f"Confirmação inválida. Digite exatamente `{self.confirmation}`.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        reason = str(self.reason_input).strip()
        actor = interaction.user
        announcement_channel_id: int | None = None
        announcement = ""
        if self.action in {"lockdown", "containment"}:
            channel_value = str(self.announcement_channel_input or "").strip()
            announcement = str(self.announcement_input or "").strip()
            if not channel_value.isdigit() or interaction.guild.get_channel(int(channel_value)) is None:
                await interaction.followup.send("Informe um ID válido de canal de texto para o anúncio.", ephemeral=True)
                return
            announcement_channel_id = int(channel_value)
        try:
            if self.action == "lockdown":
                result = await self.center.lockdown_server(interaction.guild, actor=actor, reason=reason)
                announcement_message = await self.center.send_lockdown_announcement(
                    interaction.guild,
                    actor=actor,
                    channel_id=int(announcement_channel_id or 0),
                    announcement=announcement,
                )
                message = (
                    f"Lockdown aplicado em **{result.get('changed_channels', 0)}** canal(is) e anúncio "
                    f"publicado em {announcement_message.channel.mention}."
                )
            elif self.action == "restore_lockdown":
                result = await self.center.restore_lockdown(interaction.guild, actor=actor, reason=reason)
                message = f"Canais restaurados. Estado: **{result.get('lockdown_status')}**."
            elif self.action == "block_public":
                await self.center.set_public_emergency(interaction.guild, actor=actor, enabled=True, reason=reason)
                message = "Todos os sistemas públicos da Bella foram bloqueados."
            elif self.action == "release_public":
                await self.center.set_public_emergency(interaction.guild, actor=actor, enabled=False, reason=reason)
                message = "Os sistemas públicos da Bella foram liberados."
            elif self.action == "containment":
                result = await self.center.lockdown_server(interaction.guild, actor=actor, reason=reason)
                await self.center.set_public_emergency(interaction.guild, actor=actor, enabled=True, reason=reason)
                announcement_message = await self.center.send_lockdown_announcement(
                    interaction.guild,
                    actor=actor,
                    channel_id=int(announcement_channel_id or 0),
                    announcement=announcement,
                )
                message = (
                    f"Contenção total aplicada em **{result.get('changed_channels', 0)}** canal(is) e anúncio "
                    f"publicado em {announcement_message.channel.mention}."
                )
            elif self.action == "restore_all":
                state = await self.center.supreme_state(interaction.guild.id)
                if state.get("lockdown_active"):
                    await self.center.restore_lockdown(interaction.guild, actor=actor, reason=reason)
                if self.center.maintenance and await self.center.maintenance.is_active():
                    await self.center.set_public_emergency(interaction.guild, actor=actor, enabled=False, reason=reason)
                message = "Canais e sistemas públicos foram restaurados."
            else:
                raise RuntimeError("Ação crítica desconhecida.")
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(message, ephemeral=True)


class StaffReportsPanelView(discord.ui.View):
    def __init__(self, center: StaffCenter) -> None:
        super().__init__(timeout=None)
        self.center = center

    @discord.ui.button(label="Gerar relatório", style=discord.ButtonStyle.primary, custom_id="bella:staff:channel:report")
    async def report(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not await self.center.require(interaction, minimum="vice_president"):
            return
        await _reply(interaction, embed=await self.center.report_embed(interaction.guild))


class StaffAuditPanelView(discord.ui.View):
    def __init__(self, center: StaffCenter) -> None:
        super().__init__(timeout=None)
        self.center = center

    @discord.ui.button(label="Ver auditoria", style=discord.ButtonStyle.primary, custom_id="bella:staff:channel:audit")
    async def audit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not await self.center.require(interaction, minimum="vice_president"):
            return
        await _reply(interaction, embed=await self.center.audit_embed(interaction.guild.id))


class StaffRequestsPanelView(discord.ui.View):
    def __init__(self, center: StaffCenter) -> None:
        super().__init__(timeout=None)
        self.center = center

    @discord.ui.button(label="Nova solicitação", style=discord.ButtonStyle.primary, custom_id="bella:staff:channel:request:new")
    async def create(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.center.require(interaction):
            await interaction.response.send_modal(InterventionModal(self.center))

    @discord.ui.button(label="Ver pendentes", style=discord.ButtonStyle.secondary, custom_id="bella:staff:channel:request:list")
    async def pending(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not await self.center.require(interaction, minimum="vice_president"):
            return
        records = await self.center._guild_records(STAFF_INTERVENTIONS_SECTION, interaction.guild.id)
        lines = [f"**#{item['id']}** • {item['subject']} • <@{item['actor_id']}>" for item in records if item.get("status") == "pending"]
        await _reply(interaction, "## Solicitações pendentes\n" + ("\n".join(lines[:15]) or "Nenhuma pendência."))


class StaffOccurrencesPanelView(discord.ui.View):
    def __init__(self, center: StaffCenter) -> None:
        super().__init__(timeout=None)
        self.center = center

    @discord.ui.button(label="Registrar ocorrência", style=discord.ButtonStyle.danger, custom_id="bella:staff:channel:occurrence:new")
    async def create(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.center.require(interaction):
            await interaction.response.send_modal(OccurrenceModal(self.center))

    @discord.ui.button(label="Ver recentes", style=discord.ButtonStyle.secondary, custom_id="bella:staff:channel:occurrence:list")
    async def recent(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not await self.center.require(interaction):
            return
        records = await self.center._guild_records(STAFF_OCCURRENCES_SECTION, interaction.guild.id)
        records.sort(key=lambda item: int(item.get("id") or 0), reverse=True)
        lines = [f"**#{item['id']}** • <@{item['target_id']}> • {item['title']}" for item in records[:15]]
        await _reply(interaction, "## Ocorrências recentes\n" + ("\n".join(lines) or "Nenhuma ocorrência."))


class StaffCommandPanelView(discord.ui.View):
    def __init__(self, center: StaffCenter) -> None:
        super().__init__(timeout=None)
        self.center = center

    async def _open(self, interaction: discord.Interaction, action: str) -> None:
        if await self.center.require(interaction):
            try:
                await interaction.response.send_modal(ModerationActionModal(self.center, action))
            except discord.HTTPException as exc:
                LOGGER.exception("Falha ao abrir formulário moderativo %s: %s", action, exc)
                await _reply(
                    interaction,
                    "Não consegui abrir o formulário agora. A falha foi registrada; tente novamente em instantes.",
                )

    @discord.ui.button(label="Banimento", style=discord.ButtonStyle.danger, custom_id="bella:staff:command:ban")
    async def ban(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._open(interaction, "ban")

    @discord.ui.button(label="Kick", style=discord.ButtonStyle.danger, custom_id="bella:staff:command:kick")
    async def kick(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._open(interaction, "kick")

    @discord.ui.button(label="Mute", style=discord.ButtonStyle.primary, custom_id="bella:staff:command:timeout")
    async def timeout_member(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._open(interaction, "timeout")

    @discord.ui.button(label="Advertência", style=discord.ButtonStyle.secondary, custom_id="bella:staff:command:warning")
    async def warning(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._open(interaction, "warning")

    @discord.ui.button(label="Consultar membro", style=discord.ButtonStyle.secondary, custom_id="bella:staff:command:member")
    async def member(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.center.require(interaction):
            await interaction.response.send_modal(MemberLookupModal(self.center))

    @discord.ui.button(label="Registrar ocorrência", style=discord.ButtonStyle.secondary, custom_id="bella:staff:command:occurrence")
    async def occurrence(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.center.require(interaction):
            await interaction.response.send_modal(OccurrenceModal(self.center))


class ModerationActionModal(discord.ui.Modal):
    def __init__(self, center: StaffCenter, action: str) -> None:
        super().__init__(title=f"Ação da Staff • {action.title()}", timeout=600)
        self.center = center
        self.action = action
        self.target_input = discord.ui.TextInput(label=None, max_length=20)
        self.reason_input = discord.ui.TextInput(label=None, style=discord.TextStyle.paragraph, max_length=1500)
        self.duration_input = discord.ui.TextInput(
            label=None,
            default="60",
            required=action == "timeout",
            max_length=5,
        )
        self.evidence_upload = discord.ui.FileUpload(required=True, min_values=1, max_values=1)
        self.add_item(discord.ui.Label(text="Membro afetado", component=self.target_input))
        self.add_item(discord.ui.Label(text="Justificativa", component=self.reason_input))
        if action == "timeout":
            self.add_item(discord.ui.Label(text="Duração do mute", component=self.duration_input))
        self.add_item(
            discord.ui.Label(
                text="Imagem obrigatória",
                description="Anexe print ou evidência visual da ocorrência.",
                component=self.evidence_upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not str(self.target_input).isdigit() or not self.evidence_upload.values:
            await _reply(interaction, "Informe um ID válido e anexe uma imagem.")
            return
        duration = 60
        if self.action == "timeout":
            if not str(self.duration_input).isdigit():
                await _reply(interaction, "A duração precisa ser informada em minutos.")
                return
            duration = int(str(self.duration_input))
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            record = await self.center.execute_moderation_action(
                interaction,
                action=self.action,
                target_id=int(str(self.target_input)),
                reason=str(self.reason_input),
                evidence=self.evidence_upload.values[0],
                duration_minutes=duration,
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        extra = f" • advertência {record['warning_count']}" if record.get("warning_count") else ""
        await interaction.followup.send(
            f"Ação **{record['effective_action']}** aplicada e registrada no log privado{extra}.", ephemeral=True
        )


class StaffSuggestionPanelView(discord.ui.View):
    def __init__(self, center: StaffCenter) -> None:
        super().__init__(timeout=None)
        self.center = center

    @discord.ui.button(label="Enviar sugestão", style=discord.ButtonStyle.primary, custom_id="bella:staff:suggestion:new")
    async def suggest(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.center.require(interaction):
            await interaction.response.send_modal(StaffSuggestionModal(self.center))


class StaffSuggestionModal(discord.ui.Modal, title="Sugestão para a Administração"):
    suggestion_title = discord.ui.TextInput(label="Título", max_length=120)
    description = discord.ui.TextInput(label="Sugestão", style=discord.TextStyle.paragraph, max_length=2000)
    benefit = discord.ui.TextInput(label="Qual problema resolve?", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, center: StaffCenter) -> None:
        super().__init__()
        self.center = center

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            record = await self.center.create_suggestion(
                interaction.guild,
                interaction.user,
                str(self.suggestion_title),
                str(self.description),
                str(self.benefit),
            )
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(f"Sugestão **#{record['id']}** enviada para análise.", ephemeral=True)


class StaffSuggestionReviewView(discord.ui.LayoutView):
    def __init__(self, center: StaffCenter, record: dict[str, Any] | None = None) -> None:
        super().__init__(timeout=None)
        self.center = center
        self.record = record or {}
        self._render()

    def _render(self) -> None:
        status = str(self.record.get("status") or "pending")
        labels = {"pending": "Em análise", "approved": "Aprovada", "rejected": "Recusada"}
        colors = {"pending": 0xE39A32, "approved": 0x57F287, "rejected": 0xED4245}
        title = str(self.record.get("title") or "Sugestão da Staff")
        description = str(self.record.get("description") or "O conteúdo será carregado ao interagir.")
        benefit = str(self.record.get("benefit") or "Não informado")
        author = f"{self.record.get('author_name', 'Staff')} (`{self.record.get('author_id', '—')}`)"
        text = (
            f"# 💡 {title}\n{description}\n\n"
            f"**Benefício esperado:** {benefit}\n"
            f"**Enviada por:** {author}\n"
            f"**Status:** {labels.get(status, status)}"
        )
        if self.record.get("reviewed_by"):
            text += f"\n**Revisada por:** {self.record.get('reviewer_name', 'Liderança')} (`{self.record['reviewed_by']}`)"
        if self.record.get("review_reason"):
            text += f"\n**Observação:** {self.record['review_reason']}"
        container = discord.ui.Container(accent_color=discord.Color(colors.get(status, 0xE39A32)))
        container.add_item(discord.ui.TextDisplay(text[:4000]))
        if status == "pending":
            actions = discord.ui.ActionRow()
            approve = discord.ui.Button(
                label="Aprovar", style=discord.ButtonStyle.success, custom_id="bella:staff:suggestion:approve"
            )
            reject = discord.ui.Button(
                label="Recusar", style=discord.ButtonStyle.danger, custom_id="bella:staff:suggestion:reject"
            )
            approve.callback = self._approve
            reject.callback = self._reject
            actions.add_item(approve)
            actions.add_item(reject)
            container.add_item(actions)
        self.add_item(container)

    async def _approve(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return
        if not await self.center.require(interaction, minimum="vice_president"):
            return
        try:
            record = await self.center.review_suggestion(
                interaction.guild, interaction.user, interaction.message.id, status="approved"
            )
        except ValueError as exc:
            await _reply(interaction, str(exc))
            return
        await interaction.response.edit_message(view=StaffSuggestionReviewView(self.center, record))

    async def _reject(self, interaction: discord.Interaction) -> None:
        if not await self.center.require(interaction, minimum="vice_president"):
            return
        await interaction.response.send_modal(StaffSuggestionRejectModal(self.center, interaction.message.id))


class StaffSuggestionRejectModal(discord.ui.Modal, title="Recusar sugestão"):
    reason = discord.ui.TextInput(label="Motivo", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, center: StaffCenter, message_id: int) -> None:
        super().__init__()
        self.center = center
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return
        try:
            record = await self.center.review_suggestion(
                interaction.guild,
                interaction.user,
                self.message_id,
                status="rejected",
                reason=str(self.reason),
            )
        except ValueError as exc:
            await _reply(interaction, str(exc))
            return
        await interaction.response.edit_message(view=StaffSuggestionReviewView(self.center, record))


class OccurrenceModal(discord.ui.Modal, title="Registrar ocorrência"):
    member_id = discord.ui.TextInput(label="ID do membro", max_length=20)
    occurrence_title = discord.ui.TextInput(label="Título", max_length=120)
    details = discord.ui.TextInput(label="Descrição e evidências", style=discord.TextStyle.paragraph, max_length=2000)

    def __init__(self, center: StaffCenter) -> None:
        super().__init__()
        self.center = center

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not str(self.member_id).isdigit():
            await _reply(interaction, "Informe um ID de membro válido.")
            return
        record = await self.center.create_occurrence(
            interaction.guild.id, interaction.user.id, int(str(self.member_id)), str(self.occurrence_title), str(self.details)
        )
        await _reply(interaction, f"Ocorrência **#{record['id']}** registrada para revisão.")


class MemberLookupModal(discord.ui.Modal, title="Consultar membro"):
    member_id = discord.ui.TextInput(label="ID do membro", max_length=20)

    def __init__(self, center: StaffCenter) -> None:
        super().__init__()
        self.center = center

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not str(self.member_id).isdigit():
            await _reply(interaction, "Informe um ID válido.")
            return
        member = interaction.guild.get_member(int(str(self.member_id)))
        if member is None:
            await _reply(interaction, "Esse membro não está disponível no cache do servidor.")
            return
        await _reply(interaction, embed=await self.center.member_embed(interaction.guild, member))


class InterventionModal(discord.ui.Modal, title="Solicitar intervenção"):
    subject = discord.ui.TextInput(label="Assunto", max_length=120)
    details = discord.ui.TextInput(label="Contexto", style=discord.TextStyle.paragraph, max_length=2000)

    def __init__(self, center: StaffCenter) -> None:
        super().__init__()
        self.center = center

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        record = await self.center.create_intervention(
            interaction.guild.id, interaction.user.id, str(self.subject), str(self.details)
        )
        await _reply(interaction, f"Solicitação **#{record['id']}** enviada à liderança.")


class StaffCopilotModal(discord.ui.Modal, title="Perguntar ao Copiloto"):
    question = discord.ui.TextInput(label="Pergunta", style=discord.TextStyle.paragraph, max_length=2000)

    def __init__(self, center: StaffCenter) -> None:
        super().__init__()
        self.center = center

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        answer = await self.center.copilot(str(self.question), interaction.guild, interaction.user)
        await interaction.followup.send(answer, ephemeral=True)


async def _reply(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(content, embed=embed, ephemeral=True)


def _discord_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "Data indisponível"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return f"<t:{int(parsed.timestamp())}:R>"


def _channel_name_with_divider(name: str) -> str:
    cleaned = name.strip()
    for separator in ("╺╸", "────", "───", "──", "━━", "——"):
        if separator not in cleaned:
            continue
        prefix, base = cleaned.split(separator, 1)
        prefix = prefix.strip()
        base = base.strip(" ╺╸─━—-")
        if prefix and base:
            return f"{prefix}╺╸{base}"[:100]
    return cleaned[:100]
