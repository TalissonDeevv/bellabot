from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

import aiohttp
from rapidfuzz import fuzz

from .config import Settings
from .http_utils import read_limited
from .identity import OWNER_DISCORD_ID
from .state import StateStore

MAX_KNOWLEDGE_BYTES = 6 * 1024 * 1024
JS_STRING = r'"(?P<{name}>(?:\\.|[^"\\])*)"'
FAQ_PAIR_RE = re.compile(
    r"\{q:"
    + JS_STRING.format(name="question")
    + r",a:"
    + JS_STRING.format(name="answer")
    + r"\}",
    re.DOTALL,
)
QUESTION_START_RE = re.compile(
    r"^(?:"
    r"como|qual|quais|quem|onde|quando|quanto|quantos|quanta|quantas|"
    r"por que|porque|o que|que tipo|há quanto|tem como|"
    r"você|voce|vocês|voces|aceita|faz|oferece|posso|pode|"
    r"poderia|consegue|existe|há|ha|seria"
    r")\b",
    re.IGNORECASE,
)
QUESTION_PHRASE_RE = re.compile(
    r"\b(?:"
    r"queria saber|gostaria de saber|poderia me dizer|"
    r"algu[eé]m sabe|tenho uma d[uú]vida|me explica|"
    r"como fa[çc]o|onde encontro|qual [ée]"
    r")\b",
    re.IGNORECASE,
)
CASUAL_ONLY_RE = re.compile(
    r"^(?:oi+|ol[aá]|opa|e a[ií]|bom dia|boa tarde|boa noite|"
    r"tudo bem|valeu|obrigad[oa]|show|legal|parab[eé]ns|"
    r"ficou (?:muito )?(?:bom|bonito|legal|top))[\s!.]*$",
    re.IGNORECASE,
)
NON_SITE_GENERIC_RE = re.compile(
    r"^(?:"
    r"quem [ée] voc[eê]|o que voc[eê] [ée]|como voc[eê] est[aá]|"
    r"o que voc[eê] acha|qual (?:[ée] )?a sua opini[aã]o|"
    r"voc[eê] gosta de\b"
    r")",
    re.IGNORECASE,
)
CASUAL_NORMALIZED = {
    "oi",
    "ola",
    "opa",
    "e ai",
    "oi tudo bem",
    "ola tudo bem",
    "bom dia",
    "boa tarde",
    "boa noite",
    "tudo bem",
    "valeu",
    "obrigado",
    "obrigada",
    "parabens",
}
STOPWORDS = {
    "a",
    "ao",
    "aos",
    "as",
    "com",
    "como",
    "da",
    "das",
    "de",
    "do",
    "dos",
    "e",
    "ela",
    "ele",
    "em",
    "essa",
    "esse",
    "esta",
    "este",
    "eu",
    "ha",
    "isso",
    "me",
    "melhor",
    "minha",
    "meu",
    "na",
    "nas",
    "no",
    "nos",
    "o",
    "os",
    "para",
    "pela",
    "pelo",
    "por",
    "posso",
    "qual",
    "quais",
    "que",
    "se",
    "seu",
    "sua",
    "talisson",
    "tem",
    "uma",
    "um",
    "voce",
}
CONCEPT_GROUPS = {
    "contato": {
        "contato",
        "contactar",
        "conversar",
        "falar",
        "mensagem",
        "chamar",
        "whatsapp",
        "email",
    },
    "preco": {
        "custa",
        "custo",
        "investimento",
        "orcamento",
        "preco",
        "valor",
        "valores",
    },
    "prazo": {
        "demora",
        "duracao",
        "leva",
        "prazo",
        "tempo",
    },
    "projeto": {
        "aplicacao",
        "aplicativo",
        "automacao",
        "bot",
        "ecommerce",
        "plataforma",
        "projeto",
        "sistema",
        "site",
    },
    "tecnologia": {
        "framework",
        "linguagem",
        "stack",
        "tecnologia",
        "tecnologias",
    },
    "manutencao": {
        "ajuste",
        "correcao",
        "manutencao",
        "suporte",
        "atualizacao",
    },
    "formacao": {
        "curso",
        "diploma",
        "estudo",
        "faculdade",
        "formacao",
    },
    "experiencia": {
        "carreira",
        "experiencia",
        "trajetoria",
        "trabalha",
    },
    "consultoria": {
        "analise",
        "consultoria",
        "diagnostico",
        "revisao",
    },
    "mentoria": {
        "ensinar",
        "mentor",
        "mentoria",
        "orientacao",
    },
    "comunidade": {
        "comunidade",
        "discord",
        "servidor",
    },
    "social": {
        "discord",
        "github",
        "instagram",
        "linkedin",
        "rede",
        "redes",
        "seguir",
        "social",
        "sociais",
        "whatsapp",
    },
    "empresa": {
        "ceo",
        "cheetah",
        "companhia",
        "empresa",
        "empresas",
        "fundador",
        "negocio",
        "negocios",
    },
    "portfolio": {
        "portfolio",
        "projetos",
        "trabalhos",
    },
    "seguranca": {
        "criptografia",
        "protecao",
        "seguranca",
        "vulnerabilidade",
    },
    "responsivo": {
        "celular",
        "mobile",
        "responsivo",
        "tablet",
    },
    "internacional": {
        "brasil",
        "exterior",
        "fora",
        "internacional",
        "mundo",
    },
    "horario": {
        "atendimento",
        "disponibilidade",
        "horario",
    },
}
CONCEPT_LOOKUP = {
    word: concept for concept, words in CONCEPT_GROUPS.items() for word in words
}


