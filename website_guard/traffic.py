from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .config import Settings
from .state import StateStore

COMBINED_LOG_RE = re.compile(
    r'^(?P<ip>\S+)\s+\S+\s+\S+\s+\[(?P<timestamp>[^\]]+)\]\s+'
    r'"(?P<method>\S+)\s+(?P<path>\S+)(?:\s+HTTP/[^"]+)?"\s+'
    r'(?P<status>\d{3})\s+\S+(?:\s+"(?P<referer>[^"]*)"\s+'
    r'"(?P<user_agent>[^"]*)")?'
)

ASSET_EXTENSIONS = {
    ".avif",
    ".css",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".map",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".svg",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".xml",
}

BOT_MARKERS = (
    "bot",
    "crawler",
    "spider",
    "slurp",
    "headless",
    "lighthouse",
    "discord-site-sentinel",
    "uptimerobot",
    "pingdom",
    "statuscake",
    "google-inspectiontool",
)


def verify_vercel_signature(
    raw_body: bytes,
    received_signature: str,
    secret: str,
) -> bool:
    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha1,
    ).hexdigest()
    return (
        len(received_signature) == len(expected)
        and hmac.compare_digest(received_signature, expected)
    )


@dataclass(frozen=True)
class TrafficSnapshot:
    available: bool
    source: str
    period_start: str
    period_end: str
    pageviews: int = 0
    visits: int = 0
    unique_visitors: int = 0
    top_pages: list[tuple[str, int]] = field(default_factory=list)
    top_referrers: list[tuple[str, int]] = field(default_factory=list)
    top_countries: list[tuple[str, int]] = field(default_factory=list)
    device_types: dict[str, int] = field(default_factory=dict)
    browsers: dict[str, int] = field(default_factory=dict)
    daily_pageviews: dict[str, int] = field(default_factory=dict)
    status_counts: dict[str, int] = field(default_factory=dict)
    bot_requests: int = 0
    monitor_requests: int = 0
    crawler_requests: int = 0
    automated_requests: int = 0
    excluded_requests: int = 0
    records_processed: int = 0
    session_minutes: int = 30
    native_sessions: bool = False
    source_kind: str = "access_log"
    data_note: str = ""
    error: str | None = None


@dataclass(frozen=True)
class ParsedTrafficEvent:
    timestamp: datetime
    method: str
    path: str
    status: int
    client_ip: str
    user_agent: str
    referer: str
    host: str
    source_id: str
    visitor_id: str = ""
    session_id: str = ""
    country: str = ""
    device_type: str = ""
    browser: str = ""
    deduplicate: bool = True


