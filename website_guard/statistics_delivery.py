from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .identity import OWNER_DISCORD_ID
from .state import StateStore

STATISTICS_SCHEDULE_SECTION = "statistics_delivery_schedules"
STATISTICS_DELIVERY_HISTORY = "statistics_delivery_history"
STATISTICS_TIMEZONE = ZoneInfo("America/Sao_Paulo")
STATISTICS_TYPES = {
    "site",
    "servidor",
    "jarvis",
    "perfil",
    "ranking",
    "boosts",
    "conhecimentos",
}
FREQUENCIES = {"daily", "weekly", "monthly"}


@dataclass(frozen=True)
class StatisticsSchedule:
    key: str
    guild_id: int
    statistics_type: str
    channel_id: int
    target_user_id: int | None
    frequency: str
    hour: int
    minute: int
    weekday: int
    month_day: int
    period_days: int
    enabled: bool
    next_run_at: float
    last_run_at: float
    last_status: str
    last_message_id: int
    created_at: float
    updated_at: float
    created_by: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StatisticsSchedule":
        return cls(
            key=str(value.get("key", "")),
            guild_id=int(value.get("guild_id", 0)),
            statistics_type=_statistics_type(value.get("statistics_type")),
            channel_id=int(value.get("channel_id", 0)),
            target_user_id=_optional_int(value.get("target_user_id")),
            frequency=_frequency(value.get("frequency")),
            hour=_bounded_int(value.get("hour"), 0, 23, 9),
            minute=_bounded_int(value.get("minute"), 0, 59, 0),
            weekday=_bounded_int(value.get("weekday"), 0, 6, 0),
            month_day=_bounded_int(value.get("month_day"), 1, 28, 1),
            period_days=_period_days(value.get("period_days")),
            enabled=bool(value.get("enabled", True)),
            next_run_at=float(value.get("next_run_at", 0)),
            last_run_at=float(value.get("last_run_at", 0)),
            last_status=str(value.get("last_status", "pending")),
            last_message_id=int(value.get("last_message_id", 0)),
            created_at=float(value.get("created_at", 0)),
            updated_at=float(value.get("updated_at", 0)),
            created_by=int(value.get("created_by", OWNER_DISCORD_ID)),
        )


class StatisticsDeliveryStore:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    async def configure(
        self,
        *,
        guild_id: int,
        statistics_type: str,
        channel_id: int,
        created_by: int,
        frequency: str = "daily",
        hour: int = 9,
        minute: int = 0,
        weekday: int = 0,
        month_day: int = 1,
        period_days: int = 7,
        target_user_id: int | None = None,
    ) -> StatisticsSchedule:
        if created_by != OWNER_DISCORD_ID:
            raise PermissionError(
                "Somente o proprietário pode configurar estatísticas automáticas."
            )
        selected_type = _statistics_type(statistics_type)
        if selected_type not in STATISTICS_TYPES:
            raise ValueError("Tipo de estatística inválido.")
        if channel_id <= 0:
            raise ValueError("Canal de destino inválido.")
        selected_frequency = _frequency(frequency)
        now = time.time()
        key = self.key(guild_id, selected_type)
        current = (await self.state.get_section(STATISTICS_SCHEDULE_SECTION)).get(
            key
        )
        created_at = (
            float(current.get("created_at", now))
            if isinstance(current, dict)
            else now
        )
        schedule_values = {
            "frequency": selected_frequency,
            "hour": _bounded_int(hour, 0, 23, 9),
            "minute": _bounded_int(minute, 0, 59, 0),
            "weekday": _bounded_int(weekday, 0, 6, 0),
            "month_day": _bounded_int(month_day, 1, 28, 1),
        }
        record = StatisticsSchedule(
            key=key,
            guild_id=guild_id,
            statistics_type=selected_type,
            channel_id=channel_id,
            target_user_id=target_user_id,
            period_days=_period_days(period_days),
            enabled=True,
            next_run_at=_next_run(schedule_values, now=now),
            last_run_at=(
                float(current.get("last_run_at", 0))
                if isinstance(current, dict)
                else 0
            ),
            last_status="scheduled",
            last_message_id=(
                int(current.get("last_message_id", 0))
                if isinstance(current, dict)
                else 0
            ),
            created_at=created_at,
            updated_at=now,
            created_by=created_by,
            **schedule_values,
        )
        await self.state.update_section(
            STATISTICS_SCHEDULE_SECTION,
            key,
            record.to_dict(),
        )
        return record

    async def remove(
        self,
        guild_id: int,
        statistics_type: str,
        *,
        requested_by: int,
    ) -> bool:
        if requested_by != OWNER_DISCORD_ID:
            raise PermissionError(
                "Somente o proprietário pode remover agendas estatísticas."
            )
        key = self.key(guild_id, statistics_type)
        removed = False

        def mutate(records: dict[str, Any]) -> None:
            nonlocal removed
            removed = records.pop(key, None) is not None

        await self.state.mutate_section(STATISTICS_SCHEDULE_SECTION, mutate)
        return removed

    async def schedules(
        self,
        guild_id: int | None = None,
    ) -> list[StatisticsSchedule]:
        records = await self.state.get_section(STATISTICS_SCHEDULE_SECTION)
        result: list[StatisticsSchedule] = []
        for value in records.values():
            if not isinstance(value, dict):
                continue
            try:
                schedule = StatisticsSchedule.from_dict(value)
            except (TypeError, ValueError):
                continue
            if guild_id is not None and schedule.guild_id != guild_id:
                continue
            result.append(schedule)
        result.sort(key=lambda item: (item.guild_id, item.statistics_type))
        return result

    async def due(self, *, limit: int = 3) -> list[StatisticsSchedule]:
        now = time.time()
        due = [
            schedule
            for schedule in await self.schedules()
            if schedule.enabled and schedule.next_run_at <= now
        ]
        due.sort(key=lambda item: item.next_run_at)
        return due[: max(1, limit)]

    async def mark_result(
        self,
        schedule: StatisticsSchedule,
        *,
        success: bool,
        message_id: int = 0,
        error: str = "",
    ) -> StatisticsSchedule:
        now = time.time()
        values = schedule.to_dict()
        next_run_at = (
            _next_run(values, now=now + 60)
            if success
            else now + 300
        )
        updated = StatisticsSchedule(
            **{
                **values,
                "next_run_at": next_run_at,
                "last_run_at": now,
                "last_status": "sent" if success else "failed",
                "last_message_id": message_id if success else 0,
                "updated_at": now,
            }
        )
        await self.state.update_section(
            STATISTICS_SCHEDULE_SECTION,
            schedule.key,
            updated.to_dict(),
        )
        await self._record_history(
            updated,
            success=success,
            message_id=message_id,
            error=error,
        )
        return updated

    async def _record_history(
        self,
        schedule: StatisticsSchedule,
        *,
        success: bool,
        message_id: int,
        error: str,
    ) -> None:
        now = time.time()

        def mutate(records: dict[str, Any]) -> None:
            items = records.get(schedule.key, [])
            if not isinstance(items, list):
                items = []
            items.append(
                {
                    "sent_at": now,
                    "channel_id": schedule.channel_id,
                    "message_id": message_id,
                    "status": "sent" if success else "failed",
                    "error": error[:500],
                }
            )
            records[schedule.key] = items[-90:]

        await self.state.mutate_section(STATISTICS_DELIVERY_HISTORY, mutate)

    @staticmethod
    def key(guild_id: int, statistics_type: str) -> str:
        return f"{guild_id}:{_statistics_type(statistics_type)}"


