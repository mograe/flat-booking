import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot.ui import Screen, navigation
from app.database.models import Role
from app.exceptions import AccessDenied, DomainError
from app.services.users import UsersService

logger = logging.getLogger(__name__)


class InteractionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (CallbackQuery, Message)):
            return None
        message = event.message if isinstance(event, CallbackQuery) else event
        if (
            isinstance(event, Message)
            and message is not None
            and message.chat.type in {"group", "supergroup"}
            and event.from_user is not None
            and event.from_user.id in data["users"].admin_ids
            and await Command("chatid")(event, data["bot"])
        ):
            return await handler(event, data)
        if message is None or message.chat.type != "private" or event.from_user is None:
            if isinstance(event, CallbackQuery):
                await self.answer(event, "Откройте бота в личном чате.")
            return None

        users: UsersService = data["users"]
        state: FSMContext = data["state"]
        sender = event.from_user
        actor = await users.register(
            sender.id, sender.first_name, sender.last_name, sender.username
        )
        screen = Screen(data["bot"], event, state)
        data.update(actor=actor, screen=screen)
        response: str | None = None
        try:
            if isinstance(event, CallbackQuery):
                stored = await state.get_data()
                if (
                    not isinstance(message, Message)
                    or stored.get("screen") != message.message_id
                    or event.data not in stored.get("actions", ())
                ):
                    raise DomainError("Кнопка устарела. Откройте меню командой /start.")
                callback = event.data or ""
                if callback == "noop":
                    return None
                if callback not in {"home", "contact", "access", "access:send"}:
                    if not actor.is_active or actor.role == Role.GUEST:
                        raise AccessDenied()
                    if callback.startswith("ad:") and actor.role != Role.ADMIN:
                        raise AccessDenied()
            elif (not actor.is_active or actor.role == Role.GUEST) and not await Command(
                "start", "id"
            )(event, data["bot"]):
                raise AccessDenied()
            return await handler(event, data)
        except DomainError as error:
            response = str(error)
            if isinstance(event, Message):
                # Keep the current screen and its buttons available while the user corrects input.
                await message.answer(response)
        except TelegramAPIError as error:
            logger.warning("Telegram не принял действие (%s)", type(error).__name__)
            response = "Действие не удалось отобразить. Откройте /start."
        finally:
            if isinstance(event, CallbackQuery):
                await self.answer(event, response)

    @staticmethod
    async def answer(query: CallbackQuery, text: str | None = None) -> None:
        try:
            await query.answer(text, show_alert=bool(text))
        except TelegramAPIError as error:
            logger.debug("Ответ на кнопку не доставлен (%s)", type(error).__name__)


async def unknown_callback(query: CallbackQuery) -> None:
    raise DomainError("Кнопка устарела. Откройте меню командой /start.")


async def unknown_message(message: Message, screen: Screen, state: FSMContext) -> None:
    if await state.get_state():
        await message.answer("Для этого шага используйте кнопки или отправьте текст.")
    elif (message.text or "").startswith("/"):
        await screen.show(
            "Используйте /start для входа в меню или /id для просмотра вашего ID.", navigation()
        )
