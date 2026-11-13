from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import discord

from website_guard.embed_builder import (
    EmbedDraft,
    EmbedEditorSession,
    V2ContainerDraft,
    V2EditorSession,
    V2ItemDraft,
)
from website_guard.english_learning import EnglishLearningManager, english_panel_embeds
from website_guard.partner_program import PartnerProgramDatabase, PartnerProgramService
from website_guard.state import StateStore
from website_guard.tickets import TicketConfig, TicketPanelView, ticket_panel_message_kwargs


async def _saved(*args, **kwargs) -> str:
    del args, kwargs
    return "salvo"


def _top_level_button_ids(view: discord.ui.LayoutView) -> set[str | None]:
    return {
        getattr(item, "custom_id", None)
        for child in view.children
        if isinstance(child, discord.ui.ActionRow)
        for item in child.children
        if isinstance(item, discord.ui.Button)
    }


class ProtectedEditorTests(unittest.TestCase):
    def test_v1_protected_editor_keeps_full_embed_controls_without_button_controls(self) -> None:
        session = EmbedEditorSession(
            1,
            SimpleNamespace(),
            draft=EmbedDraft(title="", description="", timestamp=True),
            save_callback=_saved,
            allow_link_buttons=False,
        )
        view = session.preview_view()
        labels = {getattr(item, "label", None) for item in view.children}

        self.assertNotIn("Adicionar Botão", labels)
        self.assertNotIn("Remover Botão", labels)
        self.assertNotIn("Enviar Personalizado", labels)
        self.assertIn("Salvar painel", labels)
        self.assertIn("Data/Hora", labels)
        restored = EmbedDraft.from_dict(session.draft.to_dict())
        self.assertTrue(restored.timestamp)

    def test_v2_protected_editor_hides_component_buttons(self) -> None:
        session = V2EditorSession(
            1,
            SimpleNamespace(),
            save_callback=_saved,
            allow_component_buttons=False,
            allow_message_content=False,
        )
        view = session.editor_view()
        option_values = {
            option.value
            for item in view.walk_children()
            for option in getattr(item, "options", [])
        }
        labels = {getattr(item, "label", None) for item in view.walk_children()}

        self.assertNotIn("link_button", option_values)
        self.assertNotIn("interactive_button", option_values)
        self.assertNotIn("Mensagem", labels)
        self.assertIn("Salvar painel", labels)


class TicketPanelCustomizationTests(unittest.TestCase):
    def test_ticket_v1_payload_preserves_multiple_embeds_and_removed_elements(self) -> None:
        config = TicketConfig(
            guild_id=10,
            opening_channel_id=20,
            opening_hours_enabled=False,
            panel_v1_payload={
                "content": "Mensagem personalizada",
                "embeds": [
                    EmbedDraft(description="Somente descrição", author="", footer="").to_dict(),
                    EmbedDraft(title="Segundo bloco", description="").to_dict(),
                ],
            },
        )
        kwargs = ticket_panel_message_kwargs(SimpleNamespace(), config)

        self.assertEqual(kwargs["content"], "Mensagem personalizada")
        self.assertEqual(len(kwargs["embeds"]), 2)
        self.assertIsNone(kwargs["embeds"][0].title)
        self.assertIsNone(kwargs["embeds"][0].footer.text)

    def test_ticket_v2_payload_is_custom_but_opening_buttons_remain_protected(self) -> None:
        config = TicketConfig(
            guild_id=10,
            opening_channel_id=20,
            opening_hours_enabled=False,
            panel_mode="button_only",
            panel_buttons=[
                {"label": "Abrir atendimento Geral", "category": "Outro", "emoji": "", "style": "primary"}
            ],
            panel_v2_payload={
                "containers": [
                    V2ContainerDraft(
                        title="",
                        description="",
                        items=[V2ItemDraft(kind="text", content="Conteúdo totalmente livre")],
                    ).to_dict()
                ]
            },
        )
        view = TicketPanelView(SimpleNamespace(), config)
        texts = [
            str(getattr(item, "content", ""))
            for item in view.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        ]
        button_ids = {
            getattr(item, "custom_id", None)
            for item in view.walk_children()
            if isinstance(item, discord.ui.Button)
        }

        self.assertTrue(any("Conteúdo totalmente livre" in text for text in texts))
        self.assertIn("jarvis:ticket:open", button_ids)
        self.assertIn("jarvis:ticket:open", _top_level_button_ids(view))

    def test_closed_ticket_notice_is_not_inside_v1_panel(self) -> None:
        config = TicketConfig(
            guild_id=10,
            opening_channel_id=20,
            system_enabled=False,
            closed_reason="Manutenção programada.",
        )

        kwargs = ticket_panel_message_kwargs(SimpleNamespace(), config)

        self.assertNotIn("Manutenção programada", kwargs["content"] or "")
        self.assertFalse(
            any(
                field.name == config.closed_block_title
                for embed in kwargs["embeds"]
                for field in embed.fields
            )
        )

    def test_closed_ticket_notice_is_not_inside_v2_panel(self) -> None:
        config = TicketConfig(
            guild_id=10,
            opening_channel_id=20,
            system_enabled=False,
            panel_render_mode="v2",
            closed_reason="Manutenção programada.",
        )

        view = TicketPanelView(SimpleNamespace(), config)
        panel_text = [
            str(item.content)
            for item in view.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        ]

        self.assertFalse(any("Manutenção programada" in text for text in panel_text))


class PersistedPanelCustomizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_english_payload_survives_settings_reload(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            manager = EnglishLearningManager(
                StateStore(Path(directory) / "state.json"),
                SimpleNamespace(),
            )
            draft = EmbedDraft(title="", description="Painel sem título nem rodapé")
            await manager.save_panel_payload(
                10,
                mode="v1",
                editor_payload={"content": "Topo", "embeds": [draft.to_dict()]},
            )

            settings = await manager.settings_for(10)
            embeds = english_panel_embeds(settings)
            self.assertEqual(settings["panel_v1_payload"]["content"], "Topo")
            self.assertIsNone(embeds[0].title)
            self.assertIsNone(embeds[0].footer.text)

    async def test_partner_payload_round_trip_and_protected_application_button(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            database = PartnerProgramDatabase(Path(directory) / "partners.db")
            panel = await database.create_default_panel(
                guild_id=10,
                name="Parceiros",
                created_by=1,
                report_channel_id=None,
                partner_role_id=None,
            )
            service = object.__new__(PartnerProgramService)
            service.db = database
            payload = {
                "containers": [
                    V2ContainerDraft(
                        title="Painel exclusivo",
                        description="",
                        items=[V2ItemDraft(kind="text", content="Bloco removível")],
                    ).to_dict()
                ]
            }
            await database.save_editor_payload(panel.id, "v2", payload)

            restored = await database.editor_payload(panel.id, "v2")
            view = await service.build_panel_v2_view(panel.id)
            custom_ids = {
                getattr(item, "custom_id", None)
                for item in view.walk_children()
                if isinstance(item, discord.ui.Button)
            }
            texts = [
                str(getattr(item, "content", ""))
                for item in view.walk_children()
                if isinstance(item, discord.ui.TextDisplay)
            ]

            self.assertEqual(restored, payload)
            self.assertTrue(any("Bloco removível" in text for text in texts))
            self.assertIn(f"jarvis:partner:apply:{panel.id}", custom_ids)
            self.assertIn(
                f"jarvis:partner:apply:{panel.id}",
                _top_level_button_ids(view),
            )


if __name__ == "__main__":
    unittest.main()
