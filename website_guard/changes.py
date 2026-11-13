from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from .config import Settings
from .http_utils import read_limited
from .metrics import RequestObservation
from .state import StateStore

MAX_PAGE_BYTES = 3 * 1024 * 1024


@dataclass(frozen=True)
class ContentChange:
    change_id: str
    url: str
    title: str
    summary: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    change_types: list[str] = field(default_factory=list)
    structural_changes: list[str] = field(default_factory=list)
    seo_changes: list[str] = field(default_factory=list)
    technology_changes: list[str] = field(default_factory=list)
    form_findings: list[str] = field(default_factory=list)
    link_findings: list[str] = field(default_factory=list)
    before_excerpt: str = ""
    after_excerpt: str = ""
    page_metrics: dict[str, Any] = field(default_factory=dict)
    detected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChangeScanReport:
    checked_at: str
    pages_checked: int = 0
    changes: list[ContentChange] = field(default_factory=list)
    requests: list[RequestObservation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    renderer: str = "html"
    seo_findings: list[str] = field(default_factory=list)
    form_findings: list[str] = field(default_factory=list)
    link_findings: list[str] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)
    pages_added: list[str] = field(default_factory=list)
    pages_removed: list[str] = field(default_factory=list)
    page_metrics: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "pages_checked": self.pages_checked,
            "changes": [change.to_dict() for change in self.changes],
            "requests": [item.to_dict() for item in self.requests],
            "errors": self.errors,
            "renderer": self.renderer,
            "seo_findings": self.seo_findings,
            "form_findings": self.form_findings,
            "link_findings": self.link_findings,
            "technologies": self.technologies,
            "pages_added": self.pages_added,
            "pages_removed": self.pages_removed,
            "page_metrics": self.page_metrics,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChangeScanReport":
        return cls(
            checked_at=str(data.get("checked_at", "")),
            pages_checked=int(data.get("pages_checked", 0)),
            changes=[
                ContentChange(**item)
                for item in data.get("changes", [])
                if isinstance(item, dict)
            ],
            requests=[
                RequestObservation.from_dict(item)
                for item in data.get("requests", [])
                if isinstance(item, dict)
            ],
            errors=[str(item) for item in data.get("errors", [])],
            renderer=str(data.get("renderer", "html")),
            seo_findings=[str(item) for item in data.get("seo_findings", [])],
            form_findings=[str(item) for item in data.get("form_findings", [])],
            link_findings=[str(item) for item in data.get("link_findings", [])],
            technologies=[str(item) for item in data.get("technologies", [])],
            pages_added=[str(item) for item in data.get("pages_added", [])],
            pages_removed=[str(item) for item in data.get("pages_removed", [])],
            page_metrics={
                str(key): dict(value)
                for key, value in dict(data.get("page_metrics", {})).items()
                if isinstance(value, dict)
            },
        )


