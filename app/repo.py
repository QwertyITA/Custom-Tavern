"""Persistence helpers for characters, chats, messages and variants (§11).

Messages store raw text only — dialogue/action is render-time markup (§8) — and
every message owns a list of variants so a swipe is a branch rather than an
overwrite (§9).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .db import Database, now
from .models import Character


def new_id() -> str:
    return uuid.uuid4().hex


# ------------------------------------------------------------- characters


def save_character(db: Database, character: Character) -> Character:
    payload = json.loads(character.model_dump_json())
    timestamp = now()

    def _save(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO characters(id, name, version, data, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "version=excluded.version, data=excluded.data, updated_at=excluded.updated_at",
            (
                character.id,
                character.name,
                character.version,
                json.dumps(payload),
                timestamp,
                timestamp,
            ),
        )

    db.write_sync(_save)
    return character


def get_character(db: Database, character_id: str) -> Character | None:
    row = db.query_one("SELECT data FROM characters WHERE id=?", (character_id,))
    if row is None:
        return None
    return Character.model_validate_json(row["data"])


def list_characters(db: Database) -> list[dict]:
    """Enough to draw a character row without fetching every card in full.

    The portrait and chat count are here rather than on the client because a
    roster of ten characters would otherwise be ten more round trips, on a
    phone, before the list can be drawn at all.
    """
    counts = {
        row["character_id"]: row["n"]
        for row in db.query("SELECT character_id, COUNT(*) AS n FROM chats GROUP BY character_id")
    }
    out: list[dict] = []
    # Starred first, then by name. Not by recency: a roster that reorders
    # itself as you use it is one you have to re-read every time.
    for row in db.query(
        "SELECT id, name, version, data, favourite FROM characters "
        "ORDER BY favourite DESC, name"
    ):
        try:
            card = json.loads(row["data"])
        except (TypeError, ValueError):
            card = {}
        pfp_set = card.get("pfp_set") or {}
        out.append(
            {
                "id": row["id"],
                "name": row["name"],
                "version": row["version"],
                "pfp": pfp_set.get("neutral") or next(iter(pfp_set.values()), ""),
                "chats": counts.get(row["id"], 0),
                "favourite": bool(row["favourite"]),
            }
        )
    return out


def set_favourite(db: Database, character_id: str, favourite: bool) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE characters SET favourite=? WHERE id=?",
            (int(favourite), character_id),
        )
    )


# ----------------------------------------------------------------- personas


def list_personas(db: Database) -> list[dict]:
    return [
        dict(row)
        for row in db.query(
            "SELECT id, name, description, avatar, is_default FROM personas "
            "ORDER BY is_default DESC, name"
        )
    ]


def get_persona(db: Database, persona_id: str) -> dict | None:
    if not persona_id:
        return None
    row = db.query_one(
        "SELECT id, name, description, avatar, is_default FROM personas WHERE id=?",
        (persona_id,),
    )
    return dict(row) if row else None


def default_persona(db: Database) -> dict | None:
    row = db.query_one(
        "SELECT id, name, description, avatar, is_default FROM personas "
        "ORDER BY is_default DESC, created_at LIMIT 1"
    )
    return dict(row) if row else None


def save_persona(db: Database, persona: dict) -> dict:
    persona_id = persona.get("id") or new_id()
    timestamp = now()

    def _save(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO personas(id, name, description, avatar, is_default, created_at, "
            "updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "name=excluded.name, description=excluded.description, avatar=excluded.avatar, "
            "is_default=excluded.is_default, updated_at=excluded.updated_at",
            (
                persona_id,
                str(persona.get("name") or "").strip(),
                str(persona.get("description") or ""),
                str(persona.get("avatar") or ""),
                int(bool(persona.get("is_default"))),
                timestamp,
                timestamp,
            ),
        )
        # Exactly one default, enforced here rather than trusted to the caller:
        # two defaults means the fallback depends on row order.
        if persona.get("is_default"):
            conn.execute("UPDATE personas SET is_default=0 WHERE id != ?", (persona_id,))

    db.write_sync(_save)
    return get_persona(db, persona_id) or {}


def delete_persona(db: Database, persona_id: str) -> None:
    """Chats keep the id. A dangling one falls back the same way an unset one
    does, so deleting the persona you were using does not break old chats."""
    db.write_sync(lambda conn: conn.execute("DELETE FROM personas WHERE id=?", (persona_id,)))


def set_chat_persona(db: Database, chat_id: str, persona_id: str) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE chats SET persona_id=?, updated_at=? WHERE id=?",
            (persona_id, now(), chat_id),
        )
    )


def set_character_persona(db: Database, character_id: str, persona_id: str) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE characters SET persona_id=?, updated_at=? WHERE id=?",
            (persona_id, now(), character_id),
        )
    )


def character_persona_id(db: Database, character_id: str) -> str:
    row = db.query_one("SELECT persona_id FROM characters WHERE id=?", (character_id,))
    return (row["persona_id"] if row else "") or ""


def active_persona(db: Database, chat: dict) -> dict | None:
    """Who `{{user}}` is in this chat.

    Three places to look, most specific first: the chat's own choice, then the
    persona this character is usually played with, then the global default.
    Each falls through when it names something that no longer exists, so
    deleting a persona degrades to the default rather than to a literal
    placeholder reaching the model.
    """
    for candidate in (
        chat.get("persona_id") or "",
        character_persona_id(db, chat.get("character_id", "")),
    ):
        persona = get_persona(db, candidate)
        if persona:
            return persona
    return default_persona(db)


def delete_character(db: Database, character_id: str) -> None:
    db.write_sync(
        lambda conn: conn.execute("DELETE FROM characters WHERE id=?", (character_id,))
    )


# -------------------------------------------------------------------- chats


def create_chat(db: Database, character_id: str, title: str = "") -> dict:
    chat_id = new_id()
    timestamp = now()

    def _create(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO chats(id, character_id, title, version, settings, created_at, "
            "updated_at) VALUES(?,?,?,1,'{}',?,?)",
            (chat_id, character_id, title, timestamp, timestamp),
        )
        conn.execute(
            "INSERT INTO chat_summaries(chat_id, text, covered_turn, updated_at) "
            "VALUES(?,'',0,?)",
            (chat_id, timestamp),
        )
        # A solo chat is a group of one (roadmap 8). Seeding the member here
        # means there is never a chat with nobody in it to reply.
        conn.execute(
            "INSERT INTO chat_members(chat_id, character_id, muted, talkativeness, "
            "joined_at) VALUES(?,?,0,1.0,?)",
            (chat_id, character_id, timestamp),
        )

    db.write_sync(_create)
    return get_chat(db, chat_id)


def get_chat(db: Database, chat_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM chats WHERE id=?", (chat_id,))
    if row is None:
        return None
    chat = dict(row)
    chat["settings"] = json.loads(chat["settings"] or "{}")
    return chat


def list_chats(db: Database, character_id: str | None = None) -> list[dict]:
    if character_id:
        rows = db.query(
            "SELECT id, character_id, title, created_at, updated_at FROM chats "
            "WHERE character_id=? ORDER BY updated_at DESC",
            (character_id,),
        )
    else:
        rows = db.query(
            "SELECT id, character_id, title, created_at, updated_at FROM chats "
            "ORDER BY updated_at DESC"
        )
    return [dict(row) for row in rows]


def delete_chat(db: Database, chat_id: str) -> None:
    db.write_sync(lambda conn: conn.execute("DELETE FROM chats WHERE id=?", (chat_id,)))


def touch_chat(db: Database, chat_id: str) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE chats SET updated_at=? WHERE id=?", (now(), chat_id)
        )
    )


def update_chat_settings(db: Database, chat_id: str, settings: dict[str, Any]) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE chats SET settings=?, updated_at=? WHERE id=?",
            (json.dumps(settings), now(), chat_id),
        )
    )


# ----------------------------------------------------------------- messages


def next_turn(db: Database, chat_id: str) -> int:
    row = db.query_one("SELECT MAX(turn) AS t FROM messages WHERE chat_id=?", (chat_id,))
    return (row["t"] or 0) + 1


def add_message(
    db: Database,
    chat_id: str,
    role: str,
    text: str,
    *,
    turn: int | None = None,
    provider: str = "",
    model: str = "",
    speaker_id: str = "",
    thinking: str = "",
) -> dict:
    message_id = new_id()
    variant_id = new_id()
    timestamp = now()
    resolved_turn = turn if turn is not None else next_turn(db, chat_id)

    def _add(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO messages(id, chat_id, turn, role, active_variant, edited, stage, "
            "speaker_id, created_at) VALUES(?,?,?,?,?,0,'verbatim',?,?)",
            (message_id, chat_id, resolved_turn, role, variant_id, speaker_id, timestamp),
        )
        conn.execute(
            "INSERT INTO message_variants(id, message_id, idx, text, provider, model, "
            "thinking, created_at) VALUES(?,?,0,?,?,?,?,?)",
            (variant_id, message_id, text, provider, model, thinking, timestamp),
        )
        conn.execute("UPDATE chats SET updated_at=? WHERE id=?", (timestamp, chat_id))

    db.write_sync(_add)
    return {
        "id": message_id,
        "chat_id": chat_id,
        "turn": resolved_turn,
        "role": role,
        "text": text,
        "variant_id": variant_id,
        "variant_index": 0,
        "variant_count": 1,
        "edited": False,
        "has_thinking": bool(thinking),
        "created_at": timestamp,
    }


def add_variant(
    db: Database,
    message_id: str,
    text: str,
    *,
    provider: str = "",
    model: str = "",
    thinking: str = "",
) -> dict:
    """Add a swipe variant and make it active."""
    variant_id = new_id()
    timestamp = now()

    def _add(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(idx), -1) + 1 AS idx FROM message_variants WHERE message_id=?",
            (message_id,),
        ).fetchone()
        index = row["idx"]
        conn.execute(
            "INSERT INTO message_variants(id, message_id, idx, text, provider, model, "
            "thinking, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (variant_id, message_id, index, text, provider, model, thinking, timestamp),
        )
        conn.execute(
            "UPDATE messages SET active_variant=? WHERE id=?", (variant_id, message_id)
        )
        return index

    index = db.write_sync(_add)
    return {"id": variant_id, "idx": index, "text": text, "has_thinking": bool(thinking)}


def set_active_variant(db: Database, message_id: str, variant_id: str) -> bool:
    row = db.query_one(
        "SELECT id FROM message_variants WHERE id=? AND message_id=?",
        (variant_id, message_id),
    )
    if row is None:
        return False
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE messages SET active_variant=? WHERE id=?", (variant_id, message_id)
        )
    )
    return True


def update_variant_text(db: Database, variant_id: str, text: str, *, edited: bool = True) -> None:
    def _update(conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE message_variants SET text=? WHERE id=?", (text, variant_id))
        if edited:
            conn.execute(
                "UPDATE messages SET edited=1 WHERE active_variant=?", (variant_id,)
            )

    db.write_sync(_update)


def set_message_hidden(db: Database, message_id: str, hidden: bool) -> None:
    """Keep it on screen, take it out of the prompt."""
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE messages SET hidden=? WHERE id=?", (int(hidden), message_id)
        )
    )


def get_message(db: Database, message_id: str) -> dict | None:
    row = db.query_one(
        "SELECT m.*, v.text AS text, v.translation AS translation, v.idx AS variant_index, "
        "(LENGTH(COALESCE(v.thinking, '')) > 0) AS has_thinking "
        "FROM messages m LEFT JOIN message_variants v ON v.id = m.active_variant "
        "WHERE m.id=?",
        (message_id,),
    )
    if row is None:
        return None
    message = dict(row)
    message["text"] = message["text"] or ""
    message["edited"] = bool(message["edited"])
    message["has_thinking"] = bool(message["has_thinking"])
    message["variant_id"] = message.pop("active_variant")
    count = db.query_one(
        "SELECT COUNT(*) AS c FROM message_variants WHERE message_id=?", (message_id,)
    )
    message["variant_count"] = count["c"] if count else 1
    return message


def list_variants(db: Database, message_id: str) -> list[dict]:
    return [
        dict(row)
        for row in db.query(
            "SELECT id, idx, text, provider, model FROM message_variants "
            "WHERE message_id=? ORDER BY idx",
            (message_id,),
        )
    ]


def list_messages(db: Database, chat_id: str, include_dropped: bool = True) -> list[dict]:
    sql = (
        "SELECT m.id, m.turn, m.role, m.edited, m.stage, m.hidden, m.speaker_id, "
        "m.created_at, m.active_variant, "
        "v.text AS text, v.translation AS translation, v.idx AS variant_index, "
        "(LENGTH(COALESCE(v.thinking, '')) > 0) AS has_thinking, "
        "(SELECT COUNT(*) FROM message_variants mv WHERE mv.message_id = m.id) AS variant_count "
        "FROM messages m LEFT JOIN message_variants v ON v.id = m.active_variant "
        "WHERE m.chat_id=?"
    )
    if not include_dropped:
        sql += " AND m.stage != 'dropped'"
    sql += " ORDER BY m.turn, m.created_at"
    out = []
    for row in db.query(sql, (chat_id,)):
        message = dict(row)
        message["text"] = message["text"] or ""
        message["edited"] = bool(message["edited"])
        message["has_thinking"] = bool(message["has_thinking"])
        message["variant_id"] = message.pop("active_variant")
        out.append(message)
    return out


def delete_message(db: Database, message_id: str) -> None:
    db.write_sync(lambda conn: conn.execute("DELETE FROM messages WHERE id=?", (message_id,)))


# ---------------------------------------------------------------- summaries


def get_summary(db: Database, chat_id: str) -> dict:
    row = db.query_one(
        "SELECT text, covered_turn FROM chat_summaries WHERE chat_id=?", (chat_id,)
    )
    if row is None:
        return {"text": "", "covered_turn": 0}
    return {"text": row["text"], "covered_turn": row["covered_turn"]}


def set_summary(db: Database, chat_id: str, text: str, covered_turn: int) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO chat_summaries(chat_id, text, covered_turn, updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET text=excluded.text, "
            "covered_turn=excluded.covered_turn, updated_at=excluded.updated_at",
            (chat_id, text, covered_turn, now()),
        )
    )


# --------------------------------------------------------------------- meta


def get_meta(db: Database, key: str, default: str = "") -> str:
    row = db.query_one("SELECT value FROM meta WHERE key=?", (key,))
    return row["value"] if row else default


def set_meta(db: Database, key: str, value: str) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    )


# ------------------------------------------------------- itemised prompts

# How many turns of a chat keep the full breakdown of what was sent. The
# itemisation is roughly the size of the prompt, so keeping every turn's would
# grow the database with the square of the conversation — on a phone, on the
# one file the user cannot afford to lose. Recent turns are the ones anyone
# asks about; older messages say plainly that theirs was not kept.
PROMPT_HISTORY_TURNS = 20


def save_prompt_record(db: Database, run_id: str, chat_id: str, parts: list[dict]) -> None:
    """Store the itemisation for one run, then prune the old ones."""
    payload = json.dumps(parts, ensure_ascii=False)

    def _write(conn) -> None:
        conn.execute("UPDATE pass_runs SET prompt=? WHERE id=?", (payload, run_id))
        conn.execute(
            "UPDATE pass_runs SET prompt=NULL WHERE chat_id=? AND prompt IS NOT NULL "
            "AND turn <= (SELECT MAX(turn) FROM pass_runs WHERE chat_id=?) - ?",
            (chat_id, chat_id, PROMPT_HISTORY_TURNS),
        )

    db.write_sync(_write)


def thinking_for(db: Database, message_id: str) -> dict | None:
    """What the model thought before writing the variant now on screen (§5.6).

    Per variant, like the prompt record and for the same reason: a re-roll
    thought its own way to its own answer, and showing the first attempt's
    reasoning under the third attempt's text would be worse than showing
    nothing.

    Fetched on demand rather than sent with every message — reasoning routinely
    runs longer than the reply it produced, and a transcript that carries all of
    it is one that costs several times what it shows.
    """
    row = db.query_one(
        "SELECT v.thinking, v.model, v.provider, v.created_at FROM messages m "
        "JOIN message_variants v ON v.id = m.active_variant WHERE m.id=?",
        (message_id,),
    )
    if row is None:
        return None
    return {
        "thinking": row["thinking"] or "",
        "model": row["model"] or "",
        "provider": row["provider"] or "",
        "created_at": row["created_at"],
    }


def prompt_record(db: Database, message_id: str) -> dict | None:
    """What was sent for the reply shown in this message.

    Matched on the variant rather than the turn: a re-roll is a different
    prompt, assembled after whatever the previous attempt changed, and showing
    the first attempt's breakdown next to the third attempt's text would be
    worse than showing nothing (§9).
    """
    message = get_message(db, message_id)
    if message is None:
        return None
    # `prompt IS NOT NULL` is the filter that identifies a reply run, rather
    # than a name: `kind` is the pass definition's kind (canonical/custom) and
    # `pass_id` is configurable, but only the pass that assembles a full reply
    # ever stores an itemisation.
    row = db.query_one(
        "SELECT prompt, model, tier, tokens_in, started_at FROM pass_runs "
        "WHERE chat_id=? AND turn=? AND prompt IS NOT NULL "
        "AND (variant_id=? OR variant_id IS NULL) "
        "ORDER BY (variant_id IS NULL), started_at DESC, rowid DESC LIMIT 1",
        (message["chat_id"], message["turn"], message["variant_id"]),
    )
    if row is None or not row["prompt"]:
        return None
    try:
        parts = json.loads(row["prompt"])
    except json.JSONDecodeError:
        return None
    return {
        "parts": parts,
        "model": row["model"],
        "tier": row["tier"],
        "tokens_in": row["tokens_in"],
        "sent_at": row["started_at"],
    }


# ---------------------------------------------------------- chat management


def rename_chat(db: Database, chat_id: str, title: str) -> None:
    """Rename without touching `updated_at`: the chat list is ordered by when
    the story last moved, and renaming one is not the story moving."""
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE chats SET title=? WHERE id=?", (title.strip(), chat_id)
        )
    )


def search_chats(db: Database, query: str, limit: int = 40) -> list[dict]:
    """Chats whose title or messages contain `query`, most recent first.

    LIKE rather than FTS: a full-text index is a second copy of every message
    in a database that lives on a phone, and the honest reach of this feature
    is "find that chat where they mentioned the lighthouse" over a few hundred
    conversations, which LIKE does in milliseconds.

    The matching message comes back with the row, because a list of chat titles
    is not an answer to a search for a phrase.
    """
    if not query.strip():
        return []
    # `%` and `_` are LIKE's own wildcards, so a query containing one would
    # match far more than the person typing it meant — `%` alone would return
    # every chat they have.
    escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    needle = f"%{escaped}%"
    rows = db.query(
        "SELECT c.id, c.character_id, c.title, c.created_at, c.updated_at, "
        "       ch.name AS character_name, "
        "       (SELECT v.text FROM messages m "
        "          JOIN message_variants v ON v.id = m.active_variant "
        "         WHERE m.chat_id = c.id AND v.text LIKE :needle ESCAPE '\\' "
        "         ORDER BY m.turn DESC LIMIT 1) AS hit "
        "  FROM chats c "
        "  LEFT JOIN characters ch ON ch.id = c.character_id "
        " WHERE c.title LIKE :needle ESCAPE '\\' OR EXISTS ("
        "         SELECT 1 FROM messages m "
        "           JOIN message_variants v ON v.id = m.active_variant "
        "          WHERE m.chat_id = c.id AND v.text LIKE :needle ESCAPE '\\') "
        " ORDER BY c.updated_at DESC LIMIT :limit",
        {"needle": needle, "limit": limit},
    )
    return [dict(row) for row in rows]
