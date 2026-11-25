import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, url: str) -> None:
        parsed = make_url(url)
        if parsed.database and parsed.database != ":memory:":
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(url, echo=False, hide_parameters=True)

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure_sqlite(connection, record) -> None:
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA busy_timeout=10000")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

        self.reminder_lock = asyncio.Lock()
        self.notification_lock = asyncio.Lock()
        self.booking_status_lock = asyncio.Lock()
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def write(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session, session.begin():
            await session.execute(text("BEGIN IMMEDIATE"))
            yield session
        for action in session.info.get("admin_actions", ()):
            logger.info("Административное действие выполнено (%s)", action)
