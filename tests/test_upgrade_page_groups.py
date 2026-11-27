"""After a resume every worker tab of the root page sits in the ``workers`` group.

An upgrade restarts the terminal server, so the root fleet resumed with its tabs gone. Most of its workers were
lent to job sub-fleets (a job sub-fleet's supervisor is a root worker, its slot ``delegated``). Seen on the root page
afterwards: the tabs of the lent slots and of the new worker generations sat in the ``supervisor`` group.

Read from the code before the fix:
- ``subfleet.reopen_parent_views`` opens a new client tab for each lent tmux slot next to the first live pane of
  ``workers + ['supervisor']``; with no live worker tab yet that is the supervisor, and ``attach_view`` tabs the new
  client into the peer's group. The next lent slot then takes the first one as its peer.
- ``RuntimeV2.respawn`` (page kept) tabs a worker without an old tab next to any live worker tab, a lent one too.
- ``Fleet.arrange_page`` leaves lent slots out, so nothing moves them back.

Real ``fleetmux`` server (``$FLEETMUX_REAL_BIN``, default ``rust/target/debug/fleetmux``), private state directory,
private tmux socket; the group members are read from the server (``layout.get`` / ``pane.list``), not from the
fleet's own registry. A missing binary fails the test (docs/design.md: a skip for a missing dependency is a failure).
"""
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_fleetmux_backend import _launcher, _teardown
from fleetmux import fleet, runtime_v2, subfleet
from fleetmux.backends import fleetmux as fm

ROOT = Path(__file__).resolve().parents[1]
REAL = Path(os.environ.get('FLEETMUX_REAL_BIN') or ROOT / 'rust/target/debug/fleetmux')
SESSION = 'fm-groups'


# Dump paths for the evidence logs, read once at import: the fixture replaces the whole environment below.
ECHO_DUMP = os.environ.get('FLEETMUX_TEST_ECHO_DUMP')
LAYOUT_DUMP = os.environ.get('FLEETMUX_TEST_LAYOUT_DUMP')
WHITELIST = ('HOME', 'SHELL', 'TMUX_TMPDIR')  # the only variables the echo prints
LOGIN_FILES = ('.bash_profile', '.bash_login', '.profile', '.bashrc')
# Written by the echo pane only when BASH_ENV or ENV is set there: the probe tests set-ness with
# ${VAR+x} and prints neither the names nor the values; the echo itself stays HOME / SHELL / TMUX_TMPDIR only.
MARKER = 'inherited-startup-entry.marker'
ECHO = ('if [ -n "${{BASH_ENV+x}}${{ENV+x}}" ]; then : > {marker}; fi; '
        'printf "HOME=%s\\nSHELL=%s\\nTMUX_TMPDIR=%s\\n" "$HOME" "$SHELL" "$TMUX_TMPDIR" > {out}')


def minimal_env(root, home, tmux_tmp, binary):
    """The whole environment of the test process while the server runs: built here, nothing inherited.
    The server is started without ``env=`` (backends/fleetmux.py), so it gets this plus PYTEST_CURRENT_TEST (kept by
    use_only) and the PWD / SHLVL / _ that the /bin/sh launcher adds; a pane gets the server's plus COLORTERM
    (portable-pty). No BASH_ENV / ENV / TMUX / ZDOTDIR: a
    non-interactive bash or sh has no inherited start-up file to read; the login bash of a pane reads /etc/profile and
    the temporary HOME's files only. The echo pane proves BASH_ENV / ENV unset with MARKER.

    PATH holds no Homebrew directory: ``<root>/bin`` (the fleetmux launcher and a tmux symlink, see ``server``) and the
    system directories. What each command resolves to:
    - the launcher's ``#!/bin/sh``: the kernel runs /bin/sh;
    - the pane's ``bash`` (argv[0], backends/fleetmux.py ``bash -lc``), searched by the server along this PATH:
      <root>/bin has no bash and /usr/bin has none, so /bin/bash;
    - ``sh`` and ``sleep`` inside a pane, after /etc/profile's path_helper put /etc/paths first: /bin/sh, /bin/sleep
      (path_helper appends /etc/paths.d entries, Homebrew's among them, after /bin);
    - ``tmux`` run by this test (``tmux_session``, test_fleetmux_backend._teardown): <root>/bin/tmux, the symlink to
      the tmux binary found on the runner's PATH before the environment was replaced;
    - ``cmux.TMUX`` in the tab attach command: an absolute path fixed at import from the real environment, the same
      tmux binary."""
    path = os.pathsep.join(dict.fromkeys([str(binary.parent), '/usr/bin', '/bin', '/usr/sbin', '/sbin']))
    return {'HOME': str(home), 'SHELL': '/bin/sh', 'TMUX_TMPDIR': str(tmux_tmp), 'PATH': path,
            'TMPDIR': str(root / 'tmp'), 'LANG': 'en_US.UTF-8', 'TERM': 'xterm-256color', fm.BIN_ENV: str(binary)}


