from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from .changes import ChangeScanReport
from .metrics import WeeklyMetricsSnapshot
from .monitor import MonitorReport
from .state import StateStore

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class OperationalEvent:
    event_type: str
    title: str
    severity: str
    scope: str
    occurred_at: str
    source_url: str = ""
    status: str = "detected"
    facts: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)
    history: dict[str, Any] = field(default_factory=dict)
    technical: dict[str, Any] = field(default_factory=dict)
    correlation_key: str = ""
    event_id: str = ""

    def __post_init__(self) -> None:
        if self.event_id:
            return
        identity = json.dumps(
            {
                "event_type": self.event_type,
                "scope": self.scope,
                "occurred_at": self.occurred_at,
                "correlation_key": self.correlation_key,
                "facts": self.facts,
                "changes": self.changes,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        object.__setattr__(
            self,
            "event_id",
            hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
        )

    @property
    def domain(self) -> str:
        return (
            urlparse(self.source_url).hostname
            or str(self.facts.get("domain", ""))
            or "Plataforma monitorada"
        ).removeprefix("www.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OperationalAnalysis:
    executive_summary: str
    what_happened: str
    impact: str
    relevance: str
    severity: str
    likely_causes: tuple[str, ...]
    recommendations: tuple[str, ...]
    trend: str
    conclusion: str
    key_indicators: tuple[str, ...] = ()
    attention_points: tuple[str, ...] = ()
    sentiment: str = "not_applicable"
    emotional_tone: str = "not_applicable"
    praise: tuple[str, ...] = ()
    criticism: tuple[str, ...] = ()
    satisfaction_score: int | None = None
    confidence: float = 0.0
    generated_by: str = "local_rules"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OperationalAnalysis":
        return cls(
            executive_summary=_public_text(data.get("executive_summary", "")),
            what_happened=_public_text(data.get("what_happened", "")),
            impact=_public_text(data.get("impact", "")),
            relevance=_public_text(data.get("relevance", "")),
            severity=str(data.get("severity", "informational")),
            likely_causes=tuple(
                _public_text(item) for item in data.get("likely_causes", [])
            ),
            recommendations=tuple(
                _public_text(item) for item in data.get("recommendations", [])
            ),
            trend=_public_text(data.get("trend", "")),
            conclusion=_public_text(data.get("conclusion", "")),
            key_indicators=tuple(
                _public_text(item) for item in data.get("key_indicators", [])
            ),
            attention_points=tuple(
                _public_text(item) for item in data.get("attention_points", [])
            ),
            sentiment=str(data.get("sentiment", "not_applicable")),
            emotional_tone=_public_text(
                data.get("emotional_tone", "not_applicable")
            ),
            praise=tuple(_public_text(item) for item in data.get("praise", [])),
            criticism=tuple(
                _public_text(item) for item in data.get("criticism", [])
            ),
            satisfaction_score=_optional_score(data.get("satisfaction_score")),
            confidence=_confidence(data.get("confidence")),
            generated_by=str(data.get("generated_by", "local_rules")),
        )


@dataclass(frozen=True)
class PlatformScore:
    overall: int
    intelligence: int
    availability: int
    performance: int
    monitoring: int
    content: int
    seo: int
    feedbacks: int
    security: int
    stability: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class OperationalIntelligence:
    def __init__(
        self,
        state: StateStore,
        *,
        max_response_chars: int = 1800,
    ) -> None:
        self.state = state
        self.max_response_chars = max_response_chars

    async def analyze_event(
        self,
        event: OperationalEvent,
    ) -> OperationalAnalysis:
        cached = await self.state.get_section("operational_analysis_cache")
        cached_value = cached.get(event.event_id)
        if (
            isinstance(cached_value, dict)
            and cached_value.get("generated_by") == "local_rules"
        ):
            analysis = OperationalAnalysis.from_dict(cached_value)
            await self._record(event, analysis)
            return analysis
        analysis = _local_evidence_analysis(event)
        await self._cache_analysis(event, analysis)
        await self._record(event, analysis)
        return analysis

    async def _cache_analysis(
        self,
        event: OperationalEvent,
        analysis: OperationalAnalysis,
    ) -> None:
        def mutate(cache: dict[str, Any]) -> None:
            cache[event.event_id] = {
                **analysis.to_dict(),
                "_cached_at": datetime.now(timezone.utc).isoformat(),
            }
            while len(cache) > 1000:
                oldest = min(
                    cache,
                    key=lambda key: str(cache[key].get("_cached_at", ""))
                    if isinstance(cache[key], dict)
                    else "",
                )
                del cache[oldest]

        await self.state.mutate_section("operational_analysis_cache", mutate)

    async def analyze_report(
        self,
        period_name: str,
        context: dict[str, Any],
    ) -> OperationalAnalysis:
        now = datetime.now(timezone.utc).isoformat()
        recent_events = await self.recent_events(days=90, limit=120)
        event = OperationalEvent(
            event_type=f"{period_name}_report",
            title=f"Relatório {period_name}",
            severity="informational",
            scope="platform",
            occurred_at=now,
            source_url=str(context.get("site_url", "")),
            facts={"period": period_name},
            metrics=context,
            history={
                "recent_events": [
                    _compact_event_record(item)
                    for item in recent_events[-60:]
                ],
            },
            correlation_key=f"report:{period_name}",
        )
        return await self.analyze_event(event)

    async def recent_events(
        self,
        *,
        days: int,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        history = await self.state.get_section("operational_events")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        selected = []
        for item in history.values():
            if not isinstance(item, dict):
                continue
            occurred_at = _parse_time(str(item.get("occurred_at", "")))
            if occurred_at < cutoff:
                continue
            selected.append(item)
        selected.sort(key=lambda item: str(item.get("occurred_at", "")))
        return selected[-limit:]

    async def health_score(
        self,
        metrics: WeeklyMetricsSnapshot,
        site: MonitorReport | None,
        changes: ChangeScanReport | None,
        *,
        active_incidents: int,
    ) -> PlatformScore:
        availability = _score(metrics.availability_percent)
        performance = _performance_score(metrics)
        monitoring = _monitoring_score(metrics)
        content = _content_score(changes)
        seo = await self._seo_score()
        feedbacks = await self._feedback_score()
        security = _security_score(site)
        stability = _stability_score(
            availability,
            performance,
            active_incidents,
            metrics.errors_detected,
        )
        overall = round(
            availability * 0.22
            + performance * 0.17
            + monitoring * 0.12
            + content * 0.10
            + seo * 0.10
            + feedbacks * 0.08
            + security * 0.12
            + stability * 0.09
        )
        intelligence = round(
            availability * 0.16
            + performance * 0.16
            + seo * 0.12
            + content * 0.12
            + feedbacks * 0.10
            + security * 0.16
            + stability * 0.18
        )
        score = PlatformScore(
            overall=_score(overall),
            intelligence=_score(intelligence),
            availability=availability,
            performance=performance,
            monitoring=monitoring,
            content=content,
            seo=seo,
            feedbacks=feedbacks,
            security=security,
            stability=stability,
        )
        await self.state.set("latest_platform_score", score.to_dict())
        return score

    async def trend_context(
        self,
        snapshots: dict[str, WeeklyMetricsSnapshot],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for label, snapshot in snapshots.items():
            result[label] = {
                "period_start": snapshot.period_start,
                "period_end": snapshot.period_end,
                "availability_percent": snapshot.availability_percent,
                "average_response_ms": snapshot.average_response_ms,
                "errors": snapshot.errors_detected,
                "changes": snapshot.changes_detected,
                "feedbacks": snapshot.feedbacks_detected,
                "site_checks": snapshot.site_checks,
            }
        result["feedback_sentiment"] = await self._feedback_trend()
        result["event_counts"] = await self._event_counts()
        return result

    async def _recent_context(
        self,
        event: OperationalEvent,
    ) -> list[dict[str, Any]]:
        values = await self.recent_events(days=30, limit=60)
        related = [
            {
                "event_type": item.get("event_type"),
                "title": item.get("title"),
                "severity": item.get("severity"),
                "status": item.get("status"),
                "occurred_at": item.get("occurred_at"),
                "executive_summary": item.get("analysis", {}).get(
                    "executive_summary"
                ),
            }
            for item in values
            if item.get("scope") == event.scope
            or item.get("correlation_key") == event.correlation_key
        ]
        return related[-12:]

    async def _record(
        self,
        event: OperationalEvent,
        analysis: OperationalAnalysis,
    ) -> None:
        record = event.to_dict()
        record["analysis"] = analysis.to_dict()
        record["recorded_at"] = datetime.now(timezone.utc).isoformat()

        def mutate(history: dict[str, Any]) -> None:
            history[event.event_id] = record
            cutoff = datetime.now(timezone.utc) - timedelta(days=180)
            ordered = sorted(
                history.items(),
                key=lambda item: str(
                    item[1].get("occurred_at", "")
                    if isinstance(item[1], dict)
                    else ""
                ),
            )
            for key, item in ordered:
                if not isinstance(item, dict):
                    del history[key]
                    continue
                if _parse_time(str(item.get("occurred_at", ""))) < cutoff:
                    del history[key]
            while len(history) > 2500:
                oldest = min(
                    history,
                    key=lambda key: str(history[key].get("occurred_at", "")),
                )
                del history[oldest]

        await self.state.mutate_section("operational_events", mutate)

    async def _seo_score(self) -> int:
        snapshots = await self.state.get_section("page_snapshots")
        if not snapshots:
            return 85
        possible = 0
        present = 0
        for snapshot in snapshots.values():
            if not isinstance(snapshot, dict):
                continue
            structure = snapshot.get("structure", {})
            seo = structure.get("seo", {}) if isinstance(structure, dict) else {}
            if not isinstance(seo, dict):
                continue
            for key in (
                "title",
                "description",
                "canonical",
                "robots",
                "open_graph",
                "twitter_cards",
                "structured_data",
            ):
                possible += 1
                value = seo.get(key)
                if value:
                    present += 1
        return _score((present / possible) * 100 if possible else 85)

    async def _feedback_score(self) -> int:
        events = await self.recent_events(days=90, limit=500)
        values = []
        for event in events:
            if event.get("event_type") != "feedback":
                continue
            score = event.get("analysis", {}).get("satisfaction_score")
            if score is not None:
                values.append(_score(score))
        return round(sum(values) / len(values)) if values else 90

    async def _feedback_trend(self) -> dict[str, Any]:
        events = await self.recent_events(days=90, limit=500)
        sentiments: dict[str, int] = {}
        satisfaction: list[int] = []
        for event in events:
            if event.get("event_type") != "feedback":
                continue
            analysis = event.get("analysis", {})
            sentiment = str(analysis.get("sentiment", "not_applicable"))
            sentiments[sentiment] = sentiments.get(sentiment, 0) + 1
            score = analysis.get("satisfaction_score")
            if score is not None:
                satisfaction.append(_score(score))
        return {
            "sentiments": sentiments,
            "average_satisfaction": (
                round(sum(satisfaction) / len(satisfaction), 1)
                if satisfaction
                else None
            ),
            "sample_size": len(satisfaction),
        }

    async def _event_counts(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        now = datetime.now(timezone.utc)
        events = await self.recent_events(days=90, limit=2500)
        for days in (7, 30, 90):
            cutoff = now - timedelta(days=days)
            counts: dict[str, int] = {}
            for event in events:
                if _parse_time(str(event.get("occurred_at", ""))) < cutoff:
                    continue
                event_type = str(event.get("event_type", "other"))
                counts[event_type] = counts.get(event_type, 0) + 1
            result[f"{days}d"] = counts
        return result


def _analysis_prompt(payload: dict[str, Any]) -> str:
    return (
        "Atue como analista senior de observabilidade e operacoes digitais. "
        "Analise exclusivamente os dados estruturados abaixo. Produza uma "
        "interpretacao unica, factual e acionavel. Nao copie logs tecnicos, "
        "nao invente causas e nao use emojis. Para feedbacks, avalie sentimento, "
        "tom, elogios, criticas e satisfacao. Para alteracoes, explique o "
        "significado da mudanca. Para incidentes resolvidos, considere duracao "
        "e historico. Para relatorios, preencha resumo executivo, indicadores, "
        "pontos de atencao, conclusao e recomendacoes.\n\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )


def _compact_event_record(item: dict[str, Any]) -> dict[str, Any]:
    analysis = item.get("analysis", {})
    if not isinstance(analysis, dict):
        analysis = {}
    return {
        "event_type": item.get("event_type"),
        "title": item.get("title"),
        "scope": item.get("scope"),
        "severity": item.get("severity"),
        "status": item.get("status"),
        "occurred_at": item.get("occurred_at"),
        "executive_summary": analysis.get("executive_summary"),
        "impact": analysis.get("impact"),
        "trend": analysis.get("trend"),
        "sentiment": analysis.get("sentiment"),
        "satisfaction_score": analysis.get("satisfaction_score"),
    }


def _analysis_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "executive_summary": {"type": "string"},
            "what_happened": {"type": "string"},
            "impact": {"type": "string"},
            "relevance": {"type": "string"},
            "severity": {
                "type": "string",
                "enum": ["informational", "low", "medium", "high", "critical"],
            },
            "likely_causes": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 4,
            },
            "recommendations": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 5,
            },
            "trend": {"type": "string"},
            "conclusion": {"type": "string"},
            "key_indicators": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
            },
            "attention_points": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 6,
            },
            "sentiment": {
                "type": "string",
                "enum": [
                    "positive",
                    "neutral",
                    "negative",
                    "mixed",
                    "not_applicable",
                ],
            },
            "emotional_tone": {"type": "string"},
            "praise": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 5,
            },
            "criticism": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 5,
            },
            "satisfaction_score": {
                "type": ["integer", "null"],
                "minimum": 0,
                "maximum": 100,
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": [
            "executive_summary",
            "what_happened",
            "impact",
            "relevance",
            "severity",
            "likely_causes",
            "recommendations",
            "trend",
            "conclusion",
            "key_indicators",
            "attention_points",
            "sentiment",
            "emotional_tone",
            "praise",
            "criticism",
            "satisfaction_score",
            "confidence",
        ],
    }


