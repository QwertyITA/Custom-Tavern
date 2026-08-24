# Setting up a MuseTalk avatar service

A step-by-step path to a running service that implements
[AVATAR-VIDEO-CONTRACT.md](AVATAR-VIDEO-CONTRACT.md) on top of MuseTalk —
from a clean GPU machine to a character in Custom Tavern actually talking.

**None of this runs on the phone.** Everything below happens on your own
GPU machine (a 5090 or similar). Custom Tavern never gains a dependency
from this — it only ever holds a URL, a key, and an `httpx` client, exactly
like it already does for Ollama.

## 0. What you end up with

```
LLM reply text
   → this service's /render endpoint
       → Piper synthesises the line to a .wav
           → MuseTalk lip-syncs that audio onto your idle loop
               → an .mp4 comes back, Custom Tavern plays it once
```

MuseTalk itself does the lip-sync inpainting only — it has no network
server and no TTS. Both of those are the ~150 lines of glue in part 4
below. If you'd rather not run any of this yourself, that's the whole
point of the contract being a separate document: anything that answers the
same four endpoints works, MuseTalk or not.

## 1. Prerequisites

- A GPU with real headroom — MuseTalk itself uses roughly 8–12GB VRAM;
  budget for your LLM running at the same time on the same card (a
  quantized GGUF model via Ollama/llama.cpp fits comfortably alongside it
  on a 32GB card).
- **CUDA ≥ 11.7**, matching whatever PyTorch build you install.
- **Python ≥ 3.10.**
- **ffmpeg** on `PATH` (a static build is fine — MuseTalk shells out to it).
- Enough disk for model weights (a few GB) and rendered clips.

## 2. Install MuseTalk

```bash
git clone https://github.com/TMElyralab/MuseTalk.git
cd MuseTalk
git checkout main   # v1.5 (musetalkV15) lives on main as of this writing —
                     # confirm against the repo's own README if that's changed

conda create -n musetalk python=3.10 -y
conda activate musetalk
pip install -r requirements.txt

# Pose/parsing stack MuseTalk depends on but doesn't pin in requirements.txt:
pip install --no-cache-dir -U openmim
mim install mmengine
mim install "mmcv>=2.0.1"
mim install "mmdet>=3.1.0"
mim install "mmpose>=1.1.0"
```

Download the model weights using whichever script the repo ships
(`download_weights.sh` on Linux/macOS, `download_weights.bat` on Windows,
as of this writing) — it pulls the Whisper, VAE, DWPose, face-parsing and
MuseTalk checkpoints into `./models`. Run it and confirm `./models` is
populated before moving on.

**Sanity check before writing a single line of glue code:** run the
repo's own bundled sample —

```bash
python -m scripts.realtime_inference \
  --inference_config configs/inference/realtime.yaml \
  --batch_size 4
```

This should encode the sample avatar and render its sample audio clips
into `results/avatars/`. If this step doesn't work, nothing built on top
of it will either — fix it here first. While you're at it, note the exact
path a rendered clip landed at (`results/avatars/<avatar>/vid_output/
<clip-name>.mp4` as of this writing) — you'll confirm the wrapper server
in part 4 agrees with it.

## 3. Install Piper for TTS

MuseTalk needs audio to lip-sync; it has no opinion on where that audio
comes from. Piper is a small, fully local, CPU-friendly TTS engine — no
network calls, no per-line cost, good enough quality for this.

```bash
pip install piper-tts
python -m piper.download_voices en_US-lessac-medium
# an Italian voice, if you want one:
python -m piper.download_voices it_IT-riccardo-x_low
```

Confirm it actually works before wiring it into anything:

```bash
echo "Sit wherever. The fire's better on the left." | \
  piper --model en_US-lessac-medium.onnx --output_file test.wav
```

Play `test.wav`. If piper-tts's exact Python API differs from what's used
below (it has changed shape across releases), `piper --help` and the
project's own README are the source of truth — adjust the one function
that calls it in part 4 rather than fighting the version you have.

