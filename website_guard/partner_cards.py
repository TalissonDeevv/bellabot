from __future__ import annotations

import io
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont, ImageOps


CARD_SIZE = (1652, 952)
BRAND_SAFE_RIGHT = 1325
BRAND_MARK_BOUNDS = (1325, 55, 1610, 270)
BACKGROUND_PATH = (
    Path(__file__).resolve().parent
    / "assets"
    / "partners"
    / "partner-intelligence-background.png"
)
WHITE = (246, 244, 240, 255)
MUTED = (174, 170, 164, 255)
GOLD = (194, 124, 43, 255)
GOLD_LIGHT = (235, 174, 91, 255)
RED = (232, 91, 102, 255)
PANEL = (8, 8, 10, 218)
PANEL_SOFT = (15, 14, 14, 196)
BORDER = (154, 96, 35, 150)


def render_hub_card() -> io.BytesIO:
    image, draw = _canvas()
    _header(
        draw,
        "PROGRAMA DE PARCEIROS",
        "indicações legítimas, acompanhamento transparente e inteligência comercial",
        "50% DE COMISSÃO",
    )
    _kpi_row(
        draw,
        [
            ("COMISSÃO OFICIAL", "50% por projeto"),
            ("RASTREAMENTO", "Link permanente"),
            ("ACOMPANHAMENTO", "Status em tempo real"),
            ("PRIVACIDADE", "Dados protegidos"),
        ],
    )
    _list_panel(
        draw,
        (80, 330, 795, 820),
        "COMO FUNCIONA",
        [
            "Você registra uma oportunidade real e autorizada",
            "A equipe valida o contato e conduz a negociação",
            "O projeto é desenvolvido, entregue e finalizado",
            "A comissão é liberada conforme as regras do programa",
        ],
        accent=GOLD_LIGHT,
    )
    _list_panel(
        draw,
        (835, 330, 1572, 820),
        "PARTNER INTELLIGENCE",
        [
            "Painel pessoal com métricas financeiras e comerciais",
            "Jornada com níveis, conquistas e missões reais",
            "Funil inteligente com gargalos e próxima melhor ação",
            "Simulador de ganhos e kit personalizado de abordagem",
        ],
        accent=GOLD_LIGHT,
    )
    _footer(draw, "Bella Partner Intelligence  •  oportunidades reais, sem spam e sem promessas")
    return _output(image)


def render_profile_card(
    partner_name: str,
    profile: dict[str, Any],
) -> io.BytesIO:
    members = profile.get("members") or {}
    projects = profile.get("projects") or {}
    commissions = profile.get("commissions") or {}
    invite = profile.get("invite") or {}
    revenue = float(projects.get("revenue_total", 0) or 0)
    paid = float(commissions.get("paid", 0) or 0)
    pending = float(commissions.get("pending", 0) or 0)
    image, draw = _canvas()
    _header(
        draw,
        "PERFIL DO PARCEIRO",
        f"{_clean(partner_name, 70)}  •  visão operacional privada",
        f"RANK #{profile.get('rank')}" if profile.get("rank") else "PARTNER PROFILE",
    )
    _kpi_row(
        draw,
        [
            ("ENTRADAS", str(int(members.get("invited_total", 0)))),
            ("CLIENTES", str(int(members.get("clients_total", 0)))),
            ("PROJETOS FECHADOS", str(int(projects.get("closed_projects", 0)))),
            ("EM NEGOCIAÇÃO", str(int(projects.get("negotiating_projects", 0)))),
        ],
    )
    _finance_panel(
        draw,
        (80, 330, 795, 810),
        [
            ("Receita gerada", _money(revenue)),
            ("Comissão recebida", _money(paid)),
            ("Comissão pendente", _money(pending)),
            ("Lucro líquido estimado", _money(max(0.0, revenue - paid - pending))),
        ],
    )
    _list_panel(
        draw,
        (835, 330, 1572, 810),
        "STATUS OPERACIONAL",
        [
            "Link permanente ativo" if invite else "Link permanente ainda não cadastrado",
            f"{int(members.get('active_total', 0))} convidados permanecem ativos",
            f"{int(members.get('left_total', 0))} convidados saíram do servidor",
            "Métricas calculadas somente com registros reais",
        ],
        accent=GOLD_LIGHT,
    )
    _footer(draw, "Perfil privado  •  informações financeiras visíveis somente para o parceiro")
    return _output(image)


