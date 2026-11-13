from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from src.ai.intent_parser import GeminiIntentParser
from website_guard.bella_agent import (
    INBOX_SECTION,
    BellaAgent,
    StripeClient,
    classify_urgency,
    format_brl,
    parse_price,
)
from website_guard.executive import ExecutiveOutcome, ExecutiveRequest
from website_guard.state import StateStore

OWNER = 933780111742877777


class FakeExecutive:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.before_execute = None
        self.after_execute = None
        self.ran: list[ExecutiveRequest] = []

    def register_handler(self, action, handler) -> None:
        self.handlers[action] = handler

    async def run_request(self, message, request, *, reply=True):
        self.ran.append(request)
        outcome = ExecutiveOutcome(True, "ok", "ok")
        if self.after_execute is not None:
            await self.after_execute(message, request, outcome)
        return outcome


class FakeChannel:
    id = 10
    name = "geral"


class FakeGuild:
    id = 1
    name = "Servidor"
    members: list = []

    def get_member(self, _):
        return None

    def get_role(self, _):
        return None

    def get_channel(self, _):
        return None


class FakeMessage:
    def __init__(self) -> None:
        self.guild = FakeGuild()
        self.channel = FakeChannel()
        self.author = SimpleNamespace(id=OWNER, display_name="Talisson", bot=False)
        self.replies: list[str] = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(id=99)


def _agent(directory: str) -> tuple[BellaAgent, FakeExecutive, StateStore]:
    state = StateStore(Path(directory) / "state.json")
    executive = FakeExecutive()
    agent = BellaAgent(SimpleNamespace(), state, executive, owner_id=OWNER)
    agent.install()
    return agent, executive, state


class MoneyAndUrgencyTests(unittest.TestCase):
    def test_parse_price_accepts_common_formats(self) -> None:
        self.assertEqual(parse_price("1500.00"), Decimal("1500.00"))
        self.assertEqual(parse_price("1.500"), Decimal("1500.00"))
        self.assertEqual(parse_price("1.500,90"), Decimal("1500.90"))
        self.assertEqual(parse_price("R$ 89,9"), Decimal("89.90"))
        self.assertIsNone(parse_price("abc"))
        self.assertIsNone(parse_price("0"))

    def test_format_brl(self) -> None:
        self.assertEqual(format_brl(Decimal("1500.5")), "R$ 1.500,50")
        self.assertEqual(format_brl(Decimal("89.90")), "R$ 89,90")

    def test_urgency(self) -> None:
        self.assertTrue(classify_urgency("o site caiu, socorro")[0])
        self.assertFalse(classify_urgency("boa noite talisson")[0])


class StripeClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_payment_link_uses_price_in_cents_and_single_use(self) -> None:
        client = StripeClient("sk_test_123", currency="brl")
        calls: list[tuple[str, dict]] = []

        async def fake_post(path, data):
            calls.append((path, data))
            return {"id": "price_1"} if path == "/prices" else {"id": "plink_1", "url": "https://buy.stripe.com/x"}

        client._post = fake_post
        link = await client.create_payment_link(
            amount=Decimal("1500.50"),
            service="Site institucional",
            customer="João",
            metadata={"cliente": "João"},
        )
        self.assertEqual(link["url"], "https://buy.stripe.com/x")
        self.assertEqual(calls[0][1]["unit_amount"], "150050")
        self.assertEqual(calls[0][1]["currency"], "brl")
        self.assertEqual(calls[1][1]["line_items[0][price]"], "price_1")
        self.assertEqual(calls[1][1]["restrictions[completed_sessions][limit]"], "1")
        self.assertTrue(client.test_mode)


class AgentFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_todos_add_list_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, executive, _ = _agent(directory)
            message = FakeMessage()
            await executive.handlers["todo_add"](message, ExecutiveRequest("todo_add", text="revisar contrato"))
            await executive.handlers["todo_add"](message, ExecutiveRequest("todo_add", text="pagar hospedagem"))
            listing = await executive.handlers["todo_list"](message, ExecutiveRequest("todo_list"))
            self.assertIn("2. pagar hospedagem", listing.public_message)
            done = await executive.handlers["todo_done"](message, ExecutiveRequest("todo_done", amount=1))
            self.assertIn("revisar contrato", done.public_message)
            listing = await executive.handlers["todo_list"](message, ExecutiveRequest("todo_list"))
            self.assertNotIn("revisar", listing.public_message)

    async def test_history_feeds_continuity_and_undo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, executive, _ = _agent(directory)
            message = FakeMessage()
            ban = ExecutiveRequest("ban", target_id=55, reason="spam", permanent=True)
            await agent.after_execute(message, ban, ExecutiveOutcome(True, "ok", "ok"))
            recent = await agent.recent_actions(1)
            self.assertEqual(recent[0]["action"], "ban")
            self.assertEqual(recent[0]["target_id"], 55)

            outcome = await executive.handlers["undo_last"](message, ExecutiveRequest("undo_last"))
            self.assertTrue(outcome.success)
            self.assertEqual(executive.ran[-1].action, "unban")
            self.assertEqual(executive.ran[-1].target_id, 55)
            # O próprio desfazer não vira histórico.
            self.assertEqual(await agent.recent_actions(1), [])

    async def test_kick_cannot_be_undone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, executive, _ = _agent(directory)
            message = FakeMessage()
            await agent.after_execute(
                message, ExecutiveRequest("kick", target_id=7), ExecutiveOutcome(True, "ok", "ok")
            )
            outcome = await executive.handlers["undo_last"](message, ExecutiveRequest("undo_last"))
            self.assertFalse(outcome.success)
            self.assertIn("não consigo desfazer", outcome.public_message)

    async def test_gate_routes_plans_payments_and_plain_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, _, _ = _agent(directory)
            agent.stripe = StripeClient("")
            message = FakeMessage()
            self.assertFalse(await agent.before_execute(message, ExecutiveRequest("ban", target_id=5)))
            self.assertTrue(
                await agent.before_execute(message, ExecutiveRequest("payment_link_create", price="10"))
            )
            self.assertIn("Configurar Stripe", message.replies[-1])
            self.assertTrue(
                await agent.before_execute(
                    message,
                    ExecutiveRequest(
                        "channel_create_text",
                        name="suporte",
                        steps=(
                            ExecutiveRequest("channel_create_text", name="suporte"),
                            ExecutiveRequest("message_send", text="bem-vindo"),
                        ),
                    ),
                )
            )
            self.assertIn("posso fazer?", message.replies[-1])
            self.assertTrue(await agent.before_execute(message, ExecutiveRequest("message_purge", amount=50)))
            self.assertIn("não tem volta", message.replies[-1])

    async def test_briefing_groups_urgent_mentions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, _, state = _agent(directory)
            now = datetime.now(timezone.utc)
            await state.update_section(
                INBOX_SECTION,
                "1",
                [
                    {
                        "at": now.isoformat(),
                        "author_name": "Cliente",
                        "channel_name": "suporte",
                        "jump_url": "https://discord.com/x",
                        "snippet": "o site caiu",
                        "urgent": True,
                    },
                    {
                        "at": now.isoformat(),
                        "author_name": "Amigo",
                        "channel_name": "geral",
                        "jump_url": "https://discord.com/y",
                        "snippet": "bora jogar",
                        "urgent": False,
                    },
                ],
            )
            text = await agent.build_briefing(FakeGuild(), now - timedelta(hours=1))
            self.assertIn("te chamaram 2x", text)
            self.assertIn("[urgente] Cliente", text)
            self.assertLess(text.index("Cliente"), text.index("Amigo"))


