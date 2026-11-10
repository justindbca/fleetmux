#!/usr/bin/env bash
# Real-TUI smoke of one transport-v2 CLI pair: 1 supervisor + 2 workers, lifecycle smoke.
#
#   scripts/smoke_pair.sh <supervisor_cli> <worker_cli> <task|service>
#   e.g. scripts/smoke_pair.sh pi claude task     (default pair: pi Astra supervisor + official Claude workers)
#        SUPERVISOR_PI_MODEL=openai-codex/gpt-6-astra PI_MODEL=flashnext/... scripts/smoke_pair.sh pi pi task  (night pair)
#        scripts/smoke_pair.sh codex claude service
#        OPENCODE_MODEL=opencode/deepseek-v4.1-flash-free PI_MODEL=... scripts/smoke_pair.sh pi opencode task
#        DSH_HOME=<shared dsh-tui home> SUPERVISOR_PI_MODEL=... scripts/smoke_pair.sh pi dsh task
#
# Run it inside a cmux terminal with the CLIs logged in. Environment (optional):
#   PI_MODEL=provider/model  run-wide pi model (--pi-model): required for pi workers, and for a pi
#                            supervisor unless SUPERVISOR_PI_MODEL is set
#   SUPERVISOR_PI_MODEL=provider/model, SUPERVISOR_PI_THINKING=level  pi supervisor's own model/thinking
#   PI_THINKING=level        run-wide pi thinking (--pi-thinking)
#   PI=/path/to/pi           pi binary (--pi; default: first on PATH, which inside cmux is its wrapper shim)
#   CLAUDE=/path/to/claude   official Claude Code binary (--claude; a shell function/shim is not a binary)
#   SUPERVISOR_MODEL=<id>    claude supervisor --model value (--supervisor-model)
#   OPENCODE_MODEL=provider/model  opencode workers' model (--opencode-model): required, no default (free list changes)
#   OPENCODE=/path/to/opencode     opencode binary (--opencode; >= 2.0.29)
#   DSH_MODEL=provider/model  dsh workers' model, a pi models.json entry (--dsh-model; default fleet.yaml#dsh.default_model)
#   DSH=/path/to/dsh          dsh binary (--dsh; == 0.2.1-rc.5)
#   DSH_HOME=<dir>            shared DSH_HOME with dsh-tui 0.18.0 installed (--dsh-home; default ~/.fleetmux/dsh-home)
#                             dsh only: worker-1 also writes `git config user.email` (via the pane HOME's ~/.gitconfig link)
#   BACKEND=cmux|fleetmux|wezterm  terminal backend (--backend; default: the platform's)
#   SMOKE_PROFILE=<codex profile>  supervisor/worker Codex profile (default: fleet's own default)
#   SMOKE_TIMEOUT=900        seconds for READY + work
#   SMOKE_KEEP=1             keep the temp dir for inspection (processes are still stopped)
#
# Isolation: CMUX_FLEET_HOME / CMUX_FLEET_STATE / the private Redis runtime all live in one
# mktemp dir; never the production Redis or ~/.cmux-fleet, never another fleet. Cleanup stops
# only this run (fleet stop, its own cmux tabs, processes whose command line names the temp dir).
#
# v2 mandatory checks (design.md §11, master design v2 §4.12), each printed PASS/FAIL:
#   1 no message files      no .inbox / msg-*.md anywhere under the run
#   2 zero-model cards      results reach the supervisor as UI-only cards (no mechanical relay turn)
#   3 stale callback        a retired-generation callback is rejected (turn.stale / settle rejected)
#   4 human input           a manual line typed into worker-1 is not settled as a delivery
# Evidence lines (not checks): each agent's cmux tab title, the model/thinking a pi agent actually ran
# (its session file; pi renames its process so ps shows no argv) and the screen line naming the model
# (pi status line). The title is the slot label (design.md §3); session/status line only prove the
# model, they do not replace a title acceptance.
# Exit 0 only if the run finished and all four PASS. A PASS does not flip the pair table to
# verified: a human edits config/fleet.yaml#cli_pairs (status + verified_by).
set -u
export DISABLE_AUTOUPDATER=1  # every Claude Code this smoke starts never self-updates (10/14 01:36 broken shared install)
SUP=${1:?supervisor cli}; WRK=${2:?worker cli}; MODE=${3:?task|service}
case "$MODE" in task|service) ;; *) echo "mode must be task or service" >&2; exit 2;; esac
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3}
FLEET="$ROOT/cmux_fleet/fleet.py"
TMP=$(mktemp -d /tmp/cfsmoke.XXXXXX)          # short path: Unix socket limit is 103 bytes
unset FLEETMUX_HOME FLEETMUX_STATE FLEETMUX_CONFIG  # §50: the new names would win over the isolated ones
export CMUX_FLEET_HOME="$TMP/home" CMUX_FLEET_STATE="$TMP/state"
mkdir -p "$CMUX_FLEET_HOME" "$CMUX_FLEET_STATE"
CFG="$CMUX_FLEET_HOME/latest.json"
RESULT=1

