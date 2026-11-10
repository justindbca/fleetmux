#!/usr/bin/env bash
# End-to-end smoke of §35 (lost bridge link -> the slot's session restarts).
#
#   PI_MODEL=provider/model [SUPERVISOR_PI_MODEL=provider/model] scripts/smoke_link_loss.sh
#
# Run inside a cmux terminal with pi logged in. pi supervisor + one pi worker (task mode). While the worker
# is mid-turn reading files, the smoke kills that worker's bridge process (agent_bridge.py), then asserts:
#   1. the slot is marked link_lost and held without pausing the fleet
#   2. the watchdog restarts it: generation 0 -> 1, label worker-1.1, link_lost back to 0, link.restarted audited
#   3. the cancelled work goes to the supervisor (a FROZEN ... bridge link lost message), never re-run by itself
#   4. the task still completes (the supervisor re-dispatches) within SMOKE_TIMEOUT
# Temp dirs are kept for inspection; only processes are stopped.
set -u
export DISABLE_AUTOUPDATER=1
: "${PI_MODEL:?PI_MODEL=provider/model is required}"
N_FILES=${N_FILES:-12}
LINES_PER_FILE=${LINES_PER_FILE:-400}
SMOKE_TIMEOUT=${SMOKE_TIMEOUT:-1500}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3}
FLEET="$ROOT/cmux_fleet/fleet.py"

TMP=$(mktemp -d /tmp/cfsmoke-ll.XXXXXX)   # short path: Unix socket limit is 103 bytes
unset FLEETMUX_HOME FLEETMUX_STATE FLEETMUX_CONFIG  # §50: the new names would win over the isolated ones
export CMUX_FLEET_HOME="$TMP/home" CMUX_FLEET_STATE="$TMP/state"
mkdir -p "$CMUX_FLEET_HOME" "$CMUX_FLEET_STATE" "$TMP/input"
CFG="$CMUX_FLEET_HOME/latest.json"
stop_all() {
  set +e
  [ -f "$CFG" ] && "$PY" "$FLEET" stop --config "$CFG" >/dev/null 2>&1
  [ -f "$CFG" ] && "$PY" - "$CFG" "$ROOT" <<'EOF' >/dev/null 2>&1
import sys
sys.path.insert(0, sys.argv[2])
from cmux_fleet import fleet
fleet.manager_for(fleet.load(sys.argv[1])).close_all()
EOF
  sleep 2; pids=$(pgrep -f "$TMP" || true); [ -n "$pids" ] && kill $pids 2>/dev/null
  echo "kept (not deleted): $TMP"
}
trap stop_all EXIT
trap 'echo INTERRUPTED >&2; exit 130' INT TERM

for i in $(seq 1 "$N_FILES" | xargs printf '%04d '); do
  code=$(LC_ALL=C tr -dc 'A-Z0-9' </dev/urandom | head -c 8)
  { printf 'FILE %s BEGIN\n' "$i"
    yes "link-loss smoke filler line for file $i" | head -n "$LINES_PER_FILE"
    printf 'SECRET CODE FOR FILE %s: %s\n' "$i" "$code"
    printf 'FILE %s END\n' "$i"; } > "$TMP/input/file_$i.txt"
done
# Big windows: no mid-turn handoff may interfere with this smoke.
printf 'workers:\n  - name: worker-1\nhandoff:\n  pi: {window: 2000000, midturn_ratio: 0.95}\n' > "$TMP/roster.yaml"
args=(start --lifecycle smoke --supervisor-cli pi --worker-cli pi --allow-development-v2 --allow-unsandboxed-pi
      --redis-runtime-dir "$TMP/r" --name smoke-link-loss --roster "$TMP/roster.yaml" --pi-model "$PI_MODEL")
[ -n "${SUPERVISOR_PI_MODEL:-}" ] && args+=(--supervisor-pi-model "$SUPERVISOR_PI_MODEL")
echo "starting in $TMP"
(cd "$TMP" && "$PY" "$FLEET" "${args[@]}") || { echo "RESULT FAIL start"; exit 1; }

