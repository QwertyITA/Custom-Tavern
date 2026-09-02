#!/usr/bin/env bash
# Termux launcher with the hardening from §2.
#
# Most people should run ./start.sh instead — it updates first, then calls this.
#
# Android will happily kill a long-running server. The four things that matter:
#   * termux-wake-lock            — keeps the CPU alive
#   * battery set to Unrestricted — Settings > Apps > Termux > Battery
#   * a foreground notification   — makes the process visible to the OS
#   * tmux                        — survives the terminal session going away
#
# On Android 12+, also raise the phantom process limit once over ADB, or the
# system reaps background children after a while:
#   adb shell settings put global settings_enable_monitor_phantom_procs false
#
# Usage:  ./run.sh              start in this console (Ctrl+C to stop)
#         ./run.sh background   detach into tmux instead
#         ./run.sh stop         stop it
#         ./run.sh logs         follow the log
#
# Opens a browser tab on the running app once the port answers.
# TAVERN_NO_BROWSER=1 skips that.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="tavern"
DATA_DIR="${TAVERN_DATA_DIR:-$HERE/data}"
LOG="$DATA_DIR/tavern.log"
PYTHON="${PYTHON:-python3}"

have() { command -v "$1" >/dev/null 2>&1; }

# host/port from data/settings.json (§KNOWN-ISSUES.md, "host and port in
# settings do nothing" — validated and saved there, but nothing ever bound
# to either). Prints nothing on a missing file, a missing field, or an
# unreadable one, so the bash fallback chain below behaves exactly as it did
# before this file existed. TAVERN_HOST/TAVERN_PORT still win when actually
# set: an explicit override at the moment you start the server should not be
# silently beaten by a preference saved in the GUI on some earlier day.
settings_field() {
  "$PYTHON" - "$DATA_DIR/settings.json" "$1" <<'PY' 2>/dev/null
import json, sys
path, key = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        data = json.load(f)
except (OSError, ValueError):
    sys.exit(0)
value = data.get(key)
if value not in (None, ""):
    print(value)
PY
}

PORT="${TAVERN_PORT:-$(settings_field port)}"
PORT="${PORT:-8787}"
HOST="${TAVERN_HOST:-$(settings_field host)}"
HOST="${HOST:-127.0.0.1}"

STARTUP_TIMEOUT="${TAVERN_STARTUP_TIMEOUT:-25}"

port_open() {
  # Python rather than curl or nc: python is guaranteed present here, those are not.
  "$PYTHON" - "$HOST" "$PORT" <<'PY' 2>/dev/null
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
with socket.socket() as s:
    s.settimeout(1.5)
    sys.exit(0 if s.connect_ex((host, port)) == 0 else 1)
PY
}

wait_for_port() {
  local waited=0
  while [ "$waited" -lt "$STARTUP_TIMEOUT" ]; do
    port_open && return 0
    # A session that has already gone means the server died; stop waiting.
    tmux has-session -t "$SESSION" 2>/dev/null || return 1
    sleep 1
    waited=$((waited + 1))
  done
  return 1
}

# Runs a command detached and time-capped, the same caution termux_try takes
# with the Termux:API app — xdg-open has been known to block when it falls
# through to a helper that itself waits on something. Never let opening a
# browser tab be the reason startup hangs.
bg_try() {
  if have timeout; then
    ( timeout 5 "$@" >/dev/null 2>&1 & )
  else
    ( "$@" >/dev/null 2>&1 & )
  fi
}

# One tab, opened once the port actually answers — not at spawn, which would
# be a blank page half the time. termux-open-url hands the URL to whatever
# browser is set as default on the phone; xdg-open/open cover a desktop
# checkout run the same way for testing. TAVERN_NO_BROWSER=1 skips this for
# a headless run (CI, SSH, a second start.sh already watched).
open_browser() {
  [ "${TAVERN_NO_BROWSER:-}" = "1" ] && return 0
  local url="http://localhost:$PORT"
  if have termux-open-url; then
    termux_try termux-open-url "$url"
  elif have xdg-open; then
    bg_try xdg-open "$url"
  elif have open; then
    bg_try open "$url"
  fi
}

# For the plain-foreground path (no tmux involved, so nothing else is going
# to notice the port come up): poll the same way wait_for_port does, minus
# the tmux-session liveness check that path needs and this one has no
# session to make.
open_when_ready() {
  local waited=0
  while [ "$waited" -lt "$STARTUP_TIMEOUT" ]; do
    port_open && { open_browser; return 0; }
    sleep 1
    waited=$((waited + 1))
  done
}

server_alive() {
  if have pgrep; then
    pgrep -f "uvicorn app.main:app" >/dev/null 2>&1
  else
    ps aux 2>/dev/null | grep -q "[u]vicorn app.main:app"
  fi
}

