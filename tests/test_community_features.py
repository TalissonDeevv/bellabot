from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import discord

from website_guard import community_features as cf
from website_guard.community_features import CommunityFeatures, MusicPanelView
from website_guard.state import StateStore

GUILD_ID = 1467976297689911377
CARD_CHANNEL = 1467981828559212544


class Member:
    def __init__(self, member_id: int, name: str, *, blocked: bool = False, guild=None) -> None:
        self.id = member_id
        self.display_name = name
        self.name = name.casefold().replace(" ", "")
        self.global_name = name
        self.bot = False
        self.blocked = blocked
        self.sent: list[str] = []
        self.guild = guild
        self.mention = f"<@{member_id}>"
        self.joined_at = datetime.now(timezone.utc) - timedelta(days=200)
        self.display_avatar = SimpleNamespace(replace=lambda **kw: SimpleNamespace(read=self._no_avatar))

    async def _no_avatar(self):
        raise RuntimeError("sem avatar no teste")

    async def send(self, text=None, **kwargs):
        if self.blocked:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="x"), "dm fechada")
        self.sent.append(text)
        return SimpleNamespace(id=len(self.sent))


class Channel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        return SimpleNamespace(id=1000 + len(self.sent))


class AI:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.prompts: list[str] = []

    async def generate_json(self, prompt, schema, **kwargs):
        self.prompts.append(prompt)
        if "ELOGIO ANÔNIMO" in prompt:
            if not self.ok:
                return {"ok": False, "reason": "parece uma ironia maldosa"}
            return {"ok": True, "message": "Você explica as coisas com uma paciência e clareza que fazem diferença pra todo mundo!"}
        return {"opener": "que ideia boa!", "questions": ["quem usaria?", "que problema resolve?", "como ganharia dinheiro?"]}

    async def generate_text(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return "Forte: resolve uma dor real.\n```python\nprint('x')\n```\nRiscos: concorrência. Comece validando com 10 pessoas."


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self._dir.name) / "s.json")
        self.card_channel = Channel(CARD_CHANNEL)
        self.guild = SimpleNamespace(id=GUILD_ID, name="Escritório do Talisson", members=[])
        self.sender = Member(1, "Maria", guild=self.guild)
        self.fulano = Member(2, "Fulano", guild=self.guild)
        self.guild.members = [self.sender, self.fulano, Member(3, "Ana", guild=self.guild), Member(4, "Ana", guild=self.guild)]
        self.guild.get_member = lambda uid: next((m for m in self.guild.members if m.id == uid), None)
        self.bot = SimpleNamespace(
            get_guild=lambda gid: self.guild if gid == GUILD_ID else None,
            get_channel=lambda cid: self.card_channel if cid == CARD_CHANNEL else None,
        )
        self.ai = AI()
        self.features = CommunityFeatures(self.bot, self.state, ai_service=self.ai)

    async def asyncTearDown(self) -> None:
        self._dir.cleanup()

    def dm(self, text: str, author=None):
        return SimpleNamespace(content=text, author=author or self.sender, guild=None, channel=SimpleNamespace(id=50), reference=None, id=9)

    def msg(self, text: str, author=None, channel_id: int = 60, reference=None):
        return SimpleNamespace(content=text, author=author or self.sender, guild=self.guild, channel=Channel(channel_id),
                               reference=reference, id=10)


