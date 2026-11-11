"""No global request deadline; an expired request deadline holds only the executing slot (docs/design.md §43).

2026-11-09 user: 「supervisor检查没有完成，但是状态正常一直等待不行吗？」「把很恶心的900s冻结全fleet那个一块改掉」.
`request_timeout` defaulted to 900 s and its expiry held the executing slot with the generic `hold()` (pause=True):
the whole service fleet stopped dispatching, queued requests aged out behind it, nothing resumed it (10-26 a service sub-fleet
sub-fleet stall). Now a request without `timeout_s` (and a run without `--request-timeout`) never expires by wall
clock -- health is the watchdog's (§24) -- and an expired deadline is a stale answer: only that slot is held, the
supervisor is never held for it, everything else keeps flowing, a late answer is audited but never applied.

Real private Redis + real hook entry points (Codex supervisor + official Claude workers, service mode).
"""
import json
import time

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn, typed
from test_v2_codex_service import route, service  # noqa: F401  (fixture)
from test_fleet import req as v1_req, results as v1_results, sfx, submit as v1_submit  # noqa: F401  (fixture)
from fleetmux import fleet, runtime_v2


def post(f, rid, *, to=None, timeout_s=None, replay=None):
    spec = dict(id=rid, payload={'message': 'Summarise the overnight fills'},
                reply={'required': ['reply'], 'allowed': ['reply']})
    if to:
        spec['to'] = to
    if timeout_s is not None:
        spec['timeout_s'] = timeout_s
    if replay is not None:
        spec['replay'] = replay
    runtime_v2.post_event(f.v2.bus, dict(kind='request', request_id='cmd-' + rid, spec=spec), 'cmd-' + rid)
    assert f.v2.step(10)
    f.pump()


def result(f, rid):
    raw = f.r.get(fleet.RESULT + rid)
    return json.loads(raw) if raw else None


def audits(f, event):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == event]


def age(f, rid, seconds):
    f.r.hset(fleet.REQ + rid, 'created', time.time() - seconds)


def in_flight(f, slot):
    lease = f.v2.bus.slot_state(slot)
    return f.v2.bus.message(lease['message_id']) if lease.get('current_delivery') else None


def test_there_is_no_global_default_and_an_explicit_one_still_works():
    assert fleet.DEFAULTS['request_timeout'] is None
    assert fleet.cli().parse_args(['start']).request_timeout is None
    assert fleet.cli().parse_args(['start', '--request-timeout', '600']).request_timeout == 600
    assert fleet.request_limit({}, {'request_timeout': None}) is None
    assert fleet.request_limit({}, {}) is None
    assert fleet.request_limit({}, {'request_timeout': 600}) == 600
    assert fleet.request_limit({'timeout_s': 120}, {'request_timeout': None}) == 120
    assert fleet.request_limit({'timeout_s': 120}, {'request_timeout': 600}) == 120


def test_request_without_a_deadline_never_expires_and_its_messages_carry_none(service):
    f = service
    post(f, 'r1', to='worker-1')
    msg = in_flight(f, 'worker-1')
    # The sentinel survives a real Redis round trip as an integer (Lua %.14g would turn >= 1e14 into 2.5e+14).
    assert int(msg['deadline_at']) == runtime_v2.NO_DEADLINE_MS
    age(f, 'r1', 30 * 86400)                          # a month old: no wall clock ends it
    f.v2.tick()
    f.v2.renewed.clear()
    f.v2.renew_tui()
    assert result(f, 'r1') is None and not f.paused() and not audits(f, 'execution.held')
    assert f.agent('worker-1')['state'] == 'busy'
    assert answer_turn(f, 'worker-1', '{"reply": "fills ok"}')['status'] == 'committed'
    assert result(f, 'r1')['status'] == 'ok'


def test_supervisor_routing_message_carries_no_bus_deadline_even_for_a_timed_request(service):
    f = service
    post(f, 'r1', timeout_s=60)                       # no `to`: the supervisor routes it
    msg = in_flight(f, 'supervisor')
    assert msg['request_id'] == 'r1' and int(msg['deadline_at']) == runtime_v2.NO_DEADLINE_MS


