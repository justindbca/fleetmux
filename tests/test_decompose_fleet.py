"""The decomposition tree in a running v2 task fleet (docs/design.md §62), switch on: the first reply hands in the tree,
the program keeps it in fx:tree + tree.json, tells the supervisor what can go now, dispatches only its blocks (fx:items
unchanged beside it), and no fx:items delete path touches the tree."""
import json

from fleetmux import decompose, fleet, runtime_v2
from test_decompose_tree import sample
from test_long_split import supervisor_texts
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_v2_codex_claude import answer_turn, mixed, typed  # noqa: F401


def reply(**kw):
    return json.dumps(dict({'status': 'RUNNING', 'completion': 5, 'reason': 'plan', 'dispatch': []}, **kw))


def submit(f, **spec):
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task',
                                         spec=dict({'goal': 'add one pre-send check'}, **spec)), 'new-task')
    assert f.v2.step(10)
    f.pump()


def tree(f):
    return json.loads(f.r.get(decompose.TREE))


def node(t, nid):
    return next(n for n in t['nodes'] if n['id'] == nid)


def audit(f, kind):
    return [a for _, a in f.r.xrange(f.v2.bus.audit_key) if a.get('event') == kind]


def free_costs(f):
    """Costs and margin at 0 (the shipped initial costs make a 10-minute block not worth splitting)."""
    f.decompose_load()
    p = decompose.params_get(f.r)
    decompose.params_put(f.r, dict(p, split=dict(p['split'], margin=0), cost={k: 0 for k in p['cost']}))


def hand_in_tree(f):
    submit(f)
    assert answer_turn(f, 'supervisor', reply(tree=sample()))['status'] == 'committed'
    f.pump()


def test_the_first_tree_is_kept_mirrored_and_answered(mixed, decompose_on):
    f = mixed
    hand_in_tree(f)
    t = tree(f)
    assert t['rev'] == 1 and t['accepted'] == 1 and t['chain'] == {'ids': ['M0', 'M1', 'M3'], 'minutes': 45}
    for path in (f.run_dir / 'tree.json', f.workspace() / 'tree.json'):
        assert json.loads(path.read_text()) == t
    assert len((f.run_dir / 'tree.log.jsonl').read_text().splitlines()) == 1
    assert [x for x in supervisor_texts(f) + typed(f, 'supervisor') if x.startswith('[tree] rev 1 accepted')]
    assert json.loads(audit(f, 'tree.accepted')[0]['chain']) == ['M0', 'M1', 'M3']


def test_a_block_is_dispatched_with_its_brief_and_fx_items_is_written_as_before(mixed, decompose_on):
    f = mixed
    free_costs(f)
    hand_in_tree(f)
    assert answer_turn(f, 'supervisor', reply(dispatch=[{'to': 'worker-1', 'id': 'M0', 'instruction':
                                                         'Write the skeleton and the contract tests.'}]))['status'] == 'committed'
    f.pump()
    text = typed(f, 'worker-1')[-1]
    assert '[M0] Write the skeleton' in text and '[tree M0] owns gate/check.py, tests/test_check_contract.py' in text
    assert json.loads(f.r.get(fleet.ITEMS))['M0']['to'] == 'worker-1'
    m0 = node(tree(f), 'M0')
    assert m0['state'] == 'running' and m0['to'] == 'worker-1' and m0['actual']['dispatches'] == 1
    items = json.loads(audit(f, 'dispatch')[-1]['items'])
    assert items == [{'id': 'M0', 'to': 'worker-1', 'parent': 'root', 'est_minutes': 10, 'est_sessions': 1,
                      'critical': True, 'leaf': False}]  # free costs: a split may pay


def test_a_dispatch_off_the_tree_is_refused_with_the_reason(mixed, decompose_on):
    f = mixed
    hand_in_tree(f)
    answer_turn(f, 'supervisor', reply(dispatch=[{'to': 'worker-1', 'id': 'M7', 'instruction': 'Do something else.'}]))
    assert any('dispatch id M7 is not in the tree: add it to the tree first' in x for x in supervisor_texts(f))
    assert 'M7' not in json.loads(f.r.get(fleet.ITEMS) or '{}')