def render_invite_card(
    partner_name: str,
    profile: dict[str, Any],
) -> io.BytesIO:
    invite = profile.get("invite") or {}
    members = profile.get("members") or {}
    invited = int(members.get("invited_total", 0))
    active = int(members.get("active_total", 0))
    retention = (active / invited * 100) if invited else None
    image, draw = _canvas()
    _header(
        draw,
        "LINK PERSONALIZADO",
        f"{_clean(partner_name, 70)}  •  rastreamento permanente de entradas",
        "MONITORAMENTO ATIVO" if invite else "CONFIGURAÇÃO",
    )
    _kpi_row(
        draw,
        [
            ("ENTRADAS", str(invited)),
            ("ATIVOS", str(active)),
            ("SAÍRAM", str(int(members.get("left_total", 0)))),
            ("RETENÇÃO", f"{retention:.1f}%".replace(".", ",") if retention is not None else "—"),
        ],
    )
    _text_panel(
        draw,
        (80, 340, 1572, 610),
        "CONVITE MONITORADO",
        str(invite.get("invite_url") or "Nenhum convite permanente foi cadastrado até agora."),
        accent=GOLD_LIGHT,
        font_size=30,
    )
    _list_panel(
        draw,
        (80, 640, 1572, 825),
        "PROTEÇÃO DO RASTREAMENTO",
        [
            "O convite precisa pertencer a este servidor",
            "Não pode possuir expiração ou limite de usos",
            "A Bella compara os usos para identificar cada nova entrada",
        ],
        accent=GOLD_LIGHT,
    )
    _footer(draw, "Link privado  •  compartilhe apenas em contextos relevantes e sem divulgação abusiva")
    return _output(image)


def render_history_card(
    partner_name: str,
    rows: list[dict[str, Any]],
    page: int,
) -> io.BytesIO:
    active = sum(1 for row in rows if row.get("status") == "active")
    clients = sum(1 for row in rows if row.get("is_client"))
    image, draw = _canvas()
    _header(
        draw,
        "HISTÓRICO DO PARCEIRO",
        f"{_clean(partner_name, 70)}  •  página {page + 1}",
        "REGISTROS RASTREADOS",
    )
    _kpi_row(
        draw,
        [
            ("REGISTROS NA PÁGINA", str(len(rows))),
            ("ATIVOS", str(active)),
            ("CLIENTES", str(clients)),
            ("PÁGINA", str(page + 1)),
        ],
    )
    items = []
    for row in rows[:7]:
        status = "ativo" if row.get("status") == "active" else "saiu"
        client = "cliente" if row.get("is_client") else "membro"
        items.append(f"ID {row.get('member_id')}  •  {status}  •  {client}")
    _list_panel(
        draw,
        (80, 330, 1572, 820),
        "MOVIMENTAÇÕES RECENTES",
        items or ["Nenhum registro encontrado nesta página"],
        accent=GOLD_LIGHT,
    )
    _footer(draw, "Histórico privado  •  use os controles abaixo para navegar entre as páginas")
    return _output(image)


def render_rules_card() -> io.BytesIO:
    image, draw = _canvas()
    _header(
        draw,
        "REGRAS DO PROGRAMA",
        "clareza, consentimento e oportunidades reais em todas as etapas",
        "CONDUTA OFICIAL",
    )
    _list_panel(
        draw,
        (80, 300, 795, 810),
        "INDICAÇÕES",
        [
            "Registre somente contatos obtidos legitimamente",
            "Confirme autorização antes de compartilhar dados",
            "Não utilize spam, listas compradas ou informações enganosas",
            "Forneça contexto real sobre a necessidade identificada",
        ],
        accent=GOLD_LIGHT,
    )
    _list_panel(
        draw,
        (835, 300, 1572, 810),
        "COMISSÕES E RESPONSABILIDADES",
        [
            "A indicação precisa ser validada pela equipe",
            "A comissão depende da contratação e finalização do projeto",
            "Negociação, execução, entrega e suporte ficam com a equipe",
            "A participação não cria vínculo trabalhista ou societário",
        ],
        accent=GOLD_LIGHT,
    )
    _footer(draw, "Consulte a página oficial para acessar o regulamento completo e atualizado")
    return _output(image)