def _local_evidence_analysis(event: OperationalEvent) -> OperationalAnalysis:
    if event.event_type.endswith("_report"):
        return _local_report_analysis(event)
    if event.event_type == "feedback":
        return _local_feedback_analysis(event)

    facts = [
        f"{_local_label(str(key))}: {value}"
        for key, value in event.facts.items()
        if value not in (None, "", [], {})
    ]
    indicators = [
        f"{_local_label(str(key))}: {value}"
        for key, value in event.metrics.items()
        if value not in (None, "", [], {}) and not isinstance(value, (dict, list))
    ]
    summary = event.title
    if facts:
        summary = f"{event.title}. {facts[0]}."
    severity = event.severity.casefold()
    impact = {
        "critical": "Há risco elevado de indisponibilidade ou impacto direto aos usuários.",
        "high": "O evento pode afetar uma parte relevante da operação e exige acompanhamento.",
        "medium": "O evento indica degradação ou alteração que merece validação operacional.",
        "low": "O impacto estimado é limitado, mas a próxima medição deve ser acompanhada.",
        "informational": "Não há impacto negativo confirmado nos dados coletados.",
    }.get(severity, "O impacto será confirmado pelas próximas verificações locais.")
    recommendations = _local_recommendations(event)
    recent_count = len(
        event.history.get("recent_events", [])
        if isinstance(event.history, dict)
        else []
    )
    trend = (
        f"Foram encontrados {recent_count} evento(s) relacionado(s) no histórico recente."
        if recent_count
        else "Não há repetição relevante no histórico recente."
    )
    return OperationalAnalysis(
        executive_summary=summary,
        what_happened=summary,
        impact=impact,
        relevance=f"Evento operacional classificado como {event.severity}.",
        severity=event.severity,
        likely_causes=(),
        recommendations=recommendations,
        trend=trend,
        conclusion=(
            "Resumo calculado localmente a partir das medições registradas, "
            "sem uso de inteligência artificial."
        ),
        key_indicators=tuple(indicators[:8]),
        attention_points=tuple(facts[:6]),
        confidence=1.0,
        generated_by="local_rules",
    )


