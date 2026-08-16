"""The motion dial (ANIMATIONS §1.3).

`prefers-reduced-motion` is a switch; this is the dial between it and full.
Someone who finds the interface busy but does not want it dead has nowhere to
go otherwise. The OS setting still wins over anything set here — that is a
stylesheet rule rather than a server one, so what is checked below is only that
the value survives the round trip and refuses nonsense.
"""

from __future__ import annotations

import pytest

from app.config import Settings, SettingsError, build_settings


def base() -> dict:
    return {
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
    }


def test_it_defaults_to_everything_moving():
    """The feature is opt-out. An install that never opens the panel should
    look exactly as it did before the dial existed."""
    assert Settings().motion == 100


@pytest.mark.parametrize("value", [0, 10, 50, 100])
def test_a_value_in_range_is_kept(value):
    assert build_settings({**base(), "motion": value}, Settings()).motion == value


@pytest.mark.parametrize("value", [-1, 101, 1000])
def test_a_value_out_of_range_is_refused(value):
    """Clamping silently would leave the slider disagreeing with the stored
    value, which is worse than saying no."""
    with pytest.raises(SettingsError):
        build_settings({**base(), "motion": value}, Settings())


def test_nonsense_is_refused():
    with pytest.raises(SettingsError):
        build_settings({**base(), "motion": "quickly"}, Settings())


def test_leaving_it_out_keeps_what_was_there():
    """Every other panel saves the whole settings object, so an omitted key has
    to mean "unchanged" rather than "back to default"."""
    current = Settings(motion=30)
    assert build_settings(base(), current).motion == 30


def test_it_saves_and_comes_back(client, isolated_settings):
    from app import config

    assert client.put("/api/settings", json={**base(), "motion": 40}).json()["ok"] is True
    assert config.SETTINGS.motion == 40
    assert client.get("/api/settings").json()["motion"] == 40


def test_zero_is_a_real_setting_not_a_missing_one(client, isolated_settings):
    """0 is falsy in both languages this crosses, and "off" has to survive the
    trip rather than being read as "unset" and replaced by the default."""
    from app import config

    client.put("/api/settings", json={**base(), "motion": 0})
    assert config.SETTINGS.motion == 0
    assert client.get("/api/settings").json()["motion"] == 0


# ------------------------------------------------------------------- glass


def test_glass_is_off_until_asked_for():
    """A frosted interface is a preference, not a default. An install that
    never opens the panel should look exactly as it did before."""
    assert Settings().glass is False
    assert Settings().glass_amount == 60


def test_glass_saves_and_comes_back(client, isolated_settings):
    from app import config

    body = {**base(), "glass": True, "glass_amount": 80}
    assert client.put("/api/settings", json=body).json()["ok"] is True
    assert config.SETTINGS.glass is True
    assert config.SETTINGS.glass_amount == 80

    back = client.get("/api/settings").json()
    assert back["glass"] is True
    assert back["glass_amount"] == 80


def test_glass_off_survives_the_round_trip(client, isolated_settings):
    """False is falsy in both languages this crosses; "off" has to mean off
    rather than "unset, use the default"."""
    from app import config

    client.put("/api/settings", json={**base(), "glass": True})
    client.put("/api/settings", json={**base(), "glass": False})
    assert config.SETTINGS.glass is False


@pytest.mark.parametrize("value", [-1, 101])
def test_a_glass_amount_out_of_range_is_refused(value):
    with pytest.raises(SettingsError):
        build_settings({**base(), "glass_amount": value}, Settings())


def test_a_nonsense_glass_amount_is_refused():
    with pytest.raises(SettingsError):
        build_settings({**base(), "glass_amount": "very"}, Settings())


def test_glass_is_independent_of_the_palette():
    """The whole point of it being a layer: it must not carry colours of its
    own, or it would fight whichever preset is in force."""
    settings = build_settings({**base(), "glass": True, "theme": {"--bg": "#101014"}}, Settings())
    assert settings.glass is True
    assert settings.theme["--bg"] == "#101014"
