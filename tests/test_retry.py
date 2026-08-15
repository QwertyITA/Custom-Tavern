"""Answering a message whose reply never came (UX audit 4).

A turn whose reply failed leaves the transcript ending on a question nobody
answered. The three things that must hold:

- Retrying does not send the message a second time. It is already stored; only
  the reply is missing.
- A retry is an ordinary turn from the user message onwards — same state decay,
  same nudges, same background passes — because two code paths that answer a
  message will eventually answer it differently.
- Retrying when nothing is waiting is refused rather than inventing a second
  reply to a message that already has one.
"""

from __future__ import annotations

import json

from app import repo
from app.passes import registry

from tests.conftest import drain, events_of, sync, turn


def dangle(db, sched, chat_id: str, text: str = "is the ferry running?") -> dict:
    """A user message with no reply, as a failed turn leaves behind."""
    message = repo.add_message(db, chat_id, "user", text)
    return message


# ------------------------------------------------------------- the basics


def test_it_answers_the_dangling_message(db, chat, character, sched):
    dangle(db, sched, chat["id"])
    events = sync(drain(sched.retry_turn(chat["id"])))

    assert events_of(events, "reply"), events
    history = repo.list_messages(db, chat["id"])
    assert history[-1]["role"] == "assistant"


def test_it_does_not_send_the_message_again(db, chat, character, sched):
    """The whole point. Sending again would put the same words in twice."""
    dangle(db, sched, chat["id"], "is the ferry running?")
    before = [m["text"] for m in repo.list_messages(db, chat["id"])]
    sync(drain(sched.retry_turn(chat["id"])))
    after = [m["text"] for m in repo.list_messages(db, chat["id"])]

    assert after.count("is the ferry running?") == 1
    assert after[: len(before)] == before, "nothing before the reply moved"


def test_the_reply_is_bound_to_the_message_it_answers(db, chat, character, sched):
    mine = dangle(db, sched, chat["id"])
    sync(drain(sched.retry_turn(chat["id"])))
    theirs = repo.list_messages(db, chat["id"])[-1]
    assert theirs["turn"] == mine["turn"]


def test_it_says_the_message_is_already_on_screen(db, chat, character, sched):
    """`turn_start` makes the frontend append the user message. A retry's is
    already there, so a second one would be a duplicate on screen only."""
    dangle(db, sched, chat["id"])
    events = sync(drain(sched.retry_turn(chat["id"])))
    assert not events_of(events, "turn_start")
    assert events_of(events, "turn_resume")


# ------------------------------------------------------------ refusing it


def test_it_refuses_when_the_last_message_has_a_reply(db, chat, character, sched):
    sync(turn(sched, chat["id"], "hello"))
    events = sync(drain(sched.retry_turn(chat["id"])))
    assert events_of(events, "error"), "a second reply is not what was asked for"
    assert not events_of(events, "reply")


def test_it_refuses_an_empty_chat(db, chat, character, sched):
    """A brand-new chat holds only the greeting, which is not a question."""
    events = sync(drain(sched.retry_turn(chat["id"])))
    assert not events_of(events, "reply")


def test_it_refuses_an_unknown_chat(db, sched):
    events = sync(drain(sched.retry_turn("nope")))
    assert events_of(events, "error")


# ----------------------------------------------------- the rest of a turn


def test_a_retry_runs_the_background_passes_too(db, chat, character, sched):
    """A retry that skipped them would leave the state behind by one turn, and
    the gap would only show up much later as a summary that missed something."""
    dangle(db, sched, chat["id"])
    events = sync(drain(sched.retry_turn(chat["id"])))
    sync(sched.await_pending(chat["id"], timeout=20))
    assert events_of(events, "background_queued")


def test_a_retry_fires_the_nudges(db, chat, character, sched):
    """The character card nudges `trust` down on "liar". Answering the message
    a second time must still see it."""
    dangle(db, sched, chat["id"], "you are a liar")
    events = sync(drain(sched.retry_turn(chat["id"])))
    assert events_of(events, "nudges"), events


def test_a_retry_searches_when_the_switch_is_on(db, chat, character, sched):
    """Same turn, same steps — a retry must not silently drop the search."""
    import httpx

    from app import websearch
    from app.config import Settings

    calls: list[str] = []

    class Fake(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(str(request.url))
            return httpx.Response(200, json={"results": [{"title": "T", "content": "s"}]})

    real = httpx.AsyncClient
    httpx.AsyncClient = lambda **kw: real(**{**kw, "transport": Fake()})
    try:
        sync(registry.set_toggle(db, "web_search", True))
        sched.settings = Settings(search_url="http://s.test/?q={q}")
        dangle(db, sched, chat["id"])
        events = sync(drain(sched.retry_turn(chat["id"])))
    finally:
        httpx.AsyncClient = real

    assert calls, "the retry should have looked it up like any other turn"
    assert events_of(events, "search_done")
    assert websearch.configured(sched.settings)


# ---------------------------------------------------------------- the route


def a_chat(client) -> str:
    character_id = client.get("/api/characters").json()[0]["id"]
    return client.post("/api/chats", json={"character_id": character_id}).json()["id"]


def test_the_route_streams_a_reply(client, db):
    chat_id = a_chat(client)
    repo.add_message(db, chat_id, "user", "is the ferry running?")

    events = []
    with client.stream("POST", f"/api/chats/{chat_id}/retry") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))

    assert [e for e in events if e["type"] == "reply"], events
    messages = client.get(f"/api/chats/{chat_id}/messages").json()
    assert messages[-1]["role"] == "assistant"
    assert [m["text"] for m in messages].count("is the ferry running?") == 1


def test_the_route_404s_on_an_unknown_chat(client):
    assert client.post("/api/chats/nope/retry").status_code == 404
