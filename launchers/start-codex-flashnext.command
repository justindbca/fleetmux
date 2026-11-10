#!/bin/bash
# Start Flash Next in pi; keep this filename for existing desktop shortcuts.
# Default: new cmux workspace. --terminal: use the current terminal (also for JSON/RPC).
# Provider configuration: ~/.pi/agent/models.json (flashnext, OpenAI chat API).
# PI_FLASHNEXT_PROVIDER picks another provider entry there (default flashnext), e.g. another machine serving Flash Next.
# The old Codex/tmux launcher (rollback) is taken from $CMUX_FLEET_LEGACY_LAUNCHER.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LEGACY="${CMUX_FLEET_LEGACY_LAUNCHER:-}"

# Existing pair/long-task commands still belong to the Codex orchestrator.
case "${1:-}" in
  reset-all|new-task|--long)
    [ -n "$LEGACY" ] || { echo "set CMUX_FLEET_LEGACY_LAUNCHER to the old Codex/tmux launcher" >&2; exit 2; }
    exec /bin/bash "$LEGACY" "$@"
    ;;
  --legacy-codex)
    shift
    [ -n "$LEGACY" ] || { echo "set CMUX_FLEET_LEGACY_LAUNCHER to the old Codex/tmux launcher" >&2; exit 2; }
    exec /bin/bash "$LEGACY" "$@"
    ;;
esac

# Do not inherit the calling pi session's PI_MODEL or the cloud Codex default.
# Model id comes from the flashnext provider in models.json, so swapping the
# served model only needs one edit there (the old hardcoded oQ4e id went stale).
PI_MODELS_JSON="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/models.json"
FN_PROVIDER="${PI_FLASHNEXT_PROVIDER:-flashnext}"
FLASHNEXT_ID="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["providers"][sys.argv[2]]["models"][0]["id"])' "$PI_MODELS_JSON" "$FN_PROVIDER" 2>/dev/null || true)"
[ -n "$FLASHNEXT_ID" ] || { echo "ERROR: $PI_MODELS_JSON 里没有 $FN_PROVIDER provider 的模型" >&2; exit 1; }
export PI_MODEL="$FN_PROVIDER/$FLASHNEXT_ID"
export PI_THINKING=off
# Flash Next 是项目的本地助手：在 PI_WORKDIR 起（项目根目录的兼容入口会设成该项目；缺省本仓），
# 读它的 AGENTS.md，并装领域热载扩展。
export PI_WORKDIR="${PI_WORKDIR:-$PROJECT_DIR}"
if [ ! -d "$PI_WORKDIR" ]; then echo 'ERROR: PI_WORKDIR 目录不存在。' >&2; exit 2; fi
export PI_WORKDIR="$(cd "$PI_WORKDIR" && pwd)"

CHECK_ONLY=0
IN_TERMINAL=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --check-only) CHECK_ONLY=1; shift ;;
    --terminal) IN_TERMINAL=1; shift ;;
    *) break ;;
  esac
done

