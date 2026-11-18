"""Lease commands of ``fleetmux applet`` and the SDK lease calls (docs/applet-interface.md sections 5 and 7).

The same v2 service fleet as ``test_applet_lease``; the daemon side of a command runs in the test (no daemon
process), and the SDK's op door posts on the test fleet's bus instead of running ``fleetmux request``.
"""
import argparse
import json
import threading

import pytest
import redis

from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_runtime_v2 import event, fx  # noqa: F401
from test_applet_lease import NAME, TOKEN, ack, decide, events, grant, host, message, rec  # noqa: F401
from fleetmux import applet_cli, applet_lease as al, fleet
from fleetmux.applet import Lease, LeaseDenied, LeaseEnded, Rejected, Reply, Review, Ruling  # noqa: F401
from fleetmux.applet.lease import LeaseCalls


@pytest.fixture
def cli(host, monkeypatch):
    real = al.ask
    monkeypatch.setattr(al, 'ask', lambda r, req, wait=None: real(r, req, wait=2, sleep=lambda s: al.tick(host)))
    monkeypatch.setattr(applet_cli, 'declared', lambda cfg: {e['name']: e for e in host.applet_specs})
    return host


def run(f, cmd, **args):
    return applet_cli.COMMANDS[cmd].run(f.cfg, f.r, argparse.Namespace(wait=2, **args))


def test_commands_are_registered():
    assert {'pause', 'resume', 'leases', 'revoke', 'answer'} <= set(applet_cli.COMMANDS)
    p = applet_cli.parser()
    args = p.parse_args(['answer', 'a.1', 'approve', '--choice', '1', '--note', 'go'])
    assert (args.op_id, args.decision, args.choice, args.note) == ('a.1', 'approve', 1, 'go')


def test_pause_resume_leases_revoke(cli):
    code, out = run(cli, 'pause', name=NAME, note='hold')
    assert code == applet_cli.OK and cli.r.hget(al.hkey(NAME), 'paused') == '1'
    assert run(cli, 'pause', name='applet-other-1', note='')[0] == applet_cli.REJECTED
    run(cli, 'resume', name=NAME, note='')
    lid = grant(cli)
    code, out = run(cli, 'leases', all=False)
    assert code == applet_cli.OK and [x['id'] for x in out['leases']] == [lid]
    code, out = run(cli, 'revoke', lease=lid, note='operator stop')
    assert code == applet_cli.OK and rec(cli, lid)['end_reason'] == 'operator'
    assert run(cli, 'revoke', lease=lid, note='again')[1]['reason'] == 'lease_not_active'
    al.tick(cli)
    assert run(cli, 'leases', all=False)[1]['leases'] == []
    assert [x['id'] for x in run(cli, 'leases', all=True)[1]['leases']] == [lid]


def test_answer_only_for_human_escalations(cli):
    from test_applet_lease import op
    op(cli, 'applet.escalate', 'e.1', **{'class': 'delete'}, what='Remove build/', why='stale',
       paths=['/srv/build'], options=['remove', 'keep'])
    assert run(cli, 'answer', op_id='e.1', decision='approve', choice=None, note='')[0] == applet_cli.REJECTED
    code, out = run(cli, 'answer', op_id='e.1', decision='approve', choice=0, note='ok')
    assert code == applet_cli.OK
    assert json.loads(cli.r.get(fleet.RESULT + 'e.1'))['choice'] == 0
    assert run(cli, 'answer', op_id='e.1', decision='reject', choice=None, note='')[0] == applet_cli.REJECTED


def test_status_tree_marks_the_leased_slot(cli):
    lid = grant(cli)
    out = {'slots': {'worker-1': {}, 'worker-2': {}}}
    al.annotate(cli.r, out)
    assert out['slots']['worker-1']['applet_lease']['id'] == lid
    assert out['slots']['worker-1']['applet_lease']['holder'] == NAME and 'applet_lease' not in out['slots']['worker-2']


