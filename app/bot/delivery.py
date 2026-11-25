from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import LinkPreviewOptions

from app.services.scheduler import DeliveryError


class TelegramDelivery:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send(self, chat_id: int, body: str) -> None:
        try:
            await self.bot.send_message(
                chat_id,
                body,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                request_timeout=8,
            )
        except TelegramRetryAfter as error:
            raise DeliveryError(type(error).__name__, retry_after=error.retry_after) from None
        except (TelegramNetworkError, TelegramServerError) as error:
            raise DeliveryError(type(error).__name__, retry_after=30) from None
        except TelegramAPIError as error:
            raise DeliveryError(type(error).__name__) from None