cleanup() {
  set +e
  if [ -f "$CFG" ]; then
    "$PY" "$FLEET" stop --config "$CFG" >/dev/null 2>&1
    "$PY" - "$CFG" "$ROOT" <<'EOF' >/dev/null 2>&1
import sys
sys.path.insert(0, sys.argv[2])
from cmux_fleet import fleet
fleet.manager_for(fleet.load(sys.argv[1])).close_all()   # this run's own tabs (isolated state dir)
EOF
  fi
  sleep 2
  # Only processes this run started carry the temp dir in their command line. SIGTERM only, never -9.
  pids=$(pgrep -f "$TMP" || true)
  [ -n "$pids" ] && kill $pids 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do [ -z "$(pgrep -f "$TMP" || true)" ] && break; sleep 1; done
  if [ "${SMOKE_KEEP:-0}" = 1 ]; then echo "kept: $TMP"; else rm -rf "$TMP"; fi
  left=$(pgrep -f "$TMP" || true)
  [ -n "$left" ] && echo "WARN leftover processes: $left" >&2
}
trap cleanup EXIT
trap 'exit 130' INT TERM

if ! "$PY" "$FLEET" start --help 2>/dev/null | grep -q -- '--lifecycle'; then
  echo "FAIL setup: this checkout's start has no --lifecycle (item D); unverified pairs can only start with --lifecycle smoke" >&2
  exit 3
fi

args=(start --lifecycle smoke --supervisor-cli "$SUP" --worker-cli "$WRK" --workers 2
      --allow-development-v2 --redis-runtime-dir "$TMP/r" --name "smoke-$SUP-$WRK-$MODE")
[ "$MODE" = service ] && args+=(--mode service)
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
[ "$SUP" = claude ] && [ -n "${SUPERVISOR_MODEL:-}" ] && args+=(--supervisor-model "$SUPERVISOR_MODEL")
if [ "$WRK" = opencode ]; then
  : "${OPENCODE_MODEL:?OPENCODE_MODEL=provider/model is required for opencode workers (e.g. opencode/deepseek-v4.1-flash-free)}"
  args+=(--opencode-model "$OPENCODE_MODEL")
  [ -n "${OPENCODE:-}" ] && args+=(--opencode "$OPENCODE")
fi
if [ "$WRK" = dsh ]; then
  [ -n "${DSH_MODEL:-}" ] && args+=(--dsh-model "$DSH_MODEL")
  [ -n "${DSH:-}" ] && args+=(--dsh "$DSH")
  [ -n "${DSH_HOME:-}" ] && args+=(--dsh-home "$DSH_HOME")
fi
[ -n "${BACKEND:-}" ] && args+=(--backend "$BACKEND")
[ -n "${SMOKE_PROFILE:-}" ] && args+=(--profile "$SMOKE_PROFILE")
echo "smoke: $SUP+$WRK/$MODE in $TMP"
if ! (cd "$TMP" && "$PY" "$FLEET" "${args[@]}"); then
  echo "FAIL setup: start refused or failed (see above)" >&2
  exit 4
fi

"$PY" - "$CFG" "$MODE" "${SMOKE_TIMEOUT:-900}" "$ROOT" "$TMP" <<'EOF'
import json, shutil, subprocess, sys, time, uuid
cfg_path, mode, timeout, root, tmp = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4], sys.argv[5]
sys.path.insert(0, root)
from pathlib import Path
import redis
from cmux_fleet import fleet, runtime_v2

cfg = fleet.load(cfg_path)
r = fleet.connect(cfg)
bus = runtime_v2.bus_for(cfg, r)
run = Path(cfg['run'])
names = ['supervisor'] + cfg['workers']
deadline = time.time() + timeout
checks = {}
mgr = fleet.manager_for(cfg)
# --lifecycle smoke closes the run (fleet stop, Redis down) as soon as the work ends. While it is
# alive, keep a live view of the audit / UI stream / agents / results; once it is closed, the checks
# read the final state from Redis's own persisted data (final_from_disk), never a stale live view.
snap = {'audit': [], 'ui': 0, 'agents': {}, 'results': {}, 'alive': True, 'source': 'live'}