def use_only(monkeypatch, env):
    """Replace the process environment with ``env`` for this test (monkeypatch restores it afterwards). pytest's own
    PYTEST_CURRENT_TEST stays: pytest sets and pops it around each phase."""
    for key in list(os.environ):
        if key != 'PYTEST_CURRENT_TEST':
            monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    left = set(os.environ) - set(env) - {'PYTEST_CURRENT_TEST'}
    assert not left, 'inherited variables left: %s' % sorted(left)


def echo_env(env):
    """What a pane of the server under test sees: HOME, SHELL and TMUX_TMPDIR only (the whitelist), which login
    file of the temporary HOME it read (each one touches a marker; nothing is exported or printed), and whether
    BASH_ENV or ENV was set there (MARKER; written before the echo file, so it exists by the time the echo is read)."""
    out = env.root / 'echo.txt'
    command = ECHO.format(out=shlex.quote(str(out)), marker=shlex.quote(str(env.root / MARKER)))
    res = env.mgr.spawn_window('echo', 'sh -c ' + shlex.quote(command), metadata={'type': 'worker'})
    assert res.success, env.mgr.last_error
    for _ in range(100):
        if out.is_file() and out.read_text().count('\n') >= len(WHITELIST):
            break
        time.sleep(0.05)
    seen = dict(line.split('=', 1) for line in out.read_text().splitlines())
    assert set(seen) == set(WHITELIST), seen
    read = sorted(name for name in LOGIN_FILES if (env.home / (name + '.read')).exists())
    inherited = (env.root / MARKER).exists()
    if ECHO_DUMP:
        with open(ECHO_DUMP, 'a') as fh:
            fh.write('[%s] %s\n%sTEMP_HOME_LOGIN_FILES_READ=%s\nINHERITED_STARTUP_ENTRY_MARKER=%s\n' % (
                os.environ.get('PYTEST_CURRENT_TEST', '').split(' ')[0], out, out.read_text(), ','.join(read),
                'present' if inherited else 'absent'))
    env.mgr.kill_pane(res.pane_id)
    return seen, read, inherited


@pytest.fixture
def server(monkeypatch):
    """A private real server run in an explicitly built minimal environment: temporary HOME, SHELL=/bin/sh,
    temporary TMUX_TMPDIR, PATH of <root>/bin (launcher, tmux symlink) and the system directories, nothing
    inherited. Its runtime is under the temporary root; the test's tmux servers use private sockets under it
    (``-f /dev/null``). Before the test body runs, a pane must echo exactly the temporary HOME / SHELL / TMUX_TMPDIR,
    its login bash must have read the temporary HOME's .bash_profile, and it must not have written MARKER (BASH_ENV /
    ENV set); otherwise the test fails here."""
    if not (REAL.is_file() and os.access(REAL, os.X_OK)):
        pytest.fail('real fleetmux not built: ' + str(REAL) + ' (cargo build -p fleetmux, or set FLEETMUX_REAL_BIN)')
    if not shutil.which('tmux'):
        pytest.fail('tmux not found on PATH')
    root = Path(tempfile.mkdtemp(prefix='fmg', dir='/tmp')).resolve()  # short: Unix socket path limits
    home, tmux_tmp = root / 'home', root / 'tmux-tmp'
    for d in (home, tmux_tmp, root / 'state', root / 'tmux', root / 'tmp'):
        d.mkdir()
    for name in LOGIN_FILES:  # a marker per file: which of them the pane's login bash read
        (home / name).write_text(': > "$HOME/%s.read"\n' % name)
    bindir = root / 'bin'
    bindir.mkdir()
    binary = _launcher(bindir / 'fleetmux', [REAL])
    (bindir / 'tmux').symlink_to(Path(shutil.which('tmux')).resolve())  # PATH needs no Homebrew directory
    use_only(monkeypatch, minimal_env(root, home, tmux_tmp, binary))
    env = SimpleNamespace(root=root, state=root / 'state', bindir=bindir, binary=binary, home=home, tmux_tmp=tmux_tmp)
    env.mgr = fm.FleetmuxSessionManager(SESSION, state_dir=env.state)  # --runtime <root>/state/fleetmux/<token>
    try:
        seen, read, inherited = echo_env(env)
        want = {'HOME': str(home), 'SHELL': '/bin/sh', 'TMUX_TMPDIR': str(tmux_tmp)}
        if seen != want or '.bash_profile' not in read or inherited:
            pytest.fail('the server under test does not run in the temporary environment: %r != %r, login files '
                        'read from the temporary HOME: %r, BASH_ENV or ENV set in the pane: %r'
                        % (seen, want, read, inherited))
        env.echo, env.login_read, env.marker = seen, read, root / MARKER
        yield env
    finally:
        _teardown(env)


