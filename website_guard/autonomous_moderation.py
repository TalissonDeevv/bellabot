from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import discord

from .state import StateStore

LOGGER = logging.getLogger(__name__)

CONFIG_SECTION = "autonomous_moderation_configs"
RISK_SECTION = "autonomous_moderation_risk"
METRICS_SECTION = "autonomous_moderation_metrics"

URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
INVITE_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[a-z0-9-]+",
    re.IGNORECASE,
)
SCAM_TERMS = (
    "free nitro",
    "nitro gratis",
    "nitro grátis",
    "claim reward",
    "ganhe dinheiro rapido",
    "ganhe dinheiro rápido",
    "lucro garantido",
    "renda facil",
    "renda fácil",
    "receive usdt",
    "free money",
    "crypto casino",
    "cassino online",
    "promo code",
    "bonus code",
    "bônus instantâneo",
    "saque instantaneo",
    "saque instantâneo",
)
SUSPICIOUS_TLDS = {
    "click",
    "country",
    "gq",
    "kim",
    "link",
    "loan",
    "men",
    "ml",
    "mom",
    "party",
    "pw",
    "rest",
    "review",
    "science",
    "stream",
    "tk",
    "top",
    "work",
    "zip",
}
SHORTENERS = {
    "bit.ly",
    "cutt.ly",
    "goo.su",
    "is.gd",
    "rb.gy",
    "rebrand.ly",
    "shorturl.at",
    "tiny.cc",
    "tinyurl.com",
}


@dataclass(frozen=True)
class AutonomousModerationConfig:
    guild_id: int
    enabled: bool = False
    log_channel_id: int | None = None
    delete_threshold: int = 20
    timeout_threshold: int = 70
    timeout_minutes: int = 10
    exempt_role_ids: frozenset[int] = field(default_factory=frozenset)

    def to_dict(self) -> dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "enabled": self.enabled,
            "log_channel_id": self.log_channel_id,
            "delete_threshold": self.delete_threshold,
            "timeout_threshold": self.timeout_threshold,
            "timeout_minutes": self.timeout_minutes,
            "exempt_role_ids": sorted(self.exempt_role_ids),
        }

    @classmethod
    def from_dict(
        cls,
        guild_id: int,
        value: Any,
    ) -> "AutonomousModerationConfig":
        data = value if isinstance(value, dict) else {}
        return cls(
            guild_id=guild_id,
            enabled=bool(data.get("enabled", False)),
            log_channel_id=_optional_int(data.get("log_channel_id")),
            delete_threshold=max(10, min(100, _integer(data.get("delete_threshold"), 20))),
            timeout_threshold=max(
                30,
                min(200, _integer(data.get("timeout_threshold"), 70)),
            ),
            timeout_minutes=max(
                1,
                min(1440, _integer(data.get("timeout_minutes"), 10)),
            ),
            exempt_role_ids=frozenset(
                _integer_values(data.get("exempt_role_ids", []))
            ),
        )


@dataclass(frozen=True)
class LocalModerationAnalysis:
    score: int
    reasons: tuple[str, ...]
    categories: tuple[str, ...]
    ambiguous: bool = False


@dataclass
class RiskRecord:
    score: int = 0
    updated_at: float = field(default_factory=time.time)