def read_into(client, rids=()):
    snap['audit'] = [f for _, f in client.xrange(bus.audit_key)]
    snap['ui'] = client.xlen(bus.prefix + ':ui:supervisor')
    snap['agents'] = {n: client.hgetall(fleet.key('agent', n)) for n in names}
    snap['task'] = client.hgetall(fleet.TASK)
    for x in rids:
        raw = client.get(fleet.RESULT + x)
        if raw:
            snap['results'][x] = json.loads(raw)


def refresh(rids=()):
    if not snap['alive']:
        return False
    try:
        read_into(r, rids)
        return True
    except (redis.exceptions.ConnectionError, OSError):
        snap['alive'] = False
        print('   run closed (Redis gone): the checks read its final persisted state (Redis AOF/RDB) + result.json')
        return False


def final_from_disk(rids=(), wait_s=60):
    """The run's Redis shut down (AOF fsync + final RDB, then pid file and socket removed). Open a
    copy of its data dir on a throwaway socket inside the temp dir and read the final state: a
    result card written in the same instant as COMPLETED, after the last live view, is not lost."""
    src = Path(cfg['redis_runtime_dir'])
    gone = lambda: not (src / 'redis.pid').exists() and not Path(cfg['redis_socket']).exists()
    end = time.time() + wait_s
    while not gone() and time.time() < end:
        time.sleep(1)
    if not gone():
        print('   final state: the run Redis did not finish shutting down within %ds' % wait_s)
        return False
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
    try:
        for _ in range(100):
            try:
                if client.ping():
                    break
            except (redis.exceptions.ConnectionError, redis.exceptions.BusyLoadingError, OSError):
                time.sleep(.1)
        read_into(client, rids)
        snap['source'] = 'final (persisted data of the closed run)'
        return True
    except (redis.exceptions.RedisError, OSError) as e:
        print('   final state: read failed: %r' % e)
        return False
    finally:
        try:
            client.shutdown(nosave=True)
        except (redis.exceptions.RedisError, OSError):
            pass


def agent(n):
    refresh()
    return snap['agents'].get(n, {})


def wait(pred, what):
    while time.time() < deadline:
        if pred():
            return True
        if not snap['alive']:
            break
        time.sleep(3)
    print('FAIL timeout waiting for ' + what)
    return False


if not wait(lambda: all(agent(n).get('runtime_ready') == '1' and agent(n).get('state') == 'idle' for n in names),
            'READY of supervisor + 2 workers'):
    sys.exit(1)
print('ok READY: ' + ', '.join(n + '=' + agent(n).get('state', '?') for n in names))

# Evidence (not a check): tab title, pi session model/thinking, screen line naming the model.
for n in names:
    a = agent(n)
    if hasattr(mgr, '_where'):  # cmux / wezterm: per-workspace surfaces
        ws = mgr._where(a.get('pane', ''))
        info = mgr._surfaces(ws).get(a.get('pane', ''), {}) if ws else {}
    else:  # fleetmux: one registry
        info = mgr._surfaces().get(a.get('pane', ''), {})
    print('   evidence title %s label=%s tab=%r' % (n, a.get('label'), info.get('title')))
    cli = fleet.agent_cli(cfg, n)
    if cli == 'pi':
        # pi renames its process to "pi" (ps shows no argv); its session file records the model it ran.
        seen = {}
        for path in sorted((run / 'pi-sessions' / n).glob('g*/*.jsonl')):
            for line in path.read_text().splitlines():
                row = json.loads(line)
                if row.get('type') == 'model_change':
                    seen['model'] = row.get('provider', '') + '/' + row.get('modelId', '')
                elif row.get('type') == 'thinking_level_change':
                    seen['thinking'] = row.get('thinkingLevel')
        print('   evidence session %s model=%s thinking=%s' % (n, seen.get('model'), seen.get('thinking')))
    if cli == 'opencode':
        # The fleet plugin's activity log (= transcript) records the model each assistant turn ran on.
        rows = [line for path in sorted((run / 'opencode-sessions' / n).glob('g*/*.jsonl'))
                for line in path.read_text().splitlines() if '"model"' in line]
        print('   evidence session %s opencode_model=%s last=%s' % (n, cfg.get('opencode_model'), rows[-1][:200] if rows else None))
    if cli == 'dsh':
        # The pane's model patch pins the dsh-tui row; the bridge plugin fails a turn on any other model.
        rows = [line for path in sorted((run / 'dsh-tui-home' / n).glob('g*/model.patch.yml'))
                for line in path.read_text().splitlines() if 'model:' in line]
        print('   evidence session %s dsh_model=%s patch=%s' % (n, cfg.get('dsh_model'), rows[-1].strip() if rows else None))
    screen = mgr.capture_output(a.get('pane', ''), 60) if a.get('pane') else ''
    model = (fleet.agent_cfg(cfg, n).get('pi_model') if cli == 'pi' else cfg.get('opencode_model') if cli == 'opencode'
             else cfg.get('dsh_model') if cli == 'dsh' else cfg.get('worker_model')) or ''
    key = model.split('/')[-1][:24]  # the status line may truncate a long model id
    tail = [l.strip() for l in screen.splitlines() if key and key in l]
    print('   evidence screen %s model=%s line=%r' % (n, model, tail[-1][:160] if tail else None))

