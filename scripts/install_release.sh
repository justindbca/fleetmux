#!/bin/bash
# Install one fleetmux release on the target machine (docs/design.md §50).
#   scripts/install_release.sh <sha> <tarball from `git archive <sha>`>
#       [--bin-dir DIR [--headless]] [--tool-sha SHA] [--test] [--activate]
#
# Steps: pick the interpreter -> verify the --bin-dir binaries -> create $RELEASES/<sha> with a bare mkdir (never
# -p: an existing release is never joined or overwritten) -> .venv with the pinned packages -> the verified GUI
# binaries into <release>/bin (--bin-dir) -> deps.yaml check (fail = stop; <release>/bin is first; programs are found
# on the login shell's PATH like fleet discovery, then in the directories ~/fleetmux-current's installed.json recorded;
# the log and rows are kept in $RELEASES/.logs; nothing is installed or upgraded) ->
# optional full test suite (fail = stop; its log is kept OUTSIDE the release dir) -> private Redis runtime dirs
# owner-only (700; start refuses anything else) -> optional: point ~/fleetmux-current at it, with the read-only
# switch baseline check before and after (a failure puts current back). A failed install
# removes only the release dir this run created (CREATED is set by the successful mkdir), so a retry starts clean
# and an existing release/current is never touched.
#
# <sha> must be 7-40 hex characters (upper case accepted, normalised to lower): it is the release dir name, so
# anything else could make the failure cleanup remove a directory outside $RELEASES.
#
# On success the installer prints one explicit start command with <release>/bin first on PATH (a plain `fleetmux`
# in a normal shell resolves to ~/.local/bin, i.e. possibly another build) and records it as `start_command` in
# <release>/bin/installed.json. That file, and the build manifest it verifies, are real JSON: parsed with the
# release interpreter and re-parsed after writing, so quotes/backslashes in flags and paths stay valid JSON.
#
# --test          run the release's own full suite; the complete log is written to $RELEASES/.logs/<sha>-<ts>.log
#                 (outside the release dir, so a failed install still leaves the whole log) and its path is printed.
#
# --bin-dir DIR   an already-built binaries dir of ONE source SHA: DIR/fleetmux, DIR/fleetmux-web,
#                 DIR/SHA256SUMS and DIR/manifest.json. Both binaries must match their own SHA256SUMS line and
#                 the manifest's source_sha must equal <sha> or start with it (short <sha>); the two files then land in <release>/bin next to
#                 SHA256SUMS, manifest.json and installed.json. A missing file or any mismatch stops the install
#                 before the release dir is created.
# --headless      with --bin-dir: the build is for a box without a desktop (a server, a container), so DIR holds
#                 only `fleetmux` (+ SHA256SUMS, manifest.json); fleetmux-web is neither required nor installed
#                 (deps.yaml marks it desktop-only) and installed.json records it as null.
# Releases root: $FLEETMUX_RELEASES, else ~/fleetmux-releases. --activate points ~/fleetmux-current at the release.
# Version index (with --bin-dir): installed.json records the release tree's version (rust/Cargo.toml
#                 [workspace.package] version, null when absent) and, once the release is ready, $RELEASES/versions/<v>
#                 is pointed at ../<sha> (replaced atomically; a later install of the same version takes it over). This
#                 is what `fleetmux upgrade --to version:<v>` and rollout's {version} commands resolve. A failed install
#                 never moves it.
#
# --tool-sha SHA  the installer-tool commit doing this install (env FLEETMUX_TOOL_SHA); defaults to the git
#                 HEAD of the checkout this script lives in. This is a DIFFERENT identity from <sha>: <sha> is
#                 the application release (source archive + binaries), the tool commit only changes how it is
#                 installed. installed.json records both, plus the installer script's own sha256.
#
# Python: FLEETMUX_PYTHON wins (it must satisfy deps.yaml python.min). Otherwise macOS tries
# /opt/homebrew/bin/python3.14 and /usr/local/bin/python3.14, then every host tries `python3.14` on PATH and
# $HOME/.local/bin/python3.14 (UTM: 3.14.10), then uv-managed $HOME/.local/share/uv/python/cpython-3.14*/bin/python3.14.
# The first candidate that satisfies python.min AND builds a venv with pip (tried in a temp dir) wins; the choice and
# every skipped candidate are printed with the reason. There is no fallback to an older python3: a host with only
# 3.12 stops with a message, because the release venv must satisfy deps.yaml python.min.
#
# Example (Linux/UTM, the 356 application release installed by a newer tool commit):
#   scripts/install_release.sh 35670632e0b923e56dc4fa357189711dbd39328a src-35670632.tgz \
#     --bin-dir ~/a42/builds/final-35670632/utm-linux --tool-sha <tool commit>
# Example (macOS): the same, with --bin-dir .../builds/final-35670632/macos.
set -euo pipefail
FLEETMUX_PYTHON="${FLEETMUX_PYTHON:-}"
FLEETMUX_RELEASES="${FLEETMUX_RELEASES:-}"
FLEETMUX_TOOL_SHA="${FLEETMUX_TOOL_SHA:-}"
SHA="${1:?sha}"; TARBALL="${2:?tarball}"; shift 2
TEST=0; ACTIVATE=0; BIN_DIR=""; TOOL_SHA="$FLEETMUX_TOOL_SHA"; BINS="fleetmux fleetmux-web"
while [ $# -gt 0 ]; do
  case "$1" in
    --test) TEST=1; shift ;;
    --headless) BINS="fleetmux"; shift ;;
    --activate) ACTIVATE=1; shift ;;
    --bin-dir) BIN_DIR="${2:?--bin-dir needs a directory}"; shift 2 ;;
    --tool-sha) TOOL_SHA="${2:?--tool-sha needs a sha}"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
