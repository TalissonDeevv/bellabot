from __future__ import annotations

import os
from dataclasses import dataclass

from .freellmapi_client import freellmapi_enabled
from .intent_router import normalize_text


@dataclass(frozen=True)
class BellaGenerationRules:
    max_output_tokens: int
    max_response_chars: int
    timeout_seconds: float
    groq_attempts: int
    regenerate_invalid: bool
    instruction: str


def social_generation_rules(message: str, *, has_media: bool = False) -> BellaGenerationRules:
    """Define limites rápidos para conversa social da Bella.

    A conversa social é o ponto em que a sensação de "digita e para" mais aparece.
    Por isso o caminho feliz usa uma chamada curta. Se a Groq devolver voz robótica,
    o validador pode pedir uma única regeneração, sem loop.
    """

    text = normalize_text(message)
    high_energy = _high_energy(message)
    asks_help = any(term in text for term in ("ajuda", "erro", "bug", "problema", "nao entendi", "não entendi"))
    tokens = 140 if asks_help or has_media else 88
    chars = 750 if asks_help or has_media else 420
    timeout = 4.8 if has_media else (3.2 if asks_help else 2.6)
    if freellmapi_enabled():
        # A FreeLLMAPI pode trocar de provedor no meio do caminho; 2,6 s é pouco.
        timeout = max(timeout, 8.0)
    return BellaGenerationRules(
        max_output_tokens=max(64, min(220, tokens)),
        max_response_chars=chars,
        timeout_seconds=float(os.getenv("BELLA_SOCIAL_TIMEOUT_SECONDS", timeout) or timeout),
        groq_attempts=max(1, min(2, int(os.getenv("BELLA_SOCIAL_GROQ_ATTEMPTS", "1") or "1"))),
        regenerate_invalid=os.getenv("BELLA_REGENERATE_INVALID", "true").strip().casefold()
        in {"1", "true", "yes", "on", "sim"},
        instruction=(
            "Responda curta, natural e com energia alta, acompanhando o caps/zoeira do usuário."
            if high_energy
            else "Responda curta e natural, sem cara de atendimento e sem português perfeito demais."
        ),
    )


def _high_energy(message: str) -> bool:
    letters = [char for char in message if char.isalpha()]
    if len(letters) >= 5 and sum(char.isupper() for char in letters) / len(letters) >= 0.65:
        return True
    return message.count("!") >= 2
