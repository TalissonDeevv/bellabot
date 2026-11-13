from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import discord

from .config import Settings
from .state import StateStore

ADMIN_COMMAND_ROOTS = {
    "atualizar-conhecimento",
    "backup-servidor",
    "conectar-voz",
    "configurar-recepcao",
    "criar-logs",
    "definir-canal",
    "definir-canal-alteracoes",
    "definir-canal-conhecimento",
    "definir-canal-feedbacks",
    "definir-canal-relatorios",
    "definir-canal-semanal",
    "definir-canal-tecnico",
    "desativar-recepcao",
    "desconectar-voz",
    "embed-criar",
    "embed-editar",
    "feedbacks",
    "logs",
    "logs-sincronizar",
    "presence",
    "relatorio",
    "relatorio-diario",
    "relatorio-mensal",
    "relatorio-semanal",
    "resumir-servidor",
    "scan-seguranca",
    "seguranca",
    "sincronizar-feedbacks",
    "trafego",
    "verificar-alteracoes",
    "voice-status",
}

SUCCESS_COLOR = discord.Color.from_rgb(46, 160, 67)
WARNING_COLOR = discord.Color.from_rgb(210, 153, 34)
ERROR_COLOR = discord.Color.from_rgb(218, 54, 51)
BRAND_COLOR = discord.Color.from_rgb(24, 119, 242)


@dataclass(frozen=True)
class AuditContext:
    started_at: float
    command_name: str


