from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Awaitable, Callable, Iterable

import discord

from .central_logs import CentralLogService
from .security import (
    RecommendationEngine,
    SecurityCollector,
    SecurityRuleEngine,
    SnapshotDiffEngine,
    SnapshotNormalizer,
)
from .state import StateStore

LOGGER = logging.getLogger(__name__)

SECURITY_SCANS_SECTION = "security_scans"
SECURITY_FINDINGS_SECTION = "security_findings"
SECURITY_SNAPSHOTS_SECTION = "security_snapshots"
SECURITY_JOBS_SECTION = "security_scan_jobs"
SECURITY_CONFIG_SECTION = "security_scan_config"
SECURITY_EXCEPTIONS_SECTION = "security_exceptions"
SECURITY_PANEL_SECTION = "security_panel_config"
SECURITY_MONITOR_SECTION = "security_monitor_events"
SECURITY_SCHEMA_VERSION = 2
SCANNER_VERSION = "2.0.0"


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    COLLECTING = "COLLECTING"
    ANALYZING = "ANALYZING"
    GENERATING_RECOMMENDATIONS = "GENERATING_RECOMMENDATIONS"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class FindingKind(StrEnum):
    FINDING = "FINDING"
    WARNING = "WARNING"
    RECOMMENDATION = "RECOMMENDATION"


SEVERITY_CRITERIA: dict[str, str] = {
    Severity.CRITICAL: "Exposição ampla ou controle total com impacto imediato e grande alcance.",
    Severity.HIGH: "Permissão administrativa ampla, recurso sensível exposto ou combinação perigosa com impacto relevante.",
    Severity.MEDIUM: "Configuração excessiva ou frágil que merece revisão, mas depende de contexto adicional para exploração.",
    Severity.LOW: "Desvio de boas práticas, inconsistência ou risco limitado.",
    Severity.INFO: "Informação contextual, limitação da API ou mudança sem risco confirmado.",
}

SENSITIVE_PERMISSIONS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_webhooks",
    "ban_members",
    "kick_members",
    "mention_everyone",
    "manage_messages",
    "moderate_members",
    "manage_events",
    "manage_threads",
    "view_audit_log",
)
CONTROL_PLANE_PERMISSIONS = {
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_webhooks",
}
ADMIN_CHANNEL_TERMS = {
    "admin",
    "administracao",
    "staff",
    "moderacao",
    "moderação",
    "seguranca",
    "segurança",
    "logs",
    "auditoria",
    "backups",
    "sistema",
    "interno",
}


@dataclass(frozen=True)
class SecurityFinding:
    id: str
    rule_id: str
    category: str
    severity: Severity
    title: str
    description: str
    evidence: str
    resource_type: str
    resource_id: str
    resource_name: str
    reason: str
    recommendation: str
    timestamp: str
    status: str = "OPEN"
    kind: FindingKind = FindingKind.FINDING
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        data["kind"] = self.kind.value
        return data


@dataclass(frozen=True)
class SecurityChange:
    resource_key: str
    change_type: str
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SecurityScanResult:
    scan_id: str
    guild_id: int
    status: JobStatus
    started_at: str
    finished_at: str
    findings: tuple[SecurityFinding, ...]
    changes: tuple[SecurityChange, ...]
    source_errors: tuple[str, ...]
    snapshot: dict[str, Any]
    actor_id: int = 0
    scanner_version: str = SCANNER_VERSION
    snapshot_schema_version: int = SECURITY_SCHEMA_VERSION
    areas_analyzed: tuple[str, ...] = ()
    areas_unavailable: tuple[str, ...] = ()
    finding_diff: dict[str, list[str]] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        counter = Counter(item.severity.value for item in self.findings)
        return {severity.value: counter.get(severity.value, 0) for severity in Severity}


@dataclass
class SecurityScanConfig:
    guild_id: int
    enabled_areas: list[str] = field(
        default_factory=lambda: ["guild", "roles", "members", "channels", "automod", "bots", "webhooks", "integrations"]
    )
    keep_scans: int = 20

    @classmethod
    def from_dict(cls, guild_id: int, value: Any) -> "SecurityScanConfig":
        data = value if isinstance(value, dict) else {}
        allowed = {"guild", "roles", "members", "channels", "automod", "bots", "webhooks", "integrations"}
        areas = [str(item) for item in data.get("enabled_areas", []) if str(item) in allowed]
        return cls(
            guild_id=guild_id,
            enabled_areas=areas or sorted(allowed),
            keep_scans=max(2, min(50, int(data.get("keep_scans", 20) or 20))),
        )


AuthorizationCallback = Callable[[discord.Member], bool]
AIExplanationCallback = Callable[[str], Awaitable[str]]


class SecurityRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    async def config(self, guild_id: int) -> SecurityScanConfig:
        section = await self.state.get_section(SECURITY_CONFIG_SECTION)
        return SecurityScanConfig.from_dict(guild_id, section.get(str(guild_id)))

    async def save_result(self, result: SecurityScanResult, *, keep: int) -> None:
        scan_record = {
            "scan_id": result.scan_id,
            "guild_id": result.guild_id,
            "status": result.status.value,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "counts": result.counts,
            "finding_ids": [item.id for item in result.findings],
            "changes": [item.to_dict() for item in result.changes],
            "source_errors": list(result.source_errors),
            "executor_id": result.actor_id,
            "scanner_version": result.scanner_version,
            "snapshot_schema_version": result.snapshot_schema_version,
            "areas_analyzed": list(result.areas_analyzed),
            "areas_unavailable": list(result.areas_unavailable),
            "finding_diff": result.finding_diff,
            "schema_version": SECURITY_SCHEMA_VERSION,
        }
        key = _scan_key(result.guild_id, result.scan_id)
        await self.state.update_section(SECURITY_SCANS_SECTION, key, scan_record)
        await self.state.update_section(SECURITY_SNAPSHOTS_SECTION, key, result.snapshot)

        def mutate_findings(section: dict[str, Any]) -> None:
            for finding in result.findings:
                section[f"{key}:{finding.id}"] = {
                    **finding.to_dict(),
                    "scan_id": result.scan_id,
                    "guild_id": result.guild_id,
                }

        await self.state.mutate_section(SECURITY_FINDINGS_SECTION, mutate_findings)
        await self._prune(result.guild_id, keep)

    async def scans(self, guild_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        section = await self.state.get_section(SECURITY_SCANS_SECTION)
        prefix = f"{guild_id}:"
        values = [dict(item) for key, item in section.items() if key.startswith(prefix) and isinstance(item, dict)]
        values.sort(key=lambda item: str(item.get("started_at", "")), reverse=True)
        return values[:limit]

    async def latest(self, guild_id: int) -> dict[str, Any] | None:
        scans = await self.scans(guild_id, limit=1)
        return scans[0] if scans else None

    async def snapshot(self, guild_id: int, scan_id: str) -> dict[str, Any] | None:
        section = await self.state.get_section(SECURITY_SNAPSHOTS_SECTION)
        value = section.get(_scan_key(guild_id, scan_id))
        return dict(value) if isinstance(value, dict) else None

    async def findings(self, guild_id: int, scan_id: str) -> list[SecurityFinding]:
        section = await self.state.get_section(SECURITY_FINDINGS_SECTION)
        prefix = f"{guild_id}:{scan_id}:"
        result: list[SecurityFinding] = []
        for key, value in section.items():
            if not key.startswith(prefix) or not isinstance(value, dict):
                continue
            try:
                result.append(
                    SecurityFinding(
                        id=str(value["id"]),
                        rule_id=str(value["rule_id"]),
                        category=str(value["category"]),
                        severity=Severity(str(value["severity"])),
                        title=str(value["title"]),
                        description=str(value["description"]),
                        evidence=str(value["evidence"]),
                        resource_type=str(value["resource_type"]),
                        resource_id=str(value["resource_id"]),
                        resource_name=str(value["resource_name"]),
                        reason=str(value["reason"]),
                        recommendation=str(value["recommendation"]),
                        timestamp=str(value["timestamp"]),
                        status=str(value.get("status", "OPEN")),
                        kind=FindingKind(str(value.get("kind", FindingKind.FINDING.value))),
                        fingerprint=str(value.get("fingerprint", "")),
                    )
                )
            except (KeyError, ValueError):
                continue
        return sorted(result, key=lambda item: (_severity_rank(item.severity), item.title))

    async def exceptions(self, guild_id: int) -> list[dict[str, Any]]:
        section = await self.state.get_section(SECURITY_EXCEPTIONS_SECTION)
        prefix = f"{guild_id}:"
        return [dict(value) for key, value in section.items() if key.startswith(prefix) and isinstance(value, dict)]

    async def set_exception(
        self,
        guild_id: int,
        finding: SecurityFinding,
        *,
        actor_id: int,
        mode: str,
        reason: str = "",
        valid_until: str = "",
        scan_id: str = "",
    ) -> None:
        payload = {
            "guild_id": guild_id,
            "finding_id": finding.id,
            "rule_id": finding.rule_id,
            "resource_type": finding.resource_type,
            "resource_id": finding.resource_id,
            "fingerprint": finding.fingerprint,
            "mode": mode,
            "actor_id": actor_id,
            "reason": reason[:500],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "valid_until": valid_until,
            "scan_id": scan_id,
        }
        await self.state.update_section(SECURITY_EXCEPTIONS_SECTION, f"{guild_id}:{finding.id}", payload)

    async def remove_exception(self, guild_id: int, finding_id: str) -> None:
        key = f"{guild_id}:{finding_id}"

        def mutate(section: dict[str, Any]) -> None:
            section.pop(key, None)

        await self.state.mutate_section(SECURITY_EXCEPTIONS_SECTION, mutate)

    async def update_finding_status(self, guild_id: int, scan_id: str, finding_id: str, status: str) -> None:
        key = f"{guild_id}:{scan_id}:{finding_id}"

        def mutate(section: dict[str, Any]) -> None:
            value = section.get(key)
            if isinstance(value, dict):
                value["status"] = status

        await self.state.mutate_section(SECURITY_FINDINGS_SECTION, mutate)

    async def apply_exceptions(
        self,
        guild_id: int,
        scan_id: str,
        findings: Iterable[SecurityFinding],
    ) -> tuple[SecurityFinding, ...]:
        exceptions = {str(item.get("finding_id")): item for item in await self.exceptions(guild_id)}
        now = datetime.now(timezone.utc)
        result: list[SecurityFinding] = []
        for finding in findings:
            exception = exceptions.get(finding.id)
            status = finding.status
            if exception:
                valid_until = str(exception.get("valid_until", ""))
                expired = False
                if valid_until:
                    try:
                        expired = datetime.fromisoformat(valid_until) <= now
                    except ValueError:
                        expired = True
                same_fingerprint = str(exception.get("fingerprint", "")) == finding.fingerprint
                mode = str(exception.get("mode", ""))
                same_scan = str(exception.get("scan_id", "")) == scan_id
                if not expired and same_fingerprint and mode == "INTENTIONAL":
                    status = "INTENTIONAL"
                elif not expired and same_fingerprint and mode == "IGNORE_ONCE" and same_scan:
                    status = "IGNORED_ONCE"
            result.append(
                SecurityFinding(
                    **{**finding.to_dict(), "severity": finding.severity, "kind": finding.kind, "status": status}
                )
            )
        return tuple(result)

    async def panel_config(self, guild_id: int) -> dict[str, Any] | None:
        section = await self.state.get_section(SECURITY_PANEL_SECTION)
        value = section.get(str(guild_id))
        return dict(value) if isinstance(value, dict) else None

    async def save_panel_config(self, guild_id: int, payload: dict[str, Any]) -> None:
        await self.state.update_section(SECURITY_PANEL_SECTION, str(guild_id), payload)

    async def record_monitor_event(self, guild_id: int, event: dict[str, Any]) -> None:
        def mutate(section: dict[str, Any]) -> None:
            values = section.setdefault(str(guild_id), [])
            if not isinstance(values, list):
                values = []
            values.append(event)
            section[str(guild_id)] = values[-300:]

        await self.state.mutate_section(SECURITY_MONITOR_SECTION, mutate)

    async def set_job(self, guild_id: int, job_id: str, payload: dict[str, Any]) -> None:
        await self.state.update_section(SECURITY_JOBS_SECTION, _scan_key(guild_id, job_id), payload)

    async def _prune(self, guild_id: int, keep: int) -> None:
        scans = await self.scans(guild_id, limit=1000)
        remove = {str(item.get("scan_id")) for item in scans[keep:] if item.get("scan_id")}
        if not remove:
            return
        prefix = f"{guild_id}:"

        def prune_scans(section: dict[str, Any]) -> None:
            for key in list(section):
                if key.startswith(prefix) and key.split(":", 1)[1] in remove:
                    section.pop(key, None)

        def prune_findings(section: dict[str, Any]) -> None:
            for key in list(section):
                parts = key.split(":", 2)
                if len(parts) >= 2 and parts[0] == str(guild_id) and parts[1] in remove:
                    section.pop(key, None)

        await self.state.mutate_section(SECURITY_SCANS_SECTION, prune_scans)
        await self.state.mutate_section(SECURITY_SNAPSHOTS_SECTION, prune_scans)
        await self.state.mutate_section(SECURITY_FINDINGS_SECTION, prune_findings)


class SecurityCenter:
    def __init__(
        self,
        state: StateStore,
        *,
        admin_role_ids: Iterable[int] = (),
        bella_user_id: int = 0,
        logs: CentralLogService | None = None,
        ai_explainer: AIExplanationCallback | None = None,
        authorize: AuthorizationCallback | None = None,
    ) -> None:
        self.state = state
        self.repository = SecurityRepository(state)
        self.admin_role_ids = {int(item) for item in admin_role_ids}
        self.bella_user_id = int(bella_user_id)
        self.logs = logs
        self.ai_explainer = ai_explainer
        self.authorize = authorize or (lambda member: False)
        self.collector = SecurityCollector()
        self.rule_engine = SecurityRuleEngine()
        self.diff_engine = SnapshotDiffEngine()
        self.recommendation_engine = RecommendationEngine()
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: dict[int, asyncio.Task[SecurityScanResult]] = {}

    def start_scan(self, guild: discord.Guild, *, actor_id: int) -> str:
        current = self._tasks.get(guild.id)
        if current is not None and not current.done():
            raise RuntimeError("Já existe uma análise de segurança em andamento neste servidor.")
        job_id = uuid.uuid4().hex[:16]
        task = asyncio.create_task(self.run_scan(guild, actor_id=actor_id, scan_id=job_id))
        self._tasks[guild.id] = task
        task.add_done_callback(lambda finished: self._scan_finished(guild.id, finished))
        return job_id

    def _scan_finished(self, guild_id: int, task: asyncio.Task[SecurityScanResult]) -> None:
        self._tasks.pop(guild_id, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            LOGGER.exception("Scan de segurança em background falhou no servidor %s.", guild_id)

    async def run_scan(self, guild: discord.Guild, *, actor_id: int, scan_id: str | None = None) -> SecurityScanResult:
        lock = self._locks.setdefault(guild.id, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("Já existe uma análise de segurança em andamento neste servidor.")
        async with lock:
            scan_id = scan_id or uuid.uuid4().hex[:16]
            started = datetime.now(timezone.utc)
            await self.repository.set_job(
                guild.id,
                scan_id,
                _job_payload(scan_id, guild.id, actor_id, JobStatus.PENDING, started_at=started.isoformat()),
            )
            if self.logs is not None:
                await self.logs.send_event(
                    guild,
                    "security",
                    title="Scan de segurança iniciado",
                    description=f"Análise `{scan_id}` iniciada por <@{actor_id}>.",
                    severity="INFO",
                )
            try:
                await self._set_stage(guild, scan_id, actor_id, JobStatus.COLLECTING, started)
                snapshot, source_errors = await self.collector.collect(guild)
                config = await self.repository.config(guild.id)
                await self._set_stage(guild, scan_id, actor_id, JobStatus.ANALYZING, started)
                findings = tuple(
                    _convert_rule_finding(item)
                    for item in self.rule_engine.analyze(
                        snapshot,
                        admin_role_ids=self.admin_role_ids,
                        bella_user_id=self.bella_user_id,
                        enabled_areas=set(config.enabled_areas),
                    )
                )
                findings = await self.repository.apply_exceptions(guild.id, scan_id, findings)
                await self._set_stage(guild, scan_id, actor_id, JobStatus.GENERATING_RECOMMENDATIONS, started)
                previous_records = await self.repository.scans(guild.id, limit=1)
                previous_snapshot = None
                previous_findings: list[SecurityFinding] = []
                if previous_records:
                    previous_scan_id = str(previous_records[0].get("scan_id") or "")
                    if previous_scan_id:
                        previous_snapshot = await self.repository.snapshot(guild.id, previous_scan_id)
                        previous_findings = await self.repository.findings(guild.id, previous_scan_id)
                changes = tuple(
                    SecurityChange(item.resource_key, item.change_type, item.before, item.after)
                    for item in self.diff_engine.compare(previous_snapshot or {}, snapshot)
                )
                status = JobStatus.PARTIAL if source_errors else JobStatus.COMPLETED
                current_ids = {item.id for item in findings if item.status == "OPEN"}
                previous_ids = {item.id for item in previous_findings if item.status == "OPEN"}
                recommendation_ids = {item.id for item in findings if item.kind == FindingKind.RECOMMENDATION}
                finding_diff = {
                    "new": sorted(current_ids - previous_ids),
                    "resolved": sorted(previous_ids - current_ids),
                    "new_recommendations": sorted((current_ids - previous_ids) & recommendation_ids),
                }
                finished = datetime.now(timezone.utc)
                result = SecurityScanResult(
                    scan_id=scan_id,
                    guild_id=guild.id,
                    status=status,
                    started_at=started.isoformat(),
                    finished_at=finished.isoformat(),
                    findings=findings,
                    changes=changes,
                    source_errors=tuple(source_errors),
                    snapshot=snapshot,
                    actor_id=actor_id,
                    areas_analyzed=tuple(snapshot.get("areas_analyzed", [])),
                    areas_unavailable=tuple(snapshot.get("areas_unavailable", [])),
                    finding_diff=finding_diff,
                )
                await self.repository.save_result(result, keep=config.keep_scans)
                await self.repository.set_job(
                    guild.id,
                    scan_id,
                    _job_payload(
                        scan_id,
                        guild.id,
                        actor_id,
                        status,
                        started_at=started.isoformat(),
                        finished_at=finished.isoformat(),
                        details={"counts": result.counts, "source_errors": source_errors},
                    ),
                )
                await self._log_result(guild, result, actor_id)
                await self.refresh_fixed_panel(guild)
                return result
            except asyncio.CancelledError:
                finished = datetime.now(timezone.utc)
                await self.repository.set_job(
                    guild.id,
                    scan_id,
                    _job_payload(
                        scan_id,
                        guild.id,
                        actor_id,
                        JobStatus.CANCELLED,
                        started_at=started.isoformat(),
                        finished_at=finished.isoformat(),
                    ),
                )
                await self.refresh_fixed_panel(guild, active_status=JobStatus.CANCELLED.value)
                raise
            except Exception as exc:
                finished = datetime.now(timezone.utc)
                await self.repository.set_job(
                    guild.id,
                    scan_id,
                    _job_payload(
                        scan_id,
                        guild.id,
                        actor_id,
                        JobStatus.FAILED,
                        started_at=started.isoformat(),
                        finished_at=finished.isoformat(),
                        error=f"{type(exc).__name__}: {exc}",
                    ),
                )
                if self.logs is not None:
                    await self.logs.send_event(
                        guild,
                        "security",
                        title="Scan de segurança falhou",
                        description=f"Análise `{scan_id}`: {type(exc).__name__}: {exc}",
                        severity="HIGH",
                        critical=True,
                    )
                await self.refresh_fixed_panel(guild, active_status=JobStatus.FAILED.value)
                raise

    async def _set_stage(
        self,
        guild: discord.Guild,
        scan_id: str,
        actor_id: int,
        status: JobStatus,
        started: datetime,
    ) -> None:
        await self.repository.set_job(
            guild.id,
            scan_id,
            _job_payload(scan_id, guild.id, actor_id, status, started_at=started.isoformat()),
        )
        await self.refresh_fixed_panel(guild, active_status=status.value)

    async def publish_panel(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        *,
        actor_id: int,
        authorize: AuthorizationCallback,
        force_recreate: bool = False,
    ) -> discord.Message:
        config = await self.repository.panel_config(guild.id)
        if config and not force_recreate:
            existing_channel = guild.get_channel(int(config.get("channel_id", 0) or 0))
            if isinstance(existing_channel, discord.TextChannel):
                try:
                    message = await existing_channel.fetch_message(int(config.get("message_id", 0) or 0))
                    view = await self.fixed_panel_view(guild, authorize=authorize)
                    await message.edit(view=view)
                    return message
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        view = await self.fixed_panel_view(guild, authorize=authorize)
        message = await channel.send(view=view)
        await self.repository.save_panel_config(
            guild.id,
            {
                "guild_id": guild.id,
                "channel_id": channel.id,
                "message_id": message.id,
                "created_by": actor_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return message

    async def fixed_panel_view(
        self,
        guild: discord.Guild,
        *,
        authorize: AuthorizationCallback,
        active_status: str = "",
    ) -> "PersistentSecurityPanelView":
        view = PersistentSecurityPanelView(self, authorize)
        await view.refresh(guild, active_status=active_status)
        return view

    async def refresh_fixed_panel(self, guild: discord.Guild, *, active_status: str = "") -> bool:
        config = await self.repository.panel_config(guild.id)
        if not config:
            return False
        channel = guild.get_channel(int(config.get("channel_id", 0) or 0))
        if not isinstance(channel, discord.TextChannel):
            await self.repository.save_panel_config(guild.id, {**config, "missing": True})
            return False
        try:
            message = await channel.fetch_message(int(config.get("message_id", 0) or 0))
            view = PersistentSecurityPanelView(self, self.authorize)
            await view.refresh(guild, active_status=active_status)
            await message.edit(view=view)
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await self.repository.save_panel_config(guild.id, {**config, "missing": True})
            return False

    async def hardening_plan(self, guild_id: int, scan_id: str) -> dict[str, list[dict[str, str]]]:
        return self.recommendation_engine.hardening_plan(await self.repository.findings(guild_id, scan_id))

    async def explain_finding(self, finding: SecurityFinding, question: str) -> str:
        if self.ai_explainer is None:
            return "A explicação por IA não está configurada. Use a evidência e a recomendação determinística exibidas."
        prompt = (
            "Explique em português do Brasil um finding de segurança do Discord. Não invente endpoints, "
            "não afirme exploração e não sugira alteração automática.\n"
            f"Regra: {finding.rule_id}\nTipo: {finding.kind.value}\nSeveridade: {finding.severity.value}\n"
            f"Título: {finding.title}\nDescrição: {finding.description}\nEvidência: {finding.evidence}\n"
            f"Motivo: {finding.reason}\nRecomendação determinística: {finding.recommendation}\n"
            f"Pergunta do administrador: {question[:800]}"
        )
        return (await self.ai_explainer(prompt))[:3500]

    async def panel_view(
        self,
        guild: discord.Guild,
        *,
        owner_id: int,
        authorize: AuthorizationCallback,
    ) -> "SecurityPanelView":
        view = SecurityPanelView(self, guild, owner_id, authorize)
        await view.refresh()
        return view

    async def _log_result(self, guild: discord.Guild, result: SecurityScanResult, actor_id: int) -> None:
        if self.logs is None:
            return
        counts = result.counts
        critical = counts[Severity.CRITICAL.value] > 0
        await self.logs.send_event(
            guild,
            "security",
            title="Scan de segurança concluído",
            description=(
                f"Análise `{result.scan_id}` iniciada por <@{actor_id}> terminou como "
                f"**{result.status.value}**. Mudanças detectadas: **{len(result.changes)}**."
            ),
            severity="CRITICAL" if critical else "INFO",
            fields=[
                ("Críticos", str(counts[Severity.CRITICAL.value]), True),
                ("Altos", str(counts[Severity.HIGH.value]), True),
                ("Médios", str(counts[Severity.MEDIUM.value]), True),
            ],
            critical=critical,
        )


async def collect_security_snapshot(guild: discord.Guild) -> tuple[dict[str, Any], list[str]]:
    return await SecurityCollector().collect(guild)

    # Implementação legada mantida temporariamente para compatibilidade de histórico.
    errors: list[str] = []
    roles = [_serialize_role(role) for role in guild.roles]
    channels = [_serialize_channel(channel, guild) for channel in guild.channels]
    bots = [_serialize_bot(member) for member in guild.members if member.bot]

    automod_rules: list[dict[str, Any]] = []
    try:
        automod_rules = [_serialize_automod_rule(rule) for rule in await guild.fetch_automod_rules()]
    except (discord.Forbidden, discord.HTTPException) as exc:
        errors.append(f"automod:{type(exc).__name__}")

    webhooks: list[dict[str, Any]] = []
    try:
        webhooks = [_serialize_webhook(item) for item in await guild.webhooks()]
    except (discord.Forbidden, discord.HTTPException) as exc:
        errors.append(f"webhooks:{type(exc).__name__}")

    integrations: list[dict[str, Any]] = []
    try:
        integrations = [_serialize_integration(item) for item in await guild.integrations()]
    except (discord.Forbidden, discord.HTTPException) as exc:
        errors.append(f"integrations:{type(exc).__name__}")

    audit_entries: list[dict[str, Any]] = []
    if guild.me is not None and guild.me.guild_permissions.view_audit_log:
        try:
            async for entry in guild.audit_logs(limit=25):
                audit_entries.append(
                    {
                        "id": int(entry.id),
                        "action": str(entry.action),
                        "user_id": int(entry.user.id) if entry.user else None,
                        "target_id": int(entry.target.id) if getattr(entry.target, "id", None) else None,
                        "created_at": entry.created_at.isoformat(),
                    }
                )
        except (discord.Forbidden, discord.HTTPException) as exc:
            errors.append(f"audit_log:{type(exc).__name__}")

    snapshot = normalize_security_snapshot(
        {
            "schema_version": SECURITY_SCHEMA_VERSION,
            "guild": {
                "id": guild.id,
                "name": guild.name,
                "owner_id": guild.owner_id,
                "member_count": guild.member_count,
                "verification_level": int(guild.verification_level.value),
                "explicit_content_filter": int(guild.explicit_content_filter.value),
                "mfa_level": int(guild.mfa_level.value),
            },
            "roles": roles,
            "channels": channels,
            "automod": automod_rules,
            "bots": bots,
            "webhooks": webhooks,
            "integrations": integrations,
            "audit_log": audit_entries,
            "source_errors": sorted(errors),
        }
    )
    return snapshot, errors


def analyze_security_snapshot(
    snapshot: dict[str, Any],
    *,
    admin_role_ids: set[int] | None = None,
    bella_user_id: int = 0,
    enabled_areas: set[str] | None = None,
) -> list[SecurityFinding]:
    return [
        _convert_rule_finding(item)
        for item in SecurityRuleEngine().analyze(
            snapshot,
            admin_role_ids=admin_role_ids or set(),
            bella_user_id=bella_user_id,
            enabled_areas=enabled_areas,
        )
    ]

    # Implementação legada mantida temporariamente para compatibilidade de histórico.
    admin_ids = {int(item) for item in (admin_role_ids or set())}
    areas = enabled_areas or {"roles", "channels", "automod", "bots", "webhooks", "integrations"}
    findings: list[SecurityFinding] = []
    if "roles" in areas:
        findings.extend(_analyze_roles(snapshot, admin_ids))
    if "channels" in areas:
        findings.extend(_analyze_channels(snapshot))
    if "automod" in areas:
        findings.extend(_analyze_automod(snapshot))
    if "bots" in areas:
        findings.extend(_analyze_bots(snapshot, bella_user_id))
    if "webhooks" in areas:
        findings.extend(_analyze_webhooks(snapshot))
    if "integrations" in areas:
        findings.extend(_analyze_integrations(snapshot))
    return sorted(findings, key=lambda item: (_severity_rank(item.severity), item.category, item.title))


def normalize_security_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return SnapshotNormalizer().normalize(snapshot)

    # Implementação legada mantida temporariamente para compatibilidade de histórico.
    data = json.loads(json.dumps(snapshot, ensure_ascii=False, default=str))
    sort_keys = {
        "roles": "id",
        "channels": "id",
        "automod": "id",
        "bots": "id",
        "webhooks": "id",
        "integrations": "id",
        "audit_log": "id",
    }
    for key, identity in sort_keys.items():
        values = data.get(key)
        if isinstance(values, list):
            data[key] = sorted(
                (_normalize_value(item) for item in values),
                key=lambda item: str(item.get(identity, "")) if isinstance(item, dict) else str(item),
            )
    data["guild"] = _normalize_value(data.get("guild", {}))
    data["source_errors"] = sorted(str(item) for item in data.get("source_errors", []))
    return data


def compare_security_snapshots(before: dict[str, Any], after: dict[str, Any]) -> list[SecurityChange]:
    return [
        SecurityChange(item.resource_key, item.change_type, item.before, item.after)
        for item in SnapshotDiffEngine().compare(before, after)
    ]

    # Implementação legada mantida temporariamente para compatibilidade de histórico.
    if not before:
        return []
    previous = _security_projection(normalize_security_snapshot(before))
    current = _security_projection(normalize_security_snapshot(after))
    changes: list[SecurityChange] = []
    for key in sorted(set(previous) | set(current)):
        if key not in previous:
            changes.append(SecurityChange(key, "ADDED", None, current[key]))
        elif key not in current:
            changes.append(SecurityChange(key, "REMOVED", previous[key], None))
        elif previous[key] != current[key]:
            changes.append(SecurityChange(key, "CHANGED", previous[key], current[key]))
    return changes


class SecurityPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        center: SecurityCenter,
        guild: discord.Guild,
        owner_id: int,
        authorize: AuthorizationCallback,
    ) -> None:
        super().__init__(timeout=900)
        self.center = center
        self.guild = guild
        self.owner_id = owner_id
        self.authorize = authorize
        self.latest: dict[str, Any] | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = (
            interaction.user.id == self.owner_id
            and isinstance(interaction.user, discord.Member)
            and self.authorize(interaction.user)
        )
        if not allowed:
            await interaction.response.send_message("Este painel pertence a outro administrador.", ephemeral=True)
        return allowed

    async def refresh(self) -> "SecurityPanelView":
        self.latest = await self.center.repository.latest(self.guild.id)
        self._render()
        return self

    def _render(self) -> None:
        self.clear_items()
        counts = self.latest.get("counts", {}) if isinstance(self.latest, dict) else {}
        status = str(self.latest.get("status", "Nunca executado")) if self.latest else "Nunca executado"
        finished = str(self.latest.get("finished_at", "—")) if self.latest else "—"
        self.add_item(
            discord.ui.TextDisplay(
                "# Central de Segurança\n"
                "Auditoria somente leitura de cargos, canais, AutoMod, bots, webhooks e integrações."
            )
        )
        self.add_item(
            discord.ui.TextDisplay(
                f"**Última análise:** {finished}\n**Status:** {status}\n\n"
                f"**Críticos:** {counts.get('CRITICAL', 0)}  •  **Altos:** {counts.get('HIGH', 0)}  •  "
                f"**Médios:** {counts.get('MEDIUM', 0)}  •  **Baixos:** {counts.get('LOW', 0)}  •  "
                f"**Informativos:** {counts.get('INFO', 0)}"
            )
        )
        first = discord.ui.ActionRow()
        scan = discord.ui.Button(label="Iniciar análise", style=discord.ButtonStyle.success)
        diagnostic = discord.ui.Button(label="Diagnóstico", style=discord.ButtonStyle.primary)
        roles = discord.ui.Button(label="Cargos", style=discord.ButtonStyle.secondary)
        channels = discord.ui.Button(label="Canais", style=discord.ButtonStyle.secondary)
        scan.callback = self._scan
        diagnostic.callback = self._diagnostic
        roles.callback = lambda interaction: self._category(interaction, "roles")
        channels.callback = lambda interaction: self._category(interaction, "channels")
        for item in (scan, diagnostic, roles, channels):
            first.add_item(item)
        self.add_item(first)

        second = discord.ui.ActionRow()
        automod = discord.ui.Button(label="AutoMod", style=discord.ButtonStyle.secondary)
        bots = discord.ui.Button(label="Bots", style=discord.ButtonStyle.secondary)
        webhooks = discord.ui.Button(label="Webhooks", style=discord.ButtonStyle.secondary)
        history = discord.ui.Button(label="Histórico", style=discord.ButtonStyle.secondary)
        automod.callback = lambda interaction: self._category(interaction, "automod")
        bots.callback = lambda interaction: self._category(interaction, "bots")
        webhooks.callback = lambda interaction: self._category(interaction, "webhooks")
        history.callback = self._history
        for item in (automod, bots, webhooks, history):
            second.add_item(item)
        self.add_item(second)

        third = discord.ui.ActionRow()
        compare = discord.ui.Button(label="Comparar análises", style=discord.ButtonStyle.primary)
        configure = discord.ui.Button(label="Critérios", style=discord.ButtonStyle.secondary)
        refresh = discord.ui.Button(label="Atualizar painel", style=discord.ButtonStyle.secondary)
        compare.callback = self._compare
        configure.callback = self._criteria
        refresh.callback = self._refresh_panel
        for item in (compare, configure, refresh):
            third.add_item(item)
        self.add_item(third)

    async def _scan(self, interaction: discord.Interaction) -> None:
        try:
            job_id = self.center.start_scan(self.guild, actor_id=interaction.user.id)
        except Exception as exc:
            await interaction.response.send_message(f"Não foi possível iniciar: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Análise `{job_id}` iniciada em background. O painel será atualizado por etapa.",
            ephemeral=True,
        )

    async def _diagnostic(self, interaction: discord.Interaction) -> None:
        latest = await self.center.repository.latest(self.guild.id)
        if not latest:
            await interaction.response.send_message("Ainda não existe análise. Use **Iniciar análise**.", ephemeral=True)
            return
        findings = await self.center.repository.findings(self.guild.id, str(latest.get("scan_id")))
        await interaction.response.send_message(embed=findings_embed(findings, title="Diagnóstico de Segurança"), ephemeral=True)

    async def _category(self, interaction: discord.Interaction, category: str) -> None:
        latest = await self.center.repository.latest(self.guild.id)
        if not latest:
            await interaction.response.send_message("Ainda não existe análise de segurança.", ephemeral=True)
            return
        findings = await self.center.repository.findings(self.guild.id, str(latest.get("scan_id")))
        selected = [item for item in findings if item.category == category]
        await interaction.response.send_message(
            embed=findings_embed(selected, title=f"Segurança • {category.title()}"),
            ephemeral=True,
        )

    async def _history(self, interaction: discord.Interaction) -> None:
        scans = await self.center.repository.scans(self.guild.id, limit=10)
        await interaction.response.send_message(embed=history_embed(scans), ephemeral=True)

    async def _compare(self, interaction: discord.Interaction) -> None:
        scans = await self.center.repository.scans(self.guild.id, limit=2)
        if len(scans) < 2:
            await interaction.response.send_message("São necessárias duas análises para comparar.", ephemeral=True)
            return
        await interaction.response.send_message(embed=comparison_embed(scans[0]), ephemeral=True)

    async def _criteria(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="Critérios de severidade", color=discord.Color.from_rgb(255, 80, 120))
        for severity in Severity:
            embed.add_field(name=severity.value, value=SEVERITY_CRITERIA[severity], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _refresh_panel(self, interaction: discord.Interaction) -> None:
        await self.refresh()
        await interaction.response.edit_message(view=self)


class PersistentSecurityPanelView(discord.ui.LayoutView):
    """Painel V2 persistente; toda ação revalida o RBAC no backend."""

    def __init__(self, center: SecurityCenter, authorize: AuthorizationCallback) -> None:
        super().__init__(timeout=None)
        self.center = center
        self.authorize = authorize
        self._render(None, "")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = isinstance(interaction.user, discord.Member) and self.authorize(interaction.user)
        if not allowed:
            await interaction.response.send_message("Você não possui acesso à Central de Segurança.", ephemeral=True)
        return allowed

    async def refresh(self, guild: discord.Guild, *, active_status: str = "") -> "PersistentSecurityPanelView":
        self._render(await self.center.repository.latest(guild.id), active_status)
        return self

    def _render(self, latest: dict[str, Any] | None, active_status: str) -> None:
        self.clear_items()
        counts = latest.get("counts", {}) if isinstance(latest, dict) else {}
        status = active_status or (str(latest.get("status", "Nunca executado")) if latest else "Nunca executado")
        finished = str(latest.get("finished_at", "—")) if latest else "—"
        unavailable = latest.get("areas_unavailable", []) if isinstance(latest, dict) else []
        self.add_item(discord.ui.TextDisplay("# Central de Segurança\nAuditoria determinística e somente leitura da postura de segurança do servidor."))
        self.add_item(discord.ui.TextDisplay(
            f"**Status:** {status}\n**Última análise:** {finished}\n"
            f"**Críticos:** {counts.get('CRITICAL', 0)}  •  **Altos:** {counts.get('HIGH', 0)}  •  "
            f"**Médios:** {counts.get('MEDIUM', 0)}  •  **Baixos:** {counts.get('LOW', 0)}  •  **Info:** {counts.get('INFO', 0)}\n"
            f"**Áreas não verificadas:** {', '.join(map(str, unavailable)) if unavailable else 'nenhuma'}"
        ))
        self.add_item(discord.ui.Separator())

        rows = (discord.ui.ActionRow(), discord.ui.ActionRow(), discord.ui.ActionRow())
        buttons = (
            ("Iniciar scan", discord.ButtonStyle.success, "bella:security:scan:v2", self._scan),
            ("Diagnóstico", discord.ButtonStyle.primary, "bella:security:diagnostic:v2", self._diagnostic),
            ("Plano de hardening", discord.ButtonStyle.primary, "bella:security:hardening:v2", self._hardening),
            ("Cargos", discord.ButtonStyle.secondary, "bella:security:roles:v2", lambda i: self._category(i, "roles")),
            ("Canais", discord.ButtonStyle.secondary, "bella:security:channels:v2", lambda i: self._category(i, "channels")),
            ("Membros", discord.ButtonStyle.secondary, "bella:security:members:v2", lambda i: self._category(i, "members")),
            ("Bots", discord.ButtonStyle.secondary, "bella:security:bots:v2", lambda i: self._category(i, "bots")),
            ("AutoMod", discord.ButtonStyle.secondary, "bella:security:automod:v2", lambda i: self._category(i, "automod")),
            ("Webhooks", discord.ButtonStyle.secondary, "bella:security:webhooks:v2", lambda i: self._category(i, "webhooks")),
            ("Histórico", discord.ButtonStyle.secondary, "bella:security:history:v2", self._history),
            ("Comparar", discord.ButtonStyle.secondary, "bella:security:compare:v2", self._compare),
            ("Atualizar", discord.ButtonStyle.secondary, "bella:security:refresh:v2", self._refresh),
        )
        for index, (label, style, custom_id, callback) in enumerate(buttons):
            button = discord.ui.Button(label=label, style=style, custom_id=custom_id)
            button.callback = callback
            rows[min(index // 5, 2)].add_item(button)
        for row in rows:
            self.add_item(row)

    async def _guild(self, interaction: discord.Interaction) -> discord.Guild | None:
        if interaction.guild is None:
            await interaction.response.send_message("Use este painel dentro do servidor.", ephemeral=True)
            return None
        return interaction.guild

    async def _scan(self, interaction: discord.Interaction) -> None:
        guild = await self._guild(interaction)
        if guild is None:
            return
        try:
            job_id = self.center.start_scan(guild, actor_id=interaction.user.id)
        except RuntimeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Scan `{job_id}` iniciado em background. O painel exibirá o progresso.", ephemeral=True
        )

    async def _diagnostic(self, interaction: discord.Interaction) -> None:
        guild = await self._guild(interaction)
        if guild is None:
            return
        latest = await self.center.repository.latest(guild.id)
        if not latest:
            await interaction.response.send_message("Ainda não existe análise.", ephemeral=True)
            return
        findings = await self.center.repository.findings(guild.id, str(latest.get("scan_id")))
        view = SecurityFindingReviewView(self.center, guild.id, str(latest.get("scan_id")), findings, self.authorize)
        await interaction.response.send_message(embed=findings_embed(findings, title="Diagnóstico de Segurança"), view=view, ephemeral=True)

    async def _category(self, interaction: discord.Interaction, category: str) -> None:
        guild = await self._guild(interaction)
        if guild is None:
            return
        latest = await self.center.repository.latest(guild.id)
        if not latest:
            await interaction.response.send_message("Ainda não existe análise.", ephemeral=True)
            return
        scan_id = str(latest.get("scan_id"))
        findings = [item for item in await self.center.repository.findings(guild.id, scan_id) if item.category == category]
        view = SecurityFindingReviewView(self.center, guild.id, scan_id, findings, self.authorize)
        await interaction.response.send_message(embed=findings_embed(findings, title=f"Segurança • {category.title()}"), view=view, ephemeral=True)

    async def _hardening(self, interaction: discord.Interaction) -> None:
        guild = await self._guild(interaction)
        if guild is None:
            return
        latest = await self.center.repository.latest(guild.id)
        if not latest:
            await interaction.response.send_message("Execute um scan antes de gerar o plano.", ephemeral=True)
            return
        plan = await self.center.hardening_plan(guild.id, str(latest.get("scan_id")))
        await interaction.response.send_message(embed=hardening_embed(plan), ephemeral=True)

    async def _history(self, interaction: discord.Interaction) -> None:
        guild = await self._guild(interaction)
        if guild is not None:
            await interaction.response.send_message(embed=history_embed(await self.center.repository.scans(guild.id, limit=10)), ephemeral=True)

    async def _compare(self, interaction: discord.Interaction) -> None:
        guild = await self._guild(interaction)
        if guild is None:
            return
        scans = await self.center.repository.scans(guild.id, limit=2)
        if len(scans) < 2:
            await interaction.response.send_message("São necessários dois scans.", ephemeral=True)
            return
        await interaction.response.send_message(embed=comparison_embed(scans[0]), ephemeral=True)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        guild = await self._guild(interaction)
        if guild is None:
            return
        await self.refresh(guild)
        await interaction.response.edit_message(view=self)


class SecurityFindingReviewView(discord.ui.View):
    def __init__(self, center: SecurityCenter, guild_id: int, scan_id: str, findings: list[SecurityFinding], authorize: AuthorizationCallback) -> None:
        super().__init__(timeout=900)
        self.center = center
        self.guild_id = guild_id
        self.scan_id = scan_id
        self.findings = {item.id: item for item in findings}
        self.authorize = authorize
        self.selected_id = next(iter(self.findings), "")
        if findings:
            select = discord.ui.Select(
                placeholder="Selecione um finding para revisar",
                options=[discord.SelectOption(label=item.title[:100], value=item.id, description=f"{item.kind.value} • {item.severity.value}"[:100]) for item in findings[:25]],
            )
            select.callback = self._select
            self.add_item(select)
        for label, style, callback in (
            ("Marcar como intencional", discord.ButtonStyle.success, self._intentional),
            ("Ignorar uma vez", discord.ButtonStyle.secondary, self._ignore_once),
            ("Restaurar alerta", discord.ButtonStyle.danger, self._restore),
            ("Perguntar à Bella", discord.ButtonStyle.primary, self._ask),
        ):
            button = discord.ui.Button(label=label, style=style, disabled=not bool(findings))
            button.callback = callback
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = isinstance(interaction.user, discord.Member) and self.authorize(interaction.user)
        if not allowed:
            await interaction.response.send_message("Você não possui acesso a este finding.", ephemeral=True)
        return allowed

    def selected(self) -> SecurityFinding | None:
        return self.findings.get(self.selected_id)

    async def _select(self, interaction: discord.Interaction) -> None:
        select = next((item for item in self.children if isinstance(item, discord.ui.Select)), None)
        if select and select.values:
            self.selected_id = select.values[0]
        finding = self.selected()
        await interaction.response.edit_message(embed=finding_detail_embed(finding) if finding else None, view=self)

    async def _intentional(self, interaction: discord.Interaction) -> None:
        finding = self.selected()
        if finding:
            await interaction.response.send_modal(SecurityExceptionModal(self, finding, "INTENTIONAL"))

    async def _ignore_once(self, interaction: discord.Interaction) -> None:
        finding = self.selected()
        if finding:
            await self.center.repository.set_exception(self.guild_id, finding, actor_id=interaction.user.id, mode="IGNORE_ONCE", scan_id=self.scan_id)
            await self.center.repository.update_finding_status(self.guild_id, self.scan_id, finding.id, "IGNORED_ONCE")
            await interaction.response.send_message("Finding ignorado somente neste scan.", ephemeral=True)

    async def _restore(self, interaction: discord.Interaction) -> None:
        finding = self.selected()
        if finding:
            await self.center.repository.remove_exception(self.guild_id, finding.id)
            await self.center.repository.update_finding_status(self.guild_id, self.scan_id, finding.id, "OPEN")
            await interaction.response.send_message("Alerta restaurado para os próximos scans.", ephemeral=True)

    async def _ask(self, interaction: discord.Interaction) -> None:
        finding = self.selected()
        if finding:
            await interaction.response.send_modal(SecurityQuestionModal(self.center, finding))


class SecurityExceptionModal(discord.ui.Modal, title="Exceção de segurança"):
    reason = discord.ui.TextInput(label="Motivo opcional", style=discord.TextStyle.paragraph, required=False, max_length=500)

    def __init__(self, view: SecurityFindingReviewView, finding: SecurityFinding, mode: str) -> None:
        super().__init__()
        self.review = view
        self.finding = finding
        self.mode = mode

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.review.center.repository.set_exception(self.review.guild_id, self.finding, actor_id=interaction.user.id, mode=self.mode, reason=str(self.reason), scan_id=self.review.scan_id)
        await self.review.center.repository.update_finding_status(self.review.guild_id, self.review.scan_id, self.finding.id, "INTENTIONAL")
        if interaction.guild and self.review.center.logs:
            await self.review.center.logs.send_event(interaction.guild, "security", title="Configuração marcada como intencional", description=f"Regra `{self.finding.rule_id}` no recurso `{self.finding.resource_type}:{self.finding.resource_id}`.", severity="INFO")
        await interaction.response.send_message("Configuração marcada como intencional enquanto o fingerprint não mudar.", ephemeral=True)


class SecurityQuestionModal(discord.ui.Modal, title="Perguntar à Bella"):
    question = discord.ui.TextInput(label="Sua pergunta", style=discord.TextStyle.paragraph, max_length=800)

    def __init__(self, center: SecurityCenter, finding: SecurityFinding) -> None:
        super().__init__()
        self.center = center
        self.finding = finding

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            answer = await self.center.explain_finding(self.finding, str(self.question))
        except Exception as exc:
            answer = f"Não consegui consultar a IA agora: {type(exc).__name__}: {exc}"
        await interaction.followup.send(answer, ephemeral=True)


def scan_result_embed(result: SecurityScanResult) -> discord.Embed:
    counts = result.counts
    embed = discord.Embed(
        title="Análise de segurança concluída",
        description=(
            f"Scan `{result.scan_id}` finalizado como **{result.status.value}**. "
            f"Foram detectadas **{len(result.changes)}** mudanças desde o scan anterior."
        ),
        color=_severity_color(Severity.CRITICAL if counts["CRITICAL"] else Severity.HIGH if counts["HIGH"] else Severity.INFO),
        timestamp=datetime.now(timezone.utc),
    )
    for severity in Severity:
        embed.add_field(name=severity.value, value=str(counts[severity.value]), inline=True)
    if result.source_errors:
        embed.add_field(name="Coleta parcial", value="\n".join(result.source_errors)[:1024], inline=False)
    return embed


def findings_embed(findings: Iterable[SecurityFinding], *, title: str) -> discord.Embed:
    values = list(findings)
    embed = discord.Embed(
        title=title,
        description=(
            "\n\n".join(
                f"**{item.kind.value} • {item.severity.value} • {item.title}**\n"
                f"{item.evidence}\n**Recomendação:** {item.recommendation}"
                f"\n**Estado:** {item.status}"
                for item in values[:8]
            )[:4000]
            or "Nenhum finding nessa área."
        ),
        color=_severity_color(values[0].severity if values else Severity.INFO),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"{len(values)} finding(s) • exibindo até 8")
    return embed


def finding_detail_embed(finding: SecurityFinding | None) -> discord.Embed:
    if finding is None:
        return discord.Embed(title="Finding não encontrado", color=discord.Color.red())
    embed = discord.Embed(
        title=f"{finding.kind.value} • {finding.title}",
        description=finding.description,
        color=_severity_color(finding.severity),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Severidade", value=finding.severity.value, inline=True)
    embed.add_field(name="Estado", value=finding.status, inline=True)
    embed.add_field(name="Recurso", value=f"`{finding.resource_type}:{finding.resource_id}`\n{finding.resource_name}"[:1024], inline=False)
    embed.add_field(name="Evidência", value=finding.evidence[:1024], inline=False)
    embed.add_field(name="Critério", value=finding.reason[:1024], inline=False)
    embed.add_field(name="Recomendação", value=finding.recommendation[:1024], inline=False)
    embed.set_footer(text=f"Regra {finding.rule_id} • fingerprint {finding.fingerprint[:12]}")
    return embed


def hardening_embed(plan: dict[str, list[dict[str, str]]]) -> discord.Embed:
    embed = discord.Embed(
        title="Plano de Hardening",
        description="Plano somente leitura. Nenhuma alteração administrativa é executada automaticamente.",
        color=discord.Color.from_rgb(210, 153, 34),
        timestamp=datetime.now(timezone.utc),
    )
    if not plan:
        embed.description = "Nenhuma ação aberta para o plano atual."
        return embed
    for priority, items in plan.items():
        lines = [f"**{item['problem']}** — {item['recommendation']} (`{item['resource']}`)" for item in items[:6]]
        embed.add_field(name=priority, value="\n".join(lines)[:1024] or "—", inline=False)
    return embed


def history_embed(scans: Iterable[dict[str, Any]]) -> discord.Embed:
    values = list(scans)
    lines = []
    for item in values:
        counts = item.get("counts", {}) if isinstance(item.get("counts"), dict) else {}
        lines.append(
            f"`{item.get('scan_id', '?')}` • {item.get('status', '?')} • "
            f"C:{counts.get('CRITICAL', 0)} A:{counts.get('HIGH', 0)} M:{counts.get('MEDIUM', 0)} • "
            f"{item.get('finished_at', '—')}"
        )
    return discord.Embed(
        title="Histórico de análises",
        description="\n".join(lines)[:4000] or "Nenhuma análise salva.",
        color=discord.Color.from_rgb(24, 119, 242),
    )


def comparison_embed(scan: dict[str, Any]) -> discord.Embed:
    changes = scan.get("changes", []) if isinstance(scan.get("changes"), list) else []
    finding_diff = scan.get("finding_diff", {}) if isinstance(scan.get("finding_diff"), dict) else {}
    lines = [
        f"**{item.get('change_type', '?')}** `{item.get('resource_key', '?')}`"
        for item in changes[:30]
        if isinstance(item, dict)
    ]
    embed = discord.Embed(
        title="Comparação com a análise anterior",
        description="\n".join(lines)[:4000] or "Nenhuma mudança estrutural de segurança detectada.",
        color=discord.Color.from_rgb(210, 153, 34) if lines else discord.Color.from_rgb(46, 160, 67),
    )
    embed.add_field(name="Novos problemas", value=str(len(finding_diff.get("new", []))), inline=True)
    embed.add_field(name="Problemas resolvidos", value=str(len(finding_diff.get("resolved", []))), inline=True)
    embed.add_field(name="Novas recomendações", value=str(len(finding_diff.get("new_recommendations", []))), inline=True)
    return embed


def _analyze_roles(snapshot: dict[str, Any], admin_role_ids: set[int]) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    guild_id = int(snapshot.get("guild", {}).get("id", 0) or 0)
    for role in snapshot.get("roles", []):
        if not isinstance(role, dict):
            continue
        permissions = set(role.get("sensitive_permissions", []))
        if not permissions:
            continue
        role_id = int(role.get("id", 0) or 0)
        name = str(role.get("name", "cargo"))
        members = int(role.get("member_count", 0) or 0)
        managed = bool(role.get("managed"))
        is_everyone = role_id == guild_id
        authorized = role_id in admin_role_ids
        if "administrator" in permissions:
            if is_everyone:
                severity = Severity.CRITICAL
            elif managed:
                severity = Severity.MEDIUM
            elif not authorized and members >= 5:
                severity = Severity.HIGH
            else:
                severity = Severity.MEDIUM
            findings.append(
                _finding(
                    "role-administrator",
                    "roles",
                    severity,
                    "Cargo com permissão Administrator",
                    "Administrator concede controle amplo e ignora overwrites específicos de canais.",
                    f"Cargo: {name} | membros afetados: {members} | gerenciado: {managed}",
                    "role",
                    role_id,
                    name,
                    "O alcance depende do número de membros, se o cargo é gerenciado e se faz parte do RBAC administrativo esperado.",
                    "Confirme se o cargo realmente precisa de Administrator; prefira permissões específicas.",
                )
            )
            continue
        control = sorted(permissions & CONTROL_PLANE_PERMISSIONS)
        if len(control) >= 2:
            severity = Severity.HIGH if members >= 5 and not authorized else Severity.MEDIUM
            findings.append(
                _finding(
                    "role-control-plane-combination",
                    "roles",
                    severity,
                    "Combinação de permissões administrativas",
                    "A combinação permite alterar a estrutura, cargos, integrações ou webhooks sem Administrator.",
                    f"Cargo: {name} | permissões: {', '.join(control)} | membros: {members}",
                    "role",
                    role_id,
                    name,
                    "Combinações de permissões de controle podem resultar em escalonamento indireto.",
                    "Separe responsabilidades e remova permissões que não sejam necessárias ao cargo.",
                )
            )
        elif "mention_everyone" in permissions and members >= 10 and not authorized:
            findings.append(
                _finding(
                    "role-mention-everyone",
                    "roles",
                    Severity.MEDIUM,
                    "Cargo amplo pode mencionar everyone/here",
                    "Menções globais podem causar spam, engenharia social ou alertas indevidos.",
                    f"Cargo: {name} | membros afetados: {members}",
                    "role",
                    role_id,
                    name,
                    "O risco aumenta conforme a quantidade de membros e a ausência de necessidade operacional.",
                    "Restrinja Mention Everyone aos cargos administrativos necessários.",
                )
            )
    return findings


def _analyze_channels(snapshot: dict[str, Any]) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    role_members = {int(item.get("id", 0)): int(item.get("member_count", 0)) for item in snapshot.get("roles", []) if isinstance(item, dict)}
    for channel in snapshot.get("channels", []):
        if not isinstance(channel, dict):
            continue
        channel_id = int(channel.get("id", 0) or 0)
        name = str(channel.get("name", "canal"))
        normalized = name.casefold().replace("-", " ").replace("_", " ")
        admin_like = any(term in normalized for term in ADMIN_CHANNEL_TERMS)
        if admin_like and channel.get("public_view"):
            findings.append(
                _finding(
                    "channel-admin-public",
                    "channels",
                    Severity.HIGH,
                    "Canal administrativo potencialmente público",
                    "O nome sugere conteúdo administrativo, mas @everyone consegue visualizar o canal.",
                    f"Canal: #{name} | tipo: {channel.get('type')}",
                    "channel",
                    channel_id,
                    name,
                    "Logs, backups e auditorias podem conter informações operacionais sensíveis.",
                    "Revise o overwrite de @everyone e conceda acesso apenas aos cargos necessários.",
                )
            )
        if channel.get("category_id") and not channel.get("permissions_synced", True):
            findings.append(
                _finding(
                    "channel-unsynced-permissions",
                    "channels",
                    Severity.LOW,
                    "Permissões não sincronizadas com a categoria",
                    "O canal possui overwrites próprios diferentes da categoria.",
                    f"Canal: #{name} | categoria: {channel.get('category_id')}",
                    "channel",
                    channel_id,
                    name,
                    "Isso pode ser intencional, mas também pode manter acessos antigos após mudanças na categoria.",
                    "Compare os overwrites com a categoria e sincronize se a exceção não for necessária.",
                )
            )
        for overwrite in channel.get("overwrites", []):
            if not isinstance(overwrite, dict):
                continue
            sensitive = set(overwrite.get("sensitive_allow", []))
            if not sensitive:
                continue
            target_type = str(overwrite.get("target_type", "unknown"))
            target_id = int(overwrite.get("target_id", 0) or 0)
            members = role_members.get(target_id, 1)
            severity = Severity.HIGH if sensitive & CONTROL_PLANE_PERMISSIONS else Severity.MEDIUM
            if target_type == "role" and members <= 2 and severity == Severity.HIGH:
                severity = Severity.MEDIUM
            findings.append(
                _finding(
                    "channel-sensitive-overwrite",
                    "channels",
                    severity,
                    "Overwrite concede permissão sensível",
                    "Um overwrite individual ou de cargo libera ações administrativas neste canal.",
                    f"Canal: #{name} | alvo: {target_type}:{target_id} | permissões: {', '.join(sorted(sensitive))}",
                    "channel",
                    channel_id,
                    name,
                    "Overwrites individuais são fáceis de esquecer e permissões de controle podem ampliar o impacto de uma conta comprometida.",
                    "Confirme o alvo e prefira cargos administrativos bem definidos em vez de usuários individuais.",
                )
            )
    return findings


def _analyze_automod(snapshot: dict[str, Any]) -> list[SecurityFinding]:
    rules = [item for item in snapshot.get("automod", []) if isinstance(item, dict)]
    errors = set(snapshot.get("source_errors", []))
    if any(str(item).startswith("automod:") for item in errors):
        return [
            _finding(
                "automod-unavailable",
                "automod",
                Severity.INFO,
                "AutoMod não pôde ser analisado",
                "A API oficial não retornou as regras para esta análise.",
                "Permissão Manage Guild pode estar ausente ou a API respondeu com erro.",
                "guild",
                snapshot.get("guild", {}).get("id", 0),
                snapshot.get("guild", {}).get("name", "servidor"),
                "Sem leitura confiável não é possível afirmar se as regras estão corretas.",
                "Conceda Gerenciar Servidor à Bella apenas se desejar auditar o AutoMod.",
            )
        ]
    if not rules:
        return [
            _finding(
                "automod-no-rules",
                "automod",
                Severity.MEDIUM,
                "Servidor sem regras nativas de AutoMod",
                "Nenhuma regra de AutoMod foi retornada pela API oficial.",
                "Regras encontradas: 0",
                "guild",
                snapshot.get("guild", {}).get("id", 0),
                snapshot.get("guild", {}).get("name", "servidor"),
                "A ausência de AutoMod aumenta dependência de moderação manual e bots.",
                "Avalie regras para spam, menções em massa e conteúdo incompatível com a comunidade.",
            )
        ]
    findings: list[SecurityFinding] = []
    for rule in rules:
        if not rule.get("enabled", False):
            findings.append(
                _finding(
                    "automod-disabled-rule",
                    "automod",
                    Severity.LOW,
                    "Regra de AutoMod desativada",
                    "A regra existe, mas não está protegendo o servidor no momento.",
                    f"Regra: {rule.get('name')} | trigger: {rule.get('trigger_type')}",
                    "automod_rule",
                    rule.get("id", 0),
                    str(rule.get("name", "regra")),
                    "Regras pausadas podem ser intencionais durante manutenção.",
                    "Confirme se a desativação ainda é necessária.",
                )
            )
        exemptions = len(rule.get("exempt_role_ids", [])) + len(rule.get("exempt_channel_ids", []))
        if exemptions >= 10:
            findings.append(
                _finding(
                    "automod-many-exemptions",
                    "automod",
                    Severity.MEDIUM,
                    "Regra de AutoMod com muitas exceções",
                    "Muitas exceções podem reduzir bastante a cobertura real da regra.",
                    f"Regra: {rule.get('name')} | exceções: {exemptions}",
                    "automod_rule",
                    rule.get("id", 0),
                    str(rule.get("name", "regra")),
                    "A severidade considera quantidade, não assume que cada exceção seja incorreta.",
                    "Revise cargos e canais liberados e remova exceções antigas.",
                )
            )
    return findings


def _analyze_bots(snapshot: dict[str, Any], bella_user_id: int) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    for bot in snapshot.get("bots", []):
        if not isinstance(bot, dict):
            continue
        permissions = set(bot.get("sensitive_permissions", []))
        if "administrator" not in permissions:
            continue
        bot_id = int(bot.get("id", 0) or 0)
        is_bella = bot_id == bella_user_id
        findings.append(
            _finding(
                "bot-administrator",
                "bots",
                Severity.LOW if is_bella else Severity.HIGH,
                "Bot com permissão Administrator",
                "Bots com Administrator têm acesso amplo caso a aplicação ou token seja comprometido.",
                f"Bot: {bot.get('name')} | cargos: {', '.join(bot.get('role_names', []))}",
                "bot",
                bot_id,
                str(bot.get("name", "bot")),
                "A Bella recebe severidade menor para não tratar a própria operação esperada como invasão, mas a permissão continua sensível.",
                "Prefira permissões mínimas e mantenha o cargo do bot abaixo dos cargos humanos críticos.",
            )
        )
    return findings


def _analyze_webhooks(snapshot: dict[str, Any]) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    by_channel = Counter(int(item.get("channel_id", 0) or 0) for item in snapshot.get("webhooks", []) if isinstance(item, dict))
    for webhook in snapshot.get("webhooks", []):
        if not isinstance(webhook, dict):
            continue
        if webhook.get("user_id") is None:
            findings.append(
                _finding(
                    "webhook-unowned",
                    "webhooks",
                    Severity.LOW,
                    "Webhook sem criador disponível",
                    "A API não retornou um usuário criador para este webhook.",
                    f"Webhook: {webhook.get('name')} | canal: {webhook.get('channel_id')} | tipo: {webhook.get('type')}",
                    "webhook",
                    webhook.get("id", 0),
                    str(webhook.get("name", "webhook")),
                    "Isso é comum em webhooks de aplicação, mas merece inventário.",
                    "Confirme se o webhook pertence a uma integração ativa. Tokens nunca são coletados pelo scanner.",
                )
            )
    for channel_id, count in by_channel.items():
        if channel_id and count >= 6:
            findings.append(
                _finding(
                    "webhook-channel-volume",
                    "webhooks",
                    Severity.MEDIUM,
                    "Muitos webhooks no mesmo canal",
                    "Uma concentração alta aumenta a superfície de publicação externa.",
                    f"Canal: {channel_id} | webhooks: {count}",
                    "channel",
                    channel_id,
                    str(channel_id),
                    "O volume não prova abuso, mas dificulta inventário e revogação rápida.",
                    "Remova webhooks antigos e documente o responsável por cada integração.",
                )
            )
    return findings


def _analyze_integrations(snapshot: dict[str, Any]) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    role_map = {int(item.get("id", 0)): item for item in snapshot.get("roles", []) if isinstance(item, dict)}
    for integration in snapshot.get("integrations", []):
        if not isinstance(integration, dict):
            continue
        role = role_map.get(int(integration.get("role_id", 0) or 0))
        if role and "administrator" in set(role.get("sensitive_permissions", [])):
            findings.append(
                _finding(
                    "integration-admin-role",
                    "integrations",
                    Severity.MEDIUM,
                    "Integração vinculada a cargo Administrator",
                    "A integração possui um cargo gerenciado com controle administrativo amplo.",
                    f"Integração: {integration.get('name')} | cargo: {role.get('name')}",
                    "integration",
                    integration.get("id", 0),
                    str(integration.get("name", "integração")),
                    "Cargos gerenciados podem ser esperados, mas ainda ampliam o impacto de comprometimento da integração.",
                    "Revise permissões concedidas durante a instalação da integração.",
                )
            )
    return findings


def _serialize_role(role: discord.Role) -> dict[str, Any]:
    sensitive = [name for name in SENSITIVE_PERMISSIONS if getattr(role.permissions, name, False)]
    return {
        "id": role.id,
        "name": role.name,
        "position": role.position,
        "permissions": role.permissions.value,
        "sensitive_permissions": sensitive,
        "managed": role.managed,
        "bot_managed": role.tags is not None and getattr(role.tags, "bot_id", None) is not None,
        "integration_managed": role.tags is not None and getattr(role.tags, "integration_id", None) is not None,
        "member_count": len(role.members),
    }


def _serialize_channel(channel: discord.abc.GuildChannel, guild: discord.Guild) -> dict[str, Any]:
    overwrites: list[dict[str, Any]] = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        overwrites.append(
            {
                "target_id": target.id,
                "target_type": "role" if isinstance(target, discord.Role) else "member",
                "allow": allow.value,
                "deny": deny.value,
                "sensitive_allow": [name for name in SENSITIVE_PERMISSIONS if getattr(allow, name, False)],
            }
        )
    return {
        "id": channel.id,
        "name": channel.name,
        "type": str(channel.type),
        "position": channel.position,
        "category_id": channel.category_id,
        "permissions_synced": getattr(channel, "permissions_synced", True),
        "public_view": channel.permissions_for(guild.default_role).view_channel,
        "overwrites": sorted(overwrites, key=lambda item: (item["target_type"], item["target_id"])),
    }


def _serialize_bot(member: discord.Member) -> dict[str, Any]:
    permissions = member.guild_permissions
    return {
        "id": member.id,
        "name": member.display_name,
        "role_ids": sorted(role.id for role in member.roles),
        "role_names": [role.name for role in sorted(member.roles, key=lambda item: item.position, reverse=True)],
        "top_role_position": member.top_role.position,
        "sensitive_permissions": [name for name in SENSITIVE_PERMISSIONS if getattr(permissions, name, False)],
    }


def _serialize_automod_rule(rule: discord.AutoModRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "name": rule.name,
        "enabled": rule.enabled,
        "event_type": int(rule.event_type.value),
        "trigger_type": int(rule.trigger.type.value) if rule.trigger.type else None,
        "trigger_metadata": rule.trigger.to_metadata_dict(),
        "actions": [action.to_dict() for action in rule.actions],
        "exempt_role_ids": sorted(int(item) for item in rule.exempt_role_ids),
        "exempt_channel_ids": sorted(int(item) for item in rule.exempt_channel_ids),
    }


def _serialize_webhook(webhook: discord.Webhook) -> dict[str, Any]:
    return {
        "id": webhook.id,
        "name": webhook.name,
        "type": int(webhook.type.value),
        "channel_id": webhook.channel_id,
        "guild_id": webhook.guild_id,
        "user_id": webhook.user.id if webhook.user else None,
        "application_id": webhook.application_id,
    }


def _serialize_integration(integration: discord.Integration) -> dict[str, Any]:
    role = getattr(integration, "role", None)
    account = getattr(integration, "account", None)
    return {
        "id": integration.id,
        "name": integration.name,
        "type": integration.type,
        "enabled": getattr(integration, "enabled", True),
        "role_id": role.id if role else None,
        "account_id": getattr(account, "id", None),
        "application_id": getattr(getattr(integration, "application", None), "id", None),
    }


def _security_projection(snapshot: dict[str, Any]) -> dict[str, Any]:
    projection: dict[str, Any] = {}
    for key in ("roles", "channels", "automod", "bots", "webhooks", "integrations"):
        for item in snapshot.get(key, []):
            if not isinstance(item, dict):
                continue
            stable = {name: value for name, value in item.items() if name not in {"created_at", "updated_at"}}
            projection[f"{key}:{item.get('id')}"] = _normalize_value(stable)
    projection["guild:settings"] = _normalize_value(snapshot.get("guild", {}))
    return projection


def _normalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list):
        normalized = [_normalize_value(item) for item in value]
        if all(isinstance(item, (str, int, float, bool, type(None))) for item in normalized):
            return sorted(normalized, key=str)
        return normalized
    return value


def _convert_rule_finding(item: Any) -> SecurityFinding:
    stable = f"{item.rule_id}:{item.resource_type}:{item.resource_id}"
    finding_id = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]
    fingerprint_payload = json.dumps(
        {
            "rule_id": item.rule_id,
            "resource_type": item.resource_type,
            "resource_id": str(item.resource_id),
            "evidence": item.evidence,
            "recommendation": item.recommendation,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()
    return SecurityFinding(
        id=finding_id,
        rule_id=item.rule_id,
        category=item.category,
        severity=Severity(item.severity),
        title=item.title,
        description=item.description,
        evidence=item.evidence,
        resource_type=item.resource_type,
        resource_id=str(item.resource_id),
        resource_name=item.resource_name,
        reason=item.reason,
        recommendation=item.recommendation,
        timestamp=datetime.now(timezone.utc).isoformat(),
        kind=FindingKind(item.kind),
        fingerprint=fingerprint,
    )


def _finding(
    rule_id: str,
    category: str,
    severity: Severity,
    title: str,
    description: str,
    evidence: str,
    resource_type: str,
    resource_id: Any,
    resource_name: str,
    reason: str,
    recommendation: str,
) -> SecurityFinding:
    stable = f"{rule_id}:{resource_type}:{resource_id}"
    finding_id = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]
    return SecurityFinding(
        id=finding_id,
        rule_id=rule_id,
        category=category,
        severity=severity,
        title=title,
        description=description,
        evidence=evidence,
        resource_type=resource_type,
        resource_id=str(resource_id),
        resource_name=resource_name,
        reason=reason,
        recommendation=recommendation,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _severity_rank(severity: Severity) -> int:
    return {
        Severity.CRITICAL: 0,
        Severity.HIGH: 1,
        Severity.MEDIUM: 2,
        Severity.LOW: 3,
        Severity.INFO: 4,
    }[severity]


def _severity_color(severity: Severity) -> discord.Color:
    return {
        Severity.CRITICAL: discord.Color.from_rgb(150, 0, 24),
        Severity.HIGH: discord.Color.from_rgb(218, 54, 51),
        Severity.MEDIUM: discord.Color.from_rgb(210, 153, 34),
        Severity.LOW: discord.Color.from_rgb(24, 119, 242),
        Severity.INFO: discord.Color.from_rgb(110, 118, 129),
    }[severity]


def _scan_key(guild_id: int, scan_id: str) -> str:
    return f"{guild_id}:{scan_id}"


def _job_payload(
    job_id: str,
    guild_id: int,
    actor_id: int,
    status: JobStatus,
    *,
    started_at: str,
    finished_at: str = "",
    details: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "guild_id": guild_id,
        "actor_id": actor_id,
        "status": status.value,
        "started_at": started_at,
        "finished_at": finished_at,
        "details": details or {},
        "error": error[:1000],
    }