## 4. The wrapper server

A single FastAPI script implementing every endpoint
[AVATAR-VIDEO-CONTRACT.md](AVATAR-VIDEO-CONTRACT.md) defines. It shells
out to MuseTalk's own CLI entry point rather than importing its internals
— slower per call (model weights load fresh each invocation) but robust
to whatever a given checkout's internal API actually looks like, and it's
the exact invocation the project's own README already documents. If you
later want lower per-line latency, the natural next step is a long-lived
worker that keeps the models loaded between calls instead of restarting
per render — worth doing once the simple version is working, not before.

Two things in here are very likely to need a small adjustment for your
exact checkout, marked below: the demo audio filename used to warm up
`preparation`, and the output path a finished render lands at.

Save this next to your MuseTalk checkout (not inside the Custom-Tavern
repo — it has its own dependencies this repo deliberately never takes on):

```python
"""musetalk_avatar_server.py — reference implementation of
AVATAR-VIDEO-CONTRACT.md. Runs on the GPU machine. Requires: fastapi,
uvicorn, httpx, pyyaml, piper-tts — none of which Custom Tavern itself
depends on.

    uvicorn musetalk_avatar_server:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import subprocess
import uuid
import wave
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---- paths — point these at your own machine --------------------------
MUSETALK_DIR = Path("/home/you/MuseTalk")            # your clone from part 2
CONFIG_PATH = MUSETALK_DIR / "configs/inference/realtime_server.yaml"
RESULTS_DIR = MUSETALK_DIR / "results/avatars"       # where MuseTalk writes output
IDLE_DIR = Path("./idle_videos")                     # downloaded idle loops
AUDIO_DIR = Path("./audio")                          # Piper output
PIPER_VOICES_DIR = Path(".")                         # wherever `piper.download_voices` put yours

# Must be reachable from the phone — the same address you put in Custom
# Tavern's Settings → Avatar URL (AVATAR-VIDEO-CONTRACT.md's "Reachability,
# both directions").
PUBLIC_BASE_URL = "http://100.x.y.z:8000"

# A short clip that ships with MuseTalk, reused only to warm up the
# preparation pass — its content is irrelevant, only its existence matters.
# Confirm this filename against your own checkout's data/audio/ directory;
# it has moved between releases.
WARMUP_CLIP = MUSETALK_DIR / "data/audio/yongen.wav"

IDLE_DIR.mkdir(exist_ok=True)
AUDIO_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()
app.mount("/files", StaticFiles(directory=RESULTS_DIR), name="files")

AVATARS: dict[str, dict] = {}   # avatar_id -> {"status": "pending"|"ready"|"failed", "error"?: str}
JOBS: dict[str, dict] = {}      # job_id -> {"status": "pending"|"done"|"failed", "video_url"?: str, "error"?: str}


class PrepareBody(BaseModel):
    idle_video_url: str


class RenderBody(BaseModel):
    text: str
    voice: str = ""


def write_avatar_config(avatar_id: str, video_path: Path, preparation: bool,
                         audio_clips: dict[str, str]) -> Path:
    """One avatar's block, in the exact shape scripts.realtime_inference
    already reads (§2's sanity check ran against the same file shape)."""
    config = {
        avatar_id: {
            "preparation": preparation,
            "bbox_shift": 0,
            "video_path": str(video_path),
            "audio_clips": audio_clips,
        }
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(yaml.safe_dump(config))
    return CONFIG_PATH


def run_musetalk(config_path: Path) -> subprocess.CompletedProcess:
    """Blocking — always called via asyncio.to_thread, never straight from
    an async route. A subprocess, not an import, so a MuseTalk crash can
    never take this server down with it."""
    return subprocess.run(
        ["python", "-m", "scripts.realtime_inference",
         "--inference_config", str(config_path), "--batch_size", "4"],
        cwd=MUSETALK_DIR, capture_output=True, text=True,
    )


async def do_prepare(avatar_id: str, idle_path: Path) -> None:
    config_path = write_avatar_config(
        avatar_id, idle_path, preparation=True,
        audio_clips={"warmup": str(WARMUP_CLIP)},
    )
    result = await asyncio.to_thread(run_musetalk, config_path)
    if result.returncode == 0:
        AVATARS[avatar_id] = {"status": "ready"}
    else:
        AVATARS[avatar_id] = {"status": "failed", "error": result.stderr[-500:]}


@app.post("/avatars/{avatar_id}/prepare")
async def prepare(avatar_id: str, body: PrepareBody):
    idle_path = IDLE_DIR / f"{avatar_id}.mp4"
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(body.idle_video_url)
        response.raise_for_status()
        idle_path.write_bytes(response.content)

    AVATARS[avatar_id] = {"status": "pending"}
    asyncio.create_task(do_prepare(avatar_id, idle_path))
    return {"status": "queued"}


@app.get("/avatars/{avatar_id}/status")
async def status(avatar_id: str):
    return AVATARS.get(avatar_id) or {"status": "failed", "error": "never prepared"}


def synthesize_speech(text: str, voice: str, out_path: Path) -> None:
    """§3 — adjust this one function if your installed piper-tts exposes a
    different API than the one shown here."""
    from piper.voice import PiperVoice

    model_path = PIPER_VOICES_DIR / f"{voice or 'en_US-lessac-medium'}.onnx"
    voice_model = PiperVoice.load(str(model_path))
    with wave.open(str(out_path), "wb") as wav_file:
        voice_model.synthesize(text, wav_file)


async def do_render(avatar_id: str, job_id: str, text: str, voice: str) -> None:
    wav_path = AUDIO_DIR / f"{job_id}.wav"
    try:
        await asyncio.to_thread(synthesize_speech, text, voice, wav_path)
    except Exception as exc:  # noqa: BLE001 — a broken voice must not hang the job
        JOBS[job_id] = {"status": "failed", "error": f"tts: {exc}"}
        return

    idle_path = IDLE_DIR / f"{avatar_id}.mp4"
    config_path = write_avatar_config(
        avatar_id, idle_path, preparation=False, audio_clips={job_id: str(wav_path)},
    )
    result = await asyncio.to_thread(run_musetalk, config_path)
    if result.returncode != 0:
        JOBS[job_id] = {"status": "failed", "error": result.stderr[-500:]}
        return

    # Confirmed against §2's sanity check; adjust if your checkout differs.
    output = RESULTS_DIR / avatar_id / "vid_output" / f"{job_id}.mp4"
    if not output.exists():
        JOBS[job_id] = {"status": "failed", "error": f"musetalk wrote nothing at {output}"}
        return
    JOBS[job_id] = {
        "status": "done",
        "video_url": f"{PUBLIC_BASE_URL}/files/{avatar_id}/vid_output/{job_id}.mp4",
    }


@app.post("/avatars/{avatar_id}/render")
async def render(avatar_id: str, body: RenderBody):
    avatar = AVATARS.get(avatar_id)
    if avatar is None or avatar.get("status") != "ready":
        raise HTTPException(400, "avatar is not prepared yet")

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "pending"}
    asyncio.create_task(do_render(avatar_id, job_id, body.text, body.voice))
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
async def job(job_id: str):
    return JOBS.get(job_id) or {"status": "failed", "error": "unknown job"}
```

