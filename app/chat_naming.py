"""The "Latest chat" placeholder and the rename queue behind it.

A brand new chat is called "Latest chat" — no LLM call, no queueing, nothing
to get wrong — until either another chat is created or an existing one is
opened. Either event ends its run: it falls back to being named after its
character, and is queued for a real title from the chat_rename pass
(app/passes/registry.py, app/passes/scheduler.py's `_drain_rename_queue`),
which works through the queue one chat at a time, once per successful reply
anywhere in the app.

Only one chat ever holds "latest" status at once (`Settings.latest_chat_id`),
and the queue (`Settings.rename_queue`) is capped at `rename_queue_max` —
FIFO, oldest dropped first once it's full, since a chat that fell out still
keeps a perfectly usable name (the character's) rather than a broken one.
"""

from __future__ import annotations

from . import repo
from .config import Settings, save_settings
from .db import Database

LATEST_LABEL = "Latest chat"


def mark_latest(db: Database, settings: Settings, chat_id: str) -> None:
    """A freshly created chat takes over "latest"; demote whoever had it."""
    _demote_current_latest(db, settings)
    settings.latest_chat_id = chat_id
    save_settings(settings)


def note_opened(db: Database, settings: Settings, chat_id: str) -> None:
    """Opening a chat other than the current "latest" one ends its run.

    Opening the latest chat itself — including the create-then-open call
    that follows every `mark_latest` — is not "another chat" and changes
    nothing; re-opening after it has already been demoted is a no-op too,
    since there is nothing left in `latest_chat_id` to demote.
    """
    if settings.latest_chat_id and settings.latest_chat_id != chat_id:
        _demote_current_latest(db, settings)
        save_settings(settings)


def queue_all_unnamed(db: Database, settings: Settings) -> int:
    """Push every chat still titled exactly its character's name onto the
    queue — the ones a demotion queued and the cap later dropped, or that
    were already sitting there from before this feature existed. Already
    queued chats and the current "latest" one (mid-placeholder on purpose)
    are left alone.

    Processed oldest-active first, so that if there are more eligible chats
    than the queue can hold, it is the least recently active ones the cap
    drops — not an arbitrary cutoff (§ _demote_current_latest, same rule).
    """
    queue = list(settings.rename_queue)
    added = 0
    for row in reversed(repo.list_chats(db)):
        if row["id"] == settings.latest_chat_id or row["id"] in queue:
            continue
        character = repo.get_character(db, row["character_id"])
        if character is None or row["title"] != character.name:
            continue
        queue.append(row["id"])
        added += 1
    if added:
        cap = max(1, settings.rename_queue_max)
        settings.rename_queue = queue[-cap:]
        save_settings(settings)
    return added


def _demote_current_latest(db: Database, settings: Settings) -> None:
    chat_id = settings.latest_chat_id
    if not chat_id:
        return
    chat = repo.get_chat(db, chat_id)
    if chat is not None:
        character = repo.get_character(db, chat["character_id"])
        repo.rename_chat(db, chat_id, character.name if character else "Chat")
        queue = [c for c in settings.rename_queue if c != chat_id]
        queue.append(chat_id)
        cap = max(1, settings.rename_queue_max)
        settings.rename_queue = queue[-cap:]
    settings.latest_chat_id = ""
