from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from website_guard.smart_tasks import (
    OPERATIONAL_PENDING_SECTION,
    SMART_TASKS_SECTION,
    TICKET_RECORD_SECTION,
    SmartTaskCenter,
)
from website_guard.state import StateStore


class FakeService:
    available = True

    def __init__(self) -> None:
        self.calls = 0

    async def generate_json(self, prompt, schema, **kwargs):
        del prompt, schema, kwargs
        self.calls += 1
        return {
            "title": "Orçamentos durante ausência",
            "objective": "Orientar interessados em orçamento.",
            "trigger_type": "keyword",
            "trigger_terms": ["orçamento", "contratar"],
            "action_type": "reply",
            "response_text": (
                "O Talisson está ausente no momento. Para solicitar um orçamento, "
                "use https://forms.gle/teste"
            ),
            "scope_channel_ids": [],
            "scope_category_ids": [],
            "scope_user_ids": [],
            "scope_role_ids": [],
            "target_channel_id": 0,
            "ends_at": "",
            "end_condition": "owner_message",
            "confidence": 0.98,
        }


class FakeRole:
    def __init__(self, role_id: int, name: str) -> None:
        self.id = role_id
        self.name = name

    def is_default(self) -> bool:
        return False


class FakeAuthor:
    def __init__(self, user_id: int, name: str = "Usuário") -> None:
        self.id = user_id
        self.name = name
        self.display_name = name
        self.bot = False
        self.roles = []

    async def send(self, content: str) -> None:
        self.last_dm = content


class FakeMessage:
    def __init__(
        self,
        *,
        message_id: int,
        content: str,
        author: FakeAuthor,
        guild,
        channel,
    ) -> None:
        self.id = message_id
        self.content = content
        self.author = author
        self.guild = guild
        self.channel = channel
        self.mentions = []
        self.role_mentions = []
        self.channel_mentions = []
        self.replies: list[str] = []
        self.jump_url = f"https://discord.com/channels/10/20/{message_id}"

    async def reply(self, content: str, **kwargs) -> None:
        del kwargs
        self.replies.append(content)