def _local_report_analysis(event: OperationalEvent) -> OperationalAnalysis:
    context = event.metrics if isinstance(event.metrics, dict) else {}
    metrics = context.get("metrics") if isinstance(context.get("metrics"), dict) else {}
    score = (
        context.get("health_score")
        if isinstance(context.get("health_score"), dict)
        else {}
    )
    period = str(context.get("period") or event.facts.get("period") or "executivo")
    availability = _number_value(metrics.get("availability_percent"))
    average_ms = _number_value(metrics.get("average_response_ms"))
    maximum_ms = _number_value(metrics.get("maximum_response_ms"))
    errors = _int_value(metrics.get("errors_detected"))
    changes = _int_value(metrics.get("changes_detected"))
    feedbacks = _int_value(metrics.get("feedbacks_detected"))
    checks = _int_value(metrics.get("site_checks"))
    successes = _int_value(metrics.get("site_successes"))
    active_incidents = _int_value(context.get("active_incidents"))
    overall = _int_value(score.get("overall"))

    indicators = (
        f"Disponibilidade: {availability:.2f}%",
        f"Tempo médio de resposta: {average_ms:.0f} ms",
        f"Tempo máximo de resposta: {maximum_ms:.0f} ms",
        f"Verificações bem-sucedidas: {successes}/{checks}",
        f"Erros detectados: {errors}",
        f"Alterações detectadas: {changes}",
        f"Feedbacks registrados: {feedbacks}",
        f"Incidentes ativos: {active_incidents}",
    )
    attention: list[str] = []
    recommendations: list[str] = []
    if active_incidents:
        attention.append(f"{active_incidents} incidente(s) permanece(m) ativo(s).")
        recommendations.append("Priorizar a resolução dos incidentes ativos.")
    if checks and availability < 99.9:
        attention.append(f"Disponibilidade do período em {availability:.2f}%.")
        recommendations.append("Revisar as falhas registradas no período.")
    if average_ms >= 1000 or maximum_ms >= 2500:
        attention.append(
            f"Latência média de {average_ms:.0f} ms e máxima de {maximum_ms:.0f} ms."
        )
        recommendations.append("Verificar os endpoints com maior tempo de resposta.")
    if errors:
        attention.append(f"{errors} erro(s) operacional(is) foram contabilizados.")
    if changes:
        recommendations.append("Validar as alterações detectadas nas páginas monitoradas.")
    if not recommendations:
        recommendations.append("Manter o acompanhamento com a rotina atual de verificações.")

    healthy = active_incidents == 0 and (not checks or availability >= 99.9) and errors == 0
    summary = (
        f"No período {period}, a plataforma registrou {availability:.2f}% de "
        f"disponibilidade e tempo médio de resposta de {average_ms:.0f} ms. "
        f"O Health Score calculado foi {overall}/100."
    )
    return OperationalAnalysis(
        executive_summary=summary,
        what_happened=(
            f"Foram processadas {checks} verificações, com {successes} resultados "
            f"bem-sucedidos, {errors} erro(s) e {changes} alteração(ões)."
        ),
        impact=(
            "A operação permaneceu estável no período."
            if healthy
            else "Os pontos de atenção listados podem afetar disponibilidade ou desempenho."
        ),
        relevance="Consolidação automática das métricas operacionais do período.",
        severity="informational" if healthy else "medium",
        likely_causes=(),
        recommendations=tuple(dict.fromkeys(recommendations)),
        trend=_local_report_trend(context.get("trends")),
        conclusion=(
            "Os indicadores permanecem dentro do padrão operacional esperado."
            if healthy
            else "O período requer acompanhamento dos pontos de atenção identificados."
        ),
        key_indicators=indicators,
        attention_points=tuple(attention or ("Nenhum ponto crítico identificado.",)),
        confidence=1.0,
        generated_by="local_rules",
    )


