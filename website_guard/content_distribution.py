from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp
import discord

from src.ai.groq_service import GroqService
from src.ai.moderation_ai import sanitize_output

from .config import Settings
from .http_utils import read_limited
from .state import StateStore

LOGGER = logging.getLogger(__name__)
ITEMS_SECTION = "content_distribution_items"
SOURCES_SECTION = "content_distribution_sources"
LOG_SECTION = "content_distribution_log"
ASSETS_SECTION = "content_distribution_assets"


@dataclass(frozen=True)
class ContentSource:
    key: str
    platform: str
    profile_url: str
    feed_url: str = ""
    token: str = field(default="", repr=False)


@dataclass
class ContentItem:
    source_key: str
    platform: str
    url: str
    title: str = ""
    description: str = ""
    content: str = ""
    image_url: str = ""
    published_at: str = ""
    detected_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContentItem":
        return cls(
            source_key=str(data.get("source_key", "")),
            platform=str(data.get("platform", "")),
            url=str(data.get("url", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            content=str(data.get("content", "")),
            image_url=str(data.get("image_url", "")),
            published_at=str(data.get("published_at", "")),
            detected_at=str(data.get("detected_at", "")),
        )


@dataclass
class ContentScanReport:
    checked_at: str
    discovered: int = 0
    queued: int = 0
    seeded: int = 0
    items: list[ContentItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source_status: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "discovered": self.discovered,
            "queued": self.queued,
            "seeded": self.seeded,
            "items": [item.to_dict() for item in self.items],
            "errors": list(self.errors),
            "source_status": dict(self.source_status),
        }


class ContentDistributionMonitor:
    def __init__(
        self,
        settings: Settings,
        state: StateStore,
        service: GroqService,
    ) -> None:
        self.settings = settings
        self.state = state
        self.service = service
        self.sources = (
            ContentSource(
                "instagram",
                "Instagram",
                settings.content_instagram_url,
                settings.content_instagram_feed_url,
                settings.content_instagram_token,
            ),
            ContentSource(
                "linkedin",
                "LinkedIn",
                settings.content_linkedin_url,
                settings.content_linkedin_feed_url,
                settings.content_linkedin_token,
            ),
            ContentSource(
                "tiktok",
                "TikTok",
                settings.content_tiktok_url,
                settings.content_tiktok_feed_url,
                settings.content_tiktok_token,
            ),
            ContentSource(
                "x",
                "X",
                settings.content_x_url,
                settings.content_x_feed_url,
                settings.content_x_token,
            ),
            ContentSource(
                "articles",
                "Artigos",
                settings.content_articles_url,
                settings.content_articles_feed_url,
            ),
        )

    async def scan(
        self,
        session: aiohttp.ClientSession,
    ) -> ContentScanReport:
        checked_at = datetime.now(timezone.utc).isoformat()
        report = ContentScanReport(checked_at=checked_at)
        records = await self.state.get_section(ITEMS_SECTION)
        source_states = await self.state.get_section(SOURCES_SECTION)

        for source in self.sources:
            if not source.profile_url:
                continue
            current_state = dict(source_states.get(source.key, {}))
            try:
                discovered = await self._discover_source(
                    session,
                    source,
                    initialized=bool(current_state.get("initialized_at")),
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                report.errors.append(f"{source.platform}: {message}")
                report.source_status[source.key] = "indisponível"
                current_state.update(
                    {
                        "platform": source.platform,
                        "profile_url": source.profile_url,
                        "last_checked_at": checked_at,
                        "last_error": message,
                    }
                )
                source_states[source.key] = current_state
                continue

            report.discovered += len(discovered)
            report.source_status[source.key] = (
                "monitorada" if discovered else "sem itens públicos"
            )
            current_state.update(
                {
                    "platform": source.platform,
                    "profile_url": source.profile_url,
                    "last_checked_at": checked_at,
                    "last_error": None,
                    "last_discovered": len(discovered),
                }
            )
            initialized = bool(current_state.get("initialized_at"))
            if discovered and not initialized:
                current_state["initialized_at"] = checked_at
                announce_existing = (
                    self.settings.content_distribution_announce_existing
                )
                existing_candidate = discovered[0].url if announce_existing else ""
                for item in discovered:
                    item.detected_at = checked_at
                    key = _item_key(item.url)
                    if key in records:
                        continue
                    status = (
                        "pending"
                        if item.url == existing_candidate
                        else "seeded"
                    )
                    records[key] = _record(item, status=status)
                    if status == "pending":
                        report.queued += 1
                    else:
                        report.seeded += 1
            elif initialized:
                for item in reversed(discovered):
                    item.detected_at = checked_at
                    key = _item_key(item.url)
                    if key in records:
                        continue
                    records[key] = _record(item, status="pending")
                    report.queued += 1
            source_states[source.key] = current_state

        pending = [
            (key, value)
            for key, value in records.items()
            if isinstance(value, dict)
            and value.get("status") in {"pending", "failed"}
        ]
        pending.sort(
            key=lambda entry: str(entry[1].get("detected_at", ""))
        )
        selected = pending[
            : self.settings.content_distribution_max_items_per_scan
        ]
        for key, record in selected:
            item_data = record.get("item", {})
            if not isinstance(item_data, dict):
                continue
            item = ContentItem.from_dict(item_data)
            try:
                item = await self._enrich_item(session, item)
                record["item"] = item.to_dict()
                record["enrichment_error"] = None
            except Exception as exc:
                record["enrichment_error"] = f"{type(exc).__name__}: {exc}"
            records[key] = record
            report.items.append(item)

        await self.state.set_many(
            {
                ITEMS_SECTION: _trim_records(records),
                SOURCES_SECTION: source_states,
                "content_distribution_last_scan": report.to_dict(),
            }
        )
        return report

    async def analyze(self, item: ContentItem) -> dict[str, str]:
        context = {
            "platform": item.platform,
            "url": item.url,
            "title": item.title,
            "description": item.description,
            "content": item.content[:7000],
            "published_at": item.published_at,
            "detected_at": item.detected_at,
        }
        schema = {
            "type": "object",
            "properties": {
                "introduction": {"type": "string"},
                "content_title": {"type": "string"},
                "summary": {"type": "string"},
                "main_theme": {"type": "string"},
                "reader_value": {"type": "string"},
                "audience": {"type": "string"},
            },
            "required": [
                "introduction",
                "content_title",
                "summary",
                "main_theme",
                "reader_value",
                "audience",
            ],
            "additionalProperties": False,
        }
        prompt = (
            "Analise uma nova publicação oficial de Talisson Souza e gere o texto "
            "de divulgação em português brasileiro. Use somente os dados reais "
            "fornecidos. A introdução deve ser curta, natural e diferente de uma "
            "notificação genérica. O resumo deve explicar o assunto em até 420 "
            "caracteres. Informe o tema principal, o benefício para o leitor e o "
            "público mais adequado. Não invente fatos, não use emojis, não cite "
            "Não use linguagem de terminal e não repita o link no texto. "
            "Se o conteúdo disponível for limitado, seja factual e reconheça "
            "apenas o que pode ser inferido pelo título e descrição.\n\n"
            f"Dados da publicação: {json.dumps(context, ensure_ascii=False)}"
        )
        try:
            data = await self.service.generate_json(
                prompt,
                schema,
                max_output_tokens=850,
                timeout_seconds=12,
            )
            result = {
                key: sanitize_output(str(data.get(key, "")), limit)
                for key, limit in (
                    ("introduction", 300),
                    ("content_title", 220),
                    ("summary", 650),
                    ("main_theme", 250),
                    ("reader_value", 420),
                    ("audience", 250),
                )
            }
            if result["summary"] and result["introduction"]:
                result["analysis_mode"] = "groq"
                return result
        except Exception as exc:
            await self.state.set(
                "content_distribution_last_ai_error",
                f"{type(exc).__name__}: {exc}",
            )
        return _local_analysis(item)

    async def mark_sent(
        self,
        item: ContentItem,
        *,
        message_id: int,
        channel_id: int,
        analysis_mode: str,
    ) -> None:
        await self._update_delivery(
            item,
            status="sent",
            message_id=message_id,
            channel_id=channel_id,
            analysis_mode=analysis_mode,
        )

    async def mark_failed(
        self,
        item: ContentItem,
        error: str,
    ) -> None:
        await self._update_delivery(
            item,
            status="failed",
            error=error,
        )

    async def _update_delivery(
        self,
        item: ContentItem,
        *,
        status: str,
        message_id: int | None = None,
        channel_id: int | None = None,
        analysis_mode: str = "",
        error: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        key = _item_key(item.url)

        def update_records(records: dict[str, Any]) -> None:
            current = dict(records.get(key, _record(item, status=status)))
            current.update(
                {
                    "status": status,
                    "last_attempt_at": now,
                    "attempts": int(current.get("attempts", 0)) + 1,
                    "error": error or None,
                    "message_id": message_id,
                    "channel_id": channel_id,
                    "analysis_mode": analysis_mode,
                }
            )
            if status == "sent":
                current["sent_at"] = now
            records[key] = current

        await self.state.mutate_section(ITEMS_SECTION, update_records)

        def append_log(logs: dict[str, Any]) -> None:
            log_key = f"{now}:{key[:12]}"
            logs[log_key] = {
                "platform": item.platform,
                "url": item.url,
                "detected_at": item.detected_at,
                "processed_at": now,
                "status": status,
                "message_id": message_id,
                "channel_id": channel_id,
                "analysis_mode": analysis_mode,
                "error": error or None,
            }
            while len(logs) > 1000:
                logs.pop(next(iter(logs)))

        await self.state.mutate_section(LOG_SECTION, append_log)

    async def _discover_source(
        self,
        session: aiohttp.ClientSession,
        source: ContentSource,
        *,
        initialized: bool,
    ) -> list[ContentItem]:
        target_url = source.feed_url or source.profile_url
        body, content_type, final_url = await _fetch(
            session,
            target_url,
            token=source.token,
            limit=4 * 1024 * 1024,
        )
        text = body.decode("utf-8", errors="replace")
        stripped = text.lstrip()
        if "json" in content_type or stripped.startswith(("{", "[")):
            raw_items = _items_from_json(text, source)
        elif (
            "xml" in content_type
            or stripped.startswith("<?xml")
            or stripped.startswith("<rss")
            or stripped.startswith("<feed")
        ):
            raw_items = _items_from_xml(text, source)
        else:
            raw_items = _items_from_html(text, final_url, source)
            if source.key == "articles":
                raw_items.extend(
                    await self._discover_article_bundle(
                        session,
                        text,
                        final_url,
                        source,
                        initialized=initialized,
                    )
                )
        unique: dict[str, ContentItem] = {}
        for item in raw_items:
            normalized = _normalize_url(item.url)
            if not normalized or not _matches_source(normalized, source.key):
                continue
            item.url = normalized
            unique.setdefault(normalized, item)
        return list(unique.values())[:100]

    async def _discover_article_bundle(
        self,
        session: aiohttp.ClientSession,
        page_html: str,
        page_url: str,
        source: ContentSource,
        *,
        initialized: bool,
    ) -> list[ContentItem]:
        parser = _PageParser()
        parser.feed(page_html)
        asset_url = next(
            (
                urljoin(page_url, value)
                for value in parser.scripts
                if re.search(r"/assets/index-[^/]+\.js(?:$|\?)", value)
            ),
            "",
        )
        if not asset_url:
            return []
        assets = await self.state.get_section(ASSETS_SECTION)
        if initialized and assets.get(source.key) == asset_url:
            return []
        body, _, _ = await _fetch(
            session,
            asset_url,
            limit=8 * 1024 * 1024,
        )
        text = body.decode("utf-8", errors="replace")
        items = _items_from_html(text, source.profile_url, source)
        await self.state.update_section(ASSETS_SECTION, source.key, asset_url)
        return items

    async def _enrich_item(
        self,
        session: aiohttp.ClientSession,
        item: ContentItem,
    ) -> ContentItem:
        body, _, final_url = await _fetch(
            session,
            item.url,
            limit=3 * 1024 * 1024,
        )
        text = body.decode("utf-8", errors="replace")
        details = _page_details(text, final_url)
        item.title = details.get("title") or item.title
        item.description = details.get("description") or item.description
        item.content = details.get("content") or item.content
        item.image_url = details.get("image_url") or item.image_url
        item.published_at = details.get("published_at") or item.published_at
        if (
            item.source_key == "articles"
            and self.settings.render_dynamic_pages
            and len(item.content) < 500
        ):
            rendered = await self._render_article(item.url)
            item.title = rendered.get("title") or item.title
            item.description = rendered.get("description") or item.description
            item.content = rendered.get("content") or item.content
            item.image_url = rendered.get("image_url") or item.image_url
        item.title = _clean_text(item.title)[:240]
        item.description = _clean_text(item.description)[:1500]
        item.content = _clean_text(item.content)[:8000]
        return item

    async def _render_article(self, url: str) -> dict[str, str]:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=self.settings.browser_headless,
            )
            page = await browser.new_page(
                user_agent="Bella-Content-Monitor/3.3",
                locale="pt-BR",
            )
            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.settings.request_timeout_seconds * 1000,
                )
                await page.wait_for_timeout(1000)
                return await page.evaluate(
                    """
                    () => {
                      const root =
                        document.querySelector("article") ||
                        document.querySelector("main") ||
                        document.querySelector('[role="main"]') ||
                        document.body;
                      const clone = root.cloneNode(true);
                      clone.querySelectorAll(
                        "script,style,noscript,svg,nav,footer,button"
                      ).forEach((node) => node.remove());
                      const meta = (selector) =>
                        document.querySelector(selector)?.content || "";
                      return {
                        title:
                          document.querySelector("article h1, main h1, h1")
                            ?.textContent ||
                          meta('meta[property="og:title"]') ||
                          document.title,
                        description:
                          meta('meta[property="og:description"]') ||
                          meta('meta[name="description"]'),
                        image_url: meta('meta[property="og:image"]'),
                        content: clone.innerText || clone.textContent || "",
                      };
                    }
                    """
                )
            finally:
                await page.close()
                await browser.close()


def content_announcement_embed(
    item: ContentItem,
    analysis: dict[str, str],
) -> discord.Embed:
    timestamp = _parse_datetime(item.published_at or item.detected_at)
    embed = discord.Embed(
        title="Novo Conteúdo Publicado",
        description=_clip(
            analysis.get("introduction")
            or "Uma nova publicação oficial está disponível.",
            500,
        ),
        url=item.url,
        color=discord.Color.from_rgb(28, 105, 212),
        timestamp=timestamp,
    )
    embed.set_author(name="Talisson Souza")
    content_title = analysis.get("content_title") or item.title
    if content_title:
        embed.add_field(
            name="Conteúdo",
            value=_clip(content_title, 1024),
            inline=False,
        )
    embed.add_field(name="Plataforma", value=item.platform, inline=True)
    embed.add_field(
        name="Data",
        value=f"<t:{int(timestamp.timestamp())}:F>",
        inline=True,
    )
    embed.add_field(
        name="Resumo",
        value=_clip(
            analysis.get("summary")
            or item.description
            or item.title
            or "Publicação oficial recém-identificada.",
            1024,
        ),
        inline=False,
    )
    theme = analysis.get("main_theme", "")
    value = analysis.get("reader_value", "")
    if theme or value:
        embed.add_field(
            name="Por que conferir",
            value=_clip(
                "\n".join(
                    part
                    for part in (
                        f"**Tema:** {theme}" if theme else "",
                        f"**Valor:** {value}" if value else "",
                    )
                    if part
                ),
                1024,
            ),
            inline=False,
        )
    audience = analysis.get("audience", "")
    if audience:
        embed.add_field(
            name="Público recomendado",
            value=_clip(audience, 1024),
            inline=False,
        )
    embed.add_field(
        name="Acesso",
        value=f"[Abrir publicação]({item.url})",
        inline=False,
    )
    if item.image_url.startswith(("http://", "https://")):
        embed.set_image(url=item.image_url)
    embed.set_footer(text="Bella | Distribuição automática de conteúdo")
    return embed


async def _fetch(
    session: aiohttp.ClientSession,
    url: str,
    *,
    token: str = "",
    limit: int,
) -> tuple[bytes, str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/137 Safari/537.36 "
            "Bella-Content-Monitor/3.3"
        ),
        "Accept": (
            "application/atom+xml,application/rss+xml,application/json,"
            "text/html;q=0.9,*/*;q=0.7"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with session.get(
        url,
        allow_redirects=True,
        headers=headers,
    ) as response:
        body = await read_limited(response.content, limit)
        if response.status >= 400:
            raise RuntimeError(f"HTTP {response.status} em {url}")
        return (
            body,
            response.headers.get("Content-Type", "").casefold(),
            str(response.url),
        )


def _items_from_xml(text: str, source: ContentSource) -> list[ContentItem]:
    root = ElementTree.fromstring(text)
    items: list[ContentItem] = []
    for node in root.iter():
        if _local_name(node.tag) not in {"item", "entry"}:
            continue
        values: dict[str, str] = {}
        for child in node.iter():
            name = _local_name(child.tag)
            value = (child.text or "").strip()
            if name == "link":
                value = child.attrib.get("href", "") or value
            if name in {
                "title",
                "link",
                "description",
                "summary",
                "content",
                "published",
                "updated",
                "pubdate",
            } and value:
                values.setdefault(name, value)
            if name in {"thumbnail", "content"}:
                media_url = child.attrib.get("url", "")
                if media_url:
                    values.setdefault("image", media_url)
        url = values.get("link", "")
        if not url:
            continue
        items.append(
            ContentItem(
                source_key=source.key,
                platform=source.platform,
                url=url,
                title=_strip_markup(values.get("title", "")),
                description=_strip_markup(
                    values.get("description")
                    or values.get("summary", "")
                ),
                content=_strip_markup(values.get("content", "")),
                image_url=values.get("image", ""),
                published_at=(
                    values.get("published")
                    or values.get("updated")
                    or values.get("pubdate", "")
                ),
            )
        )
    return items


def _items_from_json(text: str, source: ContentSource) -> list[ContentItem]:
    data = json.loads(text)
    items: list[ContentItem] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for entry in value[:500]:
                visit(entry)
            return
        if not isinstance(value, dict):
            return
        url = _first_string(
            value,
            "permalink",
            "share_url",
            "web_url",
            "post_url",
            "link",
            "url",
        )
        if (
            not url
            and source.key == "x"
            and str(value.get("id", "")).isdigit()
            and _first_string(value, "text", "full_text")
        ):
            username = urlparse(source.profile_url).path.strip("/@")
            url = f"https://x.com/{username}/status/{value['id']}"
        if url and _matches_source(_normalize_url(url), source.key):
            image = _first_string(
                value,
                "media_url",
                "media_url_https",
                "thumbnail_url",
                "image",
                "cover_image_url",
            )
            items.append(
                ContentItem(
                    source_key=source.key,
                    platform=source.platform,
                    url=url,
                    title=_first_string(value, "title", "headline", "name"),
                    description=_first_string(
                        value,
                        "caption",
                        "description",
                        "summary",
                        "text",
                        "full_text",
                    ),
                    content=_first_string(
                        value,
                        "content",
                        "article_body",
                        "body",
                    ),
                    image_url=image,
                    published_at=_first_string(
                        value,
                        "published_at",
                        "created_at",
                        "datePublished",
                        "timestamp",
                    ),
                )
            )
        for child in value.values():
            if isinstance(child, (dict, list)):
                visit(child)

    visit(data)
    return items


def _items_from_html(
    text: str,
    base_url: str,
    source: ContentSource,
) -> list[ContentItem]:
    parser = _PageParser()
    try:
        parser.feed(text)
    except Exception:
        pass
    candidates = list(parser.links)
    raw = html.unescape(text).replace("\\/", "/").replace("\\u002F", "/")
    candidates.extend(re.findall(r"https?://[^\s\"'<>\\]+", raw))
    relative_patterns = {
        "instagram": r"/(?:p|reel|tv)/[A-Za-z0-9_-]+/?",
        "linkedin": (
            r"/(?:posts/[A-Za-z0-9_%:-]+|feed/update/"
            r"urn:li:(?:activity|share):\d+)/?"
        ),
        "tiktok": r"/@[A-Za-z0-9._-]+/video/\d+/?",
        "x": r"/[A-Za-z0-9_]+/status/\d+/?",
        "articles": r"/artigos/[A-Za-z0-9_-]+/?",
    }
    pattern = relative_patterns[source.key]
    candidates.extend(re.findall(pattern, raw, flags=re.IGNORECASE))
    items = []
    for candidate in candidates:
        url = urljoin(base_url, candidate)
        if not _matches_source(_normalize_url(url), source.key):
            continue
        items.append(
            ContentItem(
                source_key=source.key,
                platform=source.platform,
                url=url,
            )
        )
    return items


def _page_details(text: str, url: str) -> dict[str, str]:
    parser = _PageParser()
    try:
        parser.feed(text)
    except Exception:
        pass
    metadata = parser.metadata
    details = {
        "title": (
            metadata.get("og:title")
            or metadata.get("twitter:title")
            or parser.title
        ),
        "description": (
            metadata.get("og:description")
            or metadata.get("twitter:description")
            or metadata.get("description", "")
        ),
        "image_url": (
            metadata.get("og:image")
            or metadata.get("twitter:image")
            or ""
        ),
        "published_at": (
            metadata.get("article:published_time")
            or metadata.get("date")
            or ""
        ),
        "content": " ".join(parser.text_parts),
    }
    for payload in re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>"
        r"(.*?)</script>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        try:
            data = json.loads(html.unescape(payload))
        except (json.JSONDecodeError, TypeError):
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            details["title"] = details["title"] or str(
                entry.get("headline") or entry.get("name") or ""
            )
            details["description"] = details["description"] or str(
                entry.get("description") or ""
            )
            details["content"] = details["content"] or str(
                entry.get("articleBody") or ""
            )
            details["published_at"] = details["published_at"] or str(
                entry.get("datePublished") or ""
            )
            image = entry.get("image")
            if isinstance(image, list):
                image = image[0] if image else ""
            if isinstance(image, dict):
                image = image.get("url", "")
            details["image_url"] = details["image_url"] or str(image or "")
    if details["image_url"]:
        details["image_url"] = urljoin(url, details["image_url"])
    return {key: _clean_text(value) for key, value in details.items()}


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.scripts: list[str] = []
        self.metadata: dict[str, str] = {}
        self.text_parts: list[str] = []
        self.title = ""
        self._title_depth = 0
        self._skip_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        lowered = tag.casefold()
        if lowered == "a" and values.get("href"):
            self.links.append(values["href"])
        elif lowered == "script":
            if values.get("src"):
                self.scripts.append(values["src"])
            self._skip_depth += 1
        elif lowered in {"style", "noscript", "svg"}:
            self._skip_depth += 1
        elif lowered == "meta":
            key = str(
                values.get("property")
                or values.get("name")
                or values.get("itemprop")
                or ""
            ).casefold()
            content = values.get("content", "")
            if key and content:
                self.metadata.setdefault(key, content)
        elif lowered == "title":
            self._title_depth += 1

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in {"script", "style", "noscript", "svg"}:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif lowered == "title":
            self._title_depth = max(0, self._title_depth - 1)

    def handle_data(self, data: str) -> None:
        value = _clean_text(data)
        if not value:
            return
        if self._title_depth:
            self.title = f"{self.title} {value}".strip()
        elif not self._skip_depth and len(value) > 1:
            self.text_parts.append(value)


def _local_analysis(item: ContentItem) -> dict[str, str]:
    variants = (
        "Uma nova publicação oficial de Talisson Souza já está disponível.",
        "Talisson Souza acaba de disponibilizar um novo conteúdo para a comunidade.",
        "Um novo material de Talisson Souza foi identificado e já pode ser acessado.",
        "Há uma nova publicação de Talisson Souza disponível para consulta.",
    )
    index = int(hashlib.sha256(item.url.encode()).hexdigest()[:4], 16)
    base = item.description or item.content or item.title
    summary = _clip(
        _clean_text(base)
        or f"Novo conteúdo publicado por Talisson Souza no {item.platform}.",
        620,
    )
    return {
        "introduction": variants[index % len(variants)],
        "content_title": item.title or f"Nova publicação no {item.platform}",
        "summary": summary,
        "main_theme": item.title or item.platform,
        "reader_value": (
            "O material apresenta informações diretamente na fonte oficial."
        ),
        "audience": "Pessoas interessadas no conteúdo profissional de Talisson Souza.",
        "analysis_mode": "local_fallback",
    }


def _record(item: ContentItem, *, status: str) -> dict[str, Any]:
    return {
        "platform": item.platform,
        "url": item.url,
        "detected_at": item.detected_at,
        "status": status,
        "attempts": 0,
        "message_id": None,
        "channel_id": None,
        "error": None,
        "item": item.to_dict(),
    }


def _trim_records(records: dict[str, Any]) -> dict[str, Any]:
    if len(records) <= 5000:
        return records
    ordered = sorted(
        records.items(),
        key=lambda entry: str(entry[1].get("detected_at", ""))
        if isinstance(entry[1], dict)
        else "",
        reverse=True,
    )
    return dict(ordered[:5000])


def _matches_source(url: str, source_key: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.casefold().removeprefix("www.")
    path = parsed.path
    if source_key == "instagram":
        return host == "instagram.com" and bool(
            re.fullmatch(r"/(?:p|reel|tv)/[A-Za-z0-9_-]+/?", path)
        )
    if source_key == "linkedin":
        return host == "linkedin.com" and bool(
            re.search(
                r"/(?:posts/|feed/update/urn:li:(?:activity|share):)",
                path,
                flags=re.IGNORECASE,
            )
        )
    if source_key == "tiktok":
        return host == "tiktok.com" and bool(
            re.fullmatch(
                r"/@[A-Za-z0-9._-]+/video/\d+/?",
                path,
                flags=re.IGNORECASE,
            )
        )
    if source_key == "x":
        return host in {"x.com", "twitter.com"} and bool(
            re.fullmatch(
                r"/[A-Za-z0-9_]+/status/\d+/?",
                path,
                flags=re.IGNORECASE,
            )
        )
    return host.endswith("talissonsouza.com.br") and bool(
        re.fullmatch(r"/artigos/[A-Za-z0-9_-]+/?", path)
    )


def _normalize_url(value: str) -> str:
    if not value:
        return ""
    cleaned = html.unescape(value).replace("\\/", "/").strip(" \"'<>")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    host = parsed.netloc.casefold()
    if host in {"twitter.com", "www.twitter.com"}:
        host = "x.com"
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    return urlunparse(("https", host, path, "", "", ""))


def _item_key(url: str) -> str:
    return hashlib.sha256(_normalize_url(url).encode("utf-8")).hexdigest()


def _first_string(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = _first_string(value, "url", "src", "text", "value")
            if nested:
                return nested
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                nested = _first_string(first, "url", "src", "text", "value")
                if nested:
                    return nested
    return ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _strip_markup(value: str) -> str:
    return _clean_text(re.sub(r"<[^>]+>", " ", html.unescape(value)))


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _clip(value: str, limit: int) -> str:
    cleaned = _clean_text(value)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)].rstrip() + "…"


def _parse_datetime(value: str) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)
