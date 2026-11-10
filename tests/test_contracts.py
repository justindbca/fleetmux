"""Interface contracts (task spec "contracts": true): registry, task table, P0/P1 gate, change flow.
In-memory Redis + fake cmux (v1 path) plus one real-Redis v2 check of the P0 exemption."""
import json
import sys
from pathlib import Path

import pytest

import test_fleet
from test_fleet import FakeMgr, FakeRedis, write_rollout
from cmux_fleet import contracts, fleet, runtime_v2

WORKERS = ['worker-1', 'worker-2', 'worker-3']


def iface(i, producer, path, consumers=(), **extra):
    e = {'id': i, 'version': 1, 'producer': producer, 'consumers': list(consumers), 'path': path,
         'contains': ['## Sources'], 'sample': '## Sources\n| url |\n'}
    e.update(extra)
    return e


PLAN = [{'id': 'M0', 'goal': 'fetch sources', 'provides': ['IF-M0-src'], 'consumes': []},
        {'id': 'M1', 'goal': 'compute metrics', 'provides': ['IF-M1-metrics'], 'consumes': []},
        {'id': 'M2', 'goal': 'write the report', 'provides': [], 'consumes': ['IF-M0-src@1', 'IF-M1-metrics@1']}]
IFACES = [iface('IF-M0-src', 'M0', 'worker-1/sources.md', ['M2']),
          iface('IF-M1-metrics', 'M1', 'worker-2/metrics.json', ['M2'], contains=[], json_keys=['symbol', 'iv_rank'],
                writers={'symbol': 'M1', 'iv_rank': 'M1'}, sample='{"symbol": "GOOG", "iv_rank": 0.42}',
                decisions=[{'q': 'time zone of fetched_at', 'answer': 'UTC', 'status': 'pending_user'}])]


def p0_reply(plan=PLAN, interfaces=IFACES, **extra):
    return dict({'status': 'RUNNING', 'completion': 0, 'reason': 'fix interfaces', 'phase': 'interfaces',
                 'plan': plan, 'interfaces': interfaces, 'dispatch': []}, **extra)


# ------------------------------------------------------------------ pure rules
def test_p0_accepts_complete_interfaces_and_freezes_nothing_yet():
    plan, entries = contracts.p0(p0_reply())
    assert sorted(entries) == ['IF-M0-src@1', 'IF-M1-metrics@1']
    assert all(e['status'] == 'proposed' for e in entries.values())
    assert entries['IF-M0-src@1']['consumers'] == ['M2']


@pytest.mark.parametrize('mutate,text', [
    (lambda d: d.update(dispatch=[{'to': 'worker-1', 'instruction': 'do the thing now please'}]),
     'P0: interfaces are not fixed yet; reply with phase "interfaces", a full plan and no dispatch'),
    (lambda d: d.pop('phase'), 'P0: interfaces are not fixed yet'),
    (lambda d: d['interfaces'].pop(0), 'P0: M2 consumes IF-M0-src@1 but no task provides it'),
    (lambda d: d['plan'].pop(2) and d['interfaces'][0].update(consumers=[]),
     'P0: IF-M0-src is provided by M0 but nobody consumes it; add a consumer or mark final'),
    (lambda d: d['plan'][0]['consumes'].append('IF-M1-metrics@1') or d['plan'][1]['consumes'].append('IF-M0-src@1'),
     'P0: interface cycle: M0 -> M1 -> M0'),
    (lambda d: d['interfaces'][0].update(sample='no heading'), 'P0: IF-M0-src sample fails its own check: missing "## Sources"'),
    (lambda d: d['interfaces'][0].pop('sample'), 'sample is required and must itself satisfy min_chars / contains / json_keys'),
    (lambda d: d['interfaces'][1]['writers'].pop('iv_rank'), 'P0: field iv_rank of IF-M1-metrics has no writer'),
    (lambda d: d['interfaces'][1]['writers'].update(iv_rank=['M1', 'M2']),
     'P0: field iv_rank of IF-M1-metrics has two writers: M1, M2'),
    (lambda d: d['interfaces'][0].update(id='IF-x'), 'interface id must match'),
    (lambda d: d['interfaces'][0].update(decisions=[{'q': 'tz', 'answer': 'UTC', 'status': 'maybe'}]),
     'decisions must be a list of {q, answer, status: pending_user|final}'),
])
def test_p0_rejects_each_gap_by_name(mutate, text):
    d = json.loads(json.dumps(p0_reply()))
    mutate(d)
    with pytest.raises(ValueError) as exc:
        contracts.p0(d)
    assert text in str(exc.value)


def test_final_interface_needs_no_consumer():
    d = json.loads(json.dumps(p0_reply()))
    d['plan'].pop(2)
    for e in d['interfaces']:
        e.update(consumers=[], final=True)
    contracts.p0(d)


def frozen_state():
    state = contracts.new_state('worker-1')
    state['plan'], state['interfaces'] = contracts.p0(p0_reply())
    for e in state['interfaces'].values():
        e['status'] = 'declared'
    state['tasks'] = {p['id']: {'id': p['id'], 'status': 'pending', 'attempts': 0, 'stale': [],
                                'provides': p['provides'], 'consumes': p['consumes'], 'depends_on': []}
                      for p in state['plan']}
    state['phase'] = 'parallel'
    return state


