from __future__ import annotations

import asyncio
import calendar
import io
import json
import logging
import math
import re
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from PIL import Image, ImageDraw, ImageFont

from src.ai.groq_service import GroqService

from .central_logs import CentralLogService, PanelChannelDefinition

LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "America/Recife"
ANALYTICS_VERSION = "1.0.0"
REPORT_SCHEMA_VERSION = 1
PARTNER_ANALYTICS_COLOR = 0xC27C2B
PARTNER_ANALYTICS_CHANNEL = PanelChannelDefinition(
    "partner_analytics",
    "Partner Analytics",
    "parceiros-analytics",
    "📈",
)
REFERRAL_CONVERTED_STATUSES = {"closed", "completed"}
REFERRAL_QUALIFIED_STATUSES = {"accepted", "negotiation", "closed", "completed"}
REFERRAL_LOST_STATUSES = {"rejected"}
WEEKDAY_LABELS = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)


@dataclass(frozen=True)
class AnalyticsPeriod:
    start: datetime
    end: datetime
    label: str
    partial: bool = False
    timezone_name: str = DEFAULT_TIMEZONE

    @property
    def days(self) -> int:
        return max(1, (self.end.date() - self.start.date()).days)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "label": self.label,
            "partial": self.partial,
            "timezone": self.timezone_name,
        }


@dataclass
class PartnerAnalyticsConfig:
    guild_id: int
    analytics_channel_id: int | None = None
    panel_message_id: int | None = None
    weekly_enabled: bool = False
    weekly_weekday: int = 0
    weekly_time: str = "09:00"
    weekly_timezone: str = DEFAULT_TIMEZONE
    weekly_channel_id: int | None = None
    monthly_enabled: bool = False
    monthly_day: int = 1
    monthly_time: str = "09:00"
    monthly_timezone: str = DEFAULT_TIMEZONE
    monthly_channel_id: int | None = None
    monthly_short_month_policy: str = "last_day"
    annual_enabled: bool = False
    annual_month: int = 1
    annual_day: int = 1
    annual_time: str = "09:00"
    annual_timezone: str = DEFAULT_TIMEZONE
    annual_channel_id: int | None = None
    annual_short_month_policy: str = "last_day"
    updated_at: str = ""

    @classmethod
    def from_row(cls, guild_id: int, row: sqlite3.Row | None) -> "PartnerAnalyticsConfig":
        if row is None:
            return cls(guild_id=guild_id)
        payload = dict(row)
        payload["weekly_enabled"] = bool(payload.get("weekly_enabled"))
        payload["monthly_enabled"] = bool(payload.get("monthly_enabled"))
        payload["annual_enabled"] = bool(payload.get("annual_enabled"))
        return cls(**payload)


def percentage_change(current: float | int | None, previous: float | int | None) -> float | None:
    if current is None or previous is None:
        return None
    current_value = float(current)
    previous_value = float(previous)
    if previous_value == 0:
        return 0.0 if current_value == 0 else None
    return ((current_value - previous_value) / abs(previous_value)) * 100.0


def previous_equivalent_period(period: AnalyticsPeriod) -> AnalyticsPeriod:
    duration = period.end - period.start
    start = period.start - duration
    end = period.start
    return AnalyticsPeriod(
        start=start,
        end=end,
        label=f"{start:%d/%m/%Y} a {(end - timedelta(microseconds=1)):%d/%m/%Y}",
        partial=False,
        timezone_name=period.timezone_name,
    )


def previous_report_period(period: AnalyticsPeriod, report_type: str) -> AnalyticsPeriod:
    if report_type == "annual":
        zone = _timezone(period.timezone_name)
        local_start = period.start.astimezone(zone)
        previous_start_date = date(local_start.year - 1, 1, 1)
        previous_end_date = date(local_start.year, 1, 1)
        if period.partial:
            elapsed = period.end - period.start
            previous_start = datetime.combine(previous_start_date, time.min, zone)
            full_previous_end = datetime.combine(previous_end_date, time.min, zone)
            previous_end = min(previous_start + elapsed, full_previous_end)
            return AnalyticsPeriod(
                start=previous_start.astimezone(timezone.utc),
                end=previous_end.astimezone(timezone.utc),
                label=(
                    f"{previous_start_date:%d/%m/%Y} a "
                    f"{(previous_end - timedelta(microseconds=1)).astimezone(zone).date():%d/%m/%Y}"
                ),
                partial=True,
                timezone_name=period.timezone_name,
            )
        previous = _period_from_dates(
            previous_start_date,
            previous_end_date,
            period.timezone_name,
            partial=False,
        )
        return AnalyticsPeriod(
            start=previous.start,
            end=previous.end,
            label=str(previous_start_date.year),
            partial=False,
            timezone_name=period.timezone_name,
        )
    if report_type != "monthly":
        return previous_equivalent_period(period)
    zone = _timezone(period.timezone_name)
    local_start = period.start.astimezone(zone)
    current_month_start = local_start.date().replace(day=1)
    previous_month_end = current_month_start
    previous_month_start = (current_month_start - timedelta(days=1)).replace(day=1)
    if period.partial:
        elapsed = period.end - period.start
        previous_start = datetime.combine(previous_month_start, time.min, zone)
        full_previous_end = datetime.combine(previous_month_end, time.min, zone)
        previous_end = min(previous_start + elapsed, full_previous_end)
        return AnalyticsPeriod(
            start=previous_start.astimezone(timezone.utc),
            end=previous_end.astimezone(timezone.utc),
            label=(
                f"{previous_month_start:%d/%m/%Y} a "
                f"{(previous_end - timedelta(microseconds=1)).astimezone(zone).date():%d/%m/%Y}"
            ),
            partial=True,
            timezone_name=period.timezone_name,
        )
    return _period_from_dates(
        previous_month_start,
        previous_month_end,
        period.timezone_name,
        partial=False,
    )


def last_complete_week(now: datetime, timezone_name: str = DEFAULT_TIMEZONE) -> AnalyticsPeriod:
    local = now.astimezone(_timezone(timezone_name))
    current_monday = local.date() - timedelta(days=local.weekday())
    start_date = current_monday - timedelta(days=7)
    end_date = current_monday
    return _period_from_dates(start_date, end_date, timezone_name, partial=False)


def current_partial_week(now: datetime, timezone_name: str = DEFAULT_TIMEZONE) -> AnalyticsPeriod:
    local = now.astimezone(_timezone(timezone_name))
    start_date = local.date() - timedelta(days=local.weekday())
    start = datetime.combine(start_date, time.min, _timezone(timezone_name)).astimezone(timezone.utc)
    return AnalyticsPeriod(
        start=start,
        end=now.astimezone(timezone.utc),
        label=f"{start_date:%d/%m/%Y} a {local.date():%d/%m/%Y}",
        partial=True,
        timezone_name=timezone_name,
    )


def last_complete_month(now: datetime, timezone_name: str = DEFAULT_TIMEZONE) -> AnalyticsPeriod:
    local = now.astimezone(_timezone(timezone_name))
    current_month = local.date().replace(day=1)
    previous_month_end = current_month - timedelta(days=1)
    start_date = previous_month_end.replace(day=1)
    period = _period_from_dates(start_date, current_month, timezone_name, partial=False)
    return AnalyticsPeriod(
        start=period.start,
        end=period.end,
        label=_month_label(start_date),
        partial=False,
        timezone_name=timezone_name,
    )


def current_partial_month(now: datetime, timezone_name: str = DEFAULT_TIMEZONE) -> AnalyticsPeriod:
    local = now.astimezone(_timezone(timezone_name))
    start_date = local.date().replace(day=1)
    start = datetime.combine(start_date, time.min, _timezone(timezone_name)).astimezone(timezone.utc)
    return AnalyticsPeriod(
        start=start,
        end=now.astimezone(timezone.utc),
        label=_month_label(start_date),
        partial=True,
        timezone_name=timezone_name,
    )


def last_complete_year(now: datetime, timezone_name: str = DEFAULT_TIMEZONE) -> AnalyticsPeriod:
    local = now.astimezone(_timezone(timezone_name))
    start_date = date(local.year - 1, 1, 1)
    end_date = date(local.year, 1, 1)
    period = _period_from_dates(start_date, end_date, timezone_name, partial=False)
    return AnalyticsPeriod(
        start=period.start,
        end=period.end,
        label=str(start_date.year),
        partial=False,
        timezone_name=timezone_name,
    )


def current_partial_year(now: datetime, timezone_name: str = DEFAULT_TIMEZONE) -> AnalyticsPeriod:
    local = now.astimezone(_timezone(timezone_name))
    start_date = date(local.year, 1, 1)
    start = datetime.combine(start_date, time.min, _timezone(timezone_name)).astimezone(timezone.utc)
    return AnalyticsPeriod(
        start=start,
        end=now.astimezone(timezone.utc),
        label=str(local.year),
        partial=True,
        timezone_name=timezone_name,
    )


