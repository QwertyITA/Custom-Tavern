"""Time actually spent in a chat (roadmap 40).

The whole feature is one claim — *this* many minutes were really spent here —
so what these check is mostly the ways that claim could be inflated: a tab
left open, a phone asleep, a beat replayed, two devices in the same chat. The
accounting lives in repo.mark_active and is deliberately small enough to
state: a sitting is worth its last beat minus its first, and a gap wider than
SESSION_GAP is not inside a sitting at all.
"""

from __future__ import annotations

import pytest

from app import groups, repo
from app.config import BELL_CHOICES, Settings, SettingsError, build_settings
from app.repo import SESSION_GAP

BEAT = 20.0  # what the client actually sends at (§ PRESENCE_BEAT_MS, app.js)
T0 = 1_000_000.0


@pytest.fixture
def solo(db, character, chat):
    groups.ensure_member(db, chat["id"], character.id)
    return chat


def beats(db, chat_id: str, start: float, count: int, every: float = BEAT) -> float:
    """`count` heartbeats from `start`. Returns the last stamp sent."""
    last = start
    for i in range(count):
        last = start + i * every
        repo.mark_active(db, chat_id, last)
    return last


# ----------------------------------------------------------- the accounting


def test_one_sitting_is_worth_its_first_beat_to_its_last(db, solo):
    beats(db, solo["id"], T0, 7)  # 0s .. 120s
    stat = repo.chat_time(db)[solo["id"]]
    assert stat == {"seconds": 120.0, "sessions": 1, "average": 120.0}


def test_a_single_beat_is_worth_nothing_yet(db, solo):
    """Deliberately not "one interval": all that is known is that someone was
    there at one instant. Under-counting the tail of a sitting is the right
    way round for a number whose whole job is not to be inflated."""
    repo.mark_active(db, solo["id"], T0)
    assert repo.chat_time(db)[solo["id"]]["seconds"] == 0.0


def test_a_gap_wider_than_the_window_is_never_inside_a_sitting(db, solo):
    """The one that matters: an hour asleep between two short visits is two
    sittings and two short times, not one hour."""
    beats(db, solo["id"], T0, 4)                    # 60s
    beats(db, solo["id"], T0 + 3600, 4)             # 60s, an hour later
    stat = repo.chat_time(db)[solo["id"]]
    assert stat["sessions"] == 2
    assert stat["seconds"] == 120.0
    assert stat["average"] == 60.0


def test_a_late_but_not_absent_beat_extends_the_same_sitting(db, solo):
    """A slow request or one missed beat is not a new sitting — otherwise a
    bad connection would quietly shred the average into fragments."""
    repo.mark_active(db, solo["id"], T0)
    repo.mark_active(db, solo["id"], T0 + SESSION_GAP - 1)
    stat = repo.chat_time(db)[solo["id"]]
    assert stat["sessions"] == 1
    assert stat["seconds"] == SESSION_GAP - 1


def test_the_window_boundary_belongs_to_the_same_sitting(db, solo):
    repo.mark_active(db, solo["id"], T0)
    repo.mark_active(db, solo["id"], T0 + SESSION_GAP)
    assert repo.chat_time(db)[solo["id"]]["sessions"] == 1
    repo.mark_active(db, solo["id"], T0 + SESSION_GAP * 2 + 1)
    assert repo.chat_time(db)[solo["id"]]["sessions"] == 2


def test_a_replayed_or_out_of_order_beat_cannot_shorten_a_sitting(db, solo):
    """Two devices in one chat, or a retried request: last_seen_at only ever
    moves forward, so an old stamp landing late cannot rewind the clock."""
    beats(db, solo["id"], T0, 5)                    # 80s
    repo.mark_active(db, solo["id"], T0 + 40)       # an old beat, arriving late
    assert repo.chat_time(db)[solo["id"]]["seconds"] == 80.0


def test_time_never_runs_past_the_last_beat(db, solo):
    """Nothing anywhere reads the wall clock to decide what a sitting is
    worth — which is what stops a tab abandoned mid-sitting from accruing
    until someone notices."""
    beats(db, solo["id"], T0, 3)                    # 40s
    first = repo.chat_time(db)[solo["id"]]["seconds"]
    # A long time passes with nothing sent.
    assert repo.chat_time(db)[solo["id"]]["seconds"] == first == 40.0


# --------------------------------------------------------- rolling it up


def test_a_character_totals_every_chat_they_are_in(db, character, solo):
    second = repo.create_chat(db, character.id, "another")
    groups.ensure_member(db, second["id"], character.id)
    beats(db, solo["id"], T0, 4)            # 60s
    beats(db, second["id"], T0, 7)          # 120s

    per_chat = repo.chat_time(db)
    assert per_chat[solo["id"]]["seconds"] == 60.0
    assert per_chat[second["id"]]["seconds"] == 120.0

    total = repo.character_time(db)[character.id]
    assert total["seconds"] == 180.0
    assert total["sessions"] == 2
    assert total["average"] == 90.0


