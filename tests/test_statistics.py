from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from website_guard.metrics import WeeklyMetricsSnapshot
from website_guard.state import StateStore
from website_guard.statistics import (
    MessageStatisticsRecorder,
    StatisticsImageRenderer,
)


class StatisticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_statistics_are_aggregated_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            recorder = MessageStatisticsRecorder(state)
            now = datetime.now(timezone.utc)

            await recorder.record(
                guild_id=10,
                channel_id=100,
                user_id=1,
                created_at=now,
                character_count=20,
                attachment_count=1,
            )
            await recorder.record(
                guild_id=10,
                channel_id=100,
                user_id=2,
                created_at=now,
                character_count=30,
                attachment_count=0,
            )
            await recorder.record(
                guild_id=10,
                channel_id=200,
                user_id=1,
                created_at=now,
                character_count=10,
                attachment_count=0,
            )

            snapshot = await recorder.snapshot(10, days=7)
            restored = MessageStatisticsRecorder(
                StateStore(Path(directory) / "state.json")
            )
            persisted = await restored.snapshot(10, days=7)

            self.assertEqual(snapshot.total_messages, 3)
            self.assertEqual(snapshot.active_users, 2)
            self.assertEqual(snapshot.active_channels, 2)
            self.assertEqual(snapshot.attachment_messages, 1)
            self.assertEqual(snapshot.total_characters, 60)
            self.assertEqual(snapshot.channels["100"], 2)
            self.assertEqual(persisted.total_messages, 3)

    async def test_voice_and_member_join_statistics_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            recorder = MessageStatisticsRecorder(state)
            now = datetime.now(timezone.utc)

            await recorder.record_member_join(guild_id=10, joined_at=now)
            await recorder.voice_state_update(
                guild_id=10,
                user_id=1,
                before_channel_id=None,
                after_channel_id=500,
                occurred_at=now - timedelta(hours=1),
            )
            await recorder.voice_state_update(
                guild_id=10,
                user_id=1,
                before_channel_id=500,
                after_channel_id=None,
                occurred_at=now,
            )

            snapshot = await recorder.snapshot(10, days=7)

            self.assertEqual(snapshot.new_members, 1)
            self.assertEqual(snapshot.voice_seconds, 3600)
            self.assertEqual(snapshot.voice_channels["500"], 3600)
            self.assertEqual(snapshot.voice_users["1"], 3600)

    async def test_statistics_renderers_generate_valid_png_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            recorder = MessageStatisticsRecorder(state)
            now = datetime.now(timezone.utc)
            for index in range(8):
                await recorder.record(
                    guild_id=10,
                    channel_id=100 if index < 5 else 200,
                    user_id=(index % 3) + 1,
                    created_at=now,
                    character_count=20 + index,
                    attachment_count=1 if index == 0 else 0,
                )
            server_snapshot = await recorder.snapshot(10, days=7)
            day_key = now.date().isoformat()
            site_snapshot = WeeklyMetricsSnapshot(
                period_start=day_key,
                period_end=day_key,
                total_requests=12,
                average_response_ms=82.5,
                minimum_response_ms=61,
                maximum_response_ms=129,
                status_counts={"200": 12},
                category_counts={"site": 12},
                errors_detected=0,
                events_processed=12,
                changes_detected=1,
                feedbacks_detected=2,
                site_checks=12,
                site_successes=12,
                availability_percent=100.0,
                daily={
                    day_key: {
                        "timed_requests": 12,
                        "response_time_total_ms": 990,
                        "site_checks": 12,
                        "site_successes": 12,
                    }
                },
            )
            renderer = StatisticsImageRenderer()

            site_image = renderer.render_site(
                site_snapshot,
                site_name="talissonsouza.com.br",
            )
            server_image = renderer.render_server(
                server_snapshot,
                guild_name="Escritório do Talisson",
                channel_names={"100": "#bate-papo", "200": "#projetos"},
            )

            self.assertTrue(site_image.getvalue().startswith(b"\x89PNG"))
            self.assertTrue(server_image.getvalue().startswith(b"\x89PNG"))
            self.assertEqual(Image.open(site_image).size, (1600, 900))
            self.assertEqual(Image.open(server_image).size, (1600, 900))
            self.assertEqual(Image.open(site_image).getpixel((0, 0)), (2, 2, 3))
            self.assertEqual(Image.open(server_image).getpixel((0, 0)), (2, 2, 3))

    async def test_summary_and_profile_cards_generate_valid_png(self) -> None:
        renderer = StatisticsImageRenderer()

        summary = renderer.render_summary(
            title="Bella Runtime",
            subtitle="Teste local",
            badge="SYSTEM",
            metrics=(
                ("CPU", "12%", "Uso atual", (55, 214, 255)),
                ("RAM", "256 MB", "Memória", (155, 105, 255)),
                ("PING", "42 ms", "Discord", (66, 211, 146)),
                ("CACHE", "18", "Itens", (73, 122, 255)),
            ),
            left_title="Infraestrutura",
            left_rows=[("Banco", "Operacional"), ("Discord", "Conectado")],
            right_title="Operação",
            right_rows=[("Respostas", "120"), ("Tickets", "14")],
        )
        profile = renderer.render_profile(
            display_name="Talisson",
            username="talissondev",
            user_id=1,
            guild_name="Escritório do Talisson",
            avatar_bytes=None,
            main_role="Proprietário",
            joined_at="30/06/2026",
            created_at="01/01/2020",
            messages=500,
            tickets=3,
            days_in_server=365,
        )

        self.assertTrue(summary.getvalue().startswith(b"\x89PNG"))
        self.assertTrue(profile.getvalue().startswith(b"\x89PNG"))
        self.assertEqual(Image.open(summary).size, (1600, 900))
        self.assertEqual(Image.open(profile).size, (1600, 900))


if __name__ == "__main__":
    unittest.main()
