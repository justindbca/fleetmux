"""``fleetmux upgrade running``: the release directories the fleet daemons of this machine run now.

Rollout's release_install target reads this before it moves ``~/fleetmux-current``: one
``fleetmux scenario upgrade --from <dir> --to <new release>`` per directory listed. The last stdout line is JSON:
``{"result": "pass", "releases": [<absolute dirs>], "running": true, "daemons": [...]}``, exit 0. A daemon's release
is its ``<run>/release`` record, found as a directory under the releases roots or as the directory of the run
config's ``python``. Without a running daemon the installed release (``~/fleetmux-current``) is listed with
``"running": false``; with neither the list is empty. A daemon whose release is unknown, or whose directory is gone,
fails (exit 1, ``"step": "running"``): rollout must not judge a switch it could not try.

Roots and daemons are injected; nothing running on this machine is read.
"""
import json
import shutil
from pathlib import Path

import pytest

from upgrade_kit import Tree, make_release
from fleetmux import releases, upgrade


class Machine:
    """Which run directories have a daemon (``alive``) and which root configs ps would list (``roots``)."""

    def __init__(self):
        self.alive, self.roots = set(), []

    def daemon_running(self, run):
        return str(run) in self.alive

    def running_roots(self):
        return list(self.roots)

    def self_detach(self):
        raise AssertionError('`upgrade running` only reads: it never detaches')


@pytest.fixture
def m(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('FLEETMUX_RELEASES', str(tmp_path / 'fleetmux-releases'))
    return {'tmp': tmp_path, 'ops': Machine(), 'a': make_release(tmp_path, 'aaaaaaa1'),
            'c': make_release(tmp_path, 'ccccccc3')}


def tree(m, key, rel, subs=()):
    t = Tree(m['tmp'] / key, rel)
    for s in subs:
        t.child(s)
    m['ops'].roots.append(t.cfg_path)
    m['ops'].alive.update(str(d) for d in t.nodes.values())
    return t


def running(m, *extra):
    lines = []
    code = upgrade.main(['running'] + list(extra), ops=m['ops'], out=lines.append)
    return code, json.loads(lines[-1]), lines


def test_it_lists_each_release_a_running_daemon_runs_once(m):
    tree(m, 'A', m['a'], subs=('M1',))
    b = tree(m, 'B', m['a'], subs=('M1',))
    (b.nodes['M1'] / 'release').write_text('ccccccc3\n')              # a sub-fleet still on another release
    code, out, _ = running(m)
    assert code == 0 and out['result'] == 'pass', out
    assert out['releases'] == [str(m['a'].resolve()), str(m['c'].resolve())], out
    assert out['running'] is True and len(out['daemons']) == 4, out


def test_a_sub_fleet_without_a_daemon_is_not_listed(m):
    t = tree(m, 'A', m['a'], subs=('M1',))
    (t.nodes['M1'] / 'release').write_text('ccccccc3\n')
    m['ops'].alive.discard(str(t.nodes['M1']))                         # it ended; its record stays on disk
    code, out, _ = running(m)
    assert code == 0 and out['releases'] == [str(m['a'].resolve())], out


def test_the_python_path_names_the_release_when_the_run_has_no_record(m):
    t = Tree(m['tmp'] / 'A', m['c'], release_file=False)              # a daemon started before <run>/release existed
    m['ops'].roots.append(t.cfg_path)
    m['ops'].alive.add(str(t.run))
    code, out, _ = running(m)
    assert code == 0 and out['releases'] == [str(m['c'].resolve())], out


def test_config_limits_it_to_one_tree(m):
    tree(m, 'A', m['a'])
    b = tree(m, 'B', m['c'])
    code, out, _ = running(m, '--config', str(b.cfg_path))
    assert code == 0 and out['releases'] == [str(m['c'].resolve())], out


def test_without_a_running_daemon_it_lists_the_installed_release(m, monkeypatch):
    (m['tmp'] / 'fleetmux-current').symlink_to(m['c'])
    monkeypatch.setattr(releases, 'LINK', str(m['tmp'] / 'fleetmux-current'))   # the suite pins it elsewhere
    code, out, _ = running(m)
    assert code == 0 and out['result'] == 'pass', out
    assert out['releases'] == [str(m['c'].resolve())] and out['running'] is False, out


def test_without_a_daemon_or_an_installed_release_the_list_is_empty(m):
    code, out, _ = running(m)
    assert code == 0 and out['result'] == 'pass' and out['releases'] == [], out
    assert 'nothing to switch from' in out['reason'], out


def test_an_unknown_release_fails(m):
    t = Tree(m['tmp'] / 'A', m['tmp'] / 'checkout', release_file=False)  # python of a checkout, no record
    m['ops'].roots.append(t.cfg_path)
    m['ops'].alive.add(str(t.run))
    code, out, _ = running(m)
    assert code == 1 and out['result'] == 'fail' and out['step'] == 'running', out
    assert str(t.run) in out['reason'] and 'unknown' in out['reason'], out
    assert out['releases'] == [], out


def test_a_release_whose_directory_is_gone_fails(m):
    gone = make_release(m['tmp'], 'ddddddd4')
    tree(m, 'A', gone)
    shutil.rmtree(gone)
    code, out, _ = running(m)
    assert code == 1 and out['step'] == 'running' and 'ddddddd4' in out['reason'], out

