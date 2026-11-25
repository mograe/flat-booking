import json
import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.config import Config

from app.config import StudioOptions


def test_migrations(tmp_path, monkeypatch):
    path = tmp_path / "migration.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.check(config)
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "users",
            "bookings",
            "notifications",
            "audit_log",
            "studio_settings",
            "blocked_time_slots",
            "access_requests",
        } <= tables
    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest.mark.parametrize(("old_limit", "new_limit"), [(240, 1440), (360, 360)])
def test_migration_preserves_members_and_booking_data(tmp_path, monkeypatch, old_limit, new_limit):
    path = tmp_path / "previous.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    config = Config("alembic.ini")
    command.upgrade(config, "ccf47a25f067")
    stamp = "2026-11-26 12:00:00.000000"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO studio_settings (id, options) VALUES (1, ?)",
            (json.dumps(StudioOptions(max_booking_minutes=old_limit).model_dump()),),
        )
        connection.executemany(
            """
            INSERT INTO users
            (id, telegram_id, first_name, display_name, role, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, 101, "Админ", "Админ", "admin", True, stamp, stamp),
                (2, 102, "Участник", "Участник", "member", True, stamp, stamp),
                (3, 103, "Участник 103", "Участник 103", "member", False, stamp, stamp),
                (4, 104, "Гость", "Гость", "guest", True, stamp, stamp),
            ],
        )
        connection.execute(
            """
            INSERT INTO access_requests (user_id, status, created_at, reviewed_at, reviewed_by)
            VALUES (2, 'approved', ?, ?, 1)
            """,
            (stamp, "2026-11-26 13:00:00.000000"),
        )
        connection.execute(
            """
            INSERT INTO bookings
            (id, user_id, start_at, end_at, purpose, comment, status, version, created_at, updated_at)
            VALUES (8, 2, '2026-11-28 08:00:00', '2026-11-28 12:00:00',
                    'mix', 'Существующая бронь', 'confirmed', 3, ?, ?)
            """,
            (stamp, stamp),
        )
    command.upgrade(config, "head")
    command.check(config)
    with closing(sqlite3.connect(path)) as connection:
        users = connection.execute(
            "SELECT role, is_active, registered_at, whitelist_added_at FROM users ORDER BY id"
        ).fetchall()
        assert users == [
            ("admin", 1, stamp, None),
            ("member", 1, stamp, "2026-11-26 13:00:00.000000"),
            ("member", 0, None, stamp),
            ("guest", 1, stamp, None),
        ]
        assert connection.execute(
            "SELECT id, user_id, comment, status, version FROM bookings"
        ).fetchall() == [(8, 2, "Существующая бронь", "confirmed", 3)]
        options, initialized = connection.execute(
            "SELECT options, whitelist_initialized FROM studio_settings"
        ).fetchone()
        assert json.loads(options)["max_booking_minutes"] == new_limit
        assert initialized == 0
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
