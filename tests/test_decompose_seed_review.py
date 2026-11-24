"""The submitter's tree is kept as the seed (docs/design.md §62): a spec ``tree`` is snapshotted into ``tree['seed']``
(``by``, ``at``, the root, each block's est / owns / after / depends_on / consumes / decides, the decisions); the
supervisor's later changes never touch it; ``review['seed']`` compares the seed with the tree as it ran (each seed
block kept / changed fields / merged into / split into / dropped, the blocks the supervisor added, actual / seed
estimate, the seed's longest chain against the plan's and the actual one). Numbers only; null without a seed."""
import json

import pytest

from fleetmux import decompose, fleet
from test_decompose_fleet import answer_turn, reply, submit, tree
from test_decompose_review import T0
from test_decompose_tree import PARAMS, sample
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_v2_codex_claude import mixed  # noqa: F401

CTX = {'root': 'root', 'prefix': 'M', 'params': PARAMS}


def seed_file(tmp_path, by=None, **extra):
    data = {'nodes': [{'id': 'TOP', 'goal': 'the whole job'}] + [x for x in sample() if 'id' in x],
            'decisions': [x['decision'] for x in sample() if 'decision' in x]}
    if by is not None:
        data['by'] = by
    data.update(extra)
    path = tmp_path / 'tree.json'
    path.write_text(json.dumps(data))
    return str(path)


def seeded(tmp_path, **kw):
    return decompose.seed_from(seed_file(tmp_path), 'root', 'goal', 'M', now=T0, **kw)


def finish(t, nid, start, end):
    n = decompose.by_id(t)[nid]
    n['state'] = 'reported'
    n['actual'] = dict(decompose.blank_actual(), started=T0 + start * 60, ended=T0 + end * 60, minutes=end - start,
                       session_minutes=end - start)


def row(rev, nid):
    return next(b for b in rev['seed']['blocks'] if b['id'] == nid)


# ------------------------------------------------------------------ tree['seed']
def test_a_seed_file_is_snapshotted_with_its_submitter_and_time(tmp_path):
    t = seeded(tmp_path)
    seed = t['seed']
    assert set(seed) == {'by', 'at', 'root', 'blocks', 'decisions'}
    assert seed['by'] == 'submitter' and seed['at'] == T0 and seed['root'] == 'TOP'
    assert [b['id'] for b in seed['blocks']] == ['M0', 'M1', 'M2', 'M3']
    assert seed['blocks'][1] == {'id': 'M1', 'goal': 'implement the check', 'est': {'minutes': 25},
                                 'owns': ['gate/check.py'], 'after': ['M0'], 'depends_on': [],
                                 'consumes': ['D1', 'D2'], 'decides': []}
    assert seed['decisions'] == [{'id': 'D1', 'owner': 'M0', 'status': 'open'},
                                 {'id': 'D2', 'owner': 'supervisor', 'status': 'pending_user'}]


def test_by_comes_from_the_caller_then_the_file_then_defaults(tmp_path):
    assert decompose.seed_from(seed_file(tmp_path, by='orchestrator'), 'root', 'g', 'M')['seed']['by'] == \
        'orchestrator'
    assert decompose.seed_from(seed_file(tmp_path, by='orchestrator'), 'root', 'g', 'M', by='ops')['seed']['by'] == \
        'ops'
    for bad in ('', '  ', 7, 'x' * 81):
        with pytest.raises(ValueError, match='by'):
            decompose.seed_from(seed_file(tmp_path), 'root', 'g', 'M', by=bad)


def test_the_seed_is_read_only_whatever_the_supervisor_does(tmp_path):
    t = seeded(tmp_path)
    kept = json.loads(json.dumps(t['seed']))
    t = decompose.apply(t, [{'id': 'M1', 'est': {'minutes': 40}, 'owns': ['gate/check.py', 'gate/util.py']},
                            {'id': 'M4', 'goal': 'docs', 'cut': 'function', 'est': {'minutes': 5},
                             'owns': ['docs/a.md'], 'accept': 'ok'}], [], CTX)
    t = decompose.apply(t, [{'id': 'M3', 'goal': 'integrate', 'est': {'minutes': 15}, 'depends_on': ['M1', 'M2'],
                             'serial_reason': 'integration', 'accept': 'ok', 'owns': ['docs/a.md'],
                             'merge_of': ['M4']}], ['M4'], CTX)
    assert t['seed'] == kept


