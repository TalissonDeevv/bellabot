from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.ai.bella_prompt_builder import build_bella_social_prompt
from src.ai.local_conversation import _social_fallback
from src.ai.memory import ConversationMemory, known_people_facts, known_user_facts
from website_guard.state import StateStore


class KnownUserMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_profiles_are_isolated_by_user_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            memory = ConversationMemory(StateStore(Path(directory) / "state.json"))
            boyfriend = await memory.facts(1, 1355150439057985687)
            anthropic = await memory.facts(1, 1189671608080679012)
            furia = await memory.facts(1, 1552721267415584799)
            brother = await memory.facts(1, 1409982177332166676)
            unknown = await memory.facts(1, 999)

        self.assertIn("namorado fictício", boyfriend["relationship"])
        self.assertIn("dependência emocional", boyfriend["relationship_safety"])
        self.assertEqual(anthropic["professional_identity"], "É Anthropic Senior Staff.")
        self.assertIn("FURIA", furia["professional_identity"])
        self.assertIn("irmão fictício", brother["relationship"])
        self.assertIn("bro", brother["relationship_style"])
        self.assertEqual(unknown, {})
        self.assertNotIn("relationship", anthropic)

    def test_social_prompt_receives_only_current_user_profile(self) -> None:
        facts = known_user_facts(1355150439057985687)
        prompt = build_bella_social_prompt(
            message="oi",
            display_name="Pessoa",
            channel_history=[],
            user_facts=facts,
        )
        self.assertIn("namorado fictício", prompt)
        self.assertIn("Nunca incentivar dependência emocional real", prompt)
        self.assertNotIn("Anthropic Senior Staff", prompt)

    def test_known_people_are_available_to_every_conversation(self) -> None:
        prompt = build_bella_social_prompt(
            message="quem é seu namorado?",
            display_name="Outra pessoa",
            channel_history=[],
            user_facts=known_people_facts(),
        )

        self.assertIn("<@1355150439057985687>", prompt)
        self.assertIn("Anthropic Senior Staff", prompt)
        self.assertIn("pro player profissional da FURIA", prompt)

    def test_boyfriend_question_has_safe_local_fallback(self) -> None:
        answer = _social_fallback(
            "quem é seu namorado?",
            "quem é seu namorado?",
        )

        self.assertIsNotNone(answer)
        self.assertIn("<@1355150439057985687>", answer or "")
        self.assertIn("fictício", answer or "")

    def test_other_known_people_have_direct_identity_answers(self) -> None:
        anthropic = _social_fallback(
            "Bella, conhece <@1189671608080679012>?",
            "bella, conhece <@1189671608080679012>?",
        )
        furia = _social_fallback(
            "quem é <@1552721267415584799>?",
            "quem é <@1552721267415584799>?",
        )

        self.assertIn("Anthropic Senior Staff", anthropic or "")
        self.assertIn("perito em Claude", anthropic or "")
        self.assertIn("pro player profissional", furia or "")
        self.assertIn("FURIA", furia or "")

    def test_fictional_brother_is_recognized(self) -> None:
        brother = _social_fallback(
            "Bella, conhece <@1409982177332166676>?",
            "bella, conhece <@1409982177332166676>?",
        )
        prompt = build_bella_social_prompt(
            message="salve Bella",
            display_name="Bro",
            channel_history=[],
            user_facts={**known_people_facts(), **known_user_facts(1409982177332166676)},
        )

        self.assertIn("meu irmão fictício", brother or "")
        self.assertIn("meu bro", brother or "")
        self.assertIn("irmão fictício", prompt)
        self.assertIn("tratamento de bro", prompt)

    def test_each_current_user_profile_guides_social_dialogue(self) -> None:
        boyfriend_prompt = build_bella_social_prompt(
            message="oi bella",
            display_name="Namorado",
            channel_history=[],
            user_facts={**known_people_facts(), **known_user_facts(1355150439057985687)},
        )
        anthropic_prompt = build_bella_social_prompt(
            message="o que acha do Claude?",
            display_name="Especialista",
            channel_history=[],
            user_facts={**known_people_facts(), **known_user_facts(1189671608080679012)},
        )
        furia_prompt = build_bella_social_prompt(
            message="oi bella",
            display_name="Jogador",
            channel_history=[],
            user_facts={**known_people_facts(), **known_user_facts(1552721267415584799)},
        )

        self.assertIn("carinho leve de casal", boyfriend_prompt)
        self.assertIn("relationship_safety", boyfriend_prompt)
        self.assertIn("Anthropic Senior Staff", anthropic_prompt)
        self.assertIn("especialista sênior em Claude", anthropic_prompt)
        self.assertIn("pro player profissional da FURIA", furia_prompt)
        self.assertIn("sem inventar equipe", furia_prompt)


if __name__ == "__main__":
    unittest.main()
