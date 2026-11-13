from __future__ import annotations

from dataclasses import dataclass

import discord
import pytest

from website_guard.staff_center import (
    CATEGORY_NAME,
    CHANNEL_DISPLAY_NAMES,
    DANGEROUS_HELPER_PERMISSION_NAMES,
    LEADERSHIP_CHANNELS,
    SAFE_HELPER_PERMISSION_NAMES,
    SHARED_CHANNELS,
    StaffCenter,
    StaffConfig,
    SupremeAdminPanelView,
    SupremeCriticalActionModal,
    build_quiz_item,
    can_manage_staff,
    mass_ban_candidates,
    quiz_catalog_size,
    staff_level,
    warning_escalation,
    _channel_name_with_divider,
)
from website_guard.state import StateStore
from website_guard.tickets import (
    TicketConfig,
    helper_can_access_ticket_category,
)


@dataclass
class FakeRole:
    id: int


@dataclass
class FakeMember:
    id: int
    roles: list[FakeRole]
    bot: bool = False


class FakeTicketStore:
    def __init__(self) -> None:
        self.value: TicketConfig | None = None

    async def config(self, _: int) -> TicketConfig | None:
        return self.value

    async def save_config(self, config: TicketConfig) -> None:
        self.value = config


class FakeTickets:
    def __init__(self) -> None:
        self.store = FakeTicketStore()


class FakeLogs:
    async def send_event(self, *args, **kwargs) -> None:
        return None


def make_center(tmp_path) -> StaffCenter:
    return StaffCenter(
        bot=object(),
        state=StateStore(tmp_path / "state.json"),
        tickets=FakeTickets(),
        logs=FakeLogs(),
        authorize=lambda _: False,
    )


def test_staff_hierarchy_is_explicit() -> None:
    config = StaffConfig(
        guild_id=1,
        president_role_id=10,
        vice_president_role_id=20,
        helper_role_id=30,
    )
    assert staff_level(FakeMember(1, [FakeRole(10)]), config) == "president"
    assert staff_level(FakeMember(2, [FakeRole(20)]), config) == "vice_president"
    assert staff_level(FakeMember(3, [FakeRole(30)]), config) == "helper"
    assert staff_level(FakeMember(4, []), config) == "none"
    assert can_manage_staff("president", "vice_president") is True
    assert can_manage_staff("vice_president", "helper") is True
    assert can_manage_staff("helper", "none") is False
    assert can_manage_staff("vice_president", "president") is False


def test_helper_permission_baseline_contains_no_dangerous_permissions() -> None:
    assert SAFE_HELPER_PERMISSION_NAMES.isdisjoint(DANGEROUS_HELPER_PERMISSION_NAMES)
    assert "view_channel" in SAFE_HELPER_PERMISSION_NAMES
    assert "send_messages" in SAFE_HELPER_PERMISSION_NAMES


def test_staff_resources_use_emoji_and_divider() -> None:
    assert CATEGORY_NAME.startswith("🛡️ ")
    assert set(CHANNEL_DISPLAY_NAMES) == set((*SHARED_CHANNELS, *LEADERSHIP_CHANNELS))
    assert all("╺╸" in name for name in CHANNEL_DISPLAY_NAMES.values())
    assert all("──" not in name for name in CHANNEL_DISPLAY_NAMES.values())


def test_warning_escalation_policy() -> None:
    assert warning_escalation(1) == ("warning", None)
    assert warning_escalation(3) == ("timeout", 60)
    assert warning_escalation(6) == ("kick", None)
    assert warning_escalation(12) == ("ban", None)
    assert warning_escalation(13) == ("warning", None)


def test_existing_channel_dividers_are_normalized() -> None:
    assert _channel_name_with_divider("📊 ── relatorios-staff") == "📊╺╸relatorios-staff"
    assert _channel_name_with_divider("📋 ─── auditoria-staff") == "📋╺╸auditoria-staff"
    assert _channel_name_with_divider("🚨╺╸ocorrencias") == "🚨╺╸ocorrencias"


def test_mass_ban_excludes_owner_bot_and_president() -> None:
    target_role = FakeRole(77)
    president_role = FakeRole(1467982180469833979)
    members = [
        FakeMember(1, [target_role]),
        FakeMember(2, [target_role]),
        FakeMember(3, [target_role], bot=True),
        FakeMember(4, [target_role, president_role]),
        FakeMember(5, [FakeRole(88)]),
    ]

    candidates = mass_ban_candidates(
        members,
        role_id=target_role.id,
        owner_id=1,
        bot_user_id=2,
    )

    assert candidates == []


def test_supreme_panel_has_persistent_critical_actions(tmp_path) -> None:
    view = SupremeAdminPanelView(make_center(tmp_path))
    custom_ids = {item.custom_id for item in view.children if isinstance(item, discord.ui.Button)}
    assert {
        "bella:staff:supreme:mass-ban",
        "bella:staff:supreme:lockdown",
        "bella:staff:supreme:restore-lockdown",
        "bella:staff:supreme:block-public",
        "bella:staff:supreme:release-public",
        "bella:staff:supreme:containment",
        "bella:staff:supreme:restore-all",
        "bella:staff:supreme:diagnostics",
    } <= custom_ids


