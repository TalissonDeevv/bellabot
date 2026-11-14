"""Guia para quem fala outro idioma (não português).

Quando alguém escreve para a Bella em inglês, espanhol, francês etc. (ou um
membro novo pergunta, perdido, o que é o servidor), ela responde no idioma da
pessoa, explicando o servidor ou o canal a partir do que realmente existe:
nome e descrição do servidor, lista de canais com os tópicos, regras, mensagens
fixadas e o histórico recente do canal. O texto é escrito pela IA, sem modelo
fixo.

Para economizar cota, o resumo de cada canal fica guardado por algumas horas.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from unicodedata import combining, normalize

import discord

from .discord_entities import CHANNEL_LINK_RE

LOGGER = logging.getLogger(__name__)

# Palavras bem típicas de cada idioma (sem acento, minúsculas). Palavras que
# existem igual em português (ex.: "que", "de", "a") ficam de fora.
_LANGUAGE_WORDS: dict[str, set[str]] = {
    "pt": {
        "voce", "vc", "nao", "sim", "obrigado", "obrigada", "tudo", "bem", "isso", "esse", "essa",
        "aqui", "canal", "servidor", "pra", "pro", "tem", "quero", "como", "funciona", "oi", "ola",
        "eu", "meu", "minha", "estou", "to", "ta", "sao", "fazer", "ajuda", "porque", "entao", "tambem",
        "muito", "qual", "onde", "quando", "seu", "sua", "gente", "galera", "kkk", "kkkk", "beleza",
        "duvida", "preciso", "alguem", "nada", "agora", "ainda", "depois", "ja", "vou", "vai", "bom", "dia",
    },
    "en": {
        "the", "is", "are", "what", "this", "that", "how", "does", "do", "you", "your", "i", "i'm", "im",
        "hello", "hi", "hey", "here", "server", "channel", "about", "for", "with", "can", "help",
        "where", "why", "which", "it", "its", "it's", "and", "of", "to", "in", "my", "me", "please",
        "thanks", "thank", "lost", "new", "anyone", "someone", "speak", "english", "work", "works", "used",
    },
    "es": {
        "hola", "que", "es", "esto", "este", "esta", "el", "los", "las", "una", "un", "como", "donde",
        "porque", "por", "para", "servidor", "canal", "ayuda", "gracias", "estoy", "perdido", "nuevo",
        "alguien", "habla", "espanol", "sirve", "funciona", "aqui", "usted", "tu", "yo", "muy", "pero",
        "puedo", "hay", "cual", "quien", "bueno", "entiendo",
    },
    "fr": {
        "bonjour", "salut", "est", "ce", "que", "quoi", "c'est", "le", "la", "les", "des", "un", "une",
        "je", "suis", "vous", "tu", "pour", "avec", "serveur", "canal", "salon", "aide", "merci",
        "comment", "pourquoi", "ou", "qui", "parle", "francais", "perdu", "nouveau", "ici", "sert",
    },
    "de": {
        "hallo", "ist", "das", "der", "die", "und", "ich", "bin", "was", "wie", "wo", "warum", "hier",
        "server", "kanal", "hilfe", "danke", "neu", "verloren", "spricht", "deutsch", "fur", "mit", "nicht",
    },
    "it": {
        "ciao", "che", "cosa", "questo", "questa", "il", "lo", "gli", "sono", "sei", "come", "dove",
        "perche", "server", "canale", "aiuto", "grazie", "nuovo", "perso", "parla", "italiano", "qui", "serve",
    },
}
# Palavras que, sozinhas, são iguais ou parecidas em português e espanhol.
_SHARED_PT_ES = {"como", "porque", "para", "servidor", "canal", "aqui", "funciona", "que", "un", "por", "tu"}
_LANGUAGE_NAMES = {"en": "inglês", "es": "espanhol", "fr": "francês", "de": "alemão", "it": "italiano"}
_PT_MARKERS = re.compile(r"[ãõç]|(?<!\w)é(?!\w)|\b(?:não|você|vocês|também|então|está|são|pra|pro|tá|tô)\b")
_QUESTION = re.compile(
    r"\?|\b(?:what|how|where|why|which|who|que|qué|cómo|dónde|para qué|quoi|comment|où|pourquoi|"
    r"was|wie|wo|warum|cosa|come|dove|perché|explain|explica|explique|erkläre|spiega|help|ayuda|aide|hilfe|aiuto|lost|perdido|perdu|verloren|perso)\b",
    re.IGNORECASE,
)
_ABOUT_SERVER = re.compile(
    r"\b(?:server|servidor|serveur|discord|channel|canal|canale|kanal|salon|this place|community|comunidad|"
    r"communauté|rules|reglas|règles|regeln|regole|here|aquí|aqui|ici|hier|qui|lost|perdido|perdu|verloren|perso)\b",
    re.IGNORECASE,
)


def _plain(value: str) -> str:
    decomposed = normalize("NFKD", str(value or "").casefold())
    return "".join(c for c in decomposed if not combining(c))


def detect_language(text: str) -> str:
    """'pt', um código de outro idioma ('en', 'es', 'fr', 'de', 'it') ou '' se não der pra saber."""
    cleaned = re.sub(r"<[@#][!&]?\d+>|https?://\S+|:[a-z0-9_]+:", " ", text or "")
    if _PT_MARKERS.search(cleaned.casefold()):
        return "pt"
    words = re.findall(r"[a-z']+", _plain(cleaned))
    if not words:
        return ""
    scores = {lang: sum(1 for w in words if w in vocab) for lang, vocab in _LANGUAGE_WORDS.items()}
    # Palavras comuns a PT e ES não decidem nada sozinhas.
    shared = sum(1 for w in words if w in _SHARED_PT_ES)
    scores["es"] -= shared * 0.5
    scores["pt"] -= shared * 0.5
    best = max(scores, key=lambda lang: scores[lang])
    if scores[best] < 1 or (len(words) >= 3 and scores[best] < 1.5):
        return ""
    ordered = sorted(scores.values(), reverse=True)
    if len(ordered) > 1 and ordered[0] == ordered[1]:
        return ""
    return best


class ForeignGuide:
    def __init__(
        self,
        ai_service: Any | None,
        *,
        history_limit: int = 80,
        cache_hours: float = 6.0,
        newcomer_days: int = 14,
    ) -> None:
        self.ai = ai_service
        self.history_limit = history_limit
        self.cache_ttl = cache_hours * 3600
        self.newcomer_days = newcomer_days
        self._channel_cache: dict[int, tuple[float, str]] = {}

    def should_handle(self, message: Any, content: str, *, invoked: bool) -> str:
        """Código do idioma estrangeiro se a Bella deve agir como guia, senão ''."""
        if getattr(message, "guild", None) is None or not (content or "").strip():
            return ""
        language = detect_language(content)
        if not language or language == "pt":
            return ""
        if invoked:
            return language
        # Sem mencionar a Bella: só ajuda quem é novo no servidor e está perguntando
        # sobre o servidor/canal (não entra em conversa alheia).
        joined = getattr(message.author, "joined_at", None)
        is_new = joined is not None and datetime.now(timezone.utc) - joined < timedelta(days=self.newcomer_days)
        if is_new and _QUESTION.search(content) and _ABOUT_SERVER.search(content):
            return language
        return ""

    # ---------------------------------------------------------- contexto
    @staticmethod
    def _channel_kind(channel: Any) -> str:
        name = type(channel).__name__.casefold()
        if "voice" in name or "stage" in name:
            return "voz"
        if "forum" in name:
            return "fórum"
        if "thread" in name:
            return "tópico"
        return "texto"

    def server_overview(self, guild: Any, viewer: Any) -> str:
        lines = [f"Servidor: {guild.name}"]
        if getattr(guild, "description", None):
            lines.append(f"Descrição oficial: {guild.description}")
        lines.append(f"Membros: {getattr(guild, 'member_count', '?')}")
        rules = getattr(guild, "rules_channel", None)
        if rules is not None:
            lines.append(f"Canal de regras: #{rules.name}")
        lines.append("Canais que a pessoa consegue ver (categoria > canal: tópico):")
        count = 0
        for category, channels in getattr(guild, "by_category", lambda: [])():
            for channel in channels:
                perms = channel.permissions_for(viewer) if viewer is not None else None
                if perms is not None and not perms.view_channel:
                    continue
                topic = (getattr(channel, "topic", None) or "").replace("\n", " ")[:140]
                prefix = f"{category.name} > " if category is not None else ""
                lines.append(f"- {prefix}#{channel.name} ({self._channel_kind(channel)})" + (f": {topic}" if topic else ""))
                count += 1
                if count >= 60:
                    break
            if count >= 60:
                break
        return "\n".join(lines)

    async def channel_digest(self, channel: Any) -> str:
        """Tópico, mensagens fixadas e histórico recente do canal (texto bruto para a IA)."""
        cached = self._channel_cache.get(channel.id)
        if cached and time.monotonic() - cached[0] < self.cache_ttl:
            return cached[1]
        parts = [f"Canal #{getattr(channel, 'name', channel.id)} ({self._channel_kind(channel)})"]
        topic = getattr(channel, "topic", None)
        if topic:
            parts.append(f"Tópico/descrição: {topic}")
        try:
            pins = await channel.pins()
            if pins:
                parts.append("Mensagens fixadas:")
                parts += [f"- {p.author.display_name}: {p.clean_content[:400]}" for p in pins[:6]]
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass
        history: list[str] = []
        try:
            async for item in channel.history(limit=self.history_limit, oldest_first=False):
                text = (item.clean_content or "").strip()
                if not text and item.embeds:
                    embed = item.embeds[0]
                    text = " ".join(filter(None, [embed.title or "", embed.description or ""]))
                if text:
                    history.append(f"{item.author.display_name}: {text[:300]}")
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass
        if history:
            history.reverse()
            parts.append(f"Últimas {len(history)} mensagens (da mais antiga para a mais nova):")
            parts += history
        digest = "\n".join(parts)[-7000:]
        self._channel_cache[channel.id] = (time.monotonic(), digest)
        return digest

    # ------------------------------------------------------------ resposta
    @staticmethod
    def _lookup(guild: Any, channel_id: int) -> Any:
        getter = getattr(guild, "get_channel_or_thread", None) or getattr(guild, "get_channel", None)
        try:
            return getter(channel_id) if callable(getter) else None
        except Exception:  # noqa: BLE001
            return None

    def asked_channels(self, message: Any, content: str) -> list[Any]:
        """Canais que a pessoa perguntou: menção <#id>, link do canal ou o nome escrito."""
        guild = message.guild
        found: dict[int, Any] = {}
        for channel in getattr(message, "channel_mentions", []) or []:
            found[channel.id] = channel
        raw = f"{content} {getattr(message, 'content', '')}"
        for match in re.finditer(r"<#(\d{15,22})>", raw):
            channel = self._lookup(guild, int(match.group(1)))
            if channel is not None:
                found.setdefault(channel.id, channel)
        for match in CHANNEL_LINK_RE.finditer(raw):
            if int(match.group(1)) != getattr(guild, "id", None):
                continue
            channel = self._lookup(guild, int(match.group(2)))
            if channel is not None:
                found.setdefault(channel.id, channel)
        if not found:
            plain = _plain(raw)
            for channel in getattr(guild, "channels", []) or []:
                name = re.sub(r"[^a-z0-9]+", " ", _plain(getattr(channel, "name", ""))).strip()
                if len(name) >= 4 and re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", re.sub(r"[^a-z0-9]+", " ", plain)):
                    found.setdefault(channel.id, channel)
        return [c for c in found.values() if hasattr(c, "history")][:2]

    async def answer(self, message: Any, content: str, language: str) -> str:
        guild = message.guild
        viewer = getattr(message, "author", None)
        asked = self.asked_channels(message, content)
        channels = asked or ([message.channel] if hasattr(message.channel, "history") else [])
        digests = []
        for channel in channels:
            try:
                digest = await self.channel_digest(channel)
            except Exception:  # noqa: BLE001
                LOGGER.warning("Não consegui ler o histórico de %s.", getattr(channel, "id", "?"))
                continue
            label = "CANAL QUE A PESSOA PERGUNTOU" if asked else "CANAL ONDE A PESSOA ESTÁ (ela não citou outro)"
            digests.append(f"{label}:\n{digest}")
        if asked:
            digests.append(
                f"(Ela escreveu a pergunta em #{getattr(message.channel, 'name', '?')}, mas quer saber "
                "do canal perguntado acima. Explique ESSE canal.)"
            )
        overview = self.server_overview(guild, viewer)
        language_name = _LANGUAGE_NAMES.get(language, "o idioma da pessoa")
        prompt = (
            "Você é a Bella, assistente simpática deste servidor do Discord. Uma pessoa que fala "
            f"{language_name} (não português) escreveu para você e pode estar perdida.\n\n"
            f"MENSAGEM DELA ({getattr(viewer, 'display_name', 'membro')}): {content[:1500]}\n\n"
            f"SOBRE O SERVIDOR:\n{overview[:4000]}\n\n"
            + ("\n\n".join(digests) + "\n\n" if digests else "")
            + "Responda NO MESMO IDIOMA da mensagem dela, de forma natural e acolhedora, como uma "
            "pessoa que conhece o servidor. Se ela perguntou sobre o servidor ou um canal, explique "
            "o que é e para que serve com base no que você viu acima (resuma o assunto das conversas, "
            "sem citar ninguém palavra por palavra e sem expor dados pessoais). Se ajudar, diga em "
            "qual canal ela pode ir (use o nome com #). Avise de forma leve que a comunidade fala "
            "principalmente português, mas que ela é bem-vinda. Se não souber algo, diga que não "
            "sabe em vez de inventar. Não use um formato fixo nem listas longas; no máximo 6 frases."
        )
        if self.ai is None:
            return self._fallback(language, guild.name)
        try:
            text = await self.ai.generate_text(prompt, max_output_tokens=500, timeout_seconds=30, use_cache=False)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("IA indisponível para o guia em outro idioma: %s", type(exc).__name__)
            return self._fallback(language, guild.name)
        text = str(text or "").strip()
        return text[:1900] if len(text) >= 10 else self._fallback(language, guild.name)

    @staticmethod
    def _fallback(language: str, server: str) -> str:
        texts = {
            "en": f"Hi! Welcome to {server}. I had a hiccup reading the server right now. Could you ask me again in a little bit? ♡",
            "es": f"¡Hola! Bienvenido/a a {server}. Tuve un problema para leer el servidor ahora. ¿Me preguntas de nuevo en un ratito? ♡",
            "fr": f"Salut ! Bienvenue sur {server}. J'ai eu un souci pour lire le serveur. Tu peux me redemander dans un instant ? ♡",
            "de": f"Hallo! Willkommen auf {server}. Ich hatte gerade ein Problem. Frag mich bitte gleich nochmal. ♡",
            "it": f"Ciao! Benvenuto/a su {server}. Ho avuto un problema a leggere il server. Puoi chiedermelo di nuovo tra poco? ♡",
        }
        return texts.get(language, texts["en"])
