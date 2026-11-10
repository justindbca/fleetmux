#!/usr/bin/env bash
# End-to-end smoke of mid-turn handoff (docs/midturn-handoff-design.md r3, M1 daemon + M2 harness).
#
#   scripts/smoke_midturn.sh [--expect-versions <file>]
#
# Run it inside a cmux terminal with the CLIs logged in, AFTER M1 (the daemon probe/steer/split
# code in cmux_fleet/{midturn,fleet,runtime_v2,watchdog,handoff,codex_queue,roster}.py and
# cmux_fleet/pi_extension_v2.ts) has landed. This file only WRITES the harness; the orchestrator
# runs it by hand once M1 is ready. It never touches cmux_fleet/*.py or tests/ (Codex owns those
# concurrently on this same worktree).
#
# What it does: starts five small --lifecycle smoke fleets, one at a time, each in its own
# isolated CMUX_FLEET_HOME/CMUX_FLEET_STATE/private-Redis temp dir, drives a worker (or the
# supervisor) into a single turn long enough to cross a 0.10 mid-turn line, and asserts the
# r3 mechanics actually fired. Five scenarios (docs/midturn-handoff-design.md §7's combo list):
#   1. pi supervisor   + pi worker     (task)     -- worker crosses the line
#   2. pi supervisor   + claude worker (task)     -- default pair, worker crosses the line
#   3. pi supervisor   + pi worker     (service)  -- worker crosses the line; request must NOT
#                                                     time out even with a tight timeout_s, proving
#                                                     the §2.5 clock-pause credit
#   4. pi supervisor   + codex worker  (task)     -- exercises the codex_queue.bound relaxation (§4c)
#   5. claude supervisor + claude worker (task)   -- the SUPERVISOR itself crosses the line, exercising
#                                                     the Claude-supervisor UserPromptSubmit relaxation
#                                                     (§4c, flagged "possibly blocks, unverified" in the
#                                                     design) and the supervisor side of handoff_split
#
# Environment (optional, same names as scripts/smoke_pair.sh so existing exports work):
#   PI_MODEL=provider/model             pi workers' model; required whenever a scenario uses pi workers
#   SUPERVISOR_PI_MODEL=provider/model  pi supervisor's own model (default: PI_MODEL)
#   PI_THINKING / SUPERVISOR_PI_THINKING  optional thinking levels
#   PI=/path/to/pi, CLAUDE=/path/to/claude, CODEX is taken from PATH (cmux-fleet's own --codex flag
#                                           is not threaded here; add one if the target machine needs it)
#   SMOKE_PROFILE=<codex profile>        supervisor/worker Codex profile override
#   SMOKE_TIMEOUT=1800                   seconds to wait for READY + the whole mid-turn + continuation
#                                         cycle, per scenario
#   SERVICE_TIMEOUT_S=150                scenario 3's request timeout_s (deliberately tight; see TODO below)
#   N_FILES=40 / LINES_PER_FILE=800       size of the file-reading task that forces a long single turn
#   SMOKE_KEEP=1                          no-op placeholder for parity with smoke_pair.sh: this script
#                                         NEVER deletes its temp dirs regardless (requirement: leave them
#                                         for a human to inspect; only processes are stopped/killed)
#
# ---------------------------------------------------------------------------------------------
# TODO(M1 naming, 2026-10-27 snapshot): this worktree's M1 implementation is mid-flight (Codex is
# editing cmux_fleet/{fleet,runtime_v2,midturn,roster,watchdog,handoff,codex_queue}.py concurrently
# with this script). Every identifier below that crosses into M1's code is pinned to a bash
# constant, taken from grepping the snapshot at the lines noted; if the merged M1 renames any of
# these, fix the one constant, not the call sites.
#   - Roster `handoff:` table accepts a per-cli/per-cli:model `midturn_ratio` key (cmux_fleet/roster.py:155-161,
#     already landed, independent of M1's daemon half) -- MIDTURN_RATIO_KEY below.
#   - Audit event names (cmux_fleet/fleet.py:3862,3905,3959,3888,3922,3925; cmux_fleet/runtime_v2.py:839,
#     851,893,948) -- EVT_* below.
#   - Message hash field `outcome` == 'handoff_split' on a split turn's ORIGINAL message
#     (cmux_fleet/runtime_v2.py:923-926) -- OUTCOME_SPLIT below.
#   - The "no BLOCKED" event is literally `execution.held` (cmux_fleet/runtime_v2.py:1603) -- EVT_HELD below.
#   - Rotation label format `<slot_base>.<generation+1>`, e.g. worker-1.1 / supervisor-1.1
#     (cmux_fleet/fleet.py:101,158,199-201; cmux_fleet/runtime_v2.py:917-919) -- not overridable, just
#     computed inline per scenario below.
# TODO(task calibration): scenario 5 asks the SUPERVISOR role to read files itself before dispatching,
#     which is not how cmux_fleet/agents/supervisor*.md normally behaves (dispatch-only). If the
#     supervisor just dispatches immediately without reading anything, its own turn will never cross
#     the mid-turn line and this scenario fails with a clear "supervisor never ordered a mid-turn
#     handoff" message rather than a false pass -- that failure is itself useful signal, not a bug in
#     this harness.
# TODO(timing calibration): SERVICE_TIMEOUT_S in scenario 3 is a guess. It must be shorter than the
#     real wall time the run would take WITHOUT the §2.5 clock-pause credit (reading time + the
#     mid-turn handoff's own overhead) but is not provably shorter than reading time alone can be
#     computed without running on the target machine first. Recalibrate once M1 lands.
# ---------------------------------------------------------------------------------------------
set -u
export DISABLE_AUTOUPDATER=1  # every Claude Code this smoke starts never self-updates (10/14 01:36 broken shared install)