# Same cmux routing as start-pi-deepseek-free.command.
if [ "$IN_TERMINAL" -eq 0 ]; then
  CMUX_BIN="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
  if [ ! -x "$CMUX_BIN" ]; then
    echo 'ERROR: 找不到 cmux；请安装 cmux 或设置 CMUX_BIN。当前终端模式请加 --terminal。' >&2
    exit 127
  fi
  VERSION="$("$CMUX_BIN" --version)"
  if [[ ! "$VERSION" =~ ([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
    echo 'ERROR: 无法确定 cmux 版本。' >&2; exit 2
  fi
  if (( BASH_REMATCH[1] == 0 && (BASH_REMATCH[2] < 64 || (BASH_REMATCH[2] == 64 && BASH_REMATCH[3] < 25)) )); then
    echo 'ERROR: 需要 cmux >= 0.64.25。' >&2; exit 2
  fi
  # Only forward explicit launcher configuration, never API/OAuth credentials or parent session IDs.
  ENV_ARGS=("PI_WORKDIR=$PI_WORKDIR")
  [ -z "${PI_BIN:-}" ] || ENV_ARGS+=("PI_BIN=$PI_BIN")
  [ -z "${PI_CODING_AGENT_DIR:-}" ] || ENV_ARGS+=("PI_CODING_AGENT_DIR=$PI_CODING_AGENT_DIR")
  [ -z "${PI_FLASHNEXT_PROVIDER:-}" ] || ENV_ARGS+=("PI_FLASHNEXT_PROVIDER=$PI_FLASHNEXT_PROVIDER")
  printf -v LAUNCH '%q ' /usr/bin/env "${ENV_ARGS[@]}" /bin/bash \
    "$PROJECT_DIR/launchers/start-codex-flashnext.command" --terminal "$@"
  # cmux sends this into a login shell: fail before opening a workspace if the TTY line is too long.
  if [ "$(printf '%s' "$LAUNCH" | wc -c | tr -d ' ')" -gt 900 ]; then
    echo 'ERROR: 启动命令过长；请打开窗口后输入任务，或用 --terminal 在当前终端运行。' >&2
    exit 2
  fi
  if [ "$CHECK_ONLY" -eq 1 ]; then
    echo 'Check-only: cmux 参数检查通过；未启动应用/工作区，尚未验证 oMLX 或模型调用。'
    exit 0
  fi
  OPEN_BIN="${CMUX_OPEN_BIN:-/usr/bin/open}"
  if ! "$CMUX_BIN" ping >/dev/null 2>&1; then
    # Finder starts .command files in Terminal, outside cmux's process tree; cmuxOnly
    # rejects that socket client. Use cmux's official .command-file handler instead.
    if (( BASH_REMATCH[1] == 0 && BASH_REMATCH[2] < 65 )); then
      echo 'ERROR: 从外部终端打开需要 cmux >= 0.65.0 的原生 .command 支持；也可在 cmux 内运行本入口。' >&2
      exit 2
    fi
    BOOT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/pi-flashnext.XXXXXX")"
    BOOT_FILE="$BOOT_DIR/pi-flashnext.command"
    chmod 700 "$BOOT_DIR"
    {
      printf '#!/bin/bash\nset -euo pipefail\n'
      # Delete only this one private bootstrap before transferring to Pi.
      printf 'SELF="$0"\nBOOT_DIR="$(dirname "$SELF")"\nrm -f -- "$SELF"\nrmdir -- "$BOOT_DIR" 2>/dev/null || true\n'
      printf 'exec %s\n' "$LAUNCH"
    } > "$BOOT_FILE"
    chmod 700 "$BOOT_FILE"
    if ! "$OPEN_BIN" -a /Applications/cmux.app "$BOOT_FILE"; then
      rm -f -- "$BOOT_FILE"
      rmdir -- "$BOOT_DIR" 2>/dev/null || true
      echo 'ERROR: 无法通过 macOS 在 cmux 中打开启动文件。' >&2
      exit 1
    fi
    echo '已交给 cmux 原生 .command 打开；无需放宽 socket 权限。'
    exit 0
  fi
  WS_NAME='pi · Flash Next'
  [ "$FN_PROVIDER" = flashnext ] || WS_NAME="$WS_NAME ($FN_PROVIDER)"
  "$CMUX_BIN" new-workspace --name "$WS_NAME" --cwd "$PI_WORKDIR" \
    --command "$LAUNCH" --focus true
  # Workspace focus is not macOS application activation.
  if ! "$OPEN_BIN" -a /Applications/cmux.app; then
    echo 'WARNING: 工作区已创建，但无法把 cmux 切到前台；请手动点击 cmux。' >&2
  fi
  exit 0
fi
if [ "$CHECK_ONLY" -eq 1 ]; then
  echo 'Check-only: 当前终端参数检查通过；尚未验证 oMLX 或模型调用。'
  exit 0
fi

# Keep --continue/--resume separate from cloud-model sessions.
exec /bin/bash "$PROJECT_DIR/launchers/start-pi-full.command" \
  --session-dir "${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/sessions/$FN_PROVIDER" \
  -e "$PROJECT_DIR/extensions/project-domains.ts" "$@"
