from __future__ import annotations

import re
from dataclasses import dataclass

from .intent_router import normalize_text


@dataclass(frozen=True)
class SocialAssessment:
    kind: str
    serious: bool
    playful: bool
    aggressive: bool
    deescalate: bool


def assess_social_intent(
    message: str,
    recent_context: list[dict[str, str]] | None = None,
) -> SocialAssessment:
    text = normalize_text(message).strip(" ?!.,")
    aggressive = _looks_aggressive(text)
    serious = _looks_serious(text, message)
    meme = _looks_like_meme(text)
    casual = _looks_casual(text, message)
    conflict_context = _recent_conflict(recent_context or [])
    if aggressive:
        return SocialAssessment(
            kind="conflict" if conflict_context else "provocation",
            serious=False,
            playful=False,
            aggressive=True,
            deescalate=True,
        )
    if serious:
        return SocialAssessment(
            kind="serious_question",
            serious=True,
            playful=False,
            aggressive=False,
            deescalate=False,
        )
    if meme:
        return SocialAssessment(
            kind="meme",
            serious=False,
            playful=True,
            aggressive=False,
            deescalate=False,
        )
    if casual:
        return SocialAssessment(
            kind="casual",
            serious=False,
            playful=True,
            aggressive=False,
            deescalate=False,
        )
    return SocialAssessment(
        kind="conversation",
        serious=False,
        playful=False,
        aggressive=False,
        deescalate=False,
    )


def _looks_serious(text: str, original: str) -> bool:
    if not text:
        return False
    social_exceptions = (
        "tu dorme",
        "voce dorme",
        "vc dorme",
        "ta ai",
        "tá ai",
        "o pai ta on",
        "gain ou loss",
        "six seven",
        "67",
    )
    if any(value in text for value in social_exceptions):
        return False
    serious_markers = (
        "vale a pena",
        "compensa",
        "devo comprar",
        "devo usar",
        "voce recomenda",
        "você recomenda",
        "qual recomenda",
        "preciso de ajuda",
        "como funciona",
        "como fazer",
        "como criar",
        "me explique",
        "pode explicar",
        "qual a diferenca",
        "qual a diferença",
        "o que e ",
        "o que é ",
    )
    serious_topics = (
        "dominio",
        "domínio",
        "hospedagem",
        "servidor",
        "docker",
        "python",
        "javascript",
        "api",
        "codigo",
        "código",
        "banco de dados",
        "site",
        "empresa",
        "orcamento",
        "orçamento",
        "projeto",
        "seguranca",
        "segurança",
        "dinheiro",
        "contrato",
        "carreira",
        "faculdade",
    )
    if any(marker in text for marker in serious_markers):
        return True
    question_word = re.match(
        r"^(?:o que|quem|como|onde|quando|quanto|por que|porque|qual|quais)\b",
        text,
    )
    if question_word and any(topic in text for topic in serious_topics):
        return True
    return "?" in original and any(topic in text for topic in serious_topics)


def _looks_aggressive(text: str) -> bool:
    if not text:
        return False
    patterns = (
        r"\bvai\s+tomar\s+(?:no|na)\b",
        r"\b(?:vai|va)\s+se\s+f(?:u|oder)\b",
        r"\b(?:filho|filha)\s+da\s+p",
        r"\bseu\s+(?:idiota|imbecil|retardado|lixo|merda)\b",
        r"\b(?:cala|cale)\s+a\s+boca\b",
        r"\bteu\s+cu\b",
        r"\bno\s+teu\s+uc\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _looks_like_meme(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "six seven",
            "mega brain",
            "top chief",
            "o pai ta on",
            "gain ou loss",
            "farialimabets",
            "f no chat",
            "meu brodi",
            "tankou",
        )
    )


def _looks_casual(text: str, original: str) -> bool:
    if not text:
        return False
    if re.fullmatch(r"(?:k|ha|hue){3,}", text):
        return True
    if text in {
        "oi",
        "ola",
        "opa",
        "e ai",
        "iae",
        "eae",
        "salve",
        "bom dia",
        "boa tarde",
        "boa noite",
        "acorda",
        "bella acorda",
        "acorda bella",
        "jarvis acorda",
        "suave",
        "de boa",
        "tamo junto",
    }:
        return True
    letters = [character for character in original if character.isalpha()]
    if len(letters) >= 4:
        upper_ratio = sum(character.isupper() for character in letters) / len(letters)
        return upper_ratio >= 0.7 and len(text.split()) <= 10
    return False


def _recent_conflict(context: list[dict[str, str]]) -> bool:
    recent_text = " ".join(
        normalize_text(str(item.get("text", item.get("content", ""))))
        for item in context[-6:]
    )
    return _looks_aggressive(recent_text)