class AdminAudit:
    def __init__(self, settings: Settings, state: StateStore) -> None:
        self.settings = settings
        self.state = state
        self.pending: dict[int, AuditContext] = {}
        self.denied_attempts: dict[tuple[int, int], deque[float]] = {}

    def is_admin_command(self, interaction: discord.Interaction) -> bool:
        command = interaction.command
        if command is None:
            return False
        root = getattr(command, "root_parent", None)
        name = root.name if root else command.name
        return name in ADMIN_COMMAND_ROOTS

    def is_authorized(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        return self.member_is_authorized(interaction.user)

    def member_is_authorized(self, member: discord.Member) -> bool:
        if member.id == int(getattr(self.settings, "talisson_user_id", 0) or 0):
            return True
        role_ids = {role.id for role in member.roles}
        return bool(role_ids & self.settings.admin_role_ids)

    async def begin(self, interaction: discord.Interaction) -> bool:
        if not self.is_admin_command(interaction):
            return True
        command_name = _command_name(interaction)
        self.pending[interaction.id] = AuditContext(
            started_at=time.perf_counter(),
            command_name=command_name,
        )
        if self.is_authorized(interaction):
            return True

        self.pending.pop(interaction.id, None)
        await self.record(
            interaction,
            result="Acesso negado",
            reason="Usuário não possui um dos cargos administrativos autorizados.",
            duration_ms=0,
            severity="denied",
        )
        suspicious = self._record_denied_attempt(interaction)
        if suspicious:
            await self.record(
                interaction,
                result="Comportamento suspeito",
                reason=(
                    f"{suspicious} tentativas negadas em menos de 5 minutos."
                ),
                duration_ms=0,
                severity="suspicious",
            )
        await _send_ephemeral(
            interaction,
            "🔒 Você não possui permissão para utilizar este comando.\n\n"
            "Caso precise de acesso, entre em contato com um administrador do servidor.",
        )
        return False

    async def complete(self, interaction: discord.Interaction) -> None:
        context = self.pending.pop(interaction.id, None)
        if not context:
            return
        await self.record(
            interaction,
            result="Sucesso",
            reason="Comando administrativo concluído.",
            duration_ms=int((time.perf_counter() - context.started_at) * 1000),
            severity="success",
            command_name=context.command_name,
        )

    async def fail(
        self,
        interaction: discord.Interaction,
        error: BaseException,
    ) -> None:
        context = self.pending.pop(interaction.id, None)
        if not context or not self.is_admin_command(interaction):
            return
        await self.record(
            interaction,
            result="Falha",
            reason=type(error).__name__,
            duration_ms=int((time.perf_counter() - context.started_at) * 1000),
            severity="failure",
            command_name=context.command_name,
        )

    async def configure_channels(
        self,
        guild_id: int,
        *,
        admin_channel_id: int,
        security_channel_id: int,
        audit_channel_id: int,
    ) -> None:
        await self.state.update_section(
            "guild_audit_config",
            str(guild_id),
            {
                "admin_log_channel_id": admin_channel_id,
                "security_log_channel_id": security_channel_id,
                "audit_log_channel_id": audit_channel_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def channel_id_for(self, guild_id: int, severity: str) -> int | None:
        configs = await self.state.get_section("guild_audit_config")
        config = configs.get(str(guild_id), {})
        if not isinstance(config, dict):
            config = {}
        if severity in {"denied", "suspicious"}:
            value = (
                config.get("security_log_channel_id")
                or self.settings.security_log_channel_id
            )
        elif severity == "success":
            value = (
                config.get("admin_log_channel_id")
                or self.settings.admin_log_channel_id
            )
        else:
            value = (
                config.get("audit_log_channel_id")
                or self.settings.audit_log_channel_id
            )
        return int(value) if value else None

    async def record(
        self,
        interaction: discord.Interaction,
        *,
        result: str,
        reason: str,
        duration_ms: int,
        severity: str,
        command_name: str | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        embed = _audit_embed(
            interaction,
            command_name or _command_name(interaction),
            result,
            reason,
            duration_ms,
            severity,
        )
        channel_id = await self.channel_id_for(guild.id, severity)
        if channel_id:
            try:
                channel = guild.get_channel(channel_id)
                if channel is None:
                    channel = await interaction.client.fetch_channel(channel_id)
                if isinstance(channel, discord.abc.Messageable):
                    await channel.send(embed=embed)
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass
        await self._persist(interaction, result, reason, duration_ms, severity)

    async def record_executive_action(
        self,
        message: discord.Message,
        *,
        action: str,
        target: discord.abc.User | None,
        roles: tuple[discord.Role, ...],
        result: str,
        reason: str,
        duration_ms: int,
        notes: str = "",
    ) -> None:
        guild = message.guild
        if guild is None:
            return
        embed = _executive_audit_embed(
            message,
            action=action,
            target=target,
            roles=roles,
            result=result,
            reason=reason,
            duration_ms=duration_ms,
            notes=notes,
        )
        channel_id = await self.channel_id_for(guild.id, "failure")
        if channel_id:
            try:
                channel = guild.get_channel(channel_id)
                if channel is None:
                    channel = await message.client.fetch_channel(channel_id)
                if isinstance(channel, discord.abc.Messageable):
                    await channel.send(embed=embed)
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass
        now = datetime.now(timezone.utc)
        key = f"{int(now.timestamp() * 1000)}:{message.id}"

        def mutate(history: dict[str, Any]) -> None:
            history[key] = {
                "timestamp": now.isoformat(),
                "guild_id": guild.id,
                "channel_id": message.channel.id,
                "executor_id": message.author.id,
                "target_id": target.id if target else None,
                "role_ids": [role.id for role in roles],
                "action": action,
                "result": result,
                "reason": reason,
                "duration_ms": duration_ms,
                "notes": notes,
            }
            if len(history) > 5000:
                for old_key in sorted(history)[: len(history) - 5000]:
                    del history[old_key]

        await self.state.mutate_section("executive_audit_history", mutate)

    async def _persist(
        self,
        interaction: discord.Interaction,
        result: str,
        reason: str,
        duration_ms: int,
        severity: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        key = f"{int(now.timestamp() * 1000)}:{interaction.id}"

        def mutate(history: dict[str, Any]) -> None:
            history[key] = {
                "timestamp": now.isoformat(),
                "guild_id": interaction.guild_id,
                "channel_id": interaction.channel_id,
                "user_id": interaction.user.id,
                "command": _command_name(interaction),
                "result": result,
                "reason": reason,
                "duration_ms": duration_ms,
                "severity": severity,
            }
            if len(history) > 5000:
                for old_key in sorted(history)[: len(history) - 5000]:
                    del history[old_key]

        await self.state.mutate_section("admin_audit_history", mutate)

    def _record_denied_attempt(self, interaction: discord.Interaction) -> int:
        if interaction.guild_id is None:
            return 0
        key = (interaction.guild_id, interaction.user.id)
        now = time.monotonic()
        attempts = self.denied_attempts.setdefault(key, deque())
        attempts.append(now)
        cutoff = now - 300
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        return len(attempts) if len(attempts) > 5 else 0


def _audit_embed(
    interaction: discord.Interaction,
    command_name: str,
    result: str,
    reason: str,
    duration_ms: int,
    severity: str,
) -> discord.Embed:
    color = {
        "success": SUCCESS_COLOR,
        "denied": ERROR_COLOR,
        "suspicious": WARNING_COLOR,
        "failure": ERROR_COLOR,
    }.get(severity, BRAND_COLOR)
    title = {
        "success": "Comando administrativo executado",
        "denied": "Tentativa de acesso negado",
        "suspicious": "Comportamento suspeito detectado",
        "failure": "Falha em comando administrativo",
    }.get(severity, "Auditoria administrativa")
    member = interaction.user
    embed = discord.Embed(
        title=title,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Usuário",
        value=(
            f"Nome: **{discord.utils.escape_markdown(member.name)}**\n"
            f"Exibição: **{discord.utils.escape_markdown(member.display_name)}**\n"
            f"ID: `{member.id}`"
        ),
        inline=True,
    )
    roles = []
    if isinstance(member, discord.Member):
        roles = [
            f"{role.name} (`{role.id}`)"
            for role in reversed(member.roles)
            if not role.is_default()
        ]
    embed.add_field(
        name="Cargos",
        value="\n".join(roles[:12]) or "Nenhum cargo adicional",
        inline=True,
    )
    guild = interaction.guild
    channel = interaction.channel
    embed.add_field(
        name="Localização",
        value=(
            f"Servidor: **{discord.utils.escape_markdown(guild.name if guild else 'n/a')}**\n"
            f"Canal: **{getattr(channel, 'name', 'n/a')}**\n"
            f"Canal ID: `{interaction.channel_id or 'n/a'}`"
        ),
        inline=False,
    )
    embed.add_field(name="Comando", value=f"`/{command_name}`", inline=True)
    embed.add_field(name="Resultado", value=result, inline=True)
    embed.add_field(name="Tempo", value=f"{duration_ms} ms", inline=True)
    embed.add_field(name="Contexto", value=reason, inline=False)
    created = int(member.created_at.timestamp())
    joined = (
        int(member.joined_at.timestamp())
        if isinstance(member, discord.Member) and member.joined_at
        else None
    )
    embed.add_field(
        name="Conta",
        value=(
            f"Criada: <t:{created}:F>\n"
            + (f"Entrou: <t:{joined}:F>" if joined else "Entrada: n/a")
        ),
        inline=False,
    )
    embed.set_thumbnail(url=str(member.display_avatar.url))
    embed.set_footer(
        text=f"Bella Audit | Unix {int(datetime.now(timezone.utc).timestamp())}"
    )
    return embed


def _executive_audit_embed(
    message: discord.Message,
    *,
    action: str,
    target: discord.abc.User | None,
    roles: tuple[discord.Role, ...],
    result: str,
    reason: str,
    duration_ms: int,
    notes: str,
) -> discord.Embed:
    now = datetime.now(timezone.utc)
    success = result.casefold() == "sucesso"
    executor = message.author
    embed = discord.Embed(
        title=(
            "Ação executiva concluída"
            if success
            else "Falha em ação executiva"
        ),
        color=SUCCESS_COLOR if success else ERROR_COLOR,
        timestamp=now,
    )
    embed.set_author(
        name=f"Executor: {executor} | {executor.id}",
        icon_url=str(executor.display_avatar.url),
    )
    embed.add_field(
        name="Executor",
        value=(
            f"Nome: **{discord.utils.escape_markdown(executor.name)}**\n"
            f"Exibição: **{discord.utils.escape_markdown(executor.display_name)}**\n"
            f"ID: `{executor.id}`"
        ),
        inline=True,
    )
    if target:
        embed.add_field(
            name="Usuário alvo",
            value=(
                f"Nome: **{discord.utils.escape_markdown(target.name)}**\n"
                f"Exibição: **{discord.utils.escape_markdown(target.display_name)}**\n"
                f"ID: `{target.id}`"
            ),
            inline=True,
        )
        embed.set_thumbnail(url=str(target.display_avatar.url))
    else:
        embed.add_field(name="Usuário alvo", value="Não aplicável", inline=True)
        embed.set_thumbnail(url=str(executor.display_avatar.url))
    embed.add_field(
        name="Cargo",
        value=(
            _clip_audit(
                "\n".join(
                    f"{discord.utils.escape_markdown(role.name)} (`{role.id}`)"
                    for role in roles
                )
            )
            or "Não aplicável"
        ),
        inline=False,
    )
    embed.add_field(name="Ação executada", value=action, inline=True)
    embed.add_field(name="Resultado", value=result, inline=True)
    embed.add_field(name="Tempo de execução", value=f"{duration_ms} ms", inline=True)
    embed.add_field(
        name="Localização",
        value=(
            f"Servidor: **{discord.utils.escape_markdown(message.guild.name)}**\n"
            f"Canal: **{getattr(message.channel, 'name', 'n/a')}**\n"
            f"Canal ID: `{message.channel.id}`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Horário",
        value=f"<t:{int(now.timestamp())}:F>\nUnix: `{int(now.timestamp())}`",
        inline=True,
    )
    embed.add_field(
        name="Observações",
        value=_clip_audit(notes or reason),
        inline=False,
    )
    if not success:
        embed.add_field(
            name="Motivo da falha",
            value=_clip_audit(reason),
            inline=False,
        )
    embed.set_footer(text="Bella • Auditoria Executiva")
    return embed


def _clip_audit(value: str, limit: int = 1024) -> str:
    text = value.strip() or "Não informado"
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _command_name(interaction: discord.Interaction) -> str:
    command = interaction.command
    if command is None:
        return "desconhecido"
    return str(getattr(command, "qualified_name", command.name))


async def _send_ephemeral(
    interaction: discord.Interaction,
    message: str,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