def item(m, to, **extra):
    return dict({'id': m, 'to': to, 'instruction': 'Do task ' + m + ' and write its output file'}, **extra)


@pytest.mark.parametrize('items,text', [
    ([item('M0', 'worker-1', provides=['IF-M9-x'])],
     'dispatch M0 provides IF-M9-x which is not a registered interface: declare it in interfaces first'),
    ([item('M1', 'worker-2', provides=['IF-M0-src'])], 'IF-M0-src is produced by M0, not M1'),
    ([item('M0', 'worker-2', provides=['IF-M0-src'])], "path must be inside the producer's own directory"),
    ([item('M2', 'worker-3', consumes=['IF-M0-src@3'])], 'dispatch M2 consumes IF-M0-src@3 which is not a registered interface'),
    ([item('M2', 'worker-3', consumes=['IF-M0-src'])], 'consumes entries must be id@version'),
    ([item('M2', 'worker-3', depends_on=['M0'])],
     'depends_on requires serial_reason (integration / exploratory); if the dependency is an interface, consume its sample instead'),
    ([item('M2', 'worker-3', depends_on=['M0'], serial_reason='soon')], 'serial_reason must be integration or exploratory'),
    ([item('M0', 'worker-1', depends_on=['M2'], serial_reason='integration'),
      item('M2', 'worker-3', depends_on=['M0'], serial_reason='integration')], 'depends_on cycle: M0 -> M2 -> M0'),
    ([item('M2', 'worker-3', depends_on=['M0'], serial_reason='exploratory')],
     'an exploratory dependency may not also provide an interface'),
    ([item('M0', 'worker-1', edits=['src/a.py']), item('M1', 'worker-2', edits=['src/a.py'])],
     'edits conflict: src/a.py is assigned to M0 and M1 in this round; give it to one task and have the other call a function it exposes'),
    ([item('worker', 'worker-1')], 'every dispatch item needs an id'),
])
def test_dispatch_rules_reject_with_spec_texts(items, text):
    with pytest.raises(ValueError) as exc:
        contracts.dispatch(frozen_state(), items, lambda x: x)
    assert text in str(exc.value)


def test_deprecated_and_proposed_versions_cannot_be_consumed():
    state = frozen_state()
    state['interfaces']['IF-M0-src@1'].update(status='deprecated', deprecated_by='IF-M0-src@2')
    with pytest.raises(ValueError, match='dispatch M2 consumes IF-M0-src@1 which is deprecated by IF-M0-src@2'):
        contracts.dispatch(state, [item('M2', 'worker-3', consumes=['IF-M0-src@1'])], lambda x: x)
    state['pending']['IF-M0-src@2'] = {}
    with pytest.raises(ValueError, match=r'IF-M0-src@2 is proposed, not approved'):
        contracts.dispatch(state, [item('M2', 'worker-3', consumes=['IF-M0-src@2'])], lambda x: x)


def test_dependent_item_is_held_until_prerequisites_are_done():
    state = frozen_state()
    now, held = contracts.dispatch(state, [item('M0', 'worker-1', provides=['IF-M0-src']),
                                           item('M9', 'worker-3', depends_on=['M0'], serial_reason='integration')],
                                   lambda x: x)
    assert [r['id'] for r in now] == ['M0'] and [r['id'] for r in held] == ['M9']
    assert contracts.releasable(state) == []  # plan rows without an instruction are never released
    state['tasks']['M0']['status'] = 'done'
    assert [r['id'] for r in contracts.releasable(state)] == ['M9']


def test_change_classification_by_field_difference():
    state = frozen_state()
    with pytest.raises(ValueError, match='change declared compatible but path changed: it is breaking, version must be 2'):
        contracts.merge_change(state, {'id': 'IF-M0-src', 'version': 1, 'change': 'compatible',
                                       'path': 'worker-1/other.md'}, {'M0', 'M1', 'M2'})
    kind, entry = contracts.merge_change(state, {'id': 'IF-M0-src', 'version': 1, 'change': 'compatible',
                                                 'contains': ['## Sources', '| url |']}, {'M0', 'M1', 'M2'})
    assert kind == 'compatible' and entry['version'] == 1
    with pytest.raises(ValueError, match='IF-M0-src@1 is already registered with different fields: bump the version'):
        contracts.merge_change(state, dict(IFACES[0], contains=['x'], sample='x'), {'M0', 'M1', 'M2'})


def test_last_json_object_ignores_json_quoted_in_evidence():
    text = ('Done:\n- wrote it\nEvidence:\n- sample {"symbol": "GOOG"} matched\nNot done: none\nBlocked: none\n'
            'Interfaces: IF-M0-src@1 provided: ready\nQuestion for supervisor: none\n'
            '{"interface_change": {"id": "IF-M0-src", "version": 2, "change": "breaking"}}')
    fields = contracts.report_fields(text)
    assert fields['interface_change']['version'] == 2
    assert contracts.ready_claims(fields['interfaces']) == [('IF-M0-src', 1)]
    assert contracts.ready_claims('IF-M0-src@1 provided: not ready') == []
    # The v2 TUI mapping keeps the four sections and adds the contract keys only when present.
    value = runtime_v2.tui_candidates('worker-1', 'worker', text)[0]['value']
    assert value['done'] == ['wrote it'] and value['blocked'] == 'none'
    assert value['interface_change']['id'] == 'IF-M0-src' and value['interfaces'].startswith('IF-M0-src@1')
    plain = 'Done:\n- x\nEvidence:\n- y {"a": 1}\nNot done: none\nBlocked: none'
    assert set(runtime_v2.tui_candidates('worker-1', 'worker', plain)[0]['value']) == {'done', 'evidence', 'not_done', 'blocked'}