def tmux_session(env, name):
    """A live session on the test's own tmux socket (a lent slot's CLI keeps running there)."""
    sock = env.root / 'tmux' / (name + '.sock')
    # -f /dev/null: the tmux server starts with no configuration at all (never ~/.tmux.conf); the client tab
    # attaches to this running server, so the config path in its command is never loaded.
    subprocess.run(['tmux', '-S', str(sock), '-f', '/dev/null', 'new-session', '-d', '-s', name, 'sleep 600'],
                   check=True, capture_output=True, timeout=20)
    return {'tmux_socket': str(sock), 'tmux_session': name}


def tab(env, title, kind):
    res = env.mgr.spawn_window(title, 'sleep 600', metadata={'type': kind})
    assert res.success, env.mgr.last_error
    return res.pane_id


def layout(env):
    """What the server shows: {group: [pane titles]} plus the raw replies."""
    groups = env.mgr._call('layout.get').get('groups') or {}
    rows = env.mgr._list()
    title = {str(r['pane']): str(r.get('title') or '') for r in rows}
    raw = {'layout.get': groups, 'pane.list': rows}
    if LAYOUT_DUMP:
        Path(LAYOUT_DUMP).write_text(json.dumps(raw, indent=2, sort_keys=True, default=str) + '\n')
    return {g: [title.get(str(p), str(p)) for p in groups.get(g) or []] for g in fm.GROUPS}, raw


class RootFleet:
    """The part of a root ``Fleet`` the resume steps under test read and write."""

    def __init__(self, env, run_dir, agents):
        self.mgr, self.run_dir, self.cfg = env.mgr, run_dir, {'tab_prefix': ''}
        self.agents = agents
        self.workers = [s for s in agents if s != 'supervisor']
        self.logs, self.r = [], SimpleNamespace(hget=lambda *a: None, hset=lambda *a, **k: None)

    def agent(self, slot):
        return self.agents.setdefault(slot, {})

    def label(self, slot):
        return self.agent(slot).get('label') or slot

    def setf(self, slot, **fields):
        self.agent(slot).update({k: str(v) for k, v in fields.items()})

    def live_pane(self, pane):
        return bool(pane) and self.mgr.get_pane_status(pane) is not None

    def log(self, event, **kw):
        self.logs.append(dict(kw, event=event))

    def spawn(self, slot, prompt, launch_held=False):
        new = tab(SimpleNamespace(mgr=self.mgr), 'fx-' + self.label(slot), 'worker')
        self.setf(slot, pane=new)
        return new

    def refresh_history(self):
        pass

    def history_lost(self):
        return False

    def finish(self, status, reason):
        raise AssertionError('finish(%s): %s' % (status, reason))