"$PY" - "$CFG" "$ROOT" "$TMP" "$N_FILES" "$SMOKE_TIMEOUT" <<'PYEOF'
import json, subprocess, sys, time
import redis
from pathlib import Path
cfg_path, root, tmp, n, timeout = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), float(sys.argv[5])
sys.path.insert(0, root)
from cmux_fleet import fleet, runtime_v2
cfg = fleet.load(cfg_path); r = fleet.connect(cfg); bus = runtime_v2.bus_for(cfg, r)
run = Path(cfg['run']); deadline = time.time() + timeout; ok = {}
agent = lambda: r.hgetall(fleet.key('agent', 'worker-1'))
def wait(pred, what, step=3):
    while time.time() < deadline:
        if pred():
            print('ok ' + what, flush=True); return True
        time.sleep(step)
    print('FAIL timeout waiting for ' + what, flush=True); return False
def audits(event):
    return [f for _, f in r.xrange(bus.audit_key) if f.get('event') == event]

if not wait(lambda: all(r.hget(fleet.key('agent', s), 'state') in ('idle', 'busy') for s in ('supervisor', 'worker-1')), 'READY'):
    sys.exit(1)
files = 'Input files: %s/input/file_0001.txt .. file_%04d.txt.' % (tmp, n)
work = (files + ' In one continuous turn, read each file with your file-reading tool, one file per call, and after '
        'each one append "file NNNN: <SECRET CODE>" to progress.md in your own working directory. If progress.md '
        'already has lines, skip those files. Report only when all %d are done.' % n)
spec = {'goal': 'Smoke link loss: dispatch worker-1 with this instruction verbatim: ' + work + ' If worker-1 is '
                'restarted and its instruction cancelled, check worker-1/progress.md and dispatch the same '
                'instruction again. When worker-1/progress.md contains "file %04d", declare it and mark COMPLETED.' % n,
        'hours': 1.0, 'artifacts': [{'path': 'worker-1/progress.md', 'contains': ['file %04d' % n]}]}
runtime_v2.post_event(bus, dict(kind='task', request_id='smoke-ll', spec=spec), 'smoke-ll')
progress = run / 'workspace' / 'worker-1' / 'progress.md'
wait(lambda: agent().get('state') == 'busy' and progress.exists() and progress.read_text().count('file ') >= 2,
     'worker-1 busy mid-turn (progress >= 2 lines)')
gen = agent().get('generation', '0')
ps = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True).stdout.splitlines()
mine = [l for l in ps if 'agent_bridge.py' in l and run.name in l]
pids = [l.split()[0] for l in mine if l.rstrip().endswith(' worker-1 ' + gen)]
print('killing bridge pids', pids, 'generation', gen, flush=True)
if not pids:
    print('bridge candidates:', *[l[-200:] for l in mine], sep='\n  ')
    print('RESULT FAIL no bridge process for worker-1 g' + gen); sys.exit(1)
subprocess.run(['kill', '-9', *pids])
ok['1 link_lost + held, fleet not paused'] = wait(
    lambda: any(a.get('slot') == 'worker-1' and runtime_v2.LINK_LOST in (a.get('error') or '')
                for a in audits('execution.held')), 'link_lost hold', step=2) and r.hget(fleet.SERVICE, 'paused') != '1'
ok['2 restarted to worker-1.1'] = wait(lambda: agent().get('generation') == '1' and agent().get('link_lost') == '0'
                                       and bool(audits('link.restarted')), 'restart to generation 1', step=2)
def told():
    for m in r.scan_iter(bus.prefix + ':message:*'):
        if r.type(m) != 'hash':
            continue
        h = r.hgetall(m)
        if h.get('slot') == 'supervisor' and 'bridge link lost' in (h.get('body') or ''):
            return True
    return False
ok['3 supervisor told (FROZEN, bridge link lost)'] = wait(told, 'supervisor notice', step=3)
def completed():
    # --lifecycle smoke stops the run's Redis right after the task ends: then result.json is the record.
    if (run / 'result.json').exists():
        return json.loads((run / 'result.json').read_text()).get('status') == 'completed'
    try:
        return r.hget(fleet.TASK, 'status') == 'completed'
    except redis.exceptions.ConnectionError:
        return False
ok['4 task completed'] = wait(completed, 'task completed', step=10)
for k, v in ok.items():
    print(('PASS ' if v else 'FAIL ') + k)
print('RESULT ' + ('PASS' if all(ok.values()) else 'FAIL'))
sys.exit(0 if all(ok.values()) else 1)
PYEOF
