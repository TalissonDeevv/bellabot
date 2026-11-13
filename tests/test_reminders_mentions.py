from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

from website_guard.discord_entities import (
    DiscordEntity,
    allowed_mention_ids,
    extract_entities,
    render_mentions,
    resolve_by_name,
    sanitize_model_mentions,
)
from website_guard.reminder_service import (
    SECTION,
    ReminderService,
    looks_like_reminder_request,
    parse_when,
)
from website_guard.state import StateStore

RECIFE = ZoneInfo("America/Recife")
GUILD_ID = 1467976297689911377
ROLE_CLIENTES = 100000000000000010
ROLE_STAFF = 110000000000000011
VOICE_ID = 222000000000000222


# ------------------------------------------------------------------ entidades
class TextChannel(SimpleNamespace):
    pass


class VoiceChannel(SimpleNamespace):
    pass


def _guild(*, can_mention_all: bool = False):
    channels = [
        TextChannel(id=1514971417651839197, name="loja"),
        VoiceChannel(id=VOICE_ID, name="Reunião"),
        TextChannel(id=333000000000000333, name="avisos-gerais"),
        TextChannel(id=334000000000000334, name="avisos-staff"),
    ]
    roles = [
        SimpleNamespace(id=ROLE_CLIENTES, name="Clientes", mentionable=True, is_default=lambda: False),
        SimpleNamespace(id=ROLE_STAFF, name="Staff", mentionable=False, is_default=lambda: False),
    ]
    by_id = {c.id: c for c in channels}
    return SimpleNamespace(
        id=GUILD_ID,
        channels=channels,
        roles=roles,
        get_channel=by_id.get,
        get_role=lambda rid: next((r for r in roles if r.id == rid), None),
        get_member=lambda uid: SimpleNamespace(display_name="Fulano") if uid == 550000000000000055 else None,
        me=SimpleNamespace(guild_permissions=SimpleNamespace(mention_everyone=can_mention_all)),
    )


class EntityTests(unittest.TestCase):
    def test_channel_link_becomes_text_channel_entity(self) -> None:
        guild = _guild()
        found = extract_entities(
            f"compra em https://discord.com/channels/{GUILD_ID}/1514971417651839197", guild
        )
        self.assertEqual(found[0].type, "text_channel")
        self.assertEqual(found[0].mention, "<#1514971417651839197>")

    def test_link_from_other_guild_is_ignored(self) -> None:
        self.assertEqual(extract_entities("https://discord.com/channels/999/1514971417651839197", _guild()), [])

    def test_role_and_voice_mentions(self) -> None:
        found = {e.type: e for e in extract_entities("<@&100000000000000010> entra na <#222000000000000222>", _guild())}
        self.assertEqual(found["role"].mention, "<@&100000000000000010>")
        self.assertEqual(found["voice_channel"].mention, "<#222000000000000222>")

    def test_unknown_ids_are_dropped(self) -> None:
        self.assertEqual(extract_entities("<@&999000000000000999> <#888000000000000888>", _guild()), [])

    def test_name_resolution_and_ambiguity(self) -> None:
        guild = _guild()
        self.assertEqual(resolve_by_name(guild, "role", "clientes").entity.entity_id, ROLE_CLIENTES)
        self.assertEqual(resolve_by_name(guild, "voice_channel", "reuniao").entity.entity_id, VOICE_ID)
        self.assertTrue(resolve_by_name(guild, "text_channel", "avisos").ambiguous)

    def test_render_replaces_link_with_mention(self) -> None:
        entity = DiscordEntity("text_channel", 1514971417651839197, GUILD_ID, "loja")
        text = render_mentions(
            f"compra aqui: https://discord.com/channels/{GUILD_ID}/1514971417651839197", [entity]
        )
        self.assertEqual(text, "compra aqui: <#1514971417651839197>")

    def test_invented_mentions_are_removed(self) -> None:
        cleaned = sanitize_model_mentions("fala com <@&999000000000000999> no <#888000000000000888> ou <#222000000000000222>", guild=_guild())
        self.assertNotIn("999000000000000999", cleaned)
        self.assertNotIn("888000000000000888", cleaned)
        self.assertIn("<#222000000000000222>", cleaned)

    def test_allowed_mentions_never_ping_unmentionable_roles(self) -> None:
        entities = [
            DiscordEntity("role", ROLE_CLIENTES, GUILD_ID, "Clientes"),
            DiscordEntity("role", ROLE_STAFF, GUILD_ID, "Staff"),
            DiscordEntity("user", 550000000000000055, GUILD_ID, "Fulano"),
        ]
        users, roles = allowed_mention_ids(author_id=7, entities=entities, guild=_guild())
        self.assertEqual(users, [7, 550000000000000055])
        self.assertEqual(roles, [ROLE_CLIENTES])
        _, roles = allowed_mention_ids(author_id=7, entities=entities, guild=_guild(can_mention_all=True))
        self.assertEqual(roles, [ROLE_CLIENTES, ROLE_STAFF])


