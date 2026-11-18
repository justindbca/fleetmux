"""Roster `fresh: always` (docs/design.md §45, extends §37): the daemon gives an always worker a clean generation
before every dispatch once its current generation has delivered work; a busy always worker still takes queued
dispatches (no refusal); `"fresh": false` keeps the session for one dispatch. In-memory Redis + fake cmux (the
§37 fixtures), the private Redis v2 pi fixture for pi workers, the §28 fixture for clear_after_result."""
import json
import os
from types import SimpleNamespace

import pytest

from test_fleet import (CODEX_QUEUE, THREADS, SVC_THREADS, answer, append_rollout, decision, fx,  # noqa: F401
                        last_to, received, req, sfx, submit, turn)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx as pifx  # noqa: F401  (pi v2 fixture, service mode)
from test_runtime_v2 import audits, ready, receive, settle
from test_runtime_v2 import req as v2req
from test_v2_codex_service import request, route, service  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn
from test_clear_after_result import SLOT, idle_twice
from test_clear_after_result_redis import allow_exit, cleared, clearing  # noqa: F401  (fixture)
from fleetmux import fleet, roster

NEW_THREAD = '55555555-5555-5555-5555-555555555555'
OTHER = 'Write worker-1/other.md about the unrelated billing export, at least 50 chars'
DONE = json.dumps({'done': ['report written'], 'evidence': ['worker-1/report.md'], 'not_done': '', 'blocked': ''})


def always(f, slot='worker-1'):
    f.cfg['worker_fresh'] = dict(f.cfg.get('worker_fresh') or {}, **{slot: 'always'})


def worked(f, used=40000):
    """worker-1 handled one dispatch and reported: idle, delivered=1, `used` tokens of a 95k window."""
    f.pump()
    f.handle(turn('supervisor', decision()))
    f.pump()
    append_rollout(f.sessions, THREADS['worker-1'], fleet.agent_cwd(f.cfg, 'worker-1'), [
        {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
            'last_token_usage': {'total_tokens': used}, 'model_context_window': 95000}}}])
    f.handle(turn('worker-1', DONE))
    f.pump()
    assert f.agent('worker-1')['state'] == 'idle' and f.agent('worker-1')['delivered'] == '1'


def logs(f, kind):
    return [json.loads(x) for x in f.r.l.get('fx:log', []) if json.loads(x)['kind'] == kind]


def other(**extra):
    return decision(dispatch=[dict({'to': 'worker-1', 'instruction': OTHER}, **extra)])


def ready_new(f):
    f.handle({'kind': 'turn', 'agent': 'worker-1', 'generation': int(f.agent('worker-1')['generation']),
              'thread': NEW_THREAD, 'text': 'READY'})
    f.pump()


# -- 1. roster key

def parse(text, tmp_path):
    return roster.fresh(text.encode()), roster.parse_entries(text.encode(), tmp_path)


@pytest.mark.parametrize('row,want', [
    ('{name: worker-1, fresh: always}', {'worker-1': 'always'}),
    ('{name: worker-1, fresh: off}', {}),          # YAML 1.1: a bare off is false
    ('{name: worker-1, fresh: "off"}', {}),
    ('{name: worker-1}', {}),
])
def test_roster_fresh_always_off_and_default_parse(tmp_path, row, want):
    text = 'workers:\n  - ' + row + '\n  - {name: worker-2, fresh: always}\n'
    fresh, (workers, _, _) = parse(text, tmp_path)
    assert workers == ['worker-1', 'worker-2'] and fresh == dict(want, **{'worker-2': 'always'})


@pytest.mark.parametrize('value', ['true', 'yes', '1', '0.5', 'on', 'sometimes', '[always]'])
def test_roster_fresh_bad_values_are_rejected(tmp_path, value):
    text = 'workers:\n  - {name: worker-1, fresh: ' + value + '}\n'
    with pytest.raises(ValueError, match=r'workers\[0\] \(worker-1\): fresh must be "always" or "off"'):
        roster.parse_entries(text.encode(), tmp_path)


