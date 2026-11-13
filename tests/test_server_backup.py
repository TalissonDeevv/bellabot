import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from website_guard.server_backup import (
    BACKUP_SCHEMA_VERSION,
    BackupJobStatus,
    BackupMetadata,
    RestoreResult,
    ServerBackupService,
    normalize_backup_payload,
    remap_automod_rule,
    remap_permission_overwrites,
    validate_backup_schema,
)
from website_guard.state import StateStore


def payload() -> dict:
    return {
        "METADATA": {"backup_schema_version": BACKUP_SCHEMA_VERSION, "backup_id": "abc"},
        "STRUCTURE": {"id": 1, "default_role_id": 1},
        "ROLES": [{"id": 2}, {"id": 1}],
        "CHANNELS": [{"id": 20}, {"id": 10}],
        "PERMISSIONS": {},
        "AUTOMOD": [],
        "BELLA_PANELS": [],
        "EMBEDS": [],
    }


class ServerBackupTests(unittest.TestCase):
    def test_schema_validation_and_normalization(self) -> None:
        data = payload()
        self.assertEqual(validate_backup_schema(data), [])
        normalized = normalize_backup_payload(data)
        self.assertEqual([item["id"] for item in normalized["ROLES"]], [1, 2])
        self.assertEqual([item["id"] for item in normalized["CHANNELS"]], [10, 20])

    def test_schema_rejects_missing_sections(self) -> None:
        errors = validate_backup_schema({"METADATA": {"backup_schema_version": 1}})
        self.assertTrue(any("seções ausentes" in item for item in errors))

    def test_schema_one_backups_remain_compatible(self) -> None:
        data = payload()
        data["METADATA"]["backup_schema_version"] = 1
        self.assertEqual(validate_backup_schema(data), [])

    def test_permission_overwrite_id_mapping(self) -> None:
        mapped_role = discord.Object(id=200, type=discord.Role)
        member = discord.Object(id=300, type=discord.Member)
        guild = SimpleNamespace(
            get_role=lambda role_id: mapped_role if role_id == 200 else None,
            get_member=lambda member_id: member if member_id == 300 else None,
        )
        result, skipped = remap_permission_overwrites(
            [
                {"target_id": 2, "target_type": "role", "allow": 1024, "deny": 0},
                {"target_id": 300, "target_type": "member", "allow": 0, "deny": 2048},
                {"target_id": 999, "target_type": "member", "allow": 0, "deny": 1},
            ],
            {2: 200},
            guild,
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(skipped, ["member:999"])

    def test_automod_references_are_remapped(self) -> None:
        role = discord.Object(id=200, type=discord.Role)
        channel = discord.Object(id=400, type=discord.TextChannel)
        guild = SimpleNamespace(
            get_role=lambda role_id: role if role_id == 200 else None,
            get_channel=lambda channel_id: channel if channel_id == 400 else None,
        )
        mapped = remap_automod_rule(
            {
                "name": "Links",
                "enabled": True,
                "event_type": 1,
                "trigger_type": 1,
                "trigger_metadata": {"keyword_filter": ["exemplo"]},
                "actions": [{"type": 2, "metadata": {"channel_id": "40"}}],
                "exempt_role_ids": [2],
                "exempt_channel_ids": [40],
            },
            {2: 200},
            {40: 400},
            guild,
        )
        self.assertEqual(mapped["exempt_roles"], [role])
        self.assertEqual(mapped["exempt_channels"], [channel])
        self.assertEqual(mapped["actions"][0].channel_id, 400)


class ServerBackupIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_collects_existing_bella_embed_messages(self) -> None:
        channel = SimpleNamespace(
            id=20,
            name="painel",
            permissions_for=lambda _: SimpleNamespace(
                view_channel=True,
                read_message_history=True,
            ),
        )
        message = SimpleNamespace(
            id=30,
            author=SimpleNamespace(id=99),
            channel=channel,
            content="Conteúdo",
            embeds=[discord.Embed(title="Painel antigo")],
            components=[],
            attachments=[],
            created_at=discord.utils.utcnow(),
        )

        async def history(*, limit):
            self.assertGreater(limit, 0)
            yield message

        channel.history = history
        service = ServerBackupService(SimpleNamespace(user=SimpleNamespace(id=99)), SimpleNamespace())
        guild = SimpleNamespace(
            id=10,
            me=SimpleNamespace(),
            text_channels=[channel],
        )

        collected, warnings = await service._collect_bella_embed_messages(guild, excluded=set())

        self.assertEqual(warnings, [])
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0]["message_id"], 30)
        self.assertEqual(collected[0]["embeds"][0]["title"], "Painel antigo")

    async def test_standalone_bella_embed_restore_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            service = ServerBackupService(SimpleNamespace(user=None), state)
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 200
            restored_message = SimpleNamespace(id=900)
            channel.send = AsyncMock(return_value=restored_message)
            channel.fetch_message = AsyncMock(return_value=restored_message)
            guild = SimpleNamespace(
                id=10,
                get_channel=lambda channel_id: channel if channel_id == 200 else None,
            )
            item = {
                "channel_id": 20,
                "message_id": 30,
                "content": "Painel antigo",
                "embeds": [discord.Embed(title="Bella").to_dict()],
                "components": [],
            }

            created: list[str] = []
            reused: list[str] = []
            failures: list[str] = []
            await service._restore_embeds(
                guild,
                [item],
                {20: 200},
                "backup-1",
                created,
                reused,
                failures,
                skip_message_ids=set(),
            )
            await service._restore_embeds(
                guild,
                [item],
                {20: 200},
                "backup-1",
                created,
                reused,
                failures,
                skip_message_ids=set(),
            )

            self.assertEqual(channel.send.await_count, 1)
            self.assertEqual(created, ["embed:900"])
            self.assertEqual(reused, ["embed:900"])
            self.assertEqual(failures, [])

    async def test_restore_runs_security_before_and_after_and_marks_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            security_scan = AsyncMock(side_effect=[None, RuntimeError("scan indisponível")])
            service = ServerBackupService(
                SimpleNamespace(user=None),
                state,
                security_scan_callback=security_scan,
            )
            guild = SimpleNamespace(id=10)
            metadata = BackupMetadata(
                backup_id="safe",
                guild_id=10,
                guild_name="Teste",
                actor_id=5,
                created_at="agora",
                status=BackupJobStatus.COMPLETED,
                schema_version=1,
            )
            base_result = RestoreResult(
                job_id="job",
                backup_id="original",
                safety_backup_id="safe",
                status=BackupJobStatus.COMPLETED,
                role_map={},
                category_map={},
                channel_map={},
                created=(),
                reused=(),
                failures=(),
                started_at="inicio",
                finished_at="fim",
            )
            with (
                patch.object(service, "_required_payload", AsyncMock(return_value={"METADATA": {}})),
                patch.object(service, "_collect_and_save", AsyncMock(return_value=(metadata, {}))),
                patch.object(service, "_restore_payload", AsyncMock(return_value=base_result)),
            ):
                result = await service.restore_backup(
                    guild,
                    "original",
                    actor_id=5,
                    confirmation_phrase="RESTAURAR",
                )

            self.assertEqual(security_scan.await_count, 2)
            self.assertEqual(result.status, BackupJobStatus.PARTIAL)
            self.assertTrue(any("posterior" in item for item in result.failures))


if __name__ == "__main__":
    unittest.main()
