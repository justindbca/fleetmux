#!/bin/bash
# Shared core for the DSH (DeepSeek Harness) launchers; run start-dsh-<model>.command instead.
# Default: new cmux workspace running `dsh web` (the web UI opens in the browser).
# DSH_UI=tui: run the third-party dsh-TUI (@deepseek-harness-tui/dsh-tui, pinned) in the pane instead.
# --terminal: run in the current terminal. --check-only: check everything, start nothing.
# Model definitions come from pi's ~/.pi/agent/models.json (provider DSH_PROVIDER), so pi and DSH
# use one source. The API key goes only into the dsh process environment, never into a file or argv.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SELF="$ROOT/launchers/start-dsh.command"

# Operator's private launcher settings (outside the repo), e.g. DSH_PURGE=on; explicit env still wins.
# Default ~/.fleetmux/launchers.env. A file still at the pre-fleetmux place is not read: stop and say how to move it.
LAUNCHER_ENV="${FLEETMUX_LAUNCHER_ENV:-$HOME/.fleetmux/launchers.env}"
if [ -z "${FLEETMUX_LAUNCHER_ENV:-}" ] && [ ! -f "$LAUNCHER_ENV" ] && [ -f "$HOME/.config/fleetmux/launchers.env" ] \
   && [ ! -L "$HOME/.config/fleetmux/launchers.env" ]; then
  echo "fleetmux: $HOME/.config/fleetmux/launchers.env is not read any more; run 'fleetmux home migrate'" >&2
  exit 2
fi
if [ -f "$LAUNCHER_ENV" ]; then
  while IFS='=' read -r _k _v; do
    case "$_k" in ''|\#*) continue ;; esac
    [[ "$_k" =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
    [ -n "${!_k+x}" ] || export "$_k=$_v"
  done < "$LAUNCHER_ENV"
  unset _k _v
fi

: "${DSH_PROVIDER:?ERROR: 请用 start-dsh-flashnext.command / start-dsh-deepseek.command 启动（缺 DSH_PROVIDER）}"
DSH_TITLE="${DSH_TITLE:-dsh · $DSH_PROVIDER}"
DSH_UI="${DSH_UI:-web}"
case "$DSH_UI" in web|tui) ;; *) echo 'ERROR: DSH_UI 只认 web / tui。' >&2; exit 2 ;; esac

# Preserve the launcher's documented default while allowing explicit overrides.
export DSH_PERMISSION_MODE="${DSH_PERMISSION_MODE:-danger-full-access}"

# 可选 dsh-purge（缺省关）：DSH_PURGE=on 时改用装了 dsh-purge-community 插件的 profile 启动
# （按 dsh-tui → web 顺序找，找不到自动降为 off）；界面仍由 DSH_UI 决定（web 起 web 服务、tui 起 dsh-TUI）。
DSH_PURGE="${DSH_PURGE:-off}"
if [ "$DSH_PURGE" != off ]; then
  PURGE_PROFILE_RESOLVED=""
  for _p in dsh-tui web; do
    if grep -q '"dsh-purge-community"' "$HOME/.dsh/profiles/$_p/package.json" 2>/dev/null; then
      PURGE_PROFILE_RESOLVED="$_p"
      break
    fi
  done
  unset _p
  if [ -n "$PURGE_PROFILE_RESOLVED" ]; then
    DSH_PROFILE="$PURGE_PROFILE_RESOLVED"
    export DSH_PROFILE
  else
    DSH_PURGE=off
  fi
fi
[ "${DSH_PROFILE:-}" ] || DSH_PROFILE="$DSH_UI"

