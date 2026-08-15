"""Files attached to a message (§19).

Two kinds, and they behave differently on purpose:

**Text** is read once, at upload, and stored as text on the attachment row. It
then travels into the prompt with the message it is attached to, like anything
else the person said. The file itself is not kept — the text *is* the file, and
keeping a second copy on a phone to re-read later would be storage spent on
nothing.

**Images** are stored as files and shown in the bubble. They reach the model
only when the backend can actually see them; on one that cannot, the message
still carries a note that a picture is there, because a reply that ignores an
image the person clearly meant something by is worse than one that says it
cannot see it.

Nothing is decoded, resized or re-encoded. Pillow is a compiled dependency and
this has to install in Termux, so an image is stored exactly as it arrived and
sent as base64 when a backend wants one.
"""

from __future__ import annotations

import base64
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .config import DATA_DIR
from .db import Database, now

ATTACHMENT_DIR = DATA_DIR / "attachments"

# Kept small deliberately. This is a phone's storage, and an image large enough
# to matter is also large enough to make a vision request slow and expensive.
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 512 * 1024
# How much of a text file reaches the prompt. A dropped-in document can be
# enormous, and silently spending the whole context on it is worse than saying
# how much was used.
MAX_TEXT_CHARS = 8_000

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".json", ".csv", ".log", ".yaml", ".yml"}

MIME_BY_SUFFIX = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}


class AttachmentError(ValueError):
    """The file cannot be attached, with a reason worth showing."""