# ---- M1 naming constants (see TODO block above) ----------------------------------------------
MIDTURN_RATIO_KEY=midturn_ratio                 # roster handoff[cli].midturn_ratio
EVT_USAGE=midturn.usage
EVT_ORDERED=midturn.ordered
EVT_RETRY=midturn.retry
EVT_TYPE_FAILED=midturn.type_failed
EVT_NO_USAGE=midturn.no_usage
EVT_SPLIT=midturn.split
EVT_KILLED=midturn.killed
EVT_LIMIT=midturn.limit
EVT_IMPOSSIBLE=midturn.impossible
EVT_COMMITTED=committed
EVT_HELD=execution.held
OUTCOME_SPLIT=handoff_split

# ---- tunables ----------------------------------------------------------------------------------
MIDTURN_RATIO=${MIDTURN_RATIO:-0.20}
TEST_WINDOW=${TEST_WINDOW:-60000}          # roster window override: line = 0.20 x 60k = 12k, two file reads cross it
HANDOFF_RATIO_LOW=${HANDOFF_RATIO_LOW:-0.15}       # kept <= MIDTURN_RATIO (design: between-turn line <= mid-turn line); 9k > a fresh session's READY context
                                                    # must not preempt the mid-turn probe we're testing
N_FILES=${N_FILES:-40}
LINES_PER_FILE=${LINES_PER_FILE:-800}              # ~36KB/file ascii; TODO(calibrate) -- sized to clear
                                                    # a 0.10 line on both a ~200k (pi) and a ~1M (Claude)
                                                    # window; raise if a target CLI's real window is bigger
SMOKE_TIMEOUT=${SMOKE_TIMEOUT:-1800}
SERVICE_TIMEOUT_S=${SERVICE_TIMEOUT_S:-150}        # TODO(timing calibration), see header

# ---- optional --expect-versions <file> ----------------------------------------------------------
EXPECT_VERSIONS_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --expect-versions) EXPECT_VERSIONS_FILE=${2:?--expect-versions needs a file path}; shift 2;;
    *) echo "usage: $0 [--expect-versions <file>]" >&2; exit 2;;
  esac