@dataclass(frozen=True)
class KnowledgeEntry:
    entry_id: str
    question: str
    answer: str
    source_url: str
    source_title: str
    source_type: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnowledgeEntry":
        return cls(**data)


@dataclass(frozen=True)
class KnowledgeMatch:
    entry: KnowledgeEntry | None
    question_intent: float
    semantic_match: float
    site_relation: float
    answer_quality: float
    final_confidence: float
    should_reply: bool
    reason: str
    related_entries: tuple[KnowledgeEntry, ...] = ()


@dataclass
class KnowledgeRefreshReport:
    checked_at: str
    entries: int = 0
    faq_entries: int = 0
    page_entries: int = 0
    pages_processed: int = 0
    duration_ms: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class KnowledgeAnalyticsSnapshot:
    evaluated: int
    answered: int
    ignored: int
    average_answer_confidence: float
    reasons: dict[str, int]
    daily: dict[str, dict[str, Any]]


class KnowledgeBase:
    def __init__(self, settings: Settings, state: StateStore) -> None:
        self.settings = settings
        self.state = state
        self._entries: list[KnowledgeEntry] = []
        self._lock = asyncio.Lock()

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    async def load_cached(self) -> int:
        data = await self.state.get("knowledge_entries", [])
        entries = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    entries.append(KnowledgeEntry.from_dict(item))
                except TypeError:
                    continue
        async with self._lock:
            self._entries = entries
        return len(entries)

    async def candidates(
        self,
        message: str,
        limit: int = 8,
        *,
        enforce_question: bool = True,
    ) -> tuple[float, tuple[KnowledgeEntry, ...]]:
        intent = question_intent_score(
            message,
            max_length=self.settings.knowledge_max_message_length,
        )
        if enforce_question and intent < self.settings.question_intent_min:
            return intent, ()
        if not self._entries:
            await self.load_cached()
        async with self._lock:
            entries = list(self._entries)
        if not entries:
            return intent, ()

        normalized_message = normalize_entity_references(
            message,
            getattr(self.settings, "talisson_user_id", OWNER_DISCORD_ID),
        )
        semantic_message = normalized_message.replace("TALISSON_SOUZA", " ")
        requested_link = _requested_link_label(semantic_message)
        if requested_link:
            entries = [
                entry
                for entry in entries
                if entry.source_type == "link"
                and entry.question.casefold().startswith(requested_link.casefold())
            ]
        ranked = sorted(
            (_rank_entry(semantic_message, entry) for entry in entries),
            key=lambda item: (
                item[0],
                item[1],
                item[2],
                item[3].source_type == "faq",
            ),
            reverse=True,
        )
        return intent, tuple(item[3] for item in ranked[: max(1, limit)])

    async def refresh(
        self,
        session: aiohttp.ClientSession,
    ) -> KnowledgeRefreshReport:
        started = time.perf_counter()
        report = KnowledgeRefreshReport(
            checked_at=datetime.now(timezone.utc).isoformat()
        )
        entries: list[KnowledgeEntry] = []
        try:
            faq_entries = await self._load_faq_entries(session)
            entries.extend(faq_entries)
            report.faq_entries = len(faq_entries)
        except Exception as exc:
            report.warnings.append(f"FAQ indisponível: {type(exc).__name__}: {exc}")

        try:
            if self.settings.render_dynamic_pages:
                page_entries, pages_processed = await self._load_page_entries()
            else:
                # Sem Chromium (RENDER_DYNAMIC_PAGES=false), para economizar RAM:
                # lê o HTML simples e mantém as páginas aprendidas na última
                # leitura completa, porque o site é uma SPA e o HTML cru tem
                # pouco texto.
                page_entries, pages_processed = await self._load_static_page_entries(
                    session
                )
                page_entries.extend(await self._cached_page_entries())
            entries.extend(page_entries)
            report.page_entries = len(page_entries)
            report.pages_processed = pages_processed
        except Exception as exc:
            report.warnings.append(
                f"Páginas indisponíveis: {type(exc).__name__}: {exc}"
            )

        entries = _deduplicate_entries(entries)
        if not entries:
            report.error = "Nenhuma entrada oficial pôde ser carregada."
            if not self._entries:
                await self.load_cached()
        else:
            async with self._lock:
                self._entries = entries
            await self.state.set_many(
                {
                    "knowledge_entries": [entry.to_dict() for entry in entries],
                    "knowledge_last_refresh": report.checked_at,
                }
            )

        report.entries = len(entries) if entries else len(self._entries)
        report.duration_ms = int((time.perf_counter() - started) * 1000)
        await self.state.set("latest_knowledge_report", report.to_dict())
        return report

    async def match(self, message: str) -> KnowledgeMatch:
        intent = question_intent_score(
            message,
            max_length=self.settings.knowledge_max_message_length,
        )
        if intent < self.settings.question_intent_min:
            return _empty_match(intent, "not_question")

        if not self._entries:
            await self.load_cached()
        async with self._lock:
            entries = list(self._entries)
        if not entries:
            return _empty_match(intent, "no_knowledge")

        normalized_message = normalize_entity_references(
            message,
            getattr(self.settings, "talisson_user_id", OWNER_DISCORD_ID),
        )
        semantic_message = normalized_message.replace("TALISSON_SOUZA", " ")
        requested_link = _requested_link_label(semantic_message)
        if requested_link:
            entries = [
                entry
                for entry in entries
                if entry.source_type == "link"
                and entry.question.casefold().startswith(requested_link.casefold())
            ]
            if not entries:
                return _empty_match(intent, "low_match")
        ranked = sorted(
            (_rank_entry(semantic_message, entry) for entry in entries),
            key=lambda item: (
                item[0],
                item[1],
                item[2],
                item[3].source_type == "faq",
            ),
            reverse=True,
        )
        semantic, relation, answer_quality, entry = ranked[0]
        related_entries = tuple(
            item[3]
            for item in ranked[1:4]
            if item[0] >= max(0.60, semantic - 0.12)
            and item[3].entry_id != entry.entry_id
        )
        if semantic < self.settings.knowledge_match_min:
            return KnowledgeMatch(
                entry=entry,
                question_intent=intent,
                semantic_match=semantic,
                site_relation=relation,
                answer_quality=answer_quality,
                final_confidence=_final_confidence(
                    intent,
                    semantic,
                    relation,
                    answer_quality,
                ),
                should_reply=False,
                reason="low_match",
                related_entries=related_entries,
            )

        final = _final_confidence(intent, semantic, relation, answer_quality)
        should_reply = (
            relation >= self.settings.knowledge_match_min
            and answer_quality >= 0.90
            and final >= self.settings.knowledge_min_confidence
        )
        return KnowledgeMatch(
            entry=entry,
            question_intent=intent,
            semantic_match=semantic,
            site_relation=relation,
            answer_quality=answer_quality,
            final_confidence=final,
            should_reply=should_reply,
            reason="answered" if should_reply else "low_confidence",
            related_entries=related_entries,
        )

    async def _load_faq_entries(
        self,
        session: aiohttp.ClientSession,
    ) -> list[KnowledgeEntry]:
        faq_url = urljoin(self.settings.site_url.rstrip("/") + "/", "faq")
        html = await _fetch_text(session, faq_url)
        parser = _ScriptSourceParser()
        parser.feed(html)
        for source in parser.sources:
            bundle_url = urljoin(faq_url, source)
            bundle = await _fetch_text(session, bundle_url)
            entries = _parse_portuguese_faq(bundle, faq_url)
            if entries:
                return entries
        raise ValueError("As perguntas e respostas não foram encontradas no site.")

    async def _cached_page_entries(self) -> list[KnowledgeEntry]:
        data = await self.state.get("knowledge_entries", [])
        cached: list[KnowledgeEntry] = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or item.get("source_type") == "faq":
                continue
            try:
                cached.append(KnowledgeEntry.from_dict(item))
            except TypeError:
                continue
        return cached

    async def _load_static_page_entries(
        self,
        session: aiohttp.ClientSession,
    ) -> tuple[list[KnowledgeEntry], int]:
        entries: list[KnowledgeEntry] = []
        processed = 0
        seed_urls = list(
            dict.fromkeys([self.settings.site_url, *self.settings.knowledge_pages])
        )
        for raw_url in seed_urls[: self.settings.knowledge_max_pages]:
            url = _normalize_public_url(raw_url)
            try:
                html = await _fetch_text(session, url)
            except Exception:
                continue
            parser = _StaticTextParser()
            parser.feed(html)
            processed += 1
            entries.extend(
                _page_data_to_entries(url, parser.title or url, parser.sections)
            )
        return entries, processed

    async def _load_page_entries(self) -> tuple[list[KnowledgeEntry], int]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright não instalado.") from exc

        entries: list[KnowledgeEntry] = []
        pages_processed = 0
        base = urlparse(self.settings.site_url)
        seed_urls = list(dict.fromkeys([self.settings.site_url, *self.settings.knowledge_pages]))
        queue = [_normalize_public_url(url) for url in seed_urls]
        queued = set(queue)
        visited: set[str] = set()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=self.settings.browser_headless
            )
            context = await browser.new_context(
                user_agent="Discord-Site-Sentinel/2.1",
                locale="pt-BR",
            )
            try:
                while queue and len(visited) < self.settings.knowledge_max_pages:
                    url = queue.pop(0)
                    if url in visited:
                        continue
                    visited.add(url)
                    page = await context.new_page()
                    try:
                        try:
                            response = await page.goto(
                                url,
                                wait_until="domcontentloaded",
                                timeout=min(
                                    12_000,
                                    self.settings.request_timeout_seconds * 1000,
                                ),
                            )
                            if response and response.status >= 400:
                                continue
                            await page.wait_for_timeout(400)
                            page_data = await page.evaluate(
                            """
                            () => {
                              const main =
                                document.querySelector("main") ||
                                document.querySelector('[role="main"]') ||
                                document.querySelector("#root") ||
                                document.body;
                              const title = document.title || location.pathname;
                              const result = [];
                              const links = [];
                              const groups = [];
                              let heading = title;
                              for (const node of main.querySelectorAll(
                                "h1,h2,h3,p,li"
                              )) {
                                const text = (node.innerText || node.textContent || "")
                                  .replace(/\\s+/g, " ")
                                  .trim();
                                if (!text) continue;
                                if (/^H[1-3]$/.test(node.tagName)) {
                                  heading = text;
                                  continue;
                                }
                                if (text.length >= 45 && text.length <= 1600) {
                                  result.push({ heading, text });
                                }
                              }
                              for (const anchor of document.querySelectorAll("a[href]")) {
                                const href = anchor.href || "";
                                const text = (anchor.innerText || anchor.textContent || "")
                                  .replace(/\\s+/g, " ")
                                  .trim();
                                if (href) links.push({ href, text });
                              }
                              for (const headingNode of main.querySelectorAll("h2")) {
                                const items = [];
                                const section = headingNode.closest("section") ||
                                  headingNode.parentElement;
                                for (const child of section.querySelectorAll("h3")) {
                                  const value = (child.innerText || child.textContent || "")
                                    .replace(/\\s+/g, " ")
                                    .trim();
                                  if (value && !items.includes(value)) items.push(value);
                                }
                                if (items.length) {
                                  groups.push({
                                    heading: (headingNode.innerText || "")
                                      .replace(/\\s+/g, " ")
                                      .trim(),
                                    items: items.slice(0, 30)
                                  });
                                }
                              }
                              return {
                                title,
                                sections: result.slice(0, 160),
                                links: links.slice(0, 300),
                                groups: groups.slice(0, 40)
                              };
                            }
                            """
                            )
                        except Exception:
                            continue
                        pages_processed += 1
                        entries.extend(
                            _page_data_to_entries(
                                url,
                                str(page_data.get("title", url)),
                                list(page_data.get("sections", [])),
                            )
                        )
                        links = list(page_data.get("links", []))
                        entries.extend(
                            _link_data_to_entries(
                                url,
                                str(page_data.get("title", url)),
                                links,
                                base.hostname or "",
                            )
                        )
                        entries.extend(
                            _group_data_to_entries(
                                url,
                                str(page_data.get("title", url)),
                                list(page_data.get("groups", [])),
                            )
                        )
                        if self.settings.knowledge_auto_discover:
                            for item in links:
                                if not isinstance(item, dict):
                                    continue
                                candidate = _internal_public_url(
                                    str(item.get("href", "")),
                                    base.hostname or "",
                                )
                                if (
                                    candidate
                                    and candidate not in visited
                                    and candidate not in queued
                                ):
                                    queued.add(candidate)
                                    queue.append(candidate)
                    finally:
                        await page.close()
            finally:
                await context.close()
                await browser.close()
        return entries, pages_processed