class TrafficAnalytics:
    def __init__(self, settings: Settings, state: StateStore) -> None:
        self.settings = settings
        self.state = state
        self._ingest_lock = asyncio.Lock()

    async def ensure_source(self) -> bool:
        path = self.settings.traffic_log_path
        if not path:
            return False
        try:
            await asyncio.to_thread(_ensure_log_file, path)
            await self.state.set("traffic_last_error", None)
            return True
        except OSError as exc:
            await self.state.set(
                "traffic_last_error",
                f"Não foi possível preparar {path}: {type(exc).__name__}: {exc}",
            )
            return False

    async def scan(self) -> int:
        path = self.settings.traffic_log_path
        if not path:
            return 0
        if not path.exists():
            await self.state.set(
                "traffic_last_error",
                f"Arquivo de audiência não encontrado: {path}",
            )
            return 0

        try:
            lines = await self._read_new_lines(path)
            parsed = list(_parse_lines(lines))
            await self._store_events(parsed)
            await self.state.set_many(
                {
                    "traffic_last_error": None,
                    "traffic_last_scan_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            return len(parsed)
        except Exception as exc:
            await self.state.set(
                "traffic_last_error",
                f"{type(exc).__name__}: {exc}",
            )
            raise

    async def ingest_body(self, raw_body: bytes) -> int:
        text = raw_body.decode("utf-8")
        parsed = list(_parse_lines([text]))
        path = self.settings.traffic_log_path
        if path:
            async with self._ingest_lock:
                await asyncio.to_thread(_append_ingest_payload, path, text)
                processed = await self.scan()
        else:
            await self._store_events(parsed)
            processed = len(parsed)
        await self.state.set_many(
            {
                "traffic_last_error": None,
                "traffic_last_ingest_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return processed

    async def snapshot(self, days: int = 7) -> TrafficSnapshot:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)
        ga4_property_id = str(
            getattr(self.settings, "ga4_property_id", "")
        ).strip()
        if ga4_property_id:
            try:
                return await self._ga4_snapshot(days)
            except Exception as exc:
                message = f"Google Analytics 4: {type(exc).__name__}: {exc}"
                await self.state.set("traffic_last_error", message)
                return TrafficSnapshot(
                    available=False,
                    source=f"Google Analytics 4 ({ga4_property_id})",
                    period_start=start.date().isoformat(),
                    period_end=now.date().isoformat(),
                    source_kind="ga4",
                    error=message,
                    data_note=(
                        "Confira GA4_PROPERTY_ID, a credencial de servico e o "
                        "acesso da conta ao Google Analytics."
                    ),
                )
        if self.settings.traffic_log_path:
            source = str(self.settings.traffic_log_path)
        elif self.settings.traffic_ingest_enabled:
            source = (
                f"HTTP {self.settings.traffic_ingest_host}:"
                f"{self.settings.traffic_ingest_port}"
                f"{self.settings.traffic_ingest_path}"
            )
        else:
            source = "não configurada"
        if (
            not self.settings.traffic_log_path
            and not self.settings.traffic_ingest_enabled
        ):
            return TrafficSnapshot(
                available=False,
                source=source,
                period_start=start.date().isoformat(),
                period_end=now.date().isoformat(),
                error=(
                    "Configure GA4_PROPERTY_ID e GOOGLE_APPLICATION_CREDENTIALS "
                    "ou use TRAFFIC_LOG_PATH com um access log real."
                ),
                session_minutes=self.settings.traffic_session_minutes,
            )

        events = await self.state.get_section("traffic_events")
        last_error = await self.state.get("traffic_last_error")
        selected: list[dict[str, Any]] = []
        for event in events.values():
            if not isinstance(event, dict):
                continue
            timestamp = _parse_iso_timestamp(str(event.get("timestamp", "")))
            if timestamp and timestamp >= start:
                selected.append(event)

        if not selected and last_error:
            return TrafficSnapshot(
                available=False,
                source=source,
                period_start=start.date().isoformat(),
                period_end=now.date().isoformat(),
                error=str(last_error),
                session_minutes=self.settings.traffic_session_minutes,
            )
        if not selected:
            last_error = (
                "Fonte de tráfego configurada e pronta; nenhum evento real foi "
                "recebido nos últimos sete dias."
            )

        page_events = [event for event in selected if event.get("pageview")]
        page_events.sort(key=lambda item: str(item.get("timestamp", "")))
        pages = Counter(str(event.get("path", "/")) for event in page_events)
        referrers = Counter(
            str(event["referrer"])
            for event in page_events
            if event.get("referrer")
        )
        statuses = Counter(str(event.get("status", "n/a")) for event in selected)
        countries = Counter(
            str(event["country"])
            for event in page_events
            if event.get("country")
        )
        devices = Counter(
            str(event["device_type"])
            for event in page_events
            if event.get("device_type")
        )
        browsers = Counter(
            str(event["browser"])
            for event in page_events
            if event.get("browser")
        )
        daily = Counter(
            str(event.get("timestamp", ""))[:10] for event in page_events
        )
        visitors = {
            str(event.get("visitor"))
            for event in page_events
            if event.get("visitor")
        }
        visits = _count_sessions(
            page_events,
            self.settings.traffic_session_minutes,
        )
        return TrafficSnapshot(
            available=True,
            source=source,
            period_start=start.date().isoformat(),
            period_end=now.date().isoformat(),
            pageviews=len(page_events),
            visits=visits,
            unique_visitors=len(visitors),
            top_pages=pages.most_common(8),
            top_referrers=referrers.most_common(6),
            top_countries=countries.most_common(6),
            device_types=dict(devices.most_common(6)),
            browsers=dict(browsers.most_common(6)),
            daily_pageviews=dict(sorted(daily.items())),
            status_counts=dict(statuses),
            bot_requests=sum(bool(event.get("bot")) for event in selected),
            monitor_requests=sum(
                event.get("traffic_kind") == "monitor" for event in selected
            ),
            crawler_requests=sum(
                event.get("traffic_kind") == "crawler" for event in selected
            ),
            automated_requests=sum(
                event.get("traffic_kind") == "automated" for event in selected
            ),
            excluded_requests=sum(
                not bool(event.get("pageview")) and not bool(event.get("bot"))
                for event in selected
            ),
            records_processed=len(selected),
            session_minutes=self.settings.traffic_session_minutes,
            native_sessions=any(event.get("session") for event in page_events),
            error=str(last_error) if last_error else None,
        )

    async def _ga4_snapshot(self, days: int) -> TrafficSnapshot:
        cache = await self.state.get("ga4_snapshot_cache")
        cache_minutes = int(getattr(self.settings, "ga4_cache_minutes", 30))
        if isinstance(cache, dict):
            cached_at = _parse_iso_timestamp(str(cache.get("cached_at", "")))
            payload = cache.get("snapshot")
            if (
                cached_at
                and datetime.now(timezone.utc) - cached_at
                < timedelta(minutes=cache_minutes)
                and isinstance(payload, dict)
            ):
                return TrafficSnapshot(**payload)

        snapshot = await asyncio.to_thread(
            _fetch_ga4_snapshot,
            self.settings,
            days,
        )
        await self.state.set_many(
            {
                "ga4_snapshot_cache": {
                    "cached_at": datetime.now(timezone.utc).isoformat(),
                    "snapshot": asdict(snapshot),
                },
                "traffic_last_error": None,
                "traffic_last_scan_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return snapshot

    async def _read_new_lines(self, path: Path) -> list[str]:
        offsets = await self.state.get_section("traffic_offsets")
        current_offset = int(offsets.get(str(path), 0))
        file_size = path.stat().st_size
        if file_size < current_offset:
            current_offset = 0
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            handle.seek(current_offset)
            lines = handle.readlines()
            new_offset = handle.tell()
        await self.state.update_section("traffic_offsets", str(path), new_offset)
        return lines

    async def _store_events(self, events: list[ParsedTrafficEvent]) -> None:
        if not events:
            return
        salt = await self.state.get("traffic_hash_salt")
        if not salt:
            salt = secrets.token_hex(24)
            await self.state.set("traffic_hash_salt", salt)
        allowed_hosts = {
            host.lower().split(":", 1)[0] for host in self.settings.traffic_hosts
        }
        own_hosts = allowed_hosts | {
            host.removeprefix("www.") for host in allowed_hosts
        } | {
            f"www.{host}" for host in allowed_hosts if not host.startswith("www.")
        }
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.settings.traffic_retention_days
        )

        def mutate(values: dict[str, Any]) -> None:
            for key, value in list(values.items()):
                if not isinstance(value, dict):
                    del values[key]
                    continue
                timestamp = _parse_iso_timestamp(str(value.get("timestamp", "")))
                if not timestamp or timestamp < cutoff:
                    del values[key]

            for event in events:
                if allowed_hosts and event.host:
                    event_host = event.host.lower().split(":", 1)[0]
                    if event_host not in allowed_hosts:
                        continue
                path = _normalize_path(event.path)
                traffic_kind = _traffic_kind(event.user_agent)
                bot = traffic_kind != "user"
                pageview = (
                    event.method.upper() in {"GET", "HEAD"}
                    and 200 <= event.status < 400
                    and not bot
                    and not _is_asset(path)
                )
                visitor = _hash_visitor(
                    str(salt),
                    event.visitor_id or event.client_ip or event.session_id,
                    event.user_agent,
                )
                session = (
                    _hash_value(
                        str(salt),
                        f"{visitor}|{event.session_id}",
                    )
                    if event.session_id
                    else ""
                )
                referrer = _referrer_domain(event.referer, own_hosts)
                event_id = event.source_id or hashlib.sha256(
                    (
                        f"{event.timestamp.isoformat()}|{event.client_ip}|"
                        f"{event.method}|{event.path}|{event.status}|"
                        f"{event.user_agent}"
                    ).encode("utf-8")
                ).hexdigest()
                if not event.deduplicate:
                    event_id = secrets.token_hex(16)
                values[event_id] = {
                    "timestamp": event.timestamp.astimezone(timezone.utc).isoformat(),
                    "method": event.method.upper(),
                    "path": path,
                    "status": event.status,
                    "visitor": visitor,
                    "session": session,
                    "referrer": referrer,
                    "country": event.country.upper(),
                    "device_type": event.device_type.lower(),
                    "browser": event.browser,
                    "bot": bot,
                    "traffic_kind": traffic_kind,
                    "pageview": pageview,
                }
            overflow = len(values) - self.settings.traffic_max_events
            if overflow > 0:
                oldest = sorted(
                    values,
                    key=lambda key: str(values[key].get("timestamp", "")),
                )[:overflow]
                for key in oldest:
                    del values[key]

        await self.state.mutate_section("traffic_events", mutate)


def _fetch_ga4_snapshot(settings: Settings, days: int) -> TrafficSnapshot:
    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            DateRange,
            Dimension,
            Metric,
            RunReportRequest,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Dependencia google-analytics-data nao instalada."
        ) from exc

    property_id = str(getattr(settings, "ga4_property_id", "")).strip()
    property_id = property_id.removeprefix("properties/")
    if not property_id:
        raise ValueError("GA4_PROPERTY_ID nao foi configurado.")

    credentials_path = getattr(settings, "ga4_credentials_path", None)
    if credentials_path:
        path = Path(credentials_path)
        if not path.exists():
            raise FileNotFoundError(
                f"Credencial do Google Analytics nao encontrada: {path}"
            )
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            str(path),
            scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        )
        client = BetaAnalyticsDataClient(credentials=credentials)
    else:
        client = BetaAnalyticsDataClient()

    date_range = DateRange(
        start_date=f"{max(0, days - 1)}daysAgo",
        end_date="today",
    )

    def report(
        metric_names: list[str],
        dimension_name: str | None = None,
        limit: int = 100,
    ) -> Any:
        dimensions = (
            [Dimension(name=dimension_name)]
            if dimension_name
            else []
        )
        return client.run_report(
            RunReportRequest(
                property=f"properties/{property_id}",
                date_ranges=[date_range],
                dimensions=dimensions,
                metrics=[Metric(name=name) for name in metric_names],
                limit=limit,
            )
        )

    summary = report(["screenPageViews", "sessions", "totalUsers"], limit=1)
    pageviews, visits, visitors = _ga4_summary_values(summary)
    top_pages = _ga4_pairs(
        report(["screenPageViews"], "pagePath", 50),
        limit=8,
    )
    top_referrers = _ga4_pairs(
        report(["sessions"], "sessionSource", 50),
        limit=6,
    )
    top_countries = _ga4_pairs(
        report(["totalUsers"], "country", 50),
        limit=6,
    )
    device_types = dict(
        _ga4_pairs(
            report(["totalUsers"], "deviceCategory", 50),
            limit=6,
        )
    )
    browsers = dict(
        _ga4_pairs(
            report(["totalUsers"], "browser", 50),
            limit=6,
        )
    )
    daily_values = _ga4_pairs(
        report(["screenPageViews"], "date", max(8, days + 1)),
        limit=max(8, days + 1),
        sort_by_count=False,
    )
    daily = {
        _ga4_date(value): count
        for value, count in daily_values
        if _ga4_date(value)
    }
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=max(0, days - 1))
    return TrafficSnapshot(
        available=True,
        source=f"Google Analytics 4 (property {property_id})",
        period_start=start.date().isoformat(),
        period_end=now.date().isoformat(),
        pageviews=pageviews,
        visits=visits,
        unique_visitors=visitors,
        top_pages=top_pages,
        top_referrers=top_referrers,
        top_countries=top_countries,
        device_types=device_types,
        browsers=browsers,
        daily_pageviews=dict(sorted(daily.items())),
        records_processed=pageviews,
        native_sessions=True,
        source_kind="ga4",
        data_note=(
            "Dados reais do GA4. Status HTTP, monitor, crawlers e outros "
            "agentes automatizados nao sao expostos pela Data API."
        ),
    )


def _ga4_summary_values(response: Any) -> tuple[int, int, int]:
    rows = list(getattr(response, "rows", []) or [])
    if not rows:
        return 0, 0, 0
    values = list(getattr(rows[0], "metric_values", []) or [])
    parsed = [_ga4_int(getattr(item, "value", 0)) for item in values[:3]]
    parsed.extend([0] * (3 - len(parsed)))
    return parsed[0], parsed[1], parsed[2]


def _ga4_pairs(
    response: Any,
    *,
    limit: int,
    sort_by_count: bool = True,
) -> list[tuple[str, int]]:
    values: list[tuple[str, int]] = []
    for row in list(getattr(response, "rows", []) or []):
        dimensions = list(getattr(row, "dimension_values", []) or [])
        metrics = list(getattr(row, "metric_values", []) or [])
        if not dimensions or not metrics:
            continue
        label = str(getattr(dimensions[0], "value", "")).strip()
        if not label or label == "(not set)":
            continue
        values.append((label, _ga4_int(getattr(metrics[0], "value", 0))))
    if sort_by_count:
        values.sort(key=lambda item: (-item[1], item[0].casefold()))
    return values[:limit]


def _ga4_int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _ga4_date(value: str) -> str:
    if len(value) != 8 or not value.isdigit():
        return ""
    return f"{value[:4]}-{value[4:6]}-{value[6:]}"


def _ensure_log_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def _append_ingest_payload(path: Path, text: str) -> None:
    _ensure_log_file(path)
    stripped = text.strip()
    if not stripped:
        return
    lines: list[str]
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    else:
        items = payload if isinstance(payload, list) else [payload]
        lines = [
            json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            for item in items
        ]
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(line)
            handle.write("\n")


def _parse_lines(lines: Iterable[str]) -> Iterable[ParsedTrafficEvent]:
    buffered = list(lines)
    combined = "".join(buffered).strip()
    if combined.startswith("[") and combined.endswith("]"):
        try:
            payload = json.loads(combined)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, list):
            for item in payload:
                event = _parse_json_event(item)
                if event:
                    yield event
            return

    for line in buffered:
        value = line.strip()
        if not value:
            continue
        if value.startswith(("{", "[")):
            try:
                payload = json.loads(value)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, list):
                for item in payload:
                    event = _parse_json_event(item)
                    if event:
                        yield event
                continue
            if isinstance(payload, dict):
                event = _parse_json_event(payload)
                if event:
                    yield event
                continue
        event = _parse_combined_event(value)
        if event:
            yield event


