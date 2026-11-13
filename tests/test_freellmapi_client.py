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
        backoff_base_seconds=0,
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

    async def test_model_list_moves_to_next_on_quota_or_slowness(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.content)["model"]
            if model == "gemini-2.5-flash":
                return httpx.Response(429, json={"error": {"message": "sem cota"}})
            if model == "lento":
                raise httpx.ReadTimeout("demorou", request=request)
            return _ok(f"veio do {model}")

        client, seen = _client(handler)
        client.text_model = "gemini-2.5-flash, lento ,gpt-oss-120b"
        response = await client.chat.completions.create(model="x", messages=[])
        self.assertEqual(response.choices[0].message.content, "veio do gpt-oss-120b")
        self.assertEqual(
            [json.loads(r.content)["model"] for r in seen],
            ["gemini-2.5-flash", "lento", "gpt-oss-120b"],
        )

    async def test_empty_answer_moves_to_next_model(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.content)["model"]
            return _ok("" if model == "vazio" else "certo")

        client, seen = _client(handler)
        client.text_model = "vazio,gpt-oss-120b"
        response = await client.chat.completions.create(model="x", messages=[])
        self.assertEqual(response.choices[0].message.content, "certo")
        self.assertEqual(len(seen), 2)

    async def test_model_list_stops_when_freellmapi_is_down(self) -> None:
        def down(request):
            raise httpx.ConnectError("recusado", request=request)

        client, seen = _client(down)
        client.text_model = "a,b,c"
        with self.assertRaises(OpenAICompatError):
            await client.chat.completions.create(model="x", messages=[])
        # Conexão recusada: tenta de novo no mesmo modelo (backoff), não nos outros.
        self.assertEqual(len(seen), 1 + client.max_retries)

    async def test_server_error_is_retried_with_backoff(self) -> None:
        answers = iter([httpx.Response(503, json={"error": {"message": "ocupado"}}), _ok("voltou")])
        client, seen = _client(lambda request: next(answers))
        response = await client.chat.completions.create(model="x", messages=[])
        self.assertEqual(response.choices[0].message.content, "voltou")
        self.assertEqual(len(seen), 2)

    async def test_short_retry_after_is_honored(self) -> None:
        answers = iter([httpx.Response(429, json={"error": {"message": "calma"}}, headers={"retry-after": "0"}), _ok("ok")])
        client, seen = _client(lambda request: next(answers))
        response = await client.chat.completions.create(model="x", messages=[])
        self.assertEqual(response.choices[0].message.content, "ok")
        self.assertEqual(len(seen), 2)

    async def test_auth_error_fails_fast(self) -> None:
        client, seen = _client(lambda request: httpx.Response(401, json={"error": {"message": "chave"}}))
        client.text_model = "a,b"
        with self.assertRaises(OpenAICompatError) as caught:
            await client.chat.completions.create(model="x", messages=[])
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(len(seen), 1)

    async def test_invalid_json_moves_to_next_model(self) -> None:
        def handler(request):
            if json.loads(request.content)["model"] == "a":
                return httpx.Response(200, content=b"<html>proxy</html>")
            return _ok("do b")

        client, seen = _client(handler)
        client.text_model = "a,b"
        response = await client.chat.completions.create(model="x", messages=[])
        self.assertEqual(response.choices[0].message.content, "do b")

    async def test_total_deadline_stops_retries(self) -> None:
        client, seen = _client(lambda request: httpx.Response(500, json={"error": {"message": "x"}}))
        client.text_model = "a,b,c,d,e"
        client.total_timeout_seconds = 0.0
        with self.assertRaises(OpenAICompatError):
            await client.chat.completions.create(model="x", messages=[])
        self.assertEqual(len(seen), 0)

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
        self.assertEqual(len(seen), 1 + primary.max_retries)
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

    async def test_direct_groq_is_off_unless_enabled(self) -> None:
        from src.ai.groq_service import GroqService
        from website_guard.state import StateStore

        for flag, expect_groq in (("false", False), ("true", True)):
            with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ,
                {**ENABLED_ENV, "GROQ_API_KEY": "gsk_Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4zAb7cDe0fGh2", "GROQ_ENABLED": "true",
                 "FREELLMAPI_DIRECT_FALLBACK": flag},
            ):
                service = GroqService(SimpleNamespace(), StateStore(Path(directory) / "s.json"))
                client = await service._get_client()
                self.assertEqual(client.secondary is not None, expect_groq, flag)


if __name__ == "__main__":
    unittest.main()
