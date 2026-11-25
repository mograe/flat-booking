from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot.ui import Screen, navigation
from app.database.models import Role, User
from app.services.settings import SettingsService

router = Router(name="start")


@router.message(CommandStart())
@router.callback_query(F.data == "home")
async def home(
    event: Message | CallbackQuery, screen: Screen, actor: User, settings: SettingsService
) -> None:
    await screen.reset()
    options = await settings.get()
    title = f"🎙 {options.studio_name.upper()} | Музыкальная студия"
    contact = [("💬 Связаться с администратором", "contact")]
    if not actor.is_active:
        await screen.show(f"{title}\n\nВаш доступ приостановлен.", [contact])
    elif actor.role == Role.GUEST:
        await screen.show(
            f"🔒 Студия «{options.studio_name}»\n\n"
            "Этот бот предназначен для участников музыкальной студии.\n\n"
            "У вас пока нет доступа к системе бронирования. "
            "Обратитесь к администратору или отправьте заявку на получение доступа.\n\n"
            f"Ваш Telegram ID: {actor.telegram_id}",
            [[("📨 Запросить доступ", "access")], contact],
        )
    else:
        rows = [
            [("🎙 Забронировать", "book")],
            [("📅 Расписание", "s:home")],
            [("🎛 Мои бронирования", "p:home")],
            [("📖 Правила студии", "rules")],
            contact,
        ]
        if actor.role == Role.ADMIN:
            rows.append([("⚙️ Управление студией", "ad:home")])
        await screen.show(
            f"{title}\n\nДобро пожаловать! Здесь можно забронировать студию, "
            "посмотреть расписание и управлять своими посещениями.",
            rows,
        )


@router.message(F.chat.type.in_({"group", "supergroup"}), Command("chatid"))
async def group_id(message: Message) -> None:
    await message.reply(f"ID общего чата: {message.chat.id}")


@router.message(Command("id"))
async def show_id(message: Message, actor: User, screen: Screen) -> None:
    await screen.show(f"Ваш Telegram User ID: {actor.telegram_id}", navigation())


@router.callback_query(F.data == "rules")
async def rules(query: CallbackQuery, screen: Screen, settings: SettingsService) -> None:
    await screen.reset()
    await screen.show("📖 Правила студии\n\n" + (await settings.get()).rules, navigation())


@router.callback_query(F.data == "contact")
async def contact(query: CallbackQuery, screen: Screen, settings: SettingsService) -> None:
    await screen.reset()
    details = (await settings.get()).admin_contact or "Администратор пока не указал контакт."
    await screen.show(f"💬 Связь с администратором\n\n{details}", navigation())
