from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from website_guard.state import StateStore

from .intent_router import IntentDecision, normalize_text
from .official_knowledge import (
    asks_about_bella_artist,
    asks_about_bella_id,
    asks_about_hosting,
)


@dataclass(frozen=True)
class LocalReply:
    answer: str
    intent: str
    site_related: bool = False
    server_related: bool = False
    should_reply: bool = True


class LocalConversationEngine:
    """Contingência mínima usada somente quando o provedor de IA não responde."""

    def __init__(
        self,
        knowledge: Any,
        server_knowledge: Any,
        memory: Any,
        state: StateStore,
        *,
        max_response_chars: int = 1800,
        cache_ttl_seconds: int = 900,
    ) -> None:
        self.state = state

    async def fallback(
        self,
        decision: IntentDecision,
        message: str,
        *,
        recent_context: list[dict[str, str]],
    ) -> LocalReply:
        text = normalize_text(message)
        social = _social_fallback(message, text)
        if social:
            intent = "meme" if decision.name == "meme" or _looks_like_meme(text) else "casual"
            return LocalReply(social, intent)
        if any(
            phrase in text
            for phrase in (
                "resume isso",
                "resuma isso",
                "analisa isso",
                "explique isso",
                "o que acha disso",
            )
        ):
            return LocalReply(
                "me responde direto na mensagem ou reenvia o anexo que vc quer "
                "que eu veja. aí eu pego o conteúdo certo",
                "clarification",
            )
        if asks_about_hosting(message):
            return LocalReply(
                "Estou hospedada na Square Cloud, uma plataforma brasileira de "
                "nuvem especializada em sites, APIs e bots. Fundada em 2021, ela "
                "suporta tecnologias como Python, Node.js e Go, com foco em "
                "simplicidade, estabilidade e custo-benefício.",
                "specialized",
                site_related=True,
            )
        if asks_about_bella_artist(message):
            return LocalReply(
                "oie, minha arte foi desenhada pelo <@993285196956844044>. "
                "ele manda muito bem com desenhos, emotes e artes full body; "
                "se vc curtir esse estilo, vale chamar ele pra ver comissões",
                "specialized",
                site_related=True,
            )
        if asks_about_bella_id(message):
            return LocalReply(
                "oie, meu ID no Discord é `1230594375994572881`. "
                "se quiser me mencionar direto, usa <@1230594375994572881>",
                "identity",
                site_related=True,
            )
        if decision.name == "identity" and _asks_brazilian_identity(text):
            return LocalReply(
                "sou brasileira sim kkkkk. em Copa então eu viro torcedora oficial "
                "da bagunça pela Seleção Brasileira",
                "identity",
            )
        if "sobra" in text and "beta" in text:
            return LocalReply(
                "Na lógica do meme? Pro beta não sobra nada.",
                "meme",
            )
        if text.strip(" ?!.,") in {"oi", "ola", "opa", "e ai"}:
            return LocalReply("", "conversation", should_reply=False)
        if "conhece" in text and ("<@" in message or "@" in message):
            return LocalReply(
                "ainda não conheço essa pessoa direito. se vc me contar quem é, eu acompanho melhor",
                "conversation",
            )
        if decision.name == "monitoring":
            return LocalReply(await self._monitoring_reply(), "monitoring")
        if decision.name == "support":
            return LocalReply(
                "calma, vamos por partes. me manda o erro principal, o que vc esperava e o que já tentou",
                "support",
            )
        if decision.name == "short_memory":
            return LocalReply("", "conversation", should_reply=False)
        if "?" in message:
            return LocalReply("", "conversation", should_reply=False)
        if recent_context:
            return LocalReply("", "conversation", should_reply=False)
        return LocalReply("", "conversation", should_reply=False)

    async def _monitoring_reply(self) -> str:
        score = await self.state.get("latest_platform_score", {})
        site = await self.state.get("latest_site_report", {})
        if not isinstance(score, dict):
            score = {}
        if not isinstance(site, dict):
            site = {}
        status = "operacional" if site.get("ok") else "em acompanhamento"
        latency = site.get("response_ms")
        parts = [f"A plataforma está {status}."]
        if latency is not None:
            parts.append(f"O último tempo de resposta foi {latency} ms.")
        if score.get("overall") is not None:
            parts.append(f"O Health Score atual é {score['overall']}/100.")
        return " ".join(parts)


