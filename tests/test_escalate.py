"""Worker escalate -> supervisor approve / revise / reject -> program starts the sub-fleet (design §20).

v1 path (in-memory Redis, fake cmux) for the task-table rules; the real two-fleet v2 world of
test_subfleet for escalate -> approve -> request file -> spawn -> child completed -> task done.
"""
import json
from pathlib import Path

import pytest

from test_contracts import cf, inbox, item, report, sup, to_parallel, write  # noqa: F401  (fixture + helpers)
from test_fleet import received  # noqa: E402
from test_subfleet import (adopt_by_hook, child_daemon, given, hook, no_launch, parent_hook, step,  # noqa: F401
                           stop_child_like_daemon, tree)
from cmux_fleet import fleet, subfleet

PROPOSAL = [{'id': 'M0.0', 'instruction': 'Fetch sources one to three into m0.0.md', 'output': 'm0.0.md'},
            {'id': 'M0.1', 'instruction': 'Fetch sources four and five into m0.1.md', 'output': 'm0.1.md'}]


def escalate(task='M0', **extra):
    proposal = [dict(p, id=p['id'].replace('M0', task, 1)) for p in PROPOSAL]
    return json.dumps(dict({'escalate': task + ' is too big: five sources to fetch and merge',
                            'proposal': proposal, 'workers': 2}, **extra))


def answer(f, task, decision, **extra):
    sup(f, {'status': 'RUNNING', 'completion': 20, 'dispatch': [],
            'escalations': [dict({'task': task, 'decision': decision}, **extra)]})


@pytest.fixture
def ok_check(monkeypatch):
    """The v1 fake fleet is not a v2 Claude fleet: the slot-kind refusal is not under test here."""
    calls = []
    real = subfleet.check

    def check(f, slot, task_id, workers, ready=True, **kw):
        calls.append((slot, task_id, workers, ready))
        reason = real(f, slot, task_id, workers, ready, **kw)
        return reason if reason and 'only official Claude Code' not in reason else None
    monkeypatch.setattr(subfleet, 'check', check)
    return calls


def parallel_m0(f):
    to_parallel(f)
    sup(f, {'status': 'RUNNING', 'completion': 10, 'dispatch': [item('M0', 'worker-1', provides=['IF-M0-src'])]})
    f.r.l.clear()


# ------------------------------------------------------------------ contract task table (v1)
def test_escalate_keeps_the_row_open_and_reaches_the_supervisor(cf, ok_check):
    parallel_m0(cf)
    report(cf, 'worker-1', escalate())
    row = cf.contract_state()['tasks']['M0']
    assert row['status'] == 'escalated'  # not failed: the worker did not claim completion
    up = inbox(cf, 'supervisor')[-1]
    assert 'worker-1 asks to split M0 into 2 pieces' in up and '"escalations": [{"task": "M0"' in up
    assert '- M0.1: Fetch sources four and five into m0.1.md -> m0.1.md' in up
    assert 'M0 escalated' in up and not inbox(cf, 'worker-1')
    assert ok_check[-1] == ('worker-1', 'M0', 2, False)  # limits checked at escalate time, slot state later
    assert cf.escalation_state()['M0']['status'] == 'escalated'


def test_approve_writes_the_spawn_request_and_marks_delegated(cf, ok_check):
    parallel_m0(cf)
    report(cf, 'worker-1', escalate())
    answer(cf, 'M0', 'approve', workers=3, note='keep the source list short')
    assert cf.task()['bad_replies'] == '0', inbox(cf, 'supervisor')
    row = cf.contract_state()['tasks']['M0']
    assert row['status'] == 'delegated' and row['attempts'] == 2
    rec = cf.escalation_state()['M0']
    assert rec['status'] == 'approved' and rec['workers'] == 3
    spec = fleet.load(cf.run_dir / 'subfleet-requests' / (rec['rid'] + '.json'))
    assert spec['task_id'] == 'M0' and spec['slot'] == 'worker-1' and spec['workers'] == 3 and spec['contracts']
    assert spec['goal'].startswith('Do task M0 and write its output file') and 'keep the source list short' in spec['goal']
    assert spec['output'] == str(cf.workspace() / 'worker-1/sources.md') and spec['proposal'][1]['id'] == 'M0.1'
    # The answer file says rejected: the supervisor is told and may answer again.
    fleet.save(subfleet.answer_path(cf.run_dir, rec['rid']), {'status': 'rejected', 'reason': 'worker-1 must be idle'})
    cf.escalation_tick()
    assert cf.escalation_state()['M0']['status'] == 'escalated'
    assert cf.contract_state()['tasks']['M0']['status'] == 'escalated'
    assert 'sub-fleet M0 could not start: worker-1 must be idle' in inbox(cf, 'supervisor')[-1]
    answer(cf, 'M0', 'approve')
    rid = cf.escalation_state()['M0']['rid']
    fleet.save(subfleet.answer_path(cf.run_dir, rid), {'status': 'accepted', 'run': '/x', 'task_id': 'M0'})
    cf.escalation_tick()
    assert cf.escalation_state()['M0'] == dict(cf.escalation_state()['M0'], status='delegated', run='/x')