class KnowledgeAnalytics:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    async def record(self, match: KnowledgeMatch, *, replied: bool) -> None:
        today = datetime.now(timezone.utc).date().isoformat()

        def mutate(metrics: dict[str, Any]) -> None:
            metrics["evaluated"] = int(metrics.get("evaluated", 0)) + 1
            reason = "answered" if replied else match.reason
            reasons = metrics.setdefault("reasons", {})
            reasons[reason] = int(reasons.get(reason, 0)) + 1
            if replied:
                metrics["answered"] = int(metrics.get("answered", 0)) + 1
                metrics["confidence_total"] = (
                    float(metrics.get("confidence_total", 0.0)) + match.final_confidence
                )
            else:
                metrics["ignored"] = int(metrics.get("ignored", 0)) + 1

            daily = metrics.setdefault("daily", {})
            bucket = daily.setdefault(
                today,
                {
                    "evaluated": 0,
                    "answered": 0,
                    "ignored": 0,
                    "confidence_total": 0.0,
                    "reasons": {},
                },
            )
            bucket["evaluated"] = int(bucket.get("evaluated", 0)) + 1
            if replied:
                bucket["answered"] = int(bucket.get("answered", 0)) + 1
                bucket["confidence_total"] = (
                    float(bucket.get("confidence_total", 0.0)) + match.final_confidence
                )
            else:
                bucket["ignored"] = int(bucket.get("ignored", 0)) + 1
            bucket_reasons = bucket.setdefault("reasons", {})
            bucket_reasons[reason] = int(bucket_reasons.get(reason, 0)) + 1

            cutoff = (
                datetime.now(timezone.utc).date() - timedelta(days=45)
            ).isoformat()
            for day in list(daily):
                if day < cutoff:
                    del daily[day]

        await self.state.mutate_section("knowledge_metrics", mutate)

    async def snapshot(self, days: int = 7) -> KnowledgeAnalyticsSnapshot:
        metrics = await self.state.get_section("knowledge_metrics")
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
        selected: dict[str, dict[str, Any]] = {}
        totals = Counter()
        confidence_total = 0.0
        daily = metrics.get("daily", {})
        if isinstance(daily, dict):
            for day, bucket in daily.items():
                if day < cutoff.isoformat() or not isinstance(bucket, dict):
                    continue
                selected[day] = bucket
                totals["evaluated"] += int(bucket.get("evaluated", 0))
                totals["answered"] += int(bucket.get("answered", 0))
                totals["ignored"] += int(bucket.get("ignored", 0))
                confidence_total += float(bucket.get("confidence_total", 0.0))
                reasons = bucket.get("reasons", {})
                if isinstance(reasons, dict):
                    for reason, count in reasons.items():
                        totals[f"reason:{reason}"] += int(count)
        answered = totals["answered"]
        return KnowledgeAnalyticsSnapshot(
            evaluated=totals["evaluated"],
            answered=answered,
            ignored=totals["ignored"],
            average_answer_confidence=(
                round(confidence_total / answered, 4) if answered else 0.0
            ),
            reasons={
                key.removeprefix("reason:"): count
                for key, count in totals.items()
                if key.startswith("reason:")
            },
            daily=selected,
        )


