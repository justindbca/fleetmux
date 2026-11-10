"""§24: a Claude Stop deferred by background tasks reaches the daemon as a turn_deferred event.

Real private Redis, the real Stop hook entry (fleet.claude_stop via hook_sim) and the real daemon step: the
daemon marks the slot (bg_wait / bg_wait_act / bg_tasks) only for the in-flight delivery of the same run,
generation and session; anything else is audit only. The watchdog then does not count the waiting slot as quiet.
"""
import json
import time

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn, dispatch_one, mixed, typed  # noqa: F401
from test_watchdog import REPORT, audits, keys, stop_with, wd  # noqa: F401  (fixture)
import hook_sim

BG = [{'id': 't1', 'type': 'shell', 'status': 'running', 'command': 'python3 -m pytest tests', 'description': 'tests'}]


def events(f, kind):
    rows = [json.loads(fields['data']) for _, fields in f.r.xrange(f.v2.bus.prefix + ':events')]
    return [e for e in rows if e.get('kind') == kind]


def drain(f):
    while f.v2.step(10):
        pass


def test_deferred_stop_marks_the_in_flight_slot_and_the_watchdog_waits(wd):
    f = wd
    f.cfg.update(stall_timeout=1200, stall_window=300, stall_cpu_pct=5)
    f.mgr.process_cpu = lambda pane: 10.0
    dispatch_one(f)
    lease = f.v2.bus.slot_state('worker-1')
    rows = [hook_sim.user_row(typed(f, 'worker-1')[-1], True)]
    assert stop_with(f, 'worker-1', rows, background=BG) is False
    ev, = events(f, 'turn_deferred')
    assert ev['agent'] == 'worker-1' and ev['generation'] == 0 and ev['thread'] == f.agent('worker-1')['thread']
    assert ev['tasks'] == [{'id': 't1', 'type': 'shell', 'status': 'running', 'description': 'tests'}]
    drain(f)
    info = f.agent('worker-1')
    assert info['bg_wait'] == lease['current_delivery'] and info['bg_tasks'] == '1'
    assert float(info['bg_wait_act']) == pytest.approx(f.last_activity(info))
    deferred, = audits(f, 'turn.deferred')
    assert deferred['tasks'] == '1' and deferred['types'] == 'shell'
    assert info['state'] == 'busy' and f.v2.bus.slot_state('worker-1')['status'] == 'received'  # nothing settled
    t0 = time.time()
    for dt in (0, 100, 200, 310):
        f.v2.watchdog.tick(t0 + dt)
        assert f.agent('worker-1')['watch_state'] == 'working'
    assert keys(f, 'worker-1') == []


def test_a_stop_without_background_tasks_posts_no_event(wd):
    f = wd
    dispatch_one(f)
    answer_turn(f, 'worker-1', REPORT)
    drain(f)
    assert events(f, 'turn_deferred') == [] and audits(f, 'turn.deferred') == []
    assert f.agent('worker-1').get('bg_wait', '') == ''


@pytest.mark.parametrize('case,audit', [
    ('generation', 'event.stale'),            # an older generation's hook
    ('session', 'event.stale_session'),       # another session in the same generation
    ('held', None),                           # the lease is not in flight (held): audit only
])
def test_a_deferred_stop_that_is_not_the_in_flight_delivery_is_only_audited(wd, case, audit):
    f = wd
    dispatch_one(f)
    if case == 'held':
        f.v2.finish('blocked', 'supervisor gave up')
    before = len(audits(f, audit)) if audit else 0
    thread = f.agent('worker-1')['thread']
    transcript = f.test_claude / 'worker-1' / (thread + '.jsonl')
    payload = {'session_id': 'another-session' if case == 'session' else thread,
               'transcript_path': str(transcript), 'last_assistant_message': '', 'background_tasks': BG}
    gen = '4' if case == 'generation' else '0'
    assert hook_sim.claude_stop(f.path, 'worker-1', gen, json.dumps(payload)) is False
    assert len(events(f, 'turn_deferred')) == 1
    drain(f)
    if audit:
        assert len([a for a in audits(f, audit) if a.get('slot') == 'worker-1']) == before + 1
    assert [a for a in audits(f, 'turn_deferred') if a.get('slot') == 'worker-1']   # the daemon saw it
    assert f.agent('worker-1').get('bg_wait', '') == ''
