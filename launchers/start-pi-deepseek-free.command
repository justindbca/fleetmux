#!/bin/bash
# Default: new cmux workspace. --terminal: use the current terminal (also for JSON/RPC).
# Pi UI/tools -> official OpenCode local service -> DeepSeek V4.1 Flash Free.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Do not report a successful --check-only for a standalone/misplaced copy.
for required in launchers/start-pi-full.command extensions/opencode-free/index.ts extensions/opencode-free/backend.mjs extensions/opencode-free/mcp-stub.mjs; do
  if [ ! -f "$ROOT/$required" ]; then
    printf 'ERROR: 启动器位置错误或安装不完整，缺少：%s\n' "$ROOT/$required" >&2
    echo '请运行 cmux-fleet-pi/launchers 内的正式启动器；其他项目请使用 launchers/compat 内的薄 wrapper。' >&2
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
export PI_MODEL='opencode-cli/deepseek-v4.1-flash-free'
export PI_THINKING=off
export PI_WORKDIR="${PI_WORKDIR:-$ROOT}"
if [ ! -d "$PI_WORKDIR" ]; then echo 'ERROR: PI_WORKDIR 目录不存在。' >&2; exit 2; fi
export PI_WORKDIR="$(cd "$PI_WORKDIR" && pwd)"
for arg in "$@"; do
  case "$arg" in
    --model|--model=*|--provider|--provider=*|--models|--models=*|--api-key|--api-key=*)
      echo 'ERROR: 免费壳固定使用 deepseek-v4.1-flash-free，不接受模型/凭证覆盖。' >&2; exit 2 ;;
  esac
done
if [ -z "${PI_OPENCODE_BIN:-}" ]; then
  PI_OPENCODE_BIN="$(command -v opencode || true)"
  if [ -z "$PI_OPENCODE_BIN" ] && [ -x "$HOME/.local/bin/opencode" ]; then
    PI_OPENCODE_BIN="$HOME/.local/bin/opencode"
  fi
fi
if [ ! -x "${PI_OPENCODE_BIN:-}" ]; then
  echo 'ERROR: 找不到官方 opencode；安装并在 OpenCode 中登录，或设置 PI_OPENCODE_BIN 为绝对路径。' >&2
  exit 127
fi
# Resolve before the shared wrapper changes directories.
export PI_OPENCODE_BIN="$(cd "$(dirname "$PI_OPENCODE_BIN")" && pwd)/$(basename "$PI_OPENCODE_BIN")"
printf 'Pi UI + tools → official OpenCode → deepseek-v4.1-flash-free\n'
echo '不复制 OAuth、不走 Console 直连接口、不自动转付费。正文/思考为真实 SSE 流式，usage 来自 OpenCode。'
echo 'Pi 工具具备当前用户权限，不是沙盒；OpenCode 服务在沙箱里常驻复用（每个 Pi 会话一个），Pi 退出或空闲 15 分钟后停止。'
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
  ENV_ARGS=("PI_WORKDIR=$PI_WORKDIR" "PI_OPENCODE_BIN=$PI_OPENCODE_BIN")
  [ -z "${PI_BIN:-}" ] || ENV_ARGS+=("PI_BIN=$PI_BIN")
  [ -z "${PI_CODING_AGENT_DIR:-}" ] || ENV_ARGS+=("PI_CODING_AGENT_DIR=$PI_CODING_AGENT_DIR")
  printf -v LAUNCH '%q ' /usr/bin/env "${ENV_ARGS[@]}" /bin/bash \
    "$ROOT/launchers/start-pi-deepseek-free.command" --terminal "$@"
  # cmux sends this into a login shell: fail before opening a workspace if the TTY line is too long.
  if [ "$(printf '%s' "$LAUNCH" | wc -c | tr -d ' ')" -gt 900 ]; then
    echo 'ERROR: 启动命令过长；请打开窗口后输入任务，或用 --terminal 在当前终端运行。' >&2
    exit 2
  fi
  if [ "$CHECK_ONLY" -eq 1 ]; then
    echo 'Check-only: cmux 参数检查通过；未启动应用/工作区，尚未验证登录、免费额度或模型调用。'
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
    BOOT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/pi-deepseek-free.XXXXXX")"
    BOOT_FILE="$BOOT_DIR/pi-deepseek-free.command"
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
  "$CMUX_BIN" new-workspace --name 'pi · DeepSeek Free' --cwd "$PI_WORKDIR" \
    --command "$LAUNCH" --focus true
  # Workspace focus is not macOS application activation: Finder's Terminal may
  # otherwise remain in front of an already-running cmux after a double-click.
  if ! "$OPEN_BIN" -a /Applications/cmux.app; then
    echo 'WARNING: 工作区已创建，但无法把 cmux 切到前台；请手动点击 cmux。' >&2
  fi
  exit 0
fi
if [ "$CHECK_ONLY" -eq 1 ]; then
  echo 'Check-only: 当前终端参数检查通过；尚未验证登录、免费额度或模型调用。'
  exit 0
fi
exec /bin/bash "$ROOT/launchers/start-pi-full.command" \
  --provider opencode-cli --models "$PI_MODEL" \
  --extension "$ROOT/extensions/opencode-free/index.ts" \
  --session-dir "${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/sessions/opencode-free" "$@"
