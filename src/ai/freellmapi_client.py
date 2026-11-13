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

import logging
import os
import time
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
        """Tenta cada modelo da lista; passa para o próximo em limite, erro ou demora."""
        last_error: OpenAICompatError | None = None
        for model in self._models_for(kwargs):
            try:
                return await self._create_once(model, kwargs)
            except OpenAICompatError as exc:
                last_error = exc
                if exc.connection_failed or exc.status_code in {401, 403}:
                    raise  # FreeLLMAPI fora do ar ou chave errada: outro modelo não ajuda
                LOGGER.info("FreeLLMAPI: %s falhou (%s); tentando o próximo.", model, exc)
        assert last_error is not None
        raise last_error

    async def _create_once(self, model: str, kwargs: dict[str, Any]) -> Any:
        body = {key: value for key, value in kwargs.items() if key not in GROQ_ONLY_PARAMS}
        body["model"] = model
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            response = await self._http.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers=headers,
                timeout=self.attempt_timeout_seconds,
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
        payload = response.json()
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
