"""A stream that says nothing still has to say something.

Reported live, as "the tavern's server is not answering — check Termux is
still open". It was answering. It was waiting for a worker.

The ambient bus has pinged every 20 seconds since it was written, with a note
saying it "keeps the connection from idling out". The turn stream — the one
that idles *longest*, because it is silent for the whole time the backend is
thinking, which on the AI Horde is a queue wait measured in minutes — had no
keepalive at all. A phone drops an idle connection long before that; the
fetch then rejects with a bare TypeError, and the app reports the one thing
that had not happened.

A turn that can hold several generations with silences between them
(§ groups.plan) made it likelier, which is how it surfaced.
"""

from __future__ import annotations

import asyncio
import json

from app import main
from tests.conftest import sync


def events(body: bytes) -> list[dict]:
    return [
        json.loads(line[5:])
        for line in body.decode().splitlines()
        if line.startswith("data:")
    ]


async def collect(response) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)


def test_a_silent_stream_still_proves_it_is_alive(monkeypatch):
    async def case():
        monkeypatch.setattr(main, "STREAM_PING_SECONDS", 0.05)

        async def slow():
            await asyncio.sleep(0.26)
            yield {"type": "reply", "text": "at last"}

        got = events(await collect(await main._stream(slow())))
        assert [e["type"] for e in got if e["type"] != "ping"] == ["reply"]
        assert sum(1 for e in got if e["type"] == "ping") >= 2

    sync(case())


def test_the_pings_stop_when_the_events_start(monkeypatch):
    """A keepalive that kept firing between two fast events would be noise on
    the wire and a second thing for the client to filter out of every turn."""
    async def case():
        monkeypatch.setattr(main, "STREAM_PING_SECONDS", 10)

        async def quick():
            for i in range(3):
                yield {"type": "delta", "text": str(i)}

        got = events(await collect(await main._stream(quick())))
        assert [e["type"] for e in got] == ["delta", "delta", "delta"]

    sync(case())


def test_the_stream_still_ends_when_the_generator_does(monkeypatch):
    async def case():
        monkeypatch.setattr(main, "STREAM_PING_SECONDS", 0.05)

        async def two():
            yield {"type": "turn_start", "turn": 1}
            await asyncio.sleep(0.12)
            yield {"type": "turn_end", "turn": 1}

        got = events(await collect(await main._stream(two())))
        assert got[0]["type"] == "turn_start"
        assert got[-1]["type"] == "turn_end"

    sync(case())


def test_a_failure_still_reaches_the_client_as_an_error(monkeypatch):
    async def case():
        monkeypatch.setattr(main, "STREAM_PING_SECONDS", 10)

        async def breaks():
            yield {"type": "turn_start", "turn": 1}
            raise RuntimeError("the backend fell over")

        got = events(await collect(await main._stream(breaks())))
        assert got[-1]["type"] == "error"
        assert "fell over" in got[-1]["error"]

    sync(case())


def test_hanging_up_mid_turn_does_not_leave_the_turn_running(monkeypatch):
    """Whatever was being awaited when the reader went away is a whole turn,
    and nobody is left to receive it."""
    async def case():
        monkeypatch.setattr(main, "STREAM_PING_SECONDS", 0.05)
        closed = asyncio.Event()

        async def long():
            try:
                yield {"type": "turn_start", "turn": 1}
                await asyncio.sleep(30)
                yield {"type": "turn_end", "turn": 1}
            finally:
                closed.set()

        response = await main._stream(long())
        iterator = response.body_iterator.__aiter__()
        await iterator.__anext__()          # the turn_start
        await iterator.aclose()             # the reader goes away
        await asyncio.wait_for(closed.wait(), timeout=2)

    sync(case())
