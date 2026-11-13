from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-flash-lite-latest"


class BellaGeminiClient:
    """Cliente Gemini dedicado à conversa social da Bella.

    A personalidade fica no `system_instruction`, e o prompt do turno entra como
    input. O client é isolado para não misturar regra de voz com infraestrutura.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        enabled: bool,
        timeout_seconds: float,
    ) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip() or DEFAULT_GEMINI_MODEL
        self.enabled = bool(enabled)
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self._client: Any | None = None
        self._cooldown_until = 0.0
        self._sdk_error: str = ""
        concurrency = max(
            1,
            min(4, int(os.getenv("GEMINI_CONCURRENCY", "2") or "2")),
        )
        self._request_semaphore = asyncio.Semaphore(concurrency)
        self._executor = ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="bella-gemini",
        )

    @classmethod
    def from_env(cls) -> "BellaGeminiClient":
        api_key = (
            os.getenv("GEMINI_API_KEY", "").strip()
            or os.getenv("GOOGLE_API_KEY", "").strip()
        )
        enabled = _env_bool("GEMINI_ENABLED", bool(api_key))
        return cls(
            api_key=api_key,
            model=os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
            or DEFAULT_GEMINI_MODEL,
            enabled=enabled,
            timeout_seconds=float(os.getenv("GEMINI_TIMEOUT_SECONDS", "3.0") or "3.0"),
        )

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.api_key)

    @property
    def available(self) -> bool:
        return self.configured and time.monotonic() >= self._cooldown_until

    async def generate_text(
        self,
        prompt: str,
        *,
        system_prompt: str,
        max_output_tokens: int,
        timeout_seconds: float | None = None,
    ) -> str:
        if not self.available:
            return ""
        request_timeout = max(0.5, float(timeout_seconds or self.timeout_seconds))
        loop = asyncio.get_running_loop()
        await self._request_semaphore.acquire()
        release_now = True
        future = loop.run_in_executor(
            self._executor,
            functools.partial(
                self._generate_sync,
                    prompt,
                    system_prompt=system_prompt,
                    max_output_tokens=max_output_tokens,
            ),
        )
        try:
            answer = await asyncio.wait_for(
                asyncio.shield(future),
                timeout=request_timeout,
            )
            if not answer.strip():
                LOGGER.warning("Gemini social da Bella retornou uma resposta vazia.")
            return answer
        except Exception as exc:
            if _looks_rate_limited(exc):
                self._cooldown_until = time.monotonic() + _cooldown_seconds(exc)
            elif isinstance(exc, asyncio.TimeoutError):
                self._cooldown_until = time.monotonic() + _timeout_cooldown_seconds()
                release_now = False
                future.add_done_callback(
                    lambda _future: loop.call_soon_threadsafe(
                        self._request_semaphore.release
                    )
                )
            LOGGER.warning("Gemini social da Bella falhou: %s", _safe_error(exc))
            return ""
        finally:
            if release_now:
                self._request_semaphore.release()

    def _generate_sync(
        self,
        prompt: str,
        *,
        system_prompt: str,
        max_output_tokens: int,
    ) -> str:
        client = self._get_client()
        generation_config = {
            "temperature": float(os.getenv("GEMINI_TEMPERATURE", "0.92") or "0.92"),
            "top_p": float(os.getenv("GEMINI_TOP_P", "0.94") or "0.94"),
            "max_output_tokens": max(16, min(256, int(max_output_tokens))),
            "thinking_level": os.getenv("GEMINI_THINKING_LEVEL", "low").strip()
            or "low",
        }
        if hasattr(client, "interactions"):
            try:
                interaction = client.interactions.create(
                    model=self.model,
                    system_instruction=system_prompt,
                    input=prompt,
                )
            except TypeError:
                return self._generate_content_fallback(
                    client,
                    prompt,
                    system_prompt=system_prompt,
                    generation_config=generation_config,
                )
            return _response_text(interaction)
        if hasattr(client, "models"):
            return self._generate_content_fallback(
                client,
                prompt,
                system_prompt=system_prompt,
                generation_config=generation_config,
            )
        return ""

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from google import genai  # type: ignore
        except Exception as exc:
            self._sdk_error = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(
                "Biblioteca google-genai não instalada. Rode `pip install -r requirements.txt`."
            ) from exc
        self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _generate_content_fallback(
        self,
        client: Any,
        prompt: str,
        *,
        system_prompt: str,
        generation_config: dict[str, Any],
    ) -> str:
        try:
            from google.genai import types  # type: ignore

            config = types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=generation_config["temperature"],
                top_p=generation_config["top_p"],
                max_output_tokens=generation_config["max_output_tokens"],
            )
            response = client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=config,
            )
        except Exception:
            response = client.models.generate_content(
                model=self.model,
                contents=prompt,
            )
        return _response_text(response)


def _response_text(response: Any) -> str:
    direct = getattr(response, "output_text", None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    direct = getattr(response, "text", None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    candidates = getattr(response, "candidates", None) or []
    values: list[str] = []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                values.append(str(text))
    return "".join(values).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on", "sim"}


def _looks_rate_limited(exc: Exception) -> bool:
    text = str(exc).casefold()
    return "429" in text or "rate" in text or "quota" in text or "resource_exhausted" in text


def _cooldown_seconds(exc: Exception) -> int:
    text = str(exc).casefold()
    retry_after = _retry_after_seconds(text)
    if "perday" in text or "per day" in text or "requests per day" in text:
        return max(
            retry_after,
            int(os.getenv("GEMINI_DAILY_QUOTA_COOLDOWN_SECONDS", "900") or "900"),
        )
    if "quota" in text or "resource_exhausted" in text:
        return max(
            retry_after,
            int(os.getenv("GEMINI_QUOTA_COOLDOWN_SECONDS", "180") or "180"),
        )
    return max(retry_after, 20)


def _retry_after_seconds(text: str) -> int:
    matches = [
        int(value)
        for value in re.findall(r"(?:retry(?:delay| after| in)?['\"\s:=]+)(\d+)\s*s", text)
        if value.isdigit()
    ]
    matches.extend(
        int(value)
        for value in re.findall(r"try again in\s+(\d+)\s*s", text)
        if value.isdigit()
    )
    return min(max(matches or [0]), 3600)


def _timeout_cooldown_seconds() -> int:
    return max(3, int(os.getenv("GEMINI_TIMEOUT_COOLDOWN_SECONDS", "8") or "8"))


def _safe_error(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    for secret in (
        os.getenv("GEMINI_API_KEY", ""),
        os.getenv("GOOGLE_API_KEY", ""),
    ):
        if secret:
            message = message.replace(secret, "[secret]")
    return message[:500]
