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
                # Every list that draws a face needs the shape it is drawn in,
                # or the roster frames a standing figure as a square while the
                # conversation beside it does not.
                "pfp_shape": "square" if card.get("pfp_shape") == "square" else "portrait",
                # Same idea, for the colour treatment: a glow chosen for one
                # character is wrong on the next, so the roster has to know it
                # too rather than drawing every face plain.
                "pfp_effect": card.get("pfp_effect") if isinstance(card.get("pfp_effect"), dict) else {},
                # Whether there is anything beyond neutral worth bundling
                # into a PNG export (§ export_character_png, main.py) — the
                # roster's own export link picks its URL from this rather
                # than the card fetching its own full pfp_set just to ask
                # the same one-bit question.
                "has_expressions": len(pfp_set) > 1,
                # So starring/unstarring can show the character's own line
                # instead of a generic toast, without a second round trip.
                "reactions": card.get("reactions") if isinstance(card.get("reactions"), dict) else {},
                "chats": counts.get(row["id"], 0),
                "favourite": bool(row["favourite"]),
                "vaulted": bool(card.get("vaulted")),
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


def avatar_still_wanted(db: Database, filename: str) -> bool:
    """Whether anything left still points at this file in data/avatars/.

    The directory is shared: character portraits and persona pictures upload
    into the same place through the same endpoint, so a filename outliving
    the character it was cropped for is not proof nothing wants it — only
    finding it nowhere at all is. Checked *after* the character row is gone,
    so the character being deleted never counts as a reason to keep its own
    picture.
    """
    needle = f"/avatars/{filename}"
    for row in db.query("SELECT data FROM characters"):
        try:
            card = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        if needle in (card.get("pfp_set") or {}).values():
            return True
    return db.query_one("SELECT 1 FROM personas WHERE avatar=?", (filename,)) is not None


def avatar_idle_still_wanted(db: Database, filename: str) -> bool:
    """Whether any character still points at this file in data/avatar_idle/.

    Same reasoning as avatar_still_wanted above, checked after the character
    row is gone so the character being deleted never counts as a reason to
    keep its own idle loop."""
    needle = f"/avatar_idle/{filename}"
    for row in db.query("SELECT data FROM characters"):
        try:
            card = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        if needle == (card.get("avatar_video") or {}).get("idle_video"):
            return True
    return False


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
    full_text: str = "",
    draft_text: str = "",
    echoes_user: str = "",
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
            "thinking, full_text, draft_text, echoes_user, created_at) VALUES(?,?,0,?,?,?,?,?,?,?,?)",
            (variant_id, message_id, text, provider, model, thinking, full_text, draft_text,
             echoes_user, timestamp),
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
        "has_full_text": bool(full_text),
        "has_draft_text": bool(draft_text),
        "echoes_user": echoes_user,
        "user_reaction": "",
        "reaction_ack": "",
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
    full_text: str = "",
    draft_text: str = "",
    echoes_user: str = "",
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
            "thinking, full_text, draft_text, echoes_user, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (variant_id, message_id, index, text, provider, model, thinking, full_text, draft_text,
             echoes_user, timestamp),
        )
        conn.execute(
            "UPDATE messages SET active_variant=? WHERE id=?", (variant_id, message_id)
        )
        return index

    index = db.write_sync(_add)
    return {
        "id": variant_id, "idx": index, "text": text,
        "has_thinking": bool(thinking), "has_full_text": bool(full_text),
        "has_draft_text": bool(draft_text), "echoes_user": echoes_user,
        "user_reaction": "", "reaction_ack": "",
    }


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


def restore_full_text(db: Database, variant_id: str) -> str | None:
    """Undo a paragraph cut (§ reply_length.cut): the variant's `text` becomes
    what `full_text` held, and `full_text` clears — restoring is a one-way
    trip back to what the model actually wrote, not a toggle, so there is
    nothing left to cut back down to afterward. Returns the restored text, or
    None when this variant was never cut (nothing for the caller to do).
    """
    row = db.query_one(
        "SELECT full_text FROM message_variants WHERE id=?", (variant_id,)
    )
    if row is None or not row["full_text"]:
        return None
    text = row["full_text"]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE message_variants SET text=?, full_text='' WHERE id=?",
            (text, variant_id),
        )
    )
    return text