class AutonomousModeration:
    def __init__(
        self,
        bot: discord.Client,
        state: StateStore,
        *,
        owner_id: int,
        admin_role_ids: set[int] | None = None,
    ) -> None:
        self.bot = bot
        self.state = state
        self.owner_id = owner_id
        self.admin_role_ids = set(admin_role_ids or ())
        self._message_history: dict[
            tuple[int, int],
            deque[tuple[float, str]],
        ] = defaultdict(lambda: deque(maxlen=20))
        self._join_history: dict[int, deque[float]] = defaultdict(
            lambda: deque(maxlen=100)
        )
        self._risk: dict[tuple[int, int], RiskRecord] = {}
        self._config_cache: dict[int, AutonomousModerationConfig] = {}
        self._analysis_cache: OrderedDict[str, LocalModerationAnalysis] = OrderedDict()
        self._last_raid_alert: dict[int, float] = {}
        # Preenchido pelo agente da Bella: recebe ocorrências de risco para
        # sugerir ban/silenciamento ao proprietário com botões.
        self.suggestion_callback: Any | None = None

    async def config(self, guild_id: int) -> AutonomousModerationConfig:
        cached = self._config_cache.get(guild_id)
        if cached is not None:
            return cached
        section = await self.state.get_section(CONFIG_SECTION)
        config = AutonomousModerationConfig.from_dict(
            guild_id,
            section.get(str(guild_id)),
        )
        self._config_cache[guild_id] = config
        return config

    async def configure(
        self,
        guild_id: int,
        *,
        log_channel_id: int,
        timeout_threshold: int = 70,
        timeout_minutes: int = 10,
    ) -> AutonomousModerationConfig:
        current = await self.config(guild_id)
        config = AutonomousModerationConfig(
            guild_id=guild_id,
            enabled=current.enabled,
            log_channel_id=log_channel_id,
            delete_threshold=current.delete_threshold,
            timeout_threshold=max(30, min(200, timeout_threshold)),
            timeout_minutes=max(1, min(1440, timeout_minutes)),
            exempt_role_ids=current.exempt_role_ids,
        )
        await self._save_config(config)
        return config

    async def set_enabled(
        self,
        guild_id: int,
        enabled: bool,
    ) -> AutonomousModerationConfig:
        current = await self.config(guild_id)
        config = AutonomousModerationConfig(
            guild_id=current.guild_id,
            enabled=enabled,
            log_channel_id=current.log_channel_id,
            delete_threshold=current.delete_threshold,
            timeout_threshold=current.timeout_threshold,
            timeout_minutes=current.timeout_minutes,
            exempt_role_ids=current.exempt_role_ids,
        )
        await self._save_config(config)
        return config

    async def set_role_exemption(
        self,
        guild_id: int,
        role_id: int,
        exempt: bool,
    ) -> AutonomousModerationConfig:
        current = await self.config(guild_id)
        role_ids = set(current.exempt_role_ids)
        role_ids.add(role_id) if exempt else role_ids.discard(role_id)
        config = AutonomousModerationConfig(
            guild_id=current.guild_id,
            enabled=current.enabled,
            log_channel_id=current.log_channel_id,
            delete_threshold=current.delete_threshold,
            timeout_threshold=current.timeout_threshold,
            timeout_minutes=current.timeout_minutes,
            exempt_role_ids=frozenset(role_ids),
        )
        await self._save_config(config)
        return config

    async def handle_message(self, message: discord.Message) -> bool:
        if message.guild is None or not isinstance(message.author, discord.Member):
            return False
        config = await self.config(message.guild.id)
        if not config.enabled or self._is_exempt(message.author, config):
            return False

        analysis = self._analyze(message)
        risk = self._update_risk(
            message.guild.id,
            message.author.id,
            analysis.score,
        )
        if analysis.score <= 0:
            return False

        deleted = False
        timed_out = False
        action_error = ""
        if analysis.score >= config.delete_threshold:
            try:
                await message.delete()
                deleted = True
            except discord.NotFound:
                deleted = True
            except discord.Forbidden:
                action_error = "Sem permissão para remover a mensagem."
            except discord.HTTPException as exc:
                action_error = f"Falha ao remover a mensagem: {exc}"

        if risk >= config.timeout_threshold:
            try:
                await message.author.timeout(
                    timedelta(minutes=config.timeout_minutes),
                    reason=(
                        "Score de risco local da Bella atingiu "
                        f"{risk} pontos: {', '.join(analysis.reasons)}"
                    ),
                )
                timed_out = True
                self._risk[(message.guild.id, message.author.id)] = RiskRecord()
            except discord.Forbidden:
                action_error = (
                    f"{action_error} Sem permissão ou hierarquia para aplicar timeout."
                ).strip()
            except discord.HTTPException as exc:
                action_error = (
                    f"{action_error} Falha ao aplicar timeout: {exc}"
                ).strip()

        await self._persist_incident(
            message,
            analysis,
            risk=risk,
            deleted=deleted,
            timed_out=timed_out,
        )
        await self._send_log(
            message,
            config,
            analysis,
            risk=risk,
            deleted=deleted,
            timed_out=timed_out,
            action_error=action_error,
        )
        if (
            self.suggestion_callback is not None
            and risk >= max(1, config.timeout_threshold // 2)
        ):
            try:
                await self.suggestion_callback(
                    message,
                    reasons=analysis.reasons,
                    risk=risk,
                    deleted=deleted,
                    timed_out=timed_out,
                )
            except Exception:
                LOGGER.exception("Falha ao enviar sugestão de moderação.")
        return deleted

    async def handle_member_join(self, member: discord.Member) -> bool:
        config = await self.config(member.guild.id)
        if not config.enabled:
            return False
        now = time.time()
        history = self._join_history[member.guild.id]
        history.append(now)
        while history and now - history[0] > 20:
            history.popleft()
        if len(history) < 8:
            return False
        if now - self._last_raid_alert.get(member.guild.id, 0) < 60:
            return True
        self._last_raid_alert[member.guild.id] = now
        await self._increment_metrics(
            member.guild.id,
            category="raid",
            deleted=False,
            timed_out=False,
        )
        embed = discord.Embed(
            title="Possível Raid Detectada",
            description=(
                f"Foram identificadas **{len(history)} entradas em até 20 segundos**. "
                "A Bella elevou o nível de atenção local, sem utilizar IA."
            ),
            color=discord.Color.from_rgb(218, 54, 51),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Membro mais recente", value=member.mention)
        await self._send_embed(member.guild, config.log_channel_id, embed)
        return True

    async def risk_score(self, guild_id: int, user_id: int) -> int:
        record = self._risk.get((guild_id, user_id))
        if record is not None:
            return self._decayed_score(record)
        section = await self.state.get_section(RISK_SECTION)
        guild = section.get(str(guild_id), {})
        data = guild.get(str(user_id), {}) if isinstance(guild, dict) else {}
        if not isinstance(data, dict):
            return 0
        try:
            updated_at = datetime.fromisoformat(str(data.get("updated_at"))).timestamp()
        except (TypeError, ValueError):
            updated_at = time.time()
        record = RiskRecord(
            score=max(0, _integer(data.get("score"), 0)),
            updated_at=updated_at,
        )
        self._risk[(guild_id, user_id)] = record
        return self._decayed_score(record)

    async def metrics_snapshot(self, guild_id: int) -> dict[str, Any]:
        section = await self.state.get_section(METRICS_SECTION)
        value = section.get(str(guild_id), {})
        return value if isinstance(value, dict) else {}

    def _analyze(self, message: discord.Message) -> LocalModerationAnalysis:
        content = " ".join(message.content.casefold().split()).strip()
        key = (message.guild.id, message.author.id)
        now = time.time()
        history = self._message_history[key]
        while history and now - history[0][0] > 30:
            history.popleft()

        score = 0
        reasons: list[str] = []
        categories: list[str] = []
        recent_eight_seconds = sum(1 for timestamp, _ in history if now - timestamp <= 8)
        repeated = bool(
            content
            and sum(1 for _, previous in history if previous == content) >= 2
        )
        if recent_eight_seconds >= 6:
            score += 15
            reasons.append("frequência elevada de mensagens")
            categories.append("flood")
        if repeated:
            score += 20
            reasons.append("mensagem repetida")
            categories.append("spam")
        history.append((now, content))

        static = self._static_analysis(message)
        score += static.score
        reasons.extend(static.reasons)
        categories.extend(static.categories)

        if score and message.author.created_at > datetime.now(timezone.utc) - timedelta(days=7):
            score += 15
            reasons.append("conta criada há menos de sete dias")
            categories.append("new_account")

        if score == 0:
            self._reduce_risk(message.guild.id, message.author.id, 5)
        return LocalModerationAnalysis(
            score=score,
            reasons=tuple(dict.fromkeys(reasons)),
            categories=tuple(dict.fromkeys(categories)),
            ambiguous=False,
        )

    def _static_analysis(self, message: discord.Message) -> LocalModerationAnalysis:
        content = " ".join(message.content.casefold().split()).strip()
        digest = hashlib.sha256(
            (
                content
                + "|"
                + str(len(message.mentions))
                + "|"
                + str(len(message.role_mentions))
            ).encode("utf-8")
        ).hexdigest()
        cached = self._analysis_cache.get(digest)
        if cached is not None:
            self._analysis_cache.move_to_end(digest)
            return cached

        score = 0
        reasons: list[str] = []
        categories: list[str] = []
        mention_count = len(message.mentions) + len(message.role_mentions)
        if mention_count >= 6:
            score += 25
            reasons.append(f"{mention_count} menções na mesma mensagem")
            categories.append("mentions")
        if INVITE_PATTERN.search(content):
            score += 20
            reasons.append("convite externo do Discord")
            categories.append("invite")

        links = URL_PATTERN.findall(content)
        suspicious_links = [
            link for link in links if _is_suspicious_url(link)
        ]
        scam_language = any(term in content for term in SCAM_TERMS)
        if suspicious_links:
            score += 30
            reasons.append("link com características suspeitas")
            categories.append("phishing")
        if links and scam_language:
            score += 80
            reasons.append("linguagem associada a golpe acompanhada de link")
            categories.append("scam")
        elif scam_language:
            score += 35
            reasons.append("linguagem financeira ou promocional suspeita")
            categories.append("suspicious")

        result = LocalModerationAnalysis(
            score=score,
            reasons=tuple(reasons),
            categories=tuple(categories),
            ambiguous=False,
        )
        self._analysis_cache[digest] = result
        self._analysis_cache.move_to_end(digest)
        while len(self._analysis_cache) > 1000:
            self._analysis_cache.popitem(last=False)
        return result

    def _update_risk(self, guild_id: int, user_id: int, delta: int) -> int:
        key = (guild_id, user_id)
        current = self._risk.setdefault(key, RiskRecord())
        current.score = max(0, self._decayed_score(current) + max(0, delta))
        current.updated_at = time.time()
        return current.score

    def _reduce_risk(self, guild_id: int, user_id: int, amount: int) -> None:
        key = (guild_id, user_id)
        current = self._risk.get(key)
        if current is None:
            return
        current.score = max(0, self._decayed_score(current) - amount)
        current.updated_at = time.time()

    def _decayed_score(self, record: RiskRecord) -> int:
        elapsed_blocks = int(max(0, time.time() - record.updated_at) // 600)
        return max(0, record.score - elapsed_blocks * 5)

    def _is_exempt(
        self,
        member: discord.Member,
        config: AutonomousModerationConfig,
    ) -> bool:
        if member.id in {self.owner_id, member.guild.owner_id}:
            return True
        role_ids = {role.id for role in member.roles}
        return bool(
            role_ids
            & (
                self.admin_role_ids
                | set(config.exempt_role_ids)
            )
        )

    async def _save_config(self, config: AutonomousModerationConfig) -> None:
        self._config_cache[config.guild_id] = config
        await self.state.update_section(
            CONFIG_SECTION,
            str(config.guild_id),
            config.to_dict(),
        )

    async def _persist_incident(
        self,
        message: discord.Message,
        analysis: LocalModerationAnalysis,
        *,
        risk: int,
        deleted: bool,
        timed_out: bool,
    ) -> None:
        now = datetime.now(timezone.utc)
        guild_id = message.guild.id
        user_id = message.author.id

        def mutate_risk(section: dict[str, Any]) -> None:
            guild = section.setdefault(str(guild_id), {})
            guild[str(user_id)] = {
                "score": risk,
                "updated_at": now.isoformat(),
            }

        await self.state.mutate_section(RISK_SECTION, mutate_risk)
        for index, category in enumerate(analysis.categories or ("other",)):
            await self._increment_metrics(
                guild_id,
                category=category,
                deleted=deleted and index == 0,
                timed_out=timed_out and index == 0,
                increment_incident=index == 0,
            )

    async def _increment_metrics(
        self,
        guild_id: int,
        *,
        category: str,
        deleted: bool,
        timed_out: bool,
        increment_incident: bool = True,
    ) -> None:
        def mutate(section: dict[str, Any]) -> None:
            guild = section.setdefault(str(guild_id), {})
            if increment_incident:
                guild["incidents"] = int(guild.get("incidents", 0)) + 1
            categories = guild.setdefault("categories", {})
            categories[category] = int(categories.get(category, 0)) + 1
            if deleted:
                guild["messages_deleted"] = int(
                    guild.get("messages_deleted", 0)
                ) + 1
            if timed_out:
                guild["timeouts"] = int(guild.get("timeouts", 0)) + 1
            today = datetime.now(timezone.utc).date().isoformat()
            daily = guild.setdefault("daily", {})
            bucket = daily.setdefault(today, {})
            if increment_incident:
                bucket["incidents"] = int(bucket.get("incidents", 0)) + 1
            if deleted:
                bucket["messages_deleted"] = int(
                    bucket.get("messages_deleted", 0)
                ) + 1
            if timed_out:
                bucket["timeouts"] = int(bucket.get("timeouts", 0)) + 1
            cutoff = (
                datetime.now(timezone.utc).date() - timedelta(days=120)
            ).isoformat()
            for day in list(daily):
                if day < cutoff:
                    del daily[day]

        await self.state.mutate_section(METRICS_SECTION, mutate)

    async def _send_log(
        self,
        message: discord.Message,
        config: AutonomousModerationConfig,
        analysis: LocalModerationAnalysis,
        *,
        risk: int,
        deleted: bool,
        timed_out: bool,
        action_error: str,
    ) -> None:
        embed = discord.Embed(
            title="Moderação Autônoma",
            description="Comportamento suspeito identificado por análise local.",
            color=discord.Color.from_rgb(218, 54, 51),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Usuário",
            value=f"{message.author.mention}\n`{message.author.id}`",
            inline=True,
        )
        embed.add_field(name="Score de risco", value=str(risk), inline=True)
        embed.add_field(
            name="Detecções",
            value="\n".join(f"• {reason}" for reason in analysis.reasons)[:1024],
            inline=False,
        )
        embed.add_field(
            name="Ações",
            value=(
                f"Mensagem removida: {'Sim' if deleted else 'Não'}\n"
                f"Timeout aplicado: {'Sim' if timed_out else 'Não'}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Canal",
            value=f"{message.channel.mention}\n[Mensagem original]({message.jump_url})",
            inline=False,
        )
        if message.content:
            embed.add_field(
                name="Conteúdo",
                value=message.content[:1024],
                inline=False,
            )
        if action_error:
            embed.add_field(name="Falha operacional", value=action_error[:1024])
        await self._send_embed(message.guild, config.log_channel_id, embed)

    async def _send_embed(
        self,
        guild: discord.Guild,
        channel_id: int | None,
        embed: discord.Embed,
    ) -> None:
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                return


def _is_suspicious_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").casefold().strip(".")
    except ValueError:
        return True
    if not hostname:
        return True
    if hostname in SHORTENERS or hostname.startswith("xn--"):
        return True
    if any(part.startswith("xn--") for part in hostname.split(".")):
        return True
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    tld = hostname.rsplit(".", 1)[-1] if "." in hostname else ""
    return tld in SUSPICIOUS_TLDS


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _integer_values(value: Any) -> set[int]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    result: set[int] = set()
    for item in value:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            continue
    return result
