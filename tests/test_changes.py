from __future__ import annotations

import unittest

from website_guard.changes import _normalize_page_text, _summarize_change


class ChangeSummaryTests(unittest.TestCase):
    def test_dynamic_security_challenge_is_stable(self) -> None:
        first = _normalize_page_text(
            "Formulário Verificação de segurança 2 × 12 = ? Enviar mensagem"
        )
        second = _normalize_page_text(
            "Formulário Verificação de segurança 8 - 7 = ? Enviar mensagem"
        )
        self.assertEqual(first, second)

    def test_summary_uses_natural_language_sections(self) -> None:
        change = _summarize_change(
            "https://example.com/sobre",
            "Sobre",
            "Experiência em APIs.",
            "Experiência em APIs modernas. Nova certificação.",
        )
        self.assertIn("foi atualizada", change.summary)
        self.assertTrue(change.added or change.modified)
        self.assertNotIn("{", change.summary)


if __name__ == "__main__":
    unittest.main()
