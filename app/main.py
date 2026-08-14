"""FastAPI application: routes, SSE streaming, static PWA hosting.

Runs on the phone at localhost:PORT and is reached only from the phone itself
(§2) — so there is no auth layer here by design. Do not expose this port.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import assembly, cards, config, memory as memory_store, providers, repo, state as state_mod
from .config import DATA_DIR, SETTINGS, STATIC_DIR, reload_settings
from .db import get_db
from .events import BUS
from .markup import parse_to_dicts
from .models import (
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
    return PassScheduler(db, SETTINGS)


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
                yield _sse(event)
        except asyncio.CancelledError:  # client navigated away mid-turn
            raise
        except Exception as exc:  # noqa: BLE001 — surface it to the client
            yield _sse({"type": "error", "error": repr(exc)})

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
        "tier_names": list(config.TIERS),
        "kind_defaults": config.kind_defaults(),
        "path": str(config.settings_path()),
    }


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
    return {"ok": True, "settings": settings.to_dict()}


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
        # str(exc) can carry a base_url with an embedded token; mask it.
        return {"ok": False, "error": config.MASK.join(str(exc).split(backend.api_key))
                if backend.api_key else str(exc)}
    finally:
        await provider.aclose()

    return {
        "ok": True,
        "model": result.model or backend.model,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "sample": result.text[:120],
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


@app.get("/api/characters/{character_id}/export")
async def export_character(character_id: str) -> dict:
    character = repo.get_character(get_db(), character_id)
    if character is None:
        raise HTTPException(404, "character not found")
    return cards.to_card_json(character)


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
    # part in context assembly and can be swiped like any other.
    if character.first_mes.strip():
        repo.add_message(db, chat["id"], "assistant", character.first_mes.strip(), turn=0)
    return chat


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
        "messages": repo.list_messages(db, chat_id, include_dropped=False),
        "state": {
            "values": values,
            "bands": [
                {"variable": label, "band": band, "guidance": guidance}
                for label, band, guidance in state_mod.band_guidance(schema, values)
            ],
        },
        "slices": state_mod.read_all_slices(db, chat_id),
        "summary": repo.get_summary(db, chat_id),
        "toggles": registry.toggle_states(db, chat["character_id"], chat_id),
    }


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str) -> dict:
    repo.delete_chat(get_db(), chat_id)
    return {"ok": True}


@app.get("/api/chats/{chat_id}/messages")
async def list_messages(chat_id: str, include_dropped: bool = False) -> list[dict]:
    return repo.list_messages(get_db(), chat_id, include_dropped=include_dropped)


@app.get("/api/chats/{chat_id}/state")
async def chat_state(chat_id: str) -> dict:
    db = get_db()
    return {
        "slices": state_mod.read_all_slices(db, chat_id),
        "summary": repo.get_summary(db, chat_id),
    }


@app.post("/api/chats/{chat_id}/send")
async def send_message(chat_id: str, payload: SendMessageRequest):
    if repo.get_chat(get_db(), chat_id) is None:
        raise HTTPException(404, "chat not found")
    if not payload.text.strip():
        raise HTTPException(400, "empty message")
    return await _stream(scheduler().run_turn(chat_id, payload.text.strip()))


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
    repo.delete_message(get_db(), message_id)
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
