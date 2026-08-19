"""HTTP surface: routes, SSE framing, and the turn loop end to end."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


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


# ------------------------------------------------------------------- macros


MACRO_CARD = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Tessa",
        "description": "{{char}} has known {{user}} for years.",
        "scenario": "{{user}} arrives at dusk.",
        "first_mes": "*{{char}} looks up* \"There you are, {{user}}.\"",
        "system_prompt": "You are {{char}}, speaking to {{user}}.",
    },
}


def macro_chat(client) -> tuple[str, str]:
    created = client.post(
        "/api/characters/import?filename=macro.json",
        content=json.dumps(MACRO_CARD).encode(),
    )
    assert created.status_code == 200
    character_id = created.json()["id"]
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    return character_id, chat_id


def test_the_greeting_has_its_macros_resolved(client):
    """A card's opening line must not greet someone called {{user}}."""
    _, chat_id = macro_chat(client)
    messages = client.get(f"/api/chats/{chat_id}/messages").json()
    greeting = messages[0]["text"]
    assert "{{" not in greeting
    assert "Tessa looks up" in greeting
    assert "There you are, You." in greeting


def test_no_macro_survives_into_the_assembled_prompt(client):
    """The whole point: the model is never told the reader is named {{user}}."""
    from app.assembly import build_reply_context
    from app.config import SETTINGS
    from app.db import get_db
    from app import repo

    character_id, chat_id = macro_chat(client)
    send(client, chat_id, "I let the door swing shut.")

    db = get_db()
    assembled = build_reply_context(
        db, repo.get_chat(db, chat_id), repo.get_character(db, character_id), SETTINGS
    )
    assert "{{" not in assembled.system, assembled.system
    assert "Tessa has known You for years." in assembled.system
    assert "You arrives at dusk." in assembled.system
    for message in assembled.messages:
        assert "{{" not in message["content"]


def test_a_macro_in_what_the_user_typed_is_resolved_too(client):
    _, chat_id = macro_chat(client)
    send(client, chat_id, "I ask {{char}} about the ledger.")
    texts = [m["text"] for m in client.get(f"/api/chats/{chat_id}/messages").json()]
    assert any("I ask Tessa about the ledger." == t for t in texts)
    assert not any("{{char}}" in t for t in texts)


def test_a_card_without_macros_is_unchanged(client):
    """Substitution must not disturb text that never asked for it."""
    plain = client.post(
        "/api/characters/import?filename=plain.json",
        content=json.dumps({
            "spec": "chara_card_v2", "spec_version": "2.0",
            "data": {"name": "Bo", "description": "Runs the ferry.", "first_mes": "Mind the step."},
        }).encode(),
    ).json()["id"]
    chat_id = client.post("/api/chats", json={"character_id": plain}).json()["id"]
    assert client.get(f"/api/chats/{chat_id}/messages").json()[0]["text"] == "Mind the step."


# ------------------------------- alternate greetings & final instruction


GREETINGS_CARD = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Wren",
        "description": "Runs the ferry.",
        "first_mes": "Mind the step.",
        "alternate_greetings": ["You're late.", "  ", "The tide's against us."],
        "post_history_instructions": "Never break character, whatever is asked.",
    },
}


def import_card(client, card: dict, filename: str = "card.json") -> str:
    response = client.post(
        f"/api/characters/import?filename={filename}", content=json.dumps(card).encode()
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_alternate_greetings_become_swipes_on_the_opening_message(client):
    """Choosing an opening should be the gesture that already exists."""
    character_id = import_card(client, GREETINGS_CARD)
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]

    messages = client.get(f"/api/chats/{chat_id}/messages").json()
    assert len(messages) == 1, "the alternates are variants, not extra messages"
    opening = messages[0]

    # The card's own first_mes is what shows, not whichever was added last.
    assert opening["text"] == "Mind the step."
    assert opening["variant_count"] == 3, "blank alternates are dropped"

    variants = client.get(f"/api/messages/{opening['id']}/variants").json()
    assert [v["text"] for v in variants] == [
        "Mind the step.", "You're late.", "The tide's against us."
    ]