def render_evolution_card(
    partner_name: str,
    evolution: dict[str, Any],
) -> io.BytesIO:
    image, draw = _canvas()
    _header(
        draw,
        "JORNADA DO PARCEIRO",
        f"{_clean(partner_name, 70)}  •  evolução calculada com dados reais",
        "PARTNER INTELLIGENCE",
    )
    badges = list(evolution.get("badges") or [])
    missions = list(evolution.get("missions") or [])
    next_level = evolution.get("next_level")
    _kpi_row(
        draw,
        [
            ("NÍVEL ATUAL", str(evolution.get("level") or "Primeiros passos")),
            ("PONTUAÇÃO", str(int(evolution.get("score") or 0))),
            ("CONQUISTAS", str(len(badges))),
            ("MISSÕES ATIVAS", str(len(missions))),
        ],
    )
    current_score = int(evolution.get("score") or 0)
    if next_level:
        target = current_score + int(next_level.get("remaining") or 0)
        progress_label = (
            f"Próximo nível: {next_level.get('name')}  •  "
            f"faltam {int(next_level.get('remaining') or 0)} pontos"
        )
    else:
        target = max(current_score, 1)
        progress_label = "Nível máximo atual alcançado"
    _progress(draw, (80, 310, 1572, 344), current_score, target, progress_label)
    _list_panel(
        draw,
        (80, 390, 795, 820),
        "CONQUISTAS DESBLOQUEADAS",
        badges or ["Sua primeira conquista aparecerá aqui"],
        accent=GOLD_LIGHT,
    )
    _list_panel(
        draw,
        (835, 390, 1572, 820),
        "PRÓXIMAS MISSÕES",
        missions or ["Continue construindo oportunidades legítimas"],
        accent=GOLD_LIGHT,
    )
    _footer(draw, "Bella Partner Intelligence  •  progresso privado e atualizado em tempo real")
    return _output(image)


def render_funnel_card(
    partner_name: str,
    metrics: dict[str, Any],
    diagnosis: dict[str, str],
) -> io.BytesIO:
    image, draw = _canvas()
    _header(
        draw,
        "FUNIL INTELIGENTE",
        f"{_clean(partner_name, 70)}  •  diagnóstico de conversão e retenção",
        "LIVE PIPELINE",
    )
    _kpi_row(
        draw,
        [
            ("INDICAÇÕES", str(int(metrics.get("total") or 0))),
            ("ACEITAS", str(int(metrics.get("accepted") or 0))),
            ("NEGOCIAÇÕES", str(int(metrics.get("negotiation") or 0))),
            ("FINALIZADAS", str(int(metrics.get("closed") or 0))),
        ],
    )
    _metric_panel(
        draw,
        (80, 310, 600, 820),
        "SAÚDE DO FUNIL",
        [
            ("Qualificação", metrics.get("qualification_rate")),
            ("Fechamento", metrics.get("closing_rate")),
            ("Retenção", metrics.get("retention_rate")),
        ],
        confidence=str(metrics.get("confidence") or "Inicial"),
    )
    _text_panel(
        draw,
        (640, 310, 1572, 545),
        "DIAGNÓSTICO DA BELLA",
        diagnosis.get("diagnosis") or "Ainda não há dados suficientes para diagnosticar o funil.",
        accent=GOLD_LIGHT,
        font_size=25,
    )
    _text_panel(
        draw,
        (640, 580, 1572, 820),
        "PRÓXIMA MELHOR AÇÃO",
        diagnosis.get("next_action") or "Registre atividades reais para receber uma recomendação.",
        accent=GOLD_LIGHT,
        font_size=25,
    )
    _footer(draw, "Dados privados  •  nenhuma conversa pessoal é acessada para gerar esta leitura")
    return _output(image)


