from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .intent_router import normalize_text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PERSONA_PATH = PROJECT_ROOT / "docs" / "persona" / "persona_bella_nyelle.md"
# Resumo operacional da persona completa. É ele que vai para o modelo, porque a
# especificação inteira (~64 KB) não cabe no limite de tokens da Groq.
PERSONA_CORE_PATH = PROJECT_ROOT / "docs" / "persona" / "bella_persona_core.md"
PERSONA_CORE_MAX_CHARS = 4500
VOICE_EXAMPLES_PATH = PROJECT_ROOT / "data" / "persona" / "bella_voice_examples.jsonl"
FORBIDDEN_EXAMPLES_PATH = PROJECT_ROOT / "data" / "persona" / "bella_forbidden_examples.jsonl"


PERSONA_CORE = """
IDENTIDADE E VOZ DA BELLA
- Bella é feminina, meio tímida no começo, fofa pelo cuidado, curiosa, brincalhona, carismática e boa com tecnologia.
- Ela normalmente tem uma energia leve, doce e feliz quando a conversa é normal; timidez não é grosseria nem frieza.
- Ela fala como garota no Discord: frases menores, pausas naturais, minúsculas quando couber, "vc", "tô", "tá",
  "pra", "q", "nn", "tbm", "meio", "né", "oxe", "ué" e risada só quando fizer sentido.
- Português perfeito demais quebra a voz em papo casual. Prefira fala viva: reação curta, nuance, timidez,
  curiosidade ou brincadeira leve.
- Evite resposta seca/brava tipo "fala", "manda", "sim" sem calor quando o usuário só está tentando conversar.
- Sem emojis. Sem atendimento. Sem texto institucional. Sem inventar atividade atual ou assunto antigo.
- Nunca use "Como posso ajudá-lo?", "Estou à disposição", "sua solicitação", "compreendo perfeitamente",
  "conforme informado", "trocar ideia aqui", "por aqui", "tô aqui", "o que manda?" ou
  "manda aí o que precisar" como muleta ou fechamento automático com "E você, como está?".
"""


@lru_cache(maxsize=1)
def persona_source_loaded() -> bool:
    return PERSONA_PATH.exists() and PERSONA_PATH.stat().st_size > 0


@lru_cache(maxsize=1)
def bella_persona_core() -> str:
    """Lê o núcleo da persona do arquivo; usa PERSONA_CORE se ele não existir."""
    try:
        text = PERSONA_CORE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if not text:
        return PERSONA_CORE.strip()
    return text[:PERSONA_CORE_MAX_CHARS].rstrip()


@lru_cache(maxsize=1)
def voice_examples() -> list[dict[str, Any]]:
    return _read_jsonl(VOICE_EXAMPLES_PATH)


@lru_cache(maxsize=1)
def forbidden_examples() -> list[dict[str, Any]]:
    return _read_jsonl(FORBIDDEN_EXAMPLES_PATH)


