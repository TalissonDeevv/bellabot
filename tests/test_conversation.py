from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.ai.channel_context import ChannelContextBuffer
from src.ai.gemini_service import MediaInput
from src.ai.intent_router import IntentRouter
from src.ai.memory import ConversationMemory
from src.ai.public_assistant import PublicAssistant
from src.ai.server_knowledge import DiscordKnowledgeIndex, ServerKnowledgeEntry
from website_guard.knowledge import KnowledgeBase, KnowledgeEntry
from website_guard.state import StateStore


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
        talisson_user_id=933780111742877777,
    )


class RecordingService:
    available = True
    timeout_seconds = 15

    def __init__(self, answers: list[str] | None = None) -> None:
        self.answers = list(answers or ["tô bem, meio quietinha hj kkkk"])
        self.prompts: list[str] = []
        self.kwargs: list[dict[str, object]] = []

    async def generate_json(self, prompt, schema, **kwargs):
        self.prompts.append(prompt)
        self.kwargs.append(kwargs)
        return {
            "intent": "conversation",
            "should_reply": True,
            "site_related": False,
            "specialized_context_used": False,
            "answer_supported": True,
            "confidence": 0.98,
            "answer": self.answers.pop(0),
        }

    async def generate_text(self, prompt, **kwargs):
        self.prompts.append(prompt)
        self.kwargs.append(kwargs)
        return self.answers.pop(0)


class FailingGeminiService:
    available = True

    async def generate_json(self, *args, **kwargs):
        raise RuntimeError("Gemini quota exhausted")

    async def generate_text(self, *args, **kwargs):
        raise RuntimeError("Gemini quota exhausted")


class SpecializedKnowledge:
    def __init__(self, entry: KnowledgeEntry) -> None:
        self.entry = entry

    async def candidates(self, *args, **kwargs):
        return 1.0, (self.entry,)


class CountingKnowledge(SpecializedKnowledge):
    def __init__(self, entry: KnowledgeEntry) -> None:
        super().__init__(entry)
        self.calls = 0

    async def candidates(self, *args, **kwargs):
        self.calls += 1
        return await super().candidates(*args, **kwargs)


