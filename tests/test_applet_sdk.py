"""Process side of the applet SDK ``fleetmux.applet`` (docs/applet-interface.md sections 2, 7 and 8).

The repo's private test Redis and ``tmp_path`` only; the Applet runs in this process with a fake environment.
"""
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from fleetmux import applet as sdk
from fleetmux.applet import core

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(private_redis_v2, tmp_path):
    r = private_redis_v2
    r.flushdb()
    run = tmp_path / 'run' / 'applets' / 'applet-a-1'
    run.mkdir(parents=True)
    return {'FLEETMUX_APPLET_NAME': 'applet-a-1', 'FLEETMUX_APPLET_INTERFACE': '1',
            'FLEETMUX_APPLET_CONFIG': str(tmp_path / 'run' / 'config.json'),
            'FLEETMUX_APPLET_REDIS': r.connection_pool.connection_kwargs['path'],
            'FLEETMUX_APPLET_TOKEN': 'a' * 32, 'FLEETMUX_APPLET_STATE': str(tmp_path / 'state'),
            'FLEETMUX_APPLET_RUN': str(run), 'FLEETMUX_APPLET_HEARTBEAT_S': '0.05'}


@pytest.fixture
def app(env, private_redis_v2):
    a = sdk.Applet.from_env(env, start=False)
    yield a
    a.stopping.set()


def test_public_attributes_for_the_lease_calls(app, env, private_redis_v2):
    assert app.name == 'applet-a-1' and app.token == 'a' * 32
    assert app.config_path == Path(env['FLEETMUX_APPLET_CONFIG'])
    assert app.state_dir == Path(env['FLEETMUX_APPLET_STATE']) and app.run_dir == Path(env['FLEETMUX_APPLET_RUN'])
    assert app.redis.ping() and isinstance(app.stopping, threading.Event) and not app.stopping.is_set()
    assert set(sdk.__all__) == {'INTERFACE', 'Applet', 'Params', 'Lease', 'LeaseDenied', 'LeaseEnded', 'Rejected',
                                'Reply', 'Ruling', 'Escalation', 'ESCALATE_NO_WAIT'}


def test_sdk_exports_the_no_wait_marker():
    """An applet tests ESCALATE_NO_WAIT before calling escalate(wait=False): an older SDK has neither."""
    import inspect
    assert sdk.ESCALATE_NO_WAIT is True
    params = inspect.signature(sdk.Applet.escalate).parameters
    assert 'wait' in params and params['wait'].default is True


@pytest.mark.parametrize('drop, value', [('FLEETMUX_APPLET_TOKEN', None), ('FLEETMUX_APPLET_INTERFACE', '2'),
                                         ('FLEETMUX_APPLET_REDIS', '/nonexistent/redis.sock')])
def test_from_env_exits_78_on_a_bad_environment(env, drop, value, capsys):
    bad = dict(env)
    if value is None:
        bad.pop(drop)
    else:
        bad[drop] = value
    with pytest.raises(SystemExit) as exc:
        sdk.Applet.from_env(bad, start=False)
    assert exc.value.code == 78
    assert 'applet config error' in capsys.readouterr().err


def test_heartbeat_thread_writes_pid_and_hb_at(app, private_redis_v2):
    r = private_redis_v2
    app._parent = None
    app.start()
    first = float(r.hget('fx:applet:applet-a-1', 'hb_at'))
    assert r.hget('fx:applet:applet-a-1', 'pid') == str(os.getpid())
    deadline = __import__('time').time() + 5
    while float(r.hget('fx:applet:applet-a-1', 'hb_at')) == first and __import__('time').time() < deadline:
        __import__('time').sleep(.02)
    assert float(r.hget('fx:applet:applet-a-1', 'hb_at')) > first
    os.kill(os.getpid(), signal.SIGTERM)  # the handler installed by start(): sets stopping, no exit
    assert app.stopping.wait(2)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def test_the_applet_does_not_follow_its_parent_process(app):
    """applets.md 2.3: the applet keeps running when the host daemon is gone (no parent-process watch)."""
    app.start()
    assert not app.stopping.wait(0.4)
    assert 'getppid' not in Path(core.__file__).read_text()
    app.stopping.set()
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def test_lost_redis_ends_the_run_with_exit_zero_after_stale_after_s(env, tmp_path):
    import redis
    import time
    a = sdk.Applet.from_env(dict(env, FLEETMUX_APPLET_STALE_AFTER_S='0.4'), start=False)
    assert a.stale_after_s == 0.4
    a.start()
    class Gone:
        def __getattr__(self, name):
            def call(*args, **kwargs):
                raise redis.ConnectionError('host Redis is gone')
            return call
    a.redis = Gone()  # the host Redis went away while the applet runs
    started = time.time()
    with pytest.raises(SystemExit) as exc:
        a.run(lambda app: 0.05)
    took = time.time() - started
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    assert exc.value.code == 0 and 0.3 <= took < 3  # measured from the last good beat, up to one beat earlier


