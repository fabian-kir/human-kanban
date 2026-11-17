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
if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  fail 'Python 3.10 or newer is required for editing. Install Python from python.org (or brew install python), then retry.'
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) and sys.version_info.major == 3 else 1)' >/dev/null 2>&1; then
  fail 'Python 3.10 or newer is required. Check: python3 --version. No extra Python packages are needed.'
fi
printf '\nStarting Human Kanban using %s...\n' "$PYTHON"
"$PYTHON" ./server.py "$@"
exitcode=$?
if [ "$exitcode" -ne 0 ]; then
  fail "The server exited with code $exitcode. If port 8765 is occupied, retry with: bash start-kanban.bash --port 8766"
fi