# ====================================================================== SDK lease calls
class FakeApplet(LeaseCalls):
    """The public attributes the mixin uses, and an op door that posts on the test fleet's bus."""
    def __init__(self, f, then=None):
        self.name, self.token, self.redis, self.config_path = NAME, TOKEN, f.r, f.path
        self.state_dir, self.stopping, self.f, self.then = None, threading.Event(), f, then or (lambda spec: None)

    def submit(self, spec):  # noqa: D401  (replaces the class's door)
        event(self.f, dict(kind='request', request_id='cmd-' + spec['id'], spec=spec))
        self.then(spec)
        return ack(self.f, spec['id'])


def test_sdk_request_send_renew_release(host):
    def supervisor(spec):
        if spec.get('op') == 'applet.lease.request':
            decide(host, {'id': spec['id'], 'decision': 'grant', 'slot': 'worker-1'})
            al.tick(host)
        elif spec.get('op') == 'applet.lease.renew':
            decide(host, {'id': spec['id'], 'decision': 'grant', 'ttl_s': 120})
        elif spec.get('to') and not host.r.get(fleet.RESULT + spec['id']):  # refused: nothing to deliver
            from test_runtime_v2 import receive, settle
            host.pump()
            settle(host, 'worker-1', receive(host, 'worker-1'), {'status': 'done'})
    app = FakeApplet(host, supervisor)
    lease = app.request_lease(job='job-1', purpose='Fix the failing test', ttl_s=600)
    assert isinstance(lease, Lease) and lease.slot == 'worker-1'
    assert lease.remaining_s() >= 0 and lease.expires_at == 1000.0 + 600  # read from the record once active
    al.tick(host)
    assert [x.id for x in app.leases()] == [lease.id]
    reply = lease.send({'file': 'a.py'}, instruction='Fix only the failing test.', required=['status'])
    assert (reply.status, reply.result) == ('ok', {'status': 'done'})
    assert lease.renew(need_s=120, progress='half done') is True and lease.expires_at == 1000.0 + 120
    with lease:
        pass  # leaving the block releases
    assert rec(host, lease.id)['state'] == 'released'
    with pytest.raises(LeaseEnded):
        lease.send({}, instruction='One more change please.', required=['status'])


def test_sdk_denied_ruled_notified_audited(host):
    app = FakeApplet(host, lambda spec: decide(host, {'id': spec['id'], 'decision': 'deny', 'note': 'busy day'})
                     if spec.get('op') == 'applet.lease.request' else
                     decide(host, {'id': spec['id'], 'decision': 'approve', 'choice': 1, 'note': 'hold it'})
                     if spec.get('op') == 'applet.escalate' else None)
    with pytest.raises(LeaseDenied) as exc:
        app.request_lease(job='job-1', purpose='Fix the failing test')
    assert (exc.value.by, exc.value.reason, exc.value.note) == ('supervisor', 'supervisor', 'busy day')
    ruling = app.escalate(cls='plan', what='Drop branch x', why='fails alone', options=['drop', 'hold'])
    assert (ruling.decision, ruling.choice, ruling.by) == ('approve', 1, 'supervisor')
    app.notify('batch 7 merged')
    app.audit('batch.bounced', branch='feat/x')
    assert app._flush_audits(5) == 0
    assert [x for _, x in host.r.xrange(host.v2.bus.audit_key) if x.get('event') == 'applet.event']
    with pytest.raises(ValueError):
        app.audit('Bad Event')


def test_answer_command_rules_a_human_escalation(cli):
    from test_applet_lease import op
    op(cli, 'applet.escalate', 'h.1', **{'class': 'human'}, what='Deploy batch 7', why='WAIT_USER step')
    code, out = run(cli, 'answer', op_id='h.1', decision='approve', choice=None, note='go')
    assert code == applet_cli.OK
    res = json.loads(cli.r.get(fleet.RESULT + 'h.1'))
    assert (res['decision'], res['by']) == ('approve', 'human')


