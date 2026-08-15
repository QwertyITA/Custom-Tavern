"""Per-backend instruct templates (§13).

ChatML / Llama3 / Mistral disagree about turn framing, and using the wrong one
degrades output in ways that look like a bad prompt rather than a bad template.
Chat-native APIs skip all of this and take the message list directly.
"""

from __future__ import annotations

from collections.abc import Callable

Message = dict[str, str]


def _chatml(system: str, messages: list[Message], prefill: str = "") -> str:
    parts = []
    if system:
        parts.append(f"<|im_start|>system\n{system}<|im_end|>\n")
    for msg in messages:
        parts.append(f"<|im_start|>{msg['role']}\n{msg['content']}<|im_end|>\n")
    parts.append("<|im_start|>assistant\n" + prefill)
    return "".join(parts)


def _llama3(system: str, messages: list[Message], prefill: str = "") -> str:
    parts = ["<|begin_of_text|>"]
    if system:
        parts.append(
            f"<|start_header_id|>system<|end_header_id|>\n\n{system}<|eot_id|>"
        )
    for msg in messages:
        parts.append(
            f"<|start_header_id|>{msg['role']}<|end_header_id|>\n\n"
            f"{msg['content']}<|eot_id|>"
        )
    parts.append("<|start_header_id|>assistant<|end_header_id|>\n\n" + prefill)
    return "".join(parts)


def _mistral(system: str, messages: list[Message], prefill: str = "") -> str:
    """Mistral has no system turn — it folds into the first user instruction."""
    parts = ["<s>"]
    pending_system = system
    for msg in messages:
        if msg["role"] == "assistant":
            parts.append(f" {msg['content']}</s>")
        else:
            body = msg["content"]
            if pending_system:
                body = f"{pending_system}\n\n{body}"
                pending_system = ""
            parts.append(f"[INST] {body} [/INST]")
    if pending_system:  # system-only prompt
        parts.append(f"[INST] {pending_system} [/INST]")
    return "".join(parts) + (f" {prefill}" if prefill else "")


def _plain(system: str, messages: list[Message], prefill: str = "") -> str:
    parts = []
    if system:
        parts.append(f"{system}\n")
    for msg in messages:
        label = {"user": "User", "assistant": "Assistant", "system": "System"}.get(
            msg["role"], msg["role"].title()
        )
        parts.append(f"{label}: {msg['content']}")
    parts.append("Assistant: " + prefill)
    return "\n".join(parts)


TEMPLATES: dict[str, Callable[[str, list[Message], str], str]] = {
    "chatml": _chatml,
    "llama3": _llama3,
    "mistral": _mistral,
    "plain": _plain,
}

# Stop strings that keep a completion model from writing the user's next turn.
STOP_STRINGS: dict[str, list[str]] = {
    "chatml": ["<|im_end|>", "<|im_start|>"],
    "llama3": ["<|eot_id|>", "<|start_header_id|>"],
    "mistral": ["</s>", "[INST]"],
    "plain": ["\nUser:", "\nSystem:"],
}


def guess_template(model: str) -> str:
    """Best-effort template pick from a model name, for `template: "auto"`."""
    name = model.lower()
    if "llama-3" in name or "llama3" in name:
        return "llama3"
    if "mistral" in name or "mixtral" in name:
        return "mistral"
    if any(tag in name for tag in ("qwen", "chatml", "yi", "hermes", "openchat")):
        return "chatml"
    return "chatml"


def render(
    template: str,
    system: str,
    messages: list[Message],
    prefill: str = "",
    spec: dict | None = None,
) -> str:
    if template == "custom":
        return _custom(system, messages, prefill, spec)
    fn = TEMPLATES.get(template, _chatml)
    return fn(system, messages, prefill)


def stop_for(template: str, spec: dict | None = None) -> list[str]:
    """A custom template stops on its own turn markers, which is what the
    named ones do too — the sequences are just written down rather than
    hardcoded.

    Three of the eight boxes end a reply: the marker that closes the
    character's turn, and the two that would open yours. They are stripped
    first, because the padding around a marker is layout — a model emits
    `<|im_end|>` and may or may not follow it with the newline, and a stop
    string that includes the newline would sit there waiting for it.
    """
    if template == "custom":
        fields = custom_spec(spec)
        raw = (fields["assistant_suffix"], fields["user_prefix"], fields["user_suffix"])
        return [s for s in dict.fromkeys(x.strip() for x in raw) if s]
    return list(STOP_STRINGS.get(template, []))

# --------------------------------------------------------------- custom


