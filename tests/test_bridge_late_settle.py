"""A pi bridge whose `settled` the daemon answers late (2026-11-09 channels fleet, design §47).

Real persistent AgentBridge + real private Redis + the real daemon step/pump/tick loop, which the
test can hold still to play a daemon that is busy for longer than the bridge waits for its answer.
No model, GUI or production socket.
"""
import json
import threading

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture used by fx)
from test_runtime_v2 import Output, fx, session, wait_until  # noqa: F401  (fx: real-Redis v2 fleet)
from fleetmux import agent_bridge, fleet, runtime_v2
from fleetmux.agent_bridge import AgentBridge


class Daemon:
    """The daemon loop on a thread; `held()` keeps it from taking any event (a busy daemon)."""
    def __init__(self, f):
        self.f, self.stop, self.gate, self.busy, self.errors = f, threading.Event(), threading.Event(), threading.Lock(), []
        self.gate.set()
        self.thread = threading.Thread(target=self.loop)

    def loop(self):
        try:
            while not self.stop.is_set():
                if not self.gate.wait(.01):
                    continue
                with self.busy:
                    if self.gate.is_set():  # hold() may have closed it while this waited for the lock
                        self.f.v2.step(20); self.f.pump(); self.f.tick()
        except BaseException as exc:
            self.errors.append(exc)

    def hold(self):
        with self.busy:
            self.gate.clear()

    def release(self):
        self.gate.set()


@pytest.fixture
def rig(fx, monkeypatch):
    # A short lease so the bridge's renew loop (period lease/3) follows the message within a second.
    cfg = dict(fx.cfg, v2_lease_ms=1500)
    outputs = {slot: Output() for slot in ('supervisor', 'worker-1')}
    bridges = {slot: AgentBridge(cfg, slot, 0, outputs[slot]) for slot in outputs}
    metadata = {slot: session(fx, slot) for slot in bridges}
    for slot in bridges:
        fx.setf(slot, state='starting', bridge_available=0)
    daemon = Daemon(fx)
    daemon.thread.start()
    observer = fleet.connect(fx.cfg)
    try:
        for slot, bridge in bridges.items():
            bridge.initialize(metadata[slot])
            bridge.command(dict(metadata[slot], kind='startup', text='READY', failed=False, aborted=False))
            bridge.command(dict(metadata[slot], kind='availability', idle=True))
        yield View(observer, runtime_v2.bus_for(fx.cfg, observer)), bridges, outputs, metadata, daemon, None
    finally:
        for bridge in bridges.values():
            bridge.stop()
        daemon.stop.set(); daemon.release(); daemon.thread.join(timeout=3)
        observer.close(); observer.connection_pool.disconnect()
    assert not daemon.thread.is_alive() and not daemon.errors


class View:
    """Main-thread reads on their own connection (the fleet's client is the daemon thread's)."""
    def __init__(self, r, bus):
        self.r, self.bus = r, bus

    def agent(self, slot):
        return self.r.hgetall(fleet.key('agent', slot))

    def paused(self):
        return self.r.hget(fleet.SERVICE, 'paused') == '1'


def frame(output, kind, timeout=5):
    while True:
        item = output.frames.get(timeout=timeout)
        if item['kind'] == kind:
            return item
        assert item['kind'] in ('reply', 'ui', 'completed', 'delivery'), item  # never a fault on the way


def short_wait(monkeypatch, seconds=.3):
    """The bridge's wait for the daemon's answer runs out at once (as 30 s did on 11-09)."""
    real = runtime_v2.wait_reply
    monkeypatch.setattr(agent_bridge, 'wait_reply',
                        lambda bus, correlation, timeout=30, reader=None: real(bus, correlation, timeout=seconds, reader=reader))


def decision(to='worker-1'):
    return [{'tool': 'fleet_decision', 'value': {'status': 'RUNNING', 'skip': [],
             'dispatch': [{'to': to, 'instruction': 'Reply OK in the reply field.'}]}}]


def audits(f, name):
    return [e for _, e in f.r.xrange(f.bus.audit_key) if e.get('event') == name]