@pytest.mark.parametrize('decision,text', [('revise', 'asks you to revise your split of M0: two pieces only'),
                                           ('reject', 'rejected splitting M0: two pieces only\nDo M0 yourself now.')])
def test_revise_and_reject_go_back_to_the_worker(cf, ok_check, decision, text):
    parallel_m0(cf)
    report(cf, 'worker-1', escalate())
    answer(cf, 'M0', decision, note='two pieces only')
    msg = inbox(cf, 'worker-1')[-1]
    assert text in msg and '[M0]' in msg
    state = cf.contract_state()
    assert state['tasks']['M0']['status'] == 'running' and state['owners']['worker-1'][-1] == 'M0'
    assert 'M0' not in cf.escalation_state()
    write(cf, 'worker-1/sources.md', '## Sources\n| https://a |\n')
    report(cf, 'worker-1', 'Interfaces: IF-M0-src@1 provided: ready')  # the next report closes M0 as usual
    assert cf.contract_state()['tasks']['M0']['status'] == 'done'


def test_limit_refusal_reaches_worker_and_supervisor(cf, ok_check):
    parallel_m0(cf)
    report(cf, 'worker-1', escalate(workers=5))
    assert 'refused by the program: child_workers_max 3: asked for 5 workers' in inbox(cf, 'worker-1')[-1]
    assert '[escalate] refused for worker-1: child_workers_max 3' in inbox(cf, 'supervisor')[-1]
    state = cf.contract_state()
    assert state['tasks']['M0']['status'] == 'running' and state['owners']['worker-1'] == ['M0']
    assert cf.escalation_state() == {}
    # A proposal that is not <own task>.<n>: refused with the rule.
    report(cf, 'worker-1', escalate(task='M1'))
    assert 'M1 is not a running task of worker-1' in inbox(cf, 'worker-1')[-1]
    report(cf, 'worker-1', json.dumps({'escalate': 'too big', 'proposal': [dict(PROPOSAL[0], id='M5')]}))
    assert 'proposal ids must be distinct and all <your task id>.<n>' in inbox(cf, 'worker-1')[-1]


def test_approve_limit_is_no_strike_and_bad_answers_are(cf, ok_check):
    parallel_m0(cf)
    report(cf, 'worker-1', escalate())
    answer(cf, 'M0', 'approve', workers=4)
    assert cf.task()['bad_replies'] == '0'
    assert 'approve of M0 refused by the program: child_workers_max 3' in inbox(cf, 'supervisor')[-1]
    assert cf.escalation_state()['M0']['status'] == 'escalated'
    answer(cf, 'M7', 'approve')
    assert 'M7 has no open escalate request' in inbox(cf, 'supervisor')[-1] and cf.task()['bad_replies'] == '1'
    answer(cf, 'M0', 'maybe')
    assert 'decision approve / revise / reject' in inbox(cf, 'supervisor')[-1]
    cf.r.hset(fleet.TASK, mapping={'bad_replies': 0})
    sup(cf, {'status': 'RUNNING', 'completion': 20, 'escalations': [{'task': 'M0', 'decision': 'approve'}],
             'dispatch': [item('M2', 'worker-1', consumes=['IF-M0-src@1', 'IF-M1-metrics@1'])]})
    assert 'do not dispatch to it in the same reply' in inbox(cf, 'supervisor')[-1]


