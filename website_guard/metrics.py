from __future__ import annotations

import time
from dataclasses import dataclass, field
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from .state import StateStore


@dataclass(frozen=True)
class RequestObservation:
    category: str
    url: str
    status_code: int | None
    response_ms: int | None
    error: str | None = None
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "url": self.url,
            "status_code": self.status_code,
            "response_ms": self.response_ms,
            "error": self.error,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RequestObservation":
        return cls(
            category=str(data.get("category", "request")),
            url=str(data.get("url", "")),
            status_code=data.get("status_code"),
            response_ms=data.get("response_ms"),
            error=data.get("error"),
            checked_at=str(
                data.get(
                    "checked_at",
                    datetime.now(timezone.utc).isoformat(),
                )
            ),
        )


@dataclass(frozen=True)
class MetricsSnapshot:
    started_at: str
    generated_at: str
    uptime_seconds: int
    total_requests: int
    average_response_ms: float
    minimum_response_ms: int | None
    maximum_response_ms: int | None
    status_counts: dict[str, int]
    errors_detected: int
    events_processed: int
    changes_detected: int
    feedbacks_detected: int
    checks_completed: int
    recent_requests: list[dict[str, Any]]


@dataclass(frozen=True)
class WeeklyMetricsSnapshot:
    period_start: str
    period_end: str
    total_requests: int
    average_response_ms: float
    minimum_response_ms: int | None
    maximum_response_ms: int | None
    status_counts: dict[str, int]
    category_counts: dict[str, int]
    errors_detected: int
    events_processed: int
    changes_detected: int
    feedbacks_detected: int
    site_checks: int
    site_successes: int
    availability_percent: float
    daily: dict[str, dict[str, Any]]