# A template written out as the strings that go around each turn, rather than
# as code. Every named template above can be expressed in these seven fields,
# which is what makes them presets rather than special cases — and what lets
# someone who has never heard the phrase "instruct template" fill one in by
# looking at what a model's own documentation prints.
CUSTOM_FIELDS: list[dict[str, str]] = [
    {"key": "prompt_start", "label": "Very beginning of the prompt",
     "hint": "A start-of-text marker, if your model wants one. Usually blank."},
    {"key": "system_prefix", "label": "Before the instructions",
     "hint": "Opens the block that tells the model who it is."},
    {"key": "system_suffix", "label": "After the instructions",
     "hint": "Closes it."},
    {"key": "user_prefix", "label": "Before your message",
     "hint": "Marks a turn as yours."},
    {"key": "user_suffix", "label": "After your message", "hint": "Ends your turn."},
    {"key": "assistant_prefix", "label": "Before the reply",
     "hint": "Marks a turn as the character's."},
    {"key": "assistant_suffix", "label": "After the reply",
     "hint": "Ends their turn."},
    {"key": "reply_start", "label": "Start of the new reply",
     "hint": "The last thing in the prompt — where the model takes over."},
]

CUSTOM_KEYS = tuple(field["key"] for field in CUSTOM_FIELDS)


def custom_spec(raw: dict | None) -> dict[str, str]:
    """A full spec from whatever was stored, missing fields blank."""
    raw = raw if isinstance(raw, dict) else {}
    return {key: str(raw.get(key, "") or "") for key in CUSTOM_KEYS}


def _custom(system: str, messages: list[Message], prefill: str = "", spec: dict | None = None) -> str:
    fields = custom_spec(spec)
    parts: list[str] = [fields["prompt_start"]]
    if system:
        parts.append(f"{fields['system_prefix']}{system}{fields['system_suffix']}")
    for msg in messages:
        # A system message mid-conversation — the author's note, the state
        # block — is framed as the user speaking, because most instruct formats
        # have no third role and dropping the frame entirely runs it into the
        # previous turn.
        if msg["role"] == "assistant":
            parts.append(f"{fields['assistant_prefix']}{msg['content']}{fields['assistant_suffix']}")
        else:
            parts.append(f"{fields['user_prefix']}{msg['content']}{fields['user_suffix']}")
    parts.append(fields["reply_start"] + prefill)
    return "".join(parts)


# The presets, as data. Filling the boxes from one of these is how someone
# starts: pick the closest, then change the part their model disagrees about.
CUSTOM_PRESETS: dict[str, dict[str, str]] = {
    "chatml": {
        "prompt_start": "",
        "system_prefix": "<|im_start|>system\n", "system_suffix": "<|im_end|>\n",
        "user_prefix": "<|im_start|>user\n", "user_suffix": "<|im_end|>\n",
        "assistant_prefix": "<|im_start|>assistant\n", "assistant_suffix": "<|im_end|>\n",
        "reply_start": "<|im_start|>assistant\n",
    },
    "llama3": {
        "prompt_start": "<|begin_of_text|>",
        "system_prefix": "<|start_header_id|>system<|end_header_id|>\n\n",
        "system_suffix": "<|eot_id|>",
        "user_prefix": "<|start_header_id|>user<|end_header_id|>\n\n",
        "user_suffix": "<|eot_id|>",
        "assistant_prefix": "<|start_header_id|>assistant<|end_header_id|>\n\n",
        "assistant_suffix": "<|eot_id|>",
        "reply_start": "<|start_header_id|>assistant<|end_header_id|>\n\n",
    },
    # Not byte-identical to the built-in `mistral`, which folds the system
    # prompt into the first user instruction — a rule, not a pair of strings,
    # so the boxes cannot express it. This is the other common Mistral shape:
    # the system prompt as an instruction of its own. Both work; pick `mistral`
    # from the template list if you want the folding one.
    "mistral": {
        "prompt_start": "<s>",
        "system_prefix": "[INST] ", "system_suffix": " [/INST]",
        "user_prefix": "[INST] ", "user_suffix": " [/INST]",
        "assistant_prefix": " ", "assistant_suffix": "</s>",
        "reply_start": " ",
    },
    "plain": {
        "prompt_start": "",
        "system_prefix": "", "system_suffix": "\n\n",
        "user_prefix": "User: ", "user_suffix": "\n",
        "assistant_prefix": "Assistant: ", "assistant_suffix": "\n",
        "reply_start": "Assistant: ",
    },
}


def custom_presets() -> dict[str, dict[str, str]]:
    return {name: dict(spec) for name, spec in CUSTOM_PRESETS.items()}


def custom_fields() -> list[dict[str, str]]:
    return [dict(field) for field in CUSTOM_FIELDS]