def test_child_completion_closes_the_row_and_releases_dependents(cf, ok_check):
    to_parallel(cf)
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [
        item('M0', 'worker-1', provides=['IF-M0-src']),
        item('M9', 'worker-3', depends_on=['M0'], serial_reason='integration')]})
    report(cf, 'worker-1', escalate())
    answer(cf, 'M0', 'approve')
    cf.save_escalations(dict(cf.escalation_state(), M0=dict(cf.escalation_state()['M0'], status='delegated')))
    # failed: the row fails with the attempt
    extra = cf.escalation_finished({'task_id': 'M0', 'attempt': 1, 'reason': 'budget', 'outcome': 'failed'}, False)
    assert 'M0 failed' in extra and cf.contract_state()['tasks']['M0']['reason'] == 'sub-fleet failed (attempt 1): budget'
    # completed, but the delivered file misses the shape: failed too
    cf.save_escalations({'M0': {'status': 'delegated', 'slot': 'worker-1'}})
    write(cf, 'worker-1/sources.md', 'no heading')
    cf.escalation_finished({'task_id': 'M0', 'attempt': 2}, True)
    assert 'missing "## Sources"' in cf.contract_state()['tasks']['M0']['reason']
    # completed with the right shape: done, interface ready, consumers told, held M9 released
    cf.save_escalations({'M0': {'status': 'delegated', 'slot': 'worker-1'}})
    write(cf, 'worker-1/sources.md', '## Sources\n| https://a |\n')
    extra = cf.escalation_finished({'task_id': 'M0', 'attempt': 3}, True)
    state = cf.contract_state()
    assert state['tasks']['M0']['status'] == 'done' and state['interfaces']['IF-M0-src@1']['status'] == 'ready'
    assert state['tasks']['M9']['status'] == 'running' and '[M9]' in inbox(cf, 'worker-3')[-1]
    assert '[contract] ready IF-M0-src@1' in extra and cf.escalation_state() == {}
    assert cf.escalation_finished({'task_id': 'M0'}, True) == ''  # an operator spawn: no table change