class ComplimentTests(Base):
    async def test_anonymous_compliment_is_rewritten_and_delivered(self) -> None:
        reply = await self.features.handle(self.dm("manda um elogio anônimo pro Fulano dizendo que ele explica muito bem"), "manda um elogio anônimo pro Fulano dizendo que ele explica muito bem", invoked=False)
        self.assertIn("entregue", reply.text)
        self.assertEqual(len(self.fulano.sent), 1)
        delivered = self.fulano.sent[0]
        self.assertIn("elogio anônimo", delivered)
        self.assertIn("paciência e clareza", delivered)
        self.assertNotIn("Maria", delivered)

    async def test_mention_works_in_dm(self) -> None:
        self.fulano.id = 200000000000000002
        text = "manda um elogio anônimo pro <@200000000000000002> que ele é muito gente boa"
        await self.features.handle(self.dm(text), text, invoked=False)
        self.assertEqual(len(self.fulano.sent), 1)

    async def test_asks_what_to_say_then_delivers(self) -> None:
        first = await self.features.handle(self.dm("manda um elogio anônimo pro Fulano"), "manda um elogio anônimo pro Fulano", invoked=False)
        self.assertIn("o que você quer dizer", first.text)
        self.assertEqual(self.fulano.sent, [])
        await self.features.handle(self.dm("ele sempre ajuda todo mundo no servidor"), "ele sempre ajuda todo mundo no servidor", invoked=False)
        self.assertEqual(len(self.fulano.sent), 1)

    async def test_offense_is_blocked(self) -> None:
        reply = await self.features.handle(self.dm("manda um elogio anônimo pro Fulano dizendo que ele é um idiota"), "manda um elogio anônimo pro Fulano dizendo que ele é um idiota", invoked=False)
        self.assertIn("não dá pra eu entregar", reply.text)
        self.assertEqual(self.fulano.sent, [])
        self.features.ai = AI(ok=False)
        reply = await self.features.handle(self.dm("manda um elogio anônimo pro Fulano dizendo que ele é ótimo... só que não"), "manda um elogio anônimo pro Fulano dizendo que ele é ótimo... só que não", invoked=False)
        self.assertIn("ironia", reply.text)
        self.assertEqual(self.fulano.sent, [])

    async def test_limits_ambiguity_self_and_closed_dm(self) -> None:
        text = "manda um elogio anônimo pro Fulano dizendo que ele arrasa"
        await self.features.handle(self.dm(text), text, invoked=False)
        again = await self.features.handle(self.dm(text), text, invoked=False)
        self.assertIn("já mandou um elogio pro Fulano hoje", again.text)
        ambiguous = await self.features.handle(self.dm("manda um elogio anônimo pra Ana que ela é demais"), "manda um elogio anônimo pra Ana que ela é demais", invoked=False)
        self.assertIn("mais de uma pessoa", ambiguous.text)
        itself = await self.features.handle(self.dm("manda um elogio anônimo pra Maria que ela é demais"), "manda um elogio anônimo pra Maria que ela é demais", invoked=False)
        self.assertIn("você mesmo", itself.text)
        self.fulano.blocked = True
        self.fulano.id = 22
        self.guild.members.append(Member(5, "Ciclano", blocked=True, guild=self.guild))
        closed = await self.features.handle(self.dm("manda um elogio anônimo pro Ciclano que ele é muito legal"), "manda um elogio anônimo pro Ciclano que ele é muito legal", invoked=False)
        self.assertIn("DM do Ciclano tá fechada", closed.text)

    async def test_in_public_channel_points_to_dm(self) -> None:
        reply = await self.features.handle(self.msg("bella manda um elogio anônimo pro Fulano"), "manda um elogio anônimo pro Fulano", invoked=True)
        self.assertIn("DM", reply.text)
        self.assertEqual(self.fulano.sent, [])


class IdeaReviewTests(Base):
    async def test_questions_then_analysis_without_code(self) -> None:
        first = await self.features.handle(self.msg("tenho uma ideia de app que organiza a fila do barbeiro"), "tenho uma ideia de app que organiza a fila do barbeiro", invoked=True)
        self.assertIn("quem usaria?", first.text)
        second = await self.features.handle(self.msg("donos de barbearia pequena, hoje usam whatsapp, cobraria mensalidade"), "donos de barbearia pequena, hoje usam whatsapp, cobraria mensalidade", invoked=False)
        self.assertIn("Riscos", second.text)
        self.assertNotIn("```", second.text)
        self.assertIn("organiza a fila do barbeiro", self.ai.prompts[-1])
        # Sessão encerrada: a próxima mensagem volta ao fluxo normal.
        self.assertIsNone(await self.features.handle(self.msg("valeu bella"), "valeu bella", invoked=False))


