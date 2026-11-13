from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from website_guard.identity_monitor import (
    IdentityFilterConfig,
    IdentityMonitor,
    inspect_identity_value,
    normalize_identity_text,
)
from website_guard.state import StateStore


class IdentityMonitorTests(unittest.IsolatedAsyncioTestCase):
    def test_normalization_blocks_unicode_spacing_and_leetspeak_bypass(self) -> None:
        self.assertEqual(normalize_identity_text("Adm"), "adm")
        self.assertEqual(normalize_identity_text("ＡＤＭ"), "adm")
        self.assertEqual(normalize_identity_text("a.d.m"), "adm")
        self.assertEqual(normalize_identity_text("a d m"), "adm")
        self.assertEqual(normalize_identity_text("M0derador"), "moderador")
        self.assertEqual(normalize_identity_text("ＭＯＤＥＲＡＤＯＲ"), "moderador")
        self.assertEqual(normalize_identity_text("mоderаdor"), "moderador")

    def test_blacklist_and_exact_whitelist_are_applied(self) -> None:
        config = IdentityFilterConfig(
            guild_id=1,
            enabled=True,
            prohibited_terms=("admin", "moderador"),
            allowed_values=("Administrador Talisson",),
        )

        blocked = inspect_identity_value("A.d.m.i.n Oficial", config)
        allowed = inspect_identity_value("Administrador Talisson", config)

        self.assertTrue(blocked.matched)
        self.assertEqual(blocked.term, "admin")
        self.assertFalse(allowed.matched)

    async def test_configuration_survives_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            monitor = IdentityMonitor(
                SimpleNamespace(),
                StateStore(path),
                owner_id=10,
                admin_role_ids={20},
            )
            await monitor.configure(
                1,
                "nickname",
                log_channel_id=99,
                enabled=True,
            )
            await monitor.add_term(1, "nickname", "Equipe Oficial")
            await monitor.add_allowed_value(
                1,
                "nickname",
                "Suporte Oficial Talisson",
            )
            await monitor.set_user_exemption(1, "nickname", 50, True)
            await monitor.set_role_exemption(1, "nickname", 60, True)

            restored = IdentityMonitor(
                SimpleNamespace(),
                StateStore(path),
                owner_id=10,
                admin_role_ids={20},
            )
            config = await restored.config(1, "nickname")

            self.assertTrue(config.enabled)
            self.assertEqual(config.log_channel_id, 99)
            self.assertIn("Equipe Oficial", config.prohibited_terms)
            self.assertIn("Suporte Oficial Talisson", config.allowed_values)
            self.assertIn(50, config.exempt_user_ids)
            self.assertIn(60, config.exempt_role_ids)

            view = restored.nickname_panel_view()
            custom_ids = [getattr(item, "custom_id", None) for item in view.children]
            self.assertIsNone(view.timeout)
            self.assertEqual(len(custom_ids), len(set(custom_ids)))
            self.assertGreaterEqual(len(custom_ids), 10)


if __name__ == "__main__":
    unittest.main()
