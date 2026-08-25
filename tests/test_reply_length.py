"""The "Cut excess paragraphs" hard backstop (§ app/reply_length.py) and its
undo, restore_full_text (§ app/repo.py, the message action wheel's "Restore
full length")."""

from __future__ import annotations

from dataclasses import replace

from app import reply_length, repo
from app.config import SETTINGS

from .conftest import events_of, sync, turn

ON = replace(SETTINGS, cut_excess_paragraphs=True)


def three_paragraphs() -> str:
    return "First.\n\nSecond.\n\nThird."


def test_off_by_default_leaves_a_long_reply_alone():
    kept, full = reply_length.cut(three_paragraphs(), SETTINGS)
    assert kept == three_paragraphs()
    assert full == ""


def test_on_with_no_configured_ceiling_leaves_it_alone():
    """craft:length off, or edited past what the regex can read a number out
    of — nothing to enforce, so nothing is cut."""
    off_length = replace(
        ON,
        prompt_sections=[{"id": "craft:length", "enabled": False}],
    )
    kept, full = reply_length.cut(three_paragraphs(), off_length)
    assert kept == three_paragraphs()
    assert full == ""

    unparseable = replace(
        ON,
        prompt_sections=[
            {"id": "craft:length", "enabled": True, "text": "Keep it short please."}
        ],
    )
    kept, full = reply_length.cut(three_paragraphs(), unparseable)
    assert kept == three_paragraphs()
    assert full == ""


def test_a_reply_within_the_ceiling_is_untouched():
    settings = replace(
        ON,
        prompt_sections=[
            {"id": "craft:length", "enabled": True, "text": "1 to 4 paragraphs, roughly 100 words."}
        ],
    )
    kept, full = reply_length.cut(three_paragraphs(), settings)
    assert kept == three_paragraphs()
    assert full == ""


def test_a_reply_past_the_ceiling_is_cut_at_a_paragraph_boundary():
    settings = replace(
        ON,
        prompt_sections=[
            {"id": "craft:length", "enabled": True, "text": "1 to 2 paragraphs, roughly 100 words."}
        ],
    )
    kept, full = reply_length.cut(three_paragraphs(), settings)
    assert kept == "First.\n\nSecond."
    assert full == three_paragraphs()


def test_a_single_number_paragraph_range_still_enforces():
    """setLengthRange()'s min==max shape ("3 paragraphs", no "to") has to
    parse the same way the two-number shape does."""
    settings = replace(
        ON,
        prompt_sections=[
            {"id": "craft:length", "enabled": True, "text": "1 paragraph, roughly 100 words."}
        ],
    )
    kept, full = reply_length.cut(three_paragraphs(), settings)
    assert kept == "First."
    assert full == three_paragraphs()


def test_configured_max_reads_the_upper_box():
    settings = replace(
        ON,
        prompt_sections=[
            {"id": "craft:length", "enabled": True, "text": "2 to 5 paragraphs, roughly 200 words."}
        ],
    )
    assert reply_length.configured_max(settings) == 5


# ---------------------------------------------------------- repo round-trip


def test_add_message_stores_full_text_only_when_given_one(db, character, chat):
    cut = repo.add_message(db, chat["id"], "assistant", "First.", full_text="First.\n\nSecond.")
    assert cut["has_full_text"] is True

    uncut = repo.add_message(db, chat["id"], "assistant", "Just this.")
    assert uncut["has_full_text"] is False


def test_restore_full_text_puts_it_back_and_clears_itself(db, character, chat):
    message = repo.add_message(
        db, chat["id"], "assistant", "First.", full_text="First.\n\nSecond."
    )
    restored = repo.restore_full_text(db, message["variant_id"])
    assert restored == "First.\n\nSecond."

    again = repo.get_message(db, message["id"])
    assert again["text"] == "First.\n\nSecond."
    assert again["has_full_text"] is False

    # Nothing left to restore a second time.
    assert repo.restore_full_text(db, message["variant_id"]) is None


def test_restore_full_text_on_an_uncut_variant_is_a_no_op(db, character, chat):
    message = repo.add_message(db, chat["id"], "assistant", "Just this.")
    assert repo.restore_full_text(db, message["variant_id"]) is None


# --------------------------------------------------------------------- API


def test_restore_route_puts_the_full_text_back(client, db, character, chat):
    message = repo.add_message(
        db, chat["id"], "assistant", "First.", full_text="First.\n\nSecond."
    )
    response = client.post(f"/api/messages/{message['id']}/restore")
    assert response.status_code == 200
    assert response.json()["text"] == "First.\n\nSecond."


def test_restore_route_404s_for_an_unknown_message(client):
    response = client.post("/api/messages/nope/restore")
    assert response.status_code == 404


def test_restore_route_400s_when_nothing_was_cut(client, db, character, chat):
    message = repo.add_message(db, chat["id"], "assistant", "Just this.")
    response = client.post(f"/api/messages/{message['id']}/restore")
    assert response.status_code == 400


# ------------------------------------------------------- through a real turn


def test_a_real_turn_gets_cut_and_can_be_restored(sched, chat, monkeypatch):
    """The scheduler wiring itself (§ passes/scheduler.py, both the initial
    reply and the swipe path call reply_length.cut right before storing) —
    everything above tests the cut in isolation, this is the two call sites
    actually reached."""
    from app.providers import echo

    monkeypatch.setattr(
        echo.EchoProvider,
        "_reply_body",
        lambda self, request: "First paragraph here.\n\nSecond one follows.\n\nThird and last.",
    )
    sched.settings.cut_excess_paragraphs = True
    sched.settings.prompt_sections = [
        {"id": "craft:length", "enabled": True, "text": "1 to 1 paragraphs, roughly 50 words."}
    ]

    events = sync(turn(sched, chat["id"], "Hello."))
    reply_events = events_of(events, "reply")
    assert len(reply_events) == 1
    message = reply_events[0]["message"]
    assert message["text"] == "First paragraph here."
    assert message["has_full_text"] is True

    stored = repo.get_message(sched.db, message["id"])
    assert stored["text"] == "First paragraph here."
    assert stored["has_full_text"] is True

    restored = repo.restore_full_text(sched.db, stored["variant_id"])
    assert restored == (
        "First paragraph here.\n\nSecond one follows.\n\nThird and last."
    )