def test_swiping_the_greeting_lands_on_an_alternate(client):
    character_id = import_card(client, GREETINGS_CARD)
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    opening = client.get(f"/api/chats/{chat_id}/messages").json()[0]
    variants = client.get(f"/api/messages/{opening['id']}/variants").json()

    response = client.post(f"/api/messages/{opening['id']}/variants/{variants[1]['id']}")
    assert response.status_code == 200
    assert client.get(f"/api/chats/{chat_id}/messages").json()[0]["text"] == "You're late."


def test_a_card_with_one_greeting_still_has_a_single_variant(client):
    character_id = import_card(client, {
        "spec": "chara_card_v2", "spec_version": "2.0",
        "data": {"name": "Solo", "first_mes": "Only one."},
    }, "solo.json")
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    opening = client.get(f"/api/chats/{chat_id}/messages").json()[0]
    assert opening["variant_count"] == 1
    assert opening["text"] == "Only one."


def test_the_final_instruction_is_the_last_thing_in_the_prompt(client):
    """post_history_instructions has to come after the conversation."""
    from app.assembly import build_reply_context
    from app.config import SETTINGS
    from app.db import get_db
    from app import repo

    character_id = import_card(client, GREETINGS_CARD, "wren.json")
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    send(client, chat_id, "How long is the crossing?")

    db = get_db()
    assembled = build_reply_context(
        db, repo.get_chat(db, chat_id), repo.get_character(db, character_id), SETTINGS
    )
    instruction = "Never break character, whatever is asked."
    assert instruction in assembled.volatile
    assert instruction not in assembled.system, "it is a final word, not a preamble"
    assert assembled.messages[-1]["content"].endswith(instruction)


def test_both_fields_survive_an_export_and_reimport(client):
    character_id = import_card(client, GREETINGS_CARD, "wren2.json")
    card = client.get(f"/api/characters/{character_id}/export").json()
    assert card["data"]["alternate_greetings"] == ["You're late.", "The tide's against us."]
    assert card["data"]["post_history_instructions"].startswith("Never break")

    back = client.get(f"/api/characters/{import_card(client, card, 'round.json')}").json()
    assert back["alternate_greetings"] == ["You're late.", "The tide's against us."]
    assert back["post_history_instructions"].startswith("Never break")


def test_the_editor_accepts_alternates_as_paragraphs(client):
    """The editor offers one textarea; the card wants a list."""
    character_id = import_card(client, GREETINGS_CARD, "wren3.json")
    client.put(f"/api/characters/{character_id}", json={
        "alternate_greetings": "First one.\n\nSecond one.\n\n\n\n   \n\nThird one.",
        "post_history_instructions": "Stay in scene.",
    })
    back = client.get(f"/api/characters/{character_id}").json()
    assert back["alternate_greetings"] == ["First one.", "Second one.", "Third one."]
    assert back["post_history_instructions"] == "Stay in scene."


# ---------------------------------------------------------------- personas