def test_set_phase_is_capped(app, private_redis_v2):
    app.set_phase('x' * 300)
    assert private_redis_v2.hget('fx:applet:applet-a-1', 'phase') == 'x' * 200


def test_events_in_order_remember_the_position_and_draining_sets_stopping(app, private_redis_v2):
    r = private_redis_v2
    stream = 'fx:applet:applet-a-1:events'
    r.xadd(stream, {'data': json.dumps({'type': 'lease.active', 'at': 1, 'lease': 'L-00000001'})})
    r.xadd(stream, {'data': json.dumps({'type': 'future.thing', 'at': 2})})
    assert [e['type'] for e in app.events(block_s=0.1)] == ['lease.active', 'future.thing']
    assert list(app.events(block_s=0.1)) == []
    r.xadd(stream, {'data': json.dumps({'type': 'host.draining', 'at': 3, 'reason': 'stop', 'deadline_at': 9})})
    again = sdk.Applet(name=app.name, token=app.token, redis=r, config_path=app.config_path,
                       state_dir=app.state_dir, run_dir=app.run_dir)  # a restarted process: same state dir
    assert [e['type'] for e in again.events(block_s=0.1)] == ['host.draining'] and again.stopping.is_set()
    pos = json.loads((app.state_dir / core.EVENTS_POS).read_text())
    assert pos['run'] == str(app.run_dir)
    other_run = sdk.Applet(name=app.name, token=app.token, redis=r, config_path=app.config_path,
                           state_dir=app.state_dir, run_dir=app.run_dir.parent / 'new')
    assert len(list(other_run.events(block_s=0.1))) == 3  # a new host run reads its new stream from 0-0


def test_inbox_yields_what_is_queued_then_returns(app, private_redis_v2):
    r = private_redis_v2
    assert list(app.inbox(block_s=0.1)) == []
    for item in ({'ref': 'a'}, {'ref': 'b'}):
        r.rpush('fx:applet:applet-a-1:inbox', json.dumps(item))
    r.rpush('fx:applet:applet-a-1:inbox', 'not json')
    assert list(app.inbox(block_s=1)) == [{'ref': 'a'}, {'ref': 'b'}]
    assert r.llen('fx:applet:applet-a-1:inbox') == 0


def test_params_hot_keys_live_in_redis_and_are_written_back(env, private_redis_v2, tmp_path):
    r = private_redis_v2
    path = tmp_path / 'app.yaml'
    path.write_text('other: {keep: 1}\napplet:\n  hot: [poll_s, retries]\n  writeback_s: 60\n  poll_s: 5\n'
                    '  retries: 2\n  command: [make, test]\n')
    a = sdk.Applet.from_env(dict(env, FLEETMUX_APPLET_PARAMS_FILE=str(path), FLEETMUX_APPLET_PARAMS_BLOCK='applet'),
                            start=False)
    p = a.params()
    assert a.params() is p and p.writeback_s == 60
    assert r.hgetall('fx:applet:applet-a-1:params') == {'poll_s': '5', 'retries': '2'}
    assert p.get('poll_s') == 5 and p.get('command') == ['make', 'test'] and p.get('missing', 7) == 7
    r.hset('fx:applet:applet-a-1:params', 'retries', '4')  # fleetmux applet param
    assert p.get('retries') == 4
    p.set('poll_s', 9)
    with pytest.raises(KeyError):
        p.set('command', [])
    p.flush()
    import yaml
    data = yaml.safe_load(path.read_text())
    assert data['applet']['poll_s'] == 9 and data['applet']['retries'] == 4 and data['other'] == {'keep': 1}
    stamp = path.stat().st_mtime_ns
    p.flush()  # nothing changed since: the file is not rewritten
    assert path.stat().st_mtime_ns == stamp
    assert sdk.Applet.from_env(env, start=False).params().get('x', 1) == 1  # no params entry: defaults only