def test_notices_shows_the_full_text(cli):
    from test_applet_lease import op
    text = 'Ready. Run: fleetmux applet answer x.1 approve --note ok. ' + 'y' * 800
    op(cli, 'applet.notify', 'n.1', text='first')
    op(cli, 'applet.notify', 'n.2', text=text)
    code, out = run(cli, 'notices', name=NAME, last=1)
    assert code == applet_cli.OK and [(n['id'], n['text']) for n in out['notices']] == [('n.2', text)]
    assert [n['id'] for n in run(cli, 'notices', name=NAME, last=10)[1]['notices']] == ['n.1', 'n.2']
    assert run(cli, 'notices', name='applet-other-1', last=10)[0] == applet_cli.REJECTED
    assert applet_cli.parser().parse_args(['notices', NAME, '--last', '3']).last == 3


def test_sdk_escalate_returns_its_id_and_a_restart_reattaches(host):
    """escalate() takes the op id the applet chose (persist it first) and returns it on the Ruling. A process that
    stopped while waiting finds the escalation with escalations() and claims the ruling with Escalation.ruling()."""
    app = FakeApplet(host, then=lambda spec: app.stopping.set())  # it stops while the human has not answered
    with pytest.raises(Rejected) as exc:
        app.escalate(cls='human', what='Deploy batch 7', why='WAIT_USER step', options=['deploy', 'hold'],
                     op_id=NAME + '.esc-1')
    assert exc.value.reason == 'stopping'
    nxt = FakeApplet(host)
    pending = nxt.escalations()
    assert [(e.id, e.cls, e.status, e.options) for e in pending] == [
        (NAME + '.esc-1', 'human', 'awaiting_human', ['deploy', 'hold'])]
    al.operator(host, {'action': 'answer', 'id': NAME + '.esc-1', 'decision': 'approve', 'choice': 0, 'note': 'ok'})
    ruling = pending[0].ruling()
    assert (ruling.id, ruling.decision, ruling.choice, ruling.by) == (NAME + '.esc-1', 'approve', 0, 'human')
    assert nxt.escalations()[0].status == 'ruled'
    fresh = FakeApplet(host, lambda spec: decide(host, {'id': spec['id'], 'decision': 'reject', 'note': 'no'}))
    r = fresh.escalate(cls='plan', what='Drop x', why='fails alone')
    assert r.decision == 'reject' and r.id.startswith(NAME + '.') and r.by == 'supervisor'


def test_sdk_escalate_has_a_local_deadline(host, monkeypatch):
    """With wait_s the SDK stops waiting at wait_s plus a grace even when no host daemon answers."""
    import time
    from fleetmux.applet import lease as sdk_lease
    monkeypatch.setattr(sdk_lease, 'ESCALATE_GRACE_S', 0.1)
    app = FakeApplet(host)
    app.submit = lambda spec: {'status': 'accepted'}  # the door took it; no daemon ever rules
    started = time.time()
    with pytest.raises(Rejected) as exc:
        app.escalate(cls='plan', what='Drop x', why='fails alone', wait_s=0.2)
    assert exc.value.reason == 'no_decision' and time.time() - started < 2


def test_sdk_escalate_without_waiting_returns_the_op_id(host, monkeypatch):
    """escalate(wait=False) returns the op id once the host opened the escalation, without waiting for the
    ruling. The same op id again is safe while open and after the ruling: no second record, no second notice."""
    import time
    from fleetmux import tabs
    seen = []
    monkeypatch.setattr(tabs, 'notice', lambda f, text, label: seen.append(text))
    app = FakeApplet(host)
    rid = NAME + '.esc-nw'
    started = time.time()
    out = app.escalate(cls='human', what='Approve request 42 r3', why='WAIT_USER', options=['approve', 'risk'],
                       op_id=rid, wait=False)
    assert out == rid and time.time() - started < 2 and not app.stopping.is_set()
    assert host.r.hget(al.esc_key(NAME, rid), 'status') == 'awaiting_human' and len(seen) == 1
    assert app.escalate(cls='human', what='Approve request 42 r3', why='WAIT_USER', options=['approve', 'risk'],
                        op_id=rid, wait=False) == rid
    assert len(seen) == 1 and [e.id for e in app.escalations()] == [rid]
    al.operator(host, {'action': 'answer', 'id': rid, 'decision': 'approve', 'choice': 1, 'note': 'ok'})
    esc = app.escalations()[0]
    assert esc.status == 'ruled'
    ruling = esc.ruling(wait_s=0)
    assert (ruling.id, ruling.decision, ruling.choice, ruling.by) == (rid, 'approve', 1, 'human')
    assert app.escalate(cls='human', what='Approve request 42 r3', why='WAIT_USER', options=['approve', 'risk'],
                        op_id=rid, wait=False) == rid
    assert len(seen) == 1 and app.escalations()[0].status == 'ruled'


