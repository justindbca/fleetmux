"""Deterministic deadline races and unknown slash completion, on private Redis."""
import json
import time
from types import SimpleNamespace

import pytest

from cmux_fleet import claude_hook, fleet
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_claude_supervisor import make_fleet

HOOK_CHANNEL = True


@pytest.fixture
def channel(private_redis_v2, tmp_path):
    f = make_fleet(private_redis_v2, tmp_path, 'pi', 'service')
    thread = f.test_threads['supervisor']
    transcript = tmp_path / 'transcript.jsonl'
    transcript.write_text('')
    f.setf('supervisor', generation=0, state='idle', thread=thread,
           transcript=str(transcript), runtime_ready=1)
    f.v2.bus.register_slot('supervisor', 0, thread)
    yield f, f.v2.hook_keys('supervisor', 0), thread, transcript
    f.v2.close()


def stop_deadline(channel, monkeypatch, race=lambda r, k: None):
    f, k, thread, _ = channel
    clock = [0]
    monkeypatch.setattr(fleet, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    def expired(r, *args, **kwargs):
        race(r, k)
        clock[0] = 2
        return None, None
    monkeypatch.setattr(claude_hook, 'wait', expired)
    return fleet.claude_hook_next(f.cfg, 'supervisor', 0, thread, 1)


@pytest.mark.parametrize('racer', ['delivery', 'armed', 'stopping', 'rewake_keepalive'])
def test_stop_deadline_cannot_clobber_a_concurrent_owner(channel, monkeypatch, racer):
    f, k, thread, _ = channel
    expected = []
    def race(r, k):
        if racer in ('delivery', 'rewake_keepalive'):
            r.set(k['bind'], json.dumps(dict(kind='delivery' if racer == 'delivery' else 'keepalive',
                  generation=0, session_id=thread, delivery_id='new', at=1, via='rewake')))
        else:
            r.set(k[racer], 'another-live-hook')
        expected.append(r.get(k['bind']))
    assert stop_deadline(channel, monkeypatch, race) is None
    assert f.r.get(k['bind']) == expected[0]


def test_stop_and_rewake_have_one_keepalive_winner(channel, monkeypatch):
    f, k, thread, _ = channel
    mine = f.r.incr(k['rewake'])
    assert stop_deadline(channel, monkeypatch) == claude_hook.block(claude_hook.KEEPALIVE)
    bind = f.r.get(k['bind'])
    assert not fleet.supervisor_rewake_keepalive(f.cfg, f.r, k, 0, thread, mine)
    assert f.r.get(k['bind']) == bind


@pytest.mark.parametrize('with_model_row', [False, True])
@pytest.mark.parametrize('prompt_hook', [False, True])
def test_slash_without_stop_has_bounded_explicit_hold(channel, monkeypatch, with_model_row, prompt_hook):
    f, k, thread, transcript = channel
    transcript.write_text(json.dumps(dict(type='queue-operation', operation='enqueue', content='/status')) + '\n')
    # Exercise the actual Stop wait yielding to the queue, even if the CLI never calls UserPromptSubmit.
    assert fleet.claude_hook_next(f.cfg, 'supervisor', 0, thread, time.monotonic() + 1,
                                  transcript=str(transcript)) is None
    with transcript.open('a') as out:
        out.write(json.dumps(dict(type='queue-operation', operation='dequeue')) + '\n')
        if with_model_row:
            out.write(json.dumps(dict(type='assistant', message=dict(content='still running'))) + '\n')
    if prompt_hook:
        assert fleet.claude_prompt(f.path, 'supervisor', 0, json.dumps(dict(session_id=thread, prompt='/status'))) is None
    f.v2.bus.enqueue('supervisor', {'text': 'worker report waiting behind slash'})
    offered = f.v2.bus.offer('supervisor')
    assert f.v2.type_into('supervisor', offered)
    f.setf('supervisor', state='busy')
    lease = f.v2.bus.slot_state('supervisor')
    assert lease['status'] == 'received'
    mailbox = f.r.lrange(k['mailbox'], 0, -1)
    monkeypatch.setattr(fleet.runtime_v2, 'time', SimpleNamespace(time=lambda: time.time() + claude_hook.HUMAN_HOLD + 1,
                                                               monotonic=time.monotonic))
    f.v2.renew_tui()
    assert f.agent('supervisor')['state'] == 'held'
    assert f.v2.bus.slot_state('supervisor')['status'] == 'uncertain'
    assert f.r.lrange(k['mailbox'], 0, -1) == mailbox
    assert 'completion unconfirmed' in f.v2.bus.message(lease['message_id'])['uncertain_reason']
    assert not f.mgr.sent


@pytest.mark.parametrize('field,value', [('run_id', 'foreign'), ('slot', 'worker-1'),
                                         ('generation', '1'), ('session_id', 'other')])
def test_stop_rearm_rechecks_full_lease_identity(channel, monkeypatch, field, value):
    f, k, _, _ = channel
    assert stop_deadline(channel, monkeypatch, lambda r, k: r.hset(
        f.v2.bus.lease_key('supervisor'), field, value)) is None
    assert not f.r.exists(k['bind'])


def test_worker_stop_rearm_uses_worker_identity_not_supervisor(channel, monkeypatch):
    f, _, _, _ = channel
    f.setf('worker-1', generation=3, state='idle', thread='worker-session')
    f.v2.bus.register_slot('worker-1', 3, 'worker-session')
    k = f.v2.hook_keys('worker-1', 3)
    clock = [0]
    monkeypatch.setattr(fleet, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    def expired(*a, **kw):
        clock[0] = 2
        return None, None
    monkeypatch.setattr(claude_hook, 'wait', expired)
    assert fleet.claude_hook_next(f.cfg, 'worker-1', 3, 'worker-session', 1) == claude_hook.block(claude_hook.KEEPALIVE)
    assert json.loads(f.r.get(k['bind']))['session_id'] == 'worker-session'
    assert not f.r.exists(f.v2.hook_keys('supervisor', 0)['bind'])


def test_consumed_keepalive_does_not_admit_a_late_writer_or_phantom_input(channel):
    f, k, thread, _ = channel
    mine = f.r.incr(k['rewake'])
    assert fleet.supervisor_rewake_keepalive(f.cfg, f.r, k, 0, thread, mine)
    assert fleet.claude_hook_keepalive(f.cfg, f.r, 'supervisor', 0, thread)
    assert not fleet.supervisor_rewake_keepalive(f.cfg, f.r, k, 0, thread, mine)
    from test_claude_hook_delivery import rewake_rows
    prompt = rewake_rows(claude_hook.KEEPALIVE)[0]['message']['content']
    events = f.r.xlen(f.v2.events)
    fleet.supervisor_yield_wait(f.cfg, f.r, 'supervisor', 0, thread)
    assert 'keepalive' in fleet.claude_prompt(f.path, 'supervisor', 0,
        json.dumps(dict(session_id=thread, prompt=prompt)))
    assert f.r.xlen(f.v2.events) == events
    assert not f.r.exists(k['yield_wait'])


def test_next_stop_allows_rearm_but_old_stop_proof_cannot(channel):
    f, k, thread, _ = channel
    mine = f.r.incr(k['rewake'])
    assert fleet.supervisor_rewake_keepalive(f.cfg, f.r, k, 0, thread, mine)
    fleet.hook_mark_stopping(f.cfg, 'supervisor', 0, thread)
    assert fleet.claude_hook_keepalive(f.cfg, f.r, 'supervisor', 0, thread)
    f.r.delete(k['stopping'])
    assert not fleet.supervisor_rewake_keepalive(f.cfg, f.r, k, 0, thread, mine, started=0)
    assert fleet.supervisor_rewake_keepalive(f.cfg, f.r, k, 0, thread, mine)


def test_unknown_yield_blocks_rewake_after_human_ttl_and_real_stop_resolves_it(channel, monkeypatch):
    f, k, thread, transcript = channel
    fleet.supervisor_yield_wait(f.cfg, f.r, 'supervisor', 0, thread)
    marker = f.r.get(k['yield_wait'])
    fleet.hook_mark_stopping(f.cfg, 'supervisor', 0, 'stale-session')
    assert f.r.get(k['yield_wait']) == marker
    f.r.delete(k['human'], k['stopping'])
    claude_hook.post(f.r, k, dict(kind='nudge', generation=0, text='must stay queued'))
    fleet.save(f.path, dict(f.cfg, claude_hook_timeout=10))
    clock = [0]
    def advance(seconds):
        clock[0] += seconds
    monkeypatch.setattr(fleet, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time, sleep=advance))
    assert fleet.claude_rewake(f.path, 'supervisor', 0, json.dumps(dict(
        session_id=thread, transcript_path=str(transcript)))) is None
    assert f.r.llen(k['mailbox']) == 1 and not f.r.exists(k['bind'])
    fleet.hook_mark_stopping(f.cfg, 'supervisor', 0, thread)
    assert not f.r.exists(k['yield_wait'])


def test_unknown_slash_holds_even_without_delivery_but_not_another_generation(channel, monkeypatch):
    f, k, thread, _ = channel
    fleet.supervisor_yield_wait(f.cfg, f.r, 'supervisor', 0, thread)
    marker = f.r.get(k['yield_wait'])
    monkeypatch.setattr(fleet.runtime_v2, 'time', SimpleNamespace(time=lambda: time.time() + claude_hook.HUMAN_HOLD + 1,
                                                               monotonic=time.monotonic))
    f.setf('supervisor', generation=1)
    f.v2.renew_tui()
    assert f.agent('supervisor')['state'] == 'idle'
    f.setf('supervisor', generation=0)
    f.v2.renew_tui()
    assert f.agent('supervisor')['state'] == 'held'
    assert f.r.get(k['yield_wait']) == marker
    held = [row for _, row in f.r.xrange(f.v2.bus.audit_key) if row.get('event') == 'execution.held']
    assert len(held) == 1 and 'completion unconfirmed' in held[0]['error']
    f.v2.renew_tui()
    assert len([row for _, row in f.r.xrange(f.v2.bus.audit_key) if row.get('event') == 'execution.held']) == 1


def test_blocked_operator_proof_clears_the_unknown_yield(channel):
    f, k, thread, _ = channel
    fleet.supervisor_yield_wait(f.cfg, f.r, 'supervisor', 0, thread)
    fleet.supervisor_prompt_blocked(f.cfg, f.r, 'supervisor', 0, thread)
    assert not f.r.exists(k['yield_wait'])


def test_keepalive_consumption_cannot_delete_a_concurrent_delivery(channel, monkeypatch):
    f, k, thread, _ = channel
    f.r.set(k['bind'], json.dumps(dict(kind='keepalive', generation=0, session_id=thread)))
    delivery = json.dumps(dict(kind='delivery', generation=0, session_id=thread, delivery_id='new'))
    read = fleet.hook_bind
    def replaced(r, keys):
        old = read(r, keys)
        r.set(keys['bind'], delivery)
        return old
    monkeypatch.setattr(fleet, 'hook_bind', replaced)
    fleet.claude_hook_keepalive(f.cfg, f.r, 'supervisor', 0, thread)
    assert f.r.get(k['bind']) == delivery


def test_yield_at_the_stop_deadline_still_gets_a_bounded_wait(channel, monkeypatch):
    f, k, _, _ = channel
    assert stop_deadline(channel, monkeypatch, lambda r, k: r.set(k['human'], 'queued slash')) is None
    assert f.r.exists(k['yield_wait'])


def test_old_generation_keepalive_cannot_prevent_successor_rearm(channel):
    f, k, thread, _ = channel
    mine = f.r.incr(k['rewake'])
    assert fleet.supervisor_rewake_keepalive(f.cfg, f.r, k, 0, thread, mine)
    f.setf('supervisor', generation=1, thread='successor')
    f.v2.bus.register_slot('supervisor', 1, 'successor')
    next_keys = f.v2.hook_keys('supervisor', 1)
    assert fleet.supervisor_rewake_keepalive(f.cfg, f.r, next_keys, 1, 'successor', mine)
    assert json.loads(f.r.get(next_keys['bind']))['generation'] == 1
    assert not fleet.supervisor_rewake_keepalive(f.cfg, f.r, k, 0, thread, mine)
