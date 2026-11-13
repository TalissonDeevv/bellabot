from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from website_guard.operational_memory import OperationalMemory
from website_guard.state import StateStore


class FakeBot:
    def __init__(self) -> None:
        self.user = SimpleNamespace(id=100)
        self.channels: dict[int, object] = {}

    def get_channel(self, channel_id: int) -> object | None:
        return self.channels.get(channel_id)


class FakeAI:
    available = True

    def __init__(self, *, violation: bool = True, confidence: float = 0.9) -> None:
        self.violation = violation
        self.confidence = confidence
        self.prompts: list[str] = []

    async def generate_json(self, prompt: str, schema: dict, **kwargs) -> dict:
        self.prompts.append(prompt)
        return {
            "violation": self.violation,
            "confidence": self.confidence,
            "reason": "Violação semântica detectada no teste.",
        }


class FakeRuleAI(FakeAI):
    async def generate_json(self, prompt: str, schema: dict, **kwargs) -> dict:
        self.prompts.append(prompt)
        return {
            "name": "Ajuda de programação",
            "policy_type": "themed_help_channel",
            "channel_id": 1514971351427977316,
            "log_channel_id": 0,
            "topic": "Programação, código, linguagens e tecnologias.",
            "required_context": [
                "Linguagem ou tecnologia",
                "O que está tentando fazer",
                "Código relevante",
                "Mensagem de erro",
                "O que já tentou",
            ],
            "auto_assist": True,
            "condition_type": "insufficient_context",
            "keywords": [],
            "action": "delete",
            "duration_seconds": 0,
            "delete_message": True,
            "guidance": (
                "Envie linguagem, objetivo, código, mensagem de erro e tentativas."
            ),
            "confidence": 0.99,
        }


class FakeAuthor:
    def __init__(self, user_id: int = 933780111742877777) -> None:
        self.id = user_id
        self.bot = False
        self.mention = f"<@{user_id}>"
        self.sent: list[str] = []
        self.timeouts: list[tuple[object, str]] = []
        self.kicked_reason: str | None = None

    async def send(self, content: str) -> None:
        self.sent.append(content)

    async def timeout(self, until: object, *, reason: str) -> None:
        self.timeouts.append((until, reason))

    async def kick(self, *, reason: str) -> None:
        self.kicked_reason = reason


class FakeChannel:
    def __init__(
        self,
        channel_id: int = 1514971351427977316,
        name: str = "duvidas",
    ) -> None:
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.sent: list[str] = []

    async def send(self, content: str, **kwargs) -> None:
        self.sent.append(content)


class FakeGuild:
    def __init__(self, guild_id: int = 10) -> None:
        self.id = guild_id
        self.bans: list[tuple[object, str]] = []
        self.channels: dict[int, object] = {}

    async def ban(self, user: object, *, reason: str, **kwargs) -> None:
        self.bans.append((user, reason))

    def get_channel(self, channel_id: int) -> object | None:
        return self.channels.get(channel_id)


class FakeMessage:
    def __init__(
        self,
        content: str,
        *,
        author_id: int = 933780111742877777,
        channel_id: int = 1514971351427977316,
        mentions: list[object] | None = None,
        guild: FakeGuild | None = None,
        attachments: list[object] | None = None,
    ) -> None:
        self.content = content
        self.author = FakeAuthor(author_id)
        self.guild = guild or FakeGuild()
        self.channel = FakeChannel(channel_id)
        self.guild.channels[self.channel.id] = self.channel
        self.channel_mentions = [self.channel] if f"<#{channel_id}>" in content else []
        self.mentions = mentions or []
        self.attachments = attachments or []
        self.embeds = []
        self.id = 55
        self.jump_url = f"https://discord.com/channels/{self.guild.id}/{channel_id}/{self.id}"
        self.deleted = False
        self.replies: list[str] = []

    async def reply(self, content: str, **kwargs) -> None:
        self.replies.append(content)

    async def delete(self) -> None:
        self.deleted = True


class OperationalMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_can_save_and_list_channel_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            memory = OperationalMemory(state, FakeBot())
            message = FakeMessage(
                "Bella, o canal <#1514971351427977316> é exclusivamente para "
                "dúvidas direcionadas a você. Se alguém enviar mensagens fora de "
                "contexto ou que não sejam dúvidas, apague a mensagem e oriente o "
                "usuário corretamente."
            )

            handled = await memory.handle_instruction(message)
            list_message = FakeMessage("Bella, liste todas as regras salvas.")
            await memory.handle_instruction(list_message)

            rules = await memory.rules_for(10, channel_id=1514971351427977316)
            self.assertTrue(handled)
            self.assertEqual(len(rules), 1)
            self.assertEqual(rules[0].policy_type, "question_only_channel")
            self.assertTrue(rules[0].delete_noncompliant)
            self.assertTrue(list_message.author.sent)

    async def test_non_owner_instruction_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            memory = OperationalMemory(state, FakeBot())
            message = FakeMessage(
                "Bella, salve a seguinte regra: nunca faça isso.",
                author_id=123,
            )

            handled = await memory.handle_instruction(message)
            rules = await memory.rules_for(10)

            self.assertTrue(handled)
            self.assertFalse(rules)
            self.assertFalse(message.replies)

    async def test_question_only_rule_deletes_non_question_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            memory = OperationalMemory(state, FakeBot())
            owner_message = FakeMessage(
                "Bella, o canal <#1514971351427977316> é exclusivamente para "
                "dúvidas direcionadas a você. Se alguém enviar mensagens fora de "
                "contexto ou que não sejam dúvidas, apague a mensagem."
            )
            await memory.handle_instruction(owner_message)
            user_message = FakeMessage("bom dia", author_id=456)

            handled = await memory.apply(user_message)

            self.assertTrue(handled)
            self.assertTrue(user_message.deleted)
            self.assertIn("exclusivamente", user_message.channel.sent[0])

    async def test_image_rule_bans_and_reports_to_log_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            bot = FakeBot()
            memory = OperationalMemory(state, bot)
            guild = FakeGuild()
            target_channel = FakeChannel(1517910759357550663)
            log_channel = FakeChannel(1467986858167369980)
            guild.channels[target_channel.id] = target_channel
            guild.channels[log_channel.id] = log_channel
            bot.channels[log_channel.id] = log_channel

            rule = await memory.register_text_rule(
                guild_id=guild.id,
                guild=guild,
                author_id=933780111742877777,
                content=(
                    "no canal <#1517910759357550663> se alguem enviar imagem "
                    "aplique banimento permanente e mande relatorio no canal "
                    "<#1467986858167369980> com motivo"
                ),
            )
            user_message = FakeMessage(
                "",
                author_id=456,
                channel_id=target_channel.id,
                guild=guild,
                attachments=[
                    SimpleNamespace(
                        filename="promo.png",
                        content_type="image/png",
                    )
                ],
            )

            handled = await memory.apply(user_message)

            self.assertTrue(handled)
            self.assertEqual(rule.policy_type, "moderation_rule")
            self.assertEqual(rule.condition_type, "image")
            self.assertEqual(rule.moderation_action, "ban_permanent")
            self.assertEqual(rule.log_channel_id, log_channel.id)
            self.assertTrue(user_message.deleted)
            self.assertEqual(guild.bans[0][0].id, 456)
            self.assertIn("ban_permanent", log_channel.sent[0])
            self.assertIn(str(user_message.id), log_channel.sent[0])

    async def test_link_rule_applies_timeout_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            bot = FakeBot()
            memory = OperationalMemory(state, bot)
            guild = FakeGuild()
            target_channel = FakeChannel(111, "links")
            log_channel = FakeChannel(222, "logs-gerais")
            guild.channels[target_channel.id] = target_channel
            guild.channels[log_channel.id] = log_channel
            bot.channels[log_channel.id] = log_channel

            rule = await memory.register_text_rule(
                guild_id=guild.id,
                guild=guild,
                author_id=933780111742877777,
                content=(
                    "no canal #links se alguem enviar link aplique timeout de "
                    "1 hora e mande relatorio no canal #logs-gerais"
                ),
            )
            user_message = FakeMessage(
                "veja https://example.com",
                author_id=789,
                channel_id=target_channel.id,
                guild=guild,
            )

            handled = await memory.apply(user_message)

            self.assertTrue(handled)
            self.assertEqual(rule.condition_type, "link")
            self.assertEqual(rule.moderation_action, "timeout")
            self.assertEqual(rule.duration_seconds, 3600)
            self.assertEqual(rule.log_channel_id, log_channel.id)
            self.assertTrue(user_message.deleted)
            self.assertTrue(user_message.author.timeouts)
            self.assertIn("timeout", log_channel.sent[0])

    async def test_semantic_rule_uses_ai_before_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            bot = FakeBot()
            ai = FakeAI(violation=True, confidence=0.95)
            memory = OperationalMemory(state, bot, ai)
            guild = FakeGuild()
            target_channel = FakeChannel(333, "chat")
            log_channel = FakeChannel(444, "logs")
            guild.channels[target_channel.id] = target_channel
            guild.channels[log_channel.id] = log_channel
            bot.channels[log_channel.id] = log_channel

            rule = await memory.register_text_rule(
                guild_id=guild.id,
                guild=guild,
                author_id=933780111742877777,
                content=(
                    "no canal #chat se alguem fizer spam agressivo apague e "
                    "mande relatorio no canal #logs"
                ),
            )
            user_message = FakeMessage(
                "compra agora compra agora compra agora",
                author_id=321,
                channel_id=target_channel.id,
                guild=guild,
            )

            handled = await memory.apply(user_message)

            self.assertTrue(handled)
            self.assertEqual(rule.condition_type, "semantic")
            self.assertTrue(ai.prompts)
            self.assertTrue(user_message.deleted)
            self.assertIn("delete", log_channel.sent[0])

    async def test_themed_help_rule_deletes_incomplete_and_auto_assists_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            ai = FakeRuleAI()
            memory = OperationalMemory(state, FakeBot(), ai)
            owner_message = FakeMessage(
                "Bella, o canal <#1514971351427977316> é para ajuda relacionada "
                "a programação e códigos. Ao pedir ajuda, a pessoa deve informar "
                "linguagem/tecnologia, o que está tentando fazer, código relevante, "
                "mensagem de erro e o que já tentou. Você deve ajudar a pessoa. "
                "Se mandar algo sem contexto, apague."
            )

            handled = await memory.handle_instruction(owner_message)
            rules = await memory.rules_for(
                10,
                channel_id=1514971351427977316,
            )
            vague_message = FakeMessage("me ajuda", author_id=456)
            vague_handled = await memory.apply(vague_message)
            complete_message = FakeMessage(
                "Uso Python e estou tentando criar uma API. O código é "
                "```py\\nprint(valor)\\n```. A mensagem de erro é NameError. "
                "Já tentei declarar a variável, mas continua.",
                author_id=456,
            )
            complete_handled = await memory.apply(complete_message)

            self.assertTrue(handled)
            self.assertEqual(rules[0].policy_type, "themed_help_channel")
            self.assertTrue(rules[0].auto_assist)
            self.assertEqual(len(rules[0].required_context), 5)
            self.assertTrue(vague_handled)
            self.assertTrue(vague_message.deleted)
            self.assertIn("Linguagem ou tecnologia", vague_message.channel.sent[0])
            self.assertFalse(complete_handled)
            self.assertFalse(complete_message.deleted)
            self.assertTrue(
                await memory.should_auto_assist(
                    10,
                    1514971351427977316,
                )
            )


if __name__ == "__main__":
    unittest.main()
