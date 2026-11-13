from __future__ import annotations

import io
from dataclasses import dataclass, field

import discord


@dataclass(frozen=True)
class DashboardPage:
    key: str
    label: str
    title: str
    description: str
    filename: str
    image: bytes
    color: int
    fields: tuple[tuple[str, str, bool], ...] = field(default_factory=tuple)

    def file(self) -> discord.File:
        return discord.File(
            io.BytesIO(self.image),
            filename=self.filename,
            description=f"Gráfico da seção {self.title}",
        )

    def header_markdown(self) -> str:
        return f"## {self.title}\n{self.description}"[:4000]

    def fields_markdown(self) -> str:
        if not self.fields:
            return ""
        lines = ["### Indicadores"]
        for name, value, inline in self.fields[:12]:
            clean_name = str(name).strip()[:120] or "Indicador"
            clean_value = str(value).strip()[:1000] or "Sem dados"
            if inline and "\n" not in clean_value:
                lines.append(f"**{clean_name}:** {clean_value}")
            else:
                lines.append(f"**{clean_name}**\n{clean_value}")
        return "\n".join(lines)[:4000]


class StatisticsDashboardView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        owner_id: int,
        pages: list[DashboardPage],
        active_key: str,
    ) -> None:
        super().__init__(timeout=86400)
        if not pages:
            raise ValueError("O dashboard exige pelo menos uma página.")
        self.owner_id = owner_id
        self.pages = {page.key: page for page in pages}
        self.page_order = tuple(page.key for page in pages)
        self.active_key = (
            active_key if active_key in self.pages else self.page_order[0]
        )
        self.current_file: discord.File
        self.buttons: dict[str, discord.ui.Button] = {}
        self._build_layout()

    def _build_layout(self) -> None:
        self.clear_items()
        self.buttons.clear()
        page = self.pages[self.active_key]
        self.current_file = page.file()
        position = self.page_order.index(page.key) + 1

        container = discord.ui.Container(
            accent_color=discord.Color(page.color),
        )
        container.add_item(discord.ui.TextDisplay(page.header_markdown()))

        field_text = page.fields_markdown()
        if field_text:
            container.add_item(
                discord.ui.Separator(
                    spacing=discord.SeparatorSpacing.small,
                )
            )
            container.add_item(discord.ui.TextDisplay(field_text))

        container.add_item(
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.large,
            )
        )
        container.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(
                    self.current_file,
                    description=f"{page.title}: {page.description}"[:1024],
                )
            )
        )

        if len(self.page_order) > 1:
            container.add_item(
                discord.ui.Separator(
                    spacing=discord.SeparatorSpacing.small,
                )
            )
            row = discord.ui.ActionRow()
            for key in self.page_order:
                selected_page = self.pages[key]
                button = discord.ui.Button(
                    label=selected_page.label[:80],
                    style=(
                        discord.ButtonStyle.primary
                        if key == self.active_key
                        else discord.ButtonStyle.secondary
                    ),
                    custom_id=f"jarvis:statistics:v2:{key}"[:100],
                    disabled=key == self.active_key,
                )

                async def callback(
                    interaction: discord.Interaction,
                    selected_key: str = key,
                ) -> None:
                    await self.show_page(interaction, selected_key)

                button.callback = callback
                row.add_item(button)
                self.buttons[key] = button
            container.add_item(row)

        container.add_item(
            discord.ui.TextDisplay(
                "-# Bella Intelligence Center"
                f"  •  Seção {position} de {len(self.page_order)}"
                "  •  Dados reais do período selecionado"
            )
        )
        self.add_item(container)

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Somente quem abriu o painel pode utilizar estes controles.",
            ephemeral=True,
        )
        return False

    async def show_page(
        self,
        interaction: discord.Interaction,
        key: str,
    ) -> None:
        if key not in self.pages:
            await interaction.response.send_message(
                "Essa seção não está mais disponível.",
                ephemeral=True,
            )
            return
        self.active_key = key
        self._build_layout()
        await interaction.response.edit_message(
            attachments=[self.current_file],
            view=self,
        )
