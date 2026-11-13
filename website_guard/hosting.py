from __future__ import annotations

import io
import os
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependência opcional em runtime
    load_dotenv = None  # type: ignore[assignment]


SQUARE_BASE_URL = "https://api.squarecloud.app/v2"
SQUARE_SECTION = "squarecloud_hosting"
TEXT = (245, 247, 255)
MUTED = (154, 162, 185)
PANEL = (12, 14, 24)
PANEL_2 = (18, 22, 38)
GRID = (42, 49, 75)
GREEN = (80, 230, 160)
BLUE = (88, 131, 255)
CYAN = (69, 220, 255)
AMBER = (255, 190, 80)
RED = (255, 96, 120)
PURPLE = (165, 110, 255)
LUCIDE_ICON_DIR = Path(__file__).resolve().parent / "assets" / "lucide"


@dataclass(frozen=True)
class SquareConfig:
    api_key: str
    app_id: str
    alert_channel_id: int | None = None
    auto_restart: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.app_id)


class SquareAppResolutionError(RuntimeError):
    def __init__(self, app_id: str, apps: list[dict[str, Any]]) -> None:
        self.app_id = app_id
        self.apps = apps
        details = _format_app_candidates(apps)
        super().__init__(
            "A aplicação da Square Cloud não foi encontrada. "
            f"Valor atual de SQ_APP_ID: {app_id!r}. "
            + (
                f"Aplicações disponíveis: {details}"
                if details
                else "Não consegui listar aplicações disponíveis com essa chave."
            )
        )


@dataclass
class HostingSnapshot:
    app_id: str
    app_name: str
    status: str
    region: str = "Não informado"
    uptime: str = "Não informado"
    latency_ms: int | None = None
    ram_percent: float | None = None
    ram_mb: float | None = None
    cpu_percent: float | None = None
    storage_percent: float | None = None
    storage_mb: float | None = None
    network_in_mb: float | None = None
    network_out_mb: float | None = None
    requests: int | None = None
    last_deploy: str = "Não informado"
    raw_status: dict[str, Any] = field(default_factory=dict)
    raw_metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def online(self) -> bool:
        return self.status.casefold() in {"running", "online", "started", "up"}

    @property
    def health_label(self) -> str:
        if not self.online:
            return "offline"
        if (self.cpu_percent or 0) >= 90 or (self.ram_percent or 0) >= 90:
            return "atenção"
        return "saudável"


@dataclass(frozen=True)
class HostingAlert:
    severity: str
    title: str
    description: str
    mention_owner: bool = False


def _reload_dotenv() -> None:
    if callable(load_dotenv):
        load_dotenv(encoding="utf-8-sig", override=True)


