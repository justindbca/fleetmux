#!/bin/bash
# Compatibility template: copy into a sibling project's ROOT, not its launchers/.
# Keep all startup logic and extensions in the fleetmux checkout ($FLEETMUX_PI_DIR, default: a sibling fleetmux/).
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
STARTER="${FLEETMUX_PI_DIR:-$PROJECT_DIR/../fleetmux}/launchers/start-pi-deepseek-free.command"
if [ ! -f "$STARTER" ]; then
  printf 'ERROR: 找不到正式启动器：%s\n' "$STARTER" >&2
  exit 127
fi
export PI_WORKDIR="${PI_WORKDIR:-$PROJECT_DIR}"
exec /bin/bash "$STARTER" "$@"
