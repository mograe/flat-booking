from datetime import date, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.calendar import calendar_view, month_days, read_date
from app.bot.ui import Screen, chunks, navigation, number, pagination
from app.config import MAX_BOOKING_MINUTES
from app.database.models import Role, User, utcnow
from app.exceptions import Conflict, DomainError
from app.services.availability import AvailabilityService, Interval
from app.services.booking import PURPOSES, BookingService
from app.services.settings import SettingsService
from app.utils.time import format_booking_period, local_instant, minutes_text, parse_duration

router = Router(name="booking")
QUICK_LENGTHS = (60, 120, 180, 240, 360, 480, 720, 1080, MAX_BOOKING_MINUTES)


class BookingFlow(StatesGroup):
    calendar = State()
    times = State()
    lengths = State()
    duration = State()
    purpose = State()
    comment = State()
    confirm = State()


async def begin_booking(
    screen: Screen,
    state: FSMContext,
    actor: User,
    settings: SettingsService,
    availability: AvailabilityService,
    *,
    booking_id: int | None = None,
    version: int | None = None,
    owner_id: int | None = None,
    back: str = "home",
) -> None:
    await screen.reset()
    await state.update_data(
        booking_id=booking_id,
        version=version,
        owner_id=owner_id,
        back=back,
    )
    today = utcnow().astimezone(ZoneInfo((await settings.get()).timezone)).date()
    await show_month(screen, state, actor, settings, availability, today)


async def show_month(
    screen: Screen,
    state: FSMContext,
    actor: User,
    settings: SettingsService,
    availability: AvailabilityService,
    month: date,
) -> None:
    options = await settings.get()
    now = utcnow()
    today = now.astimezone(ZoneInfo(options.timezone)).date()
    admin = actor.role == Role.ADMIN
    latest = date(2200, 12, 31) if admin else today + timedelta(days=options.max_booking_days)
    days = [day for day in month_days(month) if today <= day <= latest]
    if not days:
        raise DomainError("Месяц вне доступного периода.")
    data = await state.get_data()
    slots = await availability.slots(
        actor.id, days, now, admin=admin, exclude=data.get("booking_id"), first_free_only=True
    )
    available = {day for day, values in slots.items() if any(value.lengths for value in values)}
    await state.update_data(month=month.replace(day=1).isoformat(), manual=False)
    await state.set_state(BookingFlow.calendar)
    message, rows = calendar_view(
        month,
        today,
        month_prefix="bk:month",
        day_prefix="bk:day",
        back=data["back"],
        earliest=today,
        latest=latest,
        available=available,
    )
    if admin:
        rows.insert(-1, [("Указать дату и время", "ad:manual")])
    await screen.show(message, rows)


async def show_times(
    screen: Screen,
    state: FSMContext,
    actor: User,
    availability: AvailabilityService,
    day: date,
    page: int,
) -> None:
    data = await state.get_data()
    slots = (
        await availability.slots(
            actor.id,
            [day],
            utcnow(),
            admin=actor.role == Role.ADMIN,
            exclude=data.get("booking_id"),
        )
    )[day]
    start = page * 48
    if start >= len(slots):
        raise DomainError("Страница устарела.")
    await state.update_data(day=day.isoformat(), page=page)
    await state.set_state(BookingFlow.times)
    items = [
        (
            f"{slot.status} {slot.minute // 60:02}:{slot.minute % 60:02}",
            f"bk:time:{slot.minute}" if slot.lengths else "noop",
        )
        for slot in slots[start : start + 48]
    ]
    await screen.show(
        f"🕐 {day:%d.%m.%Y} · Время начала\n🟢 Свободно · 🔴 Занято · ⚫ Недоступно",
        chunks(items, 4)
        + pagination("bk:page", page, start + 48 < len(slots))
        + navigation(f"bk:month:{day.replace(day=1)}"),
    )