def test_a_group_chat_counts_in_full_for_every_member(db, character, chat):
    """An hour with three characters in the room is an hour with each of
    them. Splitting it between them would answer a question nobody asked —
    and the roster already shows a group chat in every member's own history
    for the same reason (§ repo.list_chats)."""
    from app.models import Character

    repo.save_character(db, Character(id="second", name="Other"))
    groups.ensure_member(db, chat["id"], character.id)
    groups.ensure_member(db, chat["id"], "second")
    beats(db, chat["id"], T0, 7)            # 120s

    times = repo.character_time(db)
    assert times[character.id]["seconds"] == 120.0
    assert times["second"]["seconds"] == 120.0


def test_a_chat_nobody_sat_in_is_absent_rather_than_zero(db, solo):
    assert repo.chat_time(db) == {}
    assert repo.character_time(db) == {}


def test_deleting_a_chat_takes_its_sittings_with_it(db, solo):
    beats(db, solo["id"], T0, 4)
    assert repo.chat_time(db)
    repo.delete_chat(db, solo["id"])
    assert repo.chat_time(db) == {}


# ------------------------------------------------------ what the lists carry


def test_the_roster_carries_each_characters_total(db, character, solo):
    beats(db, solo["id"], T0, 4)
    row = next(c for c in repo.list_characters(db) if c["id"] == character.id)
    assert row["time"] == {"seconds": 60.0, "sessions": 1, "average": 60.0}


def test_a_character_with_no_time_still_carries_zeroes(db, character):
    """Zeroes rather than a missing key: the roster should never have to ask
    whether the field is there before drawing a row."""
    row = next(c for c in repo.list_characters(db) if c["id"] == character.id)
    assert row["time"] == {"seconds": 0.0, "sessions": 0, "average": 0.0}


def test_the_history_list_carries_each_chats_own_time(db, character, solo):
    beats(db, solo["id"], T0, 7)
    row = next(c for c in repo.list_chats(db, character.id) if c["id"] == solo["id"])
    assert row["time"]["seconds"] == 120.0
    assert row["time"]["sessions"] == 1


# ------------------------------------------------------------- the endpoint


def test_the_heartbeat_endpoint_records_a_sitting(client, db, chat):
    assert client.post(f"/api/chats/{chat['id']}/active").status_code == 200
    assert client.post(f"/api/chats/{chat['id']}/active").status_code == 200
    stat = repo.chat_time(db)[chat["id"]]
    assert stat["sessions"] == 1


def test_the_heartbeat_stamps_the_time_itself(client, db, chat):
    """The client never says *when*. One that could would be able to send a
    very old stamp and a very new one and claim the afternoon between them."""
    import time as _time

    before = _time.time()
    client.post(f"/api/chats/{chat['id']}/active", json={"at": 1.0, "seconds": 99999})
    row = db.query_one("SELECT started_at FROM chat_sessions WHERE chat_id=?", (chat["id"],))
    assert row["started_at"] >= before


def test_the_heartbeat_404s_on_a_chat_that_is_gone(client, db, chat):
    repo.delete_chat(db, chat["id"])
    assert client.post(f"/api/chats/{chat['id']}/active").status_code == 404
    assert repo.chat_time(db) == {}


# ---------------------------------------------------------- the tavern bell


def test_the_bell_is_off_by_default():
    assert Settings().tavern_bell_minutes == 0