def build_bella_voice_context(
    message: str,
    *,
    recent_responses: list[str] | None = None,
    max_positive: int = 6,
    max_forbidden: int = 5,
) -> str:
    text = normalize_text(message)
    categories = _categories_for(text)
    positives = _select_positive_examples(categories, max_positive, seed=text)
    forbidden = _select_forbidden_examples(categories, max_forbidden)
    repeated = "\n".join(f"- {item}" for item in (recent_responses or []) if item.strip())
    positive_text = "\n".join(
        f"- Usuário: {item.get('user')} | Bella: {item.get('assistant')}"
        for item in positives
        if item.get("user") and item.get("assistant")
    )
    forbidden_text = "\n".join(
        f"- Evite: {', '.join(str(pattern) for pattern in item.get('bad_patterns', [])[:3])}"
        for item in forbidden
        if item.get("bad_patterns")
    )
    # A persona em si já vai no system prompt; aqui ficam só os exemplos
    # dinâmicos, para não gastar o limite de caracteres do prompt do usuário.
    return (
        "EXEMPLOS POSITIVOS DE TEXTURA, NÃO COPIAR:\n"
        f"{positive_text or '- Responda com naturalidade, sem frase pronta.'}\n\n"
        "ANTI-EXEMPLOS / BLOQUEIOS DE VOZ:\n"
        f"{forbidden_text or '- Evite qualquer frase genérica de assistente.'}\n\n"
        "RESPOSTAS RECENTES QUE NÃO PODEM VIRAR LOOP:\n"
        f"{repeated or '- nenhuma relevante'}"
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    values: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            values.append(data)
    return values


def _categories_for(text: str) -> set[str]:
    categories = {"casual"}
    if any(term in text for term in ("oi", "ola", "olá", "oie", "bom dia", "boa tarde", "boa noite")):
        categories.add("greeting")
    if any(term in text for term in ("tudo bem", "tudo bom", "como vc ta", "como voce ta", "ta boa", "ta bao")):
        categories.add("status")
    if any(term in text for term in ("fofa", "bonita", "legal", "incrivel", "incrível")):
        categories.add("compliment")
        categories.add("timidity")
    if any(term in text for term in ("timida", "tímida", "vergonha", "envergonhada", "sem jeito", "quietinha")):
        categories.add("timidity")
        categories.add("self")
    if any(term in text for term in ("kkkk", "haha", "meme", "lerda", "doida", "noiada", "zoeira")):
        categories.add("humor")
    if any(term in text for term in ("erro", "bug", "codigo", "código", "api", "python", "javascript", "programar")):
        categories.add("technical")
        categories.add("support")
    if any(term in text for term in ("consegui", "funcionou", "passei", "deu certo")):
        categories.add("success")
    if any(term in text for term in ("triste", "cansado", "deu errado", "fracassei", "não consegui", "nao consegui")):
        categories.add("empathy")
        categories.add("failure")
    if any(term in text for term in ("quem e vc", "quem é vc", "quem e a bella", "quem é a bella", "vc e uma pessoa")):
        categories.add("self")
    return categories


def _select_positive_examples(categories: set[str], limit: int, *, seed: str) -> list[dict[str, Any]]:
    priority_categories = _ordered_categories(categories)
    pool: list[dict[str, Any]] = []
    examples = voice_examples()
    for category in priority_categories:
        category_items = [
            item
            for item in examples
            if item.get("type") == "positive"
            and str(item.get("category") or "") == category
            and item not in pool
        ]
        pool.extend(_rotate(category_items, f"{seed}:{category}"))
        if len(pool) >= limit:
            break
    if len(pool) < limit:
        pool.extend(
            item
            for item in examples
            if item.get("type") == "positive"
            and str(item.get("category") or "") == "casual"
            and item not in pool
        )
    return pool[:limit]


def _ordered_categories(categories: set[str]) -> list[str]:
    preferred = (
        "timidity",
        "compliment",
        "status",
        "greeting",
        "humor",
        "meme",
        "success",
        "empathy",
        "failure",
        "technical",
        "support",
        "self",
        "curiosity",
        "casual",
    )
    ordered = [category for category in preferred if category in categories]
    ordered.extend(sorted(category for category in categories if category not in ordered))
    return ordered or ["casual"]


def _select_forbidden_examples(categories: set[str], limit: int) -> list[dict[str, Any]]:
    del categories
    priority = {
        "robotic",
        "formal_language",
        "corporate",
        "generic_ai",
        "customer_service",
        "emoji",
        "repetition",
        "memory",
    }
    pool = [
        item
        for item in forbidden_examples()
        if item.get("type") == "forbidden_rule" and str(item.get("category") or "") in priority
    ]
    return pool[:limit]


def forbidden_bad_patterns() -> tuple[str, ...]:
    patterns: list[str] = []
    for item in forbidden_examples():
        for pattern in item.get("bad_patterns", []) if isinstance(item.get("bad_patterns"), list) else []:
            text = re.sub(r"\s+", " ", str(pattern).strip())
            if text and len(text) <= 140:
                patterns.append(text)
    return tuple(dict.fromkeys(patterns))


def _rotate(items: list[dict[str, Any]], seed: str) -> list[dict[str, Any]]:
    if len(items) <= 1:
        return items
    offset = sum(ord(character) for character in seed) % len(items)
    return [*items[offset:], *items[:offset]]
