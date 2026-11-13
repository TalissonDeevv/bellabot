from __future__ import annotations

import unittest

from website_guard.discord_embeds import feedback_embed, welcome_embed
from website_guard.embed_builder import DraftField, EmbedDraft
from website_guard.feedbacks import Feedback


class EmbedTests(unittest.TestCase):
    def test_feedback_embed_contains_professional_context(self) -> None:
        feedback = Feedback(
            feedback_id="abc123",
            name="Ana",
            role="Engenheira",
            message="Excelente trabalho.",
            avatar_url="https://example.com/avatar.png",
            source_url="https://example.com/feedbacks",
            rating="5",
        )
        embed = feedback_embed(feedback, "2026-06-10T10:00:00+00:00")
        payload = embed.to_dict()
        field_names = {field["name"] for field in payload["fields"]}
        self.assertIn("ID único", field_names)
        self.assertIn("Origem", field_names)
        self.assertEqual(payload["thumbnail"]["url"], feedback.avatar_url)

    def test_builder_supports_dynamic_fields(self) -> None:
        draft = EmbedDraft(
            title="Aviso",
            description="Descrição",
            fields=[DraftField("Campo", "Valor", True)],
        )
        embed = draft.to_embed()
        self.assertEqual(embed.fields[0].name, "Campo")
        self.assertTrue(embed.fields[0].inline)

    def test_welcome_embed_represents_the_server(self) -> None:
        embed = welcome_embed("Talisson", "https://example.com/avatar.png")
        payload = embed.to_dict()
        self.assertIn("Escritório do Talisson Souza", payload["title"])
        self.assertIn("produtos digitais", payload["description"])
        self.assertEqual(payload["thumbnail"]["url"], "https://example.com/avatar.png")


if __name__ == "__main__":
    unittest.main()
