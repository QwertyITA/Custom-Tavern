"""Talking video avatar, per reply (AVATAR-VIDEO-CONTRACT.md).

No lip-sync model runs here — the deploy target is a phone, and a model like
MuseTalk needs a real GPU. This module is a client for a service the user
runs on their own machine, in exactly the "flat `_url`/`_key` on Settings, no
tiers, plain httpx call, off until configured" shape `websearch.py` already
uses for the same reason (an external, self-hosted, non-LLM-shaped call has
no use for the provider/pass machinery built for prompts and sampling).

Two phases, matching the service's own reasoning for existing at all: a slow
one-time `prepare` per character (face detection, parsing, encoding an idle
loop — cached service-side) and a fast `render` per reply that reuses it.
Skipping the split and re-preparing on every line would be real-time in name
only. Both are fire-and-forget from their call sites — `main.py` after an
idle-loop upload, `passes/scheduler.py` after a reply — the same shape
`character_reactions.py` uses and for the same reason: a broken or
unreachable avatar service must never hold up an upload or a reply.

A render that fails, times out, or was never configured in the first place
is silent: the message simply keeps its ordinary static portrait. There is
no retry queue here the way there is for reaction lines — a missed video is
gone, not owed to the next turn, because it was an illustration of a specific
line that has already scrolled past by the time anyone would notice.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from .config import Settings
from .db import Database
from .models import Character
from .events import BUS
from . import repo

log = logging.getLogger(__name__)

POLL_INTERVAL = 3.0


def configured(settings: Settings) -> bool:
    return bool((settings.avatar_url or "").strip())


def _headers(settings: Settings) -> dict[str, str]:
    if settings.avatar_key:
        return {"Authorization": f"Bearer {settings.avatar_key}"}
    return {}


def _base(settings: Settings) -> str:
    return settings.avatar_url.rstrip("/")


def _set_prep_status(db: Database, character: Character, status: str) -> None:
    """Refetch before writing: `prepare` can run for a while, and a card
    edited in the meantime should not have that edit clobbered by a slow
    background write landing after it."""
    current = repo.get_character(db, character.id)
    if current is None:
        return
    current.avatar_video = current.avatar_video.model_copy(update={"prep_status": status})
    repo.save_character(db, current)


async def prepare(
    db: Database, settings: Settings, character: Character, idle_video_url: str
) -> None:
    """Tell the avatar service about a character's idle loop, then poll until
    it says the loop is ready to render against (or gives up).

    Called once per upload, never per reply — see the module docstring for
    why re-running this every line would defeat the entire point of the
    service being real-time.
    """
    if not configured(settings) or not character.avatar_video.enabled:
        return
    if not idle_video_url:
        return

    _set_prep_status(db, character, "pending")
    url = f"{_base(settings)}/avatars/{character.id}"
    try:
        async with httpx.AsyncClient(
            timeout=settings.avatar_timeout, headers=_headers(settings)
        ) as client:
            submit = await client.post(
                f"{url}/prepare", json={"idle_video_url": idle_video_url}
            )
            submit.raise_for_status()

            deadline = asyncio.get_running_loop().time() + settings.avatar_timeout
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                check = await client.get(f"{url}/status")
                check.raise_for_status()
                status = check.json().get("status")
                if status == "ready":
                    _set_prep_status(db, character, "ready")
                    return
                if status == "failed":
                    log.info("avatar prep failed for %s", character.id)
                    _set_prep_status(db, character, "failed")
                    return
                if asyncio.get_running_loop().time() > deadline:
                    log.info("avatar prep timed out for %s", character.id)
                    _set_prep_status(db, character, "failed")
                    return
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.info("avatar prep request failed for %s: %s", character.id, exc)
        _set_prep_status(db, character, "failed")


async def render_for_reply(
    db: Database, settings: Settings, character: Character,
    chat_id: str, message_id: str, text: str,
) -> None:
    """Render one reply as a talking clip and publish it to the chat's event
    bus once it lands — the same `BUS.publish` a background pass result
    reaches the client through, since a render can easily take longer than
    the reply it illustrates and so must be able to arrive after the turn's
    own request has already closed (§4.5)."""
    if not configured(settings) or not character.avatar_video.enabled:
        return
    if character.avatar_video.prep_status != "ready" or not text.strip():
        return

    url = f"{_base(settings)}/avatars/{character.id}"
    try:
        async with httpx.AsyncClient(
            timeout=settings.avatar_timeout, headers=_headers(settings)
        ) as client:
            submit = await client.post(
                f"{url}/render",
                json={"text": text, "voice": character.avatar_video.voice},
            )
            submit.raise_for_status()
            job_id = submit.json()["job_id"]

            deadline = asyncio.get_running_loop().time() + settings.avatar_timeout
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                check = await client.get(f"{_base(settings)}/jobs/{job_id}")
                check.raise_for_status()
                job = check.json()
                status = job.get("status")
                if status == "done":
                    video_url = job.get("video_url")
                    if video_url:
                        BUS.publish(chat_id, {
                            "type": "avatar_video",
                            "message_id": message_id,
                            "video_url": video_url,
                        })
                    return
                if status == "failed":
                    log.info("avatar render failed for %s: %s", character.id, job.get("error"))
                    return
                if asyncio.get_running_loop().time() > deadline:
                    log.info("avatar render timed out for %s", character.id)
                    return
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.info("avatar render request failed for %s: %s", character.id, exc)