def _local_feedback_analysis(event: OperationalEvent) -> OperationalAnalysis:
    message = str(event.facts.get("message") or event.facts.get("feedback") or "")
    rating = _number_value(event.facts.get("rating"))
    normalized = message.casefold()
    positive_terms = ("excelente", "ótimo", "bom", "rápido", "gostei", "parabéns")
    negative_terms = ("ruim", "péssimo", "lento", "erro", "problema", "decepcion")
    positive = sum(term in normalized for term in positive_terms)
    negative = sum(term in normalized for term in negative_terms)
    if rating:
        positive += int(rating >= 4)
        negative += int(rating <= 2)
    sentiment = "positive" if positive > negative else "negative" if negative > positive else "neutral"
    satisfaction = round(rating * 20) if rating else 50 + (positive - negative) * 15
    satisfaction = max(0, min(100, satisfaction))
    return OperationalAnalysis(
        executive_summary=event.title,
        what_happened="Um novo feedback foi registrado e classificado por regras locais.",
        impact="O conteúdo contribui para acompanhar a percepção dos usuários.",
        relevance="Feedback registrado para acompanhamento operacional.",
        severity=event.severity,
        likely_causes=(),
        recommendations=("Considerar os pontos citados nas próximas revisões.",),
        trend="A tendência será consolidada com os demais feedbacks do período.",
        conclusion="Feedback organizado sem uso de inteligência artificial.",
        key_indicators=(f"Satisfação calculada: {satisfaction}/100",),
        attention_points=(),
        sentiment=sentiment,
        emotional_tone=sentiment,
        praise=(message[:180],) if sentiment == "positive" and message else (),
        criticism=(message[:180],) if sentiment == "negative" and message else (),
        satisfaction_score=satisfaction,
        confidence=0.75,
        generated_by="local_rules",
    )