def kind_for(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in TEXT_SUFFIXES:
        return "text"
    raise AttachmentError(
        f"{suffix or 'that'} is not a kind of file this can attach — "
        "images (png, jpg, webp, gif) or text (txt, md, json, csv, log, yaml)"
    )


def _safe_name(filename: str) -> str:
    """The name as shown, never as a path. Only ever displayed."""
    stem = Path(filename).name
    return "".join(c for c in stem if c.isalnum() or c in " .-_")[:80] or "file"


# How long a staged attachment survives without being sent. Someone picks a
# file, changes their mind, and closes the app; without this the image sits on
# their phone forever.
STAGED_TTL_SECONDS = 60 * 60


def store(
    db: Database, message_id: str | None, data: bytes, filename: str
) -> dict[str, Any]:
    """Save one attachment and return its row.

    `message_id` is None while staged — the file is uploaded before the message
    it belongs to exists, and the turn claims it once there is one.
    """
    if not data:
        raise AttachmentError("that file is empty")
    kind = kind_for(filename)
    name = _safe_name(filename)
    suffix = Path(name).suffix.lower()

    text = ""
    stored_as = ""
    if kind == "text":
        if len(data) > MAX_TEXT_BYTES:
            raise AttachmentError(
                f"that text file is {len(data) // 1024}KB; the limit is "
                f"{MAX_TEXT_BYTES // 1024}KB"
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AttachmentError("that file is not readable as UTF-8 text") from exc
        text = text[:MAX_TEXT_CHARS]
    else:
        if len(data) > MAX_IMAGE_BYTES:
            raise AttachmentError(
                f"that image is {len(data) // (1024 * 1024)}MB; the limit is "
                f"{MAX_IMAGE_BYTES // (1024 * 1024)}MB"
            )
        ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
        stored_as = f"{uuid.uuid4().hex}{suffix}"
        (ATTACHMENT_DIR / stored_as).write_bytes(data)

    attachment_id = uuid.uuid4().hex
    timestamp = now()

    def _insert(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO attachments(id, message_id, kind, name, stored_as, mime, "
            "size, text, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (attachment_id, message_id or None, kind, name, stored_as,
             MIME_BY_SUFFIX.get(suffix, "text/plain"), len(data), text, timestamp),
        )

    db.write_sync(_insert)
    return get(db, attachment_id)


def claim(db: Database, ids: list[str], message_id: str) -> list[dict]:
    """Bind staged attachments to the message that has just been created.

    Only ones still unclaimed: an id that already belongs to a message is not
    re-pointed, so a replayed request cannot move someone's picture onto a
    different turn.
    """
    wanted = [i for i in ids if i]
    if not wanted:
        return []
    placeholders = ",".join("?" for _ in wanted)

    def _bind(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"UPDATE attachments SET message_id=? WHERE message_id IS NULL "
            f"AND id IN ({placeholders})",
            (message_id, *wanted),
        )

    db.write_sync(_bind)
    return for_message(db, message_id)


def clear_stale_staged(db: Database) -> int:
    """Drop staged attachments nobody ever sent."""
    cutoff = now() - STAGED_TTL_SECONDS
    rows = db.query(
        "SELECT id FROM attachments WHERE message_id IS NULL AND created_at < ?", (cutoff,)
    )
    for row in rows:
        delete(db, row["id"])
    return len(rows)


def get(db: Database, attachment_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM attachments WHERE id=?", (attachment_id,))
    return _row(row) if row else None


def for_message(db: Database, message_id: str) -> list[dict]:
    return [
        _row(row)
        for row in db.query(
            "SELECT * FROM attachments WHERE message_id=? ORDER BY created_at, rowid",
            (message_id,),
        )
    ]


def for_chat(db: Database, chat_id: str) -> dict[str, list[dict]]:
    """Every attachment in a chat, grouped by message.

    One query rather than one per message: a chat with fifty messages would
    otherwise be fifty round trips before the transcript can be drawn.
    """
    rows = db.query(
        "SELECT a.* FROM attachments a JOIN messages m ON m.id = a.message_id "
        "WHERE m.chat_id=? ORDER BY a.created_at, a.rowid",
        (chat_id,),
    )
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row["message_id"], []).append(_row(row))
    return out


def _row(row) -> dict:
    body = dict(row)
    # The stored filename never leaves the server: it is an implementation
    # detail, and the id is what the URL is built from.
    body.pop("stored_as", None)
    return body


def path_for(db: Database, attachment_id: str) -> Path | None:
    row = db.query_one(
        "SELECT stored_as FROM attachments WHERE id=? AND kind='image'", (attachment_id,)
    )
    if row is None or not row["stored_as"]:
        return None
    path = ATTACHMENT_DIR / row["stored_as"]
    return path if path.is_file() else None


def delete(db: Database, attachment_id: str) -> bool:
    row = db.query_one("SELECT stored_as FROM attachments WHERE id=?", (attachment_id,))
    if row is None:
        return False
    if row["stored_as"]:
        (ATTACHMENT_DIR / row["stored_as"]).unlink(missing_ok=True)
    db.write_sync(
        lambda conn: conn.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
    )
    return True


def delete_orphans(db: Database) -> int:
    """Remove image files with no row left pointing at them.

    Messages cascade-delete their attachment rows, which leaves the files
    behind — on a phone, silently, forever. Called after a message or chat is
    deleted rather than on a timer, so the tidying happens when the mess does.
    """
    if not ATTACHMENT_DIR.is_dir():
        return 0
    known = {
        row["stored_as"]
        for row in db.query("SELECT stored_as FROM attachments WHERE stored_as != ''")
    }
    removed = 0
    for file in ATTACHMENT_DIR.iterdir():
        if file.is_file() and file.name not in known:
            file.unlink(missing_ok=True)
            removed += 1
    return removed


# ----------------------------------------------------------------- prompting


def prompt_suffix(items: list[dict], can_see_images: bool) -> str:
    """What a message's attachments add to its text in the prompt.

    Text files are quoted in full. Images are named either way: on a backend
    that can see them the name is context for the picture it is being sent, and
    on one that cannot it is the difference between a reply that acknowledges
    the picture and one that reads as if nothing was sent.
    """
    parts: list[str] = []
    for item in items:
        if item["kind"] == "text":
            parts.append(f"[Attached file: {item['name']}]\n{item['text']}")
        elif can_see_images:
            parts.append(f"[Attached image: {item['name']}]")
        else:
            parts.append(
                f"[Attached image: {item['name']} — you cannot see images, "
                "so say so rather than guessing what it shows]"
            )
    return "\n\n".join(parts)


def images_for(db: Database, items: list[dict]) -> list[str]:
    """Base64 payloads for the images among these attachments.

    Encoded on demand rather than stored encoded: base64 is a third larger than
    the bytes, and most turns never send an image at all.
    """
    out: list[str] = []
    for item in items:
        if item["kind"] != "image":
            continue
        path = path_for(db, item["id"])
        if path is not None:
            out.append(base64.b64encode(path.read_bytes()).decode("ascii"))
    return out