done

# ---- 1. print + optionally compare CLI versions (no hardcoded /Users/... paths: command -v + $HOME) --
print_and_check_versions() {
  local fail=0
  declare -A seen
  for tool in pi claude codex cmux redis-server; do
    local path ver
    path=$(command -v "$tool" 2>/dev/null || true)
    if [ -z "$path" ]; then
      echo "VERSION $tool: NOT FOUND on PATH" >&2
      seen[$tool]="NOT FOUND"
      fail=1
      continue
    fi
    ver=$("$tool" --version 2>&1 | head -1)
    echo "VERSION $tool: $ver   (path: $path)"
    seen[$tool]="$ver"
  done
  if [ -n "$EXPECT_VERSIONS_FILE" ]; then
    [ -f "$EXPECT_VERSIONS_FILE" ] || { echo "FAIL --expect-versions file not found: $EXPECT_VERSIONS_FILE" >&2; exit 3; }
    while IFS=' ' read -r tool expected; do
      [ -z "${tool:-}" ] && continue
      case "$tool" in \#*) continue;; esac
      local actual="${seen[$tool]:-}"
      case "$actual" in
        *"$expected"*) echo "ok version match: $tool expected~='$expected' actual='$actual'";;
        *) echo "FAIL version mismatch: $tool expected~='$expected' actual='$actual'" >&2; fail=1;;
      esac
    done < "$EXPECT_VERSIONS_FILE"
  fi
  [ "$fail" = 0 ] || { echo "FAIL: version check failed, not starting any fleet" >&2; exit 3; }
}
print_and_check_versions

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3}
FLEET="$ROOT/cmux_fleet/fleet.py"

if ! "$PY" "$FLEET" start --help 2>/dev/null | grep -q -- '--lifecycle'; then
  echo "FAIL setup: this checkout's start has no --lifecycle; M1/M2 prerequisite is missing" >&2
  exit 3
fi

# ---- bookkeeping across scenarios (never deleted; see requirement 5) ---------------------------
declare -a RESULT_LINES=()
OVERALL_RC=0
CURRENT_TMP=""
CURRENT_CFG=""

stop_current() {
  set +e
  if [ -n "$CURRENT_CFG" ] && [ -f "$CURRENT_CFG" ]; then
    "$PY" "$FLEET" stop --config "$CURRENT_CFG" >/dev/null 2>&1
    "$PY" - "$CURRENT_CFG" "$ROOT" <<'EOF' >/dev/null 2>&1
import sys
sys.path.insert(0, sys.argv[2])
from cmux_fleet import fleet
fleet.manager_for(fleet.load(sys.argv[1])).close_all()
EOF
  fi
  if [ -n "$CURRENT_TMP" ]; then
    sleep 2
    pids=$(pgrep -f "$CURRENT_TMP" || true)
    [ -n "$pids" ] && kill $pids 2>/dev/null
    sleep 1
    pids=$(pgrep -f "$CURRENT_TMP" || true)
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null
    echo "kept (not deleted): $CURRENT_TMP"
  fi
}
trap 'stop_current' EXIT
trap 'echo INTERRUPTED >&2; stop_current; exit 130' INT TERM

# make_input_files <dir> <n> <lines_per_file> -- the "必然长回合" payload: N plain-text files the
# worker/supervisor must open and summarize one after another, inside a single continuous turn.
make_input_files() {
  local dir=$1 n=$2 lines=$3
  mkdir -p "$dir"
  local i
  for i in $(seq 1 "$n" | xargs printf '%04d '); do
    local code mid
    code=$(LC_ALL=C tr -dc 'A-Z0-9' </dev/urandom | head -c 8)
    mid=$(( (RANDOM % (lines - 20)) + 10 ))
    { printf 'FILE %s BEGIN\n' "$i"
      yes "midturn smoke filler line for file $i -- read me, summarize me, move on" | head -n "$mid"
      printf 'SECRET CODE FOR FILE %s: %s\n' "$i" "$code"
      yes "midturn smoke filler line for file $i -- read me, summarize me, move on" | head -n "$(( lines - mid ))"
      printf 'FILE %s END\n' "$i"
    } > "$dir/file_$i.txt"
  done
}

