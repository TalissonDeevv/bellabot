from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import discord

from .identity import OWNER_DISCORD_ID
from .state import StateStore

AUTOMOD_CONFIG_SECTION = "discord_automod_config"
AUTOMOD_STATS_SECTION = "discord_automod_stats"
RULE_PREFIX = "Bella AutoMod | "
PRIMARY_KEYWORD_RULE_NAME = "Filtro Principal"
PRESET_RULE_NAME = "Conteúdo Sinalizado"
SPAM_RULE_NAME = "Spam"
HARMFUL_LINK_RULE_NAME = "Links Maliciosos"
MENTION_RULE_NAME = "Menções em Massa"
MEMBER_PROFILE_RULE_NAME = "Perfil de Membro"
MAX_NATIVE_REGEX_PATTERNS = 10
MAX_NATIVE_REGEX_PATTERN_LENGTH = 260
AUTOMOD_BADGE_REQUIRED_RULES = 100
DEFAULT_BLOCK_MESSAGE = (
    "Essa mensagem foi bloqueada automaticamente pelas regras "
    "de segurança do servidor."
)
AUTOMOD_PANEL_BANNER_ATTACHMENT = "jarvis-automod-banner.png"
LEGACY_KEYWORD_RULE_NAMES = {
    "Palavras Proibidas",
    "Links e Domínios Bloqueados",
    "Convites Discord",
}
DISCORD_INVITE_PATTERNS = [
    "discord.gg/",
    "discord.com/invite/",
    "discordapp.com/invite/",
]


@dataclass(frozen=True)
class AutoModGuildConfig:
    guild_id: int
    enabled: bool = True
    log_channel_id: int | None = None
    custom_message: str = DEFAULT_BLOCK_MESSAGE
    blocked_words: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    allowed_invites: tuple[str, ...] = ()
    allowed_phrases: tuple[str, ...] = ()
    regex_patterns: tuple[str, ...] = ()
    # Todos os regex cadastrados. A Bella confere cada mensagem contra eles (sem o
    # limite de 10 padrões/260 caracteres do nativo); os que o Discord aceita
    # também vão em regex_patterns, na regra nativa.
    bella_regex_patterns: tuple[str, ...] = ()
    mention_limit: int = 6
    mention_raid_protection: bool = True
    spam_enabled: bool = True
    harmful_links_enabled: bool = True
    mentions_enabled: bool = True
    keyword_presets: tuple[str, ...] = ()
    member_profile_enabled: bool = False
    timeout_duration_seconds: int = 0
    exempt_role_ids: tuple[int, ...] = ()
    exempt_channel_ids: tuple[int, ...] = ()
    exempt_user_ids: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "enabled": self.enabled,
            "log_channel_id": self.log_channel_id,
            "custom_message": self.custom_message,
            "blocked_words": list(self.blocked_words),
            "blocked_domains": list(self.blocked_domains),
            "allowed_domains": list(self.allowed_domains),
            "allowed_invites": list(self.allowed_invites),
            "allowed_phrases": list(self.allowed_phrases),
            "regex_patterns": list(self.regex_patterns),
            "bella_regex_patterns": list(self.bella_regex_patterns),
            "mention_limit": self.mention_limit,
            "mention_raid_protection": self.mention_raid_protection,
            "spam_enabled": self.spam_enabled,
            "harmful_links_enabled": self.harmful_links_enabled,
            "mentions_enabled": self.mentions_enabled,
            "keyword_presets": list(self.keyword_presets),
            "member_profile_enabled": self.member_profile_enabled,
            "timeout_duration_seconds": self.timeout_duration_seconds,
            "exempt_role_ids": list(self.exempt_role_ids),
            "exempt_channel_ids": list(self.exempt_channel_ids),
            "exempt_user_ids": list(self.exempt_user_ids),
        }

    @classmethod
    def from_dict(cls, guild_id: int, value: dict[str, Any] | None) -> "AutoModGuildConfig":
        value = value or {}
        return cls(
            guild_id=guild_id,
            enabled=bool(value.get("enabled", True)),
            log_channel_id=_optional_int(value.get("log_channel_id")),
            custom_message=str(value.get("custom_message") or DEFAULT_BLOCK_MESSAGE)[:150],
            blocked_words=tuple(_clean_list(value.get("blocked_words", []))),
            blocked_domains=tuple(_clean_list(value.get("blocked_domains", []))),
            allowed_domains=tuple(_clean_list(value.get("allowed_domains", []))),
            allowed_invites=tuple(_clean_list(value.get("allowed_invites", []))),
            allowed_phrases=tuple(_clean_list(value.get("allowed_phrases", []))),
            regex_patterns=tuple(_clean_regex_patterns(value.get("regex_patterns", []))),
            bella_regex_patterns=tuple(
                clean_bella_patterns(value.get("bella_regex_patterns", []) or value.get("regex_patterns", []))
            ),
            mention_limit=max(1, min(50, _safe_int(value.get("mention_limit"), 6))),
            mention_raid_protection=bool(value.get("mention_raid_protection", True)),
            spam_enabled=bool(value.get("spam_enabled", True)),
            harmful_links_enabled=bool(value.get("harmful_links_enabled", True)),
            mentions_enabled=bool(value.get("mentions_enabled", True)),
            keyword_presets=tuple(_clean_presets(value.get("keyword_presets", []))),
            member_profile_enabled=bool(value.get("member_profile_enabled", False)),
            timeout_duration_seconds=max(0, min(2419200, _safe_int(value.get("timeout_duration_seconds"), 0))),
            exempt_role_ids=tuple(_int_list(value.get("exempt_role_ids", []))),
            exempt_channel_ids=tuple(_int_list(value.get("exempt_channel_ids", []))),
            exempt_user_ids=tuple(_int_list(value.get("exempt_user_ids", []))),
        )


@dataclass(frozen=True)
class LocalAutoModRule:
    id: str
    name: str
    enabled: bool = True


