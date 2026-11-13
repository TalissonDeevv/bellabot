from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from website_guard.incidents import IncidentManager
from website_guard.monitor import MonitorAlert
from website_guard.state import StateStore


class IncidentManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        state_path = Path(self.temp_dir.name) / "state.json"
        self.manager = IncidentManager(StateStore(state_path))
        self.alert = MonitorAlert(
            key="verification_missing",
            title="Tag ausente",
            description="A tag não foi encontrada.",
            severity="critical",
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_incident_only_repeats_after_reminder_and_recovers_once(self) -> None:
        opened = await self.manager.evaluate("site", [self.alert], 3600)
        self.assertEqual([event.kind for event in opened], ["opened"])
        await self.manager.mark_notified(opened[0])

        duplicate = await self.manager.evaluate("site", [self.alert], 3600)
        self.assertEqual(duplicate, [])

        recovered = await self.manager.evaluate("site", [], 3600)
        self.assertEqual([event.kind for event in recovered], ["recovered"])
        await self.manager.mark_notified(recovered[0])

        duplicate_recovery = await self.manager.evaluate("site", [], 3600)
        self.assertEqual(duplicate_recovery, [])


if __name__ == "__main__":
    unittest.main()
