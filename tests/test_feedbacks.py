from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from website_guard.feedbacks import (
    Feedback,
    FeedbackMonitor,
    _discover_feedback_script_sources,
    _parse_feedback_bundle,
)
from website_guard.state import StateStore


class FeedbackParserTests(unittest.TestCase):
    def test_parses_literal_and_asset_variable_avatars(self) -> None:
        bundle = (
            'const pic="/assets/person.png",items=['
            '{name:"Ana",role:{pt:"Engenheira",en:"Engineer"},'
            'text:{pt:"Excelente trabalho.",en:"Excellent work."},'
            'image:"https://cdn.example/ana.png"},'
            '{name:"Bruno",role:{pt:"CEO",en:"CEO"},'
            'text:{pt:"Entrega consistente.",en:"Consistent delivery."},'
            "image:pic}];"
        )
        feedbacks = _parse_feedback_bundle(
            bundle,
            "https://example.com/feedbacks",
        )
        self.assertEqual(len(feedbacks), 2)
        self.assertEqual(feedbacks[0].name, "Ana")
        self.assertEqual(
            feedbacks[1].avatar_url,
            "https://example.com/assets/person.png",
        )
        self.assertNotEqual(
            feedbacks[0].feedback_id,
            feedbacks[1].feedback_id,
        )

    def test_parses_vite_bundle_with_template_literal_strings(self) -> None:
        bundle = (
            "var e=[{name:`Ana`,role:{pt:`Engenheira`,en:`Engineer`},"
            "text:{pt:`Excelente trabalho.`,en:`Excellent work.`},"
            "image:`https://cdn.example/ana.png`}];"
        )
        feedbacks = _parse_feedback_bundle(bundle, "https://example.com/feedbacks")
        self.assertEqual(len(feedbacks), 1)
        self.assertEqual(feedbacks[0].name, "Ana")
        self.assertEqual(feedbacks[0].role, "Engenheira")

    def test_discovers_feedback_chunks_from_vite_bundles(self) -> None:
        bundle = (
            'const chunks=["assets/Feedbacks-ABC123.js",'
            '"./signature-feedbacks-XYZ987.js"]'
        )
        sources = _discover_feedback_script_sources(
            bundle,
            bundle_url="https://example.com/assets/index.js",
            source_url="https://example.com/feedbacks",
        )
        self.assertEqual(
            sources,
            [
                "https://example.com/assets/Feedbacks-ABC123.js",
                "https://example.com/assets/signature-feedbacks-XYZ987.js",
            ],
        )


class FeedbackStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_survives_state_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = StateStore(path)
            await state.set(
                "latest_feedback_report",
                {
                    "checked_at": "2026-06-10T10:00:00+00:00",
                    "feedbacks": [
                        {"feedback_id": "one"},
                        {"feedback_id": "two"},
                    ],
                    "error": None,
                },
            )
            await state.update_section(
                "processed_feedbacks",
                "one",
                {"processed_at": "2026-06-10T10:00:01+00:00"},
            )
            monitor = FeedbackMonitor(
                "https://example.com/feedbacks",
                StateStore(path),
            )
            status = await monitor.status()
            self.assertEqual(status.total_found, 2)
            self.assertEqual(status.total_synchronized, 1)
            self.assertEqual(status.pending, 1)

    async def test_archives_full_feedback_payload_for_resend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            monitor = FeedbackMonitor("https://example.com/feedbacks", state)
            feedback = Feedback(
                feedback_id="feedback-one",
                name="Ana",
                message="Excelente trabalho.",
                source_url="https://example.com/feedbacks",
                role="Engenheira",
            )
            await monitor.archive_feedbacks([feedback], "2026-09-23T10:00:00+00:00")
            archived = await monitor.archived_feedbacks()
            self.assertEqual(archived, [feedback])


if __name__ == "__main__":
    unittest.main()