def render_commission_card(
    project_value: float,
    projects_per_month: int,
    probability: int,
) -> io.BytesIO:
    commission = project_value * 0.5
    monthly = commission * projects_per_month
    weighted = monthly * probability / 100
    image, draw = _canvas()
    _header(
        draw,
        "LABORATÓRIO DE GANHOS",
        "simulação visual baseada na comissão oficial de 50%",
        "CENÁRIOS",
    )
    _kpi_row(
        draw,
        [
            ("VALOR DO PROJETO", _money(project_value)),
            ("COMISSÃO", _money(commission)),
            ("PROJETOS / MÊS", str(projects_per_month)),
            ("PROBABILIDADE", f"{probability}%"),
        ],
    )
    _projection_panel(
        draw,
        (80, 330, 745, 790),
        [
            ("Potencial mensal bruto", _money(monthly)),
            ("Estimativa mensal ponderada", _money(weighted)),
            ("Estimativa anual ponderada", _money(weighted * 12)),
        ],
    )
    _scenario_panel(
        draw,
        (785, 330, 1572, 790),
        monthly,
        probability,
    )
    _footer(draw, "Simulação educacional  •  não representa promessa de contratação ou pagamento")
    return _output(image)


def render_approach_card(
    *,
    target_name: str,
    business_area: str,
    tone: str,
    opening: str,
    follow_up: str,
    handoff: str,
) -> io.BytesIO:
    image, draw = _canvas()
    _header(
        draw,
        "KIT PERSONALIZADO",
        f"abordagem preparada para {_clean(target_name, 45)}  •  {_clean(business_area, 45)}",
        _clean(tone or "NATURAL", 22).upper(),
    )
    _text_panel(
        draw,
        (80, 280, 1572, 425),
        "PRIMEIRO CONTATO",
        opening,
        accent=GOLD_LIGHT,
        font_size=23,
    )
    _text_panel(
        draw,
        (80, 450, 1572, 600),
        "FOLLOW-UP SEM PRESSÃO",
        follow_up,
        accent=GOLD_LIGHT,
        font_size=23,
    )
    _text_panel(
        draw,
        (80, 620, 1572, 840),
        "RESUMO PARA REGISTRO",
        handoff,
        accent=GOLD_LIGHT,
        font_size=19,
    )
    _footer(draw, "Confirme o consentimento antes do envio  •  revise e personalize a mensagem")
    return _output(image)


def _canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    try:
        background = Image.open(io.BytesIO(_background_bytes())).convert("RGBA")
        image = ImageOps.fit(background, CARD_SIZE, method=Image.Resampling.LANCZOS)
    except Exception:
        image = Image.new("RGBA", CARD_SIZE, (4, 4, 5, 255))
    veil = Image.new("RGBA", CARD_SIZE, (0, 0, 0, 72))
    image = Image.alpha_composite(image, veil)
    return image, ImageDraw.Draw(image, "RGBA")


@lru_cache(maxsize=1)
def _background_bytes() -> bytes:
    return BACKGROUND_PATH.read_bytes()


def _header(draw: ImageDraw.ImageDraw, title: str, subtitle: str, tag: str) -> None:
    draw.rounded_rectangle((65, 42, 220, 50), radius=4, fill=GOLD)
    draw.text((65, 70), title, font=_font(48, bold=True), fill=WHITE)
    draw.text((68, 132), subtitle, font=_font(21), fill=MUTED)
    tag_font = _font(16, bold=True)
    tag_width = _text_width(draw, tag, tag_font) + 48
    right = 1325
    draw.rounded_rectangle(
        (right - tag_width, 72, right, 116),
        radius=22,
        fill=(30, 22, 14, 220),
        outline=BORDER,
        width=2,
    )
    _center_text(draw, (right - tag_width, 72, right, 116), tag, tag_font, GOLD_LIGHT)


def _kpi_row(draw: ImageDraw.ImageDraw, cards: Iterable[tuple[str, str]]) -> None:
    cards = list(cards)
    gap = 20
    left = 80
    total_width = BRAND_SAFE_RIGHT - left
    width = int((total_width - gap * (len(cards) - 1)) / max(1, len(cards)))
    for index, (label, value) in enumerate(cards):
        x = left + index * (width + gap)
        draw.rounded_rectangle(
            (x, 195, x + width, 282),
            radius=20,
            fill=PANEL_SOFT,
            outline=BORDER,
            width=2,
        )
        draw.text((x + 20, 212), label, font=_font(14, bold=True), fill=MUTED)
        value_font = _font(25 if len(value) < 22 else 19, bold=True)
        draw.text((x + 20, 242), _clean(value, 32), font=value_font, fill=WHITE)