RELEASES="${FLEETMUX_RELEASES:-$HOME/fleetmux-releases}"
case "$RELEASES" in /*) ;; *) RELEASES="$(pwd)/$RELEASES" ;; esac
# <sha> names the release dir under $RELEASES. Only hex is accepted (7-40 chars: older callers pass a short SHA),
# so a value like ../canary can never make the failure cleanup rm -rf anything outside $RELEASES.
case "$SHA" in
  *[!0-9a-fA-F]*) echo "invalid <sha> '$SHA': expected 7-40 hex characters (it is the release dir name)" >&2; exit 2 ;;
esac
if [ "${#SHA}" -lt 7 ] || [ "${#SHA}" -gt 40 ]; then
  echo "invalid <sha> '$SHA': expected 7-40 hex characters (it is the release dir name)" >&2; exit 2
fi
SHA="$(printf '%s' "$SHA" | tr 'A-F' 'a-f')"
REL="$RELEASES/$SHA"
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ "$(uname -s)" = Darwin ]; then
  PATH="/opt/homebrew/opt/redis/bin:/Applications/cmux.app/Contents/Resources/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH"
else
  PATH="$HOME/.local/bin:$PATH"
fi
export PATH

abspath() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s\n' "$(cd "$(dirname "$1")" && pwd)/$(basename "$1")" ;;
  esac
}
TARBALL="$(abspath "$TARBALL")"
if [ -n "$BIN_DIR" ]; then BIN_DIR="$(abspath "$BIN_DIR")"; fi
if [ -z "$TOOL_SHA" ]; then
  TOOL_SHA="$(git -C "$HERE/.." rev-parse HEAD 2>/dev/null || true)"
fi

# deps.yaml `python: min: "3.14"` (first block, so a later `min:` for a program never wins).
MIN_PY="$(awk '/^python:[[:space:]]*$/{p=1; next} /^[a-zA-Z_]+:/{p=0} p && $1=="min:"{gsub(/["\047]/,"",$2); print $2; exit}' \
  "$HERE/../deps.yaml" 2>/dev/null || true)"
MIN_PY="${MIN_PY:-3.14}"

python_ok() {
  "$1" -c 'import sys
need = tuple(int(x) for x in sys.argv[1].split(".")[:2])
raise SystemExit(0 if sys.version_info[:2] >= need else 1)' "$MIN_PY" >/dev/null 2>&1
}

# A candidate must also build a venv with pip: a Homebrew python3.14 whose ensurepip fails passes the version check
# and then breaks `-m venv` inside the release dir. Tried in a temp dir that is always removed.
venv_ok() {
  local tmp rc=0
  tmp="$(mktemp -d)" || return 1
  if ! "$1" -m venv "$tmp/v" >"$tmp/out" 2>&1 || ! "$tmp/v/bin/pip" --version >>"$tmp/out" 2>&1; then
    VENV_WHY="$(tail -1 "$tmp/out" 2>/dev/null)"; rc=1
  fi
  rm -rf "$tmp"
  return $rc
}

# Prints the chosen interpreter on stdout; why each other candidate was skipped goes to stderr.
pick_python() {
  local c tried=" "
  if [ -n "$FLEETMUX_PYTHON" ]; then
    [ -x "$FLEETMUX_PYTHON" ] || { echo "FLEETMUX_PYTHON is not executable: $FLEETMUX_PYTHON" >&2; return 1; }
    if ! python_ok "$FLEETMUX_PYTHON"; then
      echo "FLEETMUX_PYTHON is not Python >= $MIN_PY (deps.yaml python.min): $FLEETMUX_PYTHON ($("$FLEETMUX_PYTHON" -V 2>&1))" >&2
      return 1
    fi
    echo "python: $FLEETMUX_PYTHON (FLEETMUX_PYTHON)" >&2
    printf '%s\n' "$FLEETMUX_PYTHON"; return 0
  fi
  local cands=()
  if [ "$(uname -s)" = Darwin ]; then cands+=(/opt/homebrew/bin/python3.14 /usr/local/bin/python3.14); fi
  cands+=("$(command -v python3.14 2>/dev/null || true)" "$HOME/.local/bin/python3.14")
  for c in "$HOME"/.local/share/uv/python/cpython-3.14*/bin/python3.14; do cands+=("$c"); done  # uv-managed
  for c in "${cands[@]}"; do
    if [ -z "$c" ] || [ ! -x "$c" ]; then continue; fi
    case "$tried" in *" $c "*) continue ;; esac
    tried="$tried$c "
    if ! python_ok "$c"; then echo "python: skip $c (not Python >= $MIN_PY)" >&2; continue; fi
    if ! venv_ok "$c"; then echo "python: skip $c (cannot create a venv with pip: ${VENV_WHY:-no output})" >&2; continue; fi
    echo "python: $c (Python >= $MIN_PY, venv with pip works)" >&2
    printf '%s\n' "$c"; return 0
  done
  echo "no Python >= $MIN_PY on this host (deps.yaml python.min); set FLEETMUX_PYTHON=/path/to/python3.14" >&2
  echo "  (an older python3 such as 3.12 is not used: the release venv must satisfy deps.yaml)" >&2
  return 1
}