@pytest.mark.parametrize('heading', [
    # 2026-10-28 ci-1 (Opus 5.5): three reports rejected as "exactly one structured candidate is required"
    'Evidence (`FLEETMUX_GATE=m3`, temporary helper copy in place):',
    'Evidence (with `FLEETMUX_GATE=m3`):',
    'Evidence (`FLEETMUX_GATE=m3`):',
    '**Evidence** (gate m3):',
])
def test_a_heading_with_a_parenthetical_qualifier_is_still_the_section(heading):
    text = 'Done:\n- merged 9e246a51\n' + heading + '\n- 33 tests collected, 0 errors\nNot done: none.\n\nBlocked: none.'
    value, = [c['value'] for c in runtime_v2.tui_candidates('worker-1', 'worker', text)]
    assert value['done'] == ['merged 9e246a51'] and value['evidence'] == ['33 tests collected, 0 errors']
    assert value['not_done'] == 'none.' and value['blocked'] == 'none.'


# ------------------------------------------------------------------ fleet wiring (v1 path)
THREADS = {n: str(i) * 8 + '-1111-1111-1111-111111111111' for i, n in enumerate(['supervisor'] + WORKERS, 1)}


@pytest.fixture
def cf(tmp_path):
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    cfg = {'run': str(run), 'redis_socket': '/tmp/cmux-fleet-test.sock', 'python': sys.executable,
           'workers': list(WORKERS), 'handoff_ratio': .6, 'handoff_timeout': 900, 'window': 95000,
           'codex_home': str(tmp_path / 'codex-home')}
    (run / 'config.json').write_text(json.dumps(cfg))
    sessions = tmp_path / 'sessions'
    f = fleet.Fleet(run / 'config.json', FakeRedis(), FakeMgr(), sessions=sessions)
    f.start_all()
    for name in f.names:
        write_rollout(sessions, THREADS[name], fleet.agent_cwd(cfg, name))
        f.handle(turn(name, 'READY'))
    return f


def turn(name, text):
    q = test_fleet.CODEX_QUEUE.pop(THREADS[name], None)  # the queued delivery this turn answers
    return {'kind': 'turn', 'agent': name, 'generation': 0, 'thread': THREADS[name], 'text': text,
            **({'turn': q['turn']} if q else {})}


def submit(f, **spec):
    f.handle({'kind': 'task', 'request_id': 'r1', 'spec': dict({'goal': 'research report'}, **spec)})
    assert json.loads(f.r.get('fx:reply:r1'))['status'] == 'accepted', f.r.get('fx:reply:r1')


def sup(f, obj):
    f.handle(turn('supervisor', json.dumps(obj)))


def inbox(f, name):
    return f.r.l.get('fx:inbox:' + name, [])


def report(f, name, extra=''):
    f.handle(turn(name, 'Done:\n- did it\nEvidence:\n- ' + name + '/x\nNot done: none\nBlocked: none\n' + extra))


def write(f, rel, text):
    path = f.workspace() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def to_parallel(f, **spec):
    submit(f, contracts=True, **spec)
    sup(f, p0_reply())
    write(f, 'worker-1/skeleton.md', 'IF-M0-src@1 · M0 → M2 · PASS · ok\nIF-M1-metrics@1 · M1 → M2 · PASS · ok\n')
    report(f, 'worker-1')
    assert f.contract_state()['phase'] == 'parallel'
    f.r.l.clear()