@dataclass(frozen=True)
class _PageContent:
    url: str
    title: str
    text: str
    status_code: int | None
    response_ms: int
    structure: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class WebsiteChangeMonitor:
    def __init__(self, settings: Settings, state: StateStore) -> None:
        self.settings = settings
        self.state = state

    async def scan(self, session: aiohttp.ClientSession) -> ChangeScanReport:
        report = ChangeScanReport(
            checked_at=datetime.now(timezone.utc).isoformat(),
        )
        pages: list[_PageContent]
        if self.settings.render_dynamic_pages:
            try:
                pages = await self._render_pages()
                report.renderer = "navegador"
            except Exception as exc:
                report.errors.append(
                    "Renderização dinâmica indisponível; a leitura HTML de contingência foi usada. "
                    f"Detalhe: {type(exc).__name__}: {exc}"
                )
                pages = await self._fetch_static_pages(session)
                report.renderer = "html"
        else:
            pages = await self._fetch_static_pages(session)

        snapshots = await self.state.get_section("page_snapshots")
        updated_snapshots = dict(snapshots)
        discovered_links: set[str] = set()
        technologies: set[str] = set()
        for page in pages:
            report.pages_checked += 1
            report.requests.append(
                RequestObservation(
                    category="content_page",
                    url=page.url,
                    status_code=page.status_code,
                    response_ms=page.response_ms,
                    error=page.error,
                )
            )
            if page.error:
                report.errors.append(f"{page.url}: {page.error}")
                continue
            page_findings = _audit_page_structure(page)
            report.seo_findings.extend(page_findings["seo"])
            report.form_findings.extend(page_findings["forms"])
            technologies.update(page_findings["technologies"])
            report.page_metrics[page.url] = dict(
                page.structure.get("performance", {})
            )
            discovered_links.update(_internal_links(page.url, page.structure))
            normalized = _normalize_page_text(page.text)
            if len(normalized) < 40:
                report.errors.append(
                    f"{page.url}: conteúdo útil insuficiente para comparar com segurança."
                )
                continue

            fingerprint = json.dumps(
                {
                    "text": normalized,
                    "structure": page.structure,
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            content_hash = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
            previous = snapshots.get(page.url)
            updated_snapshots[page.url] = {
                "title": page.title,
                "text": normalized,
                "structure": page.structure,
                "content_hash": content_hash,
                "checked_at": report.checked_at,
            }
            if not isinstance(previous, dict):
                continue
            if previous.get("content_hash") == content_hash:
                continue

            previous_text = str(previous.get("text", ""))
            report.changes.append(
                _summarize_change(
                    page.url,
                    page.title,
                    previous_text,
                    normalized,
                    previous.get("structure", {}),
                    page.structure,
                    page_metrics={
                        "response_ms": page.response_ms,
                        **dict(page.structure.get("performance", {})),
                    },
                )
            )

        previous_pages = {
            str(item)
            for item in await self.state.get("discovered_public_pages", [])
        }
        if previous_pages:
            report.pages_added = sorted(discovered_links - previous_pages)[:50]
            report.pages_removed = sorted(previous_pages - discovered_links)[:50]
        report.technologies = sorted(technologies)
        report.link_findings.extend(
            await self._audit_links(session, discovered_links, report)
        )
        report.link_findings.extend(
            await self._audit_site_files(session, report)
        )

        await self.state.set_many(
            {
                "page_snapshots": updated_snapshots,
                "discovered_public_pages": sorted(discovered_links),
                "latest_change_report": report.to_dict(),
            }
        )
        return report

    async def _audit_links(
        self,
        session: aiohttp.ClientSession,
        links: set[str],
        report: ChangeScanReport,
    ) -> list[str]:
        findings: list[str] = []
        semaphore = asyncio.Semaphore(8)

        async def check(url: str) -> None:
            start = time.perf_counter()
            status: int | None = None
            error: str | None = None
            try:
                async with semaphore:
                    async with session.get(
                        url,
                        allow_redirects=True,
                        headers={
                            "User-Agent": "Discord-Site-Sentinel/3.0",
                            "Accept": "text/html,*/*;q=0.8",
                        },
                    ) as response:
                        status = response.status
                        await read_limited(response.content, 64 * 1024)
                        if response.status >= 400:
                            findings.append(
                                f"Link indisponível: {url} (HTTP {response.status})"
                            )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                findings.append(f"Link não pôde ser validado: {url}")
            report.requests.append(
                RequestObservation(
                    category="link",
                    url=url,
                    status_code=status,
                    response_ms=int((time.perf_counter() - start) * 1000),
                    error=error,
                )
            )

        await asyncio.gather(
            *(check(url) for url in sorted(links)[:40]),
            return_exceptions=True,
        )
        return findings[:40]

    async def _audit_site_files(
        self,
        session: aiohttp.ClientSession,
        report: ChangeScanReport,
    ) -> list[str]:
        parsed = urlparse(self.settings.site_url)
        base = f"{parsed.scheme}://{parsed.netloc}/"
        findings = []
        for name in ("robots.txt", "sitemap.xml"):
            url = urljoin(base, name)
            start = time.perf_counter()
            status: int | None = None
            error: str | None = None
            try:
                async with session.get(
                    url,
                    allow_redirects=True,
                    headers={"User-Agent": "Discord-Site-Sentinel/3.0"},
                ) as response:
                    status = response.status
                    await read_limited(response.content, 512 * 1024)
                    if response.status >= 400:
                        findings.append(
                            f"{name} indisponível (HTTP {response.status})"
                        )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                findings.append(f"{name} não pôde ser validado")
            report.requests.append(
                RequestObservation(
                    category="seo_file",
                    url=url,
                    status_code=status,
                    response_ms=int((time.perf_counter() - start) * 1000),
                    error=error,
                )
            )
        return findings

    async def _render_pages(self) -> list[_PageContent]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright não instalado. Execute o script setup.ps1."
            ) from exc

        results: list[_PageContent] = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=self.settings.browser_headless,
            )
            context = await browser.new_context(
                user_agent="Discord-Site-Sentinel/2.0",
                locale="pt-BR",
            )
            try:
                for url in self.settings.pages_to_monitor:
                    page = await context.new_page()
                    start = time.perf_counter()
                    status_code: int | None = None
                    error: str | None = None
                    title = url
                    text = ""
                    try:
                        response = await page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=self.settings.request_timeout_seconds * 1000,
                        )
                        status_code = response.status if response else None
                        await page.wait_for_timeout(800)
                        title = await page.title()
                        page_data = await page.evaluate(
                            """
                            () => {
                              const source =
                                document.querySelector("main") ||
                                document.querySelector('[role="main"]') ||
                                document.querySelector("#root") ||
                                document.body;
                              const clone = source.cloneNode(true);
                              clone.querySelectorAll(
                                "script,style,noscript,svg,nav,footer,[aria-hidden='true']"
                              ).forEach((node) => node.remove());
                              const meta = (name, property = false) => {
                                const selector = property
                                  ? `meta[property="${name}"]`
                                  : `meta[name="${name}"]`;
                                return document.querySelector(selector)?.content || "";
                              };
                              const links = [...document.querySelectorAll("a[href]")]
                                .map((node) => ({
                                  text: (node.innerText || node.getAttribute("aria-label") || "").trim(),
                                  href: node.href,
                                }))
                                .filter((item) => item.href)
                                .slice(0, 250);
                              const forms = [...document.querySelectorAll("form")]
                                .map((form) => ({
                                  action: form.action || location.href,
                                  method: (form.method || "get").toUpperCase(),
                                  fields: [...form.querySelectorAll("input,textarea,select")]
                                    .map((field) => ({
                                      name: field.name || field.id || field.type || "campo",
                                      type: field.type || field.tagName.toLowerCase(),
                                      required: Boolean(field.required),
                                      disabled: Boolean(field.disabled),
                                    }))
                                    .slice(0, 50),
                                }))
                                .slice(0, 30);
                              const structuredData = [...document.querySelectorAll('script[type="application/ld+json"]')]
                                .map((node) => {
                                  try {
                                    const value = JSON.parse(node.textContent || "{}");
                                    return value["@type"] || "Schema.org";
                                  } catch {
                                    return "JSON-LD inválido";
                                  }
                                });
                              const scripts = [...document.scripts].map((item) => item.src || item.textContent || "");
                              const html = document.documentElement.innerHTML;
                              const technologySignals = [
                                ["React", Boolean(document.querySelector("#root")) || scripts.some((item) => /react/i.test(item))],
                                ["Next.js", Boolean(document.querySelector("#__next")) || /_next\\//i.test(html)],
                                ["Tailwind CSS", /class="[^"]*(?:sm:|md:|lg:|flex|grid|text-|bg-)/i.test(html)],
                                ["Vercel", /vercel/i.test(html)],
                                ["Supabase", /supabase/i.test(html)],
                                ["Cloudflare", /cloudflare|cdn-cgi/i.test(html)],
                                ["Google Analytics", /googletagmanager|gtag\\(/i.test(html)],
                              ].filter((item) => item[1]).map((item) => item[0]);
                              const navigation = performance.getEntriesByType("navigation")[0];
                              const resources = performance.getEntriesByType("resource");
                              return {
                                text: clone.innerText || clone.textContent || "",
                                structure: {
                                  buttons: [...document.querySelectorAll("button,[role='button']")]
                                    .map((node) => (node.innerText || node.getAttribute("aria-label") || "").trim())
                                    .filter(Boolean)
                                    .slice(0, 100),
                                  forms,
                                  links,
                                  images: [...document.images]
                                    .map((image) => ({ src: image.currentSrc || image.src, alt: image.alt || "" }))
                                    .filter((item) => item.src)
                                    .slice(0, 150),
                                  sections: [...document.querySelectorAll("h1,h2,h3")]
                                    .map((node) => (node.innerText || "").trim())
                                    .filter(Boolean)
                                    .slice(0, 100),
                                  seo: {
                                    title: document.title || "",
                                    description: meta("description"),
                                    canonical: document.querySelector('link[rel="canonical"]')?.href || "",
                                    robots: meta("robots"),
                                    open_graph: {
                                      title: meta("og:title", true),
                                      description: meta("og:description", true),
                                      image: meta("og:image", true),
                                      url: meta("og:url", true),
                                    },
                                    twitter_cards: {
                                      card: meta("twitter:card"),
                                      title: meta("twitter:title"),
                                      description: meta("twitter:description"),
                                      image: meta("twitter:image"),
                                    },
                                    structured_data: structuredData,
                                  },
                                  technologies: technologySignals,
                                  performance: {
                                    dom_ms: navigation ? Math.round(navigation.domContentLoadedEventEnd) : null,
                                    load_ms: navigation ? Math.round(navigation.loadEventEnd) : null,
                                    resources: resources.length,
                                    transfer_bytes: Math.round(resources.reduce((sum, item) => sum + (item.transferSize || 0), 0)),
                                  },
                                },
                              };
                            }
                            """
                        )
                        if isinstance(page_data, dict):
                            text = str(page_data.get("text", ""))
                            structure = page_data.get("structure", {})
                        else:
                            text = str(page_data or "")
                            structure = {}
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                        structure = {}
                    finally:
                        response_ms = int((time.perf_counter() - start) * 1000)
                        await page.close()
                    results.append(
                        _PageContent(
                            url=url,
                            title=title,
                            text=text,
                            status_code=status_code,
                            response_ms=response_ms,
                            structure=(
                                structure if isinstance(structure, dict) else {}
                            ),
                            error=error,
                        )
                    )
            finally:
                await context.close()
                await browser.close()
        return results

    async def _fetch_static_pages(
        self,
        session: aiohttp.ClientSession,
    ) -> list[_PageContent]:
        results: list[_PageContent] = []
        for url in self.settings.pages_to_monitor:
            start = time.perf_counter()
            status_code: int | None = None
            error: str | None = None
            title = url
            text = ""
            structure: dict[str, Any] = {}
            try:
                separator = "&" if "?" in url else "?"
                request_url = f"{url}{separator}_sentinel_probe={time.time_ns()}"
                async with session.get(
                    request_url,
                    allow_redirects=True,
                    headers={
                        "User-Agent": "Discord-Site-Sentinel/2.0",
                        "Accept": "text/html",
                        "Cache-Control": "no-cache",
                    },
                ) as response:
                    status_code = response.status
                    body = await read_limited(response.content, MAX_PAGE_BYTES)
                    if response.status >= 400:
                        raise ValueError(f"HTTP {response.status}")
                    html = body.decode(response.charset or "utf-8", errors="replace")
                    parser = _VisibleTextParser()
                    parser.feed(html)
                    title = parser.title or url
                    text = "\n".join(parser.parts)
                    structure = parser.structure(str(response.url))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            results.append(
                _PageContent(
                    url=url,
                    title=title,
                    text=text,
                    status_code=status_code,
                    response_ms=int((time.perf_counter() - start) * 1000),
                    structure=structure,
                    error=error,
                )
            )
        return results


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._ignored_depth = 0
        self._in_title = False
        self._current_button = False
        self._current_heading = False
        self._current_anchor: dict[str, str] | None = None
        self.buttons: list[str] = []
        self.links: list[dict[str, str]] = []
        self.images: list[dict[str, str]] = []
        self.forms: list[dict[str, Any]] = []
        self.sections: list[str] = []
        self.meta: dict[str, str] = {}
        self.canonical = ""
        self.structured_data_count = 0
        self.script_sources: list[str] = []
        self.class_values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        values = {name.casefold(): value or "" for name, value in attrs}
        if values.get("class"):
            self.class_values.append(values["class"])
        if lowered in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
            if lowered == "script":
                if values.get("src"):
                    self.script_sources.append(values["src"])
                if values.get("type") == "application/ld+json":
                    self.structured_data_count += 1
        elif lowered == "title":
            self._in_title = True
        elif lowered == "meta":
            name = values.get("name") or values.get("property")
            if name:
                self.meta[name.casefold()] = values.get("content", "")
        elif lowered == "link" and "canonical" in values.get("rel", "").casefold():
            self.canonical = values.get("href", "")
        elif lowered == "a" and values.get("href"):
            self._current_anchor = {"href": values["href"], "text": ""}
        elif lowered in {"button"} or values.get("role") == "button":
            self._current_button = True
        elif lowered in {"h1", "h2", "h3"}:
            self._current_heading = True
        elif lowered == "img" and values.get("src"):
            self.images.append(
                {"src": values["src"], "alt": values.get("alt", "")}
            )
        elif lowered == "form":
            self.forms.append(
                {
                    "action": values.get("action", ""),
                    "method": values.get("method", "get").upper(),
                    "fields": [],
                }
            )
        elif lowered in {"input", "textarea", "select"} and self.forms:
            self.forms[-1]["fields"].append(
                {
                    "name": values.get("name")
                    or values.get("id")
                    or values.get("type")
                    or lowered,
                    "type": values.get("type", lowered),
                    "required": "required" in values,
                    "disabled": "disabled" in values,
                }
            )

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif lowered == "title":
            self._in_title = False
        elif lowered == "a" and self._current_anchor is not None:
            self.links.append(self._current_anchor)
            self._current_anchor = None
        elif lowered == "button":
            self._current_button = False
        elif lowered in {"h1", "h2", "h3"}:
            self._current_heading = False

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if not value or self._ignored_depth:
            return
        if self._in_title:
            self.title = value
        else:
            self.parts.append(value)
        if self._current_anchor is not None:
            self._current_anchor["text"] = " ".join(
                (self._current_anchor["text"], value)
            ).strip()
        if self._current_button:
            self.buttons.append(value)
        if self._current_heading:
            self.sections.append(value)

    def structure(self, base_url: str) -> dict[str, Any]:
        source = "\n".join(
            [*self.script_sources, *self.class_values, "\n".join(self.parts)]
        )
        technologies = []
        for name, pattern in (
            ("React", r"\breact\b|id=[\"']root"),
            ("Next.js", r"_next/|__next"),
            ("Tailwind CSS", r"\b(?:sm:|md:|lg:|flex|grid|text-|bg-)"),
            ("Vercel", r"\bvercel\b"),
            ("Supabase", r"\bsupabase\b"),
            ("Cloudflare", r"cloudflare|cdn-cgi"),
            ("Google Analytics", r"googletagmanager|gtag"),
        ):
            if re.search(pattern, source, flags=re.IGNORECASE):
                technologies.append(name)
        return {
            "buttons": self.buttons[:100],
            "forms": self.forms[:30],
            "links": self.links[:250],
            "images": self.images[:150],
            "sections": self.sections[:100],
            "seo": {
                "title": self.title,
                "description": self.meta.get("description", ""),
                "canonical": self.canonical,
                "robots": self.meta.get("robots", ""),
                "open_graph": {
                    key.removeprefix("og:"): value
                    for key, value in self.meta.items()
                    if key.startswith("og:")
                },
                "twitter_cards": {
                    key.removeprefix("twitter:"): value
                    for key, value in self.meta.items()
                    if key.startswith("twitter:")
                },
                "structured_data": (
                    ["Schema.org"] * self.structured_data_count
                ),
            },
            "technologies": technologies,
            "performance": {},
            "base_url": base_url,
        }


