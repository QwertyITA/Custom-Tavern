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
from typing import Any

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

# ------------------------------------------------------------ quality (§41)
#
# What a memory can be *about*, as a closed set. The categories are not
# decoration: they are what makes "is this worth keeping" a question with a
# checkable answer instead of a matter of taste, and each one names a class of
# fact that is still true in fifty turns.
#
# The important entry is the one that is not here. CHATTER is a label the
# extracting pass is told it may return and this module never stores — a bin
# for greetings, mood, weather and restatements of the character sheet. A
# model asked for facts will produce *something*, because producing something
# feels like doing the job; without somewhere legitimate to put small talk it
# dresses small talk up as a fact, which is exactly how a store fills with
# "the user said hello".
KINDS = (
    "identity",      # who someone is: names, roles, jobs, where they live
    "relationship",  # how two people stand with each other, and how that changed
    "commitment",    # promises, plans, arrangements, debts — things owed
    "event",         # something that happened and still has consequences
    "preference",    # durable likes, dislikes, habits, fears
    "possession",    # objects, injuries, scars, money, the state of the world
)
CHATTER = "chatter"

# The floor, applied here rather than in the prompt. A model told in prose to
# hold a bar will rationalise its way under it; a model asked for a number and
# held to it by code cannot. 1 is reserved for "trivial" and never survives.
MIN_IMPORTANCE = 2

# When the store is worth tidying. Both conditions, not either: a big store
# that has not changed does not need re-reading, and a handful of new facts in
# a small store is not yet a mess. Cheap to check, and it means the expensive
# pass is paid for when there is actually something to do.
COMPRESS_AT = 30
COMPRESS_EVERY = 12
# What compression aims to leave behind. Not a hard cap enforced on write —
# an abrupt "memory full" is worse than a store that is briefly untidy — but
# the number the compressing pass is asked to work towards.
MEMORY_BUDGET = 40


def rated(kind: str, importance: Any) -> tuple[str, int]:
    """One (kind, importance) pair, normalised. Anything unrecognised comes
    back as ('', 0) — unrated rather than rejected, which is what every
    memory written before this existed is, and what a hand-written one stays."""
    kind = str(kind or "").strip().lower()
    if kind not in KINDS and kind != CHATTER:
        kind = ""
    try:
        score = int(importance)
    except (TypeError, ValueError):
        score = 0
    return kind, max(0, min(5, score))


def store(
    db: Database,
    character_id: str,
    items: list[dict],
    *,
    chat_id: str = "",
    turn: int = 0,
    source: str = "memory_pass",
) -> list[str]:
    """Rate → gate → dedupe → store. Returns the ids actually inserted.

    The gate is the part that matters (§41). Everything the extracting pass
    offers arrives with a kind and an importance it had to commit to, and the
    two rejections below are made here rather than asked for in the prompt:
    anything it filed as CHATTER, and anything it rated under MIN_IMPORTANCE.
    A model can be talked out of its own instructions; it cannot be talked out
    of a threshold applied after it has answered.

    A hand-written memory (source other than the pass) is never rated and
    never gated. Someone who typed a fact in has already made the judgement
    this gate exists to make.
    """
    from_pass = source == "memory_pass"
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
        kind, importance = rated(
            item.get("kind") if isinstance(item, dict) else "",
            item.get("importance") if isinstance(item, dict) else 0,
        )
        if from_pass and (kind == CHATTER or kind == "" or importance < MIN_IMPORTANCE):
            # Small talk, an unrecognised category, or a fact its own
            # extractor would not stand behind. An unrated one from the pass
            # is refused too: the rating is the contract, and a reply that
            # skips it is a reply that skipped the thinking.
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
                kind,
                importance,
            )
        )
        inserted.append(memory_id)

    if not pending:
        return []

    def _insert(conn: sqlite3.Connection) -> None:
        conn.executemany(
            "INSERT INTO memories(id, character_id, chat_id, text, keys, created_turn, "
            "source, created_at, kind, importance) VALUES(?,?,?,?,?,?,?,?,?,?)",
            pending,
        )

    db.write_sync(_insert)
    return inserted