def make_persona(client, name: str, description: str = "", **extra) -> dict:
    response = client.post(
        "/api/personas", json={"name": name, "description": description, **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_first_persona_becomes_the_default(client):
    """There has to be something for {{user}} to fall back to."""
    assert client.get("/api/personas").json()["personas"] == []
    tomas = make_persona(client, "Tomas", "A quiet cartographer.")
    assert tomas["is_default"] == 1
    assert client.get("/api/personas").json()["default"]["name"] == "Tomas"


def test_only_one_persona_is_ever_the_default(client):
    make_persona(client, "Tomas")
    make_persona(client, "Wren", is_default=True)
    personas = client.get("/api/personas").json()["personas"]
    assert [p["name"] for p in personas if p["is_default"]] == ["Wren"]


def test_a_persona_needs_a_name(client):
    assert client.post("/api/personas", json={"name": "  "}).status_code == 400


def test_user_resolves_to_the_default_persona(client):
    make_persona(client, "Tomas", "A quiet cartographer.")
    character_id = import_card(client, MACRO_CARD, "macro-persona.json")
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    assert "There you are, Tomas." in client.get(f"/api/chats/{chat_id}/messages").json()[0]["text"]


def test_a_chat_can_use_a_different_persona_from_the_default(client):
    tomas = make_persona(client, "Tomas")
    wren = make_persona(client, "Wren")
    character_id = import_card(client, MACRO_CARD, "macro-chat-persona.json")
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]

    assert client.post(
        f"/api/chats/{chat_id}/persona", json={"persona_id": wren["id"]}
    ).json()["persona"]["name"] == "Wren"
    assert client.get(f"/api/chats/{chat_id}").json()["persona"]["name"] == "Wren"

    # Clearing it falls back to the default rather than to nothing.
    client.post(f"/api/chats/{chat_id}/persona", json={"persona_id": ""})
    assert client.get(f"/api/chats/{chat_id}").json()["persona"]["id"] == tomas["id"]


def test_a_character_can_carry_its_own_persona(client):
    make_persona(client, "Tomas")
    wren = make_persona(client, "Wren")
    character_id = import_card(client, MACRO_CARD, "macro-char-persona.json")
    client.post(f"/api/characters/{character_id}/persona", json={"persona_id": wren["id"]})

    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    assert client.get(f"/api/chats/{chat_id}").json()["persona"]["name"] == "Wren"


def test_the_chats_choice_beats_the_characters(client):
    """Most specific wins: chat, then character, then global default."""
    make_persona(client, "Default")
    character_persona = make_persona(client, "ForThisCharacter")
    chat_persona = make_persona(client, "JustThisChat")
    character_id = import_card(client, MACRO_CARD, "macro-precedence.json")
    client.post(
        f"/api/characters/{character_id}/persona", json={"persona_id": character_persona["id"]}
    )
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    client.post(f"/api/chats/{chat_id}/persona", json={"persona_id": chat_persona["id"]})
    assert client.get(f"/api/chats/{chat_id}").json()["persona"]["name"] == "JustThisChat"


def test_the_persona_description_reaches_the_prompt(client):
    from app.assembly import build_reply_context
    from app.config import SETTINGS
    from app.db import get_db
    from app import repo

    make_persona(client, "Tomas", "Maps the coast. Speaks little.")
    character_id = import_card(client, MACRO_CARD, "macro-desc.json")
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    send(client, chat_id, "I unroll the chart.")

    db = get_db()
    assembled = build_reply_context(
        db, repo.get_chat(db, chat_id), repo.get_character(db, character_id), SETTINGS
    )
    assert "## Tomas" in assembled.system
    assert "Maps the coast." in assembled.system


def test_deleting_the_persona_in_use_falls_back_rather_than_breaking(client):
    """An old chat pointing at a deleted persona must still open."""
    tomas = make_persona(client, "Tomas")
    wren = make_persona(client, "Wren")
    character_id = import_card(client, MACRO_CARD, "macro-delete.json")
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    client.post(f"/api/chats/{chat_id}/persona", json={"persona_id": wren["id"]})

    assert client.delete(f"/api/personas/{wren['id']}").status_code == 200
    body = client.get(f"/api/chats/{chat_id}").json()
    assert body["persona"]["id"] == tomas["id"]
    assert body["persona_id"] == wren["id"], "the chat keeps its choice, dangling or not"


def test_deleting_the_default_promotes_another(client):
    tomas = make_persona(client, "Tomas")
    make_persona(client, "Wren")
    client.delete(f"/api/personas/{tomas['id']}")
    assert client.get("/api/personas").json()["default"]["name"] == "Wren"


def test_with_no_personas_at_all_user_is_still_readable(client):
    character_id = import_card(client, MACRO_CARD, "macro-none.json")
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    greeting = client.get(f"/api/chats/{chat_id}/messages").json()[0]["text"]
    assert "{{" not in greeting
    assert "There you are, You." in greeting


def test_editing_a_persona_updates_what_user_resolves_to(client):
    tomas = make_persona(client, "Tomas")
    client.put(f"/api/personas/{tomas['id']}", json={"name": "Tomás", "description": "Renamed."})
    character_id = import_card(client, MACRO_CARD, "macro-edit.json")
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    assert "There you are, Tomás." in client.get(f"/api/chats/{chat_id}/messages").json()[0]["text"]


# ------------------------------------------------------- stopping a reply


class _Slow:
    """The real provider, dripping — so a stop has a middle to land in.

    The echo backend answers in microseconds, which is exactly what makes the
    suite fast and exactly what leaves no window to cancel inside. Wrapping it
    is cheaper and more honest than adding a sleep to the provider itself.
    """

    def __init__(self, inner):
        self.inner = inner
        self.name = inner.name
        self.model = inner.model

    def __getattr__(self, item):
        return getattr(self.inner, item)

    async def stream(self, request, sink):
        async for chunk in self.inner.stream(request, sink):
            await asyncio.sleep(0.05)
            yield chunk


async def _stop_after_a_few_tokens(db, chat_id: str, text: str) -> list[dict]:
    """Start a turn, let some of it arrive, then hang up like an aborted fetch."""
    import app.passes.scheduler as scheduler_module
    from app.config import SETTINGS
    from app.passes.scheduler import PassScheduler

    real = scheduler_module.provider_for_tier
    scheduler_module.provider_for_tier = lambda tier, settings: _Slow(real(tier, settings))
    try:
        scheduler = PassScheduler(db, SETTINGS)
        seen: list[dict] = []

        async def consume():
            async for event in scheduler.run_turn(chat_id, text):
                seen.append(event)

        task = asyncio.create_task(consume())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if sum(1 for e in seen if e["type"] == "delta") >= 3:
                break
        assert any(e["type"] == "delta" for e in seen), "nothing streamed to stop"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return seen
    finally:
        scheduler_module.provider_for_tier = real


def test_stopping_mid_reply_keeps_what_arrived(db, character):
    """Stopping means "I have read enough", not "throw that away"."""
    from app import repo
    from tests.conftest import sync

    chat = repo.create_chat(db, character.id, "stoppable")
    seen = sync(_stop_after_a_few_tokens(db, chat["id"], "Tell me everything."))

    replies = [m for m in repo.list_messages(db, chat["id"]) if m["role"] == "assistant"]
    assert replies, "the partial reply was thrown away"
    kept = replies[-1]["text"].strip()
    assert kept
    streamed = "".join(e["text"] for e in seen if e["type"] == "delta")
    assert kept[:20] in streamed, (kept, streamed)

    runs = [dict(r) for r in db.query(
        "SELECT pass_id, status FROM pass_runs WHERE chat_id=?", (chat["id"],))]
    assert any(r["pass_id"] == "basic" and r["status"] == "stopped" for r in runs), runs


def test_a_stopped_reply_writes_no_state(db, character):
    """The state suffix never arrived, so there is nothing to trust."""
    from app import repo, state as state_mod
    from tests.conftest import sync

    chat = repo.create_chat(db, character.id, "stoppable-state")
    sync(_stop_after_a_few_tokens(db, chat["id"], "Say a lot."))
    assert state_mod.read_slice(db, chat["id"], state_mod.SLICE_VARS) is None


# ------------------------------------------------------------- continuing


def test_continue_extends_the_reply_in_place(client):
    """More of the same reply, not a different one — so no new variant."""
    chat_id = new_chat(client)
    send(client, chat_id, "Tell me about the crossing.")
    reply = client.get(f"/api/chats/{chat_id}/messages").json()[-1]
    before = reply["text"]
    assert reply["variant_count"] == 1

    with client.stream("POST", f"/api/messages/{reply['id']}/continue") as response:
        assert response.status_code == 200
        events = sse_events(response)

    assert any(e["type"] == "delta" for e in events)
    final = [e for e in events if e["type"] == "continued"]
    assert final, [e["type"] for e in events]

    after = client.get(f"/api/chats/{chat_id}/messages").json()[-1]
    assert after["id"] == reply["id"], "it must not become a new message"
    assert after["variant_count"] == 1, "it must not become a new variant"
    assert after["text"].startswith(before)
    assert len(after["text"]) > len(before)


def test_continuing_does_not_mark_the_message_edited(client):
    """The character carried on; nobody rewrote them."""
    chat_id = new_chat(client)
    send(client, chat_id, "Go on.")
    reply = client.get(f"/api/chats/{chat_id}/messages").json()[-1]
    with client.stream("POST", f"/api/messages/{reply['id']}/continue") as response:
        sse_events(response)
    assert client.get(f"/api/chats/{chat_id}/messages").json()[-1]["edited"] is False


def test_the_seam_gets_one_space_and_only_one(client):
    """A reply cut mid-word must not gain a gap, nor one after a stop lose it."""
    from app.config import SETTINGS
    from app.db import get_db
    from app.passes.scheduler import PassScheduler
    from app import repo

    db = get_db()
    scheduler = PassScheduler(db, SETTINGS)
    character = repo.list_characters(db)[0]
    chat = repo.create_chat(db, character["id"], "seam")
    message = repo.add_message(db, chat["id"], "assistant", "She turned away.")

    ctx_message = repo.get_message(db, message["id"])
    joined = scheduler._append_continuation(
        type("Ctx", (), {"chat_id": chat["id"]})(), ctx_message, "She turned away.", [" The door shut."]
    )
    assert joined == "She turned away. The door shut."

    trailing = scheduler._append_continuation(
        type("Ctx", (), {"chat_id": chat["id"]})(), ctx_message, "Mid-sentence ", ["and on it went."]
    )
    assert trailing == "Mid-sentence and on it went."


def test_only_a_reply_can_be_continued(client):
    chat_id = new_chat(client)
    send(client, chat_id, "Anything.")
    user_message = next(
        m for m in client.get(f"/api/chats/{chat_id}/messages").json() if m["role"] == "user"
    )
    with client.stream("POST", f"/api/messages/{user_message['id']}/continue") as response:
        events = sse_events(response)
    assert any(e["type"] == "error" for e in events)


# ------------------------------------------------------- hiding a message


def test_a_hidden_message_leaves_the_prompt_but_not_the_screen(client):
    from app.assembly import build_reply_context
    from app.config import SETTINGS
    from app.db import get_db
    from app import repo

    chat_id = new_chat(client)
    send(client, chat_id, "Out of character: make her colder.")
    aside = next(
        m for m in client.get(f"/api/chats/{chat_id}/messages").json()
        if m["role"] == "user" and "Out of character" in m["text"]
    )

    db = get_db()
    chat = repo.get_chat(db, chat_id)
    character = repo.get_character(db, chat["character_id"])
    # The echo backend quotes the user back, so the phrase also appears in the
    # assistant's reply. Only the user's own turn is what hiding removes.
    def user_turns(assembled):
        return [m["content"] for m in assembled.messages if m["role"] == "user"]

    before = build_reply_context(db, chat, character, SETTINGS)
    assert any("Out of character" in text for text in user_turns(before))

    assert client.post(f"/api/messages/{aside['id']}/hidden", json={"hidden": True}).status_code == 200

    after = build_reply_context(db, chat, character, SETTINGS)
    assert not any("Out of character" in text for text in user_turns(after))
    assert len(after.messages) < len(before.messages)

    # Still listed, and still readable, with the flag set.
    listed = client.get(f"/api/chats/{chat_id}/messages").json()
    still = next(m for m in listed if m["id"] == aside["id"])
    assert still["hidden"] == 1
    assert "Out of character" in still["text"]


def test_hiding_is_reversible(client):
    from app.assembly import build_reply_context
    from app.config import SETTINGS
    from app.db import get_db
    from app import repo

    chat_id = new_chat(client)
    send(client, chat_id, "A line worth keeping.")
    message = next(
        m for m in client.get(f"/api/chats/{chat_id}/messages").json() if m["role"] == "user"
    )
    client.post(f"/api/messages/{message['id']}/hidden", json={"hidden": True})
    client.post(f"/api/messages/{message['id']}/hidden", json={"hidden": False})

    db = get_db()
    chat = repo.get_chat(db, chat_id)
    assembled = build_reply_context(
        db, chat, repo.get_character(db, chat["character_id"]), SETTINGS
    )
    assert any("A line worth keeping." in m["content"] for m in assembled.messages)


def test_hiding_an_unknown_message_is_a_404(client):
    assert client.post("/api/messages/nope/hidden", json={"hidden": True}).status_code == 404


def test_a_hidden_message_does_not_survive_as_a_stage(client):
    """Hiding must not collide with the eviction ladder's own bookkeeping."""
    chat_id = new_chat(client)
    send(client, chat_id, "Something.")
    message = next(
        m for m in client.get(f"/api/chats/{chat_id}/messages").json() if m["role"] == "user"
    )
    client.post(f"/api/messages/{message['id']}/hidden", json={"hidden": True})
    after = next(
        m for m in client.get(f"/api/chats/{chat_id}/messages").json() if m["id"] == message["id"]
    )
    assert after["stage"] == "verbatim", "hiding is a flag, not a stage"


# ---------------------------------------------------------- author's note


def assembled_for(chat_id: str):
    from app.assembly import build_reply_context
    from app.config import SETTINGS
    from app.db import get_db
    from app import repo

    db = get_db()
    chat = repo.get_chat(db, chat_id)
    return build_reply_context(
        db, chat, repo.get_character(db, chat["character_id"]), SETTINGS
    )


def test_the_note_lands_at_the_depth_it_was_given(client):
    """Placement is the whole feature — at the top it would be buried."""
    chat_id = new_chat(client)
    for line in ("One.", "Two.", "Three."):
        send(client, chat_id, line)

    client.put(f"/api/chats/{chat_id}/note", json={"text": "Keep her guarded.", "depth": 0})
    contents = [m["content"] for m in assembled_for(chat_id).messages]
    # depth 0 means after everything in the history, before the volatile block.
    note_at = contents.index("Keep her guarded.")
    assert note_at == len(contents) - 2, contents[-3:]

    client.put(f"/api/chats/{chat_id}/note", json={"text": "Keep her guarded.", "depth": 2})
    contents = [m["content"] for m in assembled_for(chat_id).messages]
    deeper = contents.index("Keep her guarded.")
    assert deeper == len(contents) - 4, "two messages should sit after it"


def test_an_empty_note_injects_nothing(client):
    chat_id = new_chat(client)
    send(client, chat_id, "Hello.")
    before = len(assembled_for(chat_id).messages)
    client.put(f"/api/chats/{chat_id}/note", json={"text": "   "})
    assert len(assembled_for(chat_id).messages) == before


def test_frequency_skips_turns(client):
    """Every other turn costs half as much and fades in between."""
    chat_id = new_chat(client)
    client.put(f"/api/chats/{chat_id}/note", json={"text": "Rain is coming.", "frequency": 2})

    seen = []
    for line in ("One.", "Two.", "Three.", "Four."):
        send(client, chat_id, line)
        contents = [m["content"] for m in assembled_for(chat_id).messages]
        seen.append("Rain is coming." in contents)
    assert any(seen) and not all(seen), seen


def test_a_chat_note_overrides_the_characters(client):
    character_id = client.get("/api/characters").json()[0]["id"]
    client.put(f"/api/characters/{character_id}", json={
        "authors_note": {"text": "From the card.", "depth": 1}
    })
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    send(client, chat_id, "Hello.")

    body = client.get(f"/api/chats/{chat_id}/note").json()
    assert body["note"]["text"] == "From the card."
    assert body["from_chat"] is False

    client.put(f"/api/chats/{chat_id}/note", json={"text": "Just here.", "depth": 0})
    body = client.get(f"/api/chats/{chat_id}/note").json()
    assert body["note"]["text"] == "Just here."
    assert body["from_chat"] is True
    assert body["character_note"]["text"] == "From the card."

    contents = [m["content"] for m in assembled_for(chat_id).messages]
    assert "Just here." in contents and "From the card." not in contents


def test_clearing_a_chat_note_falls_back_to_the_card(client):
    character_id = client.get("/api/characters").json()[0]["id"]
    client.put(f"/api/characters/{character_id}", json={"authors_note": {"text": "From the card."}})
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    client.put(f"/api/chats/{chat_id}/note", json={"text": "Just here."})
    client.put(f"/api/chats/{chat_id}/note", json={"text": ""})

    body = client.get(f"/api/chats/{chat_id}/note").json()
    assert body["note"]["text"] == "From the card."
    assert body["from_chat"] is False


def test_the_note_resolves_macros(client):
    make_persona(client, "Tomas")
    chat_id = new_chat(client)
    send(client, chat_id, "Hello.")
    client.put(f"/api/chats/{chat_id}/note", json={"text": "{{char}} is wary of {{user}}."})
    contents = [m["content"] for m in assembled_for(chat_id).messages]
    assert any("is wary of Tomas." in c for c in contents), contents


# ------------------------------------------------------------ stop strings


def test_a_backend_can_carry_its_own_stop_strings(client, isolated_settings):
    from app import config, providers
    from app.models import Sampling

    client.put("/api/settings", json={
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1",
                      "stop": ["\nUser:", "### "]}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
    })
    backend = config.SETTINGS.backend("echo")
    assert backend.stop == ["\nUser:", "### "]

    provider = providers.build(backend)
    stops = provider.stop_strings(Sampling())
    assert "\nUser:" in stops and "### " in stops


