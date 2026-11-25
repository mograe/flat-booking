import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.config import MAX_BOOKING_MINUTES
from app.exceptions import DomainError

MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def local_date(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def local_instant(day: date, minute: int, zone: ZoneInfo) -> datetime:
    naive = datetime.combine(day, time()) + timedelta(minutes=minute)
    local = naive.replace(tzinfo=zone)
    instant = local.astimezone(UTC)
    if instant.astimezone(zone).replace(tzinfo=None) != naive:
        raise DomainError("Это местное время пропущено при переводе часов.")
    return instant


def format_interval(start: datetime, end: datetime, timezone: str) -> str:
    zone = ZoneInfo(timezone)
    first, last = start.astimezone(zone), end.astimezone(zone)
    finish = last.strftime("%H:%M")
    if first.date() != last.date():
        finish = f"{local_date(last.date())}, {finish}"
    return f"{local_date(first.date())}, {first:%H:%M}–{finish}"


def minutes_text(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    word = (
        "часов"
        if 11 <= hours % 100 <= 14
        else {1: "час", 2: "часа", 3: "часа", 4: "часа"}.get(hours % 10, "часов")
    )
    parts = ([f"{hours} {word}"] if hours else []) + ([f"{rest} мин"] if rest else [])
    return " ".join(parts)


def parse_duration(value: str) -> int:
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,2}:[0-5][0-9]", value):
        hours, minutes = map(int, value.split(":"))
        result = Decimal(hours * 60 + minutes)
    elif re.fullmatch(r"[0-9]{1,2}([.,][0-9]{1,8})?", value):
        result = Decimal(value.replace(",", ".")) * 60
    else:
        raise DomainError("Введите продолжительность в часах, например 1:30, 2,5 или 24.")
    if not 0 < result <= MAX_BOOKING_MINUTES or result != result.to_integral_value():
        raise DomainError("Укажите продолжительность в целых минутах от 1 минуты до 24 часов.")
    return int(result)


def format_booking_period(start: datetime, end: datetime, timezone: str) -> str:
    zone = ZoneInfo(timezone)
    first, last = start.astimezone(zone), end.astimezone(zone)
    length = int((end.astimezone(UTC) - start.astimezone(UTC)) // timedelta(minutes=1))
    if first.date() == last.date():
        dates = f"📅 {format_interval(start, end, timezone)}"
    else:
        dates = (
            f"📅 Начало: {local_date(first.date())}, {first:%H:%M}\n"
            f"📅 Окончание: {local_date(last.date())}, {last:%H:%M}"
        )
    return f"{dates}\n⏳ Продолжительность: {minutes_text(length)}"
