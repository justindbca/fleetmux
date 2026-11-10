#!/bin/bash
# Start pi (pi.dev coding agent) with full access.
# 与 start-codex-danger-full.command 同构：解析真实 CLI，避开 openclaw shim。
# pi 本身没有沙盒和审批，默认就是全权限，不需要 bypass 参数。

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# PI_WORKDIR: pi 的工作目录（决定读哪份 AGENTS.md）；缺省本仓。
PROJECT_DIR="${PI_WORKDIR:-$PROJECT_DIR}"
cd "$PROJECT_DIR"

export TZ="${TZ:-Asia/Shanghai}"

case ":$PATH:" in
  *":/opt/homebrew/bin:"*) ;;
  *) export PATH="/opt/homebrew/bin:$PATH" ;;
esac

SHIM_DIR="$PROJECT_DIR/scripts/openclaw-shims"
clean_path_without_shims() {
  printf '%s' "$PATH" \
    | tr ':' '\n' \
    | awk -v shim="$SHIM_DIR" '$0 != shim' \
    | paste -sd ':' -
}

# 路径解析优先级：PI_BIN → fnm default → Homebrew → 去掉 shim 的 PATH。
if [ -n "${PI_BIN:-}" ] && [ -x "$PI_BIN" ]; then
  :
elif [ -n "${FNM_DIR:-}" ] && [ -x "$FNM_DIR/aliases/default/bin/pi" ]; then
  PI_BIN="$FNM_DIR/aliases/default/bin/pi"
elif [ -x "$HOME/.local/share/fnm/aliases/default/bin/pi" ]; then
  PI_BIN="$HOME/.local/share/fnm/aliases/default/bin/pi"
elif [ -x "/opt/homebrew/bin/pi" ]; then
  PI_BIN="/opt/homebrew/bin/pi"
else
  CLEAN_PATH="$(clean_path_without_shims)"
  PI_BIN="$(PATH="$CLEAN_PATH" command -v pi 2>/dev/null || true)"
  if [ -z "$PI_BIN" ]; then
    echo "ERROR: 找不到 pi。先 npm install -g @mariozechner/pi-coding-agent，或设置 PI_BIN。" >&2
    exit 127
  fi
fi

# Finder / GUI-launched shells may not have initialized fnm. Pi's JS entrypoint
# uses /usr/bin/env node, so resolving pi alone is insufficient: prefer its sibling Node.
PI_BIN_DIR="$(cd "$(dirname "$PI_BIN")" && pwd)"
PI_BIN="$PI_BIN_DIR/$(basename "$PI_BIN")"
if [ -x "$PI_BIN_DIR/node" ]; then
  export PATH="$PI_BIN_DIR:$PATH"
fi
if ! PI_VERSION="$("$PI_BIN" --version)"; then
  echo "ERROR: Pi 无法启动。请检查 Node.js 或 PI_BIN（${PI_BIN}）；未创建 Pi 会话。" >&2
  exit 127
fi

# 不写死模型：留空时跟 ~/.codex/config.toml 顶层 model 走（走 ChatGPT 订阅），
# 读不到再用 pi 上次选的模型。例：PI_MODEL=openai-codex/gpt-6.1-sol
MODEL="${PI_MODEL:-}"
if [ -z "$MODEL" ] && [ -f "$HOME/.codex/config.toml" ]; then
  CODEX_MODEL="$(grep -E '^model *=' "$HOME/.codex/config.toml" | head -1 | cut -d'"' -f2 || true)"
  [ -n "$CODEX_MODEL" ] && MODEL="openai-codex/$CODEX_MODEL"
fi
THINKING="${PI_THINKING:-high}"

ARGS=(--thinking "$THINKING")
[ -n "$MODEL" ] && ARGS+=(--model "$MODEL")

echo "Directory: $(pwd)"
echo "pi: $PI_BIN ($PI_VERSION)"
echo "Options: ${ARGS[*]}"
if [[ "$MODEL" == flashnext/* ]]; then
  echo "Flash Next 使用本地 oMLX，无需 ChatGPT 登录；上下文由 pi 自动压缩。"
elif [[ "$MODEL" == opencode-cli/* ]]; then
  echo "免费模型通过官方 OpenCode CLI 本地服务调用；认证由 OpenCode 管理，无需 Pi /login。"
elif [[ "$MODEL" == workbuddy-ds/* ]]; then
  echo "WorkBuddy AI 的 DeepSeek 经本机 magpie 网关调用；登录由 magpie 管理，无需 Pi /login。"
elif [[ "$MODEL" == opencode/* ]]; then
  echo "OpenCode Console 使用 /login opencode 或 OPENCODE_API_KEY；不使用 ChatGPT 登录。"
else
  echo "AGENTS.md / CLAUDE.md 由 pi 自动读入。首次使用先 /login 选 ChatGPT Plus/Pro (Codex)。"
fi

# 不用 exec：保留 wrapper 作为 pane 主进程，收到信号时清理 pi 子进程树。
child=""
exit_code=0

cleanup() {
  trap '' HUP TERM INT
  if [ -n "${child:-}" ]; then
    kill -TERM "$child" 2>/dev/null || true
  fi
  kill -- -$$ 2>/dev/null || true
  if [ -n "${child:-}" ]; then
    wait "$child" 2>/dev/null || true
  fi
}

handle_signal() {
  local signal="$1"
  local signal_exit_code=0

  case "$signal" in
    HUP) signal_exit_code=129 ;;
    INT) signal_exit_code=130 ;;
    TERM) signal_exit_code=143 ;;
  esac

  cleanup
  trap - EXIT
  exit "$signal_exit_code"
}

trap cleanup EXIT
trap 'handle_signal HUP' HUP
trap 'handle_signal TERM' TERM
trap 'handle_signal INT' INT

"$PI_BIN" "${ARGS[@]}" "$@" <&0 &
child=$!
wait "$child" || exit_code=$?
exit "$exit_code"
