#!/bin/bash
# Compatibility template: copy into a sibling project's ROOT, not its launchers/.
# Keep all startup logic in cmux-fleet-pi.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
STARTER="$PROJECT_DIR/../cmux-fleet-pi/launchers/start-dsh-flashnext.command"
if [ ! -f "$STARTER" ]; then
  printf 'ERROR: 找不到正式启动器：%s\n' "$STARTER" >&2
  exit 127
fi
export DSH_WORKDIR="${DSH_WORKDIR:-$PROJECT_DIR}"
exec /bin/bash "$STARTER" "$@"