class SmartTaskTests(unittest.IsolatedAsyncioTestCase):
    def test_ticket_absence_instruction_is_detected(self) -> None:
        content = (
            "Bella, nesse ticket aqui, se o usuário que abriu lá mandar mensagem "
            "me respondendo diga que precisei sair. Assim que eu responder no "
            "ticket, desative os avisos."
        )

        self.assertTrue(SmartTaskCenter.looks_like_instruction(content))

    async def test_task_uses_ai_once_then_executes_locally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            service = FakeService()
            channel = SimpleNamespace(id=20, name="geral", category_id=None)
            owner = FakeAuthor(933780111742877777, "Talisson")
            member = FakeAuthor(50, "João")
            guild = SimpleNamespace(
                id=10,
                channels=[channel],
                roles=[FakeRole(30, "Membro")],
                members=[owner, member],
            )
            center = SmartTaskCenter(
                SimpleNamespace(),
                state,
                service,
            )
            instruction = FakeMessage(
                message_id=1,
                content=(
                    "Bella, até eu voltar, se alguém perguntar sobre orçamento "
                    "manda este formulário: https://forms.gle/teste"
                ),
                author=owner,
                guild=guild,
                channel=channel,
            )

            handled = await center.handle_instruction(
                instruction,
                invoked=True,
            )
            question = FakeMessage(
                message_id=2,
                content="Como solicito um orçamento?",
                author=member,
                guild=guild,
                channel=channel,
            )
            executed = await center.apply_message(question)
            repeated = await center.apply_message(question)

            self.assertTrue(handled)
            self.assertTrue(executed)
            self.assertFalse(repeated)
            self.assertEqual(service.calls, 1)
            self.assertIn("Guardei essa tarefa temporária", instruction.replies[0])
            self.assertIn("https://forms.gle/teste", question.replies[0])

    async def test_owner_message_ends_until_return_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            service = FakeService()
            channel = SimpleNamespace(id=20, name="geral", category_id=None)
            owner = FakeAuthor(933780111742877777, "Talisson")
            guild = SimpleNamespace(
                id=10,
                channels=[channel],
                roles=[FakeRole(30, "Membro")],
                members=[owner],
            )
            center = SmartTaskCenter(SimpleNamespace(), state, service)
            instruction = FakeMessage(
                message_id=1,
                content=(
                    "Bella, até eu voltar, se alguém perguntar sobre orçamento "
                    "manda este formulário: https://forms.gle/teste"
                ),
                author=owner,
                guild=guild,
                channel=channel,
            )
            await center.handle_instruction(instruction, invoked=True)
            returned = FakeMessage(
                message_id=2,
                content="Voltei, pessoal.",
                author=owner,
                guild=guild,
                channel=channel,
            )

            await center.apply_message(returned)
            records = await state.get_section(SMART_TASKS_SECTION)

            self.assertFalse(next(iter(records.values()))["enabled"])

    async def test_ticket_task_resolves_thread_creator_and_clears_old_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            service = FakeService()
            command_channel = SimpleNamespace(
                id=20,
                name="comandos",
                category_id=None,
            )
            ticket_thread = SimpleNamespace(
                id=200,
                name="orcamento-prioritario",
                category_id=None,
            )
            owner = FakeAuthor(933780111742877777, "Talisson")
            creator = FakeAuthor(50, "João")
            outsider = FakeAuthor(51, "Maria")
            guild = SimpleNamespace(
                id=10,
                channels=[command_channel],
                threads=[ticket_thread],
                roles=[FakeRole(30, "Membro")],
                members=[owner, creator, outsider],
            )
            await state.update_section(
                TICKET_RECORD_SECTION,
                str(ticket_thread.id),
                {
                    "guild_id": guild.id,
                    "creator_id": creator.id,
                    "creator_name": creator.display_name,
                    "category": "Orçamento",
                    "status": "open",
                },
            )
            await state.update_section(
                OPERATIONAL_PENDING_SECTION,
                f"{guild.id}:{owner.id}",
                {"content": "regra antiga", "created_at": "2026-07-02T08:00:00+00:00"},
            )
            center = SmartTaskCenter(SimpleNamespace(), state, service)
            instruction = FakeMessage(
                message_id=10,
                content=(
                    "Bella, nesse ticket aqui <#200>, se o usuário que abriu lá "
                    "mandar mensagem me respondendo diga que precisei sair para "
                    "resolver umas coisas pessoais e ele deve esperar eu voltar. "
                    "Assim que eu responder no ticket desative os avisos."
                ),
                author=owner,
                guild=guild,
                channel=command_channel,
            )
            instruction.channel_mentions = [ticket_thread]

            self.assertTrue(
                await center.handle_instruction(instruction, invoked=True)
            )
            tasks = await state.get_section(SMART_TASKS_SECTION)
            task = next(iter(tasks.values()))
            pending = await state.get_section(OPERATIONAL_PENDING_SECTION)

            self.assertEqual(task["trigger_type"], "any_message")
            self.assertEqual(task["scope_channel_ids"], [ticket_thread.id])
            self.assertEqual(task["scope_user_ids"], [creator.id])
            self.assertEqual(task["end_condition"], "owner_message")
            self.assertNotIn(f"{guild.id}:{owner.id}", pending)

            creator_message = FakeMessage(
                message_id=11,
                content="Conseguiu ver meu orçamento?",
                author=creator,
                guild=guild,
                channel=ticket_thread,
            )
            outsider_message = FakeMessage(
                message_id=12,
                content="Oi",
                author=outsider,
                guild=guild,
                channel=ticket_thread,
            )
            self.assertTrue(await center.apply_message(creator_message))
            self.assertFalse(await center.apply_message(outsider_message))

            owner_elsewhere = FakeMessage(
                message_id=13,
                content="Ainda estou resolvendo umas coisas.",
                author=owner,
                guild=guild,
                channel=command_channel,
            )
            await center.apply_message(owner_elsewhere)
            active = await state.get_section(SMART_TASKS_SECTION)
            self.assertTrue(next(iter(active.values()))["enabled"])

            owner_in_ticket = FakeMessage(
                message_id=14,
                content="Voltei. Pode continuar.",
                author=owner,
                guild=guild,
                channel=ticket_thread,
            )
            await center.apply_message(owner_in_ticket)
            finished = await state.get_section(SMART_TASKS_SECTION)
            self.assertFalse(next(iter(finished.values()))["enabled"])


if __name__ == "__main__":
    unittest.main()
