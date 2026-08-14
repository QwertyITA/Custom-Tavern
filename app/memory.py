"""Memory store (§7.3).

Durable facts extracted by the background `memory` pass, kept outside the
eviction ladder so a fact survives the message that carried it. This is what
makes dropping old messages safe rather than lossy (§7.2).

Retrieval is keyword-first by decision — it reuses the lorebook's matching and
adds no new infrastructure. Embedding similarity is the upgrade path if recall
disappoints. Scope is per-character.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid

from .db import Database, now
from .markup import to_plain

_WORD = re.compile(r"[a-z0-9']+")
_STOPWORDS = frozenset(
    """a an and are as at be been but by for from had has have he her his i if in is it its
    me my not of on or she that the their them they this to was were what when who will with
    you your""".split()
)

MAX_KEYS = 8


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def normalise(text: str) -> str:
    return " ".join(_words(to_plain(text)))


def derive_keys(text: str, given: list[str] | None = None) -> list[str]:
    """Keys the model supplied, plus content words as a fallback."""
    keys = [k.strip().lower() for k in (given or []) if k and k.strip()]
    if len(keys) < 3:
        keys.extend(w for w in _words(text) if w not in _STOPWORDS and len(w) > 3)
    seen: list[str] = []
    for key in keys:
        if key not in seen:
            seen.append(key)
    return seen[:MAX_KEYS]


def _similar(a: str, b: str) -> float:
    """Jaccard over content words — enough to catch a restated fact."""
    left = {w for w in a.split() if w not in _STOPWORDS}
    right = {w for w in b.split() if w not in _STOPWORDS}
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


DEDUPE_THRESHOLD = 0.7


def store(
    db: Database,
    character_id: str,
    items: list[dict],
    *,
    chat_id: str = "",
    turn: int = 0,
    source: str = "memory_pass",
) -> list[str]:
    """Extract → dedupe → store. Returns the ids actually inserted."""
    existing = [
        normalise(row["text"])
        for row in db.query("SELECT text FROM memories WHERE character_id=?", (character_id,))
    ]

    pending: list[tuple] = []
    inserted: list[str] = []
    for item in items:
        raw = item.get("text") if isinstance(item, dict) else item
        text = str(raw).strip() if raw else ""
        if not text:
            continue
        norm = normalise(text)
        if not norm or any(_similar(norm, other) >= DEDUPE_THRESHOLD for other in existing):
            continue
        existing.append(norm)
        memory_id = uuid.uuid4().hex
        keys = derive_keys(text, item.get("keys") if isinstance(item, dict) else None)
        pending.append(
            (
                memory_id,
                character_id,
                chat_id or None,
                text,
                json.dumps(keys),
                turn,
                source,
                now(),
            )
        )
        inserted.append(memory_id)

    if not pending:
        return []

    def _insert(conn: sqlite3.Connection) -> None:
        conn.executemany(
            "INSERT INTO memories(id, character_id, chat_id, text, keys, created_turn, "
            "source, created_at) VALUES(?,?,?,?,?,?,?,?)",
            pending,
        )

    db.write_sync(_insert)
    return inserted


def retrieve(db: Database, character_id: str, query_text: str, limit: int = 6) -> list[dict]:
    """Keyword-match retrieval, most relevant first, recency as the tiebreak."""
    rows = db.query(
        "SELECT id, text, keys, created_turn FROM memories WHERE character_id=? "
        "ORDER BY created_turn DESC",
        (character_id,),
    )
    if not rows:
        return []

    haystack = set(_words(to_plain(query_text)))
    scored: list[tuple[float, int, dict]] = []
    for row in rows:
        keys = set(json.loads(row["keys"]))
        hits = len(keys & haystack)
        if hits == 0:
            # Fall back to the memory's own content words, so a fact still
            # surfaces when the model gave it unhelpful keys.
            content = {w for w in _words(row["text"]) if w not in _STOPWORDS and len(w) > 3}
            hits = len(content & haystack) * 0.5
        if hits <= 0:
            continue
        scored.append((hits, row["created_turn"], dict(row)))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [
        {"id": item[2]["id"], "text": item[2]["text"], "created_turn": item[2]["created_turn"]}
        for item in scored[:limit]
    ]


def list_all(db: Database, character_id: str) -> list[dict]:
    return [
        {
            "id": row["id"],
            "text": row["text"],
            "keys": json.loads(row["keys"]),
            "created_turn": row["created_turn"],
            "source": row["source"],
        }
        for row in db.query(
            "SELECT id, text, keys, created_turn, source FROM memories "
            "WHERE character_id=? ORDER BY created_turn DESC",
            (character_id,),
        )
    ]


def forget(db: Database, memory_id: str) -> None:
    db.write_sync(lambda conn: conn.execute("DELETE FROM memories WHERE id=?", (memory_id,)))


def render(memories: list[dict]) -> str:
    return "\n".join(f"- {m['text']}" for m in memories)