def _local_recommendations(event: OperationalEvent) -> tuple[str, ...]:
    event_type = event.event_type.casefold()
    if "performance" in event_type or "latency" in event_type:
        return (
            "Comparar a próxima medição com a média histórica.",
            "Verificar recursos ou endpoints com maior latência.",
        )
    if "incident_resolved" in event_type:
        return ("Manter o acompanhamento para confirmar a estabilidade da recuperação.",)
    if "security" in event_type or "log" in event_type:
        return ("Revisar os registros técnicos no canal restrito e manter o monitoramento.",)
    if "change" in event_type:
        return ("Validar se a alteração detectada corresponde a uma publicação esperada.",)
    return ("Acompanhar a próxima verificação e validar qualquer recorrência.",)


def _local_report_trend(value: Any) -> str:
    if not isinstance(value, dict):
        return "Tendência calculada com o histórico disponível."
    recent = value.get("7d") if isinstance(value.get("7d"), dict) else {}
    long_term = value.get("30d") if isinstance(value.get("30d"), dict) else {}
    recent_latency = _number_value(recent.get("average_response_ms"))
    long_latency = _number_value(long_term.get("average_response_ms"))
    if recent_latency and long_latency:
        direction = "melhor" if recent_latency < long_latency else "pior" if recent_latency > long_latency else "estável"
        return (
            f"A média de 7 dias ({recent_latency:.0f} ms) está {direction} que "
            f"a média de 30 dias ({long_latency:.0f} ms)."
        )
    return "Tendência calculada com o histórico disponível."


