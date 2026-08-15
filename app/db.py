"""SQLite access layer: WAL reads in parallel, every write through one queue.

Parallel passes (§5.5) all want to write the moment they land, and SQLite's
writer lock turns that into "database is locked". So: readers get their own
per-thread connection and run concurrently under WAL; writers hand a callable
to a single dedicated writer thread that owns the one write connection.
"""

from __future__ import annotations

import asyncio
import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from .config import DATA_DIR

T = TypeVar("T")

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
_SENTINEL = object()


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")


class Database:
    """One database file; one writer thread; many reader threads."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DATA_DIR / "tavern.db"
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_queue: queue.Queue[Any] = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        self._write_conn: sqlite3.Connection | None = None
        self._closed = False
        self._start_writer()
        self.migrate()

    # ---------------------------------------------------------------- writer

    def _start_writer(self) -> None:
        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="tavern-db-writer", daemon=True
        )
        self._writer_thread.start()

    def _writer_loop(self) -> None:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        _configure(conn)
        self._write_conn = conn
        while True:
            item = self._write_queue.get()
            if item is _SENTINEL:
                break
            fn, result_box, done = item
            try:
                with conn:  # commits on success, rolls back on exception
                    result_box["value"] = fn(conn)
            except BaseException as exc:  # noqa: BLE001 — relayed to the caller
                result_box["error"] = exc
            finally:
                done.set()
        conn.close()

    def write_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run `fn` on the writer thread and block until it commits."""
        if self._closed:
            raise RuntimeError("database is closed")
        box: dict[str, Any] = {}
        done = threading.Event()
        self._write_queue.put((fn, box, done))
        done.wait()
        if "error" in box:
            raise box["error"]
        return box["value"]

    async def write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Async wrapper: never blocks the event loop on the writer queue."""
        return await asyncio.to_thread(self.write_sync, fn)

    # ---------------------------------------------------------------- reader

    @property
    def read_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            _configure(conn)
            self._local.conn = conn
        return conn

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return list(self.read_conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.read_conn.execute(sql, params).fetchone()

    async def aquery(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self.query, sql, params)

    async def aquery_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return await asyncio.to_thread(self.query_one, sql, params)

    # ------------------------------------------------------------- lifecycle

    def migrate(self) -> None:
        """Apply schema.sql, then any versioned migrations (§17)."""
        schema = SCHEMA_PATH.read_text()

        def _apply(conn: sqlite3.Connection) -> None:
            conn.executescript(schema)
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            current = int(row["value"]) if row else 0
            for version, steps in sorted(MIGRATIONS.items()):
                if version > current:
                    for step in steps:
                        _run_migration_step(conn, step)
                    current = version
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(max(current, SCHEMA_VERSION)),),
            )

        self.write_sync(_apply)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._write_queue.put(_SENTINEL)
        if self._writer_thread:
            self._writer_thread.join(timeout=5)


SCHEMA_VERSION = 7

def _run_migration_step(conn: sqlite3.Connection, step: str) -> None:
    """Apply one migration statement, tolerating one that has already landed.

    schema.sql is the create-from-nothing path and carries every column, so on
    a fresh database the `ADD COLUMN` migrations that exist for older installs
    are describing a column that is already there. SQLite has no
    `ADD COLUMN IF NOT EXISTS`, so the duplicate is recognised and skipped —
    and only that one error, because any other failure is a real one.
    """
    try:
        conn.execute(step)
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


# version -> list of statements applied when upgrading past it. schema.sql is
# always CREATE-IF-NOT-EXISTS, so these only carry ALTERs for existing installs.
MIGRATIONS: dict[int, list[str]] = {
    # Personas, and the per-chat and per-character bindings that point at them.
    # schema.sql creates the table for a fresh install; these carry the columns
    # onto a database that already exists.
    2: [
        "ALTER TABLE chats ADD COLUMN persona_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE characters ADD COLUMN persona_id TEXT NOT NULL DEFAULT ''",
    ],
    # Hiding is its own flag rather than a `stage`: the eviction ladder owns
    # stage and moves messages through it, so a hidden message expressed that
    # way would be promoted back into the prompt the next time it ran.
    3: ["ALTER TABLE messages ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0"],
    # The itemised prompt for a reply (§15). Nullable and pruned, because it is
    # the one record whose size is proportional to the prompt rather than to
    # the reply, and keeping every turn's would grow with the square of a chat.
    4: ["ALTER TABLE pass_runs ADD COLUMN prompt TEXT"],
    # Starring a character (§11). Tags and folders were deliberately not built:
    # one flag answers "which of these do I actually use", and a taxonomy for a
    # roster of a dozen is more work to maintain than to scroll past.
    5: ["ALTER TABLE characters ADD COLUMN favourite INTEGER NOT NULL DEFAULT 0"],
    # Attachments (§19). A CREATE rather than an ALTER, so schema.sql's
    # IF NOT EXISTS already covers the fresh-install path and this is a no-op
    # there — it exists for a database that predates the table.
    6: [
        "CREATE TABLE IF NOT EXISTS attachments ("
        "  id TEXT PRIMARY KEY,"
        "  message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,"
        "  kind TEXT NOT NULL, name TEXT NOT NULL,"
        "  stored_as TEXT NOT NULL DEFAULT '', mime TEXT NOT NULL DEFAULT '',"
        "  size INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL DEFAULT '',"
        "  created_at REAL NOT NULL)",
    ],
    # Per-character state namespacing (§15), the prerequisite for group chats.
    # Trust and mood are held *by someone*; the weather is not. Existing rows
    # are renamed in place rather than left to a fallback path, so there is one
    # shape to reason about instead of two forever. Slices whose chat has since
    # lost its character keep their old name — they are unreachable either way,
    # and a rename to ":" would be worse than leaving them alone.
    7: [
        "UPDATE state_slices SET slice_name = slice_name || ':' || ("
        "  SELECT c.character_id FROM chats c WHERE c.id = state_slices.chat_id)"
        " WHERE slice_name IN ('state.vars', 'state.expression', 'state.signals')"
        "   AND EXISTS (SELECT 1 FROM chats c WHERE c.id = state_slices.chat_id)",
        "UPDATE state_writes SET slice_name = slice_name || ':' || ("
        "  SELECT c.character_id FROM chats c WHERE c.id = state_writes.chat_id)"
        " WHERE slice_name IN ('state.vars', 'state.expression', 'state.signals')"
        "   AND EXISTS (SELECT 1 FROM chats c WHERE c.id = state_writes.chat_id)",
    ],
}


def now() -> float:
    return time.time()


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db


def set_db(db: Database | None) -> None:
    """Test hook: swap in a temporary database."""
    global _db
    _db = db