@pytest.mark.parametrize('body', [None, 'applet: {poll_s: [\n', 'other: {keep: 1}\n', 'applet: [1, 2]\n', '- 1\n'])
def test_params_that_cannot_be_read_exit_78(env, tmp_path, body, capsys):
    """Section 2.4: a missing or unreadable params file, or a missing block, is a config error (exit 78)."""
    path = tmp_path / 'project.yaml'
    if body is not None:
        path.write_text(body)
    a = sdk.Applet.from_env(dict(env, FLEETMUX_APPLET_PARAMS_FILE=str(path), FLEETMUX_APPLET_PARAMS_BLOCK='applet'),
                            start=False)
    with pytest.raises(SystemExit) as exc:
        a.params()
    assert exc.value.code == 78
    assert capsys.readouterr().err.strip().splitlines()[-1].startswith('applet config error: ')


WRITEBACK_BEFORE = ('# the project file: one YAML, blocks\n'
                    'team:  # the host roster\n'
                    '  workers:\n'
                    '    - {name: fix-1, role: roles/fixer.md}\n'
                    '  poll_s: 1   # same name in another block\n'
                    '\n'
                    '# ---- block: applet\n'
                    'applet:\n'
                    '  hot: [poll_s, targets, mode, retries]\n'
                    '  writeback_s: 60\n'
                    "  poll_s: 5   # seconds between rounds\n"
                    '  targets:\n'
                    '    - a\n'
                    '    - b\n'
                    '  # a comment inside the block\n'
                    '  nested: {poll_s: 3}\n'
                    '  mode: slow\n'
                    '\n'
                    'zeta: {keep: 1}  # the last block\n')


def test_writeback_rewrites_only_the_changed_lines(env, private_redis_v2, tmp_path):
    """Section 2.5: write-back changes only the changed hot keys; comments, order and other blocks stay."""
    path = tmp_path / 'project.yaml'
    path.write_text(WRITEBACK_BEFORE)
    a = sdk.Applet.from_env(dict(env, FLEETMUX_APPLET_PARAMS_FILE=str(path), FLEETMUX_APPLET_PARAMS_BLOCK='applet'),
                            start=False)
    p = a.params()
    p.set('poll_s', 9)
    p.set('targets', ['c'])
    p.set('mode', 'fast')
    p.set('retries', 4)  # hot but absent from the file: added at the end of the block
    p.flush()
    assert path.read_text() == WRITEBACK_BEFORE.replace(
        '  poll_s: 5   # seconds', '  poll_s: 9   # seconds').replace(
        '  targets:\n    - a\n    - b\n', '  targets: [c]\n').replace(
        '  mode: slow\n', '  mode: fast\n  retries: 4\n')


def test_writeback_refuses_a_flow_style_block_and_leaves_the_file(env, private_redis_v2, tmp_path):
    path = tmp_path / 'project.yaml'
    text = '# keep me\napplet: {hot: [poll_s], poll_s: 5}\n'
    path.write_text(text)
    a = sdk.Applet.from_env(dict(env, FLEETMUX_APPLET_PARAMS_FILE=str(path), FLEETMUX_APPLET_PARAMS_BLOCK='applet'),
                            start=False)
    a.params().set('poll_s', 9)
    with pytest.raises(ValueError):
        a.params().flush()
    assert path.read_text() == text


def test_writeback_holds_the_project_yaml_lock(env, private_redis_v2, tmp_path):
    """Section 2.5: every writer of the project YAML takes ``<file>.lock`` first."""
    from fleetmux.applet.lease import file_lock
    path = tmp_path / 'project.yaml'
    path.write_text(WRITEBACK_BEFORE)
    a = sdk.Applet.from_env(dict(env, FLEETMUX_APPLET_PARAMS_FILE=str(path), FLEETMUX_APPLET_PARAMS_BLOCK='applet'),
                            start=False)
    a.params().set('poll_s', 9)
    with file_lock(path):
        t = threading.Thread(target=a.params().flush)
        t.start()
        t.join(0.3)
        assert t.is_alive() and 'poll_s: 5' in path.read_text()
    t.join(5)
    assert 'poll_s: 9' in path.read_text()