def custom_period(
    start_date: date,
    end_date_inclusive: date,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> AnalyticsPeriod:
    if end_date_inclusive < start_date:
        raise ValueError("A data final não pode ser anterior à data inicial.")
    return _period_from_dates(
        start_date,
        end_date_inclusive + timedelta(days=1),
        timezone_name,
        partial=True,
    )


def effective_monthly_day(year: int, month: int, requested_day: int, policy: str) -> int | None:
    last_day = calendar.monthrange(year, month)[1]
    requested = max(1, min(31, int(requested_day)))
    if requested <= last_day:
        return requested
    return last_day if policy == "last_day" else None


def schedule_is_due(
    config: PartnerAnalyticsConfig,
    report_type: str,
    now: datetime,
) -> bool:
    if report_type == "weekly":
        if not config.weekly_enabled:
            return False
        local = now.astimezone(_timezone(config.weekly_timezone))
        return local.weekday() == config.weekly_weekday and local.time() >= _parse_time(config.weekly_time)
    if report_type == "monthly":
        if not config.monthly_enabled:
            return False
        local = now.astimezone(_timezone(config.monthly_timezone))
        expected_day = effective_monthly_day(
            local.year,
            local.month,
            config.monthly_day,
            config.monthly_short_month_policy,
        )
        return bool(expected_day and local.day == expected_day and local.time() >= _parse_time(config.monthly_time))
    if report_type == "annual":
        if not config.annual_enabled:
            return False
        local = now.astimezone(_timezone(config.annual_timezone))
        expected_day = effective_monthly_day(
            local.year,
            config.annual_month,
            config.annual_day,
            config.annual_short_month_policy,
        )
        return bool(
            expected_day
            and local.month == config.annual_month
            and local.day == expected_day
            and local.time() >= _parse_time(config.annual_time)
        )
    return False


class PartnerAnalyticsRepository:
    def __init__(self, partners_path: Path, applications_path: Path) -> None:
        self.partners_path = Path(partners_path)
        self.applications_path = Path(applications_path)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.partners_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _applications_connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.applications_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS partner_analytics_config (
                    guild_id INTEGER PRIMARY KEY,
                    analytics_channel_id INTEGER,
                    panel_message_id INTEGER,
                    weekly_enabled INTEGER NOT NULL DEFAULT 0,
                    weekly_weekday INTEGER NOT NULL DEFAULT 0,
                    weekly_time TEXT NOT NULL DEFAULT '09:00',
                    weekly_timezone TEXT NOT NULL DEFAULT 'America/Recife',
                    weekly_channel_id INTEGER,
                    monthly_enabled INTEGER NOT NULL DEFAULT 0,
                    monthly_day INTEGER NOT NULL DEFAULT 1,
                    monthly_time TEXT NOT NULL DEFAULT '09:00',
                    monthly_timezone TEXT NOT NULL DEFAULT 'America/Recife',
                    monthly_channel_id INTEGER,
                    monthly_short_month_policy TEXT NOT NULL DEFAULT 'last_day',
                    annual_enabled INTEGER NOT NULL DEFAULT 0,
                    annual_month INTEGER NOT NULL DEFAULT 1,
                    annual_day INTEGER NOT NULL DEFAULT 1,
                    annual_time TEXT NOT NULL DEFAULT '09:00',
                    annual_timezone TEXT NOT NULL DEFAULT 'America/Recife',
                    annual_channel_id INTEGER,
                    annual_short_month_policy TEXT NOT NULL DEFAULT 'last_day',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS partner_analytics_reports (
                    report_id TEXT PRIMARY KEY,
                    dedupe_key TEXT UNIQUE,
                    guild_id INTEGER NOT NULL,
                    report_type TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    period_label TEXT NOT NULL,
                    partial INTEGER NOT NULL DEFAULT 0,
                    generated_at TEXT NOT NULL,
                    published_at TEXT,
                    generated_by INTEGER,
                    indicators_json TEXT NOT NULL DEFAULT '{}',
                    comparison_json TEXT NOT NULL DEFAULT '{}',
                    summary TEXT NOT NULL DEFAULT '',
                    destination_channel_id INTEGER,
                    message_id INTEGER,
                    status TEXT NOT NULL,
                    error TEXT,
                    analytics_version TEXT NOT NULL,
                    schema_version INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_partner_analytics_reports_guild
                    ON partner_analytics_reports(guild_id, generated_at DESC);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(partner_analytics_config)")
            }
            if "analytics_channel_id" not in columns:
                connection.execute(
                    "ALTER TABLE partner_analytics_config ADD COLUMN analytics_channel_id INTEGER"
                )
            if "panel_message_id" not in columns:
                connection.execute(
                    "ALTER TABLE partner_analytics_config ADD COLUMN panel_message_id INTEGER"
                )
            annual_migrations = {
                "annual_enabled": "INTEGER NOT NULL DEFAULT 0",
                "annual_month": "INTEGER NOT NULL DEFAULT 1",
                "annual_day": "INTEGER NOT NULL DEFAULT 1",
                "annual_time": "TEXT NOT NULL DEFAULT '09:00'",
                "annual_timezone": "TEXT NOT NULL DEFAULT 'America/Recife'",
                "annual_channel_id": "INTEGER",
                "annual_short_month_policy": "TEXT NOT NULL DEFAULT 'last_day'",
            }
            for column, definition in annual_migrations.items():
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE partner_analytics_config ADD COLUMN {column} {definition}"
                    )

    def config(self, guild_id: int) -> PartnerAnalyticsConfig:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM partner_analytics_config WHERE guild_id=?",
                (guild_id,),
            ).fetchone()
        return PartnerAnalyticsConfig.from_row(guild_id, row)

    def save_config(self, config: PartnerAnalyticsConfig) -> None:
        config.updated_at = _now()
        payload = asdict(config)
        payload["weekly_enabled"] = int(config.weekly_enabled)
        payload["monthly_enabled"] = int(config.monthly_enabled)
        payload["annual_enabled"] = int(config.annual_enabled)
        columns = tuple(payload)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO partner_analytics_config ({', '.join(columns)})
                VALUES ({', '.join('?' for _ in columns)})
                ON CONFLICT(guild_id) DO UPDATE SET
                    analytics_channel_id=excluded.analytics_channel_id,
                    panel_message_id=excluded.panel_message_id,
                    weekly_enabled=excluded.weekly_enabled,
                    weekly_weekday=excluded.weekly_weekday,
                    weekly_time=excluded.weekly_time,
                    weekly_timezone=excluded.weekly_timezone,
                    weekly_channel_id=excluded.weekly_channel_id,
                    monthly_enabled=excluded.monthly_enabled,
                    monthly_day=excluded.monthly_day,
                    monthly_time=excluded.monthly_time,
                    monthly_timezone=excluded.monthly_timezone,
                    monthly_channel_id=excluded.monthly_channel_id,
                    monthly_short_month_policy=excluded.monthly_short_month_policy,
                    annual_enabled=excluded.annual_enabled,
                    annual_month=excluded.annual_month,
                    annual_day=excluded.annual_day,
                    annual_time=excluded.annual_time,
                    annual_timezone=excluded.annual_timezone,
                    annual_channel_id=excluded.annual_channel_id,
                    annual_short_month_policy=excluded.annual_short_month_policy,
                    updated_at=excluded.updated_at
                """,
                tuple(payload[column] for column in columns),
            )

    def configured_guild_ids(self) -> list[int]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT guild_id FROM partner_analytics_config
                 WHERE weekly_enabled=1 OR monthly_enabled=1 OR annual_enabled=1
                """
            ).fetchall()
        return [int(row["guild_id"]) for row in rows]

    def collect(self, guild_id: int, period: AnalyticsPeriod) -> dict[str, Any]:
        start = period.start.isoformat()
        end = period.end.isoformat()
        with self._lock, self._connect() as connection:
            partner_row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN status!='active' THEN 1 ELSE 0 END) AS inactive,
                    SUM(CASE WHEN joined_at>=? AND joined_at<? THEN 1 ELSE 0 END) AS new
                  FROM partners
                 WHERE guild_id=? AND joined_at<?
                """,
                (start, end, guild_id, end),
            ).fetchone()
            referral_row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN created_at>=? AND created_at<? THEN 1 ELSE 0 END) AS new,
                    SUM(CASE WHEN created_at>=? AND created_at<? AND status IN ('accepted','negotiation','closed','completed') THEN 1 ELSE 0 END) AS qualified,
                    SUM(CASE WHEN created_at>=? AND created_at<? AND status='negotiation' THEN 1 ELSE 0 END) AS negotiation,
                    SUM(CASE WHEN created_at>=? AND created_at<? AND status IN ('closed','completed') THEN 1 ELSE 0 END) AS converted,
                    SUM(CASE WHEN created_at>=? AND created_at<? AND status='rejected' THEN 1 ELSE 0 END) AS lost
                  FROM partner_referrals
                 WHERE guild_id=? AND created_at<?
                """,
                (start, end, start, end, start, end, start, end, start, end, guild_id, end),
            ).fetchone()
            status_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS total
                  FROM partner_referrals
                 WHERE guild_id=? AND created_at>=? AND created_at<?
                 GROUP BY status
                """,
                (guild_id, start, end),
            ).fetchall()
            project_row = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status IN ('closed','fechado') THEN 1 ELSE 0 END) AS contracts,
                    SUM(CASE WHEN status IN ('closed','fechado') THEN revenue ELSE 0 END) AS revenue,
                    SUM(CASE WHEN status IN ('negotiation','negociacao','em_negociacao') THEN 1 ELSE 0 END) AS negotiations
                  FROM partner_projects
                 WHERE guild_id=? AND created_at>=? AND created_at<?
                """,
                (guild_id, start, end),
            ).fetchone()
            commercial_row = connection.execute(
                """
                SELECT COUNT(*) AS total FROM (
                    SELECT partner_id FROM partner_referrals
                     WHERE guild_id=? AND created_at>=? AND created_at<?
                    UNION
                    SELECT partner_id FROM partner_projects
                     WHERE guild_id=? AND created_at>=? AND created_at<?
                )
                """,
                (guild_id, start, end, guild_id, start, end),
            ).fetchone()
            commission_row = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN created_at>=? AND created_at<? THEN amount ELSE 0 END) AS generated,
                    SUM(CASE WHEN status='pending' AND created_at<? THEN amount ELSE 0 END) AS pending,
                    SUM(CASE WHEN status='paid' AND paid_at>=? AND paid_at<? THEN amount ELSE 0 END) AS paid
                  FROM partner_commissions
                 WHERE guild_id=?
                """,
                (start, end, end, start, end, guild_id),
            ).fetchone()
            top_referrals = connection.execute(
                """
                SELECT partner_id, COUNT(*) AS total,
                       SUM(CASE WHEN status IN ('closed','completed') THEN 1 ELSE 0 END) AS conversions
                  FROM partner_referrals
                 WHERE guild_id=? AND created_at>=? AND created_at<?
                 GROUP BY partner_id
                 ORDER BY total DESC, conversions DESC, partner_id ASC
                 LIMIT 5
                """,
                (guild_id, start, end),
            ).fetchall()
            top_conversions = connection.execute(
                """
                SELECT partner_id, COUNT(*) AS total,
                       SUM(CASE WHEN status IN ('closed','completed') THEN 1 ELSE 0 END) AS conversions
                  FROM partner_referrals
                 WHERE guild_id=? AND created_at>=? AND created_at<?
                 GROUP BY partner_id
                HAVING conversions>0
                 ORDER BY conversions DESC, total DESC, partner_id ASC
                 LIMIT 5
                """,
                (guild_id, start, end),
            ).fetchall()
            daily_referrals = connection.execute(
                """
                SELECT created_at
                  FROM partner_referrals
                 WHERE guild_id=? AND created_at>=? AND created_at<?
                """,
                (guild_id, start, end),
            ).fetchall()
            daily_projects = connection.execute(
                """
                SELECT created_at, status
                  FROM partner_projects
                 WHERE guild_id=? AND created_at>=? AND created_at<?
                """,
                (guild_id, start, end),
            ).fetchall()
        with self._lock, self._applications_connect() as connection:
            application_row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN created_at>=? AND created_at<? THEN 1 ELSE 0 END) AS new,
                    SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) AS approved,
                    SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected,
                    SUM(CASE WHEN status='approved' AND COALESCE(reviewed_at, updated_at)>=? AND COALESCE(reviewed_at, updated_at)<? THEN 1 ELSE 0 END) AS approved_period,
                    SUM(CASE WHEN status='rejected' AND COALESCE(reviewed_at, updated_at)>=? AND COALESCE(reviewed_at, updated_at)<? THEN 1 ELSE 0 END) AS rejected_period
                  FROM partner_applications
                 WHERE guild_id=? AND created_at<?
                """,
                (start, end, start, end, start, end, guild_id, end),
            ).fetchone()
        partners = _numbers(partner_row)
        partners["approved"] = partners.get("total", 0)
        partners["commercially_active"] = int(commercial_row["total"] or 0) if commercial_row else 0
        referrals = _numbers(referral_row)
        applications = _numbers(application_row)
        projects = _numbers(project_row)
        commissions = _numbers(commission_row)
        decisions = applications["approved_period"] + applications["rejected_period"]
        applications["approval_rate"] = (
            applications["approved_period"] / decisions * 100.0 if decisions else None
        )
        referrals["conversion_rate"] = (
            referrals["converted"] / referrals["new"] * 100.0
            if referrals["new"]
            else None
        )
        status_distribution = {str(row["status"]): int(row["total"] or 0) for row in status_rows}
        daily = _daily_series(period, daily_referrals, daily_projects)
        return {
            "period": period.to_dict(),
            "partners": partners,
            "applications": applications,
            "referrals": referrals,
            "projects": projects,
            "commissions": commissions,
            "status_distribution": status_distribution,
            "daily": daily,
            "top_referrals": [dict(row) for row in top_referrals],
            "top_conversions": [dict(row) for row in top_conversions],
            "definitions": {
                "active_partner": "Parceiro aprovado cujo cadastro está ativo no fim do período.",
                "commercially_active_partner": "Parceiro com ao menos uma indicação ou resultado comercial registrado dentro do período.",
                "new_referral": "Indicação criada dentro do período de referência.",
                "conversion_rate": "Indicações criadas no período que atualmente estão como projeto fechado ou finalizado, divididas pelas novas indicações do período.",
                "approval_rate": "Aprovações divididas pelas decisões de aprovação ou recusa registradas no período.",
                "revenue": "Receita dos projetos cadastrados como fechados dentro do período.",
            },
        }

    def reserve_report(
        self,
        *,
        guild_id: int,
        report_type: str,
        period: AnalyticsPeriod,
        generated_by: int | None,
        dedupe_key: str | None,
    ) -> tuple[str, bool]:
        now = _now()
        with self._lock, self._connect() as connection:
            if dedupe_key:
                existing = connection.execute(
                    "SELECT * FROM partner_analytics_reports WHERE dedupe_key=?",
                    (dedupe_key,),
                ).fetchone()
                if existing is not None:
                    stale = _is_stale(str(existing["generated_at"]), minutes=20)
                    if str(existing["status"]) in {"failed", "cancelled"} or (
                        str(existing["status"]) == "generating" and stale
                    ):
                        connection.execute(
                            """
                            UPDATE partner_analytics_reports
                               SET status='generating', error=NULL, generated_at=?, generated_by=?
                             WHERE report_id=?
                            """,
                            (now, generated_by, existing["report_id"]),
                        )
                        return str(existing["report_id"]), True
                    return str(existing["report_id"]), False
            report_id = uuid.uuid4().hex[:16]
            connection.execute(
                """
                INSERT INTO partner_analytics_reports (
                    report_id, dedupe_key, guild_id, report_type, period_start,
                    period_end, period_label, partial, generated_at, generated_by,
                    status, analytics_version, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'generating', ?, ?)
                """,
                (
                    report_id,
                    dedupe_key,
                    guild_id,
                    report_type,
                    period.start.isoformat(),
                    period.end.isoformat(),
                    period.label,
                    int(period.partial),
                    now,
                    generated_by,
                    ANALYTICS_VERSION,
                    REPORT_SCHEMA_VERSION,
                ),
            )
        return report_id, True

    def complete_report(
        self,
        report_id: str,
        *,
        indicators: dict[str, Any],
        comparison: dict[str, Any],
        summary: str,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE partner_analytics_reports
                   SET indicators_json=?, comparison_json=?, summary=?, status='generated', error=NULL
                 WHERE report_id=?
                """,
                (
                    json.dumps(indicators, ensure_ascii=False),
                    json.dumps(comparison, ensure_ascii=False),
                    summary,
                    report_id,
                ),
            )

    def fail_report(self, report_id: str, error: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE partner_analytics_reports SET status='failed', error=? WHERE report_id=?",
                (error[:1500], report_id),
            )

    def mark_published(self, report_id: str, channel_id: int, message_id: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE partner_analytics_reports
                   SET status='published', published_at=?, destination_channel_id=?, message_id=?, error=NULL
                 WHERE report_id=?
                """,
                (_now(), channel_id, message_id, report_id),
            )

    def report(self, report_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM partner_analytics_reports WHERE report_id=?",
                (report_id,),
            ).fetchone()
        return _report_row(row)

    def history(self, guild_id: int, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM partner_analytics_reports
                 WHERE guild_id=?
                 ORDER BY generated_at DESC
                 LIMIT ?
                """,
                (guild_id, max(1, min(100, limit))),
            ).fetchall()
        return [_report_row(row) for row in rows]


class PartnerAnalyticsService:
    def __init__(
        self,
        bot: discord.Client,
        partners_path: Path,
        applications_path: Path,
        ai_service: GroqService,
        central_logs: CentralLogService,
    ) -> None:
        self.bot = bot
        self.ai_service = ai_service
        self.central_logs = central_logs
        self.repository = PartnerAnalyticsRepository(partners_path, applications_path)
        self.program_service: Any = None
        self._locks: dict[int, asyncio.Lock] = {}

    async def authorized(self, user: discord.abc.User, panel_id: int) -> bool:
        if self.program_service is None:
            return False
        panel = await self.program_service.db.panel(panel_id)
        return bool(panel and self.program_service.can_manage_application(user, panel))

    async def open_center(self, interaction: discord.Interaction, panel_id: int) -> None:
        if interaction.guild is None or not await self.authorized(interaction.user, panel_id):
            await _reply(interaction, "Você não pode acessar os relatórios comerciais.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel, config = await self.ensure_analytics_channel(
                interaction.guild,
                actor=interaction.user,
            )
            history = await asyncio.to_thread(self.repository.history, interaction.guild.id, 5)
            message = await self.publish_center_panel(
                interaction.guild,
                channel,
                panel_id,
                config,
                history,
            )
        except (discord.Forbidden, discord.HTTPException, PermissionError, RuntimeError) as exc:
            await interaction.followup.send(
                f"Não consegui preparar a Central de Analytics: {exc}",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Central de Analytics pronta em {channel.mention}: {message.jump_url}",
            ephemeral=True,
        )

    def register_persistent_view(self, panel_id: int) -> None:
        if hasattr(self.bot, "add_view"):
            self.bot.add_view(PartnerAnalyticsCenterV2View(self, panel_id))

    async def ensure_analytics_channel(
        self,
        guild: discord.Guild,
        *,
        actor: discord.abc.User,
    ) -> tuple[discord.TextChannel, PartnerAnalyticsConfig]:
        channel = await self.central_logs.ensure_panel_channel(
            guild,
            PARTNER_ANALYTICS_CHANNEL,
            topic=(
                "Central privada de relatórios, gráficos, comparativos e agendamentos "
                "do Programa de Parceiros da Bella."
            ),
            reason=f"Bella Partner Analytics solicitado por {actor}",
        )
        config = await asyncio.to_thread(self.repository.config, guild.id)
        config.analytics_channel_id = channel.id
        if config.weekly_channel_id is None:
            config.weekly_channel_id = channel.id
        if config.monthly_channel_id is None:
            config.monthly_channel_id = channel.id
        if config.annual_channel_id is None:
            config.annual_channel_id = channel.id
        await asyncio.to_thread(self.repository.save_config, config)
        return channel, config

    async def publish_center_panel(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        panel_id: int,
        config: PartnerAnalyticsConfig,
        history: list[dict[str, Any]],
    ) -> discord.Message:
        view = PartnerAnalyticsCenterV2View(
            self,
            panel_id,
            markdown=await self.center_markdown(guild, config, history),
        )
        message = None
        if config.panel_message_id:
            try:
                message = await channel.fetch_message(config.panel_message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        if message is None:
            message = await channel.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await message.edit(
                content=None,
                embeds=[],
                attachments=[],
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if not message.pinned:
            try:
                await message.pin(reason="Painel fixo do Bella Partner Analytics")
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Não foi possível fixar a Central de Analytics em %s.", channel.id)
        config.analytics_channel_id = channel.id
        config.panel_message_id = message.id
        await asyncio.to_thread(self.repository.save_config, config)
        return message

    async def center_markdown(
        self,
        guild: discord.Guild,
        config: PartnerAnalyticsConfig,
        history: list[dict[str, Any]],
    ) -> str:
        weekly_channel = await self.resolve_channel(guild, config, "weekly")
        monthly_channel = await self.resolve_channel(guild, config, "monthly")
        annual_channel = await self.resolve_channel(guild, config, "annual")
        latest = history[0] if history else None
        history_text = (
            f"Último relatório: **{latest['period_label']}** (`{latest['status']}`)"
            if latest
            else "Nenhum relatório foi gerado ainda."
        )
        return (
            "## Bella Partner Analytics\n"
            "Central privada de indicadores, relatórios visuais e comparativos do Programa de Parceiros.\n\n"
            "### Relatório semanal\n"
            f"Status: **{'Ativo' if config.weekly_enabled else 'Desativado'}**  •  "
            f"{WEEKDAY_LABELS[config.weekly_weekday]} às **{config.weekly_time}**  •  "
            f"`{config.weekly_timezone}`\n"
            f"Destino: {weekly_channel.mention if weekly_channel else 'não configurado'}\n\n"
            "### Relatório mensal\n"
            f"Status: **{'Ativo' if config.monthly_enabled else 'Desativado'}**  •  "
            f"dia **{config.monthly_day}** às **{config.monthly_time}**  •  "
            f"`{config.monthly_timezone}`\n"
            f"Destino: {monthly_channel.mention if monthly_channel else 'não configurado'}\n\n"
            "### Relatório anual\n"
            f"Status: **{'Ativo' if config.annual_enabled else 'Desativado'}**  •  "
            f"dia **{config.annual_day}/{config.annual_month:02d}** às **{config.annual_time}**  •  "
            f"`{config.annual_timezone}`\n"
            f"Destino: {annual_channel.mention if annual_channel else 'não configurado'}\n\n"
            f"### Histórico\n{history_text}\n\n"
            f"-# Bella Partner Analytics • v{ANALYTICS_VERSION} • dados comerciais privados"
        )[:4000]

    async def center_embed(
        self,
        guild: discord.Guild,
        config: PartnerAnalyticsConfig,
        history: list[dict[str, Any]],
    ) -> discord.Embed:
        weekly_channel = await self.resolve_channel(guild, config, "weekly")
        monthly_channel = await self.resolve_channel(guild, config, "monthly")
        annual_channel = await self.resolve_channel(guild, config, "annual")
        embed = discord.Embed(
            title="Bella Partner Analytics",
            description=(
                "Central administrativa de indicadores, relatórios visuais, comparativos e "
                "publicações automáticas do Programa de Parceiros."
            ),
            color=discord.Color(PARTNER_ANALYTICS_COLOR),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Relatório semanal",
            value=(
                f"Status: **{'Ativo' if config.weekly_enabled else 'Desativado'}**\n"
                f"Envio: **{WEEKDAY_LABELS[config.weekly_weekday]} às {config.weekly_time}**\n"
                f"Fuso: `{config.weekly_timezone}`\n"
                f"Destino: {weekly_channel.mention if weekly_channel else 'não configurado'}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Relatório mensal",
            value=(
                f"Status: **{'Ativo' if config.monthly_enabled else 'Desativado'}**\n"
                f"Envio: **dia {config.monthly_day} às {config.monthly_time}**\n"
                f"Fuso: `{config.monthly_timezone}`\n"
                f"Destino: {monthly_channel.mention if monthly_channel else 'não configurado'}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Relatório anual",
            value=(
                f"Status: **{'Ativo' if config.annual_enabled else 'Desativado'}**\n"
                f"Envio: **{config.annual_day:02d}/{config.annual_month:02d} às {config.annual_time}**\n"
                f"Fuso: `{config.annual_timezone}`\n"
                f"Destino: {annual_channel.mention if annual_channel else 'não configurado'}"
            ),
            inline=True,
        )
        latest = history[0] if history else None
        embed.add_field(
            name="Histórico",
            value=(
                f"Último: **{latest['period_label']}**\nStatus: `{latest['status']}`"
                if latest
                else "Nenhum relatório gerado ainda."
            ),
            inline=False,
        )
        embed.set_footer(text=f"Bella Partner Analytics • v{ANALYTICS_VERSION}")
        return embed

    async def generate(
        self,
        guild: discord.Guild,
        *,
        report_type: str,
        period: AnalyticsPeriod,
        generated_by: int | None,
        automatic: bool = False,
        destination_channel_id: int | None = None,
    ) -> dict[str, Any] | None:
        lock = self._locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            dedupe_key = None
            if automatic:
                dedupe_key = (
                    f"{guild.id}:{report_type}:{period.start.isoformat()}:"
                    f"{period.end.isoformat()}:{destination_channel_id or 0}"
                )
            report_id, claimed = await asyncio.to_thread(
                self.repository.reserve_report,
                guild_id=guild.id,
                report_type=report_type,
                period=period,
                generated_by=generated_by,
                dedupe_key=dedupe_key,
            )
            if not claimed:
                return await asyncio.to_thread(self.repository.report, report_id)
            try:
                previous_period = previous_report_period(period, report_type)
                indicators, previous = await asyncio.gather(
                    asyncio.to_thread(self.repository.collect, guild.id, period),
                    asyncio.to_thread(self.repository.collect, guild.id, previous_period),
                )
                comparison = build_comparison(indicators, previous)
                summary = await self._summary(indicators, comparison)
                await asyncio.to_thread(
                    self.repository.complete_report,
                    report_id,
                    indicators=indicators,
                    comparison=comparison,
                    summary=summary,
                )
                return await asyncio.to_thread(self.repository.report, report_id)
            except Exception as exc:
                LOGGER.exception("Falha ao gerar relatório de parceiros %s", report_id)
                await asyncio.to_thread(
                    self.repository.fail_report,
                    report_id,
                    f"{type(exc).__name__}: {exc}",
                )
                return None

    async def _summary(self, indicators: dict[str, Any], comparison: dict[str, Any]) -> str:
        fallback = deterministic_summary(indicators, comparison)
        prompt = (
            "Produza um resumo executivo em português brasileiro com exatamente 4 parágrafos curtos "
            "e no máximo 1.200 caracteres no total. Não use título, subtítulo, lista ou marcadores. "
            "No primeiro parágrafo resuma indicações, qualificações, conversões, contratos e receita; "
            "no segundo, parceiros, inscrições e comissões; no terceiro, compare com o período anterior; "
            "no quarto, destaque o parceiro líder quando houver ranking. "
            "Ao citar um parceiro pelo ID, use a menção Discord no formato <@ID>. "
            "Use negrito Markdown apenas nos números mais relevantes. Use somente os números do JSON. "
            "Não invente causas, não cite contatos, não faça promessas e sinalize ausência de base "
            "comparativa. Seja profissional, natural e objetivo.\n\n"
            + json.dumps(
                {"indicators": _summary_payload(indicators), "comparison": comparison},
                ensure_ascii=False,
            )
        )
        try:
            result = await self.ai_service.generate_text(
                prompt,
                max_output_tokens=420,
                timeout_seconds=25,
                model_policy="economy",
                use_cache=False,
                system_prompt=(
                    "Você é a Bella em modo de análise comercial. Explique somente dados validados, "
                    "sem inferir causas ou expor informações pessoais."
                ),
                prompt_char_limit=7000,
            )
        except Exception as exc:
            LOGGER.warning(
                "IA indisponível para resumo de parceiros; usando fallback: %s",
                type(exc).__name__,
            )
            return fallback
        summary = _compact_summary(result, limit=1200)
        return summary if len(summary.split("\n\n")) == 4 else fallback

    async def report_artifacts(
        self,
        report: dict[str, Any],
        *,
        panel_id: int | None = None,
        allow_publish: bool = False,
    ) -> tuple[discord.File, "PartnerAnalyticsReportV2View"]:
        image = await asyncio.to_thread(render_partner_analytics, report)
        filename = f"bella-partner-analytics-{report['report_id']}.png"
        file = discord.File(image, filename=filename)
        view = PartnerAnalyticsReportV2View(
            self,
            panel_id,
            report,
            image=file,
            allow_publish=allow_publish,
        )
        return file, view

    async def publish_report(
        self,
        guild: discord.Guild,
        report: dict[str, Any],
        *,
        channel_id: int | None = None,
    ) -> discord.Message:
        if report.get("status") == "published" and report.get("message_id"):
            raise RuntimeError("Este relatório já foi publicado.")
        config = await asyncio.to_thread(self.repository.config, guild.id)
        report_type = str(report.get("report_type") or "weekly")
        channel = guild.get_channel(channel_id or 0) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            channel = await self.resolve_channel(guild, config, report_type)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("O canal de logs de parcerias não está configurado.")
        if not _channel_is_private(channel):
            raise RuntimeError(
                "O canal escolhido é visível ao @everyone. Configure um canal privado para os relatórios comerciais."
            )
        existing = await _find_existing_publication(channel, str(report["report_id"]))
        if existing is not None:
            await asyncio.to_thread(
                self.repository.mark_published,
                str(report["report_id"]),
                channel.id,
                existing.id,
            )
            return existing
        file, view = await self.report_artifacts(report)
        message = await channel.send(
            file=file,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await asyncio.to_thread(
            self.repository.mark_published,
            str(report["report_id"]),
            channel.id,
            message.id,
        )
        return message

    async def resolve_channel(
        self,
        guild: discord.Guild,
        config: PartnerAnalyticsConfig,
        report_type: str,
    ) -> discord.TextChannel | None:
        configured = (
            config.weekly_channel_id
            if report_type == "weekly"
            else config.monthly_channel_id
            if report_type == "monthly"
            else config.annual_channel_id
            if report_type == "annual"
            else config.analytics_channel_id
        )
        channel = guild.get_channel(configured or 0)
        if isinstance(channel, discord.TextChannel) and _channel_is_private(channel):
            return channel
        analytics_channel = guild.get_channel(config.analytics_channel_id or 0)
        if isinstance(analytics_channel, discord.TextChannel) and _channel_is_private(analytics_channel):
            return analytics_channel
        channels = await self.central_logs.channel_ids(guild.id)
        central = guild.get_channel(int(channels.get("partners") or 0))
        return (
            central
            if isinstance(central, discord.TextChannel) and _channel_is_private(central)
            else None
        )

    async def run_due(self, now: datetime | None = None) -> int:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        published = 0
        guild_ids = await asyncio.to_thread(self.repository.configured_guild_ids)
        for guild_id in guild_ids:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            config = await asyncio.to_thread(self.repository.config, guild_id)
            for report_type in ("weekly", "monthly", "annual"):
                if not schedule_is_due(config, report_type, now):
                    continue
                if report_type == "weekly":
                    timezone_name = config.weekly_timezone
                    period = last_complete_week(now, timezone_name)
                elif report_type == "monthly":
                    timezone_name = config.monthly_timezone
                    period = last_complete_month(now, timezone_name)
                else:
                    timezone_name = config.annual_timezone
                    period = last_complete_year(now, timezone_name)
                channel = await self.resolve_channel(guild, config, report_type)
                channel_id = channel.id if channel else None
                report = await self.generate(
                    guild,
                    report_type=report_type,
                    period=period,
                    generated_by=None,
                    automatic=True,
                    destination_channel_id=channel_id,
                )
                if not report:
                    continue
                if report.get("status") == "published":
                    continue
                try:
                    await self.publish_report(guild, report, channel_id=channel_id)
                    published += 1
                except Exception as exc:
                    LOGGER.exception("Falha ao publicar relatório automático de parceiros.")
                    await asyncio.to_thread(
                        self.repository.fail_report,
                        str(report["report_id"]),
                        f"{type(exc).__name__}: {exc}",
                    )
        return published

class _AuthorizedAnalyticsView(discord.ui.View):
    def __init__(self, service: PartnerAnalyticsService, panel_id: int, *, timeout: float = 300) -> None:
        super().__init__(timeout=timeout)
        self.service = service
        self.panel_id = int(panel_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.service.authorized(interaction.user, self.panel_id):
            return True
        await _reply(interaction, "Você não pode acessar os relatórios comerciais.")
        return False


class PartnerAnalyticsCenterV2View(discord.ui.LayoutView):
    def __init__(
        self,
        service: PartnerAnalyticsService,
        panel_id: int,
        *,
        markdown: str | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.panel_id = int(panel_id)
        container = discord.ui.Container(accent_color=discord.Color(PARTNER_ANALYTICS_COLOR))
        container.add_item(
            discord.ui.TextDisplay(
                markdown
                or (
                    "## Bella Partner Analytics\n"
                    "Central privada de relatórios do Programa de Parceiros.\n\n"
                    "Use os controles abaixo para gerar, agendar ou consultar relatórios."
                )
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        row = discord.ui.ActionRow()
        generate = discord.ui.Button(
            label="Gerar agora",
            style=discord.ButtonStyle.success,
            custom_id=f"bella:partners:analytics:generate:{self.panel_id}",
        )
        generate.callback = self._generate_now  # type: ignore[method-assign]
        row.add_item(generate)
        schedule = discord.ui.Button(
            label="Agendamento",
            style=discord.ButtonStyle.primary,
            custom_id=f"bella:partners:analytics:schedule:{self.panel_id}",
        )
        schedule.callback = self._schedule  # type: ignore[method-assign]
        row.add_item(schedule)
        history = discord.ui.Button(
            label="Histórico",
            style=discord.ButtonStyle.secondary,
            custom_id=f"bella:partners:analytics:history:{self.panel_id}",
        )
        history.callback = self._history  # type: ignore[method-assign]
        row.add_item(history)
        container.add_item(row)
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.service.authorized(interaction.user, self.panel_id):
            return True
        await _reply(interaction, "Você não pode acessar os relatórios comerciais.")
        return False

    async def _generate_now(self, interaction: discord.Interaction) -> None:
        await _reply(
            interaction,
            "Escolha o período. A Bella gera uma prévia privada antes de qualquer publicação.",
            view=PartnerReportPeriodView(self.service, self.panel_id),
        )

    async def _schedule(self, interaction: discord.Interaction) -> None:
        await _reply(
            interaction,
            "Configure os relatórios automáticos e os canais de destino.",
            view=PartnerAnalyticsScheduleView(self.service, self.panel_id),
        )

    async def _history(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        reports = await asyncio.to_thread(self.service.repository.history, interaction.guild.id, 20)
        if not reports:
            await _reply(interaction, "Nenhum relatório foi gerado ainda.")
            return
        await _reply(
            interaction,
            view=PartnerAnalyticsHistoryV2View(self.service, self.panel_id, reports),
        )

class PartnerAnalyticsReportV2View(discord.ui.LayoutView):
    def __init__(
        self,
        service: PartnerAnalyticsService,
        panel_id: int | None,
        report: dict[str, Any],
        *,
        image: discord.File,
        allow_publish: bool,
    ) -> None:
        super().__init__(timeout=300 if allow_publish else None)
        self.service = service
        self.panel_id = int(panel_id or 0)
        self.report_id = str(report["report_id"])
        self.current_file = image
        container = discord.ui.Container(accent_color=discord.Color(PARTNER_ANALYTICS_COLOR))
        container.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(
                    image,
                    description=f"Dashboard estatístico do relatório {self.report_id}",
                )
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        blocks = report_v2_blocks(report)
        for index, block in enumerate(blocks):
            container.add_item(discord.ui.TextDisplay(block))
            if index < len(blocks) - 1:
                container.add_item(
                    discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
                )
        self.add_item(container)
        if allow_publish:
            row = discord.ui.ActionRow()
            publish = discord.ui.Button(
                label="Publicar no canal configurado",
                style=discord.ButtonStyle.success,
                custom_id=f"bella:partners:analytics:publish:{self.report_id}"[:100],
            )
            publish.callback = self._publish  # type: ignore[method-assign]
            row.add_item(publish)
            self.add_item(row)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.panel_id and await self.service.authorized(interaction.user, self.panel_id):
            return True
        await _reply(interaction, "Você não pode publicar relatórios comerciais.")
        return False

    async def _publish(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = await asyncio.to_thread(self.service.repository.report, self.report_id)
        if report is None:
            await interaction.followup.send("Relatório não encontrado.", ephemeral=True)
            return
        try:
            message = await self.service.publish_report(interaction.guild, report)
        except Exception as exc:
            await interaction.followup.send(f"Não consegui publicar: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Relatório publicado em {message.channel.mention}.",
            ephemeral=True,
        )


class PartnerAnalyticsCenterView(_AuthorizedAnalyticsView):
    @discord.ui.button(label="Gerar agora", style=discord.ButtonStyle.success)
    async def generate_now(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await _reply(
            interaction,
            "Escolha o período. A Bella gera uma prévia privada antes de qualquer publicação.",
            view=PartnerReportPeriodView(self.service, self.panel_id),
        )

    @discord.ui.button(label="Agendamento", style=discord.ButtonStyle.primary)
    async def schedule(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await _reply(
            interaction,
            "Configure os relatórios automáticos e os canais de destino.",
            view=PartnerAnalyticsScheduleView(self.service, self.panel_id),
        )

    @discord.ui.button(label="Histórico", style=discord.ButtonStyle.secondary)
    async def history(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if interaction.guild is None:
            return
        reports = await asyncio.to_thread(self.service.repository.history, interaction.guild.id, 20)
        if not reports:
            await _reply(interaction, "Nenhum relatório foi gerado ainda.")
            return
        await _reply(
            interaction,
            view=PartnerAnalyticsHistoryV2View(self.service, self.panel_id, reports),
        )


class PartnerReportPeriodView(_AuthorizedAnalyticsView):
    async def _generate(self, interaction: discord.Interaction, report_type: str, period: AnalyticsPeriod) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = await self.service.generate(
            interaction.guild,
            report_type=report_type,
            period=period,
            generated_by=interaction.user.id,
        )
        if report is None:
            await interaction.followup.send("Não consegui gerar o relatório.", ephemeral=True)
            return
        file, view = await self.service.report_artifacts(
            report,
            panel_id=self.panel_id,
            allow_publish=True,
        )
        await interaction.followup.send(
            file=file,
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Última semana", style=discord.ButtonStyle.primary, row=0)
    async def weekly(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if interaction.guild is None:
            return
        config = await asyncio.to_thread(self.service.repository.config, interaction.guild.id)
        await self._generate(
            interaction,
            "weekly",
            last_complete_week(datetime.now(timezone.utc), config.weekly_timezone),
        )

    @discord.ui.button(label="Último mês", style=discord.ButtonStyle.primary, row=0)
    async def monthly(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if interaction.guild is None:
            return
        config = await asyncio.to_thread(self.service.repository.config, interaction.guild.id)
        await self._generate(
            interaction,
            "monthly",
            last_complete_month(datetime.now(timezone.utc), config.monthly_timezone),
        )

    @discord.ui.button(label="Último ano", style=discord.ButtonStyle.primary, row=0)
    async def annual(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if interaction.guild is None:
            return
        config = await asyncio.to_thread(self.service.repository.config, interaction.guild.id)
        await self._generate(
            interaction,
            "annual",
            last_complete_year(datetime.now(timezone.utc), config.annual_timezone),
        )

    @discord.ui.button(label="Semana parcial", style=discord.ButtonStyle.secondary, row=1)
    async def partial_week(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if interaction.guild is None:
            return
        config = await asyncio.to_thread(self.service.repository.config, interaction.guild.id)
        await self._generate(
            interaction,
            "weekly",
            current_partial_week(datetime.now(timezone.utc), config.weekly_timezone),
        )

    @discord.ui.button(label="Mês parcial", style=discord.ButtonStyle.secondary, row=1)
    async def partial_month(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if interaction.guild is None:
            return
        config = await asyncio.to_thread(self.service.repository.config, interaction.guild.id)
        await self._generate(
            interaction,
            "monthly",
            current_partial_month(datetime.now(timezone.utc), config.monthly_timezone),
        )

    @discord.ui.button(label="Ano parcial", style=discord.ButtonStyle.secondary, row=1)
    async def partial_year(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if interaction.guild is None:
            return
        config = await asyncio.to_thread(self.service.repository.config, interaction.guild.id)
        await self._generate(
            interaction,
            "annual",
            current_partial_year(datetime.now(timezone.utc), config.annual_timezone),
        )

    @discord.ui.button(label="Personalizado", style=discord.ButtonStyle.secondary, row=1)
    async def custom(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await interaction.response.send_modal(PartnerCustomPeriodModal(self.service, self.panel_id))


class PartnerAnalyticsPreviewView(_AuthorizedAnalyticsView):
    def __init__(self, service: PartnerAnalyticsService, panel_id: int, report_id: str) -> None:
        super().__init__(service, panel_id)
        self.report_id = report_id

    @discord.ui.button(label="Publicar no canal configurado", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = await asyncio.to_thread(self.service.repository.report, self.report_id)
        if report is None:
            await interaction.followup.send("Relatório não encontrado.", ephemeral=True)
            return
        try:
            message = await self.service.publish_report(interaction.guild, report)
        except Exception as exc:
            await interaction.followup.send(f"Não consegui publicar: {exc}", ephemeral=True)
            return
        await interaction.followup.send(f"Relatório publicado em {message.channel.mention}.", ephemeral=True)


class PartnerCustomPeriodModal(discord.ui.Modal, title="Período personalizado"):
    start_date = discord.ui.TextInput(label="Data inicial", placeholder="01/10/2026", max_length=10)
    end_date = discord.ui.TextInput(label="Data final", placeholder="11/10/2026", max_length=10)
    timezone_name = discord.ui.TextInput(
        label="Fuso horário",
        default=DEFAULT_TIMEZONE,
        max_length=80,
    )

    def __init__(self, service: PartnerAnalyticsService, panel_id: int) -> None:
        super().__init__()
        self.service = service
        self.panel_id = panel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not await self.service.authorized(interaction.user, self.panel_id):
            await _reply(interaction, "Você não pode gerar relatórios.")
            return
        try:
            start = datetime.strptime(self.start_date.value, "%d/%m/%Y").date()
            end = datetime.strptime(self.end_date.value, "%d/%m/%Y").date()
            period = custom_period(start, end, self.timezone_name.value.strip())
        except (ValueError, ZoneInfoNotFoundError) as exc:
            await _reply(interaction, f"Período inválido: {exc}")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = await self.service.generate(
            interaction.guild,
            report_type="custom",
            period=period,
            generated_by=interaction.user.id,
        )
        if report is None:
            await interaction.followup.send("Não consegui gerar o relatório.", ephemeral=True)
            return
        file, view = await self.service.report_artifacts(
            report,
            panel_id=self.panel_id,
            allow_publish=True,
        )
        await interaction.followup.send(
            file=file,
            view=view,
            ephemeral=True,
        )


class PartnerAnalyticsScheduleView(_AuthorizedAnalyticsView):
    @discord.ui.button(label="Semanal", style=discord.ButtonStyle.primary, row=0)
    async def weekly(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if interaction.guild is None:
            return
        config = await asyncio.to_thread(self.service.repository.config, interaction.guild.id)
        await interaction.response.send_modal(PartnerWeeklyScheduleModal(self.service, self.panel_id, config))

    @discord.ui.button(label="Mensal", style=discord.ButtonStyle.primary, row=0)
    async def monthly(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if interaction.guild is None:
            return
        config = await asyncio.to_thread(self.service.repository.config, interaction.guild.id)
        await interaction.response.send_modal(PartnerMonthlyScheduleModal(self.service, self.panel_id, config))

    @discord.ui.button(label="Anual", style=discord.ButtonStyle.primary, row=0)
    async def annual(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        if interaction.guild is None:
            return
        config = await asyncio.to_thread(self.service.repository.config, interaction.guild.id)
        await interaction.response.send_modal(PartnerAnnualScheduleModal(self.service, self.panel_id, config))

    @discord.ui.button(label="Canal semanal", style=discord.ButtonStyle.secondary, row=1)
    async def weekly_channel(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await _reply(
            interaction,
            "Escolha o canal do relatório semanal.",
            view=PartnerAnalyticsChannelView(self.service, self.panel_id, "weekly"),
        )

    @discord.ui.button(label="Canal mensal", style=discord.ButtonStyle.secondary, row=1)
    async def monthly_channel(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await _reply(
            interaction,
            "Escolha o canal do relatório mensal.",
            view=PartnerAnalyticsChannelView(self.service, self.panel_id, "monthly"),
        )

    @discord.ui.button(label="Canal anual", style=discord.ButtonStyle.secondary, row=1)
    async def annual_channel(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await _reply(
            interaction,
            "Escolha o canal do relatório anual.",
            view=PartnerAnalyticsChannelView(self.service, self.panel_id, "annual"),
        )


class PartnerWeeklyScheduleModal(discord.ui.Modal, title="Agendamento semanal"):
    def __init__(self, service: PartnerAnalyticsService, panel_id: int, config: PartnerAnalyticsConfig) -> None:
        super().__init__()
        self.service = service
        self.panel_id = panel_id
        self.enabled = discord.ui.TextInput(label="Ativo?", default="sim" if config.weekly_enabled else "não", max_length=3)
        self.weekday = discord.ui.TextInput(label="Dia (0=segunda, 6=domingo)", default=str(config.weekly_weekday), max_length=1)
        self.hour = discord.ui.TextInput(label="Horário", default=config.weekly_time, max_length=5)
        self.timezone_name = discord.ui.TextInput(label="Fuso horário", default=config.weekly_timezone, max_length=80)
        for item in (self.enabled, self.weekday, self.hour, self.timezone_name):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not await self.service.authorized(interaction.user, self.panel_id):
            return
        try:
            enabled = _parse_bool(self.enabled.value)
            weekday = int(self.weekday.value)
            if weekday not in range(7):
                raise ValueError("O dia deve estar entre 0 e 6.")
            hour = _normalize_time(self.hour.value)
            timezone_name = _timezone(self.timezone_name.value.strip()).key
        except (ValueError, ZoneInfoNotFoundError) as exc:
            await _reply(interaction, f"Configuração inválida: {exc}")
            return
        config = await asyncio.to_thread(self.service.repository.config, interaction.guild.id)
        config.weekly_enabled = enabled
        config.weekly_weekday = weekday
        config.weekly_time = hour
        config.weekly_timezone = timezone_name
        await asyncio.to_thread(self.service.repository.save_config, config)
        await _reply(interaction, "Agendamento semanal atualizado.")


class PartnerMonthlyScheduleModal(discord.ui.Modal, title="Agendamento mensal"):
    def __init__(self, service: PartnerAnalyticsService, panel_id: int, config: PartnerAnalyticsConfig) -> None:
        super().__init__()
        self.service = service
        self.panel_id = panel_id
        self.enabled = discord.ui.TextInput(label="Ativo?", default="sim" if config.monthly_enabled else "não", max_length=3)
        self.day = discord.ui.TextInput(label="Dia do mês", default=str(config.monthly_day), max_length=2)
        self.hour = discord.ui.TextInput(label="Horário", default=config.monthly_time, max_length=5)
        self.timezone_name = discord.ui.TextInput(label="Fuso horário", default=config.monthly_timezone, max_length=80)
        self.policy = discord.ui.TextInput(label="Mês curto: last_day ou skip", default=config.monthly_short_month_policy, max_length=8)
        for item in (self.enabled, self.day, self.hour, self.timezone_name, self.policy):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not await self.service.authorized(interaction.user, self.panel_id):
            return
        try:
            enabled = _parse_bool(self.enabled.value)
            day = int(self.day.value)
            if day not in range(1, 32):
                raise ValueError("O dia deve estar entre 1 e 31.")
            hour = _normalize_time(self.hour.value)
            timezone_name = _timezone(self.timezone_name.value.strip()).key
            policy = self.policy.value.strip().casefold()
            if policy not in {"last_day", "skip"}:
                raise ValueError("Use last_day ou skip para meses curtos.")
        except (ValueError, ZoneInfoNotFoundError) as exc:
            await _reply(interaction, f"Configuração inválida: {exc}")
            return
        config = await asyncio.to_thread(self.service.repository.config, interaction.guild.id)
        config.monthly_enabled = enabled
        config.monthly_day = day
        config.monthly_time = hour
        config.monthly_timezone = timezone_name
        config.monthly_short_month_policy = policy
        await asyncio.to_thread(self.service.repository.save_config, config)
        await _reply(interaction, "Agendamento mensal atualizado.")


class PartnerAnnualScheduleModal(discord.ui.Modal, title="Agendamento anual"):
    def __init__(self, service: PartnerAnalyticsService, panel_id: int, config: PartnerAnalyticsConfig) -> None:
        super().__init__()
        self.service = service
        self.panel_id = panel_id
        self.enabled = discord.ui.TextInput(
            label="Ativo?",
            default="sim" if config.annual_enabled else "não",
            max_length=3,
        )
        self.date = discord.ui.TextInput(
            label="Data anual (MM-DD)",
            default=f"{config.annual_month:02d}-{config.annual_day:02d}",
            max_length=5,
        )
        self.hour = discord.ui.TextInput(label="Horário", default=config.annual_time, max_length=5)
        self.timezone_name = discord.ui.TextInput(
            label="Fuso horário",
            default=config.annual_timezone,
            max_length=80,
        )
        self.policy = discord.ui.TextInput(
            label="Data inválida: last_day ou skip",
            default=config.annual_short_month_policy,
            max_length=8,
        )
        for item in (self.enabled, self.date, self.hour, self.timezone_name, self.policy):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not await self.service.authorized(interaction.user, self.panel_id):
            return
        try:
            enabled = _parse_bool(self.enabled.value)
            month_text, day_text = self.date.value.strip().split("-", 1)
            month = int(month_text)
            day = int(day_text)
            if month not in range(1, 13):
                raise ValueError("O mês deve estar entre 1 e 12.")
            if day not in range(1, 32):
                raise ValueError("O dia deve estar entre 1 e 31.")
            hour = _normalize_time(self.hour.value)
            timezone_name = _timezone(self.timezone_name.value.strip()).key
            policy = self.policy.value.strip().casefold()
            if policy not in {"last_day", "skip"}:
                raise ValueError("Use last_day ou skip para datas inválidas.")
        except (ValueError, ZoneInfoNotFoundError) as exc:
            await _reply(interaction, f"Configuração inválida: {exc}")
            return
        config = await asyncio.to_thread(self.service.repository.config, interaction.guild.id)
        config.annual_enabled = enabled
        config.annual_month = month
        config.annual_day = day
        config.annual_time = hour
        config.annual_timezone = timezone_name
        config.annual_short_month_policy = policy
        await asyncio.to_thread(self.service.repository.save_config, config)
        await _reply(interaction, "Agendamento anual atualizado.")


class PartnerAnalyticsChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, parent: "PartnerAnalyticsChannelView") -> None:
        super().__init__(channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not await self.parent_view.service.authorized(interaction.user, self.parent_view.panel_id):
            return
        selected = self.values[0]
        channel_id = int(selected.id)
        channel = interaction.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel) or not _channel_is_private(channel):
            await _reply(
                interaction,
                "Escolha um canal privado, sem acesso de visualização para o @everyone.",
            )
            return
        config = await asyncio.to_thread(self.parent_view.service.repository.config, interaction.guild.id)
        if self.parent_view.report_type == "weekly":
            config.weekly_channel_id = channel_id
        elif self.parent_view.report_type == "monthly":
            config.monthly_channel_id = channel_id
        else:
            config.annual_channel_id = channel_id
        await asyncio.to_thread(self.parent_view.service.repository.save_config, config)
        await _reply(interaction, f"Canal {self.parent_view.report_type} atualizado para <#{channel_id}>.")


class PartnerAnalyticsChannelView(_AuthorizedAnalyticsView):
    def __init__(self, service: PartnerAnalyticsService, panel_id: int, report_type: str) -> None:
        super().__init__(service, panel_id)
        self.report_type = report_type
        self.add_item(PartnerAnalyticsChannelSelect(self))


class PartnerAnalyticsHistorySelect(discord.ui.Select):
    def __init__(self, parent: "PartnerAnalyticsHistoryV2View", reports: list[dict[str, Any]]) -> None:
        options = [
            discord.SelectOption(
                label=str(item["period_label"])[:100],
                value=str(item["report_id"]),
                description=f"{item['report_type']} • {item['status']}"[:100],
            )
            for item in reports[:25]
        ]
        super().__init__(placeholder="Escolha um relatório", options=options)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.parent_view.interaction_check(interaction):
            return
        report = await asyncio.to_thread(self.parent_view.service.repository.report, self.values[0])
        if report is None:
            await _reply(interaction, "Relatório não encontrado.")
            return
        file, view = await self.parent_view.service.report_artifacts(report)
        await _reply(interaction, file=file, view=view)


class PartnerAnalyticsHistoryV2View(discord.ui.LayoutView):
    def __init__(self, service: PartnerAnalyticsService, panel_id: int, reports: list[dict[str, Any]]) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.panel_id = int(panel_id)
        container = discord.ui.Container(accent_color=discord.Color(PARTNER_ANALYTICS_COLOR))
        container.add_item(discord.ui.TextDisplay(history_markdown(reports)))
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        row = discord.ui.ActionRow()
        row.add_item(PartnerAnalyticsHistorySelect(self, reports))
        container.add_item(row)
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.service.authorized(interaction.user, self.panel_id):
            return True
        await _reply(interaction, "Você não pode acessar os relatórios comerciais.")
        return False


def build_comparison(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    paths = {
        "new_partners": ("partners", "new"),
        "new_applications": ("applications", "new"),
        "new_referrals": ("referrals", "new"),
        "converted_referrals": ("referrals", "converted"),
        "conversion_rate": ("referrals", "conversion_rate"),
        "contracts": ("projects", "contracts"),
        "revenue": ("projects", "revenue"),
        "commissions_generated": ("commissions", "generated"),
        "commissions_paid": ("commissions", "paid"),
    }
    result: dict[str, Any] = {"previous_period": previous.get("period", {})}
    for key, (section, field) in paths.items():
        current_value = current.get(section, {}).get(field)
        previous_value = previous.get(section, {}).get(field)
        variation = percentage_change(current_value, previous_value)
        result[key] = {
            "current": current_value,
            "previous": previous_value,
            "absolute": (
                float(current_value) - float(previous_value)
                if current_value is not None and previous_value is not None
                else None
            ),
            "percent": variation,
            "trend": _trend(current_value, previous_value),
        }
    return result


def deterministic_summary(indicators: dict[str, Any], comparison: dict[str, Any]) -> str:
    partners = indicators.get("partners", {})
    applications = indicators.get("applications", {})
    referrals = indicators.get("referrals", {})
    projects = indicators.get("projects", {})
    commissions = indicators.get("commissions", {})
    new_referrals = int(referrals.get("new") or 0)
    qualified = int(referrals.get("qualified") or 0)
    converted = int(referrals.get("converted") or 0)
    contracts = int(projects.get("contracts") or 0)
    revenue = float(projects.get("revenue") or 0)
    conversion_rate = referrals.get("conversion_rate")
    if new_referrals == 0:
        first = (
            "O Programa de Parceiros não registrou novas indicações no período. "
            f"Foram contabilizados **{contracts} {_plural(contracts, 'contrato fechado', 'contratos fechados')}** "
            f"e **{_money(revenue)} em receita atribuída**."
        )
    else:
        first = (
            f"O Programa de Parceiros registrou **{new_referrals} {_plural(new_referrals, 'nova indicação', 'novas indicações')}**, "
            f"sendo **{qualified} qualificadas e {converted} convertidas**, com taxa de conversão de "
            f"**{_percent_or_unavailable(conversion_rate)}**. O período encerrou com "
            f"**{contracts} {_plural(contracts, 'contrato fechado', 'contratos fechados')} e "
            f"{_money(revenue)} em receita atribuída**."
        )

    active = int(partners.get("active") or 0)
    new_partners = int(partners.get("new") or 0)
    new_applications = int(applications.get("new") or 0)
    generated = float(commissions.get("generated") or 0)
    pending = float(commissions.get("pending") or 0)
    paid = float(commissions.get("paid") or 0)
    commission_state = (
        "integralmente pendentes de pagamento"
        if generated > 0 and math.isclose(generated, pending, abs_tol=0.005)
        else "integralmente pagas"
        if generated > 0 and math.isclose(generated, paid, abs_tol=0.005)
        else f"com **{_money(pending)} pendentes** e **{_money(paid)} pagas**"
    )
    second = (
        f"Atualmente, o programa conta com **{active} {_plural(active, 'parceiro ativo', 'parceiros ativos')}**, "
        f"**{_count_text(new_partners, 'nova adesão', 'novas adesões', 'nenhuma nova adesão')}** e "
        f"**{_count_text(new_applications, 'inscrição recebida', 'inscrições recebidas', 'nenhuma inscrição recebida')}**. "
        f"Foram gerados **{_money(generated)} em comissões**, {commission_state}."
    )

    third = "Em comparação ao período anterior, " + _comparison_overview(comparison) + "."
    compared_items = (
        comparison.get("new_referrals", {}),
        comparison.get("converted_referrals", {}),
        comparison.get("revenue", {}),
    )
    if any(item.get("percent") is None for item in compared_items):
        third += " As variações percentuais sem base anterior válida não foram calculadas."

    top_referrals = indicators.get("top_referrals", [])
    top_conversions = indicators.get("top_conversions", [])
    if top_referrals:
        leader = top_referrals[0]
        leader_id = int(leader.get("partner_id") or 0)
        leader_referrals = int(leader.get("total") or 0)
        leader_conversions = next(
            (
                int(item.get("conversions") or 0)
                for item in top_conversions
                if int(item.get("partner_id") or 0) == leader_id
            ),
            0,
        )
        fourth = (
            f"O destaque do período foi <@{leader_id}>, responsável por "
            f"**{leader_referrals} {_plural(leader_referrals, 'indicação', 'indicações')} e "
            f"{leader_conversions} {_plural(leader_conversions, 'conversão', 'conversões')}**."
        )
    else:
        fourth = "O período ainda não possui movimentação suficiente para definir um parceiro em destaque."
    return "\n\n".join((first, second, third, fourth))


def render_partner_analytics(report: dict[str, Any]) -> io.BytesIO:
    width, height = 1600, 900
    image = Image.new("RGB", (width, height), "#08090B")
    draw = ImageDraw.Draw(image, "RGBA")
    _gradient_background(image)
    title_font = _font(45, bold=True)
    subtitle_font = _font(20)
    section_font = _font(24, bold=True)
    value_font = _font(34, bold=True)
    label_font = _font(16)
    small_font = _font(14)
    indicators = report.get("indicators", {})
    referrals = indicators.get("referrals", {})
    projects = indicators.get("projects", {})
    commissions = indicators.get("commissions", {})
    draw.text((70, 55), "BELLA PARTNER ANALYTICS", font=title_font, fill="#F5F6F8")
    period_text = str(report.get("period_label") or "Período não informado")
    if report.get("partial") and "parcial" not in period_text.casefold():
        period_text += " • PARCIAL"
    draw.text((72, 115), period_text, font=subtitle_font, fill="#C58A42")
    kpis = (
        ("NOVAS INDICAÇÕES", str(int(referrals.get("new") or 0)), "#D99038"),
        ("CONVERSÕES", str(int(referrals.get("converted") or 0)), "#4BC38A"),
        ("RECEITA ATRIBUÍDA", _money(float(projects.get("revenue") or 0)), "#69A7FF"),
        ("COMISSÕES GERADAS", _money(float(commissions.get("generated") or 0)), "#C58A42"),
    )
    for index, (label, value, accent) in enumerate(kpis):
        left = 70 + index * 375
        _rounded_panel(draw, (left, 175, left + 340, 300), accent)
        draw.text((left + 24, 200), label, font=label_font, fill="#AEB3BD")
        _fit_text(draw, value, (left + 24, 234, left + 316, 286), value_font, "#F7F7F8")
    _rounded_panel(draw, (70, 335, 1010, 760), "#C27C2B")
    chart_title = "Evolução mensal" if str(report.get("report_type")) == "annual" else "Evolução diária"
    draw.text((98, 362), chart_title, font=section_font, fill="#F5F6F8")
    daily = indicators.get("daily", [])
    _line_chart(draw, daily, (105, 415, 975, 715), small_font)
    _rounded_panel(draw, (1040, 335, 1530, 760), "#4BC38A")
    distribution = indicators.get("status_distribution", {})
    if any(int(value or 0) > 0 for value in distribution.values()):
        draw.text((1068, 362), "Distribuição dos leads", font=section_font, fill="#F5F6F8")
        _status_bars(draw, distribution, (1070, 420, 1495, 705), label_font)
    else:
        draw.text((1068, 362), "Atividade comercial", font=section_font, fill="#F5F6F8")
        _commercial_summary(
            draw,
            projects,
            commissions,
            (1070, 420, 1495, 705),
            label_font,
        )
    comparison = report.get("comparison", {})
    trend = comparison.get("new_referrals", {}).get("trend", "unavailable")
    trend_label = {
        "up": "Tendência de indicações: crescimento",
        "down": "Tendência de indicações: redução",
        "stable": "Tendência de indicações: estabilidade",
    }.get(trend, "Tendência de indicações: sem base comparativa")
    draw.text((72, 800), trend_label, font=subtitle_font, fill="#D3D6DC")
    draw.text(
        (72, 844),
        f"Bella Partner Analytics • v{ANALYTICS_VERSION} • dados consolidados sem contatos pessoais",
        font=small_font,
        fill="#767B85",
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer


def report_v2_blocks(report: dict[str, Any]) -> tuple[str, ...]:
    indicators = report.get("indicators", {})
    comparison = report.get("comparison", {})
    summary = _compact_summary(report.get("summary"), limit=1200)
    if len(summary.split("\n\n")) != 4:
        summary = deterministic_summary(indicators, comparison)
    content = (
        "## Relatório do Programa de Parceiros\n"
        f"-# {_report_period_heading(report)}\n\n"
        f"{summary}\n\n"
        f"-# Snapshot imutável • Analytics {report.get('analytics_version', ANALYTICS_VERSION)}"
    )
    return (content[:4000],)


def report_markdown(report: dict[str, Any]) -> str:
    return "\n".join(report_v2_blocks(report))[:4000]


def history_markdown(reports: list[dict[str, Any]]) -> str:
    lines = [
        "## Histórico de relatórios",
        "Consulte snapshots imutáveis já gerados pelo Partner Analytics.",
        "",
    ]
    for item in reports[:10]:
        destination = (
            f"<#{item['destination_channel_id']}>"
            if item.get("destination_channel_id")
            else "não publicado"
        )
        lines.extend(
            (
                f"**{item['period_label']}**",
                f"`{item['report_type']}` • `{item['status']}` • {destination} • ID `{item['report_id']}`",
                "",
            )
        )
    lines.append("-# Os indicadores armazenados são snapshots imutáveis.")
    return "\n".join(lines)[:4000]


def report_embed(report: dict[str, Any], filename: str) -> discord.Embed:
    indicators = report.get("indicators", {})
    referrals = indicators.get("referrals", {})
    projects = indicators.get("projects", {})
    commissions = indicators.get("commissions", {})
    applications = indicators.get("applications", {})
    embed = discord.Embed(
        title="Relatório do Programa de Parceiros",
        description=str(report.get("summary") or "Resumo indisponível.")[:4096],
        color=discord.Color(PARTNER_ANALYTICS_COLOR),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Período de referência",
        value=(
            f"**{report.get('period_label')}**"
            + ("\n⚠️ Relatório parcial." if report.get("partial") else "\nPeríodo completo.")
        ),
        inline=False,
    )
    embed.add_field(
        name="Parceiros e inscrições",
        value=(
            f"Parceiros ativos: **{int(indicators.get('partners', {}).get('active') or 0)}**\n"
            f"Novos parceiros: **{int(indicators.get('partners', {}).get('new') or 0)}**\n"
            f"Novas inscrições: **{int(applications.get('new') or 0)}**\n"
            f"Taxa de aprovação: **{_percent_or_unavailable(applications.get('approval_rate'))}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Indicações",
        value=(
            f"Novas: **{int(referrals.get('new') or 0)}**\n"
            f"Qualificadas: **{int(referrals.get('qualified') or 0)}**\n"
            f"Convertidas: **{int(referrals.get('converted') or 0)}**\n"
            f"Conversão: **{_percent_or_unavailable(referrals.get('conversion_rate'))}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Resultado comercial",
        value=(
            f"Contratos fechados: **{int(projects.get('contracts') or 0)}**\n"
            f"Receita: **{_money(float(projects.get('revenue') or 0))}**\n"
            f"Comissões geradas: **{_money(float(commissions.get('generated') or 0))}**\n"
            f"Pendentes: **{_money(float(commissions.get('pending') or 0))}**\n"
            f"Pagas no período: **{_money(float(commissions.get('paid') or 0))}**"
        ),
        inline=False,
    )
    comparison = report.get("comparison", {})
    embed.add_field(
        name="Comparativo com o período anterior",
        value=(
            f"Indicações: **{_variation_label(comparison.get('new_referrals', {}))}**\n"
            f"Conversões: **{_variation_label(comparison.get('converted_referrals', {}))}**\n"
            f"Receita: **{_variation_label(comparison.get('revenue', {}), money=True)}**"
        ),
        inline=False,
    )
    referral_ranking = indicators.get("top_referrals", [])
    conversion_ranking = indicators.get("top_conversions", [])
    if referral_ranking or conversion_ranking:
        referral_lines = [
            f"<@{int(item['partner_id'])}> — {int(item.get('total') or 0)}"
            for item in referral_ranking[:3]
        ]
        conversion_lines = [
            f"<@{int(item['partner_id'])}> — {int(item.get('conversions') or 0)}"
            for item in conversion_ranking[:3]
        ]
        embed.add_field(
            name="Destaques do período",
            value=(
                "**Mais indicações**\n"
                + ("\n".join(referral_lines) or "Sem base disponível.")
                + "\n\n**Mais conversões**\n"
                + ("\n".join(conversion_lines) or "Sem base disponível.")
            ),
            inline=False,
        )
    embed.set_image(url=f"attachment://{filename}")
    embed.set_footer(
        text=(
            f"Snapshot imutável • Analytics {report.get('analytics_version', ANALYTICS_VERSION)} "
            f"• Relatório {report.get('report_id')}"
        )
    )
    return embed


def history_embed(reports: list[dict[str, Any]]) -> discord.Embed:
    lines = []
    for item in reports[:10]:
        destination = (
            f"<#{item['destination_channel_id']}>" if item.get("destination_channel_id") else "não publicado"
        )
        lines.append(
            f"`{item['report_id']}` • **{item['period_label']}**\n"
            f"Tipo: `{item['report_type']}` • Status: `{item['status']}` • Destino: {destination}"
        )
    embed = discord.Embed(
        title="Histórico de relatórios",
        description="\n\n".join(lines)[:4096],
        color=discord.Color(PARTNER_ANALYTICS_COLOR),
    )
    embed.set_footer(text="Os indicadores armazenados são snapshots imutáveis.")
    return embed


def _period_from_dates(
    start_date: date,
    end_date_exclusive: date,
    timezone_name: str,
    *,
    partial: bool,
) -> AnalyticsPeriod:
    zone = _timezone(timezone_name)
    start = datetime.combine(start_date, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(end_date_exclusive, time.min, zone).astimezone(timezone.utc)
    label = f"{start_date:%d/%m/%Y} a {(end_date_exclusive - timedelta(days=1)):%d/%m/%Y}"
    return AnalyticsPeriod(
        start=start,
        end=end,
        label=label,
        partial=partial,
        timezone_name=timezone_name,
    )


def _timezone(value: str) -> ZoneInfo:
    return ZoneInfo(value or DEFAULT_TIMEZONE)


def _channel_is_private(channel: discord.TextChannel) -> bool:
    permissions = channel.permissions_for(channel.guild.default_role)
    return not permissions.view_channel


def _parse_time(value: str) -> time:
    return datetime.strptime(_normalize_time(value), "%H:%M").time()


def _normalize_time(value: str) -> str:
    parsed = datetime.strptime(value.strip(), "%H:%M")
    return parsed.strftime("%H:%M")


def _parse_bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"sim", "s", "1", "true", "ativo"}:
        return True
    if normalized in {"não", "nao", "n", "0", "false", "inativo"}:
        return False
    raise ValueError("Use sim ou não.")


def _month_label(value: date) -> str:
    months = (
        "janeiro", "fevereiro", "março", "abril", "maio", "junho",
        "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
    )
    return f"{months[value.month - 1]} de {value.year}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_stale(value: str, *, minutes: int) -> bool:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - moment.astimezone(timezone.utc) >= timedelta(minutes=minutes)


def _numbers(row: sqlite3.Row | None) -> dict[str, float | int]:
    if row is None:
        return {}
    result: dict[str, float | int] = {}
    for key in row.keys():
        value = row[key]
        if isinstance(value, float):
            result[str(key)] = float(value or 0)
        else:
            result[str(key)] = int(value or 0)
    return result


def _daily_series(
    period: AnalyticsPeriod,
    referral_rows: Iterable[sqlite3.Row],
    project_rows: Iterable[sqlite3.Row],
) -> list[dict[str, Any]]:
    zone = _timezone(period.timezone_name)
    if period.days > 62:
        return _monthly_series(period, referral_rows, project_rows, zone)
    referrals: dict[str, int] = {}
    projects: dict[str, int] = {}
    for row in referral_rows:
        key = _local_day(str(row["created_at"]), zone)
        referrals[key] = referrals.get(key, 0) + 1
    for row in project_rows:
        if str(row["status"] or "") not in {"closed", "fechado"}:
            continue
        key = _local_day(str(row["created_at"]), zone)
        projects[key] = projects.get(key, 0) + 1
    current = period.start.astimezone(zone).date()
    end = period.end.astimezone(zone).date()
    result = []
    while current < end and len(result) < 62:
        key = current.isoformat()
        result.append(
            {
                "date": key,
                "label": current.strftime("%d/%m"),
                "referrals": referrals.get(key, 0),
                "conversions": projects.get(key, 0),
            }
        )
        current += timedelta(days=1)
    return result


def _monthly_series(
    period: AnalyticsPeriod,
    referral_rows: Iterable[sqlite3.Row],
    project_rows: Iterable[sqlite3.Row],
    zone: ZoneInfo,
) -> list[dict[str, Any]]:
    referrals: dict[str, int] = {}
    projects: dict[str, int] = {}
    for row in referral_rows:
        key = _local_month(str(row["created_at"]), zone)
        referrals[key] = referrals.get(key, 0) + 1
    for row in project_rows:
        if str(row["status"] or "") not in {"closed", "fechado"}:
            continue
        key = _local_month(str(row["created_at"]), zone)
        projects[key] = projects.get(key, 0) + 1
    current = period.start.astimezone(zone).date().replace(day=1)
    end = period.end.astimezone(zone).date()
    result = []
    while current < end and len(result) < 24:
        key = current.strftime("%Y-%m")
        result.append(
            {
                "date": key,
                "label": f"{_month_label(current).split()[0][:3]}/{str(current.year)[2:]}",
                "referrals": referrals.get(key, 0),
                "conversions": projects.get(key, 0),
            }
        )
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    return result


def _local_day(value: str, zone: ZoneInfo) -> str:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(zone).date().isoformat()


def _local_month(value: str, zone: ZoneInfo) -> str:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(zone).strftime("%Y-%m")


def _report_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    result["partial"] = bool(result.get("partial"))
    for key in ("indicators_json", "comparison_json"):
        try:
            result[key.removesuffix("_json")] = json.loads(str(result.get(key) or "{}"))
        except json.JSONDecodeError:
            result[key.removesuffix("_json")] = {}
        result.pop(key, None)
    return result


def _trend(current: Any, previous: Any) -> str:
    if current is None or previous is None:
        return "unavailable"
    current_value = float(current)
    previous_value = float(previous)
    if math.isclose(current_value, previous_value, abs_tol=1e-9):
        return "stable"
    return "up" if current_value > previous_value else "down"


def _summary_payload(indicators: dict[str, Any]) -> dict[str, Any]:
    return {
        key: indicators.get(key, {})
        for key in (
            "period",
            "partners",
            "applications",
            "referrals",
            "projects",
            "commissions",
            "top_referrals",
            "top_conversions",
        )
    }


def _compact_summary(value: Any, limit: int = 1200) -> str:
    raw_blocks = re.split(r"\n\s*\n", str(value or "").strip())
    blocks: list[str] = []
    for raw_block in raw_blocks:
        lines = [line.strip() for line in raw_block.splitlines() if line.strip()]
        if not lines:
            continue
        if not blocks and lines[0].lstrip("# ").casefold().startswith("resumo executivo"):
            lines.pop(0)
        text = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if text:
            blocks.append(text)
        if len(blocks) == 4:
            break
    text = "\n\n".join(blocks)
    if len(text) <= limit:
        return text
    shortened = text[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return shortened + "."


def _report_period_heading(report: dict[str, Any]) -> str:
    label = str(report.get("period_label") or "Período não informado")
    report_type = str(report.get("report_type") or "custom")
    partial = bool(report.get("partial"))
    type_label = {
        "weekly": "Acompanhamento semanal parcial" if partial else "Fechamento semanal",
        "monthly": "Acompanhamento mensal parcial" if partial else "Fechamento mensal",
        "annual": "Acompanhamento anual parcial" if partial else "Fechamento anual",
        "custom": "Período personalizado",
    }.get(report_type, "Resumo do período")
    if report_type == "monthly" and label:
        label = label[0].upper() + label[1:]
    return f"{label} • {type_label}"


def _plural(value: int, singular: str, plural: str) -> str:
    return singular if value == 1 else plural


def _count_text(value: int, singular: str, plural: str, zero: str) -> str:
    if value == 0:
        return zero
    return f"{value} {_plural(value, singular, plural)}"


def _comparison_overview(comparison: dict[str, Any]) -> str:
    entries = (
        (comparison.get("new_referrals", {}), "indicação", "indicações", False),
        (comparison.get("converted_referrals", {}), "conversão", "conversões", False),
        (comparison.get("revenue", {}), "receita", "receita", True),
    )
    available = [entry for entry in entries if entry[0].get("absolute") is not None]
    if not available:
        return "não há base válida para comparar os resultados"
    differences = [float(entry[0]["absolute"]) for entry in available]
    nonzero = [value for value in differences if not math.isclose(value, 0.0, abs_tol=0.005)]
    if not nonzero:
        return "indicações, conversões e receita permaneceram estáveis"
    same_direction = len(nonzero) == len(differences) and (
        all(value > 0 for value in nonzero) or all(value < 0 for value in nonzero)
    )
    if same_direction:
        direction = "crescimento" if nonzero[0] > 0 else "redução"
        values = [
            _comparison_value(abs(float(item.get("absolute") or 0)), singular, plural, money)
            for item, singular, plural, money in available
        ]
        return f"houve {direction} de " + _join_natural(values)
    fragments = [
        _comparison_fragment(item, singular, plural, money=money)
        for item, singular, plural, money in available
    ]
    return _join_natural([fragment for fragment in fragments if fragment])


def _comparison_value(value: float, singular: str, plural: str, money: bool) -> str:
    if money:
        return f"**{_money(value)} em {singular}**"
    integer = int(round(value))
    return f"**{integer} {_plural(integer, singular, plural)}**"


def _join_natural(values: list[str]) -> str:
    if not values:
        return "sem alterações mensuráveis"
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + " e " + values[-1]


def _comparison_fragment(
    item: dict[str, Any],
    singular: str,
    plural: str,
    *,
    money: bool = False,
) -> str:
    absolute = item.get("absolute")
    if absolute is None:
        return ""
    difference = float(absolute)
    if math.isclose(difference, 0.0, abs_tol=0.005):
        return f"a {singular} permaneceu estável" if money else f"{plural} permaneceram estáveis"
    magnitude = abs(difference)
    if money:
        direction = "cresceu" if difference > 0 else "diminuiu"
        value = _money(magnitude)
        return f"a {singular} {direction} **{value}**"
    direction = "cresceram" if difference > 0 else "diminuíram"
    integer = int(round(magnitude))
    return f"{plural} {direction} em **{integer}**"


def _money(value: float) -> str:
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


def _percent_or_unavailable(value: Any) -> str:
    if value is None:
        return "indisponível"
    return f"{float(value):.1f}%".replace(".", ",")


def _variation_label(item: dict[str, Any], *, money: bool = False) -> str:
    absolute = item.get("absolute")
    percent = item.get("percent")
    trend = str(item.get("trend") or "unavailable")
    if absolute is None:
        return "comparação indisponível"
    prefix = "+" if float(absolute) > 0 else ""
    absolute_text = _money(float(absolute)) if money else f"{prefix}{int(absolute)}"
    if money and float(absolute) > 0:
        absolute_text = "+" + absolute_text
    if percent is None:
        return f"{absolute_text} • sem percentual válido"
    arrow = {"up": "↑", "down": "↓", "stable": "→"}.get(trend, "•")
    percent_text = f"{float(percent):+.1f}".replace(".", ",")
    return f"{arrow} {absolute_text} ({percent_text}%)"


async def _reply(
    interaction: discord.Interaction,
    content: str | None = None,
    **kwargs: Any,
) -> None:
    kwargs.setdefault("ephemeral", True)
    if interaction.response.is_done():
        await interaction.followup.send(content, **kwargs)
    else:
        await interaction.response.send_message(content, **kwargs)


async def _find_existing_publication(
    channel: discord.TextChannel,
    report_id: str,
) -> discord.Message | None:
    marker = f"Relatório {report_id}"
    try:
        async for message in channel.history(limit=25):
            legacy_embed = any(
                marker in str(getattr(getattr(embed, "footer", None), "text", "") or "")
                for embed in message.embeds
            )
            components_v2 = any(
                _component_contains_text(component, marker)
                for component in getattr(message, "components", ())
            )
            if legacy_embed or components_v2:
                return message
    except (discord.Forbidden, discord.HTTPException, AttributeError, TypeError):
        return None
    return None


def _component_contains_text(component: Any, marker: str) -> bool:
    if marker in str(getattr(component, "content", "") or ""):
        return True
    return any(
        _component_contains_text(child, marker)
        for child in getattr(component, "children", ())
    )


def _gradient_background(image: Image.Image) -> None:
    pixels = image.load()
    width, height = image.size
    for y in range(height):
        for x in range(width):
            glow = max(0.0, 1.0 - math.hypot(x - width, y) / 1200.0)
            pixels[x, y] = (
                int(8 + 15 * glow),
                int(9 + 9 * glow),
                int(11 + 2 * glow),
            )


def _rounded_panel(draw: ImageDraw.ImageDraw, bounds: tuple[int, int, int, int], accent: str) -> None:
    draw.rounded_rectangle(bounds, radius=24, fill="#101114", outline="#2B2D32", width=2)
    draw.rounded_rectangle((bounds[0] + 1, bounds[1] + 1, bounds[0] + 8, bounds[3] - 1), radius=4, fill=accent)


def _line_chart(
    draw: ImageDraw.ImageDraw,
    rows: list[dict[str, Any]],
    bounds: tuple[int, int, int, int],
    font: ImageFont.ImageFont,
) -> None:
    left, top, right, bottom = bounds
    draw.line((left, bottom, right, bottom), fill="#3A3D44", width=2)
    maximum = max(
        1,
        max(
            (
                max(
                    int(item.get("referrals") or 0),
                    int(item.get("conversions") or 0),
                )
                for item in rows
            ),
            default=0,
        ),
    )
    has_activity = any(
        int(item.get("referrals") or 0) or int(item.get("conversions") or 0)
        for item in rows
    )
    if not rows or not has_activity:
        draw.text((left + 20, top + 100), "Sem movimentação no período", font=_font(22), fill="#868B95")
        return
    step = (right - left) / max(1, len(rows) - 1)
    referral_points = []
    conversion_points = []
    for index, item in enumerate(rows):
        x = left + index * step
        referral_points.append((x, bottom - (int(item.get("referrals") or 0) / maximum) * (bottom - top - 20)))
        conversion_points.append((x, bottom - (int(item.get("conversions") or 0) / maximum) * (bottom - top - 20)))
    if len(referral_points) == 1:
        x, y = referral_points[0]
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill="#D99038")
    else:
        draw.line(referral_points, fill="#D99038", width=5, joint="curve")
        draw.line(conversion_points, fill="#4BC38A", width=4, joint="curve")
    indexes = sorted({0, len(rows) // 2, len(rows) - 1})
    for index in indexes:
        x = left + index * step
        draw.text((x - 20, bottom + 12), str(rows[index].get("label") or ""), font=font, fill="#777C86")
    draw.text((left, top - 26), "Indicações", font=font, fill="#D99038")
    draw.text((left + 120, top - 26), "Conversões", font=font, fill="#4BC38A")


def _status_bars(
    draw: ImageDraw.ImageDraw,
    distribution: dict[str, Any],
    bounds: tuple[int, int, int, int],
    font: ImageFont.ImageFont,
) -> None:
    labels = (
        ("submitted", "Enviadas", "#69707D"),
        ("reviewing", "Em análise", "#69A7FF"),
        ("accepted", "Aceitas", "#58B8D6"),
        ("negotiation", "Negociação", "#D99038"),
        ("closed", "Fechadas", "#4BC38A"),
        ("completed", "Finalizadas", "#7DDFAE"),
        ("rejected", "Perdidas", "#D75B62"),
    )
    available = [(key, label, color, int(distribution.get(key) or 0)) for key, label, color in labels]
    available = [item for item in available if item[3] > 0]
    left, top, right, bottom = bounds
    if not available:
        draw.text((left + 20, top + 100), "Sem leads no período", font=_font(22), fill="#868B95")
        return
    maximum = max(item[3] for item in available)
    row_height = min(44, (bottom - top) // len(available))
    for index, (_, label, color, value) in enumerate(available):
        y = top + index * row_height
        draw.text((left, y), label, font=font, fill="#C7CBD2")
        bar_left = left + 125
        bar_width = int((right - bar_left - 45) * (value / maximum))
        draw.rounded_rectangle((bar_left, y + 2, right - 45, y + 19), radius=8, fill="#24272C")
        draw.rounded_rectangle((bar_left, y + 2, bar_left + max(8, bar_width), y + 19), radius=8, fill=color)
        draw.text((right - 32, y), str(value), font=font, fill="#F1F2F4")


def _commercial_summary(
    draw: ImageDraw.ImageDraw,
    projects: dict[str, Any],
    commissions: dict[str, Any],
    bounds: tuple[int, int, int, int],
    font: ImageFont.ImageFont,
) -> None:
    left, top, right, _ = bounds
    rows = (
        ("Contratos fechados", str(int(projects.get("contracts") or 0)), "#4BC38A"),
        ("Em negociação", str(int(projects.get("negotiations") or 0)), "#D99038"),
        ("Comissões pendentes", _money(float(commissions.get("pending") or 0)), "#69A7FF"),
        ("Comissões pagas", _money(float(commissions.get("paid") or 0)), "#7DDFAE"),
    )
    for index, (label, value, color) in enumerate(rows):
        y = top + index * 62
        draw.text((left, y), label, font=font, fill="#AEB3BD")
        draw.rounded_rectangle((left, y + 25, right, y + 48), radius=10, fill="#24272C")
        draw.rounded_rectangle((left, y + 25, left + 12, y + 48), radius=6, fill=color)
        value_width = draw.textbbox((0, 0), value, font=font)[2]
        draw.text((right - value_width - 12, y), value, font=font, fill="#F1F2F4")


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    bounds: tuple[int, int, int, int],
    font: ImageFont.ImageFont,
    fill: str,
) -> None:
    left, top, right, _ = bounds
    selected = font
    size = getattr(font, "size", 34)
    while draw.textbbox((0, 0), text, font=selected)[2] > right - left and size > 18:
        size -= 2
        selected = _font(size, bold=True)
    draw.text((left, top), text, font=selected, fill=fill)


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()
