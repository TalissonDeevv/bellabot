from __future__ import annotations

from datetime import datetime, timezone

import discord

from .changes import ChangeScanReport, ContentChange
from .feedbacks import Feedback, FeedbackScanReport, FeedbackStatus
from .incidents import IncidentEvent
from .knowledge import KnowledgeAnalyticsSnapshot, KnowledgeRefreshReport
from .log_analyzer import LogAlert, LogScanReport
from .metrics import MetricsSnapshot, WeeklyMetricsSnapshot
from .monitor import MonitorReport
from .traffic import TrafficSnapshot

BRAND_COLOR = discord.Color.from_rgb(24, 119, 242)
SUCCESS_COLOR = discord.Color.from_rgb(46, 160, 67)
WARNING_COLOR = discord.Color.from_rgb(210, 153, 34)
ERROR_COLOR = discord.Color.from_rgb(218, 54, 51)


def site_incident_embed(
    report: MonitorReport,
    event: IncidentEvent,
) -> discord.Embed:
    if event.kind == "recovered":
        title = f"Incidente resolvido: {event.alert.title}"
        description = (
            "A condição que gerou o alerta não aparece mais na verificação atual."
        )
        color = SUCCESS_COLOR
    elif event.kind == "reminder":
        title = f"Incidente ainda ativo: {event.alert.title}"
        description = event.alert.description
        color = ERROR_COLOR if event.alert.severity == "critical" else WARNING_COLOR
    else:
        title = f"Novo alerta do site: {event.alert.title}"
        description = event.alert.description
        color = ERROR_COLOR if event.alert.severity == "critical" else WARNING_COLOR

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=_parse_timestamp(report.checked_at),
        url=report.final_url or report.site_url,
    )
    _add_site_fields(embed, report)
    embed.add_field(
        name="Incidente",
        value=(
            f"Estado: **{_incident_kind_label(event.kind)}**\n"
            f"Início: <t:{int(event.first_seen)}:F>\n"
            f"ID: `{event.incident_id}`"
        ),
        inline=False,
    )
    if event.alert.evidence:
        embed.add_field(
            name="Contexto técnico",
            value=_clip(event.alert.evidence, 1000),
            inline=False,
        )
    embed.set_footer(text="Site Sentinel | alertas sem duplicação")
    return embed


def site_report_embed(
    report: MonitorReport,
    title: str = "Status atual do site",
) -> discord.Embed:
    color = SUCCESS_COLOR if not report.alerts else WARNING_COLOR
    if report.critical:
        color = ERROR_COLOR
    embed = discord.Embed(
        title=title,
        description=_site_executive_summary(report),
        color=color,
        timestamp=_parse_timestamp(report.checked_at),
        url=report.final_url or report.site_url,
    )
    _add_site_fields(embed, report)
    if report.alerts:
        embed.add_field(
            name="Pontos de atenção",
            value=_clip(
                "\n".join(
                    f"- **{alert.title}:** {alert.description}"
                    for alert in report.alerts
                ),
                1000,
            ),
            inline=False,
        )
    embed.set_footer(text="Site Sentinel | verificação sob demanda")
    return embed


