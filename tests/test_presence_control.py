from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import discord

from website_guard.presence_control import (
    DEFAULT_DAY_TEXT,
    DEFAULT_NIGHT_TEXT,
    default_presence_config,
    is_night_period,
    normalize_presence_config,
    resolve_presence,
    validate_clock,
    validate_timezone,
)
from website_guard.state import StateStore


class PresenceScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = default_presence_config()

    def resolve(self, hour: int, minute: int = 0):
        return resolve_presence(
            self.config,
            datetime(2026, 10, 14, hour, minute, tzinfo=timezone.utc),
        )

    def test_night_window_crosses_midnight(self) -> None:
        from datetime import time

        self.assertTrue(is_night_period(time(21, 0), time(21, 0), time(7, 0)))
        self.assertTrue(is_night_period(time(6, 59), time(21, 0), time(7, 0)))
        self.assertFalse(is_night_period(time(7, 0), time(21, 0), time(7, 0)))
        self.assertFalse(is_night_period(time(20, 59), time(21, 0), time(7, 0)))

    def test_automatic_presence_uses_sao_paulo_timezone(self) -> None:
        night = self.resolve(0, 0)
        day = self.resolve(10, 0)
        self.assertIsNotNone(night)
        self.assertIsNotNone(day)
        self.assertEqual(night.status, "idle")
        self.assertEqual(night.text, DEFAULT_NIGHT_TEXT)
        self.assertEqual(day.status, "dnd")
        self.assertEqual(day.text, DEFAULT_DAY_TEXT)

    def test_manual_mode_preserves_selected_state_and_activity(self) -> None:
        self.config.update(
            mode="manual",
            manual_status="online",
            manual_activity_type="watching",
            manual_text="Painel em manutenção",
        )
        presence = resolve_presence(self.config)
        self.assertEqual(presence.status, "online")
        self.assertEqual(presence.activity_type, "watching")
        self.assertEqual(presence.text, "Painel em manutenção")
        self.assertEqual(presence.source, "manual")

    def test_disabled_mode_yields_no_override(self) -> None:
        self.config["mode"] = "disabled"
        self.assertIsNone(resolve_presence(self.config))

    def test_invalid_values_are_normalized(self) -> None:
        config = normalize_presence_config(
            {
                "mode": "unknown",
                "timezone": "Invalid/Zone",
                "night_start": "99:99",
                "manual_status": "broken",
                "external_mode": "unknown",
                "external_guild_id": "invalid",
            }
        )
        self.assertEqual(config["mode"], "automatic")
        self.assertEqual(config["timezone"], "America/Sao_Paulo")
        self.assertEqual(config["night_start"], "21:00")
        self.assertEqual(config["manual_status"], "dnd")
        self.assertEqual(config["external_mode"], "")
        self.assertIsNone(config["external_guild_id"])

    def test_validators_reject_invalid_schedule(self) -> None:
        with self.assertRaises(ValueError):
            validate_clock("7:00")
        with self.assertRaises(ValueError):
            validate_timezone("Recife")


class PresencePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.state = StateStore(Path(self.temporary_directory.name) / "state.json")
        self.client = discord.Client(intents=discord.Intents.none())
        from website_guard.presence_control import PresenceControlManager

        self.manager = PresenceControlManager(self.client, self.state, authorize=lambda member: True)

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_defaults_and_manual_selection_are_persistent(self) -> None:
        self.assertTrue(await self.manager.ensure_defaults())
        self.assertFalse(await self.manager.ensure_defaults())
        await self.manager.set_manual_status("online", actor_id=123)
        reloaded = await self.manager.config()
        self.assertEqual(reloaded["mode"], "manual")
        self.assertEqual(reloaded["manual_status"], "online")
        self.assertEqual(reloaded["updated_by"], 123)

    async def test_persistent_panel_is_components_v2(self) -> None:
        view = self.manager.persistent_view()
        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertEqual(view.timeout, None)
        self.assertEqual(len(view.children), 1)

        custom_ids: set[str] = set()

        def collect(item) -> None:
            custom_id = getattr(item, "custom_id", None)
            if custom_id:
                custom_ids.add(custom_id)
            for child in getattr(item, "children", []):
                collect(child)

        collect(view)
        self.assertTrue(
            {
                "bella:presence:system:automatic:v2",
                "bella:presence:system:neffex:v2",
                "bella:presence:system:squarecloud:v2",
                "bella:presence:system:dynamic:v2",
                "bella:presence:system:streamer:v2",
                "bella:presence:edit-activity:v2",
            }.issubset(custom_ids)
        )

    async def test_external_mode_is_persistent_and_manual_mode_clears_it(self) -> None:
        await self.manager.set_external_mode("neffex", guild_id=456, actor_id=123)
        external = await self.manager.config()
        self.assertEqual(external["mode"], "disabled")
        self.assertEqual(external["external_mode"], "neffex")
        self.assertEqual(external["external_guild_id"], 456)

        await self.manager.set_manual_status("online", actor_id=123)
        manual = await self.manager.config()
        self.assertEqual(manual["mode"], "manual")
        self.assertEqual(manual["external_mode"], "")
        self.assertIsNone(manual["external_guild_id"])

    async def test_unknown_external_mode_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self.manager.set_external_mode("unknown", guild_id=456, actor_id=123)


if __name__ == "__main__":
    unittest.main()
