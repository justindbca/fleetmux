"""M4 B3: a tmux slot's CLI liveness and retirement on the fleetmux backend (design-1/m4-design.md v0.7 §6.1 rows
17 / 22, §8.2b; spec py-1 ``m4-tmux-liveness.md`` §3).

A tmux slot's fleetmux pane only runs a ``tmux attach`` client; its CLI runs under the fleet's own tmux server. Today
``has_cli_process`` only looks at the pane's tty (always False for such a pane, F8) and ``kill_pane`` only ends the
client (the session and its CLI run on). cmux has both tmux branches (``tests/test_tmux_view.py`` pins them).

These tests do not use pages: the child manager is an unbound ``fleetmux:ops`` manager, so they are red for the tmux
reason only. Fixture ``fm_tmux_world`` borrows ``tests/test_tmux_view.py``'s isolation (fake HOME, ``TMUX_TMPDIR``,
``SHELL=/bin/sh``, a marked ``~/.tmux.conf``, ``claude -> /bin/sleep``) but not its clean-up: no ``tmp_path``, no
``pkill -f``, no ``rmtree``. The root ``~/.m4e/fm4-py/tv-*`` is kept; the fake CLI, tmux server and fleetmux servers
are registered right after they start; teardown = ``kill_pane`` -> ``subfleet.stop_tmux`` -> ``end_server()``, then the
identity-checked fallback (which fails the teardown if it had to signal anything).
"""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _fake_claude  # noqa: E402
import _fm4_world as W  # noqa: E402
from _fleetmux_milestone import milestone  # noqa: E402
from fleetmux import subfleet  # noqa: E402

TMUX = 'tmux'


@pytest.fixture(params=['fake', 'real'])
def fm_tmux_world(request, monkeypatch):
    root = W.new_root('tv', request, request.param)
    home = root / 'h'
    home.mkdir()
    (home / '.tmux.conf').write_text('set -g history-limit 7\nset -g @poisoned 1\n')  # must never be read
    tmux_tmp = root / 'tmux-default'
    tmux_tmp.mkdir()
    cli = root / 'cli'
    cli.mkdir()
    claude = _fake_claude.make(cli)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('TMUX_TMPDIR', str(tmux_tmp))
    monkeypatch.setenv('SHELL', '/bin/sh')
    W.install_fleetmux(monkeypatch, root, request.param)
    run = root / 'run'
    (run / 'prompts').mkdir(parents=True)
    state = root / 'state'
    state.mkdir()
    s = W.Servers(root, request.param, state)
    w = SimpleNamespace(root=root, run=run, sock=str(run / 'tmux.sock'), claude=str(claude), servers=s,
                        ledger=s.ledger, parent=s.root_mgr(), child=s.root_mgr(W.ROOT_SESSION + ':ops'))
    W.note(root, 'tmux socket: %s' % w.sock)
    yield w
    s.close(extra=[lambda: subfleet.stop_tmux({'run': str(run)})])


def tmux(w, *args):
    return subprocess.run([TMUX, '-S', w.sock, *args], capture_output=True, text=True, timeout=10)


def sessions(w):
    done = tmux(w, 'list-sessions', '-F', '#{session_name}')
    return done.stdout.split() if done.returncode == 0 else []


def clients(w, session):
    done = tmux(w, 'list-clients', '-t', '=' + session, '-F', '#{client_tty}')
    return done.stdout.split() if done.returncode == 0 else []


def history(w, session):
    done = tmux(w, 'capture-pane', '-p', '-J', '-S', '-', '-t', '=' + session + ':')  # '=<s>' alone is a pane name
    return done.stdout if done.returncode == 0 else ''


def cli_pid(w, session):
    """This session's fake CLI: the process under the tmux pane whose argv[0] is our ``claude`` (as
    ``tests/test_tmux_view.py`` matches it), found from the tmux pane pid down, never by a machine-wide pattern."""
    done = tmux(w, 'list-panes', '-s', '-t', '=' + session, '-F', '#{pane_pid}')
    roots = {int(p) for p in done.stdout.split() if p.isdigit()}
    table = subprocess.run(['ps', '-axww', '-o', 'pid=,ppid=,args='], capture_output=True, text=True).stdout
    rows = []
    for line in table.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    tree, grew = set(roots), True
    while grew:
        grew = False
        for pid, ppid, _ in rows:
            if ppid in tree and pid not in tree:
                tree.add(pid)
                grew = True
    for pid, _, args in rows:
        if pid in tree and args.split()[:1] == [w.claude]:
            return pid
    return None


