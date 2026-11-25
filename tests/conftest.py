from datetime import UTC, datetime

import pytest

from app.config import StudioOptions
from app.database.models import Base
from app.database.session import Database
from app.services.booking import BookingService
from app.services.settings import SettingsService
from app.services.users import UsersService

NOW = datetime(2026, 11, 28, 7, 59, 59, 990000, tzinfo=UTC)


@pytest.fixture
async def studio(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'studio.sqlite'}")
    try:
        async with db.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        settings = SettingsService(db)
        await settings.initialize(StudioOptions())
        users = UsersService(db, frozenset({1}))
        admin = await users.register(1, "Админ", None, None)
        member = await users.add(admin.id, 2)
        other = await users.add(admin.id, 3)
        guest = await users.register(4, "Гость", None, None)
        yield db, BookingService(db, lambda: NOW), settings, users, admin, member, other, guest
    finally:
        await db.engine.dispose()