def test_roster_unknown_key_message_lists_fresh(tmp_path):
    with pytest.raises(ValueError) as err:
        roster.parse_entries(b'workers:\n  - {name: worker-1, model: x}\n', tmp_path)
    assert 'optional "role", "profile", "writable", "window",' in str(err.value) and '"fresh"' in str(err.value)


# -- 2. two dispatches in a row

def test_always_worker_first_dispatch_direct_second_rotates_clean(fx):
    always(fx)
    worked(fx)
    assert fx.agent('worker-1')['generation'] == '0'
    assert [x['reason'] for x in logs(fx, 'dispatch.fresh_skipped')] == ['delivered_0']
    old = fx.agent('worker-1')['pane']
    fx.handle(turn('supervisor', other()))
    fx.pump()
    info = fx.agent('worker-1')
    assert info['generation'] == '1' and info['label'] == 'worker-1.1' and info['state'] == 'starting'
    assert old in fx.mgr.killed and fx.r.llen('fx:inbox:worker-1') == 1
    assert not list((fx.run_dir / 'handoffs').glob('worker-1-g0*.md'))  # no handoff note
    prompt = open(fx.mgr.spawned[-1]['prompt_file']).read()
    assert 'handoff' not in prompt.lower() and 'taking over' not in prompt
    audit = logs(fx, 'dispatch.fresh')[-1]
    assert audit['source'] == 'always' and audit['from_generation'] == 0 and audit['generation'] == 1
    ready_new(fx)
    assert 'worker-1/other.md' in last_to(fx, 'worker-1') and fx.agent('worker-1')['state'] == 'busy'
    assert fx.agent('worker-1')['generation'] == '1'  # the new generation (delivered 0) took it directly
    assert logs(fx, 'dispatch.fresh_skipped')[-1]['reason'] == 'delivered_0'


def test_explicit_true_on_an_always_worker_is_audited_as_explicit(fx):
    always(fx)
    worked(fx)
    fx.handle(turn('supervisor', other(fresh=True)))
    fx.pump()
    assert fx.agent('worker-1')['generation'] == '1'
    assert logs(fx, 'dispatch.fresh')[-1]['source'] == 'explicit'


# -- 3. busy always worker: queued, not refused, rotated when its turn comes

@pytest.mark.parametrize('extra', [{}, {'fresh': True}])
def test_dispatch_to_a_busy_always_worker_is_queued_then_rotated(fx, extra):
    always(fx)
    fx.pump()
    fx.handle(turn('supervisor', decision()))
    fx.pump()
    assert fx.agent('worker-1')['state'] == 'busy'
    fx.handle(turn('supervisor', other(**extra)))
    assert fx.task().get('bad_replies', '0') == '0'          # not refused
    assert fx.r.llen('fx:inbox:worker-1') == 1 and fx.agent('worker-1')['generation'] == '0'
    fx.pump()
    assert fx.agent('worker-1')['generation'] == '0'          # still busy: nothing rotates mid-turn
    fx.handle(turn('worker-1', DONE))
    fx.pump()
    assert fx.agent('worker-1')['generation'] == '1' and fx.agent('worker-1')['state'] == 'starting'
    assert fx.r.llen('fx:inbox:worker-1') == 1
    ready_new(fx)
    assert 'worker-1/other.md' in last_to(fx, 'worker-1')


def test_service_request_to_a_busy_always_worker_is_not_refused(sfx):
    always(sfx, 'exit')
    submit(sfx, req('r30', to='exit'))
    sfx.pump()
    assert sfx.agent('exit')['state'] == 'busy'
    submit(sfx, req('r31'))
    sfx.handle(turn('supervisor', json.dumps({'status': 'RUNNING', 'skip': [], 'dispatch': [
        {'to': 'exit', 'request_id': 'r31', 'instruction': 'Judge the exit of this new fill', 'fresh': True}]})))
    assert sfx.r.hget('fx:req:r31', 'status') != 'routing'
    assert not any('refused' in x for x in sfx.r.l.get('fx:inbox:supervisor', []))
    answer(sfx, 'exit', '{"verdict": 1, "param": 2}')
    sfx.pump()
    assert sfx.agent('exit')['generation'] == '1' and sfx.agent('exit')['label'] == 'exit.1'
    assert logs(sfx, 'dispatch.fresh')[-1]['source'] == 'explicit'


