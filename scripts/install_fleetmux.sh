#!/bin/bash
# Install the prebuilt fleetmux binary pinned by deps.yaml (programs.fleetmux.min) and verify its sha256; on a
# desktop session also install the fleetmux-web window (brief-m25 §3.6 W5).
#   scripts/install_fleetmux.sh [--dest DIR] [--print-target]
# Contract: tests/test_install_fleetmux.py. Release assets come from .github/workflows/release.yml:
#   $FLEETMUX_RELEASE_BASE/v<ver>/fleetmux-<ver>-<triple>.tar.gz   (one top-level `fleetmux` executable)
#   $FLEETMUX_RELEASE_BASE/v<ver>/fleetmux-<ver>-<triple>.tar.gz.sha256   (`<hex>  <file name>`)
#   $FLEETMUX_RELEASE_BASE/v<ver>/fleetmux-web-<ver>-<web triple>.tar.gz (+ .sha256)   (top-level `fleetmux-web`;
#   gnu on Linux, the same triple on macOS)
# Desktop session = macOS, or Linux with DISPLAY or WAYLAND_DISPLAY set. Headless: fleetmux only, no request for
# fleetmux-web, one line saying so.
# --dest DIR      install DIR/fleetmux (+ DIR/fleetmux-web) (default ~/.local/bin); nothing lands in DIR unless its
#                 sha256 matches, and an earlier DIR/fleetmux-web is never removed or replaced by an unverified one
# --print-target  print the host's fleetmux target triple and exit (no network)
# Exit: 0 installed / printed (also when the fleetmux-web asset cannot be downloaded: a warning, fleetmux stays
# installed), 1 unsupported host, fleetmux download / sha256 failure, or fleetmux-web sha256 failure (fleetmux-web
# not installed; the verified fleetmux stays), 2 usage.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
BASE=${FLEETMUX_RELEASE_BASE:-https://github.com/justindbca/fleetmux/releases/download}
dest=$HOME/.local/bin
print_target=0

usage() { echo "usage: $0 [--dest DIR] [--print-target]" >&2; exit 2; }
die() { echo "install_fleetmux: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case $1 in
    --dest) [ $# -ge 2 ] || usage; dest=$2; shift 2 ;;
    --dest=*) dest=${1#--dest=}; shift ;;
    --print-target) print_target=1; shift ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

target() {
  local system machine
  system=$(uname -s); machine=$(uname -m)
  case "$system $machine" in
    'Darwin arm64'|'Darwin aarch64') echo aarch64-apple-darwin ;;
    'Darwin x86_64') echo x86_64-apple-darwin ;;
    'Linux x86_64'|'Linux amd64') echo x86_64-unknown-linux-musl ;;
    'Linux aarch64'|'Linux arm64') echo aarch64-unknown-linux-musl ;;
    *) die "no prebuilt fleetmux for $system $machine (build it: cargo install --path fleetmux/crates/app)" ;;
  esac
}

triple=$(target)
if [ "$print_target" = 1 ]; then echo "$triple"; exit 0; fi

# programs.fleetmux.min, e.g.  fleetmux: {cmd: [fleetmux, --version], min: "0.1.0", when: fleetmux, ...}
version=$(sed -n 's/^  fleetmux:.*[{ ,]min: *"\{0,1\}\([0-9][0-9A-Za-z.+-]*\)"\{0,1\}.*/\1/p' "$ROOT/deps.yaml" | head -n 1)
[ -n "$version" ] || die "no programs.fleetmux.min in $ROOT/deps.yaml"

if command -v sha256sum >/dev/null 2>&1; then
  hash_of() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
  hash_of() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
  die "need sha256sum or shasum to verify the download"
fi
command -v curl >/dev/null 2>&1 || die "need curl"

name=fleetmux-$version-$triple.tar.gz
url=$BASE/v$version/$name
work=$(mktemp -d "${TMPDIR:-/tmp}/fleetmux-install.XXXXXX")  # never inside DEST: a failed run leaves DEST as it was
staged=
cleanup() { rm -rf "$work"; if [ -n "$staged" ]; then rm -f "$staged"; fi; }
trap cleanup EXIT

curl -fsSL -o "$work/$name" "$url" || die "download failed: $url"
curl -fsSL -o "$work/$name.sha256" "$url.sha256" || die "download failed: $url.sha256"
read -r want listed _ < "$work/$name.sha256" || true
listed=${listed#\*}  # sha256sum -b marks binary mode with '*'
[ -n "${want:-}" ] || die "sha256: empty checksum file $url.sha256"
if [ -n "${listed:-}" ] && [ "$listed" != "$name" ]; then die "sha256: checksum file names $listed, not $name"; fi
have=$(hash_of "$work/$name")
if [ "$have" != "$want" ]; then die "sha256 mismatch for $name: expected $want, got $have; nothing installed"; fi

mkdir -p "$work/x"
tar -xzf "$work/$name" -C "$work/x" fleetmux || die "$name has no top-level fleetmux"
[ -f "$work/x/fleetmux" ] || die "$name has no top-level fleetmux"
chmod 755 "$work/x/fleetmux"

mkdir -p "$dest"
staged=$dest/.fleetmux.new.$$
cp "$work/x/fleetmux" "$staged"
mv -f "$staged" "$dest/fleetmux"  # same directory: an atomic replace, even of a running binary
staged=
echo "installed fleetmux $version ($triple) to $dest/fleetmux"

# ---------------------------------------------------------------- fleetmux-web (desktop sessions only)
desktop() {
  [ "$(uname -s)" = Darwin ] && return 0
  [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]
}
if ! desktop; then
  echo "fleetmux-web not installed: no desktop session (DISPLAY / WAYLAND_DISPLAY unset); run this again from a desktop session to add the window"
  exit 0
fi

case $triple in
  *-unknown-linux-musl) web_triple=${triple%-musl}-gnu ;;  # the window links webkit2gtk dynamically: gnu, not musl
  *) web_triple=$triple ;;
esac
web=fleetmux-web-$version-$web_triple.tar.gz
web_url=$BASE/v$version/$web
if ! curl -fsSL -o "$work/$web" "$web_url" || ! curl -fsSL -o "$work/$web.sha256" "$web_url.sha256"; then
  echo "install_fleetmux: warning: fleetmux-web not installed: download failed: $web_url (fleetmux $version is installed)" >&2
  exit 0
fi
want=; listed=
read -r want listed _ < "$work/$web.sha256" || true
listed=${listed#\*}
[ -n "${want:-}" ] || die "sha256: empty checksum file $web_url.sha256; fleetmux-web not installed (fleetmux $version is installed)"
if [ -n "${listed:-}" ] && [ "$listed" != "$web" ]; then
  die "sha256: checksum file names $listed, not $web; fleetmux-web not installed (fleetmux $version is installed)"
fi
have=$(hash_of "$work/$web")
if [ "$have" != "$want" ]; then
  die "sha256 mismatch for $web: expected $want, got $have; fleetmux-web not installed (fleetmux $version is installed)"
fi

mkdir -p "$work/w"
tar -xzf "$work/$web" -C "$work/w" fleetmux-web || die "$web has no top-level fleetmux-web; fleetmux-web not installed"
[ -f "$work/w/fleetmux-web" ] || die "$web has no top-level fleetmux-web; fleetmux-web not installed"
chmod 755 "$work/w/fleetmux-web"
staged=$dest/.fleetmux-web.new.$$
cp "$work/w/fleetmux-web" "$staged"
mv -f "$staged" "$dest/fleetmux-web"
staged=
echo "installed fleetmux-web $version ($web_triple) to $dest/fleetmux-web"