hash_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else echo "need sha256sum or shasum to verify the binaries" >&2; return 1
  fi
}

sums_hash() {
  awk -v n="$1" '{ name=$2; sub(/^\*/, "", name); if (name == n) { print $1; exit } }' "$2"
}

# manifest.json is JSON, so it is parsed as JSON (a valid pretty-printed manifest used to be refused by the old
# sed regex). Prints the lower-cased source_sha, or nothing when absent; a parse error is reported and exits 3.
manifest_source_sha() {
  "$PY" - "$1" <<'PY'
import json, sys
path = sys.argv[1]
try:
    with open(path, encoding='utf-8') as fh:
        data = json.load(fh)
except OSError as exc:
    print('cannot read manifest.json (%s): %s' % (exc, path), file=sys.stderr)
    raise SystemExit(3)
except ValueError as exc:
    print('manifest.json is not valid JSON (%s): %s' % (exc, path), file=sys.stderr)
    raise SystemExit(3)
src = data.get('source_sha') if isinstance(data, dict) else None
print(str(src or '').strip().lower())
PY
}

# One explicit console command for this release: <release>/bin first on PATH (the caller's own PATH is kept, not
# replaced) plus the release's own Python fleet CLI (the venv console script `fleetmux-py` when the install made
# it, else `python -m`). Every path is shell-quoted by shlex.quote, so the printed line is executable as-is even when the
# release directory contains spaces or quotes; a raw "%s/bin:$PATH %s" concatenation would split there. Printed
# on success and recorded in installed.json, so the operator never silently runs the stale ~/.local/bin build.
start_command() {
  local mode=module
  if [ -x "$REL/.venv/bin/fleetmux-py" ]; then mode=script; fi
  "$PY" - "$REL" "$mode" <<'PY'
import shlex, sys
rel, mode = sys.argv[1], sys.argv[2]
if mode == 'script':
    entry = shlex.quote(rel + '/.venv/bin/fleetmux-py')
else:
    entry = shlex.quote(rel + '/.venv/bin/python') + ' -m fleetmux.fleet'
print('PATH=' + shlex.quote(rel + '/bin') + ':$PATH ' + entry + ' --help')
PY
}