def test_task_without_contracts_is_unchanged(cf):
    submit(cf)
    assert cf.r.get(fleet.CONTRACTS) is None and cf.task().get('contracts') is None
    assert not (fleet.agent_cwd(cf.cfg, 'supervisor') / 'CONTRACTS.md').exists()
    assert 'Interface contracts' not in inbox(cf, 'supervisor')[-1]
    sup(cf, {'status': 'RUNNING', 'completion': 1, 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Write worker-1/x.md with the findings'}]})
    assert inbox(cf, 'worker-1')[-1].startswith('[Supervisor instruction ')
    report(cf, 'worker-1')
    assert inbox(cf, 'supervisor')[-1] == ('[worker-1 report]\nDone:\n- did it\nEvidence:\n- worker-1/x\nNot done: '
                                           'none\nBlocked: none\n\n\nWorker status now (state, queue, context, last task):\n'
                                           '- worker-1: idle, 1 queued\n- worker-2: idle\n- worker-3: idle\n\n'  # §37 board
                                           'Reply with the JSON format from AGENTS.md.')
    assert not (cf.workspace() / 'contracts.json').exists()
    cf.finish('cancelled', 'next')
    with pytest.raises(ValueError, match='contracts must be a boolean'):
        cf.submit({'goal': 'g', 'contracts': 'yes'})


def test_p0_gate_then_skeleton_then_frozen(cf):
    submit(cf, contracts=True)
    prompt = inbox(cf, 'supervisor')[-1]
    assert 'Interface contracts are ON' in prompt and 'phase "interfaces"' in prompt
    for name in cf.names:
        assert '## 4. Interface contracts' in (fleet.agent_cwd(cf.cfg, name) / 'CONTRACTS.md').read_text()
    # P0: a dispatch is refused with the spec text; an empty-dispatch RUNNING is not "idle".
    sup(cf, p0_reply(dispatch=[{'to': 'worker-1', 'instruction': 'Fetch sources into worker-1/sources.md'}]))
    assert 'P0: interfaces are not fixed yet' in inbox(cf, 'supervisor')[-1] and cf.task()['bad_replies'] == '0'
    bad = json.loads(json.dumps(p0_reply()))
    bad['interfaces'][0]['sample'] = 'nothing'
    sup(cf, bad)
    assert 'P0: IF-M0-src sample fails its own check' in inbox(cf, 'supervisor')[-1]
    sup(cf, p0_reply())
    assert cf.task()['bad_replies'] == '0'
    state = cf.contract_state()
    assert state['phase'] == 'skeleton' and state['skeleton_tries'] == 1 and set(state['tasks']) == {'M0', 'M1', 'M2'}
    skeleton = inbox(cf, 'worker-1')[-1]
    assert '[skeleton]' in skeleton and 'skeleton.md' in skeleton
    registry = json.loads((cf.workspace() / 'contracts.json').read_text())
    assert registry['phase'] == 'skeleton' and len(registry['interfaces']) == 2
    assert registry['interfaces'][0]['consumers_runtime'][0]['m'] == 'M2'
    assert (cf.run_dir / 'contracts.json').read_text() == (cf.workspace() / 'contracts.json').read_text()
    # P1: supervisor may idle (no dispatch) but may not dispatch.
    sup(cf, {'status': 'RUNNING', 'completion': 1, 'dispatch': []})
    assert cf.task()['bad_replies'] == '0'
    sup(cf, {'status': 'RUNNING', 'completion': 1, 'dispatch': [
        {'id': 'M0', 'to': 'worker-1', 'instruction': 'Fetch sources into worker-1/sources.md'}]})
    assert 'P1: the skeleton run is in progress' in inbox(cf, 'supervisor')[-1]
    write(cf, 'worker-1/skeleton.md', 'IF-M0-src@1 · M0 → M2 · PASS · ok\nIF-M1-metrics@1 · M1 → M2 · PASS · ok\n')
    report(cf, 'worker-1')
    state = cf.contract_state()
    assert state['phase'] == 'parallel' and {e['status'] for e in state['interfaces'].values()} == {'declared'}
    assert '[contract] interfaces frozen at v1' in inbox(cf, 'supervisor')[-1]
    log = [json.loads(x) for x in (cf.run_dir / 'contracts.log.jsonl').read_text().splitlines()]
    assert [x['action'] for x in log] == ['p0.passed', 'skeleton']


def test_skeleton_fail_returns_to_p0_without_cap(cf):
    submit(cf, contracts=True)
    sup(cf, p0_reply())
    write(cf, 'worker-1/skeleton.md', 'IF-M0-src@1 · M0 → M2 · FAIL · column missing\n')
    report(cf, 'worker-1')
    assert cf.contract_state()['phase'] == 'interfaces'
    msg = inbox(cf, 'supervisor')[-1]
    assert '[contract] skeleton run FAILED (try 1)' in msg and 'column missing' in msg
    assert 'no PASS line for IF-M1-metrics@1' in msg
    for _ in range(3):  # user 10/13: no retry cap; only the hours budget bounds it
        sup(cf, p0_reply())
        report(cf, 'worker-1')
        assert cf.task()['status'] == 'running' and cf.contract_state()['phase'] == 'interfaces'


def test_p0_gap_lists_never_block(cf):
    submit(cf, contracts=True)
    bad = json.loads(json.dumps(p0_reply()))
    bad['interfaces'][0]['sample'] = 'nothing'
    for _ in range(5):
        sup(cf, bad)
        assert 'P0: IF-M0-src sample fails its own check' in inbox(cf, 'supervisor')[-1]
    assert cf.task()['status'] == 'running' and cf.task()['bad_replies'] == '0'


def test_p0_entry_field_gaps_never_block(cf):
    submit(cf, contracts=True)
    bad = json.loads(json.dumps(p0_reply()))
    bad['interfaces'][1]['path'] = 'metrics.json'
    for _ in range(4):
        sup(cf, bad)
        assert inbox(cf, 'supervisor')[-1].count('P0') >= 1
    assert cf.task()['status'] == 'running' and cf.task()['bad_replies'] == '0'


def test_skeleton_header_with_literal_pass_fail_is_not_a_failure():
    text = ('IF-x@1 · producer → consumer · PASS/FAIL · evidence\n| interface | verdict (PASS/FAIL) |\n'
            'IF-M0-src@1 · M0 → M2 · PASS · ok\n')
    assert contracts.skeleton_verdict(text, ['IF-M0-src@1']) == []
    assert contracts.skeleton_verdict('IF-M0-src@1 · M0 → M2 · FAIL · bad\n', ['IF-M0-src@1'])


def test_parallel_dispatch_ready_notices_and_release(cf):
    to_parallel(cf)
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [
        item('M0', 'worker-1', provides=['IF-M0-src']), item('M1', 'worker-2', provides=['IF-M1-metrics']),
        item('M2', 'worker-3', consumes=['IF-M0-src@1', 'IF-M1-metrics@1']),
        item('M9', 'worker-3', depends_on=['M0', 'M1'], serial_reason='integration')]})
    assert cf.task()['bad_replies'] == '0', inbox(cf, 'supervisor')
    assert len(inbox(cf, 'worker-3')) == 1 and '[M2]' in inbox(cf, 'worker-3')[0]
    assert 'consumes IF-M0-src@1, IF-M1-metrics@1' in inbox(cf, 'worker-3')[0]
    tasks = cf.contract_state()['tasks']
    assert tasks['M9']['status'] == 'pending' and tasks['M0']['attempts'] == 1
    # A ready claim that fails the literal check: not ready, producer and consumer told.
    write(cf, 'worker-1/sources.md', 'no heading here')
    report(cf, 'worker-1', 'Interfaces: IF-M0-src@1 provided: ready\nQuestion for supervisor: which date format?')
    state = cf.contract_state()
    assert state['interfaces']['IF-M0-src@1']['status'] == 'declared' and state['tasks']['M0']['status'] == 'failed'
    assert any('[contract] not ready IF-M0-src@1' in x for x in inbox(cf, 'worker-3'))
    up = inbox(cf, 'supervisor')[-1]
    assert '[worker question] worker-1: which date format?' in up and 'M0 failed' in up
    # Redispatch M0; this time the file is right.
    sup(cf, {'status': 'RUNNING', 'completion': 20, 'dispatch': [item('M0', 'worker-1', provides=['IF-M0-src'])]})
    write(cf, 'worker-1/sources.md', '## Sources\n| url |\n| https://a |\n')
    owners = cf.contract_state()['owners']['worker-1']
    assert owners == [None, 'M0']  # its own not-ready notice, then the redispatch
    report(cf, 'worker-1')  # the notice turn: no task changes
    assert cf.contract_state()['tasks']['M0']['status'] == 'running'
    report(cf, 'worker-1', 'Interfaces: IF-M0-src@1 provided: ready')
    state = cf.contract_state()
    assert state['interfaces']['IF-M0-src@1']['status'] == 'ready' and state['tasks']['M0']['status'] == 'done'
    assert state['tasks']['M0']['attempts'] == 2
    assert inbox(cf, 'worker-3')[-1].startswith('[contract] ready IF-M0-src@1')
    assert state['tasks']['M9']['status'] == 'pending'  # still waits on M1
    write(cf, 'worker-2/metrics.json', '{"symbol": "GOOG", "iv_rank": 0.5}')
    report(cf, 'worker-2', 'Interfaces: IF-M1-metrics@1 provided: ready')
    state = cf.contract_state()
    assert state['tasks']['M9']['status'] == 'running' and '[M9]' in inbox(cf, 'worker-3')[-1]
    # worker-3's queue: M2, not-ready notice, ready notice, ready notice, M9 — a notice turn closes no task.
    assert state['owners']['worker-3'] == ['M2', None, None, None, 'M9']
    report(cf, 'worker-3')
    assert cf.contract_state()['tasks']['M2']['status'] == 'done'
    report(cf, 'worker-3')
    assert cf.contract_state()['tasks']['M9']['status'] == 'running'


