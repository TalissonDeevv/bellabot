"""Cliente da FreeLLMAPI com a Groq direta como reserva.

A FreeLLMAPI (github.com/tashfeenahmed/freellmapi) expõe um endpoint no formato
da OpenAI (/v1/chat/completions) e alterna sozinha entre vários provedores
gratuitos. O SDK da Groq não serve para ela porque chama /openai/v1/..., então
este módulo imita só o pedaço que o GroqService usa:

    client.chat.completions.create(**kwargs)
    client.models.list()

Se a FreeLLMAPI estiver desligada, sem cota (429 quota_exceeded) ou com erro
de servidor, a mesma chamada é repetida na Groq com o modelo original.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import random
import time
import uuid
from types import SimpleNamespace
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)

# Parâmetros que só a Groq entende; outros provedores podem recusar.
GROQ_ONLY_PARAMS = ("include_reasoning", "reasoning_effort")
FALLBACK_STATUSES = {401, 402, 403, 404, 408, 409, 422, 429, 500, 502, 503, 504}


def freellmapi_enabled() -> bool:
    raw = os.getenv("FREELLMAPI_ENABLED", "false").strip().casefold()
    return raw in {"1", "true", "yes", "on", "sim"} and bool(
        os.getenv("FREELLMAPI_BASE_URL", "").strip()
    )


def bella_ai_provider() -> str:
    """Com a FreeLLMAPI ligada, conversa também passa por ela (não pelo Gemini
    direto, que tem cota diária pequena). Sem ela, vale BELLA_AI_PROVIDER."""
    if freellmapi_enabled():
        return "groq"
    return os.getenv("BELLA_AI_PROVIDER", "gemini").strip().casefold()


class OpenAICompatError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None,
        headers: Any = None,
        *,
        connection_failed: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.connection_failed = connection_failed
        self.response = SimpleNamespace(headers=dict(headers or {}))


def _has_image(messages: Any) -> bool:
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "image_url" for part in content
        ):
            return True
    return False


def direct_fallback_enabled() -> bool:
    """Groq direta como reserva da FreeLLMAPI. Desligada por padrão: com a
    FreeLLMAPI ligada, toda a IA passa só por ela."""
    raw = os.getenv("FREELLMAPI_DIRECT_FALLBACK", "false").strip().casefold()
    return raw in {"1", "true", "yes", "on", "sim"}


def _completion_text(payload: Any) -> str:
    try:
        content = payload["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


def _error_kind(exc: "OpenAICompatError") -> str:
    if exc.connection_failed:
        return "connection"
    status = exc.status_code or 0
    if status in {401, 403}:
        return "auth"
    if status == 408:
        return "timeout"
    if status == 429:
        return "rate_limit"
    if "vazia" in str(exc):
        return "empty"
    if "JSON inválido" in str(exc):
        return "invalid_json"
    if status >= 500:
        return "server"
    return f"http_{status}"


def _retry_after_seconds(headers: Any) -> float | None:
    raw = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


class LLMMetrics:
    """Contadores simples em memória (expostos em logs e no /status)."""

    def __init__(self) -> None:
        self.requests = 0
        self.successes = 0
        self.failures = 0
        self.retries = 0
        self.by_kind: dict[str, int] = {}
        self.total_latency = 0.0

    def record_request(self, ok: bool, seconds: float) -> None:
        self.requests += 1
        self.total_latency += seconds
        if ok:
            self.successes += 1
        else:
            self.failures += 1

    def record_failure(self, kind: str) -> None:
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "retries": self.retries,
            "attempt_failures": dict(self.by_kind),
            "avg_latency_ms": int(1000 * self.total_latency / self.requests) if self.requests else 0,
        }


METRICS = LLMMetrics()
# Quem está conversando (servidor:canal:pessoa), para os logs de cada chamada.
LLM_CONVERSATION_ID: contextvars.ContextVar[str] = contextvars.ContextVar("llm_conversation_id", default="-")


def _to_namespace(payload: Any) -> Any:
    if isinstance(payload, dict):
        return SimpleNamespace(**{key: _to_namespace(value) for key, value in payload.items()})
    if isinstance(payload, list):
        return [_to_namespace(item) for item in payload]
    return payload


class FreeLLMAPIClient:
    """Cliente mínimo para um endpoint /v1 compatível com a OpenAI."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        text_model: str = "auto",
        json_model: str = "auto",
        vision_model: str = "auto",
        timeout_seconds: float = 20.0,
        attempt_timeout_seconds: float = 10.0,
        total_timeout_seconds: float | None = None,
        max_retries: int = 2,
        backoff_base_seconds: float = 0.6,
        max_retry_after_seconds: float = 8.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if not self.base_url.endswith("/v1"):
            self.base_url += "/v1"
        self.api_key = api_key.strip()
        # Cada um aceita uma lista em ordem, separada por vírgula:
        # "gemini-2.5-flash,gpt-oss-120b". Modelo específico responde em ~1 s;
        # "auto" testa o catálogo inteiro e pode passar de 20 s.
        self.text_model = text_model or "auto"
        self.json_model = json_model or self.text_model
        self.vision_model = vision_model or self.text_model
        self.attempt_timeout_seconds = max(2.0, attempt_timeout_seconds)
        self.total_timeout_seconds = max(self.attempt_timeout_seconds, float(total_timeout_seconds or timeout_seconds * 2))
        self.max_retries = max(0, int(max_retries))
        self.backoff_base_seconds = max(0.0, float(backoff_base_seconds))
        self.max_retry_after_seconds = max_retry_after_seconds
        self._http = httpx.AsyncClient(timeout=timeout_seconds)
        self.last_routed_via = ""
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=self._list_models)

    @classmethod
    def from_env(cls) -> "FreeLLMAPIClient":
        text_model = os.getenv("FREELLMAPI_MODEL", "auto").strip() or "auto"
        return cls(
            base_url=os.getenv("FREELLMAPI_BASE_URL", "http://127.0.0.1:3001/v1"),
            api_key=os.getenv("FREELLMAPI_API_KEY", ""),
            text_model=text_model,
            json_model=os.getenv("FREELLMAPI_JSON_MODEL", "").strip() or text_model,
            vision_model=os.getenv("FREELLMAPI_VISION_MODEL", "").strip() or text_model,
            timeout_seconds=float(os.getenv("FREELLMAPI_TIMEOUT_SECONDS", "20") or 20),
            attempt_timeout_seconds=float(
                os.getenv("FREELLMAPI_ATTEMPT_TIMEOUT_SECONDS", "10") or 10
            ),
            total_timeout_seconds=float(os.getenv("FREELLMAPI_TOTAL_TIMEOUT_SECONDS", "40") or 40),
            max_retries=int(os.getenv("FREELLMAPI_MAX_RETRIES", "2") or 2),
            backoff_base_seconds=float(os.getenv("FREELLMAPI_BACKOFF_SECONDS", "0.6") or 0.6),
        )

    def _model_for(self, kwargs: dict[str, Any]) -> str:
        if _has_image(kwargs.get("messages")):
            return self.vision_model
        if kwargs.get("response_format"):
            return self.json_model
        return self.text_model

    def _models_for(self, kwargs: dict[str, Any]) -> list[str]:
        models = [item.strip() for item in self._model_for(kwargs).split(",")]
        return [item for item in models if item] or ["auto"]

    async def _create(self, **kwargs: Any) -> Any:
        """Tenta cada modelo da lista, com nova tentativa em falha temporária.

        - 408/5xx/conexão/resposta vazia ou inválida: espera exponencial com
          jitter (ou o Retry-After do servidor, se for curto) e tenta de novo.
        - 429 com reset longo (cota do dia): não espera, vai para o próximo modelo.
        - 401/403: chave errada; nenhum modelo vai funcionar, falha na hora.
        Tudo respeita um prazo total, para nenhuma mensagem ficar presa.
        """
        request_id = uuid.uuid4().hex[:10]
        conversation = LLM_CONVERSATION_ID.get()
        deadline = time.monotonic() + self.total_timeout_seconds
        started = time.monotonic()
        attempts = 0
        last_error: OpenAICompatError | None = None
        for model in self._models_for(kwargs):
            for retry in range(self.max_retries + 1):
                if time.monotonic() >= deadline:
                    break
                attempts += 1
                attempt_started = time.monotonic()
                try:
                    response = await self._create_once(
                        model, kwargs, timeout=min(self.attempt_timeout_seconds, max(1.0, deadline - time.monotonic()))
                    )
                except OpenAICompatError as exc:
                    last_error = exc
                    kind = _error_kind(exc)
                    METRICS.record_failure(kind)
                    LOGGER.warning(
                        "llm_request id=%s conv=%s model=%s attempt=%s status=%s kind=%s ms=%s erro=%s",
                        request_id, conversation, model, attempts, exc.status_code, kind,
                        int((time.monotonic() - attempt_started) * 1000), str(exc)[:200],
                    )
                    if kind == "auth":
                        METRICS.record_request(False, time.monotonic() - started)
                        raise
                    wait = self._retry_wait(exc, retry)
                    if wait is None or retry >= self.max_retries:
                        break  # próximo modelo
                    if time.monotonic() + wait >= deadline:
                        break
                    METRICS.retries += 1
                    await asyncio.sleep(wait)
                    continue
                METRICS.record_request(True, time.monotonic() - started)
                LOGGER.info(
                    "llm_request id=%s conv=%s model=%s via=%s attempt=%s status=200 ms=%s",
                    request_id, conversation, model, self.last_routed_via or "?", attempts,
                    int((time.monotonic() - started) * 1000),
                )
                return response
            if last_error is not None and last_error.connection_failed:
                break  # FreeLLMAPI fora do ar: outro modelo não ajuda
        METRICS.record_request(False, time.monotonic() - started)
        if last_error is None:
            last_error = OpenAICompatError("FreeLLMAPI: prazo total esgotado", 408)
        raise last_error

    def _retry_wait(self, exc: "OpenAICompatError", retry: int) -> float | None:
        """Segundos até a próxima tentativa no mesmo modelo, ou None para pular."""
        status = exc.status_code
        retry_after = _retry_after_seconds(exc.response.headers)
        if status == 429:
            if retry_after is not None and retry_after <= self.max_retry_after_seconds:
                return retry_after
            return None  # cota do modelo acabou; o próximo da lista resolve
        kind = _error_kind(exc)
        if kind in {"timeout", "empty"}:
            return None  # modelo lento ou mudo: o próximo da lista é mais rápido
        if exc.connection_failed or status in {500, 502, 503, 504}:
            if retry_after is not None:
                return min(retry_after, self.max_retry_after_seconds)
            base = self.backoff_base_seconds * (2 ** retry)
            return min(base, 8.0) * random.uniform(0.5, 1.5)
        return None

    async def _create_once(self, model: str, kwargs: dict[str, Any], *, timeout: float | None = None) -> Any:
        body = {key: value for key, value in kwargs.items() if key not in GROQ_ONLY_PARAMS}
        body["model"] = model
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            response = await self._http.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers=headers,
                timeout=timeout or self.attempt_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise OpenAICompatError(f"FreeLLMAPI timeout em {model}: {exc}", 408) from exc
        except httpx.HTTPError as exc:
            raise OpenAICompatError(
                f"FreeLLMAPI connection error: {exc}", 503, connection_failed=True
            ) from exc
        if response.status_code >= 400:
            try:
                error = response.json().get("error", {})
                detail = error.get("message") or error.get("code") or response.text
            except ValueError:
                detail = response.text
            raise OpenAICompatError(
                f"FreeLLMAPI {response.status_code}: {str(detail)[:300]}",
                response.status_code,
                response.headers,
            )
        self.last_routed_via = response.headers.get("x-routed-via", "")
        try:
            payload = response.json()
        except ValueError as exc:
            raise OpenAICompatError(f"FreeLLMAPI {model}: JSON inválido", 502) from exc
        if not _completion_text(payload).strip():
            # 200 sem texto (ex.: modelo que "não viu" a imagem): conta como falha
            # para a lista seguir para o próximo modelo.
            raise OpenAICompatError(f"FreeLLMAPI {model}: resposta vazia", 502)
        return _to_namespace(payload)

    async def _list_models(self) -> Any:
        return SimpleNamespace(data=[])

    async def aclose(self) -> None:
        await self._http.aclose()