def test_run_returns_leases_on_a_normal_stop_only(env, monkeypatch):
    """Section 2.4 "SIGTERM: release leases, exit 0": run() spends at most half of stop_grace_s on it; a crash
    or an explicit exit (75 restart, 78 config) keeps the leases for the next process to re-attach."""
    a = sdk.Applet.from_env(dict(env, FLEETMUX_APPLET_STOP_GRACE_S='12'), start=False)
    assert a.stop_grace_s == 12
    calls = []
    monkeypatch.setattr(a, '_return_leases', lambda wait_s: calls.append(wait_s) or [])
    with pytest.raises(SystemExit) as exc:
        a.run(lambda app: app.stopping.set())
    assert exc.value.code == 0 and calls == [6]
    for step, code in ((lambda app: 1 / 0, 1), (lambda app: sys.exit(75), 75)):
        a.stopping.clear()
        with pytest.raises(SystemExit) as exc:
            a.run(step)
        assert exc.value.code == code and calls == [6]


def test_run_exits_zero_on_stop_and_one_on_an_exception(app):
    calls = []

    def step(a):
        calls.append(1)
        if len(calls) == 3:
            a.stopping.set()
        return 0
    with pytest.raises(SystemExit) as exc:
        app.run(step)
    assert exc.value.code == 0 and len(calls) == 3
    app.stopping.clear()
    with pytest.raises(SystemExit) as exc:
        app.run(lambda a: 1 / 0)
    assert exc.value.code == 1


def test_sdk_imports_only_stdlib_and_redis():
    code = ('import sys; import fleetmux.applet; '
            'print(sorted(m for m in sys.modules if m.split(".")[0] in ("yaml", "fleetmux")))')
    out = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    # fleetmux.awake: the clock the SDK waits on (standard library only, docs/design.md section 35)
    assert out.strip() == ("['fleetmux', 'fleetmux.applet', 'fleetmux.applet.core', 'fleetmux.applet.lease', "
                           "'fleetmux.awake']")


def test_ops_run_the_release_interpreter_not_the_applets(env, tmp_path, monkeypatch):
    """Section 2.3: an applet in its own environment imports only the SDK; ``fleetmux request`` runs on the
    release's interpreter, which the host passes as FLEETMUX_APPLET_PYTHON."""
    from fleetmux.applet import lease as sdk_lease
    seen = []

    class FakePopen:
        def __init__(self, argv, **kw):
            seen.append((argv, kw['env']))

        def communicate(self, data=None, timeout=None):
            return '{"status": "accepted"}\n', ''
    monkeypatch.setattr(sdk_lease.subprocess, 'Popen', FakePopen)
    a = sdk.Applet.from_env(dict(env, FLEETMUX_APPLET_PYTHON='/release/.venv/bin/python', FLEETMUX_APPLET_SDK='/rel'),
                            start=False)
    assert a.submit({'id': 'applet-a-1.1', 'op': 'applet.audit'}) == {'status': 'accepted'}
    argv, child_env = seen[0]
    assert argv[:3] == ['/release/.venv/bin/python', '-m', 'fleetmux.fleet'] and argv[3:5] == ['request', '-']
    assert child_env['PYTHONPATH'].split(os.pathsep)[0] == '/rel'


def test_section_7_signatures_match_the_sdk():
    """docs/applet-interface.md section 7 lists every SDK method with the parameters the code takes."""
    import ast
    import inspect
    text = (ROOT / 'docs' / 'applet-interface.md').read_text()
    code = text.split('## 7. Python SDK surface')[1].split('## 8.')[0].split('```python\n')[1].split('```')[0]

    def names(params):
        return ([p.name for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)],
                [p.name for p in params if p.kind == p.KEYWORD_ONLY])
    seen = 0
    for cls in (n for n in ast.parse(code).body if isinstance(n, ast.ClassDef)):
        for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
            a = fn.args
            doc = ([x.arg for x in a.posonlyargs + a.args], [x.arg for x in a.kwonlyargs])
            raw = inspect.getattr_static(getattr(sdk, cls.name), fn.name)
            real = list(inspect.signature(raw.__func__ if isinstance(raw, classmethod) else raw).parameters.values())
            assert doc == names(real), cls.name + '.' + fn.name
            seen += 1
    assert seen > 15