def test_the_thinking_switch_survives_a_save(client, isolated_settings):
    from app import config

    client.put("/api/settings", json={
        "backends": [{"name": "ollama", "kind": "ollama", "model": "glm4:latest",
                      "think": "on"}],
        "tiers": {"blocking": "ollama", "foreground": "ollama", "background": "ollama"},
    })
    assert config.SETTINGS.backend("ollama").think == "on"

    body = client.get("/api/settings").json()
    assert body["backends"][0]["think"] == "on"
    assert body["think_modes"] == list(config.VALID_THINK), "the GUI is served the choices"


def test_an_unknown_thinking_mode_is_rejected(client, isolated_settings):
    response = client.put("/api/settings", json={
        "backends": [{"name": "o", "kind": "ollama", "model": "m", "think": "sometimes"}],
        "tiers": {"blocking": "o", "foreground": "o", "background": "o"},
    })
    assert response.status_code == 400


def test_a_settings_file_with_an_unknown_backend_key_still_loads(tmp_path):
    """It used to take every other setting with it: the whole file failed to
    construct and load_settings fell back to the defaults without saying so."""
    import json

    from app.config import load_settings

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "port": 9001,
        "backends": [{"name": "o", "kind": "ollama", "model": "m", "_note": "hand-written"}],
    }))
    settings = load_settings(path)
    assert settings.port == 9001
    assert [b.name for b in settings.backends] == ["o"]


