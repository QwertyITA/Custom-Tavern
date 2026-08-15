"""Exporting and importing a single chat (§10).

A chat is worth more than the app is: the messages are the thing someone spent
their evenings on, and they should be able to get them out and put them back.
So the format is plain JSON with every field named, readable in a text editor,
and an import that refuses rather than guesses when something does not fit.

**What travels and what does not.** Messages, their swipe variants, the rolling
summary and the state slices all travel — they are the chat. The character does
not: it has its own export (a TavernCard), it is usually shared between several
chats, and copying it into every one of them would mean an imported chat could
silently fork someone's character. An import binds to a character that is
already here, and says plainly when it cannot find one.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import repo, state as state_mod
from .db import Database, now

FORMAT = "personal-tavern-chat"
VERSION = 1


class ChatFileError(ValueError):
    """The file is not a chat we can read, with a reason worth showing."""


def export_chat(db: Database, chat_id: str) -> dict[str, Any]:
    chat = repo.get_chat(db, chat_id)
    if chat is None:
        raise ChatFileError("no such chat")
    character = repo.get_character(db, chat["character_id"])

    messages = []
    for message in repo.list_messages(db, chat_id, include_dropped=True):
        variants = db.query(
            "SELECT id, text, idx, provider, model, created_at FROM message_variants "
            "WHERE message_id=? ORDER BY idx",
            (message["id"],),
        )
        messages.append({
            "id": message["id"],
            "turn": message["turn"],
            "role": message["role"],
            "stage": message["stage"],
            "hidden": bool(message["hidden"]),
            "edited": bool(message["edited"]),
            "created_at": message["created_at"],
            # list_messages hands this back under its own name.
            "active_variant": message["variant_id"],
            "variants": [dict(v) for v in variants],
        })

    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": now(),
        "chat": {
            "title": chat["title"],
            "created_at": chat["created_at"],
            "updated_at": chat["updated_at"],
            "settings": chat["settings"],
            "persona_id": chat.get("persona_id") or "",
        },
        # Enough to find the character again, and to say whose chat this was if
        # it cannot be found. Not the character itself — see the module note.
        "character": {
            "id": chat["character_id"],
            "name": character.name if character else "",
        },
        "messages": messages,
        "summary": repo.get_summary(db, chat_id),
        "state": state_mod.read_all_slices(db, chat_id),
    }


def _character_for(db: Database, payload: dict, override: str = "") -> str:
    """Which character an imported chat binds to.

    In order: an explicit choice, the same id if it is still here, then the
    same name. A chat with nobody in it is not a chat, so failing to resolve
    one is an error rather than a blank.
    """
    if override:
        if repo.get_character(db, override) is None:
            raise ChatFileError(f"no character with id {override!r}")
        return override

    wanted = payload.get("character") or {}
    by_id = str(wanted.get("id") or "")
    if by_id and repo.get_character(db, by_id) is not None:
        return by_id

    name = str(wanted.get("name") or "").strip()
    if name:
        for character in repo.list_characters(db):
            if character["name"].strip().lower() == name.lower():
                return character["id"]

    raise ChatFileError(
        f"this chat belongs to {name or 'a character'} who is not here yet — "
        "import the character card first, or pick who it belongs to"
    )


def import_chat(db: Database, payload: Any, *, character_id: str = "") -> dict:
    """Restore a chat from an export. Returns the new chat row."""
    if not isinstance(payload, dict):
        raise ChatFileError("that file is not a chat export")
    if payload.get("format") != FORMAT:
        raise ChatFileError("that file is not a Personal Tavern chat export")
    if int(payload.get("version") or 0) > VERSION:
        raise ChatFileError(
            "this export came from a newer version of the app than this one"
        )

    owner = _character_for(db, payload, character_id)
    details = payload.get("chat") or {}
    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        raise ChatFileError("the messages in that file are not a list")

    # A new id, always. Importing an export of a chat that is still here should
    # give you a second copy, not silently overwrite the first.
    chat = repo.create_chat(db, owner, str(details.get("title") or "Imported chat"))
    chat_id = chat["id"]

    def _restore(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE chats SET settings=?, persona_id=? WHERE id=?",
            (json.dumps(details.get("settings") or {}),
             str(details.get("persona_id") or ""), chat_id),
        )
        # Ids are regenerated so an import can never collide with a message
        # that is already here; the mapping keeps active_variant pointing at
        # the right one afterwards.
        for index, raw in enumerate(messages):
            if not isinstance(raw, dict):
                continue
            message_id = repo.new_id()
            variants = raw.get("variants") or []
            active_source = raw.get("active_variant")
            active_id = ""
            conn.execute(
                "INSERT INTO messages(id, chat_id, turn, role, stage, hidden, edited, "
                "created_at, active_variant) VALUES(?,?,?,?,?,?,?,?,'')",
                (message_id, chat_id, int(raw.get("turn") or 0),
                 str(raw.get("role") or "user"), str(raw.get("stage") or "verbatim"),
                 int(bool(raw.get("hidden"))), int(bool(raw.get("edited"))),
                 float(raw.get("created_at") or now())),
            )
            for order, variant in enumerate(variants):
                if not isinstance(variant, dict):
                    continue
                variant_id = repo.new_id()
                if variant.get("id") == active_source or (not active_id and order == 0):
                    active_id = variant_id
                conn.execute(
                    "INSERT INTO message_variants(id, message_id, idx, text, provider, "
                    "model, created_at) VALUES(?,?,?,?,?,?,?)",
                    (variant_id, message_id, order, str(variant.get("text") or ""),
                     str(variant.get("provider") or ""), str(variant.get("model") or ""),
                     float(variant.get("created_at") or now())),
                )
            if not active_id:
                # A message with no variants has no text; skip it rather than
                # leaving a bubble that renders as nothing.
                conn.execute("DELETE FROM messages WHERE id=?", (message_id,))
                continue
            conn.execute(
                "UPDATE messages SET active_variant=? WHERE id=?", (active_id, message_id)
            )

        summary = payload.get("summary") or {}
        conn.execute(
            "UPDATE chat_summaries SET text=?, covered_turn=?, updated_at=? WHERE chat_id=?",
            (str(summary.get("text") or ""), int(summary.get("covered_turn") or 0),
             now(), chat_id),
        )

        for name, slice_body in (payload.get("state") or {}).items():
            if not isinstance(slice_body, dict):
                continue
            conn.execute(
                "INSERT INTO state_slices(chat_id, slice_name, value, source_turn, "
                "source_pass, provisional, updated_at) VALUES(?,?,?,?,?,?,?)",
                (chat_id, str(name), json.dumps(slice_body.get("value")),
                 int(slice_body.get("source_turn") or 0),
                 str(slice_body.get("source_pass") or "import"),
                 int(bool(slice_body.get("provisional"))), now()),
            )

    db.write_sync(_restore)
    return repo.get_chat(db, chat_id)


def filename_for(chat: dict, character_name: str = "") -> str:
    """A name someone can find again in a downloads folder."""
    parts = [character_name.strip(), (chat.get("title") or "").strip()]
    stem = " - ".join(p for p in parts if p) or "chat"
    safe = "".join(c if c.isalnum() or c in " -_" else "-" for c in stem).strip()
    return f"{(safe or 'chat')[:60]}.json"