# The release tree's version: rust/Cargo.toml [workspace.package] version (the key rollout tags from); empty when the
# tree has none or it is not a plain version name (it becomes a path component of the index).
release_version() {
  [ -f rust/Cargo.toml ] || return 0
  "$PY" - rust/Cargo.toml <<'PY'
import re, sys, tomllib
try:
    with open(sys.argv[1], 'rb') as fh:
        v = tomllib.load(fh).get('workspace', {}).get('package', {}).get('version')
except (OSError, ValueError):
    v = None
if isinstance(v, str) and re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z._+-]*', v) and v not in ('.', '..'):
    print(v)
PY
}

# $RELEASES/versions/<v> -> ../<sha>, replaced atomically (a temp link renamed over it: no `mv -T` on macOS).
write_version_index() {
  mkdir -p "$RELEASES/versions"
  "$PY" - "$RELEASES/versions" "$VERSION" "$SHA" <<'PY'
import os, sys
d, version, sha = sys.argv[1:4]
tmp = os.path.join(d, '.%s.new.%d' % (version, os.getpid()))
os.symlink('../' + sha, tmp)
os.replace(tmp, os.path.join(d, version))
PY
}

# installed.json is written by json.dump (every value escaped) and then re-parsed: a --tool-sha with a quote used to
# produce invalid JSON while the install still exited 0.
write_installed_json() {
  local fmux_sha web_sha
  local web_bytes=
  fmux_sha="$(hash_of bin/fleetmux)"; web_sha=
  if [ -f bin/fleetmux-web ]; then web_sha="$(hash_of bin/fleetmux-web)"; web_bytes="$(wc -c < bin/fleetmux-web | tr -d ' ')"; fi
  "$PY" - bin/installed.json "$SHA" "$(manifest_source_sha bin/manifest.json)" "$TOOL_SHA" \
    "$(hash_of "$HERE/install_release.sh")" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PY" \
    "$(hash_of "$TARBALL")" "$(hash_of bin/manifest.json)" "$(hash_of bin/SHA256SUMS)" \
    "$fmux_sha" "$(wc -c < bin/fleetmux | tr -d ' ')" \
    "$web_sha" "$web_bytes" "$(start_command)" "$DEPS_JSON" "$VERSION" <<'PY'
import json, sys
(out, release_sha, source_sha, tool_sha, installer_sha, installed_at, python, tarball_sha, manifest_sha,
 sums_sha, fmux_sha, fmux_bytes, web_sha, web_bytes, start_command, deps_json, version) = sys.argv[1:18]
try:  # the dependency rows with the absolute path of each program (absent from an older release's check_deps.py)
    with open(deps_json, encoding='utf-8') as fh:
        dependencies = json.load(fh)
except (OSError, ValueError):
    dependencies = None
doc = {
    'release_sha': release_sha,
    'source_sha': source_sha,
    'version': version or None,  # the release tree's own version; the key of $RELEASES/versions/<v>
    'tool_sha': tool_sha or None,
    'installer': 'scripts/install_release.sh',
    'installer_sha256': installer_sha,
    'installed_at': installed_at,
    'python': python,
    'source_tarball_sha256': tarball_sha,
    'manifest_sha256': manifest_sha,
    'sha256sums_sha256': sums_sha,
    'binaries': {
        'fleetmux': {'sha256': fmux_sha, 'bytes': int(fmux_bytes)},
        'fleetmux-web': {'sha256': web_sha, 'bytes': int(web_bytes)} if web_sha else None,  # None: --headless
    },
    'start_command': start_command,
    'dependencies': dependencies,
    'note': 'installed release is not a native PASS and not a receipt',
}
with open(out, 'w', encoding='utf-8') as fh:
    json.dump(doc, fh, indent=2)
    fh.write('\n')
PY
  "$PY" -c 'import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    json.load(fh)' bin/installed.json \
    || { echo "installed.json does not parse as JSON; not releasing: $REL/bin/installed.json" >&2; exit 1; }
}

