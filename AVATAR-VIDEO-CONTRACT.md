# Talking avatar service — HTTP contract

Custom Tavern can show a character's chat avatar as a lip-synced talking
video instead of a static portrait, generated per reply. It does not run
that generation itself — the deploy target is a phone, and a lip-sync model
(MuseTalk, LatentSync, or anything else that fits this shape) needs a real
GPU. Instead, Custom Tavern is a small HTTP client for a service **you**
run on your own machine, and this document is the contract between the two.

Written generically — no field or endpoint here assumes MuseTalk
specifically. Point `avatar_url` (in Settings) at anything that implements
this and it works the same way.

## Why two phases

MuseTalk's own design (and the reasoning applies to any similar model)
splits into a slow one-time step per avatar — face detection, face parsing,
VAE-encoding an idle loop — and a fast per-line step that reuses that work.
Skipping the split and redoing the slow step on every reply is the
difference between real-time and not. The contract mirrors that split:
`prepare` once per character, `render` on every reply after that.

## Reachability, both directions

Custom Tavern's own backend binds to `127.0.0.1` by default and is meant to
be reached only from the phone it runs on — so by default, nothing on your
network can reach *it* either, and `prepare` (below) has no way to hand your
avatar service a URL it can actually fetch. If you want the prepare step to
work, bind Custom Tavern's `host` setting to `0.0.0.0` (or your Tailscale
interface) and set `avatar_self_url` in Settings to whatever address your
avatar service should use to reach this app — see that setting's own note
for the fallback used when it is left blank, and why that fallback rarely
resolves to anything reachable in the typical everything-on-the-phone
deployment.

## Authentication

Every request below carries `Authorization: Bearer {avatar_key}` **if**
one is configured in Settings. `idle_video_url` and the `video_url` a
render eventually produces are a different matter: **the browser loads
those directly into a `<video>` tag**, which cannot attach a header. Serve
rendered files unauthenticated, trusted by network reachability (a
Tailscale ACL, a LAN-only bind) rather than by the same bearer token that
guards the control API.

## Endpoints

### `POST {avatar_url}/avatars/{avatar_id}/prepare`

Called once when a character's idle loop is uploaded or replaced.

Request:
```json
{ "idle_video_url": "https://your-tavern-host/avatar_idle/mira-loop.mp4" }
```

Response (immediately — preparation itself happens in the background):
```json
{ "status": "queued" }
```

### `GET {avatar_url}/avatars/{avatar_id}/status`

Polled by Custom Tavern after `prepare`, and checked before a `render` is
ever attempted.

Response:
```json
{ "status": "pending", "error": "" }
```
`status` is one of `pending`, `ready`, `failed`. `error` is a short,
human-readable string, present only when `status` is `failed`.

### `POST {avatar_url}/avatars/{avatar_id}/render`

Called after a reply's text is final. `avatar_id` must already be `ready`.

Request:
```json
{ "text": "Sit wherever. The fire's better on the left.", "voice": "mira-warm" }
```
`voice` is opaque to Custom Tavern — it is whatever the character's editor
has typed into the Voice field, and it means whatever your service decides
it means (a named TTS preset, a reference clip id, nothing at all). Custom
Tavern stores and forwards it; it does not validate or enumerate it.

Response:
```json
{ "job_id": "a1b2c3" }
```

### `GET {avatar_url}/jobs/{job_id}`

Polled Horde-style (a several-second interval, generous timeout — see
`app/providers/horde.py` for the existing precedent this follows) until
done, failed, or the client's own timeout budget (`avatar_timeout` in
Settings) runs out.

Response:
```json
{ "status": "done", "video_url": "https://your-tavern-host/render/xyz.mp4", "error": "" }
```
`status` is one of `pending`, `done`, `failed`. `video_url` is present only
when `status` is `done`; `error` only when `failed`.

## What Custom Tavern does with the result

The video plays once, inline, over the avatar of the one message it was
rendered for, muted only if the browser's autoplay policy demands it, and
the row reverts to the character's ordinary static portrait the moment
playback ends. No other row — scrollback, the roster, the enlarged
portrait view — ever shows video; at most one `<video>` element is ever
live at a time. A render that fails, times out, or was never configured in
the first place is silent: the message simply keeps its static portrait.

## What Custom Tavern will never send you

No audio. No Whisper features. No TTS request of any kind. `render`'s
`text` field is plain reply text — everything from there to a finished
video, including your own TTS step, is entirely your service's concern.
