from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from website_guard.discord_embeds import weekly_report_embeds
from website_guard.knowledge import KnowledgeAnalyticsSnapshot
from website_guard.metrics import MetricsRecorder, RequestObservation
from website_guard.state import StateStore
from website_guard.traffic import TrafficSnapshot


class WeeklyReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_weekly_metrics_include_availability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = MetricsRecorder(StateStore(Path(directory) / "state.json"))
            now = datetime.now(timezone.utc).isoformat()
            await recorder.record_request(
                RequestObservation("site", "https://example.com", 200, 120, None, now)
            )
            await recorder.record_request(
                RequestObservation("site", "https://example.com", 500, 450, None, now)
            )
            weekly = await recorder.weekly_snapshot()
            self.assertEqual(weekly.site_checks, 2)
            self.assertEqual(weekly.site_successes, 1)
            self.assertEqual(weekly.availability_percent, 50.0)

    async def test_report_is_generated_when_week_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = MetricsRecorder(StateStore(Path(directory) / "state.json"))
            weekly = await recorder.weekly_snapshot()
            knowledge = KnowledgeAnalyticsSnapshot(
                evaluated=0,
                answered=0,
                ignored=0,
                average_answer_confidence=0.0,
                reasons={},
                daily={},
            )
            embeds = weekly_report_embeds(
                weekly,
                knowledge,
                TrafficSnapshot(
                    available=False,
                    source="não configurada",
                    period_start=weekly.period_start,
                    period_end=weekly.period_end,
                ),
                knowledge_entries=0,
                knowledge_last_refresh=None,
                active_incidents=0,
                incident_counts={"opened": 0, "reminder": 0, "recovered": 0},
                latest_site=None,
                latest_feedback=None,
                latest_changes=None,
            )
            self.assertEqual(len(embeds), 3)
            self.assertIn("sem incidentes", embeds[0].description.lower())
            self.assertIn("audiência", embeds[1].title.lower())
            self.assertGreater(len(embeds[0].fields), 0)


if __name__ == "__main__":
    unittest.main()
