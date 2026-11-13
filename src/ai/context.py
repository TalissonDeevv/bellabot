from __future__ import annotations

import re
from dataclasses import dataclass

from website_guard.knowledge import KnowledgeBase, KnowledgeEntry

from .bella_voice_guard import filter_history_for_bella, is_bella_author, is_generic_bella_text
from .intent_router import normalize_text
from .memory import ConversationMemory, known_people_facts
from .official_knowledge import curated_entries
from .server_knowledge import DiscordKnowledgeIndex, ServerKnowledgeEntry


@dataclass(frozen=True)
class PublicContext:
    history: list[dict[str, str]]
    channel_history: list[dict[str, str]]
    site_entries: tuple[KnowledgeEntry, ...]
    server_entries: tuple[ServerKnowledgeEntry, ...]
    short_term_facts: dict[str, str]

    def site_payload(self) -> list[dict[str, str]]:
        return [
            {
                "entry_id": entry.entry_id,
                "question": entry.question[:500],
                "title": entry.source_title,
                "type": entry.source_type,
                "url": entry.source_url,
                "content": entry.answer[:1000],
            }
            for entry in self.site_entries
        ]

    def server_payload(self) -> list[dict[str, str]]:
        return [
            {
                "channel": entry.channel_mention,
                "name": entry.channel_name,
                "category": entry.category,
                "topic": entry.topic[:600],
                "content": entry.content[:1400],
            }
            for entry in self.server_entries
        ]


class AIContextBuilder:
    def __init__(
        self,
        knowledge: KnowledgeBase,
        memory: ConversationMemory,
        server_knowledge: DiscordKnowledgeIndex | None = None,
    ) -> None:
        self.knowledge = knowledge
        self.memory = memory
        self.server_knowledge = server_knowledge

    async def public(
        self,
        message: str,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
        recent_context: list[dict[str, str]] | None = None,
    ) -> PublicContext:
        history = filter_history_for_bella(
            await self.memory.history(guild_id, channel_id, user_id)
        )
        channel_history = _compact_channel_history(recent_context or ())
        previous_parts = [
            str(item.get("text", ""))
            for item in history[-4:]
            if str(item.get("text", "")).strip()
        ]
        previous_context = " ".join(previous_parts)
        retrieval_parts = [*previous_parts, message]
        retrieval_query = " ".join(retrieval_parts)[-1600:]
        current_specialized = _needs_specialized_context(message)
        follow_up_specialized = (
            _is_referential_follow_up(message)
            and _needs_specialized_context(previous_context)
        )
        specialized = current_specialized or follow_up_specialized
        curated = curated_entries(message if current_specialized else retrieval_query)
        if specialized:
            _, retrieved = await self.knowledge.candidates(
                retrieval_query,
                limit=4,
                enforce_question=False,
            )
        else:
            retrieved = ()
        entries = tuple(dict.fromkeys((*curated, *retrieved)))[:5]
        server_entries = (
            await self.server_knowledge.search(guild_id, retrieval_query, limit=3)
            if self.server_knowledge
            and (
                _needs_server_context(message)
                or (
                    _is_referential_follow_up(message)
                    and _needs_server_context(previous_context)
                )
            )
            else ()
        )
        facts = {
            **known_people_facts(),
            **await self.memory.facts(guild_id, user_id),
        }
        return PublicContext(
            history=history[-10:],
            channel_history=channel_history,
            site_entries=entries,
            server_entries=server_entries,
            short_term_facts=facts,
        )


def catalog_item(item_id: int, name: str, extra: str = "") -> dict[str, object]:
    payload: dict[str, object] = {"id": item_id, "name": name}
    if extra:
        payload["extra"] = extra
    return payload


def _compact_channel_history(
    values: list[dict[str, str]] | tuple[dict[str, str], ...],
) -> list[dict[str, str]]:
    compact: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in reversed(list(values)):
        author = str(item.get("author", "")).strip()[:80]
        text = " ".join(str(item.get("text", "")).split()).strip()[:500]
        if not text:
            continue
        if is_bella_author(author) and is_generic_bella_text(text):
            continue
        key = (normalize_text(author), normalize_text(text))
        if key in seen:
            continue
        seen.add(key)
        compact.append({"author": author, "text": text})
        if len(compact) >= 15:
            break
    compact.reverse()
    return compact


def _needs_specialized_context(value: str) -> bool:
    text = normalize_text(value)
    return any(
        term in text
        for term in (
            "talisson",
            "talinho",
            "talinhodev",
            "cheetah",
            "werneck",
            "portfolio",
            "site oficial",
            "projeto",
            "empresa",
            "contato",
            "instagram",
            "linkedin",
            "github",
            "whatsapp",
            "square cloud",
            "squarecloud",
            "hospedado",
            "hospedagem",
            "quem te criou",
            "quem criou voce",
        )
    )


def _needs_server_context(value: str) -> bool:
    text = normalize_text(value)
    return any(
        term in text
        for term in (
            "servidor",
            "comunidade",
            "canal",
            "regras",
            "faq",
            "changelog",
            "roadmap",
            "suporte",
            "onde vejo",
            "onde encontro",
        )
    )


def _is_referential_follow_up(value: str) -> bool:
    text = normalize_text(value)
    return bool(
        re.search(
            r"\b(?:ele|ela|dele|dela|isso|esse projeto|essa empresa|"
            r"nesse canal)\b",
            text,
        )
    )