def lend(run_dir, name, slot, view, old_pane):
    """A running job sub-fleet that took ``slot`` as its supervisor (adopted tmux session ``view``)."""
    d = run_dir / 'sub' / name
    d.mkdir(parents=True)
    (d / 'subfleet.json').write_text(json.dumps({'state': 'running', 'slot': slot, 'view': True, 'pane': old_pane,
                                                 'task_id': name, 'created': len(list((run_dir / 'sub').iterdir()))}))
    (d / 'config.json').write_text(json.dumps({'adopt': {'info': view}}))


@pytest.fixture
def lent_root(server, tmp_path, monkeypatch):
    """Right after the upgrade's terminal restart: the root supervisor's new tab is up, its two workers are lent to
    job sub-fleets M1 / M2 (their old tabs died with the server), no other worker tab is live yet."""
    monkeypatch.setattr(subfleet, 'child_supervisor_generation', lambda cfg: '0')  # still the adopted session
    monkeypatch.setattr(fleet, 'load', lambda path: json.loads(Path(path).read_text()))
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    sup = tab(server, 'fx-supervisor', 'supervisor')
    agents = {'supervisor': {'pane': sup, 'label': 'supervisor'},
              'dev-1': {'state': 'delegated', 'pane': 'gone-1', 'label': 'dev-1'},
              'dev-2': {'state': 'delegated', 'pane': 'gone-2', 'label': 'dev-2'}}
    lend(run_dir, 'M1', 'dev-1', tmux_session(server, 'm1-sup'), 'gone-1')
    lend(run_dir, 'M2', 'dev-2', tmux_session(server, 'm2-sup'), 'gone-2')
    return RootFleet(server, run_dir, agents)


def assert_worker_tabs_in_workers(env, f):
    groups, raw = layout(env)
    workers = ['fx-' + f.label(w) for w in f.workers if f.live_pane(f.agent(w).get('pane'))]
    assert workers, raw
    misplaced = [t for t in workers if t not in groups['workers']]
    assert not misplaced, 'worker tabs outside the workers group: %s\n%s' % (
        misplaced, json.dumps(raw, indent=1, sort_keys=True, default=str))
    assert groups['supervisor'] == ['fx-supervisor'], groups


def test_server_panes_see_only_the_temporary_environment(server):
    """First case: the proof the rest of this file stands on. A pane of the server under test prints only
    HOME, SHELL and TMUX_TMPDIR, all temporary; the login bash read the temporary HOME's .bash_profile (marker); the
    test process itself holds no inherited variable (BASH_ENV, ENV, ... absent), and neither BASH_ENV nor ENV was set
    in the pane (no MARKER)."""
    assert server.echo == {'HOME': str(server.home), 'SHELL': '/bin/sh', 'TMUX_TMPDIR': str(server.tmux_tmp)}
    assert '.bash_profile' in server.login_read, server.login_read
    assert str(server.home).startswith(str(server.root)) and str(server.tmux_tmp).startswith(str(server.root))
    assert not {'BASH_ENV', 'ENV', 'TMUX', 'ZDOTDIR'} & set(os.environ), sorted(os.environ)
    assert server.marker == server.root / MARKER and not server.marker.exists(), 'BASH_ENV or ENV was set in the pane'


def test_reopened_delegated_view_lands_in_workers(server, lent_root):
    f = lent_root
    subfleet.reopen_parent_views(f)
    assert [e['event'] for e in f.logs] == ['resume.delegated_view_reattached'] * 2, f.logs
    assert_worker_tabs_in_workers(server, f)


def test_new_generation_worker_does_not_follow_a_misplaced_peer(server, lent_root):
    """The page stayed (respawn's second branch): the new generation of dev-3, whose old tab is gone, is tabbed
    next to a live worker tab -- a lent slot's tab reopened just before."""
    f = lent_root
    f.agents['dev-3'] = {'state': 'dead', 'pane': '', 'label': 'dev-3', 'generation': '1'}
    f.workers.append('dev-3')
    subfleet.reopen_parent_views(f)
    rt = SimpleNamespace(f=f, confirm_retired=lambda slot: None, audit=lambda *a, **k: None)
    runtime_v2.RuntimeV2.respawn(rt, ['dev-3'], True, prompt='You are {label}. Reply READY.')
    assert f.label('dev-3') == 'dev-3.2' and f.live_pane(f.agent('dev-3')['pane'])
    assert_worker_tabs_in_workers(server, f)