# ------------------------------------------------------------------ lembretes
class FakeUser:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[str] = []
        self.error = error

    async def send(self, text, **kwargs):
        if self.error is not None:
            raise self.error
        self.sent.append(text)
        return SimpleNamespace(id=len(self.sent))


class FakeBot:
    def __init__(self, user: FakeUser, channel=None) -> None:
        self.user = user
        self.channel = channel

    def get_user(self, user_id):
        return self.user

    def get_channel(self, channel_id):
        return self.channel


def _http_error(status: int, cls=discord.HTTPException):
    return cls(SimpleNamespace(status=status, reason="x"), "erro")


class ReminderParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 11, 13, 10, 0, tzinfo=RECIFE)  # sexta

    def test_relative_and_named_days(self) -> None:
        self.assertEqual(parse_when("amanhã às 15h", self.now)[0], datetime(2026, 11, 14, 15, 0, tzinfo=RECIFE))
        self.assertEqual(parse_when("dia 20/11 às 9:30", self.now)[0], datetime(2026, 11, 20, 9, 30, tzinfo=RECIFE))
        self.assertEqual(parse_when("daqui a 2 horas", self.now)[0], datetime(2026, 11, 13, 12, 0, tzinfo=RECIFE))
        self.assertEqual(parse_when("segunda às 8h", self.now)[0], datetime(2026, 11, 16, 8, 0, tzinfo=RECIFE))
        self.assertEqual(parse_when("hoje às 7 da noite", self.now)[0], datetime(2026, 11, 13, 19, 0, tzinfo=RECIFE))

    def test_missing_time_is_reported(self) -> None:
        when, missing = parse_when("amanhã tenho reunião", self.now)
        self.assertIsNone(when)
        self.assertIn("horário", missing)

    def test_intent_detection(self) -> None:
        self.assertTrue(looks_like_reminder_request("Bella, me lembra da reunião amanhã às 15h"))
        self.assertTrue(looks_like_reminder_request("quais lembretes eu tenho?"))
        self.assertTrue(looks_like_reminder_request("cancela o lembrete da reunião"))
        self.assertFalse(looks_like_reminder_request("lembra quando a gente jogou ontem kkkk"))


class ReminderSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self._dir.name) / "s.json")

    async def asyncTearDown(self) -> None:
        self._dir.cleanup()

    def _service(self, user: FakeUser, channel=None) -> ReminderService:
        return ReminderService(FakeBot(user, channel), self.state, ai_service=None, poll_seconds=0)

    async def _due(self, service: ReminderService, **kwargs) -> dict:
        record = await service.create(
            user_id=7, description="reunião sobre o projeto",
            event_at=service.now() + timedelta(minutes=3), channel_id=5, **kwargs,
        )
        return record

    async def test_persisted_with_utc_and_original_timezone(self) -> None:
        service = self._service(FakeUser())
        event = service.now() + timedelta(days=1)
        record = await service.create(user_id=7, description="dentista", event_at=event)
        stored = (await self.state.get_section(SECTION))[record["id"]]
        self.assertEqual(stored["timezone"], "America/Recife")
        self.assertTrue(stored["event_at_utc"].endswith("+00:00"))
        notify = datetime.fromisoformat(stored["notify_at_utc"])
        self.assertEqual(datetime.fromisoformat(stored["event_at_utc"]) - notify, timedelta(minutes=5))
        # Outro processo (reinício) enxerga o mesmo lembrete.
        reloaded = StateStore(Path(self._dir.name) / "s.json")
        self.assertIn(record["id"], await reloaded.get_section(SECTION))

    async def test_sends_once_even_if_ticked_twice(self) -> None:
        user = FakeUser()
        service = self._service(user)
        record = await self._due(service)
        await service.tick()
        await service.tick()
        self.assertEqual(len(user.sent), 1)
        self.assertIn("reunião sobre o projeto", user.sent[0])
        stored = (await self.state.get_section(SECTION))[record["id"]]
        self.assertEqual(stored["status"], "SENT")
        self.assertEqual(stored["text_origin"], "contingencia")

    async def test_future_reminder_is_not_sent_early(self) -> None:
        user = FakeUser()
        service = self._service(user)
        await service.create(user_id=7, description="x", event_at=service.now() + timedelta(hours=2))
        await service.tick()
        self.assertEqual(user.sent, [])

    async def test_closed_dm_falls_back_to_channel(self) -> None:
        channel = SimpleNamespace(sent=[])

        async def send(text, **kwargs):
            channel.sent.append((text, kwargs["allowed_mentions"]))
            return SimpleNamespace(id=1)

        channel.send = send
        service = self._service(FakeUser(_http_error(403, discord.Forbidden)), channel)
        record = await self._due(service)
        await service.tick()
        self.assertTrue(channel.sent[0][0].startswith("<@7>"))
        stored = (await self.state.get_section(SECTION))[record["id"]]
        self.assertEqual(stored["delivered_via"], "canal")

    async def test_temporary_failure_is_retried_later(self) -> None:
        service = self._service(FakeUser(_http_error(500)))
        record = await self._due(service)
        await service.tick()
        stored = (await self.state.get_section(SECTION))[record["id"]]
        self.assertEqual(stored["status"], "PENDING")
        self.assertTrue(stored["next_attempt_at"])
        logs = (await self.state.get_section("bella_reminder_delivery_logs"))["log"]
        self.assertEqual(logs[-1]["outcome"], "retry")

    async def test_stale_processing_is_recovered_and_old_expired(self) -> None:
        user = FakeUser()
        service = self._service(user)
        stuck = await self._due(service)
        old = await self._due(service)
        past = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()

        def mutate(section):
            section[stuck["id"]].update(status="PROCESSING", claimed_at=past)
            section[old["id"]]["event_at_utc"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

        await self.state.mutate_section(SECTION, mutate)
        await service.tick()
        section = await self.state.get_section(SECTION)
        self.assertEqual(section[stuck["id"]]["status"], "SENT")
        self.assertEqual(section[old["id"]]["status"], "EXPIRED")

    async def test_natural_create_list_cancel_with_ambiguity(self) -> None:
        service = self._service(FakeUser())
        message = SimpleNamespace(author=SimpleNamespace(id=7), guild=None, channel=SimpleNamespace(id=5), id=1)
        tomorrow = (service.now() + timedelta(days=1)).strftime("%d/%m")
        answer = await service.handle_natural(message, f"me lembra da reunião com cliente dia {tomorrow} às 15h")
        self.assertIn("vou te lembrar", answer)
        await service.handle_natural(message, f"me lembra da reunião com fornecedor dia {tomorrow} às 17h")
        listing = await service.handle_natural(message, "quais lembretes eu tenho?")
        self.assertIsNotNone(listing)
        service.ai_service = SimpleNamespace(generate_json=_fixed({"operation": "cancel", "description": "", "datetime_local": "", "target": "reunião"}))
        ambiguous = await service.handle_natural(message, "cancela o lembrete da reunião")
        self.assertIn("qual deles", ambiguous)
        service.ai_service = SimpleNamespace(generate_json=_fixed({"operation": "cancel", "description": "", "datetime_local": "", "target": "fornecedor"}))
        done = await service.handle_natural(message, "cancela o lembrete do fornecedor")
        self.assertIn("cancelado", done)
        self.assertEqual(len(await service.list_for(7)), 1)

    async def test_past_time_is_refused(self) -> None:
        service = self._service(FakeUser())
        with self.assertRaises(ValueError):
            await service.create(user_id=7, description="x", event_at=service.now() - timedelta(minutes=1))


def _fixed(value):
    async def generate_json(*args, **kwargs):
        return dict(value)

    return generate_json


if __name__ == "__main__":
    unittest.main()
