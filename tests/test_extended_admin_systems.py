import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from website_guard.critical_changes import CriticalChangeDetector
from website_guard.giveaways import GiveawayService, _split_ints
from website_guard.panel_repair import PanelRepairService
from website_guard.satisfaction import SatisfactionService
from website_guard.state import StateStore


class CriticalChangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_role_gaining_administrator_creates_critical_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            logs = SimpleNamespace(send_event=AsyncMock(), channel_ids=AsyncMock(return_value={}))
            service = CriticalChangeDetector(state, logs, authorize=lambda _: True)
            guild = SimpleNamespace(id=10)
            before = SimpleNamespace(
                id=5,
                name="Suporte",
                guild=guild,
                permissions=discord.Permissions.none(),
                members=[1, 2],
                position=3,
            )
            after = SimpleNamespace(
                id=5,
                name="Suporte",
                guild=guild,
                permissions=discord.Permissions(administrator=True),
                members=[1, 2],
                position=3,
            )

            await service.on_role_update(before, after)

            events = await service.recent_events(guild.id)
            self.assertEqual(events[0]["rule_id"], "role.permissions_gained")
            self.assertEqual(events[0]["severity"], "CRITICAL")
            self.assertTrue(logs.send_event.await_args.kwargs["critical"])


class PanelRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_deleted_registered_panel_opens_issue_without_history_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            await state.set(
                "fixed_admin_panels",
                {
                    "guilds": {
                        "10": {
                            "ticket_admin": [
                                {"channel_id": 20, "message_id": 30},
                            ]
                        }
                    }
                },
            )
            guild = SimpleNamespace(id=10)
            bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 10 else None)
            logs = SimpleNamespace(send_event=AsyncMock())
            service = PanelRepairService(bot, state, logs, authorize=lambda _: True)

            found = await service.on_raw_message_delete(
                SimpleNamespace(guild_id=10, channel_id=20, message_id=30)
            )

            self.assertTrue(found)
            issues = await service.issues(10)
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["kind"], "ticket_admin")
            self.assertEqual(issues[0]["problem"], "missing_message")

    async def test_deleted_role_is_available_for_selective_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            guild = SimpleNamespace(id=10)
            role = MagicMock(spec=discord.Role)
            role.id = 50
            role.name = "Suporte"
            role.guild = guild
            role.managed = False
            role.is_default.return_value = False
            role.permissions = discord.Permissions(manage_messages=True)
            role.colour = discord.Colour(0xFF5078)
            role.hoist = True
            role.mentionable = False
            role.position = 4
            logs = SimpleNamespace(send_event=AsyncMock())
            service = PanelRepairService(
                SimpleNamespace(get_guild=lambda _: guild),
                state,
                logs,
                authorize=lambda _: True,
            )

            await service.on_role_delete(role)

            issues = await service.issues(guild.id)
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["resource_type"], "role")
            self.assertEqual(issues[0]["snapshot"]["name"], "Suporte")
            self.assertEqual(issues[0]["snapshot"]["permissions"], role.permissions.value)

    async def test_selected_channel_is_remapped_before_panel_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            await state.set(
                "panel_repair_issues",
                {
                    "10": [
                        {
                            "id": "channel-1",
                            "status": "OPEN",
                            "resource_type": "channel",
                            "resource_id": 20,
                            "snapshot": {"name": "painel", "type": "text"},
                        },
                        {
                            "id": "panel-1",
                            "status": "OPEN",
                            "resource_type": "panel",
                            "channel_id": 20,
                            "message_id": 30,
                            "kind": "ticket_admin",
                            "snapshot": {"channel_name": "painel"},
                        },
                    ]
                },
            )
            await state.set(
                "fixed_admin_panels",
                {
                    "guilds": {
                        "10": {
                            "ticket_admin": [{"channel_id": 20, "message_id": 30}]
                        }
                    }
                },
            )
            restored_channel = MagicMock(spec=discord.TextChannel)
            restored_channel.id = 220
            guild = SimpleNamespace(
                id=10,
                get_channel=lambda channel_id: restored_channel if channel_id == 220 else None,
            )
            message = SimpleNamespace(id=330, jump_url="https://discord.test/messages/330")
            logs = SimpleNamespace(send_event=AsyncMock())
            service = PanelRepairService(
                SimpleNamespace(get_guild=lambda _: guild),
                state,
                logs,
                authorize=lambda _: True,
            )
            recreate = AsyncMock(return_value=message)
            service.set_recreate_callback(recreate)

            with patch.object(service, "_restore_channel", AsyncMock(return_value=restored_channel)):
                restored, failures = await service.restore_selected(
                    guild,
                    ["panel-1", "channel-1"],
                    actor_id=99,
                )

            self.assertEqual(failures, [])
            self.assertEqual(len(restored), 2)
            self.assertIs(recreate.await_args.args[2], restored_channel)
            references = await service.references(guild.id)
            self.assertEqual(references[0][1]["channel_id"], 220)
            self.assertEqual(references[0][1]["message_id"], 330)