def _progress(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int, int, int],
    value: int,
    maximum: int,
    label: str,
) -> None:
    left, top, right, bottom = bounds
    draw.rounded_rectangle(bounds, radius=17, fill=(31, 28, 25, 230))
    ratio = min(1.0, max(0.0, value / max(maximum, 1)))
    fill_right = left + max(34, int((right - left) * ratio))
    draw.rounded_rectangle((left, top, fill_right, bottom), radius=17, fill=GOLD)
    draw.text((left, bottom + 12), label, font=_font(17, bold=True), fill=MUTED)


def _list_panel(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int, int, int],
    title: str,
    items: list[str],
    *,
    accent: tuple[int, int, int, int],
) -> None:
    _panel(draw, bounds, title, accent)
    left, top, right, _ = bounds
    y = top + 90
    for item in items[:7]:
        draw.ellipse((left + 30, y + 8, left + 42, y + 20), fill=accent)
        lines = _wrap(draw, _clean(item, 180), _font(21), right - left - 100, 2)
        draw.multiline_text(
            (left + 60, y),
            "\n".join(lines),
            font=_font(21),
            fill=WHITE,
            spacing=6,
        )
        y += max(50, len(lines) * 30 + 18)


def _metric_panel(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int, int, int],
    title: str,
    metrics: list[tuple[str, float | None]],
    *,
    confidence: str,
) -> None:
    _panel(draw, bounds, title, GOLD_LIGHT)
    left, top, right, _ = bounds
    y = top + 105
    for label, value in metrics:
        shown = 0 if value is None else max(0.0, min(100.0, float(value)))
        draw.text((left + 32, y), label, font=_font(19, bold=True), fill=WHITE)
        value_text = "—" if value is None else f"{shown:.1f}%".replace(".", ",")
        value_font = _font(18, bold=True)
        draw.text((right - 32 - _text_width(draw, value_text, value_font), y), value_text, font=value_font, fill=GOLD_LIGHT)
        bar_top = y + 38
        draw.rounded_rectangle((left + 32, bar_top, right - 32, bar_top + 22), radius=11, fill=(42, 38, 34, 220))
        if value is not None:
            bar_right = left + 32 + max(22, int((right - left - 64) * shown / 100))
            draw.rounded_rectangle((left + 32, bar_top, bar_right, bar_top + 22), radius=11, fill=GOLD)
        y += 104
    draw.text((left + 32, y + 10), "CONFIANÇA DA LEITURA", font=_font(14, bold=True), fill=MUTED)
    draw.text((left + 32, y + 40), confidence, font=_font(27, bold=True), fill=WHITE)


def _text_panel(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int, int, int],
    title: str,
    text: str,
    *,
    accent: tuple[int, int, int, int],
    font_size: int,
    value_font_size: int | None = None,
) -> None:
    _panel(draw, bounds, title, accent)
    left, top, right, bottom = bounds
    font = _font(value_font_size or font_size, bold=bool(value_font_size))
    max_lines = max(2, int((bottom - top - 90) / ((value_font_size or font_size) + 9)))
    lines = _wrap(draw, _clean_multiline(text, 1500), font, right - left - 64, max_lines)
    draw.multiline_text(
        (left + 32, top + 72),
        "\n".join(lines),
        font=font,
        fill=WHITE,
        spacing=9,
    )


def _scenario_panel(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int, int, int],
    monthly: float,
    probability: int,
) -> None:
    _panel(draw, bounds, "MAPA DE CENÁRIOS", GOLD_LIGHT)
    left, top, right, _ = bounds
    scenarios = [("Conservador", 25), ("Informado", probability), ("Forte", 75)]
    y = top + 105
    for label, percent in scenarios:
        draw.text((left + 34, y), label, font=_font(20, bold=True), fill=WHITE)
        value = _money(monthly * percent / 100)
        value_font = _font(20, bold=True)
        draw.text((right - 34 - _text_width(draw, value, value_font), y), value, font=value_font, fill=GOLD_LIGHT)
        bar_top = y + 43
        draw.rounded_rectangle((left + 34, bar_top, right - 34, bar_top + 27), radius=13, fill=(42, 38, 34, 220))
        bar_right = left + 34 + max(27, int((right - left - 68) * min(percent, 100) / 100))
        draw.rounded_rectangle((left + 34, bar_top, bar_right, bar_top + 27), radius=13, fill=GOLD)
        draw.text((left + 34, bar_top + 38), f"{percent}% de probabilidade", font=_font(15), fill=MUTED)
        y += 112