def test_late_settle_answer_is_not_a_fault_and_the_turn_commits(rig, monkeypatch):
    f, bridges, outputs, md, daemon, _ = rig
    bus = f.bus
    sup = bridges['supervisor']
    ack = sup.command(dict(md['supervisor'], kind='operator_input', text='让 worker-1 回复 OK', correlation='ui-late'))
    assert ack['status'] == 'accepted'
    env = frame(outputs['supervisor'], 'delivery')['delivery']
    sup.command(dict(md['supervisor'], kind='started'))
    # The daemon is busy (11-09: one event per 20-30 s tick with 11 busy panes) when the turn ends.
    daemon.hold()
    short_wait(monkeypatch)
    answer = sup.command(dict(md['supervisor'], kind='settled', failed=False, aborted=False, unfinished=False,
                              candidates=decision()))
    assert answer['status'] == 'pending'
    # Not answered yet is not "unconfirmed": the settled event is durable in the event stream.
    assert not sup.faulted
    assert bus.message(env['message_id'])['status'] == 'received'
    assert not audits(f, 'uncertain')
    daemon.release()
    assert frame(outputs['supervisor'], 'completed')['message_id'] == env['message_id']
    sup.command(dict(md['supervisor'], kind='availability', idle=True))  # the extension's report on that frame
    assert bus.message(env['message_id'])['status'] == 'committed'
    assert f.agent('supervisor').get('state') != 'held' and not f.paused()
    assert not audits(f, 'settle.late') and not audits(f, 'execution.held')
    # The supervisor decision was applied: worker-1 gets the dispatch and answers the request.
    work = frame(outputs['worker-1'], 'delivery')['delivery']
    bridges['worker-1'].command(dict(md['worker-1'], kind='settled', failed=False, aborted=False, unfinished=False,
                                     candidates=[{'tool': 'fleet_result', 'value': {'reply': 'OK'}}]))
    wait_until(lambda: f.r.get(fleet.RESULT + ack['id']), timeout=5)
    assert json.loads(f.r.get(fleet.RESULT + ack['id']))['status'] == 'ok'
    assert work['message_id'] != env['message_id']
    # And the supervisor takes its next delivery: the late completion reopened its inbox.
    sup.command(dict(md['supervisor'], kind='operator_input', text='再让 worker-1 回复 OK', correlation='ui-next'))
    assert frame(outputs['supervisor'], 'delivery')['delivery']['message_id'] != env['message_id']


def test_late_hold_answer_still_holds(rig, monkeypatch):
    """Only the wait got longer: a daemon that answers `held` still makes the bridge stop the slot."""
    f, bridges, outputs, md, daemon, _ = rig
    bus = f.bus
    sup = bridges['supervisor']
    sup.command(dict(md['supervisor'], kind='operator_input', text='让 worker-1 回复 OK', correlation='ui-held'))
    env = frame(outputs['supervisor'], 'delivery')['delivery']
    daemon.hold()
    short_wait(monkeypatch)
    answer = sup.command(dict(md['supervisor'], kind='settled', failed=False, aborted=True, unfinished=False,
                              candidates=[]))
    assert answer['status'] == 'pending' and not sup.faulted
    daemon.release()
    wait_until(lambda: sup.faulted, timeout=5)
    assert bus.message(env['message_id'])['status'] in ('uncertain', 'expired')
    assert f.agent('supervisor').get('state') == 'held'


def test_operator_input_mid_turn_is_queued_and_the_turn_settles(rig):
    """§5: text typed into a busy pi supervisor becomes an operator_input request; the running
    delivery settles as usual and the typed instruction is delivered after it."""
    f, bridges, outputs, md, daemon, _ = rig
    bus = f.bus
    sup = bridges['supervisor']
    sup.command(dict(md['supervisor'], kind='operator_input', text='让 worker-1 回复 OK', correlation='ui-first'))
    env = frame(outputs['supervisor'], 'delivery')['delivery']
    sup.command(dict(md['supervisor'], kind='started'))
    typed = sup.command(dict(md['supervisor'], kind='operator_input', text='再让 worker-2 回复 OK', correlation='ui-typed'))
    assert typed['status'] == 'accepted'
    answer = sup.command(dict(md['supervisor'], kind='settled', failed=False, aborted=False, unfinished=False,
                              candidates=decision()))
    assert answer['status'] == 'committed' and not sup.faulted
    assert frame(outputs['supervisor'], 'completed')['message_id'] == env['message_id']
    sup.command(dict(md['supervisor'], kind='availability', idle=True))
    nxt = frame(outputs['supervisor'], 'delivery')['delivery']
    assert nxt['message_id'] != env['message_id'] and nxt['request_id'] == typed['id']
    assert 'worker-2' in nxt['body']['text']
    assert f.agent('supervisor').get('state') != 'held' and not f.paused()
