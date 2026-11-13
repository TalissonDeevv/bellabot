from __future__ import annotations

from typing import Awaitable, Callable
from urllib.parse import urlparse

import discord

from .discord_embeds import feedback_report_embed, feedback_status_embed
from .feedbacks import FeedbackMonitor, FeedbackScanReport
from .state import StateStore

AuthorizationCallback = Callable[[discord.Member], bool]
ScanCallback = Callable[..., Awaitable[FeedbackScanReport]]
MonitoringCallback = Callable[[], bool]
SITE_FEEDBACK_CHANNELS_SECTION = "site_feedback_panel_channels"


class SiteFeedbackPanelService:
    def __init__(
        self,
        bot: discord.Client,
        state: StateStore,
        monitor: FeedbackMonitor,
        *,
        authorize: AuthorizationCallback,
        run_scan: ScanCallback,
        monitoring_active: MonitoringCallback,
        resend_all: ScanCallback | None = None,
    ) -> None:
        self.bot = bot
        self.state = state
        self.monitor = monitor
        self.authorize = authorize
        self.run_scan = run_scan
        self.monitoring_active = monitoring_active
        self.resend_all = resend_all or run_scan

    def panel_view(self) -> "SiteFeedbackPanelView":
        return SiteFeedbackPanelView(self)

    def panel_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Feedbacks do Site",
            description=(
                "Painel fixo para sincronizar, diagnosticar e encaminhar os feedbacks "
                "publicados no site, sem depender de vários comandos slash."
            ),
            color=discord.Color.from_rgb(255, 80, 120),
        )
        embed.add_field(
            name="Destino",
            value=(
                "O canal `feedbacks-site` da Central de Logs é configurado automaticamente "
                "por `/logs-sincronizar`. Também é possível trocar o destino neste painel."
            ),
            inline=False,
        )
        embed.add_field(
            name="Sincronização segura",
            value="A rotina normal envia somente itens ainda não processados. O reenvio completo exige confirmação.",
            inline=False,
        )
        return embed

    async def status_embed(self) -> discord.Embed:
        status = await self.monitor.status(monitoring_active=self.monitoring_active())
        return feedback_status_embed(status)

    async def latest_embed(self) -> discord.Embed:
        payload = await self.state.get("latest_feedback_report")
        if not isinstance(payload, dict):
            return discord.Embed(
                title="Feedbacks do Site",
                description="Ainda não existe uma leitura salva. Use **Sincronizar agora**.",
                color=discord.Color.from_rgb(210, 153, 34),
            )
        return feedback_report_embed(FeedbackScanReport.from_dict(payload))

    async def set_channel(self, channel_id: int, guild_id: int | None = None) -> None:
        if guild_id is None:
            await self.state.set("feedback_channel_id", int(channel_id))
            return
        await self.state.update_section(
            SITE_FEEDBACK_CHANNELS_SECTION,
            str(guild_id),
            int(channel_id),
        )

    async def configured_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        overrides = await self.state.get_section(SITE_FEEDBACK_CHANNELS_SECTION)
        channel_id = int(overrides.get(str(guild.id), 0) or 0) if isinstance(overrides, dict) else 0
        if not channel_id:
            channel_id = int(await self.state.get("feedback_channel_id", 0) or 0)
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        if not channel_id:
            return None
        try:
            fetched = await self.bot.fetch_channel(channel_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.TextChannel) else None

    async def test_destination(self, guild: discord.Guild) -> discord.TextChannel:
        channel = await self.configured_channel(guild)
        if channel is None:
            raise ValueError("Configure o canal dos feedbacks primeiro.")
        await channel.send(
            embed=discord.Embed(
                title="Teste do monitor de feedbacks",
                description="O canal está acessível e pronto para receber os feedbacks do site.",
                color=discord.Color.from_rgb(46, 160, 67),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return channel


class SiteFeedbackPanelView(discord.ui.View):
    def __init__(self, service: SiteFeedbackPanelService) -> None:
        super().__init__(timeout=None)
        self.service = service
        parsed = urlparse(service.monitor.source_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            self.add_item(
                discord.ui.Button(
                    label="Abrir página",
                    style=discord.ButtonStyle.link,
                    url=service.monitor.source_url,
                    row=1,
                )
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = isinstance(interaction.user, discord.Member) and self.service.authorize(interaction.user)
        if not allowed:
            await interaction.response.send_message("Você não possui acesso a este painel.", ephemeral=True)
        return allowed

    @discord.ui.button(
        label="Sincronizar agora",
        style=discord.ButtonStyle.success,
        custom_id="bella:site_feedbacks:sync:v1",
        row=0,
    )
    async def sync(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = await self.service.run_scan(include_processed=False)
        await interaction.followup.send(embed=feedback_report_embed(report), ephemeral=True)

    @discord.ui.button(
        label="Status",
        style=discord.ButtonStyle.primary,
        custom_id="bella:site_feedbacks:status:v1",
        row=0,
    )
    async def status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(embed=await self.service.status_embed(), ephemeral=True)

    @discord.ui.button(
        label="Última leitura",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:site_feedbacks:latest:v1",
        row=0,
    )
    async def latest(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(embed=await self.service.latest_embed(), ephemeral=True)

    @discord.ui.button(
        label="Canal",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:site_feedbacks:channel:v1",
        row=0,
    )
    async def channel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Escolha o canal que receberá os feedbacks do site:",
            view=SiteFeedbackChannelView(self.service, interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Testar destino",
        style=discord.ButtonStyle.secondary,
        custom_id="bella:site_feedbacks:test:v1",
        row=1,
    )
    async def test(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel = await self.service.test_destination(interaction.guild)
        except (ValueError, discord.Forbidden, discord.HTTPException) as exc:
            await interaction.followup.send(f"Não consegui testar o destino: {exc}", ephemeral=True)
            return
        await interaction.followup.send(f"Teste enviado em {channel.mention}.", ephemeral=True)

    @discord.ui.button(
        label="Reenviar todos",
        style=discord.ButtonStyle.danger,
        custom_id="bella:site_feedbacks:resend:v1",
        row=1,
    )
    async def resend(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Isso pode publicar novamente feedbacks já enviados. Confirma o reenvio completo?",
            view=SiteFeedbackResendConfirmView(self.service, interaction.user.id),
            ephemeral=True,
        )


class SiteFeedbackChannelView(discord.ui.View):
    def __init__(self, service: SiteFeedbackPanelService, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.owner_id = owner_id
        select = discord.ui.ChannelSelect(
            placeholder="Canal dos feedbacks do site",
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
        select = self.children[0]
        selected = select.values[0] if isinstance(select, discord.ui.ChannelSelect) else None
        channel_id = int(getattr(selected, "id", 0) or 0)
        if not channel_id:
            await interaction.response.send_message("Canal inválido.", ephemeral=True)
            return
        await self.service.set_channel(channel_id, interaction.guild.id)
        await interaction.response.send_message(
            f"Os feedbacks do site serão enviados em <#{channel_id}>.",
            ephemeral=True,
        )


class SiteFeedbackResendConfirmView(discord.ui.View):
    def __init__(self, service: SiteFeedbackPanelService, owner_id: int) -> None:
        super().__init__(timeout=90)
        self.service = service
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Somente quem abriu esta confirmação pode usá-la.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirmar reenvio", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = await self.service.resend_all()
        self.stop()
        await interaction.followup.send(embed=feedback_report_embed(report), ephemeral=True)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Reenvio cancelado.", view=None)
