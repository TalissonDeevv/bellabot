from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from website_guard.state import StateStore
from website_guard.tickets import (
    SMART_TICKET_ACTIONS,
    TICKET_CATEGORIES,
    TICKET_OWNER_ID,
    SmartTicketManager,
    TicketButtonManagerView,
    TicketConfig,
    TicketFieldManagerView,
    TicketMenuManagerView,
    TicketPanelView,
    TicketSetupSession,
    TicketSetupView,
    TicketStore,
    _canonical_ticket_status,
    _clean_ticket_name,
    _sync_ticket_panel_reply,
    _unique_ticket_option_value,
)


class TicketSystemTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_panel_reply_is_sent_once_and_references_panel(self) -> None:
        config = TicketConfig(
            guild_id=10,
            opening_channel_id=20,
            system_enabled=False,
            closed_reason="Manutenção programada.",
        )
        panel = SimpleNamespace(id=55)
        reply = SimpleNamespace(
            id=77,
            reference=SimpleNamespace(message_id=55),
            edit=AsyncMock(),
            delete=AsyncMock(),
        )
        channel = SimpleNamespace(
            send=AsyncMock(return_value=reply),
            fetch_message=AsyncMock(return_value=reply),
        )
        manager = SimpleNamespace(
            store=SimpleNamespace(save_config=AsyncMock()),
        )

        await _sync_ticket_panel_reply(manager, config, channel, panel)
        await _sync_ticket_panel_reply(manager, config, channel, panel)

        channel.send.assert_awaited_once()
        send_kwargs = channel.send.await_args.kwargs
        self.assertIs(send_kwargs["reference"], panel)
        self.assertFalse(send_kwargs["mention_author"])
        sent_content = channel.send.await_args.args[0]
        self.assertTrue(sent_content.startswith("# Atendimento fechado no momento"))
        self.assertNotIn("Icon_Moderator_Alumni", sent_content)
        self.assertIn("Nossa Central de Atendimento", sent_content)
        self.assertIn("- Segunda a sexta: 07h às 18h", sent_content)
        self.assertIn("<#1514971351427977316>", sent_content)
        reply.edit.assert_awaited_once()
        self.assertEqual(config.panel_notice_message_id, 77)

    def test_legacy_closed_message_defaults_are_migrated(self) -> None:
        config = TicketConfig.from_dict(
            {
                "guild_id": 10,
                "opening_channel_id": 20,
                "closed_block_title": "Atendimento fechado no momento",
                "closed_block_template": (
                    "{motivo}\n\nHorários de atendimento:\n{horarios}\n\n"
                    "Se for uma dúvida geral, envie no canal {canal_duvidas}."
                ),
                "closed_schedule_text": (
                    "Segunda a sexta: 07:00 às 18:00\n"
                    "Sábado: 07:00 às 12:00\n"
                    "Domingo: fechado"
                ),
            }
        )

        self.assertEqual(config.closed_block_title, "Atendimento fechado no momento")
        self.assertIn("Nossa Central de Atendimento", config.closed_block_template)
        self.assertIn("07h às 18h", config.closed_schedule_text)

    async def test_partner_ticket_uses_channel_override_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            service = SimpleNamespace(available=False)
            bot = SimpleNamespace(user=SimpleNamespace(id=999))
            manager = SmartTicketManager(bot, service, state)
            await manager.store.save_config(
                TicketConfig(
                    guild_id=10,
                    opening_channel_id=20,
                    opening_hours_enabled=False,
                )
            )
            manager._add_team_members = AsyncMock()
            manager._refine_welcome = AsyncMock()

            thread = MagicMock(spec=discord.Thread)
            thread.id = 500
            thread.mention = "<#500>"
            thread.add_user = AsyncMock()
            thread.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 30
            channel.permissions_for.return_value = SimpleNamespace(
                create_private_threads=True,
                send_messages_in_threads=True,
            )
            channel.create_thread = AsyncMock(return_value=thread)

            guild = MagicMock(spec=discord.Guild)
            guild.id = 10
            guild.me = MagicMock(spec=discord.Member)
            guild.get_thread.return_value = None
            member = MagicMock(spec=discord.Member)
            member.id = 100
            member.display_name = "Parceiro"
            interaction = SimpleNamespace(
                guild=guild,
                user=member,
                response=SimpleNamespace(defer=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()),
            )

            await manager.create_ticket(
                interaction,
                selected_category="Suporte de Parceiros",
                opening_channel=channel,
                context_instruction="Contexto exclusivo do programa.",
                allow_outside_hours=True,
                force_auto_attendance=True,
            )
            await asyncio.sleep(0)

            record = await manager.store.ticket(thread.id)
            self.assertEqual(record["opening_channel_id"], channel.id)
            self.assertEqual(record["context_instruction"], "Contexto exclusivo do programa.")
            self.assertTrue(record["force_auto_attendance"])
            channel.create_thread.assert_awaited_once()
            manager._refine_welcome.assert_awaited_once()

    async def test_config_and_metrics_survive_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = TicketStore(StateStore(path))
            config = TicketConfig(
                guild_id=10,
                opening_channel_id=20,
                logs_channel_id=30,
                support_role_id=40,
            )
            config.title = "Suporte Talisson"
            await store.save_config(config)
            await store.mutate_metrics(
                10,
                opened=1,
                response_seconds=2.5,
                category="Suporte",
            )

            restored = TicketStore(StateStore(path))
            restored_config = await restored.config(10)
            metrics = await restored.metrics(10)

            self.assertEqual(restored_config.title, "Suporte Talisson")
            self.assertEqual(restored_config.logs_channel_id, 30)
            self.assertEqual(metrics["opened"], 1)
            self.assertEqual(metrics["categories"]["Suporte"], 1)

    async def test_open_ticket_lookup_uses_threads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TicketStore(
                StateStore(Path(directory) / "state.json")
            )
            await store.save_ticket(
                999,
                {
                    "guild_id": 10,
                    "creator_id": 50,
                    "status": "open",
                },
            )

            result = await store.open_for_user(10, 50)

            self.assertEqual(result[0], 999)
            self.assertNotIn("channel_id", result[1])

    def test_only_owner_or_configured_team_can_manage_ticket(self) -> None:
        config = TicketConfig(
            guild_id=10,
            opening_channel_id=20,
            support_role_id=40,
            admin_role_id=41,
        )
        owner = SimpleNamespace(id=TICKET_OWNER_ID)
        support = SimpleNamespace(
            id=100,
            roles=[SimpleNamespace(id=40)],
        )
        outsider = SimpleNamespace(
            id=101,
            roles=[SimpleNamespace(id=99)],
        )

        self.assertTrue(SmartTicketManager.member_is_team(owner, config))
        self.assertTrue(SmartTicketManager.member_is_team(support, config))
        self.assertFalse(SmartTicketManager.member_is_team(outsider, config))

    def test_default_panel_exposes_all_triage_categories(self) -> None:
        config = TicketConfig(guild_id=10, opening_channel_id=20)

        self.assertEqual(len(config.select_options), len(TICKET_CATEGORIES))
        self.assertEqual(
            {item["label"] for item in config.select_options},
            set(TICKET_CATEGORIES),
        )

    def test_default_v2_panel_uses_only_select_menu(self) -> None:
        config = TicketConfig(guild_id=10, opening_channel_id=20)
        view = TicketPanelView(SimpleNamespace(), config)
        custom_ids = {
            item.custom_id
            for item in view.walk_children()
            if getattr(item, "custom_id", None)
        }

        self.assertTrue(view.has_components_v2())
        self.assertIn("jarvis:ticket:subject", custom_ids)
        self.assertNotIn("jarvis:ticket:open", custom_ids)

    def test_ticket_panel_can_enable_button_modes(self) -> None:
        config = TicketConfig(
            guild_id=10,
            opening_channel_id=20,
            panel_mode="button_and_select",
        )
        combined = TicketPanelView(SimpleNamespace(), config)
        combined_ids = {
            item.custom_id
            for item in combined.walk_children()
            if getattr(item, "custom_id", None)
        }
        config.panel_mode = "button_only"
        button_only = TicketPanelView(SimpleNamespace(), config)
        button_ids = {
            item.custom_id
            for item in button_only.walk_children()
            if getattr(item, "custom_id", None)
        }

        self.assertEqual(
            combined_ids,
            {"jarvis:ticket:subject", "jarvis:ticket:open"},
        )
        self.assertEqual(button_ids, {"jarvis:ticket:open"})

    def test_old_ticket_config_migrates_to_select_only(self) -> None:
        config = TicketConfig.from_dict(
            {
                "guild_id": 10,
                "opening_channel_id": 20,
                "title": "Atendimento",
            }
        )

        self.assertEqual(config.panel_mode, "select_only")

    def test_old_button_configuration_migrates_without_losing_text(self) -> None:
        config = TicketConfig.from_dict(
            {
                "guild_id": 10,
                "opening_channel_id": 20,
                "open_button_label": "Falar com a equipe",
                "open_button_emoji": "📨",
                "panel_mode": "button_only",
            }
        )

        self.assertEqual(config.panel_buttons[0]["label"], "Falar com a equipe")
        self.assertEqual(config.panel_buttons[0]["emoji"], "📨")

    def test_setup_editor_exposes_dedicated_managers(self) -> None:
        config = TicketConfig(guild_id=10, opening_channel_id=20)
        session = TicketSetupSession(SimpleNamespace(), 50, config)
        setup = TicketSetupView(session)
        labels = {
            item.label
            for item in setup.children
            if getattr(item, "label", None)
        }

        self.assertIn("Botões de abertura", labels)
        self.assertIn("Menu de assuntos", labels)
        self.assertIn("Campos", labels)
        self.assertNotIn("Componentes", labels)
        self.assertTrue(TicketButtonManagerView(session).children)
        self.assertTrue(TicketMenuManagerView(session).children)
        self.assertTrue(TicketFieldManagerView(session).children)

    def test_custom_opening_buttons_are_rendered_individually(self) -> None:
        config = TicketConfig(
            guild_id=10,
            opening_channel_id=20,
            panel_mode="button_only",
            panel_buttons=[
                {
                    "label": "Orçamento",
                    "category": "Orçamento",
                    "emoji": "",
                    "style": "success",
                },
                {
                    "label": "Suporte",
                    "category": "Suporte",
                    "emoji": "",
                    "style": "danger",
                },
            ],
        )

        view = TicketPanelView(SimpleNamespace(), config, preview=True)
        buttons = [
            item
            for item in view.walk_children()
            if getattr(item, "custom_id", "").startswith("jarvis:ticket:open")
        ]

        self.assertEqual(
            [item.custom_id for item in buttons],
            ["jarvis:ticket:open", "jarvis:ticket:open:1"],
        )
        self.assertEqual([item.label for item in buttons], ["Orçamento", "Suporte"])
        self.assertTrue(all(item.disabled for item in buttons))

    def test_menu_values_are_made_unique_automatically(self) -> None:
        options = [
            {
                "label": "Suporte",
                "value": "suporte",
                "description": "",
                "emoji": "",
            }
        ]

        self.assertEqual(
            _unique_ticket_option_value(options, "Suporte"),
            "suporte-2",
        )

    def test_fast_ticket_conversation_is_short_and_natural(self) -> None:
        greeting = SmartTicketManager.fast_ticket_decision(
            "oi",
            display_name="João",
            current_category="Suporte",
        )
        wellbeing = SmartTicketManager.fast_ticket_decision(
            "tudo bao?",
            display_name="João",
            current_category="Suporte",
        )

        self.assertEqual(greeting["action"], "answer")
        self.assertLess(len(greeting["reply"]), 100)
        self.assertEqual(wellbeing["action"], "answer")
        self.assertNotIn("excelente por aqui", wellbeing["reply"].lower())

    def test_direct_talisson_request_escalates_without_gemini(self) -> None:
        result = SmartTicketManager.fast_ticket_decision(
            "quero falar com o Talisson",
            display_name="João",
            current_category="Suporte",
        )

        self.assertEqual(result["action"], "escalate")
        self.assertEqual(result["category"], "Solicitação ao Talisson")
        self.assertIn("encaminhar", result["reply"].lower())

    def test_commercial_request_escalates_without_gemini(self) -> None:
        result = SmartTicketManager.fast_ticket_decision(
            "quero um orçamento para um projeto personalizado",
            display_name="João",
            current_category="Outro",
        )

        self.assertEqual(result["action"], "escalate")
        self.assertEqual(result["category"], "Orçamento")

    def test_technical_issue_gets_immediate_diagnostic_question(self) -> None:
        result = SmartTicketManager.fast_ticket_decision(
            "meu site está dando erro 500",
            display_name="João",
            current_category="Suporte",
        )

        self.assertEqual(result["action"], "ask")
        self.assertEqual(result["category"], "Problema técnico")
        self.assertIn("captura de tela", result["reply"])

    def test_confirmed_solution_closes_ticket(self) -> None:
        result = SmartTicketManager.fast_ticket_decision(
            "deu certo, pode fechar",
            display_name="João",
            current_category="Suporte",
        )

        self.assertEqual(result["action"], "resolve")
        self.assertTrue(result["user_satisfied"])

    def test_ticket_order_catalog_and_custom_labels_are_supported(self) -> None:
        self.assertIn("smart_ticket_rename", SMART_TICKET_ACTIONS)
        self.assertIn("smart_ticket_status", SMART_TICKET_ACTIONS)
        self.assertEqual(
            _clean_ticket_name("<@123> Pendente Orçamento"),
            "Pendente Orçamento",
        )
        self.assertEqual(
            _canonical_ticket_status("Aguardando equipe"),
            "escalated",
        )
        self.assertIsNone(_canonical_ticket_status("Pendente Orçamento"))


if __name__ == "__main__":
    unittest.main()
