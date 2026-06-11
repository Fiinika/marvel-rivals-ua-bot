"""Nightly on-server backup of the SQLite database.

Once a day, at DATABASE_BACKUP_HOUR local time (ARTICLE_TIMEZONE), the bot
snapshots its SQLite file with ``VACUUM INTO`` — a consistent copy even while
the bot keeps writing — into the ``backups/`` folder next to the database
file. In production that folder lives on the persistent ``botdata`` volume,
so snapshots survive image upgrades; downloading them off the server is left
to the operator on purpose. The newest DATABASE_BACKUP_KEEP snapshots are
kept, older ones are pruned.

Failures are contained exactly like in the news scheduler: any error is
logged and the loop simply waits for the next night.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from config import Config


logger = logging.getLogger(__name__)


def start_db_backup_scheduler(config: Config) -> asyncio.Task | None:
    """Start the nightly backup loop, or return None when the feature is off."""
    if not config.enable_database_backup:
        logger.info("Nightly database backup is disabled (ENABLE_DATABASE_BACKUP).")
        return None

    logger.info(
        "Nightly database backup enabled: daily at %02d:00 %s, keeping the %s newest files in %s",
        config.database_backup_hour,
        config.article_timezone,
        config.database_backup_keep,
        backups_dir_for(config.database_path),
    )
    return asyncio.create_task(_backup_loop(config), name="db-backup-scheduler")


async def _backup_loop(config: Config) -> None:
    tz = _resolve_timezone(config.article_timezone)
    last_backup_date = None
    while True:
        now = datetime.now(tz)
        next_run = next_run_at(now, config.database_backup_hour)
        await asyncio.sleep(max(1.0, (next_run - now).total_seconds()))
        # asyncio.sleep follows the monotonic clock while next_run_at reads the
        # wall clock: an NTP step backwards during the ~24 h sleep would re-run
        # the same night, so a calendar-date guard keeps the backup once-a-day.
        run_date = datetime.now(tz).date()
        if run_date == last_backup_date:
            continue
        try:
            await run_backup_once(config)
            last_backup_date = run_date
        except Exception:
            logger.exception("Nightly database backup failed; retrying next night.")


def next_run_at(now: datetime, hour: int) -> datetime:
    """The next occurrence of ``hour``:00 strictly after ``now`` (same tz)."""
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _resolve_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning("Unknown ARTICLE_TIMEZONE %r; using UTC for the backup schedule.", name)
        return timezone.utc


def backups_dir_for(database_path: str) -> Path:
    return Path(database_path).parent / "backups"


async def run_backup_once(config: Config) -> None:
    """Write one dated snapshot into backups/ and prune the oldest ones."""
    db_path = Path(config.database_path)
    if not db_path.exists():
        logger.warning("Database file %s does not exist; skipping backup.", db_path)
        return

    backups_dir = backups_dir_for(config.database_path)
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc)
    final = backups_dir / f"{db_path.stem}-backup-{stamp:%Y-%m-%d}.db"
    # Snapshot to a temp name first so a crash mid-copy can never leave a
    # truncated file under the final, "looks like a valid backup" name.
    tmp = final.with_name(final.name + ".tmp")
    try:
        await asyncio.to_thread(vacuum_into, db_path, tmp)
        tmp.replace(final)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove temporary backup file %s", tmp)

    size_kb = max(1, final.stat().st_size // 1024)
    pruned = prune_old_backups(backups_dir, db_path.stem, keep=config.database_backup_keep)
    logger.info(
        "Nightly database backup written: %s (%s KB); pruned %s old file(s).",
        final,
        size_kb,
        pruned,
    )


def prune_old_backups(backups_dir: Path, stem: str, *, keep: int) -> int:
    """Keep the ``keep`` newest snapshots, drop the rest; returns the removed count.

    The dated ISO filenames sort chronologically by name. Leftover ``.tmp``
    files from a crashed run are always garbage by the time this is called.
    """
    stale = sorted(backups_dir.glob(f"{stem}-backup-*.db"))[: -max(1, keep)]
    stale += backups_dir.glob(f"{stem}-backup-*.db.tmp")
    removed = 0
    for path in stale:
        try:
            path.unlink()
            removed += 1
        except OSError:
            logger.warning("Could not remove old backup %s", path)
    return removed


def vacuum_into(source: Path, target: Path) -> None:
    """Write a consistent snapshot of ``source`` to ``target`` via VACUUM INTO.

    Runs on a dedicated connection so it is safe alongside the bot's aiosqlite
    writes; SQLite requires the target file to not exist yet. The generous busy
    timeout lets the copy wait out a concurrent write transaction instead of
    failing with "database is locked" once the file grows.
    """
    target.unlink(missing_ok=True)
    connection = sqlite3.connect(str(source), timeout=30)
    try:
        connection.execute("VACUUM INTO ?", (str(target),))
    finally:
        connection.close()
