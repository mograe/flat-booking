import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic

from sqlalchemy.engine import make_url

from app.config import Config


def database_path(path: Path | None) -> Path:
    if path is not None:
        return path.resolve()
    name = make_url(Config().database_url).database
    if not name or name == ":memory:":
        raise ValueError("Укажите путь к файловой базе через --database.")
    return Path(name).resolve()


def ensure_distinct(source: Path, target: Path) -> None:
    if source.resolve() == target.resolve() or (target.exists() and source.samefile(target)):
        raise ValueError("Исходная база и файл назначения совпадают.")


def copy_database(source: Path, target: Path, *, verify_source: bool = False) -> None:
    deadline = monotonic() + 15

    def progress(status: int, remaining: int, total: int) -> None:
        if status in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} and monotonic() >= deadline:
            raise TimeoutError("База занята. Повторите команду после завершения записи.")

    uri = source.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as original:
        if verify_source:
            check_integrity(original)
        with closing(sqlite3.connect(target, timeout=5)) as destination:
            original.backup(destination, pages=128, progress=progress)
            check_integrity(destination)


def check_integrity(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise ValueError("Проверка целостности SQLite обнаружила ошибку.")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("В базе нарушены внешние ключи.")
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if not {"bookings", "users", "studio_settings"} <= tables:
        raise ValueError("Файл не содержит схему Flat Booking.")
