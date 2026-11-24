#!/bin/bash
# Record `fleetmux demo` for the README (docs/images/README-demo-recording.md).
#
# Usage: scripts/record_demo.sh [OUT_DIR]          (default: docs/images)
# Env:   PYTHON    interpreter with requirements.txt and requirements-dev.txt (pyte) (default: .venv/bin/python,
#                  else python3.14)
#        FLEETMUX  the fleetmux binary (default: $FLEETMUX_BIN, else `fleetmux` on PATH)
#        COLS/ROWS size of the recorded terminal (default 120x34)
#
# A private `fleetmux server --record` in a new /tmp/fmxd-rec-XXXXXX directory runs one pane: this checkout's
# `fleetmux demo` in the attach view (SSH_CONNECTION is set in that pane only, so no window opens). The pane's
# PTY output is the recording: the demo's own lines, the attached fleet view, the summary. The script then
#   1. redacts it with scripts/redact_recording.py (e-mail, home paths, tokens, plus --literal user and host names),
#   2. converts it to asciicast v2 with scripts/rec_to_cast.py,
#   3. renders the .cast as one looping animated SVG with scripts/cast_to_svg.py (the image the README shows),
#   4. fails if the user name, host name or home directory is still in any of the three files.
# Output: OUT_DIR/fleetmux-demo.rec, .cast and .svg. For a GIF, install agg and make it from the same .cast.
# Clean-up (also on Ctrl-C): the recording server is shut down through its own control socket, the directory removed.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
OUT=$(mkdir -p "${1:-$REPO/docs/images}" && cd "${1:-$REPO/docs/images}" && pwd)
PY=${PYTHON:-}
if [ -z "$PY" ]; then
  if [ -x "$REPO/.venv/bin/python" ]; then PY=$REPO/.venv/bin/python; else PY=$(command -v python3.14); fi
fi
FM=${FLEETMUX:-${FLEETMUX_BIN:-$(command -v fleetmux || true)}}
[ -n "$FM" ] && [ -x "$FM" ] || { echo "record_demo: no fleetmux binary (set FLEETMUX)" >&2; exit 2; }
COLS=${COLS:-120}
ROWS=${ROWS:-34}
W=$(mktemp -d /tmp/fmxd-rec-XXXXXX)
chmod 700 "$W"
mkdir -m 700 "$W/rec" "$W/rt"

cleanup() {
  "$PY" - "$W/rt/ctl.sock" <<'EOF' >/dev/null 2>&1 || true
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(30); s.connect(sys.argv[1])
s.sendall(b'{"id":1,"cmd":"server.shutdown","force":true}\n'); s.recv(4096)
EOF
  rm -rf "$W"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# the recording server sees none of the caller's fleet settings; the interface is English whatever the private
# config and the system language say (ui.language, docs/design.md §63)
env -u FLEETMUX_HOME -u FLEETMUX_STATE -u FLEETMUX_CONFIG -u TMUX -u TMUX_PANE FLEETMUX_UI_LANGUAGE=en \
  "$FM" server --runtime "$W/rt" --session demo-rec --daemonize --default-size "${COLS}x${ROWS}" --record "$W/rec"

"$PY" - "$W/rt/ctl.sock" "$PY" "$REPO/fleetmux/fleet.py" "$FM" "$W" <<'EOF'
import json, socket, sys, time
sock, py, fleet_py, fm, work = sys.argv[1:]
s = socket.socket(socket.AF_UNIX); s.settimeout(30); s.connect(sock)
f = s.makefile('rwb')
n = [0]
def call(**req):
    n[0] += 1
    f.write((json.dumps(dict(req, id=n[0])) + '\n').encode()); f.flush()
    return json.loads(f.readline())
assert call(cmd='hello')['ok']
env = {'SSH_CONNECTION': 'recording 0 local 0', 'FLEETMUX_BIN': fm, 'FLEETMUX_DISCOVERY': 'off', 'FLEETMUX_UI_LANGUAGE': 'en'}
pane = call(cmd='pane.spawn', argv=[py, fleet_py, 'demo'], cwd=work, env=env, title='fleetmux demo', type='supervisor')
assert pane['ok'], pane
assert call(cmd='layout.set', zoom=pane['pane'])['ok']  # the one pane fills the screen
info = call(cmd='pane.get', pane=pane['pane'])
print('recording pane %s (%sx%s)' % (pane['pane'], info.get('cols'), info.get('rows')), flush=True)
deadline = time.time() + 180
while time.time() < deadline:
    if not call(cmd='pane.get', pane=pane['pane'])['ok']:
        break  # the demo exited: its pane is gone
    time.sleep(0.5)
else:
    raise SystemExit('record_demo: the demo did not finish within 180 s')
open(work + '/pane', 'w').write(pane['pane'] + ' ' + str(info.get('cols')) + ' ' + str(info.get('rows')) + '\n')
EOF

read -r PANE PCOLS PROWS < "$W/pane"
REC="$W/rec/$PANE.rec"
[ -s "$REC" ] || { echo "record_demo: no recording at $REC" >&2; exit 1; }
LIT=(--literal "$(id -un)" --literal "$(hostname -s)")
[ "$(hostname)" != "$(hostname -s)" ] && LIT+=(--literal "$(hostname)")
"$PY" "$REPO/scripts/redact_recording.py" "${LIT[@]}" "$REC" "$OUT/fleetmux-demo.rec"
"$PY" "$REPO/scripts/rec_to_cast.py" --cols "$PCOLS" --rows "$PROWS" --idle-limit 2 --title "fleetmux demo" \
  "$OUT/fleetmux-demo.rec" "$OUT/fleetmux-demo.cast"
"$PY" "$REPO/scripts/cast_to_svg.py" "$OUT/fleetmux-demo.cast" "$OUT/fleetmux-demo.svg"
for needle in "$(id -un)" "$(hostname -s)" "$HOME"; do
  if LC_ALL=C grep -aqF -- "$needle" "$OUT/fleetmux-demo.rec" "$OUT/fleetmux-demo.cast" "$OUT/fleetmux-demo.svg"; then
    echo "record_demo: '$needle' is still in the recording; not kept" >&2
    rm -f "$OUT/fleetmux-demo.rec" "$OUT/fleetmux-demo.cast" "$OUT/fleetmux-demo.svg"
    exit 1
  fi
done
echo "recorded: $OUT/fleetmux-demo.rec $OUT/fleetmux-demo.cast $OUT/fleetmux-demo.svg (${PCOLS}x${PROWS})"
