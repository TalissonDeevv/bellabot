from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from website_guard.automation import AUTOMATIONS_SECTION, KnowledgeAutomationCenter
from website_guard.state import StateStore


class FakeService:
    available = True

    def has_ready_model(self) -> bool:
        return True

    async def generate_text(self, *args, **kwargs) -> str:
        return "Reflexão única para a comunidade."


class FakeChannel:
    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name
        self.sent: list[str] = []

    async def send(self, content: str, **kwargs):
        self.sent.append(content)
        return SimpleNamespace(id=999)


class FakeGuild:
    def __init__(self, channels: list[FakeChannel]) -> None:
        self.channels = channels


class FakeBot:
    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel

    def get_channel(self, channel_id: int):
        return self.channel if channel_id == self.channel.id else None


class AutomationTests(unittest.IsolatedAsyncioTestCase):
    async def test_define_daily_automation_from_knowledge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            channel = FakeChannel(1473790456788815924, "reflexoes")
            center = KnowledgeAutomationCenter(state, FakeService())

            result = await center.define_from_text(
                (
                    "Todos os dias de manhã mande uma reflexão no canal de "
                    "#reflexoes (<#1473790456788815924>) e mencione o cargo "
                    "<@&1473792760061366424>."
                ),
                user_id=933780111742877777,
                guild=FakeGuild([channel]),
            )

            records = await state.get_section(AUTOMATIONS_SECTION)
            self.assertIsNotNone(result)
            self.assertEqual(result["type"], "automation")
            self.assertEqual(len(records), 1)
            record = next(iter(records.values()))
            self.assertEqual(record["frequency"], "daily")
            self.assertEqual(record["channel_id"], channel.id)
            self.assertEqual(record["role_ids"], ["1473792760061366424"])

    async def test_run_due_automation_sends_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            channel = FakeChannel(123, "reflexoes")
            center = KnowledgeAutomationCenter(state, FakeService())
            result = await center.define_from_text(
                "Todos os dias às 08:00 envie reflexão no canal <#123>.",
                user_id=933780111742877777,
                guild=FakeGuild([channel]),
            )
            key = result["id"]

            def make_due(records):
                records[key]["next_run_at"] = 0

            await state.mutate_section(AUTOMATIONS_SECTION, make_due)
            sent = await center.run_due(FakeBot(channel))

            self.assertEqual(sent, 1)
            self.assertIn("Reflexão única", channel.sent[0])


if __name__ == "__main__":
    unittest.main()
