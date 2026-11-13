from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.ai.groq_service import GroqService, MediaInput
from src.ai.complexity import assess_generation_mode, is_playful_tease
from src.ai.intent_parser import GeminiIntentParser
from src.ai.intent_router import IntentRouter
from src.ai.knowledge import AIKnowledgeResponder
from src.ai.local_conversation import LocalConversationEngine
from src.ai.moderation_ai import neutralize_mass_mentions, sanitize_output
from src.ai.personality import (
    BELLA_SOCIAL_SYSTEM_PROMPT,
    BELLA_SYSTEM_PROMPT,
)
from src.ai.personality_engine import CommunityPersonalityEngine
from src.ai.prompts import (
    social_message_needs_context,
    social_response_prompt,
)
from src.ai.public_assistant import (
    PublicAssistant,
    _apply_social_style,
    _looks_like_personal_attack,
    _looks_like_service_reply,
    _looks_like_social_explanation,
    _misses_six_seven_tone,
)
from website_guard.knowledge import KnowledgeBase, KnowledgeEntry
from website_guard.state import StateStore


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        groq_enabled=True,
        groq_api_key="gsk_test_key",
        groq_model="test-model",
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
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text),
            )
        ]
    )


class CompletionStub:
    def __init__(
        self,
        responses: list[str | Exception],
        *,
        delay: float = 0,
    ) -> None:
        self.responses = list(responses)
        self.delay = delay
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return _response(value)


def _client(completions: CompletionStub) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=completions,
        )
    )


