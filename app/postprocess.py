"""Output post-processing (§13).

Part of anti-slop lives here rather than in the prompt, because prompting a
model not to write the user's next turn is unreliable and deleting it is not.
Runs on the reply *after* the state suffix has been split off.
"""

from __future__ import annotations

import re

# Template scaffolding a model sometimes emits verbatim.
_ARTIFACTS = [
    re.compile(r"<\|im_(start|end)\|>"),
    re.compile(r"<\|eot_id\|>"),
    re.compile(r"<\|(start|end)_header_id\|>"),
    re.compile(r"^\s*</s>\s*"),
    re.compile(r"\[/?INST\]"),
]

# "User:" / "{{user}}:" starting a line — the model continuing our turn.
_LEAKAGE = re.compile(
    r"\n\s*(?:\{\{user\}\}|user|you|human)\s*:.*\Z",
    re.IGNORECASE | re.DOTALL,
)

_THINK = re.compile(r"<think>(.*?)</think>\s*", re.IGNORECASE | re.DOTALL)
_OPEN_THINK = re.compile(r"<think>.*\Z", re.IGNORECASE | re.DOTALL)


def split_thinking(text: str) -> tuple[str, str]:
    """Pull `<think>` blocks out of the visible reply (§5.6).

    Reasoning models put their scratchpad inline. It is captured for the HUD and
    removed from display — never shown in the message stream.
    """
    thoughts = [m.group(1).strip() for m in _THINK.finditer(text)]
    body = _THINK.sub("", text)
    # An unterminated block means the stream was cut mid-thought; drop the tail
    # rather than showing it.
    if _OPEN_THINK.search(body):
        thoughts.append(_OPEN_THINK.search(body).group(0)[len("<think>") :].strip())
        body = _OPEN_THINK.sub("", body)
    return body, "\n\n".join(t for t in thoughts if t)


def clean_reply(text: str, *, strip_leakage: bool = True, user_names: tuple[str, ...] = ()) -> str:
    """Strip template artifacts and any continuation of the user's turn."""
    body = text
    for pattern in _ARTIFACTS:
        body = pattern.sub("", body)
    if strip_leakage:
        body = _LEAKAGE.sub("", body)
        for name in user_names:
            if not name.strip():
                continue
            named = re.compile(rf"\n\s*{re.escape(name)}\s*:.*\Z", re.IGNORECASE | re.DOTALL)
            body = named.sub("", body)
    # Collapse the runaway blank lines that stop-token truncation leaves behind.
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()
