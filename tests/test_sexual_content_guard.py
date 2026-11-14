from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from website_guard.sexual_content_guard import CASES_SECTION, SexualContentGuard, local_score
from website_guard.state import StateStore

OWNER = 933780111742877777
LOG_CHANNEL = 1570783779574780176
LISA = "É uma coisa nojenta, eu deixo meu cachorro me foder 💦🥵😈\nMe adicione no Telegram: @PuppyQueenn"


class Sent:
    def __init__(self, channel, data) -> None:
        self.channel = channel
        self.data = data
        self.id = len(channel.sent)

    async def edit(self, **kwargs):
        self.data.update(kwargs)


class Channel:
    def __init__(self, channel_id: int, name: str = "canal") -> None:
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs):
        data = {"content": content, **kwargs}
        self.sent.append(data)
        return Sent(self, data)


class AI:
    def __init__(self, violation: bool = True) -> None:
        self.violation = violation
        self.calls: list[str] = []

    async def generate_json(self, prompt, schema, **kwargs):
        self.calls.append("json")
        return {"violation": self.violation, "category": "zoofilia" if self.violation else "nenhum",
                "theme": "ato sexual com animal e divulgação de perfil", "refers_to": "zoofilia e convite para Telegram",
                "severity": 5, "confidence": 0.95}

    async def generate_text(self, prompt, **kwargs):
        mention = prompt.split("começando com ")[1].split(",")[0]
        return f"{mention}, esse tipo de conteúdo não é permitido aqui. O caso já foi encaminhado pra equipe."


