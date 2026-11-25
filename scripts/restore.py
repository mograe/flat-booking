import argparse
import sqlite3
from pathlib import Path

from scripts.sqlite_tools import copy_database, database_path, ensure_distinct


def restore(source: Path, target: Path) -> None:
    source, target = source.resolve(), target.resolve()
    if not source.is_file():
        raise ValueError("Резервная копия не найдена.")
    ensure_distinct(source, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # SQLite replaces the database contents in its own write transaction, including WAL handling.
    copy_database(source, target, verify_source=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Восстановить SQLite-базу Flat Booking.")
    parser.add_argument("source", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--yes", action="store_true", help="Подтвердить замену содержимого базы")
    args = parser.parse_args()
    if not args.yes:
        parser.error("Остановите бота и подтвердите восстановление параметром --yes.")
    try:
        restore(args.source, database_path(args.database))
    except (ValueError, sqlite3.Error, OSError) as error:
        parser.exit(1, f"Восстановление прервано. {error}\n")
    print("База восстановлена.")


if __name__ == "__main__":
    main()
