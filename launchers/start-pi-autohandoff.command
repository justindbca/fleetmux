#!/bin/bash
# start-pi-autohandoff.command — pi 自动接力窗口启动器（cmux-fleet-pi）
#
# 用法: start-pi-autohandoff.command <交接单文件>
# 作用: 在 cmux 开新 workspace 跑接力会话。首条 prompt 由 auto-handoff 扩展在 session_start
#      读 PI_HANDOFF_FILE 注入，命令行保持短，不把长交接单塞进 cmux --command。
#
# 环境: PI_HANDOFF_RELAUNCH（有则照它启动，见 auto-handoff.ts）、PI_WORKDIR、PI_HANDOFF_TASK、PI_HANDOFF_HOP、PI_AUTO_HANDOFF_MODEL、
#      CMUX_BIN、FLEETMUX_PI_DIR、PI_BIN、PI_CANONICAL_LAUNCHER
#
# 机器差异：本机 canonical 有 --terminal 分支且认 PI_WORKDIR；Studio 的 canonical 两者都没有。
# 所以：canonical 含 IN_TERMINAL → 走 --terminal；否则直接调 pi，cwd 用 PI_WORKDIR。
set -euo pipefail

HANDOFF_FILE="${1:?用法: start-pi-autohandoff.command <交接单文件>}"
case "$HANDOFF_FILE" in
  /*) : ;;
  *) HANDOFF_FILE="$PWD/$HANDOFF_FILE" ;;
esac
[ -s "$HANDOFF_FILE" ] || { echo "ERROR: 交接单为空或不存在: $HANDOFF_FILE" >&2; exit 2; }

CMUX_BIN="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"
# fleet 仓根目录（可选）；没设就跳过这一候选。
FLEET_DIR="${FLEETMUX_PI_DIR:-${CMUX_FLEET_PI_DIR:-}}"  # CMUX_FLEET_PI_DIR: legacy name, kept for compatibility
PI_WORKDIR="${PI_WORKDIR:-$PWD}"
TASK="${PI_HANDOFF_TASK:-$(basename "$PI_WORKDIR")}"
HOP="${PI_HANDOFF_HOP:-1}"
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"

CANON=""
for c in "${PI_CANONICAL_LAUNCHER:-}" "$SELF_DIR/start-codex-flashnext.command" "${FLEET_DIR:+$FLEET_DIR/launchers/start-codex-flashnext.command}" "$HOME/cmux-fleet/launchers/start-codex-flashnext.command"; do
  if [ -n "$c" ] && [ -x "$c" ]; then CANON="$c"; break; fi
done

ENV_PREFIX="/usr/bin/env PI_WORKDIR=$(printf '%q' "$PI_WORKDIR") PI_HANDOFF_FILE=$(printf '%q' "$HANDOFF_FILE") PI_HANDOFF_TASK=$(printf '%q' "$TASK") PI_HANDOFF_HOP=$HOP"
if [ -n "${PI_AUTO_HANDOFF_MODEL:-}" ]; then
  ENV_PREFIX="$ENV_PREFIX PI_AUTO_HANDOFF_MODEL=$(printf '%q' "$PI_AUTO_HANDOFF_MODEL")"
fi

if [ -n "${PI_HANDOFF_RELAUNCH:-}" ] && [ -s "$PI_HANDOFF_RELAUNCH" ]; then
  # auto-handoff wrote how the handed-off session was started (its pi command line, model in use,
  # launcher env): the next session gets the same model and setup whatever launcher started it.
  REMOTE_CMD="/bin/bash $(printf '%q' "$PI_HANDOFF_RELAUNCH")"
elif [ -n "$CANON" ] && grep -q 'IN_TERMINAL' "$CANON" 2>/dev/null; then
  # 本机 canonical：--terminal 分支保留 PI_MODEL/领域扩展，且不做 900 字节启动行检查。
  REMOTE_CMD="cd $(printf '%q' "$PI_WORKDIR"); $ENV_PREFIX /bin/bash $(printf '%q' "$CANON") --terminal"
else
  # Studio 型 canonical：不认 PI_WORKDIR / --terminal；直接调 pi，cwd 用 PI_WORKDIR。
  PIBIN="${PI_BIN:-}"
  if [ -z "$PIBIN" ]; then
    for c in "$HOME/cmux-fleet/npm/bin/pi" "$(command -v pi 2>/dev/null || true)"; do
      if [ -n "$c" ] && [ -x "$c" ]; then PIBIN="$c"; break; fi
    done
  fi
  [ -n "$PIBIN" ] || { echo "ERROR: 找不到 pi 可执行文件；设置 PI_BIN。" >&2; exit 127; }
  MODEL="${PI_AUTO_HANDOFF_MODEL:-${PI_MODEL:-}}"
  MODEL_ARG=""
  if [ -n "$MODEL" ]; then MODEL_ARG=" --model $(printf '%q' "$MODEL")"; fi
  REMOTE_CMD="cd $(printf '%q' "$PI_WORKDIR"); export PATH=\"/opt/homebrew/bin:$(dirname "$PIBIN"):\$PATH\"; $ENV_PREFIX $(printf '%q' "$PIBIN")$MODEL_ARG"
fi

if ! "$CMUX_BIN" ping >/dev/null 2>&1; then
  echo "ERROR: cmux 不可用。手动接力：" >&2
  echo "  PI_WORKDIR=$PI_WORKDIR /bin/bash -c \"$REMOTE_CMD\"" >&2
  exit 1
fi

"$CMUX_BIN" new-workspace --name "autohandoff $HOP:$TASK" --cwd "$PI_WORKDIR" --command "$REMOTE_CMD" --focus true
