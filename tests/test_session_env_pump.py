"""Session environment: nothing is delivered to a routed slot while its applet port is closed.

docs/distributed-development.md, "Fail closed without a rotation storm": a session that needs the route is held, not sent
to. The delivery pump keeps the slot's queue (same order), sets ``route_wait=1``, spends no attempt, starts no
generation, and delivers on the first pump after the port answers. Real private Redis; loopback ports this test
opens itself only.
"""
import json
import socket

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import mixed  # noqa: F401  (fixture: Codex supervisor + Claude workers, task mode)
from test_v2_codex_service import service  # noqa: F401  (fixture: Codex supervisor + Claude workers, service)
from fleetmux import fleet, runtime_v2, session_env


def closed_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def listener():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    s.listen(16)
    yield s.getsockname()[1]
    s.close()


def route(f, port, clis=('claude',)):
    f.cfg['session_env'] = [{'applet': 'applet-tunnel-1', 'port': port, 'clis': list(clis),
                             'vars': {'HTTPS_PROXY': 'http://127.0.0.1:' + str(port)}}]
    f.__dict__.pop('route_probe', None)  # the next pump probes again


def in_flight(f, slot):
    """The message handed to the slot's session (set by deliver, before a Claude hook takes it)."""
    return f.v2.bus.slot_state(slot).get('message_id')


def pending_texts(f, slot):
    return [f.v2.bus.message(m)['body'].get('text', '') for m in f.v2.bus.pending(slot)]


def delivered_text(f, slot):
    return f.v2.bus.message(f.v2.bus.slot_state(slot)['message_id'])['body'].get('text', '')


def spy_deliver(f, monkeypatch):
    """Every hand-over of queued work to a slot's session goes through Fleet.deliver (called by the pump only)."""
    calls, real = [], f.deliver
    monkeypatch.setattr(f, 'deliver', lambda name: calls.append((name, pending_texts(f, name)[:1])) or real(name))
    return calls


def test_a_routed_worker_gets_nothing_while_the_port_is_closed_then_its_queue_in_order(mixed, listener,  # noqa: F811
                                                                                        monkeypatch):
    f = mixed
    calls = spy_deliver(f, monkeypatch)
    route(f, closed_port())
    generation = f.agent('worker-1')['generation']
    f.queue('worker-1', 'First: write a.md.')
    f.queue('worker-1', 'Second: write b.md.')
    for _ in range(3):
        f.pump()
    info = f.agent('worker-1')
    assert calls == [] and not in_flight(f, 'worker-1'), 'nothing reaches the session while the port is closed'
    assert info['route_wait'] == '1' and info['state'] == 'idle' and info['generation'] == generation
    held = f.v2.bus.pending('worker-1')
    assert [str(f.v2.bus.message(m).get('attempt')) for m in held] == ['0', '0'], 'no attempt spent'
    assert ['First' in t for t in pending_texts(f, 'worker-1')] == [True, False]
    route(f, listener)
    f.pump()
    assert f.agent('worker-1')['route_wait'] == '0'
    assert [name for name, _ in calls] == ['worker-1'] and 'First' in calls[0][1][0], 'the oldest goes first'
    assert f.agent('worker-1')['generation'] == generation


def test_a_slot_whose_cli_is_not_routed_is_not_held(mixed):  # noqa: F811
    f = mixed
    route(f, closed_port())  # routes claude; the supervisor runs codex
    f.queue('supervisor', 'A report for you.')
    f.pump()
    assert in_flight(f, 'supervisor') and not f.agent('supervisor').get('route_wait')


def test_a_routed_supervisor_is_held_and_gets_its_queue_after_the_port_opens(mixed, listener, monkeypatch):  # noqa: F811
    f = mixed
    monkeypatch.setattr(session_env, 'settings', lambda: dict(session_env.SETTINGS, verified_clis=['claude', 'codex']))
    route(f, closed_port(), clis=('codex',))  # codex stands in for a routed supervisor CLI (verified in this test only)
    f.queue('supervisor', 'A report for you.')
    f.pump()
    assert not in_flight(f, 'supervisor') and f.agent('supervisor')['route_wait'] == '1'
    route(f, listener, clis=('codex',))
    f.pump()
    assert in_flight(f, 'supervisor') and f.agent('supervisor')['route_wait'] == '0'


def test_a_service_request_with_to_is_held_not_failed(service, listener):  # noqa: F811
    f = service
    route(f, closed_port())
    spec = dict(id='r-held', to='worker-1', payload={'message': 'hi'}, reply={'required': ['reply'], 'allowed': ['reply']})
    runtime_v2.post_event(f.v2.bus, dict(kind='request', request_id='cmd-r-held', spec=spec), 'cmd-r-held')
    assert f.v2.step(10)
    for _ in range(3):
        f.pump()
    assert not in_flight(f, 'worker-1') and f.agent('worker-1')['route_wait'] == '1'
    assert f.r.get(fleet.RESULT + 'r-held') is None, 'held, not failed'
    req = f.r.hgetall(fleet.REQ + 'r-held')
    assert req['status'] == 'dispatched' and req['attempts'] == '0'
    route(f, listener)
    f.pump()
    assert in_flight(f, 'worker-1')
    assert f.v2.bus.message(f.v2.bus.slot_state('worker-1')['message_id'])['request_id'] == 'r-held'
    assert json.loads(f.r.get(fleet.RESULT + 'r-held') or 'null') is None  # still running, not failed