# run_case <case_name> <supervisor_cli> <worker_cli> <task|service> <worker|supervisor:trigger>
run_case() {
  local CASE=$1 SUP=$2 WRK=$3 MODE=$4 TRIGGER=$5
  echo
  echo "=== scenario: $CASE  ($SUP+$WRK/$MODE, trigger=$TRIGGER) ==="
  local TMP CFG INPUT_DIR
  TMP=$(mktemp -d /tmp/cfsmoke-mt.XXXXXX)   # short path: Unix socket limit is 103 bytes
  CURRENT_TMP=$TMP
  unset FLEETMUX_HOME FLEETMUX_STATE FLEETMUX_CONFIG  # §50: the new names would win over the isolated ones
  export CMUX_FLEET_HOME="$TMP/home" CMUX_FLEET_STATE="$TMP/state"
  mkdir -p "$CMUX_FLEET_HOME" "$CMUX_FLEET_STATE"
  CFG="$CMUX_FLEET_HOME/latest.json"
  CURRENT_CFG=$CFG
  INPUT_DIR="$TMP/input"
  make_input_files "$INPUT_DIR" "$N_FILES" "$LINES_PER_FILE"

  # --- roster: the only way (confirmed in cmux_fleet/roster.py) to set midturn_ratio per CLI -----
  local ROSTER="$TMP/roster.yaml"
  {
    echo "workers:"
    echo "  - name: worker-1"
    echo "handoff:"
    for cli in claude codex pi; do
      if [ "$cli" = claude ]; then   # a fresh Claude Code session starts near 54k: give it room
        echo "  $cli: {window: ${CLAUDE_TEST_WINDOW:-400000}, $MIDTURN_RATIO_KEY: ${CLAUDE_MIDTURN_RATIO:-0.25}}"
      else
        echo "  $cli: {window: $TEST_WINDOW, $MIDTURN_RATIO_KEY: $MIDTURN_RATIO}"
      fi
    done
  } > "$ROSTER"

  # --roster replaces --workers/--worker-names outright (fleet.py refuses both); the roster's own
  # "workers: [{name: worker-1}]" above is what defines the single worker slot here.
  local args=(start --lifecycle smoke --supervisor-cli "$SUP" --worker-cli "$WRK"
              --allow-development-v2 --redis-runtime-dir "$TMP/r" --name "smoke-mt-$SUP-$WRK-$MODE"
              --roster "$ROSTER" --handoff-ratio "$HANDOFF_RATIO_LOW")
  [ "$MODE" = service ] && args+=(--mode service --request-timeout "$SERVICE_TIMEOUT_S")
  if [ "$SUP" = pi ] || [ "$WRK" = pi ]; then
    if [ "$WRK" = pi ] || [ -z "${SUPERVISOR_PI_MODEL:-}" ]; then
      : "${PI_MODEL:?PI_MODEL=provider/model is required for pi workers (and a pi supervisor without SUPERVISOR_PI_MODEL)}"
    fi
    args+=(--allow-unsandboxed-pi)
    [ -n "${PI_MODEL:-}" ] && args+=(--pi-model "$PI_MODEL")
    [ -n "${PI_THINKING:-}" ] && args+=(--pi-thinking "$PI_THINKING")
  fi
  if [ "$SUP" = pi ]; then
    [ -n "${SUPERVISOR_PI_MODEL:-}" ] && args+=(--supervisor-pi-model "$SUPERVISOR_PI_MODEL")
    [ -n "${SUPERVISOR_PI_THINKING:-}" ] && args+=(--supervisor-pi-thinking "$SUPERVISOR_PI_THINKING")
  fi
  [ -n "${PI:-}" ] && args+=(--pi "$PI")
  [ -n "${CLAUDE:-}" ] && args+=(--claude "$CLAUDE")
  [ -n "${SMOKE_PROFILE:-}" ] && args+=(--profile "$SMOKE_PROFILE")

  echo "starting: $SUP+$WRK/$MODE in $TMP"
  if ! (cd "$TMP" && "$PY" "$FLEET" "${args[@]}"); then
    echo "FAIL setup: start refused or failed for $CASE (see above)" >&2
    RESULT_LINES+=("FAIL  $CASE  (start failed)")
    OVERALL_RC=1
    CURRENT_TMP=""; CURRENT_CFG=""
    return
  fi

  local rc=0
  "$PY" - "$CFG" "$MODE" "$TRIGGER" "$SMOKE_TIMEOUT" "$ROOT" "$TMP" "$INPUT_DIR" "$N_FILES" \
        "$EVT_ORDERED" "$EVT_SPLIT" "$EVT_KILLED" "$EVT_LIMIT" "$EVT_IMPOSSIBLE" "$EVT_HELD" \
        "$OUTCOME_SPLIT" "$SUP" "$WRK" <<'PYEOF' || rc=$?
import json, shutil, subprocess, sys, time, uuid
from pathlib import Path

(cfg_path, mode, trigger, timeout, root, tmp, input_dir, n_files,
 EVT_ORDERED, EVT_SPLIT, EVT_KILLED, EVT_LIMIT, EVT_IMPOSSIBLE, EVT_HELD,
 OUTCOME_SPLIT, sup_cli, wrk_cli) = sys.argv[1:]
timeout, n_files = float(timeout), int(n_files)
sys.path.insert(0, root)
import redis
from cmux_fleet import fleet, runtime_v2, control_view

cfg = fleet.load(cfg_path)
r = fleet.connect(cfg)
bus = runtime_v2.bus_for(cfg, r)
run = Path(cfg['run'])
trigger_slot = 'supervisor' if trigger == 'supervisor' else 'worker-1'
names = ['supervisor', 'worker-1']
deadline = time.time() + timeout
checks = {}
alive = {'v': True}
final_cache = {}


def agent(name, client=None):
    c = client or r
    try:
        return c.hgetall(fleet.key('agent', name))
    except (redis.exceptions.ConnectionError, OSError):
        alive['v'] = False
        return {}


def wait(pred, what):
    while time.time() < deadline:
        if pred():
            return True
        if not alive['v']:
            break
        time.sleep(3)
    print('FAIL timeout waiting for ' + what)
    return False


def final_from_disk(wait_s=60):
    """Mirrors scripts/smoke_pair.sh's final-state reader: once --lifecycle smoke stops this run's
    own Redis, open a throwaway copy of its persisted data so late-settling facts are not lost."""
    if 'client' in final_cache:
        return final_cache['client']
    src = Path(cfg['redis_runtime_dir'])
    gone = lambda: not (src / 'redis.pid').exists() and not Path(cfg['redis_socket']).exists()
    end = time.time() + wait_s
    while not gone() and time.time() < end:
        time.sleep(1)
    if not gone():
        print('   final state: run redis did not finish shutting down within %ds' % wait_s)
        return None
    dst = Path(tmp) / 'final'
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns('redis.log', 'redis.pid', '*.sock'))
    sock = dst / 'f.sock'
    subprocess.run([cfg['redis_server'], '--port', '0', '--unixsocket', str(sock), '--unixsocketperm', '700',
                    '--dir', str(dst), '--dbfilename', 'fleet.rdb',
                    '--appendonly', 'yes' if (dst / 'appendonlydir').is_dir() else 'no',
                    '--save', '', '--daemonize', 'yes', '--pidfile', str(dst / 'redis.pid'),
                    '--logfile', str(dst / 'redis.log')], check=True)
    client = redis.Redis(unix_socket_path=str(sock), decode_responses=True, socket_connect_timeout=3, socket_timeout=10)
    for _ in range(100):
        try:
            if client.ping():
                break
        except (redis.exceptions.ConnectionError, redis.exceptions.BusyLoadingError, OSError):
            time.sleep(.1)
    final_cache['client'] = client
    return client