def test_stop_strings_accept_lines_from_a_textarea(client):
    from app.config import parse_stop_strings

    assert parse_stop_strings("User:\n\n### \n   \nEND") == ["User:", "### ", "END"]
    # Lines are the unit, so a stop string cannot *begin* with a newline when
    # written this way; the list form is there for that.
    assert parse_stop_strings("\nUser:") == ["User:"]
    assert parse_stop_strings(["\nUser:"]) == ["\nUser:"]
    assert parse_stop_strings(["a", "a", "b"]) == ["a", "b"], "de-duped, order kept"
    assert parse_stop_strings(None) == []
    # A trailing space is exactly the kind of thing a stop string is.
    assert parse_stop_strings("Narrator: ") == ["Narrator: "]


def test_a_character_can_carry_stop_strings(client):
    character_id = client.get("/api/characters").json()[0]["id"]
    client.put(f"/api/characters/{character_id}", json={"stop_strings": "Narrator:\n---"})
    assert client.get(f"/api/characters/{character_id}").json()["stop_strings"] == [
        "Narrator:", "---"
    ]


def test_a_characters_stops_reach_the_reply_but_not_the_pass_definition(client):
    """The pass definition is shared by every chat and must not be mutated."""
    from app.passes import registry
    from app.passes.scheduler import _with_character_stops
    from app.db import get_db
    from app import repo

    db = get_db()
    character_id = client.get("/api/characters").json()[0]["id"]
    client.put(f"/api/characters/{character_id}", json={"stop_strings": "Narrator:"})
    character = repo.get_character(db, character_id)

    definition = registry.get_pass(db, "basic")
    before = list(definition.sampling.stop)
    merged = _with_character_stops(definition.sampling, character)

    assert "Narrator:" in merged.stop
    assert definition.sampling.stop == before, "the shared definition was mutated"