class StripeSetupTests(unittest.IsolatedAsyncioTestCase):
    def test_guide_fits_one_discord_message(self) -> None:
        from website_guard.bella_agent import STRIPE_GUIDE

        self.assertLess(len("o Stripe ainda não tá configurado.\n\n" + STRIPE_GUIDE), 2000)
        self.assertIn("Payment Links", STRIPE_GUIDE)
        self.assertIn("não me manda a chave aqui no chat", STRIPE_GUIDE)

    def test_write_env_value_replaces_and_appends(self) -> None:
        from website_guard.bella_agent import write_env_value

        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("DISCORD_TOKEN=abc\nSTRIPE_SECRET_KEY=\n", encoding="utf-8")
            write_env_value(env, "STRIPE_SECRET_KEY", "rk_test_novo")
            write_env_value(env, "STRIPE_CURRENCY", "brl")
            self.assertEqual(
                env.read_text(encoding="utf-8"),
                "DISCORD_TOKEN=abc\nSTRIPE_SECRET_KEY=rk_test_novo\nSTRIPE_CURRENCY=brl\n",
            )

    async def _configure(self, verify_error=None, key="rk_test_" + "a" * 24):
        import website_guard.bella_agent as module

        with tempfile.TemporaryDirectory() as directory:
            agent, _, _ = _agent(directory)
            env = Path(directory) / ".env"
            env.write_text("STRIPE_SECRET_KEY=\n", encoding="utf-8")
            original_env, original_verify = module.ENV_PATH, module.StripeClient.verify

            async def fake_verify(_self):
                if verify_error is not None:
                    raise verify_error

            module.ENV_PATH = env
            module.StripeClient.verify = fake_verify
            saved_environ = {k: os.environ.get(k) for k in ("STRIPE_SECRET_KEY", "STRIPE_CURRENCY")}
            try:
                result = await agent.configure_stripe(key, "BRL")
                return result, agent, env.read_text(encoding="utf-8")
            finally:
                module.ENV_PATH, module.StripeClient.verify = original_env, original_verify
                for name, value in saved_environ.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    async def test_valid_key_is_saved_and_used(self) -> None:
        result, agent, env_text = await self._configure()
        self.assertIn("Stripe configurado (modo teste", result)
        self.assertTrue(agent.stripe.configured)
        self.assertIn("STRIPE_SECRET_KEY=rk_test_", env_text)
        self.assertIn("STRIPE_CURRENCY=brl", env_text)
        self.assertNotIn("rk_test_", result)  # a chave nunca volta na resposta

    async def test_rejected_and_unauthorized_keys_are_not_saved(self) -> None:
        from website_guard.bella_agent import StripeError

        result, agent, env_text = await self._configure(StripeError("Invalid API Key", 401))
        self.assertIn("recusou", result)
        self.assertFalse(agent.stripe.configured)
        self.assertEqual(env_text, "STRIPE_SECRET_KEY=\n")
        result, _, _ = await self._configure(StripeError("forbidden", 403))
        self.assertIn("Payment Links", result)
        result, _, _ = await self._configure(key="minha-chave")
        self.assertIn("não parece do Stripe", result)

    async def test_startup_sends_guide_only_once(self) -> None:
        sent: list[str] = []

        class Owner:
            async def send(self, text, **kwargs):
                sent.append(text)

        class Bot:
            async def wait_until_ready(self):
                return None

            def add_view(self, view):
                self.view = view

            def get_user(self, _):
                return Owner()

        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            agent = BellaAgent(Bot(), state, FakeExecutive(), owner_id=OWNER)
            agent.stripe = StripeClient("")
            await agent.startup()
            await agent.startup()
            self.assertEqual(len(sent), 1)
            self.assertIn("Configurar Stripe", sent[0])


class PlanParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_steps_become_plan(self) -> None:
        class Service:
            available = True

            async def generate_json(self, prompt, schema, **kwargs):
                return {
                    "action": "channel_create_text",
                    "name": "suporte-joao",
                    "confidence": 0.95,
                    "next_steps": [
                        {"action": "channel_make_private"},
                        {"action": "channel_access_grant", "target_id": 123},
                        {"action": "message_send", "text": "bem-vindo!"},
                        {"action": "payment_link_create", "price": "10"},
                    ],
                }

        with tempfile.TemporaryDirectory() as directory:
            parser = GeminiIntentParser(Service(), StateStore(Path(directory) / "s.json"))
            request = await parser.interpret(
                "Bella cria um canal privado suporte-joao, libera pro <@123> e manda boas vindas",
                bot_user_id=999,
                mentioned_user_ids=[123],
                mentioned_role_ids=[],
                guild_id=1,
                current_channel_id=10,
            )
            self.assertEqual(
                [step.action for step in request.steps],
                ["channel_create_text", "channel_make_private", "channel_access_grant", "message_send"],
            )
            self.assertEqual(request.steps[2].target_id, 123)


if __name__ == "__main__":
    unittest.main()
