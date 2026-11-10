#!/usr/bin/env bash
# Default-pair end-to-end check (feat/codex-via-pi, completion criterion 3): a `start` that names no
# supervisor/worker CLI and no model must come up as pi supervisor on openai-codex/gpt-6-astra
# (thinking high) + 2 official Claude Code workers, run 5 sequential worker steps with every
# receipt committed, and survive one real supervisor handoff (supervisor-1 -> supervisor-1.1).
#
#   scripts/smoke_codex_via_pi_e2e.sh            (inside a cmux terminal, CLIs logged in)
#   E2E_WORK=service scripts/smoke_codex_via_pi_e2e.sh
#       5 separate submissions instead of one task: `start --lifecycle service` (existing lifecycle; its base
#       sets mode service, no --mode / CLI / model flag), then 5 requests through the public
#       `fleet.py request` CLI, each submitted after the previous result is in, one supervisor handoff
#       between them; at the end the script (as operator) runs `fleet.py stop` and the lifecycle closes
#       the workspace (close_workspace_after_stop).
#
# Environment (optional):
#   CLAUDE=/path/to/claude   official Claude Code binary (--claude; a shell function / cmux shim is not one)
#   E2E_TIMEOUT=2400         seconds for READY + the whole task
#   E2E_LIFECYCLE=smoke      --lifecycle (E2E_WORK=service: service)
#   E2E_WORK=task            task: one task with 5 sequential steps; service: 5 separate requests
#   PYTHON=python3
#
# What is passed to start, and why (nothing else):
#   --lifecycle $E2E_LIFECYCLE  required by start; an unverified pair may only run under smoke
#   --allow-development-v2      v2 gate;  --allow-unsandboxed-pi  pi has no sandbox (confirmation flag)
#   --roster                    two rows worker-1/worker-2 (names only) + a top-level `handoff: {pi: ...}`
#                               table that scopes the handoff threshold to pi (the supervisor): a global
#                               --handoff-ratio would also hit the Claude workers
#   --redis-runtime-dir/--name  isolation / naming;  --claude  binary path only
# Supervisor handoff: after the supervisor's first committed decision the script rewrites the roster's
# `handoff: {pi: {ratio}}` to just below its measured context (hot reload, design §16/§22), so the next
# supervisor delivery rotates it. Once generation 1 exists, the ratio goes back to 0.6 so the successor
# does not rotate again. This is a test injection of the threshold only; CLI/model stay default.
#
# Evidence goes to $TMP/evidence (kept): audit.jsonl (streamed while the run is alive), agents.json,
# screens/*.txt (live pane captures; the supervisor's pi footer must name gpt-6-astra), argv records,
# summary.json. Exit 0 only if every check passes.
# Cleanup touches only this run: the smoke lifecycle closes it on task end; if it is still up, the
# script runs `fleet stop` on this run's config and closes this run's own tabs (isolated state dir), then
# stops processes whose command line names the temp dir. Other workspaces are never touched.
set -u
export DISABLE_AUTOUPDATER=1  # every Claude Code this smoke starts never self-updates (10/14 01:36 broken shared install)
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3}
FLEET="$ROOT/cmux_fleet/fleet.py"
TMP=$(mktemp -d /tmp/cfe2e.XXXXXX)            # short path: Unix socket limit is 103 bytes
unset FLEETMUX_HOME FLEETMUX_STATE FLEETMUX_CONFIG  # §50: the new names would win over the isolated ones
export CMUX_FLEET_HOME="$TMP/home" CMUX_FLEET_STATE="$TMP/state"
mkdir -p "$CMUX_FLEET_HOME" "$CMUX_FLEET_STATE" "$TMP/evidence/screens"
CFG="$CMUX_FLEET_HOME/latest.json"
ROSTER="$TMP/roster.yaml"
WORK=${E2E_WORK:-task}
case "$WORK" in task) LIFECYCLE=${E2E_LIFECYCLE:-smoke};; service) LIFECYCLE=${E2E_LIFECYCLE:-service};;
  *) echo "E2E_WORK must be task or service" >&2; exit 2;; esac