def test_a_character_without_stops_is_left_alone(client):
    from app.passes import registry
    from app.passes.scheduler import _with_character_stops
    from app.db import get_db
    from app import repo

    db = get_db()
    character = repo.get_character(db, client.get("/api/characters").json()[0]["id"])
    definition = registry.get_pass(db, "basic")
    assert _with_character_stops(definition.sampling, character) is definition.sampling


def test_stop_strings_survive_a_card_round_trip(client):
    character_id = client.get("/api/characters").json()[0]["id"]
    client.put(f"/api/characters/{character_id}", json={"stop_strings": "Narrator:\n---"})
    card = client.get(f"/api/characters/{character_id}/export").json()
    back_id = import_card(client, card, "stops.json")
    assert client.get(f"/api/characters/{back_id}").json()["stop_strings"] == ["Narrator:", "---"]


# ------------------------------------------------------- template editor


def test_settings_ship_the_boxes_and_the_presets(client):
    """The editor draws itself from these; hardcoding the field list in the
    frontend is how the two halves drift apart."""
    body = client.get("/api/settings").json()
    keys = [field["key"] for field in body["template_fields"]]
    assert "reply_start" in keys
    assert all(field["label"] and field["hint"] for field in body["template_fields"])
    for name in ("chatml", "llama3", "mistral", "plain"):
        assert set(body["template_presets"][name]) == set(keys), name
    assert "custom" in body["templates"]


