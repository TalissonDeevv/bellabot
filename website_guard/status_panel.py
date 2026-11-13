from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from unicodedata import combining, normalize
from urllib.parse import urlparse

import discord

from .metrics import WeeklyMetricsSnapshot
from .monitor import MonitorReport
from .state import StateStore

DEFAULT_INTERVAL_SECONDS = 300
MIN_INTERVAL_SECONDS = 300
MAX_INTERVAL_SECONDS = 3600
DEFAULT_STATUS_KEYS = (
    "squarecloud",
    "infrastructure",
    "site",
    "availability",
    "response",
    "monitoring",
    "tls",
    "systems",
    "platform",
    "health",
    "custom",
)
STATUS_KEY_ALIASES = {
    "square": "squarecloud",
    "squarecloud": "squarecloud",
    "squarecloud.app": "squarecloud",
    "hosting": "squarecloud",
    "hospedagem": "squarecloud",
    "host": "squarecloud",
    "infraestrutura": "infrastructure",
    "infrastructure": "infrastructure",
    "site": "site",
    "disponibilidade": "availability",
    "availability": "availability",
    "resposta": "response",
    "latencia": "response",
    "response": "response",
    "monitoramento": "monitoring",
    "monitoring": "monitoring",
    "tls": "tls",
    "certificados": "tls",
    "sistemas": "systems",
    "systems": "systems",
    "plataforma": "platform",
    "platform": "platform",
    "saude": "health",
    "health": "health",
    "score": "health",
    "personalizado": "custom",
    "custom": "custom",
}


@dataclass(frozen=True)
class GuildStatusConfig:
    guild_id: int
    voice_channel_id: int | None = None
    voice_enabled: bool = False
    presence_enabled: bool = False
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    presence_type: str = "watching"
    custom_status: str = ""
    active_status_keys: tuple[str, ...] = DEFAULT_STATUS_KEYS
    incident_degraded: str = "Operação Degradada"
    incident_down: str = "Serviço Indisponível"
    incident_check: str = "Verificação Operacional Necessária"
    rotation_index: int = 0
    last_rotation_at: float = 0.0
    streamer_enabled: bool = False
    streamer_name: str = ""
    streamer_description: str = ""
    streamer_url: str = ""
    streamer_icon_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "voice_channel_id": self.voice_channel_id,
            "voice_enabled": self.voice_enabled,
            "presence_enabled": self.presence_enabled,
            "interval_seconds": self.interval_seconds,
            "presence_type": self.presence_type,
            "custom_status": self.custom_status,
            "active_status_keys": list(self.active_status_keys),
            "incident_degraded": self.incident_degraded,
            "incident_down": self.incident_down,
            "incident_check": self.incident_check,
            "rotation_index": self.rotation_index,
            "last_rotation_at": self.last_rotation_at,
            "streamer_enabled": self.streamer_enabled,
            "streamer_name": self.streamer_name,
            "streamer_description": self.streamer_description,
            "streamer_url": self.streamer_url,
            "streamer_icon_url": self.streamer_icon_url,
        }

    @classmethod
    def from_dict(
        cls,
        guild_id: int,
        data: dict[str, Any],
    ) -> "GuildStatusConfig":
        return cls(
            guild_id=guild_id,
            voice_channel_id=(
                int(data["voice_channel_id"])
                if data.get("voice_channel_id")
                else None
            ),
            voice_enabled=bool(data.get("voice_enabled", False)),
            presence_enabled=bool(data.get("presence_enabled", False)),
            interval_seconds=max(
                MIN_INTERVAL_SECONDS,
                min(MAX_INTERVAL_SECONDS, int(data.get("interval_seconds", 300))),
            ),
            presence_type=str(data.get("presence_type", "watching")),
            custom_status=str(data.get("custom_status", "")),
            active_status_keys=normalize_status_keys(data.get("active_status_keys")),
            incident_degraded=str(
                data.get("incident_degraded", "Operação Degradada")
            ),
            incident_down=str(
                data.get("incident_down", "Serviço Indisponível")
            ),
            incident_check=str(
                data.get(
                    "incident_check",
                    "Verificação Operacional Necessária",
                )
            ),
            rotation_index=int(data.get("rotation_index", 0)),
            last_rotation_at=float(data.get("last_rotation_at", 0.0)),
            streamer_enabled=bool(data.get("streamer_enabled", False)),
            streamer_name=str(data.get("streamer_name", ""))[:128],
            streamer_description=str(data.get("streamer_description", ""))[:128],
            streamer_url=str(data.get("streamer_url", ""))[:500],
            streamer_icon_url=str(data.get("streamer_icon_url", ""))[:500],
        )


