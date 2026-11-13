from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from website_guard.changes import ChangeScanReport, _summarize_change
from website_guard.intelligence import (
    OperationalAnalysis,
    OperationalEvent,
    OperationalIntelligence,
)
from website_guard.metrics import WeeklyMetricsSnapshot
from website_guard.monitor import MonitorReport
from website_guard.operational_embeds import operational_event_embed
from website_guard.state import StateStore


class IntelligenceCenterTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_is_analyzed_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            intelligence = OperationalIntelligence(state)
            event = OperationalEvent(
                event_type="performance_alert",
                title="Latência elevada",
                severity="medium",
                scope="site",
                occurred_at="2026-06-11T10:00:00+00:00",
                source_url="https://example.com",
                metrics={
                    "latencia_atual_ms": 1200,
                    "latencia_media_historica_ms": 400,
                },
            )

            analysis = await intelligence.analyze_event(event)
            history = await state.get_section("operational_events")

            self.assertEqual(analysis.generated_by, "local_rules")
            self.assertIn(event.event_id, history)
            self.assertIn("latência", analysis.executive_summary.lower())

    async def test_health_score_uses_real_operational_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            intelligence = OperationalIntelligence(
                StateStore(Path(directory) / "state.json"),
            )
            metrics = WeeklyMetricsSnapshot(
                period_start="2026-06-05",
                period_end="2026-06-11",
                total_requests=100,
                average_response_ms=380,
                minimum_response_ms=110,
                maximum_response_ms=900,
                status_counts={"200": 99, "500": 1},
                category_counts={"site": 100},
                errors_detected=1,
                events_processed=2,
                changes_detected=1,
                feedbacks_detected=3,
                site_checks=100,
                site_successes=99,
                availability_percent=99,
                daily={},
            )
            site = MonitorReport(
                checked_at="2026-06-11T10:00:00+00:00",
                site_url="https://example.com",
                ok=True,
                status_code=200,
                response_ms=380,
                tls_days_left=80,
                verification_status="verified",
            )

            score = await intelligence.health_score(
                metrics,
                site,
                ChangeScanReport(checked_at=site.checked_at, pages_checked=4),
                active_incidents=0,
            )

            self.assertGreaterEqual(score.overall, 80)
            self.assertEqual(score.availability, 99)
            self.assertGreater(score.security, 90)

    def test_public_embed_does_not_expose_technical_details(self) -> None:
        event = OperationalEvent(
            event_type="incident",
            title="Falha de leitura",
            severity="high",
            scope="changes",
            occurred_at="2026-06-11T10:00:00+00:00",
            technical={
                "error": "TimeoutError Page.goto waiting until domcontentloaded",
            },
        )
        analysis = OperationalAnalysis(
            executive_summary="Uma página não respondeu no período esperado.",
            what_happened="A leitura automática não foi concluída.",
            impact="A cobertura de monitoramento ficou temporariamente reduzida.",
            relevance="A página deve ser validada novamente.",
            severity="high",
            likely_causes=(),
            recommendations=("Executar uma nova verificação.",),
            trend="Evento isolado.",
            conclusion="Acompanhamento necessário.",
            generated_by="gemini",
        )

        embed = operational_event_embed(event, analysis)
        serialized = str(embed.to_dict())

        self.assertNotIn("TimeoutError", serialized)
        self.assertNotIn("Page.goto", serialized)
        self.assertNotIn("domcontentloaded", serialized)

    def test_structural_and_seo_changes_are_classified(self) -> None:
        change = _summarize_change(
            "https://example.com",
            "Página inicial",
            "8 anos de experiência",
            "10 anos de experiência",
            {
                "buttons": ["Contato"],
                "seo": {"title": "Título antigo"},
                "technologies": ["React"],
            },
            {
                "buttons": ["Contato", "Consultoria"],
                "seo": {"title": "Novo título"},
                "technologies": ["React", "Vercel"],
            },
        )

        self.assertIn("buttons", change.change_types)
        self.assertIn("seo", change.change_types)
        self.assertTrue(change.seo_changes)
        self.assertTrue(change.technology_changes)


if __name__ == "__main__":
    unittest.main()
