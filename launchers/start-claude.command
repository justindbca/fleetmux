#!/bin/bash
# Start Claude Code (Claude Code 2.x CLI) as a cmux workspace, same shape as start-dsh.command.
# Default: new cmux workspace (`claude · <workdir 名>`). --terminal: current terminal. --check-only: start nothing.
# Permission: default danger-full-access (--dangerously-skip-permissions); override with CLAUDE_PERMISSION_MODE.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SELF="$ROOT/launchers/start-claude.command"

CLAUDE_TITLE="${CLAUDE_TITLE:-claude · $(basename "${CLAUDE_WORKDIR:-$ROOT}")}"
export CLAUDE_WORKDIR="${CLAUDE_WORKDIR:-$ROOT}"
[ -d "$CLAUDE_WORKDIR" ] || { echo "ERROR: CLAUDE_WORKDIR 目录不存在: $CLAUDE_WORKDIR" >&2; exit 2; }
export CLAUDE_WORKDIR="$(cd "$CLAUDE_WORKDIR" && pwd)"

# danger-full-access = 不弹授权（对标 DSH 的 DSH_PERMISSION_MODE 默认值）。
# 设 CLAUDE_PERMISSION_MODE=default 走正常授权流程。
CLAUDE_PERMISSION_MODE="${CLAUDE_PERMISSION_MODE:-danger-full-access}"
case "$CLAUDE_PERMISSION_MODE" in
  danger-full-access|default) ;;
  *) echo 'ERROR: CLAUDE_PERMISSION_MODE 只认 danger-full-access / default。' >&2; exit 2 ;;
esac

CHECK_ONLY=0
IN_TERMINAL=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --check-only) CHECK_ONLY=1; shift ;;
    --terminal) IN_TERMINAL=1; shift ;;
    *) break ;;
  esac
done

# ---------------------------------------------------------------- 找 claude
CLAUDE_BIN="${CLAUDE_BIN:-}"
if [ -z "$CLAUDE_BIN" ]; then
  CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
fi
if [ -z "$CLAUDE_BIN" ] || [ ! -x "$CLAUDE_BIN" ]; then
  echo 'ERROR: 找不到 claude 命令；请安装 Claude Code 或设置 CLAUDE_BIN。' >&2
  exit 127
fi

CLAUDE_VERSION="$("$CLAUDE_BIN" --version 2>/dev/null | head -1 || true)"
[ -n "$CLAUDE_VERSION" ] || { echo 'ERROR: claude --version 无输出。' >&2; exit 2; }

# ---------------------------------------------------------------- 组参数
ARGS=()
[ "$CLAUDE_PERMISSION_MODE" = "danger-full-access" ] && ARGS+=("--dangerously-skip-permissions")

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "claude: $CLAUDE_BIN"
  echo "version: $CLAUDE_VERSION"
  echo "workdir: $CLAUDE_WORKDIR"
  echo "permission: $CLAUDE_PERMISSION_MODE"
  echo "args: ${ARGS[*]:-<none>}"
  echo 'Check-only: 通过；未启动会话。'
  exit 0
fi

# ---------------------------------------------------------------- 当前终端
if [ "$IN_TERMINAL" -eq 1 ]; then
  cd "$CLAUDE_WORKDIR"
  exec "$CLAUDE_BIN" ${ARGS[@]+"${ARGS[@]}"} "$@"
fi

# ---------------------------------------------------------------- cmux 工作区
CMUX_BIN="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
if [ ! -x "$CMUX_BIN" ]; then
  echo 'ERROR: 找不到 cmux；请安装 cmux 或设置 CMUX_BIN。当前终端模式请加 --terminal。' >&2
  exit 127
fi

VERSION="$("$CMUX_BIN" --version 2>/dev/null || true)"
if [[ ! "$VERSION" =~ ([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
  echo 'ERROR: 无法确定 cmux 版本。' >&2; exit 2
fi
if (( BASH_REMATCH[1] == 0 && (BASH_REMATCH[2] < 64 || (BASH_REMATCH[2] == 64 && BASH_REMATCH[3] < 25)) )); then
  echo 'ERROR: 需要 cmux >= 0.64.25。' >&2; exit 2
fi

ENV_ARGS=("CLAUDE_TITLE=$CLAUDE_TITLE" "CLAUDE_WORKDIR=$CLAUDE_WORKDIR" "CLAUDE_PERMISSION_MODE=$CLAUDE_PERMISSION_MODE")
[ -z "${CLAUDE_BIN:-}" ] || ENV_ARGS+=("CLAUDE_BIN=$CLAUDE_BIN")

printf -v LAUNCH '%q ' /usr/bin/env "${ENV_ARGS[@]}" /bin/bash "$SELF" --terminal "$@"
if [ "$(printf '%s' "$LAUNCH" | wc -c | tr -d ' ')" -gt 900 ]; then
  echo 'ERROR: 启动命令过长；请用 --terminal 在当前终端运行。' >&2
  exit 2
fi

OPEN_BIN="${CMUX_OPEN_BIN:-/usr/bin/open}"
if ! "$CMUX_BIN" ping >/dev/null 2>&1; then
  echo 'ERROR: cmux 未在运行或 socket 不可用；请先启动 cmux，或用 --terminal。' >&2
  exit 2
fi

"$CMUX_BIN" workspace new --title "$CLAUDE_TITLE" --command "$LAUNCH" >/dev/null 2>&1 \
  || { echo 'ERROR: 创建 cmux 工作区失败。' >&2; exit 2; }

echo "已启动 cmux 工作区：$CLAUDE_TITLE"
