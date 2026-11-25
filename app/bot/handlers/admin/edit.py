from uuid import uuid4
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.admin.forms import (
    SETTING_FIELDS,
    AdminInput,
    parse_interval,
    parse_setting,
    setting_value,
)
from app.bot.handlers.booking import begin_booking, confirm_view, show_purposes
from app.bot.ui import Screen, navigation, number
from app.database.models import Role, User, utcnow
from app.exceptions import DomainError
from app.services.admin import AdminService
from app.services.availability import AvailabilityService
from app.services.booking import PURPOSES, BookingService
from app.services.settings import SettingsService
from app.services.users import UsersService

router = Router(name="admin_edit")
ADMIN = "ad:home"
INTERVAL_HELP = (
    "Отправьте начало и конец на отдельных строках в формате ДД.ММ.ГГГГ ЧЧ:ММ. "
    "Указывайте местное время студии."
)


@router.callback_query(F.data.in_({"ad:block", "ad:announce", "ad:manual", "ad:book"}))
async def prompt(query: CallbackQuery, screen: Screen, state: FSMContext) -> None:
    callback = query.data
    if callback == "ad:manual":
        data = await state.get_data()
        if "month" not in data:
            raise DomainError("Сначала откройте календарь бронирования.")
        await state.update_data(manual=True)
        await state.set_state(AdminInput.manual)
        await screen.show(INTERVAL_HELP, navigation(f"bk:month:{data['month']}"))
        return
    await screen.reset()
    if callback == "ad:book":
        await screen.show(
            "Для кого создать бронирование?",
            [[("Выбрать участника", "ad:bookto:0")], [("Для себя", "ad:owner:0")]]
            + navigation("p:admin"),
        )
        return
    if callback == "ad:block":
        step, title, back = (
            AdminInput.block,
            INTERVAL_HELP + "\nНа третьей строке укажите причину.",
            "ad:blocks:0",
        )
    else:
        step, title, back = (
            AdminInput.broadcast,
            "Отправьте текст объявления до 3000 знаков.",
            ADMIN,
        )
    await state.set_state(step)
    await screen.show(title, navigation(back))


@router.callback_query(F.data.startswith("ad:owner:"))
async def owner(
    query: CallbackQuery,
    screen: Screen,
    state: FSMContext,
    actor: User,
    users: UsersService,
    availability: AvailabilityService,
    settings: SettingsService,
) -> None:
    owner_id = number((query.data or "").split(":")[-1]) or actor.id
    selected = await users.get(actor.id, owner_id)
    if not selected.is_active or selected.role == Role.GUEST:
        raise DomainError("Доступ этого участника приостановлен.")
    await begin_booking(
        screen, state, actor, settings, availability, owner_id=selected.id, back="ad:book"
    )


@router.callback_query(F.data == "ad:settings")
async def settings_panel(query: CallbackQuery, screen: Screen, settings: SettingsService) -> None:
    await screen.reset()
    options = await settings.get()
    rows = []
    for key, (label, _) in SETTING_FIELDS.items():
        value = setting_value(key, options)
        compact = f" · {value[:22]}" if key != "rules" else ""
        rows.append([(label + compact, f"ad:set:{key}")])
    await screen.show("⚙️ Настройки студии", rows + navigation(ADMIN))


@router.callback_query(F.data.startswith("ad:set:"))
async def setting_prompt(
    query: CallbackQuery,
    screen: Screen,
    state: FSMContext,
    settings: SettingsService,
) -> None:
    key = (query.data or "").split(":")[-1]
    if key not in SETTING_FIELDS:
        raise DomainError("Нет такой настройки.")
    await screen.reset()
    await state.set_state(AdminInput.settings)
    await state.update_data(setting=key)
    options = await settings.get()
    label, hint = SETTING_FIELDS[key]
    await screen.show(
        f"⚙️ {label}\n\nСейчас: {setting_value(key, options)}\n\nОтправьте новое значение. {hint}.",
        navigation("ad:settings"),
    )


@router.callback_query(F.data.startswith("ad:send:"))
async def broadcast_confirm(
    query: CallbackQuery,
    screen: Screen,
    state: FSMContext,
    actor: User,
    admin: AdminService,
) -> None:
    data = await state.get_data()
    if (await state.get_state()) != AdminInput.preview.state or (query.data or "").split(":")[
        -1
    ] != data.get("key"):
        raise DomainError("Предпросмотр устарел.")
    recipients = await admin.broadcast(actor.id, data["announcement"])
    await screen.reset()
    await screen.show(
        f"Объявление поставлено в очередь. Получателей: {recipients}.", navigation(ADMIN)
    )