def spawn(w, slot='ops-a', generation=0, lines=0):
    session = '%s-g%d' % (slot, generation)
    out = w.run / 'prompts' / ('%s.terminal.log' % session)
    cli = (('seq 1 %d; ' % lines) if lines else '') + w.claude + ' ' + _fake_claude.args(600)
    res = w.parent.spawn_interactive_cli('fx-' + slot, cli, output_file=str(out), start_directory=str(w.run),
                                         auto_close=False, tmux_socket=w.sock, tmux_session=session)
    assert res.success, (res.error, w.parent.last_error)
    register(w, session)
    return res.pane_id, out, session


def register(w, session):
    """Right after a spawn: the fleetmux server(s), the tmux server and the session's fake CLI."""
    for mgr in (w.parent, w.child):
        w.ledger.register('fleetmux-server:' + mgr.session_name, mgr._server_pid())
    done = tmux(w, 'display-message', '-p', '#{pid}')
    w.ledger.register('tmux-server', done.stdout.strip())
    assert W.wait_for(lambda: cli_pid(w, session) is not None), 'no fake CLI under ' + session
    assert w.ledger.register('fake-cli:' + session, cli_pid(w, session))


def view_of(w, owner):
    peer = w.child.spawn_window('worker-1', 'sleep 600', metadata={'type': 'worker'}, start_directory=str(w.run))
    assert peer.success, (peer.error, w.child.last_error)
    w.ledger.register('fleetmux-server:' + w.child.session_name, w.child._server_pid())
    info = w.parent.tmux_view_info(owner)
    assert info and info['tmux_socket'] == w.sock
    view = w.child.attach_view(info, peer.pane_id)
    assert view and view != owner, w.child.last_error
    return view


# ================================================================ T1 liveness

@milestone(4)
def test_fleetmux_has_cli_process_sees_the_cli_in_the_tmux_session(fm_tmux_world):
    w = fm_tmux_world
    owner, _, session = spawn(w)
    assert W.wait_for(lambda: len(clients(w, session)) == 1)
    # the pane's own tty only carries the tmux client (that is why the tty probe cannot see the CLI)
    assert 'claude' not in ' '.join(w.parent._ps(w.parent._tty(owner), 'comm='))
    assert W.wait_for(lambda: w.parent.has_cli_process(owner, ('claude',)), timeout=10), \
        'has_cli_process(owner) is False while the CLI runs in tmux session ' + session
    view = view_of(w, owner)
    assert W.wait_for(lambda: len(clients(w, session)) == 2)
    assert W.wait_for(lambda: w.child.has_cli_process(view, ('claude',)), timeout=10)
    assert w.parent.has_cli_process(owner, ('codex',)) is False  # the names still matter
    shell = w.parent.spawn_window('shell', '/bin/sh', metadata={'type': 'tab'}, start_directory=str(w.run))
    assert shell.success
    assert w.parent.has_cli_process(shell.pane_id, ('claude',)) is False  # a bare shell never matches
    assert tmux(w, 'kill-session', '-t', '=' + session).returncode == 0
    assert W.wait_for(lambda: not w.parent.has_cli_process(owner, ('claude',)))
    assert W.wait_for(lambda: not w.child.has_cli_process(view, ('claude',)))


# ================================================================ T2 owner retires its session, a view never does

@milestone(4)
def test_fleetmux_kill_pane_of_a_tmux_owner_ends_its_session(fm_tmux_world):
    w = fm_tmux_world
    owner, out, session = spawn(w, lines=3000)
    assert W.wait_for(lambda: '3000' in history(w, session).split())
    view = view_of(w, owner)
    assert W.wait_for(lambda: len(clients(w, session)) == 2)
    # a view only ends its own client: session and CLI live on
    assert w.child.kill_pane(view) is True
    assert W.wait_for(lambda: len(clients(w, session)) == 1)
    assert session in sessions(w) and w.ledger.alive('fake-cli:' + session)
    # the owner's retirement ends the session and the CLI in it, and keeps the session's history
    assert w.parent.kill_pane(owner) is True and w.parent.get_pane_status(owner) is None
    assert session not in sessions(w), 'kill_pane(owner) left tmux session %s running' % session
    assert W.wait_for(lambda: not w.ledger.alive('fake-cli:' + session)), 'the retired CLI still runs'
    words = out.read_text().split() if out.exists() else []
    assert '10' in words and '1500' in words, 'output_file has no tmux session history: %s' % out
    # the pane is already gone and only the registry remembers the owner: kill_pane still ends the session
    owner2, _, session2 = spawn(w, generation=1)
    assert W.wait_for(lambda: len(clients(w, session2)) == 1)
    assert W.ctl(Path(w.parent.ctl_socket), 'pane.kill', pane=owner2).get('ok'), 'server-side pane.kill failed'
    assert W.wait_for(lambda: W.record(Path(w.parent.ctl_socket), owner2) == {})
    assert session2 in sessions(w)  # the client is gone, the session is not
    assert w.parent.kill_pane(owner2) is True
    assert session2 not in sessions(w)
    assert W.wait_for(lambda: not w.ledger.alive('fake-cli:' + session2))


