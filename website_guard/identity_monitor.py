from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

import discord

from .state import StateStore

FilterKind = Literal["nickname", "biography"]
AuthorizationCallback = Callable[[discord.Member], bool]
NICKNAME_SECTION = "nickname_monitor_configs"
BIOGRAPHY_SECTION = "biography_monitor_configs"

DEFAULT_NICKNAME_TERMS = (
    "admin",
    "moderador",
    "suporte",
    "discord",
    "staff",
    "proprietário",
    "dono",
    "adm",
    "administrador",
)

DEFAULT_BIOGRAPHY_TERMS = (
    "Moderador do Escritório do Talisson Souza",
    "Moderador do Servidor do Talisson Souza",
    "Administrador do Talisson Souza",
    "Funcionário da Equipe Talisson Souza",
    "Staff Oficial",
    "Suporte Oficial",
    "Equipe Oficial",
    "Dono do Servidor",
    "Desenvolvedor Oficial",
)

CONFUSABLES = str.maketrans(
    {
        "а": "a",
        "ɑ": "a",
        "α": "a",
        "е": "e",
        "ε": "e",
        "о": "o",
        "ο": "o",
        "р": "p",
        "ρ": "p",
        "с": "c",
        "ϲ": "c",
        "х": "x",
        "χ": "x",
        "у": "y",
        "υ": "y",
        "і": "i",
        "ι": "i",
        "ј": "j",
        "ѕ": "s",
        "м": "m",
        "κ": "k",
        "к": "k",
        "τ": "t",
        "т": "t",
        "н": "h",
        "η": "n",
        "в": "b",
        "β": "b",
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "8": "b",
        "9": "g",
        "@": "a",
        "$": "s",
    }
)


@dataclass(frozen=True)
class IdentityFilterConfig:
    guild_id: int
    enabled: bool = False
    log_channel_id: int | None = None
    prohibited_terms: tuple[str, ...] = ()
    allowed_values: tuple[str, ...] = ()
    exempt_user_ids: frozenset[int] = field(default_factory=frozenset)
    exempt_role_ids: frozenset[int] = field(default_factory=frozenset)

    def to_dict(self) -> dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "enabled": self.enabled,
            "log_channel_id": self.log_channel_id,
            "prohibited_terms": list(self.prohibited_terms),
            "allowed_values": list(self.allowed_values),
            "exempt_user_ids": sorted(self.exempt_user_ids),
            "exempt_role_ids": sorted(self.exempt_role_ids),
        }

    @classmethod
    def from_dict(
        cls,
        guild_id: int,
        value: Any,
        *,
        kind: FilterKind,
    ) -> "IdentityFilterConfig":
        data = value if isinstance(value, dict) else {}
        defaults = (
            DEFAULT_NICKNAME_TERMS
            if kind == "nickname"
            else DEFAULT_BIOGRAPHY_TERMS
        )
        prohibited = data.get("prohibited_terms", defaults)
        if not isinstance(prohibited, list):
            prohibited = list(defaults)
        return cls(
            guild_id=guild_id,
            enabled=bool(data.get("enabled", False)),
            log_channel_id=_optional_int(data.get("log_channel_id")),
            prohibited_terms=tuple(
                _deduplicate(str(item) for item in prohibited if str(item).strip())
            ),
            allowed_values=tuple(
                _deduplicate(
                    str(item)
                    for item in data.get("allowed_values", [])
                    if str(item).strip()
                )
            ),
            exempt_user_ids=frozenset(
                _integer_values(data.get("exempt_user_ids", []))
            ),
            exempt_role_ids=frozenset(
                _integer_values(data.get("exempt_role_ids", []))
            ),
        )


@dataclass(frozen=True)
class IdentityCheckResult:
    matched: bool
    term: str = ""
    normalized_value: str = ""


