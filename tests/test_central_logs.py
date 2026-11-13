import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from website_guard.central_logs import (
    CentralLogService,
    GuildLogConfig,
    LogModuleDefinition,
    PanelChannelDefinition,
    channel_name,
)
from website_guard.admin import ADMIN_COMMAND_ROOTS
from website_guard.state import StateStore


class CentralLogTests(unittest.IsolatedAsyncioTestCase):
    def test_new_administrative_commands_use_existing_rbac(self) -> None:
        self.assertTrue(
            {"logs-sincronizar", "seguranca", "scan-seguranca", "backup-servidor"}
            <= ADMIN_COMMAND_ROOTS
        )

    def test_schema_upgrade_adds_all_new_module_channels_to_existing_config(self) -> None:
        config = GuildLogConfig.from_dict(
            1,
            {
                "schema_version": 1,
                "enabled_modules": ["site", "tickets"],
            },
        )
        self.assertTrue(
            {
                "critical_changes",
                "panel_repair",
                "giveaways",
                "satisfaction",
                "identity",
                "site_feedbacks",
                "activity",
                "errors",
                "maintenance",
            }
            <= set(config.enabled_modules)
        )

    def test_channel_name_respects_appearance(self) -> None:
        definition = LogModuleDefinition("security", "Segurança", "seguranca", "🔐", ("scan",))
        config = GuildLogConfig(guild_id=1, use_emojis=True, divider="╺╸")
        self.assertEqual(channel_name(definition, config), "🔐╺╸seguranca")
        config.use_emojis = False
        self.assertEqual(channel_name(definition, config), "seguranca")

    async def test_provision_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            binding = AsyncMock()
            panel_binding = AsyncMock()
            service = CentralLogService(
                state,
                binding_callback=binding,
                panel_binding_callback=panel_binding,
            )
            config = GuildLogConfig(guild_id=10, enabled_modules=["security", "alerts"])
            channels_by_id = {}
            category = SimpleNamespace(id=90, text_channels=[])

            async def create_text_channel(name, **kwargs):
                channel = SimpleNamespace(id=100 + len(channels_by_id), name=name)
                channels_by_id[channel.id] = channel
                category.text_channels.append(channel)
                return channel

            guild = SimpleNamespace(
                id=10,
                me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_channels=True)),
                create_text_channel=AsyncMock(side_effect=create_text_channel),
            )

            def stored(_guild, channel_id):
                return channels_by_id.get(channel_id)

            with (
                patch.object(service, "_resolve_category", AsyncMock(return_value=(category, True))),
                patch("website_guard.central_logs._stored_text_channel", side_effect=stored),
            ):
                first = await service.provision(guild, config, reason="teste")
                second = await service.provision(guild, config, reason="teste")

            self.assertEqual(set(first.created_channels), {"security", "alerts"})
            self.assertEqual(second.created_channels, ())
            self.assertEqual(set(second.reused_channels), {"security", "alerts"})
            self.assertEqual(set(first.created_panel_channels), {"backup_admin"})
            self.assertEqual(set(second.reused_panel_channels), {"backup_admin"})
            self.assertEqual(guild.create_text_channel.await_count, 3)
            self.assertEqual(binding.await_count, 2)
            self.assertEqual(panel_binding.await_count, 2)

    async def test_auxiliary_panel_channel_is_created_once_in_logs_category(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            service = CentralLogService(state)
            await service.save_config(
                GuildLogConfig(guild_id=10, use_emojis=True, divider="╺╸")
            )
            category = SimpleNamespace(id=90, text_channels=[])
            channels_by_id = {}

            async def create_text_channel(name, **kwargs):
                channel = SimpleNamespace(
                    id=120,
                    name=name,
                    edit=AsyncMock(),
                )
                channels_by_id[channel.id] = channel
                category.text_channels.append(channel)
                return channel

            guild = SimpleNamespace(
                id=10,
                me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_channels=True)),
                create_text_channel=AsyncMock(side_effect=create_text_channel),
            )
            definition = PanelChannelDefinition(
                "partner_analytics",
                "Partner Analytics",
                "parceiros-analytics",
                "📈",
            )

            with (
                patch.object(service, "_resolve_category", AsyncMock(return_value=(category, False))),
                patch(
                    "website_guard.central_logs._stored_text_channel",
                    side_effect=lambda _guild, channel_id: channels_by_id.get(channel_id),
                ),
            ):
                first = await service.ensure_panel_channel(
                    guild,
                    definition,
                    topic="Analytics privado",
                    reason="teste",
                )
                second = await service.ensure_panel_channel(
                    guild,
                    definition,
                    topic="Analytics privado",
                    reason="teste",
                )

            self.assertIs(first, second)
            self.assertEqual(first.name, "📈╺╸parceiros-analytics")
            self.assertEqual(guild.create_text_channel.await_count, 1)
            self.assertEqual(first.edit.await_count, 1)
            self.assertEqual(
                (await service.panel_channel_ids(10))["partner_analytics"],
                120,
            )


if __name__ == "__main__":
    unittest.main()
