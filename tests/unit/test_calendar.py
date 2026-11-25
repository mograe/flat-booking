from datetime import date

from app.bot.handlers.calendar import calendar_view


def test_calendar_monday_and_navigation():
    today = date(2026, 11, 28)
    _, rows = calendar_view(
        today,
        today,
        month_prefix="month",
        day_prefix="day",
        back="book",
        earliest=today,
        latest=date(2026, 12, 12),
        available={today},
    )
    assert rows[0][0] == ("Пн", "noop")
    assert rows[1][-1] == ("1", "noop")
    assert ("‹28›🟢", "day:2026-11-28") in rows[5]
    assert ("29⚫", "noop") in rows[5]
    assert rows[-2] == [("Месяц ›", "month:2026-12-01")]
