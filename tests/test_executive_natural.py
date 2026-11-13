from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.ai.intent_parser import (
    EXECUTIVE_PROMPT_CHAR_LIMIT,
    GeminiIntentParser,
    _relevant_entries,
)
from website_guard.state import StateStore

TARGET = 555000000000000001
USERS = [
    {"id": 400000000000000000 + i, "name": f"Membro {i}", "username": f"membro{i}"}
    for i in range(249)
] + [{"id": TARGET, "name": "Fulano", "username": "fulano"}]
ROLES = [{"id": 300000000000000000 + i, "name": f"Cargo {i}"} for i in range(40)]
CHANNELS = [
    {"id": 200000000000000000 + i, "name": name, "type": "TextChannel"}
    for i, name in enumerate(
        ["geral", "📣・anuncios", "chat"] + [f"canal-{i}" for i in range(57)]
    )
]


class RecordingService:
    available = True

    def __init__(self, answer: dict) -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict]] = []

    async def generate_json(self, prompt, schema, **kwargs):
        self.calls.append((prompt, kwargs))
        return self.answer


class ExecutiveNaturalLanguageTests(unittest.IsolatedAsyncioTestCase):
    async def _interpret(self, content: str, mentioned: list[int], answer: dict):
        with tempfile.TemporaryDirectory() as directory:
            service = RecordingService(answer)
            parser = GeminiIntentParser(service, StateStore(Path(directory) / "s.json"))
            request = await parser.interpret(
                content,
                bot_user_id=999,
                mentioned_user_ids=mentioned,
                mentioned_role_ids=[],
                mentioned_channel_ids=[],
                guild_id=1,
                member_catalog=USERS,
                role_catalog=ROLES,
                channel_catalog=CHANNELS,
                current_channel_id=CHANNELS[0]["id"],
                current_channel_name="geral",
                current_channel_type="TextChannel",
            )
            return request, service.calls

    async def test_order_survives_large_server_catalog(self) -> None:
        content = "Bella solta um aviso lá nos anuncios que amanhã não vai ter live"
        request, calls = await self._interpret(
            content,
            [],
            {
                "action": "message_send",
                "channel_ids": [CHANNELS[1]["id"]],
                "text": "Amanhã não vai ter live.",
                "confidence": 0.95,
            },
        )
        prompt, kwargs = calls[0]
        self.assertTrue(prompt.startswith("ORDEM DO PROPRIETÁRIO: " + content))
        self.assertLess(len(prompt), EXECUTIVE_PROMPT_CHAR_LIMIT)
        self.assertEqual(kwargs["prompt_char_limit"], EXECUTIVE_PROMPT_CHAR_LIMIT)
        self.assertIn("anuncios", prompt)
        self.assertNotIn("Membro 200", prompt)
        self.assertEqual(request.action, "message_send")
        self.assertEqual(request.channel_ids, (CHANNELS[1]["id"],))

    async def test_question_shaped_order_with_mention_reaches_ai(self) -> None:
        request, calls = await self._interpret(
            f"Bella sabe esse <@{TARGET}>? some com ele daqui de vez",
            [TARGET],
            {
                "action": "ban",
                "target_id": TARGET,
                "reason": "flood",
                "permanent": True,
                "confidence": 0.9,
            },
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(request.action, "ban")
        self.assertEqual(request.target_id, TARGET)

    async def test_plain_question_without_target_skips_ai(self) -> None:
        request, calls = await self._interpret(
            "Bella quem criou você?",
            [],
            {"action": "none", "confidence": 0},
        )
        self.assertIsNone(request)
        self.assertEqual(calls, [])

    def test_relevant_entries_matches_partial_channel_names(self) -> None:
        chosen = _relevant_entries(
            CHANNELS,
            "manda no canal de #anuncio",
            always=set(),
            limit=25,
            fallback=15,
        )
        self.assertEqual([entry["name"] for entry in chosen], ["📣・anuncios"])


if __name__ == "__main__":
    unittest.main()