class IdentityMonitor:
    def __init__(
        self,
        bot: discord.Client,
        state: StateStore,
        *,
        owner_id: int,
        admin_role_ids: set[int] | None = None,
        authorize: AuthorizationCallback | None = None,
    ) -> None:
        self.bot = bot
        self.state = state
        self.owner_id = owner_id
        self.admin_role_ids = set(admin_role_ids or ())
        self.authorize = authorize
        self._config_cache: dict[
            tuple[int, FilterKind],
            IdentityFilterConfig,
        ] = {}

    def nickname_panel_view(self) -> "NicknameMonitorPanelView":
        return NicknameMonitorPanelView(self)

    async def nickname_status_embed(self, guild: discord.Guild) -> discord.Embed:
        config = await self.config(guild.id, "nickname")
        terms = "\n".join(f"• {term}" for term in config.prohibited_terms) or "Nenhum"
        allowed = "\n".join(f"• {value}" for value in config.allowed_values) or "Nenhum"
        embed = discord.Embed(
            title="Monitor de Nicknames",
            description=(
                "Configuração atual do filtro local de nomes. A Bella usa eventos oficiais "
                "do Discord e não depende de IA para aplicar as regras."
            ),
            color=(
                discord.Color.from_rgb(46, 160, 67)
                if config.enabled
                else discord.Color.from_rgb(210, 153, 34)
            ),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Estado", value="Ativo" if config.enabled else "Pausado", inline=True)
        embed.add_field(
            name="Canal de logs",
            value=f"<#{config.log_channel_id}>" if config.log_channel_id else "Não configurado",
            inline=True,
        )
        embed.add_field(name="Termos bloqueados", value=str(len(config.prohibited_terms)), inline=True)
        embed.add_field(name="Lista de bloqueio", value=terms[:1024], inline=False)
        embed.add_field(name="Nomes permitidos", value=allowed[:1024], inline=False)
        embed.add_field(
            name="Isenções",
            value=(
                f"Usuários: **{len(config.exempt_user_ids)}**\n"
                f"Cargos: **{len(config.exempt_role_ids)}**"
            ),
            inline=False,
        )
        return embed

    async def config(
        self,
        guild_id: int,
        kind: FilterKind,
    ) -> IdentityFilterConfig:
        cache_key = (guild_id, kind)
        cached = self._config_cache.get(cache_key)
        if cached is not None:
            return cached
        section = await self.state.get_section(_section(kind))
        config = IdentityFilterConfig.from_dict(
            guild_id,
            section.get(str(guild_id)),
            kind=kind,
        )
        self._config_cache[cache_key] = config
        return config

    async def configure(
        self,
        guild_id: int,
        kind: FilterKind,
        *,
        log_channel_id: int,
        enabled: bool,
    ) -> IdentityFilterConfig:
        current = await self.config(guild_id, kind)
        updated = IdentityFilterConfig(
            guild_id=guild_id,
            enabled=enabled,
            log_channel_id=log_channel_id,
            prohibited_terms=current.prohibited_terms,
            allowed_values=current.allowed_values,
            exempt_user_ids=current.exempt_user_ids,
            exempt_role_ids=current.exempt_role_ids,
        )
        await self._save(kind, updated)
        return updated

    async def set_enabled(
        self,
        guild_id: int,
        kind: FilterKind,
        enabled: bool,
    ) -> IdentityFilterConfig:
        current = await self.config(guild_id, kind)
        updated = IdentityFilterConfig(
            guild_id=current.guild_id,
            enabled=enabled,
            log_channel_id=current.log_channel_id,
            prohibited_terms=current.prohibited_terms,
            allowed_values=current.allowed_values,
            exempt_user_ids=current.exempt_user_ids,
            exempt_role_ids=current.exempt_role_ids,
        )
        await self._save(kind, updated)
        return updated

    async def add_term(
        self,
        guild_id: int,
        kind: FilterKind,
        term: str,
    ) -> IdentityFilterConfig:
        clean = " ".join(term.split()).strip()
        if not clean:
            raise ValueError("Informe um termo válido.")
        current = await self.config(guild_id, kind)
        terms = _deduplicate((*current.prohibited_terms, clean))
        updated = _replace_config(current, prohibited_terms=tuple(terms))
        await self._save(kind, updated)
        return updated

    async def remove_term(
        self,
        guild_id: int,
        kind: FilterKind,
        term: str,
    ) -> tuple[IdentityFilterConfig, bool]:
        current = await self.config(guild_id, kind)
        normalized = normalize_identity_text(term)
        terms = tuple(
            item
            for item in current.prohibited_terms
            if normalize_identity_text(item) != normalized
        )
        removed = len(terms) != len(current.prohibited_terms)
        updated = _replace_config(current, prohibited_terms=terms)
        if removed:
            await self._save(kind, updated)
        return updated, removed

    async def add_allowed_value(
        self,
        guild_id: int,
        kind: FilterKind,
        value: str,
    ) -> IdentityFilterConfig:
        clean = " ".join(value.split()).strip()
        if not clean:
            raise ValueError("Informe um valor válido.")
        current = await self.config(guild_id, kind)
        allowed = _deduplicate((*current.allowed_values, clean))
        updated = _replace_config(current, allowed_values=tuple(allowed))
        await self._save(kind, updated)
        return updated

    async def remove_allowed_value(
        self,
        guild_id: int,
        kind: FilterKind,
        value: str,
    ) -> tuple[IdentityFilterConfig, bool]:
        current = await self.config(guild_id, kind)
        normalized = normalize_identity_text(value)
        allowed = tuple(
            item
            for item in current.allowed_values
            if normalize_identity_text(item) != normalized
        )
        removed = len(allowed) != len(current.allowed_values)
        updated = _replace_config(current, allowed_values=allowed)
        if removed:
            await self._save(kind, updated)
        return updated, removed

    async def set_user_exemption(
        self,
        guild_id: int,
        kind: FilterKind,
        user_id: int,
        exempt: bool,
    ) -> IdentityFilterConfig:
        current = await self.config(guild_id, kind)
        values = set(current.exempt_user_ids)
        values.add(user_id) if exempt else values.discard(user_id)
        updated = _replace_config(current, exempt_user_ids=frozenset(values))
        await self._save(kind, updated)
        return updated

    async def set_role_exemption(
        self,
        guild_id: int,
        kind: FilterKind,
        role_id: int,
        exempt: bool,
    ) -> IdentityFilterConfig:
        current = await self.config(guild_id, kind)
        values = set(current.exempt_role_ids)
        values.add(role_id) if exempt else values.discard(role_id)
        updated = _replace_config(current, exempt_role_ids=frozenset(values))
        await self._save(kind, updated)
        return updated

    async def inspect_nickname(
        self,
        member: discord.Member,
        *,
        previous_name: str | None = None,
    ) -> IdentityCheckResult:
        config = await self.config(member.guild.id, "nickname")
        value = member.display_name
        result = inspect_identity_value(value, config)
        if not config.enabled or not result.matched or self._is_exempt(member, config):
            return IdentityCheckResult(False)

        replacement = member.name[:32]
        changed = False
        failure = ""
        try:
            await member.edit(
                nick=replacement,
                reason=(
                    "Nickname continha termo bloqueado pelo monitor local "
                    f"da Bella: {result.term}"
                ),
            )
            changed = True
        except discord.Forbidden:
            failure = "O bot não possui permissão ou hierarquia para editar o nickname."
        except discord.HTTPException as exc:
            failure = f"O Discord recusou a alteração: {exc}"

        if changed:
            await self._send_nickname_dm(member)
        await self._send_nickname_log(
            member,
            config,
            old_name=previous_name or value,
            new_name=replacement if changed else value,
            term=result.term,
            changed=changed,
            failure=failure,
        )
        return result

    async def inspect_biography(
        self,
        member: discord.Member,
        biography: str,
    ) -> IdentityCheckResult:
        config = await self.config(member.guild.id, "biography")
        result = inspect_identity_value(biography, config)
        if not config.enabled or not result.matched or self._is_exempt(member, config):
            return IdentityCheckResult(False)
        dm_sent = await self._send_biography_dm(member)
        await self._send_biography_log(
            member,
            config,
            biography=biography,
            term=result.term,
            dm_sent=dm_sent,
        )
        return result

    def _is_exempt(
        self,
        member: discord.Member,
        config: IdentityFilterConfig,
    ) -> bool:
        if member.id in {self.owner_id, member.guild.owner_id}:
            return True
        if member.id in config.exempt_user_ids:
            return True
        role_ids = {role.id for role in member.roles}
        return bool(
            role_ids
            & (
                set(config.exempt_role_ids)
                | self.admin_role_ids
            )
        )

    async def _save(
        self,
        kind: FilterKind,
        config: IdentityFilterConfig,
    ) -> None:
        self._config_cache[(config.guild_id, kind)] = config
        await self.state.update_section(
            _section(kind),
            str(config.guild_id),
            config.to_dict(),
        )

    async def _send_nickname_dm(self, member: discord.Member) -> bool:
        try:
            await member.send(
                "Olá!\n\n"
                "Seu nickname foi alterado automaticamente porque continha um "
                "termo que não é permitido neste servidor.\n\n"
                "Caso tenha sido apenas um engano, escolha um novo nickname de "
                "acordo com as regras da comunidade. Se tiver alguma dúvida, "
                "entre em contato com a equipe.\n\n"
                "Obrigado pela compreensão."
            )
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _send_biography_dm(self, member: discord.Member) -> bool:
        try:
            await member.send(
                "Olá!\n\n"
                "Sua biografia contém uma informação que não é permitida neste "
                "servidor e pode causar confusão com cargos, funções ou "
                "representações oficiais da comunidade.\n\n"
                "Peço que faça a alteração assim que possível. Caso tenha sido "
                "apenas um engano, basta editar sua biografia normalmente.\n\n"
                "Obrigado pela compreensão."
            )
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _send_nickname_log(
        self,
        member: discord.Member,
        config: IdentityFilterConfig,
        *,
        old_name: str,
        new_name: str,
        term: str,
        changed: bool,
        failure: str,
    ) -> None:
        embed = discord.Embed(
            title="Monitoramento de Nickname",
            description=(
                "O nickname foi restaurado para o username original."
                if changed
                else "O nickname foi detectado, mas não pôde ser alterado."
            ),
            color=(
                discord.Color.from_rgb(46, 160, 67)
                if changed
                else discord.Color.from_rgb(218, 54, 51)
            ),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Usuário",
            value=f"{member.mention}\n`{member.id}`",
            inline=True,
        )
        embed.add_field(name="Termo detectado", value=f"`{term}`", inline=True)
        embed.add_field(name="Nickname anterior", value=old_name[:1024], inline=False)
        embed.add_field(name="Novo nickname", value=new_name[:1024], inline=False)
        if failure:
            embed.add_field(name="Falha", value=failure[:1024], inline=False)
        await self._send_log(member.guild, config.log_channel_id, embed)

    async def _send_biography_log(
        self,
        member: discord.Member,
        config: IdentityFilterConfig,
        *,
        biography: str,
        term: str,
        dm_sent: bool,
    ) -> None:
        embed = discord.Embed(
            title="Monitoramento de Biografia",
            description="Expressão proibida identificada em verificação administrativa.",
            color=discord.Color.from_rgb(210, 153, 34),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Usuário",
            value=(
                f"{member.mention}\nUsername: `{member.name}`\n"
                f"ID: `{member.id}`\n"
                f"[Abrir perfil](https://discord.com/users/{member.id})"
            ),
            inline=False,
        )
        embed.add_field(name="Termo detectado", value=f"`{term}`", inline=False)
        embed.add_field(name="Biografia", value=biography[:1024], inline=False)
        embed.add_field(
            name="Mensagem privada",
            value="Enviada" if dm_sent else "Não foi possível enviar",
            inline=False,
        )
        await self._send_log(member.guild, config.log_channel_id, embed)

    async def _send_log(
        self,
        guild: discord.Guild,
        channel_id: int | None,
        embed: discord.Embed,
    ) -> None:
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                return


class NicknameMonitorPanelView(discord.ui.View):
    def __init__(self, service: IdentityMonitor) -> None:
        super().__init__(timeout=None)
        self.service = service

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        allowed = isinstance(member, discord.Member) and (
            self.service.authorize(member)
            if self.service.authorize is not None
            else member.guild_permissions.manage_nicknames
        )
        if not allowed:
            await interaction.response.send_message("Você não possui acesso a este painel.", ephemeral=True)
        return allowed

    @discord.ui.button(
        label="Status",
        style=discord.ButtonStyle.primary,
        custom_id="bella:nickname_monitor:status:v1",
        row=0,
    )
    async def status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.send_message(
            embed=await self.service.nickname_status_embed(interaction.guild),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Ativar / pausar",
        style=discord.ButtonStyle.success,
        custom_id="bella:nickname_monitor:toggle:v1",
        row=0,
    )
    async def toggle(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        current = await self.service.config(interaction.guild.id, "nickname")
        if not current.enabled and not current.log_channel_id:
            await interaction.response.send_message(
                "Escolha primeiro o canal de logs ou use `/logs-sincronizar`.",
                ephemeral=True,
            )
            return
        updated = await self.service.set_enabled(interaction.guild.id, "nickname", not current.enabled)
        await interaction.response.send_message(
            f"Monitor de nicknames {'ativado' if updated.enabled else 'pausado'}.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Canal de logs",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:nickname_monitor:channel:v1",
        row=0,
    )
    async def channel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Escolha o canal dos eventos de identidade:",
            view=NicknameLogChannelView(self.service, interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Verificar membros",
        style=discord.ButtonStyle.danger,
        custom_id="bella:nickname_monitor:scan:v1",
        row=0,
    )
    async def scan(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        checked = 0
        matched = 0
        for member in interaction.guild.members:
            if member.bot:
                continue
            result = await self.service.inspect_nickname(member)
            checked += 1
            matched += int(result.matched)
        await interaction.followup.send(
            f"Verificação concluída: **{checked}** membro(s), **{matched}** ocorrência(s).",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Adicionar bloqueio",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:nickname_monitor:add_term:v1",
        row=1,
    )
    async def add_term(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(NicknameValueModal(self.service, "add_term"))

    @discord.ui.button(
        label="Remover bloqueio",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:nickname_monitor:remove_term:v1",
        row=1,
    )
    async def remove_term(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(NicknameValueModal(self.service, "remove_term"))

    @discord.ui.button(
        label="Permitir nome",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:nickname_monitor:add_allowed:v1",
        row=1,
    )
    async def add_allowed(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(NicknameValueModal(self.service, "add_allowed"))

    @discord.ui.button(
        label="Remover permitido",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:nickname_monitor:remove_allowed:v1",
        row=1,
    )
    async def remove_allowed(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(NicknameValueModal(self.service, "remove_allowed"))

    @discord.ui.button(
        label="Isentar usuário",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:nickname_monitor:exempt_user:v1",
        row=2,
    )
    async def exempt_user(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Escolha quem ficará isento do monitor:",
            view=NicknameUserExemptionView(self.service, interaction.user.id, True),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Remover isenção",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:nickname_monitor:unexempt_user:v1",
        row=2,
    )
    async def unexempt_user(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Escolha quem voltará a ser monitorado:",
            view=NicknameUserExemptionView(self.service, interaction.user.id, False),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Isentar cargo",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:nickname_monitor:exempt_role:v1",
        row=2,
    )
    async def exempt_role(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Escolha o cargo que ficará isento:",
            view=NicknameRoleExemptionView(self.service, interaction.user.id, True),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Remover cargo isento",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:nickname_monitor:unexempt_role:v1",
        row=2,
    )
    async def unexempt_role(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Escolha o cargo que voltará a ser monitorado:",
            view=NicknameRoleExemptionView(self.service, interaction.user.id, False),
            ephemeral=True,
        )


class NicknameValueModal(discord.ui.Modal):
    def __init__(self, service: IdentityMonitor, action: str) -> None:
        titles = {
            "add_term": "Adicionar termo bloqueado",
            "remove_term": "Remover termo bloqueado",
            "add_allowed": "Permitir nickname exato",
            "remove_allowed": "Remover nickname permitido",
        }
        super().__init__(title=titles[action])
        self.service = service
        self.action = action
        self.value_input = discord.ui.TextInput(
            label="Termo ou nickname",
            placeholder="Digite o valor exato",
            min_length=1,
            max_length=80,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        value = str(self.value_input.value).strip()
        if self.action == "add_term":
            await self.service.add_term(interaction.guild.id, "nickname", value)
            message = "Termo adicionado à lista de bloqueio."
        elif self.action == "remove_term":
            _, changed = await self.service.remove_term(interaction.guild.id, "nickname", value)
            message = "Termo removido." if changed else "Esse termo não está na lista."
        elif self.action == "add_allowed":
            await self.service.add_allowed_value(interaction.guild.id, "nickname", value)
            message = "Nickname adicionado à lista de permitidos."
        else:
            _, changed = await self.service.remove_allowed_value(interaction.guild.id, "nickname", value)
            message = "Nickname removido." if changed else "Esse nickname não está na lista."
        await interaction.response.send_message(message, ephemeral=True)


class NicknameLogChannelView(discord.ui.View):
    def __init__(self, service: IdentityMonitor, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.owner_id = owner_id
        select = discord.ui.ChannelSelect(
            placeholder="Canal de logs de identidade",
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
        channel_id = int(getattr(selected, "id", 0) or 0)
        if not channel_id:
            await interaction.response.send_message("Canal inválido.", ephemeral=True)
            return
        current = await self.service.config(interaction.guild.id, "nickname")
        await self.service.configure(
            interaction.guild.id,
            "nickname",
            log_channel_id=channel_id,
            enabled=current.enabled,
        )
        await interaction.response.send_message(f"Logs definidos para <#{channel_id}>.", ephemeral=True)


class NicknameUserExemptionView(discord.ui.View):
    def __init__(self, service: IdentityMonitor, owner_id: int, exempt: bool) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.owner_id = owner_id
        self.exempt = exempt
        select = discord.ui.UserSelect(placeholder="Escolha um usuário", min_values=1, max_values=1)
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
        selected = select.values[0] if isinstance(select, discord.ui.UserSelect) else None
        user_id = int(getattr(selected, "id", 0) or 0)
        await self.service.set_user_exemption(interaction.guild.id, "nickname", user_id, self.exempt)
        await interaction.response.send_message(
            f"<@{user_id}> {'foi isentado' if self.exempt else 'voltou a ser monitorado'}.",
            ephemeral=True,
        )


class NicknameRoleExemptionView(discord.ui.View):
    def __init__(self, service: IdentityMonitor, owner_id: int, exempt: bool) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.owner_id = owner_id
        self.exempt = exempt
        select = discord.ui.RoleSelect(placeholder="Escolha um cargo", min_values=1, max_values=1)
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
        selected = select.values[0] if isinstance(select, discord.ui.RoleSelect) else None
        role_id = int(getattr(selected, "id", 0) or 0)
        await self.service.set_role_exemption(interaction.guild.id, "nickname", role_id, self.exempt)
        await interaction.response.send_message(
            f"<@&{role_id}> {'foi isentado' if self.exempt else 'voltou a ser monitorado'}.",
            ephemeral=True,
        )


def nickname_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Painel de Monitoramento de Nicknames",
        description=(
            "Gerencie o filtro de nomes por um painel fixo: termos bloqueados, lista branca, "
            "isenções, canal de logs e verificação dos membros atuais."
        ),
        color=discord.Color.from_rgb(255, 80, 120),
    )
    embed.add_field(
        name="Funcionamento",
        value=(
            "Novas alterações de nickname são verificadas automaticamente. A verificação manual "
            "também pode corrigir nomes atuais que correspondam às regras."
        ),
        inline=False,
    )
    embed.add_field(
        name="Logs",
        value="Use `/logs-sincronizar` para criar o canal próprio `identidades` quando ele ainda não existir.",
        inline=False,
    )
    return embed


def inspect_identity_value(
    value: str,
    config: IdentityFilterConfig,
) -> IdentityCheckResult:
    normalized = normalize_identity_text(value)
    if not normalized:
        return IdentityCheckResult(False, normalized_value=normalized)
    allowed = {
        normalize_identity_text(item)
        for item in config.allowed_values
        if normalize_identity_text(item)
    }
    if normalized in allowed:
        return IdentityCheckResult(False, normalized_value=normalized)
    candidates = sorted(
        (
            (normalize_identity_text(term), term)
            for term in config.prohibited_terms
            if normalize_identity_text(term)
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for normalized_term, original_term in candidates:
        if normalized_term in normalized:
            return IdentityCheckResult(
                True,
                term=original_term,
                normalized_value=normalized,
            )
    return IdentityCheckResult(False, normalized_value=normalized)


def normalize_identity_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    normalized = normalized.translate(CONFUSABLES)
    normalized = unicodedata.normalize("NFKD", normalized)
    normalized = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", "", normalized)


def _replace_config(
    config: IdentityFilterConfig,
    *,
    prohibited_terms: tuple[str, ...] | None = None,
    allowed_values: tuple[str, ...] | None = None,
    exempt_user_ids: frozenset[int] | None = None,
    exempt_role_ids: frozenset[int] | None = None,
) -> IdentityFilterConfig:
    return IdentityFilterConfig(
        guild_id=config.guild_id,
        enabled=config.enabled,
        log_channel_id=config.log_channel_id,
        prohibited_terms=(
            prohibited_terms
            if prohibited_terms is not None
            else config.prohibited_terms
        ),
        allowed_values=(
            allowed_values if allowed_values is not None else config.allowed_values
        ),
        exempt_user_ids=(
            exempt_user_ids
            if exempt_user_ids is not None
            else config.exempt_user_ids
        ),
        exempt_role_ids=(
            exempt_role_ids
            if exempt_role_ids is not None
            else config.exempt_role_ids
        ),
    )


def _section(kind: FilterKind) -> str:
    return NICKNAME_SECTION if kind == "nickname" else BIOGRAPHY_SECTION


def _deduplicate(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = " ".join(str(value).split()).strip()
        key = normalize_identity_text(clean)
        if not clean or not key or key in seen:
            continue
        seen.add(key)
        result.append(clean)
    return result


def _integer_values(value: Any) -> set[int]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    result: set[int] = set()
    for item in value:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
