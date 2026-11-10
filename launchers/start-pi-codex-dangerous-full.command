#!/bin/bash
# Open Pi + ChatGPT/Codex in a new cmux workspace.
# Pi has no sandbox or per-tool approvals. --approve trusts this project's resources;
# it does not grant root privileges or bypass macOS permissions.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STARTER="$PROJECT_DIR/launchers/start-pi-full.command"
CMUX_BIN="${CMUX_BIN:-/Applications/cmux.app/Contents/Resources/bin/cmux}"

# Ignore PI_MODEL injected by a calling Pi/Flash Next session.
# Override explicitly with PI_CODEX_MODEL (bare ID or openai-codex/<ID>).
MODEL="${PI_CODEX_MODEL:-}"
if [ -z "$MODEL" ] && [ -f "$HOME/.codex/config.toml" ]; then
  MODEL="$(awk '
    /^[[:space:]]*\[/ { exit }
    /^[[:space:]]*model[[:space:]]*=/ { split($0, parts, "\""); print parts[2]; exit }
  ' "$HOME/.codex/config.toml")"
fi
if [ -z "$MODEL" ]; then
  echo 'ERROR: No top-level model in ~/.codex/config.toml. Set PI_CODEX_MODEL explicitly.' >&2
  exit 2
fi
case "$MODEL" in
  openai-codex/?*) ;;
  */*) echo 'ERROR: This launcher only accepts the openai-codex provider.' >&2; exit 2 ;;
  *) MODEL="openai-codex/$MODEL" ;;
esac
THINKING="${PI_CODEX_THINKING:-high}"

if [ ! -f "$STARTER" ] || [ ! -x "$CMUX_BIN" ]; then
  echo 'ERROR: start-pi-full.command or the cmux CLI is missing.' >&2
  exit 127
fi
VERSION="$("$CMUX_BIN" --version)"
if [[ ! "$VERSION" =~ ([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
  echo 'ERROR: Cannot determine the cmux CLI version.' >&2
  exit 2
fi
if (( BASH_REMATCH[1] == 0 && (BASH_REMATCH[2] < 64 || (BASH_REMATCH[2] == 64 && BASH_REMATCH[3] < 25)) )); then
  echo 'ERROR: cmux >= 0.64.25 is required for reliable command startup.' >&2
  exit 2
fi

# Keep the existing Pi binary resolution and child-process cleanup wrapper.
ENV_ARGS=("PI_MODEL=$MODEL" "PI_THINKING=$THINKING")
[ -z "${PI_BIN:-}" ] || ENV_ARGS+=("PI_BIN=$PI_BIN")
CHECK_ONLY=0
if [ "${1:-}" = '--check-only' ]; then CHECK_ONLY=1; shift; fi
printf -v LAUNCH '%q ' /usr/bin/env "${ENV_ARGS[@]}" /bin/bash "$STARTER" \
  --approve --provider openai-codex "$@"
# cmux types the initial command into a login shell; avoid the macOS TTY line limit.
if [ "$(printf '%s' "$LAUNCH" | wc -c | tr -d ' ')" -gt 900 ]; then
  echo 'ERROR: Startup command is too long. Enter the task inside the new Pi window instead.' >&2
  exit 2
fi

printf 'Directory: %s\nModel: %s\nThinking: %s\ncmux: %s\n' "$PROJECT_DIR" "$MODEL" "$THINKING" "$VERSION"
echo 'Access: current OS user, no Pi sandbox/per-tool approvals; project resources trusted.'
if [ "$CHECK_ONLY" -eq 1 ]; then
  echo 'Check-only: configuration valid; no application/workspace started.'
  exit 0
fi

# Launch cmux if needed, but never quit/restart an existing application or fleet.
if ! "$CMUX_BIN" ping >/dev/null 2>&1; then
  /usr/bin/open -a /Applications/cmux.app
  for ((i=0; i<15; i++)); do
    "$CMUX_BIN" ping >/dev/null 2>&1 && break
    sleep 1
  done
fi
if ! "$CMUX_BIN" ping >/dev/null 2>&1; then
  echo 'ERROR: cmux socket unavailable. Open cmux and check its automation/socket access setting.' >&2
  exit 1
fi
exec "$CMUX_BIN" new-workspace --name 'pi · Codex' --cwd "$PROJECT_DIR" \
  --command "$LAUNCH" --focus true
