"""The `echo` backend: a deterministic local stand-in for a real model.

It exists so the whole engine — streaming, the suffix contract, pass gating,
state writes, the HUD — runs end to end on a fresh clone with no network, no
Ollama and no keys. It is also what the test suite generates against, which
keeps the tests hermetic and fast.

It reads the pass prompt well enough to answer in the shape each canonical pass
expects, including the `<<<state>>>` suffix (§5.6).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator

from ..models import SIGNAL_LEVELS
from ..passes.contract import MARKER
from .base import GenRequest, GenResult, Provider, _copy_into, estimate_tokens

_WEATHER = ["clear", "overcast", "light rain", "windy", "still and cold"]
_TIME = ["early morning", "late morning", "early afternoon", "dusk", "late night"]
_PLACES = ["the tavern common room", "a quiet back room", "the road outside"]
_EMOTIONS = ["neutral", "happy", "sad", "angry", "surprised", "thoughtful"]
_EVENTS = [
    "Someone knocks twice at the door and does not wait to be asked in.",
    "The rain starts, hard enough to be heard on the roof.",
    "A glass goes over at the far end of the room.",
]


def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def _target(request: GenRequest) -> str:
    """The language a translation request asked for, read back out of its own
    system prompt — so the stand-in shows which way the text crossed."""
    match = re.search(r"into ([A-Za-z ]+?)\.", request.system)
    return match.group(1).strip() if match else "other"


def _allowed(request: GenRequest, label: str) -> str:
    """Pick the first option out of an 'Allowed <label>: a, b, c' line."""
    body = " ".join(m["content"] for m in request.messages)
    match = re.search(rf"Allowed {label}:\s*(.+)", body, re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).split("\n")[0].split(",")[0].strip()


class EchoProvider(Provider):
    kind = "echo"
    native_chat = True

    def __init__(self, config) -> None:
        super().__init__(config)
        self.delay = 0.0  # tests keep this at zero; set it to feel the UI

    # ------------------------------------------------------------------ shape

    def _last_user(self, request: GenRequest) -> str:
        for msg in reversed(request.messages):
            if msg["role"] == "user":
                return msg["content"]
        return ""

    def _reply_body(self, request: GenRequest) -> str:
        user = self._last_user(request).strip() or "…"
        seed = _seed(user)
        gesture = ["leans back", "turns a cup in one hand", "glances at the door"][seed % 3]
        return (
            f'*{gesture}* "You said: {user}" '
            f"*The words hang there a moment before the reply settles.*"
        )

    def _signals(self, request: GenRequest) -> dict:
        user = self._last_user(request)
        seed = _seed(user)
        return {
            "deltas": {"willingness": [0, 1, -1][seed % 3], "trust": [0, 0, 1][seed % 3]},
            "signals": {
                "narrative_drive": SIGNAL_LEVELS[seed % 3],
                "emotional_shift": SIGNAL_LEVELS[(seed // 3) % 3],
                "scene_change": SIGNAL_LEVELS[(seed // 7) % 3],
            },
        }

    def _structured(self, request: GenRequest) -> str:
        """Answer a structured pass in the shape that pass expects."""
        seed = _seed(request.pass_id + " " + " ".join(m["content"] for m in request.messages))

        if request.pass_id == "scene":
            return json.dumps(
                {
                    "place": _PLACES[seed % len(_PLACES)],
                    "weather": _WEATHER[seed % len(_WEATHER)],
                    "time": _TIME[seed % len(_TIME)],
                }
            )
        if request.pass_id == "expression":
            return json.dumps({"emotion": _EMOTIONS[seed % len(_EMOTIONS)]})
        if request.pass_id == "background_swap":
            return json.dumps({"background": _allowed(request, "backgrounds")})
        if request.pass_id == "summary":
            return json.dumps(
                {"summary": "They spoke at length; nothing was settled, but the mood shifted."}
            )
        if request.pass_id == "memory":
            facts = [
                {"text": "The user came in off the coast road in bad weather.",
                 "keys": ["road", "weather", "arrival"]},
                {"text": "The user is looking for a tall woman with a scar on her jaw.",
                 "keys": ["woman", "scar", "searching"]},
            ]
            return json.dumps({"memories": [facts[seed % len(facts)]]})
        if request.pass_id == "random_event":
            return json.dumps({"event": _EVENTS[seed % len(_EVENTS)]})
        if request.pass_id == "translate":
            # Not a translation — nothing here can translate. It marks the text
            # as having crossed, which is what the round trip is actually
            # testing: that both crossings happened and neither overwrote its
            # original.
            source = self._last_user(request)
            return json.dumps({"text": f"[{_target(request)}] {source}"})
        if request.pass_id == "state_auditor":
            payload = self._signals(request)
            payload["reason"] = "echo audit: deltas within personality bounds"
            return json.dumps(payload)
        return json.dumps(self._signals(request))

    def _compose(self, request: GenRequest) -> str:
        if request.expects_json:
            return self._structured(request)
        body = self._reply_body(request)
        if MARKER in request.system:
            body += "\n" + MARKER + json.dumps(self._signals(request))
        return body

    # --------------------------------------------------------------- provider

    async def list_models(self) -> list[str]:
        return ["echo-1"]

    async def generate(self, request: GenRequest) -> GenResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        text = self._compose(request)
        return GenResult(
            text=text,
            tokens_in=request.estimated_input_tokens(),
            tokens_out=estimate_tokens(text),
            model=self.model or "echo-1",
            provider=self.name,
        )

    async def stream(
        self, request: GenRequest, sink: GenResult | None = None
    ) -> AsyncIterator[str]:
        result = await self.generate(request)
        if sink is not None:
            _copy_into(result, sink)
        # Word-at-a-time so the client's streaming path is genuinely exercised.
        for chunk in re.findall(r"\S+\s*", result.text):
            if self.delay:
                await asyncio.sleep(self.delay / 20)
            yield chunk
