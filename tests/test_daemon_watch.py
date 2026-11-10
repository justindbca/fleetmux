"""§36 the supervisor's daemon watchdog: the pi extension's loop asks its bridge for the daemon heartbeat, a stale
beat starts one supervisor turn with the log facts, and the supervisor runs the restart command its AGENTS.md
names for this run. Real private Redis (test fixture), fake cmux; the extension runs in the mock-Pi harness."""
import fcntl
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx  # noqa: F401  (fixture)
from test_resume import managed, task_run, half_done, crash_and_resume, answer_turn  # noqa: F401  (fixtures)
from test_v2_codex_claude import typed
from test_pi_v2_contract import PI_ROOT
from cmux_fleet import agent_bridge, fleet, fleet_config, pi_adapter, runtime_v2


# ------------------------------------------------------------------ AGENTS.md: this run's restart command
def supervisor_scope(f):
    return f.role_scope('supervisor', f.runtime_cfg('supervisor'), {})


def test_supervisor_agents_md_names_this_runs_restart_command(fx):  # noqa: F811
    f = fx
    f.cfg.update(fleet_home='/h/fleet home', cmux_state_dir='/h/state', private_config='/h/fleet.yaml', cmux_bin='/h/cmux')
    scope = supervisor_scope(f)
    assert '## Daemon down' in scope and '{{' not in scope
    env = ['env', 'CMUX_FLEET_HOME=/h/fleet home', 'CMUX_FLEET_STATE=/h/state', 'CMUX_FLEET_CONFIG=/h/fleet.yaml',
           'CMUX_BIN=/h/cmux', sys.executable, str(Path(fleet.__file__).resolve())]
    assert shlex.join(env + ['start', '--allow-development-v2', '--resume', str(f.run_dir)]) in scope
    assert shlex.join(env + ['status', '--config', str(f.run_dir / 'config.json')]) in scope
    assert '[Daemon watchdog]' in scope and 'PROGRESS.md' in scope
    assert 'Daemon down' not in f.role_scope('worker-1', f.runtime_cfg('worker-1'), {})
    f.cfg['private_config'] = 'none'  # the run recorded that it has no private file: none is named
    assert 'CMUX_FLEET_CONFIG' not in supervisor_scope(f)
    f.cfg['parent_config'] = str(f.run_dir / 'parent.json')  # a sub-fleet's daemon is its parent's to revive
    assert 'Daemon down' not in supervisor_scope(f)


def test_pi_environment_gives_the_watch_knobs_to_the_main_supervisor_only(fx, monkeypatch, tmp_path):  # noqa: F811
    f = fx
    env = pi_adapter.environment(f.path, f.cfg, 'supervisor', 0)
    assert json.loads(env['CMUX_FLEET_DAEMON_WATCH']) == {'check_interval_s': 120, 'stale_after_s': 120,
                                                           'realert_after_s': 600}
    assert 'CMUX_FLEET_DAEMON_WATCH' not in pi_adapter.environment(f.path, f.cfg, 'worker-1', 0)
    assert 'CMUX_FLEET_DAEMON_WATCH' not in pi_adapter.environment(f.path, dict(f.cfg, parent_config='p'), 'supervisor', 0)
    private = tmp_path / 'fleet.yaml'
    private.write_text('daemon_watch:\n  stale_after_s: 300\n')
    monkeypatch.setenv(fleet_config.USER_ENV, str(private))
    knobs = json.loads(pi_adapter.environment(f.path, f.cfg, 'supervisor', 0)['CMUX_FLEET_DAEMON_WATCH'])
    assert knobs == {'check_interval_s': 120, 'stale_after_s': 300, 'realert_after_s': 600}


# ------------------------------------------------------------------ the facts: beat, audit, unread events, held
def test_outage_facts_come_from_the_beat_the_audit_stream_and_the_unread_events(fx):  # noqa: F811
    f = fx
    r, bus = f.v2.bus.r, f.v2.bus
    beat = time.time()
    r.set('fx:daemon:beat', beat)
    r.xadd(bus.audit_key, {'event': 'deliver', 'slot': 'supervisor', 'at': str(int(beat * 1000))})
    for name in ('renewed', 'received', 'turn.ignored', 'hook.dropped'):  # bridges / hooks go on without a daemon
        r.xadd(bus.audit_key, {'event': name, 'slot': 'worker-1'})
    assert runtime_v2.daemon_outage(r, bus, f.cfg, beat + 1)['pending_events'] == 0
    for n in range(2):
        runtime_v2.post_event(bus, {'kind': 'operator_input', 'text': 'waiting ' + str(n)})
    r.hset(bus.lease_key('worker-1'), mapping={'status': 'uncertain'})
    facts = runtime_v2.daemon_outage(r, bus, f.cfg, beat + 300)
    assert facts['down_s'] == 300 and facts['beat'] == pytest.approx(beat)
    assert facts['last_event'] == 'deliver' and facts['last_event_at'] == pytest.approx(beat, abs=1)
    assert facts['pending_events'] == 2 and facts['held'] == ['worker-1']