class MusicTests(Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        catalog = {
            "blinding lights the weeknd": {"id": "1", "title": "Blinding Lights", "artist": "The Weeknd", "album": "After Hours", "genre": "Pop", "artwork": "", "url": "", "preview": ""},
            "grateful neffex": {"id": "2", "title": "Grateful", "artist": "NEFFEX", "album": "Grateful", "genre": "Hip-Hop", "artwork": "", "url": "", "preview": ""},
        }

        async def search(query):
            return catalog.get(query.casefold())

        self.features._search_song = search

    async def test_recommend_vote_rank_and_feedback(self) -> None:
        added = await self.features.handle(self.msg("bella, indico a música Blinding Lights do The Weeknd"), "indico a música Blinding Lights do The Weeknd", invoked=True)
        self.assertIn("Blinding Lights", added.text)
        await self.features.handle(self.msg("indico a música Grateful do NEFFEX", author=self.fulano), "indico a música Grateful do NEFFEX", invoked=True)
        await self.features.handle(self.msg("recomendo a música Grateful do NEFFEX"), "recomendo a música Grateful do NEFFEX", invoked=True)
        ranking = await self.features.ranking()
        self.assertEqual(ranking[0]["title"], "Grateful")
        self.assertEqual(len(ranking[0]["votes"]), 2)

        channel = Channel(77)
        sent = await self.features.post_playlist(channel)
        self.assertEqual(channel.sent[0]["file"].filename, "playlist.png")
        reply_to_panel = SimpleNamespace(message_id=sent.id)
        feedback = await self.features.handle(self.msg("a do weeknd é muito boa pra treinar", reference=reply_to_panel), "a do weeknd é muito boa pra treinar", invoked=True)
        self.assertIn("Blinding Lights", feedback.text)
        by_position = await self.features.handle(self.msg("a 1 é viciante", reference=reply_to_panel), "a 1 é viciante", invoked=True)
        self.assertIn("Grateful", by_position.text)
        ranking = await self.features.ranking()
        self.assertEqual(len(ranking[1]["feedback"]), 1)

    async def test_panel_image_and_view(self) -> None:
        for index in range(7):
            song = {"id": str(index), "title": f"Música {index}", "artist": "Artista", "album": "", "genre": "", "artwork": "", "url": "", "preview": ""}

            async def fixed(query, song=song):
                return song

            self.features._search_song = fixed
            await self.features.add_song(self.msg("indico a música teste"), "indico a música teste")
        buffer, pages, chunk = await self.features.render_playlist(cf._week_key(), 1)
        self.assertEqual(pages, 2)
        self.assertEqual(len(chunk), 2)
        self.assertTrue(buffer.getvalue().startswith(b"\x89PNG"))
        view = MusicPanelView(self.features, chunk=chunk, page=1, pages=2)
        self.assertFalse(view.previous.disabled)
        self.assertTrue(view.next.disabled)
        self.assertEqual(len(view.vote_select.options), 2)

    def test_song_query_cleanup(self) -> None:
        self.assertEqual(CommunityFeatures._song_query("bella, indico a música Blinding Lights do The Weeknd pra playlist"), "Blinding Lights The Weeknd")


class MemberCardTests(Base):
    async def test_card_goes_to_card_channel(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        await self.state.update_section("server_message_statistics", str(GUILD_ID), {"daily": {today: {"users": {"1": 600}}}})
        await self.features.handle(self.msg("bella, meus gostos: python, anime e futebol"), "meus gostos: python, anime e futebol", invoked=True)
        reply = await self.features.handle(self.msg("bella, meu perfil"), "meu perfil", invoked=True)
        self.assertIn(f"<#{CARD_CHANNEL}>", reply.text)
        sent = self.card_channel.sent[0]
        self.assertEqual(sent["file"].filename, "cartao.png")
        self.assertIn("<@1>", sent["content"])
        stats = await self.features.member_stats(self.sender)
        self.assertGreater(stats["level"], 1)
        self.assertIn("Tagarela", stats["badges"])
        self.assertIn("Veterano", stats["badges"])
        self.assertEqual(stats["likes"][:3], ["python", "anime", "futebol"])


if __name__ == "__main__":
    unittest.main()