# fnm 的 dsh 是 symlink shim，purge 定位不到宿主与 CLI：显式给 DSH_BASE + PURGE_DSH_CLI。
if [ "$DSH_PURGE" != off ]; then
  if [ -z "${DSH_BASE:-}" ]; then
    for _v in "$HOME/.local/share/fnm/node-versions"/*/installation/lib/node_modules/@deepseek-ai/dsh; do
      if [ -e "$_v/node_modules/@deepseek-ai/dsh-base" ]; then
        export DSH_BASE="$_v/node_modules/@deepseek-ai"
        break
      fi
    done
    unset _v
  fi
  if [ -z "${PURGE_DSH_CLI:-}" ]; then
    _dshcli="${DSH_BIN:-$(command -v dsh || true)}"
    if [ -n "$_dshcli" ]; then
      _dshreal="$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$_dshcli" 2>/dev/null || true)"
      [ -f "$_dshreal" ] && export PURGE_DSH_CLI="$_dshreal"
    fi
    unset _dshcli _dshreal
  fi
fi
DSH_TUI_VERSION=0.18.0
DSH_MODEL_ID="${DSH_MODEL_ID:-}"
export DSH_WORKDIR="${DSH_WORKDIR:-$ROOT}"
if [ ! -d "$DSH_WORKDIR" ]; then echo 'ERROR: DSH_WORKDIR 目录不存在。' >&2; exit 2; fi
export DSH_WORKDIR="$(cd "$DSH_WORKDIR" && pwd)"

CHECK_ONLY=0
IN_TERMINAL=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --check-only) CHECK_ONLY=1; shift ;;
    --terminal) IN_TERMINAL=1; shift ;;
    *) break ;;
  esac
done

# Same cmux routing as start-codex-flashnext.command.
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
  # Only forward explicit launcher configuration, never API keys or parent session IDs.
  ENV_ARGS=("DSH_PROVIDER=$DSH_PROVIDER" "DSH_TITLE=$DSH_TITLE" "DSH_WORKDIR=$DSH_WORKDIR" "DSH_UI=$DSH_UI" "DSH_PURGE=$DSH_PURGE" "DSH_PROFILE=$DSH_PROFILE")
  [ -z "${DSH_BASE:-}" ] || ENV_ARGS+=("DSH_BASE=$DSH_BASE")
  [ -z "${PURGE_DSH_CLI:-}" ] || ENV_ARGS+=("PURGE_DSH_CLI=$PURGE_DSH_CLI")
  [ -z "${DSH_PERMISSION_MODE:-}" ] || ENV_ARGS+=("DSH_PERMISSION_MODE=$DSH_PERMISSION_MODE")
  [ -z "$DSH_MODEL_ID" ] || ENV_ARGS+=("DSH_MODEL_ID=$DSH_MODEL_ID")
  [ -z "${DSH_BIN:-}" ] || ENV_ARGS+=("DSH_BIN=$DSH_BIN")
  [ -z "${DSH_HOME:-}" ] || ENV_ARGS+=("DSH_HOME=$DSH_HOME")
  [ -z "${PI_CODING_AGENT_DIR:-}" ] || ENV_ARGS+=("PI_CODING_AGENT_DIR=$PI_CODING_AGENT_DIR")
  printf -v LAUNCH '%q ' /usr/bin/env "${ENV_ARGS[@]}" /bin/bash "$SELF" --terminal "$@"
  # cmux sends this into a login shell: fail before opening a workspace if the TTY line is too long.
  if [ "$(printf '%s' "$LAUNCH" | wc -c | tr -d ' ')" -gt 900 ]; then
    echo 'ERROR: 启动命令过长；请用 --terminal 在当前终端运行。' >&2
    exit 2
  fi
  if [ "$CHECK_ONLY" -eq 1 ]; then
    echo 'Check-only: cmux 参数检查通过；未启动工作区，尚未验证 dsh 或模型调用。'
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
    BOOT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/dsh-launch.XXXXXX")"
    BOOT_FILE="$BOOT_DIR/dsh-launch.command"
    chmod 700 "$BOOT_DIR"
    {
      printf '#!/bin/bash\nset -euo pipefail\n'
      # Delete only this one private bootstrap before transferring to dsh.
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
  "$CMUX_BIN" new-workspace --name "$DSH_TITLE" --cwd "$DSH_WORKDIR" \
    --command "$LAUNCH" --focus true
  # Workspace focus is not macOS application activation.
  if ! "$OPEN_BIN" -a /Applications/cmux.app; then
    echo 'WARNING: 工作区已创建，但无法把 cmux 切到前台；请手动点击 cmux。' >&2
  fi
  exit 0
fi

# --- current terminal: build the patch from pi's models.json and start dsh web / dsh-TUI ---
DSH_BIN="${DSH_BIN:-$(command -v dsh || true)}"
if [ ! -x "${DSH_BIN:-}" ]; then
  echo 'ERROR: 找不到 dsh；先 npm install -g @deepseek-ai/dsh，或设置 DSH_BIN。' >&2
  exit 127
fi
# One DSH home per provider, so sessions of different models stay apart (like pi's per-model session dirs).
export DSH_HOME="${DSH_HOME:-$HOME/.dsh/homes/$DSH_PROVIDER}"
mkdir -p "$DSH_HOME"
PATCH="$DSH_HOME/launcher.patch.yml"
PI_MODELS_JSON="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/models.json"
# JSON is valid YAML; the patch holds no key (apiKeyEnv names the variable instead).
MODEL_ID="$(/usr/bin/python3 -I - "$PI_MODELS_JSON" "$DSH_PROVIDER" "$DSH_MODEL_ID" "$PATCH" <<'PY'
import json, os, sys
src, provider, want, out = sys.argv[1:]
try:
    entry = json.load(open(src))["providers"][provider]
except (OSError, ValueError, KeyError):
    sys.exit(f"ERROR: {src} 里没有 provider {provider}")
models = entry.get("models") or []
if not models:
    sys.exit(f"ERROR: {src} 的 {provider} 没有模型")
model = next((m for m in models if m.get("id") == want), models[0])
model_id = want or model["id"]
row = {"id": model_id, "name": model.get("name") or model_id}
for key in ("contextWindow", "maxTokens"):
    if model.get(key):
        row[key] = model[key]
patch = [
    {"id": "llm-pi-ai", "config": {"providers": {provider: {
        "displayName": model.get("name") or provider, "apiKeyEnv": "DSH_LAUNCHER_API_KEY",
        "api": entry.get("api", "openai-completions"), "baseURL": entry["baseUrl"], "models": [row]}}}},
    {"id": "agent-default-model", "config": {"provider": provider, "model": model_id}},
]
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(patch, f, ensure_ascii=False, indent=1)
print(model_id)
PY
)"
DSH_LAUNCHER_API_KEY="$(/usr/bin/python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["providers"][sys.argv[2]].get("apiKey") or "")' "$PI_MODELS_JSON" "$DSH_PROVIDER")"
export DSH_LAUNCHER_API_KEY
export DSH_TELEMETRY_MODE=DISABLED
PROFILE=web; [ "$DSH_UI" = web ] || PROFILE=dsh-tui
printf 'DSH %s → %s/%s（%s）\n' "$DSH_UI" "$DSH_PROVIDER" "$MODEL_ID" "$("$DSH_BIN" --version)"
echo "工作目录 ${DSH_WORKDIR}；会话存 ${DSH_HOME}；遥测已关。权限 ${DSH_PERMISSION_MODE}（不弹授权）。Ctrl+C 停止。"
TUI_PKG="$DSH_HOME/profiles/dsh-tui/node_modules/@deepseek-harness-tui/dsh-tui/package.json"
if [ "$PROFILE" = dsh-tui ] && ! grep -q "\"version\": \"$DSH_TUI_VERSION\"" "$TUI_PKG" 2>/dev/null; then
  if [ "$CHECK_ONLY" -eq 1 ]; then
    echo "Check-only: $DSH_HOME 还没装 dsh-tui $DSH_TUI_VERSION profile，正式启动时会先装。"; exit 0
  fi
  echo "首次在 $DSH_HOME 装 dsh-tui $DSH_TUI_VERSION profile（需要 pnpm）…"
  "$DSH_BIN" plugin --profile dsh-tui add "@deepseek-harness-tui/dsh-tui@$DSH_TUI_VERSION" </dev/null
fi
if [ "$CHECK_ONLY" -eq 1 ]; then
  "$DSH_BIN" --profile "$PROFILE" --patch "$PATCH" --dump-config </dev/null >/dev/null
  echo 'Check-only: dsh 配置可组装；未启动服务，尚未验证模型调用。'
  exit 0
fi
cd "$DSH_WORKDIR"
if [ "$DSH_UI" = web ]; then
  exec "$DSH_BIN" --profile "$DSH_PROFILE" --patch "$PATCH" --port 0 "$@"
fi
exec "$DSH_BIN" --profile dsh-tui --patch "$PATCH" "$@"
