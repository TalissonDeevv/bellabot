from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import aiohttp

from .metrics import RequestObservation
from .http_utils import read_limited
from .state import StateStore

MAX_FEEDBACK_BODY_BYTES = 5 * 1024 * 1024
MAX_FEEDBACK_BUNDLES = 12
FEEDBACK_ARCHIVE_SECTION = "feedback_archive"


def _js_string_pattern(name: str) -> str:
    return (
        rf"(?P<{name}_quote>[\"'`])"
        rf"(?P<{name}>(?:\\.|(?!(?P={name}_quote)).)*)"
        rf"(?P={name}_quote)"
    )


FEEDBACK_OBJECT_RE = re.compile(
    r"\{\s*name\s*:\s*"
    + _js_string_pattern("person")
    + r"\s*,\s*role\s*:\s*\{\s*pt\s*:\s*"
    + _js_string_pattern("role")
    + r"\s*,\s*en\s*:\s*"
    + _js_string_pattern("role_en")
    + r"\s*\}\s*,\s*text\s*:\s*\{\s*pt\s*:\s*"
    + _js_string_pattern("message")
    + r"\s*,\s*en\s*:\s*"
    + _js_string_pattern("message_en")
    + r"\s*\}\s*,\s*image\s*:\s*(?:"
    + _js_string_pattern("avatar")
    + r"|(?P<avatar_variable>[A-Za-z_$][A-Za-z0-9_$]*))"
    + r"\s*\}",
    re.DOTALL,
)
FEEDBACK_SCRIPT_RE = re.compile(
    r"[\"'`](?P<src>[^\"'`]*(?:feedback|depoimento|testimonial)[^\"'`]*\.js)[\"'`]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Feedback:
    feedback_id: str
    name: str
    message: str
    source_url: str
    role: str | None = None
    avatar_url: str | None = None
    rating: str | None = None
    source: str = "Página pública de feedbacks"
    additional_info: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FeedbackScanReport:
    checked_at: str
    source_url: str
    feedbacks: list[Feedback] = field(default_factory=list)
    new_feedbacks: list[Feedback] = field(default_factory=list)
    requests: list[RequestObservation] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "source_url": self.source_url,
            "feedbacks": [item.to_dict() for item in self.feedbacks],
            "new_feedbacks": [item.to_dict() for item in self.new_feedbacks],
            "requests": [item.to_dict() for item in self.requests],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeedbackScanReport":
        return cls(
            checked_at=str(data.get("checked_at", "")),
            source_url=str(data.get("source_url", "")),
            feedbacks=[
                Feedback(**item)
                for item in data.get("feedbacks", [])
                if isinstance(item, dict)
            ],
            new_feedbacks=[
                Feedback(**item)
                for item in data.get("new_feedbacks", [])
                if isinstance(item, dict)
            ],
            requests=[
                RequestObservation.from_dict(item)
                for item in data.get("requests", [])
                if isinstance(item, dict)
            ],
            error=data.get("error"),
        )


@dataclass(frozen=True)
class FeedbackStatus:
    total_found: int
    total_synchronized: int
    pending: int
    last_sync: str | None
    monitoring_active: bool
    last_error: str | None = None


class FeedbackMonitor:
    def __init__(self, source_url: str, state: StateStore) -> None:
        self.source_url = source_url
        self.state = state

    async def scan(
        self,
        session: aiohttp.ClientSession,
        *,
        include_processed: bool = False,
    ) -> FeedbackScanReport:
        report = FeedbackScanReport(
            checked_at=datetime.now(timezone.utc).isoformat(),
            source_url=self.source_url,
        )
        try:
            html = await self._fetch_text(
                session,
                _cache_busted_url(self.source_url),
                "feedback_page",
                report,
            )
            parser = _FeedbackPageParser()
            parser.feed(html)
            feedbacks = _parse_json_ld_reviews(parser.json_ld, self.source_url)

            if not feedbacks:
                feedbacks = await self._scan_script_bundles(
                    session,
                    parser.script_sources,
                    report,
                )

            report.feedbacks = _deduplicate_feedbacks(feedbacks)
            if not report.feedbacks:
                raise ValueError(
                    "Nenhum feedback pôde ser extraído da página ou do bundle publicado."
                )

            await self.archive_feedbacks(report.feedbacks, report.checked_at)
            processed = await self.state.get_section("processed_feedbacks")
            report.new_feedbacks = (
                list(report.feedbacks)
                if include_processed
                else [
                    feedback
                    for feedback in report.feedbacks
                    if feedback.feedback_id not in processed
                ]
            )
            await self.state.set("latest_feedback_report", report.to_dict())
        except Exception as exc:
            report.error = f"{type(exc).__name__}: {exc}"
            await self.state.set("latest_feedback_report", report.to_dict())
        return report

    async def archived_feedbacks(self) -> list[Feedback]:
        archived = await self.state.get_section(FEEDBACK_ARCHIVE_SECTION)
        feedbacks: list[Feedback] = []
        for value in archived.values():
            if not isinstance(value, dict):
                continue
            payload = value.get("feedback", value)
            feedback = _feedback_from_dict(payload)
            if feedback is not None:
                feedbacks.append(feedback)
        return _deduplicate_feedbacks(feedbacks)

    async def archive_feedbacks(
        self,
        feedbacks: list[Feedback],
        detected_at: str,
    ) -> None:
        archived = await self.state.get_section(FEEDBACK_ARCHIVE_SECTION)
        missing = [item for item in feedbacks if item.feedback_id not in archived]
        if not missing:
            return

        def mutate(values: dict[str, Any]) -> None:
            for feedback in missing:
                values[feedback.feedback_id] = {
                    "feedback": feedback.to_dict(),
                    "first_seen_at": detected_at,
                }
            if len(values) > 2000:
                oldest = sorted(
                    values.items(),
                    key=lambda item: str(item[1].get("first_seen_at", ""))
                    if isinstance(item[1], dict)
                    else "",
                )
                for feedback_id, _ in oldest[: len(values) - 2000]:
                    del values[feedback_id]

        await self.state.mutate_section(FEEDBACK_ARCHIVE_SECTION, mutate)

    async def mark_processed(self, feedback: Feedback, detected_at: str) -> None:
        def mutate(processed: dict[str, Any]) -> None:
            processed[feedback.feedback_id] = {
                "name": feedback.name,
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "detected_at": detected_at,
            }
            if len(processed) > 2000:
                oldest = sorted(
                    processed.items(),
                    key=lambda item: str(item[1].get("processed_at", "")),
                )
                for feedback_id, _ in oldest[: len(processed) - 2000]:
                    del processed[feedback_id]

        await self.state.mutate_section("processed_feedbacks", mutate)

    async def status(self, monitoring_active: bool = True) -> FeedbackStatus:
        latest = await self.state.get("latest_feedback_report")
        processed = await self.state.get_section("processed_feedbacks")
        total_found = 0
        last_sync = None
        last_error = None
        found_ids: set[str] = set()
        if isinstance(latest, dict):
            last_sync = str(latest.get("checked_at") or "") or None
            last_error = latest.get("error")
            for item in latest.get("feedbacks", []):
                if isinstance(item, dict) and item.get("feedback_id"):
                    found_ids.add(str(item["feedback_id"]))
            total_found = len(found_ids)
        synchronized = len(processed)
        pending = (
            sum(feedback_id not in processed for feedback_id in found_ids)
            if found_ids
            else 0
        )
        return FeedbackStatus(
            total_found=total_found,
            total_synchronized=synchronized,
            pending=pending,
            last_sync=last_sync,
            monitoring_active=monitoring_active,
            last_error=str(last_error) if last_error else None,
        )

    async def _fetch_text(
        self,
        session: aiohttp.ClientSession,
        url: str,
        category: str,
        report: FeedbackScanReport,
    ) -> str:
        start = time.perf_counter()
        error: str | None = None
        status: int | None = None
        try:
            async with session.get(
                url,
                allow_redirects=True,
                headers={
                    "User-Agent": "Discord-Site-Sentinel/2.0",
                    "Accept": "text/html,application/javascript,*/*",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            ) as response:
                status = response.status
                body = await read_limited(
                    response.content,
                    MAX_FEEDBACK_BODY_BYTES,
                )
                if response.status >= 400:
                    raise aiohttp.ClientResponseError(
                        response.request_info,
                        response.history,
                        status=response.status,
                        message=response.reason or "HTTP error",
                        headers=response.headers,
                    )
                charset = response.charset or "utf-8"
                try:
                    return body.decode(charset)
                except (LookupError, UnicodeDecodeError):
                    return body.decode("utf-8", errors="replace")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            report.requests.append(
                RequestObservation(
                    category=category,
                    url=url,
                    status_code=status,
                    response_ms=int((time.perf_counter() - start) * 1000),
                    error=error,
                )
            )

    async def _scan_script_bundles(
        self,
        session: aiohttp.ClientSession,
        script_sources: list[str],
        report: FeedbackScanReport,
    ) -> list[Feedback]:
        queue = [urljoin(self.source_url, source) for source in script_sources]
        seen: set[str] = set()
        while queue and len(seen) < MAX_FEEDBACK_BUNDLES:
            script_url = queue.pop(0)
            if (
                script_url in seen
                or not script_url.startswith(("http://", "https://"))
            ):
                continue
            seen.add(script_url)
            script = await self._fetch_text(
                session,
                script_url,
                "feedback_bundle",
                report,
            )
            feedbacks = _parse_feedback_bundle(script, self.source_url)
            if feedbacks:
                return feedbacks
            for discovered in _discover_feedback_script_sources(
                script,
                bundle_url=script_url,
                source_url=self.source_url,
            ):
                if discovered not in seen and discovered not in queue:
                    queue.append(discovered)
        return []


class _FeedbackPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.script_sources: list[str] = []
        self.json_ld: list[str] = []
        self._collect_json_ld = False
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "script":
            return
        values = {key.casefold(): value or "" for key, value in attrs}
        if values.get("src"):
            self.script_sources.append(values["src"])
        if values.get("type", "").casefold() == "application/ld+json":
            self._collect_json_ld = True
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._collect_json_ld:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._collect_json_ld:
            self.json_ld.append("".join(self._script_parts))
            self._collect_json_ld = False
            self._script_parts = []


def _parse_json_ld_reviews(payloads: list[str], source_url: str) -> list[Feedback]:
    feedbacks: list[Feedback] = []
    for payload in payloads:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            reviews = node.get("review", [])
            if isinstance(reviews, dict):
                reviews = [reviews]
            for review in reviews:
                if not isinstance(review, dict):
                    continue
                author = review.get("author", {})
                name = (
                    author.get("name", "") if isinstance(author, dict) else str(author)
                )
                message = str(review.get("reviewBody", "")).strip()
                if not name or not message:
                    continue
                rating_data = review.get("reviewRating", {})
                rating = (
                    str(rating_data.get("ratingValue"))
                    if isinstance(rating_data, dict)
                    and rating_data.get("ratingValue") is not None
                    else None
                )
                feedbacks.append(
                    _build_feedback(
                        name=name,
                        message=message,
                        source_url=source_url,
                        role=None,
                        avatar_url=None,
                        rating=rating,
                    )
                )
    return feedbacks


def _parse_feedback_bundle(bundle: str, source_url: str) -> list[Feedback]:
    feedbacks: list[Feedback] = []
    for match in FEEDBACK_OBJECT_RE.finditer(bundle):
        name = _decode_js_string(match.group("person")).strip()
        role = _decode_js_string(match.group("role")).strip()
        message = _decode_js_string(match.group("message")).strip()
        avatar_value = match.group("avatar")
        if avatar_value is not None:
            avatar = _decode_js_string(avatar_value).strip()
        else:
            avatar = _resolve_bundle_variable(
                bundle,
                match.group("avatar_variable") or "",
            )
        if not name or not message:
            continue
        feedbacks.append(
            _build_feedback(
                name=name,
                message=message,
                source_url=source_url,
                role=role or None,
                avatar_url=urljoin(source_url, avatar) if avatar else None,
                rating="5",
                additional_info={
                    "idioma": "Português",
                    "fonte_técnica": "bundle público",
                },
            )
        )
    return feedbacks


def _discover_feedback_script_sources(
    bundle: str,
    *,
    bundle_url: str,
    source_url: str,
) -> list[str]:
    sources: list[str] = []
    for match in FEEDBACK_SCRIPT_RE.finditer(bundle):
        raw = match.group("src").strip()
        if not raw:
            continue
        if raw.startswith(("http://", "https://")):
            resolved = raw
        elif raw.startswith("assets/"):
            resolved = urljoin(source_url, f"/{raw}")
        else:
            resolved = urljoin(bundle_url, raw)
        if resolved not in sources:
            sources.append(resolved)
    return sources


def _build_feedback(
    *,
    name: str,
    message: str,
    source_url: str,
    role: str | None,
    avatar_url: str | None,
    rating: str | None,
    additional_info: dict[str, str] | None = None,
) -> Feedback:
    identity = "\x1f".join(
        [
            _normalize(name),
            _normalize(role or ""),
            _normalize(message),
            _normalize(avatar_url or ""),
        ]
    )
    feedback_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return Feedback(
        feedback_id=feedback_id,
        name=name,
        message=message,
        source_url=source_url,
        role=role,
        avatar_url=avatar_url,
        rating=rating,
        additional_info=additional_info or {},
    )


def _deduplicate_feedbacks(feedbacks: list[Feedback]) -> list[Feedback]:
    unique: dict[str, Feedback] = {}
    for feedback in feedbacks:
        unique.setdefault(feedback.feedback_id, feedback)
    return list(unique.values())


def _feedback_from_dict(value: Any) -> Feedback | None:
    if not isinstance(value, dict):
        return None
    try:
        return Feedback(
            feedback_id=str(value["feedback_id"]),
            name=str(value["name"]),
            message=str(value["message"]),
            source_url=str(value["source_url"]),
            role=str(value["role"]) if value.get("role") is not None else None,
            avatar_url=(
                str(value["avatar_url"])
                if value.get("avatar_url") is not None
                else None
            ),
            rating=str(value["rating"]) if value.get("rating") is not None else None,
            source=str(value.get("source") or "Página pública de feedbacks"),
            additional_info={
                str(key): str(item)
                for key, item in value.get("additional_info", {}).items()
            }
            if isinstance(value.get("additional_info"), dict)
            else {},
        )
    except (KeyError, TypeError, ValueError):
        return None


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


def _resolve_bundle_variable(bundle: str, variable: str) -> str:
    if not variable:
        return ""
    match = re.search(
        rf"(?:const|let|var)\s+{re.escape(variable)}\s*=\s*"
        + _js_string_pattern("value"),
        bundle,
        re.DOTALL,
    )
    return _decode_js_string(match.group("value")).strip() if match else ""


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _cache_busted_url(url: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}_sentinel_probe={time.time_ns()}"