State lives in plain dicts — this is a personal, single-user tool running
next to your own MuseTalk checkout, not a service with concurrent tenants;
it does not survive a restart, and doesn't need to.

This reference script doesn't check the `Authorization: Bearer` header at
all, even though Custom Tavern sends it whenever `avatar_key` is set in
Settings — it trusts the network instead (a Tailscale ACL, a LAN-only
bind). If you want the header actually enforced, add a small FastAPI
dependency that compares it against an expected value and require it on
every route; the contract already defines the header's shape, this script
simply doesn't act on it.

Install its own dependencies (kept separate from both Custom Tavern's
`requirements.txt` and MuseTalk's own — this script's env just needs
`fastapi`, `uvicorn`, `httpx`, `pyyaml` on top of what parts 2 and 3
already installed):

```bash
pip install fastapi uvicorn httpx pyyaml
```

## 5. Running it

```bash
uvicorn musetalk_avatar_server:app --host 0.0.0.0 --port 8000
```

`--host 0.0.0.0` (not `127.0.0.1`) so the phone can actually reach it.

On the Custom Tavern side (Settings → Talking avatar, per
AVATAR-VIDEO-CONTRACT.md's "Reachability, both directions"):

- **Avatar URL** → this machine's address, e.g. its Tailscale IP:
  `http://100.a.b.c:8000` (must match `PUBLIC_BASE_URL` in the script above).
- **Reachable at** → the *phone's* address, so this server's `/prepare`
  step can fetch the idle loop back from it. Left blank, Custom Tavern
  falls back to whatever address the browser used to reach it, which is
  rarely useful once these are two different machines — set it explicitly.
- Custom Tavern's own `host` setting needs to be `0.0.0.0` too, or this
  server has nothing to fetch the idle loop from.

## 6. Sharing the GPU with your LLM

- Keep them as separate processes (already true here) — the NVIDIA driver
  time-slices between processes by default, which is normally enough for
  MuseTalk's modest footprint alongside an LLM.
- Only reach for NVIDIA MPS if you actually observe latency climbing under
  heavy concurrent load; it's not needed for a typical chat-plus-avatar
  workload.
- Rough VRAM budget: MuseTalk itself ~8–12GB. A quantized GGUF model
  (Q4/Q5/Q6 via Ollama or llama.cpp) fits comfortably alongside it on a
  32GB card.
- It's overlap more than true simultaneous saturation: while the LLM is
  still generating the next tokens, this server is idle; once the reply is
  final and `/render` fires, the LLM is typically idle again until the next
  turn.

## 7. Wiring it into a character

Everything past this point already exists in Custom Tavern — this is just
where the knobs are:

1. **Settings** → Avatar URL / Key (only if you add one to the script) /
   Reachable At, as above.
2. **A character's editor** → **Talking avatar**: switch it on, upload an
   idle loop (5–15s, blink and micro-breath — WAN 2.2 Animate, LivePortrait
   or SadTalker all work for generating one), and set **Voice** to a Piper
   voice name you downloaded in part 3 (`en_US-lessac-medium`,
   `it_IT-riccardo-x_low`, …) — this reference server treats `voice`
   literally as a Piper model name.
3. Save, wait for the editor's status readout to reach **Ready**, then send
   the character a message.

## 8. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `prep_status` stuck on "Preparing…" | This server's `prepare` subprocess crashed or is still running — check its own stderr in the terminal running uvicorn. |
| `prep_status` reaches "Preparation failed" | Check the `error` this server returned from `/avatars/{id}/status` — usually MuseTalk's own stderr, tail-clipped to 500 chars in the script above; widen that slice locally if you need more. |
| Nothing happens after sending a message, no error anywhere | `avatar_url` not configured, or the character's own **Talking avatar** switch is off — both make the feature a silent no-op by design (AVATAR-VIDEO-CONTRACT.md, "What Custom Tavern does with the result"). |
| Character editor never leaves "pending" after upload | The phone can't reach the avatar service, *or* the avatar service can't reach the phone to fetch the idle loop back — check both directions independently (§5, §6 of AVATAR-VIDEO-CONTRACT.md). |
| Render always fails with a `tts:` error | Piper voice name in the character's Voice field doesn't match a downloaded model's filename — re-check `python -m piper.download_voices` output against `PIPER_VOICES_DIR`. |
| Render fails with "musetalk wrote nothing" | Your checkout's output path differs from what §2's sanity check showed — fix the `output = ...` line in `do_render`. |
| Everything works but is slow (10s+ per line) | Expected with this reference server: each render reloads MuseTalk's models from a cold subprocess. See the note at the top of §4 on keeping a warm worker instead, once the simple version is confirmed working end to end. |
