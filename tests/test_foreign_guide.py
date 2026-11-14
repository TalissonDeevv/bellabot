from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from website_guard.foreign_guide import ForeignGuide, detect_language


class Perms(SimpleNamespace):
    view_channel = True


class FakeChannel:
    def __init__(self, channel_id: int, name: str, topic: str = "", messages=()) -> None:
        self.id = channel_id
        self.name = name
        self.topic = topic
        self._messages = list(messages)
        self.history_calls = 0

    def permissions_for(self, member):
        return Perms()

    async def pins(self):
        return []

    def history(self, limit=100, oldest_first=False):
        self.history_calls += 1
        messages = [
            SimpleNamespace(clean_content=text, embeds=[], author=SimpleNamespace(display_name=author))
            for author, text in reversed(self._messages)
        ]

        async def gen():
            for item in messages[:limit]:
                yield item

        return gen()


class CapturingAI:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def generate_text(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.answer


def _setup(answer: str = "Hi! This channel is where people share their Discord bots."):
    bots = FakeChannel(10, "bots-showcase", "Mostre o bot que você criou", [
        ("Ana", "olha o bot que eu fiz pra moderar o servidor"),
        ("Rafa", "ficou top, usa discord.py?"),
    ])
    rules = FakeChannel(11, "regras", "Leia antes de postar")
    category = SimpleNamespace(name="Comunidade")
    welcome = FakeChannel(1514971417651839197, "📌—boas-vindas", "Apresente-se aqui", [
        ("Bella", "Seja bem-vindo! Leia as regras e se apresente."),
    ])
    by_id = {bots.id: bots, rules.id: rules, welcome.id: welcome}
    guild = SimpleNamespace(
        id=1467976297689911377, name="Escritório do Talisson", description="Comunidade de tecnologia e negócios",
        member_count=320, rules_channel=rules, by_category=lambda: [(category, [bots, rules, welcome])],
        channels=[bots, rules, welcome], get_channel=by_id.get,
    )
    author = SimpleNamespace(display_name="John", id=5, joined_at=datetime.now(timezone.utc) - timedelta(days=1))
    ai = CapturingAI(answer)
    return ForeignGuide(ai), ai, guild, bots, author


def _message(text: str, guild, channel, author, mentions=()):
    return SimpleNamespace(content=text, guild=guild, channel=channel, author=author, channel_mentions=list(mentions))


class LanguageDetectionTests(unittest.TestCase):
    def test_detects_foreign_languages(self) -> None:
        self.assertEqual(detect_language("what is this server about?"), "en")
        self.assertEqual(detect_language("hi, I'm new here, where can I get help?"), "en")
        self.assertEqual(detect_language("¿Qué es este servidor? Estoy perdido"), "es")
        self.assertEqual(detect_language("bonjour, c'est quoi ce serveur ?"), "fr")
        self.assertEqual(detect_language("hallo, was ist das hier für ein server?"), "de")
        self.assertEqual(detect_language("ciao, cosa è questo canale? sono nuovo"), "it")

    def test_portuguese_is_not_foreign(self) -> None:
        for text in ("o que é esse canal?", "você pode me ajudar?", "oi bella tudo bem", "tô perdido aqui kkkk", "pra que serve isso"):
            self.assertIn(detect_language(text), {"pt", ""}, text)

    def test_unclear_short_text_is_ignored(self) -> None:
        self.assertEqual(detect_language("ok"), "")
        self.assertEqual(detect_language("lol"), "")


class ForeignGuideTests(unittest.IsolatedAsyncioTestCase):
    async def test_explains_channel_using_its_history(self) -> None:
        guide, ai, guild, bots, author = _setup()
        message = _message("what is this channel for?", guild, bots, author)
        language = guide.should_handle(message, message.content, invoked=True)
        self.assertEqual(language, "en")
        answer = await guide.answer(message, message.content, language)
        self.assertIn("share their Discord bots", answer)
        prompt = ai.prompts[0]
        self.assertIn("olha o bot que eu fiz", prompt)       # leu o histórico
        self.assertIn("Mostre o bot que você criou", prompt)  # tópico do canal
        self.assertIn("Escritório do Talisson", prompt)       # visão do servidor
        self.assertIn("inglês", prompt)

    async def test_mentioned_channel_is_read(self) -> None:
        guide, ai, guild, bots, author = _setup()
        general = FakeChannel(12, "general")
        message = _message("what is <#10>?", guild, general, author, mentions=[bots])
        await guide.answer(message, message.content, "en")
        self.assertEqual(bots.history_calls, 1)
        self.assertEqual(general.history_calls, 0)

    async def test_channel_link_is_the_asked_channel(self) -> None:
        guide, ai, guild, bots, author = _setup()
        text = "what is this channel https://discord.com/channels/1467976297689911377/1514971417651839197 about?"
        await guide.answer(_message(text, guild, bots, author), text, "en")
        prompt = ai.prompts[0]
        self.assertIn("CANAL QUE A PESSOA PERGUNTOU:\nCanal #📌—boas-vindas", prompt)
        self.assertIn("Seja bem-vindo", prompt)
        self.assertEqual(bots.history_calls, 0)  # não lê o canal onde ela está

    async def test_raw_mention_and_written_name(self) -> None:
        guide, ai, guild, bots, author = _setup()
        mention = "what is <#1514971417651839197> about?"
        self.assertEqual([c.name for c in guide.asked_channels(_message(mention, guild, bots, author), mention)], ["📌—boas-vindas"])
        written = "what is the boas-vindas channel for?"
        self.assertEqual([c.name for c in guide.asked_channels(_message(written, guild, bots, author), written)], ["📌—boas-vindas"])
        other_server = "https://discord.com/channels/999/1514971417651839197"
        self.assertEqual(guide.asked_channels(_message(other_server, guild, bots, author), other_server), [])

    async def test_channel_summary_is_cached(self) -> None:
        guide, ai, guild, bots, author = _setup()
        message = _message("what is this channel?", guild, bots, author)
        await guide.answer(message, message.content, "en")
        await guide.answer(message, message.content, "en")
        self.assertEqual(bots.history_calls, 1)

    def test_newcomer_lost_without_mention(self) -> None:
        guide, _, guild, bots, author = _setup()
        lost = _message("hello, I'm lost, what is this server?", guild, bots, author)
        self.assertEqual(guide.should_handle(lost, lost.content, invoked=False), "en")
        chatting = _message("this game is so good", guild, bots, author)
        self.assertEqual(guide.should_handle(chatting, chatting.content, invoked=False), "")
        veteran = SimpleNamespace(display_name="Old", id=6, joined_at=datetime.now(timezone.utc) - timedelta(days=200))
        old = _message("what is this server?", guild, bots, veteran)
        self.assertEqual(guide.should_handle(old, old.content, invoked=False), "")

    def test_portuguese_goes_to_normal_flow(self) -> None:
        guide, _, guild, bots, author = _setup()
        message = _message("o que é esse canal?", guild, bots, author)
        self.assertEqual(guide.should_handle(message, message.content, invoked=True), "")

    async def test_ai_down_answers_in_their_language(self) -> None:
        guide, _, guild, bots, author = _setup()

        class Down:
            async def generate_text(self, *args, **kwargs):
                raise RuntimeError("fora")

        guide.ai = Down()
        answer = await guide.answer(_message("¿qué es esto?", guild, bots, author), "¿qué es esto?", "es")
        self.assertIn("Hola", answer)


if __name__ == "__main__":
    unittest.main()
