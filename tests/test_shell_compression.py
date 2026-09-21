"""Compressing the shell, and only the shell.

The service worker is network-first on purpose (§ static/sw.js: cache-first
meant every update landed a reload late), so the first boot after a `git
pull` re-fetches the whole shell — 928 KB of HTML, CSS and JS. Later boots
cost nothing on the wire, because StaticFiles sends an ETag and the browser
gets a 304 back; this is about the boot that follows an update, which is the
one a person actually notices.

The safety half is the point of most of these. `/api/**` is where the SSE
lives, and a turn stream is a response that has to reach the client a chunk
at a time — a compressor that buffered it would undo the keepalive by
holding back the very bytes that prove the connection is alive. So the rule
is a path rule, not a content-type rule, and nothing under /api can reach
the compressor at all.
"""

from __future__ import annotations

import json

from app import main


def test_the_page_comes_back_compressed(client):
    r = client.get("/", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert "<html" in r.text.lower()


def test_the_scripts_and_styles_come_back_compressed(client):
    for path in ("/static/app.js", "/static/styles.css"):
        r = client.get(path, headers={"accept-encoding": "gzip"})
        assert r.headers.get("content-encoding") == "gzip", path


def test_it_is_worth_doing(client):
    """263 KB instead of 928 KB, measured. If this ever stops being a real
    saving the middleware is pure cost on a phone's CPU."""
    r = client.get("/static/app.js", headers={"accept-encoding": "gzip"})
    packed = int(r.headers["content-length"])
    raw = len(r.content)
    assert packed < raw / 2, f"{packed} vs {raw}"


def test_a_client_that_cannot_unpack_it_gets_it_plain(client):
    r = client.get("/", headers={"accept-encoding": "identity"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert "<html" in r.text.lower()


def test_it_says_the_answer_depends_on_the_asking(client):
    """Two forms of one URL. A cache handed the wrong one serves gzip to
    something that cannot read it."""
    r = client.get("/static/app.js", headers={"accept-encoding": "gzip"})
    assert "accept-encoding" in r.headers.get("vary", "").lower()


# ------------------------------------------------------------- and only that


def test_the_api_is_never_touched(client):
    r = client.get("/api/settings", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers


def test_a_turn_still_arrives_a_piece_at_a_time(client):
    """The one that matters. Buffering a turn stream to compress it would
    hold back every delta until the turn was over — and hold back the
    keepalive pings with them (§ _stream), which is the failure this would
    be re-creating rather than preventing."""
    character_id = client.get("/api/characters").json()[0]["id"]
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]

    seen = []
    with client.stream("POST", f"/api/chats/{chat_id}/send",
                       json={"text": "hello"}) as response:
        assert response.status_code == 200
        assert "content-encoding" not in response.headers
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("data:"):
                seen.append(json.loads(line[5:])["type"])
    assert "turn_start" in seen and "turn_end" in seen


def test_the_rule_is_about_paths_not_about_content_types():
    """An /api route that answers with HTML, or a future one that streams
    something text/css-shaped, must still be left alone — which a
    content-type rule on its own would not do."""
    assert main._is_shell("/static/app.js")
    assert main._is_shell("/")
    assert main._is_shell("/sw.js")
    assert not main._is_shell("/api/settings")
    assert not main._is_shell("/api/chats/abc/events")
    assert not main._is_shell("/avatars/x.png")


def test_an_image_is_left_alone(client):
    """Already compressed. Re-packing a PNG spends a phone's CPU to make the
    file very slightly bigger."""
    r = client.get("/static/icons/icon-192.png", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
