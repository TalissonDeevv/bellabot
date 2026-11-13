from __future__ import annotations

from typing import Any

from .bella_voice_guard import filter_history_for_bella
from .intent_router import normalize_text


def bella_message_needs_context(message: str) -> bool:
    text = normalize_text(message).strip(" ?!.,")
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "isso",
            "disso",
            "nisso",
            "aquilo",
            "essa",
            "esse",
            "essas",
            "esses",
            "ele",
            "ela",
            "dele",
            "dela",
            "nome dele",
            "nome dela",
            "essa personagem",
            "que vc mandou",
            "que voce mandou",
            "mensagem anterior",
            "papo anterior",
            "o que acha disso",
            "oque acha disso",
            "olha isso",
            "continua",
            "continue",
        )
    )


def select_bella_context(
    message: str,
    channel_history: list[dict[str, Any]] | None,
    *,
    limit: int = 8,
) -> list[dict[str, str]]:
    """Evita contaminar papo casual com assunto antigo do canal."""

    if not bella_message_needs_context(message):
        return []
    filtered = filter_history_for_bella(channel_history or [])
    result: list[dict[str, str]] = []
    for item in filtered[-limit:]:
        author = str(item.get("author", "")).strip()
        text = " ".join(str(item.get("text", "")).split()).strip()
        if not text:
            continue
        result.append({"author": author, "text": text[:220]})
    return result


def recent_bella_replies(channel_history: list[dict[str, Any]] | None, *, limit: int = 5) -> list[str]:
    replies: list[str] = []
    for item in reversed(channel_history or []):
        author = normalize_text(str(item.get("author", "")))
        if "bella" not in author and "nyelle" not in author and "jarvis" not in author:
            continue
        text = " ".join(str(item.get("text", "")).split()).strip()
        if text:
            replies.append(text[:180])
        if len(replies) >= limit:
            break
    replies.reverse()
    return replies
