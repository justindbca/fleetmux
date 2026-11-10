#!/bin/bash
# Start Flash Next (Studio mlx-serve) in dsh-TUI, the terminal UI for DSH, instead of the web UI.
# Default: new cmux workspace (`dsh-tui · Flash Next`). --terminal: current terminal.
# --check-only: start nothing. Web counterpart: start-dsh-flashnext.command.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export DSH_UI=tui
export DSH_TITLE="${DSH_TITLE:-dsh-tui · Flash Next}"
exec /bin/bash "$ROOT/launchers/start-dsh-flashnext.command" "$@"
