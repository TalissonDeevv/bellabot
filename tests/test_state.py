from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from website_guard.state import StateStore


class StateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistence_does_not_block_the_event_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            original_save = store._save

            def slow_save() -> None:
                time.sleep(0.2)
                original_save()

            store._save = slow_save  # type: ignore[method-assign]
            started = asyncio.get_running_loop().time()
            task = asyncio.create_task(store.set("large_state", {"value": True}))
            await asyncio.sleep(0.03)
            elapsed = asyncio.get_running_loop().time() - started
            await task

            self.assertLess(elapsed, 0.12)

    async def test_state_is_persisted_in_sqlite_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            first = StateStore(path)
            await first.set("guild_config", {"enabled": True})

            self.assertTrue(path.exists())
            self.assertTrue(path.with_suffix(".db").exists())

            second = StateStore(path)
            self.assertEqual(
                await second.get("guild_config"),
                {"enabled": True},
            )

    async def test_corrupt_sqlite_is_rebuilt_from_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"guild_config":{"enabled":true}}', encoding="utf-8")
            path.with_suffix(".db").write_text("not a sqlite database", encoding="utf-8")

            store = StateStore(path)

            self.assertEqual(
                await store.get("guild_config"),
                {"enabled": True},
            )
            self.assertTrue(path.with_name("state.db.corrupt-1").exists())
            self.assertTrue(path.with_suffix(".db").exists())

    async def test_large_sections_are_migrated_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            original = {
                "guild_config": {"enabled": True},
                "operational_events": {"event-1": {"kind": "deploy"}},
            }
            path.write_text(json.dumps(original), encoding="utf-8")

            store = StateStore(path)

            main = json.loads(path.read_text(encoding="utf-8"))
            sidecar = path.with_name("state_sections") / "operational_events.json"
            backup = path.with_name("state.pre-externalization.backup.json")
            self.assertNotIn("operational_events", main)
            self.assertTrue(sidecar.exists())
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), original)
            self.assertEqual(
                await store.get_section("operational_events"),
                original["operational_events"],
            )

    async def test_external_section_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            first = StateStore(path)
            await first.mutate_section(
                "operational_analysis_cache",
                lambda values: values.update({"cache-1": {"score": 0.9}}),
            )

            second = StateStore(path)

            self.assertEqual(
                await second.get_section("operational_analysis_cache"),
                {"cache-1": {"score": 0.9}},
            )
            self.assertNotIn(
                "operational_analysis_cache",
                json.loads(path.read_text(encoding="utf-8")),
            )


if __name__ == "__main__":
    unittest.main()