def test_expired_request_holds_only_its_slot_and_other_requests_keep_flowing(service):
    f = service
    post(f, 'r1', to='worker-1', timeout_s=60)
    assert int(in_flight(f, 'worker-1')['deadline_at']) == runtime_v2.NO_DEADLINE_MS  # expire() alone enforces it
    age(f, 'r1', 120)
    f.v2.tick()
    assert result(f, 'r1')['status'] == 'timeout'
    assert f.agent('worker-1')['state'] == 'held' and not f.paused(), 'a stale answer must not pause the fleet'
    held, = audits(f, 'execution.held')
    assert held['slot'] == 'worker-1'
    f.v2.renewed.clear()
    f.v2.renew_tui()
    f.v2.tick()
    assert not f.paused()
    post(f, 'r2')                                     # the supervisor still gets new work at once
    assert 'Reply with the service decision JSON from AGENTS.md.' in typed(f, 'supervisor')[-1]
    assert answer_turn(f, 'supervisor', route('r2', to='worker-2'))['status'] == 'committed'
    f.pump()
    assert answer_turn(f, 'worker-2', '{"reply": "fills ok"}')['status'] == 'committed'
    assert result(f, 'r2')['status'] == 'ok' and not f.paused()


def test_request_expiring_while_the_supervisor_routes_it_leaves_the_supervisor_alone(service):
    f = service
    post(f, 'r1', timeout_s=60)
    assert in_flight(f, 'supervisor')['request_id'] == 'r1'
    age(f, 'r1', 120)
    f.v2.tick()
    assert result(f, 'r1')['status'] == 'timeout'
    assert f.agent('supervisor')['state'] == 'busy' and not f.paused()
    assert not audits(f, 'execution.held')
    # Its late routing decision commits and is ignored: the request is no longer routing.
    assert answer_turn(f, 'supervisor', route('r1'))['status'] == 'committed'
    f.pump()
    assert f.agent('worker-1')['state'] == 'idle' and f.v2.depth('worker-1') == 0
    assert result(f, 'r1')['status'] == 'timeout' and not f.paused()
    post(f, 'r2')
    assert in_flight(f, 'supervisor')['request_id'] == 'r2'


def test_late_answer_of_the_held_slot_is_audited_and_never_applied(service):
    f = service
    post(f, 'r1', to='worker-1', timeout_s=60)
    age(f, 'r1', 120)
    f.v2.tick()
    assert answer_turn(f, 'worker-1', '{"reply": "late fills"}')['status'] == 'released'
    assert result(f, 'r1')['status'] == 'timeout' and f.r.llen(fleet.RESULTS) == 1
    late, = audits(f, 'result.late')
    detail = json.loads(late['detail'])
    assert late['request_id'] == 'r1' and late['slot'] == 'worker-1'
    assert detail['public_status'] == 'timeout' and detail['reply'] == {'reply': 'late fills'}
    assert f.agent('worker-1')['state'] == 'idle' and not f.paused()


def test_explicit_run_deadline_is_the_default_for_requests_without_timeout_s(service):
    f = service
    f.cfg['request_timeout'] = 60
    post(f, 'r1', to='worker-1')
    age(f, 'r1', 120)
    f.v2.tick()
    assert result(f, 'r1')['status'] == 'timeout' and not f.paused()
    assert f.agent('worker-1')['state'] == 'held'


def test_v1_request_without_a_deadline_never_expires(sfx):  # noqa: F811
    v1_submit(sfx, v1_req('r1', to='exit'))
    sfx.r.hset('fx:req:r1', mapping={'created': 0})
    sfx.tick()
    assert v1_results(sfx) == []
    v1_submit(sfx, v1_req('r2', to='exit', timeout_s=10))
    sfx.r.hset('fx:req:r2', mapping={'created': 0})
    sfx.tick()
    assert [r['id'] for r in v1_results(sfx)] == ['r2']