def _local_label(value: str) -> str:
    labels = {
        "latencia_atual_ms": "Latência atual",
        "latencia_media_historica_ms": "Latência média histórica",
        "variacao_percentual": "Variação percentual",
        "status_code": "Status HTTP",
        "response_ms": "Tempo de resposta",
        "duration_seconds": "Duração",
    }
    return labels.get(value, value.replace("_", " ").capitalize())


def _number_value(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int_value(value: Any) -> int:
    return round(_number_value(value))


def _sanitize(value: Any, key: str = "") -> Any:
    secret_keys = (
        "token",
        "secret",
        "password",
        "senha",
        "api_key",
        "authorization",
        "cookie",
    )
    if any(item in key.casefold() for item in secret_keys):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item, key) for item in value]
    if isinstance(value, str):
        return value[:6000]
    return value


def _performance_score(metrics: WeeklyMetricsSnapshot) -> int:
    average = metrics.average_response_ms
    if average <= 0:
        return 90
    if average <= 300:
        base = 100
    elif average <= 800:
        base = 96 - int((average - 300) / 50)
    elif average <= 1500:
        base = 86 - int((average - 800) / 70)
    elif average <= 2500:
        base = 76 - int((average - 1500) / 100)
    else:
        base = 60 - int((average - 2500) / 250)
    return _score(base - min(20, metrics.errors_detected * 2))


