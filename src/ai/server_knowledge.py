from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import discord
from rapidfuzz import fuzz

from website_guard.state import StateStore

from .intent_router import normalize_text
from .moderation_ai import contains_sensitive_data

INSTITUTIONAL_NAMES = {
    "sobre",
    "faq",
    "anuncios",
    "anúncios",
    "roadmap",
    "changelog",
    "status",
    "suporte",
    "feedbacks",
    "regras",
    "informacoes",
    "informações",
    "boas-vindas",
    "novidades",
    "atualizacoes",
    "atualizações",
    "contato",
}
EXCLUDED_NAMES = {
    "logs",
    "log",
    "auditoria",
    "tecnico",
    "técnico",
    "security",
    "seguranca",
    "segurança",
    "tickets",
}


@dataclass(frozen=True)
class ServerKnowledgeEntry:
    guild_id: int
    channel_id: int
    channel_name: str
    channel_mention: str
    category: str
    topic: str
    content: str
    indexed_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServerKnowledgeEntry":
        return cls(
            guild_id=int(data.get("guild_id", 0)),
            channel_id=int(data.get("channel_id", 0)),
            channel_name=str(data.get("channel_name", "")),
            channel_mention=str(data.get("channel_mention", "")),
            category=str(data.get("category", "")),
            topic=str(data.get("topic", "")),
            content=str(data.get("content", "")),
            indexed_at=str(data.get("indexed_at", "")),
        )


