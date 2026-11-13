from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .state import StateStore

LOGGER = logging.getLogger(__name__)

GITHUB_SITE_SECTION = "github_site_monitor"
GITHUB_API_BASE = "https://api.github.com"

CRITICAL_FILE_HINTS = (
    ".env",
    "secret",
    "credential",
    "token",
    "auth",
    "middleware",
    "security",
    "login",
    "admin",
    "payment",
    "checkout",
    "api/",
    "server",
    "supabase",
    "firebase",
    "vercel.json",
    "next.config",
    "package.json",
)

ATTACK_HINTS = (
    "xss",
    "csrf",
    "sql injection",
    "injection",
    "malware",
    "phishing",
    "backdoor",
    "bypass",
    "exploit",
    "leak",
    "hack",
    "token exposed",
    "secret exposed",
)


@dataclass(frozen=True)
class GitHubFileChange:
    filename: str
    status: str
    additions: int = 0
    deletions: int = 0
    changes: int = 0


@dataclass(frozen=True)
class GitHubSiteChange:
    sha: str
    message: str
    author: str
    url: str
    detected_at: str
    severity: str
    risk_reason: str
    additions: int = 0
    deletions: int = 0
    total_changes: int = 0
    files: list[GitHubFileChange] = field(default_factory=list)

    @property
    def short_sha(self) -> str:
        return self.sha[:7]

    @property
    def title(self) -> str:
        headline = self.message.splitlines()[0].strip()
        return headline or f"Commit {self.short_sha}"


@dataclass(frozen=True)
class GitHubSiteReport:
    repository: str
    checked_at: str
    changes: list[GitHubSiteChange] = field(default_factory=list)
    error: str = ""
    configuration_error: bool = False


class GitHubRepositoryAccessError(RuntimeError):
    pass


