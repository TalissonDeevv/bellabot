from __future__ import annotations

import unittest

from website_guard.legacy_embed_migration import (
    adapt_jarvis_component_payload,
    adapt_jarvis_embed_payload,
    adapt_jarvis_identity_text,
    attachment_filenames_from_payload,
    component_payloads_have_v2,
    contains_jarvis_identity,
)


class LegacyEmbedMigrationTests(unittest.TestCase):
    def test_adapts_visible_jarvis_identity_with_gender(self) -> None:
        text = (
            "Abra um ticket com o Jarvis. Esse painel foi criado pelo Jarvis "
            "para o Jarvis acompanhar tudo."
        )

        self.assertEqual(
            adapt_jarvis_identity_text(text),
            "Abra um ticket com a Bella. Esse painel foi criado pela Bella "
            "para a Bella acompanhar tudo.",
        )

    def test_adapts_only_visible_embed_fields(self) -> None:
        payload = {
            "title": "Central AutoMod Jarvis",
            "description": "Painel operacional do Jarvis.",
            "url": "https://example.com/jarvis/nao-quebrar-link",
            "footer": {"text": "Jarvis English Learning"},
            "author": {"name": "Senhor Jarvis"},
            "fields": [
                {"name": "Regras nativas criadas pelo Jarvis", "value": "do Jarvis"},
            ],
        }

        adapted = adapt_jarvis_embed_payload(payload)

        self.assertEqual(adapted["title"], "Central AutoMod Bella")
        self.assertEqual(adapted["description"], "Painel operacional da Bella.")
        self.assertEqual(adapted["url"], payload["url"])
        self.assertEqual(adapted["footer"]["text"], "Bella English Learning")
        self.assertEqual(adapted["author"]["name"], "Bella Nyelle")
        self.assertEqual(adapted["fields"][0]["name"], "Regras nativas criadas pela Bella")
        self.assertEqual(adapted["fields"][0]["value"], "da Bella")

    def test_detects_jarvis_in_nested_payload(self) -> None:
        self.assertTrue(
            contains_jarvis_identity(
                {"embeds": [{"footer": {"text": "Assistente Jarvis"}}]}
            )
        )
        self.assertFalse(contains_jarvis_identity({"title": "Bella Nyelle"}))

    def test_collects_attachment_filenames(self) -> None:
        payload = {
            "image": {"url": "attachment://banner.png"},
            "thumbnail": {"url": "attachment://thumb.webp?x=1"},
        }

        self.assertEqual(
            attachment_filenames_from_payload(payload),
            {"banner.png", "thumb.webp"},
        )

    def test_adapts_components_v2_visible_text_without_custom_ids(self) -> None:
        payload = [
            {
                "type": 17,
                "components": [
                    {"type": 10, "content": "## Central Jarvis\nFale com o Jarvis"},
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 2,
                                "style": 1,
                                "label": "Botão do Jarvis",
                                "custom_id": "jarvis:old:button",
                            }
                        ],
                    },
                ],
            }
        ]

        adapted = adapt_jarvis_component_payload(payload)

        self.assertTrue(component_payloads_have_v2(adapted))
        self.assertEqual(
            adapted[0]["components"][0]["content"],
            "## Central Bella\nFale com a Bella",
        )
        self.assertEqual(
            adapted[0]["components"][1]["components"][0]["label"],
            "Botão da Bella",
        )
        self.assertEqual(
            adapted[0]["components"][1]["components"][0]["custom_id"],
            "jarvis:old:button",
        )


if __name__ == "__main__":
    unittest.main()