def _parse_json_event(data: Any) -> ParsedTrafficEvent | None:
    if not isinstance(data, dict):
        return None
    if str(data.get("schema", "")).startswith("vercel.analytics."):
        return _parse_vercel_analytics_event(data)
    proxy = data.get("proxy")
    proxy = proxy if isinstance(proxy, dict) else {}
    method = str(proxy.get("method") or data.get("method") or "").upper()
    path = str(proxy.get("path") or data.get("path") or "")
    status_value = proxy.get("statusCode", data.get("statusCode"))
    if not method or not path or status_value is None:
        return None
    try:
        status = int(status_value)
    except (TypeError, ValueError):
        return None
    timestamp = _unix_timestamp(proxy.get("timestamp", data.get("timestamp")))
    if not timestamp:
        return None
    user_agent = proxy.get("userAgent", data.get("userAgent", ""))
    if isinstance(user_agent, list):
        user_agent = " ".join(str(value) for value in user_agent)
    return ParsedTrafficEvent(
        timestamp=timestamp,
        method=method,
        path=path,
        status=status,
        client_ip=str(proxy.get("clientIp") or data.get("clientIp") or "unknown"),
        user_agent=str(user_agent or ""),
        referer=str(proxy.get("referer") or data.get("referer") or ""),
        host=str(proxy.get("host") or data.get("host") or ""),
        source_id=str(data.get("id") or data.get("requestId") or ""),
    )


