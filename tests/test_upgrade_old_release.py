"""`fleetmux upgrade` from a release that has no such command (fleetmux/upgrade.py): the
command runs from the new release, and the one switch it hands its plan to (fleetmux/release_drift.py) stops the old
daemons the only way that ends their loop without a cancel, SIGTERM. `fleetmux stop` and the v2 stop event retire
every slot and cancel the jobs, so they are never used.
"""
import io
import json
import signal
import tokenize
from pathlib import Path

from upgrade_kit import FakeOps, Tree, kills, make_release, run_upgrade
from fleetmux import fleet, release_drift, upgrade

JOBS = {'job-aaaaaaaaaa': {'state': 'running', 'slot': 'worker-1', 'task': 'M1', 'created': 1.0}}


def test_old_release_gets_sigterm_never_stop(tmp_path, monkeypatch):
    old, new = make_release(tmp_path, 'f95f6642'), make_release(tmp_path, '18a2b542')
    tree = Tree(tmp_path, old, release_file=False)  # an old daemon wrote no <run>/release and no jobs.json
    tree.child('M1', job='job-aaaaaaaaaa')
    tree.child('N', named=True, name='N', autostart=True)
    ops = FakeOps(tree, new, jobs=JOBS)
    monkeypatch.setattr(fleet, 'daemon_running', ops.daemon_running)
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    assert kills(ops) and all(sig == signal.SIGTERM for _, sig in kills(ops))
    for kind, *rest in ops.events:
        if kind == 'run':
            argv = rest[0]
            assert 'stop' not in argv and 'cancel' not in argv, argv
            assert argv[0] == str(new / '.venv' / 'bin' / 'python3.14')  # the new release runs every command
    assert json.loads((tree.run / 'jobs.json').read_text()) == JOBS
    assert (tree.home / 'job-runs' / (tree.run.name + '.json')).exists()
    out = json.loads(lines[-1])
    assert out['from'] == 'f95f6642' and out['to'] == '18a2b542'  # the running release from the python path


def test_the_source_never_sends_a_stop():
    """No `stop` subcommand, no v2 stop event, no bus post anywhere in the command's code or in the switch's."""
    for module in (upgrade, release_drift):
        src = Path(module.__file__).read_text()
        strings = [tok.string for tok in tokenize.generate_tokens(io.StringIO(src).readline)
                   if tok.type == tokenize.STRING]
        assert not [s for s in strings if s.strip('\'"') in ('stop', 'cancel')], module.__name__
        assert 'post_event' not in src and 'SIGKILL' not in src, module.__name__  # daemons only ever get SIGTERM
    src = Path(upgrade.__file__).read_text()
    assert "'kind'" not in src and '"kind"' not in src


def test_signals_go_to_daemon_pids_of_this_tree_only(tmp_path, monkeypatch):
    old, new = make_release(tmp_path, 'f95f6642'), make_release(tmp_path, '18a2b542')
    tree = Tree(tmp_path, old, release_file=False)
    tree.child('M1', job='job-aaaaaaaaaa')
    ops = FakeOps(tree, new, jobs=JOBS)
    monkeypatch.setattr(fleet, 'daemon_running', ops.daemon_running)
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    assert sorted(Path(r).name for _, r, _ in [e for e in ops.events if e[0] == 'kill']) == sorted(
        [tree.run.name, 'M1'])


def test_daemon_pids_match_the_exact_config_and_skip_the_wrapper():
    table = ('101 /usr/bin/caffeinate -i /r/old/.venv/bin/python3.14 /r/old/fleetmux/fleet.py daemon /h/run/config.json\n'
             '102 /r/old/.venv/bin/python3.14 /r/old/fleetmux/fleet.py daemon /h/run/config.json --resume\n'
             '103 /r/old/.venv/bin/python3.14 /r/old/fleetmux/fleet.py daemon /h/run/sub/M1/config.json\n'
             '104 /r/old/.venv/bin/python3.14 /r/old/fleetmux/fleet.py daemon /h/run2/config.json\n')

    class Table(release_drift.Ops):
        def ps(self):
            return [(int(line.split()[0]), line.split(None, 1)[1]) for line in table.splitlines()]
    assert release_drift._daemon_pids(Table(), '/h/run', True) == [102]
    assert release_drift._daemon_pids(Table(), '/h/run/sub/M1', True) == [103]