class MetricsRecorder:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    async def ensure_started(self) -> None:
        existing = await self.state.get("monitoring_started_at")
        if not existing:
            await self.state.set(
                "monitoring_started_at",
                datetime.now(timezone.utc).isoformat(),
            )

    async def record_request(self, observation: RequestObservation) -> None:
        def mutate(metrics: dict[str, Any]) -> None:
            metrics["total_requests"] = int(metrics.get("total_requests", 0)) + 1
            metrics["checks_completed"] = int(metrics.get("checks_completed", 0)) + 1
            if observation.response_ms is not None:
                response_ms = int(observation.response_ms)
                metrics["response_time_total_ms"] = (
                    int(metrics.get("response_time_total_ms", 0)) + response_ms
                )
                current_min = metrics.get("minimum_response_ms")
                current_max = metrics.get("maximum_response_ms")
                metrics["minimum_response_ms"] = (
                    response_ms
                    if current_min is None
                    else min(int(current_min), response_ms)
                )
                metrics["maximum_response_ms"] = (
                    response_ms
                    if current_max is None
                    else max(int(current_max), response_ms)
                )
                metrics["timed_requests"] = int(metrics.get("timed_requests", 0)) + 1

            status_key = (
                str(observation.status_code)
                if observation.status_code is not None
                else "sem_resposta"
            )
            statuses = metrics.setdefault("status_counts", {})
            statuses[status_key] = int(statuses.get(status_key, 0)) + 1
            if observation.error or (
                observation.status_code is not None and observation.status_code >= 400
            ):
                metrics["errors_detected"] = int(metrics.get("errors_detected", 0)) + 1

            recent = metrics.setdefault("recent_requests", [])
            recent.append(observation.to_dict())
            del recent[:-25]
            day = _observation_day(observation.checked_at)
            daily = metrics.setdefault("daily", {})
            bucket = daily.setdefault(day, _empty_daily_bucket())
            bucket["requests"] = int(bucket.get("requests", 0)) + 1
            categories = bucket.setdefault("categories", {})
            categories[observation.category] = (
                int(categories.get(observation.category, 0)) + 1
            )
            daily_statuses = bucket.setdefault("status_counts", {})
            daily_statuses[status_key] = int(daily_statuses.get(status_key, 0)) + 1
            if observation.response_ms is not None:
                response_ms = int(observation.response_ms)
                bucket["response_time_total_ms"] = (
                    int(bucket.get("response_time_total_ms", 0)) + response_ms
                )
                bucket["timed_requests"] = int(bucket.get("timed_requests", 0)) + 1
                daily_min = bucket.get("minimum_response_ms")
                daily_max = bucket.get("maximum_response_ms")
                bucket["minimum_response_ms"] = (
                    response_ms
                    if daily_min is None
                    else min(int(daily_min), response_ms)
                )
                bucket["maximum_response_ms"] = (
                    response_ms
                    if daily_max is None
                    else max(int(daily_max), response_ms)
                )
            request_failed = bool(
                observation.error
                or (
                    observation.status_code is not None
                    and observation.status_code >= 400
                )
            )
            if request_failed:
                bucket["errors"] = int(bucket.get("errors", 0)) + 1
            if observation.category == "site":
                bucket["site_checks"] = int(bucket.get("site_checks", 0)) + 1
                if (
                    not observation.error
                    and observation.status_code is not None
                    and observation.status_code < 400
                ):
                    bucket["site_successes"] = int(bucket.get("site_successes", 0)) + 1
            _trim_daily(daily)

        await self.state.mutate_section("metrics", mutate)

    async def add_counts(
        self,
        *,
        events: int = 0,
        changes: int = 0,
        feedbacks: int = 0,
        errors: int = 0,
    ) -> None:
        def mutate(metrics: dict[str, Any]) -> None:
            metrics["events_processed"] = (
                int(metrics.get("events_processed", 0)) + events
            )
            metrics["changes_detected"] = (
                int(metrics.get("changes_detected", 0)) + changes
            )
            metrics["feedbacks_detected"] = (
                int(metrics.get("feedbacks_detected", 0)) + feedbacks
            )
            metrics["errors_detected"] = int(metrics.get("errors_detected", 0)) + errors
            today = datetime.now(timezone.utc).date().isoformat()
            daily = metrics.setdefault("daily", {})
            bucket = daily.setdefault(today, _empty_daily_bucket())
            bucket["events"] = int(bucket.get("events", 0)) + events
            bucket["changes"] = int(bucket.get("changes", 0)) + changes
            bucket["feedbacks"] = int(bucket.get("feedbacks", 0)) + feedbacks
            bucket["errors"] = int(bucket.get("errors", 0)) + errors
            _trim_daily(daily)

        await self.state.mutate_section("metrics", mutate)

    async def snapshot(self) -> MetricsSnapshot:
        await self.ensure_started()
        metrics = await self.state.get_section("metrics")
        started_at = str(await self.state.get("monitoring_started_at"))
        try:
            started = datetime.fromisoformat(started_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
        except ValueError:
            started = datetime.now(timezone.utc)
            started_at = started.isoformat()

        now = datetime.now(timezone.utc)
        total_requests = int(metrics.get("total_requests", 0))
        timed_requests = int(metrics.get("timed_requests", 0))
        total_response = int(metrics.get("response_time_total_ms", 0))
        average = total_response / timed_requests if timed_requests else 0.0
        return MetricsSnapshot(
            started_at=started_at,
            generated_at=now.isoformat(),
            uptime_seconds=max(0, int(time.time() - started.timestamp())),
            total_requests=total_requests,
            average_response_ms=round(average, 1),
            minimum_response_ms=metrics.get("minimum_response_ms"),
            maximum_response_ms=metrics.get("maximum_response_ms"),
            status_counts={
                str(key): int(value)
                for key, value in dict(metrics.get("status_counts", {})).items()
            },
            errors_detected=int(metrics.get("errors_detected", 0)),
            events_processed=int(metrics.get("events_processed", 0)),
            changes_detected=int(metrics.get("changes_detected", 0)),
            feedbacks_detected=int(metrics.get("feedbacks_detected", 0)),
            checks_completed=int(metrics.get("checks_completed", 0)),
            recent_requests=list(metrics.get("recent_requests", [])),
        )

    async def weekly_snapshot(self, days: int = 7) -> WeeklyMetricsSnapshot:
        metrics = await self.state.get_section("metrics")
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=days - 1)
        daily_data = metrics.get("daily", {})
        selected: dict[str, dict[str, Any]] = {}
        statuses: Counter[str] = Counter()
        categories: Counter[str] = Counter()
        total_requests = 0
        timed_requests = 0
        total_response = 0
        minimum: int | None = None
        maximum: int | None = None
        errors = 0
        events = 0
        changes = 0
        feedbacks = 0
        site_checks = 0
        site_successes = 0
        if isinstance(daily_data, dict):
            for day, bucket in daily_data.items():
                if day < start.isoformat() or day > end.isoformat():
                    continue
                if not isinstance(bucket, dict):
                    continue
                selected[day] = bucket
                total_requests += int(bucket.get("requests", 0))
                timed_requests += int(bucket.get("timed_requests", 0))
                total_response += int(bucket.get("response_time_total_ms", 0))
                daily_min = bucket.get("minimum_response_ms")
                daily_max = bucket.get("maximum_response_ms")
                if daily_min is not None:
                    minimum = (
                        int(daily_min)
                        if minimum is None
                        else min(minimum, int(daily_min))
                    )
                if daily_max is not None:
                    maximum = (
                        int(daily_max)
                        if maximum is None
                        else max(maximum, int(daily_max))
                    )
                errors += int(bucket.get("errors", 0))
                events += int(bucket.get("events", 0))
                changes += int(bucket.get("changes", 0))
                feedbacks += int(bucket.get("feedbacks", 0))
                site_checks += int(bucket.get("site_checks", 0))
                site_successes += int(bucket.get("site_successes", 0))
                statuses.update(
                    {
                        str(key): int(value)
                        for key, value in dict(bucket.get("status_counts", {})).items()
                    }
                )
                categories.update(
                    {
                        str(key): int(value)
                        for key, value in dict(bucket.get("categories", {})).items()
                    }
                )

        availability = (site_successes / site_checks) * 100 if site_checks else 100.0
        return WeeklyMetricsSnapshot(
            period_start=start.isoformat(),
            period_end=end.isoformat(),
            total_requests=total_requests,
            average_response_ms=round(
                total_response / timed_requests if timed_requests else 0.0,
                1,
            ),
            minimum_response_ms=minimum,
            maximum_response_ms=maximum,
            status_counts=dict(statuses),
            category_counts=dict(categories),
            errors_detected=errors,
            events_processed=events,
            changes_detected=changes,
            feedbacks_detected=feedbacks,
            site_checks=site_checks,
            site_successes=site_successes,
            availability_percent=round(availability, 2),
            daily=selected,
        )


def _observation_day(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.date().isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).date().isoformat()


def _empty_daily_bucket() -> dict[str, Any]:
    return {
        "requests": 0,
        "timed_requests": 0,
        "response_time_total_ms": 0,
        "minimum_response_ms": None,
        "maximum_response_ms": None,
        "status_counts": {},
        "categories": {},
        "errors": 0,
        "events": 0,
        "changes": 0,
        "feedbacks": 0,
        "site_checks": 0,
        "site_successes": 0,
    }


def _trim_daily(daily: dict[str, Any]) -> None:
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=120)).isoformat()
    for day in list(daily):
        if day < cutoff:
            del daily[day]
