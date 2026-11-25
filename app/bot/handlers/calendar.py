import calendar
from datetime import date, timedelta

from app.bot.ui import Rows, navigation
from app.exceptions import DomainError
from app.utils.time import MONTHS

MONTH_NAMES = (
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)


def read_date(raw: str) -> date:
    try:
        value = date.fromisoformat(raw)
        if value.isoformat() != raw or not 1970 <= value.year <= 2200:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise DomainError("Выберите дату в календаре.") from None


def month_days(month: date) -> list[date]:
    return [
        month.replace(day=day)
        for day in range(1, calendar.monthrange(month.year, month.month)[1] + 1)
    ]


def calendar_view(
    month: date,
    today: date,
    *,
    month_prefix: str,
    day_prefix: str,
    back: str,
    earliest: date,
    latest: date,
    available: set[date] | None = None,
) -> tuple[str, Rows]:
    first = month.replace(day=1)
    previous = (first - timedelta(days=1)).replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    rows: Rows = [[(day, "noop") for day in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")]]
    for week in calendar.Calendar().monthdayscalendar(month.year, month.month):
        buttons = []
        for day in week:
            if not day:
                buttons.append(("·", "noop"))
                continue
            target = month.replace(day=day)
            selectable = earliest <= target <= latest
            label = str(day)
            if target == today:
                label = f"‹{day}›"
            if selectable and available is not None:
                label += "🟢" if target in available else "⚫"
                selectable = target in available
            buttons.append((label, f"{day_prefix}:{target}" if selectable else "noop"))
        rows.append(buttons)
    arrows = []
    if earliest < first:
        arrows.append(("‹ Месяц", f"{month_prefix}:{previous}"))
    if following <= latest:
        arrows.append(("Месяц ›", f"{month_prefix}:{following}"))
    message = f"📅 {MONTH_NAMES[month.month - 1]} {month.year}"
    if available is not None:
        message += "\n🟢 Есть время · ⚫ Нет свободного времени"
    if first.year == today.year and first.month == today.month:
        message += f"\nСегодня {today.day} {MONTHS[today.month - 1]}"
    return message, rows + [arrows] + navigation(back)
