from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from website_guard.config import Settings
from website_guard.state import StateStore

from .freellmapi_client import FallbackChatClient, FreeLLMAPIClient, freellmapi_enabled
from .moderation_ai import neutralize_mass_mentions
from .personality import BELLA_SYSTEM_PROMPT

LOGGER = logging.getLogger(__name__)

DEFAULT_MODEL = "openai/gpt-oss-20b"
DEFAULT_VISION_MODEL = "qwen/qwen3.6-27b"
TEXT_MODEL_FALLBACKS = (
    DEFAULT_MODEL,
    "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
)
VISION_MODEL_FALLBACKS = (
    DEFAULT_VISION_MODEL,
)
LEGACY_MODEL_REPLACEMENTS = {
    "meta-llama/llama-4-scout-17b-16e-instruct": DEFAULT_MODEL,
    "llama3-8b-8192": DEFAULT_MODEL,
    "llama3-70b-8192": DEFAULT_MODEL,
}
LEGACY_VISION_MODEL_REPLACEMENTS = {
    "meta-llama/llama-4-scout-17b-16e-instruct": DEFAULT_VISION_MODEL,
}
_INVALID_KEY_MESSAGE = (
    "GROQ_API_KEY inválida. Gere uma nova chave no Groq Console, atualize o "
    "arquivo .env e reinicie o bot."
)


@dataclass(frozen=True)
class MediaInput:
    data: bytes
    mime_type: str
    url: str = ""


class GroqCooldownError(RuntimeError):
    pass


