from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.ui import Screen, navigation, number, pagination
from app.database.models import Role, User
from app.exceptions import AccessDenied, DomainError
from app.services.settings import SettingsService
from app.services.users import UsersService
from app.utils.time import format_interval

router = Router(name="whitelist")
MENU = "ad:wl"
PAGE_SIZE = 6


class WhitelistInput(StatesGroup):
    telegram_id = State()
    search = State()
    comment = State()
    remove = State()


def entry_text(user: User, zone: ZoneInfo) -> str:
    name = user.display_name if user.registered_at else "Имя неизвестно"
    username = f"@{user.username}" if user.username else "не указан"
    registered = "зарегистрирован" if user.registered_at else "бот ещё не запускался"
    access = "разрешён" if user.is_active else "заблокирован"
    if user.role == Role.ADMIN:
        access = "администратор"
    added_at = user.whitelist_added_at
    stamp = added_at.astimezone(zone).strftime("%d.%m.%Y %H:%M") if added_at else "неизвестна"
    return (
        f"👤 {name}\nTelegram ID: {user.telegram_id}\nUsername: {username}\n"
        f"Комментарий: {user.whitelist_comment or '—'}\n"
        f"Регистрация: {registered}\nДоступ: {access}\nДобавлен: {stamp}"
    )


@router.callback_query(F.data == MENU)
async def menu(query: CallbackQuery, screen: Screen) -> None:
    await screen.reset()
    await screen.show(
        "🔐 Белый список",
        [
            [("➕ Добавить пользователя", "ad:wl:add")],
            [("➖ Удалить пользователя", "ad:wl:remove-list:0")],
            [("👥 Список пользователей", "ad:wl:list:0")],
            [("🔎 Найти пользователя", "ad:wl:find")],
            [("◀️ Назад", "ad:home")],
        ],
    )


@router.callback_query(F.data == "ad:wl:add")
async def add_menu(query: CallbackQuery, screen: Screen) -> None:
    await screen.reset()
    await screen.show(
        "Кого добавить в белый список?",
        [
            [("Ввести Telegram User ID", "ad:wl:id")],
            [("Выбрать зарегистрированного пользователя", "ad:wl:pick:0")],
        ]
        + navigation(MENU),
    )


@router.callback_query(F.data.in_({"ad:wl:id", "ad:wl:find"}))
async def ask_telegram_id(query: CallbackQuery, screen: Screen, state: FSMContext) -> None:
    await screen.reset()
    finding = query.data == "ad:wl:find"
    await state.set_state(WhitelistInput.search if finding else WhitelistInput.telegram_id)
    await screen.show(
        "Введите Telegram User ID для поиска в белом списке."
        if finding
        else "Введите Telegram User ID пользователя.",
        navigation(MENU if finding else "ad:wl:add"),
    )


@router.callback_query(F.data.regexp(r"^ad:wl:(list|pick|remove-list):[0-9]+$"))
async def list_users(
    query: CallbackQuery,
    screen: Screen,
    actor: User,
    users: UsersService,
    settings: SettingsService,
) -> None:
    await screen.reset()
    _, _, section, raw_page = (query.data or "").split(":")
    page = number(raw_page, 10**6)
    candidates = section == "pick"
    records = await users.whitelist_page(actor.id, page * PAGE_SIZE, candidates=candidates)
    titles = {
        "list": "👥 Пользователи белого списка",
        "pick": "Выберите зарегистрированного пользователя",
        "remove-list": "Выберите пользователя для удаления",
    }
    lines = [titles[section]]
    rows = []
    if not records:
        lines.append("Записей на этой странице нет.")
    zone = ZoneInfo((await settings.get()).timezone)
    for user in records[:PAGE_SIZE]:
        if candidates:
            rows.append(
                [(f"{user.display_name[:33]} · {user.telegram_id}", f"ad:wl:choose:{user.id}")]
            )
        else:
            lines.append(entry_text(user, zone))
            if section != "remove-list" or user.role != Role.ADMIN:
                action = "remove" if section == "remove-list" else "entry"
                label = user.display_name[:33] if user.registered_at else "Telegram ID"
                rows.append(
                    [(f"{label} · {user.telegram_id}", f"ad:wl:{action}:{user.telegram_id}")]
                )
    await screen.show(
        "\n\n".join(lines),
        rows + pagination(f"ad:wl:{section}", page, len(records) > PAGE_SIZE) + navigation(MENU),
    )


async def show_entry(screen: Screen, user: User, settings: SettingsService) -> None:
    await screen.reset()
    rows = []
    if user.role != Role.ADMIN:
        rows.append([("➖ Удалить пользователя", f"ad:wl:remove:{user.telegram_id}")])
    await screen.show(
        entry_text(user, ZoneInfo((await settings.get()).timezone)), rows + navigation(MENU)
    )


@router.callback_query(F.data.startswith("ad:wl:entry:"))
async def entry(
    query: CallbackQuery,
    screen: Screen,
    actor: User,
    users: UsersService,
    settings: SettingsService,
) -> None:
    telegram_id = number((query.data or "").split(":")[-1])
    await show_entry(screen, await users.whitelist_entry(actor.id, telegram_id), settings)


