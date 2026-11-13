from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from website_guard.availability import (
    TalissonAvailabilityCenter,
    categorize_reminder,
    _now_dt,
)
from website_guard.state import StateStore


class FakeAuthor:
    def __init__(
        self,
        user_id: int,
        *,
        name: str = "Usuário",
    ) -> None:
        self.id = user_id
        self.bot = False
        self.display_name = name
        self.mention = f"<@{user_id}>"


class FakeChannel:
    def __init__(
        self,
        channel_id: int,
        *,
        name: str = "bate-papo",
    ) -> None:
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.sent: list[dict[str, object]] = []
        self._next_message_id = 1000

    async def send(self, *args, **kwargs) -> object:
        self.sent.append({"args": args, "kwargs": kwargs})
        self._next_message_id += 1
        return SimpleNamespace(
            id=self._next_message_id,
            channel=self,
            content=kwargs.get("content", ""),
        )


class FakeBot:
    def __init__(self) -> None:
        self.channels: dict[int, FakeChannel] = {}

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)


class FakeService:
    available = True

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def generate_json(self, prompt, schema, **kwargs):
        self.prompts.append(str(prompt))
        return {"text": self.responses.pop(0)}


class FakeMessage:
    def __init__(
        self,
        content: str,
        *,
        author_id: int = 123,
        author_name: str = "Membro",
        channel: FakeChannel | None = None,
        mentions: list[object] | None = None,
        channel_mentions: list[object] | None = None,
        reference_message_id: int | None = None,
    ) -> None:
        self.content = content
        self.author = FakeAuthor(author_id, name=author_name)
        self.guild = SimpleNamespace(id=10)
        self.channel = channel or FakeChannel(20)
        self.mentions = mentions or []
        self.channel_mentions = channel_mentions or []
        self.id = 555
        self.jump_url = f"https://discord.com/channels/10/{self.channel.id}/{self.id}"
        self.replies: list[str] = []
        self.reference = (
            SimpleNamespace(message_id=reference_message_id)
            if reference_message_id is not None
            else None
        )

    async def reply(self, content: str, **kwargs) -> None:
        self.replies.append(content)


class AvailabilityCenterTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_sets_and_clears_absence_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            center = TalissonAvailabilityCenter(
                FakeBot(),
                StateStore(Path(directory) / "state.json"),
            )
            away = FakeMessage(
                "Bella amanhã vou estar em consulta médica.",
                author_id=933780111742877777,
            )

            handled = await center.handle_message(
                away,
                invoked=True,
                content="amanhã vou estar em consulta médica.",
            )
            status = await center.status()

            self.assertTrue(handled)
            self.assertIsNotNone(status)
            self.assertEqual(status["reason"], "consulta médica")
            self.assertIn("Pode deixar", away.replies[0])

            back = FakeMessage(
                "Bella voltei",
                author_id=933780111742877777,
            )
            handled = await center.handle_message(
                back,
                invoked=True,
                content="voltei",
            )

            self.assertTrue(handled)
            self.assertIsNone(await center.status())
            self.assertIn("Bem-vindo de volta", back.replies[0])

    async def test_owner_absence_can_be_detected_without_explicit_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            center = TalissonAvailabilityCenter(
                FakeBot(),
                StateStore(Path(directory) / "state.json"),
            )
            away = FakeMessage(
                "amanhã eu vou ficar ausente por causa de uma reunião",
                author_id=933780111742877777,
            )

            handled = await center.handle_message(
                away,
                invoked=False,
                content=away.content,
            )
            status = await center.status()
            last_event = await center.state.get("talisson_availability_last_event")

            self.assertTrue(handled)
            self.assertIsNotNone(status)
            self.assertEqual(status["reason"], "reunião")
            self.assertEqual(last_event["type"], "set")
            self.assertIn("Pode deixar", away.replies[0])

    async def test_owner_return_can_clear_without_explicit_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            center = TalissonAvailabilityCenter(
                FakeBot(),
                StateStore(Path(directory) / "state.json"),
            )
            await center.set_status(
                reason="reunião",
                original_text="reunião",
                guild_id=10,
                channel_id=20,
                message_id=1,
            )
            back = FakeMessage(
                "voltei",
                author_id=933780111742877777,
            )

            handled = await center.handle_message(
                back,
                invoked=False,
                content=back.content,
            )

            self.assertTrue(handled)
            self.assertIsNone(await center.status())
            self.assertIn("Bem-vindo de volta", back.replies[0])

    async def test_owner_creates_personal_reminder_from_natural_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            center = TalissonAvailabilityCenter(
                FakeBot(),
                StateStore(Path(directory) / "state.json"),
            )
            message = FakeMessage(
                "Bella amanhã às 19h tenho uma reunião sobre a Cheetah.",
                author_id=933780111742877777,
            )

            handled = await center.handle_message(
                message,
                invoked=True,
                content="amanhã às 19h tenho uma reunião sobre a Cheetah.",
            )
            reminders = await center.state.get_section("talisson_personal_reminders")

            self.assertTrue(handled)
            self.assertEqual(len(reminders), 1)
            record = next(iter(reminders.values()))
            self.assertEqual(record["status"], "pendente")
            self.assertIn("Cheetah", record["description"])
            self.assertIn("Perfeito", message.replies[0])

    async def test_owner_can_query_tomorrow_agenda(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            center = TalissonAvailabilityCenter(
                FakeBot(),
                StateStore(Path(directory) / "state.json"),
            )
            scheduled_at = (_now_dt() + timedelta(days=1)).replace(
                hour=10,
                minute=0,
                second=0,
                microsecond=0,
            )
            await center.create_personal_reminder(
                guild_id=10,
                channel_id=20,
                message_id=1,
                creator_id=933780111742877777,
                title="Reunião com parceiro",
                description="Reunião com parceiro",
                scheduled_at=scheduled_at.isoformat(),
                priority="normal",
            )
            message = FakeMessage(
                "Bella quais compromissos tenho amanhã?",
                author_id=933780111742877777,
            )

            handled = await center.handle_message(
                message,
                invoked=True,
                content="quais compromissos tenho amanhã?",
            )

            self.assertTrue(handled)
            self.assertIn("Reunião com parceiro", message.replies[0])

    async def test_due_personal_reminder_sends_notification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()
            reminder_channel = FakeChannel(99, name="lembretes")
            bot.channels[99] = reminder_channel
            center = TalissonAvailabilityCenter(
                bot,
                StateStore(Path(directory) / "state.json"),
            )
            await center.configure_reminder_channel(10, 99)
            scheduled_at = (_now_dt() + timedelta(minutes=20)).replace(
                second=0,
                microsecond=0,
            )
            await center.create_personal_reminder(
                guild_id=10,
                channel_id=20,
                message_id=1,
                creator_id=933780111742877777,
                title="Reunião sobre a Cheetah",
                description="Reunião sobre a Cheetah",
                scheduled_at=scheduled_at.isoformat(),
                priority="normal",
            )

            sent = await center.dispatch_due_personal_reminders()
            records = await center.state.get_section("talisson_personal_reminders")

            self.assertEqual(sent, 1)
            self.assertEqual(len(reminder_channel.sent), 1)
            self.assertIn("Cheetah", str(reminder_channel.sent[0]["kwargs"].get("content")))
            self.assertIn(30, next(iter(records.values()))["notifications_sent"])

    async def test_deterministic_reminder_preserves_original_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()
            channel = FakeChannel(20, name="agenda")
            bot.channels[20] = channel
            center = TalissonAvailabilityCenter(
                bot,
                StateStore(Path(directory) / "state.json"),
            )
            scheduled_at = (_now_dt() + timedelta(minutes=20)).replace(
                second=0,
                microsecond=0,
            )
            original = "Dia 30/06/2026 Reunião às 18:20"
            await center.create_deterministic_reminder(
                guild_id=10,
                channel_id=20,
                creator_id=933780111742877777,
                message=original,
                scheduled_at=scheduled_at.isoformat(),
                antecedence_minutes=30,
            )

            sent = await center.dispatch_due_personal_reminders()
            records = await center.state.get_section("talisson_personal_reminders")
            record = next(iter(records.values()))
            content = str(channel.sent[0]["kwargs"].get("content"))

            self.assertEqual(sent, 1)
            self.assertEqual(
                content,
                f"<@933780111742877777>\nLembrete: {original}",
            )
            self.assertEqual(record["status"], "enviado")

    async def test_scheduling_request_uses_pending_and_logs_as_agendamento(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()
            reminder_channel = FakeChannel(99, name="lembretes")
            bot.channels[99] = reminder_channel
            center = TalissonAvailabilityCenter(
                bot,
                StateStore(Path(directory) / "state.json"),
            )
            await center.configure_reminder_channel(10, 99)
            first = FakeMessage(
                "Quero marcar um horário com o Talisson.",
                author_id=321,
                author_name="João",
            )

            handled = await center.handle_message(
                first,
                invoked=False,
                content=first.content,
            )
            follow_up = FakeMessage(
                "Sobre uma reunião comercial, posso amanhã à tarde.",
                author_id=321,
                author_name="João",
            )
            handled_follow_up = await center.handle_message(
                follow_up,
                invoked=False,
                content=follow_up.content,
            )
            records = await center.state.get_section("talisson_reminders")

            self.assertTrue(handled)
            self.assertTrue(handled_follow_up)
            record = next(iter(records.values()))
            self.assertEqual(record["source"], "agendamento")
            self.assertEqual(record["category"], "Agendamento")

    async def test_mention_while_away_creates_pending_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()
            reminder_channel = FakeChannel(99, name="lembretes")
            bot.channels[99] = reminder_channel
            center = TalissonAvailabilityCenter(
                bot,
                StateStore(Path(directory) / "state.json"),
            )
            await center.configure_reminder_channel(10, 99)
            await center.set_status(
                reason="consulta médica",
                original_text="consulta médica",
                guild_id=10,
                channel_id=20,
                message_id=1,
            )
            owner = SimpleNamespace(id=933780111742877777)
            ask = FakeMessage(
                "Alguém sabe do <@933780111742877777>?",
                mentions=[owner],
            )

            handled = await center.handle_message(
                ask,
                invoked=False,
                content=ask.content,
            )

            self.assertTrue(handled)
            self.assertIn("consulta médica", ask.replies[0])

            follow_up = FakeMessage(
                "Preciso falar com ele sobre uma parceria.",
                author_id=123,
                author_name="João",
            )
            handled = await center.handle_message(
                follow_up,
                invoked=False,
                content=follow_up.content,
            )
            reminders = await center.state.get_section("talisson_reminders")

            self.assertTrue(handled)
            self.assertEqual(len(reminders), 1)
            self.assertEqual(next(iter(reminders.values()))["category"], "Parceria")
            self.assertEqual(len(reminder_channel.sent), 1)
            payload = reminder_channel.sent[0]["kwargs"]
            text = str(payload.get("content"))
            self.assertNotIn("embed", payload)
            self.assertIn("<@933780111742877777>", text)
            self.assertIn("<@123>", text)
            self.assertIn("<#20>", text)
            self.assertNotIn("Categoria", text)
            self.assertNotIn("Mensagem original", text)
            self.assertIn("Perfeito", follow_up.replies[0])

    async def test_invite_is_registered_without_follow_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()
            reminder_channel = FakeChannel(99, name="lembretes")
            bot.channels[99] = reminder_channel
            center = TalissonAvailabilityCenter(
                bot,
                StateStore(Path(directory) / "state.json"),
            )
            await center.configure_reminder_channel(10, 99)
            message = FakeMessage(
                "Bella convida o Talisson para minha party.",
                author_id=321,
            )

            handled = await center.handle_message(
                message,
                invoked=True,
                content="convida o Talisson para minha party.",
            )

            self.assertTrue(handled)
            self.assertIn("Vou avisar", message.replies[0])
            self.assertEqual(len(reminder_channel.sent), 1)
            reminders = await center.state.get_section("talisson_reminders")
            self.assertEqual(next(iter(reminders.values()))["category"], "Convite")

    async def test_direct_message_to_talisson_is_registered_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()
            reminder_channel = FakeChannel(99, name="lembretes")
            bot.channels[99] = reminder_channel
            center = TalissonAvailabilityCenter(
                bot,
                StateStore(Path(directory) / "state.json"),
            )
            await center.configure_reminder_channel(10, 99)
            message = FakeMessage(
                "Bella diz pro talisson olhar o whatsapp",
                author_id=321,
                author_name="Cheetah Solutions",
            )

            handled = await center.handle_message(
                message,
                invoked=True,
                content="diz pro talisson olhar o whatsapp",
            )

            self.assertTrue(handled)
            self.assertIn("Vou avisar", message.replies[0])
            self.assertEqual(len(reminder_channel.sent), 1)
            reminders = await center.state.get_section("talisson_reminders")
            record = next(iter(reminders.values()))
            self.assertTrue(record["delivered"])
            self.assertIn("whatsapp", record["subject"])
            payload = reminder_channel.sent[0]["kwargs"]
            text = str(payload.get("content"))
            self.assertNotIn("embed", payload)
            self.assertIn("<@933780111742877777>", text)
            self.assertIn("<@321>", text)
            self.assertIn("whatsapp", text)
            self.assertNotIn("Categoria", text)
            self.assertNotIn("Horário", text)
            self.assertNotIn("Mensagem original", text)

    async def test_owner_can_reply_to_reminder_and_continue_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = FakeBot()
            origin_channel = FakeChannel(20, name="bate-papo")
            reminder_channel = FakeChannel(99, name="lembretes")
            bot.channels[20] = origin_channel
            bot.channels[99] = reminder_channel
            service = FakeService(
                [
                    "<@933780111742877777>\n\n<@321> pediu para avisar que amanhã haverá uma reunião.",
                    "Olá <@321>. O Talisson recebeu seu recado e pediu para informar que está ciente.",
                    "<@933780111742877777>\n\n<@321> respondeu ao seu retorno e disse que enviará os detalhes.",
                ]
            )
            center = TalissonAvailabilityCenter(
                bot,
                StateStore(Path(directory) / "state.json"),
                service=service,
            )
            await center.configure_reminder_channel(10, 99)
            user_message = FakeMessage(
                "Bella avisa o Talisson que amanhã tenho uma reunião.",
                author_id=321,
                author_name="João",
                channel=origin_channel,
            )

            handled = await center.handle_message(
                user_message,
                invoked=True,
                content="avisa o Talisson que amanhã tenho uma reunião.",
            )
            records = await center.state.get_section("talisson_reminders")
            record = next(iter(records.values()))

            self.assertTrue(handled)
            self.assertEqual(record["status"], "pendente")
            self.assertEqual(len(reminder_channel.sent), 1)
            owner_delivery_id = record["delivery_message_id"]

            owner_reply = FakeMessage(
                "Certo, diga que estarei ciente.",
                author_id=933780111742877777,
                channel=reminder_channel,
                reference_message_id=owner_delivery_id,
            )
            handled = await center.handle_message(
                owner_reply,
                invoked=False,
                content=owner_reply.content,
            )
            records = await center.state.get_section("talisson_reminders")
            record = next(iter(records.values()))

            self.assertTrue(handled)
            self.assertEqual(record["status"], "respondido")
            self.assertEqual(len(origin_channel.sent), 1)
            self.assertIn("<@321>", str(origin_channel.sent[0]["kwargs"].get("content")))
            self.assertIn("ciente", str(origin_channel.sent[0]["kwargs"].get("content")))

            member_reply = FakeMessage(
                "Obrigado, fala que vou enviar os detalhes.",
                author_id=321,
                channel=origin_channel,
                reference_message_id=record["last_user_delivery_message_id"],
            )
            handled = await center.handle_message(
                member_reply,
                invoked=False,
                content=member_reply.content,
            )
            records = await center.state.get_section("talisson_reminders")
            record = next(iter(records.values()))

            self.assertTrue(handled)
            self.assertEqual(record["status"], "pendente")
            self.assertEqual(len(reminder_channel.sent), 2)
            self.assertIn("detalhes", str(reminder_channel.sent[1]["kwargs"].get("content")))

            close = FakeMessage(
                "Pode encerrar.",
                author_id=933780111742877777,
                channel=reminder_channel,
                reference_message_id=record["last_owner_delivery_message_id"],
            )
            handled = await center.handle_message(
                close,
                invoked=False,
                content=close.content,
            )
            records = await center.state.get_section("talisson_reminders")
            record = next(iter(records.values()))

            self.assertTrue(handled)
            self.assertEqual(record["status"], "encerrado")
            self.assertIn("encerrado", close.replies[0])
            self.assertGreaterEqual(len(record["history"]), 4)
            self.assertEqual(len(service.prompts), 0)

    async def test_contact_request_must_target_talisson(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            center = TalissonAvailabilityCenter(
                FakeBot(),
                StateStore(Path(directory) / "state.json"),
            )
            message = FakeMessage(
                "Bella, quero falar com você.",
                author_id=321,
            )

            handled = await center.handle_message(
                message,
                invoked=True,
                content="quero falar com você.",
            )

            self.assertFalse(handled)
            self.assertFalse(message.replies)

    async def test_announcement_text_does_not_configure_reminder_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            channel = FakeChannel(1467978707191730509, name="anuncios")
            center = TalissonAvailabilityCenter(
                FakeBot(),
                StateStore(Path(directory) / "state.json"),
            )
            message = FakeMessage(
                (
                    "Bella crie um anúncio profissional apresentando suas novas "
                    "funcionalidades. O anúncio deve ser enviado no canal "
                    "<#1467978707191730509> e mencionar o cargo @Membro. "
                    "Explique as novidades, incluindo sistema de recados, sistema "
                    "de ausência, sistema de agenda e sistema de lembretes inteligentes."
                ),
                author_id=933780111742877777,
                channel_mentions=[channel],
            )

            handled = await center.handle_message(
                message,
                invoked=True,
                content=message.content,
            )
            config = await center.reminder_channel_id(10)

            self.assertFalse(handled)
            self.assertIsNone(config)
            self.assertFalse(message.replies)

    def test_categories_are_inferred_from_subject(self) -> None:
        self.assertEqual(
            categorize_reminder("Quero conversar sobre uma parceria."),
            "Parceria",
        )
        self.assertEqual(
            categorize_reminder("Tem um bug urgente no site."),
            "Urgente",
        )
        self.assertEqual(
            categorize_reminder("Chama ele para entrar na call."),
            "Convite",
        )


if __name__ == "__main__":
    unittest.main()
