from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import discord

from .intelligence import (
    OperationalAnalysis,
    OperationalEvent,
    PlatformScore,
)
from .metrics import WeeklyMetricsSnapshot

NAVY = discord.Color.from_rgb(29, 46, 66)
GREEN = discord.Color.from_rgb(35, 134, 84)
AMBER = discord.Color.from_rgb(183, 121, 31)
RED = discord.Color.from_rgb(185, 55, 55)
BLUE = discord.Color.from_rgb(38, 101, 160)


def operational_event_embed(
    event: OperationalEvent,
    analysis: OperationalAnalysis,
    score: PlatformScore | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=_clip(event.title, 250),
        description=_clip(analysis.executive_summary, 1800),
        color=_severity_color(analysis.severity or event.severity),
        timestamp=_parse_time(event.occurred_at),
        url=event.source_url or None,
    )
    embed.add_field(
        name="Contexto operacional",
        value=_clip(
            "\n".join(
                part
                for part in (
                    f"Domínio: **{event.domain}**",
                    f"Escopo: **{_label(event.scope)}**",
                    f"Estado: **{_label(event.status)}**",
                    f"Severidade: **{_label(analysis.severity)}**",
                )
                if part
            ),
            1000,
        ),
        inline=True,
    )
    indicators = _indicator_lines(event, analysis)
    if indicators:
        embed.add_field(
            name="Indicadores",
            value=_clip("\n".join(indicators), 1000),
            inline=True,
        )
    embed.add_field(
        name="Impacto estimado",
        value=_clip(analysis.impact, 1000),
        inline=False,
    )
    if analysis.what_happened and analysis.what_happened != analysis.executive_summary:
        embed.add_field(
            name="Interpretação",
            value=_clip(analysis.what_happened, 1000),
            inline=False,
        )
    if analysis.attention_points:
        embed.add_field(
            name="Pontos de atenção",
            value=_bullet_list(analysis.attention_points),
            inline=False,
        )
    if analysis.recommendations:
        embed.add_field(
            name="Recomendações",
            value=_bullet_list(analysis.recommendations),
            inline=False,
        )
    if event.event_type == "feedback":
        embed.add_field(
            name="Leitura do feedback",
            value=_feedback_analysis(analysis),
            inline=False,
        )
    if analysis.trend:
        embed.add_field(
            name="Tendência",
            value=_clip(analysis.trend, 1000),
            inline=False,
        )
    if score:
        embed.add_field(
            name="Saúde da plataforma",
            value=(
                f"Health Score: **{score.overall}/100**\n"
                f"Intelligence Score: **{score.intelligence}/100**"
            ),
            inline=True,
        )
    embed.set_footer(text="Bella Intelligence Center | análise operacional")
    return embed