def restore_draft_text(db: Database, variant_id: str) -> str | None:
    """Undo post_process's own edit (§ app/reply_polish.py): the variant's
    `text` becomes what `draft_text` held — the model's own first draft,
    before the copy-edit — and `draft_text` clears. Independent of
    `restore_full_text` above: a reply post_process rewrote and the length
    backstop then also cut has both set, and each restores its own step.
    Returns the restored text, or None when post_process never touched this
    variant.
    """
    row = db.query_one(
        "SELECT draft_text FROM message_variants WHERE id=?", (variant_id,)
    )
    if row is None or not row["draft_text"]:
        return None
    text = row["draft_text"]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE message_variants SET text=?, draft_text='' WHERE id=?",
            (text, variant_id),
        )
    )
    return text


def set_message_hidden(db: Database, message_id: str, hidden: bool) -> None:
    """Keep it on screen, take it out of the prompt."""
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE messages SET hidden=? WHERE id=?", (int(hidden), message_id)
        )
    )


def set_reaction(db: Database, variant_id: str, emoji: str) -> None:
    """The emoji someone reacted with, or '' to clear it. Same shape as
    translation.set_translation — one column, one variant, no arbitration
    needed since nothing else ever writes this one."""
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE message_variants SET user_reaction=? WHERE id=?", (emoji, variant_id)
        )
    )


def set_reaction_ack(db: Database, variant_id: str, text: str) -> None:
    """The character's own line acknowledging a reaction (§ message_reaction
    pass, scheduler.py) — generated once and cached, same as translation."""
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE message_variants SET reaction_ack=? WHERE id=?", (text, variant_id)
        )
    )


def get_message(db: Database, message_id: str) -> dict | None:
    row = db.query_one(
        "SELECT m.*, v.text AS text, v.translation AS translation, v.idx AS variant_index, "
        "(LENGTH(COALESCE(v.thinking, '')) > 0) AS has_thinking, "
        "(LENGTH(COALESCE(v.full_text, '')) > 0) AS has_full_text, "
        "(LENGTH(COALESCE(v.draft_text, '')) > 0) AS has_draft_text, "
        "COALESCE(v.echoes_user, '') AS echoes_user, "
        "COALESCE(v.user_reaction, '') AS user_reaction, "
        "COALESCE(v.reaction_ack, '') AS reaction_ack "
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
    message["has_full_text"] = bool(message["has_full_text"])
    message["has_draft_text"] = bool(message["has_draft_text"])
    message["echoes_user"] = message["echoes_user"] or ""
    message["user_reaction"] = message["user_reaction"] or ""
    message["reaction_ack"] = message["reaction_ack"] or ""
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
            "SELECT id, idx, text, provider, model, echoes_user, "
            "COALESCE(user_reaction, '') AS user_reaction, "
            "COALESCE(reaction_ack, '') AS reaction_ack "
            "FROM message_variants WHERE message_id=? ORDER BY idx",
            (message_id,),
        )
    ]


