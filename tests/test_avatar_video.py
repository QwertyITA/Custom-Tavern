"""Talking video avatar (AVATAR-VIDEO-CONTRACT.md, app/avatar_video.py).

Mirrors test_websearch.py's shape, for the same reason: this is a plain
httpx client for an external, self-hosted, non-LLM-shaped service, off by
default and off again until both a switch and a URL are set. Three rules
matter enough to have a test each:

- Off twice over, same as web search — no `avatar_url` *and* the character's
  own switch, either one missing is enough to make every call a no-op.
- Prepare is one-time; render only happens once prep_status is "ready".
- A failed or timed-out call never raises — the reply already went out, and
  a missing video for one line is not an error the turn should carry.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app import avatar_video, repo
from app.config import Settings
from app.events import BUS
from app.models import AvatarVideo, Character

from tests.conftest import sync, turn


def ready_character(**overrides) -> Character:
    av = AvatarVideo(enabled=True, idle_video="/avatar_idle/loop.mp4", prep_status="ready")
    av = av.model_copy(update=overrides)
    return Character(id="testchar", name="Mira", avatar_video=av)


def configured(**overrides) -> Settings:
    return Settings(avatar_url="http://avatar.test", **overrides)


# --------------------------------------------------------------- transport


class FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


@pytest.fixture
def transport(monkeypatch):
    """Swap the client's transport rather than the module's functions, so
    what is under test is the real request path — headers and all."""
    holder: dict[str, FakeTransport] = {}
    real = httpx.AsyncClient

    def factory(handler):
        fake = FakeTransport(handler)
        holder["fake"] = fake
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: real(**{**kw, "transport": fake})
        )
        return fake

    return factory


def by_path(routes: dict):
    """A handler that answers by request path, 404 on anything else."""
    def handle(request: httpx.Request) -> httpx.Response:
        for suffix, response in routes.items():
            if request.url.path.endswith(suffix):
                return response(request) if callable(response) else response
        return httpx.Response(404, text=f"unhandled: {request.url.path}")
    return handle


def js(payload):
    return lambda request: httpx.Response(200, json=payload)


# ------------------------------------------------------------- off by design


def test_prepare_is_a_noop_without_avatar_url(transport, db):
    fake = transport(js({"status": "queued"}))
    sync(avatar_video.prepare(db, Settings(), ready_character(), "http://phone/loop.mp4"))
    assert not fake.requests


def test_prepare_is_a_noop_when_the_character_has_it_off(transport, db):
    fake = transport(js({"status": "queued"}))
    off = ready_character(enabled=False)
    sync(avatar_video.prepare(db, configured(), off, "http://phone/loop.mp4"))
    assert not fake.requests


def test_render_is_a_noop_without_avatar_url(transport, db):
    fake = transport(js({"job_id": "j1"}))
    sync(avatar_video.render_for_reply(
        db, Settings(), ready_character(), "chat-1", "msg-1", "hello",
    ))
    assert not fake.requests


def test_render_is_a_noop_until_prep_is_ready(transport, db):
    fake = transport(js({"job_id": "j1"}))
    pending = ready_character(prep_status="pending")
    sync(avatar_video.render_for_reply(
        db, configured(), pending, "chat-1", "msg-1", "hello",
    ))
    assert not fake.requests


def test_render_is_a_noop_on_blank_text(transport, db):
    fake = transport(js({"job_id": "j1"}))
    sync(avatar_video.render_for_reply(
        db, configured(), ready_character(), "chat-1", "msg-1", "   ",
    ))
    assert not fake.requests


# ------------------------------------------------------------------ prepare


def test_prepare_polls_through_to_ready(transport, db, character):
    character.avatar_video = AvatarVideo(enabled=True, idle_video="/avatar_idle/l.mp4")
    repo.save_character(db, character)

    statuses = iter(["pending", "ready"])
    fake = transport(by_path({
        "/prepare": js({"status": "queued"}),
        "/status": lambda r: httpx.Response(200, json={"status": next(statuses)}),
    }))
    sync(avatar_video.prepare(db, configured(), character, "http://phone/l.mp4"))

    stored = repo.get_character(db, character.id)
    assert stored.avatar_video.prep_status == "ready"
    assert fake.requests[0].url.path.endswith("/prepare")
    assert json.loads(fake.requests[0].content) == {"idle_video_url": "http://phone/l.mp4"}


def test_prepare_marks_failed_when_the_service_says_so(transport, db, character):
    character.avatar_video = AvatarVideo(enabled=True, idle_video="/avatar_idle/l.mp4")
    repo.save_character(db, character)
    transport(by_path({
        "/prepare": js({"status": "queued"}),
        "/status": js({"status": "failed", "error": "no face detected"}),
    }))
    sync(avatar_video.prepare(db, configured(), character, "http://phone/l.mp4"))
    assert repo.get_character(db, character.id).avatar_video.prep_status == "failed"


def test_prepare_marks_failed_on_submit_error(transport, db, character):
    character.avatar_video = AvatarVideo(enabled=True, idle_video="/avatar_idle/l.mp4")
    repo.save_character(db, character)
    transport(lambda r: httpx.Response(500, text="down"))
    sync(avatar_video.prepare(db, configured(), character, "http://phone/l.mp4"))
    assert repo.get_character(db, character.id).avatar_video.prep_status == "failed"


def test_prepare_marks_failed_on_connection_error(transport, db, character):
    character.avatar_video = AvatarVideo(enabled=True, idle_video="/avatar_idle/l.mp4")
    repo.save_character(db, character)

    def refuse(request):
        raise httpx.ConnectError("no route", request=request)

    transport(refuse)
    sync(avatar_video.prepare(db, configured(), character, "http://phone/l.mp4"))
    assert repo.get_character(db, character.id).avatar_video.prep_status == "failed"


def test_prepare_sends_the_bearer_token_when_one_is_set(transport, db, character):
    character.avatar_video = AvatarVideo(enabled=True, idle_video="/avatar_idle/l.mp4")
    repo.save_character(db, character)
    fake = transport(by_path({
        "/prepare": js({"status": "queued"}),
        "/status": js({"status": "ready"}),
    }))
    sync(avatar_video.prepare(
        db, configured(avatar_key="sk-EXAMPLE"), character, "http://phone/l.mp4",
    ))
    assert fake.requests[0].headers["Authorization"] == "Bearer sk-EXAMPLE"


# ------------------------------------------------------------------- render


def test_render_publishes_the_finished_video(transport, db, character):
    character.avatar_video = AvatarVideo(
        enabled=True, idle_video="/avatar_idle/l.mp4", prep_status="ready",
    )
    repo.save_character(db, character)
    transport(by_path({
        "/render": js({"job_id": "job-1"}),
        "/jobs/job-1": js({"status": "done", "video_url": "http://desk/v.mp4"}),
    }))
    queue = BUS.subscribe("chat-1")
    try:
        sync(avatar_video.render_for_reply(
            db, configured(), character, "chat-1", "msg-1", "Sit wherever.",
        ))
        event = queue.get_nowait()
    finally:
        BUS.unsubscribe("chat-1", queue)
    assert event == {
        "type": "avatar_video", "message_id": "msg-1", "video_url": "http://desk/v.mp4",
    }


def test_render_forwards_the_voice_field(transport, db, character):
    character.avatar_video = AvatarVideo(
        enabled=True, idle_video="/avatar_idle/l.mp4", prep_status="ready", voice="mira-warm",
    )
    repo.save_character(db, character)
    fake = transport(by_path({
        "/render": js({"job_id": "job-1"}),
        "/jobs/job-1": js({"status": "done", "video_url": "http://desk/v.mp4"}),
    }))
    sync(avatar_video.render_for_reply(
        db, configured(), character, "chat-1", "msg-1", "Sit wherever.",
    ))
    sent = json.loads(fake.requests[0].content)
    assert sent == {"text": "Sit wherever.", "voice": "mira-warm"}


def test_render_publishes_nothing_on_a_failed_job(transport, db, character):
    character.avatar_video = AvatarVideo(
        enabled=True, idle_video="/avatar_idle/l.mp4", prep_status="ready",
    )
    repo.save_character(db, character)
    transport(by_path({
        "/render": js({"job_id": "job-1"}),
        "/jobs/job-1": js({"status": "failed", "error": "tts unavailable"}),
    }))
    queue = BUS.subscribe("chat-1")
    try:
        sync(avatar_video.render_for_reply(
            db, configured(), character, "chat-1", "msg-1", "Sit wherever.",
        ))
        assert queue.empty()
    finally:
        BUS.unsubscribe("chat-1", queue)


def test_render_publishes_nothing_on_a_submit_error(transport, db, character):
    character.avatar_video = AvatarVideo(
        enabled=True, idle_video="/avatar_idle/l.mp4", prep_status="ready",
    )
    repo.save_character(db, character)
    transport(lambda r: httpx.Response(500, text="down"))
    queue = BUS.subscribe("chat-1")
    try:
        sync(avatar_video.render_for_reply(
            db, configured(), character, "chat-1", "msg-1", "Sit wherever.",
        ))
        assert queue.empty()
    finally:
        BUS.unsubscribe("chat-1", queue)


# ----------------------------------------------------------------- settings


def test_the_avatar_settings_save_and_come_back(client, isolated_settings):
    from app import config

    body = {
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "avatar_url": "http://100.1.2.3:9000",
        "avatar_key": "sk-EXAMPLE",
        "avatar_self_url": "http://100.4.5.6:8787",
        "avatar_timeout": 45,
    }
    assert client.put("/api/settings", json=body).json()["ok"] is True
    assert config.SETTINGS.avatar_url == "http://100.1.2.3:9000"
    assert config.SETTINGS.avatar_self_url == "http://100.4.5.6:8787"
    assert config.SETTINGS.avatar_timeout == 45

    back = client.get("/api/settings").json()
    assert back["avatar_url"] == "http://100.1.2.3:9000"
    assert back["avatar_key"] == "***"


def test_saving_the_avatar_mask_back_keeps_the_key(client, isolated_settings):
    from app import config

    base = {
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "avatar_url": "http://100.1.2.3:9000",
    }
    client.put("/api/settings", json={**base, "avatar_key": "sk-EXAMPLE"})
    client.put("/api/settings", json={**base, "avatar_key": "***"})
    assert config.SETTINGS.avatar_key == "sk-EXAMPLE"


def test_the_avatar_key_never_reaches_anywhere_but_the_settings_file(client, isolated_settings):
    client.put("/api/settings", json={
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "avatar_url": "http://100.1.2.3:9000",
        "avatar_key": "sk-EXAMPLE",
    })
    saved = json.loads(isolated_settings.read_text())
    assert saved["avatar_key"] == "sk-EXAMPLE"
    assert "sk-EXAMPLE" not in json.dumps(client.get("/api/settings").json())


# ------------------------------------------------------------------ upload


def test_uploading_an_idle_loop_stores_it_and_stays_at_none_when_unconfigured(
    client, db, character, isolated_avatar_idle,
):
    response = client.post(
        f"/api/characters/{character.id}/avatar-idle?filename=loop.mp4",
        content=b"not really a video, just bytes",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["avatar_video"]["idle_video"].startswith("/avatar_idle/loop")
    # avatar_url is unset in this test's settings, so nothing was launched —
    # "none" says that honestly rather than a "pending" that never resolves.
    assert body["avatar_video"]["prep_status"] == "none"
    assert (isolated_avatar_idle / "loop.mp4").read_bytes() == b"not really a video, just bytes"


def test_uploading_an_unsupported_video_type_is_rejected(client, character, isolated_avatar_idle):
    response = client.post(
        f"/api/characters/{character.id}/avatar-idle?filename=loop.exe",
        content=b"x",
    )
    assert response.status_code == 400


# --------------------------------------------------------------- scheduler


def test_a_reply_kicks_off_a_render_for_a_ready_avatar(transport, db, character, chat):
    """The actual hookup in passes/scheduler.py — a real turn, not just a
    direct call into the module, so a future refactor of _run_reply cannot
    quietly drop the fire-and-forget task without a test noticing."""
    from app.passes.scheduler import PassScheduler

    character.avatar_video = AvatarVideo(
        enabled=True, idle_video="/avatar_idle/l.mp4", prep_status="ready",
    )
    repo.save_character(db, character)
    transport(by_path({
        "/render": js({"job_id": "job-1"}),
        "/jobs/job-1": js({"status": "done", "video_url": "http://desk/v.mp4"}),
    }))

    sched = PassScheduler(db, configured())
    queue = BUS.subscribe(chat["id"])
    try:
        sync(turn(sched, chat["id"], "hello"))
        seen = []
        while not queue.empty():
            seen.append(queue.get_nowait())
    finally:
        BUS.unsubscribe(chat["id"], queue)
    events = [e for e in seen if e["type"] == "avatar_video"]
    assert len(events) == 1
    assert events[0]["video_url"] == "http://desk/v.mp4"


def test_a_reply_never_touches_the_avatar_service_when_it_is_off(transport, db, character, chat):
    from app.passes.scheduler import PassScheduler

    assert character.avatar_video.enabled is False
    fake = transport(by_path({
        "/render": js({"job_id": "job-1"}),
        "/jobs/job-1": js({"status": "done", "video_url": "http://desk/v.mp4"}),
    }))
    sched = PassScheduler(db, configured())
    sync(turn(sched, chat["id"], "hello"))
    assert not fake.requests


def test_deleting_a_character_removes_its_idle_loop(client, db, character, isolated_avatar_idle):
    client.post(
        f"/api/characters/{character.id}/avatar-idle?filename=loop.mp4",
        content=b"bytes",
    )
    assert (isolated_avatar_idle / "loop.mp4").exists()
    client.delete(f"/api/characters/{character.id}")
    assert not (isolated_avatar_idle / "loop.mp4").exists()
