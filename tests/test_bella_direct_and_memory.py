from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.ai.complexity import assess_generation_mode
from src.ai.public_assistant import PublicAssistant
from website_guard.bella_agent import BellaAgent
from website_guard.executive import ExecutiveRequest
from website_guard.identity import OWNER_DISCORD_ID
from website_guard.knowledge import KnowledgeBase
from website_guard.state import StateStore

SETTINGS = SimpleNamespace(
    ai_context_messages=24,
    ai_context_ttl_minutes=15,
    ai_max_response_chars=1800,
    ai_public_min_confidence=0.72,
    gemini_timeout_seconds=75,
    knowledge_max_message_length=500,
    question_intent_min=0.90,
    knowledge_match_min=0.78,
    knowledge_min_confidence=0.90,
    talisson_user_id=OWNER_DISCORD_ID,
)


class NoAIService:
    """Falha o teste se a Bella chamar IA quando não deveria."""

    available = True
    timeout_seconds = 15

    def __init__(self, answer: str = "") -> None:
        self.answer = answer
        self.calls = 0

    async def generate_text(self, prompt, **kwargs):
        self.calls += 1
        return self.answer

    async def generate_json(self, prompt, schema, **kwargs):
        self.calls += 1
        return {"intent": "conversation", "should_reply": True, "site_related": False,
                "specialized_context_used": False, "answer_supported": True,
                "confidence": 0.98, "answer": self.answer}


def _assistant(directory: str, service) -> PublicAssistant:
    state = StateStore(Path(directory) / "s.json")
    return PublicAssistant(service, SETTINGS, state, KnowledgeBase(SETTINGS, state))


async def _reply(assistant, text, recent=None):
    return await assistant.reply(
        text, display_name="Talisson", guild_id=1, channel_id=2, user_id=3,
        recent_context=recent or [],
    )


class DirectnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_code_request_is_refused_without_ai(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = NoAIService()
            assistant = _assistant(directory, service)
            reply = await _reply(assistant, "cria um codigo de um bot de discord pra eu")
            self.assertEqual(reply.source, "code_policy")
            self.assertIn("código", reply.answer)
            self.assertEqual(service.calls, 0)

    async def test_short_follow_up_after_code_request_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            assistant = _assistant(directory, NoAIService())
            recent = [
                {"author": "Talisson", "text": "cria um codigo de um bot de discord pra eu"},
                {"author": "Bella", "text": "não faço código por aqui."},
            ]
            reply = await _reply(assistant, "python", recent)
            self.assertEqual(reply.source, "code_policy")

    async def test_ai_code_block_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = NoAIService("Toma:\n```python\nprint('oi')\n```")
            assistant = _assistant(directory, service)
            reply = await _reply(assistant, "me explica como funciona uma API REST em detalhes")
            self.assertNotIn("```", reply.answer)
            self.assertIn("código", reply.answer)

    def test_tasks_leave_short_chat_mode(self) -> None:
        self.assertTrue(assess_generation_mode("me ajuda a escrever um email pro cliente").task)
        self.assertTrue(assess_generation_mode("bom dia").social)


class OwnerMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_find_and_forget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            assistant = _assistant(directory, NoAIService("custa R$ 50"))
            executive = SimpleNamespace(
                handlers={}, before_execute=None, after_execute=None,
                register_handler=lambda action, handler: executive.handlers.__setitem__(action, handler),
            )
            agent = BellaAgent(
                SimpleNamespace(), assistant.state, executive,
                owner_id=OWNER_DISCORD_ID, knowledge=assistant.permanent,
            )
            agent.install()
            link = "https://discord.com/channels/1467976297689911377/1514971417651839197"
            message = SimpleNamespace(
                content=f"se alguém querer saber quanto é o meu livro, diz que é 50 reais. pra comprar, vai nesse canal {link}. Guarde isso",
                author=SimpleNamespace(id=OWNER_DISCORD_ID),
                guild=None,
                channel=SimpleNamespace(id=2),
            )
            saved = await executive.handlers["knowledge_save"](
                message,
                ExecutiveRequest(
                    "knowledge_save",
                    name="Livro do Talisson",
                    text=f"O livro do Talisson custa R$ 50. Para comprar, vá ao canal {link}.",
                    topic="livro, preço do livro, quanto custa o livro, comprar livro, valor do livro",
                ),
            )
            self.assertTrue(saved.success)
            self.assertIn("R$ 50", saved.public_message)
            # O link do canal vira menção nativa estruturada.
            self.assertIn("<#1514971417651839197>", saved.public_message)

            payload = await assistant.permanent.context_payload("quanto custa o livro do talisson?")
            self.assertTrue(any("R$ 50" in item["content"] for item in payload))
            self.assertIn("<#1514971417651839197>", payload[0]["content"])
            self.assertEqual(payload[0]["entities"][0]["entity_id"], 1514971417651839197)
            # Pergunta curta que antes caía em conversa sem memória.
            self.assertTrue(await assistant._matches_owner_memory("quero comprar o livro"))

            listing = await executive.handlers["knowledge_list"](message, ExecutiveRequest("knowledge_list"))
            self.assertIn("Livro do Talisson", listing.public_message)

            forgot = await executive.handlers["knowledge_forget"](
                message, ExecutiveRequest("knowledge_forget", text="o preço do livro")
            )
            self.assertTrue(forgot.success)
            self.assertFalse(await assistant._matches_owner_memory("quero comprar o livro"))


if __name__ == "__main__":
    unittest.main()