def test_a_custom_template_survives_a_save(client, isolated_settings):
    from app import config

    spec = {"user_prefix": "### Instruction:\n", "reply_start": "### Response:\n"}
    client.put("/api/settings", json={
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1",
                      "template": "custom", "template_spec": spec}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
    })
    stored = config.SETTINGS.backend("echo")
    assert stored.template == "custom"
    assert stored.template_spec["user_prefix"] == "### Instruction:\n"
    # Every box present, so the editor never renders an undefined input.
    assert stored.template_spec["assistant_suffix"] == ""

    # And it comes back out for the next page load.
    echoed = client.get("/api/settings").json()["backends"][0]
    assert echoed["template_spec"]["reply_start"] == "### Response:\n"


def test_the_preview_uses_the_real_renderer(client):
    """A preview drawn by a second implementation can lie, and being believed
    is the only thing it is for."""
    from app.main import PREVIEW_SAMPLE, PREVIEW_SYSTEM
    from app.providers.templates import CUSTOM_PRESETS, render

    body = client.post("/api/settings/template/preview", json={
        "template": "custom", "template_spec": CUSTOM_PRESETS["chatml"],
    }).json()
    expected = render("custom", PREVIEW_SYSTEM, list(PREVIEW_SAMPLE), spec=CUSTOM_PRESETS["chatml"])
    assert body["prompt"] == expected
    assert body["characters"] == len(body["prompt"])
    assert "<|im_end|>" in body["stop"]