class SquareCloudClient:
    def __init__(
        self,
        config: SquareConfig,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.config = config
        self._session = session
        self._app_id_override: str | None = None
        self._resolved_app_id: str | None = None
        self._resolved_app_name: str = ""

    @classmethod
    def from_env(cls) -> "SquareCloudClient":
        _reload_dotenv()
        api_key = os.getenv("SQ_API_KEY", "").strip()
        app_id = os.getenv("SQ_APP_ID", "Bella Bot").strip() or "Bella Bot"
        alert_channel_id = _parse_int(os.getenv("SQ_ALERT_CHANNEL_ID"))
        auto_restart = os.getenv("SQ_AUTO_RESTART", "true").strip().casefold() not in {
            "0",
            "false",
            "no",
            "off",
        }
        return cls(
            SquareConfig(
                api_key=api_key,
                app_id=app_id,
                alert_channel_id=alert_channel_id,
                auto_restart=auto_restart,
            )
        )

    def reload_from_env(self) -> None:
        _reload_dotenv()
        api_key = os.getenv("SQ_API_KEY", "").strip()
        app_id = os.getenv("SQ_APP_ID", "Bella Bot").strip() or "Bella Bot"
        alert_channel_id = _parse_int(os.getenv("SQ_ALERT_CHANNEL_ID"))
        auto_restart = os.getenv("SQ_AUTO_RESTART", "true").strip().casefold() not in {
            "0",
            "false",
            "no",
            "off",
        }
        updated = SquareConfig(
            api_key=api_key,
            app_id=app_id,
            alert_channel_id=alert_channel_id,
            auto_restart=auto_restart,
        )
        if updated != self.config:
            self.config = updated
            self._resolved_app_id = None
            self._resolved_app_name = ""

    async def snapshot(self) -> HostingSnapshot:
        if not self.config.configured:
            raise RuntimeError("Configure SQ_API_KEY e SQ_APP_ID no .env.")
        started = time.perf_counter()
        status_data = await self.get_status()
        latency_ms = int((time.perf_counter() - started) * 1000)
        metrics_data = await self.get_metrics()
        try:
            deployments = await self.get_deployments(limit=1)
        except RuntimeError:
            deployments = []
        return build_snapshot(
            self._resolved_app_id or self._app_id_override or self.config.app_id,
            status_data,
            metrics_data,
            deployments,
            latency_ms,
            resolved_app_name=self._resolved_app_name,
        )

    def set_app_id(self, app_id: str) -> None:
        cleaned = app_id.strip()
        if cleaned:
            self._app_id_override = cleaned
            self._resolved_app_id = None
            self._resolved_app_name = ""

    async def resolve_app_id(self, *, force: bool = False) -> str:
        if self._resolved_app_id and not force:
            return self._resolved_app_id
        candidate = (self._app_id_override or self.config.app_id).strip()
        if not candidate:
            raise RuntimeError("Configure SQ_APP_ID no .env.")

        try:
            info = await self._request_json("GET", f"/apps/{candidate}")
            payload = _extract_payload(info)
            self._resolved_app_id = str(payload.get("id") or candidate)
            self._resolved_app_name = str(payload.get("name") or "")
            return self._resolved_app_id
        except RuntimeError as exc:
            if "APP_NOT_FOUND" not in str(exc) and "404" not in str(exc):
                raise

        apps = await self.list_apps(enrich=True)
        normalized_candidate = _normalize_lookup(candidate)
        for app in apps:
            app_id = str(app.get("id") or "")
            app_name = str(app.get("name") or app.get("app_name") or "")
            if normalized_candidate in {
                _normalize_lookup(app_id),
                _normalize_lookup(app_name),
            }:
                self._resolved_app_id = app_id
                self._resolved_app_name = app_name
                return app_id

        if len(apps) == 1 and apps[0].get("id"):
            self._resolved_app_id = str(apps[0]["id"])
            self._resolved_app_name = str(
                apps[0].get("name") or apps[0].get("app_name") or ""
            )
            return self._resolved_app_id

        raise SquareAppResolutionError(candidate, apps)

    async def get_info(self) -> dict[str, Any]:
        app_id = await self.resolve_app_id()
        return await self._request_json("GET", f"/apps/{app_id}")

    async def get_status(self) -> dict[str, Any]:
        app_id = await self.resolve_app_id()
        return await self._request_json("GET", f"/apps/{app_id}/status")

    async def get_metrics(self) -> dict[str, Any]:
        try:
            app_id = await self.resolve_app_id()
            return await self._request_json("GET", f"/apps/{app_id}/metrics")
        except RuntimeError:
            return {}

    async def get_logs(self, *, lines: int = 80) -> str:
        app_id = await self.resolve_app_id()
        data = await self._request_json("GET", f"/apps/{app_id}/logs")
        logs = _extract_value(data, ("logs", "log", "content", "data", "response"))
        if isinstance(logs, list):
            return "\n".join(str(item) for item in logs[-lines:])
        text = str(logs if logs is not None else data)
        return "\n".join(text.splitlines()[-lines:])

    async def get_deployments(self, *, limit: int = 10) -> list[dict[str, Any]]:
        app_id = await self.resolve_app_id()
        data = await self._request_json("GET", f"/apps/{app_id}/deployments")
        return _extract_deployments(data)[:limit]

    async def get_domains(self) -> list[dict[str, Any]]:
        app_id = await self.resolve_app_id()
        for path in (
            f"/apps/{app_id}/domains",
            f"/apps/{app_id}/network/dns",
        ):
            try:
                return _extract_list(await self._request_json("GET", path))
            except RuntimeError:
                continue
        return []

    async def get_snapshots(self) -> list[dict[str, Any]]:
        for path in (
            "/users/snapshots",
        ):
            try:
                return _extract_list(await self._request_json("GET", path))
            except RuntimeError:
                continue
        return []

    async def list_apps(self, *, enrich: bool = False) -> list[dict[str, Any]]:
        apps = _extract_list(await self._request_json("GET", "/apps/status"))
        if not enrich:
            return apps
        enriched: list[dict[str, Any]] = []
        for app in apps:
            app_id = str(app.get("id") or "")
            merged = dict(app)
            if app_id:
                try:
                    info = _extract_payload(
                        await self._request_json("GET", f"/apps/{app_id}")
                    )
                    if isinstance(info, dict):
                        merged.update(info)
                except RuntimeError:
                    pass
            enriched.append(merged)
        return enriched

    async def restart(self) -> dict[str, Any]:
        app_id = await self.resolve_app_id()
        return await self._request_json("POST", f"/apps/{app_id}/restart")

    async def start(self) -> dict[str, Any]:
        app_id = await self.resolve_app_id()
        return await self._request_json("POST", f"/apps/{app_id}/start")

    async def stop(self) -> dict[str, Any]:
        app_id = await self.resolve_app_id()
        return await self._request_json("POST", f"/apps/{app_id}/stop")

    async def _request_json(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        session = self._session
        close_session = False
        if session is None:
            session = aiohttp.ClientSession()
            close_session = True
        try:
            headers = {
                "Authorization": self.config.api_key,
                "Content-Type": "application/json",
            }
            url = f"{SQUARE_BASE_URL}{path}"
            for attempt in range(2):
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                    **kwargs,
                ) as response:
                    text = await response.text()
                    if response.status == 429 and attempt == 0:
                        await _sleep_rate_limit()
                        continue
                    if response.status < 200 or response.status >= 300:
                        raise RuntimeError(
                            f"Square Cloud HTTP {response.status}: {text[:500]}"
                        )
                    try:
                        data = await response.json(content_type=None)
                    except Exception as exc:
                        raise RuntimeError(f"Resposta JSON inválida: {text[:500]}") from exc
                    if not isinstance(data, dict):
                        raise RuntimeError("A Square Cloud retornou uma estrutura inesperada.")
                    return data
            raise RuntimeError("Rate limit da Square Cloud persistiu após nova tentativa.")
        finally:
            if close_session:
                await session.close()


async def _sleep_rate_limit() -> None:
    import asyncio

    await asyncio.sleep(5)


class HostingRenderer:
    def operations_image(
        self,
        snapshot: HostingSnapshot,
        *,
        history: list[dict[str, Any]] | None = None,
    ) -> io.BytesIO:
        image = Image.new("RGB", (1600, 900), (2, 3, 8))
        draw = ImageDraw.Draw(image)
        _gradient(image)
        self._header(draw, "BELLA OPERATIONS", snapshot)
        cards = [
            ("server", "BOT", "ONLINE" if snapshot.online else "OFFLINE", GREEN if snapshot.online else RED),
            ("cpu", "CPU", _percent(snapshot.cpu_percent), _status_color(snapshot.cpu_percent)),
            ("boxes", "RAM", _percent(snapshot.ram_percent), _status_color(snapshot.ram_percent)),
            ("activity", "LATÊNCIA", f"{snapshot.latency_ms or 0} ms", CYAN),
            ("heart-pulse", "UPTIME", snapshot.uptime, BLUE),
            ("rocket", "DEPLOY", snapshot.last_deploy, PURPLE),
        ]
        for index, (icon, label, value, color) in enumerate(cards):
            x = 80 + (index % 3) * 485
            y = 180 + (index // 3) * 150
            _card(draw, x, y, 430, 112, label, value, color, icon=icon)
        self._action_buttons(draw, (80, 485, 1480, 565))
        self._bars(draw, snapshot, (80, 595, 700, 800))
        self._history(draw, history or [], (780, 595, 1480, 800))
        self._footer(draw)
        return _output(image)

    def metrics_image(
        self,
        snapshot: HostingSnapshot,
        *,
        history: list[dict[str, Any]] | None = None,
    ) -> io.BytesIO:
        image = Image.new("RGB", (1600, 900), (2, 3, 8))
        draw = ImageDraw.Draw(image)
        _gradient(image)
        self._header(draw, "HOSTING ANALYTICS", snapshot)
        values = [
            ("cpu", "CPU", snapshot.cpu_percent or 0, BLUE),
            ("boxes", "RAM", snapshot.ram_percent or 0, PURPLE),
            ("hard-drive", "Storage", snapshot.storage_percent or 0, AMBER),
            ("activity", "Latência", min(100, (snapshot.latency_ms or 0) / 10), CYAN),
        ]
        for index, (icon, label, value, color) in enumerate(values):
            x = 90 + index * 370
            self._gauge(draw, (x, 190, x + 280, 470), label, value, color, icon=icon)
        self._history(draw, history or [], (90, 560, 1490, 770))
        self._footer(draw)
        return _output(image)

    def _header(self, draw: ImageDraw.ImageDraw, title: str, snapshot: HostingSnapshot) -> None:
        _draw_lucide_icon(draw, "server", 72, 58, 36, BLUE)
        draw.text((125, 55), title, font=_font(42, bold=True), fill=TEXT)
        draw.text(
            (125, 110),
            f"{snapshot.app_name} • {snapshot.region} • {datetime.now().strftime('%d/%m/%Y %H:%M')}",
            font=_font(18),
            fill=MUTED,
        )
        color = GREEN if snapshot.online else RED
        draw.rounded_rectangle((1290, 55, 1505, 108), radius=24, fill=(20, 28, 48), outline=color, width=2)
        _draw_lucide_icon(draw, "activity", 1312, 68, 24, color)
        draw.text((1335, 70), snapshot.status.upper(), font=_font(18, bold=True), fill=color)

    def _bars(
        self,
        draw: ImageDraw.ImageDraw,
        snapshot: HostingSnapshot,
        bounds: tuple[int, int, int, int],
    ) -> None:
        left, top, right, bottom = bounds
        draw.rounded_rectangle(bounds, radius=24, fill=PANEL, outline=GRID)
        _draw_lucide_icon(draw, "settings", left + 30, top + 27, 26, BLUE)
        draw.text((left + 70, top + 26), "Recursos", font=_font(25, bold=True), fill=TEXT)
        rows = [
            ("CPU", snapshot.cpu_percent or 0, BLUE),
            ("RAM", snapshot.ram_percent or 0, PURPLE),
            ("Armazenamento", snapshot.storage_percent or 0, AMBER),
            ("Rede", min(100, (snapshot.network_in_mb or 0) + (snapshot.network_out_mb or 0)), CYAN),
        ]
        for index, (label, value, color) in enumerate(rows):
            y = top + 80 + index * 38
            draw.text((left + 32, y), label, font=_font(16, bold=True), fill=MUTED)
            draw.rounded_rectangle((left + 210, y + 3, right - 110, y + 24), radius=10, fill=PANEL_2)
            width = int((right - left - 320) * max(0, min(100, value)) / 100)
            draw.rounded_rectangle((left + 210, y + 3, left + 210 + width, y + 24), radius=10, fill=color)
            draw.text((right - 92, y), _percent(value), font=_font(15, bold=True), fill=TEXT)

    def _action_buttons(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
    ) -> None:
        left, top, right, bottom = bounds
        draw.rounded_rectangle(bounds, radius=24, fill=PANEL, outline=GRID)
        draw.text(
            (left + 28, top + 23),
            "Ações rápidas",
            font=_font(20, bold=True),
            fill=TEXT,
        )
        actions = [
            ("server", "Visão", BLUE),
            ("chart-no-axes-column-increasing", "Métricas", CYAN),
            ("heart-pulse", "Health", GREEN),
            ("scroll-text", "Logs", AMBER),
            ("rocket", "Deploys", PURPLE),
            ("boxes", "Apps", BLUE),
            ("refresh-cw", "Reiniciar", RED),
            ("play", "Iniciar", GREEN),
            ("square", "Parar", RED),
        ]
        button_width = 126
        gap = 18
        x = left + 210
        for icon, label, color in actions:
            _visual_button(draw, x, top + 16, button_width, 48, icon, label, color)
            x += button_width + gap
            if x + button_width > right - 20:
                break

    def _history(
        self,
        draw: ImageDraw.ImageDraw,
        history: list[dict[str, Any]],
        bounds: tuple[int, int, int, int],
    ) -> None:
        left, top, right, bottom = bounds
        draw.rounded_rectangle(bounds, radius=24, fill=PANEL, outline=GRID)
        _draw_lucide_icon(draw, "chart-no-axes-column-increasing", left + 30, top + 27, 26, PURPLE)
        draw.text((left + 70, top + 26), "Histórico recente", font=_font(25, bold=True), fill=TEXT)
        points = history[-24:]
        if not points:
            draw.text((left + 32, top + 100), "Aguardando coleta de métricas.", font=_font(17), fill=MUTED)
            return
        series = [
            ("cpu", BLUE),
            ("ram", PURPLE),
            ("latency", CYAN),
        ]
        chart = (left + 40, top + 95, right - 40, bottom - 45)
        for tick in range(5):
            y = chart[3] - tick * (chart[3] - chart[1]) / 4
            draw.line((chart[0], y, chart[2], y), fill=GRID, width=1)
        for key, color in series:
            values = [float(item.get(key) or 0) for item in points]
            if key == "latency":
                values = [min(100, value / 10) for value in values]
            coords = _line_points(values, chart)
            if len(coords) > 1:
                draw.line(coords, fill=color, width=4, joint="curve")
        draw.text((left + 45, bottom - 35), "CPU", font=_font(13, bold=True), fill=BLUE)
        draw.text((left + 120, bottom - 35), "RAM", font=_font(13, bold=True), fill=PURPLE)
        draw.text((left + 195, bottom - 35), "LAT", font=_font(13, bold=True), fill=CYAN)

    def _gauge(
        self,
        draw: ImageDraw.ImageDraw,
        bounds: tuple[int, int, int, int],
        label: str,
        value: float,
        color: tuple[int, int, int],
        icon: str = "",
    ) -> None:
        left, top, right, bottom = bounds
        draw.rounded_rectangle(bounds, radius=26, fill=PANEL, outline=GRID)
        if icon:
            _draw_lucide_icon(draw, icon, left + 22, top + 22, 30, color)
        center = ((left + right) // 2, top + 145)
        radius = 85
        draw.arc(
            (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
            150,
            390,
            fill=PANEL_2,
            width=18,
        )
        end = 150 + 240 * max(0, min(100, value)) / 100
        draw.arc(
            (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
            150,
            end,
            fill=color,
            width=18,
        )
        _center(draw, (left, top + 115, right, top + 175), _percent(value), _font(30, bold=True), TEXT)
        _center(draw, (left, bottom - 60, right, bottom - 20), label, _font(18, bold=True), MUTED)

    def _footer(self, draw: ImageDraw.ImageDraw) -> None:
        draw.text((75, 835), "BELLA HOSTING CENTER • Square Cloud", font=_font(14, bold=True), fill=MUTED)
        generated = datetime.now().strftime("%d/%m/%Y • %H:%M")
        box = draw.textbbox((0, 0), generated, font=_font(14))
        draw.text((1520 - (box[2] - box[0]), 835), generated, font=_font(14), fill=MUTED)


def build_snapshot(
    app_id: str,
    status_data: dict[str, Any],
    metrics_data: dict[str, Any],
    deployments: list[dict[str, Any]],
    latency_ms: int,
    *,
    resolved_app_name: str = "",
) -> HostingSnapshot:
    status_payload = _extract_payload(status_data)
    metrics_payload = _extract_payload(metrics_data)
    app_name = str(
        status_payload.get("name")
        or status_payload.get("app_name")
        or status_payload.get("appName")
        or resolved_app_name
        or app_id
    )
    status = str(
        status_payload.get("status")
        or status_payload.get("state")
        or status_payload.get("currentStatus")
        or "unknown"
    ).lower()
    ram_used = _first_number(metrics_payload, "ram", "memory", "ram_usage", "memory_usage")
    ram_limit = _first_number(metrics_payload, "ram_limit", "memory_limit", "max_ram")
    cpu = _first_number(metrics_payload, "cpu", "cpu_usage", "cpu_percent")
    storage_used = _first_number(metrics_payload, "storage", "disk", "disk_usage")
    storage_limit = _first_number(metrics_payload, "storage_limit", "disk_limit", "max_storage")
    last_deploy = "Não informado"
    if deployments:
        deploy = deployments[0]
        last_deploy = str(
            deploy.get("date")
            or deploy.get("created_at")
            or deploy.get("createdAt")
            or deploy.get("time")
            or deploy.get("status")
            or "Registrado"
        )
    uptime_value = (
        status_payload.get("uptime")
        or metrics_payload.get("uptime")
        or status_payload.get("running_since")
        or status_payload.get("started_at")
    )
    return HostingSnapshot(
        app_id=app_id,
        app_name=app_name,
        status=status,
        region=str(status_payload.get("region") or status_payload.get("cluster") or "Não informado"),
        uptime=_format_uptime(uptime_value),
        latency_ms=latency_ms,
        ram_percent=_percent_from_pair(ram_used, ram_limit)
        or _first_number(metrics_payload, "ram_percent", "memory_percent"),
        ram_mb=ram_used,
        cpu_percent=cpu,
        storage_percent=_percent_from_pair(storage_used, storage_limit)
        or _first_number(metrics_payload, "storage_percent", "disk_percent"),
        storage_mb=storage_used,
        network_in_mb=_first_number(metrics_payload, "network_in", "download", "inbound"),
        network_out_mb=_first_number(metrics_payload, "network_out", "upload", "outbound"),
        requests=int(_first_number(metrics_payload, "requests", "http_requests") or 0),
        last_deploy=last_deploy,
        raw_status=status_data,
        raw_metrics=metrics_data,
    )


def build_alerts(snapshot: HostingSnapshot) -> list[HostingAlert]:
    alerts: list[HostingAlert] = []
    if not snapshot.online:
        alerts.append(
            HostingAlert(
                severity="crítica",
                title="Aplicação offline",
                description=f"{snapshot.app_name} está com status {snapshot.status}.",
                mention_owner=True,
            )
        )
    if (snapshot.cpu_percent or 0) >= 90:
        alerts.append(
            HostingAlert(
                severity="alta",
                title="CPU acima de 90%",
                description=f"CPU atual em {_percent(snapshot.cpu_percent)}.",
            )
        )
    if (snapshot.ram_percent or 0) >= 90:
        alerts.append(
            HostingAlert(
                severity="alta",
                title="RAM acima de 90%",
                description=f"RAM atual em {_percent(snapshot.ram_percent)}.",
            )
        )
    if (snapshot.storage_percent or 0) >= 85:
        alerts.append(
            HostingAlert(
                severity="média",
                title="Armazenamento perto do limite",
                description=f"Uso atual em {_percent(snapshot.storage_percent)}.",
            )
        )
    return alerts


async def basic_health_checks(domains: list[str]) -> dict[str, str]:
    results = {
        "hospedagem": "ok",
        "api_square": "ok",
        "banco": "não configurado",
        "ping": "ok",
        "dns": "não informado",
        "ssl": "não informado",
    }
    if domains:
        domain = _domain_from_value(domains[0])
        if domain:
            results["dns"] = await _check_dns(domain)
            results["ssl"] = await _check_ssl(domain)
    return results


def status_embed(snapshot: HostingSnapshot) -> discord.Embed:
    color = discord.Color.green() if snapshot.online else discord.Color.red()
    embed = discord.Embed(
        title="Status em tempo real",
        description=f"**{snapshot.app_name}** está **{snapshot.status.upper()}**.",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="CPU", value=_percent(snapshot.cpu_percent), inline=True)
    embed.add_field(name="RAM", value=_percent(snapshot.ram_percent), inline=True)
    embed.add_field(name="Uptime", value=snapshot.uptime, inline=True)
    embed.add_field(name="Latência", value=f"{snapshot.latency_ms or 0} ms", inline=True)
    embed.add_field(name="Região", value=snapshot.region, inline=True)
    embed.add_field(name="Deploy", value=snapshot.last_deploy[:1024], inline=True)
    embed.set_footer(text="Bella Hosting Center")
    return embed


def hosting_embed(snapshot: HostingSnapshot, alerts: list[HostingAlert]) -> discord.Embed:
    embed = status_embed(snapshot)
    embed.title = "Central de Hospedagem"
    embed.description = (
        f"Painel operacional da aplicação **{snapshot.app_name}** na Square Cloud.\n"
        f"Saúde atual: **{snapshot.health_label.upper()}**."
    )
    embed.add_field(name="Armazenamento", value=_percent(snapshot.storage_percent), inline=True)
    embed.add_field(
        name="Rede",
        value=(
            f"↓ {_number(snapshot.network_in_mb)} MB\n"
            f"↑ {_number(snapshot.network_out_mb)} MB"
        ),
        inline=True,
    )
    embed.add_field(name="Requests", value=str(snapshot.requests or 0), inline=True)
    if alerts:
        embed.add_field(
            name="Alertas",
            value="\n".join(f"- **{item.severity}:** {item.title}" for item in alerts[:5]),
            inline=False,
        )
    return embed


def list_embed(title: str, rows: list[str], *, empty: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description="\n".join(rows[:20]) if rows else empty,
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Bella Hosting Center")
    return embed


def diagnostics_embed(
    snapshot: HostingSnapshot,
    checks: dict[str, str],
    alerts: list[HostingAlert],
) -> discord.Embed:
    embed = discord.Embed(
        title="Diagnóstico da hospedagem",
        description=(
            f"Status: **{snapshot.status.upper()}**\n"
            f"Resultado: **{snapshot.health_label.upper()}**"
        ),
        color=discord.Color.green() if not alerts else discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Verificações",
        value="\n".join(f"- **{name.title()}:** {value}" for name, value in checks.items()),
        inline=False,
    )
    embed.add_field(
        name="Alertas locais",
        value=(
            "\n".join(f"- **{alert.severity}:** {alert.description}" for alert in alerts)
            if alerts
            else "Nenhum alerta crítico detectado por lógica local."
        ),
        inline=False,
    )
    return embed


def logs_file(logs: str) -> discord.File:
    content = logs.encode("utf-8", errors="replace")
    return discord.File(io.BytesIO(content), filename="squarecloud-logs.txt")


def _extract_payload(data: dict[str, Any]) -> dict[str, Any]:
    payload = data.get("response") or data.get("data") or data.get("result") or data
    return payload if isinstance(payload, dict) else data


def _extract_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    payload = data.get("response") or data.get("data") or data.get("result") or data
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("apps", "deployments", "deploys", "domains", "snapshots", "backups", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _extract_deployments(data: dict[str, Any]) -> list[dict[str, Any]]:
    payload = data.get("response") or data.get("data") or data.get("result") or data
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("deployments", "deploys", "items", "history", "logs"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _normalize_lookup(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _format_app_candidates(apps: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for app in apps[:10]:
        app_id = str(app.get("id") or app.get("app_id") or "").strip()
        name = str(app.get("name") or app.get("app_name") or app_id or "sem nome").strip()
        status = str(app.get("status") or app.get("state") or "sem status").strip()
        if app_id:
            rows.append(f"{name} ({app_id}) • {status}")
        else:
            rows.append(f"{name} • {status}")
    return "; ".join(rows)


def _extract_value(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    payload = _extract_payload(data)
    for key in keys:
        if isinstance(payload, dict) and key in payload:
            return payload[key]
        if key in data:
            return data[key]
    return None


def _first_number(data: dict[str, Any], *keys: str) -> float | None:
    stack = [data]
    seen = set()
    while stack:
        current = stack.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        for key in keys:
            if key in current:
                value = _to_number(current[key])
                if value is not None:
                    return value
        for value in current.values():
            if isinstance(value, dict):
                stack.append(value)
    return None


def _percent_from_pair(value: float | None, maximum: float | None) -> float | None:
    if value is None or maximum in {None, 0}:
        return None
    return max(0.0, min(100.0, value * 100 / float(maximum)))


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", ".")
        match = __import__("re").search(r"-?\d+(?:\.\d+)?", text)
        if match:
            return float(match.group(0))
    return None


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _format_uptime(value: Any) -> str:
    if value is None:
        return "Não informado"
    number = _to_number(value)
    if number is None:
        return str(value)
    seconds = int(number / 1000 if number > 10_000_000 else number)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


async def _check_dns(domain: str) -> str:
    import asyncio

    try:
        await asyncio.to_thread(socket.gethostbyname, domain)
        return "ok"
    except OSError:
        return "falhou"


async def _check_ssl(domain: str) -> str:
    import asyncio

    def run() -> str:
        try:
            context = ssl.create_default_context()
            with socket.create_connection((domain, 443), timeout=5) as sock:
                with context.wrap_socket(sock, server_hostname=domain) as secure:
                    cert = secure.getpeercert()
            expires = cert.get("notAfter")
            return f"ok ({expires})" if expires else "ok"
        except OSError:
            return "falhou"
        except ssl.SSLError:
            return "inválido"

    return await asyncio.to_thread(run)


def _domain_from_value(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return parsed.netloc or parsed.path


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _gradient(image: Image.Image) -> None:
    width, height = image.size
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            blue = int(8 + 22 * x / width + 16 * y / height)
            pixels[x, y] = (2, 3, max(8, blue))


def _card(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    height: int,
    label: str,
    value: str,
    color: tuple[int, int, int],
    *,
    icon: str = "",
) -> None:
    draw.rounded_rectangle((x, y, x + width, y + height), radius=22, fill=PANEL, outline=GRID)
    draw.rounded_rectangle((x + 22, y + 24, x + 30, y + height - 24), radius=4, fill=color)
    if icon:
        draw.rounded_rectangle((x + 48, y + 28, x + 88, y + 68), radius=14, fill=PANEL_2)
        _draw_lucide_icon(draw, icon, x + 57, y + 37, 22, color)
        text_x = x + 108
    else:
        text_x = x + 50
    draw.text((text_x, y + 22), label, font=_font(15, bold=True), fill=MUTED)
    draw.text((text_x, y + 52), str(value)[:24], font=_font(27, bold=True), fill=TEXT)


def _visual_button(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    height: int,
    icon: str,
    label: str,
    color: tuple[int, int, int],
) -> None:
    draw.rounded_rectangle(
        (x, y, x + width, y + height),
        radius=16,
        fill=(18, 23, 42),
        outline=(58, 68, 104),
        width=1,
    )
    draw.rounded_rectangle(
        (x + 8, y + 8, x + 40, y + height - 8),
        radius=11,
        fill=(25, 31, 55),
    )
    _draw_lucide_icon(draw, icon, x + 15, y + 15, 18, color)
    draw.text((x + 48, y + 15), label[:12], font=_font(14, bold=True), fill=TEXT)


def _draw_lucide_icon(
    draw: ImageDraw.ImageDraw,
    name: str,
    x: int,
    y: int,
    size: int,
    color: tuple[int, int, int],
) -> None:
    path = LUCIDE_ICON_DIR / f"{name}.svg"
    if not path.exists():
        return
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except Exception:
        return
    scale = size / 24
    stroke_width = max(2, int(size / 11))

    def point(px: float, py: float) -> tuple[float, float]:
        return x + px * scale, y + py * scale

    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "line":
            x1 = _svg_float(element.get("x1"))
            y1 = _svg_float(element.get("y1"))
            x2 = _svg_float(element.get("x2"))
            y2 = _svg_float(element.get("y2"))
            draw.line((*point(x1, y1), *point(x2, y2)), fill=color, width=stroke_width)
        elif tag == "rect":
            rx = _svg_float(element.get("rx"))
            left = _svg_float(element.get("x"))
            top = _svg_float(element.get("y"))
            width = _svg_float(element.get("width"))
            height = _svg_float(element.get("height"))
            draw.rounded_rectangle(
                (*point(left, top), *point(left + width, top + height)),
                radius=int(rx * scale),
                outline=color,
                width=stroke_width,
            )
        elif tag == "circle":
            cx = _svg_float(element.get("cx"))
            cy = _svg_float(element.get("cy"))
            radius = _svg_float(element.get("r"))
            draw.ellipse(
                (*point(cx - radius, cy - radius), *point(cx + radius, cy + radius)),
                outline=color,
                width=stroke_width,
            )
        elif tag in {"polyline", "polygon"}:
            points = _svg_points(element.get("points", ""), x, y, scale)
            if len(points) > 1:
                if tag == "polygon":
                    points.append(points[0])
                draw.line(points, fill=color, width=stroke_width, joint="curve")
        elif tag == "path":
            _draw_svg_path(draw, element.get("d", ""), x, y, scale, color, stroke_width)


def _draw_svg_path(
    draw: ImageDraw.ImageDraw,
    data: str,
    x: int,
    y: int,
    scale: float,
    color: tuple[int, int, int],
    stroke_width: int,
) -> None:
    tokens = re.findall(r"[A-Za-z]|-?\d*\.?\d+(?:e[-+]?\d+)?", data)
    index = 0
    command = ""
    current = (0.0, 0.0)
    start = (0.0, 0.0)

    def is_command(value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z]", value))

    def read_number(default: float = 0.0) -> float:
        nonlocal index
        if index >= len(tokens) or is_command(tokens[index]):
            return default
        value = float(tokens[index])
        index += 1
        return value

    def screen(point: tuple[float, float]) -> tuple[float, float]:
        return x + point[0] * scale, y + point[1] * scale

    while index < len(tokens):
        if is_command(tokens[index]):
            command = tokens[index]
            index += 1
        if not command:
            break
        absolute = command.isupper()
        code = command.upper()
        if code == "M":
            px = read_number()
            py = read_number()
            current = (px, py) if absolute else (current[0] + px, current[1] + py)
            start = current
            command = "L" if absolute else "l"
        elif code == "L":
            px = read_number()
            py = read_number()
            next_point = (px, py) if absolute else (current[0] + px, current[1] + py)
            draw.line((*screen(current), *screen(next_point)), fill=color, width=stroke_width)
            current = next_point
        elif code == "H":
            px = read_number()
            next_point = (px, current[1]) if absolute else (current[0] + px, current[1])
            draw.line((*screen(current), *screen(next_point)), fill=color, width=stroke_width)
            current = next_point
        elif code == "V":
            py = read_number()
            next_point = (current[0], py) if absolute else (current[0], current[1] + py)
            draw.line((*screen(current), *screen(next_point)), fill=color, width=stroke_width)
            current = next_point
        elif code == "C":
            values = [read_number() for _ in range(6)]
            end = (values[4], values[5])
            next_point = end if absolute else (current[0] + end[0], current[1] + end[1])
            draw.line((*screen(current), *screen(next_point)), fill=color, width=stroke_width)
            current = next_point
        elif code == "S" or code == "Q":
            values = [read_number() for _ in range(4)]
            end = (values[2], values[3])
            next_point = end if absolute else (current[0] + end[0], current[1] + end[1])
            draw.line((*screen(current), *screen(next_point)), fill=color, width=stroke_width)
            current = next_point
        elif code == "T":
            px = read_number()
            py = read_number()
            next_point = (px, py) if absolute else (current[0] + px, current[1] + py)
            draw.line((*screen(current), *screen(next_point)), fill=color, width=stroke_width)
            current = next_point
        elif code == "A":
            values = [read_number() for _ in range(7)]
            end = (values[5], values[6])
            next_point = end if absolute else (current[0] + end[0], current[1] + end[1])
            draw.line((*screen(current), *screen(next_point)), fill=color, width=stroke_width)
            current = next_point
        elif code == "Z":
            draw.line((*screen(current), *screen(start)), fill=color, width=stroke_width)
            current = start
        else:
            break


def _svg_float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def _svg_points(value: str, x: int, y: int, scale: float) -> list[tuple[float, float]]:
    numbers = [float(item) for item in re.findall(r"-?\d*\.?\d+", value)]
    return [
        (x + numbers[index] * scale, y + numbers[index + 1] * scale)
        for index in range(0, len(numbers) - 1, 2)
    ]


def _line_points(values: list[float], bounds: tuple[int, int, int, int]) -> list[tuple[float, float]]:
    left, top, right, bottom = bounds
    if not values:
        return []
    maximum = max(1.0, max(values))
    if len(values) == 1:
        return [((left + right) / 2, bottom - (bottom - top) * values[0] / maximum)]
    return [
        (
            left + index * (right - left) / (len(values) - 1),
            bottom - (bottom - top) * value / maximum,
        )
        for index, value in enumerate(values)
    ]


def _center(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int, int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(
        (
            bounds[0] + (bounds[2] - bounds[0] - (box[2] - box[0])) / 2,
            bounds[1] + (bounds[3] - bounds[1] - (box[3] - box[1])) / 2,
        ),
        text,
        font=font,
        fill=fill,
    )


def _output(image: Image.Image) -> io.BytesIO:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer


def _percent(value: float | None) -> str:
    if value is None:
        return "N/D"
    return f"{value:.0f}%"


def _number(value: float | None) -> str:
    if value is None:
        return "N/D"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return f"{value:.0f}"


def _status_color(value: float | None) -> tuple[int, int, int]:
    if value is None:
        return MUTED
    if value >= 90:
        return RED
    if value >= 75:
        return AMBER
    return GREEN
