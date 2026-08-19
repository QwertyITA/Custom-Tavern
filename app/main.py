"""FastAPI application: routes, SSE streaming, static PWA hosting.

Runs on the phone at localhost:PORT and is reached only from the phone itself
(§2) — so there is no auth layer here by design. Do not expose this port.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import assembly, attachments, cards, chat_files, config, groups, macros
from . import memory as memory_store
from . import prompt_layout
from . import providers, regex_rules, repo, state as state_mod
from . import translation
from .config import DATA_DIR, STATIC_DIR, reload_settings
from .db import get_db
from .events import BUS
from .markup import parse_to_dicts
from .models import (
    AuthorsNote,
    Character,
    CreateChatRequest,
    EditMessageRequest,
    PassDef,
    Sampling,
    SendMessageRequest,
    ToggleRequest,
)
from .passes import registry
from .passes.scheduler import PassScheduler
from .providers import close_all

SCHEDULER: PassScheduler | None = None


def scheduler() -> PassScheduler:
    if SCHEDULER is None:  # pragma: no cover — set during lifespan
        raise RuntimeError("scheduler not initialised")
    return SCHEDULER


def bootstrap() -> PassScheduler:
    """Seed the pass/toggle library and import any cards sitting on disk."""
    db = get_db()
    registry.seed(db)
    existing = {c["name"] for c in repo.list_characters(db)}
    for character in cards.load_directory(DATA_DIR / "characters"):
        if character.name not in existing:
            repo.save_character(db, character)
    # config.SETTINGS, not the name imported at module load — saving rebinds
    # the one in config, and a scheduler built from the stale import would run
    # every turn against the settings this process started with.
    return PassScheduler(db, config.SETTINGS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global SCHEDULER
    SCHEDULER = bootstrap()
    yield
    await close_all()


app = FastAPI(title="Personal Tavern", version="0.1.0", lifespan=lifespan)


# ------------------------------------------------------------------- SSE


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def _stream(generator) -> StreamingResponse:
    async def body():
        try:
            async for event in generator:
                yield _sse(_lens(event))
        except asyncio.CancelledError:  # client navigated away mid-turn
            raise
        except Exception as exc:  # noqa: BLE001 — surface it to the client
            yield _sse({"type": "error", "error": repr(exc)})

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _lens(event: dict) -> dict:
    """Attach the drawn form to any message an event carries.

    Here rather than at each `yield` in the scheduler, so a display rule is not
    something a future event type can forget to apply — the alternative is a
    message that reads one way while it streams in and another way after a
    reload, which reads as the rule being broken.

    Deltas are deliberately untouched: a rule matching across a chunk boundary
    cannot be applied to half a match, and the finished message that follows a
    moment later is rewritten correctly.
    """
    if not config.SETTINGS.regex_rules and not translation.enabled(config.SETTINGS):
        return event
    for key in ("message", "variant"):
        body = event.get(key)
        if isinstance(body, dict) and body.get("text"):
            # A variant carries no role of its own; it is always a reply.
            event = {**event, key: _with_display(body, role="assistant")}
    return event


# ----------------------------------------------------------------- system


@app.get("/api/health")
async def health() -> dict:
    db = get_db()
    return {
        "ok": True,
        "characters": len(repo.list_characters(db)),
        "chats": len(repo.list_chats(db)),
    }


@app.get("/api/settings")
async def get_settings() -> dict:
    """Masked — the browser never receives a real key."""
    # config.SETTINGS, not the name imported at module load: saving rebinds the
    # one in config, and a stale copy here would show pre-save values.
    return {
        **config.SETTINGS.to_dict(),
        "kinds": list(config.VALID_KINDS),
        "templates": list(config.VALID_TEMPLATES),
        "think_modes": list(config.VALID_THINK),
        "tier_names": list(config.TIERS),
        "kind_defaults": config.kind_defaults(),
        "theme_tokens": config.theme_tokens(),
        "template_fields": _template_fields(),
        "template_presets": _template_presets(),
        # The full layout, not the sparse stored form: the panel needs each
        # section's band, label and note, and shipping them means the frontend
        # never holds a second copy of the section list to drift from this one.
        "prompt_sections": prompt_layout.normalise(config.SETTINGS.prompt_sections),
        "prompt_bands": prompt_layout.BANDS,
        "prompt_fixed": sorted(prompt_layout.FIXED_IDS),
        "regex_rules": regex_rules.normalise(config.SETTINGS.regex_rules),
        "regex_meta": regex_rules.catalogue(),
        "backgrounds": config.available_backgrounds(),
        "path": str(config.settings_path()),
    }


def _template_fields() -> list[dict]:
    from .providers import templates as template_mod

    return template_mod.custom_fields()


def _template_presets() -> dict:
    from .providers import templates as template_mod

    return template_mod.custom_presets()


def _safe_error(exc: Exception, api_key: str) -> str:
    """Error text with the key removed.

    A base_url can carry a token in its path or query, and httpx puts the URL
    in the exception message — so a failed probe is a plausible way for a key
    to end up on screen, in a screenshot, or in a bug report.
    """
    text = str(exc) or exc.__class__.__name__
    return text.replace(api_key, config.MASK) if api_key else text


def _adopt(settings) -> None:
    config.apply_settings(settings)
    if SCHEDULER is not None:
        SCHEDULER.settings = settings


@app.put("/api/settings")
async def put_settings(payload: dict = Body(...)) -> dict:
    """Save settings.json. Keys sent back as *** keep their stored value."""
    try:
        settings = config.build_settings(payload, config.SETTINGS)
        config.save_settings(settings)
    except config.SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"could not write settings: {exc}") from exc

    _adopt(settings)
    # Cached providers hold connections built from the old config.
    await close_all()
    saved = settings.to_dict()
    # Same shape the GET hands out — the panel merges this straight into its
    # own state, and the sparse stored form would leave it with rows that have
    # no label and no band to sit in.
    saved["prompt_sections"] = prompt_layout.normalise(settings.prompt_sections)
    return {"ok": True, "settings": saved}


@app.post("/api/settings/test")
async def test_backend(payload: dict = Body(...)) -> dict:
    """Probe one backend so a key can be checked before it is relied on.

    Body is a single backend object. A masked key resolves against what is
    already stored, so an untouched key can be tested without retyping it.
    """
    try:
        backend = config.merge_backend(payload, config.SETTINGS.backends)
    except config.SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc

    provider = providers.build(backend)
    request = providers.GenRequest(
        system="Reply with the single word: ok",
        messages=[{"role": "user", "content": "ping"}],
        # Horde rejects max_length < 16 and temperature 0; the provider clamps
        # anyway, but asking for something valid keeps the probe honest.
        sampling=Sampling(max_tokens=16, temp=0.1),
        pass_id="connection_test",
    )
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(provider.generate(request), timeout=45)
    except (providers.ProviderError, asyncio.TimeoutError, OSError) as exc:
        return {"ok": False, "error": _safe_error(exc, backend.api_key)}
    finally:
        await provider.aclose()

    return {
        "ok": True,
        "model": result.model or backend.model,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "sample": result.text[:120],
    }


@app.post("/api/settings/models")
async def discover_models(payload: dict = Body(...)) -> dict:
    """Ask a backend which models it can actually serve.

    Same body shape and masked-key handling as the connection test, so the
    model list can be pulled for a stored backend without retyping its key.
    """
    try:
        backend = config.merge_backend(payload, config.SETTINGS.backends)
    except config.SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc

    provider = providers.build(backend)
    try:
        models = await asyncio.wait_for(provider.list_models(), timeout=30)
    except (providers.ProviderError, asyncio.TimeoutError, OSError) as exc:
        return {"ok": False, "models": [], "error": _safe_error(exc, backend.api_key)}
    finally:
        await provider.aclose()
    return {"ok": True, "models": models}


# Short enough to read whole on a phone, and long enough to show every box:
# a system prompt, both roles, and a turn boundary in each direction.
PREVIEW_SYSTEM = "You are Wren, who runs the ferry. Stay in character."
PREVIEW_SAMPLE = [
    {"role": "user", "content": "Is the ferry still running?"},
    {"role": "assistant", "content": '*She wipes the counter.* "Not in this wind."'},
    {"role": "user", "content": "Then I'll wait."},
]


@app.post("/api/regex/test")
async def test_regex(payload: dict = Body(...)) -> dict:
    """Run one rule against a sample (§16).

    Testable in place matters more here than anywhere else in the app: a rule
    with the wrong pattern in the input or output scope rewrites messages
    permanently, and finding out afterwards is not a recoverable position.
    """
    rules = regex_rules.normalise([payload.get("rule") or {}])
    if not rules:
        return {"ok": False, "error": "no pattern yet", "result": "", "matches": 0}
    return regex_rules.preview(rules[0], str(payload.get("sample") or ""))


@app.get("/api/samplers")
async def sampler_catalogue() -> dict:
    """What can be tuned, what it does, and which backends take it (§17).

    Served rather than duplicated in the frontend: a slider that exists for a
    parameter no backend is sent is worse than no slider.
    """
    from . import samplers

    return samplers.catalogue()


@app.post("/api/settings/template/preview")
async def preview_template(payload: dict = Body(...)) -> dict:
    """Show what a prompt would actually look like through this template.

    Rendered by the same function that runs for real, on a short made-up
    exchange. A preview drawn any other way is a second implementation that
    can disagree with the first, and the whole reason to show it is to be
    believed.
    """
    from .providers import templates as template_mod

    template = str(payload.get("template") or "custom")
    spec = payload.get("template_spec") or {}
    text = template_mod.render(template, PREVIEW_SYSTEM, list(PREVIEW_SAMPLE), spec=spec)
    return {
        "prompt": text,
        "stop": template_mod.stop_for(template, spec),
        "characters": len(text),
    }


@app.post("/api/settings/reload")
async def reload_config() -> dict:
    settings = reload_settings()
    if SCHEDULER is not None:
        SCHEDULER.settings = settings
    return settings.to_dict()


# -------------------------------------------------------------- characters


@app.get("/api/characters")
async def list_characters() -> list[dict]:
    return repo.list_characters(get_db())


@app.get("/api/characters/{character_id}")
async def get_character(character_id: str) -> dict:
    character = repo.get_character(get_db(), character_id)
    if character is None:
        raise HTTPException(404, "character not found")
    return json.loads(character.model_dump_json())


@app.post("/api/characters")
async def create_character(payload: dict = Body(default={})) -> dict:
    """A blank character to fill in from the editor.

    Importing a card is still the fast path, but a card file is a poor
    requirement for someone who just wants to write a character on the phone
    they are holding.
    """
    name = str(payload.get("name") or "New character").strip() or "New character"
    character = Character(id=repo.new_id(), name=name)
    repo.save_character(get_db(), character)
    return {"id": character.id, "name": character.name}


@app.put("/api/characters/{character_id}")
async def update_character(character_id: str, payload: dict = Body(...)) -> dict:
    """Edit the written parts of a card.

    Merged onto the stored character rather than replacing it: portraits,
    backgrounds, lorebook, state schema and nudges come from the card and are
    not editable here, and a PUT that dropped them would quietly destroy the
    parts of an imported character the editor cannot show.
    """
    db = get_db()
    character = repo.get_character(db, character_id)
    if character is None:
        raise HTTPException(404, "character not found")

    editable = (
        "name", "persona", "first_mes", "example_dialogue", "scenario",
        "system_prompt", "post_history_instructions",
    )
    for field in editable:
        if field in payload:
            setattr(character, field, str(payload[field] or ""))
    if "stop_strings" in payload:
        try:
            character.stop_strings = config.parse_stop_strings(payload["stop_strings"])
        except config.SettingsError as exc:
            raise HTTPException(400, str(exc)) from exc
    if "authors_note" in payload:
        try:
            character.authors_note = AuthorsNote.model_validate(payload["authors_note"] or {})
        except ValueError as exc:
            raise HTTPException(400, f"invalid author's note: {exc}") from exc
    if "alternate_greetings" in payload:
        raw = payload["alternate_greetings"]
        # Accepted as a list or as one textarea's worth of blank-line-separated
        # paragraphs, because that is what the editor can reasonably offer.
        if isinstance(raw, str):
            raw = [part for part in raw.split("\n\n")]
        character.alternate_greetings = [
            str(g).strip() for g in (raw or []) if str(g).strip()
        ]
    if not character.name.strip():
        raise HTTPException(400, "a character needs a name")

    repo.save_character(db, character)
    return json.loads(character.model_dump_json())


@app.post("/api/characters/{character_id}/favourite")
async def set_favourite(character_id: str, payload: dict = Body(...)) -> dict:
    """Star a character so it sorts to the top (§11).

    Tags and folders were deliberately not built. One flag answers the question
    anyone actually has of a roster this size — which of these do I use — and a
    taxonomy for a dozen characters is more work to maintain than to scroll.
    """
    db = get_db()
    if repo.get_character(db, character_id) is None:
        raise HTTPException(404, "character not found")
    favourite = bool(payload.get("favourite", True))
    repo.set_favourite(db, character_id, favourite)
    return {"ok": True, "favourite": favourite}


@app.post("/api/characters/import")
async def import_character(request: Request, filename: str = Query("card.json")) -> dict:
    """Import a card. Body is the raw file (JSON or PNG) — no multipart needed."""
    payload = await request.body()
    if not payload:
        raise HTTPException(400, "empty upload")
    try:
        character = cards.from_bytes(payload, filename)
    except cards.CardError as exc:
        raise HTTPException(400, str(exc)) from exc
    repo.save_character(get_db(), character)
    return {"id": character.id, "name": character.name}


def _card_filename(name: str) -> str:
    """A filename a phone file manager will accept, from any character name."""
    safe = "".join(c if (c.isalnum() or c in " -_") else "_" for c in name).strip()
    return f"{(safe or 'character').replace(' ', '_')}.card.json"


@app.get("/api/characters/{character_id}/export")
async def export_character(character_id: str, download: bool = False) -> JSONResponse:
    """The card as JSON. `download=1` makes the browser save it as a file.

    Two behaviours from one route because the same bytes serve both callers:
    the GUI's export button wants a download, and anything scripting against
    the API wants the object.
    """
    character = repo.get_character(get_db(), character_id)
    if character is None:
        raise HTTPException(404, "character not found")

    card = cards.to_card_json(character)
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{_card_filename(character.name)}"'
    return JSONResponse(card, headers=headers)


@app.delete("/api/characters/{character_id}")
async def delete_character(character_id: str) -> dict:
    repo.delete_character(get_db(), character_id)
    return {"ok": True}


@app.get("/api/characters/{character_id}/memories")
async def list_memories(character_id: str) -> list[dict]:
    return memory_store.list_all(get_db(), character_id)


@app.delete("/api/memories/{memory_id}")
async def forget_memory(memory_id: str) -> dict:
    memory_store.forget(get_db(), memory_id)
    return {"ok": True}


# ------------------------------------------------------------------- chats


@app.get("/api/chats")
async def list_chats(character_id: str | None = None) -> list[dict]:
    return repo.list_chats(get_db(), character_id)


@app.post("/api/chats")
async def create_chat(payload: CreateChatRequest) -> dict:
    db = get_db()
    character = repo.get_character(db, payload.character_id)
    if character is None:
        raise HTTPException(404, "character not found")
    chat = repo.create_chat(db, payload.character_id, payload.title or character.name)
    # The greeting loads at chat start (§7.4) and is a real message, so it takes
    # part in context assembly and can be swiped like any other. Its macros are
    # resolved once, here: a message is a record of something that was said, and
    # rewriting it later because a persona was renamed would falsify the
    # transcript.
    ctx = assembly.macro_context(db, chat, character)
    openings = [character.first_mes, *character.alternate_greetings]
    openings = [macros.substitute(o.strip(), ctx) for o in openings]
    openings = [o for o in openings if o]
    if openings:
        message = repo.add_message(db, chat["id"], "assistant", openings[0], turn=0)
        # The card's other openings become swipe variants of the same message,
        # so choosing between them is the gesture that already exists rather
        # than a picker that only ever appears once per chat. add_variant makes
        # each new one active, so the first is re-selected at the end to leave
        # the card's preferred greeting showing.
        for alternate in openings[1:]:
            repo.add_variant(db, message["id"], alternate)
        if len(openings) > 1:
            first = repo.list_variants(db, message["id"])[0]
            repo.set_active_variant(db, message["id"], first["id"])
    return chat


# These two sit above /api/chats/{chat_id} on purpose: routes match in the
# order they are declared, so "search" and "import" would otherwise be read
# as chat ids and 404.
@app.get("/api/chats/search")
async def search_chats(q: str = "", limit: int = 40) -> list[dict]:
    """Chats matching `q` in their title or their messages (§10)."""
    return repo.search_chats(get_db(), q, limit=max(1, min(limit, 100)))


@app.post("/api/chats/import")
async def import_chat(request: Request, character_id: str = Query("")) -> dict:
    """Body is the raw exported JSON — same shape as the card import, and for
    the same reason: multipart would mean a form parser dependency."""
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "empty upload")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"unreadable file: {exc}") from exc
    try:
        chat = chat_files.import_chat(get_db(), payload, character_id=character_id)
    except chat_files.ChatFileError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "chat": chat}


@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: str) -> dict:
    db = get_db()
    chat = repo.get_chat(db, chat_id)
    if chat is None:
        raise HTTPException(404, "chat not found")
    character = repo.get_character(db, chat["character_id"])
    schema = state_mod.load_schema(
        {k: v.model_dump() for k, v in character.state_schema.items()}
        if character and character.state_schema
        else None
    )
    values = assembly.current_values(db, chat_id, schema)
    return {
        "chat": chat,
        "character": json.loads(character.model_dump_json()) if character else None,
        # The same shape /messages hands out. This is the route the app loads a
        # chat through, so anything missing here is missing until a reload that
        # happens to hit the other one — which is to say, never.
        "messages": await list_messages(chat_id),
        "state": {
            "values": values,
            "bands": [
                {"variable": label, "band": band, "guidance": guidance}
                for label, band, guidance in state_mod.band_guidance(schema, values)
            ],
        },
        # Keyed by plain slice name for this chat's character; how they are
        # stored is a storage concern the client has no business knowing (§15).
        "slices": state_mod.slices_for(db, chat_id, chat["character_id"]),
        "summary": repo.get_summary(db, chat_id),
        "toggles": registry.toggle_states(db, chat["character_id"], chat_id),
        "persona": repo.active_persona(db, chat),
        "persona_id": chat.get("persona_id", ""),
    }


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str) -> dict:
    db = get_db()
    repo.delete_chat(db, chat_id)
    # The cascade removed the attachment rows but not the image files, which
    # would otherwise sit on the phone forever with nothing pointing at them.
    attachments.delete_orphans(db)
    return {"ok": True}


@app.patch("/api/chats/{chat_id}")
async def rename_chat(chat_id: str, payload: dict = Body(...)) -> dict:
    db = get_db()
    if repo.get_chat(db, chat_id) is None:
        raise HTTPException(404, "chat not found")
    title = str(payload.get("title") or "").strip()
    repo.rename_chat(db, chat_id, title)
    return {"ok": True, "chat": repo.get_chat(db, chat_id)}


@app.get("/api/chats/{chat_id}/export")
async def export_chat(chat_id: str, download: bool = False) -> JSONResponse:
    db = get_db()
    try:
        payload = chat_files.export_chat(db, chat_id)
    except chat_files.ChatFileError as exc:
        raise HTTPException(404, str(exc)) from exc
    headers = {}
    if download:
        name = chat_files.filename_for(
            payload["chat"], (payload.get("character") or {}).get("name", "")
        )
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return JSONResponse(payload, headers=headers)


@app.get("/api/chats/{chat_id}/members")
async def chat_members(chat_id: str) -> dict:
    db = get_db()
    chat = repo.get_chat(db, chat_id)
    if chat is None:
        raise HTTPException(404, "chat not found")
    groups.ensure_member(db, chat_id, chat["character_id"])
    return {
        "members": groups.members(db, chat_id),
        "policy": (chat.get("settings") or {}).get("policy") or groups.DEFAULT_POLICY,
        "policies": groups.POLICIES,
    }


@app.post("/api/chats/{chat_id}/members")
async def add_chat_member(chat_id: str, payload: dict = Body(...)) -> dict:
    db = get_db()
    if repo.get_chat(db, chat_id) is None:
        raise HTTPException(404, "chat not found")
    character_id = str(payload.get("character_id") or "")
    if repo.get_character(db, character_id) is None:
        raise HTTPException(404, "character not found")
    groups.add_member(db, chat_id, character_id)
    return {"ok": True, "members": groups.members(db, chat_id)}


@app.patch("/api/chats/{chat_id}/members/{character_id}")
async def update_chat_member(
    chat_id: str, character_id: str, payload: dict = Body(...)
) -> dict:
    db = get_db()
    if repo.get_chat(db, chat_id) is None:
        raise HTTPException(404, "chat not found")
    groups.update_member(
        db, chat_id, character_id,
        muted=payload.get("muted") if "muted" in payload else None,
        talkativeness=payload.get("talkativeness") if "talkativeness" in payload else None,
    )
    return {"ok": True, "members": groups.members(db, chat_id)}


@app.delete("/api/chats/{chat_id}/members/{character_id}")
async def remove_chat_member(chat_id: str, character_id: str) -> dict:
    db = get_db()
    chat = repo.get_chat(db, chat_id)
    if chat is None:
        raise HTTPException(404, "chat not found")
    remaining = [
        m for m in groups.members(db, chat_id) if m["character_id"] != character_id
    ]
    # A chat with nobody in it has nobody to reply, and the way back from that
    # state is not obvious from the UI. Mute instead — that is what it is for.
    if not remaining:
        raise HTTPException(400, "someone has to be here — mute them instead")
    groups.remove_member(db, chat_id, character_id)
    return {"ok": True, "members": groups.members(db, chat_id)}


@app.put("/api/chats/{chat_id}/policy")
async def set_turn_policy(chat_id: str, payload: dict = Body(...)) -> dict:
    db = get_db()
    chat = repo.get_chat(db, chat_id)
    if chat is None:
        raise HTTPException(404, "chat not found")
    policy = str(payload.get("policy") or "")
    if policy not in groups.POLICY_IDS:
        raise HTTPException(400, f"unknown turn policy {policy!r}")
    settings = dict(chat.get("settings") or {})
    settings["policy"] = policy
    repo.update_chat_settings(db, chat_id, settings)
    return {"ok": True, "policy": policy}


@app.get("/api/chats/{chat_id}/messages")
async def list_messages(chat_id: str, include_dropped: bool = False) -> list[dict]:
    db = get_db()
    messages = repo.list_messages(db, chat_id, include_dropped=include_dropped)
    attached = attachments.for_chat(db, chat_id)
    return [
        {**_with_display(m), "attachments": attached.get(m["id"], [])}
        for m in messages
    ]


def _with_display(message: dict, role: str = "") -> dict:
    """What to draw for this message: `display` when it differs from `text`.

    Two things can produce one — a translation back into your reading language
    (roadmap 23) and a display-scope find/replace rule (§16) — and they layer
    in that order, because a rule about how things look should apply to what
    you are actually looking at.
    """
    """Attach the display-scope rewrite (§16), leaving `text` as it was stored.

    Two fields rather than one, because they answer different questions: the
    screen wants `display`, and editing, copying and the prompt all want the
    message that was actually said.
    """
    base = translation.for_screen({**message, "role": message.get("role") or role})
    shown = regex_rules.apply(
        config.SETTINGS.regex_rules, base, "display", message.get("role") or role
    )
    return {**message, "display": shown} if shown != message.get("text") else message


@app.get("/api/chats/{chat_id}/state")
async def chat_state(chat_id: str) -> dict:
    db = get_db()
    chat = repo.get_chat(db, chat_id)
    if chat is None:
        raise HTTPException(404, "chat not found")
    return {
        # Resolved for this chat's character, so a reader asking for
        # `state.expression` gets it without knowing how it is stored (§15).
        "slices": state_mod.slices_for(db, chat_id, chat["character_id"]),
        "all_slices": state_mod.read_all_slices(db, chat_id),
        "summary": repo.get_summary(db, chat_id),
    }


@app.post("/api/chats/{chat_id}/send")
async def send_message(chat_id: str, payload: SendMessageRequest):
    if repo.get_chat(get_db(), chat_id) is None:
        raise HTTPException(404, "chat not found")
    # A message that is only an attachment is a real thing to send — "look at
    # this" with a picture and no words. Only a message with neither is empty.
    if not payload.text.strip() and not payload.attachments:
        raise HTTPException(400, "empty message")
    return await _stream(
        scheduler().run_turn(
            chat_id, payload.text.strip(), payload.attachments, payload.speaker_id
        )
    )


PING_SECONDS = 20


@app.get("/api/chats/{chat_id}/events")
async def chat_events(chat_id: str, request: Request):
    """Ambient stream: background passes landing after the turn closed (§4.5).

    The loop checks for disconnection itself rather than waiting to be
    cancelled. A phone that backgrounds the PWA, or a tab that goes away, must
    not leave a subscriber and a generator alive on the server.
    """
    queue = BUS.subscribe(chat_id)

    async def generator():
        try:
            yield {"type": "connected", "chat_id": chat_id}
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=PING_SECONDS)
                except asyncio.TimeoutError:
                    yield {"type": "ping"}  # keeps the connection from idling out
                    continue
                yield event
        finally:
            BUS.unsubscribe(chat_id, queue)

    return await _stream(generator())


# ---------------------------------------------------------------- messages


@app.post("/api/chats/{chat_id}/retry")
async def retry_turn(chat_id: str):
    """Answer a message whose reply failed, without sending it twice."""
    if repo.get_chat(get_db(), chat_id) is None:
        raise HTTPException(404, "chat not found")
    return await _stream(scheduler().retry_turn(chat_id))


@app.post("/api/chats/{chat_id}/impersonate")
async def impersonate(chat_id: str):
    """Draft the user's next message. Streams like a turn, but writes nothing."""
    return await _stream(scheduler().run_impersonate(chat_id))