def test_breaking_change_proposal_approval_stale_and_clear(cf):
    to_parallel(cf)
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [
        item('M0', 'worker-1', provides=['IF-M0-src']),
        item('M2', 'worker-3', consumes=['IF-M0-src@1', 'IF-M1-metrics@1'])]})
    # Rejected: declared compatible, but the path moved.
    report(cf, 'worker-1', json.dumps({'interface_change': {'id': 'IF-M0-src', 'version': 1, 'change': 'compatible',
                                                            'path': 'worker-1/src2.md'}}))
    assert ('interface_change declared compatible but path changed: it is breaking, version must be 2'
            in inbox(cf, 'worker-1')[-1])
    assert '[contract] rejected interface_change from worker-1' in inbox(cf, 'supervisor')[-1]
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [item('M0', 'worker-1', provides=['IF-M0-src'])]})
    # Breaking without a sample file: rejected.
    report(cf, 'worker-1', json.dumps({'interface_change': {'id': 'IF-M0-src', 'version': 2, 'change': 'breaking',
                                                            'contains': ['## Links']}}))
    assert 'a breaking change needs sample_file in your own directory' in inbox(cf, 'worker-1')[-1]
    write(cf, 'worker-1/stubs/IF-M0-src.v2.md', '## Links\n| url |\n')
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [item('M0', 'worker-1', provides=['IF-M0-src'])]})
    report(cf, 'worker-1', 'Evidence of JSON {"a": 1} above\n' + json.dumps({'interface_change': {
        'id': 'IF-M0-src', 'version': 2, 'change': 'breaking', 'reason': 'heading renamed',
        'contains': ['## Links'], 'sample_file': 'stubs/IF-M0-src.v2.md'}}))
    state = cf.contract_state()
    assert 'IF-M0-src@2' in state['pending'] and '[contract] proposal IF-M0-src@2 (breaking)' in inbox(cf, 'supervisor')[-1]
    # Consuming the unapproved version is refused.
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [
        item('M2', 'worker-3', consumes=['IF-M0-src@2', 'IF-M1-metrics@1'])]})
    assert 'IF-M0-src@2 is proposed, not approved' in inbox(cf, 'supervisor')[-1]
    sup(cf, {'status': 'RUNNING', 'completion': 15, 'interfaces': [{'id': 'IF-M0-src', 'version': 2, 'decision': 'approve'}],
             'dispatch': [item('M0', 'worker-1', provides=['IF-M0-src'])]})
    state = cf.contract_state()
    assert state['interfaces']['IF-M0-src@1']['status'] == 'deprecated'
    assert state['interfaces']['IF-M0-src@1']['deprecated_by'] == 'IF-M0-src@2'
    assert state['interfaces']['IF-M0-src@2']['status'] == 'declared'
    assert state['tasks']['M2']['stale'] == ['IF-M0-src@1']
    assert any(x.startswith('[contract] approved IF-M0-src@2 (breaking)') for x in inbox(cf, 'worker-3'))
    assert any(x.startswith('[contract] approved IF-M0-src@2') for x in inbox(cf, 'worker-1'))
    sup(cf, {'status': 'RUNNING', 'completion': 20, 'dispatch': [
        item('M2', 'worker-3', consumes=['IF-M1-metrics@1'])]})
    assert 'M2 is stale on IF-M0-src@1: the next dispatch must consume @2' in inbox(cf, 'supervisor')[-1]
    sup(cf, {'status': 'RUNNING', 'completion': 20, 'dispatch': [
        item('M2', 'worker-3', consumes=['IF-M0-src@2', 'IF-M1-metrics@1'])]})
    assert cf.contract_state()['tasks']['M2']['stale'] == []
    log = [json.loads(x)['action'] for x in (cf.run_dir / 'contracts.log.jsonl').read_text().splitlines()]
    assert 'proposed' in log and 'approved' in log and log.count('rejected') == 2


