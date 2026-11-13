from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from website_guard.knowledge import (
    KnowledgeEntry,
    KnowledgeBase,
    _group_data_to_entries,
    _parse_portuguese_faq,
    natural_reply,
    normalize_entity_references,
    question_intent_score,
)
from website_guard.state import StateStore


class KnowledgeBaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        settings = SimpleNamespace(
            knowledge_max_message_length=500,
            question_intent_min=0.90,
            knowledge_match_min=0.78,
            knowledge_min_confidence=0.90,
            talisson_user_id=933780111742877777,
        )
        self.base = KnowledgeBase(
            settings,  # type: ignore[arg-type]
            StateStore(Path(self.temp_dir.name) / "state.json"),
        )
        bundle = (
            'e==="pt"?[{title:"Contato",questions:['
            '{q:"Qual a melhor forma de entrar em contato?",'
            'a:"Pelo formulário do site ou pelo WhatsApp. Respondo em até 24 horas úteis."},'
            '{q:"Quanto custa um projeto?",'
            'a:"O investimento depende do escopo. Os projetos começam em R$ 3.000."},'
            '{q:"Você trabalha com WordPress ou plataformas prontas?",'
            'a:"Não. Meu foco é desenvolvimento sob medida com código próprio."},'
            '{q:"Há quanto tempo você trabalha com tecnologia?",'
            'a:"Desde 2016. Comecei criando bots para Discord e evoluí para sistemas web."}'
            ']}]:[{title:"About"'
        )
        self.base._entries = _parse_portuguese_faq(
            bundle,
            "https://example.com/faq",
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_paraphrased_official_question_is_answered(self) -> None:
        match = await self.base.match("Como posso falar com o Talisson?")
        self.assertTrue(match.should_reply)
        self.assertGreaterEqual(match.final_confidence, 0.90)
        self.assertEqual(
            match.entry.question if match.entry else None,
            "Qual a melhor forma de entrar em contato?",
        )

    async def test_unrelated_question_is_ignored(self) -> None:
        match = await self.base.match("Qual é a capital da França?")
        self.assertFalse(match.should_reply)
        self.assertEqual(match.reason, "low_match")

    async def test_response_uses_name_without_internal_terms(self) -> None:
        match = await self.base.match("Quanto custa um projeto?")
        response = natural_reply("Marina", match)
        self.assertIn("Marina", response)
        for forbidden in ("FAQ", "base de conhecimento", "IA", "confiança"):
            self.assertNotIn(forbidden, response)

    def test_casual_and_bot_directed_messages_are_not_questions(self) -> None:
        self.assertEqual(question_intent_score("Oi, tudo bem?"), 0.0)
        self.assertEqual(question_intent_score("Quem é você?"), 0.0)
        self.assertEqual(question_intent_score("Seu site ficou bonito!"), 0.0)

    async def test_discord_mention_is_treated_as_talisson_reference(self) -> None:
        self.base._entries.append(
            KnowledgeEntry(
                entry_id="instagram",
                question="Instagram de Talisson Souza",
                answer="https://instagram.com/talisson.dev",
                source_url="https://example.com",
                source_title="Home",
                source_type="link",
            )
        )
        match = await self.base.match(
            "<@933780111742877777> tem Instagram?"
        )
        self.assertTrue(match.should_reply)
        response = natural_reply("<@123>", match)
        self.assertIn("instagram.com", response)
        self.assertIn("<@123>", response)

    def test_entity_aliases_are_normalized(self) -> None:
        for value in (
            "TalinhoDev tem GitHub?",
            "O Tali trabalha com o quê?",
            "CEO da Cheetah faz consultoria?",
            "<@!933780111742877777> tem LinkedIn?",
        ):
            self.assertIn(
                "TALISSON_SOUZA",
                normalize_entity_references(value, 933780111742877777),
            )

    async def test_answer_is_rewritten_in_third_person(self) -> None:
        match = await self.base.match("Quanto custa um projeto?")
        response = natural_reply("<@123>", match)
        self.assertNotIn("Meu foco", response)
        self.assertNotIn("Respondo", response)

    async def test_missing_social_link_does_not_match_unrelated_content(self) -> None:
        match = await self.base.match("Talisson tem Instagram?")
        self.assertFalse(match.should_reply)

    async def test_natural_work_question_is_understood(self) -> None:
        match = await self.base.match(
            "<@933780111742877777> trabalha com o quê?"
        )
        self.assertTrue(match.should_reply)

    def test_company_count_is_derived_from_published_section(self) -> None:
        entries = _group_data_to_entries(
            "https://example.com/carreira",
            "Carreira",
            [
                {
                    "heading": "Minhas Empresas",
                    "items": ["Cheetah Solutions Network", "Werneck Group"],
                }
            ],
        )
        derived = next(
            entry for entry in entries if entry.source_type == "derived"
        )
        self.assertIn("2 empresas", derived.answer)
        self.assertIn("Werneck Group", derived.answer)


if __name__ == "__main__":
    unittest.main()


class StaticKnowledgeWithoutChromiumTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_without_chromium_keeps_learned_pages(self) -> None:
        import website_guard.knowledge as module

        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            settings = SimpleNamespace(
                render_dynamic_pages=False,
                site_url="https://example.com/",
                knowledge_pages=["/sobre"],
                knowledge_max_pages=5,
                knowledge_max_message_length=500,
                question_intent_min=0.90,
                knowledge_match_min=0.78,
                knowledge_min_confidence=0.90,
                talisson_user_id=933780111742877777,
            )
            base = KnowledgeBase(settings, state)  # type: ignore[arg-type]
            learned = KnowledgeEntry(
                entry_id="x",
                question="Sobre",
                answer="Página aprendida numa leitura completa anterior com Chromium.",
                source_url="https://example.com/sobre",
                source_title="Sobre",
                source_type="page",
            )
            await state.set("knowledge_entries", [learned.to_dict()])

            async def no_faq(_session):
                raise ValueError("sem FAQ")

            async def fake_fetch(_session, url):
                return (
                    "<html><head><title>Talisson</title>"
                    '<meta name="description" content="Desenvolvedor de sistemas web sob medida e automações para empresas."></head>'
                    "<body><script>var x='nada aqui'</script><div id=root></div></body></html>"
                )

            original_fetch = module._fetch_text
            base._load_faq_entries = no_faq
            module._fetch_text = fake_fetch
            try:
                launched = []
                base._load_page_entries = lambda: launched.append(True)
                report = await base.refresh(None)  # type: ignore[arg-type]
            finally:
                module._fetch_text = original_fetch

            self.assertEqual(launched, [])  # Chromium não foi aberto
            answers = [entry.answer for entry in base._entries]
            self.assertIn(learned.answer, answers)
            self.assertTrue(any("sob medida" in answer for answer in answers))
            self.assertNotIn("nada aqui", " ".join(answers))
            self.assertGreaterEqual(report.pages_processed, 1)