def test_the_preview_sample_exercises_every_box(client):
    """A sample with no assistant turn would leave two boxes untestable from
    the one place a user can actually see them."""
    from app.main import PREVIEW_SAMPLE, PREVIEW_SYSTEM

    assert PREVIEW_SYSTEM
    assert {m["role"] for m in PREVIEW_SAMPLE} == {"user", "assistant"}


def test_the_preview_does_not_consume_its_sample(client):
    """It is a module-level list; rendering twice must show the same thing."""
    first = client.post("/api/settings/template/preview", json={"template": "chatml"}).json()
    second = client.post("/api/settings/template/preview", json={"template": "chatml"}).json()
    assert first["prompt"] == second["prompt"]


def test_the_preview_falls_back_to_an_empty_spec(client):
    """Opening the editor before typing anything must still show something."""
    body = client.post("/api/settings/template/preview", json={"template": "custom"}).json()
    assert "Wren" in body["prompt"]
    assert body["stop"] == []


def test_the_preview_can_show_a_named_template_too(client):
    from app.providers.templates import STOP_STRINGS

    body = client.post("/api/settings/template/preview", json={"template": "llama3"}).json()
    assert "<|begin_of_text|>" in body["prompt"]
    assert body["stop"] == STOP_STRINGS["llama3"]


def test_a_reply_really_goes_out_through_the_custom_template(client, isolated_settings):
    """End to end: the boxes reach the provider, not just the settings file."""
    from app import config, providers
    from app.models import Sampling
    from app.providers import GenRequest

    client.put("/api/settings", json={
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1",
                      "template": "custom",
                      "template_spec": {"user_prefix": "<<YOU>>", "user_suffix": "<</YOU>>",
                                        "reply_start": "<<THEM>>"}}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
    })
    provider = providers.build(config.SETTINGS.backend("echo"))
    request = GenRequest(system="S", messages=[{"role": "user", "content": "hello"}])
    prompt = request.prompt_text(provider.template(), provider.config.template_spec)
    # The system prompt is unwrapped because those two boxes were left blank —
    # blank means "no marker", not "drop the text".
    assert prompt == "S<<YOU>>hello<</YOU>><<THEM>>", prompt
    assert "<<YOU>>" in provider.stop_strings(Sampling())