def list_messages(db: Database, chat_id: str, include_dropped: bool = True) -> list[dict]:
    sql = (
        "SELECT m.id, m.turn, m.role, m.edited, m.stage, m.hidden, m.speaker_id, "
        "m.created_at, m.active_variant, "
        "v.text AS text, v.translation AS translation, v.idx AS variant_index, "
        "(LENGTH(COALESCE(v.thinking, '')) > 0) AS has_thinking, "
        "(LENGTH(COALESCE(v.full_text, '')) > 0) AS has_full_text, "
        "(LENGTH(COALESCE(v.draft_text, '')) > 0) AS has_draft_text, "
        "COALESCE(v.echoes_user, '') AS echoes_user, "
        "COALESCE(v.user_reaction, '') AS user_reaction, "
        "COALESCE(v.reaction_ack, '') AS reaction_ack, "
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
        message["has_full_text"] = bool(message["has_full_text"])
        message["has_draft_text"] = bool(message["has_draft_text"])
        message["echoes_user"] = message["echoes_user"] or ""
        message["user_reaction"] = message["user_reaction"] or ""
        message["reaction_ack"] = message["reaction_ack"] or ""
        message["variant_id"] = message.pop("active_variant")
        out.append(message)
    return out


def get_user_message_for_turn(db: Database, chat_id: str, turn: int) -> dict | None:
    """The user message a given turn's reply(ies) are answering — for the
    echoed-phrase check on a swipe (§ find_echoed_phrase, ISSUES-TRIAGE.md
    #15), which regenerates the reply to a turn but is not itself hand the
    user's text the way a first attempt already is (§ scheduler._answer).
    """
    row = db.query_one(
        "SELECT m.id, m.chat_id, m.turn, m.role, v.text AS text "
        "FROM messages m JOIN message_variants v ON v.id = m.active_variant "
        "WHERE m.chat_id=? AND m.turn=? AND m.role='user' LIMIT 1",
        (chat_id, turn),
    )
    return dict(row) if row else None


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


def save_prompt_record(
    db: Database, run_id: str, chat_id: str, parts: list[dict], budget: int | None = None
) -> None:
    """Store the itemisation for one run, then prune the old ones.

    `budget` is the fitted prompt ceiling assembly aimed for this run (§ §7.1
    fit_token_budget) — what "What was sent" needs to draw usage against a
    context meter rather than just listing sections with nothing to measure
    them against. Optional and stored alongside the parts rather than as a
    second column: a record with no budget (an older row, or a backend that
    could not report one) still reads fine, just without the meter.
    """
    payload = json.dumps({"parts": parts, "budget": budget}, ensure_ascii=False)

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
        stored = json.loads(row["prompt"])
    except json.JSONDecodeError:
        return None
    # Two shapes on disk: a bare list is a record saved before the budget was
    # tracked at all, still fully readable, just with nothing to draw a meter
    # against; the dict shape is everything written since.
    if isinstance(stored, list):
        parts, budget = stored, None
    else:
        parts, budget = stored.get("parts", []), stored.get("budget")
    return {
        "parts": parts,
        "budget": budget,
        "model": row["model"],
        "tier": row["tier"],
        "tokens_in": row["tokens_in"],
        "sent_at": row["started_at"],
    }


# ---------------------------------------------------------- chat management


def rename_chat(db: Database, chat_id: str, title: str, *, manual: bool | None = None) -> None:
    """Rename without touching `updated_at`: the chat list is ordered by when
    the story last moved, and renaming one is not the story moving.

    `manual`, when given, also sets `title_manual` — the flag that stops the
    chat_rename pass (§ scheduler.py's _maybe_rename_chat) ever touching this
    chat's title again once someone has named it themselves. Left `None` for
    that pass's own write here: an auto title must never look like a manual
    one, or it would disable itself the moment it did its job.

    Clearing the title by hand (`manual=False`) also resets
    `title_auto_count` to 0 — worth naming again from scratch, not just
    eligible again at whatever count the last auto attempt already used.
    """
    def _update(conn: sqlite3.Connection) -> None:
        clean = title.strip()
        if manual is None:
            conn.execute("UPDATE chats SET title=? WHERE id=?", (clean, chat_id))
        elif manual:
            conn.execute(
                "UPDATE chats SET title=?, title_manual=1 WHERE id=?", (clean, chat_id)
            )
        else:
            conn.execute(
                "UPDATE chats SET title=?, title_manual=0, title_auto_count=0 WHERE id=?",
                (clean, chat_id),
            )

    db.write_sync(_update)


def mark_title_auto_attempt(db: Database, chat_id: str, count: int) -> None:
    """Record that chat_rename has now tried this chat at this message count
    (§ scheduler.py's _maybe_rename_chat) — set before the attempt runs, not
    after, so a swipe on the same milestone message sees the count already
    marked and does not fire a second attempt for it, win or lose."""
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE chats SET title_auto_count=? WHERE id=?", (count, chat_id)
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
