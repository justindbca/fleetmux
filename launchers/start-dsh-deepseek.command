#!/bin/bash
# Start DeepSeek V4.1 Flash in DSH (DeepSeek Harness) web UI through the local magpie gateway
# (WorkBuddy AI plan; provider workbuddy-ds in ~/.pi/agent/models.json, shared with pi).
# Default: new cmux workspace (`dsh · DeepSeek`). --terminal: current terminal. --check-only: start nothing.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export DSH_PROVIDER=workbuddy-ds
# magpie renamed the WorkBuddy model to -sg; pin it here instead of using models.json's first id.
export DSH_MODEL_ID="${DSH_DEEPSEEK_MODEL:-workbuddy-ai/deepseek-v4.1-flash-sg}"
export DSH_TITLE="${DSH_TITLE:-dsh · DeepSeek}"
for arg in "$@"; do
  case "$arg" in
    --patch|--patch=*|--profile|--profile=*)
      echo 'ERROR: DeepSeek 壳固定 web 档位与模型，不接受 --patch / --profile。' >&2; exit 2 ;;
  esac
done
MAGPIE_URL="${PI_MAGPIE_URL:-http://127.0.0.1:3425}"
if ! curl -fsS -m 5 "$MAGPIE_URL/v1/models" 2>/dev/null | grep -q "\"$DSH_MODEL_ID\""; then
  echo "ERROR: magpie 网关 $MAGPIE_URL 没在跑或没有 ${DSH_MODEL_ID}；先运行 ~/.local/bin/magpie serve 并确认 WorkBuddy AI 已登录。" >&2
  exit 2
fi
exec /bin/bash "$ROOT/launchers/start-dsh.command" "$@"