def test_lockdown_confirmation_collects_announcement(tmp_path) -> None:
    modal = SupremeCriticalActionModal(make_center(tmp_path), "lockdown", "TRANCAR SERVIDOR")
    labels = {getattr(item, "label", "") for item in modal.children}
    assert "ID do canal público do anúncio" in labels
    assert "Anúncio enviado após o lockdown" in labels
    assert len(modal.children) == 4


@pytest.mark.parametrize(
    "category",
    ["D\u00favida simples", "Suporte", "Feedback", "Sugest\u00e3o", "Problema t\u00e9cnico", "Outro"],
)
def test_helper_can_access_general_ticket_categories(category: str) -> None:
    assert helper_can_access_ticket_category(category) is True


@pytest.mark.parametrize(
    "category",
    ["Or\u00e7amento", "Financeiro", "Comercial", "Parceria", "Reuni\u00e3o", "Assunto administrativo", "categoria nova"],
)
def test_helper_ticket_access_fails_closed(category: str) -> None:
    assert helper_can_access_ticket_category(category) is False


@pytest.mark.asyncio
async def test_config_and_records_are_persistent(tmp_path) -> None:
    center = make_center(tmp_path)
    config = StaffConfig(
        guild_id=123,
        configured=True,
        helper_role_id=30,
        channel_ids={name: index for index, name in enumerate((*SHARED_CHANNELS, *LEADERSHIP_CHANNELS), 100)},
    )
    await center.save_config(config)
    loaded = await center.config(123)
    assert loaded.configured is True
    assert loaded.helper_role_id == 30
    assert set(loaded.channel_ids) == set((*SHARED_CHANNELS, *LEADERSHIP_CHANNELS))

    first = await center.create_occurrence(123, 1, 2, "Teste", "Evidência")
    second = await center.create_occurrence(123, 1, 3, "Teste 2", "Evidência 2")
    warning = await center.create_warning(123, 1, 2, "Orientação")
    request = await center.create_intervention(123, 2, "Ajuda", "Revisar situação")
    await center.set_training_progress(123, 2, 125)

    assert first["id"] == 1
    assert second["id"] == 2
    assert warning["id"] == 1
    assert request["status"] == "pending"
    training = await center.state.get_section("staff_center_training")
    assert training["123:2"]["progress"] == 100


@pytest.mark.asyncio
async def test_ticket_integration_persists_structured_helper_rules(tmp_path) -> None:
    center = make_center(tmp_path)
    center.tickets.store.value = TicketConfig(guild_id=123, opening_channel_id=456)
    config = StaffConfig(guild_id=123, configured=True, helper_role_id=789)

    await center._integrate_tickets(config)

    ticket_config = center.tickets.store.value
    assert ticket_config is not None
    assert ticket_config.staff_center_helper_role_id == 789
    assert "suporte" in ticket_config.staff_center_allowed_category_keys
    assert "comercial" in ticket_config.staff_center_restricted_category_keys


@pytest.mark.asyncio
async def test_training_progress_tracks_modules_quizzes_and_scenarios(tmp_path) -> None:
    center = make_center(tmp_path)
    profile = await center.complete_training_module(123, 456, "fundamentos")
    assert profile["xp"] == 100
    profile = await center.record_quiz_result(123, 456, "fundamentos", True)
    assert profile["xp"] == 250
    profile = await center.record_scenario_review(123, 456, True)
    assert profile["xp"] == 325
    assert profile["scenario_reviews"] == 1


def test_quiz_catalog_has_thousands_of_article_based_combinations() -> None:
    assert quiz_catalog_size() >= 5000
    questions = [build_quiz_item(456, number) for number in range(200)]
    assert len({item["id"] for item in questions}) >= 150
    assert all(item["answers"][item["correct"]] == item["explanation"] for item in questions)


@pytest.mark.asyncio
async def test_quiz_tracks_unique_questions_and_module_completion(tmp_path) -> None:
    center = make_center(tmp_path)
    items = []
    number = 0
    while len(items) < 5:
        item = build_quiz_item(456, number)
        number += 1
        if item["module"] == "fundamentos":
            items.append(item)
    for item in items:
        profile = await center.record_quiz_result(
            123,
            456,
            "fundamentos",
            True,
            question_id=item["id"],
        )
    assert profile["quiz_attempts"] == 5
    assert profile["quiz_correct"] == 5
    assert profile["quiz_correct_by_module"]["fundamentos"] == 5
    assert "fundamentos" in profile["passed_quizzes"]
    assert profile["xp"] == 225


def test_ticket_config_round_trip_keeps_staff_rules() -> None:
    original = TicketConfig(
        guild_id=1,
        opening_channel_id=2,
        staff_center_helper_role_id=3,
        staff_center_allowed_category_keys=["suporte"],
        staff_center_restricted_category_keys=["comercial"],
    )
    loaded = TicketConfig.from_dict(original.to_dict())
    assert loaded.staff_center_helper_role_id == 3
    assert loaded.staff_center_allowed_category_keys == ["suporte"]
    assert loaded.staff_center_restricted_category_keys == ["comercial"]
