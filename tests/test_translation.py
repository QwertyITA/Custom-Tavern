"""Translation, in both directions (roadmap 23).

The load-bearing rule is that the original is never overwritten. `text` is
always what was actually written; `translation` is the same thing in the other
language. That matters in both directions and for different reasons: the prompt
has to keep seeing one consistent language turn after turn, and you have to be
able to check what a character actually said rather than only what a second
model call made of it.
"""

from __future__ import annotations

import pytest

from app import assembly, translation
from app.config import Settings, build_settings


def languages(theirs: str = "Japanese", mine: str = "English") -> Settings:
    return Settings(character_language=theirs, reading_language=mine)


# ------------------------------------------------------------- on and off


def test_it_is_off_until_both_languages_are_set():
    assert not translation.enabled(Settings())
    assert not translation.enabled(Settings(character_language="Japanese"))
    assert not translation.enabled(Settings(reading_language="English"))


def test_it_is_off_when_they_are_the_same():
    """"They write in English, I read in English" is not a translation job, and
    a separate switch that could disagree with that is a switch that will."""
    assert not translation.enabled(languages("English", "English"))
    assert not translation.enabled(languages("english", "  English "))


def test_it_is_on_when_they_differ():
    assert translation.enabled(languages())


# --------------------------------------------------------- which way round


def test_a_user_message_goes_to_the_model_translated():
    message = {"role": "user", "text": "Is the ferry running?", "translation": "フェリーは動いていますか"}
    assert translation.for_prompt(message) == "フェリーは動いていますか"


def test_a_user_message_is_shown_as_it_was_typed():
    """Seeing your own words handed back to you in translation would be worse
    than useless."""
    message = {"role": "user", "text": "Is the ferry running?", "translation": "フェリーは"}
    assert translation.for_screen(message) == "Is the ferry running?"


def test_a_reply_is_shown_translated():
    message = {"role": "assistant", "text": "動いていません", "translation": "It is not running."}
    assert translation.for_screen(message) == "It is not running."


def test_a_reply_goes_back_to_the_model_as_it_was_written():
    """The prompt has to keep seeing one consistent language turn after turn."""
    message = {"role": "assistant", "text": "動いていません", "translation": "It is not running."}
    assert translation.for_prompt(message) == "動いていません"


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_no_translation_means_the_original_both_ways(role):
    """A translation that failed should leave the turn readable in the wrong
    language rather than missing entirely."""
    message = {"role": role, "text": "hello", "translation": ""}
    assert translation.for_prompt(message) == "hello"
    assert translation.for_screen(message) == "hello"


def test_a_whitespace_only_translation_counts_as_none():
    message = {"role": "assistant", "text": "hello", "translation": "   "}
    assert translation.for_screen(message) == "hello"


# ------------------------------------------------------------- in the prompt


def test_assembly_sends_the_translated_user_text(db, chat, character, sched):
    from app import repo
    from tests.conftest import sync, turn

    sync(turn(sched, chat["id"], "Is the ferry running?"))
    mine = [m for m in repo.list_messages(db, chat["id"]) if m["role"] == "user"][-1]
    translation.set_translation(db, mine["variant_id"], "TRANSLATED-IN")

    out = assembly.build_reply_context(db, chat, character, Settings())
    # User turns only: the echo backend quotes the user back inside its reply,
    # so the original phrase legitimately appears in the assistant turn.
    mine_sent = "\n".join(m["content"] for m in out.messages if m["role"] == "user")
    assert "TRANSLATED-IN" in mine_sent
    assert "Is the ferry running?" not in mine_sent


def test_assembly_falls_back_when_there_is_no_translation(db, chat, character, sched):
    from tests.conftest import sync, turn

    sync(turn(sched, chat["id"], "Is the ferry running?"))
    out = assembly.build_reply_context(db, chat, character, Settings())
    joined = "\n".join(m["content"] for m in out.messages)
    assert "Is the ferry running?" in joined


# ------------------------------------------------------------- settings


def test_the_languages_save_and_come_back(client, isolated_settings):
    from app import config

    body = {
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "character_language": "Japanese",
        "reading_language": "English",
    }
    assert client.put("/api/settings", json=body).json()["ok"] is True
    assert config.SETTINGS.character_language == "Japanese"

    back = client.get("/api/settings").json()
    assert back["character_language"] == "Japanese"
    assert back["reading_language"] == "English"