async def show_lengths(
    screen: Screen,
    state: FSMContext,
    actor: User,
    availability: AvailabilityService,
    minute: int,
) -> None:
    data = await state.get_data()
    lengths = await slot_lengths(
        availability, actor, read_date(data["day"]), minute, data.get("booking_id")
    )
    if not lengths:
        raise Conflict()
    await state.update_data(minute=minute)
    await state.set_state(BookingFlow.lengths)
    items = [
        (minutes_text(length), f"bk:length:{length}")
        for length in QUICK_LENGTHS
        if length in lengths
    ]
    await screen.show(
        "⏳ Выберите продолжительность",
        chunks(items, 3)
        + [[("Указать продолжительность вручную", "bk:manual-length")]]
        + navigation(f"bk:page:{data['page']}"),
    )


async def slot_lengths(
    availability: AvailabilityService,
    actor: User,
    day: date,
    minute: int,
    exclude: int | None,
) -> tuple[int, ...]:
    slots = (
        await availability.slots(
            actor.id,
            [day],
            utcnow(),
            admin=actor.role == Role.ADMIN,
            exclude=exclude,
        )
    )[day]
    return next((slot.lengths for slot in slots if slot.minute == minute), ())


async def select_length(
    screen: Screen,
    state: FSMContext,
    actor: User,
    settings: SettingsService,
    availability: AvailabilityService,
    booking: BookingService,
    length: int,
) -> None:
    data = await state.get_data()
    options = await settings.get()
    day, minute = read_date(data["day"]), data["minute"]
    start = local_instant(day, minute, ZoneInfo(options.timezone))
    Interval(start, start + timedelta(minutes=length)).validate(
        utcnow(), options, actor.role == Role.ADMIN
    )
    if length not in await slot_lengths(availability, actor, day, minute, data.get("booking_id")):
        raise Conflict()
    await state.update_data(length=length)
    if data.get("booking_id"):
        await confirm_view(screen, state, settings, booking, actor, "")
    else:
        await show_purposes(screen, state, minute)


async def confirm_view(
    screen: Screen,
    state: FSMContext,
    settings: SettingsService,
    booking: BookingService,
    actor: User,
    comment: str,
) -> None:
    if len(comment) > 500:
        raise DomainError("Комментарий должен содержать до 500 знаков.")
    data = await state.get_data()
    options = await settings.get()
    start = local_instant(read_date(data["day"]), data["minute"], ZoneInfo(options.timezone))
    end = start + timedelta(minutes=data["length"])
    Interval(start, end).validate(utcnow(), options, actor.role == Role.ADMIN)
    purpose = data.get("purpose")
    if data.get("booking_id"):
        original, _ = await booking.get(actor.id, data["booking_id"])
        purpose, comment = original.purpose, original.comment
    key = uuid4().hex
    await state.update_data(start=start, end=end, key=key, comment=comment, purpose=purpose)
    await state.set_state(BookingFlow.confirm)
    await screen.show(
        "🎙 Бронирование студии\n\n"
        f"{format_booking_period(start, end, options.timezone)}\n"
        f"Цель: {PURPOSES[purpose]}\n"
        f"📝 {comment or 'Комментарий отсутствует'}",
        [
            [("✅ Подтвердить", f"bk:ok:{key}")],
            [
                ("✏️ Изменить", "ad:manual" if data.get("manual") else f"bk:day:{data['day']}"),
                ("❌ Отмена", data["back"]),
            ],
        ]
        + navigation(
            ("ad:manual" if data.get("manual") else f"bk:time:{data['minute']}")
            if data.get("booking_id")
            else "bk:purpose"
        ),
    )


@router.callback_query(F.data == "book")
async def open_booking(
    query: CallbackQuery,
    screen: Screen,
    state: FSMContext,
    actor: User,
    settings: SettingsService,
    availability: AvailabilityService,
) -> None:
    await begin_booking(screen, state, actor, settings, availability)


