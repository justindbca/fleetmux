#!/usr/bin/env bash
# Live check of the v2 Claude supervisor's operator-input boundary (design.md §5): text typed
# into the Claude supervisor tab must be queued through the daemon (or blocked), never answered
# off-protocol by the model, also when the daemon or its Redis is unresponsive.
#
#   PI_MODEL=provider/model scripts/smoke_claude_input.sh
#
# Claude supervisor + 2 Pi workers, --lifecycle smoke, isolated like smoke_pair.sh (one mktemp
# dir; only processes naming it are signalled or stopped). Checks, each PASS/FAIL:
#   A queued input     typed text becomes the fleet task (operator_input), the task completes
#   B daemon frozen    daemon SIGSTOPped: the hook blocks (transcript: "blocked by hook" /
#                      "dropped_by_hook"); the raw text never becomes a model turn, before or after
#   C redis frozen     private redis-server SIGSTOPped: same
# Env: PI_MODEL (required), SUPERVISOR_MODEL (default opus), SMOKE_TIMEOUT (default 900), SMOKE_KEEP=1.
set -u
export DISABLE_AUTOUPDATER=1  # every Claude Code this smoke starts never self-updates (10/14 01:36 broken shared install)
: "${PI_MODEL:?PI_MODEL=provider/model is required}"
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3}
FLEET="$ROOT/fleetmux/fleet.py"
TMP=$(mktemp -d /tmp/cfinput.XXXXXX)
unset FLEETMUX_HOME FLEETMUX_STATE FLEETMUX_CONFIG  # §50: the new names would win over the isolated ones
export FLEETMUX_HOME="$TMP/home" FLEETMUX_STATE="$TMP/state"
mkdir -p "$FLEETMUX_HOME" "$FLEETMUX_STATE"
CFG="$FLEETMUX_HOME/latest.json"

