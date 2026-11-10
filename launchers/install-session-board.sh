#!/bin/bash
# install-session-board.sh — install the pi side of the cross-session board (docs/contracts.md §7)
#
# Usage:
#   install-session-board.sh                  # into $HOME/.pi/agent/extensions
#   install-session-board.sh --agent-dir DIR  # into DIR/extensions
#
# Copies extensions/session-board.ts and symlinks cmux_fleet/session_board.py beside it, so pi runs
# the same Python as the Claude Code hooks of this checkout. Run it from the checkout the hooks point at.
set -euo pipefail

FLEET_DIR="$(cd "$(dirname "$0")/.." && pwd)"
EXT_SRC="$FLEET_DIR/extensions/session-board.ts"
PY_SRC="$FLEET_DIR/cmux_fleet/session_board.py"
[ -f "$EXT_SRC" ] || { echo "ERROR: missing $EXT_SRC" >&2; exit 2; }
[ -f "$PY_SRC" ] || { echo "ERROR: missing $PY_SRC" >&2; exit 2; }

AGENT_DIR="$HOME/.pi/agent"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --agent-dir) AGENT_DIR="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

DEST="$AGENT_DIR/extensions"
mkdir -p "$DEST"
cp -f "$EXT_SRC" "$DEST/session-board.ts"
ln -sfn "$PY_SRC" "$DEST/session_board.py"
echo "installed -> $DEST (session_board.py -> $PY_SRC)"
ls -l "$DEST/session-board.ts" "$DEST/session_board.py"
