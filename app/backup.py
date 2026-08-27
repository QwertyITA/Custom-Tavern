"""Full-data backup (ISSUES-TRIAGE.md #1).

Everything that makes this installation *this* installation, not an empty
one: the database — chats, characters, memories, settings rows kept there —
plus the file-backed assets no other export already covers (portraits,
avatar-idle loops, custom backgrounds, message attachments), and
`settings.json` itself, since a backup that could not also restore which
backend you were talking to would still leave you starting over.

This repository is public and `settings.json` holds real credentials
(CLAUDE.md) — the zip this module builds does too, deliberately, because a
backup that can't restore your backend config isn't one. Treat the
downloaded file with the same care as the phone itself: it never leaves this
process, but once it's sitting in Downloads it's your own to protect.

Restore is deliberately not a route. Swapping a live database and a
directory tree out from under the very server serving the request is the
kind of thing that wants the server stopped first — see README, "Backup".
"""

from __future__ import annotations

import io
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from . import attachments, config
from .db import Database


def _db_snapshot_bytes(db: Database) -> bytes:
    """A consistent snapshot of the database, not a raw file copy.

    `tavern.db` runs in WAL mode (db.py) — a plain `read_bytes()` can miss
    whatever is still sitting in `-wal` and never made it into the main file.
    SQLite's own online-backup API is what actually produces a single
    consistent file regardless of journal mode, and running it as a job on
    the writer thread (`db.write_sync`) serialises it with every other write
    the same way the rest of this app already does, rather than racing one.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target_path = Path(tmp) / "tavern.db"

        def _do(conn: sqlite3.Connection) -> None:
            target = sqlite3.connect(target_path)
            try:
                conn.backup(target)
            finally:
                target.close()

        db.write_sync(_do)
        return target_path.read_bytes()


def _add_dir(zf: zipfile.ZipFile, arc_prefix: str, path: Path) -> None:
    if not path.is_dir():
        return
    for file in sorted(path.rglob("*")):
        if file.is_file():
            zf.write(file, f"{arc_prefix}/{file.relative_to(path).as_posix()}")


def build(db: Database) -> bytes:
    """The whole install as one zip: `tavern.db`, `settings.json`, and every
    asset directory the rest of the app doesn't already export on its own.

    Directories are read off the `config`/`attachments` modules at call time,
    not imported by name, so a test pointing `config.AVATAR_DIR` (etc.) at a
    tmp_path — the same pattern `isolated_avatars` already uses elsewhere —
    is actually honoured here instead of silently backing up the developer's
    own real directories.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("tavern.db", _db_snapshot_bytes(db))
        settings_path = config.settings_path()
        if settings_path.is_file():
            zf.write(settings_path, "settings.json")
        _add_dir(zf, "avatars", config.AVATAR_DIR)
        _add_dir(zf, "avatar_idle", config.AVATAR_IDLE_DIR)
        _add_dir(zf, "backgrounds", config.USER_BACKGROUND_DIR)
        _add_dir(zf, "attachments", attachments.ATTACHMENT_DIR)
    return buf.getvalue()


def filename() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"tavern-backup-{stamp}.zip"