def test_the_delete_paths_of_fx_items_leave_the_tree(mixed, decompose_on):
    f = mixed
    hand_in_tree(f)
    answer_turn(f, 'supervisor', reply(dispatch=[{'to': 'worker-1', 'id': 'M0', 'instruction':
                                                  'Write the skeleton and the contract tests.'}]))
    before = f.r.get(decompose.TREE)
    assert before and 'M0' in json.loads(f.r.get(fleet.ITEMS))
    esc = {}
    f.split_drop(esc, 'M0', {'slot': 'worker-1', 'request': 'req-1'}, 'request ended')  # P7 recall path (W4)
    assert 'M0' not in json.loads(f.r.get(fleet.ITEMS))
    assert f.r.get(decompose.TREE) == before


def test_item_leaf_reads_the_tree(mixed, decompose_on):
    f = mixed
    free_costs(f)
    hand_in_tree(f)
    items = json.loads(f.r.get(fleet.ITEMS) or '{}')
    items['M1'] = {'to': 'worker-1', 'instruction': 'x', 'leaf': True}  # a dispatch leaf is not what counts
    f.r.set(fleet.ITEMS, json.dumps(items))
    assert f.item_leaf('M2') is True and f.item_leaf('M1') is False


def test_a_seeded_tree_still_needs_the_supervisors_first_tree(mixed, decompose_on, tmp_path):
    f = mixed
    seed = tmp_path / 'tree.json'
    seed.write_text(json.dumps({'nodes': [{'id': 'TOP', 'goal': 'the whole job'}]
                                + [x for x in sample() if 'id' in x],
                                'decisions': [x['decision'] for x in sample() if 'decision' in x]}))
    f.r.set(decompose.TREE, json.dumps({'stale': True}))  # an earlier task's tree never leaks into this one
    submit(f, tree=str(seed))
    t = tree(f)
    assert t['accepted'] == 0 and {n['id'] for n in t['nodes']} == {'root', 'M0', 'M1', 'M2', 'M3'}
    assert 'Decomposition is on' in typed(f, 'supervisor')[-1]
    answer_turn(f, 'supervisor', reply(dispatch=[{'to': 'worker-1', 'id': 'M0', 'instruction': 'Write the skeleton.'}]))
    assert any('your first reply hands in the tree only' in x for x in supervisor_texts(f))
    assert tree(f)['accepted'] == 0 and 'M0' not in json.loads(f.r.get(fleet.ITEMS) or '{}')
    f.pump()
    answer_turn(f, 'supervisor', reply(tree=[{'id': 'M0', 'est': {'minutes': 12}}]))
    assert tree(f)['accepted'] == 1 and node(tree(f), 'M0')['est'] == {'minutes': 12}


def test_a_lost_redis_tree_comes_back_from_tree_json(mixed, decompose_on):
    f = mixed
    hand_in_tree(f)
    kept = tree(f)
    f.r.delete(decompose.TREE)
    f.tree_restore()
    assert tree(f) == kept


def test_a_report_settles_the_block_and_frees_what_waits_for_it(mixed, decompose_on):
    f = mixed
    hand_in_tree(f)
    answer_turn(f, 'supervisor', reply(dispatch=[{'to': 'worker-1', 'id': 'M0', 'instruction':
                                                  'Write the skeleton and the contract tests.'}]))
    f.pump()
    answer_turn(f, 'worker-1', 'Done: skeleton\nEvidence: tests red\nNot done: none\nBlocked: none')
    m0 = node(tree(f), 'M0')
    assert m0['state'] == 'reported' and m0['actual']['ended'] >= m0['actual']['started'] > 0
    assert decompose.dispatchable(tree(f)) == ['M1', 'M2']