class StatusPanelStore:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    async def get(self, guild_id: int) -> GuildStatusConfig:
        configs = await self.state.get_section("guild_status_configs")
        value = configs.get(str(guild_id), {})
        return GuildStatusConfig.from_dict(
            guild_id,
            value if isinstance(value, dict) else {},
        )

    async def all(self) -> list[GuildStatusConfig]:
        configs = await self.state.get_section("guild_status_configs")
        result = []
        for guild_id, value in configs.items():
            if not isinstance(value, dict):
                continue
            try:
                result.append(GuildStatusConfig.from_dict(int(guild_id), value))
            except (TypeError, ValueError):
                continue
        return result

    async def update(self, guild_id: int, **values: Any) -> GuildStatusConfig:
        current = await self.get(guild_id)
        data = current.to_dict()
        data.update(values)
        updated = GuildStatusConfig.from_dict(guild_id, data)
        await self.state.update_section(
            "guild_status_configs",
            str(guild_id),
            updated.to_dict(),
        )
        return updated


def build_public_statuses(
    report: MonitorReport | None,
    weekly: WeeklyMetricsSnapshot,
    custom_status: str = "",
    *,
    active_status_keys: tuple[str, ...] | list[str] | None = None,
    incident_degraded: str = "Operação Degradada",
    incident_down: str = "Serviço Indisponível",
    incident_check: str = "Verificação Operacional Necessária",
    health_score: int | None = None,
    hosting_status: str = "",
    hosting_latency_ms: int | None = None,
) -> list[str]:
    active_keys = normalize_status_keys(active_status_keys)
    site_latency = (
        report.response_ms
        if report is not None and report.response_ms is not None
        else 0
    )
    hosting_latency = f"{hosting_latency_ms if hosting_latency_ms is not None else site_latency}ms"
    squarecloud_status = f"squarecloud.app [{hosting_latency}]"
    if active_keys and all(key == "squarecloud" for key in active_keys):
        return [squarecloud_status[:100]]

    if report is None or report.error or report.status_code is None:
        statuses = []
        if "squarecloud" in active_keys:
            statuses.append(squarecloud_status)
        statuses.extend(
            [
                _safe_public_text(incident_down),
                _safe_public_text(incident_check),
            ]
        )
    elif report.critical:
        statuses = []
        if "squarecloud" in active_keys:
            statuses.append(squarecloud_status)
        statuses.append(_safe_public_text(incident_down))
    elif report.alerts:
        statuses = []
        if "squarecloud" in active_keys:
            statuses.append(squarecloud_status)
        statuses.extend(
            [
                _safe_public_text(incident_degraded),
                _safe_public_text(incident_check),
            ]
        )
    else:
        latency = site_latency
        hostname = urlparse(report.final_url or report.site_url).hostname or ""
        public_host = hostname.removeprefix("www.") or "Plataforma Oficial"
        hosting_label = hosting_status.strip().upper() if hosting_status else "ONLINE"
        catalog = {
            "squarecloud": squarecloud_status,
            "infrastructure": f"Hospedagem {hosting_label} • {hosting_latency}",
            "site": f"Plataforma • {public_host}",
            "availability": (
                f"Disponibilidade • {weekly.availability_percent:.2f}%"
            ),
            "response": f"Tempo de Resposta • {latency}ms",
            "monitoring": "Monitoramento Ativo",
            "tls": "Certificados Válidos",
            "systems": "Sistemas Operacionais",
            "platform": "Plataforma Operacional",
            "health": (
                f"Health Score • {health_score}/100"
                if health_score is not None
                else ""
            ),
            "custom": _safe_public_text(custom_status) if custom_status else "",
        }
        statuses = [
            catalog[key]
            for key in active_keys
            if catalog.get(key)
        ]
    return [status[:100] for status in statuses if status]


