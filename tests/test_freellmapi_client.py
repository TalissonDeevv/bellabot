from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from src.ai.freellmapi_client import (
    FallbackChatClient,
    FreeLLMAPIClient,
    OpenAICompatError,
    bella_ai_provider,
    freellmapi_enabled,
)

ENABLED_ENV = {
    "FREELLMAPI_ENABLED": "true",
    "FREELLMAPI_BASE_URL": "http://127.0.0.1:3001/v1",
    "FREELLMAPI_API_KEY": "freellmapi-teste",
    "FREELLMAPI_MODEL": "auto",
    "FREELLMAPI_JSON_MODEL": "auto:smart",
    "FREELLMAPI_VISION_MODEL": "auto:vision",
}


def _client(handler) -> tuple[FreeLLMAPIClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = FreeLLMAPIClient(
        base_url="http://127.0.0.1:3001",
        api_key="freellmapi-teste",
        text_model="auto",
        json_model="auto:smart",
        vision_model="auto:vision",
    )
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    return client, seen


def _ok(content: str = "oi") -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
        headers={"x-routed-via": "cerebras/llama"},
    )


class FreeLLMAPIClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_shape_and_model_routing(self) -> None:
        client, seen = _client(lambda request: _ok("tudo certo"))
        response = await client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[{"role": "user", "content": "oi"}],
            max_tokens=100,
            include_reasoning=False,
            reasoning_effort="low",
        )
        self.assertEqual(response.choices[0].message.content, "tudo certo")
        self.assertEqual(seen[0].url.path, "/v1/chat/completions")
        self.assertEqual(seen[0].headers["authorization"], "Bearer freellmapi-teste")
        body = json.loads(seen[0].content)
        self.assertEqual(body["model"], "auto")
        self.assertNotIn("include_reasoning", body)
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual(client.last_routed_via, "cerebras/llama")

        await client.chat.completions.create(
            model="x", messages=[], response_format={"type": "json_object"}
        )
        self.assertEqual(json.loads(seen[1].content)["model"], "auto:smart")
        await client.chat.completions.create(
            model="x",
            messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "u"}}]}],
        )
        self.assertEqual(json.loads(seen[2].content)["model"], "auto:vision")

    async def test_quota_error_keeps_status_and_retry_after(self) -> None:
        client, _ = _client(
            lambda request: httpx.Response(
                429,
                json={"error": {"code": "quota_exceeded", "message": "sem cota"}},
                headers={"retry-after": "30"},
            )
        )
        with self.assertRaises(OpenAICompatError) as caught:
            await client.chat.completions.create(model="x", messages=[])
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.response.headers["retry-after"], "30")

    async def test_fallback_to_groq_when_freellmapi_is_down(self) -> None:
        def down(request):
            raise httpx.ConnectError("recusado", request=request)

        primary, seen = _client(down)
        groq_calls: list[dict] = []

        async def groq_create(**kwargs):
            groq_calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="da groq"))])

        groq = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=groq_create)))
        client = FallbackChatClient(primary, groq)
        kwargs = {"model": "openai/gpt-oss-120b", "messages": [], "reasoning_effort": "low"}
        response = await client.chat.completions.create(**kwargs)
        self.assertEqual(response.choices[0].message.content, "da groq")
        self.assertEqual(groq_calls[0], kwargs)  # Groq recebe o pedido original
        self.assertEqual(client.last_provider, "groq")
        # Fora do ar: o próximo pedido vai direto para a Groq, sem esperar.
        await client.chat.completions.create(**kwargs)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(groq_calls), 2)

    async def test_without_groq_key_errors_surface(self) -> None:
        primary, _ = _client(lambda request: httpx.Response(503, json={"error": {"message": "x"}}))
        with self.assertRaises(OpenAICompatError):
            await FallbackChatClient(primary, None).chat.completions.create(model="x", messages=[])


class SwitchTests(unittest.IsolatedAsyncioTestCase):
    def test_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {"FREELLMAPI_ENABLED": "false", "BELLA_AI_PROVIDER": "gemini"}):
            self.assertFalse(freellmapi_enabled())
            self.assertEqual(bella_ai_provider(), "gemini")

    def test_enabled_routes_conversation_and_raises_social_timeout(self) -> None:
        from src.ai.bella_generation_rules import social_generation_rules

        with patch.dict(os.environ, {**ENABLED_ENV, "BELLA_AI_PROVIDER": "gemini"}):
            os.environ.pop("BELLA_SOCIAL_TIMEOUT_SECONDS", None)
            self.assertTrue(freellmapi_enabled())
            self.assertEqual(bella_ai_provider(), "groq")
            self.assertGreaterEqual(social_generation_rules("oi bella").timeout_seconds, 8.0)

    async def test_groq_service_works_with_only_freellmapi(self) -> None:
        from src.ai.groq_service import GroqService
        from website_guard.state import StateStore

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {**ENABLED_ENV, "GROQ_API_KEY": "", "GROQ_ENABLED": "true"}
        ):
            service = GroqService(SimpleNamespace(), StateStore(Path(directory) / "s.json"))
            self.assertTrue(service.available)
            client = await service._get_client()
            self.assertIsInstance(client, FallbackChatClient)
            self.assertIsNone(client.secondary)


if __name__ == "__main__":
    unittest.main()