def _member(*, admin: bool = False):
    member = MagicMock(spec=discord.Member)
    member.id = 4242
    member.bot = False
    member.mention = "<@4242>"
    member.display_name = "Lisa"
    member.__str__ = lambda self: "lisa#0001"
    member.roles = []
    member.guild_permissions = SimpleNamespace(administrator=admin, manage_messages=admin, moderate_members=admin)
    member.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    member.joined_at = datetime.now(timezone.utc) - timedelta(hours=1)
    member.timeout = AsyncMock()
    return member


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self._dir.name) / "s.json")
        self.log_channel = Channel(LOG_CHANNEL, "privado")
        self.general = Channel(77, "💬-bate-papo")
        self.where = Channel(88, "projetos")
        self.guild = SimpleNamespace(id=1, owner_id=1, text_channels=[self.where, self.general],
                                     get_channel=lambda cid: None, ban=AsyncMock(), get_member=lambda uid: self.member)
        self.member = _member()
        self.member.guild = self.guild
        self.order: list[str] = []
        self.decisions = Channel(1551649724598063125, "logs-punicoes")
        self.bot = SimpleNamespace(get_channel=lambda cid: {LOG_CHANNEL: self.log_channel, 1551649724598063125: self.decisions}.get(cid),
                                   get_guild=lambda gid: self.guild)
        self.ai = AI()
        self.guard = SexualContentGuard(self.bot, self.state, ai_service=self.ai, owner_id=OWNER, log_channel_id=LOG_CHANNEL)
        original_classify = self.guard._classify

        async def tracked(text, reasons):
            self.order.append("analise")
            return await original_classify(text, reasons)

        self.guard._classify = tracked

    async def asyncTearDown(self) -> None:
        self._dir.cleanup()

    def _message(self, text: str, author=None):
        async def delete():
            self.order.append("apagou")

        return SimpleNamespace(content=text, author=author or self.member, guild=self.guild, channel=self.where, delete=delete)

    def test_disguises_are_seen_through(self) -> None:
        disguised = (
            "𝓯𝓾𝓭𝓮𝓻 comigo, me chama no zap",          # fonte cursiva
            "𝐟𝐮𝐝𝐞𝐫 a noite toda kkk @Fulana_hot",        # negrito matemático
            "ｆｕｄｅｒ hoje? link na bio",                # largura total
            "🇫🇺🇩🇪🇷 gostoso, me add no telegram",         # letras de bandeira
            "🅵🆄🅳🅴🆁 muito, chama no pv",                 # letras em emoji
            "f.u.d.3.r gostoso, me chama no zap",         # pontos + número
            "f u d e r gostoso, me chama no zap",         # espaços
            "f🍆u🍆d🍆e🍆r me chama no pv",                  # emojis no meio
            "fu​der escondido, me chama no zap",     # caractere invisível
            "p&ck de nud3s, link na bio",                 # símbolo no lugar de letra
            "рurаriа ? não, putar|a mesmo, vem no pv",     # letras russas + |
            "b|_|c3t@ molhada, me add no tg",             # símbolos variados
        )
        for text in disguised:
            self.assertGreaterEqual(local_score(text)[0], 3, text)

    def test_innocent_text_is_not_glued(self) -> None:
        for text in ("fofo de ramos", "ele explica muito bem", "a computaria fica pronta amanhã",
                     "o # do canal é #geral e o @ é @talisson", "tenho 100% de certeza & pronto"):
            self.assertLess(local_score(text)[0], 3, text)

    def test_local_filter(self) -> None:
        score, reasons = local_score(LISA)
        self.assertGreaterEqual(score, 8)
        self.assertTrue(any("zoofilia" in r for r in reasons))
        self.assertGreaterEqual(local_score("f.u.u.d.e.r gostoso, me chama no zap")[0], 3)
        self.assertGreaterEqual(local_score("vendo pack de nud3s, link na bio")[0], 3)
        for text in ("esse projeto é foda demais", "que porra de erro é esse no deploy", "me chama no pv pra falar do orçamento",
                     "fiz um bot de telegram ontem", "meu cachorro comeu meu fone kkkk"):
            self.assertLess(local_score(text)[0], 3, text)

    async def test_owner_sees_case_during_analysis(self) -> None:
        seen = {}
        original = self.guard._classify

        async def peek(text, reasons):
            seen["card"] = self.log_channel.sent[0]["embed"].title if self.log_channel.sent else None
            return await original(text, reasons)

        self.guard._classify = peek
        await self.guard.handle_message(self._message(LISA))
        self.assertIn("Analisando", seen["card"])
        self.assertIn("bloqueado", self.log_channel.sent[0]["embed"].title)

    async def test_deletes_first_then_ai_then_punishes(self) -> None:
        handled = await self.guard.handle_message(self._message(LISA))
        self.assertTrue(handled)
        self.assertEqual(self.order[:2], ["apagou", "analise"])
        self.member.timeout.assert_awaited_once()
        self.assertEqual(self.member.timeout.await_args.args[0], timedelta(hours=168))
        warning = self.general.sent[0]["content"]
        self.assertTrue(warning.startswith("<@4242>"))
        self.assertNotIn("cachorro", warning)
        # Um card só no canal privado: criado durante a análise e atualizado no fim.
        log = self.log_channel.sent[0]
        self.assertIn("caso pra você decidir", log["content"])
        self.assertIn("caso pronto", self.log_channel.sent[1]["content"])
        fields = {f.name: f.value for f in log["embed"].fields}
        self.assertIn("zoofilia", fields["Análise da IA"])
        self.assertIn("apagada na hora", fields["O que eu fiz"])
        self.assertEqual(len(log["view"].children), 3)
        case = next(iter((await self.state.get_section(CASES_SECTION)).values()))
        self.assertEqual(case["status"], "aguardando decisão")

    async def test_false_alarm_is_restored(self) -> None:
        self.guard.ai = AI(violation=False)
        text = "a aula de educação sexual da escola foi ótima, me chama no pv"
        self.assertTrue(await self.guard.handle_message(self._message(text)))
        self.assertEqual(self.order[0], "apagou")
        self.member.timeout.assert_not_awaited()
        self.assertIn("educação sexual", self.where.sent[0]["content"])
        self.assertIn("Falso alarme", self.log_channel.sent[0]["embed"].title)

    async def test_ai_down_keeps_deleted_and_logs_without_mute(self) -> None:
        class Down:
            async def generate_json(self, *a, **k):
                raise RuntimeError("fora")

        self.guard.ai = Down()
        await self.guard.handle_message(self._message("vendo pack, me chama no zap 🔥🍆"))
        self.assertEqual(self.order[0], "apagou")
        self.member.timeout.assert_not_awaited()
        self.assertIn("IA estava indisponível", {f.name: f.value for f in self.log_channel.sent[0]["embed"].fields}["O que eu fiz"])

    async def test_staff_and_clean_messages_are_ignored(self) -> None:
        self.assertFalse(await self.guard.handle_message(self._message(LISA, author=_member(admin=True))))
        self.assertFalse(await self.guard.handle_message(self._message("bom dia pessoal, alguém sabe deploy na contabo?")))
        self.assertEqual(self.order, [])

    async def test_owner_decides_ban(self) -> None:
        await self.guard.handle_message(self._message(LISA))
        case_id = next(iter(await self.state.get_section(CASES_SECTION)))
        embed = self.log_channel.sent[0]["embed"]
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=OWNER, mention=f"<@{OWNER}>", guild_permissions=None, __str__=lambda s: "talisson"),
            message=SimpleNamespace(embeds=[embed]),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
        )
        await self.guard.decide(interaction, "ban", case_id)
        self.guild.ban.assert_awaited_once()
        case = (await self.state.get_section(CASES_SECTION))[case_id]
        self.assertEqual(case["status"], "banido")
        decision = self.decisions.sent[0]["embed"]
        self.assertIn("banido", decision.title)
        self.assertIn("zoofilia", {f.name: f.value for f in decision.fields}["Motivo"])
        stranger = SimpleNamespace(user=SimpleNamespace(id=5, guild_permissions=SimpleNamespace(ban_members=False, administrator=False)),
                                   response=SimpleNamespace(send_message=AsyncMock()))
        await self.guard.decide(stranger, "unmute", case_id)
        self.assertIn("só o Talisson", stranger.response.send_message.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