# 4 human input: a manual line in worker-1's tab before any delivery must not settle anything.
w1 = agent('worker-1')
claude = cfg.get('worker_cli') == 'claude'
typed = mgr.send_prompt_to_idle(w1['pane'], 'Manual operator note: reply with the word noted.', bracketed=claude)
time.sleep(45)
refresh()
manual_settled = [a for a in snap['audit'] if a.get('slot') == 'worker-1' and a.get('event') == 'committed']
manual_seen = [a for a in snap['audit'] if a.get('slot') == 'worker-1' and a.get('event') in ('turn.ignored',)]
checks['4 human input'] = bool(typed) and not manual_settled
print('   manual line typed=%s ignored-audit=%d committed=%d' % (typed, len(manual_seen), len(manual_settled)))

# 3 stale callback: a retired generation's turn end is refused by the daemon (before the work: the
# smoke lifecycle stops the run when the work ends).
if runtime_v2.tui_slot(cfg, 'supervisor'):
    late = fleet.tui_turn(cfg, 'supervisor', int(agent('supervisor').get('generation', 0)) + 7,
                          thread=agent('supervisor').get('thread') or str(uuid.uuid4()), transcript='',
                          text='{"status":"RUNNING","dispatch":[]}', inputs=[], wait=5)
    ok3 = late.get('status') == 'stale'
elif runtime_v2.tui_slot(cfg, 'worker-1'):
    # Official-CLI worker: its turn-end hook path (Claude Stop -> tui_turn) with a retired generation.
    late = fleet.tui_turn(cfg, 'worker-1', int(agent('worker-1').get('generation', 0)) + 7,
                          thread=agent('worker-1').get('thread') or str(uuid.uuid4()), transcript='',
                          text='Done: none', inputs=[], wait=5)
    ok3 = late.get('status') == 'stale'
    print('   stale worker-1 turn -> %s' % late.get('status'))
else:
    n0 = len(snap['audit'])
    gen = int(agent('worker-1').get('generation', 0)) + 7
    runtime_v2.post_event(bus, dict(kind='settled', agent='worker-1', generation=gen, run_id=bus.run_id,
                                    thread='stale-' + uuid.uuid4().hex, transcript='', delivery={},
                                    candidates=[], failed=False, aborted=False, unfinished=False),
                          'stale-' + uuid.uuid4().hex)
    time.sleep(5)
    refresh()
    tail = snap['audit'][n0:]
    ok3 = any(a.get('event', '').startswith(('settle.', 'turn.stale', 'event.rejected', 'event.stale')) for a in tail) \
        and not any(a.get('event') == 'committed' for a in tail)
checks['3 stale callback'] = ok3