def live_or_final():
    """A redis client that can still answer: the live one, or (once it's gone) the final copy."""
    if alive['v']:
        try:
            r.ping()
            return r
        except (redis.exceptions.ConnectionError, OSError):
            alive['v'] = False
    return final_from_disk()


def events(**kw):
    client = live_or_final()
    if client is None:
        return []
    try:
        return list(control_view.iter_events(cfg, limit=2000, **kw))
    except (redis.exceptions.ConnectionError, OSError, control_view.ControlError):
        alive['v'] = False
        client = final_from_disk()
        if client is None:
            return []
        # control_view.iter_events always opens its own connection from cfg; against the closed
        # run that connection will fail too, so fall back to a raw stream read on our final copy.
        try:
            rows = client.xrange(bus.audit_key)
        except (redis.exceptions.ConnectionError, OSError):
            return []
        out = []
        for _id, f in rows:
            if kw.get('slot') and f.get('slot') != kw['slot']:
                continue
            if kw.get('event') and f.get('event') != kw['event']:
                continue
            out.append(f)
        return out


if not wait(lambda: all(agent(n).get('runtime_ready') == '1' and agent(n).get('state') == 'idle' for n in names),
            'READY of supervisor + worker-1'):
    print('RESULT FAIL ' + mode + ' setup')
    sys.exit(1)