@app.post("/api/attachments")
async def upload_attachment(request: Request, filename: str = Query("file")) -> dict:
    """Stage a file for the next message (§19).

    Raw body, like the card import — multipart would mean a form parser
    dependency, and this has to install on a phone. The attachment has no
    message yet; sending claims it.
    """
    db = get_db()
    attachments.clear_stale_staged(db)
    data = await request.body()
    try:
        return attachments.store(db, None, data, filename)
    except attachments.AttachmentError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/attachments/{attachment_id}/file")
async def attachment_file(attachment_id: str) -> FileResponse:
    db = get_db()
    path = attachments.path_for(db, attachment_id)
    if path is None:
        raise HTTPException(404, "no such image")
    row = attachments.get(db, attachment_id) or {}
    return FileResponse(path, media_type=row.get("mime") or "application/octet-stream")


@app.delete("/api/attachments/{attachment_id}")
async def remove_attachment(attachment_id: str) -> dict:
    db = get_db()
    if not attachments.delete(db, attachment_id):
        raise HTTPException(404, "no such attachment")
    return {"ok": True}


@app.get("/api/messages/{message_id}/thinking")
async def message_thinking(message_id: str) -> dict:
    """What the model thought before it wrote this reply (§5.6).

    Never inline: reasoning is not what the character said, and a bubble that
    prints it has the character muttering their working out. It is kept because
    the question a reasoning model raises every turn — did it think, and what
    did it decide — has no other answer once the turn is over.
    """
    db = get_db()
    if repo.get_message(db, message_id) is None:
        raise HTTPException(404, "message not found")
    record = repo.thinking_for(db, message_id)
    if record is None or not record["thinking"].strip():
        return {
            "ok": False,
            "reason": "This reply came back with no reasoning attached. Either "
            "the model does not think out loud, or Thinking is off for the "
            "backend that answered.",
        }
    return {"ok": True, **record}


