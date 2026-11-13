from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .moderation_ai import contains_sensitive_data


@dataclass(frozen=True)
class ChannelMessage:
    message_id: int
    author_id: int
    author_name: str
    text: str
    created_at: datetime

    def to_prompt(self) -> dict[str, str]:
        return {
            "author": self.author_name,
            "text": self.text,
        }


class ChannelContextBuffer:
    def __init__(
        self,
        *,
        max_messages: int = 30,
        ttl_minutes: int = 15,
    ) -> None:
        self.max_messages = max(10, min(50, max_messages))
        self.ttl = timedelta(minutes=max(5, min(30, ttl_minutes)))
        self._channels: dict[tuple[int, int], deque[ChannelMessage]] = defaultdict(
            lambda: deque(maxlen=self.max_messages)
        )

    def observe(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        author_id: int,
        author_name: str,
        text: str,
    ) -> None:
        clean = " ".join(text.split()).strip()
        if not clean or contains_sensitive_data(clean):
            return
        key = (guild_id, channel_id)
        self._prune(key)
        self._channels[key].append(
            ChannelMessage(
                message_id=message_id,
                author_id=author_id,
                author_name=author_name[:80],
                text=clean[:600],
                created_at=datetime.now(timezone.utc),
            )
        )

    def recent(
        self,
        guild_id: int,
        channel_id: int,
        *,
        exclude_message_id: int | None = None,
        limit: int = 25,
    ) -> list[dict[str, str]]:
        key = (guild_id, channel_id)
        self._prune(key)
        values = [
            item.to_prompt()
            for item in self._channels.get(key, ())
            if item.message_id != exclude_message_id
        ]
        return values[-max(1, min(30, limit)) :]

    def _prune(self, key: tuple[int, int]) -> None:
        values = self._channels.get(key)
        if not values:
            return
        cutoff = datetime.now(timezone.utc) - self.ttl
        while values and values[0].created_at < cutoff:
            values.popleft()
        if not values:
            self._channels.pop(key, None)