print('ok READY')

gen_before = int(agent(trigger_slot).get('generation') or 0)
label_before = agent(trigger_slot).get('label') or trigger_slot
print('   %s generation_before=%d label_before=%s' % (trigger_slot, gen_before, label_before))

files_clause = ('There are %d input files at %s/file_0001.txt through %s/file_%04d.txt (zero-padded, '
                 'in filename order).' % (n_files, input_dir, input_dir, n_files))
read_clause = ('In one single continuous turn, with no pauses and no intermediate report: open and read '
               'each of these files in order, and after reading each one append exactly one line '
               '"file NNNN: <code from the SECRET CODE line> <short summary>" to progress.md in your own current working directory '
               '(each file hides one SECRET CODE line somewhere in the middle: read the whole file with your file-reading tool, '
               'one file per tool call; do not use shell loops, grep, head or tail to skip content) '
               '(create progress.md if it does not exist yet). Do not stop, ask a question, or send any '
               'report until every one of the %d files has been read and summarized.' % n_files)

if trigger == 'worker':
    instruction = (files_clause + ' ' + read_clause +
                   ' Only after all files are done, finish your reply.')
    if mode == 'task':
        spec = {'goal': 'Smoke mid-turn: dispatch worker-1 to read ' + str(n_files) + ' files in one turn. '
                        + instruction + ' Dispatch worker-1 with this instruction verbatim, then once '
                        'worker-1 reports done, declare worker-1/progress.md as an artifact '
                        '(must contain "file %04d") and mark COMPLETED.' % n_files,
                'hours': 1.0,
                'artifacts': [{'path': 'worker-1/progress.md', 'contains': ['file %04d' % n_files]}]}
        rid = 'smoke-mt-task'
        runtime_v2.post_event(bus, dict(kind='task', request_id=rid, spec=spec), rid)
    else:
        rid = 'smoke-mt-svc'
        # No timeout_s here: it falls back to --request-timeout (the deliberately tight
        # SERVICE_TIMEOUT_S passed to `start`), so a non-timeout result proves §2.5's clock-pause credit.
        spec = {'id': rid, 'to': 'worker-1',
                'payload': {'message': instruction + ' When done, reply with reply.count = ' + str(n_files) + '.'},
                'reply': {'required': ['count'], 'allowed': ['count', 'note']}}
        runtime_v2.post_event(bus, dict(kind='request', request_id='cmd-' + rid, spec=spec), 'cmd-' + rid)