cleanup() {
  set +e
  if [ -f "$CFG" ]; then
    run=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["run"])' "$CFG" 2>/dev/null)
    if [ -n "$run" ] && [ -f "$run/daemon.lock" ] && ! [ -f "$run/result.json" ]; then
      echo "cleanup: run still up -> fleet stop (this run only)"
      "$PY" "$FLEET" stop --config "$CFG" >/dev/null 2>&1
    fi
    sleep 5
    "$PY" - "$CFG" "$ROOT" <<'EOF' 2>&1 | sed 's/^/cleanup: /'
import sys
sys.path.insert(0, sys.argv[2])
from cmux_fleet import fleet
mgr = fleet.manager_for(fleet.load(sys.argv[1]))   # isolated CMUX_FLEET_STATE: only this run's tabs
left = list(mgr._meta().get('surfaces', {}))
print('registry surfaces before close:', left)
print('close_all ->', mgr.close_all())
print('registry surfaces after close:', list(mgr._meta().get('surfaces', {})))
EOF
  fi
  pids=$(pgrep -f "$TMP" || true)
  [ -n "$pids" ] && { echo "cleanup: stopping this run's processes $pids"; kill $pids 2>/dev/null; sleep 1; }
  pids=$(pgrep -f "$TMP" || true)
  [ -n "$pids" ] && kill -9 $pids 2>/dev/null
  left=$(pgrep -f "$TMP" || true)
  echo "cleanup: leftover processes naming $TMP: ${left:-none}"
  echo "kept: $TMP"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

printf 'workers:\n  - {name: worker-1}\n  - {name: worker-2}\nhandoff:\n  pi: {ratio: 0.6}\n' > "$ROSTER"
args=(start --lifecycle "$LIFECYCLE" --allow-development-v2 --allow-unsandboxed-pi --roster "$ROSTER"
      --redis-runtime-dir "$TMP/r" --name e2e-default-pair)
[ -n "${CLAUDE:-}" ] && args+=(--claude "$CLAUDE")
echo "e2e: default pair in $TMP"
echo "e2e: $PY $FLEET ${args[*]}"
if ! (cd "$TMP" && "$PY" "$FLEET" "${args[@]}"); then
  echo "FAIL setup: start refused or failed (see above)" >&2
  exit 4
fi

"$PY" - "$CFG" "$ROSTER" "${E2E_TIMEOUT:-2400}" "$ROOT" "$TMP/evidence" "$WORK" "$PY" <<'EOF'
import json, math, re, secrets, subprocess, sys, time
cfg_path, roster_path, timeout, root, ev_dir, work, py = sys.argv[1:8]
timeout = float(timeout)
sys.path.insert(0, root)
from pathlib import Path
import redis
from cmux_fleet import fleet, runtime_v2

ev = Path(ev_dir)
cfg = fleet.load(cfg_path)
r = fleet.connect(cfg)
bus = runtime_v2.bus_for(cfg, r)
run = Path(cfg['run'])
names = ['supervisor'] + cfg['workers']
mgr = fleet.manager_for(cfg)
deadline = time.time() + timeout
snap = {'audit': [], 'agents': {}, 'task': {}, 'results': {}, 'alive': True, 'last_id': '-'}
rids = []
checks, notes = {}, []
audit_file = (ev / 'audit.jsonl').open('a')


def log(msg):
    print(time.strftime('%H:%M:%S ') + msg, flush=True)


def refresh():
    """Stream new audit entries to disk and snapshot agents/task while Redis is up."""
    if not snap['alive']:
        return False
    try:
        start = '(' + snap['last_id'] if snap['last_id'] != '-' else '-'
        for sid, f in r.xrange(bus.audit_key, start, '+'):
            snap['audit'].append(dict(f, _id=sid))
            audit_file.write(json.dumps(dict(f, _id=sid), ensure_ascii=False) + '\n')
            snap['last_id'] = sid
        audit_file.flush()
        snap['agents'] = {n: r.hgetall(fleet.key('agent', n)) for n in names}
        snap['task'] = r.hgetall(fleet.TASK)
        for x in rids:
            raw = r.get(fleet.RESULT + x)
            if raw:
                snap['results'][x] = json.loads(raw)
        (ev / 'agents.json').write_text(json.dumps(snap['agents'], ensure_ascii=False, indent=1))
        return True
    except (redis.exceptions.ConnectionError, OSError):
        snap['alive'] = False
        log('run closed (Redis gone): using the streamed audit + result.json')
        return False