class _StaticTextParser(HTMLParser):
    """Extrai título e blocos h1-h3/p/li do HTML sem executar JavaScript."""

    _TEXT_TAGS = {"h1", "h2", "h3", "p", "li"}
    _SKIP_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.sections: list[dict[str, str]] = []
        self._heading = ""
        self._current: str | None = None
        self._buffer: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            values = {key.casefold(): value or "" for key, value in attrs}
            if values.get("name", "").casefold() == "description" and values.get("content"):
                self.sections.append(
                    {"heading": self.title or "Descrição", "text": values["content"]}
                )
        elif tag in self._TEXT_TAGS:
            self._current = tag
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == self._current:
            text = " ".join("".join(self._buffer).split())
            if tag in {"h1", "h2", "h3"}:
                self._heading = text or self._heading
            elif 45 <= len(text) <= 1600:
                self.sections.append({"heading": self._heading or self.title, "text": text})
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data.strip()
        elif self._current is not None:
            self._buffer.append(data)


class _ScriptSourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "script":
            return
        values = {key.casefold(): value or "" for key, value in attrs}
        source = values.get("src")
        if source:
            self.sources.append(source)


def question_intent_score(message: str, *, max_length: int = 500) -> float:
    text = " ".join(message.strip().split())
    if not text or len(text) > max_length or len(text) < 4:
        return 0.0
    if CASUAL_ONLY_RE.fullmatch(text):
        return 0.0
    if NON_SITE_GENERIC_RE.search(text):
        return 0.0
    letters = sum(character.isalpha() for character in text)
    if letters < 3:
        return 0.0

    normalized = _basic_normalize_text(text)
    if normalized in CASUAL_NORMALIZED:
        return 0.0
    starts_as_question = bool(QUESTION_START_RE.search(normalized))
    has_question_phrase = bool(QUESTION_PHRASE_RE.search(normalized))
    has_question_mark = "?" in text
    if starts_as_question and has_question_mark:
        return 1.0
    if has_question_phrase and has_question_mark:
        return 0.99
    if starts_as_question:
        return 0.97
    if has_question_phrase:
        return 0.95
    if has_question_mark:
        return 0.91
    return 0.0


