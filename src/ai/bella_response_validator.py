from __future__ import annotations

import re
from dataclasses import dataclass

from .bella_voice_guard import is_generic_bella_response
from .bella_voice_pack import forbidden_bad_patterns
from .intent_router import normalize_text
from .moderation_ai import sanitize_output


@dataclass(frozen=True)
class BellaValidation:
    acceptable: bool
    cleaned: str
    reasons: tuple[str, ...] = ()


def validate_bella_response(
    user_message: str,
    response: str,
    *,
    recent_responses: list[str] | None = None,
    max_chars: int = 500,
) -> BellaValidation:
    cleaned = sanitize_output(_clean_response(response), max_chars)
    reasons: list[str] = []
    if not cleaned:
        reasons.append("empty")
    if _contains_emoji(cleaned):
        reasons.append("emoji")
    if is_generic_bella_response(user_message, cleaned):
        reasons.append("generic_bella")
    if _looks_corporate(cleaned):
        reasons.append("corporate")
    if _copies_recent(cleaned, recent_responses or []):
        reasons.append("repetition")
    if _repeats_recent_opening(cleaned, recent_responses or []):
        reasons.append("repeated_opening")
    if _contains_forbidden_pattern(cleaned):
        reasons.append("forbidden_example")
    if _looks_like_prompt_leak(cleaned) or _looks_like_format_leak(user_message, cleaned):
        reasons.append("prompt_leak")
    return BellaValidation(not reasons, cleaned, tuple(reasons))


def _clean_response(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^```(?:text|markdown)?\s*|\s*```$", "", text, flags=re.I)
    text = re.sub(r"(?i)^\s*(?:bella\s*[:：-]|resposta\s*[:：-])\s*", "", text)
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    # Travess\u00e3o \u00e9 uma das marcas mais cl\u00e1ssicas de texto gerado por IA.
    text = re.sub(r"\s*\u2014\s*|\s+\u2013\s+", ", ", text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _contains_emoji(value: str) -> bool:
    return bool(
        re.search(
            "[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U00002600-\U000026FF]",
            value,
        )
    )


def _looks_corporate(value: str) -> bool:
    text = normalize_text(value)
    markers = (
        "como posso ajudar",
        "como posso ajuda-lo",
        "estou a disposicao",
        "fico a disposicao",
        "sua solicitacao",
        "solicitacao foi",
        "compreendo perfeitamente",
        "conforme informado",
        "posso auxilia-lo",
        "em que posso ajudar",
        "espero ter ajudado",
    )
    return any(marker in text for marker in markers)


def _contains_forbidden_pattern(value: str) -> bool:
    text = normalize_text(value)
    for pattern in forbidden_bad_patterns():
        normalized = normalize_text(pattern)
        if len(normalized) >= 8 and normalized in text:
            return True
    return False


def _copies_recent(value: str, recent: list[str]) -> bool:
    text = normalize_text(value)
    if not text:
        return False
    for item in recent:
        other = normalize_text(item)
        if other and (text == other or (len(text) > 25 and text in other) or (len(other) > 25 and other in text)):
            return True
    return False


def _repeats_recent_opening(value: str, recent: list[str]) -> bool:
    opening = _opening_signature(value)
    if not opening:
        return False
    for item in recent[-4:]:
        if opening == _opening_signature(item):
            return True
    return False


def _opening_signature(value: str) -> str:
    text = normalize_text(value)
    text = re.sub(r"[^\w\s]", " ", text)
    words = [word for word in re.split(r"\s+", text.strip()) if word]
    if len(words) < 3:
        return ""
    if words[0] in {"oi", "oie", "oiee", "ola", "opa"} and words[1] not in {
        "to",
        "tudo",
        "vc",
        "voce",
        "ta",
        "tá",
    }:
        return " ".join(words[:2])
    return " ".join(words[:4])


def _looks_like_prompt_leak(value: str) -> bool:
    text = normalize_text(value)
    return any(
        marker in text
        for marker in (
            "system prompt",
            "prompt de sistema",
            "persona oficial",
            "instrucoes internas",
            "instruções internas",
            "exemplos positivos",
            "anti-exemplos",
        )
    )


def _looks_like_format_leak(user_message: str, response: str) -> bool:
    user_text = normalize_text(user_message)
    text = normalize_text(response)
    if "json" in user_text or "schema" in user_text:
        return False
    return any(
        marker in text
        for marker in (
            "responder aqui em json",
            "vou responder em json",
            "vou responder aqui em json",
            "responderei em json",
            "formato json",
            "objeto json",
            "schema json",
            "como a bella costuma fazer no discord",
            "como a bella costuma fazer",
            "do jeito que a bella costuma",
            "qualquer coisa e so chamar",
        )
    )
