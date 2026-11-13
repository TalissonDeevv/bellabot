from __future__ import annotations

from typing import Any

import discord

from .normalizer import SnapshotNormalizer

SCHEMA_VERSION = 2
SENSITIVE_PERMISSIONS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_webhooks",
    "ban_members",
    "kick_members",
    "moderate_members",
    "manage_messages",
    "mention_everyone",
    "manage_events",
    "manage_threads",
    "view_audit_log",
)


class SecurityCollector:
    """Coleta somente dados suportados pela API oficial do Discord."""

    def __init__(self) -> None:
        self.normalizer = SnapshotNormalizer()

    async def collect(self, guild: discord.Guild) -> tuple[dict[str, Any], list[str]]:
        errors: list[str] = []
        areas = {"guild", "roles", "members", "channels", "bots"}
        unavailable: set[str] = set()

        roles = [self._role(role) for role in guild.roles]
        members = [self._member(member, guild) for member in guild.members if self._is_relevant_member(member, guild)]
        channels = [self._channel(channel, guild) for channel in guild.channels]
        bots = [self._bot(member) for member in guild.members if member.bot]

        automod = await self._fetch(guild.fetch_automod_rules, "automod", errors, unavailable)
        webhooks = await self._fetch(guild.webhooks, "webhooks", errors, unavailable)
        integrations = await self._fetch(guild.integrations, "integrations", errors, unavailable)
        if "automod" not in unavailable:
            areas.add("automod")
        if "webhooks" not in unavailable:
            areas.add("webhooks")
        if "integrations" not in unavailable:
            areas.add("integrations")

        audit_entries: list[dict[str, Any]] = []
        if guild.me is not None and guild.me.guild_permissions.view_audit_log:
            try:
                async for entry in guild.audit_logs(limit=25):
                    audit_entries.append(
                        {
                            "id": int(entry.id),
                            "action": str(entry.action),
                            "user_id": int(entry.user.id) if entry.user else None,
                            "target_id": int(entry.target.id) if getattr(entry.target, "id", None) else None,
                            "created_at": entry.created_at.isoformat(),
                        }
                    )
                areas.add("audit_log")
            except (discord.Forbidden, discord.HTTPException) as exc:
                errors.append(f"audit_log:{type(exc).__name__}")
                unavailable.add("audit_log")
        else:
            errors.append("audit_log:MissingPermission")
            unavailable.add("audit_log")

        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "guild": {
                "id": guild.id,
                "name": guild.name,
                "owner_id": guild.owner_id,
                "member_count": guild.member_count,
                "verification_level": int(guild.verification_level.value),
                "explicit_content_filter": int(guild.explicit_content_filter.value),
                "mfa_level": int(guild.mfa_level.value),
                "premium_tier": int(guild.premium_tier),
                "features": sorted(guild.features),
            },
            "roles": roles,
            "members": members,
            "channels": channels,
            "automod": [self._automod(item) for item in automod],
            "bots": bots,
            "webhooks": [self._webhook(item) for item in webhooks],
            "integrations": [self._integration(item) for item in integrations],
            "audit_log": audit_entries,
            "areas_analyzed": sorted(areas),
            "areas_unavailable": sorted(unavailable),
            "source_errors": sorted(errors),
        }
        return self.normalizer.normalize(snapshot), errors

    async def _fetch(self, callback: Any, area: str, errors: list[str], unavailable: set[str]) -> list[Any]:
        try:
            return list(await callback())
        except (discord.Forbidden, discord.HTTPException) as exc:
            errors.append(f"{area}:{type(exc).__name__}")
            unavailable.add(area)
            return []

    def _role(self, role: discord.Role) -> dict[str, Any]:
        return {
            "id": role.id,
            "name": role.name,
            "position": role.position,
            "permissions": role.permissions.value,
            "sensitive_permissions": self._sensitive(role.permissions),
            "managed": role.managed,
            "bot_managed": role.tags is not None and getattr(role.tags, "bot_id", None) is not None,
            "integration_managed": role.tags is not None and getattr(role.tags, "integration_id", None) is not None,
            "member_count": len(role.members),
        }

    def _member(self, member: discord.Member, guild: discord.Guild) -> dict[str, Any]:
        return {
            "id": member.id,
            "name": member.display_name,
            "bot": member.bot,
            "owner": member.id == guild.owner_id,
            "role_ids": sorted(role.id for role in member.roles),
            "role_names": [role.name for role in sorted(member.roles, key=lambda role: role.position, reverse=True)],
            "top_role_position": member.top_role.position,
            "guild_permissions": member.guild_permissions.value,
            "sensitive_permissions": self._sensitive(member.guild_permissions),
        }

    def _channel(self, channel: discord.abc.GuildChannel, guild: discord.Guild) -> dict[str, Any]:
        overwrites = []
        for target, overwrite in channel.overwrites.items():
            allow, deny = overwrite.pair()
            effective = channel.permissions_for(target)
            overwrites.append(
                {
                    "target_id": target.id,
                    "target_name": getattr(target, "name", str(target.id)),
                    "target_type": "role" if isinstance(target, discord.Role) else "member",
                    "allow": allow.value,
                    "deny": deny.value,
                    "sensitive_allow": self._sensitive(allow),
                    "effective_permissions": effective.value,
                    "effective_sensitive_permissions": self._sensitive(effective),
                }
            )
        return {
            "id": channel.id,
            "name": channel.name,
            "type": str(channel.type),
            "position": channel.position,
            "category_id": channel.category_id,
            "permissions_synced": getattr(channel, "permissions_synced", True),
            "public_view": channel.permissions_for(guild.default_role).view_channel,
            "overwrites": overwrites,
        }

    def _bot(self, member: discord.Member) -> dict[str, Any]:
        return {
            "id": member.id,
            "name": member.display_name,
            "role_ids": sorted(role.id for role in member.roles),
            "role_names": [role.name for role in sorted(member.roles, key=lambda role: role.position, reverse=True)],
            "top_role_position": member.top_role.position,
            "guild_permissions": member.guild_permissions.value,
            "sensitive_permissions": self._sensitive(member.guild_permissions),
        }

    @staticmethod
    def _automod(rule: discord.AutoModRule) -> dict[str, Any]:
        return {
            "id": rule.id,
            "name": rule.name,
            "enabled": rule.enabled,
            "creator_id": rule.creator_id,
            "event_type": int(rule.event_type.value),
            "trigger_type": int(rule.trigger.type.value) if rule.trigger.type else None,
            "trigger_metadata": rule.trigger.to_metadata_dict(),
            "actions": [action.to_dict() for action in rule.actions],
            "exempt_role_ids": sorted(int(item) for item in rule.exempt_role_ids),
            "exempt_channel_ids": sorted(int(item) for item in rule.exempt_channel_ids),
        }

    @staticmethod
    def _webhook(webhook: discord.Webhook) -> dict[str, Any]:
        return {
            "id": webhook.id,
            "name": webhook.name,
            "type": int(webhook.type.value),
            "channel_id": webhook.channel_id,
            "guild_id": webhook.guild_id,
            "user_id": webhook.user.id if webhook.user else None,
            "application_id": webhook.application_id,
        }

    @staticmethod
    def _integration(integration: discord.Integration) -> dict[str, Any]:
        role = getattr(integration, "role", None)
        account = getattr(integration, "account", None)
        return {
            "id": integration.id,
            "name": integration.name,
            "type": integration.type,
            "enabled": getattr(integration, "enabled", True),
            "role_id": role.id if role else None,
            "account_id": getattr(account, "id", None),
            "application_id": getattr(getattr(integration, "application", None), "id", None),
        }

    @staticmethod
    def _sensitive(permissions: discord.Permissions) -> list[str]:
        return [name for name in SENSITIVE_PERMISSIONS if bool(getattr(permissions, name, False))]

    @staticmethod
    def _is_relevant_member(member: discord.Member, guild: discord.Guild) -> bool:
        if member.id == guild.owner_id or member.bot:
            return True
        return any(bool(getattr(member.guild_permissions, name, False)) for name in SENSITIVE_PERMISSIONS)
