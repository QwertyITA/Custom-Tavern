"""Suggest edit: rewrite a reply per a note about it — "make it shorter",
"the perspective isn't right" — rather than branching from it, and only
ever on the literal last message in the chat."""

from __future__ import annotations

from app import repo
from app.passes.scheduler import PassScheduler

from .conftest import events_of, sync, turn
from .test_api import new_chat, send, sse_events


def _reply(sched: PassScheduler, chat) -> dict:
    sync(turn(sched, chat["id"], "hello there"))
    messages = repo.list_messages(sched.db, chat["id"])
    return next(m for m in reversed(messages) if m["role"] == "assistant")


# ---------------------------------------------------------------- the pass


def test_suggest_edit_rewrites_the_same_variant_in_place(sched, chat, character):
    reply = _reply(sched, chat)

    async def scenario():
        return [e async for e in sched.run_suggest_edit(reply["id"], "Make it shorter.")]

    events = sync(scenario())
    edited = events_of(events, "edited")
    assert edited and edited[0]["message_id"] == reply["id"]
    assert edited[0]["text"]

    assert len(repo.list_variants(sched.db, reply["id"])) == 1, "a rewrite, not a new variant"
    fetched = repo.get_message(sched.db, reply["id"])
    assert fetched["edited"] is True
    assert fetched["text"] == edited[0]["text"]

    row = sched.db.query_one(
        "SELECT status FROM pass_runs WHERE chat_id=? AND pass_id='basic' "
        "ORDER BY rowid DESC LIMIT 1",
        (chat["id"],),
    )
    assert row["status"] == "done"


def test_suggest_edit_only_works_on_the_latest_message(sched, chat, character):
    """An edit to an older reply would be revising something everything
    since has already answered."""
    sync(turn(sched, chat["id"], "first"))
    older = next(
        m for m in reversed(repo.list_messages(sched.db, chat["id"])) if m["role"] == "assistant"
    )
    sync(turn(sched, chat["id"], "second"))
    before = repo.get_message(sched.db, older["id"])["text"]

    async def scenario():
        return [e async for e in sched.run_suggest_edit(older["id"], "Make it shorter.")]

    events = sync(scenario())
    assert events_of(events, "error")
    assert repo.get_message(sched.db, older["id"])["text"] == before


def test_suggest_edit_refuses_a_user_message(sched, chat, character):
    sync(turn(sched, chat["id"], "hello there"))
    user_message = next(m for m in repo.list_messages(sched.db, chat["id"]) if m["role"] == "user")

    async def scenario():
        return [e async for e in sched.run_suggest_edit(user_message["id"], "Make it shorter.")]

    assert events_of(sync(scenario()), "error")


def test_suggest_edit_requires_an_instruction(sched, chat, character):
    reply = _reply(sched, chat)
    before = sched.db.query(
        "SELECT id FROM pass_runs WHERE chat_id=? AND pass_id='basic'", (chat["id"],)
    )

    async def scenario():
        return [e async for e in sched.run_suggest_edit(reply["id"], "   ")]

    events = sync(scenario())
    assert events_of(events, "error")
    after = sched.db.query(
        "SELECT id FROM pass_runs WHERE chat_id=? AND pass_id='basic'", (chat["id"],)
    )
    assert len(after) == len(before), "a blank note must not spend a model call"


def test_suggest_edit_reports_an_unknown_message(sched, chat):
    async def scenario():
        return [e async for e in sched.run_suggest_edit("not-a-real-message", "Make it shorter.")]

    assert events_of(sync(scenario()), "error")


# ------------------------------------------------------------------- the API


def test_suggest_edit_rewrites_in_place_over_the_api(client):
    chat_id = new_chat(client)
    send(client, chat_id, "Tell me about the crossing.")
    reply = client.get(f"/api/chats/{chat_id}/messages").json()[-1]
    assert reply["variant_count"] == 1

    with client.stream(
        "POST", f"/api/messages/{reply['id']}/suggest-edit", json={"instruction": "Make it shorter."}
    ) as response:
        assert response.status_code == 200
        events = sse_events(response)

    final = [e for e in events if e["type"] == "edited"]
    assert final, [e["type"] for e in events]

    after = client.get(f"/api/chats/{chat_id}/messages").json()[-1]
    assert after["id"] == reply["id"]
    assert after["variant_count"] == 1, "it must not become a new variant"
    assert after["edited"] is True


def test_suggest_edit_only_offered_on_the_latest_message_over_the_api(client):
    chat_id = new_chat(client)
    send(client, chat_id, "first")
    older = client.get(f"/api/chats/{chat_id}/messages").json()[-1]
    send(client, chat_id, "second")
    before = older["text"]

    with client.stream(
        "POST", f"/api/messages/{older['id']}/suggest-edit", json={"instruction": "Make it shorter."}
    ) as response:
        events = sse_events(response)
    assert any(e["type"] == "error" for e in events)

    refetched = next(
        m for m in client.get(f"/api/chats/{chat_id}/messages").json() if m["id"] == older["id"]
    )
    assert refetched["text"] == before


def test_only_a_reply_can_have_a_suggested_edit(client):
    chat_id = new_chat(client)
    send(client, chat_id, "Anything.")
    user_message = next(
        m for m in client.get(f"/api/chats/{chat_id}/messages").json() if m["role"] == "user"
    )
    with client.stream(
        "POST", f"/api/messages/{user_message['id']}/suggest-edit", json={"instruction": "Shorter."}
    ) as response:
        events = sse_events(response)
    assert any(e["type"] == "error" for e in events)


def test_suggest_edit_requires_a_note_over_the_api(client):
    chat_id = new_chat(client)
    send(client, chat_id, "hello there")
    reply = client.get(f"/api/chats/{chat_id}/messages").json()[-1]

    with client.stream(
        "POST", f"/api/messages/{reply['id']}/suggest-edit", json={"instruction": "   "}
    ) as response:
        events = sse_events(response)
    assert any(e["type"] == "error" for e in events)

    after = client.get(f"/api/chats/{chat_id}/messages").json()[-1]
    assert after["text"] == reply["text"]