def known_people_direct_reply(message: str) -> str | None:
    text = normalize_text(message)
    clean = _strip_mentions(text).strip(" ?!.,")
    addressed = _strip_bella_addressing(clean).strip(" ?!.,")
    if _asks_about_boyfriend(clean) or _asks_about_boyfriend(addressed):
        return (
            "meu namorado é o <@1355150439057985687>... mas é nosso namoro fictício "
            "de brincadeira, bem levinho e saudável kkkkk"
        )
    asks_identity = any(
        phrase in text
        for phrase in (
            "quem e",
            "quem é",
            "quem eh",
            "conhece",
            "quem sao",
            "quem são",
        )
    )
    if asks_identity and "1189671608080679012" in text:
        return (
            "conheço sim, o <@1189671608080679012> é Anthropic Senior Staff e é "
            "perito em Claude. assunto técnico com ele é papo de especialista mesmo"
        )
    if asks_identity and "1552721267415584799" in text:
        return (
            "conheço sim, o <@1552721267415584799> é membro e pro player profissional "
            "da FURIA. respeito de cenário competitivo por aqui"
        )
    if asks_identity and "1355150439057985687" in text:
        return (
            "esse é o <@1355150439057985687>, meu namorado fictício na nossa brincadeira "
            "de personagem kkkkk"
        )
    if asks_identity and "1409982177332166676" in text:
        return (
            "esse é o <@1409982177332166676>, meu irmão fictício e meu bro na nossa "
            "brincadeira de personagem kkkkk"
        )
    return None


def _social_fallback(message: str, text: str) -> str | None:
    clean = _strip_mentions(text).strip(" ?!.,")
    addressed = _strip_bella_addressing(clean).strip(" ?!.,")
    if not clean:
        return None
    seed = f"{message}:{len(clean)}"
    known_reply = known_people_direct_reply(message)
    if known_reply:
        return known_reply
    if _asks_about_artist(clean):
        return (
            "minha arte foi feita pelo <@993285196956844044>. "
            "ele faz desenho, emote e comissão full body; vale muito chamar ele"
        )
    if _asks_about_bella_self(clean):
        return _pick(
            [
                "eu sou a Bella Nyelle... meio tímida no começo, mas eu juro q converso direitinho kkkkk",
                "sou a Bella. fico mais quietinha às vezes, mas adoro aprender coisa nova e ajudar por aqui",
                "sou a Bella Nyelle, a menina meio tímida daqui kkkkk. gosto de tecnologia, conversa e uns desafios meio doidos",
            ],
            seed,
        )
    if _asks_about_status(clean) or _asks_about_status(addressed):
        return _pick(
            [
                "tô simm... meio quietinha, mas tô kkkkk. e vc?",
                "tô bemm. só um pouquinho sumida na minha cabeça kkkkk e vc?",
                "tô sim, obrigada por perguntar... fiquei até sem graça agora kkkkk. e vc?",
                "tô de boinha simm. e vc, tá tudo certinho aí?",
            ],
            seed,
        )
    if _asks_if_timid(clean) or _asks_if_timid(addressed):
        return _pick(
            [
                "sou um pouquinho sim... principalmente quando colocam atenção demais em mim kkkkk",
                "um pouco kkkkk. no começo eu fico mais quietinha, dps eu vou soltando umas besteirinhas",
                "pior q sou sim... mas não espalha muito não kkkkk",
            ],
            seed,
        )
    if _compliments_bella(clean) or _compliments_bella(addressed):
        return _pick(
            [
                "para kkkkk. eu fico toda sem graça com essas coisas",
                "ah... obrigada. fiquei meio bobinha agora kkkkk",
                "assim vc me quebra um pouquinho kkkkk. obrigada",
            ],
            seed,
        )
    if "namora comigo" in clean or "namora comigo" in addressed:
        return _pick(
            [
                "calma aí, emocionado kkkkk",
                "nem me chama assim do nada q eu fico sem jeito kkkkk",
                "olha ele KKKKKK direto assim?",
            ],
            seed,
        )
    if any(term in clean or term in addressed for term in ("travou", "ta viva", "tá viva", "acorda")):
        return _pick(
            [
                "tô viva simm... só dei uma travadinha básica kkkkk",
                "não travei não, eu tava só pensando demais... acontece kkkkk",
                "tô aqui. foi mal, minha cabeça deu uma engasgada",
            ],
            seed,
        )
    if any(term in clean or term in addressed for term in ("consegui", "funcionou", "deu certo", "terminei")):
        return _pick(
            [
                "BOAA kkkkk eu sabia q vc ia conseguir",
                "aí simm, mandou bem kkkkk",
                "boa!! agora sim ficou bonito de ver",
            ],
            seed,
        )
    if any(term in clean or term in addressed for term in ("kkkk", "ksks", "hehe", "meme", "olha isso")):
        return _pick(
            [
                "KKKKKKK mds",
                "eu não tava preparada pra isso kkkkk",
                "pera KKKKKK",
                "aí vc me quebra kkkkk",
            ],
            seed,
        )
    if clean in {
        "oi",
        "oie",
        "oiee",
        "ola",
        "olá",
        "opa",
        "e ai",
        "e aí",
        "eae",
        "iae",
        "ta ae",
        "ta ai",
        "tá ai",
        "tá aí",
    } or addressed in {
        "oi",
        "oie",
        "oiee",
        "ola",
        "olá",
        "opa",
        "e ai",
        "e aí",
        "eae",
        "iae",
        "ta ae",
        "ta ai",
        "tá ai",
        "tá aí",
    }:
        return _pick(
            [
                "oiee",
                "oii",
                "opa kkkkk",
                "tô aq simm... quietinha, mas tô kkkkk",
                "oiee, cheguei hihihi",
            ],
            seed,
        )
    if len(addressed.split()) <= 4 and any(term in addressed for term in ("boa", "salve", "coe", "fala")):
        return _pick(["oiee", "salvee kkkkk", "opa, boaa", "coee hihihi"], seed)
    return None