# -- 4. explicit "fresh": false keeps the session for that one dispatch

def test_explicit_false_keeps_the_session_and_is_audited(fx):
    always(fx)
    worked(fx)
    fx.handle(turn('supervisor', other(fresh=False)))
    fx.pump()
    info = fx.agent('worker-1')
    assert info['generation'] == '0' and info['state'] == 'busy' and 'other.md' in last_to(fx, 'worker-1')
    assert logs(fx, 'dispatch.fresh_skipped')[-1]['reason'] == 'explicit_false'
    assert not logs(fx, 'dispatch.fresh')
    fx.handle(turn('worker-1', DONE))
    fx.pump()
    fx.handle(turn('supervisor', decision(dispatch=[
        {'to': 'worker-1', 'instruction': 'Write worker-1/third.md on a third topic, at least 50 chars'}])))
    fx.pump()
    assert fx.agent('worker-1')['generation'] == '1'  # the false applied to one dispatch only


def test_explicit_false_applies_to_its_own_queued_dispatch_only(fx):
    always(fx)
    fx.pump()
    fx.handle(turn('supervisor', decision()))
    fx.pump()
    fx.handle(turn('supervisor', other()))                    # queued behind, no fresh: rotate
    fx.handle(turn('supervisor', decision(dispatch=[
        {'to': 'worker-1', 'instruction': 'Fix worker-1/other.md: add the totals table', 'fresh': False}])))
    assert fx.r.llen('fx:inbox:worker-1') == 2
    fx.handle(turn('worker-1', DONE))
    fx.pump()
    assert fx.agent('worker-1')['generation'] == '1'          # the first queued dispatch got a clean session
    ready_new(fx)
    assert 'other.md' in last_to(fx, 'worker-1')
    q = CODEX_QUEUE.pop(NEW_THREAD, None)  # the new generation's queued delivery this turn answers
    fx.handle({'kind': 'turn', 'agent': 'worker-1', 'generation': 1, 'thread': NEW_THREAD, 'text': DONE,
               **({'turn': q['turn']} if q else {})})
    fx.pump()
    assert fx.agent('worker-1')['generation'] == '1'          # the false one stays on that session
    assert 'totals table' in last_to(fx, 'worker-1')
    assert logs(fx, 'dispatch.fresh_skipped')[-1]['reason'] == 'explicit_false'


# -- 5. guards postpone the rotation, never skip it

def test_profile_swap_pending_postpones_then_rotates_before_delivery(fx, monkeypatch):
    always(fx)
    worked(fx)
    monkeypatch.setattr(fx, 'profile_swaps', lambda: ['worker-1'])
    fx.handle(turn('supervisor', other()))
    sent = len(received(fx.mgr, fx.agent('worker-1')['pane']))
    fx.pump()
    assert fx.agent('worker-1')['generation'] == '0' and fx.agent('worker-1')['state'] == 'idle'
    assert len(received(fx.mgr, fx.agent('worker-1')['pane'])) == sent  # not delivered to the old session
    monkeypatch.setattr(fx, 'profile_swaps', lambda: [])
    fx.pump()
    assert fx.agent('worker-1')['generation'] == '1' and fx.r.llen('fx:inbox:worker-1') == 1
    ready_new(fx)
    assert 'other.md' in last_to(fx, 'worker-1')


