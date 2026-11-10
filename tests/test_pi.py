"""Pi adapter and mixed Pi/official-Claude-CLI tests; no real services."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_fleet import FakeMgr, FakeRedis, turn
from cmux_fleet import fleet, pi_adapter


def make_fleet(tmp_path, monkeypatch, worker_cli='pi'):
    monkeypatch.setattr(fleet, 'CLAUDE_JSON', tmp_path / 'claude.json')
    run = tmp_path / 'run'
    run.mkdir()
    cfg = dict(run=str(run), python=sys.executable, workers=['worker-1'], mode='service',
               supervisor_cli='pi', worker_cli=worker_cli, pi='/bin/pi', pi_model='local/test',
               pi_home=str(run / 'pi-home'), allow_unsandboxed_pi=True,
               redis_socket='/tmp/cmux-fleet-pi-test.sock', codex_home=str(tmp_path / 'codex-home'))
    path = run / 'config.json'
    fleet.save(path, cfg)
    f = fleet.Fleet(path, FakeRedis(), FakeMgr())
    monkeypatch.setattr(fleet, 'connect', lambda cfg: f.r)
    f.start_all()
    return f


def pi_turn(f, name, text, **extra):
    info = f.agent(name)
    generation = int(info['generation'])
    transcript = pi_adapter.session_dir(f.cfg, name, generation) / 'session.jsonl'
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.touch()
    return dict(kind='turn', agent=name, generation=generation, thread='pi-session-' + str(generation),
                transcript=str(transcript), msg_id=info.get('msg_id', ''), text=text,
                failed=False, aborted=False, ctx_used=1000, ctx_window=100000, **extra)


def callback(f, event):
    fleet.pi_event(f.path, event['agent'], event['generation'], json.dumps(event))
    f.handle(json.loads(f.r.lpop(fleet.EVENTS)))


@pytest.fixture
def pfx(tmp_path, monkeypatch):
    f = make_fleet(tmp_path, monkeypatch)
    for name in f.names:
        callback(f, pi_turn(f, name, 'READY'))
    return f


def spec(rid='r1', to='worker-1'):
    s = {'id': rid, 'payload': {'value': 2}, 'reply': {'required': ['answer'], 'allowed': ['answer']}}
    if to:
        s['to'] = to
    return s


def test_pi_home_shares_only_pi_model_and_auth_not_claude_credentials(tmp_path):
    user = tmp_path / 'user'
    user.mkdir()
    for name in ('auth.json', 'models.json', 'settings.json', 'mcp.json', 'AGENTS.md'):
        (user / name).write_text('{}')
    (user / 'extensions').mkdir()
    home = pi_adapter.prepare_home(tmp_path / 'isolated', user)
    assert sorted(p.name for p in home.iterdir()) == ['auth.json', 'models.json', 'settings.json']
    assert (home / 'auth.json').is_symlink() and (home / 'models.json').is_symlink()
    assert json.loads((home / 'settings.json').read_text())['compaction']['enabled']
    assert not (home / 'settings.json').is_symlink()
    with pytest.raises(FileExistsError):
        pi_adapter.prepare_home(home, user)


def test_pi_argv_and_spawn_isolation(pfx):
    name = 'supervisor'
    argv = pi_adapter.argv(pfx.cfg, name, 3, 'a prompt with spaces', fleet.agent_cwd(pfx.cfg, name),
                           label='supervisor-1.3')
    assert argv[argv.index('--name') + 1] == 'supervisor-1.3'
    assert argv[0] == '/bin/pi' and argv[-2:] == ['--', 'a prompt with spaces']
    for flag in ('--no-context-files', '--no-extensions', '--no-mcp', '--no-skills', '--no-approve'):
        assert flag in argv
    assert '--profile' not in argv and '--api-key' not in argv
    assert argv[argv.index('--session-dir') + 1].endswith('/supervisor/g3')
    assert Path(argv[argv.index('--extension') + 1]).is_file()
    spawn = pfx.mgr.spawned[0]
    assert spawn['metadata']['cli_type'] == 'pi'
    assert 'CODEX_HOME' not in spawn['env_vars']
    assert spawn['env_vars']['PI_CODING_AGENT_DIR'] == pfx.cfg['pi_home']
    command = json.loads(spawn['env_vars']['CMUX_FLEET_PI_CALLBACK'])
    assert command[-4:] == ['pi-event', str(pfx.path), 'supervisor', '0']
    assert not Path(pfx.cfg['codex_home']).exists()
    scope = (fleet.agent_cwd(pfx.cfg, name) / 'AGENTS.md').read_text()
    assert 'without an OS sandbox' in scope and '{{' not in scope


@pytest.mark.parametrize('override', [{'allow_unsandboxed_pi': False}, {'pi_model': ''}])
def test_pi_actor_requires_explicit_model_and_permission(pfx, override):
    with pytest.raises(ValueError):
        pi_adapter.argv(dict(pfx.cfg, **override), 'supervisor', 0, 'hi', pfx.run_dir, label='supervisor-1')


def test_pi_supervisor_dispatches_to_official_claude_cli(tmp_path, monkeypatch):
    f = make_fleet(tmp_path, monkeypatch, worker_cli='claude')
    callback(f, pi_turn(f, 'supervisor', 'READY'))
    f.handle(turn('worker-1', 'READY'))
    f.request(spec(to=None))
    f.pump()
    callback(f, pi_turn(f, 'supervisor', json.dumps({
        'dispatch': [{'to': 'worker-1', 'request_id': 'r1', 'instruction': 'Return the value as answer.'}]})))
    f.pump()
    assert f.agent('worker-1')['state'] == 'busy'
    f.handle(dict(turn('worker-1', '{"answer":2}'), msg_id=f.agent('worker-1')['msg_id']))
    assert json.loads(f.r.get(fleet.RESULT + 'r1'))['result'] == {'answer': 2}
    assert [s['metadata']['cli_type'] for s in f.mgr.spawned] == ['pi', 'claude']
    assert 'CMUX_FLEET_PI_CALLBACK' not in f.mgr.spawned[1]['env_vars']
    argv = fleet.claude_argv(f.path, f.cfg, 'worker-1', 0, 'hi')
    assert argv[0] == 'claude' and '--model' in argv and '--extension' not in argv
    settings = json.loads(Path(argv[argv.index('--settings') + 1]).read_text())
    assert 'claude-stop' in settings['hooks']['Stop'][0]['hooks'][0]['command']
    # Exercise actor routing too: the Claude child is exec'd directly, never through pi.
    called = []
    monkeypatch.setattr(fleet.os, 'chdir', lambda cwd: None)
    monkeypatch.setattr(fleet.os, 'execvp', lambda exe, argv: called.append((exe, argv)))
    monkeypatch.setenv('CLAUDECODE', 'parent-session')
    fleet.actor(f.path, 'worker-1', 'hi')
    assert called[0][0] == 'claude' and 'CLAUDECODE' not in os.environ


@pytest.mark.parametrize('name,generation,label', [
    ('supervisor', 0, 'supervisor-1'), ('supervisor', 1, 'supervisor-1.1'),
    ('worker-1', 0, 'worker-1'), ('worker-1', 2, 'worker-1.2'),
])
def test_pi_actor_uses_fleets_original_slot_generation_label(pfx, monkeypatch, name, generation, label):
    pfx.setf(name, generation=generation, label=label)
    called = []
    monkeypatch.setattr(fleet.os, 'chdir', lambda cwd: None)
    monkeypatch.setattr(fleet.os, 'execvp', lambda exe, argv: called.append(argv))
    for env_key in pi_adapter.environment(pfx.path, pfx.cfg, name, generation):
        monkeypatch.setenv(env_key, 'before-test')
    fleet.actor(pfx.path, name, 'READY')
    argv = called[0]
    assert argv[argv.index('--name') + 1] == pfx.label(name) == label
    assert argv[argv.index('--session-dir') + 1].endswith('/' + name + '/g' + str(generation))


def operator_input(f, text='Ask worker-1 to reply OK', **extra):
    info = f.agent('supervisor')
    return dict(kind='operator_input', agent='supervisor', generation=int(info['generation']),
                thread=info['thread'], text=text, request_id='click1', **extra)


def test_supervisor_ui_input_uses_original_request_and_result_path(pfx):
    pfx.handle(operator_input(pfx))
    ack = json.loads(pfx.r.get('fx:reply:click1'))
    assert ack['status'] == 'accepted' and ack['id'].startswith('ui-')
    rid = ack['id']
    req = pfx.r.hgetall(fleet.REQ + rid)
    assert req['operator'] == '1'
    assert json.loads(req['spec'])['payload'] == {'message': 'Ask worker-1 to reply OK'}
    pfx.pump()
    callback(pfx, pi_turn(pfx, 'supervisor', json.dumps({
        'dispatch': [{'to': 'worker-1', 'request_id': rid, 'instruction': 'Reply with OK in the reply field.'}]})))
    pfx.pump()
    callback(pfx, pi_turn(pfx, 'worker-1', '{"reply":"OK"}'))
    assert json.loads(pfx.r.get(fleet.RESULT + rid))['result'] == {'reply': 'OK'}
    assert '[Operator result ' + rid + ']' in pfx.r.lrange(fleet.key('inbox', 'supervisor'), 0, -1)[0]
    pfx.pump()
    callback(pfx, pi_turn(pfx, 'supervisor', '{"status":"RUNNING","reason":"worker-1 replied OK","dispatch":[],"skip":[]}'))
    assert pfx.agent('supervisor')['state'] == 'idle' and not pfx.paused()
    assert pfx.r.llen(fleet.REQQ) == 0


@pytest.mark.parametrize('change', [{'generation': 99}, {'thread': 'old-thread'}, {'agent': 'worker-1'}, {'text': ''}])
def test_stale_or_invalid_operator_input_is_rejected(pfx, change):
    ev = dict(operator_input(pfx), **change)
    pfx.handle(ev)
    assert json.loads(pfx.r.get('fx:reply:click1'))['status'] == 'rejected'
    assert pfx.r.llen(fleet.REQQ) == 0


def test_task_mode_ui_input_keeps_original_task_lifecycle(pfx):
    pfx.service = False
    pfx.handle(operator_input(pfx, 'Write a short report'))
    task_id = pfx.task()['id']
    assert json.loads(pfx.r.get('fx:reply:click1'))['task_id'] == task_id
    assert pfx.task()['goal'] == 'Write a short report'
    pfx.handle(operator_input(pfx, 'Use two paragraphs'))
    assert pfx.task()['id'] == task_id  # a follow-up, not a replacement task
    assert pfx.r.lrange(fleet.key('inbox', 'supervisor'), 0, -1)[-1].startswith('[Operator instruction]')


def test_pi_request_reply_correlated_and_duplicate_dropped(pfx):
    pfx.request(spec())
    pfx.pump()
    info = pfx.agent('worker-1')
    assert info['msg_id'].startswith('msg-')
    sent = pfx.mgr.sent[-1][1]
    assert '\n' not in sent and '.inbox/' in sent
    old = pi_turn(pfx, 'worker-1', '{"answer":2}')
    callback(pfx, old)
    callback(pfx, old)
    assert len(pfx.r.l[fleet.RESULTS]) == 1
    pfx.request(spec('r2'))
    pfx.pump()
    callback(pfx, old)
    assert pfx.agent('worker-1')['state'] == 'busy'
    assert not pfx.r.get(fleet.RESULT + 'r2')
    callback(pfx, pi_turn(pfx, 'worker-1', '{"answer":2}'))
    assert json.loads(pfx.r.get(fleet.RESULT + 'r2'))['status'] == 'ok'


@pytest.mark.parametrize('field', ['aborted', 'failed'])
def test_pi_partial_error_or_abort_is_not_success(pfx, field):
    pfx.request(spec())
    pfx.pump()
    ev = pi_turn(pfx, 'worker-1', '{"answer":2}')
    ev[field] = True
    callback(pfx, ev)
    assert not pfx.r.get(fleet.RESULT + 'r1')
    assert pfx.agent('worker-1')['empty_tries'] == '1'
    assert pfx.r.llen(fleet.key('inbox', 'worker-1')) == 1


@pytest.mark.parametrize('text', ['', 'working on it', '{"answer":2}'])
def test_pi_startup_fails_closed_without_ready(tmp_path, monkeypatch, text):
    f = make_fleet(tmp_path, monkeypatch)
    callback(f, pi_turn(f, 'supervisor', text))
    assert f.agent('supervisor')['state'] == 'dead' and f.paused()


def test_pi_ready_failure_is_not_accepted(tmp_path, monkeypatch):
    f = make_fleet(tmp_path, monkeypatch)
    ev = pi_turn(f, 'supervisor', 'READY')
    ev['failed'] = True
    callback(f, ev)
    assert f.paused()


def test_pi_context_rotation_resets_metadata_and_rejects_old_generation(pfx, monkeypatch):
    pfx.request(spec())
    pfx.pump()
    event = pi_turn(pfx, 'worker-1', '{"answer":2}')
    event['ctx_used'] = 70000
    callback(pfx, event)
    assert pfx.usage('worker-1') == (70000, 100000)
    pfx.request(spec('r2'))
    monkeypatch.setattr(fleet, 'active_children', lambda *args: pytest.fail('pi must not inspect Codex rollouts'))
    pfx.pump()
    assert pfx.agent('worker-1')['state'] == 'handoff'
    callback(pfx, pi_turn(pfx, 'worker-1', 'handoff written'))
    info = pfx.agent('worker-1')
    assert info['generation'] == '1' and info['state'] == 'starting'
    assert info['transcript'] == '' and info['ctx_used'] == '0'
    old_session = dict(event, kind='pi_session', ctx_used=99999)
    callback(pfx, old_session)
    callback(pfx, event)
    assert pfx.agent('worker-1')['ctx_used'] == '0'
    callback(pfx, pi_turn(pfx, 'worker-1', 'READY'))
    pfx.pump()
    assert pfx.agent('worker-1')['state'] == 'busy'
    callback(pfx, pi_turn(pfx, 'worker-1', '{"answer":2}'))
    assert json.loads(pfx.r.get(fleet.RESULT + 'r2'))['worker'] == 'worker-1.1'


def test_pi_stall_and_late_aborted_callback(pfx):
    pfx.request(spec())
    pfx.pump()
    event = pi_turn(pfx, 'worker-1', '{"answer":2}')
    event['aborted'] = True
    stale = time.time() - 4000
    pfx.setf('worker-1', busy_since=stale)
    os.utime(event['transcript'], (stale, stale))
    pfx.check_stalls(time.time())
    assert json.loads(pfx.r.get(fleet.RESULT + 'r1'))['status'] == 'stalled'
    callback(pfx, event)
    assert len(pfx.r.l[fleet.RESULTS]) == 1


def test_pi_session_event_tracks_startup_activity_without_becoming_ready(tmp_path, monkeypatch):
    f = make_fleet(tmp_path, monkeypatch)
    event = dict(pi_turn(f, 'supervisor', ''), kind='pi_session')
    callback(f, event)
    assert f.agent('supervisor')['state'] == 'starting'
    assert f.agent('supervisor')['transcript'] == event['transcript']
    f.setf('supervisor', busy_since=1)
    f.check_stalls(time.time())
    assert f.agent('supervisor')['state'] == 'starting'


@pytest.mark.parametrize('change', [
    {'transcript': '/tmp/outside.jsonl'}, {'ctx_used': -1}, {'ctx_window': True},
    {'msg_id': None}, {'failed': 'false'}, {'kind': 'stop'}, {'thread': ''},
])
def test_invalid_pi_callback_is_rejected(pfx, change):
    ev = dict(pi_turn(pfx, 'supervisor', 'READY'), **change)
    with pytest.raises(ValueError):
        fleet.pi_event(pfx.path, 'supervisor', 0, json.dumps(ev))
    assert not pfx.r.llen(fleet.EVENTS)


@pytest.fixture(autouse=True)
def temp_redis_root(monkeypatch):
    """start_redis writes owner.json under the default runtime root: never the checkout's .runtime
    (it would create it 755 and later private-Redis tests refuse it). Short /tmp path: socket length limit."""
    root = Path(tempfile.mkdtemp(prefix='cfr-', dir='/tmp')).resolve()
    monkeypatch.setattr(fleet.redis_runtime, 'DEFAULT_ROOT', root / 'redis')
    yield
    shutil.rmtree(root, ignore_errors=True)


def mock_start(tmp_path, monkeypatch):
    from cmux_fleet import cmux_manager
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    mgr = SimpleNamespace(meta_path=tmp_path / 'isolated-state' / 'cmux-fleet.json',
                          session_exists=lambda: False,
                          spawn_window=lambda *a, **kw: SimpleNamespace(success=True))
    monkeypatch.setattr(cmux_manager, 'CmuxSessionManager', lambda name: mgr)
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    monkeypatch.setattr(fleet.shutil, 'which', lambda name: None if name in ('codex', 'claude', 'caffeinate') else '/bin/' + name)
    ran = []
    def run(argv, **kw):
        ran.append(argv)
        return SimpleNamespace(stdout='1.1.0\n', returncode=0)
    monkeypatch.setattr(fleet.subprocess, 'run', run)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: FakeRedis())
    # Provisioning is fake here: do not create persistent Redis directories in the checkout.
    monkeypatch.setattr(fleet.redis_runtime, 'prepare', fleet.redis_runtime.paths)
    args = ['start', '--lifecycle', 'dev_task', '--supervisor-cli', 'pi', '--worker-cli', 'pi', '--pi-model', 'local/test',
            '--allow-unsandboxed-pi', '--allow-development-v2', '--runs-dir', str(tmp_path / 'runs'),
            '--pi-agent-dir', str(tmp_path / 'user-pi'), '--codex-home', str(tmp_path / 'codex-home')]
    return args, ran


def test_all_pi_start_needs_no_codex_binary_profile_or_login(tmp_path, monkeypatch):
    args, ran = mock_start(tmp_path, monkeypatch)
    fleet.main(args)
    cfg = fleet.load(tmp_path / 'latest.json')
    assert cfg['cmux_state_dir'] == str(tmp_path / 'isolated-state')
    assert cfg['supervisor_cli'] == cfg['worker_cli'] == 'pi'
    assert cfg['codex'] is None and not (tmp_path / 'codex-home').exists()
    assert Path(cfg['pi_home']).is_dir()
    assert ran[0] == ['/bin/pi', '--version']
    assert '--unixsocket' in ran[1] and '6379' not in ran[1]
    runtime = Path(cfg['redis_runtime_dir'])
    assert runtime.parent == fleet.redis_runtime.DEFAULT_ROOT
    assert cfg['redis_socket'] == str(runtime / 'redis.sock')
    assert ran[1][ran[1].index('--dir') + 1] == str(runtime)
    assert ran[1][ran[1].index('--pidfile') + 1] == str(runtime / 'redis.pid')
    assert ran[1][ran[1].index('--logfile') + 1] == str(runtime / 'redis.log')


def test_pi_start_requires_permission_before_launching_redis(tmp_path, monkeypatch):
    args, ran = mock_start(tmp_path, monkeypatch)
    args.remove('--allow-unsandboxed-pi')
    with pytest.raises(SystemExit, match='no sandbox'):
        fleet.main(args)
    assert not ran


def test_actual_typescript_extension_lifecycle(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node >= 22.19 is needed for the Pi extension contract test')
    version = subprocess.run([node, '--version'], text=True, capture_output=True, check=True).stdout
    if tuple(map(int, version.strip().lstrip('v').split('.'))) < (22, 19, 0):
        pytest.skip('Node >= 22.19 is needed for the Pi extension contract test')
    output = tmp_path / 'callback records.jsonl'
    script = ('import json,pathlib,sys; raw=sys.stdin.read(); p=pathlib.Path(sys.argv[1]); '
              'p.open("a").write(raw+"\\n"); '
              'print(json.dumps({"status":"accepted","id":"ui-test"}) if json.loads(raw)["kind"]=="operator_input" else "")')
    env = dict(os.environ, CMUX_FLEET_PI_CALLBACK=json.dumps([sys.executable, '-c', script, str(output), 'supervisor', '0']))
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, '--experimental-strip-types', str(root / 'tests/test_pi_extension.mjs'),
                             str(root / 'cmux_fleet/pi_extension.ts'), str(output)],
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'lifecycle contract passed' in result.stdout