class GroqService:
    """Gateway único para todas as solicitações de IA da Bella."""

    def __init__(
        self,
        settings: Settings,
        state: StateStore,
        *,
        models: tuple[str, ...] | None = None,
        state_prefix: str = "groq",
    ) -> None:
        self.settings = settings
        self.state = state
        configured_model = (
            (models or ("",))[0]
            or getattr(settings, "groq_model", "")
            or os.getenv("GROQ_MODEL", "")
            or DEFAULT_MODEL
        )
        self.model = _normalize_model_id(
            configured_model,
            default=DEFAULT_MODEL,
            replacements=LEGACY_MODEL_REPLACEMENTS,
        )
        self.vision_model = _normalize_model_id(
            (
                str(getattr(settings, "groq_vision_model", "") or "").strip()
                or os.getenv("GROQ_VISION_MODEL", "").strip()
                or DEFAULT_VISION_MODEL
            ),
            default=DEFAULT_VISION_MODEL,
            replacements=LEGACY_VISION_MODEL_REPLACEMENTS,
        )
        self.models = (self.model,)
        self.state_prefix = state_prefix
        self.enabled = _env_bool(
            "GROQ_ENABLED",
            bool(getattr(settings, "groq_enabled", True)),
        )
        self.api_key = (
            str(getattr(settings, "groq_api_key", "") or "").strip()
            or os.getenv("GROQ_API_KEY", "").strip()
        )
        self.timeout_seconds = max(
            3.0,
            float(
                getattr(settings, "groq_timeout_seconds", 0)
                or os.getenv("GROQ_TIMEOUT_SECONDS", "6")
                or "6"
            ),
        )
        self.max_output_tokens = max(
            256,
            min(
                2048,
                int(os.getenv("GROQ_MAX_OUTPUT_TOKENS", "650") or "650"),
            ),
        )
        self.max_json_output_tokens = max(
            512,
            min(
                2048,
                int(os.getenv("GROQ_MAX_JSON_OUTPUT_TOKENS", "1200") or "1200"),
            ),
        )
        self._retry_delay_seconds = max(
            0.1,
            float(os.getenv("GROQ_RETRY_DELAY_SECONDS", "0.4") or "0.4"),
        )
        self._cache_ttl_seconds = max(
            0,
            int(os.getenv("GROQ_CACHE_TTL_SECONDS", "7200") or "7200"),
        )
        self._client: Any = None
        self._client_lock = asyncio.Lock()
        self._request_semaphore = asyncio.Semaphore(
            max(1, min(3, int(os.getenv("GROQ_CONCURRENCY", "2") or "2")))
        )
        self._cooldown_until = 0.0
        self._response_cache: dict[str, tuple[float, str]] = {}
        self._permanent_error: str | None = None
        self._usage_lock = asyncio.Lock()
        self._usage_loaded = False
        self._usage_day = ""
        self._usage_count = 0
        self._daily_request_limit = max(
            0,
            int(os.getenv("GROQ_DAILY_REQUEST_LIMIT", "150") or "150"),
        )
        try:
            self._usage_timezone = ZoneInfo(
                os.getenv(
                    "GROQ_DAILY_RESET_TIMEZONE",
                    "America/Sao_Paulo",
                ).strip()
                or "America/Sao_Paulo"
            )
        except ZoneInfoNotFoundError:
            self._usage_timezone = ZoneInfo("UTC")

    @property
    def configured(self) -> bool:
        return self.enabled

    def _has_groq_key(self) -> bool:
        return bool(self.api_key and not _looks_like_placeholder_key(self.api_key))

    @property
    def available(self) -> bool:
        return bool(
            self.enabled
            and (self._has_groq_key() or freellmapi_enabled())
            and not self._permanent_error
        )

    def has_ready_model(self) -> bool:
        return self.available and time.monotonic() >= self._cooldown_until

    async def initialize(self) -> bool:
        if not self.enabled:
            await self._status("disabled", None)
            return False
        if not self._has_groq_key() and not freellmapi_enabled():
            message = (
                "GROQ_ENABLED está ativo, mas GROQ_API_KEY não foi definida. "
                "O módulo de IA não foi iniciado."
            )
            LOGGER.error(message)
            await self._status("missing_key", message)
            return False
        try:
            await self._get_client()
        except Exception as exc:
            message = f"Groq indisponível: {_safe_error_message(exc)}"
            LOGGER.error(message)
            await self._status("error", message)
            return False
        await self._ensure_available_models()
        await self.reset_daily_usage(force=False)
        await self.state.set_many(
            {
                f"{self.state_prefix}_status": "ready",
                f"{self.state_prefix}_last_error": None,
                f"{self.state_prefix}_model": self.model,
                f"{self.state_prefix}_vision_model": self.vision_model,
            }
        )
        LOGGER.info("Groq configurado com o modelo %s.", self.model)
        return True

    async def _ensure_available_models(self) -> None:
        client = await self._get_client()
        try:
            response = await client.models.list()
        except Exception as exc:
            LOGGER.warning(
                "Não foi possível listar modelos Groq; usando configuração atual: %s",
                _safe_error_message(exc),
            )
            return

        available = {
            str(getattr(model, "id", "") or "").strip()
            for model in getattr(response, "data", []) or []
        }
        available.discard("")
        if not available:
            return

        if self.model not in available:
            replacement = _first_available_model(TEXT_MODEL_FALLBACKS, available)
            if replacement:
                LOGGER.warning(
                    "Modelo Groq %s não está disponível para esta chave; usando %s.",
                    self.model,
                    replacement,
                )
                self.model = replacement
                self.models = (self.model,)

        if self.vision_model not in available:
            replacement = _first_available_model(VISION_MODEL_FALLBACKS, available)
            if replacement:
                LOGGER.warning(
                    "Modelo Groq Vision %s não está disponível para esta chave; usando %s.",
                    self.vision_model,
                    replacement,
                )
                self.vision_model = replacement

    async def generate_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        max_output_tokens: int = 1600,
        timeout_seconds: float | None = None,
        media: list[MediaInput] | None = None,
        model_policy: str | None = None,
        use_cache: bool = True,
        max_attempts: int = 2,
        system_prompt: str | None = None,
        prompt_char_limit: int | None = None,
    ) -> dict[str, Any]:
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        json_prompt = (
            f"{prompt}\n\n"
            "Retorne somente um objeto json válido, sem bloco Markdown e sem texto "
            f"adicional. O objeto deve seguir este json schema:\n{schema_text}"
        )
        text = await self._request(
            json_prompt,
            response_json=True,
            response_schema=schema,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
            media=media,
            model_policy=model_policy,
            use_cache=use_cache,
            max_attempts=max_attempts,
            system_prompt=system_prompt,
            prompt_char_limit=prompt_char_limit,
        )
        data = json.loads(_strip_json_fence(text))
        if not isinstance(data, dict):
            raise ValueError("A Groq retornou uma estrutura JSON inesperada.")
        return data

    async def generate_text(
        self,
        prompt: str,
        *,
        max_output_tokens: int = 900,
        timeout_seconds: float | None = None,
        media: list[MediaInput] | None = None,
        model_policy: str | None = None,
        use_cache: bool = True,
        system_prompt: str | None = None,
        max_attempts: int = 2,
        prompt_char_limit: int | None = None,
    ) -> str:
        return await self._request(
            prompt,
            response_json=False,
            response_schema=None,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
            media=media,
            model_policy=model_policy,
            use_cache=use_cache,
            system_prompt=system_prompt,
            max_attempts=max_attempts,
            prompt_char_limit=prompt_char_limit,
        )

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is not None:
                return self._client
            groq_client = None
            if self._has_groq_key():
                from groq import AsyncGroq

                groq_client = AsyncGroq(
                    api_key=self.api_key,
                    max_retries=0,
                )
            if freellmapi_enabled():
                # FreeLLMAPI na frente (vários provedores grátis), Groq de reserva.
                self._client = FallbackChatClient(
                    FreeLLMAPIClient.from_env(),
                    groq_client,
                )
                LOGGER.info(
                    "IA via FreeLLMAPI em %s%s.",
                    os.getenv("FREELLMAPI_BASE_URL", ""),
                    " com a Groq de reserva" if groq_client else " (sem reserva)",
                )
            else:
                self._client = groq_client
            return self._client

    async def _request(
        self,
        prompt: str,
        *,
        response_json: bool,
        response_schema: dict[str, Any] | None,
        max_output_tokens: int,
        timeout_seconds: float | None,
        media: list[MediaInput] | None,
        model_policy: str | None,
        use_cache: bool,
        system_prompt: str | None = None,
        max_attempts: int = 2,
        prompt_char_limit: int | None = None,
    ) -> str:
        if self._permanent_error:
            raise RuntimeError(self._permanent_error)
        if not self.available:
            raise RuntimeError("O módulo Groq não está disponível.")
        remaining_cooldown = self._cooldown_until - time.monotonic()
        if remaining_cooldown > 0:
            raise GroqCooldownError(
                "A Groq está temporariamente em cooldown. "
                f"Nova tentativa em cerca de {max(1, int(remaining_cooldown))} segundos."
            )

        output_cap = self.max_json_output_tokens if response_json else self.max_output_tokens
        output_limit = min(max(1, int(max_output_tokens)), output_cap)
        prompt = _compact_prompt(
            prompt,
            limit=(
                max(1800, min(16000, int(prompt_char_limit)))
                if prompt_char_limit is not None
                else _prompt_char_limit(
                    model_policy,
                    response_json=response_json,
                    has_media=bool(media),
                )
            ),
        )
        cache_key = self._cache_key(
            prompt=prompt,
            response_json=response_json,
            response_schema=response_schema,
            max_output_tokens=output_limit,
            media=media,
            system_prompt=system_prompt,
        )
        loop = asyncio.get_running_loop()
        if cache_key and use_cache:
            cached = self._response_cache.get(cache_key)
            if cached and cached[0] > loop.time():
                return cached[1]
            if cached:
                self._response_cache.pop(cache_key, None)

        client = await self._get_client()
        user_content = _user_content(prompt, media)
        if response_json:
            user_content = _ensure_json_word_in_content(user_content)
        selected_model = self.vision_model if media else self.model
        output_limit = _minimum_output_for_model(
            selected_model,
            output_limit,
            configured_max=output_cap,
        )
        request_timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else self.timeout_seconds
        )
        request_timeout = max(0.05, request_timeout)
        request_values: dict[str, Any] = {
            "model": selected_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        _json_system_prompt(system_prompt)
                        if response_json
                        else system_prompt or BELLA_SYSTEM_PROMPT
                    ),
                },
                {"role": "user", "content": user_content},
            ],
            "max_tokens": output_limit,
            "temperature": 0.0 if response_json else 0.75,
        }
        _apply_model_request_options(request_values, selected_model)
        if response_json:
            request_values["response_format"] = {"type": "json_object"}

        response: Any = None
        last_error: Exception | None = None
        attempt_count = max(1, min(3, int(max_attempts or 1)))
        for attempt in range(attempt_count):
            try:
                await self._reserve_daily_request()
                async with self._request_semaphore:
                    response = await asyncio.wait_for(
                        client.chat.completions.create(**request_values),
                        timeout=request_timeout,
                    )
                if _response_text(response).strip():
                    break
                last_error = ValueError("A Groq não retornou conteúdo.")
                replacement = _fallback_after_empty_response(
                    selected_model,
                    has_media=bool(media),
                )
                if attempt < attempt_count - 1 and replacement:
                    LOGGER.warning(
                        "Modelo Groq %s retornou vazio; reenviando com %s.",
                        selected_model,
                        replacement,
                    )
                    selected_model = replacement
                    request_values["model"] = selected_model
                    _apply_model_request_options(request_values, selected_model)
                    response = None
                    continue
                response = None
                break
            except Exception as exc:
                response = None
                last_error = exc
                if _is_invalid_api_key_error(exc):
                    self._permanent_error = _INVALID_KEY_MESSAGE
                    last_error = RuntimeError(_INVALID_KEY_MESSAGE)
                    break
                can_retry = attempt < attempt_count - 1
                if can_retry and _is_model_not_found_error(exc):
                    replacement = _fallback_after_missing_model(
                        selected_model,
                        has_media=bool(media),
                    )
                    if replacement:
                        LOGGER.warning(
                            "Modelo Groq %s inacessível; reenviando com %s.",
                            selected_model,
                            replacement,
                        )
                        selected_model = replacement
                        output_limit = _minimum_output_for_model(
                            selected_model,
                            output_limit,
                            configured_max=output_cap,
                        )
                        request_values["model"] = selected_model
                        request_values["max_tokens"] = output_limit
                        _apply_model_request_options(request_values, selected_model)
                        if media:
                            self.vision_model = replacement
                        else:
                            self.model = replacement
                            self.models = (self.model,)
                        continue
                    break
                if can_retry and response_json and _is_json_mode_error(exc):
                    request_values["messages"][0]["content"] = _json_system_prompt(
                        system_prompt
                    )
                    request_values["messages"][1]["content"] = _ensure_json_word_in_content(
                        request_values["messages"][1]["content"]
                    )
                    if _is_json_validation_error(exc):
                        request_values.pop("response_format", None)
                    LOGGER.warning(
                        "Groq recusou JSON estruturado; reenviando com instrução JSON reforçada."
                    )
                    continue
                if can_retry and _is_prompt_too_large_error(exc):
                    prompt = _compact_prompt(
                        prompt,
                        limit=_retry_prompt_char_limit(
                            model_policy,
                            response_json=response_json,
                            has_media=bool(media),
                        ),
                    )
                    output_limit = min(output_limit, 600 if response_json else 256)
                    request_values["messages"][1]["content"] = _user_content(
                        prompt,
                        media,
                    )
                    request_values["max_tokens"] = output_limit
                    LOGGER.warning(
                        "Payload Groq muito grande; reenviando com prompt reduzido."
                    )
                    continue
                if can_retry and _is_transient_error(exc):
                    delay = _retry_after_seconds(
                        exc,
                        default=self._retry_delay_seconds,
                    )
                    LOGGER.warning(
                        "Solicitação Groq falhou temporariamente (%s); "
                        "nova tentativa em %.1f segundo(s).",
                        type(exc).__name__,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                cooldown = _cooldown_seconds(exc)
                if cooldown:
                    self._cooldown_until = time.monotonic() + cooldown
                    LOGGER.warning(
                        "Groq em cooldown por %s segundos após %s.",
                        cooldown,
                        type(exc).__name__,
                    )
                break

        if response is None:
            if isinstance(last_error, (TimeoutError, asyncio.TimeoutError)):
                error: Exception = TimeoutError(
                    f"A Groq não respondeu em até {request_timeout:g} segundos."
                )
            else:
                error = last_error or RuntimeError(
                    "A Groq não retornou uma resposta."
                )
            await self.record_error(error)
            raise error

        text = neutralize_mass_mentions(_response_text(response).strip())
        if selected_model != (self.vision_model if media else self.model):
            if media:
                self.vision_model = selected_model
            else:
                self.model = selected_model
                self.models = (self.model,)

        self._cooldown_until = 0.0
        await self._status("ready", None)
        if cache_key and use_cache and self._cache_ttl_seconds:
            self._response_cache[cache_key] = (
                loop.time() + self._cache_ttl_seconds,
                text,
            )
        return text

    async def reset_daily_usage(self, *, force: bool = True) -> bool:
        today = datetime.now(self._usage_timezone).date().isoformat()
        async with self._usage_lock:
            await self._load_usage_locked()
            if not force and self._usage_day == today:
                return False
            self._usage_day = today
            self._usage_count = 0
            await self._save_usage_locked()
        LOGGER.info(
            "Contador diário local da Groq reiniciado para %s (%s).",
            today,
            self._usage_timezone,
        )
        return True

    def seconds_until_daily_reset(self) -> float:
        now = datetime.now(self._usage_timezone)
        tomorrow = now.date() + timedelta(days=1)
        reset_at = datetime(
            tomorrow.year,
            tomorrow.month,
            tomorrow.day,
            tzinfo=self._usage_timezone,
        )
        return max(1.0, (reset_at - now).total_seconds())

    async def _reserve_daily_request(self) -> None:
        today = datetime.now(self._usage_timezone).date().isoformat()
        async with self._usage_lock:
            await self._load_usage_locked()
            if self._usage_day != today:
                self._usage_day = today
                self._usage_count = 0
            if (
                self._daily_request_limit
                and self._usage_count >= self._daily_request_limit
            ):
                raise GroqCooldownError(
                    "O limite diário local de chamadas à Groq foi atingido. "
                    "O contador será liberado automaticamente à meia-noite."
                )
            self._usage_count += 1
            await self._save_usage_locked()

    async def _load_usage_locked(self) -> None:
        if self._usage_loaded:
            return
        usage = await self.state.get_section("groq_daily_usage")
        self._usage_day = str(usage.get("day") or "")
        try:
            self._usage_count = max(0, int(usage.get("requests") or 0))
        except (TypeError, ValueError):
            self._usage_count = 0
        self._usage_loaded = True

    async def _save_usage_locked(self) -> None:
        await self.state.set_many(
            {
                "groq_daily_usage": {
                    "day": self._usage_day,
                    "requests": self._usage_count,
                    "limit": self._daily_request_limit,
                    "timezone": str(self._usage_timezone),
                },
                "groq_daily_requests": self._usage_count,
            }
        )

    async def record_error(self, exc: Exception) -> None:
        message = _safe_error_message(exc)
        LOGGER.error("Falha na Groq: %s", message)
        await self._status("error", message)

    async def _status(self, status: str, error: str | None) -> None:
        await self.state.set_many(
            {
                f"{self.state_prefix}_status": status,
                f"{self.state_prefix}_last_error": error,
            }
        )

    def _cache_key(
        self,
        *,
        prompt: str,
        response_json: bool,
        response_schema: dict[str, Any] | None,
        max_output_tokens: int,
        media: list[MediaInput] | None,
        system_prompt: str | None,
    ) -> str | None:
        if not self._cache_ttl_seconds or media:
            return None
        payload = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "json": response_json,
                "schema": response_schema,
                "tokens": max_output_tokens,
                "system_prompt": system_prompt or BELLA_SYSTEM_PROMPT,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_model_id(
    value: object,
    *,
    default: str,
    replacements: dict[str, str],
) -> str:
    model = str(value or "").strip() or default
    replacement = replacements.get(model)
    if replacement:
        LOGGER.warning(
            "Modelo Groq %s não é usado nesta configuração; usando %s.",
            model,
            replacement,
        )
        return replacement
    return model


def _first_available_model(
    candidates: tuple[str, ...],
    available: set[str],
) -> str | None:
    for candidate in candidates:
        if candidate in available:
            return candidate
    return None


def _fallback_after_missing_model(
    current_model: str,
    *,
    has_media: bool,
) -> str | None:
    candidates = VISION_MODEL_FALLBACKS if has_media else TEXT_MODEL_FALLBACKS
    for candidate in candidates:
        if candidate != current_model:
            return candidate
    return None


def _fallback_after_empty_response(
    current_model: str,
    *,
    has_media: bool,
) -> str | None:
    if has_media:
        return None
    for candidate in TEXT_MODEL_FALLBACKS:
        if candidate != current_model:
            return candidate
    return None


def _apply_model_request_options(
    request_values: dict[str, Any],
    model: str,
) -> None:
    if model.startswith("openai/gpt-oss"):
        request_values["include_reasoning"] = False
        # gpt-oss é um modelo de raciocínio: sem limite ele gasta o max_tokens
        # pensando, devolve resposta vazia e queima a cota diária (TPD).
        effort = os.getenv("GROQ_REASONING_EFFORT", "low").strip().casefold()
        request_values["reasoning_effort"] = (
            effort if effort in {"low", "medium", "high"} else "low"
        )
    else:
        request_values.pop("include_reasoning", None)
        request_values.pop("reasoning_effort", None)


def _minimum_output_for_model(
    model: str,
    output_limit: int,
    *,
    configured_max: int,
) -> int:
    if model.startswith("openai/gpt-oss"):
        return min(max(output_limit, 256), configured_max)
    return output_limit


def _json_system_prompt(system_prompt: str | None) -> str:
    base = (
        "Você é um gerador de dados estruturados. Responda somente em json "
        "válido, sem markdown, sem comentários, sem texto fora do objeto json. "
        "Use aspas duplas e valores compatíveis com o schema solicitado."
    )
    if not system_prompt:
        return base
    return f"{base}\n\nContexto da tarefa:\n{system_prompt}"


def _ensure_json_word_in_content(content: Any) -> Any:
    suffix = "\n\nImportante: responda em json válido."
    if isinstance(content, str):
        if "json" in content.casefold():
            return content
        return content.rstrip() + suffix
    if isinstance(content, list):
        updated: list[Any] = []
        inserted = False
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text") or "")
                if "json" not in text.casefold():
                    item = {**item, "text": text.rstrip() + suffix}
                inserted = True
            updated.append(item)
        if not inserted:
            updated.insert(0, {"type": "text", "text": "Responda em json válido."})
        return updated
    return content


def _prompt_char_limit(
    model_policy: str | None,
    *,
    response_json: bool,
    has_media: bool,
) -> int:
    configured = int(os.getenv("GROQ_MAX_PROMPT_CHARS", "2600") or "2600")
    if configured > 0:
        return max(1800, configured)
    if has_media:
        return 3200
    if model_policy == "economy":
        return 2600
    return 4200 if response_json else 3600


def _retry_prompt_char_limit(
    model_policy: str | None,
    *,
    response_json: bool,
    has_media: bool,
) -> int:
    return min(
        _prompt_char_limit(
            model_policy,
            response_json=response_json,
            has_media=has_media,
        ),
        2400 if response_json else 1800,
    )


def _compact_prompt(prompt: str, *, limit: int) -> str:
    text = str(prompt or "")
    if len(text) <= limit:
        return text
    head_size = max(600, int(limit * 0.42))
    tail_size = max(600, limit - head_size - 80)
    return (
        text[:head_size].rstrip()
        + "\n\n[contexto intermediário reduzido para caber no limite da Groq]\n\n"
        + text[-tail_size:].lstrip()
    )


def _user_content(
    prompt: str,
    media: list[MediaInput] | None,
) -> str | list[dict[str, Any]]:
    if not media:
        return prompt
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for item in media[:5]:
        mime_type = item.mime_type.strip() or "image/jpeg"
        if not mime_type.startswith("image/"):
            continue
        if item.url.strip().startswith(("https://", "http://")):
            image_url = item.url.strip()
        else:
            encoded = base64.b64encode(item.data).decode("ascii")
            image_url = f"data:{mime_type};base64,{encoded}"
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": image_url,
                },
            }
        )
    return content


