from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from .state import StateStore


_KEY_ID_PATTERN = re.compile(r"^[a-zA-Z0-9:_-]+$")
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
_PUBLIC_METRIC_UNITS = {
    "commands_executed": "count",
    "interactions_processed": "count",
    "tickets_opened": "count",
    "tickets_completed": "count",
    "security_events": "count",
    "messages_blocked": "count",
    "automod_actions": "count",
    "analyses_completed": "count",
    "community_interactions": "count",
    "community_events": "count",
    "operation_seconds": "seconds",
}
OBSERVATORY_COMMAND_METRICS_SECTION = "public_observatory_command_metrics"
OBSERVATORY_AUTOMOD_METRICS_SECTION = "public_observatory_automod_metrics"
OBSERVATORY_OPERATION_METRICS_SECTION = "public_observatory_operation_metrics"
OBSERVATORY_SYNC_SECTION = "public_observatory_sync"


class ObservatoryConfigurationError(ValueError):
    pass


class ObservatorySyncError(RuntimeError):
    pass


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "sim", "on"}


def _as_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ObservatoryConfigurationError(f"{name} precisa ser um número inteiro.") from exc


@dataclass(frozen=True)
class ObservatoryConfig:
    enabled: bool
    base_url: str
    key_id: str
    secret: str = field(repr=False)
    interval_seconds: int = 60
    sync_interval_seconds: int = 300
    timeout_seconds: int = 15
    max_attempts: int = 3
    version: str | None = None

    @classmethod
    def from_env(cls) -> ObservatoryConfig:
        base_url = os.getenv("BELLA_OBSERVATORY_URL", "").strip().rstrip("/")
        key_id = os.getenv("BELLA_OBSERVATORY_KEY_ID", "").strip()
        secret = os.getenv("BELLA_OBSERVATORY_SECRET", "").strip()
        configured = bool(base_url and key_id and secret)
        enabled = _as_bool(os.getenv("BELLA_OBSERVATORY_ENABLED"), configured)

        if enabled:
            missing = [
                name
                for name, value in (
                    ("BELLA_OBSERVATORY_URL", base_url),
                    ("BELLA_OBSERVATORY_KEY_ID", key_id),
                    ("BELLA_OBSERVATORY_SECRET", secret),
                )
                if not value
            ]
            if missing:
                raise ObservatoryConfigurationError(
                    "Integração com o Observatório incompleta: " + ", ".join(missing)
                )
            parsed = urlparse(base_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ObservatoryConfigurationError(
                    "BELLA_OBSERVATORY_URL precisa ser uma URL HTTPS válida."
                )
            if len(key_id) > 128 or not _KEY_ID_PATTERN.fullmatch(key_id):
                raise ObservatoryConfigurationError(
                    "BELLA_OBSERVATORY_KEY_ID possui formato inválido."
                )
            if len(secret) < 32:
                raise ObservatoryConfigurationError(
                    "BELLA_OBSERVATORY_SECRET precisa ter ao menos 32 caracteres."
                )

        interval_seconds = _as_int("BELLA_OBSERVATORY_INTERVAL_SECONDS", 60)
        if not 30 <= interval_seconds < 180:
            raise ObservatoryConfigurationError(
                "BELLA_OBSERVATORY_INTERVAL_SECONDS precisa ficar entre 30 e 179."
            )
        sync_interval_seconds = _as_int("BELLA_OBSERVATORY_SYNC_INTERVAL_SECONDS", 300)
        if not 120 <= sync_interval_seconds <= 3600:
            raise ObservatoryConfigurationError(
                "BELLA_OBSERVATORY_SYNC_INTERVAL_SECONDS precisa ficar entre 120 e 3600."
            )

        return cls(
            enabled=enabled,
            base_url=base_url,
            key_id=key_id,
            secret=secret,
            interval_seconds=interval_seconds,
            sync_interval_seconds=sync_interval_seconds,
            timeout_seconds=max(3, min(_as_int("BELLA_OBSERVATORY_TIMEOUT_SECONDS", 15), 60)),
            max_attempts=max(1, min(_as_int("BELLA_OBSERVATORY_MAX_ATTEMPTS", 3), 5)),
            version=os.getenv("BELLA_VERSION", "").strip()[:64] or None,
        )

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/api/integration/v1/events"


def utc_iso(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def serialize_event(event: dict[str, Any]) -> bytes:
    return json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def signature_headers(
    *,
    key_id: str,
    secret: str,
    raw_body: bytes,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    signed_at = timestamp or str(int(time.time()))
    request_nonce = nonce or secrets.token_urlsafe(24)
    canonical = b".".join((signed_at.encode(), request_nonce.encode(), raw_body))
    signature = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Bella-Key-Id": key_id,
        "X-Bella-Timestamp": signed_at,
        "X-Bella-Nonce": request_nonce,
        "X-Bella-Signature": f"sha256={signature}",
    }


def heartbeat_event(
    latency_ms: int,
    *,
    state: str = "online",
    version: str | None = None,
    event_id: str | None = None,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    latency = max(0, min(int(latency_ms), 120_000))
    normalized_state = state if state in {"online", "away", "offline", "maintenance"} else "online"
    payload: dict[str, Any] = {
        "state": normalized_state,
        "latencyMs": latency,
        "services": [
            {
                "key": "discord",
                "label": "Bot Discord",
                "state": "maintenance" if normalized_state == "maintenance" else "operational",
                "latencyMs": latency,
            }
        ],
    }
    if version:
        payload["version"] = version[:64]
    return {
        "contractVersion": 1,
        "eventId": event_id or f"heartbeat:{uuid.uuid4()}",
        "occurredAt": utc_iso(occurred_at),
        "type": "heartbeat",
        "payload": payload,
    }


def metrics_event(
    points: dict[str, int | float],
    *,
    period_start: datetime,
    period_end: datetime,
    event_id: str,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    normalized = [
        {
            "key": key,
            "value": max(0, int(value)),
            "unit": _PUBLIC_METRIC_UNITS[key],
        }
        for key, value in sorted(points.items())
        if key in _PUBLIC_METRIC_UNITS and float(value) > 0
    ]
    if not normalized:
        raise ValueError("O evento de métricas precisa possuir ao menos um indicador positivo.")
    return {
        "contractVersion": 1,
        "eventId": event_id[:128],
        "occurredAt": utc_iso(occurred_at or period_end),
        "type": "metrics",
        "payload": {
            "mode": "delta",
            "periodStart": utc_iso(period_start),
            "periodEnd": utc_iso(period_end),
            "points": normalized,
        },
    }


def community_snapshot_event(
    *,
    members: int,
    interactions: int | None,
    events: int | None,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "communityKey": "bella-discord",
        "members": max(0, int(members)),
    }
    if interactions is not None:
        payload["interactions"] = max(0, int(interactions))
    if events is not None:
        payload["events"] = max(0, int(events))
    return _content_event("community_snapshot", payload, occurred_at=occurred_at)


def command_catalog_event(
    commands: list[dict[str, Any]],
    *,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    payload = {"replace": True, "commands": commands[:250]}
    return _content_event("command_catalog", payload, occurred_at=occurred_at)


def observatory_release_events(
    *,
    version: str | None = None,
    occurred_at: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    release_payload: dict[str, Any] = {
        "category": "release",
        "title": "Observatório público conectado à Bella",
        "description": (
            "A Bella passou a sincronizar estado operacional, métricas agregadas, "
            "comunidade e catálogo público sem publicar dados pessoais."
        ),
        "origin": "Bella Nyelle Bot",
    }
    if version:
        release_payload["version"] = version[:64]
    activity = _content_event("public_activity", release_payload, occurred_at=occurred_at)
    editorial_payload: dict[str, Any] = {
        "itemId": "observatory-public-sync",
        "kind": "release",
        "status": "published",
        "title": "Sincronização segura do Observatório",
        "summary": (
            "Integração pública com dados operacionais agregados, catálogo controlado "
            "e publicação autenticada por assinatura HMAC."
        ),
        "details": [
            "Nenhum token, contato ou conteúdo privado é enviado.",
            "Eventos repetidos usam identificadores idempotentes.",
        ],
        "category": "Infraestrutura",
    }
    if version:
        editorial_payload["version"] = version[:64]
    editorial = _content_event("editorial", editorial_payload, occurred_at=occurred_at)
    return activity, editorial


def incident_events_from_state(
    incidents: dict[str, Any],
    *,
    occurred_at: datetime | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    now = occurred_at or datetime.now(timezone.utc)
    for raw_id, value in incidents.items():
        if not isinstance(value, dict):
            continue
        alert = value.get("alert") if isinstance(value.get("alert"), dict) else {}
        title = str(alert.get("title") or "Incidente operacional").strip()[:120]
        summary = str(alert.get("description") or "Condição operacional em acompanhamento.").strip()[:600]
        if not title or not summary:
            continue
        incident_id = _identifier(str(raw_id), fallback="incident")
        active = bool(value.get("active"))
        severity = str(alert.get("severity") or "warning").casefold()
        impact = "major" if severity == "critical" else "minor" if severity == "warning" else "none"
        first_seen = _datetime_from_value(value.get("first_seen")) or now
        recovered = _datetime_from_value(value.get("recovered_at"))
        payload: dict[str, Any] = {
            "incidentId": incident_id,
            "serviceKey": _service_key(str(value.get("scope") or "bella")),
            "title": title,
            "summary": summary,
            "state": "investigating" if active else "resolved",
            "impact": impact,
            "startedAt": utc_iso(first_seen),
        }
        if not active and recovered is not None:
            payload["resolvedAt"] = utc_iso(recovered)
        result.append(
            {
                "contractVersion": 1,
                "eventId": _stable_identifier("incident", payload),
                "occurredAt": utc_iso(now),
                "type": "incident",
                "payload": payload,
            }
        )
    return result


class PublicObservatoryCollector:
    def __init__(
        self,
        state: StateStore,
        *,
        timezone_name: str = "America/Sao_Paulo",
    ) -> None:
        self.state = state
        try:
            self.timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            self.timezone = timezone.utc

    async def record_command(self, guild_id: int, occurred_at: datetime | None = None) -> None:
        await self._increment_daily(
            OBSERVATORY_COMMAND_METRICS_SECTION,
            guild_id,
            "commands_executed",
            occurred_at=occurred_at,
        )

    async def record_automod_action(self, guild_id: int, occurred_at: datetime | None = None) -> None:
        await self._increment_daily(
            OBSERVATORY_AUTOMOD_METRICS_SECTION,
            guild_id,
            "automod_actions",
            occurred_at=occurred_at,
        )

    async def record_operation_seconds(
        self,
        seconds: int | float,
        occurred_at: datetime | None = None,
    ) -> None:
        amount = max(0, min(int(seconds), 3600))
        if amount <= 0:
            return
        moment = (occurred_at or datetime.now(timezone.utc)).astimezone(self.timezone)
        day_key = moment.date().isoformat()

        def mutate(section: dict[str, Any]) -> None:
            daily = section.setdefault("daily", {})
            if not isinstance(daily, dict):
                daily = {}
                section["daily"] = daily
            bucket = daily.setdefault(day_key, {})
            if not isinstance(bucket, dict):
                bucket = {}
                daily[day_key] = bucket
            bucket["operation_seconds"] = int(bucket.get("operation_seconds", 0)) + amount
            _trim_daily(daily, moment.date() - timedelta(days=120))

        await self.state.mutate_section(OBSERVATORY_OPERATION_METRICS_SECTION, mutate)

    async def completed_daily_events(
        self,
        guild_ids: set[int],
        *,
        days: int = 30,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        snapshot = await self.state.snapshot()
        local_now = (now or datetime.now(timezone.utc)).astimezone(self.timezone)
        sync = _mapping(snapshot.get(OBSERVATORY_SYNC_SECTION))
        live_cursors = _mapping(sync.get("live_cursors"))
        result: list[dict[str, Any]] = []
        for offset in range(max(1, min(days, 120)), 0, -1):
            day = local_now.date() - timedelta(days=offset)
            if day.isoformat() in live_cursors:
                continue
            totals = self._totals_for_day(snapshot, guild_ids, day)
            if not totals:
                continue
            start, end = self._day_bounds(day)
            result.append(
                metrics_event(
                    totals,
                    period_start=start,
                    period_end=end,
                    event_id=f"metrics:daily:{day.isoformat()}:v2",
                    occurred_at=end,
                )
            )
        return result

    async def prepare_live_metrics(
        self,
        guild_ids: set[int],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        sync = await self.state.get_section(OBSERVATORY_SYNC_SECTION)
        pending = sync.get("pending_live_metrics")
        if isinstance(pending, dict) and isinstance(pending.get("event"), dict):
            return dict(pending["event"])

        snapshot = await self.state.snapshot()
        local_now = (now or datetime.now(timezone.utc)).astimezone(self.timezone)
        day = local_now.date()
        totals = self._totals_for_day(snapshot, guild_ids, day)
        cursors = sync.get("live_cursors") if isinstance(sync.get("live_cursors"), dict) else {}
        baseline = cursors.get(day.isoformat()) if isinstance(cursors, dict) else {}
        baseline = baseline if isinstance(baseline, dict) else {}
        deltas = {
            key: int(value) - int(baseline.get(key, 0))
            for key, value in totals.items()
            if int(value) > int(baseline.get(key, 0))
        }
        if not deltas:
            return None
        start, _ = self._day_bounds(day)
        event = metrics_event(
            deltas,
            period_start=start,
            period_end=local_now,
            event_id=_stable_identifier(
                "metrics-live",
                {"day": day.isoformat(), "from": baseline, "to": totals},
            ),
            occurred_at=local_now,
        )

        def mutate(section: dict[str, Any]) -> None:
            section["pending_live_metrics"] = {
                "event": event,
                "day": day.isoformat(),
                "target": totals,
            }

        await self.state.mutate_section(OBSERVATORY_SYNC_SECTION, mutate)
        return event

    async def acknowledge_live_metrics(self, event_id: str) -> None:
        def mutate(section: dict[str, Any]) -> None:
            pending = section.get("pending_live_metrics")
            if not isinstance(pending, dict):
                return
            event = pending.get("event")
            if not isinstance(event, dict) or str(event.get("eventId")) != event_id:
                return
            day_key = str(pending.get("day") or "")
            target = pending.get("target")
            if day_key and isinstance(target, dict):
                cursors = section.setdefault("live_cursors", {})
                if not isinstance(cursors, dict):
                    cursors = {}
                    section["live_cursors"] = cursors
                cursors[day_key] = {key: max(0, int(value)) for key, value in target.items()}
                cutoff = (datetime.now(self.timezone).date() - timedelta(days=120)).isoformat()
                for stored_day in list(cursors):
                    if stored_day < cutoff:
                        cursors.pop(stored_day, None)
            section.pop("pending_live_metrics", None)

        await self.state.mutate_section(OBSERVATORY_SYNC_SECTION, mutate)

    async def event_was_published(self, event_id: str) -> bool:
        section = await self.state.get_section(OBSERVATORY_SYNC_SECTION)
        published = section.get("published_event_ids")
        return isinstance(published, dict) and event_id in published

    async def mark_event_published(self, event_id: str) -> None:
        def mutate(section: dict[str, Any]) -> None:
            published = section.setdefault("published_event_ids", {})
            if not isinstance(published, dict):
                published = {}
                section["published_event_ids"] = published
            published[event_id] = utc_iso()
            if len(published) > 1000:
                ordered = sorted(published.items(), key=lambda item: str(item[1]))
                for old_id, _ in ordered[: len(published) - 1000]:
                    published.pop(old_id, None)

        await self.state.mutate_section(OBSERVATORY_SYNC_SECTION, mutate)

    async def community_totals(
        self,
        guild_ids: set[int],
        *,
        members: int,
        days: int = 7,
        now: datetime | None = None,
    ) -> dict[str, int | None]:
        snapshot = await self.state.snapshot()
        local_now = (now or datetime.now(timezone.utc)).astimezone(self.timezone)
        statistics = snapshot.get("server_message_statistics")
        statistics = statistics if isinstance(statistics, dict) else {}
        interactions = 0
        events = 0
        evidence = False
        selected_days = {
            (local_now.date() - timedelta(days=offset)).isoformat()
            for offset in range(max(1, min(days, 30)))
        }
        for guild_id in guild_ids:
            guild = statistics.get(str(guild_id))
            daily = guild.get("daily") if isinstance(guild, dict) else {}
            if not isinstance(daily, dict):
                continue
            for day_key in selected_days:
                bucket = daily.get(day_key)
                if not isinstance(bucket, dict):
                    continue
                evidence = True
                interactions += max(0, int(bucket.get("messages", 0)))
                events += max(0, int(bucket.get("new_members", 0)))
        return {
            "members": max(0, int(members)),
            "interactions": interactions if evidence else None,
            "events": events if evidence else None,
        }

    async def _increment_daily(
        self,
        section_name: str,
        guild_id: int,
        field: str,
        *,
        occurred_at: datetime | None,
    ) -> None:
        moment = (occurred_at or datetime.now(timezone.utc)).astimezone(self.timezone)
        day_key = moment.date().isoformat()

        def mutate(section: dict[str, Any]) -> None:
            guild = section.setdefault(str(guild_id), {})
            if not isinstance(guild, dict):
                guild = {}
                section[str(guild_id)] = guild
            daily = guild.setdefault("daily", {})
            if not isinstance(daily, dict):
                daily = {}
                guild["daily"] = daily
            bucket = daily.setdefault(day_key, {})
            if not isinstance(bucket, dict):
                bucket = {}
                daily[day_key] = bucket
            bucket[field] = int(bucket.get(field, 0)) + 1
            _trim_daily(daily, moment.date() - timedelta(days=120))

        await self.state.mutate_section(section_name, mutate)

    def _totals_for_day(
        self,
        snapshot: dict[str, Any],
        guild_ids: set[int],
        day: date,
    ) -> dict[str, int]:
        day_key = day.isoformat()
        totals: Counter[str] = Counter()
        statistics = _mapping(snapshot.get("server_message_statistics"))
        commands = _mapping(snapshot.get(OBSERVATORY_COMMAND_METRICS_SECTION))
        automod = _mapping(snapshot.get(OBSERVATORY_AUTOMOD_METRICS_SECTION))
        autonomous = _mapping(snapshot.get("autonomous_moderation_metrics"))
        for guild_id in guild_ids:
            guild_key = str(guild_id)
            message_bucket = _daily_bucket(statistics, guild_key, day_key)
            if message_bucket is not None:
                messages = max(0, int(message_bucket.get("messages", 0)))
                new_members = max(0, int(message_bucket.get("new_members", 0)))
                totals["interactions_processed"] += messages
                totals["community_interactions"] += messages
                totals["community_events"] += new_members
            command_bucket = _daily_bucket(commands, guild_key, day_key)
            if command_bucket is not None:
                totals["commands_executed"] += max(0, int(command_bucket.get("commands_executed", 0)))
            automod_bucket = _daily_bucket(automod, guild_key, day_key)
            if automod_bucket is not None:
                totals["automod_actions"] += max(0, int(automod_bucket.get("automod_actions", 0)))
            autonomous_bucket = _daily_bucket(autonomous, guild_key, day_key)
            if autonomous_bucket is not None:
                totals["security_events"] += max(0, int(autonomous_bucket.get("incidents", 0)))
                totals["messages_blocked"] += max(0, int(autonomous_bucket.get("messages_deleted", 0)))

        operation = _mapping(snapshot.get(OBSERVATORY_OPERATION_METRICS_SECTION))
        operation_daily = _mapping(operation.get("daily"))
        operation_bucket = operation_daily.get(day_key)
        if isinstance(operation_bucket, dict):
            totals["operation_seconds"] += max(0, int(operation_bucket.get("operation_seconds", 0)))

        tickets = _mapping(snapshot.get("smart_tickets"))
        for record in tickets.values():
            if not isinstance(record, dict) or int(record.get("guild_id", 0) or 0) not in guild_ids:
                continue
            if self._local_day(record.get("created_at")) == day:
                totals["tickets_opened"] += 1
            if self._local_day(record.get("closed_at")) == day:
                totals["tickets_completed"] += 1

        scans = _mapping(snapshot.get("security_scans"))
        for record in scans.values():
            if not isinstance(record, dict) or int(record.get("guild_id", 0) or 0) not in guild_ids:
                continue
            if str(record.get("status") or "").upper() not in {"COMPLETED", "PARTIAL"}:
                continue
            if self._local_day(record.get("finished_at")) == day:
                totals["analyses_completed"] += 1
        return {key: int(value) for key, value in totals.items() if int(value) > 0}

    def _local_day(self, value: Any) -> date | None:
        parsed = _datetime_from_value(value)
        return parsed.astimezone(self.timezone).date() if parsed else None

    def _day_bounds(self, value: date) -> tuple[datetime, datetime]:
        start = datetime.combine(value, datetime_time.min, tzinfo=self.timezone)
        return start, start + timedelta(days=1)


def _content_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    occurred_at: datetime | None,
) -> dict[str, Any]:
    return {
        "contractVersion": 1,
        "eventId": _stable_identifier(event_type, payload),
        "occurredAt": utc_iso(occurred_at),
        "type": event_type,
        "payload": payload,
    }


def _stable_identifier(prefix: str, payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]
    safe_prefix = re.sub(r"[^a-zA-Z0-9:_-]", "-", prefix).strip("-") or "event"
    return f"{safe_prefix}:{digest}"[:128]


def _identifier(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9:_-]", "-", value).strip("-:_")
    return (normalized or fallback)[:128]


def _service_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]", "-", value.casefold()).strip("-_")
    return (normalized or "bella")[:64]


def _datetime_from_value(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _daily_bucket(section: dict[str, Any], guild_key: str, day_key: str) -> dict[str, Any] | None:
    guild = section.get(guild_key)
    daily = guild.get("daily") if isinstance(guild, dict) else None
    bucket = daily.get(day_key) if isinstance(daily, dict) else None
    return bucket if isinstance(bucket, dict) else None


def _trim_daily(daily: dict[str, Any], cutoff: date) -> None:
    cutoff_key = cutoff.isoformat()
    for day_key in list(daily):
        if str(day_key) < cutoff_key:
            daily.pop(day_key, None)


class ObservatoryClient:
    def __init__(
        self,
        config: ObservatoryConfig,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self._sleep = sleep

    async def send_event(
        self,
        session: aiohttp.ClientSession,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.config.enabled:
            raise ObservatoryConfigurationError("A integração com o Observatório está desativada.")

        raw_body = serialize_event(event)
        last_error: Exception | None = None
        for attempt in range(self.config.max_attempts):
            headers = signature_headers(
                key_id=self.config.key_id,
                secret=self.config.secret,
                raw_body=raw_body,
            )
            try:
                async with session.post(
                    self.config.endpoint,
                    data=raw_body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds),
                ) as response:
                    response_text = await response.text()
                    if response.status in {200, 202}:
                        if not response_text.strip():
                            return {"accepted": True}
                        try:
                            parsed = json.loads(response_text)
                        except json.JSONDecodeError:
                            return {"accepted": True}
                        return parsed if isinstance(parsed, dict) else {"accepted": True}
                    if response.status not in _TRANSIENT_STATUS_CODES:
                        raise ObservatorySyncError(
                            f"O Observatório recusou a publicação (HTTP {response.status})."
                        )
                    last_error = ObservatorySyncError(
                        f"Falha temporária no Observatório (HTTP {response.status})."
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc

            if attempt + 1 < self.config.max_attempts:
                await self._sleep(min(2**attempt, 8))

        raise ObservatorySyncError(
            "Não foi possível publicar no Observatório após novas tentativas."
        ) from last_error

    async def send_heartbeat(
        self,
        session: aiohttp.ClientSession,
        *,
        latency_ms: int,
        state: str = "online",
    ) -> dict[str, Any]:
        return await self.send_event(
            session,
            heartbeat_event(latency_ms, state=state, version=self.config.version),
        )