def _projection_panel(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int, int, int],
    metrics: list[tuple[str, str]],
) -> None:
    _panel(draw, bounds, "PROJEÇÃO PONDERADA", GOLD_LIGHT)
    left, top, right, _ = bounds
    y = top + 102
    for label, value in metrics:
        draw.text((left + 34, y), label, font=_font(18, bold=True), fill=MUTED)
        draw.text((left + 34, y + 35), value, font=_font(34, bold=True), fill=WHITE)
        y += 108
        if y < bounds[3] - 40:
            draw.line((left + 34, y - 18, right - 34, y - 18), fill=(90, 70, 48, 125), width=1)


def _finance_panel(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int, int, int],
    metrics: list[tuple[str, str]],
) -> None:
    _panel(draw, bounds, "RESUMO FINANCEIRO", GOLD_LIGHT)
    left, top, right, _ = bounds
    y = top + 92
    for label, value in metrics:
        draw.text((left + 34, y), label, font=_font(17, bold=True), fill=MUTED)
        draw.text((left + 34, y + 28), value, font=_font(29, bold=True), fill=WHITE)
        y += 88
        if y < bounds[3] - 25:
            draw.line((left + 34, y - 13, right - 34, y - 13), fill=(90, 70, 48, 125), width=1)


def _panel(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int, int, int],
    title: str,
    accent: tuple[int, int, int, int],
) -> None:
    draw.rounded_rectangle(bounds, radius=28, fill=PANEL, outline=BORDER, width=2)
    left, top, _, _ = bounds
    draw.rounded_rectangle((left + 28, top + 27, left + 35, top + 57), radius=3, fill=accent)
    draw.text((left + 52, top + 27), title, font=_font(20, bold=True), fill=accent)


def _footer(draw: ImageDraw.ImageDraw, text: str) -> None:
    draw.line((80, 870, 1572, 870), fill=(112, 72, 32, 130), width=2)
    draw.text((80, 890), text, font=_font(16), fill=MUTED)


def _wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    paragraphs = text.splitlines() or [text]
    lines: list[str] = []
    for paragraph in paragraphs:
        words = paragraph.split()
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and _text_width(draw, candidate, font) > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        if not words:
            lines.append("")
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and _text_width(draw, f"{last}…", font) > max_width:
            last = last[:-1]
        lines[-1] = f"{last.rstrip()}…"
    return lines or [""]


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    bounds = draw.textbbox((0, 0), text, font=font)
    return bounds[2] - bounds[0]


def _center_text(
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int, int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
) -> None:
    left, top, right, bottom = bounds
    text_bounds = draw.textbbox((0, 0), text, font=font)
    width = text_bounds[2] - text_bounds[0]
    height = text_bounds[3] - text_bounds[1]
    draw.text(
        (left + (right - left - width) / 2, top + (bottom - top - height) / 2 - text_bounds[1]),
        text,
        font=font,
        fill=fill,
    )


@lru_cache(maxsize=64)
def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    filenames = (
        ("C:/Windows/Fonts/seguisb.ttf", "C:/Windows/Fonts/segoeui.ttf")
        if bold
        else ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf")
    )
    linux = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    )
    for path in (*filenames, linux, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _money(value: float) -> str:
    formatted = f"{float(value):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {formatted}"


def _clean(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:limit]


def _clean_multiline(value: Any, limit: int) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in str(value or "").splitlines()]
    return "\n".join(lines).strip()[:limit]


def _output(image: Image.Image) -> io.BytesIO:
    _restore_brand_mark(image)
    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def _restore_brand_mark(image: Image.Image) -> None:
    try:
        background = Image.open(io.BytesIO(_background_bytes())).convert("RGBA")
        background = ImageOps.fit(background, CARD_SIZE, method=Image.Resampling.LANCZOS)
        mark = background.crop(BRAND_MARK_BOUNDS)
        luminance = ImageOps.grayscale(mark)
        mask = luminance.point(lambda value: 255 if value >= 92 else 0)
        image.paste(mark, BRAND_MARK_BOUNDS[:2], mask)
    except Exception:
        return
