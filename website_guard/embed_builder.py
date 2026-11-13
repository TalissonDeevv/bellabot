from __future__ import annotations

import io
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import discord


EMBED_AI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "color": {"type": "string"},
        "author": {"type": "string"},
        "footer": {"type": "string"},
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                    "inline": {"type": "boolean"},
                },
                "required": ["name", "value"],
            },
        },
        "buttons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "url": {"type": "string"},
                    "emoji": {"type": "string"},
                },
                "required": ["label", "url"],
            },
        },
    },
    "required": ["title", "description", "color", "fields", "buttons"],
}


@dataclass
class DraftField:
    name: str
    value: str
    inline: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "inline": self.inline}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DraftField":
        return cls(
            name=str(data.get("name", "")),
            value=str(data.get("value", "")),
            inline=bool(data.get("inline", False)),
        )


@dataclass
class DraftButton:
    label: str
    url: str
    emoji: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "url": self.url, "emoji": self.emoji}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DraftButton":
        return cls(
            label=str(data.get("label", "")),
            url=str(data.get("url", "")),
            emoji=str(data.get("emoji") or "") or None,
        )


@dataclass
class EmbedDraft:
    title: str = ""
    description: str = ""
    color: int = 0x5865F2
    author: str = ""
    author_icon_url: str = ""
    footer: str = ""
    footer_icon_url: str = ""
    thumbnail_url: str = ""
    image_url: str = ""
    image_position: str = "end"
    timestamp: bool = False
    fields: list[DraftField] = field(default_factory=list)
    buttons: list[DraftButton] = field(default_factory=list)

    def to_embed(
        self,
        *,
        include_image: bool = True,
        fields: list[DraftField] | None = None,
        description: str | None = None,
        include_footer: bool = True,
    ) -> discord.Embed:
        selected_fields = self.fields if fields is None else fields
        description = (self.description if description is None else description)[:4096].strip()
        if (
            not description
            and not selected_fields
            and not _valid_url(self.image_url)
            and not _valid_url(self.thumbnail_url)
        ):
            description = "\u200b"
        embed = discord.Embed(
            title=self.title[:256] or None,
            description=description or None,
            color=discord.Color(self.color),
            timestamp=datetime.now(timezone.utc) if self.timestamp else None,
        )
        if self.author:
            kwargs: dict[str, Any] = {"name": self.author[:256]}
            if _valid_url(self.author_icon_url):
                kwargs["icon_url"] = self.author_icon_url
            embed.set_author(**kwargs)
        if include_footer and self.footer:
            kwargs = {"text": self.footer[:2048]}
            if _valid_url(self.footer_icon_url):
                kwargs["icon_url"] = self.footer_icon_url
            embed.set_footer(**kwargs)
        if _valid_url(self.thumbnail_url):
            embed.set_thumbnail(url=self.thumbnail_url)
        if include_image and _valid_url(self.image_url):
            embed.set_image(url=self.image_url)
        for item in selected_fields[:25]:
            embed.add_field(
                name=item.name[:256] or "\u200b",
                value=item.value[:1024] or "\u200b",
                inline=item.inline,
            )
        return embed

    def image_embed(self) -> discord.Embed:
        embed = discord.Embed(color=discord.Color(self.color))
        embed.set_image(url=self.image_url)
        return embed

    def to_embeds(self) -> list[discord.Embed]:
        if not _valid_url(self.image_url):
            return [self.to_embed(include_image=False)]
        position = _normalize_image_position(self.image_position)
        if position == "top":
            return [self.image_embed(), self.to_embed(include_image=False)]
        if position == "middle":
            if self.fields:
                before = self.to_embed(include_image=False, fields=[], include_footer=False)
                after = self.to_embed(include_image=False, description="", fields=self.fields)
                return [before, self.image_embed(), after]
            return [self.to_embed(include_image=False), self.image_embed()]
        return [self.to_embed(include_image=True)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "color": f"#{self.color:06X}",
            "author": self.author,
            "author_icon_url": self.author_icon_url,
            "footer": self.footer,
            "footer_icon_url": self.footer_icon_url,
            "thumbnail_url": self.thumbnail_url,
            "image_url": self.image_url,
            "image_position": _normalize_image_position(self.image_position),
            "timestamp": self.timestamp,
            "fields": [item.to_dict() for item in self.fields],
            "buttons": [item.to_dict() for item in self.buttons],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmbedDraft":
        return cls(
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            color=_parse_color(str(data.get("color", "#5865F2"))),
            author=str(data.get("author", "")),
            author_icon_url=str(data.get("author_icon_url", "")),
            footer=str(data.get("footer", "")),
            footer_icon_url=str(data.get("footer_icon_url", "")),
            thumbnail_url=str(data.get("thumbnail_url", "")),
            image_url=str(data.get("image_url", "")),
            image_position=_normalize_image_position(data.get("image_position")),
            timestamp=bool(data.get("timestamp", False)),
            fields=[
                DraftField.from_dict(item)
                for item in data.get("fields", [])
                if isinstance(item, dict)
            ][:25],
            buttons=[
                DraftButton.from_dict(item)
                for item in data.get("buttons", [])
                if isinstance(item, dict)
            ][:5],
        )

    @classmethod
    def from_discord_embed(cls, source: discord.Embed) -> "EmbedDraft":
        return cls(
            title=source.title or "",
            description=source.description or "",
            color=source.color.value if source.color else 0x5865F2,
            author=source.author.name or "",
            author_icon_url=source.author.icon_url or "",
            footer=source.footer.text or "",
            footer_icon_url=source.footer.icon_url or "",
            thumbnail_url=source.thumbnail.url or "",
            image_url=source.image.url or "",
            image_position="end",
            timestamp=source.timestamp is not None,
            fields=[
                DraftField(item.name, item.value, item.inline) for item in source.fields
            ],
        )

    @classmethod
    def from_message(cls, message: discord.Message) -> "EmbedDraft":
        drafts = cls.from_message_all(message)
        if not drafts:
            raise ValueError("A mensagem não possui embed.")
        return drafts[0]

    @classmethod
    def from_message_all(cls, message: discord.Message) -> list["EmbedDraft"]:
        drafts = [cls.from_discord_embed(embed) for embed in message.embeds[:10]]
        if not drafts:
            raise ValueError("A mensagem não possui embed.")
        buttons: list[DraftButton] = []
        for row in message.components:
            for component in getattr(row, "children", []):
                if isinstance(component, discord.Button) and component.url:
                    buttons.append(
                        DraftButton(
                            label=component.label or "Abrir link",
                            url=component.url,
                            emoji=str(component.emoji) if component.emoji else None,
                        )
                    )
        if buttons:
            drafts[0].buttons = buttons[:5]
        return drafts


class EmbedEditorSession:
    def __init__(
        self,
        owner_id: int,
        target_channel: discord.abc.Messageable,
        *,
        draft: EmbedDraft | None = None,
        drafts: list[EmbedDraft] | None = None,
        target_message: discord.Message | None = None,
        message_content: str = "",
        ai_generator: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        save_callback: Callable[[discord.Interaction, "EmbedEditorSession"], Awaitable[str]] | None = None,
        allow_link_buttons: bool = True,
    ) -> None:
        self.owner_id = owner_id
        self.target_channel = target_channel
        self.drafts = drafts[:] if drafts else [draft or EmbedDraft()]
        self.active_index = 0
        self.target_message = target_message
        self.message_content = _safe_message_content(message_content)
        self.ai_generator = ai_generator
        self.save_callback = save_callback
        self.allow_link_buttons = allow_link_buttons
        self.generated_by_ai = False

    @property
    def draft(self) -> EmbedDraft:
        return self.drafts[self.active_index]

    def preview_view(self) -> "EmbedEditorView":
        return EmbedEditorView(self)

    def editor_embeds(self) -> list[discord.Embed]:
        draft = self.draft
        embeds = draft.to_embeds()
        embed = embeds[-1]
        embed.title = embed.title or "Título do Embed"
        embed.description = (
            embed.description
            or "Você também pode criar mensagens com botões, campos e menções. Use os controles abaixo para configurar."
        )
        embed.color = discord.Color(draft.color)
        summary = [
            f"Embed ativo: {self.active_index + 1}/{len(self.drafts)}",
            f"Campos: {len(draft.fields)}/25",
            f"Botões de link: {len(draft.buttons)}/5",
            f"Imagem: {_image_position_label(draft.image_position)}",
            f"Mensagem junto: {'sim' if self.message_content else 'não'}",
        ]
        embed.add_field(name="Editor", value="\n".join(summary), inline=False)
        return embeds[:10]

    def editor_embed(self) -> discord.Embed:
        return self.editor_embeds()[-1]

    def export_payload(self) -> dict[str, Any]:
        return {
            "content": self.message_content,
            "embeds": [draft.to_dict() for draft in self.drafts],
        }

    def apply_payload(self, data: dict[str, Any], *, fallback_prompt: str = "Comunicado") -> None:
        data = _normalize_embed_payload(data, fallback_prompt=fallback_prompt)
        if "content" in data:
            self.message_content = _safe_message_content(str(data.get("content") or ""))
        embed_data = data.get("embeds", data.get("embed", data))
        if isinstance(embed_data, dict):
            self.drafts = [EmbedDraft.from_dict(embed_data)]
        elif isinstance(embed_data, list):
            drafts = [
                EmbedDraft.from_dict(item)
                for item in embed_data
                if isinstance(item, dict)
            ]
            if drafts:
                self.drafts = drafts[:10]
        if not self.allow_link_buttons:
            for draft in self.drafts:
                draft.buttons = []
        self.active_index = min(self.active_index, len(self.drafts) - 1)

    def link_view(self) -> discord.ui.View | None:
        buttons = self.drafts[0].buttons if self.drafts else []
        if not buttons and not self.generated_by_ai:
            return None
        view = discord.ui.View(timeout=None)
        for button in buttons[:5]:
            kwargs: dict[str, Any] = {
                "label": button.label[:80],
                "url": button.url,
            }
            emoji = _safe_button_emoji(button.emoji)
            if emoji is not None:
                kwargs["emoji"] = emoji
            try:
                view.add_item(discord.ui.Button(**kwargs))
            except (TypeError, ValueError):
                kwargs.pop("emoji", None)
                view.add_item(discord.ui.Button(**kwargs))
        if self.generated_by_ai:
            view.add_item(
                discord.ui.Button(
                    label="Mensagem Automática do Sistema",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"jarvis:embed:ai-badge:{self.owner_id}",
                    disabled=True,
                )
            )
        return view

    async def send(
        self,
        channel: discord.abc.Messageable | None = None,
        *,
        edit_target: bool = True,
    ) -> str:
        target = channel or self.target_channel
        kwargs: dict[str, Any] = {
            "content": self.message_content or None,
            "embeds": _flatten_embed_drafts(self.drafts),
            "view": self.link_view(),
            "allowed_mentions": _allowed_mentions(),
        }
        if self.target_message and edit_target:
            await self.target_message.edit(**kwargs)
            return "Embed atualizada com sucesso."
        await target.send(**kwargs)
        return "Embed enviada com sucesso."


class EmbedSelector(discord.ui.Select["EmbedEditorView"]):
    def __init__(self, session: EmbedEditorSession) -> None:
        options = [
            discord.SelectOption(
                label=f"Embed {index + 1}",
                value=str(index),
                description=(draft.title or "Sem título")[:100],
                default=index == session.active_index,
            )
            for index, draft in enumerate(session.drafts[:10])
        ] or [discord.SelectOption(label="Embed 1", value="0")]
        super().__init__(
            placeholder="Selecione um Embed para editar",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction) -> None:
        self.session.active_index = int(self.values[0])
        await _edit_editor_message(interaction, self.session)


class EmbedEditorView(discord.ui.View):
    def __init__(self, session: EmbedEditorSession) -> None:
        super().__init__(timeout=900)
        self.session = session
        self.add_item(EmbedSelector(session))
        if not session.allow_link_buttons:
            for item in list(self.children):
                if getattr(item, "label", "") in {"Adicionar Botão", "Remover Botão"}:
                    self.remove_item(item)
        if session.save_callback is not None:
            for item in list(self.children):
                if getattr(item, "label", "") == "Enviar Personalizado":
                    self.remove_item(item)
                elif getattr(item, "label", "") == "Enviar Rápido":
                    item.label = "Salvar painel"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.session.owner_id:
            return True
        await interaction.response.send_message(
            "Somente quem abriu este editor pode usar estes controles.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Título", style=discord.ButtonStyle.secondary, row=1)
    async def title(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await interaction.response.send_modal(TitleModal(self.session))

    @discord.ui.button(label="Descrição", style=discord.ButtonStyle.secondary, row=1)
    async def description(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await interaction.response.send_modal(DescriptionModal(self.session))

    @discord.ui.button(label="Cor", style=discord.ButtonStyle.secondary, row=1)
    async def color(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await interaction.response.send_modal(ColorModal(self.session))

    @discord.ui.button(label="Autor", style=discord.ButtonStyle.secondary, row=1)
    async def author(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await interaction.response.send_modal(AuthorModal(self.session))

    @discord.ui.button(label="Mensagem", style=discord.ButtonStyle.secondary, row=1)
    async def message_content(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await interaction.response.send_modal(MessageContentModal(self.session))

    @discord.ui.button(label="Editar Campos", style=discord.ButtonStyle.secondary, row=2)
    async def fields(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await interaction.response.send_modal(FieldModal(self.session))

    @discord.ui.button(label="Imagem e Thumbnail", style=discord.ButtonStyle.secondary, row=2)
    async def media(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await interaction.response.send_modal(MediaModal(self.session))

    @discord.ui.button(label="Rodapé", style=discord.ButtonStyle.secondary, row=2)
    async def footer(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await interaction.response.send_modal(FooterModal(self.session))

    @discord.ui.button(label="Adicionar Botão", style=discord.ButtonStyle.secondary, row=2)
    async def buttons(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await interaction.response.send_modal(LinkButtonModal(self.session))

    @discord.ui.button(label="Remover Botão", style=discord.ButtonStyle.danger, row=2)
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await interaction.response.send_modal(RemoveLinkButtonModal(self.session))

    @discord.ui.button(label="Adicionar Embed", style=discord.ButtonStyle.secondary, row=3)
    async def add_embed(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        if len(self.session.drafts) >= 10:
            await interaction.response.send_message(
                "Uma mensagem aceita no máximo 10 embeds.",
                ephemeral=True,
            )
            return
        self.session.drafts.append(EmbedDraft())
        self.session.active_index = len(self.session.drafts) - 1
        await _edit_editor_message(interaction, self.session)

    @discord.ui.button(label="Remover Embed", style=discord.ButtonStyle.danger, row=3)
    async def remove_embed(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        if len(self.session.drafts) == 1:
            self.session.drafts[0] = EmbedDraft()
            self.session.active_index = 0
        else:
            del self.session.drafts[self.session.active_index]
            self.session.active_index = max(0, self.session.active_index - 1)
        await _edit_editor_message(interaction, self.session)

    @discord.ui.button(label="Gerar Embed com IA", style=discord.ButtonStyle.secondary, row=3)
    async def generate_ai(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await interaction.response.send_modal(AIGenerateModal(self.session))

    @discord.ui.button(label="Importar JSON", style=discord.ButtonStyle.primary, row=3)
    async def import_json(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await interaction.response.send_modal(EmbedJsonModal(self.session))

    @discord.ui.button(label="Exportar JSON", style=discord.ButtonStyle.primary, row=3)
    async def export_json(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        payload = json.dumps(
            self.session.export_payload(),
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        await interaction.response.send_message(
            "JSON exportado.",
            file=discord.File(io.BytesIO(payload), filename="embed.json"),
            ephemeral=True,
        )

    @discord.ui.button(label="Enviar Personalizado", style=discord.ButtonStyle.success, row=4)
    async def custom_send(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await interaction.response.send_message(
            "Escolha o canal onde deseja enviar esta embed.",
            view=EmbedSendChannelView(self.session, mode="v1"),
            ephemeral=True,
        )

    @discord.ui.button(label="Enviar Rápido", style=discord.ButtonStyle.success, row=4)
    async def quick_send(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await _send_normal_embed(interaction, self.session)

    @discord.ui.button(label="Limpar Mídia", style=discord.ButtonStyle.secondary, row=4)
    async def clear_media(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        self.session.draft.thumbnail_url = ""
        self.session.draft.image_url = ""
        self.session.draft.image_position = "end"
        await _edit_editor_message(interaction, self.session)

    @discord.ui.button(label="Limpar Embed", style=discord.ButtonStyle.danger, row=4)
    async def clear_embed(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        self.session.drafts[self.session.active_index] = EmbedDraft()
        await _edit_editor_message(interaction, self.session)

    @discord.ui.button(label="Data/Hora", style=discord.ButtonStyle.secondary, row=4)
    async def timestamp(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        self.session.draft.timestamp = not self.session.draft.timestamp
        await _edit_editor_message(interaction, self.session)


class _BaseModal(discord.ui.Modal):
    session: EmbedEditorSession

    async def refresh_editor(self, interaction: discord.Interaction) -> None:
        await _edit_editor_message(interaction, self.session)


class ContentModal(_BaseModal):
    def __init__(self, session: EmbedEditorSession, *, initial: bool) -> None:
        super().__init__(title="Gerador profissional de embeds")
        self.session = session
        self.initial = initial
        draft = session.draft
        self.embed_title = discord.ui.TextInput(
            label="Título",
            default=draft.title[:256],
            required=False,
            max_length=256,
        )
        self.description = discord.ui.TextInput(
            label="Descrição",
            default=draft.description[:4000],
            required=False,
            max_length=4000,
            style=discord.TextStyle.paragraph,
        )
        self.color = discord.ui.TextInput(
            label="Cor hexadecimal",
            default=f"#{draft.color:06X}",
            required=False,
            max_length=7,
            placeholder="#5865F2",
        )
        self.message = discord.ui.TextInput(
            label="Mensagem junto do embed / menções",
            default=session.message_content[:1800],
            required=False,
            max_length=1800,
            style=discord.TextStyle.paragraph,
            placeholder="Ex: <@&cargo> confira o comunicado em <#canal>",
        )
        for item in (self.embed_title, self.description, self.color, self.message):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            self.session.draft.color = _parse_color(str(self.color))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.session.draft.title = str(self.embed_title).strip()
        self.session.draft.description = str(self.description).strip()
        self.session.message_content = _safe_message_content(str(self.message).strip())
        if self.initial:
            await interaction.response.send_message(
                content="Pré-visualização do gerador de embed.",
                embeds=self.session.editor_embeds(),
                view=self.session.preview_view(),
                ephemeral=True,
            )
            return
        await self.refresh_editor(interaction)


class TitleModal(_BaseModal):
    def __init__(self, session: EmbedEditorSession) -> None:
        super().__init__(title="Título do embed")
        self.session = session
        self.value = discord.ui.TextInput(
            label="Título",
            default=session.draft.title[:256],
            required=False,
            max_length=256,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.session.draft.title = str(self.value).strip()
        await self.refresh_editor(interaction)


class DescriptionModal(_BaseModal):
    def __init__(self, session: EmbedEditorSession) -> None:
        super().__init__(title="Descrição do embed")
        self.session = session
        self.value = discord.ui.TextInput(
            label="Descrição",
            default=session.draft.description[:4000],
            required=False,
            max_length=4000,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.session.draft.description = str(self.value).strip()
        await self.refresh_editor(interaction)


class MessageContentModal(_BaseModal):
    def __init__(self, session: EmbedEditorSession) -> None:
        super().__init__(title="Mensagem e menções")
        self.session = session
        self.value = discord.ui.TextInput(
            label="Mensagem acima do embed",
            default=session.message_content[:1800],
            required=False,
            max_length=1800,
            style=discord.TextStyle.paragraph,
            placeholder="Ex: <@&cargo> confira em <#canal>. @everyone/@here são bloqueados.",
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.session.message_content = _safe_message_content(str(self.value).strip())
        await self.refresh_editor(interaction)


class ColorModal(_BaseModal):
    def __init__(self, session: EmbedEditorSession) -> None:
        super().__init__(title="Cor do embed")
        self.session = session
        self.value = discord.ui.TextInput(
            label="Cor hexadecimal",
            default=f"#{session.draft.color:06X}",
            required=False,
            max_length=7,
            placeholder="#5865F2",
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            self.session.draft.color = _parse_color(str(self.value))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.refresh_editor(interaction)


class AuthorModal(_BaseModal):
    def __init__(self, session: EmbedEditorSession) -> None:
        super().__init__(title="Autor do embed")
        self.session = session
        self.name = discord.ui.TextInput(
            label="Author",
            default=session.draft.author[:256],
            required=False,
            max_length=256,
        )
        self.icon = discord.ui.TextInput(
            label="Author Icon URL",
            default=session.draft.author_icon_url[:4000],
            required=False,
            max_length=4000,
        )
        self.add_item(self.name)
        self.add_item(self.icon)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        icon = str(self.icon).strip()
        if icon and not _valid_url(icon):
            await interaction.response.send_message(
                "A URL do ícone do autor precisa começar com http:// ou https://.",
                ephemeral=True,
            )
            return
        self.session.draft.author = str(self.name).strip()
        self.session.draft.author_icon_url = icon
        await self.refresh_editor(interaction)


class FooterModal(_BaseModal):
    def __init__(self, session: EmbedEditorSession) -> None:
        super().__init__(title="Rodapé do embed")
        self.session = session
        self.text = discord.ui.TextInput(
            label="Footer",
            default=session.draft.footer[:2048],
            required=False,
            max_length=2048,
        )
        self.icon = discord.ui.TextInput(
            label="Footer Icon URL",
            default=session.draft.footer_icon_url[:4000],
            required=False,
            max_length=4000,
        )
        self.add_item(self.text)
        self.add_item(self.icon)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        icon = str(self.icon).strip()
        if icon and not _valid_url(icon):
            await interaction.response.send_message(
                "A URL do ícone do rodapé precisa começar com http:// ou https://.",
                ephemeral=True,
            )
            return
        self.session.draft.footer = str(self.text).strip()
        self.session.draft.footer_icon_url = icon
        await self.refresh_editor(interaction)


class MediaModal(_BaseModal):
    def __init__(self, session: EmbedEditorSession) -> None:
        super().__init__(title="Imagem e thumbnail")
        self.session = session
        self.thumbnail = discord.ui.TextInput(
            label="URL da thumbnail",
            default=session.draft.thumbnail_url[:4000],
            required=False,
            max_length=4000,
        )
        self.image = discord.ui.TextInput(
            label="URL da imagem principal",
            default=session.draft.image_url[:4000],
            required=False,
            max_length=4000,
        )
        self.position = discord.ui.TextInput(
            label="Posição da imagem",
            default=_image_position_label(session.draft.image_position),
            required=False,
            max_length=20,
            placeholder="topo, meio ou fim",
        )
        self.add_item(self.thumbnail)
        self.add_item(self.image)
        self.add_item(self.position)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        thumbnail = str(self.thumbnail).strip()
        image = str(self.image).strip()
        for label, value in (("thumbnail", thumbnail), ("imagem", image)):
            if value and not _valid_url(value):
                await interaction.response.send_message(
                    f"A URL de {label} precisa começar com http:// ou https://.",
                    ephemeral=True,
                )
                return
        self.session.draft.thumbnail_url = thumbnail
        self.session.draft.image_url = image
        self.session.draft.image_position = _normalize_image_position(str(self.position))
        await self.refresh_editor(interaction)


class FieldModal(_BaseModal):
    def __init__(self, session: EmbedEditorSession) -> None:
        super().__init__(title="Adicionar, editar ou remover campo")
        self.session = session
        self.position = discord.ui.TextInput(
            label="Posição opcional",
            required=False,
            max_length=3,
            placeholder="Vazio adiciona; número edita; -n remove",
        )
        self.field_name = discord.ui.TextInput(
            label="Nome do campo",
            required=False,
            max_length=256,
        )
        self.field_value = discord.ui.TextInput(
            label="Valor do campo",
            required=False,
            max_length=1024,
            style=discord.TextStyle.paragraph,
        )
        self.inline = discord.ui.TextInput(
            label="Exibir lado a lado?",
            required=False,
            max_length=3,
            placeholder="sim ou não",
        )
        for item in (self.position, self.field_name, self.field_value, self.inline):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        position_text = str(self.position).strip()
        fields = self.session.draft.fields
        if position_text.startswith("-"):
            try:
                index = int(position_text[1:]) - 1
                del fields[index]
            except (ValueError, IndexError):
                await interaction.response.send_message(
                    "Informe uma posição existente para remover.",
                    ephemeral=True,
                )
                return
        else:
            name = str(self.field_name).strip()
            value = str(self.field_value).strip()
            if not name or not value:
                await interaction.response.send_message(
                    "Nome e valor são obrigatórios para adicionar ou editar.",
                    ephemeral=True,
                )
                return
            item = DraftField(
                name=name,
                value=value,
                inline=str(self.inline).strip().casefold()
                in {"sim", "s", "yes", "y", "true", "1"},
            )
            if position_text:
                try:
                    index = int(position_text) - 1
                    fields[index] = item
                except (ValueError, IndexError):
                    await interaction.response.send_message(
                        "A posição informada não existe.",
                        ephemeral=True,
                    )
                    return
            elif len(fields) >= 25:
                await interaction.response.send_message(
                    "Uma embed aceita no máximo 25 campos.",
                    ephemeral=True,
                )
                return
            else:
                fields.append(item)
        await self.refresh_editor(interaction)


class LinkButtonModal(_BaseModal):
    def __init__(self, session: EmbedEditorSession) -> None:
        super().__init__(title="Adicionar, editar ou remover botão")
        self.session = session
        self.position = discord.ui.TextInput(
            label="Posição opcional",
            required=False,
            max_length=2,
            placeholder="Vazio adiciona; número edita; -n remove",
        )
        self.label = discord.ui.TextInput(
            label="Texto do botão",
            required=False,
            max_length=80,
        )
        self.url = discord.ui.TextInput(
            label="URL do link",
            required=False,
            max_length=4000,
        )
        self.emoji = discord.ui.TextInput(
            label="Emoji opcional",
            required=False,
            max_length=100,
            placeholder="<:custom:123456789> ou emoji padrão",
        )
        for item in (self.position, self.label, self.url, self.emoji):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        position_text = str(self.position).strip()
        buttons = self.session.draft.buttons
        if position_text.startswith("-"):
            try:
                index = int(position_text[1:]) - 1
                del buttons[index]
            except (ValueError, IndexError):
                await interaction.response.send_message(
                    "Informe uma posição existente para remover.",
                    ephemeral=True,
                )
                return
        else:
            label = str(self.label).strip()
            url = str(self.url).strip()
            if not label or not _valid_url(url):
                await interaction.response.send_message(
                    "Informe um texto e uma URL http:// ou https:// válida.",
                    ephemeral=True,
                )
                return
            item = DraftButton(
                label=label,
                url=url,
                emoji=str(self.emoji).strip() or None,
            )
            if position_text:
                try:
                    index = int(position_text) - 1
                    buttons[index] = item
                except (ValueError, IndexError):
                    await interaction.response.send_message(
                        "A posição informada não existe.",
                        ephemeral=True,
                    )
                    return
            elif len(buttons) >= 5:
                await interaction.response.send_message(
                    "Uma mensagem aceita no máximo 5 links neste editor.",
                    ephemeral=True,
                )
                return
            else:
                buttons.append(item)
        await self.refresh_editor(interaction)


class RemoveLinkButtonModal(_BaseModal):
    def __init__(self, session: EmbedEditorSession) -> None:
        super().__init__(title="Remover botão de link")
        self.session = session
        self.position = discord.ui.TextInput(
            label="Número do botão",
            required=True,
            max_length=2,
            placeholder="Ex: 1",
        )
        self.add_item(self.position)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            index = int(str(self.position).strip()) - 1
            del self.session.draft.buttons[index]
        except (ValueError, IndexError):
            await interaction.response.send_message(
                "Informe o número de um botão existente.",
                ephemeral=True,
            )
            return
        await self.refresh_editor(interaction)


class AIGenerateModal(discord.ui.Modal):
    def __init__(self, session: EmbedEditorSession) -> None:
        super().__init__(title="Gerar Embed com IA")
        self.session = session
        self.prompt = discord.ui.TextInput(
            label="O que esse embed precisa comunicar?",
            required=True,
            max_length=1600,
            style=discord.TextStyle.paragraph,
            placeholder="Ex: anúncio de manutenção amanhã às 20h mencionando <@&cargo>",
        )
        self.add_item(self.prompt)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        prompt = str(self.prompt).strip()
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if self.session.ai_generator is not None:
                data = await self.session.ai_generator(prompt)
            else:
                data = _local_embed_from_prompt(prompt)
            self.session.apply_payload(data, fallback_prompt=prompt)
            self.session.generated_by_ai = True
            recreated = await _edit_or_recreate_message(
                interaction,
                content="Pré-visualização do gerador de embed.",
                embeds=self.session.editor_embeds(),
                view=self.session.preview_view(),
            )
            suffix = " Reabri a prévia porque a mensagem anterior não estava mais disponível." if recreated else ""
            await interaction.followup.send(
                f"Embed gerada. Revise e envie quando estiver pronta.{suffix}",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(
                f"Não consegui gerar a embed agora: {exc}",
                ephemeral=True,
            )


class EmbedJsonModal(_BaseModal):
    def __init__(self, session: EmbedEditorSession) -> None:
        super().__init__(title="Importar JSON")
        self.session = session
        self.payload = discord.ui.TextInput(
            label="JSON",
            required=True,
            max_length=4000,
            style=discord.TextStyle.paragraph,
            placeholder='{"content":"<@&id> texto","embeds":[{"title":"Título"}]}',
        )
        self.add_item(self.payload)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            data = json.loads(str(self.payload))
            if not isinstance(data, dict):
                raise ValueError("O JSON precisa ser um objeto.")
            self.session.apply_payload(data)
        except Exception as exc:
            await interaction.response.send_message(
                f"JSON inválido: {exc}",
                ephemeral=True,
            )
            return
        await self.refresh_editor(interaction)


class CustomSendModal(discord.ui.Modal):
    def __init__(self, session: EmbedEditorSession) -> None:
        super().__init__(title="Enviar personalizado")
        self.session = session
        self.content = discord.ui.TextInput(
            label="Mensagem acima do embed / menções",
            default=session.message_content[:1800],
            required=False,
            max_length=1800,
            style=discord.TextStyle.paragraph,
            placeholder="Ex: <@&cargo> veja em <#canal>",
        )
        self.channel = discord.ui.TextInput(
            label="Canal/thread de destino opcional",
            required=False,
            max_length=80,
            placeholder="Cole menção ou ID do canal/thread. Vazio usa o destino escolhido.",
        )
        self.add_item(self.content)
        self.add_item(self.channel)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.session.message_content = _safe_message_content(str(self.content).strip())
        channel = _resolve_text_channel(interaction, str(self.channel).strip())
        if channel is None and str(self.channel).strip():
            await interaction.response.send_message(
                "Não encontrei esse canal/thread. Use uma menção como <#123> ou o ID.",
                ephemeral=True,
            )
            return
        await _send_normal_embed(interaction, self.session, channel=channel)


@dataclass
class V2ItemDraft:
    kind: str
    content: str = ""
    url: str = ""
    label: str = ""
    emoji: str | None = None
    style: str = "secondary"
    custom_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "content": self.content,
            "url": self.url,
            "label": self.label,
            "emoji": self.emoji,
            "style": self.style,
            "custom_id": self.custom_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "V2ItemDraft":
        return cls(
            kind=str(data.get("kind", "text")),
            content=str(data.get("content", "")),
            url=str(data.get("url", "")),
            label=str(data.get("label", "")),
            emoji=str(data.get("emoji") or "") or None,
            style=str(data.get("style", "secondary")),
            custom_id=str(data.get("custom_id", "")),
        )


@dataclass
class V2ContainerDraft:
    title: str = "Criação de Containers"
    description: str = "Lembre-se de usar formatações de texto!"
    color: int = 0x5865F2
    thumbnail_url: str = ""
    items: list[V2ItemDraft] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "color": f"#{self.color:06X}",
            "thumbnail_url": self.thumbnail_url,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "V2ContainerDraft":
        return cls(
            title=str(data.get("title", "Criação de Containers")),
            description=str(data.get("description", "")),
            color=_parse_color(str(data.get("color", "#5865F2"))),
            thumbnail_url=str(data.get("thumbnail_url", "")),
            items=[
                V2ItemDraft.from_dict(item)
                for item in data.get("items", [])
                if isinstance(item, dict)
            ][:20],
        )

    def to_container(self, *, preview: bool = False) -> discord.ui.Container:
        container = discord.ui.Container(accent_color=discord.Color(self.color))

        def add_item(item: V2ItemDraft) -> None:
            if item.kind == "separator":
                container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            elif item.kind == "media" and _valid_url(item.url):
                container.add_item(
                    discord.ui.MediaGallery(
                        discord.MediaGalleryItem(item.url, description=item.content[:1024] or None)
                    )
                )
            elif item.kind == "link_button" and _valid_url(item.url):
                kwargs: dict[str, Any] = {
                    "label": item.label[:80] or "Abrir link",
                    "url": item.url,
                    "disabled": preview,
                }
                emoji = _safe_button_emoji(item.emoji)
                if emoji is not None:
                    kwargs["emoji"] = emoji
                try:
                    button = discord.ui.Button(**kwargs)
                except (TypeError, ValueError):
                    kwargs.pop("emoji", None)
                    button = discord.ui.Button(**kwargs)
                container.add_item(discord.ui.ActionRow(button))
            elif item.kind == "interactive_button":
                kwargs = {
                    "label": item.label[:80] or "Botão",
                    "style": _button_style(item.style),
                    "custom_id": (item.custom_id or f"jarvis:v2:{id(item)}")[:100],
                    "disabled": True,
                }
                emoji = _safe_button_emoji(item.emoji)
                if emoji is not None:
                    kwargs["emoji"] = emoji
                try:
                    button = discord.ui.Button(**kwargs)
                except (TypeError, ValueError):
                    kwargs.pop("emoji", None)
                    button = discord.ui.Button(**kwargs)
                container.add_item(discord.ui.ActionRow(button))
            else:
                container.add_item(discord.ui.TextDisplay(item.content or "\u200b"))

        items = list(self.items[:20])
        while items and items[0].kind in {"media", "separator"}:
            add_item(items.pop(0))

        header_parts = []
        if self.title.strip():
            header_parts.append(f"### {self.title.strip()}")
        if self.description.strip():
            header_parts.append(self.description.strip())
        header = "\n".join(header_parts).strip()
        if _valid_url(self.thumbnail_url):
            container.add_item(
                discord.ui.Section(
                    header or "\u200b",
                    accessory=discord.ui.Thumbnail(self.thumbnail_url),
                )
            )
        elif header:
            container.add_item(discord.ui.TextDisplay(header))
        for item in items:
            add_item(item)
        if not container.children:
            container.add_item(discord.ui.TextDisplay("\u200b"))
        return container


class V2EditorSession:
    def __init__(
        self,
        owner_id: int,
        target_channel: discord.abc.Messageable,
        *,
        target_message: discord.Message | None = None,
        ai_generator: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        save_callback: Callable[[discord.Interaction, "V2EditorSession"], Awaitable[str]] | None = None,
        allow_component_buttons: bool = True,
        allow_message_content: bool = True,
    ) -> None:
        self.owner_id = owner_id
        self.target_channel = target_channel
        self.target_message = target_message
        self.ai_generator = ai_generator
        self.save_callback = save_callback
        self.allow_component_buttons = allow_component_buttons
        self.allow_message_content = allow_message_content
        self.containers = [V2ContainerDraft()]
        self.active_index = 0
        self.selected_item_index: int | None = None
        self.message_content = ""

    @property
    def container(self) -> V2ContainerDraft:
        return self.containers[self.active_index]

    def editor_view(self) -> "V2EditorView":
        return V2EditorView(self)

    def publish_view(self) -> "V2PublishView":
        return V2PublishView(self.containers)

    def export_payload(self) -> dict[str, Any]:
        return {
            "content": self.message_content,
            "containers": [container.to_dict() for container in self.containers],
        }

    def apply_payload(self, data: dict[str, Any]) -> None:
        if "content" in data:
            self.message_content = _safe_message_content(str(data.get("content") or ""))
        raw_containers = data.get("containers", data.get("container", data))
        if isinstance(raw_containers, dict):
            self.containers = [V2ContainerDraft.from_dict(raw_containers)]
        elif isinstance(raw_containers, list):
            containers = [
                V2ContainerDraft.from_dict(item)
                for item in raw_containers
                if isinstance(item, dict)
            ]
            if containers:
                self.containers = containers[:5]
        if not self.allow_component_buttons:
            for container in self.containers:
                container.items = [
                    item
                    for item in container.items
                    if item.kind not in {"link_button", "interactive_button"}
                ]
        self.active_index = min(self.active_index, len(self.containers) - 1)

    async def send(
        self,
        channel: discord.abc.Messageable | None = None,
        *,
        edit_target: bool = True,
    ) -> str:
        target = channel or self.target_channel
        if self.message_content:
            await target.send(
                _safe_message_content(self.message_content),
                allowed_mentions=_allowed_mentions(),
            )
        view = self.publish_view()
        if self.target_message and edit_target:
            await self.target_message.edit(content=None, embeds=[], view=view)
            return "Container V2 atualizado com sucesso."
        await target.send(view=view)
        return "Container V2 enviado com sucesso."


class V2PublishView(discord.ui.LayoutView):
    def __init__(self, containers: list[V2ContainerDraft]) -> None:
        super().__init__(timeout=None)
        for container in containers[:5]:
            self.add_item(container.to_container(preview=False))


class V2ContainerSelect(discord.ui.Select["V2EditorView"]):
    def __init__(self, session: V2EditorSession) -> None:
        options = [
            discord.SelectOption(
                label=f"Container {index + 1}",
                value=str(index),
                description=container.title[:100],
                default=index == session.active_index,
            )
            for index, container in enumerate(session.containers[:5])
        ]
        super().__init__(
            placeholder="Selecione um Container para editar",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction) -> None:
        self.session.active_index = int(self.values[0])
        self.session.selected_item_index = None
        await _edit_v2_editor_message(interaction, self.session)


class V2AddItemSelect(discord.ui.Select["V2EditorView"]):
    def __init__(self, session: V2EditorSession) -> None:
        options = [
            discord.SelectOption(label="Texto", value="text", description="Bloco de texto com Markdown"),
            discord.SelectOption(label="Galeria de Imagem", value="media", description="Imagem ou banner por URL"),
            discord.SelectOption(label="Separador", value="separator", description="Divisão visual entre seções"),
        ]
        if session.allow_component_buttons:
            options.extend(
                [
                    discord.SelectOption(label="Botão de Link", value="link_button", description="Botão clicável para URL"),
                    discord.SelectOption(label="Botão Interativo", value="interactive_button", description="Botão visual com custom_id"),
                ]
            )
        super().__init__(placeholder="Adicionar Item", min_values=1, max_values=1, options=options)
        self.session = session

    async def callback(self, interaction: discord.Interaction) -> None:
        kind = self.values[0]
        if kind == "separator":
            self.session.container.items.append(V2ItemDraft(kind="separator"))
            await _edit_v2_editor_message(interaction, self.session)
            return
        await interaction.response.send_modal(V2ItemModal(self.session, kind=kind))


class V2EditItemSelect(discord.ui.Select["V2EditorView"]):
    def __init__(self, session: V2EditorSession) -> None:
        options = [
            discord.SelectOption(
                label=f"{index + 1}. {_item_label(item)}",
                value=str(index),
                description=(item.content or item.label or item.url or item.kind)[:100],
                default=index == session.selected_item_index,
            )
            for index, item in enumerate(session.container.items[:20])
        ] or [discord.SelectOption(label="Nenhum item criado", value="-1")]
        super().__init__(
            placeholder="Editar Item",
            min_values=1,
            max_values=1,
            options=options,
            disabled=not session.container.items,
        )
        self.session = session

    async def callback(self, interaction: discord.Interaction) -> None:
        index = int(self.values[0])
        if index < 0:
            await interaction.response.send_message("Adicione um item primeiro.", ephemeral=True)
            return
        self.session.selected_item_index = index
        item = self.session.container.items[index]
        if item.kind == "separator":
            await interaction.response.send_message(
                "Separador selecionado. Use os botões de mover ou apagar.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(V2ItemModal(self.session, kind=item.kind, index=index))


class V2EditorView(discord.ui.LayoutView):
    def __init__(self, session: V2EditorSession) -> None:
        super().__init__(timeout=900)
        self.session = session
        self.add_item(session.container.to_container(preview=True))
        self.add_item(discord.ui.ActionRow(V2ContainerSelect(session)))
        top_actions = [_v2_button("Adicionar Container", None, "add_container")]
        if session.allow_message_content:
            top_actions.append(_v2_button("Mensagem", None, "message"))
        top_actions.extend(
            [
                _v2_button("Gerar com IA", None, "ai"),
                _v2_button("Templates", None, "template"),
                _v2_button("Editor JSON", None, "json"),
            ]
        )
        self.add_item(discord.ui.ActionRow(*top_actions))
        self.add_item(discord.ui.ActionRow(V2AddItemSelect(session)))
        self.add_item(discord.ui.ActionRow(V2EditItemSelect(session)))
        self.add_item(
            discord.ui.ActionRow(
                _v2_button("Selecionar Cor", None, "color"),
                _v2_button("Apagar Container", None, "delete_container", discord.ButtonStyle.danger),
                _v2_button("Apagar Item", None, "delete_item", discord.ButtonStyle.danger),
                _v2_button("Subir", None, "move_up", discord.ButtonStyle.primary),
                _v2_button("Descer", None, "move_down", discord.ButtonStyle.primary),
            )
        )
        if session.save_callback is not None:
            self.add_item(
                discord.ui.ActionRow(
                    _v2_button("Salvar painel", None, "quick_send", discord.ButtonStyle.success),
                )
            )
        else:
            self.add_item(
                discord.ui.ActionRow(
                    _v2_button("Enviar Personalizado", None, "custom_send", discord.ButtonStyle.success),
                    _v2_button("Enviar Rápido", None, "quick_send", discord.ButtonStyle.success),
                )
            )
        for child in self.children:
            if isinstance(child, discord.ui.ActionRow):
                for button in child.children:
                    if isinstance(button, discord.ui.Button):
                        button.callback = self._callback_for(button.custom_id or "")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.session.owner_id:
            return True
        await interaction.response.send_message(
            "Somente quem abriu este editor pode usar estes controles.",
            ephemeral=True,
        )
        return False

    def _callback_for(self, custom_id: str) -> Callable[[discord.Interaction], Awaitable[None]]:
        action = custom_id.replace("jarvis:v2-editor:", "")

        async def callback(interaction: discord.Interaction) -> None:
            if action == "add_container":
                if len(self.session.containers) >= 5:
                    await interaction.response.send_message(
                        "O editor aceita até 5 containers por mensagem.",
                        ephemeral=True,
                    )
                    return
                self.session.containers.append(V2ContainerDraft())
                self.session.active_index = len(self.session.containers) - 1
                await _edit_v2_editor_message(interaction, self.session)
            elif action == "template":
                self.session.container.items = [
                    V2ItemDraft(kind="text", content="## Título da seção\nEscreva a mensagem principal aqui."),
                    V2ItemDraft(kind="separator"),
                    V2ItemDraft(kind="text", content="Use este espaço para detalhes, links e instruções."),
                ]
                await _edit_v2_editor_message(interaction, self.session)
            elif action == "message":
                await interaction.response.send_modal(V2MessageModal(self.session))
            elif action == "ai":
                await interaction.response.send_modal(V2AIGenerateModal(self.session))
            elif action == "json":
                await interaction.response.send_modal(V2JsonModal(self.session))
            elif action == "color":
                await interaction.response.send_modal(V2ContainerModal(self.session))
            elif action == "delete_container":
                if len(self.session.containers) == 1:
                    self.session.containers[0] = V2ContainerDraft(title="", description="")
                else:
                    del self.session.containers[self.session.active_index]
                    self.session.active_index = max(0, self.session.active_index - 1)
                self.session.selected_item_index = None
                await _edit_v2_editor_message(interaction, self.session)
            elif action == "delete_item":
                await _delete_v2_item(interaction, self.session)
            elif action in {"move_up", "move_down"}:
                await _move_v2_item(interaction, self.session, up=action == "move_up")
            elif action == "custom_send":
                await interaction.response.send_message(
                    "Escolha o canal onde deseja enviar este container V2.",
                    view=EmbedSendChannelView(self.session, mode="v2"),
                    ephemeral=True,
                )
            elif action == "quick_send":
                await _send_v2(interaction, self.session)

        return callback


class V2ContainerModal(discord.ui.Modal):
    def __init__(self, session: V2EditorSession) -> None:
        super().__init__(title="Configurar container")
        self.session = session
        self.title_input = discord.ui.TextInput(
            label="Título",
            default=session.container.title[:256],
            required=False,
            max_length=256,
        )
        self.description = discord.ui.TextInput(
            label="Descrição",
            default=session.container.description[:1000],
            required=False,
            max_length=1000,
            style=discord.TextStyle.paragraph,
        )
        self.color = discord.ui.TextInput(
            label="Cor",
            default=f"#{session.container.color:06X}",
            required=False,
            max_length=7,
        )
        self.thumbnail = discord.ui.TextInput(
            label="Thumbnail URL",
            default=session.container.thumbnail_url[:4000],
            required=False,
            max_length=4000,
        )
        for item in (self.title_input, self.description, self.color, self.thumbnail):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            self.session.container.color = _parse_color(str(self.color))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        thumbnail = str(self.thumbnail).strip()
        if thumbnail and not _valid_url(thumbnail):
            await interaction.response.send_message("A thumbnail precisa ser uma URL válida.", ephemeral=True)
            return
        self.session.container.title = str(self.title_input).strip()
        self.session.container.description = str(self.description).strip()
        self.session.container.thumbnail_url = thumbnail
        await _edit_v2_editor_message(interaction, self.session)


class V2MessageModal(discord.ui.Modal):
    def __init__(self, session: V2EditorSession) -> None:
        super().__init__(title="Mensagem do painel V2")
        self.session = session
        self.content = discord.ui.TextInput(
            label="Mensagem/menções antes do painel",
            default=session.message_content[:1800],
            required=False,
            max_length=1800,
            style=discord.TextStyle.paragraph,
            placeholder="Ex: <@&cargo> leia o painel abaixo. @everyone/@here são bloqueados.",
        )
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.session.message_content = _safe_message_content(str(self.content).strip())
        await _edit_v2_editor_message(interaction, self.session)


class V2AIGenerateModal(discord.ui.Modal):
    def __init__(self, session: V2EditorSession) -> None:
        super().__init__(title="Gerar painel V2 com IA")
        self.session = session
        self.prompt = discord.ui.TextInput(
            label="O que o painel precisa comunicar?",
            required=True,
            max_length=1600,
            style=discord.TextStyle.paragraph,
            placeholder="Ex: painel de regras com botões de link e menção a <@&cargo>",
        )
        self.add_item(self.prompt)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        prompt = str(self.prompt).strip()
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if self.session.ai_generator is not None:
                data = await self.session.ai_generator(prompt)
            else:
                data = _local_embed_from_prompt(prompt)
            self.session.apply_payload(_v2_payload_from_embed_payload(data, fallback_prompt=prompt))
            recreated = await _edit_or_recreate_message(
                interaction,
                view=self.session.editor_view(),
            )
            suffix = " Reabri a prévia porque a mensagem anterior não estava mais disponível." if recreated else ""
            await interaction.followup.send(
                f"Painel V2 gerado. Revise e envie quando estiver pronto.{suffix}",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(
                f"Não consegui gerar o painel V2 agora: {exc}",
                ephemeral=True,
            )


class V2ItemModal(discord.ui.Modal):
    def __init__(self, session: V2EditorSession, *, kind: str, index: int | None = None) -> None:
        super().__init__(title="Editar item V2")
        self.session = session
        self.kind = kind
        self.index = index
        current = session.container.items[index] if index is not None else V2ItemDraft(kind=kind)
        self.content = discord.ui.TextInput(
            label="Texto / descrição",
            default=current.content[:1500],
            required=False,
            max_length=1500,
            style=discord.TextStyle.paragraph,
        )
        self.url = discord.ui.TextInput(
            label="URL",
            default=current.url[:4000],
            required=kind in {"media", "link_button"},
            max_length=4000,
        )
        self.label = discord.ui.TextInput(
            label="Label do botão",
            default=current.label[:80],
            required=False,
            max_length=80,
        )
        self.emoji = discord.ui.TextInput(
            label="Emoji opcional",
            default=(current.emoji or "")[:100],
            required=False,
            max_length=100,
        )
        self.meta = discord.ui.TextInput(
            label="Estilo / custom_id",
            default=_v2_meta_default(current),
            required=False,
            max_length=120,
            placeholder="primary, success, danger, secondary | custom_id=acao",
        )
        for item in (self.content, self.url, self.label, self.emoji, self.meta):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        url = str(self.url).strip()
        if self.kind in {"media", "link_button"} and not _valid_url(url):
            await interaction.response.send_message("Informe uma URL http:// ou https:// válida.", ephemeral=True)
            return
        style, custom_id = _parse_v2_button_meta(
            str(self.meta).strip(),
            current=self.session.container.items[self.index]
            if self.index is not None
            else None,
        )
        item = V2ItemDraft(
            kind=self.kind,
            content=str(self.content).strip(),
            url=url,
            label=str(self.label).strip(),
            emoji=str(self.emoji).strip() or None,
            style=style,
            custom_id=custom_id,
        )
        if self.index is None:
            self.session.container.items.append(item)
            self.session.selected_item_index = len(self.session.container.items) - 1
        else:
            self.session.container.items[self.index] = item
            self.session.selected_item_index = self.index
        await _edit_v2_editor_message(interaction, self.session)


class V2JsonModal(discord.ui.Modal):
    def __init__(self, session: V2EditorSession) -> None:
        super().__init__(title="Editor JSON V2")
        self.session = session
        self.payload = discord.ui.TextInput(
            label="JSON",
            default=json.dumps(session.export_payload(), ensure_ascii=False)[:3800],
            required=True,
            max_length=4000,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.payload)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            data = json.loads(str(self.payload))
            if not isinstance(data, dict):
                raise ValueError("O JSON precisa ser um objeto.")
            self.session.apply_payload(data)
        except Exception as exc:
            await interaction.response.send_message(f"JSON inválido: {exc}", ephemeral=True)
            return
        await _edit_v2_editor_message(interaction, self.session)


class V2CustomSendModal(discord.ui.Modal):
    def __init__(self, session: V2EditorSession) -> None:
        super().__init__(title="Enviar container V2")
        self.session = session
        self.content = discord.ui.TextInput(
            label="Mensagem/menções antes do painel",
            default=session.message_content[:1800],
            required=False,
            max_length=1800,
            style=discord.TextStyle.paragraph,
        )
        self.channel = discord.ui.TextInput(
            label="Canal/thread de destino opcional",
            required=False,
            max_length=80,
            placeholder="Menção ou ID do canal/thread",
        )
        self.add_item(self.content)
        self.add_item(self.channel)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.session.message_content = _safe_message_content(str(self.content).strip())
        channel = _resolve_text_channel(interaction, str(self.channel).strip())
        if channel is None and str(self.channel).strip():
            await interaction.response.send_message(
                "Não encontrei esse canal/thread. Use uma menção como <#123> ou o ID.",
                ephemeral=True,
            )
            return
        await _send_v2(interaction, self.session, channel=channel)


class EmbedSendChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, view: "EmbedSendChannelView") -> None:
        self.send_view = view
        super().__init__(
            placeholder="Selecione canal ou thread para enviar",
            channel_types=_message_destination_channel_types(),
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        channel = await _resolve_selected_text_channel(
            interaction,
            self.values[0] if self.values else None,
        )
        if channel is None:
            await interaction.response.send_message("Selecione um canal de texto válido.", ephemeral=True)
            return
        await self.send_view.send_to(interaction, channel)


class EmbedSendChannelView(discord.ui.View):
    def __init__(self, session: EmbedEditorSession | V2EditorSession, *, mode: str) -> None:
        super().__init__(timeout=180)
        self.session = session
        self.mode = "v2" if mode == "v2" else "v1"
        self.add_item(EmbedSendChannelSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.session.owner_id:
            return True
        await interaction.response.send_message(
            "Somente quem abriu este editor pode escolher o canal.",
            ephemeral=True,
        )
        return False

    async def send_to(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.Messageable | None,
    ) -> None:
        if self.mode == "v2" and isinstance(self.session, V2EditorSession):
            await _send_v2(interaction, self.session, channel=channel)
            return
        if isinstance(self.session, EmbedEditorSession):
            await _send_normal_embed(interaction, self.session, channel=channel)

    @discord.ui.button(label="Enviar no canal atual", style=discord.ButtonStyle.success)
    async def send_here(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.send_to(interaction, None)

    @discord.ui.button(label="Colar ID/manual", style=discord.ButtonStyle.secondary)
    async def manual(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.mode == "v2" and isinstance(self.session, V2EditorSession):
            await interaction.response.send_modal(V2CustomSendModal(self.session))
            return
        if isinstance(self.session, EmbedEditorSession):
            await interaction.response.send_modal(CustomSendModal(self.session))


async def _send_normal_embed(
    interaction: discord.Interaction,
    session: EmbedEditorSession,
    *,
    channel: discord.abc.Messageable | None = None,
) -> None:
    if session.save_callback is not None:
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except (discord.NotFound, discord.HTTPException):
                return
        try:
            result = await session.save_callback(interaction, session)
        except Exception as exc:
            if interaction.response.is_done():
                await interaction.followup.send(
                    f"Não consegui salvar esta configuração: {exc}",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"Não consegui salvar esta configuração: {exc}",
                    ephemeral=True,
                )
            return
        if interaction.response.is_done():
            await interaction.followup.send(result, ephemeral=True)
        else:
            await _respond_edit_or_followup(
                interaction,
                content=result,
                embeds=session.editor_embeds(),
                view=session.preview_view(),
            )
        return
    try:
        result = await session.send(channel)
    except discord.HTTPException as exc:
        await interaction.response.send_message(
            f"O Discord recusou a embed: {exc}",
            ephemeral=True,
        )
        return
    if interaction.response.is_done():
        await interaction.followup.send(result, ephemeral=True)
    else:
        await _respond_edit_or_followup(
            interaction,
            content=result,
            embeds=session.editor_embeds(),
            view=None,
        )


async def _send_v2(
    interaction: discord.Interaction,
    session: V2EditorSession,
    *,
    channel: discord.abc.Messageable | None = None,
) -> None:
    if session.save_callback is not None:
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except (discord.NotFound, discord.HTTPException):
                return
        try:
            result = await session.save_callback(interaction, session)
        except Exception as exc:
            if interaction.response.is_done():
                await interaction.followup.send(
                    f"Não consegui salvar este container V2: {exc}",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"Não consegui salvar este container V2: {exc}",
                    ephemeral=True,
                )
            return
        if interaction.response.is_done():
            await interaction.followup.send(result, ephemeral=True)
        else:
            await _respond_edit_or_followup(interaction, view=session.editor_view())
            await interaction.followup.send(result, ephemeral=True)
        return
    try:
        result = await session.send(channel)
    except discord.HTTPException as exc:
        await interaction.response.send_message(
            f"O Discord recusou o container V2: {exc}",
            ephemeral=True,
        )
        return
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(result, ephemeral=True)


async def _edit_editor_message(
    interaction: discord.Interaction,
    session: EmbedEditorSession,
) -> None:
    kwargs = {
        "content": "Pré-visualização do gerador de embed.",
        "embeds": session.editor_embeds(),
        "view": session.preview_view(),
    }
    if interaction.response.is_done():
        await _edit_or_recreate_message(interaction, **kwargs)
    else:
        await _respond_edit_or_followup(interaction, **kwargs)


async def _edit_v2_editor_message(
    interaction: discord.Interaction,
    session: V2EditorSession,
) -> None:
    kwargs = {"view": session.editor_view()}
    if interaction.response.is_done():
        await _edit_or_recreate_message(interaction, **kwargs)
    else:
        await _respond_edit_or_followup(interaction, **kwargs)


async def _respond_edit_or_followup(
    interaction: discord.Interaction,
    **kwargs: Any,
) -> bool:
    try:
        await interaction.response.edit_message(**kwargs)
        return False
    except (discord.NotFound, discord.Forbidden):
        try:
            await interaction.followup.send(**kwargs, ephemeral=True)
        except (discord.NotFound, discord.Forbidden):
            return True
        return True


async def _edit_or_recreate_message(
    interaction: discord.Interaction,
    **kwargs: Any,
) -> bool:
    if interaction.message is not None:
        try:
            await interaction.message.edit(**kwargs)
            return False
        except (discord.NotFound, discord.Forbidden):
            pass
    try:
        await interaction.followup.send(**kwargs, ephemeral=True)
    except (discord.NotFound, discord.Forbidden):
        return True
    return True


async def _move_v2_item(
    interaction: discord.Interaction,
    session: V2EditorSession,
    *,
    up: bool,
) -> None:
    index = session.selected_item_index
    items = session.container.items
    if index is None or index < 0 or index >= len(items):
        await interaction.response.send_message("Selecione um item primeiro.", ephemeral=True)
        return
    target = index - 1 if up else index + 1
    if target < 0 or target >= len(items):
        await interaction.response.send_message("Esse item já está no limite.", ephemeral=True)
        return
    items[index], items[target] = items[target], items[index]
    session.selected_item_index = target
    await _edit_v2_editor_message(interaction, session)


async def _delete_v2_item(
    interaction: discord.Interaction,
    session: V2EditorSession,
) -> None:
    index = session.selected_item_index
    items = session.container.items
    if index is None or index < 0 or index >= len(items):
        await interaction.response.send_message("Selecione um item primeiro para apagar.", ephemeral=True)
        return
    removed = items.pop(index)
    del removed
    session.selected_item_index = None if not items else min(index, len(items) - 1)
    await _edit_v2_editor_message(interaction, session)


def _v2_button(
    label: str,
    emoji: str | None,
    action: str,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
) -> discord.ui.Button[Any]:
    del emoji
    return discord.ui.Button(
        label=label,
        style=style,
        custom_id=f"jarvis:v2-editor:{action}",
    )


def _v2_meta_default(item: V2ItemDraft) -> str:
    parts: list[str] = []
    if item.style:
        parts.append(item.style)
    if item.custom_id:
        parts.append(f"custom_id={item.custom_id}")
    return " | ".join(parts)


def _parse_v2_button_meta(
    value: str,
    *,
    current: V2ItemDraft | None = None,
) -> tuple[str, str]:
    style = (current.style if current else "secondary") or "secondary"
    custom_id = (current.custom_id if current else "") or ""
    if not value:
        return style, custom_id
    for raw_part in re.split(r"[|,;\n]+", value):
        part = raw_part.strip()
        normalized = part.casefold()
        if not part:
            continue
        if normalized in {"primary", "secondary", "success", "danger", "green", "red", "blue", "gray", "grey"}:
            style = {
                "green": "success",
                "red": "danger",
                "blue": "primary",
                "gray": "secondary",
                "grey": "secondary",
            }.get(normalized, normalized)
            continue
        if normalized.startswith("custom_id="):
            custom_id = part.split("=", 1)[1].strip()[:100]
    return style, custom_id


def _button_style(value: str) -> discord.ButtonStyle:
    normalized = value.strip().casefold()
    return {
        "primary": discord.ButtonStyle.primary,
        "success": discord.ButtonStyle.success,
        "danger": discord.ButtonStyle.danger,
        "secondary": discord.ButtonStyle.secondary,
    }.get(normalized, discord.ButtonStyle.secondary)


def _item_label(item: V2ItemDraft) -> str:
    return {
        "text": "Texto",
        "media": "Galeria de Imagem",
        "separator": "Separador",
        "link_button": "Botão de Link",
        "interactive_button": "Botão Interativo",
    }.get(item.kind, item.kind)


def _format_embed_description(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\r\n?", "\n", text)
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    text = re.sub(
        r"(?im)(?<!^)(?<!\n)(\b(?:Benefícios do Programa|Benefícios|Como funciona|Regras|Requisitos|Importante|Atenção|Próximos passos|Sobre o programa|Para participar|Detalhes|Resumo)\s*:)",
        r"\n\n\1",
        text,
    )
    text = re.sub(r"(?m)^([A-Za-zÀ-ÿ0-9][^\n:]{2,60})\s*:\s*", r"\1:\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if "\n\n" in text or len(text) <= 450:
        return text

    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    if len(sentences) <= 2:
        return text
    paragraphs: list[str] = []
    current: list[str] = []
    current_size = 0
    for sentence in sentences:
        current.append(sentence)
        current_size += len(sentence)
        if len(current) >= 2 or current_size >= 260:
            paragraphs.append(" ".join(current))
            current = []
            current_size = 0
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


def _extract_discord_mentions(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"<@&\d+>|<#\d+>|<@\d+>", str(text or ""))))


def _looks_like_partner_announcement_prompt(text: str) -> bool:
    normalized = str(text or "").casefold()
    return (
        ("programa de parceiros" in normalized or "afiliad" in normalized or "parceir" in normalized)
        and ("anunc" in normalized or "inscri" in normalized or "abert" in normalized or "comiss" in normalized)
    )


def _looks_like_raw_instruction_copy(text: str) -> bool:
    normalized = str(text or "").casefold()
    markers = (
        "quero que você",
        "quero que voce",
        "publique o anúncio",
        "publique o anuncio",
        "cargo para mencionar",
        "o anúncio deve",
        "o anuncio deve",
        "explique brevemente",
        "também explique",
        "tambem explique",
        "deixe claro",
    )
    return sum(1 for marker in markers if marker in normalized) >= 2


def _normalize_image_position(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = (
        text.replace("í", "i")
        .replace("ó", "o")
        .replace("é", "e")
        .replace("ã", "a")
        .replace("ç", "c")
    )
    if text in {"top", "topo", "inicio", "início", "before", "antes"}:
        return "top"
    if text in {"middle", "meio", "centro", "mid"}:
        return "middle"
    return "end"


def _image_position_label(value: Any) -> str:
    position = _normalize_image_position(value)
    return {"top": "topo", "middle": "meio", "end": "fim"}[position]


def _flatten_embed_drafts(drafts: list[EmbedDraft]) -> list[discord.Embed]:
    embeds: list[discord.Embed] = []
    for draft in drafts[:10]:
        for embed in draft.to_embeds():
            embeds.append(embed)
            if len(embeds) >= 10:
                return embeds
    return embeds or [EmbedDraft().to_embed()]


def _partner_announcement_payload(prompt: str) -> dict[str, Any]:
    role_mentions = [mention for mention in _extract_discord_mentions(prompt) if mention.startswith("<@&")]
    content = " ".join(role_mentions[:3])
    description = _format_embed_description(
        "As inscrições para o Programa de Parceiros & Afiliados estão abertas.\n\n"
        "O programa foi criado para pessoas que conhecem possíveis clientes para os serviços do Talisson e querem indicar oportunidades reais. Quando uma indicação válida resulta em um projeto fechado e o pagamento é confirmado, o parceiro recebe uma comissão conforme as regras do programa.\n\n"
        "Benefícios do Programa\n"
        "Além da comissão, os parceiros podem ter acesso a cargo exclusivo no servidor, canal exclusivo, convite personalizado para identificar indicações, acompanhamento das oportunidades, materiais de divulgação e conteúdos sobre prospecção, geração e qualificação de leads, networking, marketing, abordagem comercial, comunicação, negociação e relacionamento com clientes.\n\n"
        "Participação flexível\n"
        "Não existem horários obrigatórios, metas, quantidade mínima de indicações ou obrigação de prospecção diária. Cada pessoa pode participar no próprio ritmo e fazer uma indicação quando surgir uma oportunidade real.\n\n"
        "Quem pode participar\n"
        "A pessoa não precisa ser desenvolvedora. O mais importante é conseguir reconhecer oportunidades, conhecer pessoas ou empresas que possam precisar de soluções digitais e fazer indicações pelos canais oficiais do programa."
    )
    return {
        "content": content,
        "embeds": [
            {
                "title": "Inscrições abertas — Programa de Parceiros & Afiliados",
                "description": description[:4096],
                "color": "#5865F2",
                "footer": "Bella",
                "fields": [],
                "buttons": [],
            }
        ],
    }


def _normalize_embed_payload(data: dict[str, Any], *, fallback_prompt: str = "Comunicado") -> dict[str, Any]:
    if not isinstance(data, dict):
        return _local_embed_from_prompt(fallback_prompt)
    content = _safe_message_content(str(data.get("content") or ""))
    raw_embeds = data.get("embeds")
    if isinstance(raw_embeds, list):
        embed_items = [item for item in raw_embeds if isinstance(item, dict)]
    elif isinstance(data.get("embed"), dict):
        embed_items = [data["embed"]]
    else:
        embed_items = [data]
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(embed_items[:10]):
        title = str(item.get("title") or "").strip()
        description = _format_embed_description(item.get("description") or item.get("text") or "")
        fields = item.get("fields") if isinstance(item.get("fields"), list) else []
        image_url = str(item.get("image_url") or item.get("image") or "").strip()
        thumbnail_url = str(item.get("thumbnail_url") or item.get("thumbnail") or "").strip()
        fallback_embed = _local_embed_from_prompt(fallback_prompt)["embeds"][0]
        generic_title = title.casefold() in {"comunicado", "aviso", "embed", "mensagem"}
        if fallback_prompt.strip() and _looks_like_raw_instruction_copy(description):
            fallback_payload = _local_embed_from_prompt(fallback_prompt)
            fallback_embed = fallback_payload["embeds"][0]
            if not content:
                content = _safe_message_content(str(fallback_payload.get("content") or ""))
            description = _format_embed_description(fallback_embed.get("description") or "")
            if not title or generic_title or _looks_like_raw_instruction_copy(title):
                title = str(fallback_embed.get("title") or "").strip()
            fields = fallback_embed.get("fields") if isinstance(fallback_embed.get("fields"), list) else []
        if (
            not description
            and not fields
            and not image_url
            and not thumbnail_url
            and fallback_prompt.strip()
            and fallback_prompt.strip().casefold() != "comunicado"
        ):
            description = _format_embed_description(fallback_embed.get("description") or "")
            if not title or generic_title:
                title = str(fallback_embed.get("title") or "").strip()
        if not description:
            parts = []
            for field in fields[:6]:
                if isinstance(field, dict):
                    name = str(field.get("name") or "").strip()
                    value = str(field.get("value") or "").strip()
                    if name or value:
                        parts.append(f"{name}\n{value}".strip())
            description = _format_embed_description("\n\n".join(parts).strip())
        if not description:
            description = "\u200b"
        normalized.append(
            {
                "title": title or ("Comunicado" if index == 0 else ""),
                "description": _format_embed_description(description)[:4096],
                "color": str(item.get("color") or "#5865F2"),
                "author": str(item.get("author") or item.get("author_name") or ""),
                "author_icon_url": str(item.get("author_icon_url") or ""),
                "footer": str(item.get("footer") or ""),
                "footer_icon_url": str(item.get("footer_icon_url") or ""),
                "thumbnail_url": thumbnail_url,
                "image_url": image_url,
                "image_position": _normalize_image_position(item.get("image_position") or item.get("imagePlacement")),
                "fields": fields[:25],
                "buttons": item.get("buttons") if isinstance(item.get("buttons"), list) else [],
            }
        )
    return {"content": content, "embeds": normalized or _local_embed_from_prompt(fallback_prompt)["embeds"]}


def _local_embed_from_prompt(prompt: str) -> dict[str, Any]:
    clean = prompt.strip() or "Comunicado gerado pela Bella."
    if _looks_like_partner_announcement_prompt(clean):
        return _partner_announcement_payload(clean)
    normalized = clean.casefold()
    title = "Comunicado"
    if any(term in normalized for term in ("manutenção", "manutencao", "instabilidade", "fora do ar")):
        title = "Manutenção programada"
    elif any(term in normalized for term in ("evento", "live", "reunião", "reuniao")):
        title = "Evento comunicado"
    elif any(term in normalized for term in ("vaga", "contratando", "desenvolvedor", "emprego")):
        title = "Oportunidade divulgada"
    elif any(term in normalized for term in ("parceria", "parceiro", "afiliado")):
        title = "Programa de parceiros"
    elif any(term in normalized for term in ("aviso", "anúncio", "anuncio", "comunicado")):
        title = "Comunicado importante"
    else:
        first_sentence = re.split(r"[.!?\n]", clean, maxsplit=1)[0].strip()
        if first_sentence:
            title = first_sentence[:90]
    description_source = clean
    if _looks_like_raw_instruction_copy(clean):
        compact_lines: list[str] = []
        for line in clean.splitlines():
            stripped = line.strip(" -*")
            if not stripped:
                continue
            lowered = stripped.casefold()
            if any(
                lowered.startswith(prefix)
                for prefix in (
                    "quero que você",
                    "quero que voce",
                    "publique",
                    "cargo para mencionar",
                    "o anúncio deve",
                    "o anuncio deve",
                    "explique",
                    "também explique",
                    "tambem explique",
                    "deixe claro",
                    "não ",
                    "nao ",
                )
            ):
                continue
            compact_lines.append(stripped)
        description_source = "\n".join(compact_lines[:16]).strip() or "Comunicado gerado para publicação no Discord."
    description = _format_embed_description(description_source)
    if len(description) < 40:
        description = _format_embed_description(f"{description_source}\n\nMensagem gerada para publicação no Discord.")
    return {
        "content": "",
        "embeds": [
            {
                "title": title[:120],
                "description": _format_embed_description(description)[:1800],
                "color": "#5865F2",
                "footer": "Bella",
                "fields": [],
                "buttons": [],
            }
        ],
    }


def _v2_payload_from_embed_payload(data: dict[str, Any], *, fallback_prompt: str = "Comunicado") -> dict[str, Any]:
    data = _normalize_embed_payload(data, fallback_prompt=fallback_prompt)
    content = _safe_message_content(str(data.get("content") or ""))
    raw_embeds = data.get("embeds")
    raw_embed: dict[str, Any]
    if isinstance(raw_embeds, list) and raw_embeds and isinstance(raw_embeds[0], dict):
        raw_embed = raw_embeds[0]
    elif isinstance(data.get("embed"), dict):
        raw_embed = data["embed"]
    else:
        raw_embed = data

    fields = raw_embed.get("fields")
    buttons = raw_embed.get("buttons")
    items: list[dict[str, Any]] = []
    image_url = str(raw_embed.get("image_url") or raw_embed.get("image") or "").strip()
    if _valid_url(image_url):
        items.append({"kind": "media", "url": image_url, "content": "Banner"})
    if isinstance(fields, list):
        for field in fields[:12]:
            if not isinstance(field, dict):
                continue
            name = str(field.get("name") or "").strip()
            value = str(field.get("value") or "").strip()
            if name or value:
                items.append(
                    {
                        "kind": "text",
                        "content": f"**{name or 'Detalhe'}**\n{value or '—'}",
                    }
                )
    if isinstance(buttons, list):
        for button in buttons[:5]:
            if not isinstance(button, dict):
                continue
            url = str(button.get("url") or "").strip()
            if not _valid_url(url):
                continue
            items.append(
                {
                    "kind": "link_button",
                    "label": str(button.get("label") or "Abrir link")[:80],
                    "url": url,
                    "emoji": str(button.get("emoji") or "") or None,
                    "style": "primary",
                }
            )

    if not items:
        items.append(
            {
                "kind": "text",
                "content": str(raw_embed.get("description") or "Conteúdo gerado com IA.").strip(),
            }
        )

    return {
        "content": content,
        "containers": [
            {
                "title": str(raw_embed.get("title") or "Painel").strip()[:256],
                "description": _format_embed_description(str(raw_embed.get("description") or ""))[:1000],
                "color": str(raw_embed.get("color") or "#5865F2"),
                "thumbnail_url": str(raw_embed.get("thumbnail_url") or ""),
                "items": items[:20],
            }
        ],
    }


def _safe_button_emoji(value: str | None) -> str | discord.PartialEmoji | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"<a?:[A-Za-z0-9_]{2,32}:\d{15,25}>", text):
        return discord.PartialEmoji.from_str(text)
    if any("\U0001F000" <= character <= "\U0010FFFF" or "\u2600" <= character <= "\u27BF" for character in text):
        return text[:32]
    return None


def _resolve_text_channel(
    interaction: discord.Interaction,
    value: str,
) -> discord.TextChannel | discord.Thread | None:
    if not value or interaction.guild is None:
        return None
    match = re.search(r"<#(\d+)>", value)
    channel_id_text = match.group(1) if match else re.sub(r"\D", "", value)
    if not channel_id_text:
        return None
    channel_id = int(channel_id_text)
    getter = getattr(interaction.guild, "get_channel_or_thread", None)
    channel = (
        getter(channel_id)
        if callable(getter)
        else interaction.guild.get_channel(channel_id) or interaction.guild.get_thread(channel_id)
    )
    if channel is None:
        channel = interaction.client.get_channel(channel_id)
    return channel if isinstance(channel, (discord.TextChannel, discord.Thread)) else None


async def _resolve_selected_text_channel(
    interaction: discord.Interaction,
    selected: Any,
) -> discord.TextChannel | discord.Thread | None:
    if isinstance(selected, (discord.TextChannel, discord.Thread)):
        return selected
    channel_id = int(getattr(selected, "id", 0) or 0)
    if interaction.guild is None or not channel_id:
        return None
    getter = getattr(interaction.guild, "get_channel_or_thread", None)
    channel = (
        getter(channel_id)
        if callable(getter)
        else interaction.guild.get_channel(channel_id) or interaction.guild.get_thread(channel_id)
    ) or interaction.client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await interaction.guild.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel if isinstance(channel, (discord.TextChannel, discord.Thread)) else None


def _message_destination_channel_types() -> list[discord.ChannelType]:
    channel_types = [discord.ChannelType.text]
    for name in ("public_thread", "private_thread"):
        channel_type = getattr(discord.ChannelType, name, None)
        if channel_type is not None:
            channel_types.append(channel_type)
    return channel_types


def _safe_message_content(value: str) -> str:
    return (
        value.replace("@everyone", "@\u200beveryone")
        .replace("@here", "@\u200bhere")
        .strip()
    )


def _allowed_mentions() -> discord.AllowedMentions:
    return discord.AllowedMentions(
        everyone=False,
        users=True,
        roles=True,
        replied_user=False,
    )


def _parse_color(value: str) -> int:
    normalized = value.strip().lstrip("#") or "5865F2"
    if len(normalized) not in {3, 6}:
        raise ValueError("Use uma cor hexadecimal como #5865F2.")
    if len(normalized) == 3:
        normalized = "".join(character * 2 for character in normalized)
    try:
        return int(normalized, 16)
    except ValueError as exc:
        raise ValueError("Use uma cor hexadecimal como #5865F2.") from exc


def _valid_url(value: str) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
