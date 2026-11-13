from __future__ import annotations

import re

from .personality import FORBIDDEN_PHRASES

SENSITIVE_PATTERNS = (
    re.compile(r"\b(?:sk|AIza|AQ)[._-][A-Za-z0-9_-]{12,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
    re.compile(r"\b(?:token|api[_ -]?key|senha|password|secret)\s*[:=]\s*\S+", re.I),
)
CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:\d+>")
MASS_MENTION_PATTERN = re.compile(r"@(?:everyone|here)\b", re.IGNORECASE)
UNICODE_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001FAFF"
    "\u2300-\u23FF"
    "\u2600-\u27BF"
    "\uFE0F"
    "\u200D"
    "]+"
)


def neutralize_mass_mentions(value: str) -> str:
    """Prevents generated text from pinging or visually amplifying global mentions."""

    def replace(match: re.Match[str]) -> str:
        mention = match.group(0)
        return "@" + "\u200b" + mention[1:]

    return MASS_MENTION_PATTERN.sub(replace, value)


def sanitize_output(value: str, max_length: int = 1850) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = neutralize_mass_mentions(normalized)
    normalized = CUSTOM_EMOJI_PATTERN.sub("", normalized)
    normalized = UNICODE_EMOJI_PATTERN.sub("", normalized)
    lines = [
        re.sub(r"[ \t]{2,}", " ", re.sub(r"[ \t]+$", "", line))
        for line in normalized.split("\n")
    ]
    answer = "\n".join(lines).strip()
    answer = re.sub(r"\n{4,}", "\n\n\n", answer)
    lowered = answer.casefold()
    if any(phrase in lowered for phrase in FORBIDDEN_PHRASES):
        return ""
    if any(pattern.search(answer) for pattern in SENSITIVE_PATTERNS):
        return ""
    if len(answer) <= max_length:
        return answer
    clipped = answer[:max_length]
    boundary = max(
        clipped.rfind("\n\n"),
        clipped.rfind(". "),
        clipped.rfind("! "),
        clipped.rfind("? "),
    )
    minimum_boundary = min(600, max(80, int(max_length * 0.55)))
    if boundary >= minimum_boundary:
        return clipped[: boundary + 1].strip()
    return clipped.rsplit(" ", 1)[0].strip()


def contains_sensitive_data(value: str) -> bool:
    return any(pattern.search(value) for pattern in SENSITIVE_PATTERNS)
