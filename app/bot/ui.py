from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.exceptions import DomainError

Button = tuple[str, str]
Rows = list[list[Button]]
HOME = ("🏠 Главное меню", "home")


def chunks(items: list[Button], size: int) -> Rows:
    return [items[start : start + size] for start in range(0, len(items), size)]


def navigation(back: str = "home") -> Rows:
    return [[("‹ Назад", back), HOME]] if back != "home" else [[HOME]]


def pagination(prefix: str, page: int, more: bool) -> Rows:
    buttons = []
    if page:
        buttons.append(("‹ Страница", f"{prefix}:{page - 1}"))
    if more:
        buttons.append(("Страница ›", f"{prefix}:{page + 1}"))
    return [buttons] if buttons else []


def number(raw: str, maximum: int = 2**52) -> int:
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 16:
        raise DomainError("Некорректное значение.")
    value = int(raw)
    if not 0 <= value <= maximum:
        raise DomainError("Значение вне допустимого диапазона.")
    return value


class Screen:
    def __init__(self, bot: Bot, event: Message | CallbackQuery, state: FSMContext) -> None:
        self.bot = bot
        self.event = event
        self.state = state

    async def show(self, text: str, rows: Rows, *, fresh: bool = False) -> None:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=label, callback_data=value) for label, value in row]
                for row in rows
                if row
            ]
        )
        actions = {value for row in rows for _, value in row}
        if isinstance(self.event, CallbackQuery):
            message = self.event.message
            if not isinstance(message, Message):
                raise DomainError("Откройте меню командой /start.")
            chat_id, message_id = message.chat.id, message.message_id
        else:
            chat_id = self.event.chat.id
            message_id = (await self.state.get_data()).get("screen")
        if message_id and not fresh:
            try:
                await self.bot.edit_message_text(
                    text,
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=markup,
                    link_preview_options={"is_disabled": True},
                )
                await self.state.update_data(screen=message_id, actions=actions)
                return
            except TelegramBadRequest as error:
                if "message is not modified" in str(error):
                    await self.state.update_data(screen=message_id, actions=actions)
                    return
                if not any(
                    reason in str(error)
                    for reason in (
                        "message to edit not found",
                        "message can't be edited",
                    )
                ):
                    raise
        sent = await self.bot.send_message(
            chat_id, text, reply_markup=markup, link_preview_options={"is_disabled": True}
        )
        await self.state.update_data(screen=sent.message_id, actions=actions)

    async def reset(self) -> None:
        data = await self.state.get_data()
        await self.state.set_state(None)
        await self.state.set_data({key: data[key] for key in ("screen", "actions") if key in data})