class GitHubSiteMonitor:
    def __init__(
        self,
        repository: str,
        state: StateStore,
        *,
        token: str = "",
    ) -> None:
        self.repository = _normalize_repository(repository)
        self.state = state
        self.token = token.strip()

    @property
    def configured(self) -> bool:
        return bool(self.repository and "/" in self.repository)

    async def scan(self, session: aiohttp.ClientSession) -> GitHubSiteReport:
        checked_at = datetime.now(timezone.utc).isoformat()
        if not self.configured:
            return GitHubSiteReport(
                repository=self.repository,
                checked_at=checked_at,
                error="Repositório do site não configurado.",
            )

        try:
            commits = await self._get_json(
                session,
                f"/repos/{self.repository}/commits?per_page=8",
            )
            if not isinstance(commits, list):
                raise RuntimeError("Resposta inesperada da API de commits.")

            known = await self._known_shas()
            newest_shas = [
                str(item.get("sha", ""))
                for item in commits
                if isinstance(item, dict) and item.get("sha")
            ]
            if not known:
                await self._store_seen(newest_shas)
                return GitHubSiteReport(
                    repository=self.repository,
                    checked_at=checked_at,
                )

            changes: list[GitHubSiteChange] = []
            for sha in newest_shas:
                if sha in known:
                    continue
                detail = await self._get_json(
                    session,
                    f"/repos/{self.repository}/commits/{sha}",
                )
                if isinstance(detail, dict):
                    changes.append(self._parse_change(detail, checked_at))

            await self._store_seen([*newest_shas, *known])
            return GitHubSiteReport(
                repository=self.repository,
                checked_at=checked_at,
                changes=changes,
            )
        except GitHubRepositoryAccessError as exc:
            LOGGER.warning("Monitor GitHub do site desativado nesta rodada: %s", exc)
            return GitHubSiteReport(
                repository=self.repository,
                checked_at=checked_at,
                error=str(exc),
                configuration_error=True,
            )
        except Exception as exc:
            LOGGER.exception("Falha ao monitorar repositório GitHub do site")
            return GitHubSiteReport(
                repository=self.repository,
                checked_at=checked_at,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _get_json(
        self,
        session: aiohttp.ClientSession,
        path: str,
    ) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Bella-Site-Monitor",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        async with session.get(
            f"{GITHUB_API_BASE}{path}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            text = await response.text()
            if response.status == 403 and "rate limit" in text.lower():
                raise RuntimeError("Limite público da API GitHub atingido.")
            if response.status == 404:
                raise GitHubRepositoryAccessError(
                    "Repositório GitHub não encontrado ou privado. "
                    "Confira SITE_REPOSITORY no formato owner/repo e, se o repositório for privado, configure GITHUB_TOKEN com acesso de leitura."
                )
            if response.status >= 400:
                raise RuntimeError(
                    f"GitHub HTTP {response.status}: {text[:300]}"
                )
            return await response.json(content_type=None)

    def _parse_change(
        self,
        detail: dict[str, Any],
        checked_at: str,
    ) -> GitHubSiteChange:
        commit = detail.get("commit") if isinstance(detail.get("commit"), dict) else {}
        author_data = commit.get("author") if isinstance(commit.get("author"), dict) else {}
        stats = detail.get("stats") if isinstance(detail.get("stats"), dict) else {}
        files = [
            GitHubFileChange(
                filename=str(item.get("filename", "")),
                status=str(item.get("status", "")),
                additions=_safe_int(item.get("additions")),
                deletions=_safe_int(item.get("deletions")),
                changes=_safe_int(item.get("changes")),
            )
            for item in detail.get("files", [])
            if isinstance(item, dict)
        ]
        severity, reason = _classify_change(str(commit.get("message", "")), files)
        return GitHubSiteChange(
            sha=str(detail.get("sha", "")),
            message=str(commit.get("message", "")),
            author=str(author_data.get("name") or detail.get("author", {}).get("login") or "GitHub"),
            url=str(detail.get("html_url", "")),
            detected_at=checked_at,
            severity=severity,
            risk_reason=reason,
            additions=_safe_int(stats.get("additions")),
            deletions=_safe_int(stats.get("deletions")),
            total_changes=_safe_int(stats.get("total")),
            files=files,
        )

    async def _known_shas(self) -> list[str]:
        section = await self.state.get_section(GITHUB_SITE_SECTION)
        value = section.get(self.repository, {})
        if not isinstance(value, dict):
            return []
        shas = value.get("seen_shas", [])
        return [str(item) for item in shas if item]

    async def _store_seen(self, shas: list[str]) -> None:
        unique: list[str] = []
        for sha in shas:
            if sha and sha not in unique:
                unique.append(sha)

        def mutate(section: dict[str, Any]) -> None:
            section[self.repository] = {
                "seen_shas": unique[:80],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

        await self.state.mutate_section(GITHUB_SITE_SECTION, mutate)


def _classify_change(
    message: str,
    files: list[GitHubFileChange],
) -> tuple[str, str]:
    text = " ".join([message, *(item.filename for item in files)]).lower()
    normalized = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)
    if any(hint in text or hint in normalized for hint in ATTACK_HINTS):
        return "critical", "Possível alteração relacionada a ataque, vazamento ou bypass."
    if any(hint in text for hint in (".env", "secret", "credential", "token")):
        return "critical", "Alteração em arquivo ou termo sensível."
    if any(_file_has_hint(item.filename, CRITICAL_FILE_HINTS) for item in files):
        return "high", "Alteração em área sensível do site ou configuração."
    if len(files) >= 8:
        return "medium", "Commit alterou muitas áreas do site ao mesmo tempo."
    return "medium", "Alteração normal detectada no repositório do site."


def _file_has_hint(filename: str, hints: tuple[str, ...]) -> bool:
    lower = filename.lower().replace("\\", "/")
    return any(hint in lower for hint in hints)


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_repository(repository: str) -> str:
    value = str(repository or "").strip()
    if not value:
        return ""
    markdown_match = re.search(r"\((https?://github\.com/[^)]+)\)", value, flags=re.IGNORECASE)
    if markdown_match:
        value = markdown_match.group(1)
    if "github.com" in value.lower():
        parsed = urlparse(value)
        if parsed.netloc.lower().endswith("github.com"):
            value = parsed.path
    value = value.strip().strip("<>").strip("/")
    parts = [part for part in value.split("/") if part]
    if len(parts) >= 2:
        owner, repo = parts[0], parts[1]
        return f"{owner}/{repo.removesuffix('.git')}"
    return value.removesuffix(".git")
