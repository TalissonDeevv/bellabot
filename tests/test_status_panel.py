from __future__ import annotations

import unittest

import discord

from website_guard.metrics import WeeklyMetricsSnapshot
from website_guard.monitor import MonitorReport
from website_guard.status_panel import (
    GuildStatusConfig,
    build_public_statuses,
    make_activity,
    make_streaming_activity,
    parse_status_keys,
)


class StatusPanelTests(unittest.TestCase):
    def test_healthy_statuses_use_real_metrics(self) -> None:
        report = MonitorReport(
            checked_at="2026-06-10T10:00:00+00:00",
            site_url="https://example.com",
            ok=True,
            status_code=200,
            response_ms=42,
        )
        weekly = WeeklyMetricsSnapshot(
            period_start="2026-06-04",
            period_end="2026-06-10",
            total_requests=1,
            average_response_ms=42,
            minimum_response_ms=42,
            maximum_response_ms=42,
            status_counts={"200": 1},
            category_counts={"site": 1},
            errors_detected=0,
            events_processed=0,
            changes_detected=0,
            feedbacks_detected=0,
            site_checks=1,
            site_successes=1,
            availability_percent=99.98,
            daily={},
        )
        statuses = build_public_statuses(report, weekly)
        self.assertTrue(any("42ms" in value for value in statuses))
        self.assertTrue(any("99.98%" in value for value in statuses))

    def test_incident_status_hides_internal_details(self) -> None:
        report = MonitorReport(
            checked_at="2026-06-10T10:00:00+00:00",
            site_url="https://private.internal",
            ok=False,
            status_code=None,
            error="token=secret at 127.0.0.1",
        )
        weekly = WeeklyMetricsSnapshot(
            "2026-06-04", "2026-06-10", 0, 0.0, None, None,
            {}, {}, 1, 0, 0, 0, 0, 0, 0.0, {},
        )
        text = " ".join(build_public_statuses(report, weekly))
        self.assertNotIn("secret", text)
        self.assertNotIn("127.0.0.1", text)

    def test_activity_types_are_supported(self) -> None:
        activity = make_activity("listening", "Monitoramento Ativo")
        self.assertEqual(activity.type, discord.ActivityType.listening)

    def test_streamer_presence_is_persistent_activity(self) -> None:
        config = GuildStatusConfig.from_dict(
            1,
            {
                "streamer_enabled": True,
                "streamer_name": "Bella ao vivo",
                "streamer_description": "Monitoramento da comunidade",
                "streamer_url": "https://twitch.tv/talissondev",
                "streamer_icon_url": "https://example.com/icon.png",
            },
        )
        self.assertTrue(config.to_dict()["streamer_enabled"])
        activity = make_streaming_activity(config)
        self.assertEqual(activity.type, discord.ActivityType.streaming)
        self.assertEqual(activity.name, "Bella ao vivo")
        self.assertEqual(activity.url, "https://twitch.tv/talissondev")

    def test_rotation_selection_preserves_admin_priority(self) -> None:
        selected = parse_status_keys(
            "disponibilidade, infraestrutura, personalizado"
        )
        self.assertEqual(
            selected,
            ("availability", "infrastructure", "custom"),
        )
        config = GuildStatusConfig.from_dict(
            1,
            {"active_status_keys": list(selected)},
        )
        self.assertEqual(config.active_status_keys, selected)

    def test_custom_incident_message_is_filtered(self) -> None:
        report = MonitorReport(
            checked_at="2026-06-10T10:00:00+00:00",
            site_url="https://example.com",
            ok=False,
            status_code=None,
            error="offline",
        )
        weekly = WeeklyMetricsSnapshot(
            "2026-06-04", "2026-06-10", 0, 0.0, None, None,
            {}, {}, 1, 0, 0, 0, 0, 0, 0.0, {},
        )
        statuses = build_public_statuses(
            report,
            weekly,
            incident_down="token secreto em http://localhost",
        )
        self.assertNotIn("token", " ".join(statuses).casefold())
        self.assertNotIn("localhost", " ".join(statuses).casefold())


if __name__ == "__main__":
    unittest.main()
