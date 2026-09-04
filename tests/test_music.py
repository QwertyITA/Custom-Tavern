"""Music controls (ROADMAP #39): the shared library, the manual pick, the
music_select action_card proposal and its three answers, and the one-shot
"just roleplay" nudge."""

from __future__ import annotations

from app import assembly, config, state as state_mod
from app.config import Settings
from app.passes import registry
from app.state import SLICE_MUSIC, SLICE_MUSIC_ROLEPLAY, read_slice

from .conftest import sync
from .test_scheduler import context

TRACK_BYTES = b"not really audio, just bytes with the right extension"


# ------------------------------------------------------------ music_title


def test_music_title_prefers_the_label():
    assert config.music_title("song.mp3", {"song.mp3": {"label": "Evening Waltz"}}) == "Evening Waltz"


def test_music_title_falls_back_to_the_stem_with_no_label():
    assert config.music_title("my song.mp3", {}) == "my song"
    assert config.music_title("my song.mp3", None) == "my song"


def test_validate_music_meta_keeps_the_label(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_MUSIC_DIR", tmp_path / "music")
    config.USER_MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    (config.USER_MUSIC_DIR / "song.mp3").write_bytes(TRACK_BYTES)

    cleaned = config.validate_music_meta({"song.mp3": {"label": "  Evening Waltz  "}})
    assert cleaned == {"song.mp3": {"label": "Evening Waltz"}}


# --------------------------------------------------------------- library


def test_a_track_can_be_uploaded_listed_and_removed(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_MUSIC_DIR", tmp_path / "music")

    assert client.get("/api/music").json()["tracks"] == []

    upload = client.post("/api/music?filename=my song.MP3", content=TRACK_BYTES)
    assert upload.status_code == 200
    name = upload.json()["name"]
    assert name == "my-song.mp3", "the stem is rebuilt, not sanitised in place"

    listed = {t["name"]: t for t in client.get("/api/music").json()["tracks"]}
    assert listed[name]["removable"]
    assert client.get(f"/music/{name}").status_code == 200

    assert client.delete(f"/api/music/{name}").status_code == 200
    assert name not in {t["name"] for t in client.get("/api/music").json()["tracks"]}


def test_only_audio_types_we_serve_are_accepted(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_MUSIC_DIR", tmp_path / "music")
    assert client.post("/api/music?filename=x.exe", content=b"MZ").status_code == 400
    assert client.post("/api/music?filename=x.mp3", content=b"").status_code == 400


def test_an_oversized_track_is_rejected_before_reading_the_body(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_MUSIC_DIR", tmp_path / "music")
    response = client.post(
        "/api/music?filename=x.mp3",
        content=TRACK_BYTES,
        headers={"content-length": str(config.MAX_MUSIC_BYTES + 1)},
    )
    assert response.status_code == 400


def test_deleting_a_track_drops_its_meta(client, tmp_path, monkeypatch, isolated_settings):
    monkeypatch.setattr(config, "USER_MUSIC_DIR", tmp_path / "music")
    name = client.post("/api/music?filename=x.mp3", content=TRACK_BYTES).json()["name"]
    assert client.put(
        "/api/settings",
        json={
            "backends": [{"name": "echo", "kind": "echo"}],
            "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
            "music_meta": {name: {"description": "A slow waltz."}},
        },
    ).status_code == 200
    assert client.delete(f"/api/music/{name}").status_code == 200
    assert name not in config.SETTINGS.music_meta


def test_serving_an_unknown_track_is_a_404(client):
    assert client.get("/music/nope.mp3").status_code == 404
    assert client.delete("/api/music/nope.mp3").status_code == 404


# ------------------------------------------------------- chat-scoped API


def _seed_track(tmp_path, monkeypatch, name="song.mp3") -> str:
    monkeypatch.setattr(config, "USER_MUSIC_DIR", tmp_path / "music")
    config.USER_MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    (config.USER_MUSIC_DIR / name).write_bytes(TRACK_BYTES)
    return name


def test_picking_a_track_by_hand_skips_straight_to_playing(client, chat, tmp_path, monkeypatch):
    name = _seed_track(tmp_path, monkeypatch)
    response = client.post(f"/api/chats/{chat['id']}/music", json={"track": name})
    assert response.status_code == 200
    music = response.json()["music"]
    assert music == {"status": "playing", "track": name, "character": None}


def test_picking_an_unknown_track_is_a_404(client, chat):
    assert client.post(f"/api/chats/{chat['id']}/music", json={"track": "nope.mp3"}).status_code == 404


def _propose(db, chat_id: str, track: str, character: str = "Mira") -> None:
    sync(state_mod.write_slice(
        db, chat_id, SLICE_MUSIC,
        {"status": "proposed", "track": track, "character": character},
        source_turn=1, source_pass="music_select",
    ))


def test_respond_allow_starts_the_proposed_track(client, db, chat, tmp_path, monkeypatch):
    name = _seed_track(tmp_path, monkeypatch)
    _propose(db, chat["id"], name)

    response = client.post(f"/api/chats/{chat['id']}/music/respond", json={"choice": "allow"})
    assert response.status_code == 200
    assert response.json()["music"] == {"status": "playing", "track": name, "character": "Mira"}


def test_respond_decline_clears_the_card_and_plays_nothing(client, db, chat, tmp_path, monkeypatch):
    name = _seed_track(tmp_path, monkeypatch)
    _propose(db, chat["id"], name)

    response = client.post(f"/api/chats/{chat['id']}/music/respond", json={"choice": "decline"})
    assert response.status_code == 200
    assert response.json()["music"] == {"status": "none", "track": None, "character": None}
    assert read_slice(db, chat["id"], SLICE_MUSIC_ROLEPLAY) is None


def test_respond_roleplay_plays_nothing_but_leaves_a_one_shot_note(
    client, db, chat, tmp_path, monkeypatch
):
    name = _seed_track(tmp_path, monkeypatch)
    _propose(db, chat["id"], name)

    response = client.post(f"/api/chats/{chat['id']}/music/respond", json={"choice": "roleplay"})
    assert response.status_code == 200
    assert response.json()["music"] == {"status": "none", "track": None, "character": None}

    note = read_slice(db, chat["id"], SLICE_MUSIC_ROLEPLAY)
    assert note is not None
    assert note["value"]["used"] is False
    assert "Mira" in note["value"]["note"]
    # The title (extension-stripped filename, with no label set), not the
    # raw filename — same "nobody needs to read a file format" reasoning
    # as the card and the "Currently playing" line.
    assert "song" in note["value"]["note"]
    assert name not in note["value"]["note"]


def test_respond_is_a_no_op_when_nothing_is_proposed(client, chat):
    response = client.post(f"/api/chats/{chat['id']}/music/respond", json={"choice": "allow"})
    assert response.status_code == 200
    assert response.json()["music"] == {"status": "none", "track": None, "character": None}


def test_respond_rejects_an_unknown_choice(client, chat):
    assert client.post(
        f"/api/chats/{chat['id']}/music/respond", json={"choice": "sure whatever"}
    ).status_code == 400


def test_ended_clears_a_playing_track(client, chat, tmp_path, monkeypatch):
    name = _seed_track(tmp_path, monkeypatch)
    client.post(f"/api/chats/{chat['id']}/music", json={"track": name})

    response = client.post(f"/api/chats/{chat['id']}/music/ended")
    assert response.status_code == 200
    assert response.json()["music"] == {"status": "none", "track": None, "character": None}


def test_ended_is_a_no_op_when_nothing_is_playing(client, chat):
    response = client.post(f"/api/chats/{chat['id']}/music/ended")
    assert response.status_code == 200
    assert response.json()["music"]["status"] == "none"


# ------------------------------------------------------------- music_select


def test_music_select_fires_when_the_story_mentions_a_music_source(
    sched, chat, character, tmp_path, monkeypatch
):
    name = _seed_track(tmp_path, monkeypatch)
    monkeypatch.setattr(sched.settings, "music_meta", {})

    async def scenario():
        ctx = context(chat, character, user_text="I walk over and switch on the jukebox.")
        launched = sched._launch_background(ctx)
        assert "music_select" in launched
        await sched.await_pending(chat["id"])

    sync(scenario())
    row = sched.db.query_one(
        "SELECT status FROM pass_runs WHERE chat_id=? AND pass_id='music_select'",
        (chat["id"],),
    )
    assert row["status"] == "done"
    value = read_slice(sched.db, chat["id"], SLICE_MUSIC)["value"]
    assert value == {"status": "proposed", "track": name, "character": character.name}


def test_music_select_fires_on_a_verb_near_music_too(sched, chat, character):
    """Not just the object names — "puts on some music" etc. (§ the
    trigger's own pattern, registry.py)."""
    definition = next(d for d in registry.all_passes(sched.db) if d.id == "music_select")
    ctx = context(chat, character, reply_text="She puts on some music before sitting down.")
    assert sched.trigger_fires(definition, ctx)


def test_music_select_does_not_fire_on_an_unrelated_turn(sched, chat, character, tmp_path, monkeypatch):
    _seed_track(tmp_path, monkeypatch)
    definition = next(d for d in registry.all_passes(sched.db) if d.id == "music_select")
    ctx = context(chat, character, user_text="Tell me about the weather outside.")
    assert not sched.trigger_fires(definition, ctx)


def test_music_select_excludes_a_track_marked_auto_false(sched, chat, character, tmp_path, monkeypatch):
    name = _seed_track(tmp_path, monkeypatch)
    monkeypatch.setattr(sched.settings, "music_meta", {name: {"auto": False}})
    definition = next(d for d in registry.all_passes(sched.db) if d.id == "music_select")

    task, messages, handler = sched._build_pass_input(context(chat, character), definition)
    body = (task + " " + " ".join(m["content"] for m in messages)) if handler else ""
    assert name not in body


def test_music_select_makes_no_proposal_on_an_invalid_pick(sched, chat, character, tmp_path, monkeypatch):
    from app.providers import echo as echo_provider

    _seed_track(tmp_path, monkeypatch)
    monkeypatch.setattr(sched.settings, "music_meta", {})
    monkeypatch.setattr(echo_provider, "_first_music_id", lambda request: "not-a-real-track")
    definition = next(d for d in registry.all_passes(sched.db) if d.id == "music_select")

    async def scenario():
        ctx = context(chat, character, user_text="I switch on the jukebox.")
        launched = sched._launch_background(ctx)
        assert "music_select" in launched
        await sched.await_pending(chat["id"])

    sync(scenario())
    row = sched.db.query_one(
        "SELECT status FROM pass_runs WHERE chat_id=? AND pass_id='music_select'",
        (chat["id"],),
    )
    assert row["status"] == "stale"
    assert read_slice(sched.db, chat["id"], SLICE_MUSIC) is None


def test_music_select_re_validates_against_a_fresh_library_read(
    sched, chat, character, tmp_path, monkeypatch
):
    """Same anti-race guard as expression/background_swap: a track excluded
    after the prompt was built this same turn must not be proposed."""
    from app.providers import echo as echo_provider

    name = _seed_track(tmp_path, monkeypatch)
    monkeypatch.setattr(sched.settings, "music_meta", {})
    monkeypatch.setattr(echo_provider, "_first_music_id", lambda request: name)
    definition = next(d for d in registry.all_passes(sched.db) if d.id == "music_select")

    real_available = config.available_music_tracks

    async def scenario():
        ctx = context(chat, character, user_text="I switch on the jukebox.")
        # The prompt still lists it (built before the exclusion below), but
        # the handler re-reads settings fresh once the model answers.
        monkeypatch.setattr(sched.settings, "music_meta", {name: {"auto": False}})
        launched = sched._launch_background(ctx)
        assert "music_select" in launched
        await sched.await_pending(chat["id"])

    sync(scenario())
    assert read_slice(sched.db, chat["id"], SLICE_MUSIC) is None
    assert real_available() == [name], "the file itself was never touched"


def test_consume_music_roleplay_marks_a_note_used_exactly_once(sched, chat, character):
    note = {"note": "Mira starts playing something — no real audio.", "used": False}
    sync(state_mod.write_slice(
        sched.db, chat["id"], SLICE_MUSIC_ROLEPLAY, note, source_turn=1, source_pass="manual",
    ))

    ctx = context(chat, character, turn_no=2)
    sync(sched._consume_music_roleplay(ctx))
    stored = read_slice(sched.db, chat["id"], SLICE_MUSIC_ROLEPLAY)
    assert stored["value"]["used"] is True
    assert stored["value"]["note"] == note["note"]

    # Idempotent: consuming an already-used note a second time changes nothing.
    ctx2 = context(chat, character, turn_no=3)
    sync(sched._consume_music_roleplay(ctx2))
    assert read_slice(sched.db, chat["id"], SLICE_MUSIC_ROLEPLAY)["source_turn"] == 2


# ------------------------------------------------------- the volatile band


def test_a_playing_track_reaches_the_prompt_by_its_title(db, chat, character):
    """The title, not the description — a person recognises a song by its
    name, not by the mood note written to help the AI pick it."""
    sync(state_mod.write_slice(
        db, chat["id"], SLICE_MUSIC,
        {"status": "playing", "track": "waltz.mp3", "character": "Mira"},
        source_turn=1, source_pass="manual",
    ))
    settings = Settings(music_meta={
        "waltz.mp3": {"label": "An Old Waltz", "description": "Slow, melancholy strings."}
    })
    out = assembly.build_reply_context(db, chat, character, settings)
    assert "Currently playing: An Old Waltz." in out.volatile
    assert "melancholy" not in out.volatile


def test_a_playing_track_falls_back_to_its_filename_with_the_extension_stripped(
    db, chat, character
):
    sync(state_mod.write_slice(
        db, chat["id"], SLICE_MUSIC,
        {"status": "playing", "track": "waltz.mp3", "character": "Mira"},
        source_turn=1, source_pass="manual",
    ))
    out = assembly.build_reply_context(db, chat, character, Settings())
    assert "Currently playing: waltz." in out.volatile
    assert ".mp3" not in out.volatile


def test_a_proposed_but_unanswered_track_does_not_reach_the_prompt(db, chat, character):
    """Only "playing" is told to the character — a pending card is between
    the person and the app, not something the character already knows about."""
    sync(state_mod.write_slice(
        db, chat["id"], SLICE_MUSIC,
        {"status": "proposed", "track": "waltz.mp3", "character": "Mira"},
        source_turn=1, source_pass="music_select",
    ))
    assert "waltz" not in assembly.build_reply_context(db, chat, character, Settings()).volatile


def test_a_roleplay_note_reaches_the_prompt_once(db, chat, character):
    sync(state_mod.write_slice(
        db, chat["id"], SLICE_MUSIC_ROLEPLAY,
        {"note": "Mira starts playing a waltz — no real audio.", "used": False},
        source_turn=1, source_pass="manual",
    ))
    out = assembly.build_reply_context(db, chat, character, Settings())
    assert "Mira starts playing a waltz" in out.volatile


def test_a_used_roleplay_note_does_not(db, chat, character):
    sync(state_mod.write_slice(
        db, chat["id"], SLICE_MUSIC_ROLEPLAY,
        {"note": "Mira starts playing a waltz.", "used": True},
        source_turn=1, source_pass="manual",
    ))
    assert "waltz" not in assembly.build_reply_context(db, chat, character, Settings()).volatile


def test_no_music_state_at_all_adds_nothing(db, chat, character):
    out = assembly.build_reply_context(db, chat, character, Settings())
    assert "## Music" not in out.volatile
    assert not any(p["id"] == "music" and p.get("text") for p in out.parts)


def test_the_music_line_sits_in_the_volatile_band(db, chat, character):
    sync(state_mod.write_slice(
        db, chat["id"], SLICE_MUSIC,
        {"status": "playing", "track": "waltz.mp3", "character": "Mira"},
        source_turn=1, source_pass="manual",
    ))
    out = assembly.build_reply_context(db, chat, character, Settings())
    part = next(p for p in out.parts if p["id"] == "music")
    assert part["band"] == "volatile"