@pytest.mark.parametrize("minutes", BELL_CHOICES)
def test_every_offered_length_round_trips(minutes):
    base = {
        "backends": [{"name": "echo", "kind": "echo"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
    }
    built = build_settings({**base, "tavern_bell_minutes": minutes}, Settings())
    assert built.tavern_bell_minutes == minutes


@pytest.mark.parametrize("bad", [7, 1, 120, -30])
def test_a_length_that_is_not_on_the_dial_is_refused(bad):
    """Refused rather than rounded to a neighbour: a value that is not on the
    dial did not come from the dial, and quietly repairing it would hide
    whatever sent it."""
    base = {
        "backends": [{"name": "echo", "kind": "echo"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
    }
    with pytest.raises(SettingsError):
        build_settings({**base, "tavern_bell_minutes": bad}, Settings())


# ------------------------------------------ the half of it that lives in JS
#
# The server can only count the beats it is sent; everything that decides
# whether a beat is *earned* is in app.js. There is no JS harness here, so
# these are source-level, the same shape as tests/test_stop_button.py — and
# the invariant is structural enough to say plainly: a beat requires both a
# visible page and a recent touch, and nothing may quietly drop either half.

from pathlib import Path  # noqa: E402

APP_JS = (Path(__file__).resolve().parent.parent / "static/app.js").read_text()


def _beat_body() -> str:
    body = APP_JS.split("presenceBeat() {", 1)[1]
    return body.split("\n    },", 1)[0]


def test_a_beat_needs_both_a_visible_page_and_a_recent_touch():
    """The two halves guard different things and neither is redundant: the
    visibility check is what stops a phone in a pocket, the idle check is
    what stops one face-up on a desk. Leaving a tab open satisfies neither."""
    present = APP_JS.split("presentNow() {", 1)[1].split("\n    },", 1)[0]
    assert "document.hidden" in present
    assert "PRESENCE_IDLE_MS" in present and "this.lastTouch" in present


def test_the_beat_asks_rather_than_trusting_a_cached_flag():
    """`afk` is for the UI. A beat that read it instead of the clock would be
    one stale re-render away from counting an empty room."""
    assert "this.presentNow()" in _beat_body()


def test_a_beat_is_never_sent_while_absent():
    body = _beat_body()
    guard = body.index("if (!present) return;")
    assert guard < body.index("/api/chats/"), "the POST must sit behind the guard"


def test_the_client_never_sends_its_own_timestamp():
    """The server stamps it (§ POST /api/chats/{id}/active). A client that
    could name the time could name an afternoon of it."""
    body = _beat_body()
    post = body[body.index("/api/chats/"):]
    assert "body:" not in post and "Date.now()" not in post


def test_every_ordinary_way_of_being_present_counts_as_a_touch():
    """A narrower list would have penalised whichever way of using the app
    was left off it — reading by scrolling is as present as typing."""
    listeners = APP_JS.split("startPresence() {", 1)[1].split("\n    },", 1)[0]
    for event in ("pointerdown", "keydown", "wheel", "touchstart", "scroll"):
        assert f'"{event}"' in listeners, event
    assert "visibilitychange" in listeners


def test_the_idle_window_is_long_enough_to_read_by_and_short_enough_to_matter():
    """Bounds rather than an exact number, so the value can be tuned without
    a test to re-argue — but not to somewhere that breaks what it is for."""
    window = int(APP_JS.split("const PRESENCE_IDLE_MS = ", 1)[1].split(";", 1)[0].replace("_", ""))
    beat = int(APP_JS.split("const PRESENCE_BEAT_MS = ", 1)[1].split(";", 1)[0].replace("_", ""))
    assert 60_000 <= window <= 300_000
    assert beat < window, "a beat interval past the idle window could never fire twice"
    assert beat * 2 < SESSION_GAP * 1000, "two missed beats must not split a sitting"


def test_the_bell_keeps_running_while_the_browser_is_minimised():
    """Reported: the bell must not stop when the browser is minimised — it
    is a "you have been at this a while" nudge, and one that counted only
    foreground seconds would never arrive. So checkBell sits in *front* of
    presenceBeat's guard, unlike everything else there."""
    body = _beat_body()
    assert "this.checkBell()" in body
    assert body.index("this.checkBell()") < body.index("if (!present) return;")


def test_the_bell_reads_the_clock_rather_than_counting_ticks():
    """An accumulator could not have survived minimising either way: a
    backgrounded tab's timers are frozen on Android, so there would be
    nothing to accumulate with. Reading the clock closes the gap on the way
    back instead."""
    check = APP_JS.split("checkBell() {", 1)[1].split("\n    },", 1)[0]
    assert "Date.now() - this.bellSince" in check
    assert "bellActiveMs" not in APP_JS, "the old accumulator is gone entirely"


def test_returning_to_the_tab_settles_the_bell_at_once():
    """Otherwise a bell that came due while the tab was frozen would wait up
    to a full beat after you were already looking at the screen."""
    listeners = APP_JS.split("startPresence() {", 1)[1].split("\n    },", 1)[0]
    visible = listeners.split("visibilitychange", 1)[1]
    assert "this.checkBell()" in visible


def test_a_bell_that_came_due_unseen_sounds_once_not_once_per_period():
    """`bellsRung` jumps to where the clock already is, so an hour away is
    one bell on return rather than four."""
    check = APP_JS.split("checkBell() {", 1)[1].split("\n    },", 1)[0]
    assert "this.bellsRung = due;" in check
    assert check.index("this.bellsRung = due;") < check.index("this.ringBell()")


def test_a_ring_nobody_could_hear_stays_owed_rather_than_being_spent():
    """Counting and sounding are different questions — but the bail on a
    hidden page has to come *before* the count is advanced. Written the
    other way round first, and it swallowed exactly the ring this change
    exists to deliver: coming back found the bell already marked rung and
    said nothing about the hour that had passed. Caught live, not here."""
    check = APP_JS.split("checkBell() {", 1)[1].split("\n    },", 1)[0]
    assert "if (document.hidden) return;" in check
    assert check.index("if (document.hidden) return;") < check.index("this.bellsRung = due;")


def test_the_bell_counts_rings_rather_than_running_a_timer():
    """So a length changed mid-visit cannot strand a timer, and the same
    stretch can never ring twice."""
    check = APP_JS.split("checkBell() {", 1)[1].split("\n    },", 1)[0]
    assert "this.bellsRung" in check and "Math.floor" in check


def test_an_uploaded_sound_wins_and_the_synthesised_one_is_the_fallback():
    ring = APP_JS.split("ringBell() {", 1)[1].split("\n    },", 1)[0]
    assert "this.settings.bell_sound" in ring
    assert 'new Audio("/bell")' in ring
    assert "this.synthBell();" in ring, "and it still falls back"


# ------------------------------------------------------- the bell's own sound

BELL_BYTES = b"not really audio, just bytes with the right extension"


@pytest.fixture
def bell_dir(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "USER_BELL_DIR", tmp_path / "bell")
    return config.USER_BELL_DIR


def test_there_is_no_uploaded_sound_to_begin_with(client, bell_dir):
    from app import config

    assert config.bell_sound() == ""
    assert config.bell_sound_path() is None
    assert client.get("/bell").status_code == 404


def test_a_sound_can_be_uploaded_served_and_removed(client, bell_dir):
    from app import config

    added = client.post("/api/bell?filename=my bell.MP3", content=BELL_BYTES)
    assert added.status_code == 200
    name = added.json()["name"]
    assert name == "my-bell.mp3", "rebuilt, not sanitised in place"
    assert config.bell_sound() == name
    assert client.get("/bell").content == BELL_BYTES

    assert client.delete("/api/bell").status_code == 200
    assert config.bell_sound() == ""
    assert client.get("/bell").status_code == 404


def test_uploading_again_replaces_rather_than_piles_up(client, bell_dir):
    """There is one bell. Two files here and which one rang would come down
    to sort order."""
    from app import config

    client.post("/api/bell?filename=first.mp3", content=BELL_BYTES)
    client.post("/api/bell?filename=second.ogg", content=b"different bytes")
    assert sorted(p.name for p in bell_dir.iterdir()) == ["second.ogg"]
    assert config.bell_sound() == "second.ogg"


def test_the_settings_payload_says_which_sound_is_in_use(client, bell_dir):
    """Derived from the filesystem, never stored — one less thing that can
    disagree with what is actually on disk."""
    assert client.get("/api/settings").json()["bell_sound"] == ""
    client.post("/api/bell?filename=ding.mp3", content=BELL_BYTES)
    assert client.get("/api/settings").json()["bell_sound"] == "ding.mp3"


def test_only_audio_the_app_can_serve_is_accepted(client, bell_dir):
    assert client.post("/api/bell?filename=x.exe", content=b"MZ").status_code == 400
    assert client.post("/api/bell?filename=x.mp3", content=b"").status_code == 400


def test_an_oversized_sound_is_rejected_before_the_body_is_read(client, bell_dir):
    from app import config

    response = client.post(
        "/api/bell?filename=x.mp3",
        content=BELL_BYTES,
        headers={"content-length": str(config.MAX_BELL_BYTES + 1)},
    )
    assert response.status_code == 400


def test_a_bell_sound_is_capped_far_below_a_music_track(client):
    """Two seconds of doorbell against a song — a cap sized for the library
    would let someone put an album in here by accident."""
    from app import config

    assert config.MAX_BELL_BYTES < config.MAX_MUSIC_BYTES


def test_removing_a_sound_that_was_never_there_is_fine(client, bell_dir):
    """So the button never has a failure state of its own."""
    assert client.delete("/api/bell").status_code == 200


def test_the_bell_sound_never_lands_in_the_music_library(client, bell_dir, tmp_path, monkeypatch):
    """That library is the story's soundtrack — offered to music_select and
    pickable by hand in a chat. A doorbell has no business in either."""
    from app import config

    monkeypatch.setattr(config, "USER_MUSIC_DIR", tmp_path / "music")
    client.post("/api/bell?filename=ding.mp3", content=BELL_BYTES)
    assert config.available_music_tracks() == []


def test_the_clock_starts_even_on_an_install_with_no_characters_yet():
    """Found live: boot() returns early when the roster is empty, and
    startPresence() sat after that return — so a fresh install never started
    the clock or the bell at all, and stayed that way until the next reload
    after making a character. Nothing in presence needs the roster, so it
    goes in front of anything that can bail."""
    boot = APP_JS.split("async boot() {", 1)[1].split("\n    },", 1)[0]
    assert "this.startPresence();" in boot
    assert boot.index("this.startPresence();") < boot.index("return;"), (
        "startPresence must come before boot's first early return"
    )