def test_clearing_postpones_then_rotates_before_delivery(fx):
    always(fx)
    worked(fx)
    old = fx.agent('worker-1')['pane']
    fx.setf('worker-1', clear_required=1)
    fx.handle(turn('supervisor', other()))
    sent = len(received(fx.mgr, old))
    fx.pump()
    fx.pump()
    assert fx.agent('worker-1')['generation'] == '0' and fx.r.llen('fx:inbox:worker-1') == 1
    assert len(received(fx.mgr, old)) == sent and not any('other.md' in t for t in received(fx.mgr, old))
    fx.setf('worker-1', clear_required=0)
    fx.pump()
    assert fx.agent('worker-1')['generation'] == '1'
    ready_new(fx)
    assert 'other.md' in last_to(fx, 'worker-1')
    assert not any('other.md' in t for t in received(fx.mgr, old))


# -- 6. a clean session that does not start

def test_spawn_failure_restores_the_old_generation_and_tells_the_supervisor(fx):
    always(fx)
    worked(fx)
    old = fx.agent('worker-1')['pane']
    real = fx.mgr.spawn_interactive_cli
    fx.mgr.spawn_interactive_cli = lambda **kw: SimpleNamespace(success=False, pane_id=None, error='no cmux')
    fx.handle(turn('supervisor', other()))
    fx.pump()
    info = fx.agent('worker-1')
    assert info['generation'] == '0' and info['label'] == 'worker-1' and info['pane'] == old
    assert old not in fx.mgr.killed
    fx.pump()  # the old session takes this dispatch; no second attempt for it
    assert 'worker-1/other.md' in last_to(fx, 'worker-1') and fx.agent('worker-1')['generation'] == '0'
    assert len(logs(fx, 'dispatch.fresh_failed')) == 1
    fx.mgr.spawn_interactive_cli = real
    fx.handle(turn('worker-1', DONE))
    fx.pump()
    assert '[Fresh failed] worker-1' in last_to(fx, 'supervisor')
    fx.handle(turn('supervisor', decision(dispatch=[
        {'to': 'worker-1', 'instruction': 'Write worker-1/third.md on a third topic, at least 50 chars'}])))
    fx.pump()
    assert fx.agent('worker-1')['generation'] == '1'  # the next dispatch tries again


# -- 7. off / default worker behaves like main

def test_off_worker_reuses_its_session_like_main(fx):
    fx.cfg['worker_fresh'] = {}
    worked(fx, used=20000)
    fx.handle(turn('supervisor', other()))
    fx.pump()
    assert fx.agent('worker-1')['generation'] == '0' and 'other.md' in last_to(fx, 'worker-1')
    assert not logs(fx, 'dispatch.fresh') and not logs(fx, 'dispatch.fresh_skipped')


# -- 8. hot reload

def write_roster(f, text, step):
    path = f.run_dir / 'roster.yaml'
    path.write_text(text)
    os.utime(path, ns=(1_700_000_000_000_000_000 + step, 1_700_000_000_000_000_000 + step))
    return path


def test_hot_reload_off_to_always_applies_to_the_next_dispatch(fx, monkeypatch, tmp_path):
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    path = write_roster(fx, 'workers:\n  - name: worker-1\n', 1)
    fx.cfg['roster'] = str(path)
    fx.reload_roster(path)
    assert not fx.cfg.get('worker_fresh')
    worked(fx)
    write_roster(fx, 'workers:\n  - {name: worker-1, fresh: always}\n', 2)
    fx.reload_roster(path)
    assert fx.cfg['worker_fresh'] == {'worker-1': 'always'}
    assert fleet.load(fx.path)['worker_fresh'] == {'worker-1': 'always'}  # the run's config.json
    fx.handle(turn('supervisor', other()))
    fx.pump()
    assert fx.agent('worker-1')['generation'] == '1'
    write_roster(fx, 'workers:\n  - {name: worker-1, fresh: off}\n', 3)
    fx.reload_roster(path)
    assert fx.cfg['worker_fresh'] == {} and fleet.load(fx.path)['worker_fresh'] == {}


