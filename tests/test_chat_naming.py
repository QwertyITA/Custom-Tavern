"""The "Latest chat" placeholder and its rename queue (§ app/chat_naming.py)."""

from __future__ import annotations

from app import chat_naming, repo
from app.config import Settings


def test_mark_latest_names_a_fresh_chat(db, character, isolated_settings):
    chat = repo.create_chat(db, character.id, chat_naming.LATEST_LABEL)
    settings = Settings()

    chat_naming.mark_latest(db, settings, chat["id"])

    assert settings.latest_chat_id == chat["id"]
    assert settings.rename_queue == []
    assert repo.get_chat(db, chat["id"])["title"] == chat_naming.LATEST_LABEL


def test_creating_a_second_chat_demotes_the_first(db, character, isolated_settings):
    first = repo.create_chat(db, character.id, chat_naming.LATEST_LABEL)
    settings = Settings()
    chat_naming.mark_latest(db, settings, first["id"])

    second = repo.create_chat(db, character.id, chat_naming.LATEST_LABEL)
    chat_naming.mark_latest(db, settings, second["id"])

    assert settings.latest_chat_id == second["id"]
    assert settings.rename_queue == [first["id"]]
    assert repo.get_chat(db, first["id"])["title"] == character.name


def test_opening_a_different_chat_demotes_the_latest_one(db, character, isolated_settings):
    latest = repo.create_chat(db, character.id, chat_naming.LATEST_LABEL)
    other = repo.create_chat(db, character.id, "an older chat")
    settings = Settings()
    chat_naming.mark_latest(db, settings, latest["id"])

    chat_naming.note_opened(db, settings, other["id"])

    assert settings.latest_chat_id == ""
    assert settings.rename_queue == [latest["id"]]
    assert repo.get_chat(db, latest["id"])["title"] == character.name


def test_reopening_the_latest_chat_itself_does_not_demote_it(db, character, isolated_settings):
    latest = repo.create_chat(db, character.id, chat_naming.LATEST_LABEL)
    settings = Settings()
    chat_naming.mark_latest(db, settings, latest["id"])

    chat_naming.note_opened(db, settings, latest["id"])

    assert settings.latest_chat_id == latest["id"]
    assert settings.rename_queue == []
    assert repo.get_chat(db, latest["id"])["title"] == chat_naming.LATEST_LABEL


def test_opening_something_else_when_nothing_is_latest_is_a_no_op(db, character, isolated_settings):
    a = repo.create_chat(db, character.id, "a")
    b = repo.create_chat(db, character.id, "b")
    settings = Settings()  # latest_chat_id is already "" — nothing to demote

    chat_naming.note_opened(db, settings, b["id"])

    assert settings.latest_chat_id == ""
    assert settings.rename_queue == []
    assert repo.get_chat(db, a["id"])["title"] == "a"


def test_queue_is_capped_fifo(db, character, isolated_settings):
    settings = Settings()
    settings.rename_queue_max = 2
    chats = [repo.create_chat(db, character.id, chat_naming.LATEST_LABEL) for _ in range(3)]

    for c in chats:
        chat_naming.mark_latest(db, settings, c["id"])
    # The last one created is still "latest"; demote it by hand to see the
    # full queue the cap left behind.
    chat_naming.note_opened(db, settings, "somewhere-else")

    # Oldest (chats[0]) fell out first; the two most recent demotions remain.
    assert settings.rename_queue == [chats[1]["id"], chats[2]["id"]]


def test_a_deleted_chat_is_just_dropped(db, character, isolated_settings):
    latest = repo.create_chat(db, character.id, chat_naming.LATEST_LABEL)
    settings = Settings()
    chat_naming.mark_latest(db, settings, latest["id"])
    repo.delete_chat(db, latest["id"])

    chat_naming.note_opened(db, settings, "some-other-chat")

    assert settings.latest_chat_id == ""
    assert settings.rename_queue == []
