#!/bin/bash
# Start DeepSeek V4.1 Flash (magpie gateway, same as start-dsh-deepseek.command) in dsh-TUI,
# the terminal UI for DSH, instead of the web UI. Default: new cmux workspace (`dsh-tui · DeepSeek`).
# --terminal: current terminal. --check-only: start nothing. FlashNext: DSH_UI=tui start-dsh-flashnext.command.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export DSH_UI=tui
export DSH_TITLE="${DSH_TITLE:-dsh-tui · DeepSeek}"
exec /bin/bash "$ROOT/launchers/start-dsh-deepseek.command" "$@"