# The work: two files (task) or two requests (service), one per worker.
rids = []
if mode == 'task':
    spec = {'goal': 'Smoke: worker-1 writes worker-1/ok1.txt containing OK; worker-2 writes worker-2/ok2.txt '
                    'containing OK. Dispatch both in one decision, declare both files as artifacts '
                    '(contains OK), then COMPLETED.', 'hours': 0.25,
            'artifacts': [{'path': 'worker-1/ok1.txt', 'contains': ['OK']},
                          {'path': 'worker-2/ok2.txt', 'contains': ['OK']}]}
    if fleet.agent_cli(cfg, 'worker-1') == 'dsh':
        # The pane HOME links only ~/.gitconfig (and ~/.npmrc): git must still see the user's identity.
        email = subprocess.run(['git', 'config', '--global', 'user.email'], capture_output=True, text=True).stdout.strip()
        spec['goal'] += (' worker-1 also runs `git config user.email` and writes its output to worker-1/git_email.txt;'
                         ' declare that file as an artifact too.')
        spec['artifacts'].append({'path': 'worker-1/git_email.txt', 'contains': [email or '@']})
        print('   dsh git check: worker-1/git_email.txt must contain %r' % email)
    rid = 'smoke-task'
    runtime_v2.post_event(bus, dict(kind='task', request_id=rid, spec=spec), rid)
    ended = lambda: (refresh() and snap['task'].get('status') in ('completed', 'blocked', 'budget_exhausted')) \
        or (run / 'result.json').is_file()
    wait(ended, 'task end')
    result = json.loads((run / 'result.json').read_text()) if (run / 'result.json').is_file() else {}
    status = snap.get('task', {}).get('status') if snap['alive'] else result.get('status')
    status = status or result.get('status')
    print('   task status=%s reason=%s result.json=%s' % (status, (snap.get('task') or {}).get('reason') or result.get('reason'),
                                                         result.get('status')))
    work_ok = status == 'completed' and result.get('status') in (None, 'completed') \
        and all(not a.get('problems') for a in result.get('artifacts', []))
else:
    rids = ['smoke-r1', 'smoke-r2']
    for rid, w in zip(rids, cfg['workers']):
        # No 'to': the supervisor must route each request itself (one routing turn per request).
        spec = {'id': rid, 'payload': {'message': 'Smoke: route this to ' + w + '; it replies with reply = "OK from ' + w + '".'},
                'reply': {'required': ['reply'], 'allowed': ['reply']}, 'timeout_s': 600}
        runtime_v2.post_event(bus, dict(kind='request', request_id='cmd-' + rid, spec=spec), 'cmd-' + rid)
    wait(lambda: refresh(rids) and all(x in snap['results'] for x in rids), 'both public results')
    for x in rids:
        v = snap['results'].get(x, {})
        print('   %s status=%s result=%s' % (x, v.get('status'), v.get('result')))
    work_ok = all(snap['results'].get(x, {}).get('status') == 'ok' for x in rids)
for _ in range(5):  # let cards render (tui_cards refresh) and late hooks land, while the run is alive
    time.sleep(3)
    refresh(rids)
final_ok = True
if not snap['alive']:
    final_ok = final_from_disk(rids)
    if mode == 'task' and final_ok:
        status = snap['task'].get('status') or result.get('status')
        work_ok = work_ok and status == 'completed'
events = snap['audit']

# 1 no message files
files = [str(p) for p in run.rglob('.inbox')] + [str(p) for p in run.rglob('msg-*.md')]
checks['1 no message files'] = not files

# 2 zero-model cards: results surface as UI cards; no "[Operator result" relay is ever delivered;
#   service: the supervisor gets exactly one routing delivery per request (no mechanical relay turn).
sup_delivers = [json.loads(a.get('detail') or '{}') for a in events
                if a.get('event') == 'deliver' and a.get('slot') == 'supervisor']
relays = [d for d in sup_delivers if str(d.get('text', '')).startswith('[Operator result')]
rendered = [a for a in events if a.get('event') == 'ui.rendered' and a.get('slot') == 'supervisor']
ok2 = snap['ui'] > 0 and not relays
if runtime_v2.tui_slot(cfg, 'supervisor'):
    ok2 = ok2 and bool(rendered) and (run / 'cards' / 'supervisor.log').is_file()
if mode == 'service':
    ok2 = ok2 and len(sup_delivers) == len(rids)
checks['2 zero-model cards'] = ok2
print('   supervisor deliveries=%d relays=%d ui cards=%d rendered=%d (%s, run alive=%s)'
      % (len(sup_delivers), len(relays), snap['ui'], len(rendered), snap['source'], snap['alive']))

errors = [a for a in events if a.get('level') == 'error']
print('   audit errors=%d' % len(errors))
for a in errors[:5]:
    print('     ' + json.dumps({k: a.get(k) for k in ('event', 'slot', 'error')}, ensure_ascii=False))
print(('PASS' if work_ok else 'FAIL') + ' work finished')
if not final_ok:
    print('FAIL final state read (the run closed and its persisted data could not be read)')
for name in sorted(checks):
    print(('PASS ' if checks[name] else 'FAIL ') + name)
sys.exit(0 if work_ok and final_ok and all(checks.values()) else 1)
EOF
RESULT=$?
echo "smoke $SUP+$WRK/$MODE: $([ $RESULT = 0 ] && echo PASS || echo FAIL)"
exit $RESULT
