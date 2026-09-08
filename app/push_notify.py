"""Web Push (§ notifyReply's own history, static/app.js/sw.js) — a real
OS-level notification for a reply that arrives while the tab's own
JavaScript is not running to notice it, which is the ordinary state of a
backgrounded tab on Android within seconds of being minimized. A
page-driven approach — the first version of this feature — cannot reach
that case at all: there is no script alive to run it. Web Push routes
through the browser's own push service instead, which wakes the *service
worker*, a separate context Android does not freeze the same way.

RFC 8291 (message encryption) and RFC 8292 (VAPID), via `webpush` rather
than the more common `pywebpush` — see requirements.txt for why.
"""

from __future__ import annotations

import logging

import httpx

from . import config, markup

log = logging.getLogger(__name__)

# A syntactically valid address is all VAPID's `sub` claim needs (§ RFC 8292)
# — it is contact information a push service could use to reach the sender
# in the event of abuse, never mail this app sends or receives. Real enough
# to pass EmailStr validation, generic enough that it doesn't imply an inbox
# anyone is reading.
_VAPID_SUBSCRIBER = "tavern-app@example.com"


def _strip_actions(text: str) -> str:
    """The reply with *action* text removed — read at a glance in a
    notification, not acted out. Same idea as static/app.js's own
    actionFreeText from the page-driven version of this feature, mirrored
    here in Python (§ app/markup.py) rather than shared, since sending now
    happens server-side and never touches the page's own copy at all."""
    runs = markup.parse(text)
    body = "".join(run.text for run in runs if "action" not in run.styles)
    return " ".join(body.split())


def _send_one(wp, title: str, body: str, sub: dict) -> str | None:
    """One subscription, sent synchronously (this runs inside a worker
    thread — see notify_reply below). Returns the endpoint to drop if the
    push service says the subscription is gone (404/410 — uninstalled,
    revoked, or simply expired), None otherwise. Never raises: one dead or
    slow subscriber must not cost the others their notification, and a
    reply that already reached the screen must never be undone by a
    notification failing to send about it."""
    endpoint = sub.get("endpoint", "")
    try:
        from webpush import WebPushSubscription

        subscription = WebPushSubscription.model_validate(sub)
        message = wp.get(message={"title": title, "body": body}, subscription=subscription)
        response = httpx.post(
            str(subscription.endpoint),
            content=message.encrypted,
            headers={**message.headers, "content-type": "application/octet-stream"},
            timeout=10,
        )
        if response.status_code in (404, 410):
            return endpoint
        if response.status_code >= 300:
            log.info("push to %s failed: %s %s", endpoint[:60], response.status_code, response.text[:200])
    except Exception as exc:  # noqa: BLE001 — a courtesy, never fatal to the turn
        log.info("push to %s failed: %s", endpoint[:60], exc)
    return None


def send_all(settings: config.Settings, title: str, text: str) -> None:
    """Synchronous entry point — called via asyncio.to_thread (§
    scheduler.py's own call site), since both the encryption and the HTTP
    POST to the push service are blocking work httpx's sync client does
    here rather than pulling in a second, async-only push library for one
    call site.

    Never raises: this runs fire-and-forget after a reply is already on its
    way to the person who sent the message (§ scheduler.py's own call
    site), and a background notification failing to send must never be
    allowed to surface as a broken turn, an unhandled exception in a
    tracked task, or — the one this was actually caught doing — the whole
    process refusing to start.
    """
    if not settings.reply_notifications or not settings.push_subscriptions:
        return
    body = _strip_actions(text)
    if not body:
        return  # an action-only reply has nothing to say in a notification

    config.ensure_vapid_keys(settings)
    if not settings.vapid_private_key:
        return  # this install cannot do push at all (§ config._webpush_vapid)

    try:
        from webpush import WebPush

        wp = WebPush(
            private_key=settings.vapid_private_key.encode(),
            public_key=settings.vapid_public_key_pem.encode(),
            subscriber=_VAPID_SUBSCRIBER,
        )

        dead = [
            endpoint
            for sub in list(settings.push_subscriptions)
            if (endpoint := _send_one(wp, title, body, sub)) is not None
        ]
        if dead:
            settings.push_subscriptions = [
                s for s in settings.push_subscriptions if s.get("endpoint") not in dead
            ]
            config.save_settings(settings)
    except Exception as exc:  # noqa: BLE001 — see this function's own docstring
        log.warning("push notifications failed: %s", exc)