def _normalize_page_text(value: str) -> str:
    value = re.sub(
        r"(?i)(verifica(?:ç|c)ão de segurança)\s*"
        r"\d+\s*(?:\+|-|x|×|\*|/)\s*\d+\s*=\s*\?",
        r"\1 [desafio dinâmico]",
        value,
    )
    lines = []
    for raw_line in value.replace("\r", "\n").split("\n"):
        line = " ".join(raw_line.split())
        if line and line not in lines[-1:]:
            lines.append(line)
    return "\n".join(lines).strip()


def _summarize_change(
    url: str,
    title: str,
    previous_text: str,
    current_text: str,
    previous_structure: dict[str, Any] | None = None,
    current_structure: dict[str, Any] | None = None,
    *,
    page_metrics: dict[str, Any] | None = None,
) -> ContentChange:
    previous = _comparison_units(previous_text)
    current = _comparison_units(current_text)
    matcher = difflib.SequenceMatcher(a=previous, b=current, autojunk=False)
    added: list[str] = []
    removed: list[str] = []
    modified: list[str] = []
    structure = _compare_structure(
        previous_structure or {},
        current_structure or {},
    )

    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        old_values = previous[old_start:old_end]
        new_values = current[new_start:new_end]
        if tag == "insert":
            added.extend(new_values)
        elif tag == "delete":
            removed.extend(old_values)
        elif tag == "replace":
            pairs = min(len(old_values), len(new_values))
            for index in range(pairs):
                modified.append(
                    f'Antes: "{_clip(old_values[index], 180)}" | '
                    f'Agora: "{_clip(new_values[index], 180)}"'
                )
            removed.extend(old_values[pairs:])
            added.extend(new_values[pairs:])

    summary_parts = [f'A página "{title or url}" foi atualizada.']
    if added:
        summary_parts.append(f"{len(added)} trecho(s) foram adicionados.")
    if removed:
        summary_parts.append(f"{len(removed)} trecho(s) foram removidos.")
    if modified:
        summary_parts.append(f"{len(modified)} trecho(s) foram modificados.")
    if not (added or removed or modified):
        summary_parts.append("A estrutura da página foi atualizada.")
    if structure["structural_changes"]:
        summary_parts.append(
            f"{len(structure['structural_changes'])} mudança(s) estrutural(is)."
        )
    if structure["seo_changes"]:
        summary_parts.append(f"{len(structure['seo_changes'])} mudança(s) de SEO.")

    identity = hashlib.sha256(
        f"{url}\x1f{previous_text}\x1f{current_text}".encode("utf-8")
    ).hexdigest()[:20]
    return ContentChange(
        change_id=identity,
        url=url,
        title=title or url,
        summary=" ".join(summary_parts),
        added=[_clip(value, 300) for value in added[:8]],
        removed=[_clip(value, 300) for value in removed[:8]],
        modified=[_clip(value, 500) for value in modified[:8]],
        change_types=structure["change_types"],
        structural_changes=structure["structural_changes"],
        seo_changes=structure["seo_changes"],
        technology_changes=structure["technology_changes"],
        form_findings=structure["form_findings"],
        link_findings=structure["link_findings"],
        before_excerpt=_clip(previous_text, 1200),
        after_excerpt=_clip(current_text, 1200),
        page_metrics=page_metrics or {},
    )


