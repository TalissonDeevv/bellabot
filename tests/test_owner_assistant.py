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


class FakeAvailability:
    def __init__(self) -> None:
        self.status = None

    async def set_status(self, **kwargs):
        self.status = kwargs

    async def clear_status(self):
        self.status = None


class OwnerDailyLifeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self._dir.name) / "s.json")
        self.bot = FakeBot()
        self.availability = FakeAvailability()
        self.presence_calls = 0

        async def refresh():
            self.presence_calls += 1

        self.owner = OwnerAssistant(
            self.bot, self.state, owner_id=OWNER, channel_id=CHANNEL,
            reminders=ReminderService(self.bot, self.state, ai_service=None),
            availability=self.availability, refresh_presence=refresh,
        )

    async def asyncTearDown(self) -> None:
        self._dir.cleanup()

    async def _say(self, text: str, **kwargs):
        return await self.owner.handle(_message(text, **kwargs), text)

    async def test_travel_mode_round_trip(self) -> None:
        back = self.owner.now() + timedelta(days=5)
        label = back.strftime("%d/%m")
        answer = await self._say(f"bella, vou ficar fora até {label}")
        self.assertIn(label, answer)
        self.assertIn(f"até {label}", self.availability.status["reason"])
        presence = await self.state.get("custom_presence_override")
        self.assertTrue(presence["enabled"])
        self.assertIn(label, presence["text"])
        self.assertEqual(self.presence_calls, 1)
        await self.state.update_section("agent_inbox", "5", [{
            "at": datetime.now(timezone.utc).isoformat(), "author_name": "Cliente", "channel_name": "geral",
            "snippet": "talisson, preciso de um orçamento urgente", "urgent": True, "jump_url": "",
        }])
        summary = await self._say("voltei")
        self.assertIn("preciso de um orçamento urgente", summary)
        self.assertIsNone(self.availability.status)
        self.assertFalse((await self.state.get("custom_presence_override"))["enabled"])

    async def test_travel_needs_a_date(self) -> None:
        self.assertIn("até quando", await self._say("vou viajar"))

    async def test_travel_ends_by_itself(self) -> None:
        past = self.owner.now() - timedelta(days=3)
        await self.state.update_section("owner_assistant_meta", "travel", {
            "active": True, "until": past.isoformat(), "started_at": datetime.now(timezone.utc).isoformat(),
        })
        await self.owner.tick()
        self.assertTrue(any("bem-vindo de volta" in text for text in self.bot.channel.sent))

    async def test_idea_box_and_monthly_review(self) -> None:
        saved = await self._say("ideia: fazer curso de bots")
        self.assertIn("fazer curso de bots", saved)
        await self._say("ideia: canal no youtube")
        self.assertIn("canal no youtube", await self._say("minhas ideias"))
        await self.owner.monthly_idea_review()
        self.assertIn("revisão mensal", self.bot.channel.sent[-1])
        chosen = await self._say("a 1")
        self.assertIn("fazer curso de bots", chosen)
        todos = (await self.state.get_section(TODO_SECTION))[str(OWNER)]
        self.assertIn("tirar do papel: fazer curso de bots", todos[-1]["text"])
        self.assertEqual(len(await self.owner._ideas()), 1)

    async def test_bills_remind_once_two_days_before(self) -> None:
        due = self.owner.now() + timedelta(days=1)
        answer = await self._say(f"Contabo vence dia {due.day} todo mês R$ 40,00")
        self.assertIn("Contabo", answer)
        self.assertIn("R$ 40,00", answer)
        self.assertEqual(await self.owner.check_bills(), 1)
        self.assertIn("Contabo", self.bot.channel.sent[-1])
        self.assertEqual(await self.owner.check_bills(), 0)  # não repete no mesmo mês
        self.assertIn("paga", await self._say("paguei a Contabo"))
        self.assertIn("✅", await self._say("minhas contas"))
        self.assertIn("tirei", await self._say("remove a conta da Contabo"))

    async def test_far_bill_is_not_reminded_yet(self) -> None:
        due = self.owner.now() + timedelta(days=10)
        await self._say(f"conta de luz vence dia {due.day} todo mês")
        if (self.owner._next_due(due.day) - self.owner.now().date()).days > 2:
            self.assertEqual(await self.owner.check_bills(), 0)

    async def test_what_was_i_doing_and_welcome_back(self) -> None:
        await self._say("anota: decidi cobrar entrada de 50%")
        await self._add_task("terminar o painel do cliente")
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        log = await self.state.get_section("owner_activity_log")
        for item in log["items"]:
            item["at"] = old
        await self.state.update_section("owner_activity_log", "items", log["items"])
        await self._say("bom dia bella")
        self.assertTrue(self.bot.channel.sent)
        welcome = self.bot.channel.sent[-1]
        self.assertIn("bem-vindo de volta", welcome)
        self.assertIn("cobrar entrada de 50%", welcome)
        doing = await self._say("o que eu tava fazendo?")
        self.assertIn("bom dia bella", doing)

    async def _add_task(self, text: str) -> None:
        await self.owner._add_todo(text)


