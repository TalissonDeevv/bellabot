from __future__ import annotations

import asyncio
import hashlib
import socket
import ssl
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp

from .config import Settings
from .http_utils import read_limited
from .state import StateStore

MAX_BODY_BYTES = 2 * 1024 * 1024


@dataclass
class MonitorAlert:
    key: str
    title: str
    description: str
    severity: str = "warning"
    evidence: str | None = None


@dataclass
class MonitorReport:
    checked_at: str
    site_url: str
    ok: bool
    status_code: int | None = None
    response_ms: int | None = None
    final_url: str | None = None
    content_hash: str | None = None
    tls_days_left: int | None = None
    tls_expires_at: str | None = None
    bytes_received: int = 0
    content_type: str | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    verification_status: str = "not_configured"
    verification_meta_name: str | None = None
    alerts: list[MonitorAlert] = field(default_factory=list)
    error: str | None = None

    @property
    def has_alerts(self) -> bool:
        return bool(self.alerts)

    @property
    def critical(self) -> bool:
        return any(alert.severity == "critical" for alert in self.alerts)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["alerts"] = [asdict(alert) for alert in self.alerts]
        return value

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MonitorReport":
        alerts = [MonitorAlert(**alert) for alert in data.get("alerts", [])]
        payload = dict(data)
        payload["alerts"] = alerts
        return cls(**payload)


class WebsiteMonitor:
    def __init__(self, settings: Settings, state: StateStore) -> None:
        self.settings = settings
        self.state = state

    async def check(self, session: aiohttp.ClientSession) -> MonitorReport:
        checked_at = datetime.now(timezone.utc).isoformat()
        report = MonitorReport(
            checked_at=checked_at,
            site_url=self.settings.site_url,
            ok=True,
        )

        try:
            start = time.perf_counter()
            async with session.get(
                _cache_busted_url(self.settings.site_url),
                allow_redirects=True,
                ssl=self.settings.verify_tls,
                headers={
                    "User-Agent": "Discord-Site-Sentinel/2.0",
                    "Accept": "text/html,application/xhtml+xml",
                    "Cache-Control": "no-cache, no-store, max-age=0",
                    "Pragma": "no-cache",
                },
            ) as response:
                body = await read_limited(response.content, MAX_BODY_BYTES)
                report.response_ms = int((time.perf_counter() - start) * 1000)
                report.status_code = response.status
                report.final_url = _strip_probe_parameter(str(response.url))
                report.content_hash = hashlib.sha256(body).hexdigest()
                report.bytes_received = len(body)
                report.content_type = response.headers.get("Content-Type")
                report.response_headers = _diagnostic_headers(response.headers)
                self._check_status(report)
                self._check_latency(report)
                self._check_security_headers(report, response.headers)
                await self._check_content(report, body, response.charset)
        except Exception as exc:
            report.ok = False
            report.error = f"{type(exc).__name__}: {exc}"
            report.alerts.append(
                MonitorAlert(
                    key="site_unreachable",
                    title="Site inacessivel",
                    description="A checagem nao conseguiu completar a requisicao.",
                    severity="critical",
                    evidence=report.error,
                )
            )

        await self._check_tls(report)
        report.ok = not report.critical
        await self.state.set("latest_site_report", report.to_dict())
        return report

    def _check_status(self, report: MonitorReport) -> None:
        if report.status_code not in self.settings.expected_status_codes:
            report.alerts.append(
                MonitorAlert(
                    key=f"bad_status_{report.status_code}",
                    title="Status HTTP inesperado",
                    description=(
                        f"O site respondeu HTTP {report.status_code}, fora do esperado "
                        f"{sorted(self.settings.expected_status_codes)}."
                    ),
                    severity="critical",
                )
            )

    def _check_latency(self, report: MonitorReport) -> None:
        if report.response_ms is None:
            return
        if report.response_ms > self.settings.max_response_ms:
            report.alerts.append(
                MonitorAlert(
                    key="high_latency",
                    title="Site lento",
                    description=(
                        f"Tempo de resposta em {report.response_ms} ms, acima do limite "
                        f"de {self.settings.max_response_ms} ms."
                    ),
                    severity="warning",
                )
            )

    def _check_security_headers(
        self,
        report: MonitorReport,
        headers: aiohttp.typedefs.LooseHeaders,
    ) -> None:
        missing = [
            header
            for header in self.settings.security_headers
            if header and header not in headers
        ]
        if missing:
            report.alerts.append(
                MonitorAlert(
                    key="missing_security_headers",
                    title="Cabecalho de seguranca ausente",
                    description="Alguns cabecalhos esperados nao vieram na resposta.",
                    severity="warning",
                    evidence=", ".join(missing),
                )
            )

    async def _check_content(
        self,
        report: MonitorReport,
        body: bytes,
        charset: str | None = None,
    ) -> None:
        decoded = _decode_body(body, charset)
        self._check_site_verify_key(report, decoded)
        text = decoded.lower()

        if self.settings.content_must_contain:
            expected = self.settings.content_must_contain.lower()
            if expected not in text:
                report.alerts.append(
                    MonitorAlert(
                        key="required_content_missing",
                        title="Conteudo esperado sumiu",
                        description="Uma frase obrigatoria nao foi encontrada na pagina.",
                        severity="critical",
                        evidence=self.settings.content_must_contain,
                    )
                )

        for forbidden in self.settings.content_must_not_contain:
            if forbidden.lower() in text:
                report.alerts.append(
                    MonitorAlert(
                        key=f"forbidden_content_{hashlib.sha1(forbidden.encode()).hexdigest()[:8]}",
                        title="Conteudo suspeito encontrado",
                        description="Uma palavra ou frase proibida apareceu na pagina.",
                        severity="critical",
                        evidence=forbidden,
                    )
                )

        if not self.settings.watch_content_changes or not report.content_hash:
            return

        previous_hash = await self.state.get("baseline_content_hash")
        if previous_hash is None:
            await self.state.set("baseline_content_hash", report.content_hash)
            return

        if previous_hash != report.content_hash:
            report.alerts.append(
                MonitorAlert(
                    key="content_hash_changed",
                    title="Conteudo do site mudou",
                    description=(
                        "O hash da pagina mudou desde a ultima base salva. "
                        "Isso pode ser normal em sites dinamicos, mas tambem ajuda a detectar defacement."
                    ),
                    severity="warning",
                    evidence=f"anterior={previous_hash[:12]} atual={report.content_hash[:12]}",
                )
            )
            await self.state.set("baseline_content_hash", report.content_hash)

    def _check_site_verify_key(self, report: MonitorReport, html: str) -> None:
        expected_key = self.settings.site_verify_key
        report.verification_meta_name = self.settings.site_verify_meta_name
        if not expected_key:
            report.verification_status = "not_configured"
            return

        parser = _MetaTagParser()
        parser.feed(html)
        actual_key = parser.meta.get(self.settings.site_verify_meta_name.casefold())

        if actual_key is None:
            report.verification_status = "missing"
            report.alerts.append(
                MonitorAlert(
                    key="site_verify_key_missing",
                    title="Key de verificacao ausente",
                    description=(
                        "A meta tag de verificacao do monitor nao foi encontrada no HTML do site."
                    ),
                    severity="critical",
                    evidence=f'meta name="{self.settings.site_verify_meta_name}"',
                )
            )
            return

        if actual_key.strip() != expected_key:
            report.verification_status = "mismatch"
            report.alerts.append(
                MonitorAlert(
                    key="site_verify_key_mismatch",
                    title="Key de verificacao alterada",
                    description="A key encontrada no site e diferente da key configurada no bot.",
                    severity="critical",
                    evidence=(
                        f"esperada={_redact_key(expected_key)} "
                        f"encontrada={_redact_key(actual_key.strip())}"
                    ),
                )
            )
            return

        report.verification_status = "verified"

    async def _check_tls(self, report: MonitorReport) -> None:
        parsed = urlparse(self.settings.site_url)
        if parsed.scheme != "https":
            return

        try:
            expires_at = await asyncio.to_thread(
                _fetch_tls_expiration,
                parsed.hostname or "",
                parsed.port or 443,
            )
        except Exception as exc:
            report.alerts.append(
                MonitorAlert(
                    key="tls_check_failed",
                    title="Falha ao verificar TLS",
                    description="Nao foi possivel ler a validade do certificado HTTPS.",
                    severity="warning",
                    evidence=f"{type(exc).__name__}: {exc}",
                )
            )
            return

        now = datetime.now(timezone.utc)
        days_left = (expires_at - now).days
        report.tls_days_left = days_left
        report.tls_expires_at = expires_at.isoformat()

        if days_left < 0:
            report.alerts.append(
                MonitorAlert(
                    key="tls_expired",
                    title="Certificado HTTPS vencido",
                    description="O certificado TLS do site esta vencido.",
                    severity="critical",
                    evidence=expires_at.isoformat(),
                )
            )
        elif days_left <= self.settings.tls_warning_days:
            report.alerts.append(
                MonitorAlert(
                    key="tls_expiring",
                    title="Certificado HTTPS perto de vencer",
                    description=(
                        f"O certificado vence em {days_left} dias. "
                        f"Limite configurado: {self.settings.tls_warning_days} dias."
                    ),
                    severity="warning",
                    evidence=expires_at.isoformat(),
                )
            )


