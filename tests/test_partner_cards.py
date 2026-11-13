from __future__ import annotations

import unittest

from PIL import Image, ImageOps

from website_guard.partner_cards import (
    BACKGROUND_PATH,
    BRAND_MARK_BOUNDS,
    CARD_SIZE,
    render_approach_card,
    render_commission_card,
    render_evolution_card,
    render_funnel_card,
    render_history_card,
    render_hub_card,
    render_invite_card,
    render_profile_card,
    render_rules_card,
)


class PartnerCardTests(unittest.TestCase):
    def assert_valid_card(self, payload: object) -> None:
        self.assertTrue(hasattr(payload, "read"))
        opened = Image.open(payload)
        self.assertEqual(opened.format, "PNG")
        image = opened.convert("RGB")
        self.assertEqual(image.size, CARD_SIZE)
        source = ImageOps.fit(
            Image.open(BACKGROUND_PATH).convert("RGB"),
            CARD_SIZE,
            method=Image.Resampling.LANCZOS,
        ).crop(BRAND_MARK_BOUNDS)
        rendered = image.crop(BRAND_MARK_BOUNDS)
        mask = ImageOps.grayscale(source).point(lambda value: 255 if value >= 92 else 0)
        source_pixels = list(source.get_flattened_data())
        rendered_pixels = list(rendered.get_flattened_data())
        mask_pixels = list(mask.get_flattened_data())
        self.assertFalse(
            any(
                source_pixel != rendered_pixel
                for source_pixel, rendered_pixel, mask_pixel in zip(
                    source_pixels,
                    rendered_pixels,
                    mask_pixels,
                )
                if mask_pixel
            )
        )

    def test_background_asset_is_packaged(self) -> None:
        self.assertTrue(BACKGROUND_PATH.is_file())
        self.assertGreater(BACKGROUND_PATH.stat().st_size, 100_000)

    def test_renders_all_partner_intelligence_cards(self) -> None:
        cards = [
            render_hub_card(),
            render_profile_card(
                "Parceiro Teste",
                {
                    "rank": 2,
                    "invite": {"invite_url": "https://discord.gg/exemplo"},
                    "members": {
                        "invited_total": 12,
                        "active_total": 10,
                        "left_total": 2,
                        "clients_total": 3,
                    },
                    "projects": {
                        "closed_projects": 2,
                        "negotiating_projects": 1,
                        "revenue_total": 10000,
                    },
                    "commissions": {"paid": 3500, "pending": 1500},
                },
            ),
            render_invite_card(
                "Parceiro Teste",
                {
                    "invite": {"invite_url": "https://discord.gg/exemplo"},
                    "members": {
                        "invited_total": 12,
                        "active_total": 10,
                        "left_total": 2,
                    },
                },
            ),
            render_history_card(
                "Parceiro Teste",
                [
                    {"member_id": 123, "status": "active", "is_client": True},
                    {"member_id": 456, "status": "left", "is_client": False},
                ],
                0,
            ),
            render_rules_card(),
            render_evolution_card(
                "Parceiro Teste",
                {
                    "level": "Estrategista",
                    "score": 120,
                    "next_level": {"name": "Embaixador", "remaining": 60},
                    "badges": ["Link permanente ativo", "Projeto conquistado"],
                    "missions": ["Registrar uma nova oportunidade legítima."],
                },
            ),
            render_funnel_card(
                "Parceiro Teste",
                {
                    "total": 8,
                    "accepted": 3,
                    "negotiation": 2,
                    "closed": 1,
                    "qualification_rate": 75.0,
                    "closing_rate": 16.7,
                    "retention_rate": 80.0,
                    "confidence": "Média",
                },
                {
                    "diagnosis": "O funil possui oportunidades em andamento.",
                    "next_action": "Mantenha os dados atualizados e aguarde a equipe.",
                },
            ),
            render_commission_card(5000, 2, 40),
            render_approach_card(
                target_name="Empresa Teste",
                business_area="tecnologia",
                tone="natural",
                opening="Mensagem inicial autorizada.",
                follow_up="Acompanhamento sem pressão.",
                handoff="Contato autorizado para análise da equipe.",
            ),
        ]
        for card in cards:
            with self.subTest(card=card):
                self.assert_valid_card(card)


if __name__ == "__main__":
    unittest.main()
