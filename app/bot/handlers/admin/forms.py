from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.fsm.state import State, StatesGroup

from app.config import StudioOptions
from app.exceptions import DomainError
from app.services.availability import Interval
from app.utils.time import local_instant


class AdminInput(StatesGroup):
    block = State()
    manual = State()
    settings = State()
    broadcast = State()
    preview = State()
    edit_comment = State()


SETTING_FIELDS = {
    "studio_name": ("Название студии", "Новое название"),
    "hours": (
        "Часы работы",
        "ЧЧ:ММ–ЧЧ:ММ, например 10:00–23:30. Для круглосуточной работы 00:00–24:00",
    ),
    "durations": (
        "Мин., макс. и шаг",
        "Три числа в минутах через пробел: минимум, максимум, шаг. Максимум до 1440 минут",
    ),
    "max_booking_days": ("Горизонт бронирования", "Количество дней от 0 до 365"),
    "reminder_24h_enabled": ("Напоминание за 24 часа", "да или нет"),
    "reminder_1h_enabled": ("Напоминание за 1 час", "да или нет"),
    "group_notifications_enabled": ("Уведомления в общем чате", "да или нет"),
    "studio_chat_id": ("ID общего чата", "Отрицательное число или «нет»"),
    "admin_contact": ("Контакт администратора", "Имя, @username, ссылка или «нет»"),
    "rules": ("Текст правил", "Правила студии одним сообщением до 3000 знаков"),
}


def hhmm(minute: int) -> str:
    return f"{minute // 60:02}:{minute % 60:02}"


def setting_value(key: str, options: StudioOptions) -> str:
    if key == "hours":
        return f"{hhmm(options.open_minute)}–{hhmm(options.close_minute)}"
    if key == "durations":
        return f"{options.min_booking_minutes} / {options.max_booking_minutes} / {options.booking_step_minutes} мин"
    value = getattr(options, key)
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value) if value is not None and value != "" else "не указан"


def parse_time(value: str) -> int:
    hour, minute = map(int, value.strip().split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60) and (hour, minute) != (24, 0):
        raise ValueError
    return 60 * hour + minute


def parse_setting(key: str, text: str) -> dict[str, object]:
    try:
        if key == "hours":
            start, end = text.replace("–", "-").replace("—", "-").split("-")
            return {"open_minute": parse_time(start), "close_minute": parse_time(end)}
        if key == "durations":
            minimum, maximum, step = map(int, text.split())
            return {
                "min_booking_minutes": minimum,
                "max_booking_minutes": maximum,
                "booking_step_minutes": step,
            }
        if key.endswith("_enabled"):
            return {key: {"да": True, "нет": False}[text.lower()]}
        if key == "studio_chat_id":
            return {key: None if text.lower() == "нет" else int(text)}
        if key == "max_booking_days":
            return {key: int(text)}
        if key == "admin_contact" and text.lower() == "нет":
            return {key: ""}
        if key in {"rules", "studio_name", "admin_contact"}:
            return {key: text}
    except (ValueError, KeyError):
        raise DomainError("Проверьте формат значения и отправьте его снова.") from None
    raise DomainError("Нет такой настройки.")


def parse_interval(lines: list[str], timezone: str) -> Interval:
    zone = ZoneInfo(timezone)
    try:
        start, end = [datetime.strptime(item.strip(), "%d.%m.%Y %H:%M") for item in lines]
    except ValueError:
        raise DomainError(
            "Введите начало и конец, каждое на отдельной строке в формате ДД.ММ.ГГГГ ЧЧ:ММ."
        ) from None
    return Interval(
        local_instant(start.date(), 60 * start.hour + start.minute, zone),
        local_instant(end.date(), 60 * end.hour + end.minute, zone),
    )