def test_completed_gate_needs_ready_interfaces_and_contract_artifact_checks(cf):
    py = sys.executable
    artifacts = [{'path': 'worker-3/integration.md', 'contains': ['PASS'], 'not_contains': ['FAIL']},
                 {'path': 'checks/ok.txt', 'cmd_exit': [py, '-c', 'print("3 passed")'], 'contains': ['passed'],
                  'not_contains': [' skipped']}]
    to_parallel(cf, artifacts=artifacts[1:])  # acceptance commands come from the caller only
    write(cf, 'worker-3/integration.md', 'IF-M0-src@1 · M2 · PASS\nIF-M1-metrics@1 · M2 · FAIL\n')
    sup(cf, {'status': 'COMPLETED', 'completion': 100, 'dispatch': [], 'artifacts': artifacts})
    msg = inbox(cf, 'supervisor')[-1]
    assert msg.startswith('Independent verification failed, the task cannot end:')
    assert 'artifact contains forbidden text ["FAIL"]: worker-3/integration.md' in msg
    assert 'IF-M1-metrics@1 not ready (worker-2/metrics.json missing)' in msg
    assert (cf.workspace() / 'checks/ok.txt').read_text().strip() == '3 passed'
    write(cf, 'worker-3/integration.md', 'IF-M0-src@1 · M2 · PASS\nIF-M1-metrics@1 · M2 · PASS\n')
    state = cf.contract_state()
    for e in state['interfaces'].values():
        e['status'] = 'ready'
    cf.save_contract(state)
    bad = [{'path': 'checks/bad.txt', 'cmd_exit': [py, '-c', 'print("1 skipped"); raise SystemExit(3)']}]
    allow = {fleet.command_key(x) for x in bad}
    assert fleet.check_artifacts(bad, cf.workspace(), True, True, allow) == ['command exited 3: checks/bad.txt']
    skipped = [{'path': 'checks/skip.txt', 'cmd_exit': [py, '-c', 'print("2 passed, 1 skipped")'],
                'not_contains': [' skipped']}]
    assert 'forbidden text [" skipped"]' in fleet.check_artifacts(
        skipped, cf.workspace(), True, True, {fleet.command_key(x) for x in skipped})[0]
    slow = [{'path': 'checks/slow.txt', 'cmd_exit': [py, '-c', 'import time; time.sleep(5)'], 'timeout_s': 1}]
    assert fleet.check_artifacts(slow, cf.workspace(), True, True, {fleet.command_key(x) for x in slow}) == [
        'command timed out after 1 s: checks/slow.txt']
    with pytest.raises(ValueError, match='cmd_exit must be a non-empty argv list'):
        fleet.validate_artifacts([{'path': 'a/b', 'cmd_exit': 'rm -rf /'}], extended=True)
    sup(cf, {'status': 'COMPLETED', 'completion': 100, 'dispatch': [], 'artifacts': artifacts})
    assert cf.task()['status'] == 'completed', cf.task()
    cf.tick()
    result = json.loads((cf.run_dir / 'result.json').read_text())
    assert result['contracts']['pending_user_decisions'][0]['answer'] == 'UTC'
    assert all(not a['problems'] for a in result['artifacts'])


