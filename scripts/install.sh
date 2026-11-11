#!/bin/bash
# Install fleetmux for the current user, in one command:
#   curl -fsSL https://raw.githubusercontent.com/justindbca/fleetmux/main/scripts/install.sh | bash
#
# Steps: resolve the release (latest, or $FLEETMUX_VERSION) -> create $FLEETMUX_RELEASES/v<version> with a bare mkdir
# (an existing install is never joined or overwritten) -> unpack that tag's source -> a .venv on the Python that
# deps.yaml names, provided by uv (uv downloads that Python when the host has none) -> the pinned packages ->
# the prebuilt fleetmux binaries (scripts/install_fleetmux.sh, sha256-checked) -> links in $FLEETMUX_BIN_DIR ->
# scripts/check_deps.py. A failed install removes only the directory this run created.
#
# Needs: curl, tar, sha256sum or shasum. Nothing else from the host: no system Python, no compiler, no root.
# What the check lists as missing afterwards (redis-server, tmux, git, an agent CLI) is what `fleetmux start` needs;
# the install itself does not stop for it.
#
# Environment:
#   FLEETMUX_VERSION   release to install, e.g. 0.2.1 (default: the latest GitHub release)
#   FLEETMUX_RELEASES  where releases live (default ~/fleetmux-releases); this one goes to <dir>/v<version>
#   FLEETMUX_BIN_DIR   where the `fleetmux` / `fleetmux-web` links go (default ~/.local/bin)
#   FLEETMUX_REPO      GitHub owner/name (default justindbca/fleetmux)
#   FLEETMUX_SOURCE    a local source tarball laid out like GitHub's tag archive (one top-level directory), used
#                      instead of downloading it; needs FLEETMUX_VERSION (tests, offline installs)
#   UV                 a uv binary to use; else `uv` on PATH; else uv $UV_VERSION is installed under <releases>/.uv
set -euo pipefail
REPO=${FLEETMUX_REPO:-justindbca/fleetmux}
RELEASES=${FLEETMUX_RELEASES:-$HOME/fleetmux-releases}
BIN_DIR=${FLEETMUX_BIN_DIR:-$HOME/.local/bin}
UV_VERSION=${UV_VERSION:-0.13.7}

die() { echo "install: $*" >&2; exit 1; }
for tool in curl tar; do command -v "$tool" >/dev/null 2>&1 || die "need $tool"; done
command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || die "need sha256sum or shasum"

version=${FLEETMUX_VERSION:-}
if [ -n "${FLEETMUX_SOURCE:-}" ] && [ -z "$version" ]; then die "FLEETMUX_SOURCE needs FLEETMUX_VERSION"; fi
if [ -z "$version" ]; then
  latest=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest") \
    || die "cannot reach https://github.com/$REPO/releases/latest"
  version=${latest##*/tag/}
  [ "$version" != "$latest" ] || die "no published release at https://github.com/$REPO/releases"
fi
version=${version#v}
case "$version" in ''|*[!0-9A-Za-z.+-]*) die "invalid version '$version'" ;; esac
case "$RELEASES" in /*) ;; *) RELEASES=$(pwd)/$RELEASES ;; esac
if [ -n "${FLEETMUX_SOURCE:-}" ]; then
  [ -f "$FLEETMUX_SOURCE" ] || die "no such file: $FLEETMUX_SOURCE"
  FLEETMUX_SOURCE=$(cd "$(dirname "$FLEETMUX_SOURCE")" && pwd)/$(basename "$FLEETMUX_SOURCE")
fi
REL=$RELEASES/v$version

link() {
  mkdir -p "$BIN_DIR"
  for f in fleetmux fleetmux-web; do
    if [ -x "$REL/bin/$f" ]; then ln -sfn "$REL/bin/$f" "$BIN_DIR/$f"; fi
  done
  echo "linked: $BIN_DIR/fleetmux -> $REL/bin/fleetmux"
  case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "note: $BIN_DIR is not on PATH; add it to your shell profile" ;; esac
}

if [ -d "$REL" ]; then
  [ -x "$REL/.venv/bin/python" ] && [ -x "$REL/bin/fleetmux" ] || die "$REL exists but is incomplete; remove it and run again"
  echo "fleetmux $version is already installed: $REL"
  link
  exit 0
fi

mkdir -p "$RELEASES"
mkdir "$REL" || die "cannot create $REL"
READY=0
cleanup() { if [ "$READY" != 1 ]; then rm -rf "$REL"; echo "install failed: removed $REL" >&2; fi; }
trap cleanup EXIT
cd "$REL"

echo "fleetmux $version -> $REL"
if [ -n "${FLEETMUX_SOURCE:-}" ]; then
  tar xzf "$FLEETMUX_SOURCE" --strip-components=1 || die "cannot unpack $FLEETMUX_SOURCE"
else
  curl -fsSL "https://github.com/$REPO/archive/refs/tags/v$version.tar.gz" | tar xz --strip-components=1 \
    || die "cannot download the v$version source"
fi

uv=${UV:-$(command -v uv 2>/dev/null || true)}
if [ -z "$uv" ]; then
  uv=$RELEASES/.uv/uv
  if [ ! -x "$uv" ]; then
    echo "installing uv $UV_VERSION into $RELEASES/.uv (it provides Python)"
    curl -fsSL "https://astral.sh/uv/$UV_VERSION/install.sh" \
      | env UV_INSTALL_DIR="$RELEASES/.uv" UV_NO_MODIFY_PATH=1 sh >/dev/null || die "cannot install uv"
  fi
fi

# deps.yaml `python: min:` (the first block, so a later `min:` of a program never wins)
py=$(awk '/^python:[[:space:]]*$/{p=1; next} /^[a-zA-Z_]+:/{p=0} p && $1=="min:"{gsub(/["\047]/,"",$2); print $2; exit}' deps.yaml)
[ -n "$py" ] || die "no python.min in deps.yaml"
"$uv" venv -q --python "$py" .venv || die "uv cannot provide Python $py"
"$uv" pip install -q --python .venv/bin/python -r requirements.txt
"$uv" pip install -q --python .venv/bin/python --no-deps -e .
# start refuses a private Redis root that others can read
mkdir -p .runtime/redis && chmod 700 .runtime .runtime/redis

scripts/install_fleetmux.sh --dest "$REL/bin"
READY=1
link

echo
echo "dependency check (scripts/check_deps.py):"
if PATH="$REL/bin:$PATH" .venv/bin/python scripts/check_deps.py; then
  echo
  echo "ready: fleetmux start --help"
else
  echo
  echo "fleetmux is installed. Install the FAIL items above before \`fleetmux start\`."
  echo "Debian / Ubuntu: sudo apt install redis-server tmux git; then install and log in to one agent CLI (claude, codex, pi or opencode)."
fi