class DiscordKnowledgeIndex:
    def __init__(
        self,
        state: StateStore,
        *,
        enabled: bool = True,
        channel_ids: set[int] | None = None,
        max_channels: int = 30,
        messages_per_channel: int = 40,
    ) -> None:
        self.state = state
        self.enabled = enabled
        self.channel_ids = set(channel_ids or ())
        self.max_channels = max(1, min(100, max_channels))
        self.messages_per_channel = max(5, min(100, messages_per_channel))
        self._entries: dict[int, tuple[ServerKnowledgeEntry, ...]] = {}
        self._lock = asyncio.Lock()

    async def load_cached(self) -> int:
        values = await self.state.get_section("discord_knowledge_entries")
        loaded: dict[int, list[ServerKnowledgeEntry]] = {}
        for item in values.values():
            if not isinstance(item, dict):
                continue
            try:
                entry = ServerKnowledgeEntry.from_dict(item)
            except (TypeError, ValueError):
                continue
            loaded.setdefault(entry.guild_id, []).append(entry)
        async with self._lock:
            self._entries = {
                guild_id: tuple(entries)
                for guild_id, entries in loaded.items()
            }
        return sum(len(entries) for entries in loaded.values())

    async def refresh_guild(self, guild: discord.Guild) -> int:
        if not self.enabled:
            return 0
        entries: list[ServerKnowledgeEntry] = []
        channels = sorted(
            (
                channel
                for channel in guild.text_channels
                if self._is_indexable(channel)
            ),
            key=self._channel_priority,
            reverse=True,
        )[: self.max_channels]
        for channel in channels:
            entry = await self._read_channel(channel)
            if entry:
                entries.append(entry)
        now = datetime.now(timezone.utc).isoformat()

        def mutate(values: dict[str, Any]) -> None:
            prefix = f"{guild.id}:"
            for key in list(values):
                if key.startswith(prefix):
                    values.pop(key, None)
            for entry in entries:
                values[f"{guild.id}:{entry.channel_id}"] = entry.to_dict()

        await self.state.mutate_section("discord_knowledge_entries", mutate)
        await self.state.update_section(
            "discord_knowledge_status",
            str(guild.id),
            {
                "entries": len(entries),
                "updated_at": now,
            },
        )
        async with self._lock:
            self._entries[guild.id] = tuple(entries)
        return len(entries)

    async def search(
        self,
        guild_id: int,
        query: str,
        *,
        limit: int = 5,
    ) -> tuple[ServerKnowledgeEntry, ...]:
        if guild_id not in self._entries:
            await self.load_cached()
        async with self._lock:
            entries = self._entries.get(guild_id, ())
        normalized_query = normalize_text(query)
        ranked = sorted(
            (
                (self._score(normalized_query, entry), entry)
                for entry in entries
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        return tuple(
            entry
            for score, entry in ranked[: max(1, limit)]
            if score >= 0.36
        )

    async def answer(self, guild_id: int, query: str) -> str:
        matches = await self.search(guild_id, query, limit=3)
        if not matches:
            return ""
        primary = matches[0]
        text = normalize_text(query)
        if _asks_for_location(text):
            detail = _first_sentence(primary.topic or primary.content)
            return (
                f"Você encontra isso em {primary.channel_mention}."
                + (f" {detail}" if detail else "")
            )
        detail = _first_sentences(primary.content or primary.topic, 2)
        if detail:
            return f"{detail}\n\nO canal mais relacionado é {primary.channel_mention}."
        return f"O canal mais relacionado a isso é {primary.channel_mention}."

    def is_institutional_channel(self, channel: discord.abc.GuildChannel) -> bool:
        return isinstance(channel, discord.TextChannel) and self._is_indexable(channel)

    def _is_indexable(self, channel: discord.TextChannel) -> bool:
        if self.channel_ids:
            return channel.id in self.channel_ids
        normalized_name = normalize_text(channel.name).replace(" ", "-")
        normalized_category = normalize_text(
            channel.category.name if channel.category else ""
        ).replace(" ", "-")
        searchable_name = f"{normalized_category}-{normalized_name}"
        if any(value in searchable_name for value in EXCLUDED_NAMES):
            return False
        permissions = channel.permissions_for(channel.guild.default_role)
        if not permissions.view_channel or not permissions.read_message_history:
            return False
        return not channel.is_nsfw()

    @staticmethod
    def _channel_priority(channel: discord.TextChannel) -> tuple[int, int]:
        normalized_name = normalize_text(channel.name).replace(" ", "-")
        normalized_category = normalize_text(
            channel.category.name if channel.category else ""
        ).replace(" ", "-")
        words = set(re.split(r"[-_ ]+", f"{normalized_category}-{normalized_name}"))
        institutional = bool(
            words & {normalize_text(name) for name in INSTITUTIONAL_NAMES}
        )
        informative = bool((channel.topic or "").strip())
        return (
            2 if institutional else 1 if informative else 0,
            -channel.position,
        )

    async def _read_channel(
        self,
        channel: discord.TextChannel,
    ) -> ServerKnowledgeEntry | None:
        messages: list[str] = []
        try:
            pinned = await channel.pins()
        except (discord.Forbidden, discord.HTTPException):
            pinned = []
        for message in pinned[:20]:
            value = _safe_message_content(message)
            if value:
                messages.append(value)
        try:
            async for message in channel.history(
                limit=self.messages_per_channel,
                oldest_first=False,
            ):
                value = _safe_message_content(message)
                if value and value not in messages:
                    messages.append(value)
        except (discord.Forbidden, discord.HTTPException):
            pass
        topic = " ".join((channel.topic or "").split())[:600]
        content = "\n".join(reversed(messages[: self.messages_per_channel]))
        if not topic and not content and not channel.name:
            return None
        return ServerKnowledgeEntry(
            guild_id=channel.guild.id,
            channel_id=channel.id,
            channel_name=channel.name,
            channel_mention=channel.mention,
            category=channel.category.name if channel.category else "",
            topic=topic,
            content=content[:9000],
            indexed_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _score(query: str, entry: ServerKnowledgeEntry) -> float:
        name = normalize_text(entry.channel_name)
        category = normalize_text(entry.category)
        topic = normalize_text(entry.topic)
        content = normalize_text(entry.content[:4000])
        direct = fuzz.token_set_ratio(query, f"{name} {category}") / 100
        semantic = fuzz.token_set_ratio(query, f"{topic} {content}") / 100
        keyword_bonus = 0.18 if name and name in query else 0.0
        return min(1.0, direct * 0.55 + semantic * 0.45 + keyword_bonus)


def _safe_message_content(message: discord.Message) -> str:
    permissions = getattr(message.author, "guild_permissions", None)
    trusted_author = bool(
        getattr(message, "pinned", False)
        or getattr(message.author, "bot", False)
        or (
            permissions
            and (
                getattr(permissions, "administrator", False)
                or getattr(permissions, "manage_guild", False)
                or getattr(permissions, "manage_messages", False)
            )
        )
    )
    if not trusted_author:
        return ""
    content = " ".join(message.clean_content.split()).strip()
    if not content or contains_sensitive_data(content):
        return ""
    if len(content) < 8 and not message.attachments:
        return ""
    return f"{message.author.display_name}: {content[:1200]}"


def _asks_for_location(text: str) -> bool:
    return any(
        value in text
        for value in (
            "onde",
            "qual canal",
            "aonde",
            "como encontro",
            "onde vejo",
        )
    )


def _first_sentence(value: str) -> str:
    return _first_sentences(value, 1)


def _first_sentences(value: str, count: int) -> str:
    clean = " ".join(value.split()).strip()
    if not clean:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", clean)
    return " ".join(parts[:count])[:700]
