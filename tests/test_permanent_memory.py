from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.ai.public_assistant import PublicAssistant
from website_guard.knowledge import KnowledgeBase
from website_guard.state import StateStore


OWNER_ID = 933780111742877777


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        ai_context_messages=24,
        ai_context_ttl_minutes=15,
        ai_max_response_chars=1800,
        ai_public_min_confidence=0.72,
        gemini_timeout_seconds=75,
        knowledge_max_message_length=500,
        question_intent_min=0.90,
        knowledge_match_min=0.78,
        knowledge_min_confidence=0.90,
        talisson_user_id=OWNER_ID,
    )


class RecordingService:
    available = True

    def __init__(self, answer: str = "Resposta natural usando a memória.") -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def generate_json(self, prompt, schema, **kwargs):
        self.prompts.append(prompt)
        return {
            "intent": "specialized",
            "should_reply": True,
            "site_related": True,
            "specialized_context_used": True,
            "answer_supported": True,
            "confidence": 0.98,
            "answer": self.answer,
        }

    async def generate_text(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.answer


class OfflineService:
    available = False


class FailingService:
    available = True

    async def generate_json(self, *args, **kwargs):
        raise AssertionError("Gemini should not be required for direct memory hits")


class PermanentMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_command_can_teach_universal_knowledge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            assistant = PublicAssistant(
                RecordingService(),
                _settings(),
                state,
                KnowledgeBase(_settings(), state),
            )

            result = await assistant.permanent.define_manual_knowledge(
                "esse link é para pessoas que desejam solicitar orçamento: https://forms.gle/ULdkFdTLWujBAHfv7",
                user_id=OWNER_ID,
            )
            memories = await state.get_section("jarvis_universal_knowledge")

            self.assertEqual(result["type"], "knowledge")
            self.assertTrue(memories)
            self.assertIn("Orçamento", str(memories))

    async def test_server_summary_is_saved_and_retrieved_as_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            assistant = PublicAssistant(
                OfflineService(),
                _settings(),
                state,
                KnowledgeBase(_settings(), state),
            )

            await assistant.permanent.save_server_summary(
                guild_id=1467976297689911377,
                guild_name="Escritório do Talisson",
                summary="Comunidade sobre tecnologia, projetos e desenvolvimento.",
                channel_count=23,
                message_count=100,
            )

            reply = await assistant.reply(
                "o que tem nesse servidor?",
                display_name="João",
                guild_id=1467976297689911377,
                channel_id=2,
                user_id=123,
            )

            self.assertEqual(reply.source, "permanent_memory_direct")
            self.assertIn("tecnologia", reply.answer)

    async def test_taught_knowledge_is_sent_to_gemini_on_user_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = RecordingService("Use o formulário oficial de orçamento.")
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )
            await assistant.permanent.define_manual_knowledge(
                "esse link é para pessoas que desejam solicitar orçamento: https://forms.gle/ULdkFdTLWujBAHfv7",
                user_id=OWNER_ID,
            )

            await assistant.reply(
                "Quero um orçamento",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=123,
            )

            self.assertIn("https://forms.gle/ULdkFdTLWujBAHfv7", service.prompts[-1])
            self.assertIn("memória especializada", service.prompts[-1])

    async def test_organization_memory_is_updated_by_manual_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = RecordingService("Anna cuida de UI/UX e Social Media.")
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )
            await assistant.permanent.define_manual_knowledge(
                "a Anna é Diretora de UI/UX Design e Social Media da Cheetah Solutions Network",
                user_id=OWNER_ID,
            )

            await assistant.reply(
                "Quem é a Anna?",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=123,
            )

            self.assertIn("Anna", service.prompts[-1])
            self.assertIn("UI/UX", service.prompts[-1])

    async def test_person_memory_answers_directly_by_name_and_mention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            assistant = PublicAssistant(
                OfflineService(),
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            await assistant.permanent.define_manual_knowledge(
                "a Anna é Diretora de UI/UX Design e Social Media da Cheetah Solutions Network",
                user_id=OWNER_ID,
            )
            by_name = await assistant.reply(
                "Anna?",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=123,
            )

            self.assertEqual(by_name.source, "permanent_memory_direct")
            self.assertIn("Anna", by_name.answer)
            self.assertIn("UI/UX", by_name.answer)

            await assistant.permanent.define_manual_knowledge(
                "<@555> é Desenvolvedor Backend da Cheetah Solutions Network",
                user_id=OWNER_ID,
            )
            by_mention = await assistant.reply(
                "quem é <@555>?",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=123,
            )

            self.assertEqual(by_mention.source, "permanent_memory_direct")
            self.assertIn("Desenvolvedor Backend", by_mention.answer)

    async def test_channel_rule_is_saved_and_owner_question_is_not_relearned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            assistant = PublicAssistant(
                OfflineService(),
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            saved = await assistant.permanent.define_manual_knowledge(
                "No canal <#1514971351427977316> é somente pra dúvidas em geral, "
                "se alguma pessoa enviar alguma mensagem vaga vc deve apagar",
                user_id=OWNER_ID,
            )

            self.assertEqual(saved["type"], "channel_rule")
            self.assertEqual(saved["title"], "Canal <#1514971351427977316>")

            reply = await assistant.reply(
                "sabe qual é a função do canal de <#1514971351427977316> ne?",
                display_name="Talisson",
                guild_id=1,
                channel_id=2,
                user_id=OWNER_ID,
            )

            memories = await state.get_section("jarvis_organizational_memory")
            self.assertFalse(memories)
            self.assertEqual(reply.source, "permanent_memory_direct")
            self.assertIn("<#1514971351427977316>", reply.answer)
            self.assertIn("apagar", reply.answer.casefold())

    async def test_gemini_failure_does_not_dump_raw_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            assistant = PublicAssistant(
                FailingService(),
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            await assistant.permanent.define_manual_knowledge(
                "a Anna é Diretora de UI/UX Design e Social Media da Cheetah Solutions Network",
                user_id=OWNER_ID,
            )
            reply = await assistant.reply(
                "Quem é a Anna?",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=123,
            )

            self.assertFalse(reply.should_reply)
            self.assertEqual(reply.source, "silent_fallback")
            self.assertEqual(reply.answer, "")

    async def test_cheetah_context_has_institutional_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = RecordingService("A Cheetah estrutura operações com software.")
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            await assistant.reply(
                "O que é a Cheetah OPS?",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=123,
            )

            self.assertIn("Cheetah OPS Framework", service.prompts[-1])
            self.assertIn("Diagnóstico Operacional", service.prompts[-1])

    async def test_social_fallback_waits_for_gemini(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            assistant = PublicAssistant(
                OfflineService(),
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            reply = await assistant.reply(
                "BELLA ACORDA",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=123,
            )

            # Sem IA, a Bella responde com uma fala local na voz dela em vez
            # de avisar que a IA caiu.
            self.assertIn(reply.intent, {"casual", "meme"})
            if reply.should_reply:
                self.assertEqual(reply.source, "fallback")
                self.assertNotIn("indispon", reply.answer.casefold())
            else:
                self.assertEqual(reply.source, "silent_fallback")
