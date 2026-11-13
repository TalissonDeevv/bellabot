import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from src.ai.bella_ai_handler import BellaAIHandler
from src.ai.bella_context_manager import select_bella_context
from src.ai.bella_generation_rules import social_generation_rules
from src.ai.bella_gemini_client import _retry_after_seconds
from src.ai.bella_prompt_builder import (
    build_bella_public_voice_context,
    build_bella_social_prompt,
)
from src.ai.bella_response_validator import validate_bella_response
from src.ai.groq_service import DEFAULT_MODEL, TEXT_MODEL_FALLBACKS
from src.ai.intent_router import normalize_text
from src.ai.local_conversation import _social_fallback


class BellaPersonaPipelineTests(unittest.TestCase):
    def test_social_prompt_uses_persona_examples_and_antipatterns(self) -> None:
        prompt = build_bella_social_prompt(
            message="oi bella",
            display_name="tali",
            channel_history=[],
            style_instruction="",
        )

        self.assertIn("EXEMPLOS POSITIVOS", prompt)
        self.assertIn("ANTI-EXEMPLOS", prompt)
        self.assertIn("copia exemplos", prompt.casefold())
        self.assertIn("emoji", prompt.casefold())

    def test_short_greeting_does_not_pull_old_channel_topic(self) -> None:
        history = [
            {
                "author": "Bella Nyelle",
                "content": "Oi! Tudo ótimo por aqui, curtindo a vibe no Discord. E você, como tá?",
            },
            {"author": "tali", "content": "namora comigo?"},
        ]

        self.assertEqual(select_bella_context("bom dia bella", history), [])

    def test_validator_rejects_generic_assistant_voice(self) -> None:
        result = validate_bella_response(
            "oi bella",
            "Olá! Como posso ajudá-lo hoje?",
            recent_responses=[],
        )

        self.assertFalse(result.acceptable)

    def test_validator_rejects_internal_format_leak(self) -> None:
        result = validate_bella_response(
            "vc é tímida?",
            "Oi! Entendi direitinho, vou responder aqui em JSON como a Bella costuma fazer no Discord.",
            recent_responses=[],
        )

        self.assertFalse(result.acceptable)
        self.assertIn("prompt_leak", result.reasons)

    def test_validator_accepts_natural_bella_voice(self) -> None:
        result = validate_bella_response(
            "tudo bem?",
            "tô sim... meio quietinha, mas tô kkkkk. e vc?",
            recent_responses=[],
        )

        self.assertTrue(result.acceptable)

    def test_validator_rejects_polished_social_voice(self) -> None:
        result = validate_bella_response(
            "vc é tímida?",
            "Sou meio tímida, mas adoro conversar quando me sinto à vontade.",
            recent_responses=[],
        )

        self.assertFalse(result.acceptable)

    def test_validator_rejects_repeated_chatbot_crutch(self) -> None:
        result = validate_bella_response(
            "tudo bem mulher?",
            "Oi! Tudo tranquilo por aqui, gosto de trocar ideia aqui. E você, como tá?",
            recent_responses=[],
        )

        self.assertFalse(result.acceptable)

    def test_validator_rejects_tou_aqui_service_loop(self) -> None:
        examples = (
            "Oi Tali! Tô aqui, o que manda?",
            "Oi! Tô por aqui, manda aí o que precisar",
        )

        for answer in examples:
            with self.subTest(answer=answer):
                result = validate_bella_response(
                    "tudo bem contigo?",
                    answer,
                    recent_responses=[],
                )

                self.assertFalse(result.acceptable)

    def test_validator_rejects_repeated_opening(self) -> None:
        result = validate_bella_response(
            "tudo bem com vc?",
            "Oi Tali! tô sim, meio quietinha hoje",
            recent_responses=["Oi Tali! tô aqui, o que manda?"],
        )

        self.assertFalse(result.acceptable)
        self.assertIn("repeated_opening", result.reasons)

    def test_gemini_provider_does_not_fallback_to_groq_when_disabled(self) -> None:
        class GeminiUnavailable:
            available = False

        class GroqAvailable:
            available = True

        with patch.dict(
            "os.environ",
            {
                "BELLA_AI_PROVIDER": "gemini",
                "BELLA_FALLBACK_TO_GROQ": "false",
            },
        ):
            handler = BellaAIHandler(GroqAvailable(), object())  # type: ignore[arg-type]
            handler.gemini = GeminiUnavailable()  # type: ignore[assignment]

            self.assertFalse(handler.available)

    def test_gemini_provider_uses_groq_fallback_by_default(self) -> None:
        class GeminiUnavailable:
            available = False

        class GroqAvailable:
            available = True

        with patch.dict(
            "os.environ",
            {
                "BELLA_AI_PROVIDER": "gemini",
            },
            clear=True,
        ):
            handler = BellaAIHandler(GroqAvailable(), object())  # type: ignore[arg-type]
            handler.gemini = GeminiUnavailable()  # type: ignore[assignment]

            self.assertTrue(handler.available)

    def test_gemini_exception_falls_back_to_groq(self) -> None:
        class GeminiRaises:
            available = True

            async def generate_text(self, *args, **kwargs):
                raise TimeoutError("Gemini timeout")

        class GroqAvailable:
            available = True

        state = AsyncMock()
        with patch.dict(
            "os.environ",
            {
                "BELLA_AI_PROVIDER": "gemini",
                "BELLA_FALLBACK_TO_GROQ": "true",
            },
        ):
            handler = BellaAIHandler(GroqAvailable(), state)  # type: ignore[arg-type]
            handler.gemini = GeminiRaises()  # type: ignore[assignment]
            with patch(
                "src.ai.bella_ai_handler.generate_bella_text",
                new=AsyncMock(return_value="consigo sim, fala aí"),
            ):
                answer = asyncio.run(
                    handler._generate_bella_raw(
                        "oi bella",
                        max_output_tokens=120,
                        timeout_seconds=1.0,
                        groq_attempts=1,
                    )
                )

        self.assertEqual(answer, "consigo sim, fala aí")

    def test_retry_after_seconds_reads_gemini_retry_delay(self) -> None:
        self.assertEqual(
            _retry_after_seconds("please retry in 51s after quota exceeded"),
            51,
        )

    def test_local_degraded_fallback_keeps_simple_mentions_alive(self) -> None:
        for message in ("oi bella", "tudo bem contigo?", "vc é tímida?"):
            with self.subTest(message=message):
                answer = _social_fallback(message, normalize_text(message))

                self.assertTrue(answer)
                self.assertNotIn("como posso ajudar", normalize_text(answer or ""))

    def test_social_generation_is_fast_by_default(self) -> None:
        rules = social_generation_rules("oi bella")

        self.assertEqual(rules.groq_attempts, 1)
        self.assertLessEqual(rules.timeout_seconds, 3.0)

    def test_current_default_groq_model_is_not_legacy(self) -> None:
        self.assertEqual(DEFAULT_MODEL, "openai/gpt-oss-20b")
        self.assertEqual(TEXT_MODEL_FALLBACKS[0], "openai/gpt-oss-20b")

    def test_public_voice_context_is_available_for_non_social_paths(self) -> None:
        context = build_bella_public_voice_context("bella vc sabe programar?", [])

        self.assertIn("EXEMPLOS POSITIVOS", context)
        self.assertIn("ANTI-EXEMPLOS", context)


if __name__ == "__main__":
    unittest.main()