class FallbackChatClient:
    """Tenta a FreeLLMAPI primeiro; em falha, repete a chamada na Groq."""

    def __init__(self, primary: FreeLLMAPIClient, secondary: Any | None) -> None:
        self.primary = primary
        self.secondary = secondary
        self.last_provider = ""
        self._primary_down_until = 0.0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=self._list_models)

    async def _create(self, **kwargs: Any) -> Any:
        if time.monotonic() >= self._primary_down_until or self.secondary is None:
            try:
                response = await self.primary.chat.completions.create(**kwargs)
                self.last_provider = f"freellmapi:{self.primary.last_routed_via or '?'}"
                return response
            except OpenAICompatError as exc:
                if self.secondary is None or exc.status_code not in FALLBACK_STATUSES:
                    raise
                LOGGER.warning("FreeLLMAPI falhou (%s); usando a Groq direta.", exc)
                if exc.connection_failed:
                    # Fora do ar: não insiste em toda mensagem durante 60 s.
                    self._primary_down_until = time.monotonic() + 60
        response = await self.secondary.chat.completions.create(**kwargs)
        self.last_provider = "groq"
        return response

    async def _list_models(self) -> Any:
        if self.secondary is not None:
            return await self.secondary.models.list()
        return SimpleNamespace(data=[])