@router.callback_query(F.data.startswith("ad:details:"))
async def details(
    query: CallbackQuery,
    screen: Screen,
    state: FSMContext,
    actor: User,
    booking: BookingService,
) -> None:
    item_id = number((query.data or "").split(":")[-1])
    item, _ = await booking.get(actor.id, item_id)
    await screen.reset()
    await state.update_data(edited_id=item.id, version=item.version)
    await screen.show(
        "✏️ Выберите цель посещения",
        [[(label, f"ad:purpose:{key}")] for key, label in PURPOSES.items()]
        + navigation(f"p:adminitem:{item.id}"),
    )


@router.callback_query(F.data.startswith("ad:purpose:"))
async def details_purpose(query: CallbackQuery, screen: Screen, state: FSMContext) -> None:
    purpose = (query.data or "").split(":")[-1]
    data = await state.get_data()
    if purpose not in PURPOSES or "edited_id" not in data:
        raise DomainError("Начните редактирование заново.")
    await state.update_data(purpose=purpose)
    await state.set_state(AdminInput.edit_comment)
    await screen.show(
        "📝 Отправьте новый комментарий или выберите действие с текущим.",
        [[("Без изменений", "ad:finish:keep"), ("Очистить", "ad:finish:clear")]]
        + navigation(f"ad:details:{data['edited_id']}"),
    )


async def save_details(
    screen: Screen,
    state: FSMContext,
    actor: User,
    booking: BookingService,
    comment: str | None,
) -> None:
    data = await state.get_data()
    item = await booking.change(
        actor.id,
        data["edited_id"],
        data["version"],
        purpose=data["purpose"],
        comment=comment,
    )
    await screen.reset()
    await screen.show("Бронирование обновлено.", navigation(f"p:adminitem:{item.id}"))


@router.callback_query(AdminInput.edit_comment, F.data.in_({"ad:finish:keep", "ad:finish:clear"}))
async def details_finish(
    query: CallbackQuery,
    screen: Screen,
    state: FSMContext,
    actor: User,
    booking: BookingService,
) -> None:
    await save_details(
        screen, state, actor, booking, "" if query.data == "ad:finish:clear" else None
    )


@router.message(
    F.text,
    ~F.text.startswith("/"),
    StateFilter(
        AdminInput.block,
        AdminInput.manual,
        AdminInput.settings,
        AdminInput.broadcast,
        AdminInput.edit_comment,
    ),
)
async def enter_text(
    message: Message,
    screen: Screen,
    state: FSMContext,
    actor: User,
    settings: SettingsService,
    booking: BookingService,
) -> None:
    if actor.role != Role.ADMIN:
        raise DomainError("У вас нет доступа к этому действию.")
    value = (message.text or "").strip()
    step = await state.get_state()
    data = await state.get_data()
    if step == AdminInput.edit_comment.state:
        await save_details(screen, state, actor, booking, value)
        return
    if step == AdminInput.settings.state:
        await settings.update(actor.id, parse_setting(data["setting"], value))
        result, back = "Настройки сохранены.", "ad:settings"
    elif step == AdminInput.block.state:
        options = await settings.get()
        first, second, reason = (value.split("\n", 2) + ["", ""])[:3]
        interval = parse_interval([first, second], options.timezone)
        await booking.block(actor.id, interval, reason.strip())
        result, back = "Интервал заблокирован.", "ad:blocks:0"
    elif step == AdminInput.manual.state:
        options = await settings.get()
        interval = parse_interval(value.splitlines(), options.timezone)
        interval.validate(utcnow(), options, True)
        start = interval.start.astimezone(ZoneInfo(options.timezone))
        await state.update_data(
            day=start.date().isoformat(),
            minute=start.hour * 60 + start.minute,
            length=int((interval.end - interval.start).total_seconds() // 60),
        )
        if data.get("booking_id"):
            await confirm_view(screen, state, settings, booking, actor, "")
        else:
            await show_purposes(screen, state, start.hour * 60 + start.minute)
        return
    else:
        if not 1 <= len(value) <= 3000:
            raise DomainError("Объявление должно содержать от 1 до 3000 знаков.")
        key = uuid4().hex
        await state.set_state(AdminInput.preview)
        await state.update_data(announcement=value, key=key)
        await screen.show(
            f"Предпросмотр рассылки\n\n📢 Объявление студии\n\n{value}",
            [[("✅ Отправить участникам", f"ad:send:{key}")]] + navigation("ad:announce"),
        )
        return
    await screen.reset()
    await screen.show(result, navigation(back))
