import sqlite3
from contextlib import closing

import pytest
from sqlalchemy.engine import make_url

from scripts.backup import backup
from scripts.restore import restore


async def test_backup_reads_live_wal_and_restore_round_trip(studio, tmp_path):
    db, _, settings, _, admin, *_ = studio
    await settings.update(admin.id, {"studio_name": "До копии"})
    source = tmp_path / "studio.sqlite"
    assert make_url(db.engine.url).database == str(source)
    snapshot = tmp_path / "backup.sqlite"
    with closing(sqlite3.connect(source)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT options FROM studio_settings").fetchall()
        await settings.update(admin.id, {"studio_name": "Из WAL"})
        backup(source, snapshot)
        with pytest.raises(FileExistsError):
            backup(source, snapshot)
    await settings.update(admin.id, {"studio_name": "После копии"})
    await db.engine.dispose()
    restore(snapshot, source)
    assert (await settings.get()).studio_name == "Из WAL"

    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_text("invalid")
    with pytest.raises(sqlite3.DatabaseError):
        restore(corrupt, source)
    assert (await settings.get()).studio_name == "Из WAL"
