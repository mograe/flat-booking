import asyncio
import logging
import traceback
from copy import copy
from types import TracebackType

from aiogram import Bot
from aiogram.utils.token import TokenValidationError
from pydantic import ValidationError

from app.bot.delivery import TelegramDelivery
from app.bot.dispatcher import create_dispatcher
from app.config import Config
from app.database.session import Database
from app.services.access import AccessService
from app.services.admin import AdminService
from app.services.availability import AvailabilityService
from app.services.booking import BookingService
from app.services.scheduler import NotificationWorker
from app.services.settings import SettingsService
from app.services.users import UsersService

logger = logging.getLogger(__name__)
LIBRARY_MESSAGES = {
    "aiogram.dispatcher": "Сбой опроса Telegram",
    "aiogram.event": "Сбой обработки обновления Telegram",
    "aiogram": "Сбой Telegram-адаптера",
    "aiohttp": "Сбой HTTP-клиента",
    "aiosqlite": "Сбой SQLite-драйвера",
    "asyncio": "Сбой асинхронной задачи",
}


class RedactingFormatter(logging.Formatter):
    def __init__(self, token: str) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.token = token

    def format(self, record: logging.LogRecord) -> str:
        record = copy(record)
        record.exc_text = None
        record.stack_info = None
        library_message = LIBRARY_MESSAGES.get(record.name) or LIBRARY_MESSAGES.get(
            record.name.partition(".")[0]
        )
        if library_message:
            args = record.args if isinstance(record.args, tuple) else ()
            error = (
                record.exc_info[1]
                if record.exc_info
                else next((arg for arg in args if isinstance(arg, BaseException)), None)
            )
            record.msg = f"{library_message} ({type(error).__name__})" if error else library_message
            record.args = ()
        message = super().format(record)
        return message.replace(self.token, "[скрыто]") if self.token else message

    def formatException(
        self, exc_info: tuple[type[BaseException], BaseException, TracebackType | None]
    ) -> str:
        error_type, _, trace = exc_info
        frames = " → ".join(f"{frame.name}:{frame.lineno}" for frame in traceback.extract_tb(trace))
        return f"{error_type.__name__} ({frames})" if frames else error_type.__name__


def configure_logging(config: Config) -> None:
    handler = logging.StreamHandler()
    handler.setLevel(config.log_level)
    handler.setFormatter(RedactingFormatter(config.bot_token.get_secret_value()))
    logging.basicConfig(level=config.log_level, handlers=[handler], force=True)
    for name in ("aiohttp", "aiosqlite", "asyncio", "apscheduler", "sqlalchemy"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.ERROR)
    # An explicit engine log level keeps SQL on the application's handler.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if config.sql_echo else logging.WARNING
    )


async def run(config: Config) -> None:
    logger.info("Запуск приложения")
    db = Database(config.database_url)
    settings = SettingsService(db)
    try:
        await settings.initialize(config.studio_options(), config.whitelist_ids)
        async with Bot(config.bot_token.get_secret_value()) as bot:
            worker = NotificationWorker(
                db,
                TelegramDelivery(bot).send,
                notification_poll_interval_seconds=config.notification_poll_interval_seconds,
                booking_status_check_interval_seconds=config.booking_status_check_interval_seconds,
            )
            dispatcher = create_dispatcher()
            await worker.start()
            try:
                await dispatcher.start_polling(
                    bot,
                    close_bot_session=False,
                    tasks_concurrency_limit=32,
                    tasks_shutdown_timeout=15,
                    users=UsersService(db, config.admin_ids),
                    access=AccessService(db, config.admin_ids),
                    admin=AdminService(db),
                    settings=settings,
                    availability=AvailabilityService(db),
                    booking=BookingService(db),
                )
            finally:
                await worker.stop()
    finally:
        await db.engine.dispose()
        logger.info("Приложение остановлено")


def main() -> None:
    try:
        config = Config()
    except ValidationError as error:
        fields = sorted({".".join(map(str, item["loc"])) for item in error.errors()})
        raise SystemExit(f"Проверьте конфигурацию ({', '.join(fields)}).") from None
    if not config.bot_token.get_secret_value():
        raise SystemExit("Укажите BOT_TOKEN в .env.")
    configure_logging(config)
    if not config.admin_ids:
        logger.warning(
            "ADMIN_IDS пуст. Используйте /id, затем добавьте администратора и перезапустите бота."
        )
    try:
        asyncio.run(run(config))
    except TokenValidationError:
        logger.error("Некорректный формат BOT_TOKEN")
        raise SystemExit("Проверьте формат BOT_TOKEN.") from None
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("Приложение завершилось с ошибкой")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
