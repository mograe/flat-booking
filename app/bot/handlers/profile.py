from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.handlers.booking import begin_booking
from app.bot.ui import Screen, navigation, number, pagination
from app.database.models import BookingStatus, Role, User, utcnow
from app.exceptions import DomainError
from app.services.availability import AvailabilityService
from app.services.booking import PURPOSES, BookingService
from app.services.settings import SettingsService
from app.utils.time import format_booking_period, format_interval

router = Router(name="profile")
STATUSES = {
    BookingStatus.CONFIRMED: "Подтверждено",
    BookingStatus.CANCELLED: "Отменено",
    BookingStatus.COMPLETED: "Завершено",
}


@router.callback_query(F.data.startswith("p:"))
async def profile(
    query: CallbackQuery,
    screen: Screen,
    actor: User,
    booking: BookingService,
    settings: SettingsService,
    availability: AvailabilityService,
) -> None:
    _, command, *args = (query.data or "").split(":")
    await screen.reset()
    if command in {"home", "admin"}:
        prefix = "all" if command == "admin" else "my"
        if prefix == "all" and actor.role != Role.ADMIN:
            raise DomainError("У вас нет доступа к этому действию.")
        rows = [
            [("Предстоящие посещения", f"p:{prefix}:up:0")],
            [("История посещений", f"p:{prefix}:old:0")],
        ]
        if prefix == "all":
            rows.append([("Создать бронирование", "ad:book")])
        await screen.show(
            "📅 Все бронирования" if prefix == "all" else "🎛 Мои бронирования",
            rows + navigation("ad:home" if prefix == "all" else "home"),
        )
        return
    options = await settings.get()
    if command in {"my", "all"} and len(args) == 2:
        category, raw_page = args
        if category not in {"up", "old"}:
            raise DomainError("Список устарел.")
        page = number(raw_page, 10**6)
        admin = command == "all"
        records = await booking.list(actor.id, page * 8, history=category == "old", admin=admin)
        rows = []
        for item, owner in records[:8]:
            label = format_interval(item.start_at, item.end_at, options.timezone)
            if admin:
                label += f" · {owner[:25]}"
            rows.append([(label, f"p:{'adminitem' if admin else 'item'}:{item.id}")])
        await screen.show(
            ("Предстоящие посещения" if category == "up" else "История посещений")
            + ("" if rows else "\n\nЗдесь пока пусто."),
            rows
            + pagination(f"p:{command}:{category}", page, len(records) > 8)
            + navigation("p:admin" if admin else "p:home"),
        )
        return
    if not args:
        raise DomainError("Кнопка устарела.")
    admin_view = command.startswith("admin")
    if admin_view and actor.role != Role.ADMIN:
        raise DomainError("У вас нет доступа к этому действию.")
    mode = "admin" if admin_view else ""
    item, owner = await booking.get(actor.id, number(args[0]))
    back = "p:admin" if admin_view else "p:home"
    context_index = 2 if command == "admindrop" else 1
    suffix = ""
    if admin_view and len(args) > context_index:
        telegram_id = number(args[context_index])
        back = f"ad:wl:up:{telegram_id}:0"
        suffix = f":{telegram_id}"
    detail = f"p:{mode}item:{item.id}{suffix}"
    now = utcnow()
    editable = item.status == BookingStatus.CONFIRMED and item.end_at > now
    if command == f"{mode}item":
        status = (
            STATUSES[item.status]
            if editable or item.status != BookingStatus.CONFIRMED
            else "Завершено"
        )
        rows = []
        if editable:
            rows.append(
                [
                    ("📅 Перенести", f"p:{mode}move:{item.id}{suffix}"),
                    ("❌ Отменить", f"p:{mode}cancel:{item.id}{suffix}"),
                ]
            )
            if admin_view:
                rows.append([("✏️ Цель и комментарий", f"ad:details:{item.id}")])
        await screen.show(
            f"🎛 Бронирование №{item.id}\n"
            f"{format_booking_period(item.start_at, item.end_at, options.timezone)}\n"
            f"👤 {owner}\n{PURPOSES[item.purpose]}\n"
            f"📝 {item.comment or 'Комментарий отсутствует'}\n{status}",
            rows + navigation(back),
        )
    elif command == f"{mode}move" and editable:
        await begin_booking(
            screen,
            screen.state,
            actor,
            settings,
            availability,
            booking_id=item.id,
            version=item.version,
            back=detail,
        )
    elif command == f"{mode}cancel" and editable:
        await screen.show(
            f"Отменить бронирование №{item.id}?\n"
            f"{format_interval(item.start_at, item.end_at, options.timezone)}",
            [[("Да, отменить", f"p:{mode}drop:{item.id}:{item.version}{suffix}")]]
            + navigation(detail),
        )
    elif command == f"{mode}drop" and len(args) == (3 if suffix else 2):
        await booking.cancel(actor.id, item.id, number(args[1]))
        await screen.show("Бронирование отменено.", navigation(back))
    else:
        raise DomainError("Действие устарело. Откройте бронирование снова.")