def test_completed_with_malformed_cmd_exit_is_a_correction_not_a_run(cf):
    to_parallel(cf)
    sup(cf, {'status': 'COMPLETED', 'completion': 100, 'dispatch': [],
             'artifacts': [{'path': 'checks/x.txt', 'cmd_exit': 'rm -rf /', 'timeout_s': 'abc'}]})
    assert 'cmd_exit must be a non-empty argv list' in inbox(cf, 'supervisor')[-1]
    assert cf.task()['bad_replies'] == '1' and cf.task()['bounces'] == '0' and cf.task()['artifacts'] == '[]'
    assert not (cf.workspace() / 'checks').exists()


def test_cmd_exit_is_ignored_outside_contract_tasks(cf, tmp_path):
    marker = tmp_path / 'ran'
    item_ = {'path': 'worker-1/x.md', 'cmd_exit': [sys.executable, '-c', 'open(%r, "w")' % str(marker)]}
    write(cf, 'worker-1/x.md', 'hello')
    assert fleet.check_artifacts([item_], cf.workspace()) == []
    assert not marker.exists()


def test_state_survives_a_new_daemon_and_files_resync(cf):
    to_parallel(cf)
    (cf.workspace() / 'contracts.json').unlink()
    again = fleet.Fleet(cf.path, cf.r, FakeMgr(), sessions=cf.sessions)
    assert again.contract_state()['phase'] == 'parallel'
    again.tick()
    assert json.loads((again.workspace() / 'contracts.json').read_text())['phase'] == 'parallel'


def test_successor_generation_scope_carries_the_contract_section(cf):
    submit(cf, contracts=True)
    cf.spawn('worker-2', 'take over')
    assert '## 4. Interface contracts' in (fleet.agent_cwd(cf.cfg, 'worker-2') / 'AGENTS.md').read_text()
    cf.finish('cancelled', 'done')
    cf.spawn('worker-3', 'take over')
    assert 'Interface contracts' not in (fleet.agent_cwd(cf.cfg, 'worker-3') / 'AGENTS.md').read_text()


# ------------------------------------------------------------------ v2 (real private Redis, real hooks)
from test_message_bus_v2 import private_redis_v2  # noqa: E402,F401  (fixture)
from test_v2_codex_claude import answer_turn, mixed, typed  # noqa: E402,F401  (fixture + helpers)


def test_v2_p0_reply_passes_the_daemon_gate_and_skeleton_is_typed(mixed):  # noqa: F811
    f = mixed
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='ct', spec={'goal': 'contract task', 'contracts': True}), 'ct')
    assert f.v2.step(10)
    f.pump()
    assert 'Interface contracts are ON' in typed(f, 'supervisor')[-1]
    # No worker is busy and dispatch is empty: still a valid P0 reply (exemption reaches the v2 validation).
    assert answer_turn(f, 'supervisor', json.dumps(p0_reply()))['status'] == 'committed'
    assert f.task()['bad_replies'] == '0' and f.contract_state()['phase'] == 'skeleton'
    f.pump()
    assert '[skeleton]' in typed(f, 'worker-1')[-1]
    write(f, 'worker-1/skeleton.md', 'IF-M0-src@1 · M0 → M2 · PASS · ok\nIF-M1-metrics@1 · M1 → M2 · PASS · ok\n')
    assert answer_turn(f, 'worker-1', 'Done:\n- skeleton\nEvidence:\n- worker-1/skeleton.md\nNot done: none\n'
                                      'Blocked: none\nInterfaces: none\nQuestion for supervisor: none')['status'] == 'committed'
    assert f.contract_state()['phase'] == 'parallel'
    f.pump()
    assert '[contract] interfaces frozen at v1' in typed(f, 'supervisor')[-1]
    assert json.loads((f.workspace() / 'contracts.json').read_text())['phase'] == 'parallel'


from test_resume import answer_turn as resume_answer, crash_and_resume, managed, task_run  # noqa: E402,F401


def test_resume_restores_contract_state_and_redoes_the_skeleton_run(task_run):  # noqa: F811
    state = task_run
    f = state['f']
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='ct', spec={
        'goal': 'contract task', 'contracts': True, 'skeleton_worker': 'worker-2'}), 'ct')
    assert f.v2.step(10)
    f.pump()
    assert resume_answer(f, 'supervisor', json.dumps(p0_reply()))['status'] == 'committed'
    f.pump()
    assert '[skeleton]' in typed(f, 'worker-2')[-1]  # in flight when the daemon and Redis die
    f = crash_and_resume(state)
    assert f.contract_state()['phase'] == 'skeleton'
    assert f.contract_state()['owners']['worker-2'] == [contracts.SKELETON]
    for slot in ('supervisor', 'worker-2'):
        if f.agent(slot)['state'] == 'starting':
            assert resume_answer(f, slot, 'READY', startup=True)['status'] == 'ready'
    f.pump()
    brief = [t for t in typed(f, 'supervisor') if t.startswith('[Resumed task ')][-1]
    assert 'Interface contracts are on' in brief and '[contract] phase skeleton' in brief
    assert '[skeleton]' in typed(f, 'worker-2')[-1]  # the daemon re-queued the lost skeleton delivery
    write(f, 'worker-2/skeleton.md', 'IF-M0-src@1 · M0 → M2 · PASS · ok\nIF-M1-metrics@1 · M1 → M2 · PASS · ok\n')
    assert resume_answer(f, 'worker-2', 'Done:\n- skeleton\nEvidence:\n- worker-2/skeleton.md\nNot done: none\n'
                                        'Blocked: none')['status'] == 'committed'
    assert f.contract_state()['phase'] == 'parallel'