def advanced_report_embed(
    metrics: MetricsSnapshot,
    latest_site: MonitorReport | None,
    latest_feedback: FeedbackScanReport | None = None,
    latest_changes: ChangeScanReport | None = None,
) -> discord.Embed:
    current_errors = 0
    if latest_site:
        current_errors += len(latest_site.alerts) + int(bool(latest_site.error))
    if latest_feedback and latest_feedback.error:
        current_errors += 1
    if latest_changes:
        current_errors += len(latest_changes.errors)

    color = SUCCESS_COLOR if current_errors == 0 else WARNING_COLOR
    embed = discord.Embed(
        title="Relatório executivo de monitoramento",
        description=_advanced_executive_summary(
            metrics,
            latest_site,
            latest_feedback,
            latest_changes,
        ),
        color=color,
        timestamp=_parse_timestamp(metrics.generated_at),
    )
    embed.add_field(
        name="Requisições",
        value=(
            f"Total: **{metrics.total_requests}**\n"
            f"Média: **{metrics.average_response_ms:.1f} ms**\n"
            f"Mínima / máxima: **{_number(metrics.minimum_response_ms)} / "
            f"{_number(metrics.maximum_response_ms)} ms**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Eventos",
        value=(
            f"Processados: **{metrics.events_processed}**\n"
            f"Alterações: **{metrics.changes_detected}**\n"
            f"Novos feedbacks: **{metrics.feedbacks_detected}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Operação",
        value=(
            f"Uptime: **{_format_duration(metrics.uptime_seconds)}**\n"
            f"Verificações: **{metrics.checks_completed}**\n"
            f"Erros acumulados: **{metrics.errors_detected}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Status HTTP acumulado",
        value=_clip(
            ", ".join(
                f"`{status}`: {count}"
                for status, count in sorted(metrics.status_counts.items())
            )
            or "Nenhuma requisição registrada.",
            1000,
        ),
        inline=False,
    )
    if latest_site:
        embed.add_field(
            name="Última verificação principal",
            value=(
                f"HTTP: **{latest_site.status_code or 'sem resposta'}** | "
                f"Tempo: **{_number(latest_site.response_ms)} ms** | "
                f"TLS: **{_number(latest_site.tls_days_left)} dias** | "
                f"Validação: **{_verification_label(latest_site.verification_status)}**\n"
                f"Alertas ativos: **{len(latest_site.alerts)}**"
            ),
            inline=False,
        )
    if latest_feedback:
        embed.add_field(
            name="Feedbacks na última leitura",
            value=(
                f"Encontrados: **{len(latest_feedback.feedbacks)}** | "
                f"Novos: **{len(latest_feedback.new_feedbacks)}** | "
                f"Erro: **{'sim' if latest_feedback.error else 'não'}**"
            ),
            inline=False,
        )
    if latest_changes:
        embed.add_field(
            name="Páginas na última leitura",
            value=(
                f"Verificadas: **{latest_changes.pages_checked}** | "
                f"Alteradas: **{len(latest_changes.changes)}** | "
                f"Ocorrências: **{len(latest_changes.errors)}** | "
                f"Leitura: **{latest_changes.renderer}**"
            ),
            inline=False,
        )
    if metrics.recent_requests:
        lines = []
        for item in metrics.recent_requests[-8:]:
            lines.append(
                f"- `{item.get('category', 'request')}` "
                f"HTTP {item.get('status_code') or 'n/a'} em "
                f"{_number(item.get('response_ms'))} ms"
            )
        embed.add_field(
            name="Tempos individuais recentes",
            value=_clip("\n".join(lines), 1000),
            inline=False,
        )
    embed.set_footer(
        text=f"Monitoramento iniciado em {metrics.started_at} | Developed by Talisson Souza"
    )
    return embed


def knowledge_refresh_embed(report: KnowledgeRefreshReport) -> discord.Embed:
    color = ERROR_COLOR if report.error else SUCCESS_COLOR
    embed = discord.Embed(
        title="Base oficial de conhecimento",
        description=(
            "A atualização não pôde ser concluída."
            if report.error
            else "O conteúdo oficial foi atualizado e já pode ser consultado."
        ),
        color=color,
        timestamp=_parse_timestamp(report.checked_at),
    )
    embed.add_field(name="Entradas", value=str(report.entries), inline=True)
    embed.add_field(name="FAQ", value=str(report.faq_entries), inline=True)
    embed.add_field(name="Páginas", value=str(report.page_entries), inline=True)
    embed.add_field(
        name="Páginas processadas",
        value=str(report.pages_processed),
        inline=True,
    )
    embed.add_field(
        name="Tempo",
        value=f"{report.duration_ms} ms",
        inline=True,
    )
    embed.add_field(
        name="Estado",
        value="Disponível" if report.entries and not report.error else "Indisponível",
        inline=True,
    )
    if report.warnings:
        embed.add_field(
            name="Avisos",
            value=_clip("\n".join(f"- {item}" for item in report.warnings), 1000),
            inline=False,
        )
    if report.error:
        embed.add_field(
            name="Estado",
            value="Leitura incompleta; diagnóstico encaminhado ao canal técnico.",
            inline=False,
        )
    embed.set_footer(text="Somente conteúdo publicado no site é indexado")
    return embed


def weekly_report_embeds(
    weekly: WeeklyMetricsSnapshot,
    knowledge: KnowledgeAnalyticsSnapshot,
    traffic: TrafficSnapshot,
    *,
    knowledge_entries: int,
    knowledge_last_refresh: str | None,
    active_incidents: int,
    incident_counts: dict[str, int],
    latest_site: MonitorReport | None,
    latest_feedback: FeedbackScanReport | None,
    latest_changes: ChangeScanReport | None,
) -> list[discord.Embed]:
    healthy = active_incidents == 0 and (latest_site is None or not latest_site.alerts)
    executive = discord.Embed(
        title="Relatório semanal de monitoramento",
        description=(
            "A semana terminou sem incidentes ativos. O relatório completo é "
            "enviado mesmo quando nenhuma ocorrência é registrada."
            if healthy
            else "A semana possui ocorrências que merecem acompanhamento."
        ),
        color=SUCCESS_COLOR if healthy else WARNING_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    executive.add_field(
        name="Período",
        value=f"{weekly.period_start} até {weekly.period_end}",
        inline=False,
    )
    executive.add_field(
        name="Disponibilidade",
        value=(
            (
                f"**{weekly.availability_percent:.2f}%**\n"
                f"{weekly.site_successes}/{weekly.site_checks} "
                "verificações principais"
            )
            if weekly.site_checks
            else "Sem verificações principais registradas no período."
        ),
        inline=True,
    )
    executive.add_field(
        name="Requisições",
        value=(
            f"Total: **{weekly.total_requests}**\nErros: **{weekly.errors_detected}**"
        ),
        inline=True,
    )
    executive.add_field(
        name="Desempenho",
        value=(
            f"Média: **{weekly.average_response_ms:.1f} ms**\n"
            f"Mín./máx.: **{_number(weekly.minimum_response_ms)} / "
            f"{_number(weekly.maximum_response_ms)} ms**"
        ),
        inline=True,
    )
    executive.add_field(
        name="Status HTTP",
        value=_clip(
            ", ".join(
                f"`{status}`: {count}"
                for status, count in sorted(weekly.status_counts.items())
            )
            or "Nenhuma requisição registrada.",
            1000,
        ),
        inline=False,
    )
    executive.add_field(
        name="Atividade detectada",
        value=(
            f"Eventos: **{weekly.events_processed}**\n"
            f"Alterações: **{weekly.changes_detected}**\n"
            f"Novos feedbacks: **{weekly.feedbacks_detected}**"
        ),
        inline=True,
    )
    executive.add_field(
        name="Incidentes",
        value=(
            f"Abertos na semana: **{incident_counts.get('opened', 0)}**\n"
            f"Recuperados: **{incident_counts.get('recovered', 0)}**\n"
            f"Ativos agora: **{active_incidents}**"
        ),
        inline=True,
    )
    executive.add_field(
        name="Tipos de verificação",
        value=_clip(
            "\n".join(
                f"- **{category}:** {count}"
                for category, count in sorted(weekly.category_counts.items())
            )
            or "Nenhuma atividade registrada.",
            1000,
        ),
        inline=True,
    )
    if latest_site:
        executive.add_field(
            name="Estado atual do site",
            value=(
                f"HTTP **{latest_site.status_code or 'n/a'}**, "
                f"resposta em **{_number(latest_site.response_ms)} ms**, "
                f"TLS por **{_number(latest_site.tls_days_left)} dias** e "
                f"validação **{_verification_label(latest_site.verification_status)}**."
            ),
            inline=False,
        )
    executive.set_footer(
        text="Site Sentinel | relatório semanal automático e obrigatório"
    )

    assistant = discord.Embed(
        title="Relatório semanal de conteúdo e atendimento",
        description=(
            "Resumo do monitor de páginas, feedbacks e respostas automáticas "
            "baseadas exclusivamente no conteúdo oficial."
        ),
        color=BRAND_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    assistant.add_field(
        name="Assistente do servidor",
        value=(
            f"Mensagens avaliadas: **{knowledge.evaluated}**\n"
            f"Respondidas: **{knowledge.answered}**\n"
            f"Ignoradas: **{knowledge.ignored}**"
        ),
        inline=True,
    )
    assistant.add_field(
        name="Precisão operacional",
        value=(
            f"Confiança média das respostas: "
            f"**{knowledge.average_answer_confidence * 100:.1f}%**\n"
            f"Entradas oficiais: **{knowledge_entries}**"
        ),
        inline=True,
    )
    assistant.add_field(
        name="Atualização da fonte",
        value=(
            _format_report_timestamp(knowledge_last_refresh)
            if knowledge_last_refresh
            else "Ainda não atualizada"
        ),
        inline=True,
    )
    assistant.add_field(
        name="Motivos para silêncio",
        value=_clip(
            "\n".join(
                f"- **{_knowledge_reason_label(reason)}:** {count}"
                for reason, count in sorted(knowledge.reasons.items())
                if reason != "answered"
            )
            or "Nenhuma mensagem precisou ser ignorada.",
            1000,
        ),
        inline=False,
    )
    if latest_feedback:
        assistant.add_field(
            name="Feedbacks",
            value=(
                f"Última leitura: **{len(latest_feedback.feedbacks)}** no site\n"
                f"Novos na leitura: **{len(latest_feedback.new_feedbacks)}**\n"
                f"Erro: **{'sim' if latest_feedback.error else 'não'}**"
            ),
            inline=True,
        )
    if latest_changes:
        assistant.add_field(
            name="Conteúdo do site",
            value=(
                f"Páginas verificadas: **{latest_changes.pages_checked}**\n"
                f"Alterações na leitura: **{len(latest_changes.changes)}**\n"
                f"Ocorrências: **{len(latest_changes.errors)}**"
            ),
            inline=True,
        )
    assistant.add_field(
        name="Atividade diária",
        value=_clip(_weekly_daily_lines(weekly, knowledge), 1000),
        inline=False,
    )
    assistant.set_footer(
        text="Perguntas sem resposta oficial ou abaixo do limite permanecem sem resposta"
    )
    audience = traffic_report_embed(
        traffic,
        title="Relatório semanal de audiência",
    )
    return [executive, audience, assistant]


def traffic_report_embed(
    traffic: TrafficSnapshot,
    title: str = "Audiência do site",
) -> discord.Embed:
    if traffic.available:
        if traffic.source_kind == "ga4":
            description = (
                "Métricas reais de audiência obtidas pelo Google Analytics 4, "
                "sem depender de Log Drain ou plano pago da Vercel."
            )
        else:
            description = (
                "Métricas calculadas a partir de requisições reais, com robôs e "
                "arquivos estáticos separados das visualizações de página."
            )
        color = BRAND_COLOR
    else:
        description = (
            "A fonte de audiência ainda não está disponível. Nenhum número "
            "estimado ou gerado pelo próprio monitor foi usado."
        )
        color = WARNING_COLOR
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Período",
        value=f"{traffic.period_start} até {traffic.period_end}",
        inline=False,
    )
    if not traffic.available:
        embed.add_field(
            name="Configuração necessária",
            value=_clip(
                traffic.error
                or (
                    "Configure GA4_PROPERTY_ID e GOOGLE_APPLICATION_CREDENTIALS "
                    "ou use TRAFFIC_LOG_PATH com uma fonte de acesso real."
                ),
                1000,
            ),
            inline=False,
        )
        embed.add_field(
            name="Fonte esperada",
            value=(
                "Google Analytics 4 Data API ou access log Common/Combined "
                "em JSON/NDJSON."
            ),
            inline=False,
        )
        embed.set_footer(text="Site Sentinel | audiência sem dados artificiais")
        return embed

    embed.add_field(
        name="Audiência",
        value=(
            f"Visualizações: **{traffic.pageviews}**\n"
            f"Visitas: **{traffic.visits}**\n"
            f"Visitantes únicos: **{traffic.unique_visitors}**"
        ),
        inline=True,
    )
    if traffic.source_kind == "ga4":
        embed.add_field(
            name="Cobertura dos dados",
            value=_clip(
                traffic.data_note
                or "Sessões e usuários informados diretamente pelo GA4.",
                1000,
            ),
            inline=True,
        )
    else:
        embed.add_field(
            name="Qualidade dos dados",
            value=(
                f"Registros processados: **{traffic.records_processed}**\n"
                f"Automações separadas: **{traffic.bot_requests}**\n"
                f"Recursos/erros excluídos: **{traffic.excluded_requests}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Origem das requisições",
            value=(
                f"Usuários/pageviews: **{traffic.pageviews}**\n"
                f"Monitor Bella: **{traffic.monitor_requests}**\n"
                f"Crawlers: **{traffic.crawler_requests}**\n"
                f"Outras automações: **{traffic.automated_requests}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Status HTTP da audiência",
            value=(
                ", ".join(
                    f"`{status}`: {count}"
                    for status, count in sorted(traffic.status_counts.items())
                )
                or "Nenhum status registrado."
            ),
            inline=True,
        )
    embed.add_field(
        name="Páginas mais acessadas",
        value=_clip(
            "\n".join(
                f"{index}. **{path}** — {count} visualização(ões)"
                for index, (path, count) in enumerate(traffic.top_pages, 1)
            )
            or "Nenhuma visualização de página no período.",
            1000,
        ),
        inline=False,
    )
    embed.add_field(
        name="Principais origens",
        value=_clip(
            "\n".join(
                f"- **{domain}:** {count}"
                for domain, count in traffic.top_referrers
            )
            or "Acesso direto ou origem não informada.",
            1000,
        ),
        inline=True,
    )
    profile_parts = []
    if traffic.device_types:
        profile_parts.append(
            "**Dispositivos:** "
            + ", ".join(
                f"{name}: {count}"
                for name, count in traffic.device_types.items()
            )
        )
    if traffic.browsers:
        profile_parts.append(
            "**Navegadores:** "
            + ", ".join(
                f"{name}: {count}"
                for name, count in traffic.browsers.items()
            )
        )
    if traffic.top_countries:
        profile_parts.append(
            "**Países:** "
            + ", ".join(
                f"{name}: {count}"
                for name, count in traffic.top_countries
            )
        )
    embed.add_field(
        name="Perfil de acesso",
        value=_clip(
            "\n".join(profile_parts)
            or "A fonte atual não fornece dispositivo, navegador ou país.",
            1000,
        ),
        inline=True,
    )
    embed.add_field(
        name="Visualizações por dia",
        value=_clip(
            "\n".join(
                f"- **{day}:** {count}"
                for day, count in traffic.daily_pageviews.items()
            )
            or "Nenhuma visualização registrada.",
            1000,
        ),
        inline=True,
    )
    embed.add_field(
        name="Fonte",
        value=f"`{_clip(traffic.source, 900)}`",
        inline=False,
    )
    if traffic.error:
        embed.add_field(
            name="Aviso da última leitura",
            value=_clip(traffic.error, 1000),
            inline=False,
        )
    embed.set_footer(
        text=(
            (
                "Visitas usam as sessões nativas do GA4"
                if traffic.source_kind == "ga4"
                else "Visitas usam as sessões nativas da fonte"
                if traffic.native_sessions
                else f"Visita = sessão de até {traffic.session_minutes} minutos"
            )
            + " | "
            + (
                "nenhum IP é armazenado pela Bella"
                if traffic.source_kind == "ga4"
                else "IP é armazenado somente como hash"
            )
        )
    )
    return embed


def welcome_embed(member_name: str, avatar_url: str | None = None) -> discord.Embed:
    embed = discord.Embed(
        title="Bem-vindo ao Escritório do Talisson Souza",
        description=(
            f"Olá, **{discord.utils.escape_markdown(member_name)}**. "
            "Este é o espaço oficial da comunidade, dos projetos e dos "
            "conteúdos de Talisson Souza sobre tecnologia, produtos digitais "
            "e estratégia de negócios."
        ),
        color=BRAND_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="O que você encontra aqui",
        value=(
            "Programação, sistemas, bots, automações, artigos, análises, "
            "projetos em andamento, serviços e conversas sobre execução real."
        ),
        inline=False,
    )
    embed.add_field(
        name="Nossa proposta",
        value=(
            "Construir com inteligência, evoluir com consistência e agir "
            "com profissionalismo."
        ),
        inline=False,
    )
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    embed.set_footer(text="Mensagem de recepção temporária")
    return embed


def feedback_embed(
    feedback: Feedback,
    detected_at: str,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Novo feedback: {feedback.name}",
        description=_clip(feedback.message, 4000),
        color=BRAND_COLOR,
        url=feedback.source_url,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Pessoa",
        value=feedback.name,
        inline=True,
    )
    embed.add_field(
        name="Cargo ou identificação",
        value=feedback.role or "Não informado",
        inline=True,
    )
    embed.add_field(
        name="Avaliação",
        value=f"{feedback.rating}/5" if feedback.rating else "Não informada",
        inline=True,
    )
    embed.add_field(
        name="Origem",
        value=f"[{feedback.source}]({feedback.source_url})",
        inline=False,
    )
    embed.add_field(
        name="Detecção",
        value=f"<t:{int(_parse_timestamp(detected_at).timestamp())}:F>",
        inline=True,
    )
    embed.add_field(
        name="ID único",
        value=f"`{feedback.feedback_id}`",
        inline=True,
    )
    if feedback.additional_info:
        embed.add_field(
            name="Informações adicionais",
            value=_clip(
                "\n".join(
                    f"- **{key.replace('_', ' ').title()}:** {value}"
                    for key, value in feedback.additional_info.items()
                ),
                1000,
            ),
            inline=False,
        )
    if feedback.avatar_url:
        embed.set_author(name=feedback.name, icon_url=feedback.avatar_url)
        embed.set_thumbnail(url=feedback.avatar_url)
    else:
        embed.set_author(name=feedback.name)
    embed.set_footer(text="Feedback monitorado | horário de envio")
    return embed


def feedback_report_embed(report: FeedbackScanReport) -> discord.Embed:
    color = ERROR_COLOR if report.error else BRAND_COLOR
    description = (
        f"Foram lidos {len(report.feedbacks)} feedbacks e "
        f"{len(report.new_feedbacks)} são novos."
    )
    if report.error:
        description = "A leitura de feedbacks não pôde ser concluída."
    embed = discord.Embed(
        title="Relatório de feedbacks",
        description=description,
        color=color,
        timestamp=_parse_timestamp(report.checked_at),
        url=report.source_url,
    )
    embed.add_field(name="Total atual", value=str(len(report.feedbacks)), inline=True)
    embed.add_field(name="Novos", value=str(len(report.new_feedbacks)), inline=True)
    embed.add_field(
        name="Requisições",
        value=str(len(report.requests)),
        inline=True,
    )
    if report.error:
        embed.add_field(
            name="Estado",
            value="Diagnóstico detalhado disponível apenas no canal técnico.",
            inline=False,
        )
    return embed


def feedback_status_embed(status: FeedbackStatus) -> discord.Embed:
    color = ERROR_COLOR if status.last_error else SUCCESS_COLOR
    embed = discord.Embed(
        title="Status da sincronização de feedbacks",
        description=(
            "Resumo persistente do monitor automático e das sincronizações manuais."
        ),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Encontrados no site",
        value=f"**{status.total_found}**",
        inline=True,
    )
    embed.add_field(
        name="Sincronizados",
        value=f"**{status.total_synchronized}**",
        inline=True,
    )
    embed.add_field(
        name="Pendentes",
        value=f"**{status.pending}**",
        inline=True,
    )
    embed.add_field(
        name="Última sincronização",
        value=_format_report_timestamp(status.last_sync),
        inline=True,
    )
    embed.add_field(
        name="Monitoramento",
        value="Ativo" if status.monitoring_active else "Parado",
        inline=True,
    )
    embed.add_field(
        name="Persistência",
        value="IDs únicos armazenados em `data/state.json`",
        inline=True,
    )
    if status.last_error:
        embed.add_field(
            name="Última ocorrência",
            value="A sincronização mais recente exigiu diagnóstico técnico.",
            inline=False,
        )
    embed.set_footer(text="Bella | feedbacks sem duplicação")
    return embed


def change_embed(change: ContentChange) -> discord.Embed:
    embed = discord.Embed(
        title=f"Alteração detectada: {change.title}",
        description=change.summary,
        color=WARNING_COLOR,
        url=change.url,
        timestamp=_parse_timestamp(change.detected_at),
    )
    if change.added:
        embed.add_field(
            name="Conteúdo adicionado",
            value=_clip("\n".join(f"- {item}" for item in change.added), 1000),
            inline=False,
        )
    if change.removed:
        embed.add_field(
            name="Conteúdo removido",
            value=_clip("\n".join(f"- {item}" for item in change.removed), 1000),
            inline=False,
        )
    if change.modified:
        embed.add_field(
            name="Conteúdo modificado",
            value=_clip("\n".join(f"- {item}" for item in change.modified), 1000),
            inline=False,
        )
    embed.add_field(name="Página de origem", value=change.url, inline=False)
    embed.add_field(name="ID da alteração", value=f"`{change.change_id}`", inline=False)
    embed.set_footer(text="Comparação em linguagem natural")
    return embed


def change_report_embed(report: ChangeScanReport) -> discord.Embed:
    color = WARNING_COLOR if report.changes or report.errors else SUCCESS_COLOR
    embed = discord.Embed(
        title="Relatório de alterações do site",
        description=(
            f"{report.pages_checked} página(s) verificadas, "
            f"{len(report.changes)} alteração(ões) encontrada(s)."
        ),
        color=color,
        timestamp=_parse_timestamp(report.checked_at),
    )
    embed.add_field(
        name="Método de leitura",
        value=report.renderer,
        inline=True,
    )
    embed.add_field(
        name="Requisições",
        value=str(len(report.requests)),
        inline=True,
    )
    embed.add_field(
        name="Ocorrências",
        value=str(len(report.errors)),
        inline=True,
    )
    if report.errors:
        embed.add_field(
            name="Cobertura",
            value=(
                f"{len(report.errors)} página(s) exigem diagnóstico técnico. "
                "Nenhuma exceção interna é exibida neste canal."
            ),
            inline=False,
        )
    return embed


def log_alert_embed(report: LogScanReport, alert: LogAlert) -> discord.Embed:
    color = ERROR_COLOR if alert.severity == "critical" else WARNING_COLOR
    embed = discord.Embed(
        title=f"Alerta de log: {alert.title}",
        description=alert.description,
        color=color,
        timestamp=_parse_timestamp(report.checked_at),
    )
    _add_log_fields(embed, report)
    if alert.evidence:
        embed.add_field(
            name="Evidência",
            value=_clip(alert.evidence, 1000),
            inline=False,
        )
    embed.set_footer(text=f"Chave do alerta: {alert.key}")
    return embed


def log_report_embed(report: LogScanReport) -> discord.Embed:
    embed = discord.Embed(
        title="Resumo do access log",
        description="Última varredura do arquivo de acesso configurado.",
        color=WARNING_COLOR if report.alerts else BRAND_COLOR,
        timestamp=_parse_timestamp(report.checked_at),
    )
    _add_log_fields(embed, report)
    if report.suspicious_samples:
        embed.add_field(
            name="Amostras suspeitas",
            value=_clip("\n".join(report.suspicious_samples[:3]), 1000),
            inline=False,
        )
    return embed


def _add_site_fields(embed: discord.Embed, report: MonitorReport) -> None:
    embed.add_field(
        name="URL",
        value=report.final_url or report.site_url,
        inline=False,
    )
    embed.add_field(
        name="HTTP",
        value=str(report.status_code or "sem resposta"),
        inline=True,
    )
    embed.add_field(
        name="Tempo",
        value=f"{_number(report.response_ms)} ms",
        inline=True,
    )
    embed.add_field(
        name="TLS",
        value=f"{_number(report.tls_days_left)} dias",
        inline=True,
    )
    embed.add_field(
        name="Validação do site",
        value=(
            f"{_verification_label(report.verification_status)}\n"
            f'`meta name="{report.verification_meta_name or "não configurada"}"`'
        ),
        inline=False,
    )
    if report.response_headers:
        embed.add_field(name="Resposta", value="Cabeçalhos validados", inline=False)
    if report.error:
        embed.add_field(
            name="Estado",
            value="A verificação não foi concluída. O diagnóstico está no canal técnico.",
            inline=False,
        )


def _add_log_fields(embed: discord.Embed, report: LogScanReport) -> None:
    embed.add_field(name="Arquivo", value=report.path, inline=False)
    embed.add_field(name="Linhas lidas", value=str(report.lines_read), inline=True)
    embed.add_field(name="Erros HTTP", value=str(report.error_count), inline=True)
    embed.add_field(name="Suspeitas", value=str(report.suspicious_count), inline=True)
    if report.top_ips:
        embed.add_field(
            name="Top IPs",
            value=", ".join(f"{ip}: {count}" for ip, count in report.top_ips),
            inline=False,
        )
    if report.error:
        embed.add_field(name="Erro", value=_clip(report.error, 1000), inline=False)


def _site_executive_summary(report: MonitorReport) -> str:
    if report.error:
        return "A verificação não foi concluída; o diagnóstico foi separado para o canal técnico."
    if report.critical:
        return "O site respondeu, mas há pelo menos um problema crítico ativo."
    if report.alerts:
        return "O site está acessível, com pontos de atenção não críticos."
    return "O site está acessível, validado e sem alertas ativos."


def _advanced_executive_summary(
    metrics: MetricsSnapshot,
    site: MonitorReport | None,
    feedbacks: FeedbackScanReport | None,
    changes: ChangeScanReport | None,
) -> str:
    parts = [
        f"O monitor está ativo há {_format_duration(metrics.uptime_seconds)} "
        f"e realizou {metrics.total_requests} requisição(ões)."
    ]
    if site:
        parts.append(
            "A última verificação principal "
            + ("encontrou alertas." if site.alerts else "não encontrou alertas.")
        )
    if feedbacks:
        parts.append(
            f"A última sincronização identificou {len(feedbacks.new_feedbacks)} "
            "feedback(s) novo(s)."
        )
    if changes:
        parts.append(
            f"A última comparação encontrou {len(changes.changes)} página(s) alterada(s)."
        )
    return " ".join(parts)


def _verification_label(status: str) -> str:
    return {
        "verified": "Confirmada",
        "missing": "Meta tag ausente",
        "mismatch": "Chave divergente",
        "not_configured": "Não configurada",
    }.get(status, status)


def _knowledge_reason_label(reason: str) -> str:
    return {
        "not_question": "Não era uma dúvida",
        "no_knowledge": "Base indisponível",
        "low_match": "Sem resposta compatível",
        "low_confidence": "Confiança insuficiente",
        "cooldown": "Limite temporário do usuário",
        "channel_disabled": "Canal não habilitado",
        "send_error": "Falha de envio",
    }.get(reason, reason.replace("_", " ").title())


def _weekly_daily_lines(
    weekly: WeeklyMetricsSnapshot,
    knowledge: KnowledgeAnalyticsSnapshot,
) -> str:
    days = sorted(set(weekly.daily) | set(knowledge.daily))
    if not days:
        return "Nenhuma atividade registrada no período."
    lines = []
    for day in days:
        monitor = weekly.daily.get(day, {})
        assistant = knowledge.daily.get(day, {})
        lines.append(
            f"- **{day}:** {monitor.get('requests', 0)} req., "
            f"{monitor.get('errors', 0)} erro(s), "
            f"{assistant.get('answered', 0)} resposta(s)"
        )
    return "\n".join(lines)


def _format_report_timestamp(value: str | None) -> str:
    if not value:
        return "n/a"
    timestamp = int(_parse_timestamp(value).timestamp())
    return f"<t:{timestamp}:F>"


def _incident_kind_label(kind: str) -> str:
    return {
        "opened": "aberto",
        "reminder": "lembrete",
        "recovered": "resolvido",
    }.get(kind, kind)


def _number(value: object) -> str:
    return "n/a" if value is None else str(value)


def _format_duration(seconds: int) -> str:
    days, remainder = divmod(max(0, seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}min"
    if hours:
        return f"{hours}h {minutes}min"
    return f"{minutes}min"


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."