def natural_reply(display_name: str, match: KnowledgeMatch) -> str:
    if not match.entry:
        return ""
    if match.entry.source_type == "link":
        label = match.entry.question.split(" de Talisson Souza", 1)[0]
        content = (
            f"Olá, {display_name}!\n\n"
            f"Este é o contato ou perfil oficial de Talisson Souza "
            f"publicado no site:\n\n"
            f"**{label}:**\n{match.entry.answer}"
        )
    else:
        answer = _humanize_answer(match.entry.answer)
        content = f"Olá, {display_name}!\n\n{answer}"
    return content if len(content) <= 1900 else content[:1897] + "..."


def _semantic_score(message: str, entry: KnowledgeEntry) -> float:
    query_normalized = _normalize_text(message)
    candidate_normalized = _normalize_text(entry.question)
    query_tokens = _semantic_tokens(query_normalized)
    candidate_tokens = _semantic_tokens(f"{candidate_normalized} {entry.source_title}")
    if not query_tokens or not candidate_tokens:
        return 0.0
    if query_normalized == candidate_normalized:
        return 1.0

    query_set = set(query_tokens)
    candidate_set = set(candidate_tokens)
    intersection = len(query_set & candidate_set)
    union = len(query_set | candidate_set)
    jaccard = intersection / union if union else 0.0
    containment = intersection / min(len(query_set), len(candidate_set))
    token_set = (
        fuzz.token_set_ratio(
            " ".join(query_tokens),
            " ".join(candidate_tokens),
        )
        / 100
    )
    weighted = fuzz.WRatio(query_normalized, candidate_normalized) / 100
    concept_overlap = _concept_overlap(query_set, candidate_set)
    score = (
        0.15 * jaccard
        + 0.35 * containment
        + 0.25 * token_set
        + 0.10 * weighted
        + 0.15 * concept_overlap
    )
    if intersection >= 2 or (concept_overlap == 1.0 and len(query_set) <= 2):
        score += 0.08
    if entry.source_type == "faq":
        score += 0.035
    return round(min(1.0, score), 4)


