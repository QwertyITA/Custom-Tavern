"""Sampler catalogue: what can be set, what it does, and who accepts it (§17).

One table, three consumers: the `Sampling` model takes its defaults from here,
each provider builds its payload from here, and the GUI draws its controls from
here. Keeping them in one place is what stops a slider existing for a parameter
no backend is sent, or a parameter being sent under a name a backend rejects.

Two rules run through it:

**A sampler is only sent when it has been moved.** Every one has a neutral
value at which it does nothing, and at neutral it is left out of the payload
entirely. A backend that has never heard of `min_p` should not be handed one,
and — more to the point — a backend should not be handed a stack of parameters
nobody deliberately set, which is how output changes for reasons no one can
account for.

**Support is declared, not attempted.** Horde validates its parameters and
rejects the request outright on an unknown one, so "send it and see" is not
available. Each backend kind lists what it takes, under the name it takes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Groups, in the order the panel shows them. A group exists when its members
# are only meaningful together — DRY's four numbers are one setting wearing
# four hats, and showing them as four unrelated sliders is how they get set to
# combinations that do nothing.
GROUPS: list[dict[str, str]] = [
    {"id": "core", "label": "Randomness",
     "note": "How freely it picks the next word."},
    {"id": "truncation", "label": "What it may pick from",
     "note": "Cut the unlikely tail before choosing. One of these is usually "
             "enough — stacking them narrows harder than you mean."},
    {"id": "repetition", "label": "Repetition",
     "note": "Push down words it has already used."},
    {"id": "dry", "label": "Don't Repeat Yourself",
     "note": "Penalises repeated *sequences* rather than repeated words, so "
             "quoted names and turns of phrase survive while loops do not. "
             "Off until the multiplier is above zero."},
    {"id": "xtc", "label": "Exclude Top Choices",
     "note": "Sometimes drops the most obvious next word, to break out of the "
             "safest possible prose. Off until the probability is above zero."},
    {"id": "misc", "label": "Reproducibility", "note": ""},
]

GROUP_IDS = tuple(group["id"] for group in GROUPS)


@dataclass(frozen=True)
class Sampler:
    key: str          # the field name on Sampling
    label: str
    note: str
    group: str
    lo: float
    hi: float
    step: float
    neutral: float    # the value at which this sampler does nothing
    integer: bool = False
    # Sent whether or not it has been moved. Only for the ones where leaving
    # them out does not mean "off" but "use the backend's own idea", which is
    # different on every backend — temperature is the whole of this category:
    # nothing defaults to 1.0, so an unsent 1.0 would silently become 0.8 on
    # Ollama and something else again elsewhere.
    always: bool = False

    def is_set(self, value: Any) -> bool:
        try:
            return float(value) != float(self.neutral)
        except (TypeError, ValueError):
            return False

    def clean(self, value: Any) -> float | int:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return int(self.neutral) if self.integer else self.neutral
        number = max(self.lo, min(self.hi, number))
        return int(round(number)) if self.integer else number


SAMPLERS: list[Sampler] = [
    Sampler("temp", "Temperature", "Higher wanders further from the obvious.",
            "core", 0.0, 2.0, 0.01, 1.0, always=True),
    Sampler("top_p", "Top-p", "Keep the likeliest words that add up to this "
            "share of the probability. 1 keeps everything.",
            "truncation", 0.0, 1.0, 0.01, 1.0),
    Sampler("top_k", "Top-k", "Keep only this many candidates. 0 keeps all.",
            "truncation", 0, 200, 1, 0, integer=True),
    Sampler("min_p", "Min-p", "Drop anything less likely than this share of "
            "the best candidate. Scales with how confident the model is, which "
            "is why it is the one to reach for first.",
            "truncation", 0.0, 0.5, 0.005, 0.0),
    Sampler("typical_p", "Typical-p", "Keep words of average surprise, cutting "
            "both the obvious and the bizarre. 1 keeps everything.",
            "truncation", 0.0, 1.0, 0.01, 1.0),
    Sampler("tfs", "Tail-free", "Cuts where the probability curve flattens out. "
            "1 keeps everything.",
            "truncation", 0.0, 1.0, 0.01, 1.0),

    Sampler("rep_penalty", "Repetition penalty",
            "Divides the score of words already used. Above about 1.2 it starts "
            "eating ordinary grammar.",
            "repetition", 1.0, 1.5, 0.01, 1.0),
    Sampler("rep_range", "…looking back this far",
            "How many recent tokens the penalty considers. 0 means all of them.",
            "repetition", 0, 4096, 32, 0, integer=True),
    Sampler("freq_penalty", "Frequency penalty",
            "Scales with how often a word was used, rather than whether it was.",
            "repetition", -2.0, 2.0, 0.05, 0.0),
    Sampler("presence_penalty", "Presence penalty",
            "A flat push against anything already said, however rarely.",
            "repetition", -2.0, 2.0, 0.05, 0.0),

    Sampler("dry_multiplier", "Strength", "0 turns DRY off.",
            "dry", 0.0, 5.0, 0.05, 0.0),
    Sampler("dry_base", "Growth", "How sharply the penalty climbs with the "
            "length of the repeated run.",
            "dry", 1.0, 4.0, 0.05, 1.75),
    Sampler("dry_allowed_length", "Free length",
            "Repeat this many tokens before the penalty starts. Names and set "
            "phrases live here.",
            "dry", 1, 20, 1, 2, integer=True),
    Sampler("dry_range", "Looking back", "0 means the whole context.",
            "dry", 0, 4096, 64, 0, integer=True),

    Sampler("xtc_probability", "Probability", "How often to apply it at all. "
            "0 turns XTC off.",
            "xtc", 0.0, 1.0, 0.01, 0.0),
    Sampler("xtc_threshold", "Threshold",
            "Only candidates above this likelihood are eligible to be dropped.",
            "xtc", 0.0, 0.5, 0.01, 0.1),

    Sampler("seed", "Seed", "Same seed, same prompt, same reply. −1 picks a new "
            "one every time.",
            "misc", -1, 2**31 - 1, 1, -1, integer=True),
]

BY_KEY = {sampler.key: sampler for sampler in SAMPLERS}

# max_tokens is a sampler in the sense that it goes in the same payload, but it
# is not one anybody tunes for taste and it has no neutral value, so it keeps
# its own control rather than joining the table.

# our key -> the name each backend kind wants. A kind that omits a key does not
# accept it, and it is never sent there.
BACKEND_PARAMS: dict[str, dict[str, str]] = {
    "ollama": {
        "temp": "temperature", "top_p": "top_p", "top_k": "top_k",
        "min_p": "min_p", "typical_p": "typical_p", "tfs": "tfs_z",
        "rep_penalty": "repeat_penalty", "rep_range": "repeat_last_n",
        "freq_penalty": "frequency_penalty", "presence_penalty": "presence_penalty",
        "seed": "seed",
    },
    # llama.cpp's own server, which tracks new samplers well before Ollama
    # exposes them — this is the only kind that takes DRY and XTC.
    "llamacpp": {
        "temp": "temperature", "top_p": "top_p", "top_k": "top_k",
        "min_p": "min_p", "typical_p": "typical_p", "tfs": "tfs_z",
        "rep_penalty": "repeat_penalty", "rep_range": "repeat_last_n",
        "freq_penalty": "frequency_penalty", "presence_penalty": "presence_penalty",
        "dry_multiplier": "dry_multiplier", "dry_base": "dry_base",
        "dry_allowed_length": "dry_allowed_length", "dry_range": "dry_penalty_last_n",
        "xtc_probability": "xtc_probability", "xtc_threshold": "xtc_threshold",
        "seed": "seed",
    },
    # Horde speaks the KoboldAI parameter set and *validates* it: an unknown
    # key fails the whole request rather than being ignored. Only what its
    # schema documents goes here.
    "horde": {
        "temp": "temperature", "top_p": "top_p", "top_k": "top_k",
        "min_p": "min_p", "typical_p": "typical", "tfs": "tfs",
        "rep_penalty": "rep_pen", "rep_range": "rep_pen_range",
    },
    # The official OpenAI parameter set. Many local servers wearing this API
    # accept more, but sending top_k to the real one is a 400, and a backend
    # that fails on a setting you cannot see is the worst version of this.
    "openai": {
        "temp": "temperature", "top_p": "top_p",
        "freq_penalty": "frequency_penalty", "presence_penalty": "presence_penalty",
        "seed": "seed",
    },
    "echo": {},
}


# A group that is switched on by one of its members, and travels as a unit.
# DRY's four numbers are one setting wearing four hats: sending the multiplier
# alone would leave the other three at whatever the backend happens to think,
# which is not what the panel is showing.
GROUP_GATES: dict[str, str] = {
    "dry": "dry_multiplier",
    "xtc": "xtc_probability",
}


def supported(kind: str) -> set[str]:
    return set(BACKEND_PARAMS.get(kind, {}))


def _live_groups(sampling: Any) -> set[str]:
    live = set()
    for group, gate in GROUP_GATES.items():
        sampler = BY_KEY[gate]
        if sampler.is_set(getattr(sampling, gate, sampler.neutral)):
            live.add(group)
    return live


def params_for(kind: str, sampling: Any) -> dict[str, Any]:
    """The backend-named parameters this sampling actually asks for.

    Only what the kind accepts, only what has been moved off neutral — plus the
    handful that are always sent, and the companions of any gated group that is
    switched on. Under the name that kind uses.
    """
    mapping = BACKEND_PARAMS.get(kind, {})
    live = _live_groups(sampling)
    out: dict[str, Any] = {}
    for key, their_name in mapping.items():
        sampler = BY_KEY[key]
        value = getattr(sampling, key, sampler.neutral)
        if sampler.always or sampler.group in live or sampler.is_set(value):
            out[their_name] = sampler.clean(value)
    return out


def catalogue() -> dict[str, Any]:
    """Everything the panel needs, so the frontend holds no second copy."""
    return {
        "groups": [dict(group) for group in GROUPS],
        "samplers": [
            {
                "key": s.key, "label": s.label, "note": s.note, "group": s.group,
                "min": s.lo, "max": s.hi, "step": s.step, "neutral": s.neutral,
                "integer": s.integer, "always": s.always,
            }
            for s in SAMPLERS
        ],
        "supported": {kind: sorted(mapping) for kind, mapping in BACKEND_PARAMS.items()},
        "gates": dict(GROUP_GATES),
    }
