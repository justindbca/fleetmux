"""A sub-fleet's tree starts from its parent (docs/design.md §62): the approved split's node is the child tree's root
and its pieces the first-level blocks; a job's tree file is renumbered into the child's level. The seed travels as a
file path: escalation_apply -> subfleet.request -> spawn_child -> the child task spec ``tree``."""
import json

from fleetmux import decompose, fleet, subfleet
from test_decompose_fleet import hand_in_tree
from test_decompose_tree import PARAMS, sample
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_v2_codex_claude import mixed  # noqa: F401

PROPOSAL = [{'id': 'M1.0', 'instruction': 'Implement the size check and its tests.', 'output': 'a.md'},
            {'id': 'M1.1', 'instruction': 'Implement the price check and its tests.', 'output': 'b.md'}]


def test_a_split_seed_is_the_parent_node_with_the_approved_pieces(mixed, decompose_on):
    f = mixed
    hand_in_tree(f)
    path = f.tree_seed('M1', {'slot': 'worker-1', 'instruction': 'implement', 'proposal': PROPOSAL})
    data = json.loads(open(path).read())
    assert data['nodes'][0] == {'id': 'M1', 'goal': 'implement the check', 'accept': 'contract tests green',
                                'est': {'minutes': 25}, 'owns': ['gate/check.py']}
    child = decompose.seed_from(path, 'M1', 'Sub-fleet M1 ...', 'M1.')
    assert child['accepted'] == 0 and child['nodes'][0]['goal'] == 'implement the check'
    assert [n['id'] for n in decompose.level_nodes(child)] == ['M1.0', 'M1.1']
    assert decompose.level_nodes(child)[0]['goal'] == 'Implement the size check and its tests.'


def test_an_approved_split_hands_the_seed_to_the_sub_fleet_request(mixed, decompose_on, monkeypatch):
    f, asked = mixed, {}
    hand_in_tree(f)
    f.decompose_load()  # costs under which this split pays (the approve passes the program's account)
    p = decompose.params_get(f.r)
    decompose.params_put(f.r, dict(p, cost=dict(p['cost'], cold_start=0, supervisor=0, spawn=0)))
    monkeypatch.setattr(subfleet, 'check', lambda *a, **k: None)
    monkeypatch.setattr(fleet.Fleet, 'supervisor_chain_pick', lambda self, slot, **kw: ({}, None, ''))
    monkeypatch.setattr(subfleet, 'request', lambda run_dir, **spec: asked.update(spec) or 'sf-test')
    rec = {'slot': 'worker-1', 'instruction': 'implement the check', 'proposal': PROPOSAL, 'workers': 2,
           'status': 'escalated'}
    f.escalation_apply({}, None, 'M1', rec, {'task': 'M1', 'decision': 'approve', 'note': ''})
    assert asked['task_id'] == 'M1' and decompose.seed_from(asked['tree'], 'M1', 'g', 'M1.')['nodes'][0]['est'] == \
        {'minutes': 25}


def test_a_jobs_tree_file_is_renumbered_into_the_child_level(mixed, decompose_on, tmp_path):
    f = mixed
    spec = tmp_path / 'job-tree.json'
    items = sample()
    spec.write_text(json.dumps({'nodes': [x for x in items if 'id' in x],
                                'decisions': [x['decision'] for x in items if 'decision' in x]}))
    f.save_jobs({'job-abc': {'goal': 'the job', 'tree': str(spec)}})
    path = f.tree_seed('M4', {'slot': 'worker-1', 'instruction': 'the job', 'proposal': [], 'job': 'job-abc'})
    child = decompose.seed_from(path, 'M4', 'the job', 'M4.')
    ids = [n['id'] for n in decompose.level_nodes(child)]
    assert ids == ['M4.0', 'M4.1', 'M4.2', 'M4.3']
    m3 = decompose.by_id(child)['M4.3']
    assert m3['depends_on'] == ['M4.1', 'M4.2'] and decompose.by_id(child)['M4.1']['after'] == ['M4.0']
    assert {d['id']: d['owner'] for d in child['decisions']} == {'D1': 'M4.0', 'D2': 'supervisor'}
    child = decompose.apply(child, [], [], {'root': 'M4', 'prefix': 'M4.', 'params': PARAMS})  # a whole tree


def test_a_job_spec_may_carry_a_tree_file():
    assert 'tree' in fleet.JOB_KEYS


def test_the_request_carries_the_seed_to_spawn_child_and_the_child_task(tmp_path, monkeypatch):
    rid = subfleet.request(tmp_path, 'worker-1', 'M1', 'goal', 2, tree=str(tmp_path / 'seed.json'))
    assert fleet.load(tmp_path / 'subfleet-requests' / (rid + '.json'))['tree'] == str(tmp_path / 'seed.json')
    seen = {}

    class F:
        run_dir = tmp_path

        def log(self, *a, **k):
            pass
    monkeypatch.setattr(subfleet, 'spawn_child', lambda f, *a, **kw: seen.update(kw) or {'run': 'r', 'task_id': 'M1'})
    subfleet.process_requests(F())
    assert seen['tree'] == str(tmp_path / 'seed.json')