def _rank_entry(
    message: str,
    entry: KnowledgeEntry,
) -> tuple[float, float, float, KnowledgeEntry]:
    semantic = _semantic_score(message, entry)
    relation = _site_relation_score(semantic, entry)
    return semantic, relation, _answer_quality_score(entry), entry


def _site_relation_score(
    semantic: float,
    entry: KnowledgeEntry,
) -> float:
    source_bonus = (
        0.08
        if entry.source_type == "faq"
        else (0.06 if entry.source_type == "link" else 0.03)
    )
    return round(min(1.0, semantic + source_bonus), 4)


def _answer_quality_score(entry: KnowledgeEntry) -> float:
    if entry.source_type == "link" and entry.answer.startswith(
        ("http://", "https://", "mailto:", "tel:")
    ):
        return 1.0
    length = len(entry.answer.strip())
    if 45 <= length <= 1400:
        score = 1.0
    elif 20 <= length <= 1800:
        score = 0.94
    else:
        score = 0.75
    if entry.source_type == "faq":
        score = min(1.0, score + 0.02)
    return score


def _final_confidence(
    intent: float,
    semantic: float,
    relation: float,
    answer_quality: float,
) -> float:
    return round(
        0.18 * intent + 0.62 * semantic + 0.12 * relation + 0.08 * answer_quality,
        4,
    )


def _empty_match(intent: float, reason: str) -> KnowledgeMatch:
    return KnowledgeMatch(
        entry=None,
        question_intent=intent,
        semantic_match=0.0,
        site_relation=0.0,
        answer_quality=0.0,
        final_confidence=0.0,
        should_reply=False,
        reason=reason,
        related_entries=(),
    )


def _parse_portuguese_faq(
    bundle: str,
    source_url: str,
) -> list[KnowledgeEntry]:
    start = bundle.find('e==="pt"?')
    if start < 0:
        start = bundle.find("questions:[")
    end = bundle.find(']:[{title:"About', start)
    section = bundle[start : end if end > start else len(bundle)]
    entries = []
    for match in FAQ_PAIR_RE.finditer(section):
        question = _decode_js_string(match.group("question")).strip()
        answer = _decode_js_string(match.group("answer")).strip()
        if not question or not answer:
            continue
        entries.append(
            _make_entry(
                question=question,
                answer=answer,
                source_url=source_url,
                source_title="Perguntas frequentes",
                source_type="faq",
            )
        )
    return entries


def _page_data_to_entries(
    url: str,
    title: str,
    sections: list[Any],
) -> list[KnowledgeEntry]:
    entries: list[KnowledgeEntry] = []
    for item in sections:
        if not isinstance(item, dict):
            continue
        heading = " ".join(str(item.get("heading", "")).split())
        text = " ".join(str(item.get("text", "")).split())
        if len(text) < 45:
            continue
        question = heading or title
        entries.append(
            _make_entry(
                question=question,
                answer=text,
                source_url=url,
                source_title=title,
                source_type="page",
            )
        )
    return entries


def _link_data_to_entries(
    source_url: str,
    title: str,
    links: list[Any],
    site_host: str,
) -> list[KnowledgeEntry]:
    entries: list[KnowledgeEntry] = []
    seen: set[str] = set()
    for item in links:
        if not isinstance(item, dict):
            continue
        href = str(item.get("href", "")).strip()
        text = " ".join(str(item.get("text", "")).split())
        if not href or href in seen:
            continue
        seen.add(href)
        parsed = urlparse(href)
        is_contact = parsed.scheme in {"mailto", "tel"}
        is_external = parsed.scheme in {"http", "https"} and (
            parsed.hostname or ""
        ).removeprefix("www.") != site_host.removeprefix("www.")
        if not is_contact and not is_external:
            continue
        if parsed.scheme == "mailto" and parsed.query:
            href = f"mailto:{parsed.path}"
        label = _public_link_label(text, href)
        if not label:
            continue
        entries.append(
            _make_entry(
                question=f"{label} de Talisson Souza",
                answer=href,
                source_url=source_url,
                source_title=title,
                source_type="link",
            )
        )
    return entries