def test_a_split_seed_is_by_the_worker_who_proposed_the_pieces(tmp_path):
    path = decompose.write_seed(str(tmp_path / 's.json'), {'id': 'M1', 'goal': 'g'}, [{'id': 'M1.0', 'goal': 'a'}],
                                by='worker-1')
    assert json.loads(open(path).read())['by'] == 'worker-1'
    assert decompose.seed_from(path, 'M1', 'g', 'M1.')['seed']['by'] == 'worker-1'
    assert decompose.renumber({'nodes': [], 'decisions': [], 'by': 'ops'}, 'M4.')['by'] == 'ops'


# ------------------------------------------------------------------ review['seed']
def test_no_seed_no_seed_section():
    assert decompose.review(decompose.new_tree('root', 'goal'), now=T0)['seed'] is None


def test_a_seed_file_without_blocks_is_no_seed(tmp_path):
    path = decompose.write_seed(str(tmp_path / 's.json'), {'id': 'M1', 'goal': 'the job'}, [], by='worker-1')
    t = decompose.seed_from(path, 'M1', 'g', 'M1.')
    assert 'seed' not in t and decompose.review(t, now=T0)['seed'] is None


def test_review_follows_every_seed_block(tmp_path):
    t = seeded(tmp_path)
    t = decompose.apply(t, [  # M0 kept, M1 re-estimated and owns more, M2 merged into M5, M3 split into M6 / M7
        {'id': 'M1', 'est': {'minutes': 30, 'sessions': 1}, 'owns': ['gate/check.py', 'gate/util.py']},
        {'id': 'M5', 'goal': 'wire both send paths and docs', 'cut': 'decision', 'est': {'minutes': 20},
         'owns': ['engine/send.py', 'docs/send.md'], 'consumes': ['D1'], 'merge_of': ['M2'], 'accept': 'ok'},
        {'id': 'M8', 'goal': 'changelog', 'cut': 'function', 'est': {'minutes': 4}, 'owns': ['CHANGELOG.md'],
         'accept': 'ok'}, {'id': 'M3', 'depends_on': ['M1', 'M5']}], ['M2'], CTX)
    free = dict(PARAMS, split=dict(PARAMS['split'], margin=0, k=0), cost={k: 0 for k in PARAMS['cost']})
    t = decompose.apply(t, [
        {'id': 'M6', 'goal': 'merge', 'cut': 'serial', 'est': {'minutes': 5}, 'depends_on': ['M1', 'M5'],
         'serial_reason': 'integration', 'accept': 'ok', 'split_from': {'id': 'M3', 'dT': 1, 'dC': 0}},
        {'id': 'M7', 'goal': 'tests', 'cut': 'function', 'est': {'minutes': 5}, 'owns': ['tests/x.py'],
         'after': ['M1', 'M5'], 'accept': 'ok', 'split_from': {'id': 'M3', 'dT': 1, 'dC': 0}}], ['M3'],
        dict(CTX, params=free))
    finish(t, 'M0', 0, 15)       # seed 10 -> 1.5
    finish(t, 'M1', 15, 45)      # seed 25 -> 1.2
    finish(t, 'M5', 15, 35)      # M2 (seed 15) merged: 20 / 15
    finish(t, 'M6', 45, 50)
    finish(t, 'M7', 45, 60)      # M3 (seed 10) split: wall 45..60 = 15 / 10
    rev = decompose.review(t, now=T0 + 3600)
    seed = rev['seed']
    assert seed['by'] == 'submitter' and seed['at'] == T0
    assert row(rev, 'M0') == {'id': 'M0', 'fate': 'kept', 'changed': [], 'into': None, 'pieces': [],
                              'est_minutes': 10, 'actual_minutes': 15, 'ratio': 1.5}
    assert row(rev, 'M1')['fate'] == 'changed' and row(rev, 'M1')['changed'] == ['est', 'owns']
    assert row(rev, 'M1')['ratio'] == 1.2
    assert row(rev, 'M2')['fate'] == 'merged' and row(rev, 'M2')['into'] == 'M5'
    assert row(rev, 'M2')['actual_minutes'] == 20 and row(rev, 'M2')['ratio'] == 1.33
    assert row(rev, 'M3')['fate'] == 'split' and row(rev, 'M3')['pieces'] == ['M6', 'M7']
    assert row(rev, 'M3')['actual_minutes'] == 15 and row(rev, 'M3')['ratio'] == 1.5
    assert seed['added'] == [{'id': 'M8', 'est_minutes': 4, 'actual_minutes': None, 'ratio': None}]
    assert seed['counts'] == {'kept': 1, 'changed': 1, 'merged': 1, 'split': 1, 'dropped': 0, 'added': 1}
    assert seed['chain']['seed'] == {'ids': ['M0', 'M1', 'M3'], 'minutes': 45}
    assert seed['chain']['plan'] == {'ids': ['M0', 'M1', 'M6'], 'minutes': 45}
    assert seed['chain']['actual'] == {'ids': ['M0', 'M1', 'M7'], 'minutes': 60}