else:  # trigger == 'supervisor': the SUPERVISOR must do the reading itself before dispatching anything
    instruction = (files_clause + ' Before you dispatch any worker, exceptionally, YOU must do this '
                   'yourself first: ' + read_clause.replace('your own current working directory',
                   'your own current working directory (not a worker\'s)') +
                   ' Only once you personally have read and summarized all %d files yourself, dispatch '
                   'worker-1 one trivial task: write ok.txt containing OK. Then declare worker-1/ok.txt '
                   'as an artifact (contains OK) and mark COMPLETED.' % n_files)
    spec = {'goal': 'Smoke mid-turn (supervisor): ' + instruction, 'hours': 1.0,
            'artifacts': [{'path': 'worker-1/ok.txt', 'contains': ['OK']}]}
    rid = 'smoke-mt-sup-task'
    runtime_v2.post_event(bus, dict(kind='task', request_id=rid, spec=spec), rid)

# --- assertion 1: a mid-turn prompt delivery fired for the trigger slot -------------------------
ordered = []


def ordered_seen():
    ordered[:] = events(slot=trigger_slot, event=EVT_ORDERED)
    return bool(ordered)


if not wait(ordered_seen, EVT_ORDERED + ' on ' + trigger_slot):
    print('RESULT FAIL ' + mode + ' (mid-turn never ordered on ' + trigger_slot + '; see TODO(task calibration) '
          'in scripts/smoke_midturn.sh if trigger=supervisor)')
    sys.exit(1)
original_message_id = ordered[0].get('message_id')
checks['1 midturn ordered'] = bool(original_message_id)
print('   ok ' + EVT_ORDERED + ' message_id=%s' % original_message_id)

# --- assertion 2 + 3: generation +1, original message settled handoff_split, never BLOCKED ------
split_events = []


def _detail(e):
    d = e.get('detail') or {}
    return json.loads(d) if isinstance(d, str) else d


def split_seen():
    split_events[:] = [e for e in events(slot=trigger_slot) if e.get('event') in (EVT_SPLIT, EVT_KILLED)
                       and _detail(e).get('continues')]
    return bool(split_events) or int(agent(trigger_slot).get('generation') or gen_before) > gen_before


wait(split_seen, EVT_SPLIT + '/' + EVT_KILLED + ' on ' + trigger_slot)
msg = bus.message(original_message_id) if alive['v'] else {}
if not msg:
    client = final_from_disk()
    if client:
        try:
            raw = client.hgetall(bus.msg_key(original_message_id))
            msg = dict(raw, body=json.loads(raw['body']) if raw.get('body') else {})
        except (redis.exceptions.ConnectionError, OSError, KeyError, ValueError):
            msg = {}
checks['2 original message handoff_split'] = msg.get('outcome') == OUTCOME_SPLIT
print('   original message outcome=%s (want %s)' % (msg.get('outcome'), OUTCOME_SPLIT))

blocked = events(slot=trigger_slot, event=EVT_LIMIT) + events(slot=trigger_slot, event=EVT_IMPOSSIBLE) \
          + events(slot=trigger_slot, event=EVT_HELD)
checks['3 never BLOCKED/held'] = not blocked
if blocked:
    print('   UNEXPECTED blocked/held events: ' + json.dumps(blocked[:3], ensure_ascii=False))