@app.get("/api/messages/{message_id}/prompt")
async def message_prompt(message_id: str) -> dict:
    """What was actually sent to produce this reply, section by section (§15).

    The record, not a re-assembly: rebuilding it now would use today's state,
    today's memories and today's layout, and quietly answer a different
    question than the one being asked.
    """
    db = get_db()
    if repo.get_message(db, message_id) is None:
        raise HTTPException(404, "message not found")
    record = repo.prompt_record(db, message_id)
    if record is None:
        return {
            "ok": False,
            "kept_turns": repo.PROMPT_HISTORY_TURNS,
            "reason": "This one is too far back — only the last "
            f"{repo.PROMPT_HISTORY_TURNS} turns keep their prompt.",
        }
    # `total_tokens` is the sum of the rows on screen and always adds up to
    # them; `tokens_in` is what the backend counted for the same prompt. They
    # differ by a token or two because our estimate rounds per section, and
    # both are shown rather than one being quietly presented as the other.
    record["total_tokens"] = sum(p["tokens"] for p in record["parts"])
    return {"ok": True, **record}


@app.post("/api/messages/{message_id}/hidden")
async def set_hidden(message_id: str, payload: dict = Body(...)) -> dict:
    """Keep a message on screen but out of the prompt.

    Useful for an out-of-character aside, or a reply that went somewhere the
    story should not remember — deleting it would lose it, and editing it to
    nothing is not the same as it never having been said.
    """
    db = get_db()
    if repo.get_message(db, message_id) is None:
        raise HTTPException(404, "message not found")
    hidden = bool(payload.get("hidden", True))
    repo.set_message_hidden(db, message_id, hidden)
    return {"ok": True, "hidden": hidden}