def retrieve(
    db: Database, character_id: str, query_text: str, limit: int = 6, *, turn: int = 0
) -> list[dict]:
    """Keyword-match retrieval, weighted by what a memory is worth, recency as
    the tiebreak.

    Relevance still leads — a fact about dogs has no business surfacing on a
    conversation about boats however important it is. But among memories that
    *do* match, the ones worth more win, which is what stops a junk memory
    sharing one word with the message from crowding out the fact that
    matters (§41).

    Retrieval is also the only honest evidence of usefulness this system can
    get, so it records it: every memory handed to a prompt has its use count
    bumped. Nothing reads that to rank with — a new fact would never catch up
    — but compression does, and "never once used in a long life" is the
    strongest signal a store can offer about what it should drop.
    """
    rows = db.query(
        "SELECT id, text, keys, created_turn, kind, importance, uses "
        "FROM memories WHERE character_id=? ORDER BY created_turn DESC",
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
        # Unrated (0) sits at the neutral 3, so a memory written before any of
        # this existed is neither favoured nor punished for it.
        weight = (row["importance"] or 3) / 3.0
        scored.append((hits * weight, row["created_turn"], dict(row)))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    picked = [item[2] for item in scored[:limit]]
    if picked:
        _mark_used(db, [row["id"] for row in picked], turn)
    return [
        {"id": row["id"], "text": row["text"], "created_turn": row["created_turn"]}
        for row in picked
    ]


def _mark_used(db: Database, ids: list[str], turn: int) -> None:
    """Bookkeeping only, and deliberately not awaited by anything: a failure
    to record a use must never cost the prompt the memory it was recording."""
    placeholders = ",".join("?" for _ in ids)
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE memories SET uses = uses + 1, last_used_turn = MAX(last_used_turn, ?) "
            f"WHERE id IN ({placeholders})",
            (turn, *ids),
        )
    )


def latest_turn(db: Database, character_id: str) -> int:
    """One past whatever this character's newest memory is currently at —
    what a memory added by hand (§ POST .../memories, no chat turn of its
    own to stamp) should be given so it sorts as the newest fact rather than
    defaulting to turn 0 and reading as the oldest one there is."""
    row = db.query_one(
        "SELECT MAX(created_turn) AS latest FROM memories WHERE character_id=?", (character_id,)
    )
    latest = row["latest"] if row else None
    return (latest or 0) + 1


def list_all(db: Database, character_id: str) -> list[dict]:
    return [
        {
            "id": row["id"],
            "text": row["text"],
            "keys": json.loads(row["keys"]),
            "created_turn": row["created_turn"],
            "source": row["source"],
            "kind": row["kind"],
            "importance": row["importance"],
            "uses": row["uses"],
        }
        for row in db.query(
            "SELECT id, text, keys, created_turn, source, kind, importance, uses "
            "FROM memories WHERE character_id=? ORDER BY created_turn DESC",
            (character_id,),
        )
    ]


def get(db: Database, memory_id: str) -> dict | None:
    row = db.query_one(
        "SELECT id, text, keys, created_turn, source, kind, importance, uses "
        "FROM memories WHERE id=?", (memory_id,)
    )
    if row is None:
        return None
    return {
        "id": row["id"], "text": row["text"], "keys": json.loads(row["keys"]),
        "created_turn": row["created_turn"], "source": row["source"],
        "kind": row["kind"], "importance": row["importance"], "uses": row["uses"],
    }


def update(db: Database, memory_id: str, text: str) -> None:
    """A person overriding what the pass wrote, or fixing their own earlier
    entry — re-derives the lookup keys from the new text rather than keeping
    the old ones, since there is nothing left of the old wording to keep a
    stale key attached to."""
    text = text.strip()
    keys = derive_keys(text)
    db.write_sync(
        lambda conn: conn.execute(
            # `source` moves to 'manual' too (§41): someone who bothered to
            # correct a fact has made the judgement the compressing pass is
            # trying to approximate, and it must not overrule them later.
            "UPDATE memories SET text=?, keys=?, source='manual' WHERE id=?",
            (text, json.dumps(keys), memory_id),
        )
    )


def forget(db: Database, memory_id: str) -> None:
    db.write_sync(lambda conn: conn.execute("DELETE FROM memories WHERE id=?", (memory_id,)))


def render(memories: list[dict]) -> str:
    return "\n".join(f"- {m['text']}" for m in memories)


# -------------------------------------------------------- compression (§41)