def test_a_dropped_block_and_an_unfinished_one_have_no_ratio(tmp_path):
    t = seeded(tmp_path)
    t = decompose.apply(t, [{'id': 'M3', 'depends_on': ['M1']}], ['M2'], CTX)
    rev = decompose.review(t, now=T0)
    assert row(rev, 'M2') == {'id': 'M2', 'fate': 'dropped', 'changed': [], 'into': None, 'pieces': [],
                              'est_minutes': 15, 'actual_minutes': None, 'ratio': None}
    assert row(rev, 'M3')['changed'] == ['depends_on'] and row(rev, 'M3')['ratio'] is None
    assert rev['seed']['added'] == [] and rev['seed']['counts']['dropped'] == 1


def test_a_block_the_program_split_under_itself_lists_its_pieces(tmp_path):
    t = seeded(tmp_path)
    t['nodes'] += [{'id': 'M1.0', 'parent': 'M1', 'state': 'delegated'}, {'id': 'M1.1', 'parent': 'M1'}]
    finish(t, 'M1', 10, 50)  # a returned sub-fleet fills in the block itself
    r = row(decompose.review(t, now=T0), 'M1')
    assert r['fate'] == 'split' and r['pieces'] == ['M1.0', 'M1.1'] and r['ratio'] == 1.6


# ------------------------------------------------------------------ in a running fleet
def test_a_submitted_tree_keeps_its_seed_through_the_supervisors_changes(mixed, decompose_on, tmp_path):
    f = mixed
    submit(f, tree=seed_file(tmp_path), by='orchestrator')
    t = tree(f)
    assert t['seed']['by'] == 'orchestrator' and [b['id'] for b in t['seed']['blocks']] == ['M0', 'M1', 'M2', 'M3']
    kept = t['seed']
    answer_turn(f, 'supervisor', reply(tree=[{'id': 'M0', 'est': {'minutes': 12}}]))
    t = tree(f)
    assert t['accepted'] == 1 and t['seed'] == kept
    assert t['review']['seed']['blocks'][0]['changed'] == ['est']
    assert json.loads((f.run_dir / 'tree.json').read_text())['seed'] == kept


def test_without_a_tree_the_fleet_review_has_a_null_seed(mixed, decompose_on):
    f = mixed
    submit(f)
    answer_turn(f, 'supervisor', reply(tree=sample()))
    t = tree(f)
    assert 'seed' not in t and t['review']['seed'] is None


def test_a_split_sub_fleet_seed_names_the_worker(mixed, decompose_on):
    f = mixed
    submit(f)
    answer_turn(f, 'supervisor', reply(tree=sample()))
    path = f.tree_seed('M1', {'slot': 'worker-1', 'instruction': 'implement',
                              'proposal': [{'id': 'M1.0', 'instruction': 'a'}]})
    assert json.loads(open(path).read())['by'] == 'worker-1'


def test_a_job_spec_takes_by():
    assert 'by' in fleet.JOB_KEYS