@app.post("/api/messages/{message_id}/continue")
async def continue_reply(message_id: str):
    """Extend a reply in place rather than branching from it."""
    return await _stream(scheduler().run_continue(message_id))


@app.post("/api/messages/{message_id}/swipe")
async def swipe(message_id: str):
    return await _stream(scheduler().run_swipe(message_id))


@app.get("/api/messages/{message_id}/variants")
async def variants(message_id: str) -> list[dict]:
    return repo.list_variants(get_db(), message_id)


@app.post("/api/messages/{message_id}/variants/{variant_id}")
async def choose_variant(message_id: str, variant_id: str) -> dict:
    db = get_db()
    message = repo.get_message(db, message_id)
    if message is None:
        raise HTTPException(404, "message not found")
    # Landing on a different variant means the state written for the one we are
    # leaving must not stick around (§9).
    await state_mod.rollback_turn(db, message["chat_id"], message["turn"], message["variant_id"])
    if not repo.set_active_variant(db, message_id, variant_id):
        raise HTTPException(404, "variant not found")
    return repo.get_message(db, message_id)


@app.patch("/api/messages/{message_id}")
async def edit_message(message_id: str, payload: EditMessageRequest) -> dict:
    db = get_db()
    message = repo.get_message(db, message_id)
    if message is None:
        raise HTTPException(404, "message not found")
    repo.update_variant_text(db, message["variant_id"], payload.text)
    result = repo.get_message(db, message_id)
    if payload.reaudit:
        await scheduler().reaudit(message_id)
    return result


