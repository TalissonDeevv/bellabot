from __future__ import annotations

import unittest

import discord
from discord.http import handle_message_parameters

from website_guard.dashboard_views import DashboardPage, StatisticsDashboardView


class DashboardViewTests(unittest.TestCase):
    def test_page_builds_components_v2_with_media_and_navigation(self) -> None:
        pages = [
            DashboardPage(
                key="overview",
                label="Visão Geral",
                title="Dashboard",
                description="Resumo executivo.",
                filename="overview.png",
                image=b"\x89PNG\r\n\x1a\n",
                color=0x37D6FF,
                fields=(("Indicador", "Valor", True),),
            ),
            DashboardPage(
                key="traffic",
                label="Tráfego",
                title="Tráfego",
                description="Dados de audiência.",
                filename="traffic.png",
                image=b"\x89PNG\r\n\x1a\n",
                color=0x9B69FF,
            ),
        ]

        view = StatisticsDashboardView(
            owner_id=1,
            pages=pages,
            active_key="overview",
        )
        payload = view.to_components()
        container = payload[0]
        components = container["components"]
        gallery = next(item for item in components if item["type"] == 12)

        self.assertTrue(view.has_components_v2())
        self.assertEqual(len(view.children), 1)
        self.assertEqual(container["type"], 17)
        self.assertEqual(container["accent_color"], 0x37D6FF)
        self.assertEqual(
            gallery["items"][0]["media"]["url"],
            "attachment://overview.png",
        )
        self.assertIn(
            "## Dashboard",
            components[0]["content"],
        )
        self.assertIn(
            "**Indicador:** Valor",
            next(item["content"] for item in components if item["type"] == 10 and "Indicadores" in item["content"]),
        )
        self.assertEqual(
            view.buttons["overview"].style,
            discord.ButtonStyle.primary,
        )
        self.assertTrue(view.buttons["overview"].disabled)
        self.assertEqual(
            view.buttons["traffic"].style,
            discord.ButtonStyle.secondary,
        )
        self.assertFalse(view.buttons["traffic"].disabled)

    def test_single_page_still_uses_components_v2_without_buttons(self) -> None:
        page = DashboardPage(
            key="profile",
            label="Perfil",
            title="Perfil",
            description="Resumo do membro.",
            filename="profile.png",
            image=b"\x89PNG\r\n\x1a\n",
            color=0x42D392,
        )

        view = StatisticsDashboardView(
            owner_id=1,
            pages=[page],
            active_key="profile",
        )
        components = view.to_components()[0]["components"]

        self.assertTrue(view.has_components_v2())
        self.assertFalse(view.buttons)
        self.assertNotIn(1, {item["type"] for item in components})

        with handle_message_parameters(view=view) as parameters:
            self.assertEqual(parameters.payload["flags"], 1 << 15)
            self.assertNotIn("embeds", parameters.payload)
            self.assertNotIn("content", parameters.payload)


if __name__ == "__main__":
    unittest.main()