def describe_schedule(schedule: StatisticsSchedule) -> str:
    time_text = f"{schedule.hour:02d}:{schedule.minute:02d}"
    if schedule.frequency == "weekly":
        days = (
            "segunda-feira",
            "terça-feira",
            "quarta-feira",
            "quinta-feira",
            "sexta-feira",
            "sábado",
            "domingo",
        )
        frequency = f"toda {days[schedule.weekday]}"
    elif schedule.frequency == "monthly":
        frequency = f"todo dia {schedule.month_day} de cada mês"
    else:
        frequency = "todos os dias"
    return (
        f"{schedule.statistics_type}: {frequency} às {time_text} em "
        f"<#{schedule.channel_id}>"
    )


def _next_run(record: dict[str, Any], *, now: float) -> float:
    current = datetime.fromtimestamp(now, STATISTICS_TIMEZONE)
    hour = _bounded_int(record.get("hour"), 0, 23, 9)
    minute = _bounded_int(record.get("minute"), 0, 59, 0)
    frequency = _frequency(record.get("frequency"))
    candidate = current.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )
    if frequency == "daily":
        if candidate.timestamp() <= now:
            candidate += timedelta(days=1)
        return candidate.timestamp()
    if frequency == "weekly":
        weekday = _bounded_int(record.get("weekday"), 0, 6, 0)
        for offset in range(8):
            option = candidate + timedelta(days=offset)
            if option.weekday() == weekday and option.timestamp() > now:
                return option.timestamp()
        return (candidate + timedelta(days=7)).timestamp()
    month_day = _bounded_int(record.get("month_day"), 1, 28, 1)
    candidate = candidate.replace(day=month_day)
    if candidate.timestamp() <= now:
        month = candidate.month + 1
        year = candidate.year + (1 if month > 12 else 0)
        month = 1 if month > 12 else month
        candidate = candidate.replace(year=year, month=month, day=month_day)
    return candidate.timestamp()


def _statistics_type(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    aliases = {
        "server": "servidor",
        "community": "servidor",
        "comunidade": "servidor",
        "ia": "jarvis",
        "inteligencia": "jarvis",
        "inteligência": "jarvis",
        "profile": "perfil",
        "rank": "ranking",
        "boost": "boosts",
        "knowledge": "conhecimentos",
        "conhecimento": "conhecimentos",
    }
    return aliases.get(normalized, normalized)


def _frequency(value: Any) -> str:
    normalized = str(value or "daily").strip().casefold()
    aliases = {
        "diaria": "daily",
        "diária": "daily",
        "diario": "daily",
        "diário": "daily",
        "semanal": "weekly",
        "mensal": "monthly",
    }
    selected = aliases.get(normalized, normalized)
    return selected if selected in FREQUENCIES else "daily"


def _period_days(value: Any) -> int:
    parsed = _bounded_int(value, 1, 90, 7)
    return min((7, 30, 90), key=lambda item: abs(item - parsed))


def _bounded_int(
    value: Any,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