# ================================================================ T3 the parent's return check

@milestone(4)
def test_parent_detach_view_returns_a_live_tmux_slot_on_fleetmux(fm_tmux_world):
    w = fm_tmux_world
    owner, _, session = spawn(w)
    assert W.wait_for(lambda: len(clients(w, session)) == 1)
    detached = []
    child_stub = SimpleNamespace(_meta=lambda: {'surfaces': {'view-9': {'tmux_role': 'view'}}},
                                 detach_view=lambda sid: detached.append(sid) or True)
    f = SimpleNamespace(mgr=w.parent, cfg={'tab_prefix': ''}, label=lambda slot: slot)
    rec = {'view': 1, 'pane': owner, 'slot': 'ops-a', 'placeholder': ''}
    assert W.wait_for(lambda: subfleet.parent_detach_view(f, rec, child_stub, str(w.run)), timeout=10), \
        'parent_detach_view is False while the delegated tmux session and its CLI live'
    assert detached and detached[0] == 'view-9'  # a crashed child's client tab is disconnected first
    assert w.parent._surfaces()[owner].get('label') == 'fx-ops-a'
    assert W.record(Path(w.parent.ctl_socket), owner).get('title') == 'fx-ops-a'
    assert tmux(w, 'kill-session', '-t', '=' + session).returncode == 0
    assert W.wait_for(lambda: not w.ledger.alive('fake-cli:' + session))
    assert W.wait_for(lambda: subfleet.parent_detach_view(f, rec, child_stub, str(w.run)) is False)


# ================================================================ T4 the tmux launch keeps the cmux command shape (M39)
# M35 E2 run1 step 2: the tmux branch of ``spawn_interactive_cli`` ran only ``cli_command`` in ``new-session``: no
# ``"$(<prompt_file)"``, no ``env_vars``, no login shell after the CLI (``auto_close=False``), so ``fleet.py actor``
# exited 2 (missing ``prompt``) and the session died at once. cmux builds that inner command before ``new-session``
# (``backends/cmux.py`` ``spawn_interactive_cli``). The probe CLI records what it really got, inside tmux.

PROBE = r'''#!/bin/sh
d="$1"
printf '%s' "$$" > "$d/pid"
printf '%s' "$#" > "$d/argc"
printf '%s' "$2" > "$d/arg2"
printf '%s' "${FM4_PROBE_A-<unset>}" > "$d/env_a"
printf '%s' "${FM4_PROBE_B-<unset>}" > "$d/env_b"
printf '%s' "${HOME-<unset>}" > "$d/home"
: > "$d/done"
i=0
while [ ! -e "$d/release" ] && [ "$i" -lt 600 ]; do sleep 0.1; i=$((i + 1)); done
'''
import os  # noqa: E402

PROMPT = 'Read  the brief first.\nline two: $HOME "dq" \'sq\' `tick` ; exit 3 | x  -- end'
ENV = {'FM4_PROBE_A': 'value with  spaces', 'FM4_PROBE_B': 'b=2;c'}


def probe_cli(w):
    probe = w.root / 'cli' / 'probe'
    if not probe.exists():
        probe.write_text(PROBE)
        probe.chmod(0o755)
    return probe


def spawn_probe(w, session, auto_close, prompt=None, env=None):
    import shlex
    rec = w.run / ('rec-' + session)
    rec.mkdir()
    prompt_file = None
    if prompt is not None:
        prompt_file = w.run / 'prompts' / (session + '.prompt.md')
        prompt_file.write_text(prompt)
    out = w.run / 'prompts' / ('%s.terminal.log' % session)
    res = w.parent.spawn_interactive_cli('fx-' + session, shlex.join([str(probe_cli(w)), str(rec)]),
                                         prompt_file=str(prompt_file) if prompt_file else None, output_file=str(out),
                                         start_directory=str(w.run), auto_close=auto_close, env_vars=env,
                                         tmux_socket=w.sock, tmux_session=session)
    assert res.success, (res.error, w.parent.last_error)
    for mgr in (w.parent, w.child):
        w.ledger.register('fleetmux-server:' + mgr.session_name, mgr._server_pid())
    w.ledger.register('tmux-server', tmux(w, 'display-message', '-p', '#{pid}').stdout.strip())
    W.note(w.root, 'probe %s auto_close=%s prompt=%s env=%s' % (session, auto_close, bool(prompt), sorted(env or {})))
    assert W.wait_for(lambda: (rec / 'done').exists(), timeout=15), \
        'the CLI in tmux session %s never ran (no %s/done)' % (session, rec)
    return res.pane_id, rec