@app.delete("/api/messages/{message_id}")
async def delete_message(message_id: str) -> dict:
    db = get_db()
    repo.delete_message(db, message_id)
    attachments.delete_orphans(db)
    return {"ok": True}


@app.post("/api/render")
async def render_markup(payload: dict = Body(...)) -> dict:
    """Server-side markup parse — handy for debugging the tokenizer (§8)."""
    return {"runs": parse_to_dicts(str(payload.get("text", "")))}


# ------------------------------------------------------- passes & toggles


@app.get("/api/passes")
async def list_passes() -> list[dict]:
    return [json.loads(p.model_dump_json()) for p in registry.all_passes(get_db())]


@app.put("/api/passes/{pass_id}")
async def upsert_pass(pass_id: str, payload: dict = Body(...)) -> dict:
    payload["id"] = pass_id
    try:
        definition = PassDef.model_validate(payload)
    except ValueError as exc:
        raise HTTPException(400, f"invalid pass definition: {exc}") from exc
    await registry.save_pass(get_db(), definition)
    return json.loads(definition.model_dump_json())


@app.delete("/api/passes/{pass_id}")
async def remove_pass(pass_id: str) -> dict:
    deleted = await registry.delete_pass(get_db(), pass_id)
    return {"deleted": deleted, "disabled": not deleted}


