from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

import discord

from .central_logs import CentralLogService
from .state import StateStore

FIRST_MESSAGES_SECTION = "server_activity_first_messages"
MAX_COMMAND_DEDUPLICATION_IDS = 5000


class ServerActivityMonitor:
    def __init__(
        self,
        bot: discord.Client,
        state: StateStore,
        logs: CentralLogService,
    ) -> None:
        self.bot = bot
        self.state = state
        self.logs = logs
        self._command_ids: set[int] = set()
        self._command_order: deque[int] = deque()

    async def member_joined(self, member: discord.Member) -> None:
        def mutate(section: dict[str, Any]) -> None:
            guild_members = section.setdefault(str(member.guild.id), {})
            if not isinstance(guild_members, dict):
                guild_members = {}
                section[str(member.guild.id)] = guild_members
            guild_members.pop(str(member.id), None)

        await self.state.mutate_section(FIRST_MESSAGES_SECTION, mutate)
        await self.logs.send_event(
            member.guild,
            "activity",
            title="Membro entrou no servidor",
            description=f"{member.mention} entrou no servidor.",
            severity="INFO",
            fields=(
                ("Usuário", f"{member} (`{member.id}`)", False),
                ("Conta criada", discord.utils.format_dt(member.created_at, style="R"), True),
            ),
        )

    async def member_left(self, member: discord.Member) -> None:
        await self.logs.send_event(
            member.guild,
            "activity",
            title="Membro saiu do servidor",
            description=f"**{member}** saiu ou foi removido do servidor.",
            severity="INFO",
            fields=(
                ("Usuário", f"{member} (`{member.id}`)", False),
                (
                    "Entrada registrada",
                    discord.utils.format_dt(member.joined_at, style="R") if member.joined_at else "Indisponível",
                    True,
                ),
            ),
        )

    async def observe_message(self, message: discord.Message) -> None:
        if message.guild is None or message.webhook_id is not None:
            return
        if message.author.bot:
            interaction = getattr(message, "interaction", None)
            if interaction is not None:
                await self.command_used(
                    message.guild,
                    user=interaction.user,
                    command_name=str(interaction.name or "comando desconhecido"),
                    application_name=str(message.author),
                    channel_id=message.channel.id,
                    interaction_id=int(interaction.id),
                    external=True,
                )
            return

        is_first = [False]

        def mutate(section: dict[str, Any]) -> None:
            guild_members = section.setdefault(str(message.guild.id), {})
            if not isinstance(guild_members, dict):
                guild_members = {}
                section[str(message.guild.id)] = guild_members
            key = str(message.author.id)
            if key in guild_members:
                return
            is_first[0] = True
            guild_members[key] = {
                "message_id": message.id,
                "channel_id": message.channel.id,
                "observed_at": message.created_at.isoformat(),
            }

        await self.state.mutate_section(FIRST_MESSAGES_SECTION, mutate)
        if not is_first[0]:
            return
        await self.logs.send_event(
            message.guild,
            "activity",
            title="Primeira mensagem registrada",
            description=f"{message.author.mention} enviou a primeira mensagem observada pela Bella neste servidor.",
            severity="INFO",
            fields=(
                ("Usuário", f"{message.author} (`{message.author.id}`)", False),
                ("Canal", f"<#{message.channel.id}>", True),
                ("Mensagem", f"[Abrir mensagem]({message.jump_url})", True),
            ),
        )

    async def voice_state_changed(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot or after.channel is None or before.channel == after.channel:
            return
        moved = before.channel is not None
        await self.logs.send_event(
            member.guild,
            "activity",
            title="Membro mudou de canal de voz" if moved else "Membro entrou no canal de voz",
            description=(
                f"{member.mention} mudou de {before.channel.mention} para {after.channel.mention}."
                if moved
                else f"{member.mention} entrou em {after.channel.mention}."
            ),
            severity="INFO",
            fields=(("Usuário", f"{member} (`{member.id}`)", False),),
        )

    async def role_created(self, role: discord.Role) -> None:
        await self.logs.send_event(
            role.guild,
            "activity",
            title="Cargo criado",
            description=f"O cargo **{role.name}** foi criado.",
            severity="INFO",
            fields=(
                ("Cargo", f"{role.mention} (`{role.id}`)", False),
                ("Posição", str(role.position), True),
                ("Permissões", str(role.permissions.value), True),
            ),
        )

    async def role_updated(self, before: discord.Role, after: discord.Role) -> None:
        changes: list[str] = []
        if before.name != after.name:
            changes.append(f"Nome: `{before.name}` → `{after.name}`")
        if before.permissions.value != after.permissions.value:
            changes.append("Permissões do cargo foram alteradas.")
        if before.colour.value != after.colour.value:
            changes.append(f"Cor: `{before.colour}` → `{after.colour}`")
        if before.hoist != after.hoist:
            changes.append(f"Separado na lista: `{before.hoist}` → `{after.hoist}`")
        if before.mentionable != after.mentionable:
            changes.append(f"Mencionável: `{before.mentionable}` → `{after.mentionable}`")
        if not changes:
            return
        await self.logs.send_event(
            after.guild,
            "activity",
            title="Cargo alterado",
            description="\n".join(changes)[:4000],
            severity="MEDIUM" if before.permissions.value != after.permissions.value else "INFO",
            fields=(("Cargo", f"{after.mention} (`{after.id}`)", False),),
        )

    async def role_deleted(self, role: discord.Role) -> None:
        await self.logs.send_event(
            role.guild,
            "activity",
            title="Cargo excluído",
            description=f"O cargo **{role.name}** foi excluído.",
            severity="MEDIUM",
            fields=(("ID", str(role.id), True), ("Membros afetados", str(len(role.members)), True)),
        )

    async def channel_created(self, channel: discord.abc.GuildChannel) -> None:
        await self.logs.send_event(
            channel.guild,
            "activity",
            title="Canal criado",
            description=f"O canal **{channel.name}** foi criado.",
            severity="INFO",
            fields=(
                ("Canal", f"{getattr(channel, 'mention', channel.name)} (`{channel.id}`)", False),
                ("Tipo", str(channel.type), True),
            ),
        )

    async def channel_updated(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        changes: list[str] = []
        for attribute, label in (
            ("name", "Nome"),
            ("topic", "Tópico"),
            ("nsfw", "NSFW"),
            ("slowmode_delay", "Modo lento"),
            ("bitrate", "Bitrate"),
            ("user_limit", "Limite de usuários"),
        ):
            old_value = getattr(before, attribute, None)
            new_value = getattr(after, attribute, None)
            if old_value != new_value:
                changes.append(f"{label}: `{old_value}` → `{new_value}`")
        before_category = getattr(getattr(before, "category", None), "id", None)
        after_category = getattr(getattr(after, "category", None), "id", None)
        if before_category != after_category:
            changes.append(
                f"Categoria: `{before_category or 'nenhuma'}` → `{after_category or 'nenhuma'}`"
            )
        if _overwrite_signature(before) != _overwrite_signature(after):
            changes.append("Permissões específicas do canal foram alteradas.")
        if not changes:
            return
        await self.logs.send_event(
            after.guild,
            "activity",
            title="Canal alterado",
            description="\n".join(changes)[:4000],
            severity="MEDIUM" if "Permissões específicas do canal foram alteradas." in changes else "INFO",
            fields=(("Canal", f"{getattr(after, 'mention', after.name)} (`{after.id}`)", False),),
        )

    async def channel_deleted(self, channel: discord.abc.GuildChannel) -> None:
        await self.logs.send_event(
            channel.guild,
            "activity",
            title="Canal excluído",
            description=f"O canal **{channel.name}** foi excluído.",
            severity="MEDIUM",
            fields=(("ID", str(channel.id), True), ("Tipo", str(channel.type), True)),
        )

    async def message_deleted(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        guild = self._guild(payload.guild_id)
        if guild is None:
            return
        cached = payload.cached_message
        author = getattr(cached, "author", None)
        embed_count = len(getattr(cached, "embeds", ()) or ())
        fields: list[tuple[str, str, bool]] = [
            ("Canal", f"<#{payload.channel_id}>", True),
            ("Mensagem", str(payload.message_id), True),
            ("Embeds", str(embed_count) if cached is not None else "Cache indisponível", True),
        ]
        if author is not None:
            fields.append(("Autor", f"{author} (`{author.id}`)", False))
        await self.logs.send_event(
            guild,
            "activity",
            title="Mensagem com embed excluída" if embed_count else "Mensagem excluída",
            description=(
                "A Bella detectou a exclusão em tempo real. O Discord não informa com segurança "
                "quem executou a exclusão neste evento."
            ),
            severity="MEDIUM" if embed_count else "INFO",
            fields=fields,
        )

    async def messages_bulk_deleted(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        guild = self._guild(payload.guild_id)
        if guild is None:
            return
        cached_embeds = sum(len(message.embeds) for message in payload.cached_messages)
        await self.logs.send_event(
            guild,
            "activity",
            title="Mensagens excluídas em massa",
            description=f"Foram removidas **{len(payload.message_ids)}** mensagens de um canal.",
            severity="MEDIUM",
            fields=(
                ("Canal", f"<#{payload.channel_id}>", True),
                ("Embeds visíveis no cache", str(cached_embeds), True),
            ),
        )

    async def message_edited(self, before: discord.Message, after: discord.Message) -> None:
        if after.guild is None or before.author.id != after.author.id:
            return
        removed_embeds = max(0, len(before.embeds) - len(after.embeds))
        if not removed_embeds:
            return
        await self.logs.send_event(
            after.guild,
            "activity",
            title="Embed removida de uma mensagem",
            description=f"Uma edição removeu **{removed_embeds}** embed(s) de uma mensagem.",
            severity="MEDIUM",
            fields=(
                ("Canal", f"<#{after.channel.id}>", True),
                ("Mensagem", f"[Abrir mensagem]({after.jump_url})", True),
                ("Autor", f"{after.author} (`{after.author.id}`)", False),
            ),
        )

    async def command_used(
        self,
        guild: discord.Guild,
        *,
        user: discord.User | discord.Member,
        command_name: str,
        application_name: str,
        channel_id: int,
        interaction_id: int,
        external: bool,
    ) -> None:
        if not self._remember_command(interaction_id):
            return
        await self.logs.send_event(
            guild,
            "activity",
            title="Comando de outro bot utilizado" if external else "Comando da Bella utilizado",
            description=f"{user.mention} utilizou `/{command_name}`.",
            severity="INFO",
            fields=(
                ("Aplicação", application_name[:1024], True),
                ("Canal", f"<#{channel_id}>" if channel_id else "Indisponível", True),
                ("Usuário", f"{user} (`{user.id}`)", False),
            ),
        )

    def _remember_command(self, interaction_id: int) -> bool:
        if interaction_id in self._command_ids:
            return False
        self._command_ids.add(interaction_id)
        self._command_order.append(interaction_id)
        while len(self._command_order) > MAX_COMMAND_DEDUPLICATION_IDS:
            self._command_ids.discard(self._command_order.popleft())
        return True

    def _guild(self, guild_id: int) -> discord.Guild | None:
        return self.bot.get_guild(guild_id)


def activity_api_limitations() -> str:
    return (
        "Comandos da Bella são registrados diretamente. Comandos de outros bots só aparecem "
        "quando o Discord entrega à Bella uma mensagem pública de resposta contendo os metadados "
        "da interação; comandos efêmeros ou sem resposta pública não são expostos pela API."
    )


def _overwrite_signature(channel: discord.abc.GuildChannel) -> tuple[tuple[int, int, int], ...]:
    values: list[tuple[int, int, int]] = []
    for target, overwrite in getattr(channel, "overwrites", {}).items():
        allow, deny = overwrite.pair()
        values.append((int(target.id), allow.value, deny.value))
    return tuple(sorted(values))
