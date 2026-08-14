"""HTTP surface: routes, SSE framing, and the turn loop end to end."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(db):
    with TestClient(app) as test_client:
        yield test_client


def sse_events(response) -> list[dict]:
    out = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


def new_chat(client) -> str:
    characters = client.get("/api/characters").json()
    assert characters, "bootstrap should import data/characters/"
    chat = client.post("/api/chats", json={"character_id": characters[0]["id"]})
    assert chat.status_code == 200
    return chat.json()["id"]


def send(client, chat_id: str, text: str) -> list[dict]:
    with client.stream("POST", f"/api/chats/{chat_id}/send", json={"text": text}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return sse_events(response)


# ----------------------------------------------------------------- system


def test_health_reports_bootstrap_state(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["characters"] >= 1


def test_settings_never_leak_api_keys(client):
    body = client.get("/api/settings").json()
    for backend in body["backends"]:
        assert backend["api_key"] in ("", "***")


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Point the save path at a temp file — never the developer's real one."""
    from app import config

    original = config.SETTINGS
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "settings_path", lambda: path)
    yield path
    config.apply_settings(original)


def test_saving_settings_writes_the_file_and_takes_effect(client, isolated_settings):
    from app import config

    payload = {
        "backends": [
            {"name": "echo", "kind": "echo", "model": "echo-1"},
            {"name": "horde", "kind": "horde", "api_key": "example-horde-key-abc123", "model": "any"},
        ],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "horde"},
        "verbatim_window": 12,
    }
    body = client.put("/api/settings", json=payload).json()
    assert body["ok"] is True

    assert "example-horde-key-abc123" in isolated_settings.read_text()
    # The response, like every read, is masked.
    assert body["settings"]["backends"][1]["api_key"] == "***"
    # And the running process picked it up.
    assert config.SETTINGS.verbatim_window == 12
    assert config.SETTINGS.tiers["background"] == "horde"


