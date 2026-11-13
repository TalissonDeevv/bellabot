from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import discord

from website_guard.partner_analytics import (
    AnalyticsPeriod,
    PartnerAnalyticsConfig,
    PartnerAnalyticsRepository,
    PartnerAnalyticsService,
    PARTNER_ANALYTICS_CHANNEL,
    _channel_is_private,
    _compact_summary,
    build_comparison,
    custom_period,
    current_partial_year,
    deterministic_summary,
    effective_monthly_day,
    last_complete_month,
    last_complete_week,
    last_complete_year,
    percentage_change,
    previous_report_period,
    render_partner_analytics,
    report_markdown,
    schedule_is_due,
)
from website_guard.central_logs import GuildLogConfig, panel_channel_name
from website_guard.partner_program import PartnerProgramDatabase
from website_guard.partners import PartnerDatabase


class FailingAI:
    async def generate_text(self, *args, **kwargs):
        del args, kwargs
        raise RuntimeError("IA indisponível no teste")


class FakeLogs:
    async def channel_ids(self, guild_id: int) -> dict[str, int]:
        del guild_id
        return {}


class PartnerAnalyticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        directory = Path(self.temp.name)
        self.partners_path = directory / "partners.db"
        self.applications_path = directory / "partner_program.db"
        PartnerDatabase(self.partners_path)
        PartnerProgramDatabase(self.applications_path)
        self.repository = PartnerAnalyticsRepository(
            self.partners_path,
            self.applications_path,
        )
        self.guild_id = 10
        self.period = custom_period(
            date(2026, 10, 1),
            date(2026, 10, 7),
            "UTC",
        )
        self._seed()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_financial_reports_require_private_channel(self) -> None:
        guild = SimpleNamespace(default_role=object())
        private_channel = SimpleNamespace(
            guild=guild,
            permissions_for=lambda role: SimpleNamespace(view_channel=False),
        )
        public_channel = SimpleNamespace(
            guild=guild,
            permissions_for=lambda role: SimpleNamespace(view_channel=True),
        )
        self.assertTrue(_channel_is_private(private_channel))
        self.assertFalse(_channel_is_private(public_channel))

    def _seed(self) -> None:
        with sqlite3.connect(self.partners_path) as connection:
            connection.executemany(
                """
                INSERT INTO partners
                (guild_id, partner_id, role_id, status, joined_at, removed_at, notes)
                VALUES (?, ?, NULL, ?, ?, ?, '')
                """,
                (
                    (self.guild_id, 101, "active", "2026-10-02T10:00:00+00:00", None),
                    (self.guild_id, 102, "active", "2026-09-01T10:00:00+00:00", None),
                    (self.guild_id, 103, "inactive", "2026-08-01T10:00:00+00:00", "2026-10-03T10:00:00+00:00"),
                ),
            )
            connection.executemany(
                """
                INSERT INTO partner_referrals
                (referral_id, guild_id, partner_id, contact_name, contact_method,
                 need, context, status, created_at, updated_at, contact_type)
                VALUES (?, ?, ?, 'Contato', 'oculto', 'Projeto', '', ?, ?, ?, 'email')
                """,
                (
                    ("r1", self.guild_id, 101, "submitted", "2026-10-01T12:00:00+00:00", "2026-10-01T12:00:00+00:00"),
                    ("r2", self.guild_id, 101, "accepted", "2026-10-02T12:00:00+00:00", "2026-10-03T12:00:00+00:00"),
                    ("r3", self.guild_id, 102, "completed", "2026-10-03T12:00:00+00:00", "2026-10-05T12:00:00+00:00"),
                    ("r4", self.guild_id, 102, "rejected", "2026-10-04T12:00:00+00:00", "2026-10-05T12:00:00+00:00"),
                ),
            )
            connection.executemany(
                """
                INSERT INTO partner_projects
                (project_id, guild_id, partner_id, member_id, name, status,
                 revenue, notes, created_at, updated_at)
                VALUES (?, ?, ?, NULL, ?, ?, ?, '', ?, ?)
                """,
                (
                    ("p1", self.guild_id, 102, "Projeto A", "closed", 5000, "2026-10-05T12:00:00+00:00", "2026-10-05T12:00:00+00:00"),
                    ("p2", self.guild_id, 101, "Projeto B", "negotiation", 1000, "2026-10-06T12:00:00+00:00", "2026-10-06T12:00:00+00:00"),
                ),
            )
            connection.executemany(
                """
                INSERT INTO partner_commissions
                (commission_id, guild_id, partner_id, project_id, amount, status,
                 description, created_at, paid_at)
                VALUES (?, ?, ?, ?, ?, ?, '', ?, ?)
                """,
                (
                    ("c1", self.guild_id, 102, "p1", 2500, "pending", "2026-10-05T12:00:00+00:00", None),
                    ("c2", self.guild_id, 101, None, 1000, "paid", "2026-10-02T12:00:00+00:00", "2026-10-07T12:00:00+00:00"),
                ),
            )
        with sqlite3.connect(self.applications_path) as connection:
            connection.executemany(
                """
                INSERT INTO partner_applications
                (id, guild_id, panel_id, discord_user_id, ticket_id, status,
                 memory_json, started_at, completed_at, reviewed_at, reviewed_by,
                 created_at, updated_at)
                VALUES (?, ?, NULL, ?, NULL, ?, '{}', ?, ?, ?, NULL, ?, ?)
                """,
                (
                    ("A1", self.guild_id, 201, "approved", "2026-10-01T10:00:00+00:00", "2026-10-02T10:00:00+00:00", "2026-10-02T10:00:00+00:00", "2026-10-01T10:00:00+00:00", "2026-10-02T10:00:00+00:00"),
                    ("A2", self.guild_id, 202, "rejected", "2026-10-03T10:00:00+00:00", "2026-10-04T10:00:00+00:00", "2026-10-04T10:00:00+00:00", "2026-10-03T10:00:00+00:00", "2026-10-04T10:00:00+00:00"),
                    ("A3", self.guild_id, 203, "under_review", "2026-10-05T10:00:00+00:00", None, None, "2026-10-05T10:00:00+00:00", "2026-10-05T10:00:00+00:00"),
                ),
            )

    def test_calculates_real_partner_indicators(self) -> None:
        metrics = self.repository.collect(self.guild_id, self.period)

        self.assertEqual(metrics["partners"]["approved"], 3)
        self.assertEqual(metrics["partners"]["active"], 2)
        self.assertEqual(metrics["partners"]["commercially_active"], 2)
        self.assertEqual(metrics["applications"]["new"], 3)
        self.assertEqual(metrics["applications"]["approval_rate"], 50.0)
        self.assertEqual(metrics["referrals"]["new"], 4)
        self.assertEqual(metrics["referrals"]["qualified"], 2)
        self.assertEqual(metrics["referrals"]["converted"], 1)
        self.assertEqual(metrics["referrals"]["conversion_rate"], 25.0)
        self.assertEqual(metrics["projects"]["contracts"], 1)
        self.assertEqual(metrics["projects"]["revenue"], 5000.0)
        self.assertEqual(metrics["commissions"]["generated"], 3500.0)
        self.assertEqual(metrics["commissions"]["pending"], 2500.0)
        self.assertEqual(metrics["commissions"]["paid"], 1000.0)

    def test_missing_denominator_is_unavailable_not_fake_zero(self) -> None:
        empty = custom_period(date(2026, 11, 1), date(2026, 11, 7), "UTC")
        metrics = self.repository.collect(self.guild_id, empty)

        self.assertIsNone(metrics["referrals"]["conversion_rate"])
        self.assertIsNone(metrics["applications"]["approval_rate"])

    def test_percentage_change_handles_zero_without_infinity(self) -> None:
        self.assertIsNone(percentage_change(5, 0))
        self.assertEqual(percentage_change(0, 0), 0.0)
        self.assertEqual(percentage_change(15, 10), 50.0)

    def test_week_month_and_year_periods_use_complete_calendar_periods(self) -> None:
        now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)

        weekly = last_complete_week(now, "America/Recife")
        monthly = last_complete_month(now, "America/Recife")
        annual = last_complete_year(now, "America/Recife")
        partial_annual = current_partial_year(now, "America/Recife")

        self.assertEqual(weekly.label, "05/10/2026 a 11/10/2026")
        self.assertEqual(monthly.label, "setembro de 2026")
        self.assertEqual(annual.label, "2025")
        self.assertEqual(partial_annual.label, "2026")
        self.assertTrue(partial_annual.partial)
        self.assertEqual(weekly.timezone_name, "America/Recife")

        previous_month = previous_report_period(monthly, "monthly")
        self.assertEqual(previous_month.label, "01/08/2026 a 31/08/2026")
        previous_year = previous_report_period(annual, "annual")
        self.assertEqual(previous_year.label, "2024")

    def test_short_month_policy(self) -> None:
        self.assertEqual(effective_monthly_day(2026, 2, 31, "last_day"), 28)
        self.assertIsNone(effective_monthly_day(2026, 2, 31, "skip"))

    def test_schedule_respects_timezone_and_short_month(self) -> None:
        config = PartnerAnalyticsConfig(
            guild_id=self.guild_id,
            weekly_enabled=True,
            weekly_weekday=0,
            weekly_time="09:00",
            weekly_timezone="America/Recife",
            monthly_enabled=True,
            monthly_day=31,
            monthly_time="09:00",
            monthly_timezone="America/Recife",
            monthly_short_month_policy="last_day",
            annual_enabled=True,
            annual_month=10,
            annual_day=12,
            annual_time="09:00",
            annual_timezone="America/Recife",
        )

        monday = datetime(2026, 10, 12, 12, 5, tzinfo=timezone.utc)
        february_last_day = datetime(2026, 2, 28, 12, 5, tzinfo=timezone.utc)

        self.assertTrue(schedule_is_due(config, "weekly", monday))
        self.assertTrue(schedule_is_due(config, "monthly", february_last_day))
        self.assertTrue(schedule_is_due(config, "annual", monday))

    def test_schedule_configuration_persists(self) -> None:
        config = PartnerAnalyticsConfig(
            guild_id=self.guild_id,
            analytics_channel_id=450,
            panel_message_id=451,
            weekly_enabled=True,
            weekly_weekday=4,
            weekly_time="18:30",
            weekly_timezone="America/Recife",
            weekly_channel_id=500,
            monthly_enabled=True,
            monthly_day=31,
            monthly_time="08:15",
            monthly_timezone="America/Sao_Paulo",
            monthly_channel_id=600,
            monthly_short_month_policy="skip",
            annual_enabled=True,
            annual_month=2,
            annual_day=28,
            annual_time="10:30",
            annual_timezone="America/Recife",
            annual_channel_id=700,
        )

        self.repository.save_config(config)
        restored = self.repository.config(self.guild_id)

        self.assertTrue(restored.weekly_enabled)
        self.assertEqual(restored.analytics_channel_id, 450)
        self.assertEqual(restored.panel_message_id, 451)
        self.assertEqual(restored.weekly_channel_id, 500)
        self.assertTrue(restored.monthly_enabled)
        self.assertEqual(restored.monthly_channel_id, 600)
        self.assertEqual(restored.monthly_short_month_policy, "skip")
        self.assertTrue(restored.annual_enabled)
        self.assertEqual(restored.annual_month, 2)
        self.assertEqual(restored.annual_day, 28)
        self.assertEqual(restored.annual_channel_id, 700)

    def test_analytics_channel_uses_central_logs_visual_pattern(self) -> None:
        config = GuildLogConfig(guild_id=self.guild_id, use_emojis=True, divider="╺╸")
        self.assertEqual(
            panel_channel_name(PARTNER_ANALYTICS_CHANNEL, config),
            "📈╺╸parceiros-analytics",
        )

    def test_comparison_and_deterministic_summary(self) -> None:
        current = self.repository.collect(self.guild_id, self.period)
        previous = self.repository.collect(
            self.guild_id,
            custom_period(date(2026, 9, 24), date(2026, 9, 30), "UTC"),
        )
        comparison = build_comparison(current, previous)
        summary = deterministic_summary(current, comparison)

        self.assertEqual(comparison["new_referrals"]["trend"], "up")
        self.assertIsNone(comparison["new_referrals"]["percent"])
        self.assertIn("4 novas indicações", summary)
        self.assertNotIn("oculto", summary)
        self.assertEqual(len(summary.split("\n\n")), 4)

    def test_report_text_uses_short_narrative_paragraphs(self) -> None:
        compact = _compact_summary(
            "## Resumo Executivo – Programa de Parceiros\n\n"
            "O período teve atividade.\n\nAs conversões foram registradas."
        )
        self.assertEqual(
            compact,
            "O período teve atividade.\n\nAs conversões foram registradas.",
        )
        indicators = self.repository.collect(self.guild_id, self.period)
        markdown = report_markdown(
            {
                "report_id": "compact",
                "report_type": "monthly",
                "period_label": self.period.label,
                "partial": False,
                "summary": compact,
                "indicators": indicators,
                "comparison": build_comparison(indicators, indicators),
                "analytics_version": "1.0.0",
            }
        )
        self.assertTrue(markdown.startswith("## Relatório"))
        self.assertIn("Fechamento mensal", markdown)
        self.assertEqual(len(deterministic_summary(indicators, build_comparison(indicators, indicators)).split("\n\n")), 4)

    def test_dashboard_image_is_valid_png(self) -> None:
        indicators = self.repository.collect(self.guild_id, self.period)
        report = {
            "report_id": "preview",
            "period_label": self.period.label,
            "partial": False,
            "indicators": indicators,
            "comparison": {},
            "analytics_version": "1.0.0",
        }

        image_file = render_partner_analytics(report)
        image = Image.open(image_file)

        self.assertEqual(image.format, "PNG")
        self.assertEqual(image.size, (1600, 900))

    def test_annual_report_aggregates_chart_by_month(self) -> None:
        annual = custom_period(date(2026, 1, 1), date(2026, 12, 31), "UTC")
        metrics = self.repository.collect(self.guild_id, annual)

        self.assertEqual(len(metrics["daily"]), 12)
        october = next(item for item in metrics["daily"] if item["date"] == "2026-10")
        self.assertEqual(october["referrals"], 4)
        self.assertEqual(october["conversions"], 1)

    async def test_report_is_rendered_with_components_v2(self) -> None:
        indicators = self.repository.collect(self.guild_id, self.period)
        report = {
            "report_id": "components-v2",
            "period_label": self.period.label,
            "partial": False,
            "summary": "Resumo validado do período.",
            "indicators": indicators,
            "comparison": build_comparison(indicators, indicators),
            "analytics_version": "1.0.0",
        }
        service = PartnerAnalyticsService(
            SimpleNamespace(get_guild=lambda _: None),
            self.partners_path,
            self.applications_path,
            FailingAI(),
            FakeLogs(),
        )

        file, view = await service.report_artifacts(
            report,
            panel_id=7,
            allow_publish=True,
        )

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertTrue(view.has_components_v2())
        self.assertIsInstance(view.children[0], discord.ui.Container)
        self.assertIsInstance(view.children[0].children[0], discord.ui.MediaGallery)
        self.assertIsInstance(view.children[1], discord.ui.ActionRow)
        file.close()

    async def test_click_channel_becomes_default_report_destination(self) -> None:
        channel = SimpleNamespace(id=765)
        logs = SimpleNamespace(
            ensure_panel_channel=AsyncMock(return_value=channel),
        )
        service = PartnerAnalyticsService(
            SimpleNamespace(get_guild=lambda _: None),
            self.partners_path,
            self.applications_path,
            FailingAI(),
            logs,
        )

        returned, config = await service.ensure_analytics_channel(
            SimpleNamespace(id=self.guild_id),
            actor=SimpleNamespace(id=99, __str__=lambda self: "Talisson"),
        )

        self.assertIs(returned, channel)
        self.assertEqual(config.analytics_channel_id, 765)
        self.assertEqual(config.weekly_channel_id, 765)
        self.assertEqual(config.monthly_channel_id, 765)
        self.assertEqual(config.annual_channel_id, 765)

    async def test_generation_persists_snapshot_and_prevents_duplicate_automatic_report(self) -> None:
        service = PartnerAnalyticsService(
            SimpleNamespace(get_guild=lambda _: None),
            self.partners_path,
            self.applications_path,
            FailingAI(),
            FakeLogs(),
        )
        guild = SimpleNamespace(id=self.guild_id)

        first = await service.generate(
            guild,
            report_type="weekly",
            period=self.period,
            generated_by=None,
            automatic=True,
            destination_channel_id=50,
        )
        second = await service.generate(
            guild,
            report_type="weekly",
            period=self.period,
            generated_by=None,
            automatic=True,
            destination_channel_id=50,
        )

        self.assertIsNotNone(first)
        self.assertEqual(first["report_id"], second["report_id"])
        self.assertEqual(first["status"], "generated")
        self.assertIn("4 novas indicações", first["summary"])
        history = self.repository.history(self.guild_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["indicators"]["projects"]["revenue"], 5000.0)

    async def test_authorization_reuses_partner_program_rbac(self) -> None:
        service = PartnerAnalyticsService(
            SimpleNamespace(get_guild=lambda _: None),
            self.partners_path,
            self.applications_path,
            FailingAI(),
            FakeLogs(),
        )
        panel = SimpleNamespace(id=7)
        program = SimpleNamespace(
            db=SimpleNamespace(panel=AsyncMock(return_value=panel)),
            can_manage_application=lambda user, selected: user.id == 99 and selected is panel,
        )
        service.program_service = program

        self.assertTrue(await service.authorized(SimpleNamespace(id=99), 7))
        self.assertFalse(await service.authorized(SimpleNamespace(id=100), 7))


if __name__ == "__main__":
    unittest.main()