# The termux-* helpers talk to the Termux:API *app*. If the app is not
# installed — easy to miss, since `pkg install termux-api` only provides the
# CLI half — they block forever instead of failing. That hangs startup before
# uvicorn is ever reached, and the symptom is a server that never comes up and
# writes no log at all. Never call one of them unbounded.
termux_try() {
  have "$1" || return 0
  if have timeout; then
    timeout 5 "$@" >/dev/null 2>&1 || return 0
  else
    "$@" >/dev/null 2>&1 || return 0
  fi
}

start_server() {
  mkdir -p "$DATA_DIR"
  # Write to the log before anything that could block, so a hang is diagnosable
  # rather than showing up as a missing file.
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') starting on $HOST:$PORT ===" >> "$LOG"

  termux_try termux-wake-lock
  termux_try termux-notification --id tavern --title "Personal Tavern" \
    --content "serving on localhost:$PORT" --ongoing

  echo "Personal Tavern → http://localhost:$PORT"
  # Backgrounded and waited on, rather than run as a foreground pipeline: a
  # trapped signal is only handled once the current foreground command returns,
  # so `uvicorn | tee` would defer the Ctrl+C handler until after the server had
  # already exited — and if the server ignored the signal, forever. `wait` is
  # interruptible, so the handler runs immediately and can kill the child.
  #
  # Process substitution instead of a pipe keeps $! as uvicorn's own pid; with
  # `| tee` it would be tee's, and killing tee leaves the server running.
  #
  # No [standard] extras: uvloop and httptools do not build cleanly on Termux.
  "$PYTHON" -m uvicorn app.main:app \
    --host "$HOST" --port "$PORT" --app-dir "$HERE" > >(tee -a "$LOG") 2>&1 &
  SERVER_PID=$!
  wait "$SERVER_PID"
}

shutdown_foreground() {
  echo
  echo "stopping…"
  [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2>/dev/null
  # Belt and braces: the server may have been started by an earlier invocation.
  pkill -f "uvicorn app.main:app" 2>/dev/null
  release_termux
  exit 0
}

release_termux() {
  termux_try termux-notification-remove tavern
  termux_try termux-wake-unlock
}

stop_server() {
  have tmux && tmux kill-session -t "$SESSION" 2>/dev/null || true
  pkill -f "uvicorn app.main:app" 2>/dev/null || true
  release_termux
  echo "stopped"
}

case "${1:-start}" in
  start|foreground|--foreground|-f|background|--background|-b|detach)
    # Foreground is the default: the server runs in this console and Ctrl+C
    # stops it. Detaching into tmux is opt-in, because a server you cannot see
    # is a server whose errors you find out about much later.
    foreground=1
    case "${1:-start}" in background|--background|-b|detach) foreground=0 ;; esac
    # Inside tmux we *are* the detached server, so run directly.
    [ "${TAVERN_INNER:-}" = "1" ] && foreground=1
    have tmux || foreground=1

    if [ "$foreground" -eq 1 ]; then
      # Only the outer invocation clears the way; the tmux-inner one is the
      # server it would be killing.
      if [ "${TAVERN_INNER:-}" != "1" ] && server_alive; then
        echo "a server is already running in the background — stopping it first"
        stop_server >/dev/null
        sleep 1
      fi
      trap shutdown_foreground INT TERM
      # Only the outer process opens the tab — inside tmux, the outer
      # invocation below already does it once wait_for_port confirms the
      # server is up.
      [ "${TAVERN_INNER:-}" != "1" ] && open_when_ready &
      start_server
    else
      # Re-exec inside tmux so closing the terminal does not take the server.
      #
      # A tmux session outlives the process inside it, so "session exists" is
      # not the same as "server running" — a crashed start (missing deps, say)
      # leaves a live session with a dead server, and checking only the session
      # would refuse to ever start again.
      if tmux has-session -t "$SESSION" 2>/dev/null; then
        if server_alive; then
          open_browser
          echo "already running — attach with: tmux attach -t $SESSION"
          exit 0
        fi
        echo "found a stale '$SESSION' session with no server in it — restarting"
        tmux kill-session -t "$SESSION" 2>/dev/null || true
      fi
      # Invoke through bash explicitly rather than relying on the shebang
      # resolving the same way inside tmux.
      tmux new-session -d -s "$SESSION" "TAVERN_INNER=1 bash '$HERE/run.sh' foreground"

      # Don't claim success just because tmux accepted the command. Wait for
      # the port to actually answer, and show the log if it never does —
      # "started" printed over a server that died on import is worse than
      # useless, because it sends you looking in the wrong place.
      if wait_for_port; then
        open_browser
        echo "running → http://localhost:$PORT"
        echo "attach: tmux attach -t $SESSION   stop: $0 stop"
      else
        echo "the server did not come up within ${STARTUP_TIMEOUT}s. Last output:"
        echo "---"
        tail -n 25 "$LOG" 2>/dev/null || echo "(no log at $LOG)"
        echo "---"
        echo "full log: $0 logs"
        exit 1
      fi
    fi
    ;;
  stop) stop_server ;;
  logs) tail -f "$LOG" ;;
  *) echo "usage: $0 {start|background|stop|logs}" >&2; exit 2 ;;
esac