def executive_report_embeds(
    title: str,
    period_label: str,
    analysis: OperationalAnalysis,
    score: PlatformScore,
    metrics: WeeklyMetricsSnapshot,
    trends: dict[str, Any],
    *,
    active_incidents: int,
) -> list[discord.Embed]:
    executive = discord.Embed(
        title=title,
        description=_clip(analysis.executive_summary, 1900),
        color=GREEN if score.overall >= 90 else AMBER if score.overall >= 70 else RED,
        timestamp=datetime.now(timezone.utc),
    )
    executive.add_field(name="Período", value=period_label, inline=False)
    executive.add_field(
        name="Pontuação geral",
        value=(
            f"Health Score: **{score.overall}/100**\n"
            f"Intelligence Score: **{score.intelligence}/100**"
        ),
        inline=True,
    )
    executive.add_field(
        name="Disponibilidade",
        value=(
            f"**{metrics.availability_percent:.2f}%**\n"
            f"{metrics.site_successes}/{metrics.site_checks} verificações"
        ),
        inline=True,
    )
    executive.add_field(
        name="Performance",
        value=(
            f"Média: **{metrics.average_response_ms:.1f} ms**\n"
            f"Máxima: **{_number(metrics.maximum_response_ms)} ms**"
        ),
        inline=True,
    )
    executive.add_field(
        name="Principais eventos",
        value=_bullet_list(
            analysis.key_indicators
            or (
                f"{metrics.errors_detected} erro(s) operacional(is)",
                f"{metrics.changes_detected} alteração(ões)",
                f"{metrics.feedbacks_detected} feedback(s)",
                f"{active_incidents} incidente(s) ativo(s)",
            )
        ),
        inline=False,
    )
    executive.add_field(
        name="Pontos de atenção",
        value=_bullet_list(analysis.attention_points or ("Nenhum ponto crítico.",)),
        inline=False,
    )
    executive.add_field(
        name="Conclusão",
        value=_clip(analysis.conclusion, 1000),
        inline=False,
    )
    executive.set_footer(text="Bella Intelligence Center | resumo executivo")

    components = discord.Embed(
        title="Indicadores operacionais",
        description=(
            "Pontuações calculadas a partir das medições, incidentes, "
            "conteúdo, SEO, segurança e feedbacks."
        ),
        color=BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    for name, value in (
        ("Disponibilidade", score.availability),
        ("Performance", score.performance),
        ("Monitoramento", score.monitoring),
        ("Conteúdo", score.content),
        ("SEO", score.seo),
        ("Feedbacks", score.feedbacks),
        ("Segurança", score.security),
        ("Estabilidade", score.stability),
    ):
        components.add_field(name=name, value=f"**{value}/100**", inline=True)
    components.add_field(
        name="Tendências",
        value=_clip(_trend_lines(trends), 1000),
        inline=False,
    )
    components.add_field(
        name="Recomendações prioritárias",
        value=_bullet_list(analysis.recommendations),
        inline=False,
    )
    components.set_footer(text="Scores dinâmicos; nenhuma estimativa manual")
    return [executive, components]


def technical_event_embed(
    event: OperationalEvent,
    error: BaseException | str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Diagnóstico técnico: {event.title}",
        description=(
            "Detalhes internos destinados exclusivamente ao canal técnico."
        ),
        color=NAVY,
        timestamp=_parse_time(event.occurred_at),
    )
    embed.add_field(name="Event ID", value=event.event_id, inline=True)
    embed.add_field(name="Tipo", value=event.event_type, inline=True)
    embed.add_field(name="Escopo", value=event.scope, inline=True)
    if event.technical:
        embed.add_field(
            name="Contexto técnico",
            value=_clip(
                json.dumps(
                    event.technical,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                1000,
            ),
            inline=False,
        )
    if error:
        embed.add_field(
            name="Exceção",
            value=_clip(str(error), 1000),
            inline=False,
        )
    embed.set_footer(text="Bella Intelligence Center | canal técnico")
    return embed


def _indicator_lines(
    event: OperationalEvent,
    analysis: OperationalAnalysis,
) -> list[str]:
    lines = []
    for key, value in event.metrics.items():
        if value in (None, "", [], {}):
            continue
        lines.append(f"{_label(str(key))}: **{value}**")
    lines.extend(f"{item}" for item in analysis.key_indicators)
    return lines[:8]


def _feedback_analysis(analysis: OperationalAnalysis) -> str:
    parts = [
        f"Sentimento: **{_label(analysis.sentiment)}**",
        f"Tom: **{_label(analysis.emotional_tone)}**",
    ]
    if analysis.satisfaction_score is not None:
        parts.append(f"Satisfação estimada: **{analysis.satisfaction_score}/100**")
    if analysis.praise:
        parts.append("Elogios: " + "; ".join(analysis.praise[:3]))
    if analysis.criticism:
        parts.append("Críticas: " + "; ".join(analysis.criticism[:3]))
    return _clip("\n".join(parts), 1000)


def _trend_lines(trends: dict[str, Any]) -> str:
    lines = []
    for period in ("7d", "30d", "90d"):
        data = trends.get(period)
        if not isinstance(data, dict):
            continue
        lines.append(
            f"**{period}:** disponibilidade {data.get('availability_percent', 'n/a')}%, "
            f"média {data.get('average_response_ms', 'n/a')} ms, "
            f"{data.get('errors', 0)} erro(s)"
        )
    feedback = trends.get("feedback_sentiment", {})
    if isinstance(feedback, dict) and feedback.get("sample_size"):
        lines.append(
            "**Feedbacks:** satisfação média "
            f"{feedback.get('average_satisfaction')}/100 em "
            f"{feedback.get('sample_size')} análise(s)"
        )
    return "\n".join(lines) or "Histórico ainda insuficiente."


def _severity_color(value: str) -> discord.Color:
    normalized = value.casefold()
    if normalized in {"critical", "high"}:
        return RED
    if normalized in {"medium", "warning"}:
        return AMBER
    if normalized in {"low", "informational", "info"}:
        return BLUE
    return NAVY


def _label(value: str) -> str:
    labels = {
        "opened": "Aberto",
        "recovered": "Resolvido",
        "reminder": "Em acompanhamento",
        "detected": "Detectado",
        "critical": "Crítica",
        "high": "Alta",
        "medium": "Média",
        "low": "Baixa",
        "informational": "Informativa",
        "positive": "Positivo",
        "negative": "Negativo",
        "neutral": "Neutro",
        "mixed": "Misto",
        "not_applicable": "Não aplicável",
        "site": "Disponibilidade",
        "changes": "Conteúdo",
        "feedbacks": "Feedbacks",
        "logs": "Segurança",
    }
    return labels.get(value.casefold(), value.replace("_", " ").title())


def _bullet_list(values: tuple[str, ...] | list[str]) -> str:
    if not values:
        return "Nenhuma recomendação adicional."
    return _clip("\n".join(f"- {value}" for value in values), 1000)


def _number(value: Any) -> str:
    return "n/a" if value is None else str(value)


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _clip(value: str, limit: int) -> str:
    clean = "\n".join(
        " ".join(line.split())
        for line in str(value).splitlines()
        if line.strip()
    )
    return clean if len(clean) <= limit else clean[: limit - 3] + "..."
