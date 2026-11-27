"""On macOS a venv made from a framework Python (Homebrew, python.org) starts `<venv>/bin/python`, which re-executes
the framework's `.../Python.app/Contents/MacOS/Python`: `ps` shows that path as argv[0], and the venv's python is only
in the start environment as `__PYVENV_LAUNCHER__`. The interpreter check must take that launcher for the daemons and
the applets, and still fail a process whose launcher is another release's venv or that has no launcher at all.
"""
import json
import subprocess

import pytest

from upgrade_kit import FakeOps, Tree, make_release, py, run_upgrade
from fleetmux import fleet, release_drift, releases

FRAMEWORK = ('/opt/homebrew/Cellar/python@3.14/3.14.4_1/Frameworks/Python.framework/Versions/3.14/Resources/'
             'Python.app/Contents/MacOS/Python')


def _framework_tree(tmp_path, monkeypatch, launcher_rel):
    """A tree whose new daemons and applets show the framework Python in ps, with ``launcher_rel``'s venv python as
    their launcher (None: no launcher)."""
    old, new = make_release(tmp_path, 'aaaaaaa1'), make_release(tmp_path, 'bbbbbbb2')
    tree = Tree(tmp_path, old)
    tree.child('M1', job='job-1')
    ops = FakeOps(tree, new)
    ops.applet_pids = {'tick': (777, py(old))}
    monkeypatch.setattr(fleet, 'daemon_running', ops.daemon_running)

    def start(target):
        if target.name != 'bbbbbbb2':
            return
        rel = {'new': new, 'old': old, None: None}[launcher_rel]
        pids = [ops.pid_of[r] for r in ops.alive_runs] + [p for p, _ in ops.applet_pids.values()]
        for pid in pids:
            if pid in ops.argv_of:
                ops.argv_of[pid] = [FRAMEWORK] + ops.argv_of[pid][1:]
            if rel is not None:
                ops.launcher_of[pid] = py(rel)
        ops.applet_pids = {n: (p, FRAMEWORK) for n, (p, _) in ops.applet_pids.items()}
    ops.on_start.append(start)
    return tree, ops, new


def test_a_framework_python_whose_launcher_is_the_new_venv_is_on_the_new_interpreter(tmp_path, monkeypatch):
    tree, ops, new = _framework_tree(tmp_path, monkeypatch, 'new')
    code, lines = run_upgrade(tree, ops, new, extra=['--verify-timeout', '0'])
    out = json.loads(lines[-1])
    assert code == 0 and out['result'] == 'pass', out
    assert out['checks']['interpreter']['status'] == 'ok', out['checks']['interpreter']


@pytest.mark.parametrize('launcher', ['old', None])
def test_a_framework_python_with_another_or_no_launcher_still_fails(tmp_path, monkeypatch, launcher):
    tree, ops, new = _framework_tree(tmp_path, monkeypatch, launcher)
    code, lines = run_upgrade(tree, ops, new, extra=['--verify-timeout', '0'])
    out = json.loads(lines[-1])
    assert code == 1 and out['step'] == 'verify' and 'interpreter' in out['reason'], out
    assert 'root daemon' in out['reason'] and 'applet tick' in out['reason'], out['reason']



def test_the_way_back_is_judged_by_the_launcher_too(tmp_path, monkeypatch):
    """The release before the switch is a framework venv as well: when the new release fails its checks, the
    restarted old daemons and applets show the framework Python in ps with the old venv as launcher, and the way
    back must pass (rolled_back true), not fail on the same check."""
    tree, ops, new = _framework_tree(tmp_path, monkeypatch, None)
    old = tree.old

    def back(target):
        if target.name != old.name:
            return
        pids = [ops.pid_of[r] for r in ops.alive_runs] + [p for p, _ in ops.applet_pids.values()]
        for pid in pids:
            if pid in ops.argv_of:
                ops.argv_of[pid] = [FRAMEWORK] + ops.argv_of[pid][1:]
            ops.launcher_of[pid] = py(old)
        ops.applet_pids = {n: (p, FRAMEWORK) for n, (p, _) in ops.applet_pids.items()}
    ops.on_start.append(back)
    code, lines = run_upgrade(tree, ops, new, extra=['--verify-timeout', '0'])
    out = json.loads(lines[-1])
    assert code == 1 and out['step'] == 'verify' and 'interpreter' in out['reason'], out
    assert out['rolled_back'] is True, out['reason']
    assert 'going back' not in out['reason'], out['reason']

def test_the_launcher_does_not_rescue_a_python_that_is_not_a_framework_build(tmp_path, monkeypatch):
    """A launcher left in the environment of some other python (not a framework re-exec) does not count."""
    old, new = make_release(tmp_path, 'aaaaaaa1'), make_release(tmp_path, 'bbbbbbb2')
    tree = Tree(tmp_path, old)
    ops = FakeOps(tree, new)
    monkeypatch.setattr(fleet, 'daemon_running', ops.daemon_running)

    def start(target):
        if target.name == 'bbbbbbb2':
            pid = ops.pid_of[str(tree.run)]
            ops.argv_of[pid] = ['/usr/bin/python3'] + ops.argv_of[pid][1:]
            ops.launcher_of[pid] = py(new)
    ops.on_start.append(start)
    code, lines = run_upgrade(tree, ops, new, extra=['--verify-timeout', '0'])
    assert code == 1 and 'interpreter' in json.loads(lines[-1])['reason']


def test_the_launcher_is_read_from_the_start_environment_ps_shows():
    env = ('/fw/Python.app/Contents/MacOS/Python /r/fleetmux/fleet.py daemon /run/config.json '
           'TERM=xterm __PYVENV_LAUNCHER__=/r/.venv/bin/python3.14 HOME=/h')
    assert releases.launcher_in(env) == '/r/.venv/bin/python3.14'
    assert releases.launcher_in('/usr/bin/python3 x.py HOME=/h') is None
    assert releases.launcher_in('') is None


def test_a_launcher_path_with_blanks_is_read_whole():
    """A home directory with a blank in its name: the launcher runs up to the next environment word or the end."""
    rel = '/Users/Jane Doe/fleetmux-releases/new/.venv/bin/python3.14'
    env = ('/fw/Python.app/Contents/MacOS/Python /r/fleetmux/fleet.py daemon /run/config.json '
           'TERM=xterm __PYVENV_LAUNCHER__=%s HOME=/Users/Jane Doe LANG=C' % rel)
    assert releases.launcher_in(env) == rel
    assert releases.launcher_in('python x.py __PYVENV_LAUNCHER__=%s' % rel) == rel           # last word of the line
    assert releases.launcher_in('python x.py __PYVENV_LAUNCHER__=/a b/py\nOTHER=1') == '/a b/py'
    assert release_drift.in_release(releases.launcher_in(env), '/Users/Jane Doe/fleetmux-releases/new')


def test_on_linux_the_launcher_is_not_looked_up(monkeypatch):
    """Linux venvs keep the venv path as argv[0]: the check stays as it was, and ps -E is never run."""
    def no_ps(*a, **k):
        raise AssertionError('ps must not run on Linux')
    monkeypatch.setattr(releases.sys, 'platform', 'linux')
    monkeypatch.setattr(subprocess, 'run', no_ps)
    assert releases.launcher(12345) is None
    assert release_drift.Ops().launcher(12345) is None