def test_host_draining_sets_stopping_without_reading_events(app, private_redis_v2):
    """Section 7: ``stopping`` is set on host.draining even when the applet never calls events(); a drain sent
    before this process was started (the stream outlives a restart) is ignored; events() keeps its position."""
    import time
    r = private_redis_v2
    stream = 'fx:applet:applet-a-1:events'
    now = time.time()
    r.hset('fx:applet:applet-a-1', 'started_at', repr(now))  # written by the host when it spawned this process
    r.xadd(stream, {'data': json.dumps({'type': 'host.draining', 'at': now - 100, 'reason': 'stop'})})
    try:
        app.start()
        assert not app.stopping.wait(0.4)
        r.xadd(stream, {'data': json.dumps({'type': 'host.draining', 'at': time.time(), 'reason': 'detach'})})
        assert app.stopping.wait(3)
    finally:
        app.stopping.set()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    assert not (app.state_dir / core.EVENTS_POS).exists()
    assert [e['type'] for e in app.events(block_s=0.1)] == ['host.draining', 'host.draining']


def test_drain_watch_reads_past_a_backlog_in_one_beat(app, private_redis_v2):
    """The events stream holds up to about 1000 entries and outlives a restart: one beat reads all of them."""
    import time
    r = private_redis_v2
    stream = 'fx:applet:applet-a-1:events'
    now = time.time()
    for i in range(250):
        r.xadd(stream, {'data': json.dumps({'type': 'lease.active', 'at': now - 100, 'lease': 'L-%08x' % i})})
    r.xadd(stream, {'data': json.dumps({'type': 'host.draining', 'at': now, 'reason': 'stop'})})
    app._drain_since = now - 1
    app._watch_drain()
    assert app.stopping.is_set()


def test_interface_page_states_the_escalation_fix_and_stop_rules():
    """The behaviour the code has, written where an applet author reads it (applet-interface.md 3.7, 3.8, 9)."""
    text = (ROOT / 'docs' / 'applet-interface.md').read_text()
    esc = text.split('### 3.7 ')[1].split('### 3.8 ')[0]
    row = next(x for x in esc.splitlines() if x.startswith('| `wait_s` |'))
    assert '>= 10' in row and '86400' in row and 'clipped' in row and 'invalid' in row
    assert '`human`' in esc and 'does not widen the open lease' in esc and 'new one' in esc
    fix = text.split('### 3.8 ')[1].split('### 3.9 ')[0]
    assert 'absent or `[]` = not narrowed' in fix
    stop = text.split('## 9. ')[1].split('## 10.')[0]
    assert '`LeaseDenied` from `request_lease`' in stop and '`Rejected` from `Lease.send`, `Lease.release`' in stop
    sdk_doc = text.split('## 7. Python SDK surface')[1].split('## 8.')[0]
    assert 'Blocks until ruled' not in sdk_doc and 'Free;' not in sdk_doc
    assert 'wait: bool = True' in sdk_doc and 'ESCALATE_NO_WAIT' in sdk_doc and '`status` first' in sdk_doc
    assert 'wait=False' in esc and 'wait=False' in text.split('## 9. ')[1].split('## 10.')[0]
    human = next(x for x in esc.splitlines() if x.startswith('| `delete`, `human` |'))
    assert 'shown in `status`' not in human and '`fleetmux applet status <name>`' in human
    assert 'not in the `fleetmux status` tree' in human and 'no channel push' in human
    assert 'first line of `what`' in human and 'sub-fleet' in human
    design = (ROOT / 'docs' / 'applets.md').read_text().split('## 7. Escalation')[1].split('## 8.')[0]
    assert 'wait=False' in design
    row = next(x for x in design.splitlines() if x.startswith('| Delete or move'))
    assert 'Root host: a notice card plus a channel push when the root runs channels' in row
    assert 'Sub-fleet host: a notice card in the sub-fleet supervisor column only' in row
    assert '`fleetmux applet status <name>`' in row and 'not built yet' in row