def parse_status_keys(value: str) -> tuple[str, ...]:
    keys: list[str] = []
    for item in value.replace(";", ",").split(","):
        normalized = _normalize_key(item)
        key = STATUS_KEY_ALIASES.get(normalized)
        if key and key not in keys:
            keys.append(key)
    return tuple(keys)


def normalize_status_keys(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        parsed = parse_status_keys(value)
    elif isinstance(value, (list, tuple)):
        parsed = parse_status_keys(",".join(str(item) for item in value))
    else:
        parsed = ()
    return parsed or DEFAULT_STATUS_KEYS


def make_activity(kind: str, text: str) -> discord.BaseActivity:
    safe = text[:128]
    normalized = kind.casefold()
    if normalized == "playing":
        return discord.Game(name=safe)
    if normalized == "listening":
        return discord.Activity(type=discord.ActivityType.listening, name=safe)
    if normalized == "competing":
        return discord.Activity(type=discord.ActivityType.competing, name=safe)
    if normalized == "custom":
        try:
            return discord.CustomActivity(name=safe)
        except AttributeError:
            return discord.Game(name=safe)
    return discord.Activity(type=discord.ActivityType.watching, name=safe)


def make_streaming_activity(config: GuildStatusConfig) -> discord.Streaming:
    extra: dict[str, Any] = {}
    if config.streamer_icon_url:
        extra["assets"] = {
            "large_image": config.streamer_icon_url,
            "large_text": config.streamer_description[:128],
        }
    return discord.Streaming(
        name=config.streamer_name[:128] or "Transmissão ao vivo",
        url=config.streamer_url,
        **extra,
    )


def status_config_embed(
    config: GuildStatusConfig,
    channel: discord.abc.GuildChannel | None,
) -> discord.Embed:
    embed = discord.Embed(
        title="Painel público de status",
        description=(
            "Configuração persistente do canal de voz e da presença dinâmica."
        ),
        color=discord.Color.from_rgb(24, 119, 242),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Canal de voz",
        value=(
            f"{channel.mention if channel else 'Não configurado'}\n"
            f"Estado: **{'ativo' if config.voice_enabled else 'parado'}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Presença",
        value=(
            f"Estado: **{'ativa' if config.presence_enabled else 'parada'}**\n"
            f"Tipo: **{config.presence_type}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Modo streamer",
        value=(
            f"Estado: **{'ativo' if config.streamer_enabled else 'parado'}**\n"
            f"Atividade: **{config.streamer_name or 'Não configurada'}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Rotação",
        value=f"A cada **{config.interval_seconds} segundos**",
        inline=True,
    )
    if config.custom_status:
        embed.add_field(
            name="Status personalizado",
            value=_safe_public_text(config.custom_status),
            inline=False,
        )
    embed.add_field(
        name="Prioridade da rotação",
        value=" → ".join(config.active_status_keys),
        inline=False,
    )
    embed.add_field(
        name="Mensagens de incidente",
        value=(
            f"{_safe_public_text(config.incident_degraded)}\n"
            f"{_safe_public_text(config.incident_down)}\n"
            f"{_safe_public_text(config.incident_check)}"
        ),
        inline=False,
    )
    embed.set_footer(text="Somente informações públicas e operacionais")
    return embed


def _safe_public_text(value: str) -> str:
    text = " ".join(value.split())
    for symbol in ("🟡", "🔴", "⚠️", "⚠", "🛡️", "🛡", "🟢", "📈", "⚡", "🔒", "📡", "🌍", "🌐"):
        text = text.replace(symbol, "").strip()
    forbidden = (
        "token",
        "secret",
        "senha",
        "password",
        "127.0.0.1",
        "localhost",
        "http://",
        "https://",
        ".env",
    )
    if any(item in text.casefold() for item in forbidden):
        return "Monitoramento Ativo"
    return text[:100]


def _normalize_key(value: str) -> str:
    ascii_value = normalize("NFKD", value.casefold())
    return "".join(char for char in ascii_value if not combining(char)).strip()