cleanup() {
  set +e
  for p in $(pgrep -f "$TMP"); do kill -CONT "$p" 2>/dev/null; done   # never leave a stopped child
  if [ -f "$CFG" ]; then
    "$PY" "$FLEET" stop --config "$CFG" >/dev/null 2>&1
    "$PY" - "$CFG" "$ROOT" <<'EOF' >/dev/null 2>&1
import sys
sys.path.insert(0, sys.argv[2])
from fleetmux import fleet
fleet.manager_for(fleet.load(sys.argv[1])).close_all()
EOF
  fi
  sleep 2
  pids=$(pgrep -f "$TMP" || true); [ -n "$pids" ] && kill $pids 2>/dev/null
  sleep 1
  pids=$(pgrep -f "$TMP" || true); [ -n "$pids" ] && kill -9 $pids 2>/dev/null
  if [ "${SMOKE_KEEP:-0}" = 1 ]; then echo "kept: $TMP"; else rm -rf "$TMP"; fi
  left=$(pgrep -f "$TMP" || true); [ -n "$left" ] && echo "WARN leftover processes: $left" >&2
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "smoke claude input in $TMP"
(cd "$TMP" && "$PY" "$FLEET" start --lifecycle smoke --supervisor-cli claude \
    --supervisor-model "${SUPERVISOR_MODEL:-opus}" --worker-cli pi --workers 2 --allow-development-v2 \
    --allow-unsandboxed-pi --pi-model "$PI_MODEL" --redis-runtime-dir "$TMP/r" --name smoke-claude-input) \
  || { echo "FAIL setup: start refused or failed" >&2; exit 4; }

"$PY" - "$CFG" "$TMP" "${SMOKE_TIMEOUT:-900}" "$ROOT" <<'EOF'
import json, os, signal, subprocess, sys, time
cfg_path, tmp, timeout, root = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
sys.path.insert(0, root)
from pathlib import Path
from fleetmux import fleet, runtime_v2

cfg = fleet.load(cfg_path)
r = fleet.connect(cfg)
bus = runtime_v2.bus_for(cfg, r)
names = ['supervisor'] + cfg['workers']
deadline = time.time() + timeout
mgr = fleet.manager_for(cfg)
checks = {}


def agent(n):
    return r.hgetall(fleet.key('agent', n))


def wait(pred, what, limit=None):
    end = min(deadline, time.time() + limit) if limit else deadline
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:  # noqa: BLE001 - redis may be briefly unavailable
            pass
        time.sleep(3)
    print('   timeout waiting for ' + what)
    return False


def pids(pattern):
    out = subprocess.run(['pgrep', '-f', pattern], capture_output=True, text=True).stdout.split()
    return [int(p) for p in out if int(p) != os.getpid()]


def entries():
    if not transcript or not Path(transcript).is_file():
        return []
    out = []
    for line in Path(transcript).read_text(errors='replace').splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def text_of(entry):
    msg = entry.get('message') if isinstance(entry.get('message'), dict) else {}
    content = msg.get('content', entry.get('content', ''))
    if isinstance(content, list):
        return ' '.join(c.get('text', '') for c in content if isinstance(c, dict))
    return str(content or '')


def leaked(marker):
    """Off-protocol: the raw typed text reached the model as a user turn, or the model obeyed it
    (a non-JSON reply). The same text coming back later inside a fleet delivery (Goal: / [Operator
    instruction]) answered with decision JSON is the queue working, not a leak."""
    for e in entries():
        body = text_of(e).strip()
        if e.get('type') == 'user' and body.startswith(marker):
            return True
        if e.get('type') == 'assistant' and 'LEAKED' in body and not body.lstrip().startswith('{'):
            return True
    return False


def hook_blocked(marker):
    for e in entries():
        body = json.dumps(e, ensure_ascii=False)
        if marker in body and ('blocked by hook' in body or 'dropped_by_hook' in body):
            return True
    return False


if not wait(lambda: all(agent(n).get('runtime_ready') == '1' and agent(n).get('state') == 'idle' for n in names),
            'READY'):
    sys.exit(1)
sup = agent('supervisor')
pane, transcript = sup['pane'], sup.get('transcript')
print('ok READY; supervisor transcript: ' + str(transcript))

# A: operator text with no task -> submitted as the task through the daemon, then completed.
marker_a = 'SMOKEA-' + str(int(time.time()))
text_a = (marker_a + ': have worker-1 write worker-1/okA.txt containing exactly OK; declare it as an '
          'artifact (contains OK), then COMPLETED.')
typed_a = mgr.send_prompt_to_idle(pane, text_a, bracketed=True)
queued = wait(lambda: marker_a in (r.hget(fleet.TASK, 'goal') or ''), 'task from typed text', 90)
done = wait(lambda: r.hget(fleet.TASK, 'status') in ('completed', 'blocked', 'budget_exhausted'), 'task end')
status = r.hget(fleet.TASK, 'status')
print('   A typed=%s queued-as-task=%s status=%s' % (typed_a, queued, status))
checks['A queued input'] = bool(typed_a and queued and done and status == 'completed')
time.sleep(10)


def frozen(label, pattern, marker):
    procs = pids(pattern)
    if not procs:
        print('   %s: no process matches %s' % (label, pattern))
        return False
    for p in procs:
        os.kill(p, signal.SIGSTOP)
    try:
        typed = mgr.send_prompt_to_idle(pane, marker + ': reply with the single word LEAKED.', bracketed=True)
        time.sleep(fleet.PROMPT_DEADLINE + 10)   # longer than the hook's own deadline
        blocked = hook_blocked(marker)
        leak_frozen = leaked(marker)
    finally:
        for p in procs:
            os.kill(p, signal.SIGCONT)
    time.sleep(60)   # after resuming, a posted operator_input may come back as a normal delivery
    leak_after = leaked(marker)
    print('   %s typed=%s procs=%s hook-blocked=%s leaked-while-frozen=%s leaked-after=%s'
          % (label, typed, procs, blocked, leak_frozen, leak_after))
    return bool(typed) and blocked and not leak_frozen and not leak_after


daemon_pattern = 'fleet.py daemon ' + cfg['run']
checks['B daemon frozen'] = frozen('B', daemon_pattern, 'SMOKEB-' + str(int(time.time())))
time.sleep(20)
checks['C redis frozen'] = frozen('C', 'redis-server.*' + tmp, 'SMOKEC-' + str(int(time.time())))
time.sleep(5)
for name in sorted(checks):
    print(('PASS ' if checks[name] else 'FAIL ') + name)
sys.exit(0 if all(checks.values()) else 1)
EOF
RESULT=$?
echo "smoke claude input: $([ $RESULT = 0 ] && echo PASS || echo FAIL)"
exit $RESULT