class StatusError(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = SimpleNamespace(headers={})


class StubService:
    def __init__(self, responses):
        self.responses = list(responses)
        self.available = True
        self.timeout_seconds = 15

    async def generate_json(self, prompt, schema, **kwargs):
        return self.responses.pop(0)

    async def generate_text(self, prompt, **kwargs):
        response = self.responses.pop(0)
        if isinstance(response, str):
            return response
        return str(response.get("answer", ""))


class ExecutiveMemorySetStub:
    available = True

    async def generate_json(self, prompt, schema, **kwargs):
        return {
            "action": "memory_set",
            "target_id": 0,
            "role_ids": [],
            "channel_ids": [],
            "duration_seconds": 0,
            "amount": 0,
            "event_name": "",
            "scheduled_for": "",
            "reason": "",
            "permanent": False,
            "memory_key": "anna",
            "memory_value": "quem é a anna",
            "confidence": 0.99,
        }


class ExecutiveOperationalRuleStub:
    available = True

    async def generate_json(self, prompt, schema, **kwargs):
        return {
            "action": "operational_rule_set",
            "target_id": 0,
            "role_ids": [],
            "channel_ids": [500],
            "duration_seconds": 0,
            "amount": 0,
            "event_name": "",
            "scheduled_for": "",
            "reason": "",
            "permanent": False,
            "name": "",
            "text": (
                "O canal <#500> é para ajuda de programação. "
                "Apague mensagens sem contexto."
            ),
            "topic": "",
            "status_value": "",
            "priority_value": "",
            "memory_key": "",
            "memory_value": "",
            "confidence": 0.99,
        }


class GroqArchitectureTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_counter_resets_and_blocks_at_local_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            await state.set(
                "groq_daily_usage",
                {
                    "day": "2000-01-01",
                    "requests": 900,
                    "limit": 900,
                    "timezone": "America/Sao_Paulo",
                },
            )
            with patch.dict(
                os.environ,
                {
                    "GROQ_DAILY_REQUEST_LIMIT": "1",
                    "GROQ_DAILY_RESET_TIMEZONE": "America/Sao_Paulo",
                },
            ):
                service = GroqService(_settings(), state)
                await service._reserve_daily_request()
                usage = await state.get_section("groq_daily_usage")

                self.assertEqual(usage["requests"], 1)
                self.assertNotEqual(usage["day"], "2000-01-01")
                with self.assertRaisesRegex(
                    RuntimeError,
                    "limite diário local",
                ):
                    await service._reserve_daily_request()

                self.assertTrue(await service.reset_daily_usage(force=True))
                await service._reserve_daily_request()

    def test_seconds_until_midnight_is_valid(self) -> None:
        service = GroqService(
            _settings(),
            StateStore(Path(tempfile.gettempdir()) / "jarvis-test-state.json"),
        )

        seconds = service.seconds_until_daily_reset()

        self.assertGreater(seconds, 0)
        self.assertLessEqual(seconds, 24 * 60 * 60)

    async def test_missing_key_disables_ai_and_records_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            settings.groq_api_key = ""
            service = GroqService(settings, state)

            self.assertFalse(await service.initialize())
            self.assertEqual(await state.get("groq_status"), "missing_key")
            self.assertIn(
                "GROQ_API_KEY",
                str(await state.get("groq_last_error")),
            )

    async def test_service_uses_async_client_and_json_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            completions = CompletionStub(['{"ok": true}'])
            service = GroqService(_settings(), state)
            service._client = _client(completions)

            result = await service.generate_json(
                "Teste",
                {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            )

            self.assertEqual(result, {"ok": True})
            self.assertEqual(
                completions.calls[0]["response_format"],
                {"type": "json_object"},
            )
            self.assertEqual(await state.get("groq_status"), "ready")

    async def test_transient_error_is_retried_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            completions = CompletionStub(
                [
                    StatusError("service unavailable", 503),
                    "resposta recuperada",
                ]
            )
            service = GroqService(_settings(), state)
            service._retry_delay_seconds = 0.01
            service._client = _client(completions)

            result = await service.generate_text("Teste transitório")

            self.assertEqual(result, "resposta recuperada")
            self.assertEqual(len(completions.calls), 2)
            self.assertTrue(service.has_ready_model())

    async def test_groq_text_neutralizes_global_mentions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            completions = CompletionStub(["Olá @everyone e @here"])
            service = GroqService(_settings(), state)
            service._client = _client(completions)

            result = await service.generate_text("Teste")

            self.assertEqual(result, "Olá @\u200beveryone e @\u200bhere")

    async def test_invalid_key_stops_future_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            completions = CompletionStub(
                [StatusError("invalid api key", 401)]
            )
            service = GroqService(_settings(), state)
            service._client = _client(completions)

            with self.assertRaisesRegex(RuntimeError, "GROQ_API_KEY"):
                await service.generate_text("Teste")

            self.assertFalse(service.available)
            self.assertIn(
                "GROQ_API_KEY inválida",
                str(await state.get("groq_last_error")),
            )

    async def test_timeout_is_recorded_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            completions = CompletionStub(["resposta tardia"], delay=0.2)
            service = GroqService(_settings(), state)
            service._client = _client(completions)

            with self.assertRaisesRegex(TimeoutError, "0.05 segundos"):
                await service.generate_text("Teste", timeout_seconds=0.05)

            self.assertEqual(await state.get("groq_status"), "error")

    async def test_media_is_sent_as_data_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            completions = CompletionStub(["imagem analisada"])
            service = GroqService(_settings(), state)
            service._client = _client(completions)

            await service.generate_text(
                "Analise",
                media=[MediaInput(data=b"image", mime_type="image/png")],
            )

            content = completions.calls[0]["messages"][1]["content"]
            self.assertEqual(content[1]["type"], "image_url")
            self.assertTrue(
                content[1]["image_url"]["url"].startswith(
                    "data:image/png;base64,"
                )
            )
            self.assertEqual(
                completions.calls[0]["model"],
                "qwen/qwen3.6-27b",
            )

    async def test_media_url_uses_vision_model_without_base64(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            completions = CompletionStub(["imagem analisada"])
            service = GroqService(_settings(), state)
            service._client = _client(completions)

            await service.generate_text(
                "Leia o erro",
                media=[
                    MediaInput(
                        data=b"",
                        mime_type="image/png",
                        url="https://cdn.discordapp.com/attachments/test.png",
                    )
                ],
            )

            content = completions.calls[0]["messages"][1]["content"]
            self.assertEqual(
                content[1]["image_url"]["url"],
                "https://cdn.discordapp.com/attachments/test.png",
            )
            self.assertEqual(
                completions.calls[0]["model"],
                "qwen/qwen3.6-27b",
            )

    async def test_cache_avoids_duplicate_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            completions = CompletionStub(["resposta em cache"])
            service = GroqService(_settings(), state)
            service._client = _client(completions)

            first = await service.generate_text("Pergunta repetida")
            second = await service.generate_text("Pergunta repetida")

            self.assertEqual(first, second)
            self.assertEqual(len(completions.calls), 1)

    async def test_social_system_prompt_is_sent_to_groq(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            completions = CompletionStub(["Aí sim, agora o dia começou direito."])
            service = GroqService(_settings(), state)
            service._client = _client(completions)

            await service.generate_text(
                "bão demaize",
                system_prompt=BELLA_SOCIAL_SYSTEM_PROMPT,
                use_cache=False,
            )

            self.assertEqual(
                completions.calls[0]["messages"][0]["content"],
                BELLA_SOCIAL_SYSTEM_PROMPT,
            )

    def test_brazilian_slang_uses_social_mode(self) -> None:
        self.assertTrue(assess_generation_mode("bão demaize").social)
        self.assertEqual(
            IntentRouter().classify("bão demaize").name,
            "casual",
        )

    def test_short_everyday_messages_use_social_mode(self) -> None:
        self.assertTrue(assess_generation_mode("tô cansado hoje").social)
        self.assertTrue(
            assess_generation_mode("jarvis tu é muito lerdo kkk").social
        )
        self.assertTrue(assess_generation_mode("que isso doido").social)
        self.assertTrue(assess_generation_mode("jarvis noiado").social)
        self.assertTrue(is_playful_tease("jarvis noiado"))
        self.assertTrue(is_playful_tease("BELLA TA BUGADO"))
        self.assertTrue(assess_generation_mode("oque acha disso?").social)
        self.assertFalse(
            assess_generation_mode("conta uma piada muito sem graca ai").social
        )
        self.assertFalse(
            assess_generation_mode("nao, me conte uma piada").social
        )
        self.assertFalse(
            assess_generation_mode("me explique como funciona uma API").social
        )
        self.assertFalse(
            assess_generation_mode("preciso de suporte com um erro").social
        )

    def test_response_budget_follows_user_intent(self) -> None:
        casual = assess_generation_mode("bom dia")
        simple = assess_generation_mode(
            "Qual dica vc daria pra conseguir uma vaga no exterior?"
        )
        detailed = assess_generation_mode(
            "Me explica como funciona uma API REST"
        )
        extensive = assess_generation_mode(
            "Crie 50 ideias de nomes e explique cada uma"
        )

        self.assertEqual(casual.response_size, "short")
        self.assertLessEqual(casual.max_output_tokens, 100)
        self.assertEqual(simple.response_size, "concise")
        self.assertLess(simple.max_output_tokens, detailed.max_output_tokens)
        self.assertEqual(detailed.response_size, "detailed")
        self.assertEqual(extensive.response_size, "long")

    def test_vacancy_advice_is_not_routed_as_announcement(self) -> None:
        decision = IntentRouter().classify(
            "Qual dica ou conselho vc dá pra quem quer conseguir uma vaga "
            "de desenvolvedor no exterior?"
        )

        self.assertFalse(decision.administrative)
        self.assertEqual(decision.name, "general_question")

    def test_social_style_uses_brazilian_laughter_and_matches_caps(self) -> None:
        regular = _apply_social_style(
            "isso foi bom",
            "Hahahaha, essa foi boa.",
        )
        energetic = _apply_social_style(
            "BELLA ACORDA!!!",
            "hahaha calma, eu estou acordado.",
        )

        self.assertIn("kkkk", regular)
        self.assertNotIn("haha", regular.casefold())
        self.assertEqual(energetic, energetic.upper())
        self.assertIn("KKKKKKKK", energetic)

    async def test_jarvis_identifies_as_brazilian_in_local_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            router = IntentRouter()
            decision = router.classify("Qual é sua nacionalidade?")
            engine = LocalConversationEngine(None, None, None, state)

            reply = await engine.fallback(
                decision,
                "Qual é sua nacionalidade?",
                recent_context=[],
            )

            self.assertEqual(decision.name, "identity")
            self.assertIn("sou brasileira", reply.answer.casefold())
            self.assertIn("seleção brasileira", reply.answer.casefold())

    def test_personality_declares_brazilian_identity(self) -> None:
        self.assertIn(
            "nacionalidade e identidade cultural declarada são brasileiras",
            BELLA_SYSTEM_PROMPT,
        )
        self.assertIn("torce pela Seleção Brasileira", BELLA_SYSTEM_PROMPT)
        self.assertTrue(assess_generation_mode("VAI BRASIL").social)

    def test_social_prompt_forbids_explaining_slang(self) -> None:
        prompt = social_response_prompt(
            message="bão demaize",
            display_name="Talisson",
            channel_history=[],
        )

        self.assertIn("Não explique gíria", prompt)

    def test_social_prompt_treats_light_teasing_as_banter(self) -> None:
        prompt = social_response_prompt(
            message="jarvis noiado",
            display_name="Talisson",
            channel_history=[],
        )

        self.assertIn("provocação ou apelido leve de resenha", prompt)
        self.assertIn("não ofereça ajuda", prompt)
        self.assertIn("não finja confusão", prompt)
        self.assertIn("não esteja no histórico", prompt)
        self.assertIn("uma única frase curta", prompt)
        self.assertIn("não anuncie que está zoando", prompt)

    def test_social_prompt_includes_replied_message_and_recent_context(self) -> None:
        prompt = social_response_prompt(
            message="oque acha disso?",
            display_name="Talisson",
            channel_history=[
                {
                    "author": "RaihNoDev (mensagem respondida)",
                    "text": "O Brasil vai ganhar de 2 a 1 contra a Noruega.",
                }
            ],
            reference_context=[
                "RaihNoDev disse que o Brasil vai ganhar de 2 a 1 contra a Noruega."
            ],
        )

        self.assertIn("Brasil vai ganhar de 2 a 1", prompt)
        self.assertIn("mensagem respondida", prompt.casefold())
        self.assertIn("Histórico:", prompt)
        self.assertIn("ASSUNTO PRINCIPAL", prompt)

    def test_complete_social_question_drops_stale_topic(self) -> None:
        prompt = social_response_prompt(
            message="tu dorme jarvis?",
            display_name="Talisson",
            channel_history=[
                {"author": "RaihNoDev", "text": "Bah, é us guri."},
                {"author": "Bella Nyelle", "text": "O guri é nóis mesmo."},
            ],
        )

        self.assertFalse(social_message_needs_context("tu dorme jarvis?"))
        self.assertTrue(social_message_needs_context("oque acha disso?"))
        self.assertTrue(
            social_message_needs_context(
                "essas ideias de artigos que você mandou aí"
            )
        )
        self.assertNotIn("guri", prompt.casefold())
        self.assertIn("A mensagem é completa e inicia ou muda o assunto", prompt)
        self.assertIn("Histórico: []", prompt)

    async def test_referential_reply_never_asks_for_available_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = StubService(
                [
                    (
                        "kkk parece que voce esta se referindo ao papo anterior, "
                        "mas nao entendi direito. Pode explicar melhor?"
                    ),
                    (
                        "Brasil e Japao nesse clima? Esse jogo vai cobrar adicional "
                        "de insalubridade kkk."
                    ),
                ]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            answer = await assistant._social_ai_recovery(
                "oque acha disso?",
                display_name="Talisson",
                recent_context=[
                    {
                        "author": "RaihNoDev (mensagem respondida)",
                        "text": "Brasil vai enfrentar o Japao.",
                    }
                ],
            )

            self.assertIn("Brasil e Japao", answer)
            self.assertNotIn("explicar melhor", answer.casefold())

    async def test_social_explanation_is_regenerated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = StubService(
                [
                    "Bão demaize é uma gíria que significa bom demais.",
                    "Aí sim, hoje tá bão mesmo kkk.",
                ]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            answer = await assistant._social_ai_recovery(
                "bão demaize",
                display_name="Talisson",
                recent_context=[],
            )

            self.assertEqual(answer, "Aí sim, hoje tá bão mesmo kkk.")
            self.assertFalse(_looks_like_social_explanation(answer))

    async def test_missed_playful_tease_is_regenerated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = StubService(
                [
                    "Tô ligado, o que tá rolando?",
                    "Noiado nada, só processei essa acusação rápido demais kkk.",
                ]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            answer = await assistant._social_ai_recovery(
                "jarvis noiado",
                display_name="Talisson",
                recent_context=[],
            )

            self.assertIn("Noiado nada", answer)
            self.assertIn("kkk", answer)
            self.assertNotIn("o que tá rolando", answer.casefold())

    async def test_meta_banter_is_regenerated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = StubService(
                [
                    "Beleza, zoei de volta! Foi brincadeira.",
                    "Noiado nada, meu processador só tá inspirado kkk.",
                ]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            answer = await assistant._social_ai_recovery(
                "jarvis noiado",
                display_name="Talisson",
                recent_context=[],
            )

            self.assertIn("processador", answer)
            self.assertNotIn("zoei de volta", answer.casefold())

    async def test_personal_attack_is_regenerated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = StubService(
                [
                    "Six seven? Você é burro demais para entender.",
                    "Six seven ativado. Meu processador pediu afastamento.",
                ]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            answer = await assistant._social_ai_recovery(
                "six seven",
                display_name="Talisson",
                recent_context=[],
            )

            self.assertEqual(
                answer,
                "Six seven ativado. Meu processador pediu afastamento.",
            )
            self.assertFalse(_looks_like_personal_attack(answer))

    async def test_corporate_social_reply_is_regenerated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = StubService(
                [
                    "Compreendo sua solicitação. Estou aqui para auxiliá-lo.",
                    "Tá, essa foi boa kkk. Dessa vez eu deixo passar.",
                ]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            answer = await assistant._social_ai_recovery(
                "jarvis tu é muito lerdo kkk",
                display_name="Talisson",
                recent_context=[],
            )

            self.assertEqual(
                answer,
                "Tá, essa foi boa kkk. Dessa vez eu deixo passar.",
            )
            self.assertFalse(_looks_like_service_reply(answer))

    async def test_user_correction_does_not_argue_or_request_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = StubService(
                [
                    (
                        "Cara, não acredito que o Brasil foi desclassificado da copa. "
                        "Qual foi o placar?"
                    ),
                    "Ah, saquei. Então vc tava corrigindo o papo do jogo.",
                ]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            answer = await assistant._social_ai_recovery(
                "Mas é verdade, Noruega ganhou",
                display_name="Talisson",
                recent_context=[
                    {
                        "author": "Bella Nyelle",
                        "text": "Não acredito que o Brasil foi desclassificado da copa!",
                    }
                ],
            )

            lowered = answer.casefold()
            self.assertIn("corrigindo", lowered)
            self.assertNotIn("não acredito", lowered)
            self.assertNotIn("qual foi o placar", lowered)

    async def test_greeting_correction_is_regenerated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = StubService(
                [
                    "Bom dia, Talisson!",
                    "Foi mal, boa noite então.",
                ]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            answer = await assistant._social_ai_recovery(
                "Bom dia não, já é noite",
                display_name="Talisson",
                recent_context=[],
            )

            self.assertEqual(answer, "Foi mal, boa noite então.")

    def test_personality_removes_unrequested_numbering(self) -> None:
        engine = CommunityPersonalityEngine()
        message = "Não moço, tô falando que a Noruega ganhou do Brasil"
        style = engine.analyze(message, humor_allowed=True)

        answer = engine.adapt(
            (
                "Cara, saquei o que você quis dizer.\n"
                "1. Você tá falando que a Noruega ganhou do Brasil."
            ),
            user_message=message,
            style=style,
        )

        self.assertNotRegex(answer, r"(?m)^\s*1[.)]\s+")

    def test_personal_attack_detection_targets_humiliation(self) -> None:
        self.assertTrue(_looks_like_personal_attack("Você é um fracassado."))
        self.assertTrue(_looks_like_personal_attack("Ninguém te aguenta."))
        self.assertFalse(
            _looks_like_personal_attack(
                "Meu processador não estava preparado para essa."
            )
        )

    def test_world_cup_jokes_allow_fictional_scores(self) -> None:
        self.assertIn(
            "placares absurdos em palpites e brincadeiras",
            BELLA_SYSTEM_PROMPT,
        )
        self.assertIn("Six seven", BELLA_SOCIAL_SYSTEM_PROMPT)
        self.assertEqual(IntentRouter().classify("six seven").name, "meme")

    async def test_literal_six_seven_reply_is_regenerated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = StubService(
                [
                    "Meu número da sorte agora é 67, vou jogar na loteria.",
                    "Six seven detectado. O sistema perdeu a compostura.",
                ]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            answer = await assistant._social_ai_recovery(
                "six seven",
                display_name="Talisson",
                recent_context=[],
            )

            self.assertEqual(
                answer,
                "Six seven detectado. O sistema perdeu a compostura.",
            )
            self.assertFalse(_misses_six_seven_tone("six seven", answer))
            self.assertTrue(
                _misses_six_seven_tone(
                    "six seven",
                    "Começou o campeonato de números da sorte.",
                )
            )

    async def test_executive_parser_does_not_turn_question_into_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            parser = GeminiIntentParser(ExecutiveMemorySetStub(), state)

            result = await parser.interpret(
                "Bella quem é a <@123>?",
                bot_user_id=999,
                mentioned_user_ids=[999, 123],
                mentioned_role_ids=[],
                mentioned_channel_ids=[],
                guild_id=1,
            )

            self.assertIsNone(result)

    async def test_executive_parser_requires_commands_for_permanent_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            parser = GeminiIntentParser(ExecutiveMemorySetStub(), state)

            result = await parser.interpret(
                "Bella lembra que a Anna é diretora de design",
                bot_user_id=999,
                mentioned_user_ids=[999],
                mentioned_role_ids=[],
                mentioned_channel_ids=[],
                guild_id=1,
            )

            self.assertIsNone(result)

    async def test_supported_site_answer_is_returned(self) -> None:
        entry = KnowledgeEntry(
            entry_id="about",
            question="Quem é Talisson?",
            answer="Talisson atua com engenharia de software.",
            source_url="https://example.com/sobre",
            source_title="Sobre",
            source_type="page",
        )
        responder = AIKnowledgeResponder(
            StubService(
                [
                    {
                        "is_question": True,
                        "site_related": True,
                        "answer_supported": True,
                        "selected_entry_id": "about",
                        "question_confidence": 0.99,
                        "relation_confidence": 0.96,
                        "answer_confidence": 0.97,
                        "final_confidence": 0.96,
                        "answer": (
                            "Talisson Souza atua com engenharia de software."
                        ),
                    }
                ]
            ),
            min_confidence=0.90,
            match_min=0.78,
        )

        decision = await responder.answer(
            "Quem é Talisson?",
            (entry,),
            question_intent=0.99,
        )

        self.assertTrue(decision.match.should_reply)
        self.assertIn("Talisson Souza", decision.answer)

    async def test_natural_names_resolve_only_to_real_catalog_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parser = GeminiIntentParser(
                StubService(
                    [
                        {
                            "action": "role_add",
                            "target_id": 200,
                            "role_ids": [300, 999],
                            "channel_ids": [],
                            "duration_seconds": 0,
                            "memory_key": "",
                            "memory_value": "",
                            "confidence": 0.98,
                        }
                    ]
                ),
                StateStore(Path(directory) / "state.json"),
            )

            request = await parser.interpret(
                "Bella, adiciona o cargo Moderador ao João",
                bot_user_id=100,
                mentioned_user_ids=[100],
                mentioned_role_ids=[],
                mentioned_channel_ids=[],
                guild_id=10,
                member_catalog=[{"id": 200, "name": "João"}],
                role_catalog=[{"id": 300, "name": "Moderador"}],
                channel_catalog=[],
            )

            self.assertEqual(request.target_id, 200)
            self.assertEqual(request.role_ids, (300,))

    async def test_unban_keeps_explicit_id_outside_member_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            user_id = 1362513159193366711
            parser = GeminiIntentParser(
                StubService(
                    [
                        {
                            "action": "unban",
                            "target_id": user_id,
                            "role_ids": [],
                            "channel_ids": [],
                            "duration_seconds": 0,
                            "amount": 0,
                            "event_name": "",
                            "scheduled_for": "",
                            "reason": "",
                            "permanent": False,
                            "name": "",
                            "text": "",
                            "topic": "",
                            "status_value": "",
                            "priority_value": "",
                            "memory_key": "",
                            "memory_value": "",
                            "confidence": 0.99,
                        }
                    ]
                ),
                StateStore(Path(directory) / "state.json"),
            )

            request = await parser.interpret(
                f"Bella, tire o banimento do usuário ID {user_id}",
                bot_user_id=100,
                mentioned_user_ids=[100],
                mentioned_role_ids=[],
                mentioned_channel_ids=[],
                guild_id=10,
                member_catalog=[],
                role_catalog=[],
                channel_catalog=[],
            )

            self.assertIsNotNone(request)
            self.assertEqual(request.action, "unban")
            self.assertEqual(request.target_id, user_id)

    async def test_ticket_rename_order_keeps_requested_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parser = GeminiIntentParser(
                StubService(
                    [
                        {
                            "action": "smart_ticket_rename",
                            "target_id": 0,
                            "role_ids": [],
                            "channel_ids": [],
                            "duration_seconds": 0,
                            "amount": 0,
                            "event_name": "",
                            "scheduled_for": "",
                            "reason": "",
                            "permanent": False,
                            "name": "Pendente Orçamento",
                            "text": "",
                            "topic": "",
                            "status_value": "",
                            "priority_value": "",
                            "memory_key": "",
                            "memory_value": "",
                            "confidence": 0.99,
                        }
                    ]
                ),
                StateStore(Path(directory) / "state.json"),
            )

            request = await parser.interpret(
                "Bella, mude o nome desse ticket pra Pendente Orçamento",
                bot_user_id=100,
                mentioned_user_ids=[100],
                mentioned_role_ids=[],
                mentioned_channel_ids=[],
                guild_id=10,
                channel_catalog=[],
                current_channel_id=500,
                current_channel_name="ticket-joao-1234",
                current_channel_type="Thread",
            )

            self.assertIsNotNone(request)
            self.assertEqual(request.action, "smart_ticket_rename")
            self.assertEqual(request.name, "Pendente Orçamento")

    async def test_current_channel_is_allowed_for_semantic_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parser = GeminiIntentParser(
                StubService(
                    [
                        {
                            "action": "channel_rename",
                            "target_id": 0,
                            "role_ids": [],
                            "channel_ids": [500],
                            "duration_seconds": 0,
                            "amount": 0,
                            "event_name": "",
                            "scheduled_for": "",
                            "reason": "",
                            "permanent": False,
                            "name": "novidades",
                            "text": "",
                            "topic": "",
                            "status_value": "",
                            "priority_value": "",
                            "memory_key": "",
                            "memory_value": "",
                            "confidence": 0.98,
                        }
                    ]
                ),
                StateStore(Path(directory) / "state.json"),
            )

            request = await parser.interpret(
                "Você consegue transformar este canal em novidades?",
                bot_user_id=100,
                mentioned_user_ids=[100],
                mentioned_role_ids=[],
                mentioned_channel_ids=[],
                guild_id=10,
                channel_catalog=[],
                current_channel_id=500,
                current_channel_name="geral",
                current_channel_type="TextChannel",
            )

            self.assertIsNotNone(request)
            self.assertEqual(request.channel_ids, (500,))
            self.assertEqual(request.name, "novidades")

    async def test_channel_policy_requires_dedicated_rule_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parser = GeminiIntentParser(
                ExecutiveOperationalRuleStub(),
                StateStore(Path(directory) / "state.json"),
            )

            request = await parser.interpret(
                "Bella, o canal <#500> é para ajuda de programação; "
                "ajude dúvidas completas e apague mensagens sem contexto.",
                bot_user_id=100,
                mentioned_user_ids=[100],
                mentioned_role_ids=[],
                mentioned_channel_ids=[500],
                guild_id=10,
                channel_catalog=[{"id": 500, "name": "ajuda-codigo"}],
                current_channel_id=200,
                current_channel_name="bate-papo",
                current_channel_type="TextChannel",
            )

            self.assertIsNone(request)

    async def test_statistics_schedule_extracts_channel_and_recurrence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parser = GeminiIntentParser(
                StubService(
                    [
                        {
                            "action": "statistics_schedule_set",
                            "target_id": 0,
                            "role_ids": [],
                            "channel_ids": [500],
                            "duration_seconds": 0,
                            "amount": 0,
                            "event_name": "",
                            "scheduled_for": "",
                            "reason": "",
                            "permanent": False,
                            "name": "",
                            "text": "",
                            "topic": "",
                            "status_value": "",
                            "priority_value": "",
                            "statistics_type": "site",
                            "frequency": "daily",
                            "hour": 8,
                            "minute": 30,
                            "weekday": 0,
                            "month_day": 1,
                            "period_days": 30,
                            "memory_key": "",
                            "memory_value": "",
                            "confidence": 0.99,
                        }
                    ]
                ),
                StateStore(Path(directory) / "state.json"),
            )

            request = await parser.interpret(
                "Bella, envie as estatísticas do site todos os dias às 08:30 "
                "no canal <#500>, considerando os últimos 30 dias.",
                bot_user_id=100,
                mentioned_user_ids=[100],
                mentioned_role_ids=[],
                mentioned_channel_ids=[500],
                guild_id=10,
                channel_catalog=[{"id": 500, "name": "metricas-site"}],
                current_channel_id=200,
                current_channel_name="bate-papo",
                current_channel_type="TextChannel",
            )

            self.assertIsNotNone(request)
            self.assertEqual(request.action, "statistics_schedule_set")
            self.assertEqual(request.channel_ids, (500,))
            self.assertEqual(request.statistics_type, "site")
            self.assertEqual(request.frequency, "daily")
            self.assertEqual(request.hour, 8)
            self.assertEqual(request.minute, 30)
            self.assertEqual(request.period_days, 30)

    async def test_public_meme_reply_keeps_conversation_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            knowledge = KnowledgeBase(settings, state)
            assistant = PublicAssistant(
                StubService(["F. Respeitos prestados."]),
                settings,
                state,
                knowledge,
            )

            reply = await assistant.reply(
                "F no chat",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=3,
            )
            history = await assistant.memory.history(1, 2, 3)

            self.assertTrue(reply.should_reply)
            self.assertEqual(reply.intent, "meme")
            self.assertEqual(len(history), 2)

    def test_forbidden_identity_phrases_and_emojis_are_blocked(self) -> None:
        self.assertEqual(
            sanitize_output("Como inteligência artificial, posso ajudar."),
            "",
        )
        self.assertEqual(
            sanitize_output("Nos memes, o sigma costuma vencer."),
            "Nos memes, o sigma costuma vencer.",
        )
        self.assertEqual(
            sanitize_output("Tudo certo 😂 <:jarvis:123456789>"),
            "Tudo certo",
        )
        self.assertEqual(
            sanitize_output("Olá @everyone e @here"),
            "Olá @\u200beveryone e @\u200bhere",
        )
        self.assertEqual(
            neutralize_mass_mentions("@Everyone @HERE"),
            "@\u200bEveryone @\u200bHERE",
        )


if __name__ == "__main__":
    unittest.main()