class GiveawayTests(unittest.IsolatedAsyncioTestCase):
    def test_compact_modal_values_are_validated(self) -> None:
        self.assertEqual(_split_ints("123 | 7", 2), [123, 7])
        with self.assertRaises(ValueError):
            _split_ints("cargo | 7", 2)

    async def test_draw_selects_multiple_unique_winners(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            logs = SimpleNamespace(send_event=AsyncMock())
            bot = SimpleNamespace(guilds=[], user=None)
            service = GiveawayService(bot, state, logs, authorize=lambda _: True)
            guild = SimpleNamespace(id=10, get_member=lambda member_id: object())
            payload = {
                "id": "draw-1",
                "guild_id": 10,
                "prize": "Teste",
                "status": "ACTIVE",
                "winners_count": 2,
                "winner_ids": [],
                "rerolls": [],
                "recurrence_hours": 0,
            }
            await service._save(10, "draw-1", payload)
            await state.update_section("giveaway_entries", "draw-1", list(range(1, 9)))

            with (
                patch.object(service, "_finish_public_message", AsyncMock()),
                patch.object(service, "_append_history", AsyncMock()),
            ):
                winners = await service.draw(guild, "draw-1")

            self.assertEqual(len(winners), 2)
            self.assertEqual(len(set(winners)), 2)
            saved = await service.get(10, "draw-1")
            self.assertEqual(saved["status"], "ENDED")

    async def test_rich_draft_can_be_reused_without_copying_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            logs = SimpleNamespace(send_event=AsyncMock())
            bot = SimpleNamespace(guilds=[], user=None)
            service = GiveawayService(bot, state, logs, authorize=lambda _: True)
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 30
            channel.send = AsyncMock()
            guild = SimpleNamespace(id=10, get_channel=lambda channel_id: channel if channel_id == 30 else None)
            await service.set_channel(guild.id, channel.id)

            draft = await service.create(
                guild,
                prize="Setup completo",
                winners_count=2,
                duration_minutes=90,
                required_role_id=None,
                minimum_server_days=7,
                start_delay_minutes=0,
                recurrence_hours=24,
                created_by=1,
                details="Detalhes completos do sorteio.",
                image_url="https://example.com/banner.png",
                required_role_ids=[100, 200],
                mention_role_ids=[300],
                publish_immediately=False,
            )
            await state.update_section("giveaway_entries", str(draft["id"]), [11, 12])
            reused = await service.reuse(guild, str(draft["id"]), created_by=2)

            self.assertEqual(draft["status"], "DRAFT")
            self.assertEqual(draft["required_role_ids"], [100, 200])
            self.assertEqual(draft["mention_role_ids"], [300])
            self.assertEqual(draft["image_url"], "https://example.com/banner.png")
            self.assertEqual(reused["status"], "DRAFT")
            self.assertEqual(reused["reused_from"], draft["id"])
            self.assertEqual(await service.entries(str(reused["id"])), [])
            channel.send.assert_not_awaited()


class SatisfactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_and_criticism_summary_do_not_expose_private_comments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            service = SatisfactionService(
                SimpleNamespace(guilds=[]),
                state,
                SimpleNamespace(send_event=AsyncMock()),
                authorize=lambda _: True,
            )
            await state.set(
                "satisfaction_surveys",
                {
                    "10": {
                        "survey": {
                            "id": "survey",
                            "guild_id": 10,
                            "title": "Atendimento",
                            "created_at": "2026-01-01T00:00:00+00:00",
                            "status": "ACTIVE",
                        }
                    }
                },
            )
            await state.set(
                "satisfaction_responses",
                {
                    "survey": {
                        "1": {"rating": 2, "comment": "demora enorme no atendimento"},
                        "2": {"rating": 2, "comment": "muita demora no atendimento"},
                        "3": {"rating": 5, "comment": "ótimo atendimento"},
                    }
                },
            )

            metrics = await service.metrics(10)
            topics = await service.recurring_criticisms(10)
            embed = await service.criticisms_embed(SimpleNamespace(id=10))

            self.assertEqual(metrics["responses"], 3)
            self.assertAlmostEqual(metrics["average"], 3.0)
            self.assertIn(("demora", 2), topics)
            self.assertNotIn("demora enorme no atendimento", embed.description)


if __name__ == "__main__":
    unittest.main()