def _safe_upload_name(filename: str, taken: set[str], fallback: str) -> str:
    """A filename rebuilt from scratch, never overwriting an existing one.

    Rebuilt rather than sanitised: the stem is reduced to characters that
    cannot escape a directory however they are combined, so there is no
    traversal to get wrong. Two photos called IMG_0001 are two images.
    """
    suffix = Path(filename).suffix.lower()
    stem = "".join(c for c in Path(filename).stem if c.isalnum() or c in "-_ ").strip()
    stem = stem.replace(" ", "-")[:60] or fallback
    name = f"{stem}{suffix}"
    counter = 2
    while name in taken:
        name = f"{stem}-{counter}{suffix}"
        counter += 1
    return name


# --------------------------------------------------------------- personas


@app.get("/api/personas")
async def list_personas() -> dict:
    db = get_db()
    return {"personas": repo.list_personas(db), "default": repo.default_persona(db)}


@app.post("/api/personas")
async def create_persona(payload: dict = Body(default={})) -> dict:
    db = get_db()
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "a persona needs a name")
    # The first one is the default by definition — there is nothing else for
    # {{user}} to fall back to.
    is_default = bool(payload.get("is_default")) or not repo.list_personas(db)
    return repo.save_persona(db, {**payload, "name": name, "is_default": is_default})


