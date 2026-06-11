"""Unit tests for the nightly database backup: snapshot correctness, next-run
scheduling math, and the run_backup_once seam (snapshot + retention pruning)."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from services.db_backup import (
    backups_dir_for,
    next_run_at,
    prune_old_backups,
    run_backup_once,
    vacuum_into,
)


KYIV = ZoneInfo("Europe/Kyiv")


def test_next_run_today_when_hour_is_ahead() -> None:
    now = datetime(2026, 6, 11, 1, 30, tzinfo=KYIV)
    assert next_run_at(now, 4) == datetime(2026, 6, 11, 4, 0, tzinfo=KYIV)


def test_next_run_tomorrow_when_hour_has_passed() -> None:
    now = datetime(2026, 6, 11, 4, 0, 1, tzinfo=KYIV)
    assert next_run_at(now, 4) == datetime(2026, 6, 12, 4, 0, tzinfo=KYIV)


def test_next_run_exactly_at_the_hour_schedules_tomorrow() -> None:
    now = datetime(2026, 6, 11, 4, 0, 0, tzinfo=KYIV)
    assert next_run_at(now, 4) == datetime(2026, 6, 12, 4, 0, tzinfo=KYIV)


def _create_db(path) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE seen_sources (source_type TEXT, source_id TEXT)")
        conn.executemany(
            "INSERT INTO seen_sources VALUES (?, ?)",
            [("official_marvel_rivals", f"https://example.com/{i}") for i in range(5)],
        )


def test_vacuum_into_produces_a_readable_copy(tmp_path) -> None:
    source = tmp_path / "bot.db"
    target = tmp_path / "bot-backup.db"
    _create_db(source)

    vacuum_into(source, target)

    with closing(sqlite3.connect(target)) as copy:
        rows = copy.execute(
            "SELECT COUNT(*), MIN(source_id) FROM seen_sources"
        ).fetchone()
    assert rows == (5, "https://example.com/0")


def test_vacuum_into_overwrites_stale_target(tmp_path) -> None:
    source = tmp_path / "bot.db"
    target = tmp_path / "snapshot.db"
    _create_db(source)
    target.write_bytes(b"stale leftover from a crashed run")

    vacuum_into(source, target)  # must not raise: VACUUM INTO needs a fresh target

    with closing(sqlite3.connect(target)) as copy:
        assert copy.execute("SELECT COUNT(*) FROM seen_sources").fetchone() == (5,)


def test_run_backup_once_writes_dated_snapshot(tmp_path) -> None:
    db_path = tmp_path / "bot.db"
    _create_db(db_path)
    config = SimpleNamespace(database_path=str(db_path), database_backup_keep=14)

    asyncio.run(run_backup_once(config))

    backups = list((tmp_path / "backups").glob("bot-backup-*.db"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as copy:
        assert copy.execute("SELECT COUNT(*) FROM seen_sources").fetchone() == (5,)
    # No temp leftovers next to the snapshot.
    assert list((tmp_path / "backups").glob("*.tmp")) == []


def test_run_backup_once_prunes_old_snapshots_and_stale_tmp(tmp_path) -> None:
    db_path = tmp_path / "bot.db"
    _create_db(db_path)
    backups_dir = backups_dir_for(str(db_path))
    backups_dir.mkdir()
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (backups_dir / f"bot-backup-{day}.db").write_bytes(b"old snapshot")
    (backups_dir / "bot-backup-2026-01-04.db.tmp").write_bytes(b"crashed run leftover")
    config = SimpleNamespace(database_path=str(db_path), database_backup_keep=2)

    asyncio.run(run_backup_once(config))

    names = sorted(p.name for p in backups_dir.iterdir())
    # Newest two remain: the 2026-01-03 file and today's fresh snapshot;
    # older ones and the stale .tmp are gone.
    assert len(names) == 2
    assert "bot-backup-2026-01-03.db" in names
    assert not any(name.endswith(".tmp") for name in names)


def test_run_backup_once_skips_when_db_missing(tmp_path) -> None:
    config = SimpleNamespace(
        database_path=str(tmp_path / "absent.db"), database_backup_keep=14
    )

    asyncio.run(run_backup_once(config))

    assert not (tmp_path / "backups").exists()


def test_prune_keeps_the_newest_files(tmp_path) -> None:
    for day in ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"):
        (tmp_path / f"bot-backup-{day}.db").write_bytes(b"x")

    removed = prune_old_backups(tmp_path, "bot", keep=3)

    assert removed == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "bot-backup-2026-01-02.db",
        "bot-backup-2026-01-03.db",
        "bot-backup-2026-01-04.db",
    ]
