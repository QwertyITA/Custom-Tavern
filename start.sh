#!/usr/bin/env bash
# Update, then start. This is the one to tap.
#
#   ./start.sh              pull the latest version, then start the server
#   ./start.sh --no-update  skip the pull (offline, or you want this exact code)
#   ./start.sh stop         stop the server
#   ./start.sh logs         follow the log
#   ./start.sh --widget     install a Termux:Widget home-screen shortcut
#
# Rules it follows, in order of how much they matter:
#   * Your data is never touched. data/tavern.db and data/settings.json are
#     gitignored, so a pull cannot overwrite chats, characters or settings.
#   * A failed update never stops the app. No signal on the train still starts
#     the version you already have.
#   * Local edits are never discarded silently. A dirty worktree skips the pull
#     and says so, rather than stashing or resetting behind your back.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-python3}"
REMOTE="${TAVERN_REMOTE:-origin}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- shortcut

install_widget() {
  # Termux:Widget reads executables from ~/.shortcuts and puts them on the
  # home screen. Needs the Termux:Widget app installed separately.
  local dir="$HOME/.shortcuts"
  mkdir -p "$dir"
  cat > "$dir/Personal Tavern" <<EOF
#!/usr/bin/env bash
bash "$HERE/start.sh"
EOF
  chmod +x "$dir/Personal Tavern"
  cat > "$dir/Personal Tavern (stop)" <<EOF
#!/usr/bin/env bash
bash "$HERE/start.sh" stop
EOF
  chmod +x "$dir/Personal Tavern (stop)"
  bold "Installed home-screen shortcuts in ~/.shortcuts"
  echo "Add the Termux:Widget widget to your home screen to see them."
}

# ------------------------------------------------------------------- hooks

enable_hooks() {
  # Hooks are not carried by clone, so every checkout has to opt in. Doing it
  # here means the credential guard is live from the first launch rather than
  # from whenever someone remembers to run a setup step. The repository is
  # public: a leaked key is public the moment it is pushed.
  [ -d "$HERE/.git" ] && [ -d "$HERE/.githooks" ] || return 0
  chmod +x "$HERE/.githooks/"* 2>/dev/null
  if [ "$(git config --get core.hooksPath 2>/dev/null)" != ".githooks" ]; then
    git config core.hooksPath .githooks && bold "Enabled the credential pre-commit guard."
  fi
}

# ------------------------------------------------------------------ update

update() {
  if ! have git || [ ! -d "$HERE/.git" ]; then
    warn "not a git checkout — skipping update"
    return 0
  fi

  local branch
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" || branch=""
  if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
    warn "detached HEAD — skipping update"
    return 0
  fi

  # Uncommitted work is yours. Refuse to touch it.
  if [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    warn "you have local changes — skipping update"
    echo "     commit or discard them, then run again to pick up the latest."
    return 0
  fi

  bold "Checking for updates on $branch…"
  if ! git fetch --quiet "$REMOTE" "$branch" 2>/dev/null; then
    warn "couldn't reach $REMOTE — starting the version you have"
    return 0
  fi

  local local_head remote_head
  local_head="$(git rev-parse HEAD)"
  remote_head="$(git rev-parse "$REMOTE/$branch" 2>/dev/null || echo "$local_head")"
  if [ "$local_head" = "$remote_head" ]; then
    echo "     already up to date"
    return 0
  fi

  local before after
  before="$(checksum requirements.txt)"

  if git merge --ff-only "$REMOTE/$branch" --quiet 2>/dev/null; then
    echo "     updated $(git rev-parse --short "$local_head") → $(git rev-parse --short HEAD)"
    git --no-pager log --oneline "$local_head..HEAD" | sed 's/^/       /'
  else
    warn "can't fast-forward — your branch and $REMOTE/$branch have diverged."
    echo "     Your data is safe either way (data/ is gitignored). To take the"
    echo "     remote version as-is:"
    echo "       git reset --hard $REMOTE/$branch"
    return 0
  fi

  after="$(checksum requirements.txt)"
  if [ "$before" != "$after" ]; then
    bold "Dependencies changed — installing…"
    install_deps
  fi
}

checksum() {
  [ -f "$1" ] || { echo "missing"; return; }
  if have sha256sum; then sha256sum "$1" | cut -d' ' -f1
  elif have shasum; then shasum -a 256 "$1" | cut -d' ' -f1
  else wc -c < "$1"; fi
}

is_termux() {
  case "${PREFIX:-}" in *com.termux*) return 0 ;; esac
  [ -d /data/data/com.termux/files/usr ]
}

termux_rust_target() {
  # PyPI ships no Android wheel for pydantic-core, so it compiles from source.
  # maturin derives the target triple from Python's SOABI and gets
  # aarch64-unknown-linux-android, which rustup does not know; Termux's Rust
  # uses aarch64-linux-android. Setting it explicitly is the whole fix.
  case "$(uname -m)" in
    aarch64|arm64) echo "aarch64-linux-android" ;;
    armv7l|armv8l|arm) echo "armv7-linux-androideabi" ;;
    x86_64) echo "x86_64-linux-android" ;;
    i686|i386) echo "i686-linux-android" ;;
    *) echo "" ;;
  esac
}

install_deps() {
  if is_termux; then
    local target
    target="$(termux_rust_target)"
    if [ -n "$target" ] && [ -z "${CARGO_BUILD_TARGET:-}" ]; then
      export CARGO_BUILD_TARGET="$target"
      echo "     building native extensions for $target"
    fi
    if ! have cargo; then
      warn "Rust is not installed — pydantic-core cannot be built without it."
      echo "     run:  pkg install -y rust binutils"
      echo "     then: ./start.sh"
    fi
  fi

  # Deliberately not --quiet. On Termux this compiles pydantic-core from Rust
  # source and sits silent for ten minutes; a progress-free wait on a phone
  # looks like a hang, and the natural response is Ctrl+C — which throws the
  # work away and leaves you exactly where you started.
  if is_termux && ! "$PYTHON" -c "import pydantic" 2>/dev/null; then
    warn "First run: pydantic-core compiles from Rust source."
    echo "     This takes about ten minutes on a phone and prints almost nothing."
    echo "     Leave it alone — Ctrl+C here means starting over."
  fi

  if "$PYTHON" -m pip install -r requirements.txt; then
    return 0
  fi

  fail "pip install failed."
  if is_termux; then
    echo "  pydantic-core has no prebuilt Android wheel, so it compiles from source."
    echo "  Install the toolchain and run this script again:"
    echo "      pkg install -y rust binutils"
    echo "      ./start.sh"
    echo "  The build takes roughly ten minutes on a phone, once."
  fi
  echo "  Starting anyway with whatever is already installed."
}

# -------------------------------------------------------------------- deps

check_deps() {
  # Cheap import check — catches a first run, or a dependency that vanished
  # after a Termux upgrade, before uvicorn fails with a stack trace.
  if ! "$PYTHON" -c "import fastapi, uvicorn, httpx, pydantic" 2>/dev/null; then
    bold "Installing dependencies…"
    install_deps
  fi
}

# -------------------------------------------------------------------- main

case "${1:-start}" in
  stop|logs)
    exec bash "$HERE/run.sh" "$1"
    ;;
  --widget)
    install_widget
    exit 0
    ;;
  --no-update)
    ;;
  start|"")
    enable_hooks
    update
    ;;
  *)
    echo "usage: $0 [start|--no-update|stop|logs|--widget]" >&2
    exit 2
    ;;
esac

check_deps
exec bash "$HERE/run.sh" start
