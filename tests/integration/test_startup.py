import asyncio
import logging

import pytest
from aiogram import Dispatcher
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text

from app.config import Config
from app.database.models import Role
from app.exceptions import DomainError
from app.main import configure_logging, run


async def test_application_startup_and_shutdown_without_telegram(tmp_path, monkeypatch, caplog):
    path = tmp_path / "startup.sqlite"
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    await asyncio.to_thread(command.upgrade, AlembicConfig("alembic.ini"), "head")
    config = Config(
        _env_file=None,
        bot_token="123456:fake",
        admin_ids="1",
        whitelist_ids="101,102",
        studio_name="Из окружения",
        database_url=url,
    )
    calls = []

    async def polling(dispatcher, bot, **dependencies):
        settings = dependencies["settings"]
        users = dependencies["users"]
        admin = await users.register(1, "Админ", None, None)
        calls.append((await settings.get()).studio_name)
        if len(calls) == 1:
            assert (await users.whitelist_entry(admin.id, 101)).registered_at is None
            assert (await users.register(101, "Участник", None, None)).role == Role.MEMBER
            await users.remove(admin.id, 101)
        else:
            assert (await users.register(101, "Участник", None, None)).role == Role.GUEST
            assert (await users.whitelist_entry(admin.id, 102)).role == Role.MEMBER
            with pytest.raises(DomainError):
                await users.whitelist_entry(admin.id, 103)

    monkeypatch.setattr(Dispatcher, "start_polling", polling)
    with caplog.at_level(logging.INFO, logger="app.main"):
        await run(config)
        await run(
            config.model_copy(
                update={
                    "studio_name": "Другое значение из окружения",
                    "whitelist_ids": frozenset({101, 102, 103}),
                }
            )
        )
    assert calls == ["Из окружения", "Из окружения"]
    assert caplog.messages.count("Запуск приложения") == 2
    assert caplog.messages.count("Приложение остановлено") == 2


async def test_sql_debug_and_error_logging_keep_parameters_private(
    studio, monkeypatch, capsys, caplog
):
    db, *_ = studio
    for name in (
        "",
        "aiohttp",
        "aiosqlite",
        "asyncio",
        "apscheduler",
        "sqlalchemy",
        "aiogram",
        "sqlalchemy.engine",
    ):
        caplog.set_level(logging.getLogger(name).level, logger=name or None)
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    config = Config(_env_file=None, bot_token="123456:fake", log_level="DEBUG")
    private_value = "Личные данные участника"
    query = text("SELECT :value")

    configure_logging(config)
    assert db.engine.echo is False
    assert logging.getLogger("sqlalchemy").getEffectiveLevel() == logging.WARNING
    async with db.engine.connect() as connection:
        await connection.execute(query, {"value": private_value})
    assert "SELECT" not in capsys.readouterr().err

    configure_logging(config.model_copy(update={"sql_echo": True}))
    async with db.engine.connect() as connection:
        await connection.execute(query, {"value": private_value})
    try:
        raise ValueError(private_value)
    except ValueError:
        logging.getLogger("aiogram.event").exception("Failed for user %s", private_value)
    logging.getLogger("app").debug("Токен %s", config.bot_token.get_secret_value())
    output = capsys.readouterr().err
    assert output.count("SELECT ?") == 1
    assert "Сбой обработки обновления Telegram" in output
    assert "ValueError" in output
    assert "[скрыто]" in output
    assert private_value not in output
    assert config.bot_token.get_secret_value() not in output