@app.put("/api/personas/{persona_id}")
async def update_persona(persona_id: str, payload: dict = Body(...)) -> dict:
    db = get_db()
    existing = repo.get_persona(db, persona_id)
    if existing is None:
        raise HTTPException(404, "persona not found")
    merged = {**existing, **payload, "id": persona_id}
    if not str(merged.get("name") or "").strip():
        raise HTTPException(400, "a persona needs a name")
    return repo.save_persona(db, merged)


@app.delete("/api/personas/{persona_id}")
async def remove_persona(persona_id: str) -> dict:
    db = get_db()
    if repo.get_persona(db, persona_id) is None:
        raise HTTPException(404, "persona not found")
    repo.delete_persona(db, persona_id)
    # Something has to be the default, or {{user}} silently loses its name.
    if not repo.default_persona(db):
        return {"ok": True, "default": None}
    remaining = repo.list_personas(db)
    if not any(p["is_default"] for p in remaining):
        repo.save_persona(db, {**remaining[0], "is_default": True})
    return {"ok": True, "default": repo.default_persona(db)}


@app.get("/api/chats/{chat_id}/note")
async def get_authors_note(chat_id: str) -> dict:
    """The note in force here, and whether it is this chat's or the card's."""
    db = get_db()
    chat = repo.get_chat(db, chat_id)
    if chat is None:
        raise HTTPException(404, "chat not found")
    character = repo.get_character(db, chat["character_id"])
    override = (chat.get("settings") or {}).get("authors_note")
    return {
        "note": json.loads(assembly.authors_note_for(chat, character).model_dump_json()),
        "from_chat": bool(isinstance(override, dict) and str(override.get("text") or "").strip()),
        "character_note": json.loads(character.authors_note.model_dump_json()),
    }


@app.put("/api/chats/{chat_id}/note")
async def set_authors_note(chat_id: str, payload: dict = Body(...)) -> dict:
    """Write this chat's own note. Empty text clears it back to the card's."""
    db = get_db()
    chat = repo.get_chat(db, chat_id)
    if chat is None:
        raise HTTPException(404, "chat not found")
    try:
        note = AuthorsNote.model_validate(payload or {})
    except ValueError as exc:
        raise HTTPException(400, f"invalid author's note: {exc}") from exc

    settings = dict(chat.get("settings") or {})
    if note.text.strip():
        settings["authors_note"] = json.loads(note.model_dump_json())
    else:
        settings.pop("authors_note", None)
    repo.update_chat_settings(db, chat_id, settings)
    return await get_authors_note(chat_id)


@app.post("/api/chats/{chat_id}/persona")
async def choose_chat_persona(chat_id: str, payload: dict = Body(...)) -> dict:
    """Who you are in this one conversation."""
    db = get_db()
    if repo.get_chat(db, chat_id) is None:
        raise HTTPException(404, "chat not found")
    persona_id = str(payload.get("persona_id") or "")
    if persona_id and repo.get_persona(db, persona_id) is None:
        raise HTTPException(404, "persona not found")
    repo.set_chat_persona(db, chat_id, persona_id)
    return {"ok": True, "persona": repo.active_persona(db, repo.get_chat(db, chat_id))}


@app.post("/api/characters/{character_id}/persona")
async def choose_character_persona(character_id: str, payload: dict = Body(...)) -> dict:
    """Who you usually are with this character — used by new chats with them."""
    db = get_db()
    if repo.get_character(db, character_id) is None:
        raise HTTPException(404, "character not found")
    persona_id = str(payload.get("persona_id") or "")
    if persona_id and repo.get_persona(db, persona_id) is None:
        raise HTTPException(404, "persona not found")
    repo.set_character_persona(db, character_id, persona_id)
    return {"ok": True, "persona_id": persona_id}


@app.get("/avatars/{filename}")
async def serve_avatar(filename: str) -> FileResponse:
    path = config.avatar_path(filename)
    if path is None:
        raise HTTPException(404, "avatar not found")
    return FileResponse(path)


@app.post("/api/avatars")
async def upload_avatar(request: Request, filename: str = Query(...)) -> dict:
    """Add a persona portrait. Body is the raw image, as everywhere else."""
    suffix = Path(filename).suffix.lower()
    if suffix not in config.BACKGROUND_SUFFIXES:
        allowed = ", ".join(config.BACKGROUND_SUFFIXES)
        raise HTTPException(400, f"unsupported image type {suffix or '(none)'} — use {allowed}")

    payload = await request.body()
    if not payload:
        raise HTTPException(400, "empty upload")
    if len(payload) > config.MAX_AVATAR_BYTES:
        limit = config.MAX_AVATAR_BYTES // (1024 * 1024)
        raise HTTPException(400, f"image is larger than {limit} MB")

    config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    name = _safe_upload_name(filename, set(config.user_avatars()), "avatar")
    try:
        (config.AVATAR_DIR / name).write_bytes(payload)
    except OSError as exc:
        raise HTTPException(500, f"could not save image: {exc}") from exc
    return {"name": name, "url": f"/avatars/{name}"}


# -------------------------------------------------------------- backdrops


@app.get("/api/backgrounds")
async def list_backgrounds() -> dict:
    """Every backdrop, and which of them the user may delete."""
    removable = set(config.user_backgrounds())
    return {
        "backgrounds": [
            {"name": name, "url": f"/backgrounds/{name}", "removable": name in removable}
            for name in config.available_backgrounds()
        ]
    }


@app.get("/backgrounds/{filename}")
async def serve_background(filename: str) -> FileResponse:
    """One route for both folders, so a chosen backdrop has one URL either way."""
    path = config.background_path(filename)
    if path is None:
        raise HTTPException(404, "background not found")
    return FileResponse(path)


