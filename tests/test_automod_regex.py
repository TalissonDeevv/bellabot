from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from website_guard.discord_automod import (
    AutoModGuildConfig,
    DiscordAutoModCenter,
    _clean_regex_patterns,
    clean_bella_patterns,
    normalize_for_regex,
)
from website_guard.state import StateStore

ZOO = [
    r"(?i)z[o0]{1,2}\s*f[i1]l|zoo\s*(porn|sex)|bestialidade|sexo\s+(com|de)\s+(\S+\s+)?(cachorr|cavalo|[eé]gua|bode|cabra|porco|jumento|anima|dog)",
    r"(?i)\b(c[a4]ch[o0]rr[a-z0-9]*|d[o0]g[a-z0-9]*|c[aã]o|cavalo|[eé]gua|bode|cabra|porco|jumento|animal|pet)\b[\s,.!]+(\S+\s+){0,4}(f[ou]d[eiu]|transa|transo|goz|meteu|metendo|chup|me\s+com|estupr|penetr)",
    r"(?i)\b(f[ou]d[eiu]|transa|transo|gozei|gozar|meti|meter|chupei|chupar|estupr|penetr)[a-z]*[\s,.!]+(\S+\s+){0,4}(c[a4]ch[o0]rr|dog|c[aã]o\b|cavalo|[eé]gua|bode|cabra|porco|jumento|anima|pet\b)",
]
GUILD = 1467976297689911377


def _member(*, staff: bool = False, role_ids=()):
    member = MagicMock(spec=discord.Member)
    member.id = 4242
    member.bot = False
    member.mention = "<@4242>"
    member.__str__ = lambda self: "lisa"
    member.roles = [SimpleNamespace(id=r) for r in role_ids]
    member.guild_permissions = SimpleNamespace(administrator=staff, manage_messages=staff)
    member.timeout = AsyncMock()
    return member


class Channel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self.category_id = None
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


class Messageable(Channel, discord.abc.Messageable):
    async def _get_channel(self):
        return self


class RegexAutoModTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self._dir.name) / "s.json")
        self.center = DiscordAutoModCenter(self.state)
        self.logs = Messageable(999)
        self.where = Channel(50)
        self.guild = SimpleNamespace(id=GUILD, owner_id=1, get_channel=lambda cid: self.logs if cid == 999 else None)
        await self.center.save_config(AutoModGuildConfig(
            GUILD, log_channel_id=999, timeout_duration_seconds=86400, bella_regex_patterns=tuple(ZOO),
        ))

    async def asyncTearDown(self) -> None:
        self._dir.cleanup()

    def _message(self, text: str, member=None):
        message = SimpleNamespace(content=text, guild=self.guild, channel=self.where, author=member or _member(), delete=AsyncMock())
        return message

    def test_comma_is_not_a_separator_anymore(self) -> None:
        self.assertEqual(_clean_regex_patterns(ZOO[1] + "\n" + ZOO[2]), [ZOO[1], ZOO[2]])
        long = "(?i)" + "a" * 300
        self.assertEqual(_clean_regex_patterns([long]), [])          # grande demais pro nativo
        self.assertEqual(clean_bella_patterns([long, "(", ZOO[0]]), [long, ZOO[0]])  # inválido sai

    def test_disguises_are_undone(self) -> None:
        self.assertIn("cachorro", normalize_for_regex("meu 𝓬𝓪𝓬𝓱𝓸𝓻𝓻𝓸"))
        self.assertIn("cachorro", normalize_for_regex("meu ｃａｃｈｏｒｒｏ"))
        self.assertIn("cachorro", normalize_for_regex("meu c a c h o r r o"))
        self.assertIn("cachorro", normalize_for_regex("meu c.a.c.h.o.r.r.o"))
        self.assertIn("cachorro", normalize_for_regex("meu саchоrrо"))   # letras russas
        self.assertIn("cachorro", normalize_for_regex("meu cach​orro"))
        self.assertEqual(normalize_for_regex("é uma boa"), "é uma boa")

    async def test_match_deletes_times_out_warns_and_logs(self) -> None:
        member = _member()
        message = self._message("É uma coisa nojenta, eu deixo meu cachorro me comer. Me adiciona no Telegram", member)
        self.assertTrue(await self.center.check_message(message))
        message.delete.assert_awaited_once()
        member.timeout.assert_awaited_once()
        self.assertEqual(member.timeout.await_args.args[0], timedelta(seconds=86400))
        self.assertTrue(self.where.sent[0]["content"].startswith("<@4242>"))
        embed = self.logs.sent[0]["embed"]
        self.assertIn("cachorro me com", {f.name: f.value for f in embed.fields}["Trecho que casou"])

    async def test_disguised_message_is_caught(self) -> None:
        self.assertTrue(await self.center.check_message(self._message("meu 𝓬𝓪𝓬𝓱𝓸𝓻𝓻𝓸 me 𝓯𝓸𝓭𝓮")))
        self.assertTrue(await self.center.check_message(self._message("meu c a c h o r r o me fode")))

    async def test_normal_and_exempt_messages_pass(self) -> None:
        for text in ("meu cachorro come ração", "o cavalo do jogo é foda", "meu cachorro me lambeu quando cheguei"):
            self.assertFalse(await self.center.check_message(self._message(text)), text)
        bad = "meu cachorro me fode"
        self.assertFalse(await self.center.check_message(self._message(bad, _member(staff=True))))
        config = await self.center.config(GUILD)
        await self.center.save_config(AutoModGuildConfig(**{**config.to_dict(), "exempt_role_ids": [77]}))
        self.assertFalse(await self.center.check_message(self._message(bad, _member(role_ids=[77]))))
        await self.center.save_config(AutoModGuildConfig(**{**config.to_dict(), "allowed_phrases": ["meu cachorro me fode"]}))
        self.assertFalse(await self.center.check_message(self._message(bad)))

    async def test_update_regex_keeps_refused_patterns_for_bella(self) -> None:
        await self.center.save_config(AutoModGuildConfig(GUILD))
        calls = []

        async def fake_sync(guild, *, reason):
            config = await self.center.config(GUILD)
            calls.append(list(config.regex_patterns))
            return "o regex excede o tamanho máximo" if any("bestialidade" in p for p in config.regex_patterns) else ""

        self.center._try_sync = fake_sync
        long = "(?i)" + "|".join(f"palavra{i}" for i in range(60))
        report = await self.center.update_regex(self.guild, ZOO + [long], mode="add", actor="talisson")
        config = await self.center.config(GUILD)
        self.assertEqual(len(config.bella_regex_patterns), 4)          # a Bella guarda todos
        self.assertEqual(config.regex_patterns, tuple(ZOO[1:]))         # nativo só os aceitos
        self.assertIn("o Discord recusou", report)
        self.assertIn("passa de 260", report)
        removed = await self.center.update_regex(self.guild, [ZOO[1]], mode="remove", actor="talisson")
        config = await self.center.config(GUILD)
        self.assertNotIn(ZOO[1], config.bella_regex_patterns)
        self.assertNotIn(ZOO[1], config.regex_patterns)
        self.assertIn("removi", removed)


if __name__ == "__main__":
    unittest.main()
