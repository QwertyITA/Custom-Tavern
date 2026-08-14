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
# Usage:  ./run.sh          start (inside tmux if available)
#         ./run.sh stop     stop it
#         ./run.sh logs     follow the log

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="tavern"
PORT="${TAVERN_PORT:-8787}"
HOST="${TAVERN_HOST:-127.0.0.1}"
LOG="$HERE/data/tavern.log"
PYTHON="${PYTHON:-python3}"

have() { command -v "$1" >/dev/null 2>&1; }

STARTUP_TIMEOUT="${TAVERN_STARTUP_TIMEOUT:-25}"

wait_for_port() {
  # Python rather than curl or nc: python is guaranteed present here, those are not.
  local waited=0
  while [ "$waited" -lt "$STARTUP_TIMEOUT" ]; do
    if "$PYTHON" - "$HOST" "$PORT" <<'PY' 2>/dev/null
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
with socket.socket() as s:
    s.settimeout(1.5)
    sys.exit(0 if s.connect_ex((host, port)) == 0 else 1)
PY
    then
      return 0
    fi
    # A session that has already gone means the server died; stop waiting.
    tmux has-session -t "$SESSION" 2>/dev/null || return 1
    sleep 1
    waited=$((waited + 1))
  done
  return 1
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
  mkdir -p "$HERE/data"
  # Write to the log before anything that could block, so a hang is diagnosable
  # rather than showing up as a missing file.
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') starting on $HOST:$PORT ===" >> "$LOG"

  termux_try termux-wake-lock
  termux_try termux-notification --id tavern --title "Personal Tavern" \
    --content "serving on localhost:$PORT" --ongoing

  echo "Personal Tavern → http://localhost:$PORT"
  # No [standard] extras: uvloop and httptools do not build cleanly on Termux.
  exec "$PYTHON" -m uvicorn app.main:app \
    --host "$HOST" --port "$PORT" --app-dir "$HERE" 2>&1 | tee -a "$LOG"
}

stop_server() {
  have tmux && tmux kill-session -t "$SESSION" 2>/dev/null || true
  pkill -f "uvicorn app.main:app" 2>/dev/null || true
  termux_try termux-notification-remove tavern
  termux_try termux-wake-unlock
  echo "stopped"
}

case "${1:-start}" in
  start)
    if [ "${TAVERN_INNER:-}" = "1" ] || ! have tmux; then
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
          echo "already running — attach with: tmux attach -t $SESSION"
          exit 0
        fi
        echo "found a stale '$SESSION' session with no server in it — restarting"
        tmux kill-session -t "$SESSION" 2>/dev/null || true
      fi
      # Invoke through bash explicitly rather than relying on the shebang
      # resolving the same way inside tmux.
      tmux new-session -d -s "$SESSION" "TAVERN_INNER=1 bash '$HERE/run.sh' start"

      # Don't claim success just because tmux accepted the command. Wait for
      # the port to actually answer, and show the log if it never does —
      # "started" printed over a server that died on import is worse than
      # useless, because it sends you looking in the wrong place.
      if wait_for_port; then
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
  *) echo "usage: $0 {start|stop|logs}" >&2; exit 2 ;;
esac
