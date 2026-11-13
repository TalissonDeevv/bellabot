from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from website_guard.public_observatory import (
    ObservatoryClient,
    ObservatoryConfig,
    ObservatoryConfigurationError,
    PublicObservatoryCollector,
    command_catalog_event,
    community_snapshot_event,
    heartbeat_event,
    incident_events_from_state,
    serialize_event,
    signature_headers,
)
from website_guard.state import StateStore


class FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def text(self) -> str:
        return self._body


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[dict[str, object]] = []

    def post(self, url: str, **kwargs):
        self.requests.append({"url": url, **kwargs})
        return self.responses.pop(0)


class ObservatoryConfigTests(unittest.TestCase):
    def test_complete_configuration_enables_integration(self) -> None:
        env = {
            "BELLA_OBSERVATORY_URL": "https://bellanyellebot.vercel.app/",
            "BELLA_OBSERVATORY_KEY_ID": "bella-bot-prod",
            "BELLA_OBSERVATORY_SECRET": "s" * 32,
            "BELLA_OBSERVATORY_SYNC_INTERVAL_SECONDS": "420",
        }
        with patch.dict(os.environ, env, clear=True):
            config = ObservatoryConfig.from_env()
        self.assertTrue(config.enabled)
        self.assertEqual(
            config.endpoint,
            "https://bellanyellebot.vercel.app/api/integration/v1/events",
        )
        self.assertNotIn(config.secret, repr(config))
        self.assertEqual(config.sync_interval_seconds, 420)

    def test_enabled_incomplete_configuration_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {"BELLA_OBSERVATORY_ENABLED": "true"},
            clear=True,
        ):
            with self.assertRaises(ObservatoryConfigurationError):
                ObservatoryConfig.from_env()

    def test_plain_http_is_rejected(self) -> None:
        env = {
            "BELLA_OBSERVATORY_URL": "http://example.com",
            "BELLA_OBSERVATORY_KEY_ID": "bella-bot-prod",
            "BELLA_OBSERVATORY_SECRET": "s" * 32,
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ObservatoryConfigurationError):
                ObservatoryConfig.from_env()


