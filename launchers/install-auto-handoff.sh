#!/bin/bash
# install-auto-handoff.sh — 安装 pi 自动接力（扩展 + 接力启动器）到 pi 用户扩展目录
#
# 用法:
#   install-auto-handoff.sh                       # 装到本机 $HOME/.pi/agent/extensions
#   install-auto-handoff.sh --agent-dir DIR       # 装到指定 agent 目录
#   install-auto-handoff.sh --host HOST           # 经 ssh 装到 HOST 的 $HOME/.pi/agent/extensions
#
# 两个文件必须放同一目录（扩展按兄弟路径找启动器，自洽部署）。
set -euo pipefail

FLEET_DIR="$(cd "$(dirname "$0")/.." && pwd)"
EXT_SRC="$FLEET_DIR/extensions/auto-handoff.ts"
LAUNCH_SRC="$FLEET_DIR/launchers/start-pi-autohandoff.command"
[ -f "$EXT_SRC" ] || { echo "ERROR: 缺 $EXT_SRC" >&2; exit 2; }
[ -f "$LAUNCH_SRC" ] || { echo "ERROR: 缺 $LAUNCH_SRC" >&2; exit 2; }

TARGET_HOST=""
AGENT_DIR="$HOME/.pi/agent"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --host) TARGET_HOST="$2"; shift 2 ;;
    --agent-dir) AGENT_DIR="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$TARGET_HOST" ]; then
  DEST="$AGENT_DIR/extensions"
  mkdir -p "$DEST"
  cp -f "$EXT_SRC" "$DEST/auto-handoff.ts"
  cp -f "$LAUNCH_SRC" "$DEST/start-pi-autohandoff.command"
  chmod +x "$DEST/start-pi-autohandoff.command"
  echo "installed -> $DEST"
  ls -l "$DEST"
  exit 0
fi

RHOME="$(ssh -o BatchMode=yes "$TARGET_HOST" 'printf %s "$HOME"')"
[ -n "$RHOME" ] || { echo "ERROR: 无法取 $TARGET_HOST 的 HOME。" >&2; exit 1; }
DEST="$RHOME/.pi/agent/extensions"
ssh -o BatchMode=yes "$TARGET_HOST" "mkdir -p '$DEST'"
scp -q "$EXT_SRC" "$TARGET_HOST:$DEST/auto-handoff.ts"
scp -q "$LAUNCH_SRC" "$TARGET_HOST:$DEST/start-pi-autohandoff.command"
ssh -o BatchMode=yes "$TARGET_HOST" "chmod +x '$DEST/start-pi-autohandoff.command'; ls -l '$DEST'"
echo "installed -> $TARGET_HOST:$DEST"
