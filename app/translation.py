"""Translation, in both directions (roadmap 23).

One setting each way: **they write in** X, **you read in** Y. When the two
differ, every turn crosses the gap twice — your message is put into their
language before it reaches the model, and their reply is put into yours before
it reaches the screen.

The original is never overwritten. `text` on a variant is always what was
actually written; `translation` is the same thing in the other language. That
matters in both directions and for different reasons: the prompt has to keep
seeing one consistent language turn after turn, and you have to be able to
check what a character actually said rather than only what a second model call
made of it.

Nothing here is clever about language detection. If you say the character
writes Japanese, the pass is told to write Japanese — a detector would be one
more thing to be wrong, and the person configuring this already knows.
"""

from __future__ import annotations

import sqlite3

from .db import Database

# Kept short and generic. A long prompt here spends tokens on every single turn
# in both directions, and translation is the one task where a small model does
# better with less to read.
OUT_PROMPT = (
    "You translate roleplay prose into {target}.\n"
    "Keep the meaning, the tone and the register. Keep *asterisks* around "
    "actions and \"quotes\" around speech exactly where they are. Translate "
    "nothing else, add nothing, explain nothing.\n"
    'Reply with JSON only: {{"text": "<the translation>"}}'
)

IN_PROMPT = (
    "You translate a roleplay message into {target}.\n"
    "Keep the meaning, the tone and the register. Keep *asterisks* around "
    "actions and \"quotes\" around speech exactly where they are. Do not answer "
    "it, do not continue it, do not explain it.\n"
    'Reply with JSON only: {{"text": "<the translation>"}}'
)


def enabled(settings) -> bool:
    """On only when both languages are set and they actually differ.

    Two fields rather than a separate switch: "they write in Japanese, I read
    in Japanese" is not a translation job, and a third control that could
    disagree with that is a control that will.
    """
    theirs = (getattr(settings, "character_language", "") or "").strip()
    mine = (getattr(settings, "reading_language", "") or "").strip()
    return bool(theirs and mine and theirs.casefold() != mine.casefold())


def set_translation(db: Database, variant_id: str, text: str) -> None:
    def _write(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE message_variants SET translation=? WHERE id=?", (text, variant_id)
        )

    db.write_sync(_write)


def for_prompt(message: dict) -> str:
    """What the model should be given for this message.

    A user message crosses into the character's language; a reply is already in
    it. Falling back to the original matters: a turn whose translation failed
    should still reach the model, in the wrong language, rather than as nothing.
    """
    if message.get("role") == "user" and (message.get("translation") or "").strip():
        return message["translation"]
    return message.get("text") or ""


def for_screen(message: dict) -> str:
    """What should be drawn for this message.

    The mirror of `for_prompt`: a reply crosses back into your language, and
    your own message is shown as you typed it — seeing your own words handed
    back to you in translation would be worse than useless.
    """
    if message.get("role") != "user" and (message.get("translation") or "").strip():
        return message["translation"]
    return message.get("text") or ""