def _fetch_tls_expiration(hostname: str, port: int) -> datetime:
    if not hostname:
        raise ValueError("Hostname vazio.")

    context = ssl.create_default_context()
    with socket.create_connection((hostname, port), timeout=10) as sock:
        with context.wrap_socket(sock, server_hostname=hostname) as secure_sock:
            certificate = secure_sock.getpeercert()

    not_after = certificate.get("notAfter")
    if not not_after:
        raise ValueError("Certificado sem campo notAfter.")

    parsed = parsedate_to_datetime(not_after)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class _MetaTagParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return

        values = {name.lower(): value or "" for name, value in attrs}
        name = values.get("name") or values.get("property")
        content = values.get("content")
        if name and content is not None:
            self.meta[name.strip().casefold()] = content


def _decode_body(body: bytes, charset: str | None) -> str:
    encodings = [charset, "utf-8", "latin-1"]
    for encoding in encodings:
        if not encoding:
            continue
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _cache_busted_url(url: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["_sentinel_probe"] = str(time.time_ns())
    return urlunparse(parsed._replace(query=urlencode(query)))


def _strip_probe_parameter(url: str) -> str:
    parsed = urlparse(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "_sentinel_probe"
    ]
    return urlunparse(parsed._replace(query=urlencode(query)))


def _diagnostic_headers(headers: aiohttp.typedefs.LooseHeaders) -> dict[str, str]:
    interesting = (
        "Age",
        "Cache-Control",
        "Content-Type",
        "ETag",
        "Last-Modified",
        "Server",
        "X-Vercel-Cache",
        "CF-Cache-Status",
    )
    result: dict[str, str] = {}
    for name in interesting:
        value = headers.get(name)  # type: ignore[union-attr]
        if value:
            result[name] = str(value)
    return result


def _redact_key(value: str) -> str:
    if len(value) <= 12:
        return "***"
    return f"{value[:8]}...{value[-6:]}"
