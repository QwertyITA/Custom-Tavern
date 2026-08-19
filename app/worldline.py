"""Place, weather and time, cut down to what fits on one line (§10).

The scene pass is a small model asked to describe a setting, and small models
describe. Asked for the weather it returns "rain streaking across the window
pane"; asked where the scene is it returns "a tavern, the Long Wait, on the
harbour road". Both are good sentences and neither belongs in a header three
words wide, in a state block that is paid for on every turn, or next to two
other values in a row a thumb can cover.

So the prompt asks for one word and this makes sure of it. Normalising here
rather than trusting the prompt is the difference between usually short and
short: the same instruction that produces "Rainy" nine times produces "Rainy,
with the wind picking up" on the tenth, and the tenth is the one someone sees.

Applied on the way in *and* on the way out, so a chat that has been running
since before this existed reads as short immediately rather than after the
weather next changes.
"""

from __future__ import annotations

import re

# One word each, and the word people actually use. Order is the priority: the
# first key found wins, and it runs precipitation, then sky, then wind, then
# temperature. "Freezing rain" is Rainy and "clear and cold" is Clear — what is
# falling out of the sky beats what the sky looks like beats how it feels, and
# a header three words wide has room for exactly the first of those.
WEATHER_WORDS: tuple[tuple[str, str], ...] = (
    ("thunder", "Stormy"),
    ("storm", "Stormy"),
    ("blizzard", "Blizzard"),
    ("snow", "Snowy"),
    ("sleet", "Sleet"),
    ("hail", "Hail"),
    ("drizzl", "Drizzly"),
    ("downpour", "Rainy"),
    ("rain", "Rainy"),
    ("fog", "Foggy"),
    ("mist", "Misty"),
    ("smog", "Smoggy"),
    ("overcast", "Overcast"),
    ("cloud", "Cloudy"),
    ("clear", "Clear"),
    ("fair", "Clear"),
    ("sun", "Sunny"),
    ("wind", "Windy"),
    ("breez", "Breezy"),
    ("gale", "Windy"),
    ("humid", "Humid"),
    ("muggy", "Humid"),
    ("frost", "Frosty"),
    ("freez", "Freezing"),
    ("cold", "Cold"),
    ("chill", "Chilly"),
    ("cool", "Cool"),
    ("warm", "Warm"),
    ("heat", "Hot"),
    ("hot", "Hot"),
    ("still", "Still"),
    ("calm", "Calm"),
    ("dry", "Dry"),
)

# The whole vocabulary for the time of day. A scene is at one of these or it is
# not a time of day at all.
TIME_WORDS: tuple[tuple[str, str], ...] = (
    ("midnight", "Midnight"),
    ("dawn", "Dawn"),
    ("sunrise", "Dawn"),
    ("daybreak", "Dawn"),
    ("first light", "Dawn"),
    ("morning", "Morning"),
    ("midday", "Midday"),
    ("noon", "Midday"),
    ("afternoon", "Afternoon"),
    ("dusk", "Dusk"),
    ("sunset", "Dusk"),
    ("twilight", "Dusk"),
    ("evening", "Evening"),
    ("night", "Night"),
)

# Bands for a model that answered with a clock reading anyway.
CLOCK = re.compile(r"\b([01]?\d|2[0-3])\s*[:.]\s*[0-5]\d\s*(am|pm)?\b", re.IGNORECASE)

_ARTICLES = ("the ", "a ", "an ")
# Where a place description stops being the place and starts being a sentence
# about it. Cutting here turns "a tavern, warm and loud" into "Tavern".
_TAIL = re.compile(r"\s*[,;:—–(].*$")
_PLACE_WORDS = 3


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" \t\n.\"'")


def _unarticled(text: str) -> str:
    """Without a leading "the"/"a"/"an". A label never starts with one, and the
    fallback path would otherwise answer "A" for "a sunless grey"."""
    lowered = text.lower()
    for article in _ARTICLES:
        if lowered.startswith(article):
            return text[len(article):]
    return text


def shorten_place(value: object) -> str:
    """"A tavern" → "Tavern". Three words at the outside."""
    text = _clean(value)
    if not text:
        return ""
    text = _unarticled(_TAIL.sub("", text))
    words = text.split()
    if len(words) > _PLACE_WORDS:
        words = words[:_PLACE_WORDS]
    text = " ".join(words)
    # Only the first letter: "the Long Wait" keeps its own capitals, and a
    # title-cased "Tavern Common Room" reads as a proper noun it is not.
    return text[:1].upper() + text[1:] if text else ""


def shorten_weather(value: object) -> str:
    """"Rain streaking across the window pane" → "Rainy"."""
    text = _clean(value)
    if not text:
        return ""
    lowered = text.lower()
    for needle, word in WEATHER_WORDS:
        # At a word start, so "brainstorm" is not a storm. A prefix rather than
        # a whole word, because one key has to cover rain/rains/raining and
        # drizzle/drizzling — and then "-less" has to be excluded by hand,
        # since sunless, cloudless and windless all mean the opposite of the
        # word they contain.
        if re.search(rf"\b{needle}(?!less)", lowered):
            return word
    # Nothing recognised: keep the first word rather than inventing one. A
    # world nobody has weather words for is still allowed to have weather.
    first = _unarticled(lowered).split()[0].strip(",.")
    return first[:1].upper() + first[1:]


def _from_clock(text: str) -> str:
    match = CLOCK.search(text)
    if not match:
        return ""
    hour = int(match.group(1))
    suffix = (match.group(2) or "").lower()
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    if hour < 5:
        return "Night"
    if hour < 7:
        return "Dawn"
    if hour < 11:
        return "Morning"
    if hour < 14:
        return "Midday"
    if hour < 17:
        return "Afternoon"
    if hour < 19:
        return "Dusk"
    if hour < 22:
        return "Evening"
    return "Night"


def shorten_time(value: object) -> str:
    """"Late in the evening, well past supper" → "Evening"."""
    text = _clean(value)
    if not text:
        return ""
    lowered = text.lower()
    for needle, word in TIME_WORDS:
        if needle in lowered:
            return word
    banded = _from_clock(lowered)
    if banded:
        return banded
    first = _unarticled(lowered).split()[0].strip(",.")
    return first[:1].upper() + first[1:]


FIELDS = {"place": shorten_place, "weather": shorten_weather, "time": shorten_time}


def shorten(value: dict) -> dict:
    """One scene slice, with its three fields cut down. Anything else is left
    alone: the slice carries other keys and this is not the place to decide
    what they mean."""
    if not isinstance(value, dict):
        return value
    out = dict(value)
    for field, cut in FIELDS.items():
        if field in out:
            out[field] = cut(out[field])
    return out
