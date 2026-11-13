import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import discord

from website_guard.security_center import (
    FindingKind,
    SecurityRepository,
    Severity,
    analyze_security_snapshot,
    compare_security_snapshots,
    normalize_security_snapshot,
)
from website_guard.security.permissions import PermissionEngine
from website_guard.security.recommendations import RecommendationEngine
from website_guard.state import StateStore


def snapshot() -> dict:
    return {
        "schema_version": 1,
        "guild": {"id": 1, "name": "Teste", "owner_id": 7, "member_count": 20},
        "roles": [
            {
                "id": 1,
                "name": "@everyone",
                "position": 0,
                "permissions": 0,
                "sensitive_permissions": [],
                "managed": False,
                "member_count": 20,
            },
            {
                "id": 2,
                "name": "Suporte",
                "position": 5,
                "permissions": 8,
                "sensitive_permissions": ["administrator"],
                "managed": False,
                "member_count": 14,
            },
        ],
        "channels": [],
        "automod": [],
        "bots": [],
        "webhooks": [],
        "integrations": [],
        "audit_log": [],
        "source_errors": [],
    }


class SecurityCenterTests(unittest.TestCase):
    def test_administrator_severity_considers_affected_members(self) -> None:
        findings = analyze_security_snapshot(snapshot())
        finding = next(item for item in findings if item.rule_id == "role-administrator")
        self.assertIn(finding.severity, {Severity.CRITICAL, Severity.HIGH})
        self.assertIn("14", finding.evidence)

    def test_normalization_ignores_collection_order(self) -> None:
        first = snapshot()
        second = snapshot()
        second["roles"] = list(reversed(second["roles"]))
        self.assertEqual(normalize_security_snapshot(first), normalize_security_snapshot(second))
        self.assertEqual(compare_security_snapshots(first, second), [])

    def test_diff_reports_permission_change(self) -> None:
        before = snapshot()
        after = snapshot()
        after["roles"][1] = {**after["roles"][1], "permissions": 0, "sensitive_permissions": []}
        changes = compare_security_snapshots(before, after)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].change_type, "CHANGED")
        self.assertEqual(changes[0].resource_key, "roles:2")

    def test_permission_engine_combines_roles(self) -> None:
        manage_messages = discord.Permissions(manage_messages=True).value
        moderate_members = discord.Permissions(moderate_members=True).value
        effective = PermissionEngine.combine_guild_permissions([manage_messages, moderate_members])
        permissions = discord.Permissions(effective)
        self.assertTrue(permissions.manage_messages)
        self.assertTrue(permissions.moderate_members)

    def test_administrator_bypasses_channel_overwrites(self) -> None:
        base = discord.Permissions(administrator=True).value
        denied = discord.Permissions(view_channel=True, send_messages=True).value
        effective = PermissionEngine.apply_channel_overwrites(base, everyone=(0, denied))
        permissions = discord.Permissions(effective)
        self.assertTrue(permissions.view_channel)
        self.assertTrue(permissions.send_messages)

    def test_channel_overwrite_order_matches_discord(self) -> None:
        base = discord.Permissions(view_channel=True, send_messages=True, manage_messages=True).value
        send = discord.Permissions(send_messages=True).value
        manage = discord.Permissions(manage_messages=True).value
        view = discord.Permissions(view_channel=True).value
        effective = PermissionEngine.apply_channel_overwrites(
            base,
            everyone=(0, send),
            roles=((send, manage),),
            member=(0, view),
        )
        permissions = discord.Permissions(effective)
        self.assertFalse(permissions.view_channel)
        self.assertTrue(permissions.send_messages)
        self.assertFalse(permissions.manage_messages)

    def test_automod_absence_is_recommendation_not_vulnerability(self) -> None:
        value = snapshot()
        findings = analyze_security_snapshot(value)
        finding = next(item for item in findings if item.rule_id == "automod-no-rules")
        self.assertEqual(finding.kind, FindingKind.RECOMMENDATION)
        self.assertEqual(finding.severity, Severity.INFO)

    def test_member_effective_administrator_is_detected(self) -> None:
        value = snapshot()
        value["members"] = [
            {
                "id": 55,
                "name": "Operador",
                "bot": False,
                "owner": False,
                "role_ids": [1, 2],
                "sensitive_permissions": ["administrator"],
            }
        ]
        finding = next(item for item in analyze_security_snapshot(value) if item.rule_id == "member-effective-administrator")
        self.assertEqual(finding.resource_id, "55")
        self.assertEqual(finding.severity, Severity.HIGH)

    def test_manage_roles_high_hierarchy_is_detected(self) -> None:
        value = snapshot()
        value["roles"].append(
            {
                "id": 3,
                "name": "Operação",
                "position": 10,
                "permissions": discord.Permissions(manage_roles=True).value,
                "sensitive_permissions": ["manage_roles"],
                "managed": False,
                "member_count": 4,
            }
        )
        finding = next(
            item
            for item in analyze_security_snapshot(value)
            if item.rule_id == "role-manage-roles-high-hierarchy"
        )
        self.assertEqual(finding.severity, Severity.HIGH)
        self.assertIn("posição", finding.evidence)

    def test_normalization_sorts_overwrites(self) -> None:
        first = snapshot()
        first["channels"] = [{"id": 7, "overwrites": [{"target_id": 9, "target_type": "member"}, {"target_id": 2, "target_type": "role"}]}]
        second = snapshot()
        second["channels"] = [{"id": 7, "overwrites": list(reversed(first["channels"][0]["overwrites"]))}]
        self.assertEqual(normalize_security_snapshot(first), normalize_security_snapshot(second))

    def test_hardening_plan_omits_intentional_findings(self) -> None:
        findings = analyze_security_snapshot(snapshot())
        intentional = findings[0]
        intentional = intentional.__class__(**{**intentional.to_dict(), "severity": intentional.severity, "kind": intentional.kind, "status": "INTENTIONAL"})
        plan = RecommendationEngine().hardening_plan([intentional])
        self.assertEqual(plan, {})


class SecurityRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.repository = SecurityRepository(StateStore(Path(self.temp.name) / "state.json"))

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_panel_configuration_is_persistent(self) -> None:
        await self.repository.save_panel_config(1, {"channel_id": 10, "message_id": 20})
        self.assertEqual(await self.repository.panel_config(1), {"channel_id": 10, "message_id": 20})

    async def test_exception_requires_same_fingerprint(self) -> None:
        finding = next(item for item in analyze_security_snapshot(snapshot()) if item.rule_id == "role-administrator")
        await self.repository.set_exception(1, finding, actor_id=7, mode="INTENTIONAL")
        applied = await self.repository.apply_exceptions(1, "scan-a", [finding])
        self.assertEqual(applied[0].status, "INTENTIONAL")
        changed = finding.__class__(**{**finding.to_dict(), "severity": finding.severity, "kind": finding.kind, "fingerprint": "changed"})
        reapplied = await self.repository.apply_exceptions(1, "scan-b", [changed])
        self.assertEqual(reapplied[0].status, "OPEN")


if __name__ == "__main__":
    unittest.main()
