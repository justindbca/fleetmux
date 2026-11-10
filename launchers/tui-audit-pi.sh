#!/bin/sh
# Local, bounded TUI audit Fleet only. Preserve Fleet's explicit bridge and protocol tools.
set -eu
PI="$HOME/.local/share/fnm/aliases/default/bin/pi"
if [ "$#" -eq 1 ] && [ "$1" = '--version' ]; then
  exec "$PI" --version
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
case "$(pwd -P)" in
  "$ROOT"/.runtime/ta/runs/*/agents/supervisor|"$ROOT"/.runtime/ta/runs/*/workspace/worker-[1-5]) ;;
  *) printf '%s\n' 'Refusing non-audit Fleet cwd' >&2; exit 2 ;;
esac
exec "$PI" --exclude-tools bash,edit "$@"