def _strip_mentions(text: str) -> str:
    return " ".join(re.sub(r"<@!?\d+>", "", text).split())


def _strip_bella_addressing(text: str) -> str:
    value = re.sub(r"(?i)@?bella(?:\s+nyelle)?\b", "", text)
    value = re.sub(r"(?i)@?nyelle\b", "", value)
    return " ".join(value.split())


def _asks_about_status(text: str) -> bool:
    return any(
        term in text
        for term in (
            "tudo bem",
            "tudo bom",
            "como ta",
            "como tá",
            "ta bem",
            "tá bem",
            "ta boa",
            "ta bao",
            "tá bão",
        )
    )


def _asks_about_boyfriend(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "quem e seu namorado",
            "quem é seu namorado",
            "quem eh seu namorado",
            "quem e o seu namorado",
            "quem é o seu namorado",
            "vc tem namorado",
            "voce tem namorado",
            "você tem namorado",
        )
    )


def _asks_if_timid(text: str) -> bool:
    return any(term in text for term in ("timida", "tímida", "vergonha", "quietinha"))


def _compliments_bella(text: str) -> bool:
    return any(
        term in text
        for term in (
            "fofa",
            "bonita",
            "linda",
            "gracinha",
            "engracada",
            "engraçada",
            "legal",
        )
    )


def _asks_about_artist(text: str) -> bool:
    return (
        any(term in text for term in ("desenhou", "desenho", "arte", "avatar"))
        and any(term in text for term in ("bella", "sua", "seu", "personagem"))
    )


def _asks_about_bella_self(text: str) -> bool:
    return any(
        term in text
        for term in (
            "quem e a bella",
            "quem é a bella",
            "quem e vc",
            "quem é vc",
            "quem e voce",
            "quem é você",
            "se apresenta",
            "apresenta pra",
        )
    )


def _asks_brazilian_identity(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "nacionalidade",
            "voce e brasileiro",
            "de qual pais",
            "qual selecao",
            "torce pra qual selecao",
            "torce para qual selecao",
        )
    )


def _professional_context(text: str) -> bool:
    return any(
        term in text
        for term in (
            "ticket",
            "orcamento",
            "orçamento",
            "proposta",
            "projeto",
            "suporte",
            "atendimento",
            "empresa",
            "cheetah",
            "ops framework",
            "formulario",
            "formulário",
            "bug",
            "erro",
            "cargo",
            "banir",
            "limpar",
            "apagar",
            "anuncio",
            "anúncio",
        )
    )


def _looks_like_meme(text: str) -> bool:
    return any(
        term in text
        for term in (
            "meme",
            "kkkk",
            "loss",
            "gain",
            "tankou",
            "sigma",
            "based",
            "f no chat",
            "farialimabets",
            "six seven",
            "mega brain",
            "top chief",
        )
    )


def _is_high_energy(message: str) -> bool:
    letters = [char for char in message if char.isalpha()]
    if len(letters) >= 5:
        upper_ratio = sum(char.isupper() for char in letters) / len(letters)
        if upper_ratio >= 0.7:
            return True
    return message.count("!") >= 2


def _pick(options: list[str], seed: str) -> str:
    if not options:
        return ""
    index = sum(ord(char) for char in seed) % len(options)
    return options[index]
