#!/bin/bash
# Double-click this file in Finder on macOS.
cd "$(dirname "$0")" || exit 1
exec /bin/bash "./start-kanban.bash" "$@"
