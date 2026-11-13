from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from website_guard.partner_program import (
    PROGRAM_KNOWLEDGE,
    PartnerAdminView,
    PartnerManualAddView,
    PartnerManualDetailsModal,
    _parse_partner_money,
    _parse_partner_joined_at,
    _partner_program_knowledge_is_stale,
    _partner_project_status,
)
from website_guard.partners import (
    DEPRECATED_PARTNER_WORKSPACE_CHANNELS,
    PARTNER_V2_ACCENT_COLOR,
    PARTNER_WORKSPACE_CHANNELS,
    PARTNER_REFERRAL_REPORT_CHANNEL_ID,
    PartnerDatabase,
    PartnerIndicationsView,
    PartnerProgramManager,
    PartnerReferralAdminView,
    PartnerResultV2View,
    PartnerSettings,
    PartnerSupportView,
    PartnerWorkspaceView,
    _invite_code_from_url,
    _embed_to_v2_markdown,
    _invite_is_permanent,
    _normalize_referral_contact,
    _parse_partner_currency,
    _partner_evolution_summary,
    _partner_funnel_diagnosis,
)
from website_guard.state import StateStore


class PartnerDatabaseTests(unittest.TestCase):
    def test_workspace_defines_unique_operational_channels(self) -> None:
        identifiers = {item.identifier for item in PARTNER_WORKSPACE_CHANNELS}
        names = {item.name for item in PARTNER_WORKSPACE_CHANNELS}
        self.assertEqual(
            identifiers,
            {
                "public",
                "hub",
                "announcements",
                "indications",
                "materials",
                "support",
                "chat",
                "voice",
                "meetings_voice",
                "admin",
            },
        )
        self.assertEqual(len(names), len(PARTNER_WORKSPACE_CHANNELS))
        self.assertEqual(
            [item.identifier for item in PARTNER_WORKSPACE_CHANNELS if item.kind == "voice"],
            ["voice", "meetings_voice"],
        )
        self.assertTrue(identifiers.isdisjoint(DEPRECATED_PARTNER_WORKSPACE_CHANNELS))
        self.assertIn("50%", PROGRAM_KNOWLEDGE)
        self.assertNotIn("10%", PROGRAM_KNOWLEDGE)

    def test_workspace_panels_have_distinct_actions(self) -> None:
        manager = MagicMock(spec=PartnerProgramManager)
        hub_view = PartnerWorkspaceView(manager)
        hub_buttons = [
            item
            for item in hub_view.walk_children()
            if isinstance(item, discord.ui.Button)
        ]
        hub_labels = {str(item.label) for item in hub_buttons}
        indication_labels = {
            str(item.label) for item in PartnerIndicationsView(manager).children
        }
        support_labels = {str(item.label) for item in PartnerSupportView(manager).children}
        self.assertEqual(
            hub_labels,
            {
                "Meu painel",
                "Cadastrar/ver link",
                "Histórico",
                "Minha evolução",
                "Funil inteligente",
                "Simular ganhos",
                "Kit personalizado",
                "Regras",
                "Página oficial",
            },
        )
        self.assertEqual(
            indication_labels,
            {"Registrar indicação", "Minhas indicações"},
        )
        self.assertEqual(support_labels, {"Abrir ticket privado"})
        self.assertTrue(hub_labels.isdisjoint(indication_labels))
        self.assertIsInstance(hub_view, discord.ui.LayoutView)
        self.assertEqual(hub_view.children[0].accent_color.value, PARTNER_V2_ACCENT_COLOR)
        styles = {str(item.label): item.style for item in hub_buttons}
        self.assertEqual(styles["Meu painel"], discord.ButtonStyle.primary)
        self.assertEqual(styles["Cadastrar/ver link"], discord.ButtonStyle.success)
        self.assertEqual(styles["Histórico"], discord.ButtonStyle.secondary)
        self.assertEqual(styles["Minha evolução"], discord.ButtonStyle.primary)
        self.assertEqual(styles["Funil inteligente"], discord.ButtonStyle.primary)
        self.assertEqual(styles["Simular ganhos"], discord.ButtonStyle.success)

    def test_partner_result_uses_components_v2_accent(self) -> None:
        embed = discord.Embed(title="Teste", description="Conteúdo")
        embed.add_field(name="Diagnóstico", value="Tudo funcionando.", inline=False)
        view = PartnerResultV2View(
            embed,
            accent_color=PARTNER_V2_ACCENT_COLOR,
            image=discord.File(io.BytesIO(b"image"), filename="image.png"),
        )
        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertEqual(view.children[0].accent_color.value, PARTNER_V2_ACCENT_COLOR)
        self.assertIsInstance(view.children[0].children[0], discord.ui.MediaGallery)
        component_types = [type(item) for item in view.children[0].children]
        self.assertEqual(component_types.count(discord.ui.TextDisplay), 1)
        self.assertEqual(component_types.count(discord.ui.Separator), 1)

    def test_v2_markdown_compacts_inline_embed_fields(self) -> None:
        embed = discord.Embed(title="Perfil do Parceiro", description="Resumo privado.")
        embed.add_field(name="Nome", value="Talisson S.")
        embed.add_field(name="ID", value="`123456789`")
        embed.add_field(name="Indicações", value="Entraram: 5\nAtivos: 4\nSaíram: 1")
        embed.add_field(name="Link", value="https://discord.gg/exemplo", inline=False)

        markdown = _embed_to_v2_markdown(embed)

        self.assertIn("**Nome:** Talisson S.  •  **ID:** `123456789`", markdown)
        self.assertIn("**Indicações:** Entraram: 5 · Ativos: 4 · Saíram: 1", markdown)
        self.assertIn("**Link**\nhttps://discord.gg/exemplo", markdown)
        self.assertNotIn("### Nome", markdown)
        self.assertIn("## Perfil do Parceiro\nResumo privado.", markdown)
        self.assertIn("\n\n**Link**", markdown)

    def test_fixed_partner_panel_places_image_first(self) -> None:
        view = PartnerWorkspaceView(
            MagicMock(spec=PartnerProgramManager),
            image=discord.File(io.BytesIO(b"image"), filename="image.png"),
        )

        self.assertEqual(view.children[0].accent_color.value, PARTNER_V2_ACCENT_COLOR)
        self.assertIsInstance(view.children[0].children[0], discord.ui.MediaGallery)

    def test_admin_panel_exposes_approved_partner_management(self) -> None:
        labels = {str(item.label) for item in PartnerAdminView(MagicMock(), 10).children}

        self.assertIn("Parceiros aprovados", labels)
        self.assertIn("Adicionar parceiro", labels)
        self.assertIn("Relatórios", labels)

    def test_manual_partner_flow_uses_user_selector(self) -> None:
        view = PartnerManualAddView(MagicMock(), 10)

        self.assertEqual(len(view.children), 1)
        self.assertIsInstance(view.children[0], discord.ui.UserSelect)
        self.assertEqual(view.children[0].max_values, 1)

    def test_manual_partner_modal_collects_date_link_and_notes(self) -> None:
        modal = PartnerManualDetailsModal(MagicMock(), 10, 20)
        labels = {str(item.label) for item in modal.children}

        self.assertEqual(
            labels,
            {"Data de entrada", "Link personalizado permanente", "Observações internas"},
        )
        self.assertRegex(str(modal.joined_at.default), r"\d{2}/\d{2}/\d{4}")

    def test_partner_joined_at_accepts_brazilian_date(self) -> None:
        self.assertEqual(
            _parse_partner_joined_at("24/09/2026"),
            "2026-09-24T00:00:00+00:00",
        )
        with self.assertRaises(ValueError):
            _parse_partner_joined_at("data errada")

    def test_partner_database_can_override_manual_joined_at(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = PartnerDatabase(Path(directory) / "partners.db")
            db.upsert_partner(10, 20)
            updated = db.upsert_partner(
                10,
                20,
                joined_at="2026-09-24T00:00:00+00:00",
            )

        self.assertEqual(updated["joined_at"], "2026-09-24T00:00:00+00:00")

    def test_partner_financial_inputs_are_normalized(self) -> None:
        self.assertEqual(_parse_partner_money("R$ 5.000,00"), 5000.0)
        self.assertEqual(_partner_project_status("negociação"), "negotiation")
        self.assertEqual(_partner_project_status("fechado"), "closed")

    def test_referral_contacts_are_validated_by_type(self) -> None:
        self.assertEqual(
            _normalize_referral_contact("email", "Contato@Empresa.com.br"),
            "contato@empresa.com.br",
        )
        self.assertEqual(
            _normalize_referral_contact("whatsapp", "(11) 99999-9999"),
            "+5511999999999",
        )
        with self.assertRaises(ValueError):
            _normalize_referral_contact("email", "email-invalido")

    def test_old_commission_cache_is_rejected(self) -> None:
        self.assertTrue(
            _partner_program_knowledge_is_stale(
                "Projeto de R$ 5.000 com comissão de 10%, resultando em R$ 500."
            )
        )
        self.assertFalse(_partner_program_knowledge_is_stale(PROGRAM_KNOWLEDGE))

    def test_invite_url_and_permanence_validation(self) -> None:
        self.assertEqual(_invite_code_from_url("https://discord.gg/Bella-123"), "Bella-123")
        self.assertEqual(
            _invite_code_from_url("discord.com/invite/Parceiros"),
            "Parceiros",
        )
        self.assertIsNone(_invite_code_from_url("https://example.com/convite"))
        permanent = SimpleNamespace(
            max_age=0,
            max_uses=0,
            temporary=False,
            expires_at=None,
        )
        expiring = SimpleNamespace(
            max_age=86400,
            max_uses=0,
            temporary=False,
            expires_at=None,
        )
        limited = SimpleNamespace(
            max_age=0,
            max_uses=25,
            temporary=False,
            expires_at=None,
        )
        self.assertTrue(_invite_is_permanent(permanent))
        self.assertFalse(_invite_is_permanent(expiring))
        self.assertFalse(_invite_is_permanent(limited))

    def test_profile_dashboard_and_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = PartnerDatabase(Path(directory) / "partners.db")
            db.save_settings(
                PartnerSettings(
                    guild_id=10,
                    panel_channel_id=20,
                    partner_role_id=30,
                    log_channel_id=40,
                )
            )
            db.upsert_partner(10, 100, role_id=30)
            db.save_invite(
                guild_id=10,
                partner_id=100,
                invite_code="abc",
                invite_url="https://discord.gg/abc",
                visual_role_id=50,
                source_channel_id=20,
                expires_at=None,
            )
            db.register_member_join(
                guild_id=10,
                member_id=200,
                partner_id=100,
                invite_code="abc",
            )
            db.register_client(
                guild_id=10,
                member_id=200,
                partner_id=100,
                observations="Cliente convertido pelo ticket.",
            )
            db.register_project(
                guild_id=10,
                partner_id=100,
                member_id=200,
                name="Projeto Alpha",
                status="closed",
                revenue=2500,
            )
            db.register_commission(
                guild_id=10,
                partner_id=100,
                amount=250,
                description="Comissão inicial",
            )

            profile = db.profile(10, 100)
            self.assertEqual(profile["invite"]["invite_code"], "abc")
            self.assertEqual(profile["members"]["invited_total"], 1)
            self.assertEqual(profile["members"]["clients_total"], 1)
            self.assertEqual(profile["projects"]["closed_projects"], 1)
            self.assertEqual(profile["projects"]["revenue_total"], 2500)
            self.assertEqual(profile["commissions"]["pending"], 250)

            dashboard = db.dashboard(10)
            self.assertEqual(dashboard["total"], 1)
            self.assertEqual(dashboard["clients"], 1)
            self.assertEqual(dashboard["projects"], 1)
            self.assertEqual(dashboard["conversion"], 100)

            ranking = db.ranking(10, "receita")
            self.assertEqual(ranking[0]["partner_id"], 100)
            self.assertEqual(ranking[0]["score"], 2500)

    def test_member_left_and_commission_payment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = PartnerDatabase(Path(directory) / "partners.db")
            db.upsert_partner(10, 100)
            db.register_member_join(
                guild_id=10,
                member_id=200,
                partner_id=100,
                invite_code="manual",
            )
            db.register_commission(
                guild_id=10,
                partner_id=100,
                amount=99.9,
                description="Teste",
            )

            left = db.mark_member_left(10, 200)
            self.assertIsNotNone(left)
            self.assertEqual(left["status"], "left")

            paid = db.pay_commission(guild_id=10, partner_id=100, amount=None)
            self.assertAlmostEqual(paid, 99.9)
            profile = db.profile(10, 100)
            self.assertAlmostEqual(profile["commissions"]["paid"], 99.9)
            self.assertEqual(profile["commissions"]["pending"], 0)

    def test_saves_private_referrals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = PartnerDatabase(Path(directory) / "partners.db")
            db.upsert_partner(10, 100)
            referral = db.create_referral(
                guild_id=10,
                partner_id=100,
                contact_name="Empresa Exemplo",
                contact_method="contato autorizado",
                need="Sistema interno",
                context="A empresa pediu uma apresentação.",
                contact_type="email",
            )
            db.set_referral_report_location(
                referral["referral_id"],
                channel_id=1552631041175461979,
                message_id=999,
            )
            rows = db.referrals_for_partner(10, 100)
            self.assertEqual(referral["status"], "submitted")
            self.assertEqual(rows[0]["referral_id"], referral["referral_id"])
            self.assertEqual(rows[0]["need"], "Sistema interno")
            self.assertEqual(rows[0]["contact_type"], "email")
            self.assertEqual(rows[0]["report_channel_id"], 1552631041175461979)
            self.assertEqual(rows[0]["report_message_id"], 999)
            updated = db.update_referral_status(
                referral["referral_id"],
                status="reviewing",
                note="Contato em validação.",
                reviewed_by=77,
            )
            self.assertEqual(updated["status"], "reviewing")
            self.assertEqual(updated["status_note"], "Contato em validação.")
            self.assertEqual(updated["reviewed_by"], 77)
            self.assertEqual(
                db.referral_by_report_message(10, 999)["referral_id"],
                referral["referral_id"],
            )
            self.assertEqual(db.referral_statistics(10, 100), {"reviewing": 1, "total": 1})

    def test_partner_intelligence_calculates_level_and_next_action(self) -> None:
        profile = {
            "invite": {"invite_url": "https://discord.gg/bella"},
            "members": {"invited_total": 5, "active_total": 4},
            "commissions": {"paid": 200, "pending": 100},
        }
        referrals = {"accepted": 1, "closed": 1, "total": 2}

        evolution = _partner_evolution_summary(profile, referrals)
        diagnosis = _partner_funnel_diagnosis(profile, referrals)

        self.assertEqual(evolution["level"], "Estrategista")
        self.assertIn("Projeto conquistado", evolution["badges"])
        self.assertIn("etapa financeira", diagnosis["diagnosis"])

    def test_partner_currency_parser_accepts_brazilian_formats(self) -> None:
        self.assertEqual(_parse_partner_currency("R$ 5.000,00"), 5000)
        self.assertEqual(_parse_partner_currency("5000"), 5000)
        with self.assertRaises(ValueError):
            _parse_partner_currency("valor qualquer")

    def test_replacing_partner_link_deactivates_the_previous_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = PartnerDatabase(Path(directory) / "partners.db")
            db.upsert_partner(10, 100)
            for code in ("antigo", "novo"):
                db.save_invite(
                    guild_id=10,
                    partner_id=100,
                    invite_code=code,
                    invite_url=f"https://discord.gg/{code}",
                    visual_role_id=None,
                    source_channel_id=20,
                    expires_at=None,
                )
            changed = db.deactivate_partner_invites(10, 100, except_code="novo")
            self.assertEqual(changed, 1)
            self.assertIsNone(db.invite_by_code(10, "antigo"))
            self.assertEqual(db.active_invite_for_partner(10, 100)["invite_code"], "novo")


class PartnerWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_hub_message_is_converted_to_components_v2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PartnerProgramManager(
                SimpleNamespace(add_view=lambda _: None),
                StateStore(Path(directory) / "state.json"),
                owner_id=77,
            )
            manager.workspace_config = AsyncMock(
                return_value={"messages": {"hub": 900}}
            )
            manager._save_workspace = AsyncMock()
            message = MagicMock(spec=discord.Message)
            message.id = 900
            message.pinned = True
            message.edit = AsyncMock()
            channel = MagicMock(spec=discord.TextChannel)
            channel.fetch_message = AsyncMock(return_value=message)
            view = PartnerWorkspaceView(manager)

            result = await manager._upsert_workspace_panel(
                10,
                "hub",
                channel,
                None,
                view,
            )

            self.assertIs(result, message)
            message.edit.assert_awaited_once()
            kwargs = message.edit.await_args.kwargs
            self.assertEqual(kwargs["embeds"], [])
            self.assertIsNone(kwargs["content"])
            self.assertIs(kwargs["view"], view)

    async def test_referral_report_is_sent_to_configured_admin_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PartnerProgramManager(
                SimpleNamespace(add_view=lambda _: None),
                StateStore(Path(directory) / "state.json"),
                owner_id=77,
            )
            referral = manager.db.create_referral(
                guild_id=10,
                partner_id=100,
                contact_name="Empresa Exemplo",
                contact_method="contato@empresa.com.br",
                contact_type="email",
                need="Novo sistema",
                context="Contato autorizado pelo responsável.",
            )
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = PARTNER_REFERRAL_REPORT_CHANNEL_ID
            channel.send = AsyncMock(return_value=SimpleNamespace(id=900))
            guild = MagicMock(spec=discord.Guild)
            guild.id = 10
            guild.name = "Servidor"
            guild.get_channel.return_value = channel
            partner = MagicMock(spec=discord.Member)
            partner.id = 100
            partner.mention = "<@100>"

            sent = await manager._send_referral_report(
                guild,
                partner,
                referral,
                source_channel=SimpleNamespace(mention="<#20>"),
            )

            self.assertTrue(sent)
            guild.get_channel.assert_called_once_with(PARTNER_REFERRAL_REPORT_CHANNEL_ID)
            channel.send.assert_awaited_once()
            report_embed = channel.send.await_args.kwargs["embed"]
            report_text = "\n".join(
                str(field.value)
                for field in report_embed.fields
            )
            self.assertIn("contato@empresa.com.br", report_text)
            self.assertIn("Novo sistema", report_text)
            self.assertIn("Contato autorizado pelo responsável.", report_text)
            self.assertIsInstance(
                channel.send.await_args.kwargs["view"],
                PartnerReferralAdminView,
            )
            saved = manager.db.referrals_for_partner(10, 100)[0]
            self.assertEqual(saved["report_message_id"], 900)

    async def test_admin_status_update_notifies_partner_and_updates_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = SimpleNamespace(add_view=lambda _: None)
            manager = PartnerProgramManager(
                bot,
                StateStore(Path(directory) / "state.json"),
                owner_id=77,
            )
            referral = manager.db.create_referral(
                guild_id=10,
                partner_id=100,
                contact_name="Empresa Exemplo",
                contact_method="contato@empresa.com.br",
                contact_type="email",
                need="Novo sistema",
                context="Contato autorizado.",
            )
            manager.db.set_referral_report_location(
                referral["referral_id"],
                channel_id=PARTNER_REFERRAL_REPORT_CHANNEL_ID,
                message_id=900,
            )
            manager.audit_log = AsyncMock()

            partner = MagicMock(spec=discord.Member)
            partner.id = 100
            partner.send = AsyncMock()
            admin = MagicMock(spec=discord.Member)
            admin.id = 77
            admin.roles = []
            guild = MagicMock(spec=discord.Guild)
            guild.id = 10
            guild.get_member.return_value = partner
            report_message = MagicMock(spec=discord.Message)
            report_message.id = 900
            report_message.embeds = [
                discord.Embed(title="Nova indicação de parceiro").add_field(
                    name="Status",
                    value="Enviada para análise",
                )
            ]
            response = SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
            followup = SimpleNamespace(send=AsyncMock())
            interaction = SimpleNamespace(
                guild=guild,
                user=admin,
                message=report_message,
                response=response,
                followup=followup,
            )

            await manager.update_referral_status(interaction, status="accepted")

            saved = manager.db.referral(referral["referral_id"])
            self.assertEqual(saved["status"], "accepted")
            self.assertEqual(saved["reviewed_by"], 77)
            response.edit_message.assert_awaited_once()
            partner.send.assert_awaited_once()
            status_embed = partner.send.await_args.kwargs["embed"]
            self.assertIn("Aceita", status_embed.description)
            followup.send.assert_awaited_once()
            manager.audit_log.assert_awaited_once()

    async def test_removes_only_exact_deprecated_workspace_channels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PartnerProgramManager(
                SimpleNamespace(add_view=lambda _: None),
                StateStore(Path(directory) / "state.json"),
                owner_id=77,
            )
            category = MagicMock(spec=discord.CategoryChannel)
            category.id = 40
            actor = MagicMock(spec=discord.Member)
            actor.id = 77
            deprecated = MagicMock(spec=discord.TextChannel)
            deprecated.id = 60
            deprecated.name = "🌐・networking"
            deprecated.category_id = 40
            deprecated.delete = AsyncMock()
            renamed = MagicMock(spec=discord.TextChannel)
            renamed.id = 61
            renamed.name = "networking-renomeado"
            renamed.category_id = 40
            renamed.delete = AsyncMock()
            guild = MagicMock(spec=discord.Guild)
            guild.text_channels = [deprecated, renamed]
            guild.get_channel.side_effect = lambda channel_id: (
                deprecated if channel_id == 60 else renamed if channel_id == 61 else None
            )
            stored = {"networking": 60, "opportunities": 61}

            removed = await manager._remove_deprecated_workspace_channels(
                guild,
                category,
                stored,
                actor=actor,
            )

            self.assertEqual(removed, ["networking"])
            deprecated.delete.assert_awaited_once()
            renamed.delete.assert_not_awaited()
            self.assertNotIn("networking", stored)
            self.assertNotIn("opportunities", stored)

    async def test_partner_supplied_permanent_link_is_saved_and_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PartnerProgramManager(
                SimpleNamespace(add_view=lambda _: None),
                StateStore(Path(directory) / "state.json"),
                owner_id=77,
            )
            manager.db.save_settings(PartnerSettings(guild_id=10, partner_role_id=30))
            manager.db.upsert_partner(10, 100, role_id=30)
            manager.audit_log = AsyncMock()

            member = MagicMock(spec=discord.Member)
            member.id = 100
            member.roles = []
            member.mention = "<@100>"
            member.name = "Parceiro"
            member.display_name = "Parceiro"
            member.display_avatar = None
            invite = SimpleNamespace(
                code="bella123",
                url="https://discord.gg/bella123",
                uses=4,
                max_age=0,
                max_uses=0,
                temporary=False,
                expires_at=None,
                channel=SimpleNamespace(id=20),
            )
            guild = MagicMock(spec=discord.Guild)
            guild.id = 10
            guild.name = "Servidor"
            guild.invites = AsyncMock(return_value=[invite])
            member.guild = guild
            response = SimpleNamespace(defer=AsyncMock())
            followup = SimpleNamespace(send=AsyncMock())
            interaction = SimpleNamespace(
                guild=guild,
                user=member,
                response=response,
                followup=followup,
            )

            await manager.register_partner_invite(
                interaction,
                "https://discord.gg/bella123",
            )

            saved = manager.db.active_invite_for_partner(10, 100)
            self.assertEqual(saved["invite_code"], "bella123")
            self.assertIsNone(saved["expires_at"])
            self.assertEqual(manager.invite_cache[10]["bella123"], 4)
            response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
            followup.send.assert_awaited_once()
            manager.audit_log.assert_awaited_once()

    async def test_each_workspace_panel_receives_only_its_own_view(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PartnerProgramManager(
                SimpleNamespace(add_view=lambda _: None),
                StateStore(Path(directory) / "state.json"),
                owner_id=77,
            )
            manager._upsert_workspace_panel = AsyncMock(
                side_effect=[SimpleNamespace(id=value) for value in range(1, 5)]
            )
            channels = {
                identifier: MagicMock(spec=discord.TextChannel)
                for identifier in ("hub", "indications", "materials", "support")
            }

            await manager.publish_workspace_panels(
                SimpleNamespace(id=10),
                channels,
                PartnerSettings(guild_id=10),
            )

            views = {
                call.args[1]: call.args[4]
                for call in manager._upsert_workspace_panel.await_args_list
            }
            self.assertIsInstance(views["hub"], PartnerWorkspaceView)
            self.assertIsInstance(views["indications"], PartnerIndicationsView)
            self.assertIsNone(views["materials"])
            self.assertIsInstance(views["support"], PartnerSupportView)

    async def test_partner_support_uses_injected_ticket_opener(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            opener = AsyncMock()
            manager = PartnerProgramManager(
                SimpleNamespace(add_view=lambda _: None),
                StateStore(Path(directory) / "state.json"),
                owner_id=77,
                ticket_opener=opener,
            )
            manager.settings = AsyncMock(return_value=PartnerSettings(guild_id=10))
            manager._member_is_partner = AsyncMock(return_value=True)
            guild = MagicMock(spec=discord.Guild)
            guild.id = 10
            member = MagicMock(spec=discord.Member)
            member.id = 100
            member.guild = guild
            interaction = SimpleNamespace(
                guild=guild,
                user=member,
                response=SimpleNamespace(send_message=AsyncMock()),
            )

            await manager.open_partner_support_ticket(interaction)

            opener.assert_awaited_once_with(interaction)

    async def test_provisioning_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.json")
            manager = PartnerProgramManager(
                SimpleNamespace(add_view=lambda _: None),
                state,
                owner_id=77,
            )
            manager.publish_workspace_panels = AsyncMock(return_value={"hub": 900})
            manager.audit_log = AsyncMock()

            default_role = MagicMock(spec=discord.Role)
            default_role.id = 1
            default_role.name = "@everyone"
            bot_member = MagicMock(spec=discord.Member)
            bot_member.id = 2
            actor = MagicMock(spec=discord.Member)
            actor.id = 77
            actor.roles = []
            roles = [default_role]
            categories = []
            text_channels = []
            voice_channels = []
            resources: dict[int, object] = {}

            guild = MagicMock(spec=discord.Guild)
            guild.id = 10
            guild.default_role = default_role
            guild.me = bot_member
            guild.roles = roles
            guild.categories = categories
            guild.text_channels = text_channels
            guild.voice_channels = voice_channels
            guild.get_member.side_effect = lambda member_id: actor if member_id == 77 else None
            guild.get_role.side_effect = lambda role_id: next(
                (role for role in roles if role.id == role_id),
                None,
            )
            guild.get_channel.side_effect = resources.get

            async def create_role(**kwargs: object) -> discord.Role:
                role = MagicMock(spec=discord.Role)
                role.id = 30
                role.name = kwargs["name"]
                roles.append(role)
                return role

            async def create_category(name: str, **_: object) -> discord.CategoryChannel:
                category = MagicMock(spec=discord.CategoryChannel)
                category.id = 40
                category.name = name
                category.edit = AsyncMock()
                categories.append(category)
                resources[category.id] = category
                return category

            next_channel_id = 100

            async def create_text_channel(name: str, **kwargs: object) -> discord.TextChannel:
                nonlocal next_channel_id
                channel = MagicMock(spec=discord.TextChannel)
                channel.id = next_channel_id
                next_channel_id += 1
                channel.name = name
                channel.category_id = kwargs["category"].id
                channel.edit = AsyncMock()
                text_channels.append(channel)
                resources[channel.id] = channel
                return channel

            async def create_voice_channel(name: str, **kwargs: object) -> discord.VoiceChannel:
                nonlocal next_channel_id
                channel = MagicMock(spec=discord.VoiceChannel)
                channel.id = next_channel_id
                next_channel_id += 1
                channel.name = name
                channel.category_id = kwargs["category"].id
                channel.edit = AsyncMock()
                voice_channels.append(channel)
                resources[channel.id] = channel
                return channel

            guild.create_role = AsyncMock(side_effect=create_role)
            guild.create_category = AsyncMock(side_effect=create_category)
            guild.create_text_channel = AsyncMock(side_effect=create_text_channel)
            guild.create_voice_channel = AsyncMock(side_effect=create_voice_channel)

            first = await manager.provision_workspace(guild=guild, actor=actor)
            second = await manager.provision_workspace(guild=guild, actor=actor)

            self.assertEqual(
                len(first.created_channels),
                len(PARTNER_WORKSPACE_CHANNELS),
            )
            self.assertFalse(second.created_channels)
            self.assertEqual(
                len(second.reused_channels),
                len(PARTNER_WORKSPACE_CHANNELS),
            )
            self.assertEqual(guild.create_role.await_count, 1)
            self.assertEqual(guild.create_category.await_count, 1)
            self.assertEqual(
                guild.create_text_channel.await_count,
                len([item for item in PARTNER_WORKSPACE_CHANNELS if item.kind == "text"]),
            )
            self.assertEqual(
                guild.create_voice_channel.await_count,
                len([item for item in PARTNER_WORKSPACE_CHANNELS if item.kind == "voice"]),
            )
            self.assertEqual(manager.publish_workspace_panels.await_count, 2)


if __name__ == "__main__":
    unittest.main()