@app.post("/api/backgrounds")
async def upload_background(request: Request, filename: str = Query(...)) -> dict:
    """Add a backdrop. Body is the raw image — no multipart needed.

    The name is rebuilt from scratch rather than sanitised: the extension has
    to be one we serve, and the stem is reduced to characters that cannot
    escape the directory however they are combined.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in config.BACKGROUND_SUFFIXES:
        allowed = ", ".join(config.BACKGROUND_SUFFIXES)
        raise HTTPException(400, f"unsupported image type {suffix or '(none)'} — use {allowed}")

    payload = await request.body()
    if not payload:
        raise HTTPException(400, "empty upload")
    if len(payload) > config.MAX_BACKGROUND_BYTES:
        limit = config.MAX_BACKGROUND_BYTES // (1024 * 1024)
        raise HTTPException(400, f"image is larger than {limit} MB")

    directory = config.USER_BACKGROUND_DIR
    directory.mkdir(parents=True, exist_ok=True)
    name = _safe_upload_name(filename, set(config.available_backgrounds()), "backdrop")

    try:
        (directory / name).write_bytes(payload)
    except OSError as exc:
        raise HTTPException(500, f"could not save image: {exc}") from exc
    return {"name": name, "url": f"/backgrounds/{name}"}


@app.delete("/api/backgrounds/{filename}")
async def remove_background(filename: str) -> dict:
    """Only uploads. The bundled art ships with the app and stays."""
    if filename not in config.user_backgrounds():
        raise HTTPException(404, "not an uploaded background")
    (config.USER_BACKGROUND_DIR / filename).unlink(missing_ok=True)

    # A setting pointing at a file that no longer exists would fail validation
    # on the next save, long after the cause.
    if config.SETTINGS.background == filename:
        config.SETTINGS.background = config.NO_BACKGROUND
        try:
            config.save_settings(config.SETTINGS)
        except OSError:
            pass
    return {"ok": True, "background": config.SETTINGS.background}


@app.post("/api/chats/{chat_id}/passes/{pass_id}/run")
async def run_pass(chat_id: str, pass_id: str) -> dict:
    """Run one pass now, without waiting for its trigger to fire.

    The world-info bar is written by a pass that only runs when the reply
    suggests the scene moved, which is right nearly always and wrong exactly
    when the user is looking at a stale line and knows it.
    """
    if SCHEDULER is None:
        raise HTTPException(503, "scheduler not ready")
    result = SCHEDULER.run_pass_now(chat_id, pass_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "could not run pass"))
    return result


@app.get("/api/toggles")
async def list_toggles(character_id: str = "", chat_id: str = "") -> dict:
    db = get_db()
    return {
        "toggles": [json.loads(t.model_dump_json()) for t in registry.all_toggles(db)],
        "states": registry.toggle_states(db, character_id, chat_id),
    }


@app.post("/api/toggles/{toggle_id}")
async def set_toggle(toggle_id: str, payload: ToggleRequest) -> dict:
    await registry.set_toggle(
        get_db(), toggle_id, payload.enabled, payload.scope, payload.scope_id
    )
    return {"ok": True}


# ------------------------------------------------------ HUD & cost (§12, §14)


@app.get("/api/chats/{chat_id}/runs")
async def pass_runs(chat_id: str, turn: int | None = None, limit: int = 60) -> list[dict]:
    db = get_db()
    if turn is not None:
        rows = db.query(
            "SELECT * FROM pass_runs WHERE chat_id=? AND turn=? ORDER BY started_at, rowid",
            (chat_id, turn),
        )
    else:
        rows = db.query(
            "SELECT * FROM pass_runs WHERE chat_id=? ORDER BY turn DESC, rowid DESC LIMIT ?",
            (chat_id, limit),
        )
    return [dict(row) for row in rows]


@app.get("/api/chats/{chat_id}/cost")
async def cost(chat_id: str) -> dict:
    """Proves the gating works: spend per pass and per turn (§14)."""
    db = get_db()
    per_pass = [
        dict(row)
        for row in db.query(
            "SELECT pass_id, tier, COUNT(*) AS runs, "
            "SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out, "
            "SUM(status='failed') AS failed, SUM(status='skipped') AS skipped "
            "FROM pass_runs WHERE chat_id=? GROUP BY pass_id, tier "
            "ORDER BY (SUM(tokens_in) + SUM(tokens_out)) DESC",
            (chat_id,),
        )
    ]
    per_turn = [
        dict(row)
        for row in db.query(
            "SELECT turn, COUNT(*) AS runs, SUM(tokens_in) AS tokens_in, "
            "SUM(tokens_out) AS tokens_out FROM pass_runs WHERE chat_id=? "
            "GROUP BY turn ORDER BY turn",
            (chat_id,),
        )
    ]
    totals = db.query_one(
        "SELECT COUNT(*) AS runs, COALESCE(SUM(tokens_in),0) AS tokens_in, "
        "COALESCE(SUM(tokens_out),0) AS tokens_out FROM pass_runs WHERE chat_id=?",
        (chat_id,),
    )
    return {"per_pass": per_pass, "per_turn": per_turn, "totals": dict(totals or {})}


# ------------------------------------------------------------------ static

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _static_file(name: str, media_type: str | None = None) -> FileResponse:
    path = STATIC_DIR / name
    if not path.exists():
        raise HTTPException(404, f"{name} not found")
    return FileResponse(path, media_type=media_type)


@app.get("/")
async def index() -> FileResponse:
    return _static_file("index.html", "text/html")


@app.get("/manifest.webmanifest")
async def manifest() -> FileResponse:
    return _static_file("manifest.webmanifest", "application/manifest+json")


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    # Must be served from the root so its scope covers the whole app.
    return _static_file("sw.js", "text/javascript")


@app.exception_handler(404)
async def not_found(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": getattr(exc, "detail", "not found")}, status_code=404)
