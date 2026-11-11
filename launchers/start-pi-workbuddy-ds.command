#!/bin/bash
# Default: new cmux workspace. --terminal: use the current terminal (also for JSON/RPC).
# Pi UI/tools -> local magpie gateway (127.0.0.1:3425) -> WorkBuddy AI plan -> DeepSeek V4.1 Flash.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Do not report a successful --check-only for a standalone/misplaced copy.
for required in launchers/start-pi-full.command; do
  if [ ! -f "$ROOT/$required" ]; then
    printf 'ERROR: 启动器位置错误或安装不完整，缺少：%s\n' "$ROOT/$required" >&2
    echo '请运行 fleetmux 仓 launchers 内的正式启动器；其他项目请使用 launchers/compat 内的薄 wrapper。' >&2
    exit 127
  fi
done
CHECK_ONLY=0
IN_TERMINAL=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --check-only) CHECK_ONLY=1; shift ;;
    --terminal) IN_TERMINAL=1; shift ;;
    *) break ;;
  esac
done
# Do not inherit the parent Pi's provider/model or silently select a paid model.
export PI_MODEL='workbuddy-ds/workbuddy-ai/deepseek-v4.1-flash-sg'
export PI_THINKING=off
export PI_WORKDIR="${PI_WORKDIR:-$ROOT}"
if [ ! -d "$PI_WORKDIR" ]; then echo 'ERROR: PI_WORKDIR 目录不存在。' >&2; exit 2; fi
export PI_WORKDIR="$(cd "$PI_WORKDIR" && pwd)"
for arg in "$@"; do
  case "$arg" in
    --model|--model=*|--provider|--provider=*|--models|--models=*|--api-key|--api-key=*)
      echo 'ERROR: WorkBuddy 壳固定使用 workbuddy-ai/deepseek-v4.1-flash-sg，不接受模型/凭证覆盖。' >&2; exit 2 ;;
  esac
done
# The model is served by magpie (provider "workbuddy-ds" in ~/.pi/agent/models.json); fail before Pi starts.
MAGPIE_URL="${PI_MAGPIE_URL:-http://127.0.0.1:3425}"
if ! curl -fsS -m 5 "$MAGPIE_URL/v1/models" 2>/dev/null | grep -q '"workbuddy-ai/deepseek-v4.1-flash-sg"'; then
  echo "ERROR: magpie 网关 $MAGPIE_URL 没在跑或没有 workbuddy-ai/deepseek-v4.1-flash-sg；先运行 ~/.local/bin/magpie serve 并确认 WorkBuddy AI 已登录。" >&2
  exit 2
fi
printf 'Pi UI + tools → magpie (%s) → WorkBuddy AI → deepseek-v4.1-flash\n' "$MAGPIE_URL"
echo '用量记在 WorkBuddy AI 账号（magpie quota 查）；magpie 只放行 WorkBuddy AI，其余订阅已关。Pi 工具具备当前用户权限，不是沙盒。'
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
  ENV_ARGS=("PI_WORKDIR=$PI_WORKDIR" "PI_MAGPIE_URL=$MAGPIE_URL")
  [ -z "${PI_BIN:-}" ] || ENV_ARGS+=("PI_BIN=$PI_BIN")
  [ -z "${PI_CODING_AGENT_DIR:-}" ] || ENV_ARGS+=("PI_CODING_AGENT_DIR=$PI_CODING_AGENT_DIR")
  printf -v LAUNCH '%q ' /usr/bin/env "${ENV_ARGS[@]}" /bin/bash \
    "$ROOT/launchers/start-pi-workbuddy-ds.command" --terminal "$@"
  # cmux sends this into a login shell: fail before opening a workspace if the TTY line is too long.
  if [ "$(printf '%s' "$LAUNCH" | wc -c | tr -d ' ')" -gt 900 ]; then
    echo 'ERROR: 启动命令过长；请打开窗口后输入任务，或用 --terminal 在当前终端运行。' >&2
    exit 2
  fi
  if [ "$CHECK_ONLY" -eq 1 ]; then
    echo 'Check-only: cmux 参数检查通过；未启动应用/工作区，已验证 magpie 网关在线并列出该模型。'
    exit 0
  fi
  OPEN_BIN="${CMUX_OPEN_BIN:-/usr/bin/open}"
  if ! "$CMUX_BIN" ping >/dev/null 2>&1; then
    # Finder starts .command files in Terminal, outside cmux's process tree.
    # cmuxOnly intentionally rejects that socket client. Use cmux's official
    # macOS .command-file handler instead; do not weaken socket permissions,
    # spoof a parent process, or paste commands into an existing terminal.
    if (( BASH_REMATCH[1] == 0 && BASH_REMATCH[2] < 65 )); then
      echo 'ERROR: 从外部终端打开需要 cmux >= 0.65.0 的原生 .command 支持；也可在 cmux 内运行本入口。' >&2
      exit 2
    fi
    BOOT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/pi-workbuddy-ds.XXXXXX")"
    BOOT_FILE="$BOOT_DIR/pi-workbuddy-ds.command"
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
  "$CMUX_BIN" new-workspace --name 'pi · WorkBuddy DeepSeek' --cwd "$PI_WORKDIR" \
    --command "$LAUNCH" --focus true
  # Workspace focus is not macOS application activation: Finder's Terminal may
  # otherwise remain in front of an already-running cmux after a double-click.
  if ! "$OPEN_BIN" -a /Applications/cmux.app; then
    echo 'WARNING: 工作区已创建，但无法把 cmux 切到前台；请手动点击 cmux。' >&2
  fi
  exit 0
fi
if [ "$CHECK_ONLY" -eq 1 ]; then
  echo 'Check-only: 当前终端参数检查通过；已验证 magpie 网关在线并列出该模型。'
  exit 0
fi
exec /bin/bash "$ROOT/launchers/start-pi-full.command" \
  --provider workbuddy-ds --models "$PI_MODEL" \
  --session-dir "${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/sessions/workbuddy-ds" "$@"
