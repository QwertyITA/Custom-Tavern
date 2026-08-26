"""The post_process pass: an LLM copy-edit of a finished reply (§5.7), and —
when Settings.post_process_tracks_state is on — the same call's second job,
proposing a new tracked state variable (§6).

Runs once, after a reply has been fully generated and cleaned but before it
is shown or stored — the model is handed its own draft and asked to fix what
is mechanical: grammar and spelling, a misspelled character name, whether it
kept to the configured paragraph length (craft:length), whether it slipped
out of the point-of-view convention (craft:pov). Nothing about the content,
the events or the voice is meant to change.

Distinct from `app/postprocess.py` (§13), which strips template artifacts and
stray markup out of *every* reply regardless of this being on — that one is
mechanical and always runs; this one is an optional model call, and the
reply stays hidden from the user for as long as it takes.

Best-effort only. Any failure, an empty response, or a response so different
in size from the draft that it reads as a refusal or a rewrite rather than an
edit, is treated the same as post-process being switched off: the draft is
what gets shown. A pass whose whole job is polish must never be the reason a
turn fails or hangs. The same standard applies to the state-tracking half —
a malformed, incoherent, or duplicate proposal is silently dropped rather
than surfaced as an error, since nothing about that turn actually depended
on it.
"""

from __future__ import annotations

import asyncio

from .models import Band, PassDef, VariableSchema
from .passes.contract import parse_json_loose
from .postprocess import clean_reply, split_thinking
from .providers.base import GenRequest, Provider, ProviderError

# A rewrite this much shorter or longer than the draft reads as the model
# doing something other than editing it — refusing, restarting the scene,
# answering as itself — and is discarded in favour of the untouched draft
# rather than shown as "the corrected version". Generous on purpose: a real
# edit can legitimately move a lot in either direction (cutting a reply down
# to a configured length, or padding a too-short one up to it).
_MIN_KEEP_FRACTION = 0.3
_MAX_GROW_FACTOR = 2.5

# How many variables a character's state_schema is allowed to grow to before
# a new proposal is refused outright, regardless of how well-formed it is.
# Nothing enforces this on a schema written by hand — the whole point there
# is that a person decided how many they want — but a schema growing on its
# own needs a ceiling, or a long chat slowly turns "state" into a list
# nobody reads. Matches state_schema's own place in the prompt: unbounded
# growth there is unbounded prompt cost too.
MAX_TRACKED_VARIABLES = 8

TRACK_MARKER = "<<<track>>>"

# Plain string, not an f-string: {marker}/{existing} are `.format()` fields
# filled in per call (§ run below), and the JSON example needs its braces
# doubled for that same `.format()` call to leave them alone — an f-string
# would already have collapsed those doubled braces to single ones at
# *definition* time, leaving nothing for `.format()` to skip the second time
# around.
_TRACK_INSTRUCTIONS = """\

This story also tracks a few numeric variables about the character — \
willingness, trust, that kind of thing — each shown to you as a band \
(low/medium/high) rather than a number. Variables already tracked: \
{existing}

If this reply revealed something about the character worth tracking the \
same way, and nothing already tracked covers it, propose exactly one new \
variable. After the corrected text, on its own line, write {marker} \
followed by one JSON object:
{marker}{{"name": "trust", "min": 0, "max": 10, "baseline": 5, \
"decay": 0.15, "value": 6, "bands": [{{"range": [0, 3], "label": "guarded", \
"guidance": "resistant, deflects, needs convincing"}}, {{"range": [4, 6], \
"label": "neutral", "guidance": "engages if asked, won't volunteer"}}, \
{{"range": [7, 10], "label": "eager", "guidance": "leans in, initiates, \
generous"}}]}}
"name" is one or two words, lowercase, nothing already tracked. "value" is \
where it stands right now, given what this reply just showed — not \
necessarily the baseline. "bands" covers the whole min-to-max range with no \
gaps, three bands is normal, each "guidance" a short phrase describing how \
the character behaves at that level, never naming the number.
Most replies reveal nothing new. If nothing is worth adding, write nothing \
after the corrected text at all — no marker, no explanation.
"""


def _looks_like_an_edit(draft: str, candidate: str) -> bool:
    if not candidate.strip():
        return False
    lo = len(draft) * _MIN_KEEP_FRACTION
    hi = len(draft) * _MAX_GROW_FACTOR
    return lo <= len(candidate) <= hi