verify_binaries() {
  local dir=$1 want=$2 f have src
  [ -d "$dir" ] || { echo "--bin-dir is not a directory: $dir" >&2; return 1; }
  [ -f "$dir/SHA256SUMS" ] || { echo "--bin-dir has no SHA256SUMS: $dir/SHA256SUMS" >&2; return 1; }
  [ -f "$dir/manifest.json" ] || { echo "--bin-dir has no manifest.json: $dir/manifest.json" >&2; return 1; }
  src="$(manifest_source_sha "$dir/manifest.json")" || return 1
  [ -n "$src" ] || { echo "manifest.json has no source_sha: $dir/manifest.json" >&2; return 1; }
  # A short <sha> (the usual release dir name: a 40-char dir pushes macOS private Redis sockets past 103 bytes)
  # binds when it prefixes the manifest's full source_sha.
  case "$src" in
    "$want"*) ;;
    *) echo "manifest source_sha $src does not match the release <sha> $want; not installing" >&2; return 1 ;;
  esac
  for f in $BINS; do
    [ -f "$dir/$f" ] || { echo "--bin-dir has no $f: $dir/$f" >&2; return 1; }
    have="$(sums_hash "$f" "$dir/SHA256SUMS")"
    [ -n "$have" ] || { echo "SHA256SUMS has no line for $f: $dir/SHA256SUMS" >&2; return 1; }
    [ "$(hash_of "$dir/$f")" = "$have" ] || {
      echo "sha256 mismatch for $f: SHA256SUMS says $have, got $(hash_of "$dir/$f"); nothing installed" >&2; return 1; }
  done
}

if [ -e "$REL" ]; then echo "release exists, not touched: $REL" >&2; exit 1; fi
# The interpreter comes first: the build manifest is parsed as JSON with it (below, and again for installed.json).
PY="$(pick_python)" || exit 1
if [ -n "$BIN_DIR" ]; then verify_binaries "$BIN_DIR" "$SHA" || exit 1; fi

CREATED=0; READY=0
cleanup() {
  local rc=$?
  trap - EXIT
  if [ "$CREATED" = 1 ] && [ "$READY" != 1 ]; then rm -rf "$REL"; fi
  exit $rc
}
trap cleanup EXIT
# A bare mkdir, not `mkdir -p`: EEXIST means another run owns that release and this run must not touch it (the old
# `[ -e ]` + `mkdir -p` pair let a second install join and then rm -rf the first run's dir). CREATED is only set by
# the run whose mkdir succeeded, so cleanup can only ever remove its own directory.
# The container root may be nested (FLEETMUX_RELEASES=~/a42/releases/x), so `-p` is allowed here: only the
# release leaf below must be created atomically. `-p` on the root cannot remove or join anything.
mkdir -p "$RELEASES"
if ! mkdir "$REL" 2>/dev/null; then echo "release exists, not touched: $REL" >&2; exit 1; fi
CREATED=1
cd "$REL"
# Extracted from inside the release dir, not with `-C "$REL"`: GNU tar unquotes a -C directory name (`\b` becomes a
# backspace), so a release path with a backslash named another directory there. $TARBALL is absolute (above).
tar xzf "$TARBALL"
"$PY" -m venv .venv
.venv/bin/pip install -q -r requirements-dev.txt
.venv/bin/pip install -q --no-deps -e .
if [ -n "$BIN_DIR" ]; then
  mkdir -p bin
  for f in $BINS; do
    cp "$BIN_DIR/$f" "bin/.$f.new.$$"
    chmod 755 "bin/.$f.new.$$"
    mv -f "bin/.$f.new.$$" "bin/$f"  # same directory: an atomic replace, even of a running binary
  done
  cp "$BIN_DIR/SHA256SUMS" bin/SHA256SUMS
  cp "$BIN_DIR/manifest.json" bin/manifest.json
  PATH="$REL/bin:$PATH"
  export PATH
  echo "binaries installed: $REL/bin/{${BINS// /,}} (sha256 verified against $BIN_DIR/SHA256SUMS, source $SHA)"
fi
# The check finds programs the way fleet discovery does (login shell PATH, process PATH, then the directories earlier
# installs and discovery recorded; design §50.3) and names the absolute path of each; its output and rows are kept
# OUTSIDE the release dir, and installed.json records the rows (with each program's dir and pi's node dir, so the next
# install finds them without the caller's PATH). Nothing is installed or upgraded: a FAIL line is for the operator to
# fix, and names where the program was last found when there is a record.
mkdir -p "$RELEASES/.logs"
DEPS_LOG="$RELEASES/.logs/$SHA-$(date -u +%Y%m%dT%H%M%SZ)-$$-deps.log"
DEPS_JSON="${DEPS_LOG%.log}.json"
FLEETMUX_DEPS_JSON="$DEPS_JSON" .venv/bin/python scripts/check_deps.py 2>&1 | tee "$DEPS_LOG" \
  || { echo "deps.yaml not satisfied on this machine: not releasing (install or upgrade the FAIL items yourself;" \
            "fleetmux changes nothing on the machine); log: $DEPS_LOG" >&2; exit 1; }