def test_bridge_answers_daemon_beat_from_redis_even_after_a_fault(fx):  # noqa: F811
    f = fx
    bridge = agent_bridge.AgentBridge(f.cfg, 'supervisor', 0, io.StringIO())
    try:
        f.v2.bus.r.set('fx:daemon:beat', time.time())
        fresh = bridge.command({'kind': 'daemon_beat', 'stale_after': 120})
        assert fresh['status'] == 'ok' and 'daemon_running' not in fresh  # the lock is probed only when stale
        f.v2.bus.r.set('fx:daemon:beat', time.time() - 500)
        bridge.faulted = True  # e.g. the settle of the last delivery found no daemon
        stale = bridge.command({'kind': 'daemon_beat', 'stale_after': 120})
        assert stale['status'] == 'ok' and stale['daemon_running'] is False and stale['unfinished'] is True
        assert stale['down_s'] >= 500 and 'pending_events' in stale
        with (f.run_dir / 'daemon.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a daemon holds the run
            assert bridge.command({'kind': 'daemon_beat', 'stale_after': 120})['daemon_running'] is True
        (f.run_dir / 'result.json').write_text('{}')  # finished: never resumed by the watchdog
        assert bridge.command({'kind': 'daemon_beat', 'stale_after': 120})['unfinished'] is False
    finally:
        bridge.stop()
    worker = agent_bridge.AgentBridge(f.cfg, 'worker-1', 0, io.StringIO())
    try:
        with pytest.raises(ValueError):
            worker.command({'kind': 'daemon_beat', 'stale_after': 120})
    finally:
        worker.stop()


# ------------------------------------------------------------------ the resumed daemon briefs from the same facts
def test_resume_brief_and_audit_carry_the_outage_from_the_logs(task_run):  # noqa: F811
    state = task_run
    half_done(state)
    f = state['f']
    f.r.set('fx:daemon:beat', time.time())  # the daemon's last loop: its audit rows came before this beat
    runtime_v2.post_event(f.v2.bus, {'kind': 'operator_input', 'text': 'typed while the daemon was down'})
    f = crash_and_resume(state)  # >= 1.2 s later (AOF fsync wait)
    resumed, = [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == 'daemon.resumed']
    assert int(resumed['down_s']) >= 1 and resumed['pending_events'] == '1'
    assert resumed['last_event'] and not resumed['last_event'].startswith(runtime_v2.AGENT_AUDITS)
    assert answer_turn(f, 'supervisor', 'READY', startup=True)['status'] == 'ready'
    f.pump()
    brief = typed(f, 'supervisor')[-1]
    assert brief.startswith('[Resumed task ') and 'Daemon outage (from the logs): last heartbeat ' in brief
    assert '1 events were waiting' in brief and 'held/expired deliveries found: none' in brief


# ------------------------------------------------------------------ the extension's loop (mock Pi, real TS)
BRIDGE = '''import json,sys,time,pathlib
records=pathlib.Path(sys.argv[1])
for line in sys.stdin:
 d=json.loads(line)
 records.open('a').write(json.dumps(d)+'\\n')
 now=time.time()
 if d['kind']=='daemon_beat': answer={'status':'ok','now':now,'beat':now-500,'daemon_running':False,'unfinished':True,
  'last_event':'deliver','last_event_at':now-501,'pending_events':3,'held':['worker-1']}
 else: answer={'status':'registered' if d['kind']=='init' else 'ready' if d['kind']=='startup' else 'recorded'}
 print(json.dumps({'kind':'reply','call_id':d.get('call_id'),'answer':answer}),flush=True)
 if d['kind']=='shutdown': break
'''


def test_extension_daemon_watchdog_contract(tmp_path):
    node = shutil.which('node')
    if not node or not (PI_ROOT / 'node_modules/typebox').exists():
        pytest.skip('installed Pi/Node dependencies needed for the actual TS harness')
    root = Path(__file__).resolve().parents[1]
    extension = tmp_path / 'extension.ts'
    shutil.copy(root / 'cmux_fleet/pi_extension_v2.ts', extension)
    (tmp_path / 'node_modules').symlink_to(PI_ROOT / 'node_modules', target_is_directory=True)
    records = tmp_path / 'bridge-records.jsonl'
    child = tmp_path / 'bridge-fixture.py'
    child.write_text(BRIDGE)
    env = dict(os.environ, CMUX_FLEET_PI_BRIDGE=json.dumps([sys.executable, '-u', str(child), str(records), 'supervisor', '0']),
               CMUX_FLEET_PI_SLOT='supervisor', CMUX_FLEET_PI_MODE='service',
               CMUX_FLEET_DAEMON_WATCH=json.dumps({'check_interval_s': 0.05, 'stale_after_s': 120, 'realert_after_s': 600}))
    result = subprocess.run([node, '--experimental-strip-types', str(root / 'tests/test_pi_daemon_watch.mjs'), str(extension),
                             str(records)], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'daemon watchdog contract passed' in result.stdout
