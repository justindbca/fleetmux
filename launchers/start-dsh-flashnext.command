#!/bin/bash
# Start Flash Next in DSH (DeepSeek Harness) web UI; the pi counterpart is start-codex-flashnext.command.
# Default: new cmux workspace (`dsh · Flash Next`). --terminal: current terminal. --check-only: start nothing.
# Provider configuration: ~/.pi/agent/models.json (flashnext), shared with pi.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export DSH_PROVIDER=flashnext
unset DSH_MODEL_ID  # the served model id comes from models.json, like the pi launcher
export DSH_TITLE="${DSH_TITLE:-dsh · Flash Next}"
# Same default workdir as the pi Flash Next launcher.
export DSH_WORKDIR="${DSH_WORKDIR:-$ROOT}"
URL="$(/usr/bin/python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["providers"]["flashnext"]["baseUrl"])' "${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/models.json" 2>/dev/null || true)"
[ -n "$URL" ] || { echo 'ERROR: models.json 里没有 flashnext provider。' >&2; exit 1; }
if ! curl -fsS -m 5 "$URL/models" >/dev/null 2>&1; then
  echo "ERROR: Flash Next 服务 $URL 连不上（Studio mlx-serve 没在跑或网络不通）。" >&2
  exit 2
fi
exec /bin/bash "$ROOT/launchers/start-dsh.command" "$@"