def test_hot_reload_bad_fresh_value_is_rejected_whole(fx, monkeypatch, tmp_path):
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    path = write_roster(fx, 'workers:\n  - {name: worker-1, fresh: always}\n', 1)
    fx.cfg['roster'] = str(path)
    fx.reload_roster(path)
    write_roster(fx, 'workers:\n  - {name: worker-1, fresh: yes}\n', 2)
    fx.reload_roster(path)
    assert fx.cfg['worker_fresh'] == {'worker-1': 'always'}
    assert any('fresh must be' in e for e in logs(fx, 'roster.rejected')[-1]['errors'])


# -- 9. board and hint

def test_board_shows_fresh_always_and_no_hint_for_an_always_worker(fx):
    always(fx)
    worked(fx, used=40000)  # 42%, over the 0.30 hint ratio
    line = [x for x in last_to(fx, 'supervisor').splitlines() if x.startswith('- worker-1: ')][0]
    assert 'fresh: always' in line
    fx.handle(turn('supervisor', other(fresh=False)))
    fx.pump()
    fx.handle(turn('worker-1', DONE))
    fx.pump()
    assert '[Hint]' not in last_to(fx, 'supervisor')


def test_board_of_an_off_worker_has_no_fresh_field(fx):
    worked(fx)
    line = [x for x in last_to(fx, 'supervisor').splitlines() if x.startswith('- worker-1: ')][0]
    assert 'fresh:' not in line


def test_every_supervisor_prompt_explains_fresh_always():
    agents = fleet.HERE / 'agents'
    for name in ('supervisor.md', 'supervisor_v2.md', 'service_supervisor.md', 'service_supervisor_v2.md',
                 'service_supervisor_tui_v2.md'):
        text = (agents / name).read_text()
        assert '`fresh: always`' in text and '"fresh": false' in text, name


# -- 10. pi worker (transport v2) takes the same path

def test_pi_v2_always_worker_rotates_between_two_requests(pifx):
    f = pifx
    always(f)
    v2req(f, 'a')
    f.pump()
    settle(f, 'worker-1', receive(f, 'worker-1'))
    assert f.agent('worker-1')['state'] == 'idle' and f.agent('worker-1')['delivered'] == '1'
    old = f.agent('worker-1')['pane']
    v2req(f, 'b')
    f.pump()
    info = f.agent('worker-1')
    assert info['generation'] == '1' and info['label'] == 'worker-1.1' and info['state'] == 'starting'
    assert old in f.mgr.killed and f.v2.depth('worker-1') == 1
    fresh, = audits(f, 'dispatch.fresh')
    assert fresh['source'] == 'always'
    ready(f)
    f.pump()
    assert receive(f, 'worker-1')['request_id'] == 'b'
    assert f.agent('worker-1')['generation'] == '1'


# -- §28: a clear_after_result slot is already cleared after each result

@pytest.mark.skip(reason='docs/design.md §48: the typed §28 /clear channel is retired; start refuses '
                         '--clear-after-result')
def test_clear_after_result_slot_is_not_rotated_again(clearing, monkeypatch):
    f = clearing
    always(f, SLOT)
    request(f, 'r1')
    assert answer_turn(f, 'supervisor', route('r1'))['status'] == 'committed'
    f.pump()
    assert answer_turn(f, SLOT, '{"reply": "fills ok"}')['status'] == 'committed'
    request(f, 'r2')
    answer_turn(f, 'supervisor', route('r2'))
    f.pump()
    allow_exit(monkeypatch)
    idle_twice(f.v2)
    cleared(f)
    f.pump()
    idle_twice(f.v2)
    f.pump()
    lease = f.r.hgetall(f.v2.bus.lease_key(SLOT))
    assert lease['status'] == 'received' and f.agent(SLOT)['generation'] == '0'
    assert not audits(f, 'dispatch.fresh')
    assert 'clear_after_result' in [x.get('reason') for x in audits(f, 'dispatch.fresh_skipped')]
