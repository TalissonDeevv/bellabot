from __future__ import annotations

from typing import TYPE_CHECKING

from .groq_service import MediaInput

if TYPE_CHECKING:
    from .groq_service import GroqService


async def generate_bella_text(
    service: "GroqService",
    prompt: str,
    *,
    system_prompt: str,
    max_output_tokens: int,
    timeout_seconds: float,
    media: list[MediaInput] | None = None,
    groq_attempts: int = 1,
) -> str:
    """Adapter fino para manter a comunicação com Groq centralizada no GroqService."""

    return await service.generate_text(
        prompt,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        media=media,
        model_policy="economy",
        use_cache=False,
        system_prompt=system_prompt,
        max_attempts=max(2, groq_attempts),
    )
