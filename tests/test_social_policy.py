from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.ai.complexity import assess_generation_mode
from src.ai.social_intelligence import assess_social_intent
from website_guard.social_policy import SocialChannelPolicy
from website_guard.state import StateStore


class SocialPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_humor_channels_are_persistent_and_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            policy = SocialChannelPolicy(StateStore(path))

            self.assertFalse(await policy.allows(10, 20))
            await policy.set_channel(10, 20, enabled=True)

            restored = SocialChannelPolicy(StateStore(path))
            self.assertTrue(await restored.allows(10, 20))
            self.assertFalse(await restored.allows(10, 21))

    def test_serious_question_has_priority_over_humor(self) -> None:
        assessment = assess_social_intent(
            "Vale a pena comprar um domínio para meu site?"
        )
        mode = assess_generation_mode(
            "Vale a pena comprar um domínio para meu site?",
            humor_allowed=True,
        )

        self.assertTrue(assessment.serious)
        self.assertFalse(mode.social)

    def test_social_mode_requires_an_allowed_channel(self) -> None:
        self.assertTrue(
            assess_generation_mode(
                "IAE BELLA MEU BRODI",
                humor_allowed=True,
            ).social
        )
        blocked = assess_generation_mode(
            "IAE BELLA MEU BRODI",
            humor_allowed=False,
        )

        self.assertFalse(blocked.social)
        self.assertIn("não permite modo de zoeira", blocked.instruction)

    def test_aggression_is_classified_for_deescalation(self) -> None:
        assessment = assess_social_intent("vai tomar no teu UC")

        self.assertTrue(assessment.aggressive)
        self.assertTrue(assessment.deescalate)
        self.assertFalse(assessment.playful)


if __name__ == "__main__":
    unittest.main()