def agent(n):
    refresh()
    return snap['agents'].get(n, {})


def wait(pred, what, step=3):
    while time.time() < deadline:
        if pred():
            return True
        if not snap['alive']:
            break
        time.sleep(step)
    log('FAIL timeout/closed waiting for ' + what)
    return False


def capture(tag, n):
    a = agent(n)
    screen = mgr.capture_output(a.get('pane', ''), 80) if a.get('pane') else ''
    path = ev / 'screens' / (tag + '-' + n + '.txt')
    path.write_text(screen)
    ws = mgr._where(a.get('pane', '')) if a.get('pane') else None
    title = (mgr._surfaces(ws).get(a.get('pane', ''), {}) if ws else {}).get('title')
    return screen, title


def footer(tag):
    """Live supervisor pane: the pi footer/status line must name the model."""
    screen, title = capture(tag, 'supervisor')
    lines = [l.strip() for l in screen.splitlines() if 'gpt-6-astra' in l]
    a = snap['agents'].get('supervisor', {})
    log('footer %s label=%s gen=%s tab=%r astra-lines=%r' % (tag, a.get('label'), a.get('generation'), title,
                                                          [l[:160] for l in lines[-2:]]))
    return {'tag': tag, 'label': a.get('label'), 'generation': a.get('generation'), 'tab_title': title,
            'astra_lines': lines[-3:], 'screen': str(ev / 'screens' / ('%s-supervisor.txt' % tag))}


def set_pi_ratio(ratio):
    Path(roster_path).write_text('workers:\n  - {name: worker-1}\n  - {name: worker-2}\nhandoff:\n  pi: {ratio: %s}\n' % ratio)
    log('roster handoff pi ratio -> %s' % ratio)


# --- default resolution as written to the run config (not a check of the screen)
summary = {'run': str(run), 'run_id': run.name, 'config': {k: cfg.get(k) for k in (
    'supervisor_cli', 'worker_cli', 'mode', 'lifecycle', 'pi_model', 'pi_thinking', 'pi_models', 'pi_thinkings',
    'worker_model', 'claude', 'workers')}}
log('config ' + json.dumps(summary['config'], ensure_ascii=False))
checks['0 default config'] = (cfg.get('supervisor_cli'), cfg.get('worker_cli')) == ('pi', 'claude') and \
    cfg.get('mode') == ('service' if work == 'service' else 'task') and \
    fleet.agent_cfg(cfg, 'supervisor').get('pi_model') == 'openai-codex/gpt-6-astra' and \
    fleet.agent_cfg(cfg, 'supervisor').get('pi_thinking') == 'high' and cfg.get('workers') == ['worker-1', 'worker-2']