@router.callback_query(F.data.startswith("bk:"))
async def booking_step(
    query: CallbackQuery,
    screen: Screen,
    state: FSMContext,
    actor: User,
    settings: SettingsService,
    availability: AvailabilityService,
    booking: BookingService,
) -> None:
    _, command, *arguments = (query.data or "").split(":")
    arg = arguments[0] if len(arguments) == 1 else ""
    step = await state.get_state()
    data = await state.get_data()
    if (
        not step
        or "back" not in data
        or (not step.startswith("BookingFlow:") and not (command == "month" and data.get("manual")))
    ):
        raise DomainError("Черновик закрыт. Начните бронирование заново.")
    if command == "month":
        await show_month(screen, state, actor, settings, availability, read_date(arg))
    elif command in {"day", "page"}:
        day = read_date(arg if command == "day" else data.get("day", ""))
        await show_times(
            screen, state, actor, availability, day, 0 if command == "day" else number(arg, 29)
        )
    elif command == "time" and "day" in data:
        await show_lengths(screen, state, actor, availability, number(arg, 1439))
    elif command == "manual-length" and step == BookingFlow.lengths.state:
        await state.set_state(BookingFlow.duration)
        await screen.show(
            "⏳ Введите продолжительность в часах. Например, 1:30, 2.5, 2,5, 12 или 24.",
            navigation(f"bk:time:{data['minute']}"),
        )
    elif command == "length" and step == BookingFlow.lengths.state:
        await select_length(
            screen, state, actor, settings, availability, booking, number(arg, MAX_BOOKING_MINUTES)
        )
    elif command == "purpose" and "length" in data:
        if not arg:
            await show_purposes(screen, state, data["minute"])
        elif arg in PURPOSES and step == BookingFlow.purpose.state:
            await state.update_data(purpose=arg)
            await state.set_state(BookingFlow.comment)
            await screen.show(
                "📝 Отправьте комментарий одним сообщением или пропустите этот шаг.",
                [[("Без комментария", "bk:skip")]] + navigation("bk:purpose"),
            )
        else:
            raise DomainError("Выберите цель из списка.")
    elif command == "skip" and step == BookingFlow.comment.state:
        await confirm_view(screen, state, settings, booking, actor, "")
    elif command == "ok" and step == BookingFlow.confirm.state and data.get("key") == arg:
        interval = Interval(data["start"], data["end"])
        if data.get("booking_id"):
            result = await booking.change(
                actor.id, data["booking_id"], data["version"], interval=interval
            )
            title = "✅ Бронирование перенесено"
        else:
            result = await booking.create(
                actor.id,
                interval,
                data["purpose"],
                data["comment"],
                owner_id=data.get("owner_id"),
                confirmation_key=arg,
            )
            title = "✅ Бронирование подтверждено"
        await screen.reset()
        detail = (
            "adminitem" if (data.get("owner_id") or data["back"].startswith("p:admin")) else "item"
        )
        await screen.show(
            title,
            [[("Открыть бронирование", f"p:{detail}:{result.id}")]] + navigation(data["back"]),
        )
    else:
        raise DomainError("Кнопка устарела. Вернитесь к предыдущему шагу.")


async def show_purposes(screen: Screen, state: FSMContext, minute: int) -> None:
    back = "ad:manual" if (await state.get_data()).get("manual") else f"bk:time:{minute}"
    await state.set_state(BookingFlow.purpose)
    await screen.show(
        "🎵 Выберите цель посещения",
        [[(label, f"bk:purpose:{key}")] for key, label in PURPOSES.items()] + navigation(back),
    )


@router.message(BookingFlow.duration, F.text, ~F.text.startswith("/"))
async def manual_length(
    message: Message,
    screen: Screen,
    state: FSMContext,
    actor: User,
    settings: SettingsService,
    availability: AvailabilityService,
    booking: BookingService,
) -> None:
    await select_length(
        screen, state, actor, settings, availability, booking, parse_duration(message.text or "")
    )


@router.message(BookingFlow.comment, F.text, ~F.text.startswith("/"))
async def booking_comment(
    message: Message,
    screen: Screen,
    state: FSMContext,
    settings: SettingsService,
    booking: BookingService,
    actor: User,
) -> None:
    await confirm_view(screen, state, settings, booking, actor, (message.text or "").strip())
