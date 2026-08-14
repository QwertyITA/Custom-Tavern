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

start_server() {
  mkdir -p "$HERE/data"
  have termux-wake-lock && termux-wake-lock || true
  have termux-notification && termux-notification \
    --id tavern --title "Personal Tavern" \
    --content "serving on localhost:$PORT" --ongoing || true

  echo "Personal Tavern → http://localhost:$PORT"
  # No [standard] extras: uvloop and httptools do not build cleanly on Termux.
  exec "$PYTHON" -m uvicorn app.main:app \
    --host "$HOST" --port "$PORT" --app-dir "$HERE" 2>&1 | tee -a "$LOG"
}

stop_server() {
  have tmux && tmux kill-session -t "$SESSION" 2>/dev/null || true
  pkill -f "uvicorn app.main:app" 2>/dev/null || true
  have termux-notification-remove && termux-notification-remove tavern || true
  have termux-wake-unlock && termux-wake-unlock || true
  echo "stopped"
}

case "${1:-start}" in
  start)
    if [ "${TAVERN_INNER:-}" = "1" ] || ! have tmux; then
      start_server
    else
      # Re-exec inside tmux so closing the terminal does not take the server.
      tmux has-session -t "$SESSION" 2>/dev/null && {
        echo "already running — attach with: tmux attach -t $SESSION"; exit 0; }
      # Invoke through bash explicitly rather than relying on the shebang
      # resolving the same way inside tmux.
      tmux new-session -d -s "$SESSION" "TAVERN_INNER=1 bash '$HERE/run.sh' start"
      echo "started in tmux session '$SESSION' → http://localhost:$PORT"
      echo "attach: tmux attach -t $SESSION   stop: $0 stop"
    fi
    ;;
  stop) stop_server ;;
  logs) tail -f "$LOG" ;;
  *) echo "usage: $0 {start|stop|logs}" >&2; exit 2 ;;
esac