class FakeMember:
    def __init__(self, member_id: int, name: str, *, blocked: bool = False) -> None:
        self.id = member_id
        self.display_name = name
        self.name = name.casefold()
        self.bot = False
        self.blocked = blocked
        self.sent: list[str] = []

    async def send(self, text, **kwargs):
        if self.blocked:
            raise __import__("discord").Forbidden(SimpleNamespace(status=403, reason="x"), "dm fechada")
        self.sent.append(text)
        return SimpleNamespace(id=1)


class RelayAI:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate_text(self, prompt, **kwargs):
        self.prompts.append(prompt)
        name = prompt.split("PESSOA: ")[1].split("\n")[0]
        return (
            f"Olá, {name}, tudo bem? O Talisson me pediu pra te dar um recado: seu bot de Discord já "
            "está pronto! Ele só precisa que você confirme o restante das informações lá no canal de ticket. ♡"
        )


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self._dir.name) / "s.json")
        self.bot = FakeBot()
        self.fulano = FakeMember(4242, "Fulano")
        self.channel = FakeChannel(channel_id=777000000000000777)
        self.channel.mention = "<#777000000000000777>"
        self.bot.get_channel = lambda cid: {CHANNEL: self.bot.channel, self.channel.id: self.channel}.get(cid)
        self.guild = SimpleNamespace(id=5, members=[self.fulano, FakeMember(1, "Ana"), FakeMember(2, "Ana")])
        self.ai = RelayAI()
        self.owner = OwnerAssistant(self.bot, self.state, owner_id=OWNER, channel_id=CHANNEL, ai_service=self.ai)

    async def asyncTearDown(self) -> None:
        self._dir.cleanup()

    def _msg(self, text: str, mentions=(), channel_id: int = 9):
        return SimpleNamespace(
            content=text, author=SimpleNamespace(id=OWNER, bot=False, display_name="Talisson"),
            channel=SimpleNamespace(id=channel_id), guild=self.guild, attachments=[], mentions=list(mentions), id=10,
        )

    async def test_dm_relay_is_written_by_ai(self) -> None:
        text = (
            "<@1> manda um recado pro <@4242> de que o bot de discord dele já está pronto. "
            "Só falta ele me passar o restante das informações pra mim. Mande na DM dele"
        )
        answer = await self.owner.handle(self._msg(text, [self.fulano]), text)
        self.assertEqual(len(self.fulano.sent), 1)
        self.assertIn("Olá, Fulano", self.fulano.sent[0])
        self.assertIn("mandei pro **Fulano** na DM", answer)
        # A IA recebe o pedido com o nome no lugar da menção.
        self.assertIn("pro Fulano de que o bot", self.ai.prompts[0])
        self.assertIn("na mensagem privada", self.ai.prompts[0])

    async def test_channel_relay_mentions_only_the_person(self) -> None:
        text = f"avisa o <@4242> no <#{self.channel.id}> que a reunião mudou pra amanhã às 15h"
        answer = await self.owner.handle(self._msg(text, [self.fulano]), text)
        self.assertEqual(self.fulano.sent, [])
        self.assertTrue(self.channel.sent[0].startswith("<@4242>"))
        self.assertIn("<#777000000000000777>", answer)

    async def test_closed_dm_is_reported(self) -> None:
        blocked = FakeMember(99, "Ciclano", blocked=True)
        text = "manda uma mensagem pro <@99> que o site ficou pronto"
        answer = await self.owner.handle(self._msg(text, [blocked]), text)
        self.assertIn("DM dele tá fechada", answer)

    async def test_name_without_mention_and_ambiguity(self) -> None:
        answer = await self.owner.handle(self._msg("manda um recado pro Fulano que amanhã tem call"), "manda um recado pro Fulano que amanhã tem call")
        self.assertEqual(len(self.fulano.sent), 1)
        ambiguous = await self.owner.handle(self._msg("manda um recado pra Ana que o pix caiu"), "manda um recado pra Ana que o pix caiu")
        self.assertIn("mais de uma pessoa", ambiguous)

    async def test_ai_down_uses_fallback(self) -> None:
        self.owner.ai = None
        text = "manda um recado pro <@4242> que o pagamento foi confirmado. manda no privado"
        await self.owner.handle(self._msg(text, [self.fulano]), text)
        self.assertIn("o pagamento foi confirmado", self.fulano.sent[0])
        self.assertNotIn("privado", self.fulano.sent[0])

    async def test_talking_to_bella_is_not_a_relay(self) -> None:
        self.assertIsNone(await self.owner.handle(self._msg("fala pra mim uma curiosidade"), "fala pra mim uma curiosidade"))
        self.assertEqual(self.fulano.sent, [])


if __name__ == "__main__":
    unittest.main()