def _group_data_to_entries(
    source_url: str,
    title: str,
    groups: list[Any],
) -> list[KnowledgeEntry]:
    entries: list[KnowledgeEntry] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        heading = " ".join(str(group.get("heading", "")).split())
        items = [
            " ".join(str(item).split())
            for item in group.get("items", [])
            if str(item).strip()
        ]
        items = list(dict.fromkeys(items))
        if not heading or not items:
            continue
        answer = f"Itens publicados em {heading}: " + "; ".join(items) + "."
        entries.append(
            _make_entry(
                question=heading,
                answer=answer,
                source_url=source_url,
                source_title=title,
                source_type="section",
            )
        )
        if "empresa" in _basic_normalize_text(heading):
            count = len(items)
            names = (
                items[0]
                if count == 1
                else ", ".join(items[:-1]) + f" e {items[-1]}"
            )
            entries.append(
                _make_entry(
                    question="Quantas empresas Talisson Souza possui?",
                    answer=(
                        f"A seção pública {heading} apresenta {count} "
                        f"empresa{'s' if count != 1 else ''}: {names}."
                    ),
                    source_url=source_url,
                    source_title=title,
                    source_type="derived",
                )
            )
    return entries


def _make_entry(
    *,
    question: str,
    answer: str,
    source_url: str,
    source_title: str,
    source_type: str,
) -> KnowledgeEntry:
    identity = hashlib.sha256(
        f"{source_type}\x1f{question}\x1f{answer}".encode("utf-8")
    ).hexdigest()[:20]
    return KnowledgeEntry(
        entry_id=identity,
        question=question,
        answer=answer,
        source_url=source_url,
        source_title=source_title,
        source_type=source_type,
    )


def _deduplicate_entries(entries: list[KnowledgeEntry]) -> list[KnowledgeEntry]:
    result: dict[str, KnowledgeEntry] = {}
    answer_fingerprints: set[str] = set()
    for entry in entries:
        fingerprint = _normalize_text(entry.answer)
        if fingerprint in answer_fingerprints:
            continue
        answer_fingerprints.add(fingerprint)
        result[entry.entry_id] = entry
    return list(result.values())