def _parse_vercel_analytics_event(
    data: dict[str, Any],
) -> ParsedTrafficEvent | None:
    if data.get("eventType") != "pageview":
        return None
    timestamp = _unix_timestamp(data.get("timestamp"))
    path = str(data.get("path") or "")
    if not timestamp or not path:
        return None
    origin = str(data.get("origin") or "")
    origin_host = urlparse(origin).hostname or ""
    device_id = str(data.get("deviceId") or "")
    session_id = str(data.get("sessionId") or "")
    source_id = hashlib.sha256(
        (
            f"{data.get('projectId', '')}|{timestamp.isoformat()}|"
            f"{device_id}|{session_id}|{path}"
        ).encode("utf-8")
    ).hexdigest()
    return ParsedTrafficEvent(
        timestamp=timestamp,
        method="GET",
        path=path,
        status=200,
        client_ip="",
        user_agent=str(data.get("clientName") or ""),
        referer=str(data.get("referrer") or ""),
        host=origin_host,
        source_id=source_id,
        visitor_id=device_id,
        session_id=session_id,
        country=str(data.get("country") or ""),
        device_type=str(data.get("deviceType") or ""),
        browser=str(data.get("clientName") or ""),
    )


def _parse_combined_event(line: str) -> ParsedTrafficEvent | None:
    match = COMBINED_LOG_RE.match(line)
    if not match:
        return None
    try:
        timestamp = datetime.strptime(
            match.group("timestamp"),
            "%d/%b/%Y:%H:%M:%S %z",
        )
        status = int(match.group("status"))
    except (TypeError, ValueError):
        return None
    source_id = hashlib.sha256(line.encode("utf-8")).hexdigest()
    return ParsedTrafficEvent(
        timestamp=timestamp,
        method=match.group("method"),
        path=match.group("path"),
        status=status,
        client_ip=match.group("ip"),
        user_agent=match.group("user_agent") or "",
        referer=match.group("referer") or "",
        host="",
        source_id=source_id,
        deduplicate=False,
    )