def test_sdk_escalate_without_waiting_reports_a_program_refusal(host, monkeypatch):
    """A host that does not know class human (40a8086) refuses it rejected / program / invalid; escalate(wait=False)
    raises that Rejected at once. So does a bad op id at the bus door."""
    import time
    monkeypatch.setattr(al, 'CLASSES', ('plan', 'scope', 'access', 'delete'))
    app = FakeApplet(host)
    started = time.time()
    with pytest.raises(Rejected) as exc:
        app.escalate(cls='human', what='Approve request 42 r3', why='WAIT_USER', op_id=NAME + '.old', wait=False)
    assert (exc.value.by, exc.value.reason) == ('program', 'invalid') and 'class must be one of' in exc.value.note
    assert time.time() - started < 2 and app.escalations() == []
    with pytest.raises(Rejected) as exc:
        app.escalate(cls='plan', what='x', why='y', op_id='.bad', wait=False)
    assert (exc.value.by, exc.value.reason) == ('program', 'invalid')


def test_sdk_claim_after_restart_without_waiting(host):
    """A later round or a restarted process claims the ruling by op id: branch on Escalation.status first;
    ruling(wait_s=0) answers at once for a ruled one and raises no_decision while open or after a host timeout."""
    rid = FakeApplet(host).escalate(cls='human', what='Approve request 7 r1', why='WAIT_USER', op_id=NAME + '.c1',
                                    wait=False)
    nxt = FakeApplet(host)
    esc = {e.id: e for e in nxt.escalations()}[rid]
    assert esc.status == 'awaiting_human'
    with pytest.raises(Rejected) as exc:
        esc.ruling(wait_s=0)
    assert exc.value.reason == 'no_decision'
    host.clock['now'] += 10 ** 6
    al.tick(host)  # no wait_s: no deadline, still open
    assert nxt.escalations()[0].status == 'awaiting_human'
    al.operator(host, {'action': 'answer', 'id': rid, 'decision': 'reject', 'note': 'wrong target'})
    ruling = nxt.escalations()[0].ruling(wait_s=0)
    assert (ruling.decision, ruling.choice, ruling.by, ruling.note) == ('reject', None, 'human', 'wrong target')
    tid = nxt.escalate(cls='plan', what='Drop x', why='fails alone', wait_s=10, op_id=NAME + '.t1', wait=False)
    host.clock['now'] += 11
    al.tick(host)
    esc = {e.id: e for e in nxt.escalations()}[tid]
    assert esc.status == 'timeout'
    with pytest.raises(Rejected) as exc:
        esc.ruling(wait_s=0)
    assert exc.value.reason == 'no_decision'


