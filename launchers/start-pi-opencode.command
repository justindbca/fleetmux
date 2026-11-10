#!/bin/bash
# Pi terminal UI + OpenCode Console API, like start-codex-flashnext.command.
# Run in a cmux terminal (or double-click to use the default terminal).
# Never launch OpenCode CLI, copy its OAuth tokens, or fall back to paid models.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CHECK_ONLY=0
if [ "${1:-}" = '--check-only' ]; then CHECK_ONLY=1; shift; fi

# Ignore PI_MODEL injected by a calling Pi session.
MODEL="${PI_OPENCODE_MODEL:-}"
if [ -z "$MODEL" ]; then
  echo '需要设置 PI_OPENCODE_MODEL，不能沿用 App 的免费模型。' >&2
  echo '当前 deepseek-v4.1-flash-free 被 OpenCode 服务端限制为仅 OpenCode 客户端可用。' >&2
  echo '本启动器不会绕过限制，也不会自动改用付费模型。' >&2
  echo '请在 Pi 中 /login opencode 配置 Console API key，再通过 PI_OPENCODE_MODEL 明确选择模型。' >&2
  echo '也可通过 OPENCODE_API_KEY 环境变量提供 API key；请勿把密钥写进本文件。' >&2
  exit 2
fi
case "$MODEL" in
  opencode/?*) ;;
  */*) echo 'ERROR: 只支持 OpenCode Console（opencode），不是 Go 或其他 provider。' >&2; exit 2 ;;
  *) MODEL="opencode/$MODEL" ;;
esac
if [ "$MODEL" = 'opencode/deepseek-v4.1-flash-free' ]; then
  echo 'ERROR: deepseek-v4.1-flash-free 仅允许 OpenCode 客户端使用，不能通过 Pi 调用。' >&2
  exit 2
fi

export PI_MODEL="$MODEL"
export PI_THINKING="${PI_OPENCODE_THINKING:-off}"
export PI_WORKDIR="${PI_WORKDIR:-$PROJECT_DIR}"
if [ ! -d "$PI_WORKDIR" ]; then
  echo 'ERROR: PI_WORKDIR 目录不存在。' >&2
  exit 2
fi
SESSION_DIR="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/sessions/opencode"

printf 'Interface: Pi terminal UI\nProvider: OpenCode Console\nModel: %s\nDirectory: %s\n' "$MODEL" "$PI_WORKDIR"
echo '费用由所选模型决定；不会自动切换模型。认证使用 Pi /login opencode 或 OPENCODE_API_KEY。'
if [ "$CHECK_ONLY" -eq 1 ]; then
  echo 'Check-only: 参数检查通过；尚未验证认证、模型可用性或网络连接。'
  exit 0
fi

# Reuse the existing real-Pi resolution and child-process cleanup wrapper.
# API keys stay out of argv and files. Sessions do not mix with Flash Next/Codex.
exec /bin/bash "$PROJECT_DIR/launchers/start-pi-full.command" \
  --provider opencode --session-dir "$SESSION_DIR" "$@"
