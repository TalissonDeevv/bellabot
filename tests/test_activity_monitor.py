from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from website_guard.activity_monitor import ServerActivityMonitor, activity_api_limitations
from website_guard.state import StateStore


class ActivityMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_message_is_logged_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            logs = SimpleNamespace(send_event=AsyncMock())
            service = ServerActivityMonitor(SimpleNamespace(get_guild=lambda _: None), state, logs)
            guild = SimpleNamespace(id=10)
            author = SimpleNamespace(id=20, bot=False, mention="<@20>", __str__=lambda _: "Pessoa")
            channel = SimpleNamespace(id=30)
            message = SimpleNamespace(
                id=40,
                guild=guild,
                webhook_id=None,
                author=author,
                channel=channel,
                created_at=datetime.now(timezone.utc),
                jump_url="https://discord.com/channels/10/30/40",
            )

            await service.observe_message(message)
            await service.observe_message(message)

            self.assertEqual(logs.send_event.await_count, 1)
            self.assertEqual(logs.send_event.await_args.args[1], "activity")

    async def test_command_ids_are_deduplicated(self) -> None:
        logs = SimpleNamespace(send_event=AsyncMock())
        service = ServerActivityMonitor(SimpleNamespace(get_guild=lambda _: None), SimpleNamespace(), logs)
        guild = SimpleNamespace(id=10)
        user = SimpleNamespace(id=20, mention="<@20>", __str__=lambda _: "Pessoa")

        for _ in range(2):
            await service.command_used(
                guild,
                user=user,
                command_name="teste",
                application_name="Outro Bot",
                channel_id=30,
                interaction_id=50,
                external=True,
            )

        self.assertEqual(logs.send_event.await_count, 1)

    def test_external_command_limitation_is_documented(self) -> None:
        self.assertIn("mensagem pública", activity_api_limitations())
        self.assertIn("efêmeros", activity_api_limitations())


if __name__ == "__main__":
    unittest.main()
