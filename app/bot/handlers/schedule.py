from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.handlers.calendar import calendar_view, read_date
from app.bot.ui import Screen, navigation, number, pagination
from app.database.models import User, utcnow
from app.exceptions import DomainError
from app.services.booking import BookingService
from app.services.settings import SettingsService
from app.utils.time import local_date, local_instant

router = Router(name="schedule")


def schedule_lines(
    day: date, days: int, entries: list[tuple[datetime, datetime, str]], zone: ZoneInfo
) -> list[str]:
    lines = []
    for offset in range(days):
        current = day + timedelta(days=offset)
        first = local_instant(current, 0, zone)
        last = local_instant(current + timedelta(days=1), 0, zone)
        for start, end, label in entries:
            start, end = max(start, first), min(end, last)
            if start < end:
                finish = "24:00" if end == last else f"{end.astimezone(zone):%H:%M}"
                lines.append(
                    f"{local_date(current)}, {start.astimezone(zone):%H:%M}–{finish} · {label}"
                )
    return lines


@router.callback_query(F.data.startswith("s:"))
async def schedule(
    query: CallbackQuery,
    screen: Screen,
    actor: User,
    booking: BookingService,
    settings: SettingsService,
) -> None:
    _, command, *args = (query.data or "").split(":")
    await screen.reset()
    options = await settings.get()
    zone = ZoneInfo(options.timezone)
    today = utcnow().astimezone(zone).date()
    if command == "home":
        await screen.show(
            "📅 Расписание студии",
            [
                [("Сегодня", "s:today"), ("Завтра", "s:tomorrow")],
                [("Текущая неделя", "s:week"), ("Выбрать дату", f"s:month:{today}")],
            ]
            + navigation(),
        )
        return
    if command == "month" and len(args) == 1:
        month = read_date(args[0])
        message, rows = calendar_view(
            month,
            today,
            month_prefix="s:month",
            day_prefix="s:day",
            back="s:home",
            earliest=date(1970, 1, 1),
            latest=date(2200, 12, 31),
        )
        await screen.show(message, rows)
        return
    if command == "today":
        day = today
    elif command == "tomorrow":
        day = today + timedelta(days=1)
    elif command == "week":
        day = today - timedelta(days=today.weekday())
    elif command == "day" and args:
        day = read_date(args[0])
    else:
        raise DomainError("Выберите дату в расписании.")
    days = 7 if command == "week" else 1
    page = number(args[-1], 10**6) if len(args) == (2 if command == "day" else 1) else 0
    bookings, blocks = await booking.schedule(actor.id, day, days)
    entries = [(item.start_at, item.end_at, f"🔴 {name}") for item, name in bookings]
    entries.extend((block.start_at, block.end_at, "⚫ Студия закрыта") for block in blocks)
    entries.sort()
    fragments = schedule_lines(day, days, entries, zone)
    date_label = local_date(day)
    if days == 7:
        date_label += f" — {local_date(day + timedelta(days=6))}"
    lines = ["📅 " + date_label, ""]
    lines.extend(fragments[page * 12 : (page + 1) * 12])
    if len(lines) == 2:
        lines.append("Занятых интервалов нет." if not fragments else "Страница устарела.")
    prefix = f"s:day:{day}" if command == "day" else f"s:{command}"
    await screen.show(
        "\n".join(lines),
        pagination(prefix, page, (page + 1) * 12 < len(fragments))
        + navigation(f"s:month:{day}" if command == "day" else "s:home"),
    )
