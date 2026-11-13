from __future__ import annotations

import json
import hmac
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from website_guard.state import StateStore
from website_guard.traffic import (
    TrafficAnalytics,
    TrafficSnapshot,
    verify_vercel_signature,
)


def _settings(path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        traffic_log_path=path,
        traffic_hosts=["www.talissonsouza.com.br", "talissonsouza.com.br"],
        traffic_retention_days=45,
        traffic_max_events=100_000,
        traffic_session_minutes=30,
        traffic_ingest_enabled=False,
        traffic_ingest_host="127.0.0.1",
        traffic_ingest_port=8787,
        traffic_ingest_path="/vercel-analytics",
        ga4_property_id="",
        ga4_credentials_path=None,
        ga4_cache_minutes=30,
    )


class TrafficAnalyticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_ga4_is_primary_source_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = _settings(root / "traffic.ndjson")
            settings.ga4_property_id = "123456"
            expected = TrafficSnapshot(
                available=True,
                source="Google Analytics 4 (property 123456)",
                period_start="2026-06-05",
                period_end="2026-06-11",
                pageviews=120,
                visits=80,
                unique_visitors=70,
                source_kind="ga4",
                native_sessions=True,
            )
            analytics = TrafficAnalytics(
                settings,
                StateStore(root / "state.json"),
            )
            with patch(
                "website_guard.traffic._fetch_ga4_snapshot",
                return_value=expected,
            ) as fetch:
                snapshot = await analytics.snapshot()
                cached = await analytics.snapshot()

            self.assertEqual(snapshot, expected)
            self.assertEqual(cached, expected)
            fetch.assert_called_once()

    async def test_source_file_is_created_and_ingest_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "nested" / "traffic.ndjson"
            settings = _settings(log_path)
            settings.traffic_ingest_enabled = True
            analytics = TrafficAnalytics(
                settings,
                StateStore(root / "state.json"),
            )
            self.assertTrue(await analytics.ensure_source())
            self.assertTrue(log_path.exists())

            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            body = json.dumps(
                [
                    {
                        "schema": "vercel.analytics.v2",
                        "eventType": "pageview",
                        "timestamp": now_ms,
                        "projectId": "project-1",
                        "sessionId": 10,
                        "deviceId": 20,
                        "origin": "https://www.talissonsouza.com.br",
                        "path": "/contato",
                    }
                ]
            ).encode()
            self.assertEqual(await analytics.ingest_body(body), 1)
            self.assertIn('"path":"/contato"', log_path.read_text(encoding="utf-8"))
            self.assertEqual((await analytics.snapshot()).pageviews, 1)
            self.assertEqual(await analytics.scan(), 0)

    async def test_vercel_signature_is_verified(self) -> None:
        body = b'[{"eventType":"pageview"}]'
        secret = "drain-secret"
        signature = hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha1,
        ).hexdigest()
        self.assertTrue(verify_vercel_signature(body, signature, secret))
        self.assertFalse(verify_vercel_signature(body, "0" * 40, secret))

    async def test_combined_log_counts_real_pages_and_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "access.log"
            now = datetime.now(timezone.utc)
            timestamp_a = now.strftime("%d/%b/%Y:%H:%M:%S %z")
            timestamp_b = (now + timedelta(minutes=10)).strftime(
                "%d/%b/%Y:%H:%M:%S %z"
            )
            lines = [
                (
                    f'203.0.113.10 - - [{timestamp_a}] "GET / HTTP/1.1" 200 123 '
                    '"https://google.com/" "Mozilla/5.0 Chrome/125"'
                ),
                (
                    f'203.0.113.10 - - [{timestamp_b}] "GET /sobre?utm=x HTTP/1.1" '
                    '200 123 "-" "Mozilla/5.0 Chrome/125"'
                ),
                (
                    f'203.0.113.11 - - [{timestamp_a}] "GET /assets/app.js HTTP/1.1" '
                    '200 123 "-" "Mozilla/5.0 Chrome/125"'
                ),
                (
                    f'203.0.113.12 - - [{timestamp_a}] "GET /faq HTTP/1.1" 200 123 '
                    '"-" "Googlebot/2.1"'
                ),
            ]
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            analytics = TrafficAnalytics(
                _settings(log_path),
                StateStore(root / "state.json"),
            )

            self.assertEqual(await analytics.scan(), 4)
            snapshot = await analytics.snapshot()

            self.assertTrue(snapshot.available)
            self.assertEqual(snapshot.pageviews, 2)
            self.assertEqual(snapshot.visits, 1)
            self.assertEqual(snapshot.unique_visitors, 1)
            self.assertEqual(snapshot.bot_requests, 1)
            self.assertEqual(snapshot.top_pages[0], ("/", 1))
            self.assertIn(("google.com", 1), snapshot.top_referrers)

    async def test_vercel_ndjson_is_supported_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "vercel.ndjson"
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            event = {
                "id": "vercel-log-1",
                "source": "static",
                "host": "www.talissonsouza.com.br",
                "timestamp": now_ms,
                "proxy": {
                    "timestamp": now_ms,
                    "method": "GET",
                    "host": "www.talissonsouza.com.br",
                    "path": "/feedbacks?ref=discord",
                    "userAgent": ["Mozilla/5.0 Firefox/126"],
                    "referer": "https://discord.com/channels/1/2",
                    "statusCode": 200,
                    "clientIp": "198.51.100.20",
                },
            }
            log_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
            state = StateStore(root / "state.json")
            analytics = TrafficAnalytics(_settings(log_path), state)

            await analytics.scan()
            await state.update_section("traffic_offsets", str(log_path), 0)
            await analytics.scan()
            snapshot = await analytics.snapshot()

            self.assertEqual(snapshot.records_processed, 1)
            self.assertEqual(snapshot.pageviews, 1)
            self.assertEqual(snapshot.top_pages, [("/feedbacks", 1)])
            self.assertEqual(snapshot.top_referrers, [("discord.com", 1)])

    async def test_vercel_web_analytics_uses_native_session_and_device(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "analytics.ndjson"
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            events = [
                {
                    "schema": "vercel.analytics.v2",
                    "eventType": "pageview",
                    "timestamp": now_ms,
                    "projectId": "project-1",
                    "sessionId": 100,
                    "deviceId": 200,
                    "origin": "https://www.talissonsouza.com.br",
                    "path": "/",
                    "referrer": "https://google.com/",
                    "country": "BR",
                    "clientName": "Chrome",
                    "deviceType": "desktop",
                },
                {
                    "schema": "vercel.analytics.v2",
                    "eventType": "pageview",
                    "timestamp": now_ms + 60_000,
                    "projectId": "project-1",
                    "sessionId": 100,
                    "deviceId": 200,
                    "origin": "https://www.talissonsouza.com.br",
                    "path": "/faq",
                    "country": "BR",
                    "clientName": "Chrome",
                    "deviceType": "desktop",
                },
            ]
            log_path.write_text(json.dumps(events, indent=2), encoding="utf-8")
            analytics = TrafficAnalytics(
                _settings(log_path),
                StateStore(root / "state.json"),
            )

            await analytics.scan()
            snapshot = await analytics.snapshot()

            self.assertEqual(snapshot.pageviews, 2)
            self.assertEqual(snapshot.visits, 1)
            self.assertEqual(snapshot.unique_visitors, 1)
            self.assertTrue(snapshot.native_sessions)
            self.assertEqual(snapshot.device_types, {"desktop": 2})
            self.assertEqual(snapshot.browsers, {"Chrome": 2})
            self.assertEqual(snapshot.top_countries, [("BR", 2)])


if __name__ == "__main__":
    unittest.main()
