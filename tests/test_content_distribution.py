from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from website_guard.content_distribution import (
    ContentDistributionMonitor,
    ContentItem,
    ContentSource,
    _item_key,
    _items_from_html,
    _normalize_url,
    content_announcement_embed,
)
from website_guard.state import StateStore


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        content_instagram_url="https://www.instagram.com/talisson.dev/",
        content_instagram_feed_url="",
        content_instagram_token="",
        content_linkedin_url="https://www.linkedin.com/in/talissondevv/",
        content_linkedin_feed_url="",
        content_linkedin_token="",
        content_tiktok_url="https://www.tiktok.com/@talisson.dev",
        content_tiktok_feed_url="",
        content_tiktok_token="",
        content_x_url="https://x.com/talissondev",
        content_x_feed_url="",
        content_x_token="",
        content_articles_url="https://www.talissonsouza.com.br/artigos",
        content_articles_feed_url="",
        content_distribution_announce_existing=False,
        content_distribution_max_items_per_scan=3,
        render_dynamic_pages=False,
        browser_headless=True,
        request_timeout_seconds=10,
    )


class FakeService:
    async def generate_json(self, *args, **kwargs):
        raise RuntimeError("indisponível")


class FakeMonitor(ContentDistributionMonitor):
    def __init__(self, state: StateStore) -> None:
        super().__init__(settings(), state, FakeService())
        self.round = 0

    async def _discover_source(self, session, source, *, initialized):
        if source.key != "articles":
            return []
        items = [
            ContentItem(
                source_key="articles",
                platform="Artigos",
                url="https://www.talissonsouza.com.br/artigos/primeiro",
                title="Primeiro artigo",
            )
        ]
        if self.round:
            items.insert(
                0,
                ContentItem(
                    source_key="articles",
                    platform="Artigos",
                    url="https://www.talissonsouza.com.br/artigos/novo",
                    title="Novo artigo",
                ),
            )
        return items

    async def _enrich_item(self, session, item):
        item.description = "Descrição oficial do conteúdo."
        return item


class ContentDistributionTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_scan_seeds_and_next_scan_queues_only_new_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            monitor = FakeMonitor(state)

            first = await monitor.scan(SimpleNamespace())
            monitor.round = 1
            second = await monitor.scan(SimpleNamespace())

            self.assertEqual(first.seeded, 1)
            self.assertEqual(first.queued, 0)
            self.assertEqual(second.queued, 1)
            self.assertEqual(len(second.items), 1)
            self.assertTrue(second.items[0].url.endswith("/novo"))

    async def test_sent_url_and_delivery_log_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = StateStore(path)
            monitor = FakeMonitor(state)
            await monitor.scan(SimpleNamespace())
            monitor.round = 1
            report = await monitor.scan(SimpleNamespace())
            item = report.items[0]

            await monitor.mark_sent(
                item,
                message_id=123,
                channel_id=456,
                analysis_mode="groq",
            )

            restored = StateStore(path)
            records = await restored.get_section("content_distribution_items")
            logs = await restored.get_section("content_distribution_log")
            record = records[_item_key(item.url)]
            self.assertEqual(record["status"], "sent")
            self.assertEqual(record["message_id"], 123)
            self.assertEqual(next(iter(logs.values()))["channel_id"], 456)

    async def test_gemini_failure_uses_factual_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            monitor = FakeMonitor(
                StateStore(Path(directory) / "state.json")
            )
            result = await monitor.analyze(
                ContentItem(
                    source_key="articles",
                    platform="Artigos",
                    url="https://www.talissonsouza.com.br/artigos/apis",
                    title="Como APIs funcionam",
                    description="Uma introdução prática ao funcionamento de APIs.",
                )
            )

            self.assertEqual(result["analysis_mode"], "local_fallback")
            self.assertIn("APIs", result["content_title"])
            self.assertIn("introdução prática", result["summary"])

    def test_social_and_article_urls_are_extracted_and_normalized(self) -> None:
        instagram = ContentSource(
            "instagram",
            "Instagram",
            "https://www.instagram.com/talisson.dev/",
        )
        articles = ContentSource(
            "articles",
            "Artigos",
            "https://www.talissonsouza.com.br/artigos",
        )

        social_items = _items_from_html(
            '<a href="/reel/ABC_123/?utm_source=test">Reel</a>',
            instagram.profile_url,
            instagram,
        )
        article_items = _items_from_html(
            '"/artigos/como-criar-uma-api"',
            articles.profile_url,
            articles,
        )

        self.assertEqual(
            _normalize_url(social_items[0].url),
            "https://www.instagram.com/reel/ABC_123",
        )
        self.assertEqual(
            _normalize_url(article_items[0].url),
            "https://www.talissonsouza.com.br/artigos/como-criar-uma-api",
        )

    def test_embed_contains_professional_content_fields(self) -> None:
        item = ContentItem(
            source_key="articles",
            platform="Artigos",
            url="https://www.talissonsouza.com.br/artigos/apis",
            title="Como APIs funcionam",
            image_url="https://example.com/capa.png",
            detected_at="2026-06-12T12:00:00+00:00",
        )
        embed = content_announcement_embed(
            item,
            {
                "introduction": "Novo material técnico disponível.",
                "content_title": "Como APIs funcionam",
                "summary": "Uma visão objetiva sobre integração entre sistemas.",
                "main_theme": "APIs",
                "reader_value": "Compreender comunicação entre aplicações.",
                "audience": "Desenvolvedores iniciantes.",
            },
        )
        payload = embed.to_dict()

        self.assertEqual(payload["title"], "Novo Conteúdo Publicado")
        self.assertEqual(payload["author"]["name"], "Talisson Souza")
        self.assertEqual(payload["image"]["url"], item.image_url)
        self.assertIn(
            "Plataforma",
            {field["name"] for field in payload["fields"]},
        )


if __name__ == "__main__":
    unittest.main()