@dataclass(frozen=True)
class AutoModStats:
    blocked_total: int = 0
    rules_created: int = 0
    rules_updated: int = 0
    rules_deleted: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)
    by_user: dict[str, int] = field(default_factory=dict)
    last_action_at: float = 0.0

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "AutoModStats":
        value = value or {}
        return cls(
            blocked_total=_safe_int(value.get("blocked_total")),
            rules_created=_safe_int(value.get("rules_created")),
            rules_updated=_safe_int(value.get("rules_updated")),
            rules_deleted=_safe_int(value.get("rules_deleted")),
            by_rule={
                str(key): _safe_int(count)
                for key, count in dict(value.get("by_rule", {})).items()
            },
            by_user={
                str(key): _safe_int(count)
                for key, count in dict(value.get("by_user", {})).items()
            },
            last_action_at=float(value.get("last_action_at", 0) or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked_total": self.blocked_total,
            "rules_created": self.rules_created,
            "rules_updated": self.rules_updated,
            "rules_deleted": self.rules_deleted,
            "by_rule": self.by_rule,
            "by_user": self.by_user,
            "last_action_at": self.last_action_at,
        }


@dataclass(frozen=True)
class AutoModBadgeProgress:
    total_rules: int = 0
    active_rules: int = 0
    guilds_scanned: int = 0
    guilds_failed: int = 0

    @property
    def missing(self) -> int:
        return max(0, AUTOMOD_BADGE_REQUIRED_RULES - self.total_rules)


class AutoModNativeSyncError(RuntimeError):
    """Erro de sincronização com o AutoMod nativo do Discord."""


class DiscordAutoModCenter:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    async def config(self, guild_id: int) -> AutoModGuildConfig:
        records = await self.state.get_section(AUTOMOD_CONFIG_SECTION)
        raw = records.get(str(guild_id))
        return AutoModGuildConfig.from_dict(
            guild_id,
            raw if isinstance(raw, dict) else {},
        )

    async def save_config(self, config: AutoModGuildConfig) -> None:
        await self.state.update_section(
            AUTOMOD_CONFIG_SECTION,
            str(config.guild_id),
            config.to_dict(),
        )

    async def rules(self, guild: discord.Guild) -> list[LocalAutoModRule]:
        return [
            LocalAutoModRule(str(rule.id), rule.name, rule.enabled)
            for rule in await self._fetch_native_jarvis_rules(guild)
        ]

    async def jarvis_rules(self, guild: discord.Guild) -> list[LocalAutoModRule]:
        rules = await self.rules(guild)
        return [rule for rule in rules if rule.name.startswith(RULE_PREFIX)]

    async def upsert_keyword_rule(
        self,
        guild: discord.Guild,
        *,
        name: str,
        words: list[str],
        regex_patterns: list[str] | None = None,
        allow_list: list[str] | None = None,
        reason: str,
    ) -> LocalAutoModRule:
        config = await self.config(guild.id)
        data = config.to_dict()
        if name == PRIMARY_KEYWORD_RULE_NAME:
            data["blocked_words"] = tuple(_clean_list(words))
            data["regex_patterns"] = tuple(_clean_regex_patterns(regex_patterns or config.regex_patterns))
            data["allowed_domains"] = tuple(_clean_list(allow_list or config.allowed_domains))
            await self.save_config(AutoModGuildConfig(**data))
        await self.sync_config(guild, reason=reason)
        native_rule = await self._native_rule_by_suffix(guild, name)
        if native_rule is not None:
            return LocalAutoModRule(str(native_rule.id), native_rule.name, native_rule.enabled)
        return LocalAutoModRule(id="native-pending", name=f"{RULE_PREFIX}{name}", enabled=True)

    async def upsert_spam_rule(
        self,
        guild: discord.Guild,
        *,
        reason: str,
    ) -> LocalAutoModRule:
        config = await self.config(guild.id)
        await self.save_config(
            AutoModGuildConfig(**{**config.to_dict(), "spam_enabled": True})
        )
        await self.sync_config(guild, reason=reason)
        return LocalAutoModRule("native-spam", f"{RULE_PREFIX}{SPAM_RULE_NAME}", True)

    async def upsert_harmful_link_rule(
        self,
        guild: discord.Guild,
        *,
        reason: str,
    ) -> LocalAutoModRule:
        config = await self.config(guild.id)
        await self.save_config(
            AutoModGuildConfig(**{**config.to_dict(), "harmful_links_enabled": True})
        )
        await self.sync_config(guild, reason=reason)
        return LocalAutoModRule("native-harmful-links", f"{RULE_PREFIX}{HARMFUL_LINK_RULE_NAME}", True)

    async def upsert_mention_rule(
        self,
        guild: discord.Guild,
        *,
        limit: int,
        reason: str,
    ) -> LocalAutoModRule:
        config = await self.config(guild.id)
        config = AutoModGuildConfig(
            **{
                **config.to_dict(),
                "mention_limit": max(1, min(50, limit)),
            }
        )
        await self.save_config(config)
        await self.sync_config(guild, reason=reason)
        return LocalAutoModRule("native-mentions", f"{RULE_PREFIX}{MENTION_RULE_NAME}", True)

    async def set_log_channel(
        self,
        guild: discord.Guild,
        channel_id: int | None,
        *,
        reason: str,
    ) -> None:
        config = await self.config(guild.id)
        config = AutoModGuildConfig(
            **{
                **config.to_dict(),
                "log_channel_id": channel_id,
            }
        )
        await self.save_config(config)
        await self.sync_config(guild, reason=reason)

    async def apply_template(
        self,
        guild: discord.Guild,
        template: str,
        *,
        reason: str,
    ) -> None:
        template_key = template.strip().casefold()
        if template_key == "programacao":
            words = ["free nitro", "crack", "token grabber", "stealer", "malware"]
            domains = ["discord.gift", "steamcomrnunity", "dlscord"]
            mention_limit = 6
        elif template_key == "empresa":
            words = ["suporte oficial falso", "administrador falso", "pix grátis"]
            domains = ["discord.gift", "bit.ly", "tinyurl.com"]
            mention_limit = 5
        elif template_key == "marketplace":
            words = ["dinheiro fácil", "lucro garantido", "dobro seu pix"]
            domains = ["discord.gift", "wa.me/grupo", "t.me/"]
            mention_limit = 4
        else:
            words = ["free nitro", "dinheiro grátis", "lucro garantido", "token grabber"]
            domains = ["discord.gift", "dlscord", "steamcomrnunity"]
            mention_limit = 6

        config = await self.config(guild.id)
        config = AutoModGuildConfig(
            **{
                **config.to_dict(),
                "blocked_words": tuple(sorted(set([*config.blocked_words, *words]))),
                "blocked_domains": tuple(sorted(set([*config.blocked_domains, *domains]))),
                "mention_limit": mention_limit,
            }
        )
        await self.save_config(config)
        await self.sync_config(guild, reason=reason)

    async def sync_config(self, guild: discord.Guild, *, reason: str) -> None:
        try:
            config = await self.config(guild.id)
            await self._delete_legacy_keyword_rules(guild, reason=reason)
            await self._sync_keyword_rule(guild, config, reason=reason)
            await self._sync_keyword_preset_rule(guild, config, reason=reason)
            await self._sync_spam_rule(guild, config, reason=reason)
            await self._sync_harmful_link_rule(guild, config, reason=reason)
            await self._sync_mention_rule(guild, config, reason=reason)
            await self._sync_member_profile_rule(guild, config, reason=reason)
            await self.increment(guild.id, "rules_updated")
        except discord.Forbidden as exc:
            raise AutoModNativeSyncError(
                "O Discord recusou a operação. A Bella precisa da permissão Gerenciar Servidor "
                "e, para castigo/timeout, também precisa da permissão Moderar Membros."
            ) from exc
        except discord.HTTPException as exc:
            raise AutoModNativeSyncError(_format_native_automod_error(exc)) from exc

    async def _delete_legacy_keyword_rules(
        self,
        guild: discord.Guild,
        *,
        reason: str,
    ) -> None:
        for rule in await self._fetch_native_jarvis_rules(guild):
            suffix = rule.name.removeprefix(RULE_PREFIX)
            if suffix in LEGACY_KEYWORD_RULE_NAMES:
                await rule.delete(reason=f"{reason} | consolidando regras antigas")
                await self.increment(guild.id, "rules_deleted")

    async def _delete_rule_by_suffix(
        self,
        guild: discord.Guild,
        suffix: str,
        *,
        reason: str,
    ) -> None:
        target_name = f"{RULE_PREFIX}{suffix}"
        for rule in await self._fetch_native_jarvis_rules(guild):
            if rule.name == target_name:
                await rule.delete(reason=reason)
                await self.increment(guild.id, "rules_deleted")

    @staticmethod
    def _combined_keyword_filter(config: AutoModGuildConfig) -> list[str]:
        return _clean_list(
            [
                *config.blocked_words,
                *(_wildcard_keyword(domain) for domain in config.blocked_domains),
                *(_wildcard_keyword(pattern) for pattern in DISCORD_INVITE_PATTERNS),
            ]
        )

    @staticmethod
    def _combined_allow_list(config: AutoModGuildConfig) -> list[str]:
        return _clean_list(
            [
                *config.allowed_phrases,
                *(_wildcard_keyword(domain) for domain in config.allowed_domains),
                *(_wildcard_keyword(invite) for invite in config.allowed_invites),
            ]
        )

    @staticmethod
    def _native_presets(config: AutoModGuildConfig) -> discord.AutoModPresets:
        presets = set(config.keyword_presets)
        return discord.AutoModPresets(
            profanity="profanity" in presets,
            sexual_content="sexual_content" in presets,
            slurs="slurs" in presets,
        )

    async def _sync_keyword_rule(
        self,
        guild: discord.Guild,
        config: AutoModGuildConfig,
        *,
        reason: str,
    ) -> None:
        keywords = self._combined_keyword_filter(config)
        patterns = _clean_regex_patterns(config.regex_patterns)
        if not keywords and not patterns:
            await self._delete_rule_by_suffix(
                guild,
                PRIMARY_KEYWORD_RULE_NAME,
                reason=f"{reason} | sem filtros de palavras, domínios ou regex",
            )
            return
        trigger = discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.keyword,
            keyword_filter=keywords,
            regex_patterns=patterns,
            allow_list=self._combined_allow_list(config),
        )
        await self._upsert_rule(
            guild,
            name=f"{RULE_PREFIX}{PRIMARY_KEYWORD_RULE_NAME}",
            event_type=discord.AutoModRuleEventType.message_send,
            trigger=trigger,
            actions=self._actions(config, timeout_supported=True),
            enabled=config.enabled,
            reason=reason,
        )

    async def _sync_spam_rule(
        self,
        guild: discord.Guild,
        config: AutoModGuildConfig,
        *,
        reason: str,
    ) -> None:
        if not config.spam_enabled:
            await self._delete_rule_by_suffix(
                guild,
                SPAM_RULE_NAME,
                reason=f"{reason} | spam nativo desativado",
            )
            return
        await self._upsert_rule(
            guild,
            name=f"{RULE_PREFIX}{SPAM_RULE_NAME}",
            event_type=discord.AutoModRuleEventType.message_send,
            trigger=discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.spam),
            actions=self._actions(config, timeout_supported=False),
            enabled=config.enabled,
            reason=reason,
        )

    async def _sync_harmful_link_rule(
        self,
        guild: discord.Guild,
        config: AutoModGuildConfig,
        *,
        reason: str,
    ) -> None:
        if not config.harmful_links_enabled:
            await self._delete_rule_by_suffix(
                guild,
                HARMFUL_LINK_RULE_NAME,
                reason=f"{reason} | links maliciosos nativo desativado",
            )
            return
        await self._upsert_rule(
            guild,
            name=f"{RULE_PREFIX}{HARMFUL_LINK_RULE_NAME}",
            event_type=discord.AutoModRuleEventType.message_send,
            trigger=discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.harmful_link),
            actions=self._actions(config, timeout_supported=False),
            enabled=config.enabled,
            reason=reason,
        )

    async def _sync_mention_rule(
        self,
        guild: discord.Guild,
        config: AutoModGuildConfig,
        *,
        reason: str,
    ) -> None:
        if not config.mentions_enabled:
            await self._delete_rule_by_suffix(
                guild,
                MENTION_RULE_NAME,
                reason=f"{reason} | menções nativas desativadas",
            )
            return
        await self._upsert_rule(
            guild,
            name=f"{RULE_PREFIX}{MENTION_RULE_NAME}",
            event_type=discord.AutoModRuleEventType.message_send,
            trigger=discord.AutoModTrigger(
                type=discord.AutoModRuleTriggerType.mention_spam,
                mention_limit=max(1, min(50, config.mention_limit)),
                mention_raid_protection=config.mention_raid_protection,
            ),
            actions=self._actions(config, timeout_supported=True),
            enabled=config.enabled,
            reason=reason,
        )

    async def _sync_keyword_preset_rule(
        self,
        guild: discord.Guild,
        config: AutoModGuildConfig,
        *,
        reason: str,
    ) -> None:
        if not config.keyword_presets:
            await self._delete_rule_by_suffix(
                guild,
                PRESET_RULE_NAME,
                reason=f"{reason} | conteúdo sinalizado vazio",
            )
            return
        await self._upsert_rule(
            guild,
            name=f"{RULE_PREFIX}{PRESET_RULE_NAME}",
            event_type=discord.AutoModRuleEventType.message_send,
            trigger=discord.AutoModTrigger(
                type=discord.AutoModRuleTriggerType.keyword_preset,
                presets=self._native_presets(config),
                allow_list=self._combined_allow_list(config),
            ),
            actions=self._actions(config, timeout_supported=False),
            enabled=config.enabled,
            reason=reason,
        )

    async def _sync_member_profile_rule(
        self,
        guild: discord.Guild,
        config: AutoModGuildConfig,
        *,
        reason: str,
    ) -> None:
        keywords = self._combined_keyword_filter(config)
        patterns = _clean_regex_patterns(config.regex_patterns)
        if not config.member_profile_enabled or (not keywords and not patterns):
            await self._delete_rule_by_suffix(
                guild,
                MEMBER_PROFILE_RULE_NAME,
                reason=f"{reason} | perfil de membro nativo desativado",
            )
            return
        await self._upsert_rule(
            guild,
            name=f"{RULE_PREFIX}{MEMBER_PROFILE_RULE_NAME}",
            event_type=discord.AutoModRuleEventType.member_update,
            trigger=discord.AutoModTrigger(
                type=discord.AutoModRuleTriggerType.member_profile,
                keyword_filter=keywords,
                regex_patterns=patterns,
                allow_list=self._combined_allow_list(config),
            ),
            actions=self._member_profile_actions(config),
            enabled=config.enabled,
            reason=reason,
        )

    async def delete_all_rules(self, guild: discord.Guild, *, reason: str) -> None:
        for rule in await self._fetch_native_jarvis_rules(guild):
            await rule.delete(reason=reason)
            await self.increment(guild.id, "rules_deleted")

    async def _fetch_native_jarvis_rules(
        self,
        guild: discord.Guild,
    ) -> list[discord.AutoModRule]:
        try:
            rules = await guild.fetch_automod_rules()
        except discord.HTTPException:
            return []
        jarvis_rules = [rule for rule in rules if rule.name.startswith(RULE_PREFIX)]
        self._last_native_rules = jarvis_rules
        return jarvis_rules

    async def _native_rule_by_suffix(
        self,
        guild: discord.Guild,
        suffix: str,
    ) -> discord.AutoModRule | None:
        target_name = f"{RULE_PREFIX}{suffix}"
        for rule in await self._fetch_native_jarvis_rules(guild):
            if rule.name == target_name:
                return rule
        return None

    async def delete_rule(
        self,
        rule: LocalAutoModRule,
        *,
        reason: str,
    ) -> None:
        for guild_rule in getattr(self, "_last_native_rules", []):
            if str(getattr(guild_rule, "id", "")) == str(rule.id):
                await guild_rule.delete(reason=reason)
                await self.increment(guild_rule.guild.id, "rules_deleted")
                return
        del reason

    async def native_rule_by_input(
        self,
        guild: discord.Guild,
        query: str,
    ) -> discord.AutoModRule | None:
        needle = query.strip().casefold()
        if not needle:
            return None
        for rule in await self._fetch_native_jarvis_rules(guild):
            suffix = rule.name.removeprefix(RULE_PREFIX)
            if str(rule.id) == query.strip():
                return rule
            if rule.name.casefold() == needle or suffix.casefold() == needle:
                return rule
            if needle in suffix.casefold():
                return rule
        return None

    async def set_native_rule_enabled(
        self,
        guild: discord.Guild,
        query: str,
        enabled: bool,
        *,
        reason: str,
    ) -> discord.AutoModRule:
        rule = await self.native_rule_by_input(guild, query)
        if rule is None:
            raise AutoModNativeSyncError(
                "Não encontrei essa regra criada pela Bella. Use o ID da regra ou o nome exibido na aba Gerenciar."
            )
        try:
            updated = await rule.edit(enabled=enabled, reason=reason)
        except discord.Forbidden as exc:
            raise AutoModNativeSyncError(
                "A Bella precisa da permissão Gerenciar Servidor para editar regras do AutoMod nativo."
            ) from exc
        except discord.HTTPException as exc:
            raise AutoModNativeSyncError(_format_native_automod_error(exc)) from exc
        await self.increment(guild.id, "rules_updated")
        return updated

    async def delete_native_rule_by_input(
        self,
        guild: discord.Guild,
        query: str,
        *,
        reason: str,
    ) -> None:
        rule = await self.native_rule_by_input(guild, query)
        if rule is None:
            raise AutoModNativeSyncError(
                "Não encontrei essa regra criada pela Bella. Use o ID da regra ou o nome exibido na aba Gerenciar."
            )
        try:
            await rule.delete(reason=reason)
        except discord.Forbidden as exc:
            raise AutoModNativeSyncError(
                "A Bella precisa da permissão Gerenciar Servidor para excluir regras do AutoMod nativo."
            ) from exc
        except discord.HTTPException as exc:
            raise AutoModNativeSyncError(_format_native_automod_error(exc)) from exc
        await self.increment(guild.id, "rules_deleted")

    async def export_config(self, guild_id: int) -> str:
        config = await self.config(guild_id)
        return json.dumps(config.to_dict(), ensure_ascii=False, indent=2)

    async def import_config(
        self,
        guild: discord.Guild,
        payload: str,
        *,
        reason: str,
    ) -> AutoModGuildConfig:
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("JSON inválido.")
        config = AutoModGuildConfig.from_dict(guild.id, data)
        await self.save_config(config)
        await self.sync_config(guild, reason=reason)
        return config

    # ------------------------------------------------------------------
    # Regex da Bella: todos os padrões ficam aqui; os que o Discord aceita
    # também vão para a regra nativa.
    async def update_regex(
        self,
        guild: discord.Guild,
        patterns: list[str],
        *,
        mode: str,
        actor: str,
    ) -> str:
        config = await self.config(guild.id)
        bella = list(config.bella_regex_patterns)
        native = list(config.regex_patterns)
        if mode == "remove":
            bella = [p for p in bella if p not in patterns]
            native = [p for p in native if p not in patterns]
            await self.save_config(AutoModGuildConfig(**{**config.to_dict(), "bella_regex_patterns": bella, "regex_patterns": native}))
            await self._try_sync(guild, reason=f"Regex removidos por {actor}")
            return f"removi {len(patterns)} padrão(ões). a Bella agora confere {len(bella)}."
        if mode == "import":
            bella, native = [], []
        for pattern in patterns:
            if pattern not in bella:
                bella.append(pattern)
        bella = clean_bella_patterns(bella)
        await self.save_config(AutoModGuildConfig(**{**config.to_dict(), "bella_regex_patterns": bella, "regex_patterns": native}))
        lines: list[str] = []
        for pattern in patterns:
            if pattern in native:
                lines.append(f"✅ já estava no nativo: `{pattern[:70]}`")
                continue
            if len(pattern) > MAX_NATIVE_REGEX_PATTERN_LENGTH:
                lines.append(f"🤖 só a Bella (passa de {MAX_NATIVE_REGEX_PATTERN_LENGTH} caracteres): `{pattern[:70]}…`")
                continue
            if len(native) >= MAX_NATIVE_REGEX_PATTERNS:
                lines.append(f"🤖 só a Bella (nativo cheio, {MAX_NATIVE_REGEX_PATTERNS}/10): `{pattern[:70]}`")
                continue
            current = await self.config(guild.id)
            await self.save_config(AutoModGuildConfig(**{**current.to_dict(), "regex_patterns": native + [pattern]}))
            error = await self._try_sync(guild, reason=f"Regex adicionado por {actor}")
            if error:
                current = await self.config(guild.id)
                await self.save_config(AutoModGuildConfig(**{**current.to_dict(), "regex_patterns": native}))
                await self._try_sync(guild, reason=f"Regex recusado pelo Discord desfeito ({actor})")
                lines.append(f"🤖 só a Bella (o Discord recusou: {error[:90]}): `{pattern[:70]}`")
            else:
                native.append(pattern)
                lines.append(f"✅ nativo + Bella: `{pattern[:70]}`")
        return (
            f"a Bella agora confere **{len(bella)}** regex (nativo: {len(native)}/{MAX_NATIVE_REGEX_PATTERNS}).\n"
            + "\n".join(lines)
        )

    async def _try_sync(self, guild: discord.Guild, *, reason: str) -> str:
        try:
            await self.sync_config(guild, reason=reason)
        except discord.Forbidden:
            return "sem permissão Gerenciar Servidor"
        except (discord.HTTPException, AutoModNativeSyncError) as exc:
            return str(exc)
        return ""

    async def check_message(self, message: discord.Message) -> bool:
        """A Bella confere a mensagem contra todos os regex. True se agiu."""
        if message.guild is None or not isinstance(message.author, discord.Member) or message.author.bot:
            return False
        text = message.content or ""
        if not text.strip():
            return False
        config = await self.config(message.guild.id)
        if not config.enabled or not config.bella_regex_patterns:
            return False
        member = message.author
        perms = member.guild_permissions
        channel_ids = {message.channel.id, getattr(message.channel, "category_id", None), getattr(message.channel, "parent_id", None)}
        if (
            member.id in {OWNER_DISCORD_ID, message.guild.owner_id}
            or perms.administrator
            or perms.manage_messages
            or member.id in config.exempt_user_ids
            or set(config.exempt_role_ids) & {role.id for role in member.roles}
            or set(config.exempt_channel_ids) & channel_ids
        ):
            return False
        normalized = normalize_for_regex(text)
        lowered = text.casefold()
        if any(phrase.casefold() in lowered or phrase.casefold() in normalized for phrase in config.allowed_phrases):
            return False
        matched = await self._first_match(config.bella_regex_patterns, (text, normalized))
        if matched is None:
            return False
        pattern, excerpt = matched
        actions = []
        try:
            await message.delete()
            actions.append("mensagem apagada")
        except discord.NotFound:
            actions.append("mensagem já tinha sumido")
        except (discord.Forbidden, discord.HTTPException) as exc:
            actions.append(f"não consegui apagar ({type(exc).__name__})")
        if config.timeout_duration_seconds > 0:
            try:
                await member.timeout(
                    timedelta(seconds=config.timeout_duration_seconds),
                    reason=f"AutoMod da Bella: regex `{pattern[:80]}`",
                )
                actions.append(f"timeout de {_format_duration(config.timeout_duration_seconds)}")
            except (discord.Forbidden, discord.HTTPException) as exc:
                actions.append(f"não consegui aplicar timeout ({type(exc).__name__})")
        try:
            await message.channel.send(
                f"{member.mention} {config.custom_message}",
                allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
                delete_after=12,
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
        await self._regex_alert(message, config, pattern, excerpt, actions)
        return True

    async def _first_match(self, patterns: tuple[str, ...], texts: tuple[str, ...]) -> tuple[str, str] | None:
        def scan() -> tuple[str, str] | None:
            for pattern in patterns:
                compiled = compile_bella_pattern(pattern)
                if compiled is None:
                    continue
                for text in texts:
                    found = compiled.search(text[:2000])
                    if found:
                        return pattern, found.group(0)
            return None

        try:
            # Em thread com prazo: um regex mal escrito não trava a Bella.
            return await asyncio.wait_for(asyncio.to_thread(scan), timeout=2.0)
        except asyncio.TimeoutError:
            return None

    async def _regex_alert(self, message: discord.Message, config: AutoModGuildConfig, pattern: str, excerpt: str, actions: list[str]) -> None:
        channel = message.guild.get_channel(config.log_channel_id or 0)
        if not isinstance(channel, discord.abc.Messageable):
            return
        embed = discord.Embed(title="🛡️ AutoMod da Bella: regex", color=discord.Color(0xE74C3C), timestamp=discord.utils.utcnow())
        embed.add_field(name="Membro", value=f"{message.author.mention} ({message.author})\nID `{message.author.id}`", inline=True)
        embed.add_field(name="Canal", value=message.channel.mention, inline=True)
        embed.add_field(name="Regex", value=f"`{pattern[:1000]}`", inline=False)
        embed.add_field(name="Trecho que casou", value=f"||{excerpt[:500]}||", inline=False)
        embed.add_field(name="Mensagem (ocultada)", value=f"||{(message.content or '')[:900]}||", inline=False)
        embed.add_field(name="O que eu fiz", value="\n".join(f"• {a}" for a in actions) or "-", inline=False)
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def record_action(self, action: discord.AutoModAction) -> None:
        await self.increment(
            action.guild_id,
            "blocked_total",
            rule_id=str(action.rule_id),
            user_id=str(action.user_id),
        )

    async def increment(
        self,
        guild_id: int,
        field: str,
        *,
        rule_id: str = "",
        user_id: str = "",
    ) -> None:
        def mutate(records: dict[str, Any]) -> None:
            current = AutoModStats.from_dict(records.get(str(guild_id)))
            by_rule = dict(current.by_rule)
            by_user = dict(current.by_user)
            if rule_id:
                by_rule[rule_id] = by_rule.get(rule_id, 0) + 1
            if user_id:
                by_user[user_id] = by_user.get(user_id, 0) + 1
            values = current.to_dict()
            values[field] = _safe_int(values.get(field)) + 1
            values["by_rule"] = by_rule
            values["by_user"] = by_user
            values["last_action_at"] = time.time()
            records[str(guild_id)] = values

        await self.state.mutate_section(AUTOMOD_STATS_SECTION, mutate)

    async def stats(self, guild_id: int) -> AutoModStats:
        records = await self.state.get_section(AUTOMOD_STATS_SECTION)
        raw = records.get(str(guild_id))
        return AutoModStats.from_dict(raw if isinstance(raw, dict) else {})

    async def badge_progress(
        self,
        guilds: list[discord.Guild] | tuple[discord.Guild, ...],
    ) -> AutoModBadgeProgress:
        total_rules = 0
        active_rules = 0
        guilds_scanned = 0
        guilds_failed = 0
        for guild in guilds:
            try:
                rules = await guild.fetch_automod_rules()
            except discord.HTTPException:
                guilds_failed += 1
                continue
            guilds_scanned += 1
            total_rules += len(rules)
            active_rules += sum(1 for rule in rules if rule.enabled)
        return AutoModBadgeProgress(
            total_rules=total_rules,
            active_rules=active_rules,
            guilds_scanned=guilds_scanned,
            guilds_failed=guilds_failed,
        )

    async def _upsert_rule(
        self,
        guild: discord.Guild,
        *,
        name: str,
        event_type: discord.AutoModRuleEventType,
        trigger: discord.AutoModTrigger,
        actions: list[discord.AutoModRuleAction],
        enabled: bool,
        reason: str,
    ) -> LocalAutoModRule:
        config = await self.config(guild.id)
        exempt_roles, exempt_channels = self._native_exemptions(guild, config)
        existing = await self._native_rule_by_name(guild, name)
        if existing is not None:
            updated = await existing.edit(
                name=name,
                event_type=event_type,
                trigger=trigger,
                actions=actions,
                enabled=enabled,
                exempt_roles=exempt_roles,
                exempt_channels=exempt_channels,
                reason=reason,
            )
            await self.increment(guild.id, "rules_updated")
            return LocalAutoModRule(str(updated.id), updated.name, updated.enabled)
        created = await guild.create_automod_rule(
            name=name,
            event_type=event_type,
            trigger=trigger,
            actions=actions,
            enabled=enabled,
            exempt_roles=exempt_roles,
            exempt_channels=exempt_channels,
            reason=reason,
        )
        await self.increment(guild.id, "rules_created")
        return LocalAutoModRule(str(created.id), created.name, created.enabled)

    async def _native_rule_by_name(
        self,
        guild: discord.Guild,
        name: str,
    ) -> discord.AutoModRule | None:
        for rule in await self._fetch_native_jarvis_rules(guild):
            if rule.name == name:
                return rule
        return None

    @staticmethod
    def _native_exemptions(
        guild: discord.Guild,
        config: AutoModGuildConfig,
    ) -> tuple[list[discord.Role], list[discord.abc.GuildChannel]]:
        roles = [
            role
            for role_id in config.exempt_role_ids
            if (role := guild.get_role(role_id)) is not None
        ]
        channels = [
            channel
            for channel_id in config.exempt_channel_ids
            if (channel := guild.get_channel(channel_id)) is not None
        ]
        return roles, channels

    @staticmethod
    def _actions(
        config: AutoModGuildConfig,
        *,
        timeout_supported: bool,
    ) -> list[discord.AutoModRuleAction]:
        actions = [
            discord.AutoModRuleAction(
                type=discord.AutoModRuleActionType.block_message,
                custom_message=(config.custom_message or DEFAULT_BLOCK_MESSAGE)[:150],
            )
        ]
        if config.log_channel_id:
            actions.append(
                discord.AutoModRuleAction(
                    type=discord.AutoModRuleActionType.send_alert_message,
                    channel_id=config.log_channel_id,
                )
            )
        if timeout_supported and config.timeout_duration_seconds > 0:
            actions.append(
                discord.AutoModRuleAction(
                    type=discord.AutoModRuleActionType.timeout,
                    duration=timedelta(seconds=config.timeout_duration_seconds),
                )
            )
        return actions

    @staticmethod
    def _member_profile_actions(config: AutoModGuildConfig) -> list[discord.AutoModRuleAction]:
        actions = [
            discord.AutoModRuleAction(
                type=discord.AutoModRuleActionType.block_member_interactions,
            )
        ]
        if config.log_channel_id:
            actions.append(
                discord.AutoModRuleAction(
                    type=discord.AutoModRuleActionType.send_alert_message,
                    channel_id=config.log_channel_id,
                )
            )
        return actions


class AutoModPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        center: DiscordAutoModCenter,
        guild: discord.Guild,
        owner_id: int,
        section: str = "overview",
        all_guilds: list[discord.Guild] | tuple[discord.Guild, ...] | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.center = center
        self.guild = guild
        self.owner_id = owner_id
        self.section = section
        self.all_guilds = tuple(all_guilds or (guild,))
        self._config: AutoModGuildConfig | None = None
        self._rules: list[discord.AutoModRule] = []
        self._stats = AutoModStats()
        self._badge_progress = AutoModBadgeProgress()

    async def prepare(self) -> "AutoModPanelView":
        self._config = await self.center.config(self.guild.id)
        self._rules = await self.center.rules(self.guild)
        self._stats = await self.center.stats(self.guild.id)
        if self.section == "badge":
            self._badge_progress = await self.center.badge_progress(self.all_guilds)
        self._build()
        return self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Esse painel precisa ser usado dentro do servidor.",
                ephemeral=True,
            )
            return False
        if interaction.user.id == OWNER_DISCORD_ID or interaction.user.guild_permissions.manage_guild:
            return True
        await interaction.response.send_message(
            "Você precisa da permissão Gerenciar Servidor para mexer no AutoMod.",
            ephemeral=True,
        )
        return False

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        del item
        message = (
            str(error)
            if isinstance(error, AutoModNativeSyncError)
            else f"Não consegui aplicar a configuração no AutoMod nativo: {type(error).__name__}."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message[:1900], ephemeral=True)
            else:
                await interaction.response.send_message(message[:1900], ephemeral=True)
        except discord.HTTPException:
            pass

    async def _sync_native(
        self,
        guild: discord.Guild | None = None,
        *,
        reason: str,
    ) -> None:
        await self.center.sync_config(guild or self.guild, reason=reason)

    def _build(self) -> None:
        self.clear_items()
        config = self._config or AutoModGuildConfig(self.guild.id)
        jarvis_rules = [rule for rule in self._rules if rule.name.startswith(RULE_PREFIX)]
        container = discord.ui.Container(accent_color=discord.Color(0x050505))
        container.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(
                    f"attachment://{AUTOMOD_PANEL_BANNER_ATTACHMENT}",
                    description="Bella AutoMod",
                )
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(
            discord.ui.TextDisplay(
                self._content_for_section(config, jarvis_rules)
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        nav = discord.ui.ActionRow()
        for key, label in (
            ("overview", "Visão Geral"),
            ("words", "Palavras"),
            ("links", "Links"),
            ("mentions", "Menções"),
            ("logs", "Logs"),
        ):
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary if key == self.section else discord.ButtonStyle.secondary,
                disabled=key == self.section,
            )

            async def callback(interaction: discord.Interaction, selected: str = key) -> None:
                await self._switch(interaction, selected)

            button.callback = callback
            nav.add_item(button)
        container.add_item(nav)

        nav2 = discord.ui.ActionRow()
        for key, label in (
            ("templates", "Templates"),
            ("regex", "Regex"),
            ("stats", "Estatísticas"),
            ("badge", "Badge"),
        ):
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary if key == self.section else discord.ButtonStyle.secondary,
                disabled=key == self.section,
            )

            async def callback(interaction: discord.Interaction, selected: str = key) -> None:
                await self._switch(interaction, selected)

            button.callback = callback
            nav2.add_item(button)
        refresh = discord.ui.Button(label="Atualizar", style=discord.ButtonStyle.success)
        refresh.callback = self._refresh
        nav2.add_item(refresh)
        container.add_item(nav2)

        nav3 = discord.ui.ActionRow()
        for key, label in (
            ("content", "Conteúdo"),
            ("actions", "Ações"),
            ("exemptions", "Exceções"),
            ("manage", "Gerenciar"),
        ):
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary if key == self.section else discord.ButtonStyle.secondary,
                disabled=key == self.section,
            )

            async def callback(interaction: discord.Interaction, selected: str = key) -> None:
                await self._switch(interaction, selected)

            button.callback = callback
            nav3.add_item(button)
        container.add_item(nav3)

        action_rows = [row for row in [self._action_row_for_section(), *self._extra_action_rows_for_section()] if row is not None]
        if action_rows:
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            for action_row in action_rows:
                container.add_item(action_row)
        container.add_item(
            discord.ui.TextDisplay(
                "-# A Bella configura estas regras pela API oficial do AutoMod nativo do Discord. "
                "Os regex também são conferidos pela própria Bella, inclusive os que o nativo não aceita."
            )
        )
        self.add_item(container)

    def _content_for_section(
        self,
        config: AutoModGuildConfig,
        jarvis_rules: list[discord.AutoModRule],
    ) -> str:
        enabled = sum(1 for rule in jarvis_rules if rule.enabled)
        disabled = len(jarvis_rules) - enabled
        if self.section == "words":
            words = "\n".join(f"- {word}" for word in config.blocked_words[:30]) or "Nenhuma palavra cadastrada ainda."
            return (
                "## AutoMod • Palavras Proibidas\n"
                "Gerencie a lista que a Bella envia para a regra oficial de palavras do Discord.\n\n"
                f"**Palavras cadastradas:** {len(config.blocked_words)}\n{words}"
            )
        if self.section == "regex":
            native = set(config.regex_patterns)
            items = []
            for index, pattern in enumerate(config.bella_regex_patterns[:25], start=1):
                shown = pattern if len(pattern) <= 90 else pattern[:87] + "…"
                tag = "✅ nativo + Bella" if pattern in native else "🤖 só Bella"
                items.append(f"{index}. {tag} • `{shown}`")
            extra = len(config.bella_regex_patterns) - 25
            listing = "\n".join(items) or "Nenhum regex cadastrado."
            if extra > 0:
                listing += f"\n… e mais {extra}."
            return (
                "## AutoMod • Regex\n"
                "Todos os regex ficam guardados aqui e **a Bella confere cada mensagem contra todos** "
                "(sem o limite do Discord, e lendo até fontes especiais, letras de outros alfabetos e "
                "letras separadas por espaço). Os que o Discord aceita também vão para a regra nativa, "
                "que bloqueia antes da mensagem aparecer.\n\n"
                f"**Regex da Bella:** {len(config.bella_regex_patterns)}  •  **No nativo:** {len(native)}/{MAX_NATIVE_REGEX_PATTERNS}\n"
                f"{listing}\n\n"
                "Um regex por linha (vírgula pode fazer parte do padrão). Quando um casa, a Bella apaga, "
                "aplica o timeout da aba **Ações** e avisa no canal de logs."
            )
        if self.section == "links":
            blocked = "\n".join(f"- {item}" for item in config.blocked_domains[:20]) or "Nenhum domínio bloqueado."
            allowed = "\n".join(f"- {item}" for item in config.allowed_domains[:20]) or "Nenhum domínio liberado."
            invites = "\n".join(f"- {item}" for item in config.allowed_invites[:20]) or "Nenhum convite liberado."
            return (
                "## AutoMod • Links e Convites\n"
                "Bloqueia domínios, convites, encurtadores e links suspeitos usando regras oficiais do Discord.\n\n"
                f"**Bloqueados**\n{blocked}\n\n**Domínios permitidos**\n{allowed}\n\n**Convites permitidos**\n{invites}"
            )
        if self.section == "content":
            presets = ", ".join(config.keyword_presets) or "Nenhum preset ativo"
            return (
                "## AutoMod Nativo • Conteúdo Bloqueado\n"
                "Controla recursos oficiais do Discord além das palavras personalizadas.\n\n"
                f"**Spam nativo:** {'Ativo' if config.spam_enabled else 'Inativo'}\n"
                f"**Links maliciosos:** {'Ativo' if config.harmful_links_enabled else 'Inativo'}\n"
                f"**Perfil de membro:** {'Ativo' if config.member_profile_enabled else 'Inativo'}\n"
                f"**Presets oficiais:** {presets}"
            )
        if self.section == "mentions":
            return (
                "## AutoMod • Menções em Massa\n"
                "Controla excesso de menções usando a regra oficial de menções em massa do Discord.\n\n"
                f"**Limite atual:** {config.mention_limit} menções por mensagem\n"
                f"**Proteção contra raid de menções:** {'Ativa' if config.mention_raid_protection else 'Inativa'}"
            )
        if self.section == "actions":
            channel = f"<#{config.log_channel_id}>" if config.log_channel_id else "Não configurado"
            timeout = (
                f"{config.timeout_duration_seconds // 60} minuto(s)"
                if config.timeout_duration_seconds
                else "Desativado"
            )
            message = (config.custom_message or DEFAULT_BLOCK_MESSAGE)[:150]
            return (
                "## AutoMod Nativo • Ações\n"
                "Configura o que o Discord faz quando uma regra do AutoMod é acionada.\n\n"
                "**Bloquear mensagem:** Ativo\n"
                f"**Enviar alerta:** {channel}\n"
                f"**Castigo/timeout:** {timeout}\n"
                f"**Mensagem exibida:** {message}"
            )
        if self.section == "logs":
            channel = f"<#{config.log_channel_id}>" if config.log_channel_id else "Não configurado"
            return (
                "## AutoMod • Logs\n"
                "O Discord enviará alertas oficiais do AutoMod para o canal selecionado.\n\n"
                f"**Canal atual:** {channel}"
            )
        if self.section == "exemptions":
            roles = "\n".join(f"- <@&{role_id}> (`{role_id}`)" for role_id in config.exempt_role_ids[:20]) or "Nenhum cargo liberado."
            channels = "\n".join(f"- <#{channel_id}> (`{channel_id}`)" for channel_id in config.exempt_channel_ids[:20]) or "Nenhum canal liberado."
            allowed_phrases = "\n".join(f"- {item}" for item in config.allowed_phrases[:20]) or "Nenhuma frase liberada."
            return (
                "## AutoMod Nativo • Exceções\n"
                "Cargos e canais liberados são enviados para as exceções oficiais das regras do Discord.\n\n"
                f"**Cargos liberados**\n{roles}\n\n"
                f"**Canais liberados**\n{channels}\n\n"
                f"**Palavras/frases permitidas**\n{allowed_phrases}"
            )
        if self.section == "templates":
            return (
                "## AutoMod • Templates\n"
                "Aplique modelos prontos nas regras oficiais do AutoMod do Discord.\n\n"
                "- Programação\n- Comunidade\n- Empresa\n- Suporte/Estudos\n- Marketplace"
            )
        if self.section == "stats":
            top_users = _top_map(self._stats.by_user, prefix="<@", suffix=">", limit=5)
            return (
                "## AutoMod • Estatísticas\n"
                f"**Bloqueios registrados:** {self._stats.blocked_total}\n"
                f"**Regras criadas:** {self._stats.rules_created}\n"
                f"**Regras editadas:** {self._stats.rules_updated}\n"
                f"**Regras removidas:** {self._stats.rules_deleted}\n\n"
                f"**Usuários mais acionados**\n{top_users or 'Sem eventos registrados ainda.'}"
            )
        if self.section == "badge":
            progress = self._badge_progress
            percent = min(
                100,
                int((progress.total_rules / AUTOMOD_BADGE_REQUIRED_RULES) * 100),
            )
            return (
                '## Badge "Uses AutoMod"\n'
                "Essa seção mostra se a Bella está usando regras reais do AutoMod nativo do Discord.\n\n"
                f"**Regras AutoMod visíveis à Bella:** {progress.total_rules}/{AUTOMOD_BADGE_REQUIRED_RULES}\n"
                f"**Regras ativas:** {progress.active_rules}\n"
                f"**Faltam para a meta pública:** {progress.missing}\n"
                f"**Servidores consultados:** {progress.guilds_scanned}\n"
                f"**Falhas de consulta:** {progress.guilds_failed}\n"
                f"**Progresso estimado:** {percent}%\n\n"
                "Depois que a meta for atingida, a badge pode levar um tempo para aparecer no perfil do app."
            )
        if self.section == "manage":
            rule_lines = "\n".join(
                f"- {'Ativa' if rule.enabled else 'Inativa'} • `{rule.id}` • {rule.name.removeprefix(RULE_PREFIX)}"
                for rule in jarvis_rules[:20]
            ) or "Nenhuma regra criada pela Bella ainda."
            return (
                "## AutoMod • Gerenciamento\n"
                "Ative, desative, sincronize, exporte, importe ou remova regras oficiais criadas pela Bella.\n"
                "Para editar uma regra específica, use o ID ou o nome exibido abaixo.\n\n"
                f"**Estado geral:** {'Ativo' if config.enabled else 'Pausado'}\n\n"
                f"{rule_lines}"
            )
        native_enabled = [
            label
            for active, label in (
                (config.spam_enabled, "Spam"),
                (config.harmful_links_enabled, "Links maliciosos"),
                (config.mentions_enabled, "Menções"),
                (config.member_profile_enabled, "Perfil de membro"),
                (config.mention_raid_protection, "Raid de menções"),
            )
            if active
        ]
        return (
            "## Central AutoMod Bella\n"
            "Painel fixo para configurar o AutoMod nativo do Discord e o AutoMod de regex da Bella.\n\n"
            "### Resumo operacional\n"
            f"**Estado:** {'Ativo' if config.enabled else 'Pausado'}\n"
            f"**Regras nativas criadas pela Bella:** {len(jarvis_rules)}  •  **Ativas:** {enabled}  •  **Inativas:** {disabled}\n"
            f"**Canal de alertas:** {f'<#{config.log_channel_id}>' if config.log_channel_id else 'Não configurado'}\n"
            f"**Mensagem de bloqueio:** {(config.custom_message or DEFAULT_BLOCK_MESSAGE)[:120]}\n\n"
            "### Filtros configurados\n"
            f"**Palavras:** {len(config.blocked_words)}  •  **Regex:** {len(config.bella_regex_patterns)} (nativo {len(config.regex_patterns)}/{MAX_NATIVE_REGEX_PATTERNS})  •  "
            f"**Frases permitidas:** {len(config.allowed_phrases)}\n"
            f"**Domínios bloqueados:** {len(config.blocked_domains)}  •  **Domínios liberados:** {len(config.allowed_domains)}  •  "
            f"**Convites liberados:** {len(config.allowed_invites)}\n"
            f"**Exceções:** {len(config.exempt_role_ids)} cargo(s) e {len(config.exempt_channel_ids)} canal(is)/categoria(s)\n\n"
            "### Recursos ativos\n"
            f"{', '.join(native_enabled) if native_enabled else 'Nenhum recurso nativo extra ativo.'}\n"
            "Use as abas abaixo para editar palavras, regex, links, canais/cargos liberados, ações e templates."
        )

    def _action_row_for_section(self) -> discord.ui.ActionRow | None:
        row = discord.ui.ActionRow()
        if self.section == "words":
            add = discord.ui.Button(label="Adicionar", style=discord.ButtonStyle.success)
            remove = discord.ui.Button(label="Remover", style=discord.ButtonStyle.danger)
            imp = discord.ui.Button(label="Importar Lista", style=discord.ButtonStyle.secondary)
            clear = discord.ui.Button(label="Limpar", style=discord.ButtonStyle.danger)
            add.callback = lambda interaction: interaction.response.send_modal(WordModal(self, mode="add"))
            remove.callback = lambda interaction: interaction.response.send_modal(WordModal(self, mode="remove"))
            imp.callback = lambda interaction: interaction.response.send_modal(WordModal(self, mode="import"))
            clear.callback = self._clear_words
            for item in (add, remove, imp, clear):
                row.add_item(item)
            return row
        if self.section == "links":
            block = discord.ui.Button(label="Bloquear Domínio", style=discord.ButtonStyle.danger)
            allow = discord.ui.Button(label="Liberar Domínio", style=discord.ButtonStyle.success)
            invite = discord.ui.Button(label="Liberar Convite", style=discord.ButtonStyle.primary)
            clear = discord.ui.Button(label="Limpar Links", style=discord.ButtonStyle.secondary)
            block.callback = lambda interaction: interaction.response.send_modal(DomainModal(self, mode="block"))
            allow.callback = lambda interaction: interaction.response.send_modal(DomainModal(self, mode="allow"))
            invite.callback = lambda interaction: interaction.response.send_modal(InviteAllowModal(self))
            clear.callback = self._clear_link_filters
            for item in (block, allow, invite, clear):
                row.add_item(item)
            return row
        if self.section == "mentions":
            limit = discord.ui.Button(label="Definir Limite", style=discord.ButtonStyle.primary)
            toggle = discord.ui.Button(
                label="Desativar menções" if (self._config and self._config.mentions_enabled) else "Ativar menções",
                style=discord.ButtonStyle.secondary,
            )
            raid = discord.ui.Button(
                label="Desativar raid" if (self._config and self._config.mention_raid_protection) else "Ativar raid",
                style=discord.ButtonStyle.secondary,
            )
            limit.callback = lambda interaction: interaction.response.send_modal(MentionLimitModal(self))
            toggle.callback = lambda interaction: self._toggle_native_feature(interaction, "mentions_enabled")
            raid.callback = lambda interaction: self._toggle_native_feature(interaction, "mention_raid_protection")
            for item in (limit, toggle, raid):
                row.add_item(item)
            return row
        if self.section == "content":
            presets = discord.ui.Button(label="Presets oficiais", style=discord.ButtonStyle.primary)
            spam = discord.ui.Button(
                label="Desativar spam" if (self._config and self._config.spam_enabled) else "Ativar spam",
                style=discord.ButtonStyle.secondary,
            )
            links = discord.ui.Button(
                label="Desativar links maliciosos" if (self._config and self._config.harmful_links_enabled) else "Ativar links maliciosos",
                style=discord.ButtonStyle.secondary,
            )
            profile = discord.ui.Button(
                label="Desativar perfil" if (self._config and self._config.member_profile_enabled) else "Ativar perfil",
                style=discord.ButtonStyle.secondary,
            )
            presets.callback = lambda interaction: interaction.response.send_modal(PresetModal(self))
            spam.callback = lambda interaction: self._toggle_native_feature(interaction, "spam_enabled")
            links.callback = lambda interaction: self._toggle_native_feature(interaction, "harmful_links_enabled")
            profile.callback = lambda interaction: self._toggle_native_feature(interaction, "member_profile_enabled")
            for item in (presets, spam, links, profile):
                row.add_item(item)
            return row
        if self.section == "regex":
            add = discord.ui.Button(label="Adicionar", style=discord.ButtonStyle.success)
            remove = discord.ui.Button(label="Remover", style=discord.ButtonStyle.danger)
            imp = discord.ui.Button(label="Importar Lista", style=discord.ButtonStyle.primary)
            clear = discord.ui.Button(label="Limpar", style=discord.ButtonStyle.secondary)
            add.callback = lambda interaction: interaction.response.send_modal(RegexModal(self, mode="add"))
            remove.callback = lambda interaction: interaction.response.send_modal(RegexModal(self, mode="remove"))
            imp.callback = lambda interaction: interaction.response.send_modal(RegexModal(self, mode="import"))
            clear.callback = self._clear_regex_patterns
            for item in (add, remove, imp, clear):
                row.add_item(item)
            return row
        if self.section == "logs":
            set_log = discord.ui.Button(label="Selecionar Canal", style=discord.ButtonStyle.primary)
            remove_log = discord.ui.Button(label="Remover Logs", style=discord.ButtonStyle.danger)
            set_log.callback = lambda interaction: interaction.response.send_modal(LogChannelModal(self))
            remove_log.callback = self._remove_log_channel
            row.add_item(set_log)
            row.add_item(remove_log)
            return row
        if self.section == "actions":
            configure = discord.ui.Button(label="Configurar ações", style=discord.ButtonStyle.primary)
            message = discord.ui.Button(label="Mensagem bloqueio", style=discord.ButtonStyle.primary)
            disable_timeout = discord.ui.Button(label="Remover castigo", style=discord.ButtonStyle.secondary)
            remove_alert = discord.ui.Button(label="Remover alerta", style=discord.ButtonStyle.secondary)
            configure.callback = lambda interaction: interaction.response.send_modal(ActionSettingsModal(self))
            message.callback = lambda interaction: interaction.response.send_modal(BlockMessageModal(self))
            disable_timeout.callback = self._disable_timeout
            remove_alert.callback = self._remove_log_channel
            row.add_item(configure)
            row.add_item(message)
            row.add_item(disable_timeout)
            row.add_item(remove_alert)
            return row
        if self.section == "exemptions":
            add_role = discord.ui.Button(label="Adicionar cargo", style=discord.ButtonStyle.success)
            remove_role = discord.ui.Button(label="Remover cargo", style=discord.ButtonStyle.danger)
            add_channel = discord.ui.Button(label="Adicionar canal", style=discord.ButtonStyle.success)
            remove_channel = discord.ui.Button(label="Remover canal", style=discord.ButtonStyle.danger)
            allow_phrase = discord.ui.Button(label="Frase permitida", style=discord.ButtonStyle.primary)
            add_role.callback = lambda interaction: interaction.response.send_modal(RoleExemptionModal(self, mode="add"))
            remove_role.callback = lambda interaction: interaction.response.send_modal(RoleExemptionModal(self, mode="remove"))
            add_channel.callback = lambda interaction: interaction.response.send_modal(ChannelExemptionModal(self, mode="add"))
            remove_channel.callback = lambda interaction: interaction.response.send_modal(ChannelExemptionModal(self, mode="remove"))
            allow_phrase.callback = lambda interaction: interaction.response.send_modal(AllowedPhraseModal(self))
            for item in (add_role, remove_role, add_channel, remove_channel, allow_phrase):
                row.add_item(item)
            return row
        if self.section == "templates":
            select = TemplateSelect(self)
            row.add_item(select)
            return row
        if self.section == "manage":
            sync = discord.ui.Button(label="Aplicar Tudo", style=discord.ButtonStyle.success)
            enable_all = discord.ui.Button(label="Ativar Geral", style=discord.ButtonStyle.primary)
            pause_all = discord.ui.Button(label="Pausar Geral", style=discord.ButtonStyle.secondary)
            export = discord.ui.Button(label="Exportar JSON", style=discord.ButtonStyle.secondary)
            import_json = discord.ui.Button(label="Importar JSON", style=discord.ButtonStyle.primary)
            sync.callback = self._sync
            enable_all.callback = lambda interaction: self._set_enabled(interaction, True)
            pause_all.callback = lambda interaction: self._set_enabled(interaction, False)
            export.callback = self._export
            import_json.callback = lambda interaction: interaction.response.send_modal(ImportConfigModal(self))
            for item in (sync, enable_all, pause_all, export, import_json):
                row.add_item(item)
            return row
        if self.section == "badge":
            docs = discord.ui.Button(
                label="Critérios oficiais",
                style=discord.ButtonStyle.link,
                url="https://support-dev.discord.com/hc/en-us/articles/13847462843543-Introducing-the-AutoMod-Badge",
            )
            row.add_item(docs)
            return row
        return None

    def _extra_action_rows_for_section(self) -> list[discord.ui.ActionRow]:
        rows: list[discord.ui.ActionRow] = []
        if self.section == "exemptions":
            row = discord.ui.ActionRow()
            clear_roles = discord.ui.Button(label="Limpar cargos", style=discord.ButtonStyle.secondary)
            clear_channels = discord.ui.Button(label="Limpar canais", style=discord.ButtonStyle.secondary)
            clear_phrases = discord.ui.Button(label="Limpar frases", style=discord.ButtonStyle.secondary)
            clear_all = discord.ui.Button(label="Limpar tudo", style=discord.ButtonStyle.danger)
            clear_roles.callback = self._clear_exempt_roles
            clear_channels.callback = self._clear_exempt_channels
            clear_phrases.callback = self._clear_allowed_phrases
            clear_all.callback = self._clear_all_exemptions
            for item in (clear_roles, clear_channels, clear_phrases, clear_all):
                row.add_item(item)
            rows.append(row)
        if self.section == "manage":
            row = discord.ui.ActionRow()
            delete_all = discord.ui.Button(label="Excluir regras nativas", style=discord.ButtonStyle.danger)
            reset_config = discord.ui.Button(label="Resetar configuração", style=discord.ButtonStyle.danger)
            enable_rule = discord.ui.Button(label="Ativar regra", style=discord.ButtonStyle.success)
            disable_rule = discord.ui.Button(label="Desativar regra", style=discord.ButtonStyle.secondary)
            delete_rule = discord.ui.Button(label="Excluir regra", style=discord.ButtonStyle.danger)
            delete_all.callback = self._delete_all
            reset_config.callback = self._reset_config
            enable_rule.callback = lambda interaction: interaction.response.send_modal(
                RuleOperationModal(self, operation="enable")
            )
            disable_rule.callback = lambda interaction: interaction.response.send_modal(
                RuleOperationModal(self, operation="disable")
            )
            delete_rule.callback = lambda interaction: interaction.response.send_modal(
                RuleOperationModal(self, operation="delete")
            )
            for item in (enable_rule, disable_rule, delete_rule, delete_all, reset_config):
                row.add_item(item)
            rows.append(row)
        return rows

    async def _switch(self, interaction: discord.Interaction, section: str) -> None:
        await interaction.response.defer()
        self.section = section
        await self.prepare()
        await self._edit_panel(interaction)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.prepare()
        await self._edit_panel(interaction)

    async def _sync(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._sync_native(
            self.guild,
            reason=f"AutoMod nativo aplicado por {interaction.user}",
        )
        await self.prepare()
        await self._edit_panel(interaction)

    async def _clear_words(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config = await self.center.config(self.guild.id)
        await self.center.save_config(
            AutoModGuildConfig(**{**config.to_dict(), "blocked_words": []})
        )
        await self._sync_native(
            self.guild,
            reason=f"Lista de palavras limpa por {interaction.user}",
        )
        await self.prepare()
        await self._edit_panel(interaction)

    async def _clear_link_filters(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config = await self.center.config(self.guild.id)
        await self.center.save_config(
            AutoModGuildConfig(
                **{
                    **config.to_dict(),
                    "blocked_domains": [],
                    "allowed_domains": [],
                    "allowed_invites": [],
                }
            )
        )
        await self._sync_native(
            self.guild,
            reason=f"Filtros de links do AutoMod limpos por {interaction.user}",
        )
        await self.prepare()
        await self._edit_panel(interaction)

    async def _clear_regex_patterns(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config = await self.center.config(self.guild.id)
        await self.center.save_config(
            AutoModGuildConfig(**{**config.to_dict(), "regex_patterns": [], "bella_regex_patterns": []})
        )
        await self._sync_native(
            self.guild,
            reason=f"Padrões regex limpos por {interaction.user}",
        )
        await self.prepare()
        await self._edit_panel(interaction)

    async def _remove_log_channel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.center.set_log_channel(
            self.guild,
            None,
            reason=f"Canal de logs removido por {interaction.user}",
        )
        await self.prepare()
        await self._edit_panel(interaction)

    async def _toggle_native_feature(self, interaction: discord.Interaction, key: str) -> None:
        await interaction.response.defer()
        config = await self.center.config(self.guild.id)
        data = config.to_dict()
        data[key] = not bool(data.get(key, False))
        await self.center.save_config(AutoModGuildConfig(**data))
        await self._sync_native(
            self.guild,
            reason=f"Recurso nativo {key} ajustado por {interaction.user}",
        )
        await self.prepare()
        await self._edit_panel(interaction)

    async def _disable_timeout(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config = await self.center.config(self.guild.id)
        await self.center.save_config(
            AutoModGuildConfig(**{**config.to_dict(), "timeout_duration_seconds": 0})
        )
        await self._sync_native(
            self.guild,
            reason=f"Castigo do AutoMod removido por {interaction.user}",
        )
        await self.prepare()
        await self._edit_panel(interaction)

    async def _clear_exempt_roles(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config = await self.center.config(self.guild.id)
        await self.center.save_config(
            AutoModGuildConfig(**{**config.to_dict(), "exempt_role_ids": []})
        )
        await self._sync_native(
            self.guild,
            reason=f"Cargos isentos do AutoMod limpos por {interaction.user}",
        )
        await self.prepare()
        await self._edit_panel(interaction)

    async def _clear_exempt_channels(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config = await self.center.config(self.guild.id)
        await self.center.save_config(
            AutoModGuildConfig(**{**config.to_dict(), "exempt_channel_ids": []})
        )
        await self._sync_native(
            self.guild,
            reason=f"Canais isentos do AutoMod limpos por {interaction.user}",
        )
        await self.prepare()
        await self._edit_panel(interaction)

    async def _clear_allowed_phrases(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config = await self.center.config(self.guild.id)
        await self.center.save_config(
            AutoModGuildConfig(**{**config.to_dict(), "allowed_phrases": []})
        )
        await self._sync_native(
            self.guild,
            reason=f"Frases permitidas do AutoMod limpas por {interaction.user}",
        )
        await self.prepare()
        await self._edit_panel(interaction)

    async def _clear_all_exemptions(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config = await self.center.config(self.guild.id)
        await self.center.save_config(
            AutoModGuildConfig(
                **{
                    **config.to_dict(),
                    "exempt_role_ids": [],
                    "exempt_channel_ids": [],
                    "allowed_phrases": [],
                }
            )
        )
        await self._sync_native(
            self.guild,
            reason=f"Exceções do AutoMod limpas por {interaction.user}",
        )
        await self.prepare()
        await self._edit_panel(interaction)

    async def _set_enabled(self, interaction: discord.Interaction, enabled: bool) -> None:
        await interaction.response.defer()
        config = await self.center.config(self.guild.id)
        await self.center.save_config(
            AutoModGuildConfig(**{**config.to_dict(), "enabled": enabled})
        )
        await self._sync_native(
            self.guild,
            reason=f"AutoMod {'ativado' if enabled else 'pausado'} por {interaction.user}",
        )
        await self.prepare()
        await self._edit_panel(interaction)

    async def _reset_config(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.center.save_config(AutoModGuildConfig(guild_id=self.guild.id))
        await self._sync_native(
            self.guild,
            reason=f"Configuração do AutoMod resetada por {interaction.user}",
        )
        await self.prepare()
        await self._edit_panel(interaction)

    async def _export(self, interaction: discord.Interaction) -> None:
        payload = await self.center.export_config(self.guild.id)
        await interaction.response.send_message(
            f"```json\n{payload[:1800]}\n```",
            ephemeral=True,
        )

    async def _delete_all(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.center.delete_all_rules(
            self.guild,
            reason=f"AutoMod Bella removido por {interaction.user}",
        )
        await self.prepare()
        await self._edit_panel(interaction)

    async def _edit_panel(self, interaction: discord.Interaction) -> None:
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)


class AutoModModal(discord.ui.Modal):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        message = (
            str(error)
            if isinstance(error, AutoModNativeSyncError)
            else f"Não consegui aplicar isso no AutoMod nativo: {type(error).__name__}."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message[:1900], ephemeral=True)
            else:
                await interaction.response.send_message(message[:1900], ephemeral=True)
        except discord.HTTPException:
            pass


class WordModal(AutoModModal):
    def __init__(self, panel: AutoModPanelView, *, mode: str) -> None:
        super().__init__(
            title={
                "add": "Adicionar palavra proibida",
                "remove": "Remover palavra proibida",
                "import": "Importar lista",
            }.get(mode, "Palavras proibidas")
        )
        self.panel = panel
        self.mode = mode
        self.words = discord.ui.TextInput(
            label="Palavras ou expressões",
            style=discord.TextStyle.paragraph,
            placeholder="Uma por linha ou separadas por vírgula",
            max_length=3500,
            required=True,
        )
        self.add_item(self.words)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config = await self.panel.center.config(self.panel.guild.id)
        values = set(config.blocked_words)
        incoming = set(_clean_list(str(self.words.value).replace(",", "\n").splitlines()))
        if self.mode == "remove":
            values -= incoming
        elif self.mode == "import":
            values = incoming
        else:
            values |= incoming
        new_config = AutoModGuildConfig(
            **{**config.to_dict(), "blocked_words": tuple(sorted(values))}
        )
        await self.panel.center.save_config(new_config)
        await self.panel._sync_native(
            self.panel.guild,
            reason=f"Palavras proibidas atualizadas por {interaction.user}",
        )
        await self.panel.prepare()
        await self.panel._edit_panel(interaction)


class RegexModal(AutoModModal):
    def __init__(self, panel: AutoModPanelView, *, mode: str) -> None:
        super().__init__(
            title={
                "add": "Adicionar regex",
                "remove": "Remover regex",
                "import": "Substituir todos os regex",
            }.get(mode, "Regex nativo")
        )
        self.panel = panel
        self.mode = mode
        self.patterns = discord.ui.TextInput(
            label="Um regex por linha",
            style=discord.TextStyle.paragraph,
            placeholder=r"Um por linha. Ex: (?i)(discord(?:app)?\.gg(?:/|\s|$))",
            max_length=3500,
            required=True,
        )
        self.add_item(self.patterns)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        lines = [line.strip() for line in str(self.patterns.value).splitlines() if line.strip()]
        invalid = [line for line in lines if compile_bella_pattern(line) is None]
        incoming = [line for line in lines if line not in invalid]
        if not incoming:
            await interaction.followup.send(
                "Nenhum padrão válido. Use um regex por linha."
                + (f"\nInválidos: " + ", ".join(f"`{i[:80]}`" for i in invalid[:5]) if invalid else ""),
                ephemeral=True,
            )
            return
        report = await self.panel.center.update_regex(
            self.panel.guild,
            incoming,
            mode=self.mode,
            actor=str(interaction.user),
        )
        if invalid:
            report += "\n\n⚠️ ignorei (regex inválido): " + ", ".join(f"`{i[:80]}`" for i in invalid[:5])
        await interaction.followup.send(report[:1900], ephemeral=True)
        await self.panel.prepare()
        await self.panel._edit_panel(interaction)


class DomainModal(AutoModModal):
    def __init__(self, panel: AutoModPanelView, *, mode: str) -> None:
        super().__init__(title="Configurar domínio")
        self.panel = panel
        self.mode = mode
        self.domains = discord.ui.TextInput(
            label="Domínios",
            style=discord.TextStyle.paragraph,
            placeholder="exemplo.com, bit.ly, discord.gift",
            max_length=1500,
            required=True,
        )
        self.add_item(self.domains)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config = await self.panel.center.config(self.panel.guild.id)
        incoming = set(_clean_list(str(self.domains.value).replace(",", "\n").splitlines()))
        data = config.to_dict()
        key = "allowed_domains" if self.mode == "allow" else "blocked_domains"
        data[key] = tuple(sorted(set(data.get(key, [])) | incoming))
        await self.panel.center.save_config(AutoModGuildConfig(**data))
        await self.panel._sync_native(
            self.panel.guild,
            reason=f"Domínios AutoMod atualizados por {interaction.user}",
        )
        await self.panel.prepare()
        await self.panel._edit_panel(interaction)


class InviteAllowModal(AutoModModal):
    def __init__(self, panel: AutoModPanelView) -> None:
        super().__init__(title="Convites Discord permitidos")
        self.panel = panel
        self.invites = discord.ui.TextInput(
            label="Convites/domínios permitidos",
            style=discord.TextStyle.paragraph,
            placeholder="discord.gg/seu-servidor ou discord.com/invite/codigo",
            max_length=1500,
            required=True,
        )
        self.mode = discord.ui.TextInput(
            label="Modo",
            placeholder="adicionar, remover, importar ou limpar",
            default="adicionar",
            max_length=20,
            required=True,
        )
        self.add_item(self.invites)
        self.add_item(self.mode)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        mode = _human_key(str(self.mode.value))
        config = await self.panel.center.config(self.panel.guild.id)
        current = set(config.allowed_invites)
        incoming = set(_clean_list(str(self.invites.value).replace(",", "\n").splitlines()))
        if "limpar" in mode:
            current.clear()
        elif "remover" in mode:
            current -= incoming
        elif "importar" in mode:
            current = incoming
        else:
            current |= incoming
        await self.panel.center.save_config(
            AutoModGuildConfig(
                **{**config.to_dict(), "allowed_invites": tuple(sorted(current))}
            )
        )
        await self.panel._sync_native(
            self.panel.guild,
            reason=f"Convites permitidos do AutoMod atualizados por {interaction.user}",
        )
        await self.panel.prepare()
        await self.panel._edit_panel(interaction)


class MentionLimitModal(AutoModModal):
    def __init__(self, panel: AutoModPanelView) -> None:
        super().__init__(title="Limite de menções")
        self.panel = panel
        self.limit = discord.ui.TextInput(
            label="Máximo de menções por mensagem",
            placeholder="Ex: 6",
            max_length=2,
            required=True,
        )
        self.add_item(self.limit)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.panel.center.upsert_mention_rule(
            self.panel.guild,
            limit=_safe_int(str(self.limit.value), 6),
            reason=f"Limite de menções ajustado por {interaction.user}",
        )
        await self.panel.prepare()
        await self.panel._edit_panel(interaction)


class LogChannelModal(AutoModModal):
    def __init__(self, panel: AutoModPanelView) -> None:
        super().__init__(title="Canal de logs do AutoMod")
        self.panel = panel
        self.channel = discord.ui.TextInput(
            label="ID ou menção do canal",
            placeholder="<#123> ou 123",
            max_length=80,
            required=True,
        )
        self.add_item(self.channel)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        channel_id = _first_id(str(self.channel.value))
        channel = self.panel.guild.get_channel(channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "Esse canal não foi encontrado como canal de texto.",
                ephemeral=True,
            )
            return
        await self.panel.center.set_log_channel(
            self.panel.guild,
            channel.id,
            reason=f"Canal de logs AutoMod definido por {interaction.user}",
        )
        await self.panel.prepare()
        await self.panel._edit_panel(interaction)


class RoleExemptionModal(AutoModModal):
    def __init__(self, panel: AutoModPanelView, *, mode: str) -> None:
        super().__init__(
            title="Adicionar cargo isento" if mode == "add" else "Remover cargo isento"
        )
        self.panel = panel
        self.mode = mode
        self.role = discord.ui.TextInput(
            label="ID ou menção do cargo",
            placeholder="<@&123> ou 123",
            max_length=80,
            required=True,
        )
        self.add_item(self.role)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        role_id = _first_id(str(self.role.value))
        role = self.panel.guild.get_role(role_id or 0)
        if role is None:
            await interaction.followup.send(
                "Não encontrei esse cargo no servidor.",
                ephemeral=True,
            )
            return
        config = await self.panel.center.config(self.panel.guild.id)
        role_ids = set(config.exempt_role_ids)
        if self.mode == "remove":
            role_ids.discard(role.id)
        else:
            role_ids.add(role.id)
        await self.panel.center.save_config(
            AutoModGuildConfig(
                **{**config.to_dict(), "exempt_role_ids": tuple(sorted(role_ids))}
            )
        )
        await self.panel._sync_native(
            self.panel.guild,
            reason=f"Cargos isentos do AutoMod atualizados por {interaction.user}",
        )
        await self.panel.prepare()
        await self.panel._edit_panel(interaction)


class ChannelExemptionModal(AutoModModal):
    def __init__(self, panel: AutoModPanelView, *, mode: str) -> None:
        super().__init__(
            title="Adicionar canal liberado" if mode == "add" else "Remover canal liberado"
        )
        self.panel = panel
        self.mode = mode
        self.channel = discord.ui.TextInput(
            label="ID ou menção do canal",
            placeholder="<#123> ou 123",
            max_length=80,
            required=True,
        )
        self.add_item(self.channel)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        channel_id = _first_id(str(self.channel.value))
        channel = self.panel.guild.get_channel(channel_id or 0)
        if channel is None:
            await interaction.followup.send(
                "Não encontrei esse canal no servidor.",
                ephemeral=True,
            )
            return
        config = await self.panel.center.config(self.panel.guild.id)
        channel_ids = set(config.exempt_channel_ids)
        if self.mode == "remove":
            channel_ids.discard(channel.id)
        else:
            channel_ids.add(channel.id)
        await self.panel.center.save_config(
            AutoModGuildConfig(
                **{**config.to_dict(), "exempt_channel_ids": tuple(sorted(channel_ids))}
            )
        )
        await self.panel._sync_native(
            self.panel.guild,
            reason=f"Canais liberados do AutoMod nativo atualizados por {interaction.user}",
        )
        await self.panel.prepare()
        await self.panel._edit_panel(interaction)


class AllowedPhraseModal(AutoModModal):
    def __init__(self, panel: AutoModPanelView) -> None:
        super().__init__(title="Palavras e frases permitidas")
        self.panel = panel
        self.phrases = discord.ui.TextInput(
            label="Frases permitidas",
            style=discord.TextStyle.paragraph,
            placeholder="Uma por linha. Ex: Caneco oficial, suporte autorizado",
            max_length=3500,
            required=True,
        )
        self.mode = discord.ui.TextInput(
            label="Modo",
            placeholder="adicionar, remover, importar ou limpar",
            default="adicionar",
            max_length=20,
            required=True,
        )
        self.add_item(self.phrases)
        self.add_item(self.mode)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        mode = _human_key(str(self.mode.value))
        config = await self.panel.center.config(self.panel.guild.id)
        current = set(config.allowed_phrases)
        incoming = set(_clean_list(str(self.phrases.value).replace(",", "\n").splitlines()))
        if "limpar" in mode:
            current.clear()
        elif "remover" in mode:
            current -= incoming
        elif "importar" in mode:
            current = incoming
        else:
            current |= incoming
        await self.panel.center.save_config(
            AutoModGuildConfig(
                **{**config.to_dict(), "allowed_phrases": tuple(sorted(current))}
            )
        )
        await self.panel._sync_native(
            self.panel.guild,
            reason=f"Frases permitidas do AutoMod nativo atualizadas por {interaction.user}",
        )
        await self.panel.prepare()
        await self.panel._edit_panel(interaction)


class PresetModal(AutoModModal):
    def __init__(self, panel: AutoModPanelView) -> None:
        super().__init__(title="Presets oficiais do Discord")
        self.panel = panel
        self.presets = discord.ui.TextInput(
            label="Presets",
            placeholder="profanity, sexual_content, slurs",
            max_length=120,
            required=False,
        )
        self.add_item(self.presets)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        presets = tuple(_clean_presets(str(self.presets.value)))
        config = await self.panel.center.config(self.panel.guild.id)
        await self.panel.center.save_config(
            AutoModGuildConfig(
                **{**config.to_dict(), "keyword_presets": presets}
            )
        )
        await self.panel._sync_native(
            self.panel.guild,
            reason=f"Presets nativos do AutoMod atualizados por {interaction.user}",
        )
        await self.panel.prepare()
        await self.panel._edit_panel(interaction)


class ActionSettingsModal(AutoModModal):
    def __init__(self, panel: AutoModPanelView) -> None:
        super().__init__(title="Ações do AutoMod nativo")
        self.panel = panel
        self.log_channel = discord.ui.TextInput(
            label="Canal de alerta opcional",
            placeholder="<#123> ou 123. Deixe vazio para manter.",
            max_length=80,
            required=False,
        )
        self.timeout_minutes = discord.ui.TextInput(
            label="Castigo em minutos",
            placeholder="0 para desativar. Máximo: 40320",
            max_length=8,
            required=False,
        )
        self.add_item(self.log_channel)
        self.add_item(self.timeout_minutes)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config = await self.panel.center.config(self.panel.guild.id)
        data = config.to_dict()
        channel_text = str(self.log_channel.value or "").strip()
        if channel_text:
            channel_id = _first_id(channel_text)
            channel = self.panel.guild.get_channel(channel_id or 0)
            if not isinstance(channel, discord.TextChannel):
                await interaction.followup.send(
                    "Não encontrei esse canal de alerta como canal de texto.",
                    ephemeral=True,
                )
                return
            data["log_channel_id"] = channel.id
        timeout_text = str(self.timeout_minutes.value or "").strip()
        if timeout_text:
            minutes = max(0, min(40320, _safe_int(timeout_text, 0)))
            data["timeout_duration_seconds"] = minutes * 60
        await self.panel.center.save_config(AutoModGuildConfig(**data))
        await self.panel._sync_native(
            self.panel.guild,
            reason=f"Ações do AutoMod nativo atualizadas por {interaction.user}",
        )
        await self.panel.prepare()
        await self.panel._edit_panel(interaction)


class BlockMessageModal(AutoModModal):
    def __init__(self, panel: AutoModPanelView) -> None:
        super().__init__(title="Mensagem de bloqueio")
        self.panel = panel
        self.message = discord.ui.TextInput(
            label="Mensagem exibida pelo AutoMod",
            style=discord.TextStyle.paragraph,
            placeholder=DEFAULT_BLOCK_MESSAGE,
            max_length=150,
            required=True,
        )
        self.add_item(self.message)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        config = await self.panel.center.config(self.panel.guild.id)
        text = str(self.message.value or "").strip()[:150] or DEFAULT_BLOCK_MESSAGE
        await self.panel.center.save_config(
            AutoModGuildConfig(**{**config.to_dict(), "custom_message": text})
        )
        await self.panel._sync_native(
            self.panel.guild,
            reason=f"Mensagem de bloqueio AutoMod atualizada por {interaction.user}",
        )
        await self.panel.prepare()
        await self.panel._edit_panel(interaction)


class ImportConfigModal(AutoModModal):
    def __init__(self, panel: AutoModPanelView) -> None:
        super().__init__(title="Importar configuração AutoMod")
        self.panel = panel
        self.payload = discord.ui.TextInput(
            label="JSON exportado",
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True,
        )
        self.add_item(self.payload)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            await self.panel.center.import_config(
                self.panel.guild,
                str(self.payload.value),
                reason=f"Configuração AutoMod importada por {interaction.user}",
            )
        except AutoModNativeSyncError as exc:
            await interaction.followup.send(str(exc)[:1900], ephemeral=True)
            return
        except Exception as exc:
            await interaction.followup.send(
                f"Não consegui importar esse JSON: {type(exc).__name__}.",
                ephemeral=True,
            )
            return
        await self.panel.prepare()
        await self.panel._edit_panel(interaction)


class RuleOperationModal(AutoModModal):
    def __init__(self, panel: AutoModPanelView, *, operation: str) -> None:
        self.panel = panel
        self.operation = operation
        title = {
            "enable": "Ativar regra nativa",
            "disable": "Desativar regra nativa",
            "delete": "Excluir regra nativa",
        }.get(operation, "Gerenciar regra nativa")
        super().__init__(title=title)
        self.rule = discord.ui.TextInput(
            label="ID ou nome da regra",
            placeholder="Ex: 1234567890, Filtro Principal, Spam, Menções em Massa",
            max_length=120,
            required=True,
        )
        self.add_item(self.rule)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        query = str(self.rule.value or "").strip()
        if self.operation == "delete":
            await self.panel.center.delete_native_rule_by_input(
                self.panel.guild,
                query,
                reason=f"Regra AutoMod nativa excluída por {interaction.user}",
            )
        else:
            await self.panel.center.set_native_rule_enabled(
                self.panel.guild,
                query,
                self.operation == "enable",
                reason=f"Regra AutoMod nativa ajustada por {interaction.user}",
            )
        await self.panel.prepare()
        await self.panel._edit_panel(interaction)


class TemplateSelect(discord.ui.Select):
    def __init__(self, panel: AutoModPanelView) -> None:
        self.panel = panel
        super().__init__(
            placeholder="Escolha um template",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Servidor de Programação", value="programacao"),
                discord.SelectOption(label="Comunidade", value="comunidade"),
                discord.SelectOption(label="Empresa", value="empresa"),
                discord.SelectOption(label="Suporte e Estudos", value="estudos"),
                discord.SelectOption(label="Marketplace", value="marketplace"),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.panel.center.apply_template(
            self.panel.guild,
            self.values[0],
            reason=f"Template AutoMod aplicado por {interaction.user}",
        )
        await self.panel.prepare()
        await self.panel._edit_panel(interaction)


def automod_permission_error(interaction: discord.Interaction) -> str | None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return "Esse comando precisa ser usado dentro de um servidor."
    if not (
        interaction.user.id == OWNER_DISCORD_ID
        or interaction.user.guild_permissions.manage_guild
    ):
        return "Você precisa da permissão Gerenciar Servidor para configurar o AutoMod."
    bot_member = interaction.guild.me
    if bot_member is None or not bot_member.guild_permissions.manage_guild:
        return (
            "A Bella precisa da permissão Gerenciar Servidor para configurar o AutoMod nativo do Discord."
        )
    return None


def _format_native_automod_error(exc: discord.HTTPException) -> str:
    detail = str(exc).replace("\n", " ").strip()
    lower = detail.casefold()
    if "auto_moderation_max_rules_of_type_exceeded" in lower or "maximum number" in lower:
        return (
            "O Discord recusou porque o servidor atingiu o limite de regras AutoMod desse tipo. "
            "Remova ou consolide regras antigas no AutoMod nativo do servidor e tente sincronizar novamente."
        )
    if "regex" in lower:
        return (
            "O Discord recusou um padrão regex. Use regex compatível com o AutoMod nativo "
            "do Discord, no máximo 10 padrões e 260 caracteres por padrão."
        )
    if "custom_message" in lower or "custom message" in lower:
        return "A mensagem customizada de bloqueio foi recusada. Use até 150 caracteres."
    if "exempt" in lower:
        return (
            "O Discord recusou as exceções. O AutoMod nativo permite até 20 cargos isentos "
            "e até 50 canais/categorias isentos por regra."
        )
    if "missing permissions" in lower or "permission" in lower:
        return (
            "Permissão insuficiente. A Bella precisa de Gerenciar Servidor e, se timeout estiver ativo, Moderar Membros."
        )
    return f"O Discord recusou a regra nativa do AutoMod: {detail[:900]}"


def _clean_list(values: Any) -> list[str]:
    if isinstance(values, str):
        iterable = values.replace(",", "\n").splitlines()
    else:
        iterable = values if isinstance(values, (list, tuple, set)) else []
    result: list[str] = []
    for item in iterable:
        cleaned = str(item).strip()
        if cleaned and cleaned not in result:
            result.append(cleaned[:100])
    return result


def _clean_regex_patterns(values: Any) -> list[str]:
    """Padrões para o AutoMod nativo: um por linha (regex usa vírgula por dentro,
    como em {0,4}), no máximo 10 e 260 caracteres. Os maiores ficam só com a Bella."""
    if isinstance(values, str):
        iterable = values.splitlines()
    else:
        iterable = values if isinstance(values, (list, tuple, set)) else []
    result: list[str] = []
    for item in iterable:
        cleaned = str(item).strip()
        if not cleaned or len(cleaned) > MAX_NATIVE_REGEX_PATTERN_LENGTH:
            continue
        if cleaned not in result:
            result.append(cleaned)
        if len(result) >= MAX_NATIVE_REGEX_PATTERNS:
            break
    return result


_REGEX_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "к": "k", "м": "m", "т": "t", "н": "h",
    "в": "b", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "υ": "u", "ν": "v", "ο": "o", "α": "a", "ε": "e", "ρ": "p",
    "τ": "t", "κ": "k", "ι": "i", "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ғ": "f", "ɢ": "g", "ʜ": "h",
    "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ʀ": "r", "ꜱ": "s", "ᴛ": "t",
    "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
})


def normalize_for_regex(text: str) -> str:
    """Desfaz disfarces antes de conferir os regex: fontes especiais (𝓬𝓪𝓬𝓱𝓸𝓻𝓻𝓸, ｃａｃｈｏｒｒｏ),
    letras de emoji/bandeira, letras de outros alfabetos, caracteres invisíveis e letras
    soltas separadas por espaço ou ponto (c a c h o r r o, c.a.c.h.o.r.r.o)."""
    out = []
    for char in str(text or ""):
        code = ord(char)
        if 0x1F1E6 <= code <= 0x1F1FF:
            out.append(chr(ord("a") + code - 0x1F1E6))
        elif 0x1F170 <= code <= 0x1F189:
            out.append(chr(ord("a") + code - 0x1F170))
        elif 0x1F150 <= code <= 0x1F169:
            out.append(chr(ord("a") + code - 0x1F150))
        elif char in "​‌‍⁠﻿­️":
            continue
        else:
            out.append(char)
    value = unicodedata.normalize("NFKC", "".join(out)).casefold().translate(_REGEX_CONFUSABLES)
    # letras soltas: "c a c h o r r o" -> "cachorro" (só junta sequências de 3+ letras isoladas)
    return re.sub(
        r"(?<!\w)(?:\w[\s._\-*]){2,}\w(?!\w)",
        lambda m: re.sub(r"[\s._\-*]", "", m.group(0)),
        value,
    )


def _format_duration(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400} dia(s)"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    return f"{max(1, seconds // 60)} min"


MAX_BELLA_REGEX_PATTERNS = 200
MAX_BELLA_REGEX_LENGTH = 2000


def compile_bella_pattern(pattern: str) -> "re.Pattern[str] | None":
    """Compila no motor do Python, sem diferenciar maiúsculas (como o Discord)."""
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


def clean_bella_patterns(values: Any) -> list[str]:
    if isinstance(values, str):
        iterable = values.splitlines()
    else:
        iterable = values if isinstance(values, (list, tuple, set)) else []
    result: list[str] = []
    for item in iterable:
        cleaned = str(item).strip()
        if not cleaned or len(cleaned) > MAX_BELLA_REGEX_LENGTH or cleaned in result:
            continue
        if compile_bella_pattern(cleaned) is None:
            continue
        result.append(cleaned)
        if len(result) >= MAX_BELLA_REGEX_PATTERNS:
            break
    return result


def _clean_presets(values: Any) -> list[str]:
    aliases = {
        "profanity": "profanity",
        "palavrao": "profanity",
        "palavrão": "profanity",
        "sexual": "sexual_content",
        "sexual_content": "sexual_content",
        "conteudo sexual": "sexual_content",
        "conteúdo sexual": "sexual_content",
        "slurs": "slurs",
        "ofensas": "slurs",
        "discurso odio": "slurs",
        "discurso de odio": "slurs",
        "discurso de ódio": "slurs",
    }
    raw_items = _clean_list(values)
    result: list[str] = []
    for item in raw_items:
        key = item.strip().casefold().replace("-", "_")
        normalized = aliases.get(key) or aliases.get(_human_key(item))
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _wildcard_keyword(value: str) -> str:
    cleaned = str(value).strip()
    if not cleaned:
        return ""
    if "*" in cleaned:
        return cleaned[:100]
    return f"*{cleaned[:98]}*"


def _int_list(values: Any) -> list[int]:
    if isinstance(values, str):
        iterable = re.findall(r"\d{15,22}", values)
    else:
        iterable = values if isinstance(values, (list, tuple, set)) else []
    result: list[int] = []
    for item in iterable:
        parsed = _safe_int(item)
        if parsed > 0 and parsed not in result:
            result.append(parsed)
    return result


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> int | None:
    parsed = _safe_int(value, 0)
    return parsed if parsed > 0 else None


def _first_id(value: str) -> int | None:
    match = re.search(r"\d{15,22}", value)
    return int(match.group(0)) if match else None


def _normalize_for_filter(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    replacements = str.maketrans(
        {
            "0": "o",
            "1": "i",
            "3": "e",
            "4": "a",
            "5": "s",
            "7": "t",
            "@": "a",
            "$": "s",
        }
    )
    text = text.translate(replacements)
    text = "".join(chr(ord(ch) - 0xFEE0) if "\uFF01" <= ch <= "\uFF5E" else ch for ch in text)
    return re.sub(r"[^a-z0-9]+", "", text)


def _human_key(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _domains_from_text(value: str) -> list[str]:
    domains: list[str] = []
    for match in re.finditer(r"(?:https?://)?(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})(?:/[^\s]*)?", value.casefold()):
        domain = match.group(1).strip(".")
        if domain and domain not in domains:
            domains.append(domain)
    return domains


def _domain_matches(domain: str, configured: str) -> bool:
    configured = configured.casefold().removeprefix("http://").removeprefix("https://").removeprefix("www.").strip("/")
    domain = domain.casefold().removeprefix("www.")
    return bool(configured) and (domain == configured or domain.endswith("." + configured) or configured in domain)


def _is_discord_invite_domain(domain: str) -> bool:
    return domain in {"discord.gg", "discord.com", "discordapp.com"} or domain.endswith(".discord.gg")


def _looks_like_spam(value: str) -> bool:
    clean = value.strip()
    if len(clean) >= 12 and len(set(clean.casefold().split())) <= 2 and len(clean.split()) >= 8:
        return True
    if re.search(r"(.)\1{12,}", clean):
        return True
    uppercase = sum(1 for ch in clean if ch.isupper())
    letters = sum(1 for ch in clean if ch.isalpha())
    return letters >= 25 and uppercase / max(letters, 1) > 0.85


def _message_has_image(message: discord.Message) -> bool:
    image_extensions = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif")
    for attachment in getattr(message, "attachments", ()):
        content_type = str(getattr(attachment, "content_type", "") or "").casefold()
        filename = str(getattr(attachment, "filename", "") or "").casefold()
        if content_type.startswith("image/") or filename.endswith(image_extensions):
            return True
    for embed in getattr(message, "embeds", ()):
        if getattr(getattr(embed, "image", None), "url", None):
            return True
        if getattr(getattr(embed, "thumbnail", None), "url", None):
            return True
    return False


def _top_map(
    values: dict[str, int],
    *,
    prefix: str = "",
    suffix: str = "",
    limit: int = 5,
) -> str:
    items = sorted(values.items(), key=lambda item: item[1], reverse=True)[:limit]
    return "\n".join(f"- {prefix}{key}{suffix}: {count}" for key, count in items)