def _context(character_name: str, draft: str, assembled_parts: list[dict]) -> str:
    """The per-turn brief: who is speaking, the length/POV targets already
    given to the reply itself (pulled from the already-assembled prompt
    rather than re-read from settings, so this can never quote a different
    target than the one the draft was actually written against), and the
    draft itself.
    """
    pov_text = next((p["text"] for p in assembled_parts if p["id"] == "craft:pov"), "")
    length_text = next((p["text"] for p in assembled_parts if p["id"] == "craft:length"), "")
    lines = [f"The character speaking is {character_name}."]
    if pov_text:
        lines.append(f"Point of view to keep:\n{pov_text}")
    if length_text:
        lines.append(f"Length target:\n{length_text}")
    lines.append(f"The reply to edit:\n\n{draft}")
    return "\n\n".join(lines)


def _split_track_suffix(text: str) -> tuple[str, dict | None]:
    """(corrected text, raw proposal payload or None).

    Unlike the reply pass's own `<<<state>>>` suffix (§5.6), nothing is
    expected *after* the marker but the JSON itself — post_process's own
    contract asks for it last, and there is no separate stream to reconcile
    it against — so this only has to split on the first occurrence, not
    reassemble prose from both sides of it.
    """
    index = text.find(TRACK_MARKER)
    if index == -1:
        return text, None
    body = text[:index]
    payload = parse_json_loose(text[index + len(TRACK_MARKER):])
    return body, payload


def validate_proposal(
    payload: dict | None, existing_names: list[str], schema_size: int
) -> tuple[str, VariableSchema, float] | None:
    """(name, schema, initial value) if `payload` is a coherent, new,
    room-still-available variable — None on literally anything else. Never
    raises: a proposal this permissive a model is allowed to be wrong about
    in a dozen different shapes, and every one of them means "skip it", not
    "crash the turn".
    """
    if not isinstance(payload, dict) or schema_size >= MAX_TRACKED_VARIABLES:
        return None
    name = str(payload.get("name", "")).strip().lower()
    if not name or len(name) > 40 or name in {n.lower() for n in existing_names}:
        return None
    try:
        low = float(payload.get("min", 0))
        high = float(payload.get("max", 10))
        baseline = float(payload.get("baseline", (low + high) / 2))
        decay = max(0.0, float(payload.get("decay", 0)))
    except (TypeError, ValueError):
        return None
    if not (low < high) or not (low <= baseline <= high):
        return None

    bands: list[Band] = []
    for raw in payload.get("bands") or []:
        if not isinstance(raw, dict):
            continue
        try:
            lo, hi = raw["range"]
            lo, hi = float(lo), float(hi)
        except (KeyError, TypeError, ValueError):
            continue
        if lo > hi:
            lo, hi = hi, lo
        label = str(raw.get("label", "")).strip()
        guidance = str(raw.get("guidance", "")).strip()
        if not label or not guidance:
            continue
        bands.append(Band(range=(lo, hi), label=label, guidance=guidance))
    if not bands:
        return None

    try:
        value = float(payload.get("value", baseline))
    except (TypeError, ValueError):
        value = baseline
    value = max(low, min(high, value))

    schema = VariableSchema(min=low, max=high, baseline=baseline, decay=decay, bands=bands)
    return name, schema, value


async def run(
    provider: Provider,
    definition: PassDef,
    draft: str,
    character_name: str,
    assembled_parts: list[dict],
    timeout: float,
    *,
    track_state: bool = False,
    existing_variables: list[str] | None = None,
    schema_size: int = 0,
) -> tuple[str, tuple[str, VariableSchema, float] | None]:
    """(possibly corrected reply, new-variable proposal or None).

    The reply is `draft` itself on anything that goes wrong — this can never
    be the reason a turn fails. The proposal is independent of whether the
    edit itself was accepted: a well-formed `<<<track>>>` payload is honoured
    even when the surrounding prose was too different in size to trust
    (§ _looks_like_an_edit) and gets discarded instead.
    """
    if not draft.strip():
        return draft, None

    system = definition.prompt
    if track_state:
        existing = ", ".join(existing_variables or []) or "none yet"
        system += _TRACK_INSTRUCTIONS.format(existing=existing, marker=TRACK_MARKER)

    request = GenRequest(
        system=system,
        messages=[{"role": "user", "content": _context(character_name, draft, assembled_parts)}],
        sampling=definition.sampling,
        pass_id=definition.id,
    )
    try:
        result = await asyncio.wait_for(provider.generate(request), timeout=timeout)
    except (ProviderError, asyncio.TimeoutError, OSError):
        return draft, None

    text, _thinking = split_thinking(result.text)
    body, raw_proposal = _split_track_suffix(text) if track_state else (text, None)
    candidate = clean_reply(body, strip_leakage=False).strip()
    edited = candidate if _looks_like_an_edit(draft, candidate) else draft

    proposal = (
        validate_proposal(raw_proposal, existing_variables or [], schema_size)
        if track_state
        else None
    )
    return edited, proposal