async def ask_comment(screen: Screen, state: FSMContext, telegram_id: int) -> None:
    if not 0 < telegram_id < 2**52:
        raise DomainError("Некорректный Telegram User ID.")
    await state.set_state(WhitelistInput.comment)
    await state.update_data(telegram_id=telegram_id)
    await screen.show(
        f"Telegram ID: {telegram_id}\n\nВведите комментарий до 200 знаков или пропустите этот шаг.",
        [[("Без комментария", "ad:wl:skip")]] + navigation("ad:wl:add"),
    )


@router.callback_query(F.data.startswith("ad:wl:choose:"))
async def choose_user(
    query: CallbackQuery, screen: Screen, state: FSMContext, actor: User, users: UsersService
) -> None:
    user = await users.get(actor.id, number((query.data or "").split(":")[-1]))
    await ask_comment(screen, state, user.telegram_id)


async def save_entry(
    screen: Screen,
    state: FSMContext,
    actor: User,
    users: UsersService,
    settings: SettingsService,
    comment: str = "",
) -> None:
    telegram_id = (await state.get_data())["telegram_id"]
    user = await users.add(actor.id, telegram_id, comment)
    await show_entry(screen, user, settings)


@router.callback_query(WhitelistInput.comment, F.data == "ad:wl:skip")
async def skip_comment(
    query: CallbackQuery,
    screen: Screen,
    state: FSMContext,
    actor: User,
    users: UsersService,
    settings: SettingsService,
) -> None:
    await save_entry(screen, state, actor, users, settings)


@router.message(
    StateFilter(WhitelistInput.telegram_id, WhitelistInput.search, WhitelistInput.comment),
    F.text,
    ~F.text.startswith("/"),
)
async def enter_text(
    message: Message,
    screen: Screen,
    state: FSMContext,
    actor: User,
    users: UsersService,
    settings: SettingsService,
) -> None:
    if actor.role != Role.ADMIN:
        raise AccessDenied()
    value = (message.text or "").strip()
    step = await state.get_state()
    if step == WhitelistInput.comment.state:
        await save_entry(screen, state, actor, users, settings, value)
    elif step == WhitelistInput.search.state:
        await show_entry(screen, await users.whitelist_entry(actor.id, number(value)), settings)
    else:
        await ask_comment(screen, state, number(value))


@router.callback_query(F.data.startswith("ad:wl:remove:"))
async def confirm_removal(
    query: CallbackQuery, screen: Screen, state: FSMContext, actor: User, users: UsersService
) -> None:
    telegram_id = number((query.data or "").split(":")[-1])
    user = await users.whitelist_entry(actor.id, telegram_id)
    if user.role == Role.ADMIN or telegram_id in users.admin_ids:
        raise DomainError("Доступ администратора определяется списком ADMIN_IDS.")
    await screen.reset()
    await state.set_state(WhitelistInput.remove)
    await state.update_data(telegram_id=telegram_id, added_at=user.whitelist_added_at)
    await screen.show(
        f"Удалить Telegram ID {telegram_id} из белого списка?\n"
        "Доступ закроется сразу. Существующие бронирования сохранятся.",
        [[("Да, удалить", "ad:wl:drop")]] + navigation(f"ad:wl:entry:{telegram_id}"),
    )


@router.callback_query(WhitelistInput.remove, F.data == "ad:wl:drop")
async def remove_user(
    query: CallbackQuery, screen: Screen, state: FSMContext, actor: User, users: UsersService
) -> None:
    data = await state.get_data()
    telegram_id = data["telegram_id"]
    count = await users.remove(actor.id, telegram_id, expected_added_at=data["added_at"])
    await screen.reset()
    title = f"Доступ закрыт. Предстоящих бронирований: {count}."
    rows = []
    if count:
        title += "\nПри необходимости откройте бронирования и отмените их по отдельности."
        rows.append([("📅 Предстоящие бронирования", f"ad:wl:up:{telegram_id}:0")])
    await screen.show(title, rows + navigation(MENU))


@router.callback_query(F.data.regexp(r"^ad:wl:up:[0-9]+:[0-9]+$"))
async def future_bookings(
    query: CallbackQuery,
    screen: Screen,
    actor: User,
    users: UsersService,
    settings: SettingsService,
) -> None:
    await screen.reset()
    _, _, _, raw_id, raw_page = (query.data or "").split(":")
    telegram_id, page = number(raw_id), number(raw_page, 10**6)
    records = await users.upcoming(actor.id, telegram_id, page * 8)
    timezone = (await settings.get()).timezone
    rows = [
        [
            (
                format_interval(item.start_at, item.end_at, timezone),
                f"p:adminitem:{item.id}:{telegram_id}",
            )
        ]
        for item in records[:8]
    ]
    await screen.show(
        "📅 Предстоящие бронирования" if rows else "Предстоящих бронирований на странице нет.",
        rows + pagination(f"ad:wl:up:{telegram_id}", page, len(records) > 8) + navigation(MENU),
    )