# --- assertion 4: the continuation message continues the SAME request and the fleet finishes it ----
if mode == 'task':
    def task_ended():
        client = live_or_final()
        task = client.hgetall(fleet.TASK) if client else {}
        return task.get('status') in ('completed', 'blocked', 'budget_exhausted') or (run / 'result.json').is_file()
    wait(task_ended, 'task end')
    result = json.loads((run / 'result.json').read_text()) if (run / 'result.json').is_file() else {}
    client = live_or_final()
    task = client.hgetall(fleet.TASK) if client else {}
    status = task.get('status') or result.get('status')
    work_ok = status == 'completed' and result.get('status') in (None, 'completed') \
        and all(not a.get('problems') for a in result.get('artifacts', []))
    print('   task status=%s result.json_status=%s' % (status, result.get('status')))
else:
    rid = 'smoke-mt-svc'
    client = live_or_final()
    wait(lambda: client and client.get('fx:result:' + rid), 'service result')
    raw = client.get('fx:result:' + rid) if client else None
    res = json.loads(raw) if raw else {}
    print('   service result status=%s result=%s elapsed_s=%s' % (res.get('status'), res.get('result'), res.get('elapsed_s')))
    work_ok = res.get('status') == 'ok'
    checks['4b service request not timed out'] = res.get('status') != 'timeout'

_client = live_or_final()
gen_after = int((_client.hgetall(fleet.key('agent', trigger_slot)) if _client else {}).get('generation') or gen_before)
checks['4a generation +1'] = gen_after > gen_before
print('   %s generation_after=%d (before=%d)' % (trigger_slot, gen_after, gen_before))

expected_label = (trigger_slot if trigger == 'worker' else 'supervisor-1') + '.' + str(gen_before + 1)
history = (run / 'history.html')
history_text = history.read_text(errors='replace') if history.is_file() else ''
checks['4c history shows ' + expected_label] = expected_label in history_text
print('   history.html contains %r: %s' % (expected_label, expected_label in history_text))

checks['4d work finished'] = work_ok

# --- assertion 5: no CLI self-compaction in any pi session this scenario used -------------------
pi_slots = [s for s, cli in (('supervisor', sup_cli), ('worker-1', wrk_cli)) if cli == 'pi']
compaction_hits = []
for slot in pi_slots:
    for path in sorted((run / 'pi-sessions' / slot).glob('g*/*.jsonl')):
        for line in path.read_text(errors='replace').splitlines():
            if '"type": "compaction"' in line or '"type":"compaction"' in line:
                compaction_hits.append(str(path))
                break
checks['5 no pi self-compaction'] = not compaction_hits
if compaction_hits:
    print('   UNEXPECTED pi compaction in: ' + ', '.join(compaction_hits))

print('evidence: run dir = %s' % run)
print('evidence: handoffs dir = %s' % (run / 'handoffs'))
for name in sorted(checks):
    print(('PASS ' if checks[name] else 'FAIL ') + name)
sys.exit(0 if all(checks.values()) else 1)
PYEOF

  if [ "$rc" = 0 ]; then
    RESULT_LINES+=("PASS  $CASE")
  else
    RESULT_LINES+=("FAIL  $CASE  (exit $rc; see scenario log above)")
    OVERALL_RC=1
  fi
  stop_current
  CURRENT_TMP=""; CURRENT_CFG=""
}

# ---- the five scenarios --------------------------------------------------------------------------
# ONLY="1 2" runs a subset by number (default: all five).
want() { [ -z "${ONLY:-}" ] || [[ " $ONLY " == *" $1 "* ]]; }
want 1 && run_case "pi+pi task (worker crosses)"              pi     pi     task    worker
want 2 && run_case "pi+claude task default pair (worker)"     pi     claude task    worker
want 3 && run_case "pi+pi service, tight timeout (worker)"    pi     pi     service worker
want 4 && run_case "pi+codex task, codex worker crosses"      pi     codex  task    worker
want 5 && run_case "claude+claude task, SUPERVISOR crosses"   claude claude task    supervisor
want 6 && run_case "codex+claude task, CODEX SUPERVISOR crosses" codex claude task   supervisor

echo
echo "=== smoke_midturn summary ==="
for line in "${RESULT_LINES[@]}"; do echo "$line"; done
exit "$OVERALL_RC"
