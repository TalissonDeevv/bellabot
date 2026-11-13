import asyncio
from datetime import date, datetime, timezone

from website_guard.birthday_celebration import (
    BirthdayConfig,
    birthday_age,
    birthday_is_due,
    next_birthday,
)


def make_config(**updates):
    values = {
        "enabled": True,
        "user_id": 933780111742877777,
        "channel_id": 1467981739937759304,
        "birth_date": "2004-11-10",
        "timezone": "America/Recife",
        "hour": 0,
        "minute": 0,
        "last_sent_year": 0,
        "last_message_id": 0,
        "last_sent_at": "",
        "last_error": "",
    }
    values.update(updates)
    return BirthdayConfig(**values)


def test_age_on_2026_birthday_is_22():
    assert birthday_age(date(2004, 11, 10), date(2026, 11, 10)) == 22


def test_birthday_is_due_at_midnight_in_recife():
    config = make_config()
    now = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)
    assert birthday_is_due(config, now) is True


def test_birthday_is_not_sent_twice_in_same_year():
    config = make_config(last_sent_year=2026)
    now = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
    assert birthday_is_due(config, now) is False


def test_worker_catches_up_later_on_birthday():
    config = make_config()
    now = datetime(2026, 11, 10, 18, 30, tzinfo=timezone.utc)
    assert birthday_is_due(config, now) is True


def test_next_birthday_has_discord_target_and_age():
    config = make_config()
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    target, age, is_today = next_birthday(config, now)
    assert target.isoformat() == "2026-11-10T00:00:00-03:00"
    assert age == 22
    assert is_today is False


def test_next_birthday_rolls_to_next_year_after_date():
    config = make_config()
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    target, age, is_today = next_birthday(config, now)
    assert target.isoformat() == "2027-11-10T00:00:00-03:00"
    assert age == 23
    assert is_today is False


def test_ai_countdown_is_conversational_and_keeps_timestamps():
    class AIStub:
        async def generate_text(self, *args, **kwargs):
            return (
                "Falta pouquinho, hihihi! Você nasceu em 10/11/2004. "
                "Já estou animada para comemorar com você."
            )

    from website_guard.birthday_celebration import BirthdayCelebrationService

    service = BirthdayCelebrationService(
        bot=None,
        state=None,
        ai_service=AIStub(),
        user_id=933780111742877777,
        channel_id=1467981739937759304,
    )
    text = asyncio.run(
        service._countdown_text(
            22,
            datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc),
            False,
            1794279600,
        )
    )
    assert "<t:1794279600:F>" in text
    assert "<t:1794279600:R>" in text
    assert "22" in text
    assert "**Data:**" not in text
    assert "**Contagem:**" not in text
    assert "2004" not in text
    assert "10/11/2004" not in text