def _response_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", "") if message else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        values: list[str] = []
        for item in content:
            if isinstance(item, dict):
                values.append(str(item.get("text") or ""))
            else:
                values.append(str(getattr(item, "text", "") or ""))
        return "".join(values)
    return str(content or "")


def _strip_json_fence(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```")
        stripped = stripped.removesuffix("```")
    return stripped.strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on", "sim"}


def _looks_like_placeholder_key(value: str) -> bool:
    normalized = value.strip().casefold()
    if not normalized:
        return True
    return any(
        marker in normalized
        for marker in (
            "coloque",
            "sua_chave",
            "sua-chave",
            "api_key_aqui",
            "groq_key",
            "xxxx",
            "exemplo",
            "example",
        )
    )


def _is_invalid_api_key_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    message = f"{type(exc).__name__}: {exc}".casefold()
    return status_code in {401, 403} or any(
        marker in message
        for marker in (
            "invalid api key",
            "invalid_api_key",
            "authentication",
            "unauthorized",
            "incorrect api key",
            "groq_api_key inválida",
        )
    )


def _is_model_not_found_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    message = f"{type(exc).__name__}: {exc}".casefold()
    return status_code == 404 and any(
        marker in message
        for marker in (
            "model_not_found",
            "does not exist or you do not have access",
            "modelo não encontrado",
        )
    )


def _is_json_mode_error(exc: Exception) -> bool:
    message = f"{type(exc).__name__}: {exc}".casefold()
    return any(
        marker in message
        for marker in (
            "messages' must contain the word 'json'",
            "must contain the word json",
            "json_validate_failed",
            "failed to validate json",
        )
    )


def _is_json_validation_error(exc: Exception) -> bool:
    message = f"{type(exc).__name__}: {exc}".casefold()
    return "json_validate_failed" in message or "failed to validate json" in message


def _is_prompt_too_large_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    message = f"{type(exc).__name__}: {exc}".casefold()
    return status_code == 413 or any(
        marker in message
        for marker in (
            "payload too large",
            "request too large",
            "tokens per minute",
            "requested",
            "tpm",
        )
    ) and "too large" in message


def _is_transient_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code in {408, 409, 429, 500, 502, 503, 504}:
        return True
    message = f"{type(exc).__name__}: {exc}".casefold()
    return any(
        marker in message
        for marker in (
            "timeout",
            "timed out",
            "rate limit",
            "too many requests",
            "temporarily unavailable",
            "service unavailable",
            "connection error",
            "429",
            "500",
            "502",
            "503",
            "504",
        )
    )


def _retry_after_seconds(exc: Exception, *, default: float) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return max(0.1, min(30.0, float(raw)))
    except (TypeError, ValueError):
        message = f"{type(exc).__name__}: {exc}".casefold()
        match = (
            re.search(r"try again in\s+([0-9]+(?:\.[0-9]+)?)s", message)
            or re.search(r"tente novamente em\s+([0-9]+(?:\.[0-9]+)?)", message)
            or re.search(r"retry after\s+([0-9]+(?:\.[0-9]+)?)", message)
        )
        if match:
            return max(0.1, min(90.0, float(match.group(1))))
        return max(0.1, min(10.0, default))


def _cooldown_seconds(exc: Exception) -> int:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429 or "429" in str(exc):
        return max(10, int(_retry_after_seconds(exc, default=30)) + 2)
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return 5
    if _is_transient_error(exc):
        return 10
    return 0


def _safe_error_message(exc: Exception) -> str:
    if _is_invalid_api_key_error(exc):
        return f"{type(exc).__name__}: {_INVALID_KEY_MESSAGE}"
    message = f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()
    if len(message) > 600:
        message = f"{message[:597]}..."
    return message
