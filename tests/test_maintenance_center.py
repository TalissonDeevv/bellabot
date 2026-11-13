from __future__ import annotations

import unittest

from website_guard.maintenance_center import MaintenanceCenter, default_config, normalize_config
from website_guard.public_observatory import heartbeat_event


class FakeState:
    def __init__(self) -> None:
        self.values = {}

    async def get(self, key, default=None):
        return self.values.get(key, default)

    async def set(self, key, value):
        self.values[key] = value


class FakePresence:
    def __init__(self) -> None:
        self.value = {
            "mode": "automatic",
            "manual_status": "online",
            "manual_activity_type": "custom",
            "manual_text": "Original",
            "manual_url": "",
        }
        self.applied = 0

    async def config(self):
        return dict(self.value)

    async def save(self, config, *, actor_id=None):
        self.value = dict(config)
        return self.value

    async def reset_defaults(self, *, actor_id=None):
        self.value = {"mode": "automatic"}

    async def apply(self, *, force=False):
        self.applied += 1
        return True


class MaintenanceCenterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.state = FakeState()
        self.presence = FakePresence()
        self.center = MaintenanceCenter(
            object(),
            self.state,
            self.presence,
            object(),
            authorize=lambda _: True,
        )

    async def test_activate_snapshots_and_applies_presence(self) -> None:
        original = await self.presence.config()
        config = await self.center.activate(mode="improvements", actor_id=42)
        self.assertTrue(config["enabled"])
        self.assertEqual(config["previous_presence"]["mode"], original["mode"])
        self.assertEqual(self.presence.value["mode"], "manual")
        self.assertEqual(self.presence.value["manual_status"], "dnd")
        self.assertGreater(self.presence.applied, 0)

    async def test_deactivate_restores_exact_previous_presence(self) -> None:
        original = await self.presence.config()
        await self.center.activate(mode="maintenance", actor_id=42)
        config = await self.center.deactivate(actor_id=42)
        self.assertFalse(config["enabled"])
        self.assertEqual(self.presence.value["mode"], original["mode"])
        self.assertEqual(self.presence.value["manual_text"], original["manual_text"])

    def test_normalization_rejects_invalid_presence_values(self) -> None:
        config = normalize_config({"enabled": True, "status": "invalid", "activity_type": "invalid"})
        self.assertEqual(config["status"], "dnd")
        self.assertEqual(config["activity_type"], "watching")

    def test_default_is_operational(self) -> None:
        self.assertFalse(default_config()["enabled"])

    def test_maintenance_heartbeat_marks_service(self) -> None:
        event = heartbeat_event(31, state="maintenance", event_id="heartbeat:maintenance-test")
        self.assertEqual(event["payload"]["state"], "maintenance")
        self.assertEqual(event["payload"]["services"][0]["state"], "maintenance")


if __name__ == "__main__":
    unittest.main()