def test_plain_task_dispatch_gets_an_id_and_can_be_split(cf, ok_check):
    # §44: a dispatch item without `id` gets the next free M<n>, so any plain task can be split.
    cf.handle({'kind': 'task', 'request_id': 'r1', 'spec': {'goal': 'research report'}})
    sup(cf, {'status': 'RUNNING', 'completion': 1, 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Write worker-1/x.md with the findings'}]})
    assert '] [M0] Write worker-1/x.md' in inbox(cf, 'worker-1')[-1]
    report(cf, 'worker-1', escalate())
    assert 'worker-1 asks to split M0' in inbox(cf, 'supervisor')[-1]
    answer(cf, 'M0', 'approve')
    rec = cf.escalation_state()['M0']
    spec = fleet.load(cf.run_dir / 'subfleet-requests' / (rec['rid'] + '.json'))
    assert spec['goal'] == 'Write worker-1/x.md with the findings' and not spec['contracts'] and spec['output'] is None
    sup(cf, {'status': 'RUNNING', 'completion': 2, 'dispatch': [{'id': 'M0x', 'to': 'worker-1',
                                                                  'instruction': 'Write worker-1/x.md now please'}]})
    assert 'every dispatch item needs an id (M<n>' in inbox(cf, 'supervisor')[-1]


def test_redispatch_drops_an_open_escalation(cf, ok_check):
    parallel_m0(cf)
    report(cf, 'worker-1', escalate())
    sup(cf, {'status': 'RUNNING', 'completion': 20, 'dispatch': [item('M0', 'worker-1', provides=['IF-M0-src'])]})
    assert cf.escalation_state() == {} and cf.contract_state()['tasks']['M0']['status'] == 'running'


# ------------------------------------------------------------------ guards
def inbox_log(f):
    return f.r.l.get(fleet.LOG, [])


def test_tick_parts_never_kill_the_daemon(cf, monkeypatch):
    def boom(f):
        raise RuntimeError('broken child record')
    monkeypatch.setattr(subfleet, 'tick', boom)
    monkeypatch.setattr(cf, 'roster_tick', lambda: (_ for _ in ()).throw(OSError('disk')))
    cf.tick()
    errors = [json.loads(x) for x in inbox_log(cf) if json.loads(x)['kind'] == 'tick.error']
    assert {e['part'] for e in errors} == {'roster', 'subfleet'} and 'broken child record' in errors[1]['error']
    from cmux_fleet.message_bus import BusError
    monkeypatch.setattr(cf, 'write_result', lambda: (_ for _ in ()).throw(BusError('ambiguous')))
    with pytest.raises(BusError):
        cf.tick()


def test_broken_roster_yaml_keeps_the_last_limits(tmp_path):
    bad = tmp_path / 'roster.yaml'
    bad.write_text('workers:\n  - name: worker-1\nlimits: {max_children: [\n')
    assert subfleet.limits({'run': str(tmp_path), 'roster': str(bad)}) == subfleet.LIMITS


# ------------------------------------------------------------------ v2: escalate -> approve -> spawn -> done
def test_v2_escalate_approve_spawn_and_completion(tree, monkeypatch):
    t, f = tree, tree.f
    monkeypatch.setattr(subfleet, 'default_launch', no_launch)
    decision = {'status': 'RUNNING', 'completion': 5, 'reason': 'go', 'dispatch': [
        {'id': 'M0', 'to': 'worker-1', 'instruction': 'Research five sources and write worker-1/m0.md'}]}
    assert hook(t, f, 'supervisor', json.dumps(decision))['status'] == 'committed'
    step(t)
    work = given(t, f, 'worker-1')[-1]
    assert '[M0] Research five sources' in work
    text = ('Done:\n- read the goal\nEvidence:\n- five sources listed in the instruction\nNot done: all of it\n'
            'Blocked: none\n' + escalate())
    assert hook(t, f, 'worker-1', text, prompt=work)['status'] == 'committed'
    step(t)
    asked = received(t.world, f.agent('supervisor')['pane'])[-1]
    assert 'worker-1 asks to split M0 into 2 pieces' in asked
    approve = {'status': 'RUNNING', 'completion': 10, 'reason': 'split', 'dispatch': [],
               'escalations': [{'task': 'M0', 'decision': 'approve'}]}
    assert hook(t, f, 'supervisor', json.dumps(approve), prompt=asked)['status'] == 'committed'
    assert f.task()['bad_replies'] == '0'
    rid = f.escalation_state()['M0']['rid']
    f.tick()  # the daemon tick runs the request: worker-1's pane goes to sub-fleet M0
    assert fleet.load(subfleet.answer_path(f.run_dir, rid))['status'] == 'accepted'
    assert f.escalation_state()['M0']['status'] == 'delegated'
    assert f.agent('worker-1')['state'] == 'delegated'
    rec = subfleet.record(f.run_dir / 'sub' / 'M0')
    cf, _ = adopt_by_hook(t, monkeypatch, rec)  # §48: the role change comes through the pane's hook, never typed
    step(t)
    goal = cf.task()['goal']
    assert 'Research five sources and write worker-1/m0.md' in goal and '"M0.1"' in goal
    cf.v2.finish('completed', 'merged')
    cf.write_result()
    f.tick()
    stop_child_like_daemon(cf)
    f.tick()
    assert subfleet.record(rec['run'])['state'] == 'returned'
    assert f.escalation_state() == {}
    msgs = [f.v2.bus.message(m)['body']['text'] for m in f.v2.bus.pending('supervisor')]
    assert any('[Sub-fleet M0 completed]' in x for x in msgs)


# ------------------------------------------------------------------ sub-fleet guards (Fable r1 P2-01..03)
def test_one_bad_request_is_answered_and_never_recrashes(tree, monkeypatch):
    f = tree.f
    monkeypatch.setattr(subfleet, 'spawn_child', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    rid = subfleet.request(f.run_dir, 'worker-1', 'M3', 'goal', 1)
    subfleet.tick(f)
    assert fleet.load(subfleet.answer_path(f.run_dir, rid)) == {'status': 'rejected', 'reason': 'bad request: boom'}
    assert not (f.run_dir / 'subfleet-requests' / (rid + '.json')).exists()


def test_record_without_config_is_marked_failed(tree):
    f = tree.f
    half = f.run_dir / 'sub' / 'M4'
    half.mkdir(parents=True)
    subfleet.save_record(half, {'task_id': 'M4', 'slot': 'worker-2', 'state': 'starting'})
    subfleet.tick(f)
    assert subfleet.record(half)['state'] == 'failed' and 'config.json' in subfleet.record(half)['reason']
    subfleet.save_record(half, {'task_id': 'M4', 'slot': 'worker-2', 'state': 'running'})
    assert subfleet.stop_children(f, wait=0) and subfleet.record(half)['state'] == 'failed'
    subfleet.save_record(half, {'task_id': 'M4', 'slot': 'worker-2', 'state': 'running'})
    subfleet.resume_children(f, launch=lambda *a: None)
    assert subfleet.record(half)['state'] == 'failed'


def test_child_daemon_gone_without_result_counts_as_failed(tree):
    t, f = tree, tree.f
    rec = subfleet.spawn_child(f, 'worker-1', 'M0', 'Research five sources and merge them', 1, launch=no_launch)
    subfleet.tick(f)  # just launched: the daemon has LAUNCH_GRACE to take its lock
    assert subfleet.record(rec['run'])['state'] == 'running'
    subfleet.save_record(rec['run'], dict(subfleet.record(rec['run']), launched=0, created=0))
    subfleet.tick(f)  # no daemon, no result.json, parent task still running: collected as failed
    rec = subfleet.record(rec['run'])
    assert rec['state'] == 'failed' and rec['outcome'] == 'cancelled'
    assert rec['reason'] == 'the sub-fleet daemon exited without writing result.json'
    assert f.agent('worker-1')['state'] == 'starting'  # the pane came back to the parent


# ------------------------------------------------------------------ sub-fleet interfaces: notify the top, veto
def test_child_interfaces_notify_the_parent_and_the_top_can_veto(cf, tmp_path):
    parent_run = tmp_path / 'parent-run'
    parent_run.mkdir()
    fleet.save(tmp_path / 'parent.json', {'run': str(parent_run)})
    cf.cfg.update(parent_config=str(tmp_path / 'parent.json'), task_id='M0')
    to_parallel(cf)  # the child's skeleton passes: every interface is in effect and reported up
    notes = list((parent_run / 'subfleet-notices').glob('*.json'))
    note = fleet.load(notes[0])
    assert len(notes) == 1 and note['origin_run'] == str(cf.run_dir) and note['task'] == 'M0'
    assert [i['ref'] for i in note['interfaces']] == ['IF-M0-src@1', 'IF-M1-metrics@1']
    # this fleet as the top: the notice reaches its supervisor, which vetoes one interface
    cf.cfg.pop('parent_config')
    (cf.run_dir / 'subfleet-notices').mkdir()
    fleet.save(cf.run_dir / 'subfleet-notices' / 'n1.json', dict(note, origin_run=str(cf.run_dir)))
    cf.relay_notices()
    up = inbox(cf, 'supervisor')[-1]
    # §26: the path is absolute (the child's workspace), not readable as the parent's worker-1/...
    assert up.startswith('[sub-fleet interfaces] sub-fleet M0 registered: IF-M0-src@1 (new, '
                         + str(cf.workspace() / 'worker-1/sources.md') + ')')
    assert '"decision": "veto"' in up and not (cf.run_dir / 'subfleet-notices' / 'n1.json').exists()
    sup(cf, {'status': 'RUNNING', 'completion': 5, 'interfaces': [
        {'id': 'IF-M0-src', 'version': 1, 'decision': 'veto', 'note': 'duplicates M1'}],
        'dispatch': [item('M1', 'worker-2', provides=['IF-M1-metrics'])]})
    assert cf.task()['bad_replies'] == '0', inbox(cf, 'supervisor')
    vetoes = list((cf.run_dir / 'subfleet-vetoes').glob('*.json'))
    assert fleet.load(vetoes[0]) == {'ref': 'IF-M0-src@1', 'note': 'duplicates M1'}
    # the sub-fleet (here the same fleet) withdraws it on its next tick
    cf.apply_vetoes()
    e = cf.contract_state()['interfaces']['IF-M0-src@1']
    assert e['status'] == 'deprecated' and e['deprecated_by'] == 'veto by the top supervisor'
    assert 'the top supervisor vetoed IF-M0-src@1: duplicates M1' in inbox(cf, 'supervisor')[-1]
    assert not list((cf.run_dir / 'subfleet-vetoes').glob('*.json'))
    # a veto of an interface no sub-fleet registered (or already vetoed) is a correction
    sup(cf, {'status': 'RUNNING', 'completion': 5, 'interfaces': [
        {'id': 'IF-M0-src', 'version': 1, 'decision': 'veto'}], 'dispatch': []})
    assert 'veto applies only to interfaces a sub-fleet registered' in inbox(cf, 'supervisor')[-1]


def test_a_middle_fleet_passes_notices_up(cf, tmp_path):
    top = tmp_path / 'top-run'
    top.mkdir()
    fleet.save(tmp_path / 'top.json', {'run': str(top)})
    cf.cfg['parent_config'] = str(tmp_path / 'top.json')
    (cf.run_dir / 'subfleet-notices').mkdir()
    fleet.save(cf.run_dir / 'subfleet-notices' / 'n2.json', {'origin_run': '/grandchild', 'task': 'M0.1', 'interfaces': []})
    cf.relay_notices()
    assert fleet.load(top / 'subfleet-notices' / 'n2.json')['origin_run'] == '/grandchild'