def stats(db: Database, character_id: str) -> dict:
    """What the store looks like right now — the numbers both the tidy
    criteria and the panel read, so neither can drift from the other."""
    row = db.query_one(
        "SELECT COUNT(*) AS total, "
        "       COALESCE(SUM(uses = 0), 0) AS unused, "
        "       COALESCE(MAX(created_turn), 0) AS newest "
        "FROM memories WHERE character_id=?",
        (character_id,),
    )
    kept = db.query_one(
        "SELECT COALESCE(value, '0') AS mark FROM meta WHERE key=?",
        (f"memory_tidied:{character_id}",),
    )
    return {
        "total": int(row["total"] if row else 0),
        "unused": int(row["unused"] if row else 0),
        "since_tidy": int(row["total"] if row else 0) - int(kept["mark"] if kept else 0),
        "tidied_at_count": int(kept["mark"] if kept else 0),
    }


def needs_compression(db: Database, character_id: str) -> bool:
    """Whether this store is worth re-reading.

    Both conditions, never either: a big store nobody has added to does not
    need looking at again, and a dozen new facts in a small one is not yet a
    mess. Which is the whole point of compressing on evidence rather than on
    a timer — the expensive pass is paid for when there is something to do,
    and a quiet character never pays for it at all.
    """
    figures = stats(db, character_id)
    return figures["total"] >= COMPRESS_AT and figures["since_tidy"] >= COMPRESS_EVERY


def mark_compressed(db: Database, character_id: str) -> None:
    """Remember the size the store was left at, so `since_tidy` counts new
    facts rather than re-counting the whole store on every check."""
    total = stats(db, character_id)["total"]
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"memory_tidied:{character_id}", str(total)),
        )
    )


def for_compression(db: Database, character_id: str) -> list[dict]:
    """Every memory, oldest first, with what the compressing pass needs to
    judge it: what it is, what it was worth, and whether it has ever actually
    been used. Oldest first because supersession reads forwards — a later
    fact overriding an earlier one is the common shape, and a list in that
    order lets the model see it happen.
    """
    return [
        {
            "id": row["id"],
            "text": row["text"],
            "kind": row["kind"] or "unrated",
            "importance": row["importance"],
            "uses": row["uses"],
            "source": row["source"],
        }
        for row in db.query(
            "SELECT id, text, kind, importance, uses, source FROM memories "
            "WHERE character_id=? ORDER BY created_turn, created_at",
            (character_id,),
        )
    ]


def apply_compression(db: Database, character_id: str, plan: list[dict]) -> dict:
    """Carry out a tidy-up, with the rules the model does not get a vote on.

    Three of them, and each exists because the alternative is losing
    something that cannot be got back:

    * A memory someone wrote or edited by hand is never dropped or rewritten.
      They already made the judgement this whole mechanism is trying to
      approximate, and a model quietly overruling it would make the panel's
      edit button a suggestion.
    * A plan that would empty the store is refused outright. A reply that
      says "drop everything" is far more likely to be a confused model than
      a character with nothing worth remembering.
    * Anything the plan does not mention is kept. Silence is not consent to
      delete — an id the model forgot to list would otherwise be deleted by
      omission, which is the single worst failure mode available here.
    """
    current = {row["id"]: row for row in for_compression(db, character_id)}
    protected = {mid for mid, row in current.items() if row["source"] != "memory_pass"}

    drops: set[str] = set()
    rewrites: dict[str, str] = {}
    for entry in plan:
        if not isinstance(entry, dict):
            continue
        memory_id = str(entry.get("id") or "").strip()
        if memory_id not in current or memory_id in protected:
            continue
        action = str(entry.get("action") or "").strip().lower()
        if action == "drop":
            drops.add(memory_id)
        elif action == "merge":
            text = str(entry.get("text") or "").strip()
            # A merge with nothing to merge into is a drop wearing a hat, and
            # the model does not get to smuggle one through that way.
            if text:
                rewrites[memory_id] = text

    survivors = set(current) - drops
    if not survivors:
        return {"dropped": 0, "merged": 0, "refused": "a tidy-up may not empty the store"}

    def _apply(conn: sqlite3.Connection) -> None:
        for memory_id, text in rewrites.items():
            conn.execute(
                "UPDATE memories SET text=?, keys=? WHERE id=?",
                (text, json.dumps(derive_keys(text)), memory_id),
            )
        if drops:
            placeholders = ",".join("?" for _ in drops)
            conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", tuple(drops))

    db.write_sync(_apply)
    mark_compressed(db, character_id)
    return {"dropped": len(drops), "merged": len(rewrites), "refused": ""}