def test_a_language_is_trimmed_and_bounded():
    settings = build_settings(
        {
            "backends": [{"name": "echo", "kind": "echo"}],
            "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
            "character_language": "  Japanese  ",
            "reading_language": "x" * 200,
        },
        Settings(),
    )
    assert settings.character_language == "Japanese"
    assert len(settings.reading_language) <= 40


def test_clearing_a_language_switches_it_off(client, isolated_settings):
    from app import config

    base = {
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
    }
    client.put("/api/settings", json={**base, "character_language": "Japanese",
                                      "reading_language": "English"})
    client.put("/api/settings", json={**base, "character_language": "",
                                      "reading_language": "English"})
    assert not translation.enabled(config.SETTINGS)


# ------------------------------------------------------------ through a turn


def a_chat(client) -> str:
    character_id = client.get("/api/characters").json()[0]["id"]
    return client.post("/api/chats", json={"character_id": character_id}).json()["id"]


def send(client, chat_id: str, text: str) -> None:
    with client.stream("POST", f"/api/chats/{chat_id}/send", json={"text": text}) as r:
        assert r.status_code == 200
        for _ in r.iter_lines():
            pass


def turn_it_on(client) -> None:
    client.put("/api/settings", json={
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "character_language": "Japanese",
        "reading_language": "English",
    })


def test_a_turn_records_translations_on_both_sides(client, isolated_settings):
    """The echo backend answers the translation calls too, so what lands is not
    real Japanese — what is checked is that both crossings happened and that
    neither overwrote its original."""
    turn_it_on(client)
    chat_id = a_chat(client)
    send(client, chat_id, "Is the ferry running?")

    messages = client.get(f"/api/chats/{chat_id}/messages").json()
    mine = [m for m in messages if m["role"] == "user"][-1]
    theirs = [m for m in messages if m["role"] == "assistant" and m["turn"] > 0][-1]

    assert mine["text"] == "Is the ferry running?", "your own words are kept"
    assert mine["translation"], "and crossed for the model"
    assert theirs["text"], "what they actually wrote is kept"
    assert theirs["translation"], "and crossed back for you"


def test_your_own_message_is_still_shown_as_you_typed_it(client, isolated_settings):
    turn_it_on(client)
    chat_id = a_chat(client)
    send(client, chat_id, "Is the ferry running?")

    mine = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
            if m["role"] == "user"][-1]
    assert mine.get("display", mine["text"]) == "Is the ferry running?"


def test_a_reply_is_drawn_in_the_reading_language(client, isolated_settings):
    turn_it_on(client)
    chat_id = a_chat(client)
    send(client, chat_id, "hello")

    theirs = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
              if m["role"] == "assistant" and m["turn"] > 0][-1]
    assert theirs["display"] == theirs["translation"]
    assert theirs["display"] != theirs["text"]


def test_nothing_extra_is_sent_when_translation_is_off(client):
    """The common case must not pay for this feature."""
    chat_id = a_chat(client)
    send(client, chat_id, "hello")
    for message in client.get(f"/api/chats/{chat_id}/messages").json():
        assert not message["translation"]
        assert "display" not in message


def test_a_display_rule_applies_to_what_you_are_looking_at(client, isolated_settings):
    """Translation first, then the find/replace rule (§16) — a rule about how
    things look should apply to the text actually on screen."""
    client.put("/api/settings", json={
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "character_language": "Japanese",
        "reading_language": "English",
        "regex_rules": [{"id": "r", "label": "Ellipses", "find": r"\.\.\.",
                         "replace": "…", "scope": "display"}],
    })
    chat_id = a_chat(client)
    send(client, chat_id, "hello...")

    theirs = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
              if m["role"] == "assistant" and m["turn"] > 0][-1]
    assert "..." not in theirs["display"]


def test_the_reply_event_carries_the_translation(client, isolated_settings):
    """Otherwise the reply reads in one language while it streams and another
    after a reload."""
    import json as _json

    turn_it_on(client)
    chat_id = a_chat(client)
    events = []
    with client.stream("POST", f"/api/chats/{chat_id}/send", json={"text": "hi"}) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                events.append(_json.loads(line[5:]))

    reply = next(e for e in events if e["type"] == "reply")
    assert reply["message"].get("display"), reply["message"]
