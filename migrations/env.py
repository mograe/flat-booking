import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from app.config import Config
from app.database.models import Base
from app.database.session import Database


def migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def migrate_online(url: str) -> None:
    database = Database(url)
    try:
        async with database.engine.begin() as connection:
            # SQLite's legacy transaction mode does not start a transaction for DDL.
            await connection.exec_driver_sql("BEGIN")
            await connection.run_sync(migrate)
    finally:
        await database.engine.dispose()


url = Config().database_url
if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(migrate_online(url))
