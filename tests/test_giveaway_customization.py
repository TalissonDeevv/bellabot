from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from website_guard.embed_builder import V2ContainerDraft, V2ItemDraft
from website_guard.giveaways import (
    GiveawayAdminView,
    GiveawayBonusRoleView,
    GiveawayCreationStudioView,
    GiveawayPublishRoleView,
    GiveawayService,
    _default_giveaway_v1_payload,
    _parse_giveaway_timing,
    _parse_role_id,
    _render_giveaway_containers,
)
from website_guard.state import StateStore


class GiveawayCustomizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        state = StateStore(Path(self.temporary_directory.name) / "state.json")
        self.service = GiveawayService(
            SimpleNamespace(guilds=[], user=None),
            state,
            SimpleNamespace(send_event=AsyncMock()),
            authorize=lambda _: True,
        )

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_public_v2_keeps_participation_button_outside_custom_containers(self) -> None:
        payload = {
            "id": "draw-v2",
            "status": "ACTIVE",
            "prize": "Notebook",
            "details": "Descrição personalizada",
            "starts_at": "2026-11-01T00:00:00+00:00",
            "ends_at": "2026-11-10T00:00:00+00:00",
            "winners_count": 1,
            "containers_v2": [
                V2ContainerDraft(
                    title="{premio}",
                    items=[
                        V2ItemDraft(kind="text", content="{participantes} participantes"),
                        V2ItemDraft(kind="interactive_button", label="Botão proibido"),
                    ],
                ).to_dict()
            ],
        }
        guild = SimpleNamespace(get_role=lambda _: None)

        view = await self.service.public_view(guild, payload)

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertIsInstance(view.children[0], discord.ui.Container)
        self.assertIsInstance(view.children[-1], discord.ui.ActionRow)
        buttons = [item for item in view.children[-1].children if isinstance(item, discord.ui.Button)]
        self.assertEqual([button.label for button in buttons], ["Participar"])
        self.assertNotIn("Botão proibido", str(view.children[0].to_component_dict()))

    def test_fixed_admin_panel_exposes_customization_and_bonus_actions(self) -> None:
        labels = {getattr(item, "label", None) for item in GiveawayAdminView(self.service).children}

        self.assertIn("Gerador de embeds", labels)
        self.assertIn("Bônus por cargo", labels)

    def test_creation_studio_exposes_full_v1_v2_editors_and_publish(self) -> None:
        labels = {
            getattr(item, "label", None)
            for item in GiveawayCreationStudioView(self.service, "draw-v2", 1).children
        }

        self.assertIn("Gerenciador V1", labels)
        self.assertIn("Gerenciador V2", labels)
        self.assertIn("Configurar dados", labels)
        self.assertIn("Enviar sorteio", labels)

    def test_publish_requires_explicit_ping_roles_or_without_ping(self) -> None:
        view = GiveawayPublishRoleView(self.service, "draw-v2", 1)

        self.assertTrue(any(isinstance(item, discord.ui.RoleSelect) for item in view.children))
        self.assertIn("Enviar sem ping", {getattr(item, "label", None) for item in view.children})

    def test_bonus_roles_are_managed_individually_without_select_menu(self) -> None:
        view = GiveawayBonusRoleView(self.service, "draw-v2", 1)
        labels = {getattr(item, "label", None) for item in view.children}

        self.assertFalse(any(isinstance(item, discord.ui.RoleSelect) for item in view.children))
        self.assertIn("Adicionar/editar cargo", labels)
        self.assertIn("Remover um cargo", labels)
        self.assertEqual(_parse_role_id("<@&1529974341872128070>"), 1529974341872128070)

    def test_precise_brazilian_date_is_converted_to_discord_timestamp_source(self) -> None:
        ends_at, winners, minimum_days = _parse_giveaway_timing("10/11/2099 | 2 | 7")

        self.assertEqual(ends_at.astimezone().year, 2099)
        self.assertEqual(winners, 2)
        self.assertEqual(minimum_days, 7)

    def test_v1_generator_payload_renders_placeholders_without_editable_buttons(self) -> None:
        guild = SimpleNamespace(get_role=lambda _: None)
        payload = {
            "id": "draw-v1",
            "status": "ACTIVE",
            "prize": "Console",
            "details": "Detalhes reais",
            "starts_at": "2099-11-01T00:00:00+00:00",
            "ends_at": "2099-11-10T02:59:59+00:00",
            "winners_count": 2,
            "embed_v1_payload": _default_giveaway_v1_payload(),
        }

        embeds = self.service.public_v1_embeds(guild, payload, 4, total_entries=9)

        self.assertEqual(embeds[0].title, "🎉 Console")
        self.assertIn("Detalhes reais", embeds[0].description)
        field_values = {field.name: field.value for field in embeds[0].fields}
        self.assertEqual(field_values["Entradas válidas"], "9")

    async def test_role_bonus_is_cumulative_and_does_not_duplicate_member(self) -> None:
        member = SimpleNamespace(roles=[SimpleNamespace(id=10), SimpleNamespace(id=20)])
        payload = {
            "bonus_roles": [
                {"role_id": 10, "extra_entries": 2},
                {"role_id": 20, "extra_entries": 3},
            ]
        }

        self.assertEqual(self.service.entry_weight(member, payload), 6)

    async def test_up_to_twenty_five_roles_can_be_mentioned(self) -> None:
        payload = {"id": "draw-v2", "guild_id": 10, "mention_role_ids": []}
        await self.service._save(10, "draw-v2", payload)
        guild = SimpleNamespace(id=10)

        saved = await self.service.set_mention_roles(guild, "draw-v2", list(range(1, 31)))

        self.assertEqual(saved["mention_role_ids"], list(range(1, 26)))

    async def test_draw_ends_without_winners_when_minimum_participants_is_not_met(self) -> None:
        payload = {
            "id": "draw-minimum",
            "guild_id": 10,
            "prize": "Cinco prêmios",
            "status": "ACTIVE",
            "winners_count": 5,
            "winner_ids": [],
            "rerolls": [],
            "recurrence_hours": 0,
        }
        await self.service._save(10, "draw-minimum", payload)
        await self.service.state.update_section("giveaway_entries", "draw-minimum", list(range(1, 20)))
        guild = SimpleNamespace(id=10, get_member=lambda member_id: SimpleNamespace(id=member_id, roles=[]))

        with (
            patch.object(self.service, "_finish_public_message", AsyncMock()),
            patch.object(self.service, "_append_history", AsyncMock()),
        ):
            winners = await self.service.draw(guild, "draw-minimum")

        saved = await self.service.get(10, "draw-minimum")
        self.assertEqual(winners, [])
        self.assertEqual(saved["status"], "ENDED")
        self.assertEqual(saved["ended_reason"], "INSUFFICIENT_PARTICIPANTS")

    def test_placeholders_are_rendered_and_system_buttons_are_removed(self) -> None:
        role = SimpleNamespace(id=10, mention="<@&10>")
        guild = SimpleNamespace(get_role=lambda role_id: role if role_id == 10 else None)
        payload = {
            "id": "draw-v2",
            "status": "ACTIVE",
            "prize": "Setup",
            "details": "Detalhes",
            "starts_at": "2026-11-01T00:00:00+00:00",
            "ends_at": "2026-11-10T00:00:00+00:00",
            "winners_count": 2,
            "bonus_roles": [{"role_id": 10, "extra_entries": 4}],
            "containers_v2": [
                V2ContainerDraft(
                    title="{premio}",
                    items=[
                        V2ItemDraft(kind="text", content="{bonus_cargos}"),
                        V2ItemDraft(kind="link_button", label="Alterável", url="https://example.com"),
                    ],
                ).to_dict()
            ],
        }

        containers = _render_giveaway_containers(guild, payload, 8)

        self.assertEqual(containers[0].title, "Setup")
        self.assertIn("+4", containers[0].items[0].content)
        self.assertEqual([item.kind for item in containers[0].items], ["text"])


if __name__ == "__main__":
    unittest.main()
