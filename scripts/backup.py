import argparse
import os
import sqlite3
from pathlib import Path
from tempfile import NamedTemporaryFile

from scripts.sqlite_tools import copy_database, database_path, ensure_distinct


def backup(source: Path, target: Path) -> None:
    source, target = source.resolve(), target.resolve()
    if not source.is_file():
        raise ValueError("Исходная база не найдена.")
    ensure_distinct(source, target)
    if target.exists():
        raise FileExistsError("Резервная копия с таким именем уже существует.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(prefix=".flat-", dir=target.parent) as temporary:
        copy_database(source, Path(temporary.name))
        # Atomically publish a completed snapshot without replacing an existing backup.
        os.link(temporary.name, target)


def main() -> None:
    parser = argparse.ArgumentParser(description="Создать резервную копию активной SQLite-базы.")
    parser.add_argument("target", type=Path)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    try:
        backup(database_path(args.database), args.target)
    except (ValueError, sqlite3.Error, OSError) as error:
        parser.exit(1, f"Резервное копирование прервано. {error}\n")
    print(f"Резервная копия сохранена в {args.target}.")


if __name__ == "__main__":
    main()