# ---- acceptance commands belong to the caller (E: supervisor decisions never add / replace cmd_exit)
def _cmd(marker, text='ok'):
    return [sys.executable, '-c', 'open(%r, "a").write("x"); print(%r)' % (str(marker), text)]


def test_supervisor_cannot_add_a_cmd_exit_in_a_contract_task(cf, tmp_path):
    marker = tmp_path / 'ran'
    to_parallel(cf, artifacts=[{'path': 'worker-3/integration.md', 'contains': ['PASS']}])
    sup(cf, {'status': 'COMPLETED', 'completion': 100, 'dispatch': [],
             'artifacts': [{'path': 'checks/own.txt', 'cmd_exit': _cmd(marker)}]})
    assert 'was not declared by the caller' in inbox(cf, 'supervisor')[-1]
    assert cf.task()['bad_replies'] == '1' and cf.task()['bounces'] == '0' and cf.task()['status'] == 'running'
    assert not marker.exists() and len(json.loads(cf.task()['artifacts'])) == 1


def test_supervisor_cannot_replace_the_callers_cmd_exit(cf, tmp_path):
    caller, swapped = tmp_path / 'caller', tmp_path / 'swapped'
    declared = {'path': 'checks/tests.txt', 'cmd_exit': _cmd(caller, '3 passed'), 'contains': ['passed']}
    to_parallel(cf, artifacts=[declared])
    for forged in (dict(declared, cmd_exit=_cmd(swapped, '3 passed')), dict(declared, timeout_s=600)):
        sup(cf, {'status': 'COMPLETED', 'completion': 100, 'dispatch': [], 'artifacts': [forged]})
        assert 'was not declared by the caller' in inbox(cf, 'supervisor')[-1]
    assert not caller.exists() and not swapped.exists() and cf.task()['status'] == 'running'
    state = cf.contract_state()
    for e in state['interfaces'].values():
        e['status'] = 'ready'
    cf.save_contract(state)
    sup(cf, {'status': 'COMPLETED', 'completion': 100, 'dispatch': [], 'artifacts': [declared]})  # unchanged: ok
    assert cf.task()['status'] == 'completed' and caller.read_text() == 'x' and not swapped.exists()


def test_forged_stored_artifact_command_is_reported_not_run(cf, tmp_path):
    """Even if a cmd_exit reaches the stored artifacts list another way, only the caller's set runs."""
    marker = tmp_path / 'ran'
    to_parallel(cf, artifacts=[{'path': 'worker-3/integration.md', 'contains': ['PASS']}])
    write(cf, 'worker-3/integration.md', 'PASS')
    forged = json.loads(cf.task()['artifacts']) + [{'path': 'checks/x.txt', 'cmd_exit': _cmd(marker)}]
    cf.r.hset(fleet.TASK, mapping={'artifacts': json.dumps(forged)})
    state = cf.contract_state()
    for e in state['interfaces'].values():
        e['status'] = 'ready'
    cf.save_contract(state)
    sup(cf, {'status': 'COMPLETED', 'completion': 100, 'dispatch': []})
    assert 'cmd_exit not declared by the caller, not run: checks/x.txt' in inbox(cf, 'supervisor')[-1]
    assert not marker.exists() and cf.task()['status'] == 'running'
    old_task = {k: v for k, v in cf.task().items() if k != 'caller_artifacts'}  # stored by an older daemon
    assert fleet.caller_commands(old_task) == set()


def test_result_recheck_never_runs_commands(cf, tmp_path):
    marker = tmp_path / 'ran'
    declared = {'path': 'checks/tests.txt', 'cmd_exit': _cmd(marker, '1 passed'), 'contains': ['passed']}
    to_parallel(cf, artifacts=[declared])
    state = cf.contract_state()
    for e in state['interfaces'].values():
        e['status'] = 'ready'
    cf.save_contract(state)
    sup(cf, {'status': 'COMPLETED', 'completion': 100, 'dispatch': []})
    assert cf.task()['status'] == 'completed' and marker.read_text() == 'x'  # ran once, at the gate
    cf.tick()  # result.json re-check
    result = json.loads((cf.run_dir / 'result.json').read_text())
    assert result['artifacts'][0]['problems'] == [] and marker.read_text() == 'x'


def test_non_contract_decision_with_cmd_exit_is_rejected(cf, tmp_path):
    marker = tmp_path / 'ran'
    submit(cf)
    sup(cf, {'status': 'RUNNING', 'completion': 1, 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Write worker-1/x.md with the findings'}],
             'artifacts': [{'path': 'worker-1/x.md', 'cmd_exit': _cmd(marker)}]})
    assert 'was not declared by the caller' in inbox(cf, 'supervisor')[-1] and not marker.exists()
