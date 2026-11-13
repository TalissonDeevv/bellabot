from __future__ import annotations

import logging
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable

import discord

from .central_logs import CentralLogService
from .state import StateStore

LOGGER = logging.getLogger(__name__)

CONFIG_SECTION = "satisfaction_config"
SURVEYS_SECTION = "satisfaction_surveys"
RESPONSES_SECTION = "satisfaction_responses"
HISTORY_SECTION = "satisfaction_history"
MAX_SURVEYS_PER_GUILD = 200

AuthorizationCallback = Callable[[discord.Member], bool]

STOPWORDS = {
    "a", "ao", "aos", "as", "com", "como", "da", "das", "de", "do", "dos", "e", "ela", "ele",
    "em", "era", "essa", "esse", "esta", "este", "eu", "foi", "mais", "mas", "me", "mesmo", "muito",
    "na", "nas", "no", "nos", "não", "o", "os", "ou", "para", "por", "porque", "pra", "que", "se",
    "sem", "só", "também", "tem", "teve", "um", "uma", "vc", "vcs",
}


class SatisfactionService:
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
        self._registered_views: set[str] = set()

    async def config(self, guild_id: int) -> dict[str, Any]:
        section = await self.state.get_section(CONFIG_SECTION)
        value = section.get(str(guild_id), {})
        payload = dict(value) if isinstance(value, dict) else {}
        payload.setdefault("channel_id", 0)
        payload.setdefault("auto_ticket", False)
        return payload

    async def set_channel(self, guild_id: int, channel_id: int) -> None:
        payload = await self.config(guild_id)
        payload.update({"channel_id": channel_id, "updated_at": _now()})
        await self.state.update_section(CONFIG_SECTION, str(guild_id), payload)

    async def toggle_auto_ticket(self, guild_id: int) -> bool:
        payload = await self.config(guild_id)
        enabled = not bool(payload.get("auto_ticket", False))
        payload.update({"auto_ticket": enabled, "updated_at": _now()})
        await self.state.update_section(CONFIG_SECTION, str(guild_id), payload)
        return enabled

    async def create_public_survey(
        self,
        guild: discord.Guild,
        *,
        title: str,
        question: str,
        source: str,
        created_by: int,
    ) -> dict[str, Any]:
        config = await self.config(guild.id)
        channel = guild.get_channel(_int(config.get("channel_id")))
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Configure primeiro o canal público de pesquisas pelo painel.")
        survey = self._new_survey(
            guild.id,
            title=title,
            question=question,
            source=source,
            created_by=created_by,
            visibility="PUBLIC",
        )
        message = await channel.send(
            embed=survey_embed(survey),
            view=SatisfactionRatingView(self, str(survey["id"])),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        survey.update({"channel_id": channel.id, "message_id": message.id, "status": "ACTIVE"})
        await self._save_survey(guild.id, survey)
        self._register_view(str(survey["id"]), message.id)
        await self.logs.send_event(
            guild,
            "satisfaction",
            title="Pesquisa de satisfação criada",
            description=f"A pesquisa **{survey['title']}** foi publicada.",
            severity="INFO",
            fields=(("Origem", str(survey["source"]), True), ("ID", str(survey["id"]), True)),
        )
        return survey

    async def send_ticket_survey(
        self,
        *,
        guild: discord.Guild,
        user_id: int,
        ticket_id: int,
    ) -> bool:
        config = await self.config(guild.id)
        if not bool(config.get("auto_ticket", False)):
            return False
        user = guild.get_member(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except (discord.NotFound, discord.HTTPException):
                return False
        survey = self._new_survey(
            guild.id,
            title="Como foi seu atendimento?",
            question="De 1 a 5, como você avalia a experiência no ticket? Se quiser, conte o que poderia melhorar.",
            source=f"ticket:{ticket_id}",
            created_by=self.bot.user.id if self.bot.user else 0,
            visibility="PRIVATE",
            target_user_id=user_id,
        )
        try:
            message = await user.send(
                embed=survey_embed(survey),
                view=SatisfactionRatingView(self, str(survey["id"])),
            )
        except (discord.Forbidden, discord.HTTPException):
            return False
        survey.update({"channel_id": message.channel.id, "message_id": message.id, "status": "ACTIVE"})
        await self._save_survey(guild.id, survey)
        self._register_view(str(survey["id"]), message.id)
        await self.logs.send_event(
            guild,
            "satisfaction",
            title="Pesquisa pós-ticket enviada",
            description="Uma pesquisa privada foi enviada após o encerramento de um atendimento.",
            severity="INFO",
            fields=(("Ticket", str(ticket_id), True), ("Pesquisa", str(survey["id"]), True)),
        )
        return True

    async def record_response(
        self,
        interaction: discord.Interaction,
        survey_id: str,
        rating: int,
        comment: str,
    ) -> None:
        survey = await self.find_survey(survey_id)
        if survey is None or survey.get("status") != "ACTIVE":
            raise ValueError("Essa pesquisa não está mais recebendo respostas.")
        target_user_id = _int(survey.get("target_user_id"))
        if target_user_id and interaction.user.id != target_user_id:
            raise PermissionError("Essa pesquisa privada pertence a outra pessoa.")
        response = {
            "rating": max(1, min(5, int(rating))),
            "comment": str(comment or "").strip()[:1500],
            "responded_at": _now(),
        }

        def mutate(section: dict[str, Any]) -> None:
            responses = section.setdefault(survey_id, {})
            if not isinstance(responses, dict):
                responses = {}
                section[survey_id] = responses
            responses[str(interaction.user.id)] = response

        await self.state.mutate_section(RESPONSES_SECTION, mutate)
        guild = self.bot.get_guild(_int(survey.get("guild_id")))
        if guild is not None:
            await self.logs.send_event(
                guild,
                "satisfaction",
                title="Nova avaliação recebida",
                description="Uma resposta foi registrada. O comentário privado não foi incluído neste log.",
                severity="INFO",
                fields=(
                    ("Pesquisa", survey_id, True),
                    ("Nota", f"{rating}/5", True),
                    ("Comentário", "sim" if response["comment"] else "não", True),
                ),
            )

    async def find_survey(self, survey_id: str) -> dict[str, Any] | None:
        section = await self.state.get_section(SURVEYS_SECTION)
        for guild_data in section.values():
            if not isinstance(guild_data, dict):
                continue
            payload = guild_data.get(survey_id)
            if isinstance(payload, dict):
                return dict(payload)
        return None

    async def surveys(self, guild_id: int) -> list[dict[str, Any]]:
        section = await self.state.get_section(SURVEYS_SECTION)
        guild_data = section.get(str(guild_id), {})
        if not isinstance(guild_data, dict):
            return []
        return sorted(
            [dict(item) for item in guild_data.values() if isinstance(item, dict)],
            key=lambda item: str(item.get("created_at", "")),
            reverse=True,
        )

    async def responses_for(self, survey_id: str) -> list[dict[str, Any]]:
        section = await self.state.get_section(RESPONSES_SECTION)
        values = section.get(survey_id, {})
        return [dict(item) for item in values.values() if isinstance(item, dict)] if isinstance(values, dict) else []

    async def metrics(self, guild_id: int) -> dict[str, Any]:
        surveys = await self.surveys(guild_id)
        responses: list[dict[str, Any]] = []
        for survey in surveys:
            responses.extend(await self.responses_for(str(survey.get("id"))))
        ratings = [_int(item.get("rating")) for item in responses if 1 <= _int(item.get("rating")) <= 5]
        distribution = {rating: ratings.count(rating) for rating in range(1, 6)}
        return {
            "surveys": len(surveys),
            "responses": len(ratings),
            "average": sum(ratings) / len(ratings) if ratings else 0.0,
            "satisfaction_rate": (
                100 * sum(1 for rating in ratings if rating >= 4) / len(ratings)
                if ratings
                else 0.0
            ),
            "distribution": distribution,
        }

    async def recurring_criticisms(self, guild_id: int, *, limit: int = 8) -> list[tuple[str, int]]:
        surveys = await self.surveys(guild_id)
        words: Counter[str] = Counter()
        bigrams: Counter[str] = Counter()
        for survey in surveys:
            for response in await self.responses_for(str(survey.get("id"))):
                if _int(response.get("rating")) > 3:
                    continue
                tokens = [
                    token
                    for token in re.findall(r"[a-záàâãéêíóôõúç]{3,}", str(response.get("comment", "")).casefold())
                    if token not in STOPWORDS
                ]
                words.update(tokens)
                bigrams.update(" ".join(pair) for pair in zip(tokens, tokens[1:]))
        combined = Counter({key: count for key, count in words.items() if count >= 2})
        combined.update({key: count for key, count in bigrams.items() if count >= 2})
        return combined.most_common(limit)

    async def metrics_embed(self, guild: discord.Guild) -> discord.Embed:
        metrics = await self.metrics(guild.id)
        distribution = metrics["distribution"]
        embed = discord.Embed(
            title="Métricas de satisfação",
            description="Os resultados são agregados. Comentários e identidades não aparecem neste painel.",
            color=discord.Color.from_rgb(255, 80, 120),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Pesquisas", value=str(metrics["surveys"]), inline=True)
        embed.add_field(name="Respostas", value=str(metrics["responses"]), inline=True)
        embed.add_field(name="Média", value=f"{metrics['average']:.2f}/5", inline=True)
        embed.add_field(name="Satisfação", value=f"{metrics['satisfaction_rate']:.1f}% com nota 4 ou 5", inline=True)
        embed.add_field(
            name="Distribuição",
            value=" • ".join(f"{rating}★: {distribution[rating]}" for rating in range(1, 6)),
            inline=False,
        )
        return embed

    async def criticisms_embed(self, guild: discord.Guild) -> discord.Embed:
        topics = await self.recurring_criticisms(guild.id)
        lines = [f"**{topic}** — {count} ocorrência(s)" for topic, count in topics]
        return discord.Embed(
            title="Críticas recorrentes",
            description=(
                "\n".join(lines)
                if lines
                else "Ainda não existem padrões recorrentes suficientes para exibir."
            ),
            color=discord.Color.from_rgb(210, 153, 34),
            timestamp=datetime.now(timezone.utc),
        ).set_footer(text="Resumo local por frequência; nenhuma resposta privada é reproduzida.")

    async def active_embed(self, guild: discord.Guild) -> discord.Embed:
        surveys = [item for item in await self.surveys(guild.id) if item.get("status") == "ACTIVE"]
        lines = []
        for survey in surveys[:15]:
            count = len(await self.responses_for(str(survey.get("id"))))
            lines.append(f"**{survey.get('title')}** — {count} resposta(s) • `{survey.get('source')}`")
        return discord.Embed(
            title="Pesquisas ativas",
            description="\n".join(lines)[:4000] or "Nenhuma pesquisa ativa.",
            color=discord.Color.from_rgb(110, 118, 129),
            timestamp=datetime.now(timezone.utc),
        )

    async def register_active_views(self) -> None:
        for guild in list(self.bot.guilds):
            for survey in await self.surveys(guild.id):
                message_id = _int(survey.get("message_id"))
                if survey.get("status") == "ACTIVE" and message_id:
                    self._register_view(str(survey["id"]), message_id)

    def panel_view(self) -> "SatisfactionAdminView":
        return SatisfactionAdminView(self)

    def _new_survey(
        self,
        guild_id: int,
        *,
        title: str,
        question: str,
        source: str,
        created_by: int,
        visibility: str,
        target_user_id: int = 0,
    ) -> dict[str, Any]:
        return {
            "id": uuid.uuid4().hex[:12],
            "guild_id": guild_id,
            "title": str(title).strip()[:256] or "Pesquisa de satisfação",
            "question": str(question).strip()[:2000] or "Como foi sua experiência?",
            "source": str(source).strip()[:120] or "geral",
            "created_by": created_by,
            "created_at": _now(),
            "visibility": visibility,
            "target_user_id": target_user_id,
            "status": "PENDING",
            "channel_id": 0,
            "message_id": 0,
        }

    async def _save_survey(self, guild_id: int, survey: dict[str, Any]) -> None:
        def mutate(section: dict[str, Any]) -> None:
            guild_data = section.setdefault(str(guild_id), {})
            if not isinstance(guild_data, dict):
                guild_data = {}
                section[str(guild_id)] = guild_data
            guild_data[str(survey["id"])] = survey
            if len(guild_data) > MAX_SURVEYS_PER_GUILD:
                ordered = sorted(
                    guild_data,
                    key=lambda key: str(guild_data[key].get("created_at", ""))
                    if isinstance(guild_data[key], dict)
                    else "",
                )
                for key in ordered[: len(guild_data) - MAX_SURVEYS_PER_GUILD]:
                    guild_data.pop(key, None)

        await self.state.mutate_section(SURVEYS_SECTION, mutate)

    def _register_view(self, survey_id: str, message_id: int) -> None:
        if survey_id in self._registered_views or not hasattr(self.bot, "add_view"):
            return
        self.bot.add_view(SatisfactionRatingView(self, survey_id), message_id=message_id)
        self._registered_views.add(survey_id)


class SatisfactionRatingView(discord.ui.View):
    def __init__(self, service: SatisfactionService, survey_id: str) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.survey_id = survey_id
        for rating in range(1, 6):
            button = discord.ui.Button(
                label=f"{rating} ★",
                style=(
                    discord.ButtonStyle.danger
                    if rating <= 2
                    else discord.ButtonStyle.secondary
                    if rating == 3
                    else discord.ButtonStyle.success
                ),
                custom_id=f"bella:satisfaction:{survey_id}:{rating}",
            )
            button.callback = self._callback(rating)
            self.add_item(button)

    def _callback(self, rating: int):
        async def callback(interaction: discord.Interaction) -> None:
            survey = await self.service.find_survey(self.survey_id)
            if survey is None or survey.get("status") != "ACTIVE":
                await interaction.response.send_message("Essa pesquisa não está mais ativa.", ephemeral=True)
                return
            target = _int(survey.get("target_user_id"))
            if target and target != interaction.user.id:
                await interaction.response.send_message("Essa pesquisa privada não pertence a você.", ephemeral=True)
                return
            await interaction.response.send_modal(
                SatisfactionCommentModal(self.service, self.survey_id, rating)
            )

        return callback


class SatisfactionCommentModal(discord.ui.Modal, title="Conte como foi"):
    comment = discord.ui.TextInput(
        label="Comentário opcional",
        placeholder="O que funcionou bem ou poderia melhorar?",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1500,
    )

    def __init__(self, service: SatisfactionService, survey_id: str, rating: int) -> None:
        super().__init__()
        self.service = service
        self.survey_id = survey_id
        self.rating = rating

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            await self.service.record_response(
                interaction,
                self.survey_id,
                self.rating,
                str(self.comment.value or ""),
            )
        except (ValueError, PermissionError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            "Obrigada pela avaliação. Ela foi salva de forma privada e só entra nas métricas agregadas.",
            ephemeral=True,
        )


class SatisfactionAdminView(discord.ui.View):
    def __init__(self, service: SatisfactionService) -> None:
        super().__init__(timeout=None)
        self.service = service

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = isinstance(interaction.user, discord.Member) and self.service.authorize(interaction.user)
        if not allowed:
            await interaction.response.send_message("Você não possui acesso a este painel.", ephemeral=True)
        return allowed

    @discord.ui.button(label="Canal", style=discord.ButtonStyle.secondary, custom_id="bella:satisfaction:channel:v1")
    async def channel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Escolha o canal das pesquisas públicas:",
            view=SatisfactionChannelView(self.service, interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(label="Nova pesquisa", style=discord.ButtonStyle.success, custom_id="bella:satisfaction:create:v1")
    async def create(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(SatisfactionCreateModal(self.service))

    @discord.ui.button(label="Métricas", style=discord.ButtonStyle.primary, custom_id="bella:satisfaction:metrics:v1")
    async def metrics(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.send_message(embed=await self.service.metrics_embed(interaction.guild), ephemeral=True)

    @discord.ui.button(label="Críticas recorrentes", style=discord.ButtonStyle.secondary, custom_id="bella:satisfaction:criticisms:v1")
    async def criticisms(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.send_message(embed=await self.service.criticisms_embed(interaction.guild), ephemeral=True)

    @discord.ui.button(label="Ativas", style=discord.ButtonStyle.secondary, custom_id="bella:satisfaction:active:v1")
    async def active(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.send_message(embed=await self.service.active_embed(interaction.guild), ephemeral=True)

    @discord.ui.button(label="Pesquisa pós-ticket", style=discord.ButtonStyle.danger, custom_id="bella:satisfaction:auto_ticket:v1")
    async def auto_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        enabled = await self.service.toggle_auto_ticket(interaction.guild.id)
        await interaction.response.send_message(
            f"Pesquisa privada após tickets {'ativada' if enabled else 'desativada'}.",
            ephemeral=True,
        )


class SatisfactionChannelView(discord.ui.View):
    def __init__(self, service: SatisfactionService, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.owner_id = owner_id
        select = discord.ui.ChannelSelect(
            placeholder="Canal de pesquisas",
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
        await interaction.response.send_message(f"Canal de pesquisas definido para <#{channel_id}>.", ephemeral=True)


class SatisfactionCreateModal(discord.ui.Modal, title="Nova pesquisa"):
    survey_title = discord.ui.TextInput(label="Título", max_length=256)
    question = discord.ui.TextInput(
        label="Pergunta",
        style=discord.TextStyle.paragraph,
        max_length=2000,
    )
    source = discord.ui.TextInput(
        label="Origem",
        placeholder="evento, atendimento, comunidade...",
        default="geral",
        max_length=120,
    )

    def __init__(self, service: SatisfactionService) -> None:
        super().__init__()
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        try:
            survey = await self.service.create_public_survey(
                interaction.guild,
                title=str(self.survey_title.value),
                question=str(self.question.value),
                source=str(self.source.value),
                created_by=interaction.user.id,
            )
        except (ValueError, discord.HTTPException) as exc:
            await interaction.response.send_message(f"Não consegui criar a pesquisa: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Pesquisa **{survey.get('title')}** publicada com ID `{survey.get('id')}`.",
            ephemeral=True,
        )


def survey_embed(survey: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=str(survey.get("title") or "Pesquisa de satisfação")[:256],
        description=str(survey.get("question") or "Como foi sua experiência?")[:4096],
        color=discord.Color.from_rgb(255, 80, 120),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Como responder", value="Escolha uma nota de 1 a 5 e, se quiser, deixe um comentário privado.", inline=False)
    embed.set_footer(text=f"Bella Feedback • {survey.get('source')} • ID {survey.get('id')}")
    return embed


def panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Bella Feedback",
        description="Pesquisas de satisfação para tickets, eventos e outras experiências da comunidade.",
        color=discord.Color.from_rgb(255, 80, 120),
    )
    embed.add_field(name="Privacidade", value="Comentários não aparecem em logs ou painéis; somente métricas agregadas são exibidas.", inline=False)
    embed.add_field(name="Pós-ticket", value="Pode enviar automaticamente uma pesquisa privada quando um atendimento é encerrado.", inline=False)
    embed.add_field(name="Análise", value="A Bella identifica termos recorrentes em avaliações baixas sem reproduzir textos privados.", inline=False)
    return embed


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
