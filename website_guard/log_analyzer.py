from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .state import StateStore

STATUS_RE = re.compile(r'"\S+\s+[^"]+\s+HTTP/[\d.]+"\s+(\d{3})')
IP_RE = re.compile(r"^[0-9a-fA-F:.]+")

SUSPICIOUS_PATTERNS = (
    "wp-login.php",
    "xmlrpc.php",
    "phpmyadmin",
    "adminer.php",
    "/.env",
    "/.git",
    "../",
    "%2e%2e",
    "union select",
    "union+select",
    "select%20",
    "drop table",
    "%3cscript",
    "<script",
    "/etc/passwd",
    "cmd=",
    "exec=",
    "base64_",
    "eval(",
    "shell",
    "benchmark(",
    "sleep(",
    "config.php",
)


@dataclass
class LogAlert:
    key: str
    title: str
    description: str
    severity: str = "warning"
    evidence: str | None = None


@dataclass
class LogScanReport:
    checked_at: str
    path: str
    lines_read: int = 0
    error_count: int = 0
    suspicious_count: int = 0
    top_ips: list[tuple[str, int]] = field(default_factory=list)
    status_counts: dict[str, int] = field(default_factory=dict)
    suspicious_samples: list[str] = field(default_factory=list)
    alerts: list[LogAlert] = field(default_factory=list)
    error: str | None = None

    @property
    def has_alerts(self) -> bool:
        return bool(self.alerts)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["alerts"] = [asdict(alert) for alert in self.alerts]
        return value

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LogScanReport":
        alerts = [LogAlert(**alert) for alert in data.get("alerts", [])]
        payload = dict(data)
        payload["alerts"] = alerts
        payload["top_ips"] = [tuple(item) for item in payload.get("top_ips", [])]
        return cls(**payload)


class AccessLogAnalyzer:
    def __init__(self, settings: Settings, state: StateStore) -> None:
        self.settings = settings
        self.state = state

    async def scan(self) -> LogScanReport | None:
        if not self.settings.access_log_path:
            return None

        path = self.settings.access_log_path
        report = LogScanReport(
            checked_at=datetime.now(timezone.utc).isoformat(),
            path=str(path),
        )

        if not path.exists():
            report.error = "Arquivo de log nao encontrado."
            report.alerts.append(
                LogAlert(
                    key="log_missing",
                    title="Access log nao encontrado",
                    description="O caminho configurado em ACCESS_LOG_PATH nao existe.",
                    severity="warning",
                    evidence=str(path),
                )
            )
            await self.state.set("latest_log_report", report.to_dict())
            return report

        try:
            lines = await self._read_new_lines(path)
        except Exception as exc:
            report.error = f"{type(exc).__name__}: {exc}"
            report.alerts.append(
                LogAlert(
                    key="log_read_failed",
                    title="Falha ao ler access log",
                    description="O bot nao conseguiu ler o arquivo de log configurado.",
                    severity="warning",
                    evidence=report.error,
                )
            )
            await self.state.set("latest_log_report", report.to_dict())
            return report

        report.lines_read = len(lines)
        self._analyze_lines(lines, report)
        await self.state.set("latest_log_report", report.to_dict())
        return report

    async def _read_new_lines(self, path: Path) -> list[str]:
        offsets = await self.state.get_section("log_offsets")
        current_offset = int(offsets.get(str(path), 0))
        file_size = path.stat().st_size

        if file_size < current_offset:
            current_offset = 0

        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            handle.seek(current_offset)
            lines = handle.readlines()
            new_offset = handle.tell()

        await self.state.update_section("log_offsets", str(path), new_offset)
        return lines

    def _analyze_lines(self, lines: list[str], report: LogScanReport) -> None:
        ip_counts: Counter[str] = Counter()
        status_counts: Counter[str] = Counter()
        suspicious_samples: list[str] = []

        for line in lines:
            lowered = line.lower()
            ip = _extract_ip(line)
            if ip:
                ip_counts[ip] += 1

            status = _extract_status(line)
            if status:
                status_counts[str(status)] += 1
                if status >= 400:
                    report.error_count += 1

            if any(pattern in lowered for pattern in SUSPICIOUS_PATTERNS):
                report.suspicious_count += 1
                if len(suspicious_samples) < 8:
                    suspicious_samples.append(line.strip()[:250])

        report.top_ips = ip_counts.most_common(5)
        report.status_counts = dict(status_counts.most_common())
        report.suspicious_samples = suspicious_samples

        if report.error_count >= self.settings.log_error_threshold:
            report.alerts.append(
                LogAlert(
                    key="many_http_errors",
                    title="Muitos erros HTTP no access log",
                    description=(
                        f"Foram encontrados {report.error_count} status 4xx/5xx "
                        f"desde a ultima varredura."
                    ),
                    severity="warning",
                    evidence=str(report.status_counts),
                )
            )

        if report.suspicious_count >= self.settings.log_suspicious_threshold:
            report.alerts.append(
                LogAlert(
                    key="suspicious_requests",
                    title="Requisicoes suspeitas detectadas",
                    description=(
                        f"Foram encontradas {report.suspicious_count} linhas com padroes "
                        "comuns de scanner, brute force, SQLi, XSS ou LFI."
                    ),
                    severity="critical",
                    evidence="\n".join(report.suspicious_samples[:3]),
                )
            )

        if report.top_ips and report.top_ips[0][1] >= self.settings.log_ip_threshold:
            top_ip, count = report.top_ips[0]
            report.alerts.append(
                LogAlert(
                    key=f"ip_burst_{top_ip}",
                    title="Volume alto vindo do mesmo IP",
                    description=(
                        f"O IP {top_ip} apareceu {count} vezes desde a ultima varredura."
                    ),
                    severity="warning",
                    evidence=", ".join(
                        f"{ip}: {total}" for ip, total in report.top_ips
                    ),
                )
            )


def _extract_ip(line: str) -> str | None:
    match = IP_RE.match(line.strip())
    return match.group(0) if match else None


def _extract_status(line: str) -> int | None:
    match = STATUS_RE.search(line)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None