class ConversationArchitectureTests(unittest.IsolatedAsyncioTestCase):
    def test_router_classifies_local_and_administrative_intents(self) -> None:
        router = IntentRouter()

        self.assertEqual(router.classify("Tudo bem?").name, "casual")
        self.assertEqual(router.classify("O pai tá on?").name, "meme")
        self.assertTrue(
            router.classify("apague as últimas 100 mensagens").administrative
        )
        self.assertTrue(
            router.classify("apague 3 mensagens do chat").administrative
        )
        self.assertTrue(
            router.classify("tranque este canal").administrative
        )
        self.assertTrue(
            router.classify("destranque este canal").administrative
        )
        self.assertTrue(
            router.classify("onde vejo o changelog?").server_related
        )

    async def test_general_conversation_uses_groq(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            knowledge = KnowledgeBase(settings, state)
            service = RecordingService()
            assistant = PublicAssistant(
                service,
                settings,
                state,
                knowledge,
            )

            reply = await assistant.reply(
                "Tudo bem?",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=3,
            )

            self.assertTrue(reply.should_reply)
            self.assertEqual(reply.source, "bella_social")
            self.assertEqual(reply.answer, "tô bem, meio quietinha hj kkkk")
            self.assertEqual(len(service.prompts), 1)
            self.assertGreater(service.kwargs[0]["timeout_seconds"], 0)
            self.assertIn("Mensagem atual: Tudo bem?", service.prompts[0])

    async def test_owner_casual_correction_is_not_saved_as_knowledge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = RecordingService(
                ["Ah, saquei. Então vc tava corrigindo o papo do jogo."]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            reply = await assistant.reply(
                "Mas é verdade, Noruega ganhou",
                display_name="Talisson",
                guild_id=1,
                channel_id=2,
                user_id=settings.talisson_user_id,
                recent_context=[
                    {
                        "author": "Bella Nyelle",
                        "text": "Não acredito que o Brasil foi desclassificado da copa!",
                    }
                ],
            )

            self.assertTrue(reply.should_reply)
            self.assertEqual(reply.source, "bella_social")
            self.assertNotIn("vou lembrar", reply.answer.casefold())
            self.assertEqual(await state.get_section("jarvis_universal_knowledge"), {})
            self.assertEqual(await state.get_section("jarvis_organizational_memory"), {})

    async def test_public_reply_is_rewritten_out_of_ai_voice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = RecordingService(
                [
                    (
                        "Excelente pergunta. Conforme informado, segue abaixo "
                        "a solução. Fico à disposição."
                    )
                ]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            reply = await assistant.reply(
                "Como resolvo um erro no deploy?",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=3,
            )
            lowered = reply.answer.casefold()

            self.assertNotIn("excelente pergunta", lowered)
            self.assertNotIn("conforme informado", lowered)
            self.assertNotIn("segue abaixo", lowered)
            self.assertNotIn("fico à disposição", lowered)

    async def test_blocked_social_channel_uses_local_neutral_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = RecordingService()
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            reply = await assistant.reply(
                "IAE BELLA MEU BRODI",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=3,
                humor_allowed=False,
            )

            self.assertTrue(reply.should_reply)
            self.assertEqual(reply.source, "local_channel_policy")
            self.assertEqual(service.prompts, [])

    async def test_aggressive_message_never_receives_retaliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = RecordingService(
                [
                    "Cala a boca, seu idiota.",
                    "Vai tomar no teu também.",
                ]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            reply = await assistant.reply(
                "vai tomar no teu UC",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=3,
                humor_allowed=True,
            )

            self.assertTrue(reply.should_reply)
            self.assertNotIn("idiota", reply.answer.casefold())
            self.assertNotIn("vai tomar", reply.answer.casefold())

    async def test_social_follow_up_uses_recent_and_replied_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = RecordingService(
                ["Dois a um contra a Noruega? O brasileiro ja acordou confiante kkk."]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            reply = await assistant.reply(
                "oque acha disso?",
                display_name="Talisson",
                guild_id=1,
                channel_id=2,
                user_id=3,
                recent_context=[
                    {
                        "author": "RaihNoDev (mensagem respondida)",
                        "text": "O Brasil vai ganhar de 2 a 1 contra a Noruega.",
                    }
                ],
                attachment_context=[
                    "Mensagem respondida: Brasil 2 a 1 contra a Noruega."
                ],
            )

            self.assertTrue(reply.should_reply)
            self.assertEqual(reply.source, "bella_social")
            self.assertIn("Brasil vai ganhar de 2 a 1", service.prompts[0])
            self.assertIn("Mensagem respondida", service.prompts[0])

    async def test_new_social_topic_does_not_leak_previous_joke(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = RecordingService(
                ["Dormir eu não durmo, mas às vezes meu processo pede férias."]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            reply = await assistant.reply(
                "tu dorme jarvis?",
                display_name="Talisson",
                guild_id=1,
                channel_id=2,
                user_id=3,
                recent_context=[
                    {"author": "Talisson", "text": "Bah, é us guri."},
                    {"author": "Bella Nyelle", "text": "O guri é nóis mesmo."},
                ],
            )

            self.assertTrue(reply.should_reply)
            self.assertEqual(reply.source, "bella_social")
            # O assunto antigo não entra no histórico usado para responder;
            # a fala antiga da Bella só aparece na lista anti-repetição.
            history_line = next(
                line for line in service.prompts[0].splitlines()
                if line.startswith("Histórico realmente relevante:")
            )
            self.assertNotIn("guri", history_line.casefold())
            self.assertIn("tu dorme jarvis?", service.prompts[0].casefold())

    async def test_direct_joke_request_uses_content_generation_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = RecordingService(
                [
                    (
                        "Por que o computador foi ao medico? Porque ele estava "
                        "com um virus. Foi ruim como solicitado."
                    )
                ]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            reply = await assistant.reply(
                "conta uma piada muito sem graca ai",
                display_name="Talisson",
                guild_id=1,
                channel_id=2,
                user_id=3,
            )

            self.assertTrue(reply.should_reply)
            self.assertEqual(reply.source, "groq_fast_text")
            self.assertIn("computador", reply.answer)
            self.assertEqual(len(service.prompts), 1)

    async def test_gemini_failure_does_not_send_generic_public_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            assistant = PublicAssistant(
                FailingGeminiService(),
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            reply = await assistant.reply(
                "Qual foi o ponto mesmo?",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=3,
                recent_context=[{"author": "Maria", "text": "Contexto anterior"}],
            )

            self.assertFalse(reply.should_reply)
            self.assertEqual(reply.source, "silent_fallback")
            self.assertNotIn("Estou acompanhando", reply.answer)

    async def test_attachment_is_sent_to_gemini_with_clear_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = RecordingService(["O documento apresenta três pontos."])
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )
            media = [MediaInput(data=b"image", mime_type="image/png")]

            await assistant.reply(
                "Resume isso",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=3,
                media=media,
                attachment_context=["Anexo: resumo.png (image/png)"],
            )

            self.assertEqual(service.kwargs[0]["media"], media)
            self.assertIn("resumo.png", service.prompts[0])
            self.assertIn("Nunca finja", service.prompts[0])

    async def test_social_visual_question_sends_image_and_context_to_groq(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = RecordingService(
                ["Po, o layout tá limpo. Eu só destacaria mais o botão principal."]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )
            media = [
                MediaInput(
                    data=b"",
                    mime_type="image/png",
                    url="https://cdn.discordapp.com/attachments/layout.png",
                )
            ]

            reply = await assistant.reply(
                "oq vc acha desse layout?",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=3,
                media=media,
                attachment_context=["Anexo: layout.png (image/png)"],
            )

            self.assertTrue(reply.should_reply)
            self.assertEqual(reply.source, "bella_social")
            self.assertEqual(service.kwargs[0]["media"], media)
            self.assertIn("Imagem/anexo presente: sim", service.prompts[0])
            self.assertIn("layout.png", service.prompts[0])

    async def test_specialized_knowledge_is_sent_to_gemini(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            service = RecordingService(["Talisson atua com tecnologia."])
            knowledge = SpecializedKnowledge(
                KnowledgeEntry(
                    entry_id="about",
                    question="Quem é Talisson Souza?",
                    answer="Talisson Souza atua com engenharia de software.",
                    source_url="https://example.com/sobre",
                    source_title="Sobre",
                    source_type="page",
                )
            )
            assistant = PublicAssistant(
                service,
                _settings(),
                state,
                knowledge,
            )

            await assistant.reply(
                "Quem é Talisson Souza?",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=3,
            )

            self.assertIn(
                "Talisson Souza atua com engenharia de software.",
                service.prompts[0],
            )
            self.assertIn("Base especializada do site", service.prompts[0])

    async def test_hosting_knowledge_is_prioritized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = RecordingService(
                ["Estou hospedada na Square Cloud."]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            reply = await assistant.reply(
                "Onde você está hospedado?",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=3,
            )

            self.assertEqual(reply.source, "groq_fast_text")
            self.assertIn("Square Cloud", service.prompts[0])
            self.assertIn("fundada em 2021", service.prompts[0])

    async def test_unrelated_meme_does_not_load_site_knowledge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            entry = KnowledgeEntry(
                entry_id="about",
                question="Quem é Talisson?",
                answer="Talisson atua com tecnologia.",
                source_url="https://example.com",
                source_title="Sobre",
                source_type="page",
            )
            knowledge = CountingKnowledge(entry)
            service = RecordingService(["Pro beta não sobra nada."])
            assistant = PublicAssistant(
                service,
                _settings(),
                state,
                knowledge,
            )

            await assistant.reply(
                "O que sobra pro beta?",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=3,
                recent_context=[
                    {
                        "author": "João",
                        "text": "O que sobra pro beta?",
                    },
                    {
                        "author": "João",
                        "text": "O que sobra pro beta?",
                    },
                ],
            )

            self.assertEqual(knowledge.calls, 0)
            self.assertNotIn("Talisson atua com tecnologia", service.prompts[0])
            self.assertIn(
                "nunca interprete essa frase como versão beta de software",
                service.prompts[0],
            )

    async def test_recent_memory_is_sent_on_follow_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = _settings()
            service = RecordingService(
                [
                    "Talisson Souza criou a Bella.",
                    "Ele é Talisson Souza.",
                ]
            )
            assistant = PublicAssistant(
                service,
                settings,
                state,
                KnowledgeBase(settings, state),
            )

            await assistant.reply(
                "Quem te criou?",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=3,
            )
            await assistant.reply(
                "Quem é ele?",
                display_name="João",
                guild_id=1,
                channel_id=2,
                user_id=3,
            )

            self.assertIn("Quem te criou?", service.prompts[1])
            self.assertIn("Talisson Souza criou a Bella.", service.prompts[1])

    async def test_short_term_name_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            memory = ConversationMemory(
                state,
                max_messages=24,
                ttl_minutes=15,
            )

            await memory.observe_user_message(1, 2, "Meu nome é João.")
            facts = await memory.facts(1, 2)

            self.assertEqual(facts["name"], "João")

    def test_channel_context_keeps_recent_messages(self) -> None:
        context = ChannelContextBuffer(max_messages=30, ttl_minutes=15)
        context.observe(
            guild_id=1,
            channel_id=2,
            message_id=10,
            author_id=3,
            author_name="João",
            text="Acabei de lançar um SaaS.",
        )

        recent = context.recent(1, 2)

        self.assertEqual(recent[-1]["author"], "João")
        self.assertIn("SaaS", recent[-1]["text"])

    async def test_server_answer_mentions_relevant_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            index = DiscordKnowledgeIndex(
                StateStore(Path(directory) / "state.json")
            )
            index._entries[1] = (
                ServerKnowledgeEntry(
                    guild_id=1,
                    channel_id=20,
                    channel_name="changelog",
                    channel_mention="<#20>",
                    category="Informações",
                    topic="Atualizações da plataforma",
                    content="As atualizações e novidades são publicadas aqui.",
                    indexed_at="2026-06-11T00:00:00+00:00",
                ),
            )

            answer = await index.answer(1, "Onde vejo atualizações?")

            self.assertIn("<#20>", answer)


if __name__ == "__main__":
    unittest.main()