def got(rec, name):
    return (rec / name).read_text()


def session_args(w, session):
    """argv strings of every process under the session's tmux pane (pane pid down)."""
    done = tmux(w, 'list-panes', '-s', '-t', '=' + session, '-F', '#{pane_pid}')
    roots = {int(p) for p in done.stdout.split() if p.isdigit()}
    table = subprocess.run(['ps', '-axww', '-o', 'pid=,ppid=,args='], capture_output=True, text=True).stdout
    rows = [line.split(None, 2) for line in table.splitlines()]
    rows = [(int(r[0]), int(r[1]), r[2]) for r in rows if len(r) == 3 and r[0].isdigit() and r[1].isdigit()]
    tree, grew = set(roots), True
    while grew:
        grew = False
        for pid, ppid, _ in rows:
            if ppid in tree and pid not in tree:
                tree.add(pid)
                grew = True
    return [args for pid, _, args in rows if pid in tree]


def pid_alive(pid):
    import os
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@milestone(4)
def test_fleetmux_tmux_slot_cli_gets_prompt_env_and_keeps_a_login_shell(fm_tmux_world):
    w = fm_tmux_world
    keep = 'ops-a-g0'
    owner, rec = spawn_probe(w, keep, auto_close=False, prompt=PROMPT, env=ENV)
    # the prompt file's content is one argument (spaces / newline / shell characters are not split or run)
    assert got(rec, 'argc') == '2', 'CLI argc %s in tmux (no prompt argument?)' % got(rec, 'argc')
    assert got(rec, 'arg2') == PROMPT
    assert got(rec, 'env_a') == ENV['FM4_PROBE_A'] and got(rec, 'env_b') == ENV['FM4_PROBE_B'], \
        'env_vars did not reach the CLI inside tmux: %r %r' % (got(rec, 'env_a'), got(rec, 'env_b'))
    # HOME is not an env_vars key: the CLI gets the fleet's HOME through the tmux server it started
    assert got(rec, 'home') == os.environ['HOME'] == str(w.root / 'h'), got(rec, 'home')
    # auto_close=False: once the CLI exits the session lives on with a login shell
    cli = got(rec, 'pid')
    (rec / 'release').touch()
    assert W.wait_for(lambda: not pid_alive(cli), timeout=15), 'probe CLI did not exit'
    assert W.wait_for(lambda: '/bin/sh -l' in session_args(w, keep), timeout=10), \
        'no login shell after the CLI in %s: %s' % (keep, session_args(w, keep))
    assert keep in sessions(w) and len(clients(w, keep)) == 1
    assert w.parent.get_pane_status(owner) is not None
    # auto_close=True (same prompt / env shape): the CLI's exit ends its session
    gone = 'ops-b-g0'
    _, rec2 = spawn_probe(w, gone, auto_close=True, prompt=PROMPT + '\nsecond', env=ENV)
    assert got(rec2, 'argc') == '2' and got(rec2, 'arg2') == PROMPT + '\nsecond'
    assert got(rec2, 'env_a') == ENV['FM4_PROBE_A']
    (rec2 / 'release').touch()
    assert W.wait_for(lambda: gone not in sessions(w), timeout=15), 'auto_close=True left session ' + gone
    assert keep in sessions(w)


@milestone(4)
def test_fleetmux_tmux_slot_without_prompt_or_env_runs_the_bare_command(fm_tmux_world):
    w = fm_tmux_world
    keep = 'ops-c-g0'
    _, rec = spawn_probe(w, keep, auto_close=False)
    assert got(rec, 'argc') == '1'  # no prompt argument is invented
    assert got(rec, 'env_a') == '<unset>' and got(rec, 'env_b') == '<unset>'
    assert got(rec, 'home') == os.environ['HOME'] == str(w.root / 'h'), got(rec, 'home')
    cli = got(rec, 'pid')
    (rec / 'release').touch()
    assert W.wait_for(lambda: not pid_alive(cli), timeout=15)
    assert W.wait_for(lambda: '/bin/sh -l' in session_args(w, keep), timeout=10), session_args(w, keep)
    assert keep in sessions(w)
    gone = 'ops-d-g0'
    _, rec2 = spawn_probe(w, gone, auto_close=True)
    assert got(rec2, 'argc') == '1' and got(rec2, 'env_a') == '<unset>'
    (rec2 / 'release').touch()
    assert W.wait_for(lambda: gone not in sessions(w), timeout=15), 'auto_close=True left session ' + gone
    assert keep in sessions(w)