echo "dependency check log: $DEPS_LOG"
VERSION=
if [ -n "$BIN_DIR" ]; then VERSION="$(release_version)"; write_installed_json; fi
if [ "$TEST" = 1 ]; then
  # The log lives outside the release dir: a failed install removes $REL, and the whole point of the log is to show
  # why the failing Linux run failed on a machine the operator cannot attach a debugger to.
  mkdir -p "$RELEASES/.logs"
  TEST_LOG="$RELEASES/.logs/$SHA-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
  .venv/bin/python -m pytest -q -p no:cacheprovider tests > "$TEST_LOG" 2>&1 \
    || { echo "full suite failed: not releasing" >&2; echo "complete test log kept at: $TEST_LOG" >&2;
         tail -5 "$TEST_LOG" >&2; exit 1; }
  echo "complete test log: $TEST_LOG"
  tail -1 "$TEST_LOG"
fi
# The suite (and any earlier run) may have created these with default permissions; start refuses them.
mkdir -p .runtime/redis && chmod 700 .runtime .runtime/redis
if [ "$ACTIVATE" = 1 ]; then
  # The switch baseline (docs/design.md, "Switch baseline") lives in fleet-superpowers.yaml, never in a release: the
  # read-only `superpowers baseline check` (this release's code) runs before and after current moves. A changed block
  # hash, a new deviation in a running fleet or a workflow no longer loaded fails the install and puts current back.
  BASELINE_BEFORE="$RELEASES/.logs/$SHA-$(date -u +%Y%m%dT%H%M%SZ)-$$-baseline-before.json"
  .venv/bin/python -m fleetmux.fleet superpowers baseline check --json > "$BASELINE_BEFORE" \
    || { echo "baseline check failed before the switch: current not moved; report: $BASELINE_BEFORE" >&2; exit 1; }
  BASELINE_HASH="$("$PY" - "$BASELINE_BEFORE" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as fh:
    print(json.load(fh)['hash'])
PY
)" || { echo "baseline check before the switch printed no report: current not moved; $BASELINE_BEFORE" >&2; exit 1; }
  echo "baseline before the switch: hash $BASELINE_HASH (report: $BASELINE_BEFORE)"
  OLD="$(readlink "$HOME/fleetmux-current" 2>/dev/null || echo none)"
  ln -sfn "$REL" "$HOME/fleetmux-current"
  echo "current: $OLD -> $(readlink "$HOME/fleetmux-current")"
  if ! .venv/bin/python -m fleetmux.fleet superpowers baseline check --before "$BASELINE_HASH" \
       --before-report "$BASELINE_BEFORE"; then
    if [ "$OLD" = none ]; then rm -f "$HOME/fleetmux-current"; else ln -sfn "$OLD" "$HOME/fleetmux-current"; fi
    echo "baseline check failed after the switch (block hash changed, a new deviation, or a workflow no longer" \
         "loaded): current put back to $OLD; not releasing" >&2
    exit 1
  fi
fi
READY=1
echo "release ready: $REL"
if [ -n "$BIN_DIR" ] && [ -n "$VERSION" ]; then
  # Only now: a failed install (suite, baseline check) must never take the index from the release that has it.
  write_version_index \
    || { echo "release installed, but the version index could not be written: $RELEASES/versions/$VERSION" >&2; exit 1; }
  echo "version index: $RELEASES/versions/$VERSION -> ../$SHA"
elif [ -n "$BIN_DIR" ]; then
  echo "no version index: the release tree has no rust/Cargo.toml [workspace.package] version"
fi
echo "start (release bin first on PATH): $(start_command)"
if [ "$ACTIVATE" = 1 ]; then
  # Design §56 "Release drift": the running fleets still on an older release, and when their daemons switch them.
  # After READY=1 and never fatal: the release is installed and active whatever this prints.
  .venv/bin/python -m fleetmux.release_drift activated \
    || echo "could not list the running fleets on an older release (the release is installed and active)" >&2
fi
