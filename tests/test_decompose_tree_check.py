"""`fleetmux tree check <file>` (docs/design.md §62): the submitter checks a tree file before submitting it, with the
same shape checks the program runs when the supervisor hands in a tree (``decompose.apply`` -> ``shape_errors``) and
the same accounts (``judge``): the longest chain, the maximum parallel width, the blocks the program would mark
leaf=false with their dT upper / dC lower bounds. Exit 1 on shape errors, 0 otherwise. The decompose parameters are
the shipped ones, or a private fleet.yaml's ``decompose`` block laid over them (``--config``)."""
import json

import pytest

from fleetmux import decompose, fleet, tree_cli
from test_decompose_tree import sample

FREE = 'decompose:\n  split: {margin: 0}\n  cost: {cold_start: 0, per_decision: 0, merge: 0, supervisor: 0, spawn: 0}\n'


def tree_file(tmp_path, items=None, **extra):
    items = sample() if items is None else items
    data = dict({'nodes': [{'id': 'root', 'goal': 'the whole job'}] + [x for x in items if 'id' in x],
                 'decisions': [x['decision'] for x in items if 'decision' in x]}, **extra)
    path = tmp_path / 'tree.json'
    path.write_text(json.dumps(data))
    return str(path)


def run(capsys, *argv):
    code = tree_cli.main(list(argv))
    return code, capsys.readouterr().out


def test_a_good_tree_prints_chain_width_and_no_leaf_false_with_shipped_values(tmp_path, capsys):
    code, out = run(capsys, 'check', tree_file(tmp_path, by='orchestrator'))
    assert code == 0
    assert 'shape: ok' in out
    assert 'longest chain: M0 -> M1 -> M3 (45 min)' in out
    assert 'max parallel width: 2' in out
    assert 'leaf=false: none' in out
    assert 'seed: by orchestrator, 4 blocks' in out
    assert 'params: shipped (k 2, margin 7.4' in out


def test_a_private_block_changes_the_accounts(tmp_path, capsys):
    private = tmp_path / 'fleet.yaml'
    private.write_text(FREE)
    code, out = run(capsys, 'check', tree_file(tmp_path), '--config', str(private))
    assert code == 0 and 'params: ' + str(private) in out
    line = next(x for x in out.splitlines() if x.startswith('leaf=false:'))
    assert 'M1 (dT <= ' in line and 'dC >= 0 session min' in line


def test_the_json_form_carries_the_same_numbers(tmp_path, capsys):
    private = tmp_path / 'fleet.yaml'
    private.write_text(FREE)
    code, out = run(capsys, 'check', tree_file(tmp_path), '--config', str(private), '--json')
    data = json.loads(out)
    assert code == 0 and data['ok'] is True and data['errors'] == []
    assert data['chain'] == {'ids': ['M0', 'M1', 'M3'], 'minutes': 45} and data['width'] == 2
    assert [x['id'] for x in data['leaf_false']] == ['M0', 'M1', 'M3']
    assert set(data['leaf_false'][1]) == {'id', 'dT', 'dC', 'reason'}


def test_shape_errors_exit_1_with_the_dispatch_side_message(tmp_path, capsys):
    items = sample()
    items[3] = dict(items[3], owns=['engine/send.py'], after=[])  # M1 and M2 own the same file in parallel
    path = tree_file(tmp_path, items)
    with pytest.raises(ValueError) as exc:  # what the program says when the supervisor hands this tree in
        decompose.apply(decompose.seed_from(path, 'root', 'g', 'M'), [], [],
                        {'root': 'root', 'prefix': 'M', 'params': decompose.merged({})})
    code, out = run(capsys, 'check', path)
    assert code == 1 and 'shape: 1 error' in out
    assert '- ' + str(exc.value) in out


def test_it_runs_the_dispatch_side_shape_check_itself(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(decompose, 'shape_errors', lambda tree, ctx: ['the one shape check'])
    code, out = run(capsys, 'check', tree_file(tmp_path))
    assert code == 1 and '- the one shape check' in out


def test_an_unreadable_file_or_a_foreign_id_exits_1(tmp_path, capsys):
    code, out = run(capsys, 'check', str(tmp_path / 'missing.json'))
    assert code == 1 and 'cannot read' in out
    code, out = run(capsys, 'check', tree_file(tmp_path, [{'id': 'X9', 'parent': 'root', 'goal': 'g'}]))
    assert code == 1 and 'not an id of this level' in out


def test_a_file_with_only_a_root_has_no_seed_and_no_chain(tmp_path, capsys):
    code, out = run(capsys, 'check', tree_file(tmp_path, []))
    assert code == 0 and 'seed:' not in out and 'longest chain: - (0 min)' in out


def test_a_sub_fleet_level_takes_its_prefix(tmp_path, capsys):
    items = [{'id': 'M4.0', 'goal': 'a', 'cut': 'function', 'est': {'minutes': 10}, 'owns': ['a.py'], 'accept': 'ok'}]
    code, out = run(capsys, 'check', tree_file(tmp_path, items), '--prefix', 'M4.')
    assert code == 0 and 'longest chain: M4.0 (10 min)' in out


def test_a_bad_private_block_is_a_usage_error(tmp_path, capsys):
    private = tmp_path / 'fleet.yaml'
    private.write_text('decompose:\n  split: {k: 0}\n')
    code, out = run(capsys, 'check', tree_file(tmp_path), '--config', str(private))
    assert code == 2 and 'split.k' in out


def test_fleetmux_routes_tree_to_it(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        fleet.main(['tree', 'check', tree_file(tmp_path)])
    assert exc.value.code == 0 and 'longest chain: M0 -> M1 -> M3' in capsys.readouterr().out
