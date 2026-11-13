from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from website_guard.spam_trap import (
    SpamTrapLogView,
    SpamTrapMonitor,
    message_signature,
    render_spam_trap_evidence,
)
from website_guard.state import StateStore


class FakeHistory:
    def __init__(self, messages: list[object]) -> None:
        self.messages = messages

    def __aiter__(self):
        self._iterator = iter(self.messages)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeChannel:
    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name
        self.messages: list[FakeMessage] = []
        self.sent: list[dict[str, object]] = []
        self.fail_evidence = False
        self.fail_all = False

    def history(self, **kwargs) -> FakeHistory:
        return FakeHistory(list(reversed(self.messages)))

    async def send(self, **kwargs) -> None:
        if self.fail_all:
            raise RuntimeError("envio bloqueado")
        view = kwargs.get("view")
        if (
            self.fail_evidence
            and isinstance(view, SpamTrapLogView)
            and view.evidence_file is not None
        ):
            raise RuntimeError("anexos indisponíveis")
        self.sent.append(kwargs)


class FakeAuthor:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.bot = False
        self.mention = f"<@{user_id}>"
        self.name = "usuario_teste"
        self.display_name = "Usuário Teste"


class FakeGuild:
    def __init__(self) -> None:
        self.id = 10
        self.owner_id = 999
        self.text_channels: list[FakeChannel] = []
        self.threads: list[object] = []
        self.bans: list[tuple[int, str]] = []

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return next(
            (channel for channel in self.text_channels if channel.id == channel_id),
            None,
        )

    async def ban(self, author: FakeAuthor, *, reason: str, **kwargs) -> None:
        self.bans.append((author.id, reason))


class FakeMessage:
    def __init__(
        self,
        message_id: int,
        content: str,
        author: FakeAuthor,
        channel: FakeChannel,
        guild: FakeGuild,
    ) -> None:
        self.id = message_id
        self.content = content
        self.author = author
        self.channel = channel
        self.guild = guild
        self.attachments: list[object] = []
        self.stickers: list[object] = []
        self.jump_url = (
            f"https://discord.com/channels/{guild.id}/{channel.id}/{message_id}"
        )
        self.created_at = datetime.now(timezone.utc)
        self.deleted = False
        channel.messages.append(self)

    async def delete(self) -> None:
        self.deleted = True


class FakeBot:
    def __init__(self, guild: FakeGuild, log_channel: FakeChannel) -> None:
        self.guild = guild
        self.log_channel = log_channel

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        if channel_id == self.log_channel.id:
            return self.log_channel
        return self.guild.get_channel(channel_id)

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        return self.guild if guild_id == self.guild.id else None


class SpamTrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_trap_deletes_identical_messages_and_bans_author(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            guild = FakeGuild()
            general = FakeChannel(100, "geral")
            trap = FakeChannel(200, "detector-scam")
            logs = FakeChannel(300, "logs")
            guild.text_channels.extend((general, trap))
            author = FakeAuthor(123)
            duplicate = FakeMessage(1, "promoção suspeita", author, general, guild)
            trigger = FakeMessage(2, "promoção suspeita", author, trap, guild)
            unrelated = FakeMessage(3, "outra mensagem", author, general, guild)
            state = StateStore(Path(directory) / "state.json")
            monitor = SpamTrapMonitor(
                FakeBot(guild, logs),
                state,
                owner_id=933780111742877777,
            )
            await monitor.configure(
                guild.id,
                channel_id=trap.id,
                log_channel_id=logs.id,
            )
            for message in (duplicate, trigger, unrelated):
                monitor.observe(message)

            handled = await monitor.handle_message(trigger)

            self.assertTrue(handled)
            self.assertTrue(duplicate.deleted)
            self.assertTrue(trigger.deleted)
            self.assertFalse(unrelated.deleted)
            self.assertEqual(guild.bans[0][0], author.id)
            self.assertEqual(len(logs.sent), 1)
            log_view = logs.sent[0]["view"]
            self.assertIsInstance(log_view, SpamTrapLogView)
            self.assertIsNotNone(log_view.evidence_file)
            events = await state.get_section("spam_trap_events")
            self.assertTrue(events["10:2"]["log_delivered"])

    async def test_log_falls_back_to_v2_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            guild = FakeGuild()
            trap = FakeChannel(200, "detector-scam")
            logs = FakeChannel(300, "logs")
            logs.fail_evidence = True
            guild.text_channels.append(trap)
            author = FakeAuthor(123)
            trigger = FakeMessage(2, "mensagem suspeita", author, trap, guild)
            state = StateStore(Path(directory) / "state.json")
            monitor = SpamTrapMonitor(
                FakeBot(guild, logs),
                state,
                owner_id=933780111742877777,
            )
            await monitor.configure(
                guild.id,
                channel_id=trap.id,
                log_channel_id=logs.id,
            )

            self.assertTrue(await monitor.handle_message(trigger))
            self.assertEqual(len(logs.sent), 1)
            log_view = logs.sent[0]["view"]
            self.assertIsInstance(log_view, SpamTrapLogView)
            self.assertIsNone(log_view.evidence_file)
            delivery = await monitor.last_log_delivery(guild.id)
            self.assertTrue(delivery["delivered"])
            self.assertIn("sem evidência", delivery["detail"])

    async def test_failed_log_delivery_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            guild = FakeGuild()
            trap = FakeChannel(200, "detector-scam")
            logs = FakeChannel(300, "logs")
            logs.fail_all = True
            guild.text_channels.append(trap)
            author = FakeAuthor(123)
            trigger = FakeMessage(2, "mensagem suspeita", author, trap, guild)
            state = StateStore(Path(directory) / "state.json")
            monitor = SpamTrapMonitor(
                FakeBot(guild, logs),
                state,
                owner_id=933780111742877777,
            )
            await monitor.configure(
                guild.id,
                channel_id=trap.id,
                log_channel_id=logs.id,
            )

            self.assertTrue(await monitor.handle_message(trigger))
            delivery = await monitor.last_log_delivery(guild.id)
            self.assertFalse(delivery["delivered"])
            self.assertIn("texto simples", delivery["detail"])
            events = await state.get_section("spam_trap_events")
            self.assertFalse(events["10:2"]["log_delivered"])

    async def test_manual_log_test_sends_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            guild = FakeGuild()
            trap = FakeChannel(200, "detector-scam")
            logs = FakeChannel(300, "logs")
            guild.text_channels.append(trap)
            state = StateStore(Path(directory) / "state.json")
            monitor = SpamTrapMonitor(
                FakeBot(guild, logs),
                state,
                owner_id=933780111742877777,
            )
            await monitor.configure(
                guild.id,
                channel_id=trap.id,
                log_channel_id=logs.id,
            )

            delivered, detail = await monitor.send_test_log(
                guild,
                FakeAuthor(933780111742877777),
                source_channel_id=trap.id,
            )

            self.assertTrue(delivered)
            self.assertIn("evidência", detail)
            self.assertEqual(len(logs.sent), 1)

    async def test_owner_is_never_processed_by_trap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            guild = FakeGuild()
            trap = FakeChannel(200, "detector-scam")
            logs = FakeChannel(300, "logs")
            guild.text_channels.append(trap)
            owner = FakeAuthor(933780111742877777)
            message = FakeMessage(1, "teste", owner, trap, guild)
            monitor = SpamTrapMonitor(
                FakeBot(guild, logs),
                StateStore(Path(directory) / "state.json"),
                owner_id=owner.id,
            )
            await monitor.configure(
                guild.id,
                channel_id=trap.id,
                log_channel_id=logs.id,
            )

            self.assertFalse(await monitor.handle_message(message))
            self.assertFalse(message.deleted)
            self.assertFalse(guild.bans)

    def test_signature_normalizes_case_and_whitespace(self) -> None:
        channel = SimpleNamespace(id=1)
        guild = SimpleNamespace(id=10)
        author = FakeAuthor(1)
        first = SimpleNamespace(
            content="  TESTE   de Spam ",
            attachments=[],
            stickers=[],
            channel=channel,
            guild=guild,
            author=author,
        )
        second = SimpleNamespace(
            content="teste de spam",
            attachments=[],
            stickers=[],
            channel=channel,
            guild=guild,
            author=author,
        )

        self.assertEqual(message_signature(first), message_signature(second))

    def test_evidence_renderer_includes_message_and_image(self) -> None:
        source = Image.new("RGB", (640, 360), (220, 40, 60))
        source_bytes = io.BytesIO()
        source.save(source_bytes, format="PNG")

        evidence = render_spam_trap_evidence(
            display_name="Usuário Teste",
            username="usuario_teste",
            user_id=123,
            channel_name="detector-scam",
            channel_id=200,
            created_at=datetime.now(timezone.utc),
            content="Esta é a mensagem suspeita enviada no canal-isca.",
            attachment_names=["comprovante.png"],
            avatar_bytes=None,
            image_bytes=source_bytes.getvalue(),
        )

        rendered = Image.open(io.BytesIO(evidence))
        self.assertEqual(rendered.format, "PNG")
        self.assertEqual(rendered.width, 1200)
        self.assertGreater(rendered.height, 700)
        self.assertEqual(rendered.getpixel((0, 0)), (237, 66, 69))


if __name__ == "__main__":
    unittest.main()