def _compare_structure(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, list[str]]:
    structural: list[str] = []
    seo_changes: list[str] = []
    technologies: list[str] = []
    form_findings: list[str] = []
    link_findings: list[str] = []
    change_types: list[str] = []

    for label, key in (
        ("botão", "buttons"),
        ("imagem", "images"),
        ("seção", "sections"),
    ):
        old = _stable_items(previous.get(key, []))
        new = _stable_items(current.get(key, []))
        for item in sorted(new - old)[:12]:
            structural.append(f"{label.title()} adicionado: {item}")
        for item in sorted(old - new)[:12]:
            structural.append(f"{label.title()} removido: {item}")
        if old != new:
            change_types.append(key)

    old_forms = _stable_items(previous.get("forms", []))
    new_forms = _stable_items(current.get("forms", []))
    for item in sorted(new_forms - old_forms)[:8]:
        form_findings.append(f"Formulário adicionado: {item}")
    for item in sorted(old_forms - new_forms)[:8]:
        form_findings.append(f"Formulário removido: {item}")
    if old_forms != new_forms:
        change_types.append("forms")

    old_links = _link_items(previous.get("links", []))
    new_links = _link_items(current.get("links", []))
    for item in sorted(new_links - old_links)[:12]:
        link_findings.append(f"Link adicionado: {item}")
    for item in sorted(old_links - new_links)[:12]:
        link_findings.append(f"Link removido: {item}")
    if old_links != new_links:
        change_types.append("links")

    old_seo = previous.get("seo", {})
    new_seo = current.get("seo", {})
    if isinstance(old_seo, dict) and isinstance(new_seo, dict):
        for key in (
            "title",
            "description",
            "canonical",
            "robots",
            "open_graph",
            "twitter_cards",
            "structured_data",
        ):
            if old_seo.get(key) != new_seo.get(key):
                seo_changes.append(
                    f"{key.replace('_', ' ').title()} alterado"
                )
        if seo_changes:
            change_types.append("seo")

    old_tech = set(map(str, previous.get("technologies", [])))
    new_tech = set(map(str, current.get("technologies", [])))
    technologies.extend(
        f"Tecnologia detectada: {item}" for item in sorted(new_tech - old_tech)
    )
    technologies.extend(
        f"Tecnologia não detectada: {item}" for item in sorted(old_tech - new_tech)
    )
    if technologies:
        change_types.append("technologies")

    return {
        "change_types": list(dict.fromkeys(change_types)),
        "structural_changes": structural[:20],
        "seo_changes": seo_changes[:12],
        "technology_changes": technologies[:12],
        "form_findings": form_findings[:12],
        "link_findings": link_findings[:20],
    }


