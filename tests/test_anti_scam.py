from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from website_guard.anti_scam import (
    AntiScamResult,
    AntiScamVision,
    antiscam_status_embed,
)
from website_guard.state import StateStore


class FakeService:
    available = True


class FakeAudit:
    async def channel_id_for(self, guild_id: int, severity: str) -> int | None:
        return None


class AntiScamTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_config_is_disabled_and_extensible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            config_path = Path(directory) / "antiscam.json"
            config_path.write_text(
                '{"enabled": false, "categories": ["promo code"]}',
                encoding="utf-8",
            )
            vision = AntiScamVision(
                FakeService(),
                state,
                FakeAudit(),
                config_path=config_path,
            )

            config = await vision.config()
            enabled = await vision.set_enabled(True)

            self.assertFalse(config.enabled)
            self.assertIn("promo code", config.categories)
            self.assertTrue(enabled.enabled)

    async def test_hash_cache_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            vision = AntiScamVision(FakeService(), state, FakeAudit())
            result = AntiScamResult(
                classification="SCAM",
                confidence=0.97,
                reason="Promo code e saque falso.",
            )

            await vision._store_cache("abc", result)
            cached = await vision._cached("abc")
            await vision._touch_cache("abc")
            records = await state.get_section("anti_scam_hash_cache")

            self.assertEqual(cached.classification, "SCAM")
            self.assertEqual(records["abc"]["hits"], 2)

    def test_status_embed_does_not_claim_active_when_disabled(self) -> None:
        embed = antiscam_status_embed(
            SimpleNamespace(
                enabled=False,
                geminiEnabled=True,
                confidenceThreshold=0.88,
                cacheEnabled=True,
                logChannelId=0,
            ),
            cache_entries=0,
            events=0,
            last_error=None,
        )

        self.assertIn("desativada", embed.description.casefold())


if __name__ == "__main__":
    unittest.main()
