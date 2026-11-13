from __future__ import annotations

import unittest

from src.ai.personality_engine import CommunityPersonalityEngine


class PersonalityEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = CommunityPersonalityEngine()

    def test_technical_question_uses_developer_style_without_forced_humor(self) -> None:
        style = self.engine.analyze(
            "Como funciona o Docker no Linux?",
            recent_context=[],
            humor_allowed=True,
        )

        self.assertEqual(style.context, "technical")
        self.assertFalse(style.humor)
        self.assertIn("desenvolvedor experiente", style.instruction)

    def test_meme_only_uses_humor_when_channel_allows_it(self) -> None:
        allowed = self.engine.analyze(
            "IAE BELLA MEU BRODI",
            recent_context=[],
            humor_allowed=True,
        )
        blocked = self.engine.analyze(
            "IAE BELLA MEU BRODI",
            recent_context=[],
            humor_allowed=False,
        )

        self.assertTrue(allowed.humor)
        self.assertFalse(blocked.humor)

    def test_ticket_hint_forces_ticket_context(self) -> None:
        style = self.engine.analyze(
            "oi",
            recent_context=[],
            humor_allowed=False,
            context_hint="ticket",
        )

        self.assertEqual(style.context, "ticket")
        self.assertFalse(style.humor)

    def test_postprocessor_removes_ai_and_corporate_voice(self) -> None:
        style = self.engine.analyze(
            "preciso de ajuda com meu deploy",
            recent_context=[],
            humor_allowed=False,
        )
        adapted = self.engine.adapt(
            (
                "Excelente pergunta. Conforme informado, segue abaixo a solução. "
                "Fico à disposição para qualquer solicitação."
            ),
            user_message="preciso de ajuda com meu deploy",
            style=style,
        )
        lowered = adapted.casefold()

        self.assertNotIn("excelente pergunta", lowered)
        self.assertNotIn("conforme informado", lowered)
        self.assertNotIn("segue abaixo", lowered)
        self.assertNotIn("fico à disposição", lowered)
        self.assertNotIn("solicitação", lowered)

    def test_abbreviations_never_modify_code_or_urls(self) -> None:
        style = self.engine.analyze(
            "meu brodi olha isso kkkkk",
            recent_context=[],
            humor_allowed=True,
        )
        answer = (
            "Você pode ver porque isso aconteceu em `porque = você` "
            "ou https://exemplo.com/porque-voce."
        )
        adapted = self.engine.adapt(
            answer,
            user_message="meu brodi olha isso kkkkk",
            style=style,
        )

        self.assertIn("`porque = você`", adapted)
        self.assertIn("https://exemplo.com/porque-voce", adapted)

    def test_short_interjection_can_become_chat_rhythm(self) -> None:
        style = self.engine.analyze(
            "BELLA OLHA ISSO KKKKK",
            recent_context=[],
            humor_allowed=True,
        )
        adapted = self.engine.adapt(
            "Rapaz... agora eu entendi oq aconteceu.",
            user_message="BELLA OLHA ISSO KKKKK",
            style=style,
        )

        self.assertIn("\n\n", adapted)

    def test_aggression_enables_deescalation(self) -> None:
        style = self.engine.analyze(
            "vai tomar no teu UC",
            recent_context=[],
            humor_allowed=True,
        )
        adapted = self.engine.adapt(
            "Cala a boca, seu idiota.",
            user_message="vai tomar no teu UC",
            style=style,
        )

        self.assertTrue(style.deescalate)
        self.assertNotIn("idiota", adapted.casefold())
        self.assertIn("sem partir pra ofensa", adapted.casefold())


if __name__ == "__main__":
    unittest.main()
