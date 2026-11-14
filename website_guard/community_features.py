"""Funções para o pessoal do servidor interagir com a Bella.

- Elogio anônimo (pedido na DM da Bella, com filtro contra ofensa).
- Revisão de ideia (perguntas, riscos e por onde começar, sem código).
- Playlist da semana (indicações, votos, painel em imagem com páginas e
  feedback por resposta ao painel).
- Cartão de membro (imagem com nível, tempo no servidor, conquistas e gostos).
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from unicodedata import combining, normalize
from urllib.parse import quote_plus

import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont, ImageOps

LOGGER = logging.getLogger(__name__)

COMPLIMENTS_SECTION = "community_compliments"
IDEA_SESSIONS_SECTION = "community_idea_sessions"
MUSIC_SECTION = "community_music"
PANELS_SECTION = "community_music_panels"
PROFILE_SECTION = "community_profiles"
STATISTICS_SECTION = "server_message_statistics"

DEFAULT_GUILD_ID = 1467976297689911377
DEFAULT_CARD_CHANNEL_ID = 1467981828559212544
PAGE_SIZE = 5

COMPLIMENT_RE = re.compile(r"\belogio\b")
IDEA_RE = re.compile(
    r"\b(?:tenho|tive|to com|estou com|tô com) uma ideia\b|\bideia de (?:um |uma )?(?:app|aplicativo|site|sistema|bot|projeto|"
    r"produto|negocio|saas|startup|jogo|servico|plataforma)\b|\b(?:revisa|revise|avalia|avalie|analisa|analise) (?:a )?minha ideia\b|"
    r"\bo que (?:voce|vc) acha da minha ideia\b"
)
MUSIC_ADD_RE = re.compile(
    r"\b(?:indico|indica|indicacao|recomendo|recomenda|recomendacao|sugiro|sugestao|adiciona|adicione|coloca|coloque|bota|bote|"
    r"poe|ponha)\b.*\b(?:musica|som|cancao|track|playlist)\b|\bmusica do dia\s*[:\-]"
)
PLAYLIST_RE = re.compile(r"\b(?:playlist da semana|ranking (?:das|de) musicas|musicas da semana|top musicas|painel de musicas)\b")
PROFILE_RE = re.compile(r"\b(?:meu perfil|meu cartao|meu card|cartao de membro|meu nivel)\b")
LIKES_RE = re.compile(r"\b(?:meus gostos|no meu perfil|meu perfil)\b.*\b(?:curto|gosto|gostos|amo|curtir)\b|^meus gostos\s*[:\-]")
OFFENSIVE = {
    "idiota", "burro", "burra", "imbecil", "otario", "otaria", "lixo", "feio", "feia", "gordo", "gorda", "nojento",
    "nojenta", "vagabundo", "vagabunda", "puta", "porra", "caralho", "merda", "fdp", "desgraca", "retardado",
    "retardada", "macaco", "viado", "bicha", "corno", "inutil", "fracassado", "fracassada", "morre", "morra", "se mata",
}


def _norm(value: str) -> str:
    decomposed = normalize("NFKD", str(value or "").casefold())
    return " ".join("".join(c for c in decomposed if not combining(c)).split())


def _clean(content: str) -> str:
    text = re.sub(r"<@!?\d+>", " ", content or "")
    text = re.sub(r"(?i)^\s*bella\s*[,:]?\s*", "", text.strip())
    return " ".join(text.split())


def _strip_code(text: str) -> str:
    text = re.sub(r"```.*?```", "(sem código por aqui ♡)", text or "", flags=re.S)
    return text.replace("```", "")


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"]
        if bold
        else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"]
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int) -> str:
    text = str(text or "")
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def _rounded(image: Image.Image, size: int, radius: int) -> Image.Image:
    image = ImageOps.fit(image.convert("RGBA"), (size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size, size), radius=radius, fill=255)
    image.putalpha(mask)
    return image


async def _download_image(url: str) -> Image.Image | None:
    if not url:
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                data = await response.read()
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:  # noqa: BLE001
        return None


def _week_key(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).strftime("%G-W%V")


@dataclass
class Reply:
    text: str = ""
    handled: bool = True


class CommunityFeatures:
    def __init__(self, bot: Any, state: Any, *, ai_service: Any | None = None) -> None:
        self.bot = bot
        self.state = state
        self.ai = ai_service
        self.guild_id = int(os.getenv("COMMUNITY_GUILD_ID", "") or DEFAULT_GUILD_ID)
        self.card_channel_id = int(os.getenv("MEMBER_CARD_CHANNEL_ID", "") or DEFAULT_CARD_CHANNEL_ID)
        self.music_channel_id = int(os.getenv("MUSIC_CHANNEL_ID", "") or 0)
        self.compliments_per_day = int(os.getenv("COMPLIMENTS_PER_DAY", "5") or 5)

    # ---------------------------------------------------------------- roteador
    async def handle(self, message: Any, content: str, *, invoked: bool) -> Reply | None:
        if getattr(message.author, "bot", False):
            return None
        text = _norm(_clean(content))
        reference = getattr(message, "reference", None)
        if reference is not None and getattr(reference, "message_id", None):
            panels = await self.state.get_section(PANELS_SECTION)
            if str(reference.message_id) in panels:
                return Reply(await self.music_feedback(message, content, panels[str(reference.message_id)]))
        if message.guild is None:
            pending = await self._pending_compliment(message.author.id)
            if COMPLIMENT_RE.search(text) or pending:
                return Reply(await self.compliment(message, content, pending))
            return None
        session = await self._idea_session(message)
        if session and (invoked or self._recent(session, minutes=10)):
            return Reply(await self.idea_followup(message, content, session))
        if not invoked:
            return None
        if COMPLIMENT_RE.search(text) and re.search(r"\banonim", text):
            return Reply("pra ficar anônimo de verdade, me pede isso na minha DM (mensagem privada), tá? ♡")
        if IDEA_RE.search(text):
            return Reply(await self.idea_start(message, content))
        if PLAYLIST_RE.search(text):
            await self.post_playlist(message.channel)
            return Reply("")
        if MUSIC_ADD_RE.search(text):
            return Reply(await self.add_song(message, content))
        if LIKES_RE.search(text):
            return Reply(await self.save_likes(message, content))
        if PROFILE_RE.search(text):
            return Reply(await self.send_card(message))
        return None

    # --------------------------------------------------------- elogio anônimo
    async def _pending_compliment(self, user_id: int) -> dict[str, Any] | None:
        pending = (await self.state.get_section(COMPLIMENTS_SECTION)).get(f"pending:{user_id}")
        if isinstance(pending, dict) and time.time() - float(pending.get("at", 0)) < 900:
            return pending
        return None

    def _main_guild(self) -> Any:
        return self.bot.get_guild(self.guild_id)

    def _find_members(self, sender: Any, content: str) -> tuple[list[Any], str]:
        guild = self._main_guild()
        if guild is None:
            return [], ""
        ids = [int(i) for i in re.findall(r"<@!?(\d{15,22})>", content)]
        if ids:
            members = [guild.get_member(i) for i in ids]
            return [m for m in members if m is not None], ""
        match = re.search(
            r"\b(?:pro|pra|para o|para a|para|ao|à)\s+@?([\wÀ-ÿ.\-]{2,32}(?:\s[\wÀ-ÿ.\-]{2,32})?)",
            _clean(content), re.IGNORECASE,
        )
        if not match:
            return [], ""
        candidates = [match.group(1), match.group(1).split()[0]]
        for wanted in candidates:
            key = _norm(wanted)
            found = [
                m for m in guild.members
                if not m.bot and key in {_norm(m.display_name), _norm(m.name), _norm(getattr(m, "global_name", "") or "")}
            ]
            if found:
                return found, wanted
        return [], match.group(1)

    async def compliment(self, message: Any, content: str, pending: dict[str, Any] | None) -> str:
        sender = message.author
        guild = self._main_guild()
        if guild is None:
            return "não consegui achar o servidor agora. tenta de novo daqui a pouco?"
        target = guild.get_member(int(pending["target_id"])) if pending else None
        body = _clean(content)
        if target is None:
            members, name = self._find_members(sender, content)
            if len(members) > 1:
                options = ", ".join(f"{m.display_name} (@{m.name})" for m in members[:5])
                return f"achei mais de uma pessoa chamada {name}: {options}. me manda o @ certinho de quem é?"
            if not members:
                return (
                    "pra quem é o elogio? me fala o nome dela no servidor (ou o @), tipo: "
                    "\"manda um elogio anônimo pro fulano dizendo que ele explica muito bem\"."
                    if not name else f"não achei ninguém chamado \"{name}\" no servidor. confere o nome ou manda o @ dela?"
                )
            target = members[0]
        if not pending:
            body = self._compliment_body(body, target)
        if target.id == sender.id:
            await self._clear_pending(sender.id)
            return "elogio pra você mesmo não vale, hein? kkk mas fica aqui um meu: você é incrível ♡"
        if getattr(target, "bot", False):
            await self._clear_pending(sender.id)
            return "bots não precisam de elogio (mas obrigada pela intenção ♡)."
        if len(body) < 4:
            await self.state.update_section(COMPLIMENTS_SECTION, f"pending:{sender.id}", {"target_id": target.id, "at": time.time()})
            return f"o que você quer dizer pro {target.display_name}? me conta que eu escrevo com carinho e entrego sem dizer quem foi."
        today = datetime.now(timezone.utc).date().isoformat()
        log = list((await self.state.get_section(COMPLIMENTS_SECTION)).get("log", []))
        mine = [e for e in log if e.get("from") == sender.id and e.get("at", "").startswith(today)]
        if len(mine) >= self.compliments_per_day:
            return f"você já mandou {self.compliments_per_day} elogios hoje. amanhã tem mais! ♡"
        if any(e.get("to") == target.id for e in mine):
            return f"você já mandou um elogio pro {target.display_name} hoje. deixa pra amanhã que fica ainda mais especial ♡"
        if any(word in f" {_norm(body)} " for word in (f" {w} " for w in OFFENSIVE)):
            await self._clear_pending(sender.id)
            return "esse aí não dá pra eu entregar: tem palavra que pode ofender. me manda um elogio de verdade? ♡"
        verdict = await self._review_compliment(body, target.display_name)
        if verdict is not None and not verdict.get("ok", False):
            await self._clear_pending(sender.id)
            reason = str(verdict.get("reason") or "não parece um elogio")
            return f"esse não dá pra eu entregar ({reason}). se quiser, me manda outro, com carinho ♡"
        text = str((verdict or {}).get("message") or "").strip() or f"alguém daqui quis te dizer: \"{body[:400]}\""
        try:
            await target.send(
                f"💌 **Você recebeu um elogio anônimo** de alguém do {guild.name}:\n> {text[:1500]}\n"
                "-# a Bella não conta quem mandou ♡",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            await self._clear_pending(sender.id)
            return f"não consegui entregar: a DM do {target.display_name} tá fechada. ):"
        entry = {"from": sender.id, "to": target.id, "at": datetime.now(timezone.utc).isoformat(), "text": text[:500]}

        def mutate(section: dict[str, Any]) -> None:
            values = list(section.get("log", []))
            values.append(entry)
            section["log"] = values[-2000:]
            received = dict(section.get("received", {}))
            received[str(target.id)] = int(received.get(str(target.id), 0)) + 1
            section["received"] = received
            section.pop(f"pending:{sender.id}", None)

        await self.state.mutate_section(COMPLIMENTS_SECTION, mutate)
        return f"entregue! o {target.display_name} recebeu teu elogio, sem saber que foi você:\n> {text[:600]}"

    @staticmethod
    def _compliment_body(text: str, target: Any) -> str:
        """O pedido inteiro, se sobrar conteúdo depois de tirar o comando e o nome da pessoa."""
        names = {_norm(target.display_name), _norm(getattr(target, "name", "")), _norm(getattr(target, "global_name", "") or "")}
        cleaned = _norm(text)
        for name in sorted((n for n in names if n), key=len, reverse=True):
            cleaned = re.sub(rf"(?<![a-z0-9])@?{re.escape(name)}(?![a-z0-9])", " ", cleaned)
        cleaned = re.sub(
            r"\b(?:manda|mande|mandar|envia|envie|enviar|faz|faca|um|uma|elogio|anonimo|secreto|pro|pra|para|ao|a|o|"
            r"dizendo|falando|avisando|que|de|por favor|pfv|bella)\b",
            " ", cleaned,
        )
        return text if len(cleaned.split()) >= 2 else ""

    async def _clear_pending(self, user_id: int) -> None:
        def mutate(section: dict[str, Any]) -> None:
            section.pop(f"pending:{user_id}", None)

        await self.state.mutate_section(COMPLIMENTS_SECTION, mutate)

    async def _review_compliment(self, body: str, name: str) -> dict[str, Any] | None:
        if self.ai is None:
            return None
        try:
            return await self.ai.generate_json(
                f"Alguém quer mandar um ELOGIO ANÔNIMO para {name}, membro de um servidor do Discord.\n"
                f"TEXTO DA PESSOA: {body[:1000]}\n\n"
                "ok: true só se for um elogio ou mensagem gentil; false se for ofensa, ironia maldosa, "
                "assédio, cantada sexual, ameaça, piada humilhante ou algo que exponha dados pessoais. "
                "reason: motivo curto se false. message: se ok, reescreva com carinho e naturalidade, "
                f"falando direto com {name} (você), em português do Brasil, 1 a 3 frases, mantendo o "
                "sentido, sem revelar nem dar pistas de quem mandou, no máximo um emoji.",
                {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}, "reason": {"type": "string"}, "message": {"type": "string"}},
                    "required": ["ok"],
                },
                max_output_tokens=400,
                timeout_seconds=20,
                use_cache=False,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("IA indisponível para revisar o elogio: %s", type(exc).__name__)
            return None

    # ---------------------------------------------------------- revisão de ideia
    def _session_key(self, message: Any) -> str:
        return f"{message.channel.id}:{message.author.id}"

    async def _idea_session(self, message: Any) -> dict[str, Any] | None:
        session = (await self.state.get_section(IDEA_SESSIONS_SECTION)).get(self._session_key(message))
        if isinstance(session, dict) and session.get("stage") == "asked" and self._recent(session, minutes=30):
            return session
        return None

    @staticmethod
    def _recent(session: dict[str, Any], *, minutes: int) -> bool:
        return time.time() - float(session.get("at", 0)) < minutes * 60

    async def idea_start(self, message: Any, content: str) -> str:
        idea = _clean(content)
        questions = [
            "quem você imagina usando isso no dia a dia?",
            "que problema dessa pessoa a ideia resolve, e como ela resolve hoje sem o seu app?",
            "você pensa em ganhar dinheiro com isso? como?",
        ]
        opener = "que legal, bora pensar nisso junto!"
        if self.ai is not None:
            try:
                data = await self.ai.generate_json(
                    f"Uma pessoa contou uma ideia para a Bella (assistente de um servidor do Discord):\n{idea[:1500]}\n\n"
                    "Antes de opinar, a Bella faz perguntas. opener: uma frase curta e animada reagindo à ideia "
                    "(sem elogio exagerado). questions: 3 perguntas curtas e específicas sobre esta ideia "
                    "(público, problema real, diferencial ou como ganhar dinheiro). Português do Brasil.",
                    {"type": "object", "properties": {"opener": {"type": "string"}, "questions": {"type": "array", "items": {"type": "string"}}},
                     "required": ["questions"]},
                    max_output_tokens=500, timeout_seconds=20, use_cache=False,
                )
                questions = [str(q).strip() for q in data.get("questions", []) if str(q).strip()][:4] or questions
                opener = str(data.get("opener") or opener).strip()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("IA indisponível para a revisão de ideia: %s", type(exc).__name__)
        await self.state.update_section(IDEA_SESSIONS_SECTION, self._session_key(message), {
            "stage": "asked", "idea": idea[:2000], "questions": questions, "at": time.time(),
        })
        lines = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, start=1))
        return f"{opener}\nantes de eu te dar minha opinião, me responde:\n{lines}"

    async def idea_followup(self, message: Any, content: str, session: dict[str, Any]) -> str:
        answers = _clean(content)
        await self.state.update_section(IDEA_SESSIONS_SECTION, self._session_key(message), {**session, "stage": "done", "at": time.time()})
        if self.ai is None:
            return "anotei! agora minha IA tá fora do ar pra analisar com calma. me chama de novo daqui a pouquinho?"
        questions = "\n".join(f"- {q}" for q in session.get("questions", []))
        try:
            text = await self.ai.generate_text(
                "Você é a Bella, assistente simpática e direta de um servidor de tecnologia no Discord. "
                "Analise a ideia de uma pessoa.\n\n"
                f"IDEIA: {session.get('idea', '')}\nPERGUNTAS QUE VOCÊ FEZ:\n{questions}\nRESPOSTAS DELA: {answers[:2000]}\n\n"
                "Responda em português do Brasil, conversando com ela, com três partes curtas: o que tem de "
                "forte na ideia; os principais riscos ou pontos de atenção (seja honesta, sem desanimar); e "
                "por onde começar (2 a 4 passos práticos e pequenos, como validar com pessoas reais antes de "
                "construir). NUNCA escreva código nem blocos de código. Se a ideia já existir no mercado, "
                "diga e sugira um diferencial. Máximo de 12 linhas.",
                max_output_tokens=900, timeout_seconds=40, use_cache=False,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("IA indisponível na análise da ideia: %s", type(exc).__name__)
            return "Ops, tive uma instabilidade ao processar sua mensagem. Pode tentar novamente daqui a pouquinho? ♡"
        await self._bump_profile(message.author.id, "ideas_reviewed")
        return _strip_code(str(text or "").strip())[:1900]

    # ------------------------------------------------------------ playlist
    async def _search_song(self, query: str) -> dict[str, Any] | None:
        url = f"https://itunes.apple.com/search?term={quote_plus(query)}&entity=song&limit=8&country=BR"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(url) as response:
                    payload = await response.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Busca de música falhou: %s", type(exc).__name__)
            return None
        results = payload.get("results") or []
        if not results:
            return None
        wanted = _norm(query)
        version_words = ("remix", "mix", "live", "ao vivo", "acoustic", "acustico", "cappella", "instrumental", "karaoke", "cover", "sped up", "slowed")

        def penalty(result: dict[str, Any]) -> int:
            title = _norm(f"{result.get('trackName', '')} {result.get('collectionName', '')}")
            return sum(1 for word in version_words if word in title and word not in wanted)

        item = min(results, key=lambda result: (penalty(result), results.index(result)))
        return {
            "id": str(item.get("trackId")),
            "title": item.get("trackName", ""),
            "artist": item.get("artistName", ""),
            "album": item.get("collectionName", ""),
            "genre": item.get("primaryGenreName", ""),
            "artwork": str(item.get("artworkUrl100", "")).replace("100x100bb", "300x300bb"),
            "url": item.get("trackViewUrl", ""),
            "preview": item.get("previewUrl", ""),
        }

    @staticmethod
    def _song_query(content: str) -> str:
        text = _clean(content)
        text = re.sub(
            r"(?i)\b(?:eu\s+)?(?:indico|indica|indicação|indicacao|recomendo|recomenda|recomendação|recomendacao|sugiro|sugestão|"
            r"sugestao|adiciona|adicione|coloca|coloque|bota|bote|põe|poe|ponha)\b", " ", text,
        )
        text = re.sub(r"(?i)\b(?:a |o |essa |esse |uma |um )?(?:música|musica|som|canção|cancao|track)(?: do dia)?\b\s*[:\-]?", " ", text)
        text = re.sub(r"(?i)\b(?:na|pra|para a|para|da|na nossa)\s+playlist(?: da semana)?\b", " ", text)
        text = re.sub(r"(?i)\s+(?:do|da|de|by|feat\.?|ft\.?|-)\s+", " ", f" {text} ")
        return " ".join(text.split()).strip(" .,:;!-\"'")

    async def add_song(self, message: Any, content: str) -> str:
        query = self._song_query(content)
        if len(query) < 2:
            return "qual música? me fala o nome e o cantor, tipo \"indico a música Blinding Lights do The Weeknd\"."
        song = await self._search_song(query)
        if song is None:
            return f"não achei \"{query}\". confere o nome e o cantor?"
        week = _week_key()
        result = {}

        def mutate(section: dict[str, Any]) -> None:
            songs = section.setdefault(week, {})
            record = songs.get(song["id"])
            if record is None:
                record = {**song, "added_by": message.author.id, "added_by_name": message.author.display_name,
                          "added_at": datetime.now(timezone.utc).isoformat(), "votes": [], "feedback": []}
                songs[song["id"]] = record
            if message.author.id not in record["votes"]:
                record["votes"].append(message.author.id)
            result["record"] = dict(record)

        await self.state.mutate_section(MUSIC_SECTION, mutate)
        await self._bump_profile(message.author.id, "songs", like=song["artist"])
        record = result["record"]
        votes = len(record["votes"])
        position = next((i for i, r in enumerate(await self.ranking(week), start=1) if r["id"] == song["id"]), "?")
        return (
            f"🎵 anotei **{song['title']}** — {song['artist']} na playlist da semana! "
            f"tá em #{position} com {votes} voto(s). pra ver o ranking: \"bella, playlist da semana\"."
        )

    async def ranking(self, week: str | None = None) -> list[dict[str, Any]]:
        songs = (await self.state.get_section(MUSIC_SECTION)).get(week or _week_key(), {})
        values = [s for s in songs.values() if isinstance(s, dict)]
        return sorted(values, key=lambda s: (-len(s.get("votes", [])), s.get("added_at", "")))

    async def render_playlist(self, week: str, page: int) -> tuple[io.BytesIO, int, list[dict[str, Any]]]:
        songs = await self.ranking(week)
        pages = max(1, math.ceil(len(songs) / PAGE_SIZE))
        page = max(0, min(page, pages - 1))
        chunk = songs[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        width, row_h, top = 1100, 150, 150
        height = top + max(1, len(chunk)) * row_h + 70
        image = Image.new("RGB", (width, height), (14, 12, 24))
        draw = ImageDraw.Draw(image)
        for y in range(height):
            shade = int(14 + 26 * y / height)
            draw.line([(0, y), (width, y)], fill=(shade, 10, shade + 18))
        title_font, big, mid, small = _font(46, bold=True), _font(30, bold=True), _font(24), _font(20)
        draw.text((50, 40), "Playlist da semana", font=title_font, fill=(255, 255, 255))
        monday = datetime.strptime(week + "-1", "%G-W%V-%u")
        draw.text((52, 100), f"{monday.strftime('%d/%m')} a {(monday + timedelta(days=6)).strftime('%d/%m')} • indicações do servidor",
                  font=small, fill=(190, 170, 230))
        if not chunk:
            draw.text((50, top + 40), "Ninguém indicou música esta semana ainda. Seja a primeira pessoa!", font=mid, fill=(220, 220, 220))
        artworks = await asyncio.gather(*(_download_image(s.get("artwork", "")) for s in chunk))
        for index, (song, art) in enumerate(zip(chunk, artworks)):
            y = top + index * row_h
            rank = page * PAGE_SIZE + index + 1
            draw.rounded_rectangle((40, y + 8, width - 40, y + row_h - 8), radius=22, fill=(30, 24, 48))
            medal = {1: (255, 205, 80), 2: (205, 210, 225), 3: (220, 150, 90)}.get(rank, (160, 140, 220))
            draw.text((70, y + 48), f"#{rank}", font=big, fill=medal)
            if art is not None:
                image.paste(_rounded(art, 112, 18), (160, y + 19), _rounded(art, 112, 18))
            else:
                draw.rounded_rectangle((160, y + 19, 272, y + 131), radius=18, fill=(60, 50, 90))
                draw.text((196, y + 55), "♪", font=big, fill=(255, 255, 255))
            draw.text((300, y + 26), _fit(draw, song.get("title", ""), big, 560), font=big, fill=(255, 255, 255))
            draw.text((300, y + 66), _fit(draw, song.get("artist", ""), mid, 560), font=mid, fill=(200, 185, 240))
            draw.text((300, y + 100), _fit(draw, f"{song.get('album', '')} • {song.get('genre', '')} • indicada por {song.get('added_by_name', '?')}", small, 560),
                      font=small, fill=(150, 140, 180))
            votes = len(song.get("votes", []))
            feedback = len(song.get("feedback", []))
            draw.text((width - 210, y + 36), f"♥ {votes} voto{'s' if votes != 1 else ''}", font=mid, fill=(255, 140, 190))
            draw.text((width - 210, y + 76), "1 opinião" if feedback == 1 else f"{feedback} opiniões",
                      font=small, fill=(180, 170, 210))
        draw.text((50, height - 50), f"página {page + 1} de {pages} • responda esta mensagem pra dar sua opinião sobre uma música",
                  font=small, fill=(170, 160, 200))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer, pages, chunk

    async def playlist_payload(self, week: str, page: int) -> tuple[discord.File, discord.ui.View, int]:
        buffer, pages, chunk = await self.render_playlist(week, page)
        page = max(0, min(page, pages - 1))
        return discord.File(buffer, filename="playlist.png"), MusicPanelView(self, chunk=chunk, page=page, pages=pages), page

    async def post_playlist(self, channel: Any, week: str | None = None) -> Any:
        week = week or _week_key()
        file, view, page = await self.playlist_payload(week, 0)
        sent = await channel.send(file=file, view=view)
        await self.state.update_section(PANELS_SECTION, str(sent.id), {"week": week, "page": page, "channel_id": channel.id})
        return sent

    async def change_page(self, interaction: discord.Interaction, delta: int) -> None:
        panel = (await self.state.get_section(PANELS_SECTION)).get(str(interaction.message.id))
        if not isinstance(panel, dict):
            await interaction.response.send_message("esse painel é antigo. pede um novo: \"bella, playlist da semana\".", ephemeral=True)
            return
        file, view, page = await self.playlist_payload(panel["week"], int(panel.get("page", 0)) + delta)
        panel["page"] = page
        await self.state.update_section(PANELS_SECTION, str(interaction.message.id), panel)
        await interaction.response.edit_message(attachments=[file], view=view)

    async def vote(self, interaction: discord.Interaction, song_id: str) -> None:
        panel = (await self.state.get_section(PANELS_SECTION)).get(str(interaction.message.id))
        week = panel["week"] if isinstance(panel, dict) else _week_key()
        outcome = {}

        def mutate(section: dict[str, Any]) -> None:
            song = section.get(week, {}).get(song_id)
            if not isinstance(song, dict):
                return
            if interaction.user.id in song["votes"]:
                song["votes"].remove(interaction.user.id)
                outcome["text"] = f"tirei teu voto de **{song['title']}**."
            else:
                song["votes"].append(interaction.user.id)
                outcome["text"] = f"♥ votado: **{song['title']}** — {song['artist']}!"

        await self.state.mutate_section(MUSIC_SECTION, mutate)
        if not outcome:
            await interaction.response.send_message("essa música não tá mais na playlist.", ephemeral=True)
            return
        page = int(panel.get("page", 0)) if isinstance(panel, dict) else 0
        file, view, _ = await self.playlist_payload(week, page)
        await interaction.response.edit_message(attachments=[file], view=view)
        await interaction.followup.send(outcome["text"], ephemeral=True)

    async def music_feedback(self, message: Any, content: str, panel: dict[str, Any]) -> str:
        week = panel.get("week") or _week_key()
        songs = await self.ranking(week)
        text = _norm(_clean(content))
        chosen = None
        position = re.search(r"(?:#|\b(?:a|o|numero|n)\s*)(\d{1,2})\b", text)
        if position and 1 <= int(position.group(1)) <= len(songs):
            chosen = songs[int(position.group(1)) - 1]
        if chosen is None:
            scored = []
            for song in songs:
                title, artist = _norm(song.get("title", "")), _norm(song.get("artist", ""))
                score = (3 if title and title in text else 0) + (2 if artist and artist in text else 0)
                score += sum(1 for w in set(re.findall(r"[a-z0-9]{4,}", title)) if re.search(rf"\b{w}\b", text))
                score += sum(2 for w in set(re.findall(r"[a-z0-9]{4,}", artist)) if re.search(rf"\b{w}\b", text))
                if score:
                    scored.append((score, song))
            scored.sort(key=lambda item: -item[0])
            if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
                chosen = scored[0][1]
        if chosen is None:
            return "sobre qual música é tua opinião? fala o nome dela (ou a posição, tipo \"a 2\")."
        entry = {"user_id": message.author.id, "name": message.author.display_name, "text": _clean(content)[:500],
                 "at": datetime.now(timezone.utc).isoformat()}

        def mutate(section: dict[str, Any]) -> None:
            song = section.get(week, {}).get(chosen["id"])
            if isinstance(song, dict):
                song.setdefault("feedback", []).append(entry)
                song["feedback"] = song["feedback"][-50:]

        await self.state.mutate_section(MUSIC_SECTION, mutate)
        return f"anotei tua opinião sobre **{chosen['title']}** — {chosen['artist']} ♡"

    # --------------------------------------------------------- cartão de membro
    async def _bump_profile(self, user_id: int, counter: str, *, like: str = "") -> None:
        def mutate(section: dict[str, Any]) -> None:
            profile = section.setdefault(str(user_id), {})
            profile[counter] = int(profile.get(counter, 0)) + 1
            if like:
                artists = dict(profile.get("artists", {}))
                artists[like] = int(artists.get(like, 0)) + 1
                profile["artists"] = artists

        await self.state.mutate_section(PROFILE_SECTION, mutate)

    async def save_likes(self, message: Any, content: str) -> str:
        text = _clean(content)
        text = re.sub(r"(?i)^.*?\b(?:curto|gosto de|gosto|gostos(?: s[aã]o)?|amo)\b\s*[:\-]?\s*", "", text).strip(" .")
        likes = [part.strip(" .") for part in re.split(r",|\be\b|;|/", text) if len(part.strip()) >= 2][:8]
        if not likes:
            return "o que você curte? ex.: \"meus gostos: python, anime, futebol\"."

        def mutate(section: dict[str, Any]) -> None:
            section.setdefault(str(message.author.id), {})["likes"] = likes

        await self.state.mutate_section(PROFILE_SECTION, mutate)
        return "anotei no teu perfil: " + ", ".join(likes) + " ♡"

    async def member_stats(self, member: Any) -> dict[str, Any]:
        daily = ((await self.state.get_section(STATISTICS_SECTION)).get(str(member.guild.id)) or {}).get("daily", {})
        messages, active_days, recent_days = 0, 0, 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
        for day, bucket in daily.items():
            count = int(((bucket or {}).get("users") or {}).get(str(member.id), 0))
            if count:
                messages += count
                active_days += 1
                if day >= cutoff:
                    recent_days += 1
        profile = (await self.state.get_section(PROFILE_SECTION)).get(str(member.id), {})
        received = int((await self.state.get_section(COMPLIMENTS_SECTION)).get("received", {}).get(str(member.id), 0))
        joined = getattr(member, "joined_at", None) or datetime.now(timezone.utc)
        days_in = max(0, (datetime.now(timezone.utc) - joined).days)
        xp = messages * 10 + active_days * 25 + int(profile.get("songs", 0)) * 15 + int(profile.get("ideas_reviewed", 0)) * 20
        level = int(math.sqrt(xp / 100)) + 1
        next_xp = (level ** 2) * 100
        previous_xp = ((level - 1) ** 2) * 100
        badges = []
        rules = (
            (messages >= 1, "Primeiros passos"), (messages >= 500, "Tagarela"), (messages >= 2000, "Lenda do chat"),
            (days_in >= 30, "Um mês de casa"), (days_in >= 180, "Veterano"), (days_in >= 365, "Um ano de servidor"),
            (recent_days >= 20, "Sempre presente"), (int(profile.get("songs", 0)) >= 3, "DJ da comunidade"),
            (received >= 1, "Querido(a) da galera"), (int(profile.get("ideas_reviewed", 0)) >= 1, "Visionário"),
        )
        badges = [label for ok, label in rules if ok]
        artists = sorted((profile.get("artists") or {}).items(), key=lambda kv: -kv[1])
        likes = list(profile.get("likes") or []) + [name for name, _ in artists[:3] if name not in (profile.get("likes") or [])]
        return {"messages": messages, "active_days": active_days, "days_in": days_in, "joined": joined, "xp": xp, "level": level,
                "progress": (xp - previous_xp) / max(1, next_xp - previous_xp), "badges": badges, "likes": likes[:6]}

    async def render_card(self, member: Any, stats: dict[str, Any]) -> io.BytesIO:
        width, height = 1000, 520
        image = Image.new("RGB", (width, height), (16, 12, 28))
        draw = ImageDraw.Draw(image)
        for y in range(height):
            shade = int(16 + 30 * y / height)
            draw.line([(0, y), (width, y)], fill=(shade + 10, 12, shade + 30))
        draw.rounded_rectangle((24, 24, width - 24, height - 24), radius=34, outline=(150, 110, 230), width=3)
        avatar = None
        try:
            avatar = Image.open(io.BytesIO(await member.display_avatar.replace(size=256).read())).convert("RGBA")
        except Exception:  # noqa: BLE001
            avatar = None
        if avatar is not None:
            circle = ImageOps.fit(avatar, (190, 190))
            mask = Image.new("L", (190, 190), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 190, 190), fill=255)
            image.paste(circle, (60, 70), mask)
        if avatar is None:
            draw.ellipse((60, 70, 250, 260), fill=(70, 50, 110))
            initial = (member.display_name or "?")[:1].upper()
            letter_font = _font(90, bold=True)
            box = draw.textbbox((0, 0), initial, font=letter_font)
            draw.text((155 - (box[2] - box[0]) / 2 - box[0], 165 - (box[3] - box[1]) / 2 - box[1]), initial, font=letter_font, fill=(255, 255, 255))
        draw.ellipse((55, 65, 255, 265), outline=(200, 150, 255), width=5)
        name_font, big, mid, small = _font(44, bold=True), _font(34, bold=True), _font(24), _font(20)
        draw.text((290, 70), _fit(draw, member.display_name, name_font, 640), font=name_font, fill=(255, 255, 255))
        draw.text((292, 128), f"@{member.name}", font=mid, fill=(190, 170, 230))
        draw.text((292, 175), f"Nível {stats['level']}", font=big, fill=(255, 205, 90))
        draw.text((470, 186), f"{stats['xp']} XP", font=small, fill=(210, 200, 240))
        draw.rounded_rectangle((292, 225, 930, 250), radius=12, fill=(50, 40, 80))
        filled = 292 + int(638 * max(0.0, min(1.0, stats["progress"])))
        if filled > 300:
            draw.rounded_rectangle((292, 225, filled, 250), radius=12, fill=(190, 120, 255))
        info = [
            ("No servidor desde", stats["joined"].strftime("%d/%m/%Y") + f" ({stats['days_in']} dias)"),
            ("Mensagens (120 dias)", str(stats["messages"])),
            ("Dias ativos", str(stats["active_days"])),
        ]
        for i, (label, value) in enumerate(info):
            x = 60 + i * 300
            draw.text((x, 300), label, font=small, fill=(170, 160, 200))
            draw.text((x, 328), value, font=mid, fill=(255, 255, 255))
        draw.text((60, 380), "Conquistas", font=small, fill=(170, 160, 200))
        badges = "  •  ".join(stats["badges"]) or "ainda nenhuma: bora conversar!"
        draw.text((60, 406), _fit(draw, badges, mid, 880), font=mid, fill=(255, 225, 150))
        draw.text((60, 446), "Curte", font=small, fill=(170, 160, 200))
        likes = ", ".join(stats["likes"]) or "conta pra Bella: \"meus gostos: ...\""
        draw.text((130, 444), _fit(draw, likes, mid, 800), font=mid, fill=(200, 230, 255))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer

    async def send_card(self, message: Any) -> str:
        member = message.author
        if getattr(member, "guild", None) is None:
            return "me pede o perfil dentro do servidor, tá? ♡"
        channel = self.bot.get_channel(self.card_channel_id)
        if channel is None:
            return "não achei o canal dos cartões. avisa a staff? ):"
        stats = await self.member_stats(member)
        buffer = await self.render_card(member, stats)
        await channel.send(
            f"✨ cartão de membro de {member.mention}",
            file=discord.File(buffer, filename="cartao.png"),
            allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
        )
        if message.channel.id == channel.id:
            return ""
        return f"teu cartão tá prontinho em {channel.mention} ♡"

    # ------------------------------------------------------------- worker
    async def worker(self) -> None:
        wait = getattr(self.bot, "wait_until_ready", None)
        if callable(wait):
            await wait()
        while not getattr(self.bot, "is_closed", lambda: False)():
            try:
                now = datetime.now(timezone.utc) - timedelta(hours=3)
                meta = (await self.state.get_section(PANELS_SECTION)).get("_meta", {})
                week = _week_key(now)
                if self.music_channel_id and now.weekday() == 6 and now.hour >= 20 and meta.get("weekly") != week:
                    channel = self.bot.get_channel(self.music_channel_id)
                    if channel is not None and await self.ranking(week):
                        await self.state.update_section(PANELS_SECTION, "_meta", {**meta, "weekly": week})
                        await channel.send("🎶 **playlist da semana** fechada! as mais votadas pelo servidor:")
                        await self.post_playlist(channel, week)
            except Exception:
                LOGGER.exception("Falha no worker da playlist.")
            await asyncio.sleep(300)


class MusicPanelView(discord.ui.View):
    """Botões de página e voto do painel. Persistente: funciona depois de reiniciar."""

    def __init__(self, features: CommunityFeatures, *, chunk: list[dict[str, Any]] | None = None, page: int = 0, pages: int = 1) -> None:
        super().__init__(timeout=None)
        self.features = features
        self.previous.disabled = page <= 0
        self.next.disabled = page >= pages - 1
        options = [
            discord.SelectOption(label=f"#{page * PAGE_SIZE + i + 1} {s.get('title', '')}"[:100],
                                 description=str(s.get("artist", ""))[:100], value=str(s.get("id")))
            for i, s in enumerate(chunk or [])
        ]
        if options:
            self.vote_select.options = options
        else:
            self.remove_item(self.vote_select)

    @discord.ui.button(label="◀ Anterior", style=discord.ButtonStyle.secondary, custom_id="bella:music:prev", row=1)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self.features.change_page(interaction, -1)

    @discord.ui.button(label="Próxima ▶", style=discord.ButtonStyle.secondary, custom_id="bella:music:next", row=1)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self.features.change_page(interaction, 1)

    @discord.ui.select(placeholder="♥ Votar numa música desta página", custom_id="bella:music:vote", row=0,
                       options=[discord.SelectOption(label="-", value="-")])
    async def vote_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        values = (interaction.data or {}).get("values") or select.values
        if values:
            await self.features.vote(interaction, str(values[0]))
