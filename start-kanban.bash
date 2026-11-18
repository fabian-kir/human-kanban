#!/usr/bin/env bash
# One launcher for macOS / Linux. No downloads and no package installation.
set -u
cd "$(dirname "$0")" || { printf '\nERROR: Cannot open the Kanban folder.\n'; exit 1; }

fail() {
  printf '\nERROR: %s\n' "$1" >&2
  printf 'Read-only fallback: open %s/index.html in Safari, Chrome or Edge.\n' "$PWD" >&2
  if [ -t 0 ]; then printf '\nPress Enter to close... '; read -r _unused; fi
  exit 1
}
if [ ! -f index.html ] || [ ! -f board.js ] || [ ! -f server.py ]; then
  fail 'A required file is missing. Keep index.html, board.js and server.py together.'
fi
# The first python3 on PATH is often too old (macOS ships 3.9 at /usr/bin/python3), so try
# every usual name, then the common install locations, then any Python uv knows about.
is_usable() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) and sys.version_info.major == 3 else 1)' >/dev/null 2>&1
}
PYTHON=''
FOUND_OLD=''
for candidate in python3 python python3.14 python3.13 python3.12 python3.11 python3.10 \
    /opt/homebrew/bin/python3 /usr/local/bin/python3 "$HOME/.local/bin/python3"; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  if is_usable "$candidate"; then PYTHON=$candidate; break; fi
  FOUND_OLD=$candidate
done
if [ -z "$PYTHON" ]; then
  for uv in uv "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    command -v "$uv" >/dev/null 2>&1 || continue
    candidate=$("$uv" python find --no-project '>=3.10' 2>/dev/null) || continue
    if [ -n "$candidate" ] && is_usable "$candidate"; then PYTHON=$candidate; break; fi
  done
fi
if [ -z "$PYTHON" ] && [ -n "$FOUND_OLD" ]; then
  fail "Python 3.10 or newer is required; $FOUND_OLD is $("$FOUND_OLD" --version 2>&1). Install a newer one (python.org, brew install python, or uv python install 3.12), then retry. No extra Python packages are needed."
elif [ -z "$PYTHON" ]; then
  fail 'Python 3.10 or newer is required for editing. Install Python from python.org (or brew install python, or uv python install 3.12), then retry.'
fi
printf '\nStarting Human Kanban using %s...\n' "$PYTHON"
"$PYTHON" ./server.py "$@"
exitcode=$?
if [ "$exitcode" -ne 0 ]; then
  fail "The server exited with code $exitcode. If port 8765 is occupied, retry with: bash start-kanban.bash --port 8766"
fi
