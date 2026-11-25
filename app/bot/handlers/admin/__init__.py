from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.handlers import access as access_handlers
from app.bot.ui import Screen, navigation, number, pagination
from app.database.models import RequestStatus, Role, User
from app.services.access import AccessService
from app.services.admin import AdminService
from app.services.booking import BookingService
from app.services.settings import SettingsService
from app.services.users import UsersService
from app.utils.time import format_interval

from . import edit, whitelist

router = Router(name="admin")
ADMIN = "ad:home"
ACTIONS = {
    "member_added": "Добавлен участник",
    "whitelist_added": "Пользователь добавлен в белый список",
    "whitelist_removed": "Пользователь удалён из белого списка",
    "member_blocked": "Участник заблокирован",
    "member_unblocked": "Участник разблокирован",
    "request_approved": "Заявка одобрена",
    "request_rejected": "Заявка отклонена",
    "booking_created": "Создана бронь",
    "booking_changed": "Изменена бронь",
    "booking_cancelled": "Отменена бронь",
    "time_blocked": "Добавлена блокировка времени",
    "time_unblocked": "Снята блокировка времени",
    "settings_updated": "Изменены настройки",
    "broadcast_queued": "Объявление поставлено в очередь",
}


@router.callback_query(F.data == ADMIN)
async def panel(query: CallbackQuery, screen: Screen) -> None:
    await screen.reset()
    await screen.show(
        "⚙️ Управление студией",
        [
            [("👥 Участники", "ad:members:0")],
            [("🔐 Белый список", whitelist.MENU)],
            [("📨 Заявки на доступ", "ad:requests:0")],
            [("📅 Все бронирования", "p:admin")],
            [("🔒 Блокировка времени", "ad:blocks:0")],
            [("📢 Рассылка", "ad:announce")],
            [("📖 Редактирование правил", "ad:set:rules")],
            [("⚙️ Настройки студии", "ad:settings")],
            [("📋 Журнал действий", "ad:audit:0")],
        ]
        + navigation(),
    )


@router.callback_query(F.data.regexp(r"^ad:(members|bookto|requests|blocks|audit):[0-9]+$"))
async def lists(
    query: CallbackQuery,
    screen: Screen,
    actor: User,
    users: UsersService,
    access: AccessService,
    booking: BookingService,
    admin: AdminService,
    settings: SettingsService,
) -> None:
    await screen.reset()
    _, section, raw_page = (query.data or "").split(":")
    page = number(raw_page, 10**6)
    offset = page * 8
    back = ADMIN
    rows = []
    if section in {"members", "bookto"}:
        records = await users.list(actor.id, offset, active_only=section == "bookto")
        for user in records[:8]:
            if section == "members":
                rows.append(
                    [
                        (
                            f"{'🟢' if user.is_active else '⚫'} {user.display_name[:55]}",
                            f"ad:member:{user.id}",
                        )
                    ]
                )
            elif user.is_active:
                rows.append([(user.display_name[:55], f"ad:owner:{user.id}")])
        if section == "members":
            title = "👥 Участники"
            rows.insert(0, [("Добавить по Telegram ID", "ad:wl:id")])
        else:
            title, back = "Выберите участника для бронирования", "p:admin"
    elif section == "requests":
        title = "📨 Заявки на доступ"
        records = await access.pending(actor.id, offset)
        rows = [[(user.display_name[:55], f"ad:request:{item.id}")] for item, user in records[:8]]
    elif section == "blocks":
        title = "🔒 Блокировка времени"
        records = await booking.list_blocks(actor.id, offset)
        options = await settings.get()
        rows = [
            [
                (
                    format_interval(item.start_at, item.end_at, options.timezone),
                    f"ad:blockitem:{item.id}",
                )
            ]
            for item in records[:8]
        ]
        rows.insert(0, [("Заблокировать интервал", "ad:block")])
    else:
        options = await settings.get()
        records = await admin.audit_page(actor.id, offset)
        lines = ["📋 Журнал действий", ""]
        for entry, name in records[:8]:
            stamp = entry.created_at.astimezone(ZoneInfo(options.timezone)).strftime(
                "%d.%m.%Y %H:%M"
            )
            detail = f" · №{entry.entity_id}" if entry.entity_id is not None else ""
            if entry.action == "broadcast_queued":
                detail = f" · Получателей: {entry.details['recipients']}"
            lines.append(f"{stamp} · {name}\n{ACTIONS.get(entry.action, entry.action)}{detail}")
        title = "\n".join(lines)
    if not records:
        title += "\n\nЗаписей пока нет."
    await screen.show(
        title, rows + pagination(f"ad:{section}", page, len(records) > 8) + navigation(back)
    )


@router.callback_query(F.data.regexp(r"^ad:(member|active|request|review|blockitem|unblock):"))
async def edit_records(
    query: CallbackQuery,
    screen: Screen,
    actor: User,
    users: UsersService,
    access: AccessService,
    booking: BookingService,
    settings: SettingsService,
) -> None:
    await screen.reset()
    _, action, entity, *flags = (query.data or "").split(":")
    item_id = number(entity)
    if action in {"member", "active"}:
        user = await users.get(actor.id, item_id)
        back = "ad:members:0"
        if action == "active":
            await users.set_active(actor.id, user.id, flags == ["yes"])
            user = await users.get(actor.id, item_id)
        rows = []
        if user.role == Role.MEMBER:
            label, value = ("Заблокировать", "no") if user.is_active else ("Разблокировать", "yes")
            rows = [[(label, f"ad:active:{user.id}:{value}")]]
        await screen.show(
            f"👤 {user.display_name}\nTelegram ID: {user.telegram_id}\n"
            f"{'Администратор' if user.role == Role.ADMIN else 'Участник'} · "
            f"{'Активен' if user.is_active else 'Заблокирован'}",
            rows + navigation(back),
        )
    elif action in {"request", "review"}:
        item, user = await access.details(actor.id, item_id)
        back = "ad:requests:0"
        if action == "review":
            await access.review(actor.id, item_id, flags == ["yes"])
            await screen.show("Заявка рассмотрена.", navigation(back))
            return
        rows = (
            [[("Одобрить", f"ad:review:{item.id}:yes"), ("Отклонить", f"ad:review:{item.id}:no")]]
            if item.status == RequestStatus.PENDING
            else []
        )
        await screen.show(
            f"📨 Заявка №{item.id}\n👤 {user.display_name}\n"
            f"Telegram ID: {user.telegram_id}\n{access_handlers.REQUEST_STATUSES[item.status]}",
            rows + navigation(back),
        )
    else:
        back = "ad:blocks:0"
        if action == "unblock":
            await booking.unblock(actor.id, item_id)
            await screen.show("Блокировка снята.", navigation(back))
            return
        item = await booking.get_block(actor.id, item_id)
        options = await settings.get()
        await screen.show(
            f"🔒 {format_interval(item.start_at, item.end_at, options.timezone)}\n{item.reason}",
            [[("Снять блокировку", f"ad:unblock:{item.id}")]] + navigation(back),
        )


router.include_router(edit.router)
router.include_router(whitelist.router)