def _unix_timestamp(value: Any) -> datetime | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 10_000_000_000:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_iso_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _normalize_path(value: str) -> str:
    parsed = urlparse(value)
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return path


def _is_asset(path: str) -> bool:
    lowered = path.lower()
    if lowered.startswith(("/assets/", "/_next/", "/favicon")):
        return True
    suffix = Path(lowered).suffix
    return suffix in ASSET_EXTENSIONS


def _is_bot(user_agent: str) -> bool:
    return _traffic_kind(user_agent) != "user"


def _traffic_kind(user_agent: str) -> str:
    lowered = user_agent.lower()
    if "discord-site-sentinel" in lowered or "jarvis-site-monitor" in lowered:
        return "monitor"
    if any(
        marker in lowered
        for marker in (
            "crawler",
            "spider",
            "slurp",
            "googlebot",
            "bingbot",
            "duckduckbot",
            "yandexbot",
            "baiduspider",
        )
    ):
        return "crawler"
    if any(marker in lowered for marker in BOT_MARKERS):
        return "automated"
    return "user"


def _hash_visitor(salt: str, client_ip: str, user_agent: str) -> str:
    return hashlib.sha256(
        f"{salt}|{client_ip}|{user_agent}".encode("utf-8")
    ).hexdigest()[:24]


def _hash_value(salt: str, value: str) -> str:
    return hashlib.sha256(f"{salt}|{value}".encode("utf-8")).hexdigest()[:24]


def _referrer_domain(value: str, own_hosts: set[str]) -> str:
    if not value or value == "-":
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.hostname or "").lower()
    if not host or host in own_hosts:
        return ""
    return host.removeprefix("www.")


def _count_sessions(events: list[dict[str, Any]], session_minutes: int) -> int:
    provided_sessions = {
        str(event.get("session"))
        for event in events
        if event.get("session")
    }
    grouped: dict[str, list[datetime]] = defaultdict(list)
    for event in events:
        if event.get("session"):
            continue
        visitor = str(event.get("visitor", ""))
        timestamp = _parse_iso_timestamp(str(event.get("timestamp", "")))
        if visitor and timestamp:
            grouped[visitor].append(timestamp)
    gap = timedelta(minutes=session_minutes)
    sessions = len(provided_sessions)
    for timestamps in grouped.values():
        timestamps.sort()
        previous: datetime | None = None
        for timestamp in timestamps:
            if previous is None or timestamp - previous > gap:
                sessions += 1
            previous = timestamp
    return sessions
