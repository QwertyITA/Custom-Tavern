"""Diagnostic export for a freeze or a crash.

One plain-text bundle a person can hand over — attach to a message, paste
into a chat — without anyone having to reproduce the problem first: the tail
of the server's own log (`data/tavern.log`, written by `run.sh` regardless of
whether anything is watching it, so a crash's traceback and every request
since are already sitting in it), which pass runs are stuck mid-flight right
now across every chat (the single strongest sign a hang leaves behind — a row
still marked "running" long after a real reply would have finished), masked
backend/tier settings, and basic process health.

Nothing here is a diagnosis. It is what turns "the app froze, I don't know
why" into something somebody else can actually look at.
"""

from __future__ import annotations

import platform
import sys
import time
from datetime import UTC, datetime
from typing import Any

from . import config
from .db import Database

STARTED_AT = time.time()

# A freeze usually shows up (or its absence is itself informative) in the
# last few hundred lines; a whole session's log on a phone with no rotation
# could otherwise run to megabytes by the time anyone thinks to export it.
LOG_TAIL_LINES = 400

# A pass sitting in "running" this long is treated as stuck rather than
# merely slow — long enough that a real reply, even from a small local model,
# wouldn't still be mid-flight; short enough to catch a hang before the
# person watching it gives up and force-closes the app.
STUCK_AFTER_SECONDS = 90

# Settings worth showing for this — timeouts, which backend each tier hits,
# how many things retry. Left out on purpose: prompt_sections (the writing
# library's full text — large, and not what a hang looks like), regex_rules,
# theme/colours — none of it bears on why a turn stopped answering.
_SETTINGS_KEYS = (
    "host", "port", "tiers", "tiers_off", "backends", "token_budget",
    "pass_timeout", "blocking_await_ms", "background_retries",
    "realistic_chat_speed", "feature_web_search", "feature_talking_avatar",
    "feature_character_reactions", "post_process_tracks_state",
    "cut_excess_paragraphs",
)


def _log_tail() -> str:
    path = config.DATA_DIR / "tavern.log"
    if not path.is_file():
        return "(no log file yet — data/tavern.log)"
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError as exc:
        return f"(couldn't read {path}: {exc})"
    return "\n".join(lines[-LOG_TAIL_LINES:]) or "(empty)"


def _stuck_runs(db: Database) -> list[dict[str, Any]]:
    """Every chat's pass_runs, not one — a hang in one conversation is still
    worth seeing even if the export was triggered from a different one."""
    now = time.time()
    out = []
    for row in db.query(
        "SELECT chat_id, turn, pass_id, tier, model, started_at "
        "FROM pass_runs WHERE status='running' ORDER BY started_at"
    ):
        started = row["started_at"] or 0
        age = now - started if started else 0
        if age >= STUCK_AFTER_SECONDS:
            out.append({**dict(row), "running_for_seconds": round(age)})
    return out


def _recent_failures(db: Database, limit: int = 20) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.query(
            "SELECT chat_id, turn, pass_id, tier, model, error, started_at, finished_at "
            "FROM pass_runs WHERE status='failed' ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
    ]


def _settings_snapshot() -> dict[str, Any]:
    d = config.SETTINGS.to_dict()
    return {k: d[k] for k in _SETTINGS_KEYS if k in d}


def _process_health(scheduler: Any) -> dict[str, Any]:
    health: dict[str, Any] = {
        "uptime_seconds": round(time.time() - STARTED_AT),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }
    try:
        import resource

        health["max_rss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:  # noqa: BLE001 — best-effort, not every platform has this
        pass
    pending = getattr(scheduler, "_pending", None)
    if isinstance(pending, dict):
        health["chats_with_pending_background_work"] = sum(1 for v in pending.values() if v)
        health["pending_background_tasks"] = sum(len(v) for v in pending.values())
    locks = getattr(scheduler, "_chat_locks", None)
    if isinstance(locks, dict):
        health["chats_that_have_ever_run_a_turn"] = len(locks)
        health["chats_currently_mid_turn"] = sum(1 for lk in locks.values() if lk.locked())
    return health


def _kv(d: dict[str, Any]) -> str:
    return "\n".join(f"  {k}: {v}" for k, v in d.items()) or "  (none)"


def _kv_blocks(rows: list[dict[str, Any]]) -> str:
    return "\n\n".join(_kv(r) for r in rows) if rows else "(none)"


def build(db: Database, scheduler: Any) -> str:
    stuck = _stuck_runs(db)
    failures = _recent_failures(db)
    sections = [
        f"Personal Tavern debug export — {datetime.now(UTC).isoformat(timespec='seconds')}",
        "=" * 60,
        "",
        "-- process health --",
        _kv(_process_health(scheduler)),
        "",
        "-- settings (masked) --",
        _kv(_settings_snapshot()),
        "",
        f"-- pass runs still 'running' after {STUCK_AFTER_SECONDS}s ({len(stuck)}) --",
        _kv_blocks(stuck),
        "",
        f"-- last {len(failures)} failed pass runs --",
        _kv_blocks(failures),
        "",
        f"-- server log, last {LOG_TAIL_LINES} lines (data/tavern.log) --",
        _log_tail(),
    ]
    return "\n".join(sections)


def filename() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"tavern-debug-{stamp}.txt"
