"""``fleetmux upgrade --all-running --to <release>``: rollout's restart is one command per machine;
a machine can run more than one root fleet tree. Every root tree with a running root daemon is upgraded once, one
after the other; a tree that fails does not stop the others, and the exit code is the most severe one (4 > 3 > 1 > 2 >
0). The roots are injected here: the real enumeration (``roots_in``) reads a ``ps`` table, and no test ever lists or
touches a fleet running on this machine."""
import json
from pathlib import Path

import pytest

from upgrade_kit import FakeOps, Tree, make_release
from fleetmux import fleet, upgrade

JOBS = {'job-aaaaaaaaaa': {'state': 'running', 'slot': 'worker-1', 'task': 'M1', 'created': 1.0}}


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """Two root trees (A, B) on release aaaaaaa1, both running; the machine home is temporary."""
    monkeypatch.setenv('FLEETMUX_HOME', str(tmp_path / 'machine-home'))
    old, new = make_release(tmp_path, 'aaaaaaa1'), make_release(tmp_path, 'bbbbbbb2')
    trees, ops = {}, {}
    for key in ('A', 'B'):
        t = Tree(tmp_path / key, old)
        t.child('M1', job='job-aaaaaaaaaa')
        trees[key], ops[key] = t, FakeOps(t, new, jobs=JOBS)
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: any(o.daemon_running(run) for o in ops.values()))
    by_cfg = {str(trees[k].cfg_path): ops[k] for k in trees}
    return dict(trees=trees, ops=ops, old=old, new=new, by_cfg=by_cfg)


def run_all(m, *extra, roots=None):
    lines = []
    code = upgrade.main(['--all-running', '--to', str(m['new'])] + list(extra), ops=m['ops']['A'], out=lines.append,
                        roots=roots or (lambda: [m['trees'][k].cfg_path for k in ('A', 'B')]),
                        ops_for=lambda cfg: m['by_cfg'][str(Path(cfg))])
    return code, lines


def test_all_running_upgrades_every_root_tree_once(machine):
    code, lines = run_all(machine)
    assert code == 0, lines
    out = json.loads(lines[-1])
    assert out['result'] == 'pass' and out['to'] == 'bbbbbbb2', out
    assert [t['config'] for t in out['trees']] == [str(machine['trees'][k].cfg_path) for k in ('A', 'B')]
    assert all(t['result'] == 'pass' for t in out['trees']), out['trees']
    for key in ('A', 'B'):
        starts = [e for e in machine['ops'][key].events if e[0] == 'run' and 'start' in e[1]]
        assert len(starts) == 1, (key, machine['ops'][key].events)
    # every line before the last one names its tree; only the last line is the machine's JSON
    assert all(line.startswith('tree ') for line in lines[:-1]), lines


def test_one_failed_tree_does_not_stop_the_others_and_fails_the_exit(machine):
    machine['ops']['A'].fail['redis_down'] = RuntimeError('socket refused')   # A fails in the stop phase (3)
    code, lines = run_all(machine)
    assert code == 3, lines
    out = json.loads(lines[-1])
    assert out['result'] == 'fail' and out['step'] == 'redis_down', out
    a, b = out['trees']
    assert a['result'] == 'fail' and a['step'] == 'redis_down' and b['result'] == 'pass', out['trees']
    assert str(machine['trees']['A'].cfg_path) in out['reason'], out['reason']
    assert any('upgrade --to' in r for r in out['recover']), out['recover']


@pytest.mark.parametrize('codes,worst', [((0, 2), 2), ((2, 1), 1), ((1, 3), 3), ((3, 4), 4), ((4, 0), 4)])
def test_the_exit_code_is_the_most_severe(codes, worst):
    assert upgrade.worst_exit(codes) == worst


def test_no_root_fleet_running_passes_and_says_so(machine):
    code, lines = run_all(machine, roots=lambda: [])
    assert code == 0, lines
    out = json.loads(lines[-1])
    assert out['result'] == 'pass' and out['trees'] == [] and 'no root fleet' in out['reason'], out


def test_config_and_all_running_do_not_go_together(machine):
    with pytest.raises(SystemExit):
        upgrade.main(['--all-running', '--config', str(machine['trees']['A'].cfg_path), '--to', 'x'],
                     ops=machine['ops']['A'], out=lambda line: None)


def test_roots_are_the_daemons_without_a_parent(tmp_path):
    """The real enumeration: `fleet.py daemon <config>` lines of a ps table whose config has no parent_config; a
    keep-awake wrapper's line and sub-fleet daemons are not roots; each root once."""
    root, child = tmp_path / 'runs' / 'r1', tmp_path / 'runs' / 'r1' / 'sub' / 'M1'
    for d, cfg in ((root, {'run': str(tmp_path / 'runs' / 'r1')}),
                   (child, {'run': str(child), 'parent_config': str(root / 'config.json')})):
        d.mkdir(parents=True, exist_ok=True)
        (d / 'config.json').write_text(json.dumps(cfg))
    table = '\n'.join([
        '101 /usr/bin/caffeinate -i /r/.venv/bin/python /r/fleetmux/fleet.py daemon %s' % (root / 'config.json'),
        '102 /r/.venv/bin/python /r/fleetmux/fleet.py daemon %s --resume' % (root / 'config.json'),
        '103 /r/.venv/bin/python /r/fleetmux/fleet.py daemon %s' % (child / 'config.json'),
        '104 /bin/zsh -c echo fleet.py daemon nothing',
    ])
    assert upgrade.roots_in(table) == [root / 'config.json']
