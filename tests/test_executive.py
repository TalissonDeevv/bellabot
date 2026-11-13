from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from website_guard.admin import _executive_audit_embed
from website_guard.executive import (
    EXECUTIVE_OWNER_ID,
    ExecutiveAssistant,
    NaturalCommandInterpreter,
    TicketRegistry,
    _contains_sensitive_memory,
    _display,
)
from website_guard.state import StateStore


class NaturalCommandInterpreterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.interpreter = NaturalCommandInterpreter()
        self.bot_id = 100
        self.user_id = 200
        self.role_id = 300

    def parse(
        self,
        content: str,
        *,
        users: list[int] | None = None,
        roles: list[int] | None = None,
    ):
        return self.interpreter.interpret(
            content,
            bot_user_id=self.bot_id,
            mentioned_user_ids=users or [self.bot_id, self.user_id],
            mentioned_role_ids=roles or [self.role_id],
            mentioned_channel_ids=[],
        )

    def test_role_add_variations_share_the_same_intent(self) -> None:
        variants = (
            "<@100> coloca o cargo <@&300> no <@200>",
            "<@100> adiciona o cargo <@&300> ao <@200>",
            "<@100> dá o cargo <@&300> para <@200>",
            "<@100> promove <@200> para o cargo <@&300>",
        )
        for value in variants:
            with self.subTest(value=value):
                request = self.parse(value)
                self.assertIsNotNone(request)
                self.assertEqual(request.action, "role_add")
                self.assertEqual(request.target_id, self.user_id)
                self.assertEqual(request.role_ids, (self.role_id,))

    def test_role_remove_and_swap_are_contextual(self) -> None:
        remove = self.parse("<@100> retira o cargo <@&300> do <@200>")
        swap = self.parse(
            "<@100> troca o cargo <@&300> por <@&301> no <@200>",
            roles=[300, 301],
        )
        self.assertEqual(remove.action, "role_remove")
        self.assertEqual(swap.action, "role_swap")
        self.assertEqual(swap.role_ids, (300, 301))

    def test_timeout_parses_portuguese_duration(self) -> None:
        request = self.parse(
            "<@100> silencia <@200> por 30 minutos",
            roles=[],
        )
        self.assertEqual(request.action, "timeout")
        self.assertEqual(request.duration_seconds, 1800)

    def test_ticket_and_voice_actions_are_recognized(self) -> None:
        ticket = self.parse("<@100> cria um ticket para <@200>", roles=[])
        voice = self.parse(
            "<@100> desconecta <@200> do canal de voz",
            roles=[],
        )
        self.assertEqual(ticket.action, "ticket_create")
        self.assertEqual(voice.action, "voice_disconnect")

    def test_operational_actions_are_modular(self) -> None:
        status = self.parse("<@100> como está o status da plataforma?", users=[100], roles=[])
        feedbacks = self.parse("<@100> sincroniza os feedbacks", users=[100], roles=[])
        report = self.parse("<@100> envia o relatório semanal", users=[100], roles=[])
        self.assertEqual(status.action, "status")
        self.assertEqual(feedbacks.action, "feedback_sync")
        self.assertEqual(report.action, "weekly_report")

    def test_message_cleanup_and_event_are_local_actions(self) -> None:
        cleanup = self.parse(
            "<@100> apague as últimas 100 mensagens",
            users=[100],
            roles=[],
        )
        event = self.interpreter.interpret(
            "<@100> crie um evento chamado Reunião amanhã às 20h em <#400>",
            bot_user_id=100,
            mentioned_user_ids=[100],
            mentioned_role_ids=[],
            mentioned_channel_ids=[400],
        )

        self.assertEqual(cleanup.action, "message_purge")
        self.assertEqual(cleanup.amount, 100)
        self.assertEqual(event.action, "scheduled_event_create")
        self.assertEqual(event.event_name, "Reunião")
        self.assertTrue(event.scheduled_for)

        exact_report = self.parse(
            "<@100> apague 3 mensagens do chat",
            users=[100],
            roles=[],
        )
        self.assertEqual(exact_report.action, "message_purge")
        self.assertEqual(exact_report.amount, 3)

        screenshot_request = self.parse(
            "<@100> limpe 100 mensagens do chat aq",
            users=[100],
            roles=[],
        )
        self.assertEqual(screenshot_request.action, "message_purge")
        self.assertEqual(screenshot_request.amount, 100)

        ask_amount = self.parse(
            "<@100> limpe este chat",
            users=[100],
            roles=[],
        )
        self.assertEqual(ask_amount.action, "message_purge")
        self.assertIsNone(ask_amount.amount)

    def test_channel_lock_and_unlock_are_local_actions(self) -> None:
        locked = self.parse(
            "<@100> tranque este canal",
            users=[100],
            roles=[],
        )
        unlocked = self.parse(
            "<@100> destranque este canal",
            users=[100],
            roles=[],
        )

        self.assertEqual(locked.action, "channel_lock")
        self.assertEqual(unlocked.action, "channel_unlock")

    def test_negated_request_is_not_executed(self) -> None:
        request = self.parse("<@100> não bane <@200>", roles=[])
        self.assertIsNone(request)

    def test_channel_configuration_requires_context(self) -> None:
        request = self.interpreter.interpret(
            "<@100> usa <#400> para os logs de auditoria",
            bot_user_id=100,
            mentioned_user_ids=[100],
            mentioned_role_ids=[],
            mentioned_channel_ids=[400],
        )
        self.assertEqual(request.action, "configure_audit_channel")
        self.assertEqual(request.channel_ids, (400,))

    def test_multiple_targets_are_rejected_as_ambiguous(self) -> None:
        request = self.parse(
            "<@100> bane <@200> e <@201>",
            users=[100, 200, 201],
            roles=[],
        )
        self.assertEqual(request.action, "ban")
        self.assertIsNone(request.target_id)

    def test_unban_accepts_explicit_user_id_outside_member_catalog(self) -> None:
        user_id = 1362513159193366711
        request = self.interpreter.interpret(
            f"<@100> tire o banimento do usuário ID {user_id}",
            bot_user_id=100,
            mentioned_user_ids=[100],
            mentioned_role_ids=[],
            mentioned_channel_ids=[],
            member_catalog=[],
        )

        self.assertIsNotNone(request)
        self.assertEqual(request.action, "unban")
        self.assertEqual(request.target_id, user_id)

    def test_generic_punishment_revocation_is_recognized(self) -> None:
        user_id = 1362513159193366711
        request = self.interpreter.interpret(
            f"Bella revogue todas as punições do usuário ID {user_id}",
            bot_user_id=100,
            mentioned_user_ids=[100],
            mentioned_role_ids=[],
            mentioned_channel_ids=[],
            member_catalog=[],
        )

        self.assertIsNotNone(request)
        self.assertEqual(request.action, "punishment_revoke")
        self.assertEqual(request.target_id, user_id)

    def test_natural_names_resolve_without_gemini(self) -> None:
        request = self.interpreter.interpret(
            "Bella remove o cargo Moderador do João",
            bot_user_id=100,
            mentioned_user_ids=[100],
            mentioned_role_ids=[],
            mentioned_channel_ids=[],
            member_catalog=[
                {"id": 200, "name": "João", "username": "joao"},
            ],
            role_catalog=[{"id": 300, "name": "Moderador"}],
            channel_catalog=[],
        )

        self.assertEqual(request.action, "role_remove")
        self.assertEqual(request.target_id, 200)
        self.assertEqual(request.role_ids, (300,))


