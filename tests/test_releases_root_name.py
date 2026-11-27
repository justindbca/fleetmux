"""The releases root is $FLEETMUX_RELEASES, whatever its directory is called (the installer allows any root, also a
nested one). A release directory right under it is a release named by its directory, the same as under the default
``~/fleetmux-releases``: for the code of a directory (``release_of``), for an interpreter path (``from_python``) and
so for the release a daemon runs (``running``, ``of_argv``).
"""
import subprocess

import pytest

from fleetmux import releases


def _release(root, name):
    py = root / name / '.venv' / 'bin' / 'python3.14'
    py.parent.mkdir(parents=True)
    py.write_text('#!/bin/sh\n')
    (root / name / 'fleetmux').mkdir()
    (root / name / 'fleetmux' / 'fleet.py').write_text('')
    return root / name, py


@pytest.fixture
def no_git(monkeypatch):
    """A release directory is not a checkout: no commit to fall back on."""
    real = subprocess.run

    def run(argv, *a, **k):
        if argv[:1] == ['git']:
            return subprocess.CompletedProcess(argv, 128, '', 'not a git repository')
        return real(argv, *a, **k)
    monkeypatch.setattr(releases.subprocess, 'run', run)


def test_a_release_under_a_releases_root_of_another_name_is_known(tmp_path, monkeypatch, no_git):
    root = tmp_path / 'a42' / 'releases' / 'x'
    rel, py = _release(root, 'abc1234def')
    monkeypatch.setenv('FLEETMUX_RELEASES', str(root))
    assert releases.release_of(rel) == 'abc1234def'
    assert releases.from_python(str(py)) == 'abc1234def'
    assert releases.of_argv([str(py), str(rel / 'fleetmux' / 'fleet.py'), 'daemon']) == 'abc1234def'
    assert releases.running({'run': str(tmp_path / 'run'), 'python': str(py)}) == 'abc1234def'


def test_the_root_is_matched_as_given_and_resolved(tmp_path, monkeypatch, no_git):
    real = tmp_path / 'real-root'
    rel, py = _release(real, 'v1.2.3')
    (tmp_path / 'link-root').symlink_to(real)
    monkeypatch.setenv('FLEETMUX_RELEASES', str(tmp_path / 'link-root'))
    assert releases.release_of(rel) == 'v1.2.3'                                  # resolved path, root via link
    assert releases.from_python(str(py)) == 'v1.2.3'
    assert releases.from_python(str(tmp_path / 'link-root' / 'v1.2.3' / '.venv' / 'bin' / 'python3.14')) == 'v1.2.3'


def test_outside_the_releases_root_nothing_changes(tmp_path, monkeypatch, no_git):
    root = tmp_path / 'rels'
    _release(root, 'abc1234def')
    other, other_py = _release(tmp_path / 'elsewhere', 'abc1234def')
    monkeypatch.setenv('FLEETMUX_RELEASES', str(root))
    assert releases.release_of(other) is None                                     # not a release: a checkout or nothing
    assert releases.from_python(str(other_py)) is None
    assert releases.from_python(str(root)) is None                                # the root itself names no release
    monkeypatch.delenv('FLEETMUX_RELEASES')
    assert releases.from_python(str(root / 'abc1234def' / '.venv' / 'bin' / 'python3.14')) is None


def test_the_default_root_name_still_names_the_release(tmp_path, monkeypatch, no_git):
    rel, py = _release(tmp_path / 'fleetmux-releases', 'abc1234def')
    monkeypatch.delenv('FLEETMUX_RELEASES', raising=False)
    assert releases.release_of(rel) == 'abc1234def' and releases.from_python(str(py)) == 'abc1234def'


class _Ops:
    """Only what ``release_drift.processes`` and the server part of ``check_daemons`` read."""
    def __init__(self, procs, run):
        self.procs, self.run = procs, run

    def own(self):
        return 'old'

    def daemon_running(self, run):
        return run == str(self.run)

    def alive(self, pid):
        return pid in self.procs

    def cmdline(self, pid):
        return self.procs[pid].split() if pid in self.procs else None

    def ps(self):
        return sorted(self.procs.items())


def test_the_server_and_an_applet_under_a_root_of_another_name_name_their_release(tmp_path, monkeypatch, no_git):
    """The switch reads the release of the fleetmux server and of each applet from their command lines: under a
    releases root of another name they are known too, so a server left on the old release fails the check."""
    import json
    from types import SimpleNamespace
    from fleetmux import release_drift
    root = tmp_path / 'rels'
    old, old_py = _release(root, 'old')
    new, new_py = _release(root, 'new')
    (old / 'bin').mkdir()
    (old / 'bin' / 'fleetmux').write_text('')
    monkeypatch.setenv('FLEETMUX_RELEASES', str(root))
    run, rt = tmp_path / 'run', tmp_path / 'rt'
    (run / 'applets' / 'relay').mkdir(parents=True)
    rt.mkdir()
    (run / 'config.json').write_text(json.dumps({'run': str(run), 'python': str(new_py)}))
    (run / 'applets' / 'relay' / 'pid').write_text('400')
    (rt / 'server.json').write_text(json.dumps({'pid': 300}))
    ops = _Ops({100: '%s %s/fleetmux/fleet.py daemon %s/config.json' % (new_py, new, run),
                300: '%s/bin/fleetmux server --runtime %s --session fleetmux --daemonize' % (old, rt),
                400: '%s -m fleetmux.relay_applet' % new_py}, run)
    f = SimpleNamespace(cfg={'run': str(run), 'name': 'demo'}, mgr=SimpleNamespace(runtime_root=rt))
    rows = {r['kind']: r['release'] for r in release_drift.processes(f, ops)}
    assert rows['fleetmux server'] == 'old' and rows['applet'] == 'new'
    target = {'release': 'new', 'dir': str(new)}
    plan = {'run': str(run), 'server_runtime': str(rt), 'config': str(run / 'config.json'), 'new': target,
            'old': {'release': 'old', 'dir': str(old)}}
    out = release_drift.check_daemons(plan, {}, target, ops, {'subs': []})
    assert out['status'] == 'fail' and 'fleetmux server runs release old' in out['detail']
