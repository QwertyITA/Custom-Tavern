"""Place, weather and time cut down to a label (§10).

Reported from a real chat: the header read "the road o…", "still an…",
"late …" — three truncations in a row, because the scene pass is a small model
asked to describe a setting and small models describe. The prompt asks for one
word now; this is the part that makes sure of it.
"""

from __future__ import annotations

import pytest

from app import worldline


@pytest.mark.parametrize("given,want", [
    ("A tavern", "Tavern"),
    ("the tavern common room", "Tavern common room"),
    ("An inn", "Inn"),
    ("a tavern, warm and loud, on the harbour road", "Tavern"),
    ("the long low room where the fire is", "Long low room"),
    ("the Long Wait", "Long Wait"),
    ("", ""),
])
def test_place_is_a_name_not_a_sentence(given, want):
    assert worldline.shorten_place(given) == want


@pytest.mark.parametrize("given,want", [
    ("rain streaking across the window pane", "Rainy"),
    ("Heavy downpour", "Rainy"),
    ("freezing rain", "Rainy"),
    ("light snow beginning to settle", "Snowy"),
    ("thunder somewhere out past the hills", "Stormy"),
    ("overcast, threatening", "Overcast"),
    ("clear and cold", "Clear"),
    ("bitter cold", "Cold"),
    ("still and close", "Still"),
    ("a sunless grey", "Sunless"),
    ("", ""),
])
def test_weather_is_one_word(given, want):
    assert worldline.shorten_weather(given) == want


def test_an_unknown_sky_keeps_its_first_word_rather_than_inventing_one():
    """A world nobody has weather words for is still allowed to have weather."""
    assert worldline.shorten_weather("ashfall from the mountain") == "Ashfall"


@pytest.mark.parametrize("given,want", [
    ("early morning", "Morning"),
    ("late night", "Night"),
    ("just past sunset", "Dusk"),
    ("first light", "Dawn"),
    ("high noon", "Midday"),
    ("Late in the evening, well past supper", "Evening"),
    ("", ""),
])
def test_time_is_one_of_the_words(given, want):
    assert worldline.shorten_time(given) == want


@pytest.mark.parametrize("given,want", [
    ("21:40", "Evening"),
    ("6:15 am", "Dawn"),
    ("11:30", "Midday"),
    ("2:00", "Night"),
])
def test_a_clock_reading_is_banded(given, want):
    """The prompt forbids one. Models produce them anyway, and a header that
    says 21:40 in a world with no clocks is worse than a wrong band."""
    assert worldline.shorten_time(given) == want


def test_the_whole_slice_at_once():
    out = worldline.shorten({
        "place": "a quiet back room",
        "weather": "rain against the shutters",
        "time": "late evening",
        "other": "left alone",
    })
    assert out == {
        "place": "Quiet back room",
        "weather": "Rainy",
        "time": "Evening",
        "other": "left alone",
    }


def test_shortening_is_stable():
    """It runs on write and again on read, so it has to be a no-op the second
    time — otherwise a value would erode a word per turn."""
    once = worldline.shorten({"place": "the tavern common room",
                              "weather": "rain", "time": "dusk"})
    assert worldline.shorten(once) == once


def test_junk_survives():
    assert worldline.shorten({"place": None, "weather": 12, "time": ["x"]})["place"] == ""