class ObservatoryPayloadTests(unittest.TestCase):
    def test_heartbeat_matches_public_contract(self) -> None:
        event = heartbeat_event(
            -10,
            version="2026.10",
            event_id="heartbeat:test",
            occurred_at=datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(event["contractVersion"], 1)
        self.assertEqual(event["type"], "heartbeat")
        self.assertEqual(event["occurredAt"], "2026-10-16T15:00:00Z")
        self.assertEqual(event["payload"]["latencyMs"], 0)
        self.assertEqual(event["payload"]["services"][0]["key"], "discord")

    def test_signature_uses_exact_serialized_body(self) -> None:
        event = heartbeat_event(82, event_id="heartbeat:test")
        raw_body = serialize_event(event)
        headers = signature_headers(
            key_id="bella-bot-prod",
            secret="secret-value",
            raw_body=raw_body,
            timestamp="1792076400",
            nonce="unique-nonce-with-16-chars",
        )
        canonical = b"1792076400.unique-nonce-with-16-chars." + raw_body
        expected = hmac.new(b"secret-value", canonical, hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Bella-Signature"], f"sha256={expected}")

    def test_public_payloads_are_content_addressed(self) -> None:
        commands = [
            {
                "name": "/ingles perfil",
                "description": "Mostra seu perfil de estudos.",
                "category": "Aprenda Inglês com Bella",
                "examples": ["/ingles perfil"],
                "permissions": "Membros do servidor",
                "availability": "Servidor oficial da Bella",
                "restrictions": "Disponibilidade sujeita às configurações do servidor.",
            }
        ]
        first = command_catalog_event(commands)
        second = command_catalog_event(commands)
        self.assertEqual(first["eventId"], second["eventId"])
        community = community_snapshot_event(members=100, interactions=20, events=3)
        self.assertEqual(community["payload"]["members"], 100)

    def test_incident_payload_never_exposes_arbitrary_identifier_characters(self) -> None:
        events = incident_events_from_state(
            {
                "site status/primary": {
                    "scope": "Site Principal",
                    "active": True,
                    "first_seen": 1792076400,
                    "alert": {
                        "title": "Site indisponível",
                        "description": "Falha confirmada no monitor público.",
                        "severity": "critical",
                    },
                }
            }
        )
        self.assertEqual(len(events), 1)
        self.assertNotIn("/", events[0]["payload"]["incidentId"])
        self.assertEqual(events[0]["payload"]["impact"], "major")


class ObservatoryClientTests(unittest.IsolatedAsyncioTestCase):
    def config(self, *, attempts: int = 3) -> ObservatoryConfig:
        return ObservatoryConfig(
            enabled=True,
            base_url="https://bellanyellebot.vercel.app",
            key_id="bella-bot-prod",
            secret="s" * 32,
            max_attempts=attempts,
        )

    async def test_accepted_event_returns_server_response(self) -> None:
        session = FakeSession([FakeResponse(202, '{"accepted":true}')])
        client = ObservatoryClient(self.config())
        result = await client.send_event(session, heartbeat_event(40))
        self.assertEqual(result, {"accepted": True})
        self.assertEqual(len(session.requests), 1)
        self.assertIn("X-Bella-Signature", session.requests[0]["headers"])

    async def test_transient_failure_retries_with_a_new_nonce(self) -> None:
        delays: list[float] = []

        async def record_sleep(delay: float) -> None:
            delays.append(delay)

        session = FakeSession(
            [
                FakeResponse(503, "temporarily unavailable"),
                FakeResponse(202, '{"accepted":true}'),
            ]
        )
        client = ObservatoryClient(self.config(attempts=2), sleep=record_sleep)
        result = await client.send_event(session, heartbeat_event(40))
        self.assertEqual(result, {"accepted": True})
        self.assertEqual(delays, [1])
        first_headers = session.requests[0]["headers"]
        second_headers = session.requests[1]["headers"]
        self.assertNotEqual(first_headers["X-Bella-Nonce"], second_headers["X-Bella-Nonce"])
        self.assertEqual(session.requests[0]["data"], session.requests[1]["data"])


class PublicObservatoryCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.temporary.name) / "state.json")
        self.collector = PublicObservatoryCollector(self.state)

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_completed_day_uses_only_real_aggregate_sources(self) -> None:
        now = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
        yesterday = (now - timedelta(days=1)).astimezone(self.collector.timezone).date().isoformat()
        await self.state.update_section(
            "server_message_statistics",
            "10",
            {"daily": {yesterday: {"messages": 12, "new_members": 2}}},
        )
        await self.state.update_section(
            "autonomous_moderation_metrics",
            "10",
            {"daily": {yesterday: {"incidents": 3, "messages_deleted": 1}}},
        )
        events = await self.collector.completed_daily_events({10}, days=2, now=now)
        self.assertEqual(len(events), 1)
        points = {item["key"]: item["value"] for item in events[0]["payload"]["points"]}
        self.assertEqual(points["interactions_processed"], 12)
        self.assertEqual(points["security_events"], 3)
        self.assertEqual(points["messages_blocked"], 1)
        self.assertNotIn("commands_executed", points)

    async def test_live_outbox_reuses_event_until_acknowledged(self) -> None:
        now = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
        await self.collector.record_command(10, occurred_at=now)
        first = await self.collector.prepare_live_metrics({10}, now=now)
        second = await self.collector.prepare_live_metrics({10}, now=now)
        self.assertIsNotNone(first)
        self.assertEqual(first["eventId"], second["eventId"])
        await self.collector.acknowledge_live_metrics(str(first["eventId"]))
        self.assertIsNone(await self.collector.prepare_live_metrics({10}, now=now))
        next_day = now + timedelta(days=1)
        completed = await self.collector.completed_daily_events({10}, days=2, now=next_day)
        self.assertEqual(completed, [])

    async def test_community_totals_omit_unavailable_activity(self) -> None:
        totals = await self.collector.community_totals({10}, members=55)
        self.assertEqual(totals["members"], 55)
        self.assertIsNone(totals["interactions"])
        self.assertIsNone(totals["events"])


if __name__ == "__main__":
    unittest.main()
