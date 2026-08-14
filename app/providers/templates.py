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


def render(template: str, system: str, messages: list[Message], prefill: str = "") -> str:
    fn = TEMPLATES.get(template, _chatml)
    return fn(system, messages, prefill)


def stop_for(template: str) -> list[str]:
    return list(STOP_STRINGS.get(template, []))