def _monitoring_score(metrics: WeeklyMetricsSnapshot) -> int:
    if metrics.site_checks <= 0:
        return 70
    error_rate = metrics.errors_detected / max(1, metrics.total_requests)
    return _score(100 - error_rate * 100)


def _content_score(changes: ChangeScanReport | None) -> int:
    if changes is None:
        return 85
    penalty = len(changes.errors) * 7
    return _score(100 - penalty)


def _security_score(site: MonitorReport | None) -> int:
    if site is None:
        return 75
    score = 100
    if site.verification_status != "verified":
        score -= 20
    if site.tls_days_left is None:
        score -= 10
    elif site.tls_days_left < 14:
        score -= 20
    for alert in site.alerts:
        if alert.key == "missing_security_headers":
            score -= 12
        elif alert.severity == "critical":
            score -= 20
    return _score(score)


def _stability_score(
    availability: int,
    performance: int,
    active_incidents: int,
    errors: int,
) -> int:
    return _score(
        availability * 0.55
        + performance * 0.30
        + max(0, 100 - active_incidents * 18 - errors * 2) * 0.15
    )


def _score(value: Any) -> int:
    try:
        return max(0, min(100, round(float(value))))
    except (TypeError, ValueError):
        return 0


def _optional_score(value: Any) -> int | None:
    return None if value is None else _score(value)


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _analysis_error_message(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()
    lowered = message.casefold()
    if "invalid_api_key" in lowered or "invalid api key" in lowered:
        return (
            "Groq indisponível porque GROQ_API_KEY está inválida. "
            "Atualize o .env para restaurar as análises por IA."
        )
    if len(message) > 500:
        return f"{message[:497]}..."
    return message


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _public_text(value: Any) -> str:
    text = " ".join(str(value).split())
    replacements = {
        "TimeoutError": "tempo limite excedido",
        "Page.goto()": "carregamento da página",
        "Page.goto": "carregamento da página",
        "domcontentloaded": "carregamento inicial",
        "Call log": "diagnóstico interno",
        "Traceback": "diagnóstico interno",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(
        r"[A-Za-z]:\\(?:[^\\\s]+\\)+[^\\\s]+",
        "caminho interno",
        text,
    )
    text = re.sub(r"\b(?:File|line)\s+\"?[^\s]+\"?", "", text)
    return text[:3000].strip()
