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


def test_theme_saves_and_is_served_back(client, isolated_settings):
    import json as _json

    client.put("/api/settings", json={
        "backends": [{"name": "echo", "kind": "echo"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "theme": {"--accent": "#ff3366", "--radius": "4px"},
    })
    assert _json.loads(isolated_settings.read_text())["theme"] == {
        "--accent": "#ff3366", "--radius": "4px"
    }
    assert client.get("/api/settings").json()["theme"]["--accent"] == "#ff3366"


def test_settings_expose_the_theme_token_list(client):
    """The editor is generated from this, so it must always be present."""
    tokens = client.get("/api/settings").json()["theme_tokens"]
    assert tokens and all({"var", "label", "group", "type", "default"} <= set(t) for t in tokens)


def test_an_invalid_theme_value_is_rejected(client, isolated_settings):
    response = client.put("/api/settings", json={
        "backends": [{"name": "echo", "kind": "echo"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "theme": {"--accent": "url(javascript:1)"},
    })
    assert response.status_code == 400


def test_settings_list_the_available_backdrops(client):
    body = client.get("/api/settings").json()
    assert "tavern.svg" in body["backgrounds"]
    assert body["background"] in body["backgrounds"] + ["none"]


def test_backdrop_choice_persists(client, isolated_settings):
    import json as _json

    base = {
        "backends": [{"name": "echo", "kind": "echo"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
    }
    client.put("/api/settings", json={**base, "background": "none"})
    assert _json.loads(isolated_settings.read_text())["background"] == "none"

    client.put("/api/settings", json={**base, "background": "tavern.svg", "background_dim": 55})
    stored = _json.loads(isolated_settings.read_text())
    assert stored["background"] == "tavern.svg"
    assert stored["background_dim"] == 55


def test_an_unknown_backdrop_is_rejected(client, isolated_settings):
    response = client.put("/api/settings", json={
        "backends": [{"name": "echo", "kind": "echo"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "background": "../../../etc/passwd",
    })
    assert response.status_code == 400


def test_the_backdrop_is_actually_served(client):
    response = client.get("/static/backgrounds/tavern.svg")
    assert response.status_code == 200
    assert "svg" in response.headers["content-type"]


# ------------------------------------------------- characters & manual passes


def test_a_character_can_be_written_from_scratch(client):
    created = client.post("/api/characters", json={"name": "Tomas"})
    assert created.status_code == 200
    character_id = created.json()["id"]

    listed = client.get("/api/characters").json()
    assert any(c["id"] == character_id and c["name"] == "Tomas" for c in listed)

    # A blank character is usable immediately: a chat on it must open.
    chat = client.post("/api/chats", json={"character_id": character_id})
    assert chat.status_code == 200


def test_editing_a_character_keeps_what_the_editor_cannot_show(client):
    """The card carries more than the six text fields the editor exposes."""
    character_id = client.get("/api/characters").json()[0]["id"]
    before = client.get(f"/api/characters/{character_id}").json()
    assert before["pfp_set"], "fixture character should have portraits"

    response = client.put(
        f"/api/characters/{character_id}",
        json={"name": "Renamed", "persona": "Rewritten persona."},
    )
    assert response.status_code == 200

    after = client.get(f"/api/characters/{character_id}").json()
    assert after["name"] == "Renamed"
    assert after["persona"] == "Rewritten persona."
    assert after["pfp_set"] == before["pfp_set"]
    assert after["lorebook"] == before["lorebook"]
    assert after["state_schema"] == before["state_schema"]


def test_a_character_cannot_be_left_nameless(client):
    character_id = client.get("/api/characters").json()[0]["id"]
    assert client.put(f"/api/characters/{character_id}", json={"name": "   "}).status_code == 400


def test_the_character_list_carries_a_portrait_and_chat_count(client):
    character_id = client.get("/api/characters").json()[0]["id"]
    before = next(c for c in client.get("/api/characters").json() if c["id"] == character_id)
    client.post("/api/chats", json={"character_id": character_id})
    after = next(c for c in client.get("/api/characters").json() if c["id"] == character_id)
    assert after["chats"] == before["chats"] + 1
    assert after["pfp"]


def test_deleting_a_character_takes_its_chats_with_it(client):
    character_id = client.post("/api/characters", json={"name": "Doomed"}).json()["id"]
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    assert client.get(f"/api/chats/{chat_id}").status_code == 200

    assert client.delete(f"/api/characters/{character_id}").status_code == 200
    assert client.get(f"/api/chats/{chat_id}").status_code == 404


def test_a_pass_can_be_run_without_waiting_for_its_trigger(client):
    """The world-info bar refreshes on demand, not only when a reply moves it."""
    chat_id = new_chat(client)
    send(client, chat_id, "We step out into the rain.")

    response = client.post(f"/api/chats/{chat_id}/passes/scene/run")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] and body["pass_id"] == "scene" and body["run_id"]


def test_running_an_unknown_pass_is_rejected(client):
    chat_id = new_chat(client)
    send(client, chat_id, "hello")
    assert client.post(f"/api/chats/{chat_id}/passes/nonsense/run").status_code == 400


def test_a_manual_pass_needs_something_to_look_at(client):
    """A brand-new chat with no greeting has no material for a pass."""
    character_id = client.post("/api/characters", json={"name": "Silent"}).json()["id"]
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    assert client.post(f"/api/chats/{chat_id}/passes/scene/run").status_code == 400


# ------------------------------------------- cards, backdrops, impersonation


def test_an_exported_card_is_readable_as_a_sillytavern_card(client):
    """The v2 payload is the card; the v1 mirror is what older importers read."""
    character_id = client.get("/api/characters").json()[0]["id"]
    card = client.get(f"/api/characters/{character_id}/export").json()

    assert card["spec"] == "chara_card_v2"
    assert card["spec_version"] == "2.0"
    for field in ("name", "description", "personality", "scenario", "first_mes", "mes_example"):
        assert field in card, f"v1 mirror is missing {field}"
        assert card[field] == card["data"][field]
    for field in ("system_prompt", "alternate_greetings", "tags", "creator",
                  "character_version", "character_book", "extensions"):
        assert field in card["data"], f"v2 body is missing {field}"


def test_an_exported_card_imports_back_with_everything_intact(client):
    character_id = client.get("/api/characters").json()[0]["id"]
    original = client.get(f"/api/characters/{character_id}").json()
    card = client.get(f"/api/characters/{character_id}/export").json()

    imported = client.post(
        "/api/characters/import?filename=roundtrip.json",
        content=json.dumps(card).encode(),
    )
    assert imported.status_code == 200
    back = client.get(f"/api/characters/{imported.json()['id']}").json()

    assert back["name"] == original["name"]
    assert back["first_mes"] == original["first_mes"]
    assert back["persona"].startswith(original["persona"][:40])
    # The parts a plain v2 card has no field for ride in extensions.
    assert back["pfp_set"] == original["pfp_set"]
    assert len(back["lorebook"]) == len(original["lorebook"])

    # A character carrying no schema of its own uses the canonical variables,
    # and an import of one reads as exactly that — so the round trip is
    # equivalent rather than byte-identical here.
    from app.state import DEFAULT_STATE_SCHEMA

    expected = set(original["state_schema"]) or set(DEFAULT_STATE_SCHEMA)
    assert set(back["state_schema"]) == expected


def test_exporting_for_download_names_the_file(client):
    character_id = client.get("/api/characters").json()[0]["id"]
    plain = client.get(f"/api/characters/{character_id}/export")
    assert "content-disposition" not in plain.headers

    download = client.get(f"/api/characters/{character_id}/export?download=true")
    assert "attachment" in download.headers["content-disposition"]
    assert ".card.json" in download.headers["content-disposition"]


def test_a_card_name_with_slashes_cannot_escape_the_filename(client):
    from app.main import _card_filename

    assert "/" not in _card_filename("../../etc/passwd")
    assert "\\" not in _card_filename("a\\b")
    assert _card_filename("Mira of the Wreck") == "Mira_of_the_Wreck.card.json"
    assert _card_filename("???").endswith(".card.json")


PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000000020001e221bc330000000049454e44ae426082"
)


def test_a_background_can_be_uploaded_and_removed(client, tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "USER_BACKGROUND_DIR", tmp_path / "backgrounds")

    before = client.get("/api/backgrounds").json()["backgrounds"]
    assert all(not b["removable"] for b in before), "bundled art is not removable"

    upload = client.post("/api/backgrounds?filename=my photo.PNG", content=PNG_1PX)
    assert upload.status_code == 200
    name = upload.json()["name"]
    assert name == "my-photo.png", "the stem is rebuilt, not sanitised in place"

    listed = {b["name"]: b for b in client.get("/api/backgrounds").json()["backgrounds"]}
    assert listed[name]["removable"]
    assert client.get(f"/backgrounds/{name}").status_code == 200

    assert client.delete(f"/api/backgrounds/{name}").status_code == 200
    assert name not in {b["name"] for b in client.get("/api/backgrounds").json()["backgrounds"]}


def test_uploading_a_second_image_of_the_same_name_keeps_both(client, tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "USER_BACKGROUND_DIR", tmp_path / "backgrounds")
    first = client.post("/api/backgrounds?filename=tavern.png", content=PNG_1PX).json()["name"]
    second = client.post("/api/backgrounds?filename=tavern.png", content=PNG_1PX).json()["name"]
    assert first != second


def test_a_background_upload_cannot_write_outside_its_folder(client, tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "USER_BACKGROUND_DIR", tmp_path / "backgrounds")
    response = client.post("/api/backgrounds?filename=../../evil.png", content=PNG_1PX)
    assert response.status_code == 200
    assert response.json()["name"] == "evil.png"
    assert not (tmp_path / "evil.png").exists()
    assert (tmp_path / "backgrounds" / "evil.png").exists()


def test_only_image_types_we_serve_are_accepted(client, tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "USER_BACKGROUND_DIR", tmp_path / "backgrounds")
    assert client.post("/api/backgrounds?filename=x.exe", content=b"MZ").status_code == 400
    assert client.post("/api/backgrounds?filename=x.png", content=b"").status_code == 400


def test_a_bundled_background_cannot_be_deleted(client):
    assert client.delete("/api/backgrounds/tavern.svg").status_code == 404


def test_serving_an_unknown_background_is_a_404(client):
    assert client.get("/backgrounds/nope.png").status_code == 404
    assert client.get("/backgrounds/..%2F..%2Fsettings.json").status_code == 404


def test_impersonation_drafts_a_message_without_writing_one(client):
    chat_id = new_chat(client)
    send(client, chat_id, "I set the lantern down between us.")
    before = len(client.get(f"/api/chats/{chat_id}/messages").json())

    with client.stream("POST", f"/api/chats/{chat_id}/impersonate") as response:
        assert response.status_code == 200
        events = sse_events(response)

    kinds = [e["type"] for e in events]
    assert "delta" in kinds
    final = [e for e in events if e["type"] == "impersonated"]
    assert final and final[0]["text"].strip()
    assert not final[0]["text"].lower().startswith(("you:", "user:"))

    # It is a draft: nothing was added to the conversation.
    assert len(client.get(f"/api/chats/{chat_id}/messages").json()) == before


def test_impersonating_an_unknown_chat_reports_it(client):
    with client.stream("POST", "/api/chats/nope/impersonate") as response:
        events = sse_events(response)
    assert any(e["type"] == "error" for e in events)
