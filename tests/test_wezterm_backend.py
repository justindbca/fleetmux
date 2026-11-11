"""WezTerm backend against a fake ``wezterm`` CLI (design §7.2): every §4.2 mapping and §4.3 degradation.

No WezTerm needed. ``tests/fake_wezterm.py`` is installed as ``wezterm`` (and as
``wezterm-mux-server``) on a PATH the backend is pointed at; it records argv and keeps the
window / tab / pane tables, so a test can assert both what was asked and what came out of it.

The private mux server itself is faked too: a file standing in for the socket plus the pid the
fake "server" process writes, which is how ``close`` knows to stop only that pid.
"""
import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fleetmux.backends import base  # noqa: E402
from fleetmux.backends import wezterm as wz  # noqa: E402

FAKE = Path(__file__).parent / 'fake_wezterm.py'


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    """A PATH holding ``wezterm`` + ``wezterm-mux-server`` fakes, and a private state dir."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name in ('wezterm', 'wezterm-mux-server'):
        path = bin_dir / name
        path.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE))
        path.chmod(0o755)
    state = tmp_path / 'cli'
    state.mkdir()
    monkeypatch.setenv('FAKE_WZ_STATE', str(state))
    monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ.get('PATH', ''))
    monkeypatch.delenv('WEZTERM_UNIX_SOCKET', raising=False)
    # pytest's per-test tmp path plus ``/wezterm/<token>/mux.sock`` can exceed macOS sockaddr_un; the
    # backend refuses that (no /tmp fallback, design §4.1), so the fake runtime dir stays short.
    short = Path(tempfile.mkdtemp(prefix='wz-')).resolve()
    return {'bin': bin_dir, 'state': state, 'root': short / 'rt', 'short': short}


def argv_log(fake_cli):
    log = fake_cli['state'] / 'argv.log'
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def cli_state(fake_cli):
    return json.loads((fake_cli['state'] / 'state.json').read_text())


def panes(fake_cli):
    return [p['pane_id'] for p in cli_state(fake_cli)['panes'].values()]


def pane_rows(fake_cli):
    """{pane_id: row} of the fake server's view, with the tab's own fields merged in (as ``list`` shows)."""
    state = cli_state(fake_cli)
    out = {}
    for pane in state['panes'].values():
        tab = state['tabs'].get(str(pane['tab_id']), {})
        row = dict(pane)
        row['workspace'] = tab.get('workspace', '')
        row['tab_title'] = tab.get('title', '')
        out[pane['pane_id']] = row
    return out


@pytest.fixture
def mgr(fake_cli, monkeypatch):
    """A started session manager on the fake CLI: a fake mux server then ``get_or_create_session``."""
    def fake_server(self):
        return _fake_server(fake_cli, self)

    monkeypatch.setattr(wz.WezTermSessionManager, '_spawn_mux_server', fake_server)
    m = wz.WezTermSessionManager('wz-test', state_dir=fake_cli['root'])
    assert m.get_or_create_session() is True
    return m


def _fake_server(fake_cli, m):
    """Stand-in for the server launch: write what the real (non-daemonized) server leaves behind."""
    m.prepare_runtime()
    socket = Path(m.mux_socket)
    socket.parent.mkdir(parents=True, exist_ok=True)
    socket.write_text('fake-mux-socket')
    Path(m.mux_pid_file).write_text(str(os.getpid()))
    return os.getpid()


# -- §4.4 availability ---------------------------------------------------------------------------------
def test_available_needs_both_binaries(fake_cli, monkeypatch):
    monkeypatch.setattr(wz, 'WEZTERM_BIN', None)
    monkeypatch.setattr(wz, 'MUX_BIN', None)
    monkeypatch.setenv('PATH', str(fake_cli['bin']))  # only our fakes: no /opt/homebrew/wezterm
    assert wz.WezTermSessionManager.available() is True
    (fake_cli['bin'] / 'wezterm-mux-server').unlink()
    monkeypatch.setattr(wz, 'MUX_BIN', None)
    assert wz.WezTermSessionManager.available() is False
    assert 'wezterm-mux-server' in (wz.availability_problem() or '')


def test_available_rejects_a_too_old_wezterm(fake_cli, monkeypatch):
    monkeypatch.setattr(wz, 'WEZTERM_BIN', None)
    monkeypatch.setattr(wz, 'MUX_BIN', None)
    monkeypatch.setenv('FAKE_WZ_VERSION', 'wezterm 20230712-072601-f4abf8fd')
    monkeypatch.setenv('PATH', str(fake_cli['bin']))
    assert wz.WezTermSessionManager.available() is False
    assert '20240203' in (wz.availability_problem() or '')


def test_available_parses_the_dependency_floor(fake_cli, monkeypatch):
    monkeypatch.setattr(wz, 'WEZTERM_BIN', None)
    monkeypatch.setattr(wz, 'MUX_BIN', None)
    monkeypatch.setenv('PATH', str(fake_cli['bin']))
    monkeypatch.setenv('FAKE_WZ_VERSION', 'wezterm 20240203-110809-5046fc22')
    assert wz.WezTermSessionManager.available() is True


def test_default_backend_uses_wezterm_when_cmux_is_missing(monkeypatch):
    monkeypatch.setattr(base, 'cmux_installed', lambda: False)
    monkeypatch.setattr(base, 'unavailable_reason', lambda name: 'no cmux' if name == 'cmux' else None)
    assert base.default_backend('darwin') == 'wezterm'  # a Mac without cmux
    assert base.default_backend('linux') == 'fleetmux'  # design §5: never cmux off macOS (M3: fleetmux)


# -- §4.1 private mux server ----------------------------------------------------------------------------
def test_private_server_is_started_without_daemonize_and_never_overrides_home(fake_cli, monkeypatch):
    """M2 second correction: ``--daemonize`` is what takes ``~/.local/share/wezterm/pid`` off the operator,
    so we do not daemonize at all. The server is a plain ``Popen`` in a new session, its pid is recorded
    by us, and no call ever overrides ``HOME`` (the fake HOME is gone)."""
    seen = {}

    m = wz.WezTermSessionManager('wz-home', state_dir=fake_cli['root'])
    m.prepare_runtime()

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen['cmd'] = cmd
            seen['kwargs'] = kw
            self.pid = 424242
            self.returncode = None

        def poll(self):
            return self.returncode  # still alive

    def fake_popen(cmd, **kw):
        seen.setdefault('popen', []).append(cmd)
        proc = FakePopen(cmd, **kw)
        Path(m.mux_socket).write_text('fake-mux-socket')  # what the real server leaves behind
        return proc

    monkeypatch.setattr(wz.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(wz, '_pid_alive', lambda pid: True)
    m._start_mux_server()

    cmd = seen['cmd']
    assert cmd[0] == wz._mux() and str(cmd[0]).endswith('wezterm-mux-server')
    assert '--config-file' in cmd
    conf = Path(cmd[cmd.index('--config-file') + 1]).read_text()
    assert 'fleet-wz-home' in conf and m.mux_socket in conf
    assert '--daemonize' not in cmd, 'daemonize is exactly what steals the operator pid lock'
    assert '--pid-file-fd' not in ' '.join(cmd)

    # A plain child in its own session: no controlling tty, no stdin, output to a run-owned log.
    kw = seen['kwargs']
    assert kw['start_new_session'] is True
    assert kw['stdin'] == wz.subprocess.DEVNULL
    for key in ('stdout', 'stderr'):
        assert kw[key] != wz.subprocess.PIPE and hasattr(kw[key], 'write'), key + ' goes to a file in the run dir'

    # The pid we stop is the one Popen gave us, recorded in our own pid file inside the run directory.
    assert m.mux_pid == 424242
    assert Path(m.mux_pid_file).read_text().strip() == '424242'
    assert str(Path(m.mux_pid_file).parent) == m.runtime_root

    # HOME is never rewritten, on any call: not here, not in the client env.
    assert kw['env']['HOME'] == os.environ['HOME']
    assert m.cli_env()['HOME'] == os.environ['HOME']
    assert not hasattr(m, 'mux_home'), 'the fake HOME concept is gone'
    assert not (Path(m.runtime_root) / 'home').exists(), 'no fake HOME directory is created'


def test_started_server_log_lands_in_the_run_directory(fake_cli, monkeypatch):
    """stdout/stderr of the server go to a log file under the run dir, not to the daemon's terminal."""
    seen = {}

    m = wz.WezTermSessionManager('wz-log', state_dir=fake_cli['root'])
    m.prepare_runtime()

    class FakePopen:
        pid = 434343
        returncode = None

        def poll(self):
            return None

    def fake_popen(cmd, **kw):
        seen['out'] = kw['stdout']
        seen['out_name'] = getattr(kw['stdout'], 'name', None)
        Path(m.mux_socket).write_text('fake-mux-socket')
        return FakePopen()

    monkeypatch.setattr(wz.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(wz, '_pid_alive', lambda pid: True)
    m._start_mux_server()
    assert str(seen['out_name']).startswith(m.runtime_root), 'the log must stay inside our run directory'
    assert seen['out'].closed


def test_cli_env_only_sets_the_private_socket(fake_cli):
    """The client env is the inherited one plus our socket — nothing else, and no HOME rewrite."""
    m = wz.WezTermSessionManager('wz-env', state_dir=fake_cli['root'])
    m.prepare_runtime()
    env = m.cli_env()
    assert env['WEZTERM_UNIX_SOCKET'] == m.mux_socket
    assert env['HOME'] == os.environ['HOME'], 'the operator HOME is inherited, never overridden'
    assert 'WEZTERM_PANE' not in env
    if 'WEZTERM_CONFIG_FILE' in os.environ:
        assert 'WEZTERM_CONFIG_FILE' not in env


def test_spawn_does_not_pin_home_in_the_launcher(mgr, fake_cli):
    """No launcher-level ``export HOME=`` either: the pane simply inherits the fleet's real environment."""
    assert mgr.spawn_window('worker-1', 'echo hi', start_directory=str(fake_cli['root'])).success
    launch = list((Path(mgr.meta_path).parent / 'launch').glob('worker-1-*.sh'))
    assert launch, 'a launcher script is written per spawn'
    body = launch[-1].read_text()
    assert 'export HOME=' not in body
    for call in (c for c in argv_log(fake_cli) if 'list' in c['argv'] or 'spawn' in c['argv']):
        assert call['home'] == os.environ['HOME'], 'every CLI call runs with the inherited HOME'


def test_socket_path_over_the_limit_fails_without_fallback(tmp_path):
    deep = tmp_path / ('x' * 60) / ('y' * 60) / ('z' * 60)
    m = wz.WezTermSessionManager('wz-long', state_dir=deep)
    with pytest.raises(wz.WezTermError) as exc:
        m.prepare_runtime()
    assert 'too long' in str(exc.value)


def test_every_cli_call_is_pinned_to_the_private_socket_and_no_auto_start(mgr, fake_cli):
    mgr.spawn_window('worker-1', 'echo hi', start_directory=str(fake_cli['root']))
    calls = [c for c in argv_log(fake_cli) if 'list' in c['argv'] or 'spawn' in c['argv']]
    assert calls
    for call in calls:
        assert call['socket'] == mgr.mux_socket, 'a call without WEZTERM_UNIX_SOCKET could reach the user\'s server'
        assert '--no-auto-start' in call['argv']


def test_close_kills_only_the_recorded_pid(mgr, fake_cli, monkeypatch):
    killed = []
    recorded = int(Path(mgr.mux_pid_file).read_text())
    mgr.mux_pid = recorded  # as _start_mux_server records it
    monkeypatch.setattr(wz, '_pid_alive', lambda pid: pid == recorded)
    monkeypatch.setattr(wz.os, 'kill', lambda pid, sig: killed.append((pid, sig)))
    assert mgr.close_all() is True
    # SIGTERM first, then SIGKILL only because this stub never lets the pid die.
    assert killed[0] == (recorded, wz.signal.SIGTERM)
    assert {pid for pid, _ in killed} == {recorded}, 'no pid but the recorded one may be signalled'
    assert mgr.mux_pid is None


def test_close_never_kills_a_pid_it_did_not_start(mgr, monkeypatch):
    killed = []
    monkeypatch.setattr(wz, '_pid_alive', lambda pid: True)
    monkeypatch.setattr(wz.os, 'kill', lambda pid, sig: killed.append(pid))
    Path(mgr.mux_pid_file).write_text('999999')  # somebody else's pid
    mgr.mux_pid = None
    mgr.end_mux_server()
    assert 999999 not in killed


# -- §4.2 each mapping row ------------------------------------------------------------------------------
def test_spawn_window_opens_a_window_in_the_run_workspace(mgr, fake_cli):
    res = mgr.spawn_window('supervisor', 'echo start', metadata={'type': 'supervisor'},
                           start_directory='/tmp')
    assert res.success and res.pane_id
    call = next(c for c in argv_log(fake_cli) if c['argv'][:3] == ['cli', 'spawn', '--new-window']
                or 'spawn' in c['argv'])
    assert call['argv'].count('--new-window') == 1
    assert mgr.mux_workspace in call['argv']
    row = pane_rows(fake_cli)[int(res.pane_id)]
    assert row['workspace'] == mgr.mux_workspace
    assert mgr.owns(res.pane_id) is True


def test_spawn_runs_the_command_through_a_launcher_script(mgr, fake_cli):
    """``wezterm cli spawn -- cmd`` runs cmd at once (unlike cmux's typed --command): no shell race."""
    res = mgr.spawn_window('worker-2', 'exec /bin/echo hi', start_directory='/tmp')
    assert res.success
    call = next(c for c in argv_log(fake_cli) if 'spawn' in c['argv'])
    # ``bash -lc "<launcher>"`` where the launcher script carries cd + the caller's command.
    assert call['argv'][-3:-1] == ['bash', '-lc']
    launcher = Path(call['argv'][-1].split('/bin/bash ', 1)[1].strip("'"))
    assert launcher.exists() and 'echo hi' in launcher.read_text()
    assert 'cd /tmp' in launcher.read_text()


def test_same_window_new_tab_uses_pane_id(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    wk = mgr.spawn_window('worker-1', 'sleep 60', start_directory='/tmp')
    assert sup.success and wk.success
    call = [c for c in argv_log(fake_cli) if 'spawn' in c['argv']][-1]
    assert '--pane-id' in call['argv']
    assert '--new-window' not in call['argv']
    rows = pane_rows(fake_cli)
    assert rows[int(sup.pane_id)]['window_id'] == rows[int(wk.pane_id)]['window_id']
    assert rows[int(sup.pane_id)]['tab_id'] != rows[int(wk.pane_id)]['tab_id']


def test_split_pane_right_and_bottom(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    peer = mgr.spawn_window('worker-1', 'sleep 60', start_directory='/tmp')
    assert mgr.place_right_of(peer.pane_id, sup.pane_id) is True
    rows = pane_rows(fake_cli)
    assert rows[int(peer.pane_id)]['left_col'] > rows[int(sup.pane_id)]['left_col']
    call = [c for c in argv_log(fake_cli) if 'split-pane' in c['argv']][-1]
    assert '--right' in call['argv'] and '--percent' in call['argv']
    # already side by side: no second split
    n = sum('split-pane' in c['argv'] for c in argv_log(fake_cli))
    assert mgr.place_right_of(peer.pane_id, sup.pane_id) is True
    assert sum('split-pane' in c['argv'] for c in argv_log(fake_cli)) == n


def test_rename_sets_the_tab_title(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    mgr.rename(sup.pane_id, 'supervisor-1')
    rows = pane_rows(fake_cli)
    assert rows[int(sup.pane_id)]['tab_title'] == 'supervisor-1'
    call = [c for c in argv_log(fake_cli) if 'set-tab-title' in c['argv']][-1]
    assert call['argv'][-1] == 'supervisor-1'
    assert mgr.get_pane_status(sup.pane_id).window_name == 'supervisor-1'


def test_list_gives_liveness_tty_and_cwd(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    status = mgr.get_pane_status(sup.pane_id)
    assert status.alive is True
    assert status.metadata['tty_name'].startswith('/dev/faketty')
    assert mgr.owns(sup.pane_id) and not mgr.owns('424242')


def test_get_text_reads_the_pane(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    mgr.send_prompt_to_idle(sup.pane_id, 'hello screen')
    assert 'hello screen' in mgr.capture_output(sup.pane_id)
    call = [c for c in argv_log(fake_cli) if 'get-text' in c['argv']][-1]
    assert '--start-line' in call['argv']
    out = fake_cli['root'] / 'cap.txt'
    mgr.capture_to(sup.pane_id, str(out))
    assert 'hello screen' in out.read_text()


def test_send_text_without_brackets_is_raw_and_enter_is_cr(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    assert mgr.send_prompt_to_idle(sup.pane_id, 'plain text', bracketed=False) is True
    sends = [c['argv'] for c in argv_log(fake_cli) if 'send-text' in c['argv']]
    assert '--no-paste' in sends[0]
    assert 'plain text' in sends[0]
    assert '--no-paste' in sends[-1] and sends[-1][-2:] == ['--', '\r'], 'Enter must be its own CR'


def test_send_text_starting_with_a_dash_is_not_an_option(mgr, fake_cli):
    """Review 2026-10-28: ``- fix X`` was parsed by clap as an option (rc 2); the text follows ``--``."""
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    assert mgr.send_prompt_to_idle(sup.pane_id, '- fix X', bracketed=False) is True
    first = [c['argv'] for c in argv_log(fake_cli) if 'send-text' in c['argv']][0]
    assert first[-2:] == ['--', '- fix X']
    assert cli_state(fake_cli)['buffer'][sup.pane_id].startswith('- fix X')


def test_send_text_bracketed_wraps_the_paste(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    assert mgr.send_prompt_to_idle(sup.pane_id, 'line1\nline2', bracketed=True) is True
    sends = [c['argv'] for c in argv_log(fake_cli) if 'send-text' in c['argv']]
    assert '--no-paste' not in sends[0]
    assert cli_state(fake_cli)['buffer'][sup.pane_id].startswith('\x1b[200~line1\nline2\x1b[201~')


@pytest.mark.parametrize('key,expected', [
    ('Escape', '\x1b'), ('Esc', '\x1b'), ('C-c', '\x03'), ('Ctrl-C', '\x03'),
    ('Up', '\x1b[A'), ('Down', '\x1b[B'), ('Left', '\x1b[D'), ('Right', '\x1b[C'),
    ('Enter', '\r'), ('Tab', '\t'), ('BSpace', '\x7f'), ('Space', ' '),
])
def test_send_key_maps_the_names_to_bytes(mgr, fake_cli, key, expected):
    """Measured: send-text --no-paste delivers 0x1b / 0x03 / CSI sequences byte-exact (probe-report §2)."""
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    assert mgr.send_key(sup.pane_id, key) is True
    call = [c for c in argv_log(fake_cli) if 'send-text' in c['argv']][-1]['argv']
    assert '--no-paste' in call
    assert call[-1] == expected
    assert cli_state(fake_cli)['buffer'][sup.pane_id].endswith(reprable(expected))


def reprable(byte):
    return byte


def test_send_key_returns_false_for_an_unknown_key(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    before = len(argv_log(fake_cli))
    assert mgr.send_key(sup.pane_id, 'NoSuchKey') is False
    assert len(argv_log(fake_cli)) == before, 'an unknown key must not be typed as literal text'


def test_kill_pane_removes_it_and_forgets_ownership(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    wk = mgr.spawn_window('worker-1', 'sleep 60', start_directory='/tmp')
    assert mgr.kill_pane(wk.pane_id) is True
    assert wk.pane_id not in [str(p) for p in panes(fake_cli)]
    assert mgr.owns(wk.pane_id) is False
    assert mgr.owns(sup.pane_id) is True


def test_kill_pane_refuses_a_pane_it_does_not_own(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    _ = sup
    before = len(argv_log(fake_cli))
    assert mgr.kill_pane('31337') is False
    assert not [c for c in argv_log(fake_cli)[before:] if 'kill-pane' in c['argv']]


def test_focus_activates_the_pane(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    wk = mgr.spawn_window('worker-1', 'sleep 60', start_directory='/tmp')
    assert mgr.focus(sup.pane_id) is True
    rows = pane_rows(fake_cli)
    assert rows[int(sup.pane_id)]['is_active'] is True and rows[int(wk.pane_id)]['is_active'] is False


def test_process_state_queries_use_the_pane_tty(mgr, fake_cli, monkeypatch):
    """No cmux process backend here: tty_name -> ``ps -t`` (design §4.2 last row)."""
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    tty = mgr.get_pane_status(sup.pane_id).metadata['tty_name']
    calls = []

    real_run = wz.subprocess.run

    def run(cmd, **kw):
        from types import SimpleNamespace
        if cmd[0] != 'ps':  # only the process query is stubbed; `wezterm cli` still hits the fake CLI
            return real_run(cmd, **kw)
        calls.append(cmd)
        if '-o' in cmd and 'comm=' in cmd:
            return SimpleNamespace(returncode=0, stdout='/bin/sleep\n/usr/local/bin/codex\n', stderr='')
        if '-o' in cmd and 'stat=' in cmd:
            return SimpleNamespace(returncode=0, stdout='Ss+\nS\n', stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(wz.subprocess, 'run', run)
    assert mgr.has_cli_process(sup.pane_id, ('codex',)) is True
    assert mgr.process_states(sup.pane_id) == ['Ss+', 'S']
    assert all(tty.replace('/dev/', '') in ' '.join(c) for c in calls)


# -- §4.3 layout degradation ----------------------------------------------------------------------------
def test_capability_flags_degrade():
    assert wz.WezTermSessionManager.supports_browser is False
    assert wz.WezTermSessionManager.nested_tabs is False


def test_browser_calls_return_false(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    assert mgr.browser_goto(sup.pane_id, 'file:///h.html') is False
    assert mgr.browser_url(sup.pane_id) is None
    assert mgr.reload_browser(sup.pane_id) is False


def test_arrange_puts_workers_in_their_own_tabs(mgr, fake_cli):
    """One window: tab 1 = supervisor + control (split), one tab per worker (design §4.3)."""
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    w1 = mgr.spawn_window('worker-1', 'sleep 60', start_directory='/tmp')
    w2 = mgr.spawn_window('worker-2', 'sleep 60', start_directory='/tmp')
    ctl = mgr.spawn_window('control', 'sleep 60', metadata={'type': 'daemon'}, start_directory='/tmp')
    assert mgr.arrange(sup.pane_id, [w1.pane_id, w2.pane_id], 'file:///history.html') is None
    rows = pane_rows(fake_cli)
    sup_row = rows[int(sup.pane_id)]
    assert rows[int(w1.pane_id)]['tab_id'] != sup_row['tab_id']
    assert rows[int(w2.pane_id)]['tab_id'] != sup_row['tab_id']
    assert rows[int(w1.pane_id)]['tab_id'] != rows[int(w2.pane_id)]['tab_id']
    assert rows[int(ctl.pane_id)]['tab_id'] == sup_row['tab_id']
    assert rows[int(ctl.pane_id)]['top_row'] > sup_row['top_row'], 'control sits below the supervisor'


def test_arrange_fails_when_a_worker_is_unknown(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    assert mgr.arrange(sup.pane_id, ['does-not-exist'], 'file:///h.html') is None


def test_tab_into_degrades_to_the_target_tab(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    wk = mgr.spawn_window('worker-1', 'sleep 60', start_directory='/tmp')
    assert mgr.tab_into(wk.pane_id, sup.pane_id) is True
    rows = pane_rows(fake_cli)
    assert rows[int(wk.pane_id)]['tab_id'] == rows[int(sup.pane_id)]['tab_id']


def test_dock_calls_are_no_ops_that_succeed(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    wk = mgr.spawn_window('worker-1', 'sleep 60', start_directory='/tmp')
    assert mgr.dock_tab(wk.pane_id, sup.pane_id) is True
    assert mgr.dock_control(sup.pane_id) is True
    assert mgr.place_after(sup.pane_id) is True


def test_place_after_and_select_workspace_are_benign(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    assert mgr.select_workspace(mgr.mux_workspace) is True
    assert mgr.place_after(mgr.mux_workspace) is True
    assert mgr.workspace_id() == mgr.mux_workspace
    assert mgr.owns(sup.pane_id)


def test_session_exists_and_close_all(mgr, fake_cli):
    sup = mgr.spawn_window('supervisor', 'sleep 60', start_directory='/tmp')
    assert mgr.session_exists() is True
    assert mgr.close_all() is True
    assert mgr.owns(sup.pane_id) is False
    assert panes(fake_cli) == []


# -- failure paths ---------------------------------------------------------------------------------------
def test_spawn_failure_is_reported_not_swallowed(fake_cli, monkeypatch):
    def fake_server(self):
        return _fake_server(fake_cli, self)

    monkeypatch.setattr(wz.WezTermSessionManager, '_spawn_mux_server', fake_server)
    monkeypatch.setenv('FAKE_WZ_FAIL', 'spawn')
    m = wz.WezTermSessionManager('wz-fail', state_dir=fake_cli['root'])
    assert m.get_or_create_session() is True
    res = m.spawn_window('worker-1', 'sleep 60', start_directory='/tmp')
    assert res.success is False and res.error


def test_no_session_means_no_cli_calls(mgr, fake_cli, monkeypatch):
    monkeypatch.setenv('FAKE_WZ_FAIL', 'list')
    assert mgr.get_pane_status('1') is None
    monkeypatch.undo()


def test_runtime_dir_is_owner_only_under_a_group_writable_umask():
    """wezterm-mux-server refuses a runtime dir other users can write to; Ubuntu's default umask (002)
    gives a plain mkdir 0775, so the dir must be made 0700 explicitly — also when it already exists."""
    root = Path(tempfile.mkdtemp(prefix='wzperm', dir='/tmp'))
    old = os.umask(0o002)
    try:
        info = wz.WezTermSessionManager('perm', state_dir=root / 's').prepare_runtime()
        assert Path(info['root']).stat().st_mode & 0o777 == 0o700
        Path(info['root']).chmod(0o775)  # left behind by an earlier run with the old code
        again = wz.WezTermSessionManager('perm', state_dir=root / 's').prepare_runtime()
        assert Path(again['root']).stat().st_mode & 0o777 == 0o700
    finally:
        os.umask(old)
        shutil.rmtree(root, ignore_errors=True)