if not wait(lambda: all(agent(n).get('runtime_ready') == '1' and agent(n).get('state') == 'idle' for n in names),
            'READY of supervisor + 2 workers'):
    summary['checks'] = checks
    (ev / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    sys.exit(1)
log('READY: ' + ', '.join('%s=%s/%s' % (n, agent(n).get('label'), agent(n).get('state')) for n in names))
for n in names:
    a = agent(n)
    log('handoff limits %s: ratio=%s window=%s source=%s' % (n, a.get('handoff_ratio'), a.get('handoff_window'),
                                                             a.get('handoff_source')))
foot = [footer('g0-ready')]
titles = {}
for n in names:
    titles[n] = capture('g0-ready', n)[1]


def rotation_tick():
    """After the supervisor's first committed decision: threshold just below its context; restore after g1."""
    sup = snap['agents'].get('supervisor', {})
    sup_commits = [a for a in snap['audit'] if a.get('event') == 'committed' and a.get('slot') == 'supervisor']
    if rotation['armed_at'] is None and sup_commits and sup.get('generation') == '0':
        used, window = int(sup.get('ctx_used') or 0), int(sup.get('ctx_window') or 0)
        if used and window:
            ratio = math.floor((used - 1) / window * 1e6) / 1e6
            set_pi_ratio(ratio)
            rotation.update(armed_at=time.time(), ratio=ratio, used=used, window=window)
            foot.append(footer('g0-busy'))
    if rotation['armed_at'] and not rotation['restored'] and sup.get('generation') not in (None, '0'):
        set_pi_ratio(0.6)
        rotation['restored'] = True
    if rotation['restored'] and not any(f['tag'] == 'g1-ready' for f in foot) and sup.get('generation') == '1' \
            and sup.get('runtime_ready') == '1' and sup.get('state') in ('idle', 'busy'):
        time.sleep(2)
        foot.append(footer('g1-ready'))
        titles['supervisor-g1'] = capture('g1-ready-title', 'supervisor')[1]


rotation = {'armed_at': None, 'ratio': None, 'restored': False}
if work == 'service':
    # --- 5 separate submissions through the public CLI, each after the previous result is in
    order = ['worker-1', 'worker-2', 'worker-1', 'worker-2', 'worker-1']
    subs = []
    for k in range(5):
        w, tok = order[k], 'REQ%d %s' % (k + 1, secrets.token_hex(4))
        rid = 'e2e-req%d-%s' % (k + 1, tok.split()[1])
        spec = {'id': rid, 'payload': {'message': 'Submission %d of 5 (independent request). Route it to %s only. '
                                       '%s writes %s/req%d.txt containing exactly the line "%s" and replies with '
                                       'reply = "%s".' % (k + 1, w, w, w, k + 1, tok, tok)},
                'reply': {'required': ['reply'], 'allowed': ['reply']}, 'timeout_s': 900}
        rids.append(rid)
        t0 = time.time()
        cp = subprocess.run([py, str(Path(root) / 'cmux_fleet' / 'fleet.py'), 'request', '-', '--config', cfg_path],
                            input=json.dumps(spec), capture_output=True, text=True, timeout=120)
        ack = cp.stdout.strip()
        log('submit %d %s -> %s rc=%d ack=%s' % (k + 1, rid, w, cp.returncode, ack[:200]))
        sub = {'k': k + 1, 'id': rid, 'worker': w, 'token': tok, 'cli_rc': cp.returncode, 'ack': ack,
               'stderr': cp.stderr.strip()[-300:], 'submitted_at': t0}
        subs.append(sub)
        if cp.returncode != 0:
            break
        while time.time() < deadline and rid not in snap['results'] and snap['alive']:
            refresh()
            rotation_tick()
            time.sleep(3)
        res = snap['results'].get(rid, {})
        sub.update(result=res, secs=round(time.time() - t0, 1))
        log('result %d %s status=%s result=%s (%.0fs)' % (k + 1, rid, res.get('status'),
                                                          json.dumps(res.get('result'), ensure_ascii=False)[:160],
                                                          time.time() - t0))
        if res.get('status') != 'ok':
            break
    for _ in range(10):  # successor footer capture if the handoff landed on the last routing turn
        refresh()
        rotation_tick()
        if any(f['tag'] == 'g1-ready' for f in foot):
            break
        time.sleep(3)
    cp = subprocess.run([py, str(Path(root) / 'cmux_fleet' / 'fleet.py'), 'results', '--config', cfg_path],
                        capture_output=True, text=True, timeout=60)
    (ev / 'results-cli.txt').write_text(cp.stdout)
    summary['submissions'] = subs
    summary['results_cli_lines'] = len([l for l in cp.stdout.splitlines() if l.strip()])
    refresh()
    time.sleep(4)
    refresh()
    log('operator stop (service lifecycle: stop is user_only; this script is the operator)')
    cp = subprocess.run([py, str(Path(root) / 'cmux_fleet' / 'fleet.py'), 'stop', '--config', cfg_path],
                        capture_output=True, text=True, timeout=120)
    summary['stop'] = {'rc': cp.returncode, 'out': cp.stdout.strip()[-300:]}
    log('stop rc=%d %s' % (cp.returncode, cp.stdout.strip()[-200:]))
    for _ in range(40):
        if (run / 'result.json').is_file():
            break
        time.sleep(3)
else:
    # --- the task: 5 strictly sequential single-worker steps, each depending on the previous file
    nonce = [secrets.token_hex(4) for _ in range(5)]
    order = ['worker-1', 'worker-2', 'worker-1', 'worker-2', 'worker-1']
    steps, artifacts = [], []
    for k in range(5):
        w, path = order[k], '%s/step%d.txt' % (order[k], k + 1)
        line = 'STEP%d %s' % (k + 1, nonce[k])
        if k == 0:
            steps.append('Step 1: %s writes %s containing exactly the line "%s".' % (w, path, line))
            contains = [line]
        else:
            prev = '%s/step%d.txt' % (order[k - 1], k)
            steps.append('Step %d: %s reads ../%s and writes %s with two lines: "%s" and the first line of that file.'
                         % (k + 1, w, prev, path, line))
            contains = [line, 'STEP%d %s' % (k, nonce[k - 1])]
        artifacts.append({'path': path, 'contains': contains})
    goal = ('End-to-end check, 5 steps that must run strictly one after another. Exactly ONE dispatch per decision: '
            'dispatch one step, wait for that worker\'s report, verify its file, then dispatch the next step. Never '
            'dispatch two steps at once and never reply COMPLETED before the step 5 report has arrived and all five '
            'files are verified. ' + ' '.join(steps) + ' Declare all five files as artifacts, then COMPLETED.')
    spec = {'goal': goal, 'hours': 0.75, 'artifacts': artifacts}
    summary['task'] = {'goal': goal, 'artifacts': artifacts}
    runtime_v2.post_event(bus, dict(kind='task', request_id='e2e-task', spec=spec), 'e2e-task')
    log('task posted (5 sequential steps)')

    ended = lambda: (snap['alive'] and snap['task'].get('status') in ('completed', 'blocked', 'budget_exhausted')) \
        or (run / 'result.json').is_file()
    while time.time() < deadline and not ended():
        refresh()
        rotation_tick()
        if not snap['alive']:
            break
        time.sleep(3)
refresh()
time.sleep(4)
result = json.loads((run / 'result.json').read_text()) if (run / 'result.json').is_file() else {}
summary['result'] = result
summary['rotation'] = rotation
summary['footers'] = foot
summary['titles'] = titles

# --- receipts from the streamed audit
A = snap['audit']
by = lambda e, s=None: [a for a in A if a.get('event') == e and (s is None or a.get('slot') == s)]
deliveries = {}
for a in A:
    did = a.get('delivery_id') or ''
    if not did:
        try:
            did = (json.loads(a.get('detail') or '{}').get('delivery') or {}).get('delivery_id', '')
        except (ValueError, AttributeError):
            did = ''
    if did and a.get('slot'):
        deliveries.setdefault(did, {'slot': a['slot'], 'events': []})['events'].append((a.get('event'), a.get('at')))
rows = []
for did, d in deliveries.items():
    evs = [e for e, _ in d['events']]
    rows.append({'delivery_id': did, 'slot': d['slot'], 'events': evs,
                 'committed': 'committed' in evs, 'at': [t for _, t in d['events']][:1]})
summary['deliveries'] = rows
worker_rows = [x for x in rows if x['slot'] in ('worker-1', 'worker-2')]
worker_commits = by('committed', 'worker-1') + by('committed', 'worker-2')
held = by('execution.held')
errors = [a for a in A if a.get('level') == 'error']
rot_start, rot_done = by('rotate.start'), by('rotate.done')
for x in rows:
    log('delivery %s %-10s committed=%s events=%s' % (x['delivery_id'][:14], x['slot'], x['committed'], ','.join(x['events'])))

if work == 'service':
    subs = summary['submissions']
    ws = Path(cfg['run']) / 'workspace'
    for x in subs:
        f = ws / x['worker'] / ('req%d.txt' % x['k'])
        x['file'] = f.read_text().strip() if f.is_file() else None
    checks['1 five separate submissions ok'] = len(subs) == 5 and all(
        x['cli_rc'] == 0 and (x.get('result') or {}).get('status') == 'ok'
        and x['token'] in json.dumps((x.get('result') or {}).get('result'), ensure_ascii=False)
        and x['file'] == x['token'] for x in subs)
    checks['2 five worker receipts committed'] = len(worker_commits) >= 5 and all(x['committed'] for x in worker_rows)
    checks['8 operator stop accepted'] = summary['stop']['rc'] == 0
else:
    status = result.get('status') or snap['task'].get('status')
    checks['1 task completed + artifacts'] = status == 'completed' and bool(result.get('artifacts')) and \
        all(not a.get('problems') for a in result.get('artifacts', [])) and len(result.get('artifacts', [])) >= 5
    checks['2 five worker receipts committed'] = len(worker_commits) >= 5 and all(x['committed'] for x in worker_rows)
checks['3 every delivery committed, zero held/error'] = all(x['committed'] for x in rows) and not held and not errors
checks['4 supervisor handoff'] = bool(rot_start) and bool(rot_done) and \
    any(a.get('label') == 'supervisor-1.1' or 'supervisor-1.1' in (a.get('detail') or '') for a in rot_done + A) and \
    snap['agents'].get('supervisor', {}).get('label', '').startswith('supervisor-1.')
checks['5 astra footer live (g0 and g1)'] = all(any(f['tag'] == t and f['astra_lines'] for f in foot)
                                                for t in ('g0-ready', 'g1-ready'))
checks['6 tab title = label'] = (titles.get('supervisor') or '').endswith('supervisor-1') and \
    (titles.get('supervisor-g1') or '').endswith('supervisor-1.1')
files = [str(p) for p in run.rglob('.inbox')] + [str(p) for p in run.rglob('msg-*.md')]
checks['7 no message files'] = not files
argv = {}
for p in sorted((run / 'prompts').glob('supervisor-g*.argv.json')):
    rec = json.loads(p.read_text())
    a = rec['argv']
    argv[p.name] = {'model': a[a.index('--model') + 1], 'thinking': a[a.index('--thinking') + 1], 'name': a[a.index('--name') + 1]}
summary['argv_records'] = argv          # supplementary: prepared exec argv, not proof of the running model
summary['counts'] = {'worker_commits': len(worker_commits), 'held': len(held), 'errors': len(errors),
                     'rotate.start': len(rot_start), 'rotate.done': len(rot_done), 'deliveries': len(rows)}
summary['errors'] = [{k: a.get(k) for k in ('event', 'slot', 'error', 'reason')} for a in errors[:10]]
summary['checks'] = checks
(ev / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1))
log('counts ' + json.dumps(summary['counts']))
log('argv records (supplementary) ' + json.dumps(argv))
for k, v in checks.items():
    log(('PASS ' if v else 'FAIL ') + k)
sys.exit(0 if all(checks.values()) else 1)
EOF
RC=$?
run=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["run"])' "$CFG" 2>/dev/null)
if [ -n "$run" ]; then
  # The smoke lifecycle's own closer writes result.json "workspace": "closing" first, then the outcome.
  # Wait for the outcome so cleanup never stops the closer half-way (first run: it did).
  for _ in $(seq 1 60); do
    ws=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("workspace") or "")' "$run/result.json" 2>/dev/null)
    [ -n "$ws" ] && [ "$ws" != closing ] && break
    sleep 3
  done
  cp -p "$run"/result.json "$run"/lifecycle-close.log "$TMP/evidence/" 2>/dev/null
  cp -p "$run"/prompts/*.argv.json "$TMP/evidence/" 2>/dev/null
  cp -rp "$run"/handoffs "$TMP/evidence/" 2>/dev/null
  echo "run: $run"
  echo "result.json workspace: $("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("workspace"))' "$run/result.json" 2>/dev/null)"
fi
echo "e2e exit=$RC"
exit $RC