def test_get_after_save_never_returns_the_real_key(client, isolated_settings):
    client.put("/api/settings", json={
        "backends": [{"name": "echo", "kind": "echo"},
                     {"name": "openai", "kind": "openai", "api_key": "example-openai-key-donotleak"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
    })
    assert "example-openai-key-donotleak" not in client.get("/api/settings").text


def test_saving_with_a_masked_key_keeps_it(client, isolated_settings):
    import json as _json

    base = {
        "backends": [{"name": "echo", "kind": "echo"},
                     {"name": "openai", "kind": "openai", "api_key": "example-openai-key-keepme"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
    }
    client.put("/api/settings", json=base)

    # Re-submit exactly what a browser would: the masked key, one field changed.
    echoed = client.get("/api/settings").json()
    echoed["backends"][1]["model"] = "gpt-4o-mini"
    client.put("/api/settings", json=echoed)

    stored = _json.loads(isolated_settings.read_text())
    openai = next(b for b in stored["backends"] if b["name"] == "openai")
    assert openai["api_key"] == "example-openai-key-keepme"
    assert openai["model"] == "gpt-4o-mini"


def test_invalid_settings_return_400_and_change_nothing(client, isolated_settings):
    from app import config

    before = config.SETTINGS.verbatim_window
    response = client.put("/api/settings", json={
        "backends": [{"name": "x", "kind": "not-a-backend"}],
        "tiers": {"blocking": "x", "foreground": "x", "background": "x"},
    })
    assert response.status_code == 400
    assert not isolated_settings.exists()
    assert config.SETTINGS.verbatim_window == before


def test_connection_test_probes_a_backend(client):
    body = client.post("/api/settings/test", json={"name": "echo", "kind": "echo", "model": "echo-1"}).json()
    assert body["ok"] is True
    assert "latency_ms" in body


def test_connection_test_reports_failure_without_raising(client):
    body = client.post("/api/settings/test", json={
        "name": "dead", "kind": "ollama", "base_url": "http://127.0.0.1:1", "model": "x", "timeout": 2,
    }).json()
    assert body["ok"] is False
    assert body["error"]


def test_static_shell_is_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/manifest.webmanifest").status_code == 200
    assert client.get("/sw.js").status_code == 200


def test_unknown_chat_is_404(client):
    assert client.get("/api/chats/nope").status_code == 404


# ------------------------------------------------------------------ chats


def test_new_chat_loads_the_greeting_as_a_real_message(client):
    chat_id = new_chat(client)
    messages = client.get(f"/api/chats/{chat_id}/messages").json()
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["turn"] == 0


def test_chat_detail_carries_state_bands_and_toggles(client):
    chat_id = new_chat(client)
    body = client.get(f"/api/chats/{chat_id}").json()
    assert body["character"]["name"]
    assert body["state"]["bands"]
    assert "avoid_yes_person" in body["toggles"]


def test_empty_message_is_rejected(client):
    chat_id = new_chat(client)
    assert client.post(f"/api/chats/{chat_id}/send", json={"text": "   "}).status_code == 400


# ------------------------------------------------------------------- turn


def test_send_streams_a_full_turn(client):
    chat_id = new_chat(client)
    events = send(client, chat_id, "Cold out.")
    kinds = [e["type"] for e in events]
    assert "turn_start" in kinds
    assert "delta" in kinds
    assert "reply" in kinds
    assert kinds[-1] == "turn_end"


def test_reply_is_persisted_and_readable(client):
    chat_id = new_chat(client)
    send(client, chat_id, "Cold out.")
    messages = client.get(f"/api/chats/{chat_id}/messages").json()
    assert [m["role"] for m in messages] == ["assistant", "user", "assistant"]


def test_runs_and_cost_endpoints_reflect_the_turn(client):
    chat_id = new_chat(client)
    send(client, chat_id, "Cold out.")

    runs = client.get(f"/api/chats/{chat_id}/runs").json()
    assert any(r["pass_id"] == "basic" and r["status"] == "done" for r in runs)

    cost = client.get(f"/api/chats/{chat_id}/cost").json()
    assert cost["totals"]["tokens_in"] > 0
    assert any(row["pass_id"] == "basic" for row in cost["per_pass"])


# --------------------------------------------------------------- messages


def test_edit_updates_text_and_flags_the_message(client):
    chat_id = new_chat(client)
    send(client, chat_id, "Cold out.")
    message = client.get(f"/api/chats/{chat_id}/messages").json()[-1]

    updated = client.patch(
        f"/api/messages/{message['id']}", json={"text": "*Rewritten.*", "reaudit": False}
    ).json()
    assert updated["text"] == "*Rewritten.*"
    assert updated["edited"] is True


def test_swipe_then_choose_variant(client):
    chat_id = new_chat(client)
    send(client, chat_id, "Cold out.")
    message = client.get(f"/api/chats/{chat_id}/messages").json()[-1]

    with client.stream("POST", f"/api/messages/{message['id']}/swipe", json={}) as response:
        events = sse_events(response)
    assert any(e["type"] == "variant" for e in events)

    variants = client.get(f"/api/messages/{message['id']}/variants").json()
    assert len(variants) == 2

    chosen = client.post(f"/api/messages/{message['id']}/variants/{variants[0]['id']}").json()
    assert chosen["text"] == variants[0]["text"]


def test_choosing_an_unknown_variant_is_404(client):
    chat_id = new_chat(client)
    send(client, chat_id, "Cold out.")
    message = client.get(f"/api/chats/{chat_id}/messages").json()[-1]
    assert client.post(f"/api/messages/{message['id']}/variants/nope").status_code == 404


# -------------------------------------------------------- passes & toggles


def test_pass_library_lists_the_canonical_set(client):
    ids = {p["id"] for p in client.get("/api/passes").json()}
    assert {"basic", "scene", "expression", "summary", "memory", "state_auditor"} <= ids


def test_custom_pass_can_be_created_and_deleted(client):
    definition = {
        "kind": "custom",
        "label": "Test pass",
        "trigger": {"type": "every_n", "n": 4},
        "model_tier": "background",
        "prompt": "do a thing",
        "output": {"type": "gui_panel", "target": "test"},
        "writes_slice": "state.test",
    }
    created = client.put("/api/passes/custom_test", json=definition).json()
    assert created["id"] == "custom_test"
    assert created["animation"] == ""  # resolved at render time, not stored

    assert client.delete("/api/passes/custom_test").json()["deleted"] is True
    assert "custom_test" not in {p["id"] for p in client.get("/api/passes").json()}


def test_canonical_passes_are_disabled_rather_than_deleted(client):
    body = client.delete("/api/passes/scene").json()
    assert body == {"deleted": False, "disabled": True}
    scene = next(p for p in client.get("/api/passes").json() if p["id"] == "scene")
    assert scene["enabled"] is False


def test_invalid_pass_definition_is_rejected(client):
    assert client.put("/api/passes/bad", json={"model_tier": "nonsense"}).status_code == 400


def test_toggle_scope_overrides_global(client):
    chat_id = new_chat(client)
    assert client.get("/api/toggles").json()["states"]["anti_slop"] is True

    client.post("/api/toggles/anti_slop", json={"enabled": False, "scope": "per_chat", "scope_id": chat_id})
    scoped = client.get(f"/api/toggles?chat_id={chat_id}").json()["states"]
    assert scoped["anti_slop"] is False
    # The global default is untouched.
    assert client.get("/api/toggles").json()["states"]["anti_slop"] is True


# ---------------------------------------------------------------- markup


def test_render_endpoint_matches_the_tokenizer(client):
    runs = client.post("/api/render", json={"text": '*She nods.* "Fine."'}).json()["runs"]
    assert runs[0] == {"text": "She nods.", "styles": ["action"]}
    assert runs[-1] == {"text": '"Fine."', "styles": ["dialogue"]}


# ---------------------------------------------------------------- events


def test_ambient_stream_delivers_bus_events(db):
    """The ambient stream is what carries a background pass's result back after
    the turn request has already been answered (§1)."""
    from app.events import BUS
    from app.main import chat_events

    async def scenario():
        request = _fake_request()
        response = await chat_events("chat-1", request)
        frames = []
        async for frame in response.body_iterator:
            frames.append(frame)
            if len(frames) == 1:
                # A background pass lands while the client is listening.
                BUS.publish("chat-1", {"type": "panel", "panel": "scene", "value": {"place": "bar"}})
            if len(frames) == 2:
                request.disconnected = True
        return frames

    frames = asyncio.run(scenario())
    events = [json.loads(f.removeprefix("data: ").strip()) for f in frames]
    assert events[0]["type"] == "connected"
    assert events[1] == {"type": "panel", "panel": "scene", "value": {"place": "bar"}}
    assert BUS.subscriber_count("chat-1") == 0, "the subscriber must be released on disconnect"


class _fake_request:
    """Minimal stand-in for starlette's Request: only is_disconnected is used."""

    disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


def test_model_discovery_lists_what_a_backend_serves(client):
    body = client.post("/api/settings/models", json={"name": "echo", "kind": "echo"}).json()
    assert body["ok"] is True
    assert body["models"] == ["echo-1"]


def test_model_discovery_fails_softly_on_an_unreachable_backend(client):
    body = client.post("/api/settings/models", json={
        "name": "dead", "kind": "ollama", "base_url": "http://127.0.0.1:1", "timeout": 2,
    }).json()
    assert body["ok"] is False
    assert body["models"] == []
    assert body["error"]


def test_model_discovery_rejects_an_invalid_backend(client):
    assert client.post("/api/settings/models", json={"name": "x", "kind": "bogus"}).status_code == 400
