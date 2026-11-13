from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from website_guard.owner_assistant import (
    ACTIVITY_SECTION,
    DRAFTS_SECTION,
    PAYMENT_SECTION,
    QUESTIONS_SECTION,
    TICKET_SECTION,
    TODO_SECTION,
    OwnerAssistant,
)
from website_guard.reminder_service import ReminderService
from website_guard.state import StateStore

OWNER = 933780111742877777
CHANNEL = 1570783779574780176


class FakeChannel:
    def __init__(self, channel_id: int = CHANNEL) -> None:
        self.id = channel_id
        self.sent: list[str] = []
        self.guild = None

    async def send(self, text, **kwargs):
        self.sent.append(text)


class FakeUser:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text, **kwargs):
        self.sent.append(text)


class FakeBot:
    def __init__(self) -> None:
        self.channel = FakeChannel()
        self.users: dict[int, FakeUser] = {}
        self.user = SimpleNamespace(id=1)

    def get_channel(self, channel_id):
        return self.channel if channel_id == CHANNEL else None

    def get_user(self, user_id):
        return self.users.setdefault(user_id, FakeUser())


class FakeAI:
    def __init__(self, payload) -> None:
        self.payload = payload

    async def generate_json(self, *args, **kwargs):
        return self.payload

    async def generate_text(self, *args, **kwargs):
        return "e aí, o bot ainda tá rodando liso? qualquer coisa me chama!"


def _message(text: str, *, channel_id: int = CHANNEL, author: int = OWNER, attachments=()):
    return SimpleNamespace(
        content=text,
        author=SimpleNamespace(id=author, bot=False, display_name="Fulano"),
        channel=SimpleNamespace(id=channel_id),
        guild=SimpleNamespace(id=5),
        attachments=list(attachments),
    )


class OwnerAssistantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self._dir.name) / "s.json")
        self.bot = FakeBot()
        self.reminders = ReminderService(self.bot, self.state, ai_service=None)
        self.owner = OwnerAssistant(
            self.bot, self.state, owner_id=OWNER, reminders=self.reminders, channel_id=CHANNEL,
        )

    async def asyncTearDown(self) -> None:
        self._dir.cleanup()

    async def test_pending_today_without_ai(self) -> None:
        await self.state.update_section(TODO_SECTION, str(OWNER), [{"text": "terminar o site", "done": False}])
        later = self.reminders.now() + timedelta(minutes=30)
        if later.date() == self.reminders.now().date():
            await self.reminders.create(user_id=OWNER, description="call com cliente", event_at=later)
        answer = await self.owner.handle(_message("tem algo pendente pra hoje?"), "tem algo pendente pra hoje?")
        self.assertIn("terminar o site", answer)
        # Funciona também fora do canal privado (quando ele chama a Bella).
        elsewhere = await self.owner.handle(_message("tem algo pendente pra hoje?", channel_id=1), "tem algo pendente pra hoje?")
        self.assertIn("terminar o site", elsewhere)

    async def test_other_people_are_ignored(self) -> None:
        self.assertIsNone(await self.owner.handle(_message("tem algo pendente?", author=42), "tem algo pendente?"))

    async def test_decision_vault_and_conflict(self) -> None:
        saved = await self.owner.handle(
            _message("anota: decidi não pegar mais projeto abaixo de R$500"),
            "anota: decidi não pegar mais projeto abaixo de R$500",
        )
        self.assertIn("cofre", saved)
        listing = await self.owner.handle(_message("quais minhas decisões?"), "quais minhas decisoes?")
        self.assertIn("R$500", listing)
        warning = await self.owner.handle(
            _message("vou aceitar o projeto do Fulano por R$ 300"), "vou aceitar o projeto do Fulano por R$ 300",
        )
        self.assertIn("lembra da sua regra", warning)
        self.assertEqual(await self.owner.check_decisions("cobrar R$ 1.200,00 pelo site"), "")

    async def test_spoken_agenda_creates_reminders_and_tasks(self) -> None:
        tomorrow = (self.owner.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        self.owner.ai = FakeAI({"items": [
            {"text": "ligar pro Fulano", "datetime_local": f"{tomorrow} 15:00", "day": ""},
            {"text": "terminar o site", "datetime_local": "", "day": ""},
            {"text": "pagar a Contabo", "datetime_local": "", "day": tomorrow},
        ]})
        report = await self.owner.organize_agenda("amanhã tenho que ligar pro Fulano às 15h, terminar o site e pagar a Contabo")
        self.assertIn("ligar pro Fulano", report)
        self.assertEqual(len(await self.reminders.list_for(OWNER)), 1)
        todos = (await self.state.get_section(TODO_SECTION))[str(OWNER)]
        self.assertEqual([t["text"] for t in todos][0], "terminar o site")

    async def test_mass_leave_alert(self) -> None:
        guild = SimpleNamespace(id=5, text_channels=[])
        for index in range(5):
            await self.owner.member_left(SimpleNamespace(id=index, bot=False, display_name=f"m{index}", guild=guild))
        self.assertEqual(len(self.bot.channel.sent), 1)
        self.assertIn("5 pessoas saíram", self.bot.channel.sent[0])
        await self.owner.member_left(SimpleNamespace(id=99, bot=False, display_name="x", guild=guild))
        self.assertEqual(len(self.bot.channel.sent), 1)  # não repete o alerta em seguida

    async def test_staff_ranking(self) -> None:
        now = datetime.now(timezone.utc)
        await self.state.update_section(TICKET_SECTION, "1", {
            "creator_id": 10, "created_at": now.isoformat(),
            "human_first_responder": 77, "human_first_response_at": (now + timedelta(minutes=12)).isoformat(),
            "closed_by": 77,
        })
        ranking = await self.owner.staff_ranking()
        self.assertIn("<@77>", ranking)
        self.assertIn("12 min", ranking)

    async def test_idle_client_draft_needs_approval(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        await self.state.update_section(PAYMENT_SECTION, "pl_1", {"customer_id": 555, "service": "bot de Discord", "amount": "800", "created_at": old})
        await self.state.update_section(ACTIVITY_SECTION, "555", {"last_seen": old, "name": "Cliente"})
        self.owner.ai = FakeAI({})
        self.assertEqual(await self.owner.propose_client_drafts(), 1)
        self.assertIn("posso mandar", self.bot.channel.sent[-1])
        self.assertEqual(self.bot.get_user(555).sent, [])  # nada enviado sem aprovação
        answer = await self.owner.handle(_message("manda"), "manda")
        self.assertIn("mandei", answer)
        self.assertEqual(len(self.bot.get_user(555).sent), 1)
        drafts = await self.state.get_section(DRAFTS_SECTION)
        self.assertEqual(next(iter(drafts.values()))["status"], "sent")

    async def test_community_questions_and_content_ideas(self) -> None:
        for user in range(4):
            await self.owner.observe_message(_message("como hospedar um bot de discord de graça?", author=100 + user, channel_id=9))
        items = (await self.state.get_section(QUESTIONS_SECTION))["items"]
        self.assertEqual(len(items), 4)
        ideas = await self.owner.content_ideas()
        self.assertIn("4 pessoas", ideas)

    async def test_weekly_summary(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.state.update_section(TODO_SECTION, str(OWNER), [
            {"text": "entregar bot", "done": True, "done_at": now},
            {"text": "responder cliente", "done": False},
        ])
        summary = await self.owner.weekly_summary()
        self.assertIn("entregar bot", summary)
        self.assertIn("responder cliente", summary)
        self.assertIn("dinheiro", summary)


if __name__ == "__main__":
    unittest.main()
