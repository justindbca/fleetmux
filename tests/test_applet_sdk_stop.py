"""Stop time and local bounds of the applet SDK against a host that does not answer (docs/applet-interface.md
sections 1.1, 2.4, 7 and 9), the human notice hint (3.7) and a resent fix message (3.8).

The SDK tests use the real op door: ``FLEETMUX_APPLET_PYTHON`` names a stand-in script for the release's
interpreter. While the host is up it answers every op ``ok``; once the test marks it down it waits DOOR_WAIT_S
(the reply wait of ``fleetmux request`` is 30 s) and answers ``pending``, as the command does when the host daemon
does not answer. The repo's private test Redis and ``tmp_path`` only.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from test_runtime_v2 import event, fx, receive, settle  # noqa: F401
from test_applet_lease import NAME, TOKEN, grant, host, op, rec  # noqa: F401
from fleetmux import applet as sdk, applet_host, applet_lease as al, fleet, roster
from fleetmux.applet import Lease, LeaseDenied, Rejected

ROOT = Path(__file__).resolve().parent.parent
APP = 'applet-a-1'
DOOR = '''import json, os, sys, time
spec = json.loads(sys.stdin.read() or "{}")
with open(os.environ["DOOR_LOG"], "a") as f:
    f.write(json.dumps({"pid": os.getpid(), "op": spec.get("op") or "message", "id": spec.get("id")}) + "\\n")
if os.path.exists(os.environ["DOOR_DOWN"]):
    time.sleep(float(os.environ.get("DOOR_WAIT_S", "30")))
    print(json.dumps({"status": "pending"}))
else:
    import redis
    r = redis.Redis(unix_socket_path=os.environ["DOOR_REDIS"], decode_responses=True)
    if spec.get("op") == "applet.lease.release":
        r.hset("fx:applet:" + spec["applet"] + ":lease:" + spec["lease"], "state", "released")
    r.set("fx:result:" + spec["id"], json.dumps({"id": spec["id"], "status": "ok"}))
    print(json.dumps({"status": "accepted", "id": spec["id"]}))
'''


class Door:
    """The stand-in door and its log of calls (one line per ``fleetmux request`` child)."""
    def __init__(self, tmp_path, sock):
        self.path, self.log, self.down = tmp_path / 'door.py', tmp_path / 'door.log', tmp_path / 'door.down'
        self.path.write_text('#!' + sys.executable + '\n' + DOOR)
        self.path.chmod(0o755)
        self.env = {'DOOR_LOG': str(self.log), 'DOOR_DOWN': str(self.down), 'DOOR_REDIS': sock, 'DOOR_WAIT_S': '30'}

    def calls(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def alive(self):
        """Door children still running."""
        out = []
        for call in self.calls():
            try:
                os.kill(call['pid'], 0)
                out.append(call['pid'])
            except ProcessLookupError:
                pass
        return out


@pytest.fixture
def door(private_redis_v2, tmp_path, monkeypatch):
    private_redis_v2.flushdb()
    d = Door(tmp_path, private_redis_v2.connection_pool.connection_kwargs['path'])
    for k, v in d.env.items():
        monkeypatch.setenv(k, v)  # the in-process SDK passes its own environment to the door child
    yield d
    for pid in d.alive():
        os.kill(pid, signal.SIGKILL)


def applet_env(door, tmp_path, **extra):
    run = tmp_path / 'run' / 'applets' / APP
    run.mkdir(parents=True, exist_ok=True)
    env = {'FLEETMUX_APPLET_NAME': APP, 'FLEETMUX_APPLET_INTERFACE': '1',
           'FLEETMUX_APPLET_CONFIG': str(tmp_path / 'run' / 'config.json'),
           'FLEETMUX_APPLET_REDIS': door.env['DOOR_REDIS'], 'FLEETMUX_APPLET_TOKEN': 'a' * 32,
           'FLEETMUX_APPLET_STATE': str(tmp_path / 'state'), 'FLEETMUX_APPLET_RUN': str(run),
           'FLEETMUX_APPLET_HEARTBEAT_S': '0.2', 'FLEETMUX_APPLET_PYTHON': str(door.path)}
    env.update(extra)
    return env


def wait_for(cond, limit=10.0):
    end = time.time() + limit
    while not cond():
        assert time.time() < end, 'timed out'
        time.sleep(0.05)


def gone(door, limit=2.0):
    """The door children the SDK started are all gone (killed when cut, not left running)."""
    wait_for(lambda: not door.alive(), limit)
    return True


# ====================================================================== stop (section 2.4)
DRIVER = '''import os, sys, time
from pathlib import Path
from fleetmux.applet import Applet

app = Applet.from_env()


def step(a):
    if os.environ["MODE"] == "inflight":   # an op that waits on the silent door when SIGTERM comes
        a.notify("round done")
    else:                                    # a notify sent while the stop is under way
        Path(os.environ["READY"]).write_text("x")
        while not a.stopping.is_set():
            time.sleep(0.05)
        a.notify("stopping now")
    return 1


app.run(step)
'''


@pytest.mark.parametrize('mode', ['inflight', 'during'])
def test_sigterm_exits_well_inside_the_grace_when_the_host_is_silent(door, tmp_path, mode):
    """SIGTERM while an op waits on a door that does not answer, or a notify sent after SIGTERM: the call returns
    ``stopping`` at once, the door child is killed, run() treats it as a stop (exit 0), far inside the grace."""
    driver, ready = tmp_path / 'driver.py', tmp_path / 'ready'
    driver.write_text(DRIVER)
    door.down.write_text('x')
    env = dict(os.environ, **applet_env(door, tmp_path, FLEETMUX_APPLET_STOP_GRACE_S='30'), MODE=mode,
               READY=str(ready), PYTHONPATH=str(ROOT))
    p = subprocess.Popen([sys.executable, str(driver)], cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        if mode == 'inflight':
            wait_for(lambda: door.calls() or p.poll() is not None)
            time.sleep(1.0)  # one second into the door call
        else:
            wait_for(lambda: ready.exists() or p.poll() is not None)
        assert p.poll() is None, p.communicate()
        t0 = time.monotonic()
        p.send_signal(signal.SIGTERM)
        try:
            out, err = p.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
        took = time.monotonic() - t0
    finally:
        if p.poll() is None:
            p.kill()
            p.communicate()
    assert p.returncode == 0, err
    assert took < 5.0, mode + ': ' + str(round(took, 1)) + ' s to exit'
    assert [c['op'] for c in door.calls()] == (['applet.notify'] if mode == 'inflight' else [])
    assert gone(door)


def test_a_call_made_after_stopping_is_not_sent(door, tmp_path):
    app = sdk.Applet.from_env(applet_env(door, tmp_path), start=False)
    app.stopping.set()
    t0 = time.time()
    with pytest.raises(Rejected) as exc:
        app.notify('x')
    assert exc.value.reason == 'stopping'
    with pytest.raises(LeaseDenied) as denied:
        app.request_lease(job='j', purpose='p')
    assert denied.value.reason == 'stopping'
    lease = Lease(app, {'id': 'L-1', 'slot': 'worker-1'})
    assert lease.renew(need_s=60, progress='p') is False
    for call in (lambda: lease.send({}, instruction='x', required=['status']), lambda: lease.release(),
                 lambda: app.escalate(cls='plan', what='w', why='y')):
        with pytest.raises(Rejected) as exc:
            call()
        assert exc.value.reason == 'stopping'
    assert time.time() - t0 < 1 and door.calls() == []


def test_escalate_without_waiting_after_stopping_is_not_sent(door, tmp_path):
    """escalate(wait=False) uses the same door: called after ``stopping`` it sends nothing and returns at once."""
    app = sdk.Applet.from_env(applet_env(door, tmp_path), start=False)
    app.stopping.set()
    t0 = time.time()
    with pytest.raises(Rejected) as exc:
        app.escalate(cls='human', what='w', why='y', options=['a', 'b'], wait=False)
    assert exc.value.reason == 'stopping' and time.time() - t0 < 1 and door.calls() == []


def test_escalate_without_waiting_is_cut_by_stopping(door, tmp_path):
    """A stop while escalate(wait=False) waits on a door that does not answer kills the door child at once."""
    app = sdk.Applet.from_env(applet_env(door, tmp_path), start=False)
    door.down.write_text('x')
    threading.Timer(1.0, app.stopping.set).start()
    t0 = time.time()
    with pytest.raises(Rejected) as exc:
        app.escalate(cls='plan', what='w', why='y', wait=False)
    assert exc.value.reason == 'stopping' and time.time() - t0 < 5
    assert [c['op'] for c in door.calls()] == ['applet.escalate'] and gone(door)


def _open_lease(r):
    r.sadd('fx:applet:' + APP + ':leases', 'L-1')
    r.hset('fx:applet:' + APP + ':lease:L-1', mapping={'id': 'L-1', 'slot': 'worker-1', 'state': 'active'})


def test_exit_steps_still_send_audits_and_return_leases(door, tmp_path, private_redis_v2):
    """The bounded exit steps of run() keep using the door after ``stopping`` is set."""
    _open_lease(private_redis_v2)
    app = sdk.Applet.from_env(applet_env(door, tmp_path, FLEETMUX_APPLET_STOP_GRACE_S='20'), start=False)
    app.audit('job.done', n=1)
    t0 = time.time()
    with pytest.raises(SystemExit) as exc:
        app.run(lambda a: a.stopping.set())
    assert exc.value.code == 0 and time.time() - t0 < 10
    assert sorted(c['op'] for c in door.calls()) == ['applet.audit', 'applet.lease.release']
    assert private_redis_v2.hget('fx:applet:' + APP + ':lease:L-1', 'state') == 'released'


def test_exit_steps_are_bounded_and_leave_no_door_child_against_a_silent_host(door, tmp_path, private_redis_v2):
    """Queued audit records (a quarter of the grace) and the lease return (half of it) give up at their bounds,
    and the door children they started are killed then, not left running after the exit."""
    _open_lease(private_redis_v2)
    door.down.write_text('x')
    app = sdk.Applet.from_env(applet_env(door, tmp_path, FLEETMUX_APPLET_STOP_GRACE_S='2'), start=False)
    app.audit('job.done', n=1)
    app.audit('job.more', n=2)
    t0 = time.time()
    with pytest.raises(SystemExit) as exc:
        app.run(lambda a: a.stopping.set())
    took = time.time() - t0
    assert exc.value.code == 0 and 1.2 < took < 2.0
    assert gone(door)
    assert sorted(c['op'] for c in door.calls()) == ['applet.audit', 'applet.lease.release']


# ====================================================================== local bounds (sections 1.1, 7, 9)
CALLS = {
    'request_lease': lambda app, w: app.request_lease(job='j', purpose='p', wait_s=w),
    'send': lambda app, w: Lease(app, {'id': 'L-1', 'slot': 'worker-1'}).send(
        {}, instruction='x', required=['status'], wait_s=w),
    'renew': lambda app, w: Lease(app, {'id': 'L-1', 'slot': 'worker-1'}).renew(need_s=60, progress='p', wait_s=w),
    'release': lambda app, w: Lease(app, {'id': 'L-1', 'slot': 'worker-1'}).release(wait_s=w),
    'notify': lambda app, w: app.notify('x', wait_s=w),
}


def _expect_no_answer(name, call):
    if name == 'renew':
        assert call() is False
        return
    with pytest.raises(LeaseDenied if name == 'request_lease' else Rejected) as exc:
        call()
    assert (exc.value.by, exc.value.reason) == ('program', 'no_answer')


@pytest.mark.parametrize('name', sorted(CALLS))
def test_each_call_gives_up_at_its_bound_against_a_silent_host(door, tmp_path, name):
    door.down.write_text('x')
    app = sdk.Applet.from_env(applet_env(door, tmp_path), start=False)
    t0 = time.time()
    _expect_no_answer(name, lambda: CALLS[name](app, 1.0))
    assert 0.9 < time.time() - t0 < 3.0
    assert len(door.calls()) == 1 and gone(door)


@pytest.mark.parametrize('name', sorted(CALLS))
def test_each_call_gives_up_at_its_bound_when_the_door_took_it_but_no_result_comes(door, tmp_path, name,
                                                                                   monkeypatch):
    app = sdk.Applet.from_env(applet_env(door, tmp_path), start=False)
    monkeypatch.setattr(app, 'submit', lambda spec: {'status': 'accepted'})  # no daemon ever answers
    t0 = time.time()
    _expect_no_answer(name, lambda: CALLS[name](app, 0.5))
    assert 0.4 < time.time() - t0 < 2.0


@pytest.mark.parametrize('name', sorted(CALLS))
def test_default_bounds_come_from_the_waits_environment(door, tmp_path, name):
    door.down.write_text('x')
    app = sdk.Applet.from_env(applet_env(door, tmp_path, FLEETMUX_APPLET_WAITS=json.dumps({name: 1})),
                              start=False)
    t0 = time.time()
    _expect_no_answer(name, lambda: CALLS[name](app, None))
    assert 0.9 < time.time() - t0 < 3.0 and gone(door)


def test_sdk_default_bounds_without_the_environment(door, tmp_path):
    app = sdk.Applet.from_env(applet_env(door, tmp_path), start=False)
    assert app.waits == {'request_lease': 660, 'renew': 660, 'send': 7560, 'release': 60, 'notify': 60}


@pytest.mark.parametrize('bad', ['[1]', '{"send": 0}', '{"other": 5}', 'x'])
def test_a_bad_waits_environment_exits_78(door, tmp_path, bad):
    with pytest.raises(SystemExit) as exc:
        sdk.Applet.from_env(applet_env(door, tmp_path, FLEETMUX_APPLET_WAITS=bad), start=False)
    assert exc.value.code == 78


WORKERS = 'workers:\n  - {name: worker-1}\n'


def test_roster_waits_default_to_the_hosts_own_bounds_and_reach_the_environment(tmp_path):
    [entry] = roster.applets(WORKERS + 'applets:\n  - {name: applet-a-1, command: [x], '
                             'lease: {decision_wait_s: 900, ttl_max_s: 3600}, review: {timeout_s: 600}, '  # review: an older key, ignored
                             'waits: {notify: 20}}\n', tmp_path)
    assert entry['waits'] == {'request_lease': 960, 'renew': 960, 'send': 3960, 'release': 60, 'notify': 20}
    env = applet_host.environment({'run': str(tmp_path), 'redis_socket': '/s', 'python': '/p'}, entry, 'tok', base={})
    assert json.loads(env['FLEETMUX_APPLET_WAITS']) == entry['waits']


@pytest.mark.parametrize('row, reason', [
    ('waits: {send: 0}', 'waits.send must be a number > 0'),
    ('waits: {other: 5}', 'waits must be a mapping with keys'),
    ('waits: 5', 'waits must be a mapping with keys'),
])
def test_roster_refuses_bad_waits(tmp_path, row, reason):
    entries, errors = roster.applet_entries([dict(name='applet-a-1', command=['x'], **_yaml(row))], tmp_path,
                                            ['worker-1'])
    assert entries == [] and any(reason in e for e in errors), errors


def _yaml(text):
    import yaml
    return yaml.safe_load(text)


# ====================================================================== the human notice hint (section 3.7)
@pytest.mark.parametrize('options, hint', [
    ([], 'Answer with `fleetmux applet answer h.1 approve|reject --config {cfg}`.'),
    (['deploy', 'hold'], 'Answer with `fleetmux applet answer h.1 approve --choice <n> --config {cfg}` '
                         '(n: 0 = deploy, 1 = hold) or `fleetmux applet answer h.1 reject --config {cfg}`.'),
    (['deploy\nnow', 'x' * 80], 'Answer with `fleetmux applet answer h.1 approve --choice <n> --config {cfg}` '
                                 '(n: 0 = deploy now, 1 = ' + 'x' * 60 + ') or '
                                 '`fleetmux applet answer h.1 reject --config {cfg}`.'),
])
def test_human_notice_shows_the_answer_command_that_works(host, monkeypatch, options, hint):
    """The command names the host's config: it works from any shell, not only one whose FLEETMUX_HOME points at
    this fleet."""
    from fleetmux import tabs
    seen = []
    monkeypatch.setattr(tabs, 'notice', lambda f, text, label: seen.append(text))
    op(host, 'applet.escalate', 'h.1', **{'class': 'human'}, what='Deploy batch 7', why='WAIT_USER step',
       options=options)
    [text] = seen
    hint = hint.format(cfg=Path(host.cfg['run']) / 'config.json')
    assert text.endswith(hint) and '\n' not in text, text


def test_human_notice_is_kept_in_full_for_applet_notices(host, monkeypatch):
    """The card carries one line; ``fleetmux applet notices`` must give the owner the whole request and the
    answer command."""
    from fleetmux import tabs
    monkeypatch.setattr(tabs, 'notice', lambda f, text, label: None)
    op(host, 'applet.escalate', 'h.2', **{'class': 'human'}, what='Deploy batch 7\nthen restart the web tier',
       why='WAIT_USER step', options=['deploy', 'hold'], job='rollout.start')
    [item] = al.notices(host.r, NAME)
    assert item['id'] == 'h.2' and item['job'] == 'rollout.start' and item['class'] == 'human'
    text = item['text']
    assert text.startswith('[applet escalation h.2] needs the human owner: ')
    assert 'then restart the web tier' in text and 'WAIT_USER step' in text and '1 = hold' in text
    assert '`fleetmux applet answer h.2 approve --choice <n>' in text


# ====================================================================== a resent fix message (section 3.8)
FIELDS = ('inflight', 'messages', 'last_message')


@pytest.mark.parametrize('when', ['running', 'answered'])
def test_resending_a_fix_message_with_the_same_id_changes_nothing(host, when):
    """``screen`` must find a used message id before it writes the lease record: a resend (an applet unsure whether
    the first got through) is a duplicate whose first result stays, not a second entry in ``inflight`` and not a
    second delivery. ``answered``: the worker replied; the reply went straight back (no review)."""
    lid = grant(host)
    spec = dict(id='m.1', to='worker-1', lease=lid, applet=NAME, token=TOKEN, instruction='Fix only what broke.',
                payload={'file': 'src/app.py'}, reply={'required': ['status']},
                fix={'dir': '/srv/demo/work', 'base': 'abc123', 'head': None})
    event(host, dict(kind='request', request_id='cmd-m.1', spec=dict(spec)))
    host.pump()
    if when == 'answered':
        settle(host, 'worker-1', receive(host, 'worker-1'), {'status': 'done'})
        assert json.loads(host.r.get(fleet.RESULT + 'm.1'))['status'] == 'ok'
    before = rec(host, lid)
    event(host, dict(kind='request', request_id='cmd-again-m.1', spec=dict(spec)))
    answer = json.loads(host.r.xrange(host.v2.prefix + ':reply:cmd-again-m.1')[-1][1]['data'])
    assert answer['status'] == 'rejected' and str(answer['reason']).startswith('duplicate'), answer
    after = rec(host, lid)
    assert {k: after.get(k) for k in FIELDS} == {k: before.get(k) for k in FIELDS}
    host.pump()
    assert not host.v2.depth('worker-1')  # not delivered again