def test_sdk_escalate_without_waiting_on_a_spent_op_id(host):
    """An op id is used once. A refused id stays refused (the same refusal again); an id whose record and result
    are gone (kept time over; the host still knows the id) is Rejected duplicate instead of looking open."""
    app = FakeApplet(host)
    host.r.hset(al.hkey(NAME), 'state', 'starting')
    for _ in range(2):
        with pytest.raises(Rejected) as exc:
            app.escalate(cls='human', what='Approve request 9 r1', why='WAIT_USER', op_id=NAME + '.s1', wait=False)
        assert exc.value.reason == 'applet_not_up'
        host.r.hset(al.hkey(NAME), 'state', 'up')
    rid = app.escalate(cls='human', what='Approve request 9 r2', why='WAIT_USER', op_id=NAME + '.s2', wait=False)
    al.operator(host, {'action': 'answer', 'id': rid, 'decision': 'approve', 'note': ''})
    host.r.delete(al.esc_key(NAME, rid), fleet.RESULT + rid)  # both kept 7 days after the ruling
    with pytest.raises(Rejected) as exc:
        app.escalate(cls='human', what='Approve request 9 r2', why='WAIT_USER', op_id=rid, wait=False)
    assert (exc.value.by, exc.value.reason) == ('program', 'duplicate')


def test_sdk_escalate_without_waiting_is_bounded_and_honours_stopping(host, monkeypatch):
    """When the door took the op but the host shows neither a record nor a result, wait=False still returns the
    id after ESCALATE_ACCEPT_S; a stop while it looks is Rejected stopping."""
    import time
    from fleetmux.applet import lease as sdk_lease
    monkeypatch.setattr(sdk_lease, 'ESCALATE_ACCEPT_S', 0.3)
    app = FakeApplet(host)
    app.submit = lambda spec: {'status': 'accepted', 'id': spec['id']}
    started = time.time()
    assert app.escalate(cls='plan', what='x', why='y', op_id=NAME + '.b1', wait=False) == NAME + '.b1'
    assert time.time() - started < 2
    app.stopping.set()
    with pytest.raises(Rejected) as exc:
        app.escalate(cls='plan', what='x', why='y', op_id=NAME + '.b2', wait=False)
    assert exc.value.reason == 'stopping'


def test_sdk_message_id_and_read_back_after_a_restart(host):
    """send() takes the message id the applet chose and returns it on the Reply; a resumed applet reads the
    result back by id (message_result) instead of sending the message again."""
    lid = grant(host)
    from test_runtime_v2 import receive, settle

    def worker(spec):
        if spec.get('to'):
            host.pump()
            settle(host, 'worker-1', receive(host, 'worker-1'), {'status': 'done'})
    app = FakeApplet(host, worker)
    lease = Lease(app, {'id': lid, 'slot': 'worker-1', 'job': 'job-1'})
    reply = lease.send({'f': 1}, instruction='Fix only the failing test.', required=['status'],
                       msg_id=NAME + '.msg-1')
    assert reply.id == NAME + '.msg-1' and reply.status == 'ok'
    nxt = FakeApplet(host)
    back = nxt.message_result(NAME + '.msg-1')
    assert (back.id, back.status, back.result) == (NAME + '.msg-1', 'ok', {'status': 'done'})
    assert nxt.message_result(NAME + '.never-sent', wait_s=0.2) is None


def test_sdk_audit_is_cheap_and_never_raises(host):
    """Section 7: audit() returns at once (the record is submitted on a background thread, in order), never raises
    for a refused or failed submit or a stop, and run()'s stop path waits a bounded time for queued records."""
    import time
    app = FakeApplet(host)
    gate, sent = threading.Event(), []
    app.submit = lambda spec: (gate.wait(5), sent.append(spec['event']), {'status': 'accepted'})[-1]
    started = time.time()
    app.audit('job.one', n=1)
    app.audit('job.two', n=2)
    assert time.time() - started < 0.5 and sent == []
    gate.set()
    assert app._flush_audits(5) == 0 and sent == ['job.one', 'job.two']
    app.submit = lambda spec: 1 / 0  # a broken door: logged, not raised
    app.audit('job.three')
    assert app._flush_audits(5) == 0
    app.stopping.set()
    stuck = threading.Event()
    app.submit = lambda spec: stuck.wait(30)  # a door that does not answer
    app.audit('job.four')  # stopping: still queued, never raised
    started = time.time()
    try:
        assert app._flush_audits(0.3) == 1 and time.time() - started < 2
    finally:
        stuck.set()  # no thread left asleep for the next test
    with pytest.raises(ValueError):
        app.audit('Bad Event')  # a malformed name is the caller's bug: refused at once, without I/O


