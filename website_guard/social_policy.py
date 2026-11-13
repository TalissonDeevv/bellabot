from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .state import StateStore

SOCIAL_CHANNELS_SECTION = "social_humor_channels"


@dataclass(frozen=True)
class SocialChannelConfig:
    guild_id: int
    channel_ids: tuple[int, ...]

    def allows(self, channel_id: int) -> bool:
        return channel_id in self.channel_ids


class SocialChannelPolicy:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    async def config(self, guild_id: int) -> SocialChannelConfig:
        section = await self.state.get_section(SOCIAL_CHANNELS_SECTION)
        raw = section.get(str(guild_id), {})
        values = raw.get("channel_ids", []) if isinstance(raw, dict) else []
        channel_ids = tuple(
            sorted(
                {
                    int(value)
                    for value in values
                    if str(value).strip().isdigit()
                }
            )
        )
        return SocialChannelConfig(guild_id=guild_id, channel_ids=channel_ids)

    async def set_channel(
        self,
        guild_id: int,
        channel_id: int,
        *,
        enabled: bool,
    ) -> SocialChannelConfig:
        current = await self.config(guild_id)
        channel_ids = set(current.channel_ids)
        if enabled:
            channel_ids.add(channel_id)
        else:
            channel_ids.discard(channel_id)
        await self.state.update_section(
            SOCIAL_CHANNELS_SECTION,
            str(guild_id),
            {
                "guild_id": guild_id,
                "channel_ids": sorted(channel_ids),
            },
        )
        return SocialChannelConfig(
            guild_id=guild_id,
            channel_ids=tuple(sorted(channel_ids)),
        )

    async def allows(self, guild_id: int, channel_id: int) -> bool:
        return (await self.config(guild_id)).allows(channel_id)

    async def snapshot(self, guild_id: int) -> dict[str, Any]:
        config = await self.config(guild_id)
        return {
            "guild_id": config.guild_id,
            "channel_ids": list(config.channel_ids),
        }
