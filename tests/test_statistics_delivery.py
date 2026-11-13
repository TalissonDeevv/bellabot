from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from website_guard.identity import OWNER_DISCORD_ID
from website_guard.state import StateStore
from website_guard.statistics_delivery import (
    STATISTICS_TIMEZONE,
    StatisticsDeliveryStore,
    _next_run,
    describe_schedule,
)


class StatisticsDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_schedules_are_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = StatisticsDeliveryStore(StateStore(path))
            site = await store.configure(
                guild_id=10,
                statistics_type="site",
                channel_id=100,
                created_by=OWNER_DISCORD_ID,
                frequency="daily",
                hour=8,
                minute=30,
                period_days=30,
            )
            server = await store.configure(
                guild_id=10,
                statistics_type="servidor",
                channel_id=200,
                created_by=OWNER_DISCORD_ID,
                frequency="weekly",
                hour=9,
                minute=0,
                weekday=0,
                period_days=7,
            )

            restored = StatisticsDeliveryStore(StateStore(path))
            schedules = await restored.schedules(10)

            self.assertEqual(len(schedules), 2)
            self.assertEqual(site.channel_id, 100)
            self.assertEqual(site.period_days, 30)
            self.assertEqual(server.frequency, "weekly")
            self.assertIn("<#100>", describe_schedule(site))

    async def test_schedule_can_be_reconfigured_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StatisticsDeliveryStore(
                StateStore(Path(directory) / "state.json")
            )
            first = await store.configure(
                guild_id=10,
                statistics_type="site",
                channel_id=100,
                created_by=OWNER_DISCORD_ID,
            )
            second = await store.configure(
                guild_id=10,
                statistics_type="site",
                channel_id=300,
                created_by=OWNER_DISCORD_ID,
                frequency="monthly",
                month_day=5,
            )
            removed = await store.remove(
                10,
                "site",
                requested_by=OWNER_DISCORD_ID,
            )

            self.assertEqual(first.key, second.key)
            self.assertEqual(second.channel_id, 300)
            self.assertTrue(removed)
            self.assertFalse(await store.schedules(10))

    async def test_next_run_respects_daily_weekly_and_monthly_rules(self) -> None:
        base = datetime(
            2026,
            7,
            1,
            10,
            0,
            tzinfo=STATISTICS_TIMEZONE,
        ).timestamp()
        daily = _next_run(
            {"frequency": "daily", "hour": 9, "minute": 0},
            now=base,
        )
        weekly = _next_run(
            {
                "frequency": "weekly",
                "hour": 9,
                "minute": 0,
                "weekday": 0,
            },
            now=base,
        )
        monthly = _next_run(
            {
                "frequency": "monthly",
                "hour": 9,
                "minute": 0,
                "month_day": 1,
            },
            now=base,
        )

        self.assertEqual(
            datetime.fromtimestamp(daily, STATISTICS_TIMEZONE).date().isoformat(),
            "2026-07-02",
        )
        self.assertEqual(
            datetime.fromtimestamp(weekly, STATISTICS_TIMEZONE).date().isoformat(),
            "2026-07-06",
        )
        self.assertEqual(
            datetime.fromtimestamp(monthly, STATISTICS_TIMEZONE).date().isoformat(),
            "2026-08-01",
        )

    async def test_non_owner_cannot_configure_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StatisticsDeliveryStore(
                StateStore(Path(directory) / "state.json")
            )

            with self.assertRaises(PermissionError):
                await store.configure(
                    guild_id=10,
                    statistics_type="site",
                    channel_id=100,
                    created_by=123,
                )


if __name__ == "__main__":
    unittest.main()