async def _fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    separator = "&" if "?" in url else "?"
    request_url = f"{url}{separator}_sentinel_knowledge={time.time_ns()}"
    async with session.get(
        request_url,
        allow_redirects=True,
        headers={
            "User-Agent": "Discord-Site-Sentinel/2.1",
            "Accept": "text/html,application/javascript,*/*",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    ) as response:
        body = await read_limited(response.content, MAX_KNOWLEDGE_BYTES)
        if response.status >= 400:
            raise ValueError(f"HTTP {response.status} em {url}")
        try:
            return body.decode(response.charset or "utf-8")
        except (LookupError, UnicodeDecodeError):
            return body.decode("utf-8", errors="replace")


def _decode_js_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return (
            value.replace(r"\"", '"')
            .replace(r"\\", "\\")
            .replace(r"\/", "/")
            .replace(r"\n", "\n")
        )


def _normalize_text(value: str) -> str:
    cleaned = _basic_normalize_text(value)
    cleaned = re.sub(
        r"\bcomo (?:eu )?posso (?:falar|conversar|chamar|contactar)\b",
        "forma contato",
        cleaned,
    )
    cleaned = re.sub(
        r"\b(?:entrar em contato|falar com|conversar com|chamar no whatsapp)\b",
        "contato",
        cleaned,
    )
    cleaned = re.sub(
        r"\b(?:trabalha|atua|faz) com (?:o que|que|qual coisa)\b",
        "experiencia tecnologia",
        cleaned,
    )
    return " ".join(cleaned.split())


def normalize_entity_references(value: str, talisson_user_id: int) -> str:
    result = value
    result = re.sub(
        rf"<@!?{talisson_user_id}>",
        " TALISSON_SOUZA ",
        result,
        flags=re.IGNORECASE,
    )
    patterns = (
        r"@?(?:talisson\s+souza|talinhodev|talinho|talisson|tali)\b",
        r"\b(?:fundador|ceo)(?:\s+da)?\s+cheetah(?:\s+solutions\s+network)?\b",
        r"\bts\b",
    )
    for pattern in patterns:
        result = re.sub(
            pattern,
            " TALISSON_SOUZA ",
            result,
            flags=re.IGNORECASE,
        )
    return " ".join(result.split())


def _basic_normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    cleaned = re.sub(r"https?://\S+|<@!?\d+>", " ", without_accents)
    cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
    return " ".join(cleaned.split())


def _humanize_answer(value: str) -> str:
    answer = " ".join(value.split()).strip()
    if answer.startswith("A seção pública "):
        return answer if answer.endswith((".", "!", "?")) else answer + "."
    replacements = (
        (r"^Eu sou\s+", "Talisson Souza é "),
        (r"^Sou\s+", "Talisson Souza é "),
        (r"^Minha formação é\s+", "A formação de Talisson Souza é "),
        (r"^Desde 2016\.\s*Comecei\s+", "Talisson atua com tecnologia desde 2016. Começou "),
        (r"\bTambém sou\s+", "Também é "),
        (r"\bTrabalho com\s+", "Atua com "),
        (r"\bTrabalho há\s+", "Atua há "),
        (r"\bTrabalho\s+", "Ele trabalha "),
        (r"\bAtuo\s+", "Ele atua "),
        (r"\bTenho\s+", "Ele possui "),
        (r"\bMeu foco é\s+", "O foco dele é "),
        (r"\bMeus projetos\b", "Os projetos dele"),
        (r"\bMinha experiência\b", "A experiência dele"),
        (r"\bComecei\b", "Ele começou"),
        (r"\bEvoluí\b", "evoluiu"),
        (r"\bCriei\b", "Ele criou"),
        (r"\bFundei\b", "Ele fundou"),
        (r"\bParticipei\b", "Ele participou"),
        (r"\bAcredito\b", "Ele acredita"),
        (r"\bMantenho\b", "Ele mantém"),
        (r"\bCompartilho\b", "Ele compartilha"),
        (r"\bAnaliso\b", "Ele analisa"),
        (r"\bAplico\b", "Ele aplica"),
        (r"\bRespondo\b", "Ele responde"),
        (r"\bOfereço\b", "Ele oferece"),
        (r"\bFaço\b", "Ele realiza"),
        (r"\bDesenvolvo\b", "Ele desenvolve"),
        (r"\bAceito\b", "Ele aceita"),
        (r"\bNão trabalho\b", "Ele não trabalha"),
        (r"\bNão ofereço\b", "Ele não oferece"),
        (r"\b(?:o )?meu ([a-zà-ÿ]+)", r"o \1 dele"),
        (r"\b(?:a )?minha ([a-zà-ÿ]+)", r"a \1 dele"),
        (r"\bmeus ([a-zà-ÿ]+)", r"os \1 dele"),
        (r"\bminhas ([a-zà-ÿ]+)", r"as \1 dele"),
        (r"\bPrefiro\b", "Ele prefere"),
        (r"\bUso\b", "Ele usa"),
        (r"\bPosso\b", "Ele pode"),
    )
    for pattern, replacement in replacements:
        answer = re.sub(pattern, replacement, answer, flags=re.IGNORECASE)
    answer = re.sub(r"\bEu\b", "Ele", answer)
    if answer and answer[-1] not in ".!?":
        answer += "."
    return answer


def _public_link_label(text: str, href: str) -> str:
    combined = f"{text} {href}".casefold()
    labels = (
        ("Instagram", "instagram"),
        ("LinkedIn", "linkedin"),
        ("GitHub", "github"),
        ("Discord", "discord"),
        ("WhatsApp", "whatsapp"),
        ("E-mail", "mailto:"),
        ("Telefone", "tel:"),
        ("Portfólio", "portfolio"),
    )
    for label, marker in labels:
        if marker in combined:
            return label
    if text and len(text) <= 60:
        return text
    return ""


def _requested_link_label(value: str) -> str:
    normalized = _basic_normalize_text(value)
    for label in (
        "Instagram",
        "LinkedIn",
        "GitHub",
        "Discord",
        "WhatsApp",
        "E-mail",
        "Telefone",
        "Portfólio",
    ):
        variants = {
            _basic_normalize_text(label),
            "email" if label == "E-mail" else "",
            "portfolio" if label == "Portfólio" else "",
        }
        if any(
            variant and re.search(rf"\b{re.escape(variant)}\b", normalized)
            for variant in variants
        ):
            return label
    return ""


def _normalize_public_url(value: str) -> str:
    clean, _ = urldefrag(value)
    parsed = urlparse(clean)
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return parsed._replace(path=path, query="", fragment="").geturl()


def _internal_public_url(value: str, site_host: str) -> str | None:
    if not value.startswith(("http://", "https://")):
        return None
    parsed = urlparse(value)
    if (parsed.hostname or "").removeprefix("www.") != site_host.removeprefix("www."):
        return None
    if re.search(
        r"\.(?:avif|css|gif|ico|jpe?g|js|json|map|mp3|mp4|pdf|png|svg|webp|woff2?)$",
        parsed.path,
        re.IGNORECASE,
    ):
        return None
    if parsed.path.startswith(
        (
            "/api/",
            "/cdn-cgi/",
            "/_next/",
            "/assets/",
            "/admin",
            "/login",
            "/logout",
        )
    ):
        return None
    if parsed.query:
        return None
    return _normalize_public_url(value)


def _semantic_tokens(value: str) -> list[str]:
    tokens = []
    for raw_token in value.split():
        if raw_token in STOPWORDS or len(raw_token) < 2:
            continue
        token = CONCEPT_LOOKUP.get(raw_token, _light_stem(raw_token))
        if token not in STOPWORDS:
            tokens.append(token)
    return tokens


def _light_stem(token: str) -> str:
    for suffix in (
        "amentos",
        "imento",
        "imentos",
        "acoes",
        "acao",
        "mente",
        "idades",
        "idade",
        "ando",
        "endo",
        "indo",
        "ados",
        "adas",
        "idos",
        "idas",
        "es",
        "s",
    ):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _concept_overlap(query: set[str], candidate: set[str]) -> float:
    query_concepts = {
        CONCEPT_LOOKUP.get(token, token)
        for token in query
        if token in CONCEPT_LOOKUP or token in CONCEPT_GROUPS
    }
    candidate_concepts = {
        CONCEPT_LOOKUP.get(token, token)
        for token in candidate
        if token in CONCEPT_LOOKUP or token in CONCEPT_GROUPS
    }
    if not query_concepts:
        return 0.0
    return len(query_concepts & candidate_concepts) / len(query_concepts)
