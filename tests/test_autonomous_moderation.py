from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from website_guard.autonomous_moderation import (
    AutonomousModeration,
    _is_suspicious_url,
)
from website_guard.state import StateStore


class AutonomousModerationTests(unittest.IsolatedAsyncioTestCase):
    def test_suspicious_links_are_detected_locally(self) -> None:
        self.assertTrue(_is_suspicious_url("https://xn--discrd-abc.com/login"))
        self.assertTrue(_is_suspicious_url("https://192.0.2.20/claim"))
        self.assertTrue(_is_suspicious_url("https://bit.ly/reward"))
        self.assertTrue(_is_suspicious_url("https://premio.top/claim"))
        self.assertFalse(_is_suspicious_url("https://discord.com/developers"))

    def test_scam_message_receives_high_local_score(self) -> None:
        moderator = AutonomousModeration(
            SimpleNamespace(),
            StateStore(Path(tempfile.gettempdir()) / "jarvis-mod-test.json"),
            owner_id=1,
        )
        message = SimpleNamespace(
            content="Claim reward and receive USDT https://premio.top/claim",
            mentions=[],
            role_mentions=[],
        )

        analysis = moderator._static_analysis(message)

        self.assertGreaterEqual(analysis.score, 100)
        self.assertIn("phishing", analysis.categories)
        self.assertIn("scam", analysis.categories)

    async def test_configuration_is_persistent_and_ai_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            moderator = AutonomousModeration(
                SimpleNamespace(),
                StateStore(path),
                owner_id=1,
                admin_role_ids={10},
            )
            await moderator.configure(
                5,
                log_channel_id=50,
                timeout_threshold=80,
                timeout_minutes=15,
            )
            await moderator.set_enabled(5, True)
            await moderator.set_role_exemption(5, 70, True)

            restored = AutonomousModeration(
                SimpleNamespace(),
                StateStore(path),
                owner_id=1,
                admin_role_ids={10},
            )
            config = await restored.config(5)

            self.assertTrue(config.enabled)
            self.assertEqual(config.log_channel_id, 50)
            self.assertEqual(config.timeout_threshold, 80)
            self.assertEqual(config.timeout_minutes, 15)
            self.assertIn(70, config.exempt_role_ids)
            self.assertFalse(hasattr(restored, "ai_service"))


if __name__ == "__main__":
    unittest.main()
