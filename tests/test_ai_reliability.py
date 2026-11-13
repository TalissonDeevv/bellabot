from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.ai.groq_service import GroqService
from src.ai.public_assistant import PublicAssistant
from website_guard.knowledge import KnowledgeBase
from website_guard.state import StateStore


def _settings(*, model: str = "llama-3.1-8b-instant") -> SimpleNamespace:
    return SimpleNamespace(
        groq_enabled=True,
        groq_api_key="gsk_test_key",
        groq_model=model,
        groq_timeout_seconds=5,
        ai_min_confidence=0.90,
        knowledge_match_min=0.78,
        knowledge_max_message_length=500,
        question_intent_min=0.90,
        talisson_user_id=933780111742877777,
        ai_context_messages=8,
        ai_context_ttl_minutes=60,
        ai_max_response_chars=1800,
        ai_public_min_confidence=0.72,
    )


def _response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


class _Completions:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _response(self.responses.pop(0))


class _SocialService:
    available = True
    timeout_seconds = 5

    async def generate_text(self, *args, **kwargs) -> str:
        return ""


class AIReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_supported_model_is_not_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = GroqService(
                _settings(model="llama-3.1-8b-instant"),
                StateStore(Path(directory) / "state.json"),
            )

            self.assertEqual(service.model, "llama-3.1-8b-instant")

    async def test_empty_groq_response_retries_with_fallback_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completions = _Completions(["", "resposta recuperada"])
            service = GroqService(
                _settings(model="llama-3.1-8b-instant"),
                StateStore(Path(directory) / "state.json"),
            )
            service._client = SimpleNamespace(
                chat=SimpleNamespace(completions=completions)
            )

            answer = await service.generate_text(
                "teste",
                use_cache=False,
                max_attempts=2,
            )

            self.assertEqual(answer, "resposta recuperada")
            self.assertEqual(len(completions.calls), 2)
            self.assertNotEqual(
                completions.calls[0]["model"],
                completions.calls[1]["model"],
            )

    async def test_deescalation_never_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"BELLA_AI_PROVIDER": "groq", "GEMINI_ENABLED": "false"},
        ):
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            assistant = PublicAssistant(
                _SocialService(),
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            answer = await assistant._social_ai_recovery(
                "vai tomar no teu",
                display_name="Pessoa",
                recent_context=[],
                deescalate=True,
            )

            self.assertIsInstance(answer, str)
            self.assertTrue(answer.strip())


if __name__ == "__main__":
    unittest.main()
