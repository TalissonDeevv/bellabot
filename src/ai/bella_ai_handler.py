from __future__ import annotations

import logging
import os
from dataclasses import replace
from typing import Callable

from website_guard.state import StateStore

from .bella_generation_rules import social_generation_rules
from .freellmapi_client import bella_ai_provider
from .bella_gemini_client import BellaGeminiClient
from .bella_groq_client import generate_bella_text
from .bella_prompt_builder import build_bella_social_prompt
from .bella_response_validator import validate_bella_response
from .groq_service import GroqService, MediaInput
from .personality import (
    BELLA_GEMINI_PUBLIC_SYSTEM_PROMPT,
    BELLA_GEMINI_SOCIAL_SYSTEM_PROMPT,
    BELLA_SOCIAL_SYSTEM_PROMPT,
)

LOGGER = logging.getLogger(__name__)


class BellaAIHandler:
    """Orquestra a resposta social da Bella sem duplicar o pipeline principal."""

    def __init__(self, service: GroqService, state: StateStore) -> None:
        self.service = service
        self.state = state
        self.gemini = BellaGeminiClient.from_env()

    @property
    def available(self) -> bool:
        provider = bella_ai_provider()
        if provider in {"gemini", "google", "google-gemini"}:
            return bool(
                self.gemini.available
                or (_env_bool("BELLA_FALLBACK_TO_GROQ", True) and self.service.available)
            )
        return bool(self.service.available or self.gemini.available)

    async def generate_social_reply(
        self,
        message: str,
        *,
        display_name: str,
        recent_context: list[dict[str, str]],
        attachment_context: list[str] | None = None,
        media: list[MediaInput] | None = None,
        deescalate: bool = False,
        style_instruction: str = "",
        user_facts: dict[str, str] | None = None,
        extra_validation: Callable[[str], tuple[str, ...]] | None = None,
    ) -> str:
        if not self.available:
            return ""
        rules = social_generation_rules(message, has_media=bool(media))
        prompt = build_bella_social_prompt(
            message=message,
            display_name=display_name,
            channel_history=recent_context,
            reference_context=attachment_context,
            has_media=bool(media),
            deescalate=deescalate,
            style_instruction=style_instruction,
            generation_instruction=rules.instruction,
            user_facts=user_facts,
        )
        try:
            answer = await self._generate_bella_raw(
                prompt,
                media=media,
                max_output_tokens=rules.max_output_tokens,
                timeout_seconds=rules.timeout_seconds,
                groq_attempts=rules.groq_attempts,
            )
        except Exception as exc:
            await self.state.set(
                "bella_last_social_recovery_error",
                f"{type(exc).__name__}: {exc}",
            )
            return ""
        validation = _with_extra_reasons(
            validate_bella_response(
                message,
                answer,
                recent_responses=[item.get("text", "") for item in recent_context if item.get("author", "").casefold().find("bella") >= 0],
                max_chars=rules.max_response_chars,
            ),
            extra_validation,
        )
        if validation.acceptable:
            return validation.cleaned
        await self.state.set(
            "bella_last_rejected_social_response",
            {
                "message": message[:300],
                "answer": validation.cleaned[:500],
                "reasons": list(validation.reasons),
            },
        )
        if not rules.regenerate_invalid:
            return ""
        repair_prompt = (
            f"{prompt}\n\n"
            "A resposta anterior foi rejeitada pelo validador da Bella por estes motivos: "
            f"{', '.join(validation.reasons)}.\n"
            "Gere outra resposta mais natural, curta, sem frase pronta, sem mencionar formato "
            "interno e sem copiar exemplos."
        )
        try:
            repaired = await self._generate_bella_raw(
                repair_prompt,
                media=media,
                max_output_tokens=rules.max_output_tokens,
                timeout_seconds=rules.timeout_seconds,
                groq_attempts=1,
            )
        except Exception as exc:
            LOGGER.debug("Regeneração social da Bella falhou: %s: %s", type(exc).__name__, exc)
            return ""
        repaired_validation = _with_extra_reasons(
            validate_bella_response(
                message,
                repaired,
                recent_responses=[validation.cleaned],
                max_chars=rules.max_response_chars,
            ),
            extra_validation,
        )
        return repaired_validation.cleaned if repaired_validation.acceptable else ""

    async def generate_public_text_reply(
        self,
        prompt: str,
        *,
        user_message: str,
        recent_context: list[dict[str, str]] | None = None,
        max_output_tokens: int = 360,
        max_chars: int = 1800,
        timeout_seconds: float = 3.8,
    ) -> str:
        provider = bella_ai_provider()
        if provider not in {"gemini", "google", "google-gemini"} or not self.gemini.available:
            return ""
        answer = await self.gemini.generate_text(
            prompt,
            system_prompt=BELLA_GEMINI_PUBLIC_SYSTEM_PROMPT,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        validation = validate_bella_response(
            user_message,
            answer,
            recent_responses=[
                item.get("text", "")
                for item in (recent_context or [])
                if item.get("author", "").casefold().find("bella") >= 0
            ],
            max_chars=max_chars,
        )
        if validation.acceptable:
            await self.state.set("bella_last_public_provider", "gemini")
            return validation.cleaned
        await self.state.set(
            "bella_last_rejected_public_response",
            {
                "message": user_message[:300],
                "answer": validation.cleaned[:500],
                "reasons": list(validation.reasons),
            },
        )
        return ""

    async def _generate_bella_raw(
        self,
        prompt: str,
        *,
        media: list[MediaInput] | None = None,
        max_output_tokens: int,
        timeout_seconds: float,
        groq_attempts: int,
    ) -> str:
        provider = bella_ai_provider()
        can_use_gemini = (
            provider in {"gemini", "google", "google-gemini"}
            and not media
            and self.gemini.available
        )
        if can_use_gemini:
            try:
                answer = await self.gemini.generate_text(
                    prompt,
                    system_prompt=BELLA_GEMINI_SOCIAL_SYSTEM_PROMPT,
                    max_output_tokens=max_output_tokens,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:
                LOGGER.warning(
                    "Gemini indisponível; tentando Groq como fallback da Bella: %s: %s",
                    type(exc).__name__,
                    exc,
                )
                answer = ""
            if answer:
                await self.state.set("bella_last_social_provider", "gemini")
                return answer
            if not _env_bool("BELLA_FALLBACK_TO_GROQ", True):
                return ""
        if (
            provider in {"gemini", "google", "google-gemini"}
            and not _env_bool("BELLA_FALLBACK_TO_GROQ", True)
        ):
            return ""
        if not self.service.available:
            return ""
        answer = await generate_bella_text(
            self.service,
            prompt,
            system_prompt=BELLA_SOCIAL_SYSTEM_PROMPT,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
            media=media,
            groq_attempts=groq_attempts,
        )
        if answer:
            await self.state.set("bella_last_social_provider", "groq")
        return answer


def _with_extra_reasons(validation, extra_validation):
    """Soma ao validador as checagens sociais específicas da conversa atual."""
    if extra_validation is None or not validation.cleaned:
        return validation
    extra = tuple(extra_validation(validation.cleaned) or ())
    if not extra:
        return validation
    reasons = tuple(dict.fromkeys((*validation.reasons, *extra)))
    return replace(validation, acceptable=False, reasons=reasons)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on", "sim"}