def _stable_items(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    result = set()
    for value in values:
        if isinstance(value, dict):
            result.add(
                json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            )
        elif value:
            result.add(str(value))
    return result


def _link_items(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    result = set()
    for value in values:
        if isinstance(value, dict):
            href = str(value.get("href", "")).strip()
            text = str(value.get("text", "")).strip()
            if href:
                result.add(f"{text or 'Link'} -> {href}")
        elif value:
            result.add(str(value))
    return result


def _audit_page_structure(page: _PageContent) -> dict[str, list[str]]:
    seo_findings: list[str] = []
    form_findings: list[str] = []
    structure = page.structure if isinstance(page.structure, dict) else {}
    seo = structure.get("seo", {})
    if not isinstance(seo, dict):
        seo = {}
    for key, label in (
        ("title", "title"),
        ("description", "meta description"),
        ("canonical", "canonical"),
        ("robots", "robots"),
        ("open_graph", "Open Graph"),
        ("twitter_cards", "Twitter Cards"),
        ("structured_data", "dados estruturados"),
    ):
        if not seo.get(key):
            seo_findings.append(f"{page.url}: {label} ausente")

    forms = structure.get("forms", [])
    if isinstance(forms, list):
        for index, form in enumerate(forms, 1):
            if not isinstance(form, dict):
                continue
            fields = form.get("fields", [])
            if not fields:
                form_findings.append(
                    f"{page.url}: formulário {index} sem campos detectáveis"
                )
                continue
            enabled_fields = [
                field
                for field in fields
                if isinstance(field, dict) and not field.get("disabled")
            ]
            if not enabled_fields:
                form_findings.append(
                    f"{page.url}: formulário {index} sem campos ativos"
                )
            if not form.get("action"):
                form_findings.append(
                    f"{page.url}: formulário {index} usa a própria página como destino"
                )

    technologies = [
        str(item)
        for item in structure.get("technologies", [])
        if str(item).strip()
    ]
    return {
        "seo": seo_findings,
        "forms": form_findings,
        "technologies": technologies,
    }


def _internal_links(page_url: str, structure: dict[str, Any]) -> set[str]:
    base_host = (urlparse(page_url).hostname or "").removeprefix("www.")
    result = set()
    values = structure.get("links", [])
    if not isinstance(values, list):
        return result
    for item in values:
        href = (
            str(item.get("href", ""))
            if isinstance(item, dict)
            else str(item)
        ).strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        if (parsed.hostname or "").removeprefix("www.") != base_host:
            continue
        clean = parsed._replace(fragment="", query="").geturl()
        result.add(clean)
    return result


def _comparison_units(value: str) -> list[str]:
    units: list[str] = []
    for line in value.splitlines():
        fragments = re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-Ý0-9])", line)
        units.extend(fragment.strip() for fragment in fragments if fragment.strip())
    return units


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."
