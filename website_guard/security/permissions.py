from __future__ import annotations

from typing import Iterable

import discord


class PermissionEngine:
    """Calcula permissões efetivas conforme a ordem oficial do Discord."""

    @staticmethod
    def combine_guild_permissions(role_permissions: Iterable[int], *, owner: bool = False) -> int:
        if owner:
            return discord.Permissions.all().value
        value = 0
        for permissions in role_permissions:
            value |= int(permissions)
        if discord.Permissions(value).administrator:
            return discord.Permissions.all().value
        return value

    @staticmethod
    def apply_channel_overwrites(
        base_permissions: int,
        *,
        everyone: tuple[int, int] = (0, 0),
        roles: Iterable[tuple[int, int]] = (),
        member: tuple[int, int] = (0, 0),
        owner: bool = False,
    ) -> int:
        if owner or discord.Permissions(base_permissions).administrator:
            return discord.Permissions.all_channel().value
        value = int(base_permissions)
        value = PermissionEngine._apply_pair(value, everyone)
        role_deny = 0
        role_allow = 0
        for allow, deny in roles:
            role_allow |= int(allow)
            role_deny |= int(deny)
        value &= ~role_deny
        value |= role_allow
        value = PermissionEngine._apply_pair(value, member)
        return value

    @staticmethod
    def enabled_names(value: int, names: Iterable[str]) -> list[str]:
        permissions = discord.Permissions(int(value))
        return [name for name in names if bool(getattr(permissions, name, False))]

    @staticmethod
    def _apply_pair(value: int, pair: tuple[int, int]) -> int:
        allow, deny = pair
        value &= ~int(deny)
        value |= int(allow)
        return value