def test_sdk_stop_returns_open_leases_and_waits_for_the_host(host):
    """Section 2.4: on stop the SDK releases every open lease (force: a message may still run) and waits, bounded,
    until the host records it ended, although every other call returns ``stopping`` at once by then."""
    lid = grant(host)
    message(host, 'm.1', lid)  # a message of the lease is still unanswered
    app = FakeApplet(host)
    app.redis = _own_redis(host)  # the door runs on a thread
    app.stopping.set()
    assert app._return_leases(5) == []
    _join_releases()
    assert rec(host, lid)['state'] == 'released' and app.leases() == []
    assert app._return_leases(5) == []  # nothing open: nothing sent


def _join_releases():
    for t in threading.enumerate():
        if t.name == 'applet-release':
            t.join(5)


def _own_redis(host):
    return redis.Redis(connection_pool=redis.ConnectionPool(**host.r.connection_pool.connection_kwargs,
                       connection_class=host.r.connection_pool.connection_class))


@pytest.mark.parametrize('case', ['draining', 'sigterm'])
def test_sdk_stop_returns_leases_in_the_real_stop_sequences(host, case):
    """draining: host.draining (reason stop) is in the stream and the applet is still up; the release is not
    refused host_draining. sigterm: the host already set state stopping; the release is refused applet_not_up
    and the lease ends by the tick (applet_down). Either way the SDK sees the lease ended and returns."""
    from fleetmux import applet_host
    lid = grant(host)
    if case == 'draining':
        applet_host.draining_event(host.r, NAME, 'stop', 0)
    else:
        host.r.hset(al.hkey(NAME), 'state', 'stopping')
    app = FakeApplet(host, then=lambda spec: al.tick(host))
    app.redis = _own_redis(host)
    app.stopping.set()
    assert app._return_leases(5) == []
    _join_releases()
    assert (rec(host, lid)['state'], rec(host, lid)['end_reason']) == (
        ('released', '') if case == 'draining' else ('revoked', 'applet_down'))


def test_sdk_stop_gives_up_at_the_bound(host):
    import time
    lid = grant(host)
    app = FakeApplet(host)
    stuck = threading.Event()
    app.submit = lambda spec: stuck.wait(30)  # the host does not answer (its daemon is busy stopping)
    started = time.time()
    try:
        assert app._return_leases(0.5) == [lid]
        assert time.time() - started < 2
    finally:
        stuck.set()  # the release thread ends now, not 30 s into a later test
        _join_releases()


def test_sdk_lease_module_imports_only_stdlib_and_redis():
    import subprocess
    import sys
    from pathlib import Path
    code = ('import sys; import fleetmux.applet; '
            'print(sorted(m for m in sys.modules if m.split(".")[0] in ("yaml", "fleetmux")))')
    out = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parent.parent,
                         capture_output=True, text=True, check=True).stdout
    # fleetmux.awake: the clock the SDK waits on (standard library only, docs/design.md section 35)
    assert out.strip() == ("['fleetmux', 'fleetmux.applet', 'fleetmux.applet.core', 'fleetmux.applet.lease', "
                           "'fleetmux.awake']")


def test_sdk_stopping_is_lease_denied_or_rejected(host):
    """Section 9: ``stopping`` comes as LeaseDenied from request_lease and as Rejected from the other calls."""
    lid = grant(host)
    app = FakeApplet(host)
    app.stopping.set()
    with pytest.raises(LeaseDenied) as denied:
        app.request_lease(job='job-2', purpose='Fix another test')
    assert denied.value.reason == 'stopping'
    with pytest.raises(Rejected) as exc:
        Lease(app, {'id': lid, 'slot': 'worker-1', 'job': 'job-1'}).send({}, instruction='x', required=['status'])
    assert exc.value.reason == 'stopping'
    app.audit('job.stopped')  # never raises