class TicketRegistryTests(unittest.TestCase):
    def test_ticket_history_survives_state_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"

            async def scenario() -> None:
                store = StateStore(path)
                registry = TicketRegistry(store)
                await registry.save(
                    10,
                    20,
                    {"channel_id": 30, "status": "closed"},
                )
                restored = TicketRegistry(StateStore(path))
                record = await restored.get(10, 20)
                by_channel = await restored.find_by_channel(10, 30)
                self.assertEqual(record["status"], "closed")
                self.assertEqual(by_channel[0], 20)

            asyncio.run(scenario())


class ExecutiveSecurityTests(unittest.TestCase):
    def test_non_owner_is_routed_to_public_assistant(self) -> None:
        bot_user = SimpleNamespace(id=100)
        bot = SimpleNamespace(user=bot_user)
        settings = SimpleNamespace(talisson_user_id=999)
        assistant = ExecutiveAssistant(
            bot,
            settings,
            SimpleNamespace(),
            SimpleNamespace(),
        )
        message = SimpleNamespace(
            author=SimpleNamespace(id=999),
            mentions=[bot_user],
            guild=SimpleNamespace(id=1),
        )
        handled = asyncio.run(assistant.handle(message))
        self.assertFalse(handled)

    def test_owner_identity_is_fixed_and_not_overridden_by_settings(self) -> None:
        bot = SimpleNamespace(user=SimpleNamespace(id=100))
        assistant = ExecutiveAssistant(
            bot,
            SimpleNamespace(talisson_user_id=999),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        self.assertTrue(assistant.is_owner(EXECUTIVE_OWNER_ID))
        self.assertEqual(EXECUTIVE_OWNER_ID, 933780111742877777)
        self.assertFalse(assistant.is_owner(999))

    def test_readable_names_cannot_create_target_mentions(self) -> None:
        readable = _display("<@200> João e <@&300> Moderador")
        self.assertNotIn("<@200>", readable)
        self.assertNotIn("<@&300>", readable)
        self.assertIn("João", readable)
        self.assertIn("Moderador", readable)

    def test_memory_rejects_credentials(self) -> None:
        self.assertTrue(
            _contains_sensitive_memory("GEMINI_API_KEY AQ.example-secret-value")
        )
        self.assertFalse(
            _contains_sensitive_memory("formato de relatorio detalhado")
        )

    def test_unban_sends_official_invite_by_dm(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                user_id = 1362513159193366711

                class FakeUser:
                    def __init__(self) -> None:
                        self.id = user_id
                        self.name = "usuario"
                        self.display_name = "Usuário"
                        self.mention = f"<@{user_id}>"
                        self.sent: list[str] = []

                    async def send(self, content: str) -> None:
                        self.sent.append(content)

                    def __str__(self) -> str:
                        return self.name

                target = FakeUser()
                bot_member = SimpleNamespace(
                    guild_permissions=SimpleNamespace(ban_members=True)
                )

                class FakeGuild:
                    id = 10
                    name = "Escritório do Talisson"
                    me = bot_member

                    def __init__(self) -> None:
                        self.unbanned: list[int] = []

                    async def fetch_ban(self, user: object) -> object:
                        self.fetched_id = user.id
                        return SimpleNamespace(user=target)

                    async def unban(self, user: FakeUser, *, reason: str) -> None:
                        self.unbanned.append(user.id)

                guild = FakeGuild()
                author = SimpleNamespace(
                    id=EXECUTIVE_OWNER_ID,
                    mention=f"<@{EXECUTIVE_OWNER_ID}>",
                )
                author.__str__ = lambda: "Talisson"
                message = SimpleNamespace(
                    guild=guild,
                    author=author,
                    channel=SimpleNamespace(),
                )
                state = StateStore(Path(directory) / "state.json")
                await state.update_section(
                    "temporary_bans",
                    f"{guild.id}:{user_id}",
                    {"user_id": user_id},
                )
                assistant = ExecutiveAssistant(
                    SimpleNamespace(user=SimpleNamespace(id=100)),
                    SimpleNamespace(
                        server_invite_url="https://discord.gg/FcdCGU5mQb"
                    ),
                    state,
                    SimpleNamespace(),
                )

                outcome = await assistant._unban(
                    message,
                    SimpleNamespace(
                        target_id=user_id,
                        reason="Revisão administrativa",
                    ),
                )

                self.assertTrue(outcome.success)
                self.assertEqual(guild.unbanned, [user_id])
                self.assertTrue(target.sent)
                self.assertIn(
                    "https://discord.gg/FcdCGU5mQb",
                    target.sent[0],
                )
                temporary = await state.get_section("temporary_bans")
                self.assertNotIn(f"{guild.id}:{user_id}", temporary)

        asyncio.run(scenario())

    def test_executive_reply_survives_audit_failure(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                class FailingAudit:
                    async def record_executive_action(self, *args, **kwargs) -> None:
                        raise RuntimeError("audit unavailable")

                class ReplyMessage:
                    def __init__(self) -> None:
                        self.id = 55
                        self.guild = SimpleNamespace(id=10)
                        self.author = SimpleNamespace(
                            id=EXECUTIVE_OWNER_ID,
                            mention=f"<@{EXECUTIVE_OWNER_ID}>",
                        )
                        self.channel = SimpleNamespace(id=20)
                        self.replies: list[str] = []

                    async def reply(self, content: str, **kwargs) -> None:
                        self.replies.append(content)

                state = StateStore(Path(directory) / "state.json")
                assistant = ExecutiveAssistant(
                    SimpleNamespace(user=SimpleNamespace(id=100)),
                    SimpleNamespace(),
                    state,
                    FailingAudit(),
                )
                message = ReplyMessage()
                outcome = SimpleNamespace(
                    success=True,
                    public_message="Banimento revogado com sucesso.",
                    audit_reason="Teste",
                    target=None,
                    roles=(),
                    notes="",
                )

                await assistant._record_and_reply(
                    message,
                    action="Revogar banimento",
                    outcome=outcome,
                    started_at=0.0,
                )

                self.assertEqual(
                    message.replies,
                    ["Banimento revogado com sucesso."],
                )

        asyncio.run(scenario())

    def test_audit_embed_contains_executor_and_target_avatars(self) -> None:
        def avatar(url: str) -> SimpleNamespace:
            return SimpleNamespace(url=url)

        executor = SimpleNamespace(
            id=1,
            name="Talisson",
            display_name="Talisson Souza",
            display_avatar=avatar("https://example.com/executor.png"),
        )
        executor.__str__ = lambda: "Talisson"
        target = SimpleNamespace(
            id=2,
            name="Joao",
            display_name="João",
            display_avatar=avatar("https://example.com/target.png"),
        )
        role = SimpleNamespace(id=3, name="Moderador")
        message = SimpleNamespace(
            author=executor,
            guild=SimpleNamespace(name="Escritório"),
            channel=SimpleNamespace(id=4, name="geral"),
        )
        embed = _executive_audit_embed(
            message,
            action="Adicionar cargo",
            target=target,
            roles=(role,),
            result="Sucesso",
            reason="Cargo atribuído.",
            duration_ms=12,
            notes="",
        )
        payload = embed.to_dict()
        self.assertEqual(
            payload["author"]["icon_url"],
            "https://example.com/executor.png",
        )
        self.assertEqual(
            payload["thumbnail"]["url"],
            "https://example.com/target.png",
        )
        field_names = {field["name"] for field in payload["fields"]}
        self.assertIn("Tempo de execução", field_names)
        self.assertIn("Usuário alvo", field_names)


if __name__ == "__main__":
    unittest.main()
