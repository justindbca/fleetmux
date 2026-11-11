"""fleetmux backend behaviour (fleetmux plan §3.5, design §2–§4): one suite, two servers.

Every test runs twice through the ``impl`` fixture: ``fake`` = tests/fake_fleetmux.py (the Python
protocol fake, the reference reading of PROTOCOL.md) and ``real`` = the Rust binary
(``$FLEETMUX_REAL_BIN``, default ``rust/target/debug/fleetmux``; skipped when it is not built).
A case the real binary fails: first check the fake's reading of the protocol.

The manager is driven through its public interface only; what the server *really* did is read back
straight from the run's control socket (:func:`ctl`, rust/PROTOCOL.md v1: the top-level ``id`` is the
request number, a pane is always ``pane``), so an assertion never trusts the manager's own account.
Phase 0: every manager method raises ``NotImplementedError``, so these tests are red until M0.

Socket roots are short ``mkdtemp`` dirs under /tmp: macOS limits ``sockaddr_un`` to 103 bytes.
"""
import base64
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleetmux.backends import base  # noqa: E402
from fleetmux.backends import fleetmux as fm  # noqa: E402
from fleetmux.backends.base import PaneStatus  # noqa: E402

FAKE = Path(__file__).with_name('fake_fleetmux.py')  # py-fake's protocol fake (plan §3.5)
REAL = Path(os.environ.get('FLEETMUX_REAL_BIN') or ROOT / 'rust/target/debug/fleetmux')
SESSION = 'fm-test'
PANE_ID = re.compile(r'[0-9a-z]{4}-[1-9][0-9]*\Z')  # <epoch>-<seq>, PROTOCOL.md §1.4
GROUPS = ('supervisor', 'lower', 'workers')
GOOD_VERSION = 'fleetmux 0.2.0 (protocol %d)' % fm.PROTOCOL


# ---------------------------------------------------------------- fixture: fake and real server

def _launcher(path: Path, argv) -> Path:
    """A ``fleetmux`` on PATH / in $FLEETMUX_BIN that runs ``argv`` (the fake is a .py file)."""
    path.write_text('#!/bin/sh\nexec ' + shlex.join([str(a) for a in argv]) + ' "$@"\n')
    path.chmod(0o755)
    return path


@pytest.fixture(params=['fake', 'real'])
def impl(request, monkeypatch):
    root = Path(tempfile.mkdtemp(prefix='fm', dir='/tmp')).resolve()
    if request.param == 'real':
        if not (REAL.is_file() and os.access(REAL, os.X_OK)):
            shutil.rmtree(root, ignore_errors=True)
            pytest.skip('real fleetmux not built: ' + str(REAL) + ' (cargo build, or set FLEETMUX_REAL_BIN)')
        target = [REAL]
    else:
        target = [sys.executable, FAKE]
    bindir = root / 'bin'
    bindir.mkdir()
    binary = _launcher(bindir / 'fleetmux', target)
    monkeypatch.setenv(fm.BIN_ENV, str(binary))
    monkeypatch.setenv('PATH', str(bindir) + os.pathsep + os.environ.get('PATH', ''))
    state = root / 'state'
    state.mkdir()
    env = SimpleNamespace(kind=request.param, root=root, bindir=bindir, binary=binary, state=state)
    yield env
    _teardown(env)


def new_mgr(env, session=SESSION):
    """A fresh manager on the test's state dir. Built in the test body, not in a fixture: constructing it
    is backend behaviour too (a skeleton constructor fails the test, it does not error its setup)."""
    return fm.FleetmuxSessionManager(session, state_dir=env.state)


def _teardown(env):
    """Stop whatever the test left running under its own root, never anything else."""
    runtimes = env.state / 'fleetmux'
    for rt in sorted(runtimes.glob('*')) if runtimes.is_dir() else []:
        try:
            _send(rt / 'ctl.sock', {'id': 1, 'cmd': 'server.shutdown', 'force': True}, timeout=5)
        except Exception:
            pass
        for pgid in set(_walk(_read_json(rt / 'panes.json'), 'pgid')):
            try:
                if int(pgid) > 1 and int(pgid) != os.getpgrp():
                    os.killpg(int(pgid), signal.SIGKILL)
            except (OSError, ValueError, TypeError):
                pass
        pid = _read_json(rt / 'server.json').get('pid')
        try:
            if pid and alive(int(pid)) and str(rt) in ps_args(int(pid)):
                os.kill(int(pid), signal.SIGKILL)
        except (OSError, ValueError):
            pass
    for sock in (env.root / 'tmux').glob('*.sock') if (env.root / 'tmux').is_dir() else []:
        subprocess.run(['tmux', '-S', str(sock), 'kill-server'], capture_output=True, timeout=10)
    shutil.rmtree(env.root, ignore_errors=True)


# ---------------------------------------------------------------- helpers: what the server says

def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def _walk(obj, key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            else:
                yield from _walk(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, key)


def token(session: str) -> str:
    """Cross-language path ruling (same as Rust ``paths::token``): every character outside ASCII
    ``[A-Za-z0-9_-]`` becomes ``_`` (an explicit ASCII class -- never ``str.isalnum``), cut to 40; '' -> 'fleet'."""
    return re.sub(r'[^A-Za-z0-9_-]', '_', session, flags=re.ASCII)[:40] or 'fleet'


def runtime_of(env, session=SESSION) -> Path:
    return env.state / 'fleetmux' / token(session)


def _send(sock_path: Path, request: dict, timeout: float = 10) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(sock_path))
        sock.sendall((json.dumps(request) + '\n').encode())
        buf = b''
        while not buf.endswith(b'\n'):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf)


def ctl(env, cmd, session=SESSION, **args) -> dict:
    """One request straight on the run's control socket (PROTOCOL.md), bypassing the manager."""
    return _send(runtime_of(env, session) / 'ctl.sock', {'id': 1, 'cmd': cmd, **args})


def server_up(env, session=SESSION) -> bool:
    try:
        return ctl(env, 'hello', session).get('ok') is True
    except (OSError, ValueError):
        return False


def panes(env, session=SESSION) -> list:
    reply = ctl(env, 'pane.list', session)
    assert reply['ok'], reply
    return reply['panes']


def pane(env, pane_id, session=SESSION):
    """The pane record (PROTOCOL.md §4.0; ``pane.get`` puts its keys at the top level), None when gone."""
    reply = ctl(env, 'pane.get', session, pane=pane_id)
    if not reply.get('ok'):
        assert reply.get('code') == 'no_such_pane', reply
        return None
    return {k: v for k, v in reply.items() if k not in ('id', 'ok')}


def groups(env, session=SESSION) -> dict:
    """Tab order per group from ``pane.list``; ``layout.get`` must tell the same story."""
    out = {g: [] for g in GROUPS}
    for row in sorted(panes(env, session), key=lambda r: r['index']):
        out[row['group']].append(row['pane'])
    assert ctl(env, 'layout.get', session)['groups'] == out
    return out


def screen(env, pane_id, lines=200) -> str:
    reply = ctl(env, 'pane.read', pane=pane_id, lines=lines)
    return reply.get('text', '') if reply.get('ok') else ''


def registry(env, session=SESSION) -> dict:
    return _read_json(env.state / (session + '.fleetmux.json'))


def surfaces(env, session=SESSION) -> dict:
    return registry(env, session).get('surfaces') or {}


def wait_for(check, timeout=10.0, step=0.05) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(step)


def ps_args(pid: int) -> str:
    done = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'args='], capture_output=True, text=True)
    return done.stdout.strip()


def alive(pid: int) -> bool:
    """Running and not a zombie (a killed pane's process may linger as one until its parent reaps it)."""
    done = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
    stat = done.stdout.strip()
    return bool(stat) and not stat.startswith('Z')


def tty_comms(tty: str) -> list:
    name = tty[len('/dev/'):] if tty.startswith('/dev/') else tty
    done = subprocess.run(['ps', '-ww', '-t', name, '-o', 'comm='], capture_output=True, text=True)
    return [os.path.basename(line.strip()).lstrip('-') for line in done.stdout.splitlines() if line.strip()]


def tmux(sock, *args):
    return subprocess.run(['tmux', '-S', str(sock), *args], capture_output=True, text=True, timeout=10)


def tmux_clients(sock) -> list:
    return tmux(sock, 'list-clients', '-F', '#{client_tty}').stdout.split()


def spawn(mgr, name, command='sleep 600', type_='worker', cwd='/tmp', **meta):
    metadata = dict(meta, type=type_) if type_ is not None else dict(meta)
    res = mgr.spawn_window(name, command, metadata=metadata, start_directory=str(cwd))
    assert res.success, res.error
    assert PANE_ID.match(str(res.pane_id)), res.pane_id
    return res.pane_id


def py(code: str, *args) -> str:
    """A shell command running ``code`` in this interpreter (the pane gets it as one argv)."""
    return shlex.join([sys.executable, '-c', code, *[str(a) for a in args]])


def version_stub(env, line: str, name='stub-fleetmux') -> Path:
    path = env.root / name
    path.write_text('#!/bin/sh\nif [ "$1" = --version ]; then echo %s; exit 0; fi\nexit 2\n' % shlex.quote(line))
    path.chmod(0o755)
    return path


RECORDER = r'''
import os, sys, time, tty
out = open(sys.argv[1], 'a')
if '2004' in sys.argv[2:]:
    sys.stdout.write('\x1b[?2004h')
tty.setraw(0)
sys.stdout.write('REC-READY\r\n')
sys.stdout.flush()
while True:
    data = os.read(0, 65536)
    if not data:
        break
    out.write('%.4f %s\n' % (time.monotonic(), data.hex()))
    out.flush()
'''


def recorder(mgr, env, name='worker-1', *flags):
    """A pane whose program puts its tty in raw mode and logs every byte it reads (with a timestamp)."""
    out = env.root / (name + '.rec')
    out.write_text('')
    p = spawn(mgr, name, py(RECORDER, out, *flags))
    assert wait_for(lambda: 'REC-READY' in screen(env, p)), screen(env, p)
    return p, out


def chunks(out: Path) -> list:
    rows = []
    for line in out.read_text().splitlines():
        stamp, _, data = line.partition(' ')
        rows.append((float(stamp), bytes.fromhex(data)))
    return rows


def received(out: Path) -> bytes:
    return b''.join(data for _, data in chunks(out))


def make_run(env, name='run'):
    run = env.root / name
    run.mkdir()
    (run / 'history.html').write_text('<html></html>')
    return run, (run / 'history.html').as_uri()


# ================================================================ availability

def test_available_needs_binary_and_matching_protocol(impl, monkeypatch):
    assert fm.FleetmuxSessionManager.available() is True
    assert fm.availability_problem() is None and base.unavailable_reason('fleetmux') is None
    reported = subprocess.run([str(impl.binary), '--version'], capture_output=True, text=True, timeout=20)
    assert reported.returncode == 0 and reported.stdout.count('\n') == 1  # one line (ruling R9)
    shown = fm.VERSION_RE.fullmatch(reported.stdout.strip())
    assert shown and int(shown.group(4)) == fm.PROTOCOL, reported.stdout
    empty = impl.root / 'empty'
    empty.mkdir()
    monkeypatch.delenv(fm.BIN_ENV)
    monkeypatch.setenv('PATH', str(empty))
    assert fm.FleetmuxSessionManager.available() is False
    monkeypatch.setenv('PATH', str(impl.bindir))  # `fleetmux` on PATH is enough without the variable
    assert fm.FleetmuxSessionManager.available() is True
    other = 'fleetmux 0.2.0 (protocol %d)' % (fm.PROTOCOL + 1)
    monkeypatch.setenv(fm.BIN_ENV, str(version_stub(impl, other)))
    assert fm.FleetmuxSessionManager.available() is False
    why = fm.availability_problem()
    assert 'protocol' in why and str(fm.PROTOCOL) in why and str(fm.PROTOCOL + 1) in why


def test_available_rejects_too_old_version(impl, monkeypatch):
    monkeypatch.setenv(fm.BIN_ENV, str(version_stub(impl, 'fleetmux 0.0.9 (protocol %d)' % fm.PROTOCOL)))
    assert fm.FleetmuxSessionManager.available() is False
    why = fm.availability_problem()
    assert '0.2.0' in why and '0.0.9' in why
    monkeypatch.setenv(fm.BIN_ENV, str(version_stub(impl, 'fleetmux 0.3.0 (protocol %d)' % fm.PROTOCOL, 'newer')))
    assert fm.FleetmuxSessionManager.available() is True
    shown = subprocess.run([str(impl.binary), '--version'], capture_output=True, text=True, timeout=20).stdout
    found = fm.VERSION_RE.fullmatch(shown.strip())
    assert found and tuple(int(x) for x in found.groups()[:3]) >= fm.MIN_VERSION, shown


def test_unavailable_reason_names_what_is_missing(impl, monkeypatch):
    empty = impl.root / 'empty'
    empty.mkdir()
    monkeypatch.delenv(fm.BIN_ENV)
    monkeypatch.setenv('PATH', str(empty))
    why = base.unavailable_reason('fleetmux')
    assert why and 'fleetmux' in why and fm.BIN_ENV in why and 'PATH' in why
    broken = impl.root / 'broken-fleetmux'
    broken.write_text('#!/bin/sh\nexit 1\n')
    broken.chmod(0o755)
    monkeypatch.setenv(fm.BIN_ENV, str(broken))
    why = base.unavailable_reason('fleetmux')
    assert why and str(broken) in why and '--version' in why
    monkeypatch.setenv(fm.BIN_ENV, str(impl.binary))
    assert base.unavailable_reason('fleetmux') is None


# ================================================================ paths

def test_runtime_paths_derive_from_state_dir_and_session_only(impl):
    session = 'fleetmux:run/7 ' + 'x' * 60
    a = fm.FleetmuxSessionManager(session, state_dir=impl.state)
    b = fm.FleetmuxSessionManager(session, agents_title='Other agents', control_title='Other control',
                                  state_dir=impl.state)
    rt = impl.state.resolve() / 'fleetmux' / token(session)
    assert len(token(session)) == 40 and Path(a.runtime_root).resolve() == rt
    for m in (a, b):  # titles never move the runtime; only (state_dir, session) do
        assert Path(m.runtime_root).resolve() == rt
        assert Path(m.ctl_socket) == Path(m.runtime_root) / 'ctl.sock'
        assert Path(m.server_json) == Path(m.runtime_root) / 'server.json'
        assert Path(m.start_lock) == Path(m.runtime_root) / 'start.lock'
        assert Path(m.panes_json) == Path(m.runtime_root) / 'panes.json'
        assert Path(m.server_log) == Path(m.runtime_root) / 'server.log'
    assert a._token() == token(session)
    # literal expectations, so the helper above cannot drift from the Rust rule unnoticed
    for name, want in (('fleetmux:run/7', 'fleetmux_run_7'),
                       ('fleet-é工１-a_b', 'fleet-___-a_b'),        # non-ASCII letters / digits are replaced
                       ('ＡＢＣ', '___'),
                       ('a.b c', 'a_b_c'),
                       ('', 'fleet'),
                       ('w' * 39 + 'é' + 'z' * 5, 'w' * 39 + '_')):  # replaced first, then cut to 40
        assert token(name) == want, name
        m = fm.FleetmuxSessionManager(name, state_dir=impl.state)
        assert m._token() == want, name
        assert Path(m.runtime_root).resolve() == impl.state.resolve() / 'fleetmux' / want
    other = fm.FleetmuxSessionManager('fleetmux:run/8', state_dir=impl.state)
    assert Path(other.runtime_root) != Path(a.runtime_root)
    old = os.umask(0o002)  # Ubuntu's group-writable umask (5fb3e5f3) must not leak into the runtime
    try:
        a.prepare_runtime()
    finally:
        os.umask(old)
    assert rt.is_dir() and (rt.stat().st_mode & 0o777) == 0o700 and rt.stat().st_uid == os.getuid()


def test_socket_too_long_is_refused_no_tmp_fallback(impl):
    deep = impl.state / ('d' * 60) / ('e' * 60)
    deep.mkdir(parents=True)
    m = fm.FleetmuxSessionManager(SESSION, state_dir=deep)
    before = {p.name for p in Path(tempfile.gettempdir()).iterdir()} | {p.name for p in Path('/tmp').iterdir()}
    with pytest.raises(fm.FleetmuxError) as exc:
        m.prepare_runtime()
    assert len(str(deep / 'fleetmux' / token(SESSION) / 'ctl.sock')) > fm.SOCKET_PATH_LIMIT
    assert str(fm.SOCKET_PATH_LIMIT) in str(exc.value)
    assert m.get_or_create_session() is False and str(fm.SOCKET_PATH_LIMIT) in m.last_error
    res = m.spawn_window('control', 'sleep 600', metadata={'type': 'daemon'}, start_directory='/tmp')
    assert res.success is False and res.error
    assert not list(impl.root.rglob('ctl.sock')) and not list(impl.root.rglob('server.json'))
    after = {p.name for p in Path(tempfile.gettempdir()).iterdir()} | {p.name for p in Path('/tmp').iterdir()}
    assert not [n for n in after - before if 'fleetmux' in n or token(SESSION) in n]


def test_registry_file_is_session_dot_fleetmux_json(impl, monkeypatch):
    mgr = new_mgr(impl)
    assert mgr.meta_path == impl.state.resolve() / (SESSION + '.fleetmux.json')
    assert isinstance(mgr._meta(), dict)  # no file yet: an empty registry, not an exception
    mgr._save_meta({'run': '/runs/r1', 'surfaces': {}})
    assert (impl.state / (SESSION + '.fleetmux.json')).is_file()
    assert not (impl.state / (SESSION + '.json')).exists()  # never the cmux / wezterm file name
    assert mgr._meta()['run'] == '/runs/r1'
    mgr._tag('abcd-1', type='worker', label='worker-1')
    entry = surfaces(impl)['abcd-1']
    assert entry['type'] == 'worker' and entry['label'] == 'worker-1'
    monkeypatch.setenv('FLEETMUX_STATE', str(impl.root / 'st2'))  # default state dir, as the other backends
    assert fm.FleetmuxSessionManager('other').meta_path == (impl.root / 'st2').resolve() / 'other.fleetmux.json'


# ================================================================ start / stop

def test_get_or_create_session_starts_server_and_hello_matches(impl):
    mgr = new_mgr(impl)
    assert mgr.get_or_create_session() is True
    hello = ctl(impl, 'hello')
    assert hello['ok'] is True and hello['protocol'] == fm.PROTOCOL and hello['uid'] == os.getuid()
    info = _read_json(runtime_of(impl) / 'server.json')
    assert info['pid'] == hello['pid'] and info['epoch'] == hello['epoch'] and alive(hello['pid'])
    assert info['protocol'] == fm.PROTOCOL and isinstance(info['start_time'], int)  # Unix seconds (§8.6)
    assert re.fullmatch(r'[0-9a-z]{4}', hello['epoch']) and hello['version'] == info['version']
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:  # bad JSON keeps the connection (§1.3)
        conn.settimeout(10)
        conn.connect(str(runtime_of(impl) / 'ctl.sock'))
        line = conn.makefile('rwb')
        for raw, check in ((b'this is not json\n', lambda r: r['code'] == 'bad_request' and r['id'] is None),
                           (b'{"id": 9, "cmd": "no.such"}\n', lambda r: r['code'] == 'unknown_cmd' and r['id'] == 9),
                           (b'{"id": 10, "cmd": "hello"}\n', lambda r: r['ok'] is True and r['id'] == 10)):
            line.write(raw)
            line.flush()
            reply = json.loads(line.readline())
            assert check(reply), reply
    assert mgr.get_or_create_session() is True  # second call: the same server, not a new one
    again = ctl(impl, 'hello')
    assert (again['pid'], again['epoch']) == (hello['pid'], hello['epoch'])


def test_server_theme_env_passes_through_and_hello_reports_it(impl, monkeypatch):
    """``FLEETMUX_THEME`` is the one knob (plan §2.5): the server argv gets ``--theme FILE`` and
    ``hello.theme`` reports the merged palette (PROTOCOL.md §4.1 / §9.1)."""
    path = impl.root / 'theme.json'
    path.write_text(json.dumps({'bg': '#123456'}))
    monkeypatch.setenv(fm.THEME_ENV, str(path))
    calls = []
    real = subprocess.run

    def spy(argv, *a, **kw):
        calls.append([str(x) for x in argv] if isinstance(argv, (list, tuple)) else [str(argv)])
        return real(argv, *a, **kw)
    monkeypatch.setattr(subprocess, 'run', spy)
    mgr = new_mgr(impl)
    assert mgr.get_or_create_session() is True
    starts = [c for c in calls if 'server' in c and '--runtime' in c]
    assert starts and all('--theme' in c for c in starts), calls
    for c in starts:
        assert c[c.index('--theme') + 1] == str(path), c
    hello = mgr._hello()
    assert hello is not None and 'theme' in hello, hello
    theme = hello['theme']
    assert theme['bg'] == '#123456'                                   # the file's one key wins, lowercase out
    assert theme['fg'] == '#ffffff' and theme['cursor'] == '#98989d'  # the rest stays cmux default
    assert len(theme['ansi']) == 16 and theme['ansi'][1] == '#cc372e'


def test_server_theme_defaults_to_cmux_without_env(impl, monkeypatch):
    """No environment variable: no ``--theme`` in argv, ``hello.theme`` = the cmux palette (PROTOCOL.md §9.1)."""
    monkeypatch.delenv(fm.THEME_ENV, raising=False)
    calls = []
    real = subprocess.run

    def spy(argv, *a, **kw):
        calls.append([str(x) for x in argv] if isinstance(argv, (list, tuple)) else [str(argv)])
        return real(argv, *a, **kw)
    monkeypatch.setattr(subprocess, 'run', spy)
    mgr = new_mgr(impl)
    assert mgr.get_or_create_session() is True
    starts = [c for c in calls if 'server' in c and '--runtime' in c]
    assert starts and all('--theme' not in c for c in starts), calls
    theme = mgr._hello()['theme']
    assert theme['bg'] == '#1e1e1e' and theme['fg'] == '#ffffff' and theme['cursor'] == '#98989d'
    assert len(theme['ansi']) == 16
    assert theme['ansi'][0] == '#1a1a1a' and theme['ansi'][1] == '#cc372e' and theme['ansi'][15] == '#ffffff'


def test_start_failure_reports_server_log_tail_and_never_claims_started(impl, monkeypatch):
    mgr = new_mgr(impl)
    broken = impl.root / 'failing-fleetmux'
    broken.write_text('#!/bin/sh\n'
                      'case "$1" in\n'
                      '  --version) echo %s ;;\n'
                      '  server) while [ $# -gt 0 ]; do [ "$1" = --runtime ] && rt="$2"; shift; done\n'
                      '          echo "boom: cannot bind ctl.sock" >> "$rt/server.log"; exit 3 ;;\n'
                      '  *) exit 2 ;;\n'
                      'esac\n' % shlex.quote(GOOD_VERSION))
    broken.chmod(0o755)
    monkeypatch.setenv(fm.BIN_ENV, str(broken))
    assert mgr.get_or_create_session() is False
    assert 'boom: cannot bind ctl.sock' in mgr.last_error
    assert mgr.session_exists() is False
    res = mgr.spawn_window('control', 'sleep 600', metadata={'type': 'daemon'}, start_directory='/tmp')
    assert res.success is False and 'boom: cannot bind ctl.sock' in (res.error or '')
    assert not (runtime_of(impl) / 'server.json').exists() and not server_up(impl)


def test_session_exists_needs_server_and_at_least_one_pane(impl):
    mgr = new_mgr(impl)
    assert mgr.session_exists() is False
    assert mgr.get_or_create_session() is True
    assert server_up(impl) and mgr.session_exists() is False  # a server with no pane is not a session
    p = spawn(mgr, 'worker-1')
    assert mgr.session_exists() is True
    assert mgr.kill_pane(p) is True
    assert server_up(impl) and mgr.session_exists() is False  # lifecycle_close.all_gone must not wait on exit


def test_close_all_kills_everything_then_stops_server(impl):
    mgr = new_mgr(impl)
    sup = spawn(mgr, 'supervisor', type_='supervisor')
    w = spawn(mgr, 'worker-1')
    pids = [pane(impl, x)['pid'] for x in (sup, w)]
    srv = _read_json(runtime_of(impl) / 'server.json')['pid']
    assert all(alive(p) for p in pids)
    assert mgr.close_all() is True
    assert wait_for(lambda: not any(alive(p) for p in pids)), 'a pane process outlived close_all'
    assert wait_for(lambda: not alive(srv)), 'the server outlived close_all'
    assert not server_up(impl)
    rt = runtime_of(impl)
    assert [n for n in fm.REMOVED_ON_STOP if (rt / n).exists()] == []  # ctl.sock, server.json, panes.json
    assert [n for n in fm.KEPT_ON_STOP if not (rt / n).exists()] == []  # server.log, layout.json, start.lock
    assert surfaces(impl) == {} and mgr.session_exists() is False


def test_close_all_returns_false_when_list_fails_and_keeps_registry(impl, monkeypatch):
    mgr = new_mgr(impl)
    w = spawn(mgr, 'worker-1')
    real_call = mgr._call

    def failing(cmd, **args):
        if cmd in ('pane.list', 'pane.get'):
            raise fm.FleetmuxError('pane table unavailable', code='internal')
        return real_call(cmd, **args)
    monkeypatch.setattr(mgr, '_call', failing)
    assert mgr.close_all() is False  # "could not list" is not "nothing left"
    assert w in surfaces(impl)
    assert server_up(impl) and alive(pane(impl, w)['pid'])


def test_close_all_pid_fallback_checks_cmdline_and_start_time(impl):
    """Socket gone but server.json's pid alive: signal it only when its command line carries
    ``--runtime <this runtime>`` *and* its OS start time in integer Unix seconds equals ``start_time``
    exactly (PROTOCOL.md §8.6, ruling R6: no tolerance; unverifiable means no signal)."""
    mgr = new_mgr(impl)
    assert mgr.get_or_create_session() is True
    rt = runtime_of(impl)
    recorded = _read_json(rt / 'server.json')
    srv = recorded['pid']
    time.sleep(1.1)  # the sleepers below start in a later Unix second than the server: start_time differs
    sleeper = [sys.executable, '-c', 'import time; time.sleep(600)']
    other = subprocess.Popen(sleeper + ['--runtime', str(rt) + '.other'])
    lookalike = subprocess.Popen(sleeper + ['--runtime', str(rt)])
    try:
        assert wait_for(lambda: str(rt) in ps_args(lookalike.pid))
        (rt / 'ctl.sock').rename(rt / 'ctl.sock.hidden')  # the server no longer answers on its path
        (rt / 'server.json').write_text(json.dumps(dict(recorded, pid=other.pid)))
        assert mgr._pid_is_ours(other.pid) is False
        assert mgr.end_server() is False and other.poll() is None, 'a process of another runtime was signalled'
        (rt / 'server.json').write_text(json.dumps(dict(recorded, pid=lookalike.pid)))
        assert mgr._pid_is_ours(lookalike.pid) is False  # right command line, wrong start time
        assert mgr.end_server() is False and lookalike.poll() is None, 'a reused pid was signalled'
        (rt / 'server.json').write_text(json.dumps(recorded))
        assert mgr._pid_is_ours(srv) is True
        assert mgr.end_server() is True
        assert wait_for(lambda: not alive(srv)), 'this run\'s server was not stopped'
    finally:
        for proc in (other, lookalike):
            proc.kill()
            proc.wait()


# ================================================================ spawn

def test_first_spawn_window_starts_the_server(impl):
    mgr = new_mgr(impl)
    assert not server_up(impl)
    res = mgr.spawn_window('control', 'sleep 600', metadata={'type': 'daemon'}, start_directory='/tmp')
    assert res.success, res.error
    assert PANE_ID.match(res.pane_id) and res.window_name == 'control'
    assert server_up(impl) and res.pane_id in [p['pane'] for p in panes(impl)]
    assert mgr.owns(res.pane_id) and surfaces(impl)[res.pane_id]['type'] == 'daemon'


def test_spawn_runs_argv_bash_lc_never_types(impl):
    mgr = new_mgr(impl)
    work = impl.root / 'work dir'
    work.mkdir()
    payload = 'p' * 3000  # far past the 1024-byte line limit of typing into a shell
    out = work / 'out.txt'
    code = 'import os, sys, time; open(sys.argv[1], "w").write(os.getcwd() + "|" + sys.argv[2]); time.sleep(600)'
    p = spawn(mgr, 'worker-1', py(code, out, payload), cwd=work)
    assert wait_for(lambda: out.exists() and '|' in out.read_text())
    cwd, _, got = out.read_text().partition('|')
    assert os.path.realpath(cwd) == str(work.resolve()) and got == payload
    assert payload in ps_args(pane(impl, p)['pid'])  # one argv, untruncated
    shown = screen(impl, p)
    assert 'ppppppppppppppppppppppppp' not in shown and 'time.sleep' not in shown  # nothing was typed
    assert not list(impl.state.rglob('*.sh')) and not list(impl.state.rglob('launch*'))  # no launcher script


def test_spawn_places_pane_by_type_table(impl):
    """design §3.4, all 8 rows: where a fresh pane lands before fleet arranges anything."""
    mgr = new_mgr(impl)
    w1 = spawn(mgr, 'worker-1', type_='worker')            # worker      -> workers, end
    s1 = spawn(mgr, 'supervisor-1', type_='supervisor')    # supervisor  -> supervisor, first
    adv = spawn(mgr, 'advisor-1', type_='advisor')         # advisor     -> supervisor, end
    s2 = spawn(mgr, 'supervisor-1.1', type_='supervisor')
    t1 = spawn(mgr, 'evermem-shell', type_='tab')          # tab         -> lower, end
    d1 = spawn(mgr, 'control', type_='daemon')             # daemon      -> lower, first without history
    h = spawn(mgr, 'history', type_='history')             # history     -> lower, first
    d2 = spawn(mgr, 'control-2', type_='daemon')           # daemon      -> lower, right after history
    ph = spawn(mgr, 'placeholder', type_='placeholder')    # placeholder -> as worker
    unk = spawn(mgr, 'mystery', type_='no-such-type')      # unknown     -> workers, end
    bare = spawn(mgr, 'untyped', type_=None)               # no type     -> workers, end
    assert groups(impl) == {'supervisor': [s2, s1, adv], 'lower': [h, d2, d1, t1], 'workers': [w1, ph, unk, bare]}
    rows = {p['pane']: p for p in panes(impl)}
    for pid, want in ((w1, 'worker'), (s1, 'supervisor'), (adv, 'advisor'), (t1, 'tab'), (d1, 'daemon'),
                      (h, 'history')):
        assert rows[pid]['type'] == want
    assert rows[w1]['title'] == 'worker-1' and rows[t1]['title'] == 'evermem-shell'


def test_spawn_interactive_cli_prompt_file_and_auto_close_shell(impl):
    mgr = new_mgr(impl)
    prompt = impl.root / 'prompt.md'
    text = 'line one\nit\'s "quoted" $HOME `not run` -n\n'
    prompt.write_text(text)
    code = 'import sys; open(sys.argv[1], "w").write(sys.argv[2])'
    got = impl.root / 'got.txt'
    res = mgr.spawn_interactive_cli('worker-1', py(code, got), prompt_file=str(prompt),
                                    metadata={'type': 'worker'}, start_directory=str(impl.root))
    assert res.success, res.error
    assert wait_for(lambda: got.exists())
    assert got.read_text() == text.rstrip('\n')  # "$(<file)": one argument, verbatim, no expansion
    assert wait_for(lambda: pane(impl, res.pane_id) is None)  # auto_close: the pane ends with its CLI
    assert mgr.get_pane_status(res.pane_id) is None
    got2 = impl.root / 'got2.txt'
    res2 = mgr.spawn_interactive_cli('worker-2', py(code, got2), prompt_file=str(prompt), auto_close=False,
                                     metadata={'type': 'worker'}, start_directory=str(impl.root))
    assert res2.success, res2.error
    assert wait_for(lambda: got2.exists())
    time.sleep(1.0)
    row = pane(impl, res2.pane_id)
    assert row is not None, 'auto_close=False: a login shell must keep the pane open'
    shell = os.path.basename(os.environ.get('SHELL') or 'bash')
    assert wait_for(lambda: shell in tty_comms(row['tty']))


def test_spawn_interactive_cli_env_vars_are_structured(impl):
    mgr = new_mgr(impl)
    env_vars = {'FLEET_X': 'a b\'c"$d `e` ;rm', 'FLEET_Y': 'one\ntwo', 'FLEET_EMPTY': ''}
    dump = impl.root / 'env.json'
    code = ('import json, os, sys, time; keys = ("FLEET_X", "FLEET_Y", "FLEET_EMPTY")\n'
            'open(sys.argv[1] + ".tmp", "w").write(json.dumps({k: os.environ.get(k) for k in keys}))\n'
            'os.replace(sys.argv[1] + ".tmp", sys.argv[1]); time.sleep(600)')
    res = mgr.spawn_interactive_cli('worker-1', py(code, dump), env_vars=env_vars, metadata={'type': 'worker'},
                                    start_directory=str(impl.root))
    assert res.success, res.error
    assert wait_for(lambda: dump.exists())
    assert json.loads(dump.read_text()) == env_vars
    shown = screen(impl, res.pane_id)
    assert 'FLEET_X' not in shown and 'export' not in shown  # passed as env, never typed


def test_spawn_with_tmux_fields_builds_session_then_attaches(impl):
    mgr = new_mgr(impl)
    if not shutil.which('tmux'):
        pytest.skip('tmux not installed (deps.yaml requires it)')
    sock = impl.root / 'tmux' / 'w1.sock'
    sock.parent.mkdir()
    res = mgr.spawn_interactive_cli('worker-1', 'sleep 600', metadata={'type': 'worker'},
                                    start_directory=str(impl.root), tmux_socket=str(sock),
                                    tmux_session='worker-1-g0')
    assert res.success, res.error
    assert tmux(sock, 'has-session', '-t', '=worker-1-g0').returncode == 0
    info = mgr.tmux_view_info(res.pane_id)
    assert info['tmux_socket'] == str(sock) and info['tmux_session'] == 'worker-1-g0'
    row = pane(impl, res.pane_id)
    assert wait_for(lambda: tmux_clients(sock) == [row['tty']]), 'our pane is not the tmux client'
    commands = tmux(sock, 'list-panes', '-a', '-F', '#{pane_current_command}').stdout.split()
    assert 'sleep' in commands  # the CLI runs inside the tmux session, the pane only shows it


# ================================================================ write

def test_send_prompt_bracketed_true_always_wraps(impl):
    mgr = new_mgr(impl)
    p, out = recorder(mgr, impl)  # the inner program never turns on 2004
    assert mgr.send_prompt_to_idle(p, 'line one\nline two', bracketed=True) is True
    assert wait_for(lambda: received(out).endswith(b'\r'))
    data = received(out)
    assert data.startswith(b'\x1b[200~line one') and data.endswith(b'line two\x1b[201~\r')
    assert data.count(b'\x1b[200~') == 1 and data.count(b'\x1b[201~') == 1
    # the server's side: ``written`` counts the decoded data bytes, never the paste markers (§4.7)
    reply = ctl(impl, 'pane.write', pane=p, data=base64.b64encode(b'-x').decode(), mode='bracketed')
    assert reply['ok'] is True and reply['written'] == 2, reply
    assert wait_for(lambda: received(out).endswith(b'\x1b[200~-x\x1b[201~'))


def test_send_prompt_bracketed_false_never_wraps(impl):
    mgr = new_mgr(impl)
    p, out = recorder(mgr, impl, 'worker-1', '2004')  # the inner program asks for bracketed paste
    assert mgr.send_prompt_to_idle(p, 'hello world', bracketed=False) is True
    assert wait_for(lambda: received(out).endswith(b'\r'))
    assert received(out) == b'hello world\r'
    assert mgr.send_prompt_to_idle('zzzz-1', 'not ours') is False


def test_send_prompt_leading_dash_is_data(impl):
    mgr = new_mgr(impl)
    p, out = recorder(mgr, impl)
    text = '--help -n -- -x'
    assert mgr.send_prompt_to_idle(p, text, bracketed=False) is True
    assert wait_for(lambda: received(out) == text.encode() + b'\r')
    assert mgr.send_prompt_to_idle(p, text, bracketed=True) is True
    want = text.encode() + b'\r' + b'\x1b[200~' + text.encode() + b'\x1b[201~\r'
    assert wait_for(lambda: received(out) == want), received(out)


def test_send_prompt_then_enter_after_delay(impl):
    """Enter is a separate write after the paste: 1 s when bracketed, 0.2 s raw (paste-burst detection)."""
    mgr = new_mgr(impl)
    p, out = recorder(mgr, impl)
    for bracketed, gap in ((True, 0.9), (False, 0.15)):
        out.write_text('')
        assert mgr.send_prompt_to_idle(p, 'go', bracketed=bracketed) is True
        assert wait_for(lambda: received(out).endswith(b'\r'))
        rows = chunks(out)
        assert rows[-1][1] == b'\r', rows  # Enter arrives on its own
        assert rows[-1][0] - rows[-2][0] >= gap, rows


def test_send_key_known_names_and_unknown_returns_false(impl):
    mgr = new_mgr(impl)
    p, out = recorder(mgr, impl)  # normal cursor mode: arrows are CSI, exactly the table's bytes
    for name in fm.KEYS:
        assert mgr.send_key(p, name) is True, name
    want = b''.join(value.encode('latin-1') for value in fm.KEYS.values())
    assert wait_for(lambda: received(out) == want), received(out)
    assert mgr.send_key(p, 'NoSuchKey') is False and mgr.send_key(p, 'C-') is False
    time.sleep(0.5)
    assert received(out) == want  # an unknown name writes nothing, not even its own text
    assert mgr.send_key('zzzz-1', 'Enter') is False


# ================================================================ read

def test_capture_output_lines_physical_rows_no_trailing_blanks(impl):
    mgr = new_mgr(impl)
    code = ('import time; print("alpha   "); print("beta"); print("W" * 300)\n'
            'for i in range(200): print("L%03d" % i)\n'
            'print("END", flush=True); time.sleep(600)')
    p = spawn(mgr, 'worker-1', py(code))
    assert wait_for(lambda: 'END' in mgr.capture_output(p))
    lines = mgr.capture_output(p, 2000).split('\n')
    assert all(line == line.rstrip() for line in lines)
    assert 'alpha' in lines and 'beta' in lines
    cols = int(pane(impl, p)['cols'])
    wrapped = [line for line in lines if line and set(line) == {'W'}]
    assert len(wrapped) >= 2 and all(len(line) <= cols for line in wrapped)  # physical rows, not logical
    assert sum(map(len, wrapped)) == 300
    assert all('L%03d' % i in lines for i in range(200))
    tail = mgr.capture_output(p, 10).split('\n')
    assert len(tail) == 10 and 'L000' not in tail  # exactly the last 10 physical rows
    assert 'END' in tail and tail[-1] == ''  # the blank cursor row at the bottom is kept, not dropped


def test_capture_output_100000_returns_all_available(impl):
    mgr = new_mgr(impl)
    code = 'import time\nfor i in range(12000): print("R%05d" % i)\nprint("DONE", flush=True); time.sleep(600)'
    p = spawn(mgr, 'worker-1', py(code))
    assert wait_for(lambda: 'DONE' in mgr.capture_output(p), timeout=30)
    lines = mgr.capture_output(p, 100000).split('\n')
    assert 'DONE' in lines and 'R11999' in lines
    assert 10000 <= len(lines) <= 100000  # everything the scrollback still holds (default 10,000)
    seq = [int(line[1:]) for line in lines if re.fullmatch(r'R\d{5}', line)]
    assert seq == list(range(seq[0], 12000))  # a contiguous tail, nothing dropped in between
    small = spawn(mgr, 'worker-2', py('import time; print("one\\ntwo\\nthree", flush=True); time.sleep(600)'))
    assert wait_for(lambda: 'three' in mgr.capture_output(small, 100000))
    assert {'one', 'two', 'three'} <= set(mgr.capture_output(small, 100000).split('\n'))


def test_capture_to_is_written_by_python_not_server(impl, monkeypatch):
    mgr = new_mgr(impl)
    p = spawn(mgr, 'worker-1', py('import time; print("CAPTURED-TEXT", flush=True); time.sleep(600)'))
    assert wait_for(lambda: 'CAPTURED-TEXT' in mgr.capture_output(p))
    seen = []
    real_call = mgr._call
    monkeypatch.setattr(mgr, '_call', lambda cmd, **args: seen.append((cmd, args)) or real_call(cmd, **args))
    path = impl.root / 'captures' / 'w1.txt'
    mgr.capture_to(p, str(path))
    assert 'CAPTURED-TEXT' in path.read_text()
    assert path.read_text().rstrip('\n') == mgr.capture_output(p, 100000).rstrip('\n')
    assert seen and all(str(path) not in json.dumps(args) for _, args in seen)  # the server never sees the path
    assert {cmd for cmd, _ in seen} <= {'hello', 'pane.read', 'pane.get', 'pane.list'}
    monkeypatch.setattr(mgr, 'capture_output', lambda pane_id, lines=2000: 'SENTINEL %d' % lines)
    mgr.capture_to(p, str(path))
    assert path.read_text().startswith('SENTINEL 100000')


# ================================================================ state

def test_get_pane_status_metadata_has_tty_group_size(impl):
    mgr = new_mgr(impl)
    w = spawn(mgr, 'worker-1', label='worker-1')
    st = mgr.get_pane_status(w)
    assert isinstance(st, PaneStatus) and st.pane_id == w and st.window_name == 'worker-1' and st.alive is True
    row = pane(impl, w)
    md = st.metadata
    assert md['type'] == 'worker' and md['label'] == 'worker-1'
    assert md['tty'] == row['tty'] and md['tty'].startswith('/dev/')
    assert md['group'] == 'workers'
    assert str(md['cols']) == str(row['cols']) and int(md['cols']) > 0
    assert str(md['rows']) == str(row['rows']) and int(md['rows']) > 0
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    assert mgr.get_pane_status(s).metadata['group'] == 'supervisor'


def test_get_pane_status_none_for_foreign_or_gone_pane(impl):
    mgr = new_mgr(impl)
    assert mgr.get_pane_status('zzzz-1') is None
    other = fm.FleetmuxSessionManager('fm-other', state_dir=impl.state)  # another run, its own server
    o = spawn(other, 'worker-1')
    assert other.get_pane_status(o) is not None and mgr.get_pane_status(o) is None
    gone = spawn(mgr, 'worker-2', command='true')
    assert wait_for(lambda: mgr.get_pane_status(gone) is None)


def test_has_cli_process_uses_tty_and_ps_ww(impl, monkeypatch):
    mgr = new_mgr(impl)
    p = spawn(mgr, 'worker-1', command='sleep 600')
    bare = spawn(mgr, 'worker-2', command='exec /bin/sh -i')
    calls = []
    for name in ('run', 'check_output'):
        real = getattr(subprocess, name)

        def spy(argv, *a, _real=real, **kw):
            calls.append([str(x) for x in argv] if isinstance(argv, (list, tuple)) else [str(argv)])
            return _real(argv, *a, **kw)
        monkeypatch.setattr(subprocess, name, spy)
    assert wait_for(lambda: mgr.has_cli_process(p, ('sleep',)))
    assert mgr.has_cli_process(p, ('codex',)) is False
    assert mgr.has_cli_process(bare, ('codex', 'claude')) is False  # a bare shell is no CLI
    assert mgr.has_cli_process('zzzz-1', ('sleep',)) is False
    tty = pane(impl, p)['tty']
    ps = [c for c in calls if os.path.basename(c[0]) == 'ps']
    assert ps and all('-ww' in c for c in ps), ps
    assert any('-t' in c and os.path.basename(tty) in ' '.join(c) for c in ps), ps


def test_process_states_and_cpu_via_tty(impl):
    mgr = new_mgr(impl)
    p = spawn(mgr, 'worker-1', command='sleep 600')
    assert wait_for(lambda: 'sleep' in tty_comms(pane(impl, p)['tty']))
    states = mgr.process_states(p)
    assert states and all(re.match(r'[DIRSTUWXZ]', s) for s in states), states
    cpu = mgr.process_cpu(p)
    assert isinstance(cpu, float) and cpu >= 0.0
    assert mgr.process_states('zzzz-1') == [] and mgr.process_cpu('zzzz-1') is None


# ================================================================ tabs

def test_rename_updates_registry_label_and_title(impl):
    mgr = new_mgr(impl)
    w = spawn(mgr, 'fx-worker-1', label='worker-1')
    mgr.rename(w, 'worker-1.1')
    assert surfaces(impl)[w]['label'] == 'worker-1.1'
    assert pane(impl, w)['title'] == 'worker-1.1'
    assert mgr.get_pane_status(w).metadata['label'] == 'worker-1.1'


def test_focus_selects_tab_and_group(impl):
    mgr = new_mgr(impl)
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    spawn(mgr, 'worker-1')
    w2 = spawn(mgr, 'worker-2')
    assert mgr.focus(w2) is True
    lay = ctl(impl, 'layout.get')
    assert lay['ok'] and lay['focus'] == 'workers' and lay['selected']['workers'] == w2
    assert [p['pane'] for p in panes(impl) if p.get('focused')] == [w2]
    assert mgr.focus(s) is True
    lay = ctl(impl, 'layout.get')
    assert lay['focus'] == 'supervisor' and lay['selected']['workers'] == w2  # the other group keeps its tab
    assert mgr.focus('zzzz-9') is False


def test_kill_pane_unconfirmed_returns_false_and_keeps_registry(impl, monkeypatch):
    mgr = new_mgr(impl)
    w = spawn(mgr, 'worker-1')
    real_call = mgr._call

    def refuse(cmd, **args):
        if cmd == 'pane.kill':
            raise fm.FleetmuxError('process group still alive after 5000 ms', code='kill_timeout')
        return real_call(cmd, **args)
    monkeypatch.setattr(mgr, '_call', refuse)
    assert mgr.kill_pane(w) is False
    assert w in surfaces(impl) and mgr.owns(w) and pane(impl, w) is not None  # fleet can retry
    monkeypatch.setattr(mgr, '_call', real_call)
    assert mgr.kill_pane(w) is True and w not in surfaces(impl)
    assert mgr.kill_pane('zzzz-1') is False
    # the server side of the same contract (ruling R4): timeout_ms 2000 sends SIGHUP then SIGTERM, never SIGKILL
    stubborn = spawn(mgr, 'worker-2', py('import signal, time\nfor s in (signal.SIGHUP, signal.SIGTERM): '
                                         'signal.signal(s, signal.SIG_IGN)\nprint("STUBBORN", flush=True)\n'
                                         'time.sleep(600)'))
    assert wait_for(lambda: 'STUBBORN' in screen(impl, stubborn))
    pid = pane(impl, stubborn)['pid']
    began = time.monotonic()
    reply = ctl(impl, 'pane.kill', pane=stubborn, timeout_ms=2000)
    took = time.monotonic() - began
    assert reply['ok'] is False and reply['code'] == 'kill_timeout', reply
    assert 2.0 <= took < 4.0, took  # deadline + 500 ms reap grace, nothing more
    assert alive(pid) and pane(impl, stubborn) is not None  # no SIGKILL was sent; the pane stays
    assert stubborn in surfaces(impl) and mgr.owns(stubborn)


def test_kill_pane_appends_screen_to_output_file_first(impl):
    mgr = new_mgr(impl)
    log = impl.root / 'worker-1.log'
    log.write_text('earlier generation\n')
    res = mgr.spawn_interactive_cli('worker-1', py('import time; print("LAST-WORDS-OF-W1", flush=True); '
                                                   'time.sleep(600)'), output_file=str(log),
                                    metadata={'type': 'worker'}, start_directory=str(impl.root))
    assert res.success, res.error
    assert wait_for(lambda: 'LAST-WORDS-OF-W1' in screen(impl, res.pane_id))
    pid = pane(impl, res.pane_id)['pid']
    assert mgr.kill_pane(res.pane_id) is True
    text = log.read_text()
    assert text.startswith('earlier generation\n') and 'LAST-WORDS-OF-W1' in text
    assert pane(impl, res.pane_id) is None and not alive(pid) and res.pane_id not in surfaces(impl)


def test_kill_pane_of_vanished_pane_cleans_registry_returns_true(impl):
    mgr = new_mgr(impl)
    w = spawn(mgr, 'worker-1')
    assert ctl(impl, 'pane.kill', pane=w)['ok'] is True  # gone behind the backend's back
    assert pane(impl, w) is None and w in surfaces(impl)
    assert mgr.kill_pane(w) is True and w not in surfaces(impl)
    assert mgr.kill_pane(w) is False  # no longer ours


def test_kill_pane_unreachable_or_unreadable_server_keeps_registry(impl, monkeypatch):
    """design §4.3 / §5: a failed read is not "no pane". Socket silent or ``pane.get`` failing -> False, the
    registry entry (with its output_file) is kept and nothing is signalled; only a live server's explicit
    ``no_such_pane`` or a confirmed kill may drop it."""
    mgr = new_mgr(impl)
    log = impl.root / 'worker-1.log'
    res = mgr.spawn_interactive_cli('worker-1', 'sleep 600', output_file=str(log), metadata={'type': 'worker'},
                                    start_directory=str(impl.root))
    assert res.success, res.error
    w = res.pane_id
    pid = pane(impl, w)['pid']
    rt = runtime_of(impl)
    (rt / 'ctl.sock').rename(rt / 'ctl.sock.hidden')  # the server is alive but no longer answers on its path
    try:
        assert mgr.kill_pane(w) is False
        assert surfaces(impl)[w]['output_file'] == str(log) and alive(pid)
    finally:
        (rt / 'ctl.sock.hidden').rename(rt / 'ctl.sock')
    real_call = mgr._call

    def unreadable(cmd, **args):
        if cmd == 'pane.get':
            raise fm.FleetmuxError('pane table unavailable', code='internal')
        return real_call(cmd, **args)
    monkeypatch.setattr(mgr, '_call', unreadable)
    assert mgr.kill_pane(w) is False
    assert surfaces(impl)[w]['output_file'] == str(log) and alive(pid) and pane(impl, w) is not None
    monkeypatch.setattr(mgr, '_call', real_call)
    assert mgr.kill_pane(w) is True and w not in surfaces(impl) and pane(impl, w) is None


# ================================================================ layout

def test_arrange_spawns_history_view_returns_its_real_id_and_is_idempotent(impl):
    mgr = new_mgr(impl)
    run, url = make_run(impl)
    mgr.claim_run(str(run))
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    w1 = spawn(mgr, 'worker-1')
    w2 = spawn(mgr, 'worker-2')
    c = spawn(mgr, 'control', type_='daemon')
    hist = mgr.arrange(s, [w2, w1], url)
    assert hist and PANE_ID.match(hist)
    rows = {p['pane']: p for p in panes(impl)}
    assert hist in rows and rows[hist]['type'] == 'history'
    g = groups(impl)
    assert g['lower'][:2] == [hist, c] and g['supervisor'][0] == s and g['workers'] == [w2, w1]
    entry = surfaces(impl)[hist]
    assert entry['type'] == 'history' and entry['url'] == url
    args = ps_args(rows[hist]['pid'])
    assert 'fleetmux.history_view' in args and str(run) in args
    assert mgr.arrange(s, [w2, w1], url) == hist  # re-entry: the same pane, not a second one
    assert [p['pane'] for p in panes(impl) if p['type'] == 'history'] == [hist]


def test_arrange_refuses_history_of_another_run_returns_none(impl):
    mgr = new_mgr(impl)
    run_a, url_a = make_run(impl, 'run-a')
    _, url_b = make_run(impl, 'run-b')
    mgr.claim_run(str(run_a))
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    w = spawn(mgr, 'worker-1')
    hist = mgr.arrange(s, [w], url_a)
    assert hist
    assert mgr.arrange(s, [w], url_b) is None
    assert [p['pane'] for p in panes(impl) if p['type'] == 'history'] == [hist]
    assert surfaces(impl)[hist]['url'] == url_a


def test_arrange_focus_goes_to_supervisor(impl):
    mgr = new_mgr(impl)
    run, url = make_run(impl)
    mgr.claim_run(str(run))
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    w = spawn(mgr, 'worker-1')
    assert mgr.focus(w) is True
    assert mgr.arrange(s, [w], url)
    lay = ctl(impl, 'layout.get')
    assert lay['focus'] == 'supervisor' and lay['selected']['supervisor'] == s
    assert [p['pane'] for p in panes(impl) if p.get('focused')] == [s]


def test_arrange_shows_history_not_control_in_lower(impl):
    """11-09 user: 「fleetmux默认显示history界面而不是daemon界面」. The control (daemon) pane is spawned first,
    so it was the lower group's selected tab; arrange makes history the shown tab, focus stays on the supervisor."""
    mgr = new_mgr(impl)
    run, url = make_run(impl)
    mgr.claim_run(str(run))
    c = spawn(mgr, 'control', type_='daemon')
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    w = spawn(mgr, 'worker-1')
    hist = mgr.arrange(s, [w], url)
    lay = ctl(impl, 'layout.get')
    assert groups(impl)['lower'][:2] == [hist, c]
    assert lay['selected']['lower'] == hist
    assert lay['focus'] == 'supervisor' and lay['selected']['supervisor'] == s


def test_dock_tab_after_given_else_control_else_history(impl):
    mgr = new_mgr(impl)
    w = spawn(mgr, 'worker-1')
    h = spawn(mgr, 'history', type_='history')
    t1 = spawn(mgr, 'tab-1', type_='tab')
    assert mgr.tab_into(t1, w) is True and t1 in groups(impl)['workers']
    assert mgr.dock_tab(t1, h) is True
    assert groups(impl)['lower'] == [h, t1]  # no control: right after history
    c = spawn(mgr, 'control', type_='daemon')
    assert groups(impl)['lower'] == [h, c, t1]
    t2 = spawn(mgr, 'tab-2', type_='tab')
    assert mgr.tab_into(t2, w) is True
    assert mgr.dock_tab(t2, h) is True
    assert groups(impl)['lower'] == [h, c, t2, t1]  # right after control
    t3 = spawn(mgr, 'tab-3', type_='tab')
    assert mgr.tab_into(t3, w) is True
    assert mgr.dock_tab(t3, h, after=t2) is True
    assert groups(impl)['lower'] == [h, c, t2, t3, t1]  # right after the given one
    assert mgr.dock_tab('zzzz-1', h) is False


def test_dock_control_moves_daemon_never_kills_it(impl):
    mgr = new_mgr(impl)
    h = spawn(mgr, 'history', type_='history')
    t = spawn(mgr, 'tab-1', type_='tab')
    c = spawn(mgr, 'control', type_='daemon')
    w = spawn(mgr, 'worker-1')
    assert mgr.tab_into(c, w) is True and groups(impl)['workers'] == [c, w]
    pid = pane(impl, c)['pid']
    assert mgr.dock_control(h) is True
    assert groups(impl)['lower'] == [h, c, t] and pane(impl, c)['pid'] == pid
    assert mgr.dock_control(h) is True and groups(impl)['lower'] == [h, c, t]  # already in place
    assert mgr.dock_control('zzzz-1') is False
    assert pane(impl, c)['pid'] == pid and alive(pid)  # a failed dock leaves the daemon running


def test_place_right_of_moves_to_workers_group(impl):
    mgr = new_mgr(impl)
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    first = spawn(mgr, 'worker-1', type_='advisor')  # lands in the supervisor's group
    assert first in groups(impl)['supervisor']
    assert mgr.place_right_of(first, s) is True
    g = groups(impl)
    assert g['workers'][-1] == first and first not in g['supervisor']
    w2 = spawn(mgr, 'worker-2')
    before = groups(impl)
    assert mgr.place_right_of(w2, s) is True and groups(impl) == before  # already in another group
    assert mgr.place_right_of('zzzz-1', s) is False


def test_tab_into_before_inserts_successor_at_old_slot(impl):
    """design §3.2: spawn the next generation, tab_into(new, old), kill_pane(old) -> the old slot."""
    mgr = new_mgr(impl)
    w1 = spawn(mgr, 'worker-1')
    w2 = spawn(mgr, 'worker-2')
    w3 = spawn(mgr, 'worker-3')
    assert mgr.focus(w1) is True
    w11 = spawn(mgr, 'worker-1.1')
    assert groups(impl)['workers'] == [w1, w2, w3, w11]
    assert mgr.tab_into(w11, w1) is True
    assert groups(impl)['workers'] == [w11, w1, w2, w3]
    assert mgr.kill_pane(w1) is True
    assert groups(impl)['workers'] == [w11, w2, w3]
    assert ctl(impl, 'layout.get')['selected']['workers'] == w11  # the shown tab passes to its successor
    s1 = spawn(mgr, 'supervisor-1', type_='supervisor')
    adv = spawn(mgr, 'advisor-1', type_='advisor')
    s11 = spawn(mgr, 'supervisor-1.1', type_='supervisor')
    assert mgr.tab_into(s11, s1) is True and mgr.kill_pane(s1) is True
    assert groups(impl)['supervisor'] == [s11, adv]  # the advisor keeps its place
    t = spawn(mgr, 'tab-1', type_='tab')
    assert mgr.tab_into(t, w2, before=False) is True
    assert groups(impl)['workers'] == [w11, w2, w3, t]  # before=False: the end of the peer's group
    assert mgr.tab_into('zzzz-1', w2) is False


def test_tab_panes_lists_live_tabs_only(impl):
    mgr = new_mgr(impl)
    t1 = spawn(mgr, 'evermem-shell', type_='tab')
    t2 = spawn(mgr, 'scratch', type_='tab')
    spawn(mgr, 'worker-1')
    assert mgr.tab_panes() == {t1: 'evermem-shell', t2: 'scratch'}
    assert ctl(impl, 'pane.kill', pane=t2)['ok'] is True
    assert mgr.tab_panes() == {t1: 'evermem-shell'}


# ================================================================ browser cell (= history pane)

def test_browser_goto_false_browser_url_from_registry_reload_true_while_alive(impl):
    mgr = new_mgr(impl)
    run, url = make_run(impl)
    mgr.claim_run(str(run))
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    w = spawn(mgr, 'worker-1')
    hist = mgr.arrange(s, [w], url)
    assert hist
    assert mgr.browser_goto(hist, url + '?fleet-jump=child') is False  # nothing to navigate
    assert mgr.browser_url(hist) == url and 'fleet-jump' not in mgr.browser_url(hist)
    assert mgr.reload_browser(hist) is True  # history_view watches its file itself
    assert ctl(impl, 'pane.kill', pane=hist)['ok'] is True
    assert mgr.reload_browser(hist) is False
    assert mgr.browser_url('zzzz-1') is None


# ================================================================ handover

def test_release_pops_registry_entry(impl):
    mgr = new_mgr(impl)
    w = spawn(mgr, 'worker-1', label='worker-1')
    info = mgr.release(w)
    assert info and info['type'] == 'worker' and info['label'] == 'worker-1'
    assert w not in surfaces(impl) and mgr.owns(w) is False
    assert pane(impl, w) is not None  # released, not closed
    assert mgr.release(w) is None


def test_adopt_only_within_same_server(impl):
    mgr = new_mgr(impl)
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    w = spawn(mgr, 'worker-1', label='worker-1')
    info = mgr.release(w)
    other = fm.FleetmuxSessionManager('fm-other', state_dir=impl.state)
    s2 = spawn(other, 'supervisor-1', type_='supervisor')
    assert other.adopt(w, s2, info) is False  # another run's server: cannot take the pane
    assert w not in surfaces(impl, 'fm-other')
    assert mgr.adopt(w, s, info) is True
    assert mgr.owns(w) and surfaces(impl)[w]['label'] == 'worker-1'
    assert w in groups(impl)['supervisor']  # moved into the peer's group


# ================================================================ tmux views (as the wezterm backend)

def test_tmux_view_info_attach_view_reattach_detach(impl):
    mgr = new_mgr(impl)
    if not shutil.which('tmux'):
        pytest.skip('tmux not installed (deps.yaml requires it)')
    sock = impl.root / 'tmux' / 'ops.sock'
    sock.parent.mkdir()
    session = 'ops-g1'
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    assert tmux(sock, 'new-session', '-d', '-s', session, 'sleep 600').returncode == 0
    info = {'type': 'worker', 'label': 'ops', 'tmux_socket': str(sock), 'tmux_session': session}
    assert mgr.attach_view({'tmux_socket': str(sock), 'tmux_session': 'no-such'}, s) is None
    v = mgr.attach_view(info, s)
    assert v and PANE_ID.match(v)
    assert mgr.tmux_view_info(v) == info and mgr.tmux_session_alive(v) is True
    assert v in groups(impl)['supervisor']  # tab_into(view, peer)
    assert wait_for(lambda: tmux_clients(sock) == [pane(impl, v)['tty']])
    assert ctl(impl, 'pane.kill', pane=v)['ok'] is True  # its tab closed while the session runs on
    assert wait_for(lambda: tmux_clients(sock) == [])
    v2 = mgr.reattach_client(v, s)
    assert v2 and v2 != v
    assert mgr.tmux_view_info(v) is None and mgr.tmux_view_info(v2)['tmux_session'] == session
    assert surfaces(impl)[v2]['label'] == 'ops'
    assert wait_for(lambda: tmux_clients(sock) == [pane(impl, v2)['tty']])
    assert mgr.detach_view(v2) is True
    assert wait_for(lambda: tmux_clients(sock) == []) and pane(impl, v2) is None and v2 not in surfaces(impl)
    assert tmux(sock, 'has-session', '-t', '=' + session).returncode == 0  # the session keeps running
    v3 = mgr.attach_view(info, s)
    assert v3
    tmux(sock, 'kill-server')
    assert wait_for(lambda: mgr.tmux_session_alive(v3) is False)
    assert mgr.tmux_session_alive('zzzz-1') is None


# ================================================================ M1 / M2 additions (brief-m03 §3.5)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fleetmux_milestone import milestone  # noqa: E402


@milestone(1)
def test_arrange_sets_history_url_kv_for_prefix_o(impl):
    """``C-b o`` opens the server's kv ``history_url`` (PROTOCOL.md §10): arrange is what sets it."""
    mgr = new_mgr(impl)
    run, url = make_run(impl)
    mgr.claim_run(str(run))
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    w = spawn(mgr, 'worker-1')
    assert ctl(impl, 'kv.get', key='history_url')['value'] is None
    assert mgr.arrange(s, [w], url)
    assert ctl(impl, 'kv.get', key='history_url')['value'] == url


@milestone(2)
def test_arrange_after_server_crash_rebuilds_page(impl):
    """``kill -9`` of the server, then the resume path: a new manager sees no session, starts a new server
    and ``arrange`` builds a new history pane; the registry forgets every pane of the dead server."""
    mgr = new_mgr(impl)
    run, url = make_run(impl)
    mgr.claim_run(str(run))
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    w = spawn(mgr, 'worker-1')
    hist = mgr.arrange(s, [w], url)
    assert hist
    old = {s, w, hist}
    assert old <= set(surfaces(impl))
    pid = _read_json(runtime_of(impl) / 'server.json')['pid']
    os.kill(int(pid), signal.SIGKILL)
    assert wait_for(lambda: not alive(int(pid)) and not server_up(impl))
    mgr = new_mgr(impl)
    assert mgr.session_exists() is False
    assert mgr.get_or_create_session() is True and server_up(impl)
    mgr.claim_run(str(run))
    s2 = spawn(mgr, 'supervisor-1', type_='supervisor')
    w2 = spawn(mgr, 'worker-1')
    hist2 = mgr.arrange(s2, [w2], url)
    assert hist2 and PANE_ID.match(hist2) and hist2 not in old
    rows = {p['pane']: p for p in panes(impl)}
    assert rows[hist2]['type'] == 'history' and not old & set(rows)
    assert not old & set(surfaces(impl)) and surfaces(impl)[hist2]['url'] == url


# ---------------------------------------------------------------- M0 additions (reviewer-1 coverage-a G4)

def test_workspace_id_select_keep_open_place_after_are_local(impl):
    """design §4.3: no workspaces in fleetmux. ``workspace_id`` is ``fleetmux:<token>`` computed locally from
    the session, ``select_workspace`` accepts only that id, ``keep_windows_open`` and ``place_after`` are
    always True. None of the four needs, or starts, a server."""
    mgr = new_mgr(impl)
    own = 'fleetmux:' + token(SESSION)
    assert mgr.workspace_id() == own
    assert new_mgr(impl).workspace_id() == own  # a fresh manager on the same (state_dir, session) agrees
    odd = 'fm test/é'
    assert new_mgr(impl, odd).workspace_id() == 'fleetmux:' + token(odd) == 'fleetmux:fm_test__'
    assert mgr.select_workspace(own) is True
    for other in ('fleetmux:' + token(odd), 'fleetmux:', '', token(SESSION), 'workspace:1'):
        assert mgr.select_workspace(other) is False, other
    assert mgr.keep_windows_open([]) is True
    assert mgr.keep_windows_open(['surface:1', own]) is True
    for ws in (None, own, 'workspace:7'):
        assert mgr.place_after(ws) is True, ws
    assert not server_up(impl) and not runtime_of(impl).exists()  # all local: nothing was started


# ---------------------------------------------------------------- M0 additions (fix-m0-4, operator P1)

def _attach_top_row(impl, cols=240, rows=60):
    """A real ``fleetmux attach`` on a pty of ``cols`` x ``rows``; returns (viewer pid, top_row_after(seconds))."""
    import fcntl
    import pty
    import select
    import struct
    import termios

    import pyte
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(str(impl.binary), [str(impl.binary), 'attach', '--runtime', str(runtime_of(impl))])
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)

    def top_row_after(seconds=1.5):
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    stream.feed(os.read(fd, 65536))
                except OSError:
                    break
        return screen.display[0]
    return pid, top_row_after


def test_supervisor_rotation_label_names_new_generation(impl, monkeypatch):
    """fix-m0-4 (design §3.1 / §3.2): after ``spawn`` next generation -> ``tab_into(new, old)`` -> ``kill_pane(old)``
    the successors keep their slots and the selection, and the attached screen's upper-left label shows the full
    ``fx-supervisor-1.1`` (17 characters), as the worker label shows ``fx-worker-1.1``. The fake server renders no
    view, so the screen half runs on [real] only."""
    # The tab labels are on the top row only without a header band: a Linux server reserves one row for the GTK
    # headerbar (A26 rev7) unless told not to, like the Rust grid-chrome fixtures (tests/common Server::start).
    monkeypatch.setenv('FLEETMUX_NATIVE_HEADER', '0')
    mgr = new_mgr(impl)
    run, url = make_run(impl)
    mgr.claim_run(str(run))
    s1 = spawn(mgr, 'fx-supervisor-1', type_='supervisor')
    w1 = spawn(mgr, 'fx-worker-1')
    w2 = spawn(mgr, 'fx-worker-2')
    assert mgr.arrange(s1, [w1, w2], url)
    viewer = None
    if impl.kind == 'real':
        viewer, top = _attach_top_row(impl)
        assert 'fx-supervisor-1' in top()
    try:
        s11 = spawn(mgr, 'fx-supervisor-1.1', type_='supervisor')
        assert mgr.tab_into(s11, s1) is True and mgr.kill_pane(s1) is True
        w11 = spawn(mgr, 'fx-worker-1.1')
        assert mgr.tab_into(w11, w1) is True and mgr.kill_pane(w1) is True
        assert pane(impl, s11)['title'] == 'fx-supervisor-1.1' and pane(impl, w11)['title'] == 'fx-worker-1.1'
        assert groups(impl)['supervisor'] == [s11] and groups(impl)['workers'] == [w11, w2]
        assert ctl(impl, 'layout.get')['selected']['supervisor'] == s11
        if impl.kind == 'real':
            row = top()
            assert 'fx-worker-1.1' in row, row
            assert 'fx-supervisor-1.1' in row, row  # the full new-generation label, never cut to fx-supervisor-1
    finally:
        if viewer:
            os.kill(viewer, signal.SIGKILL)
            os.waitpid(viewer, 0)


# ================================================================ M2.5 web window (brief-m25 §3.1 W0; green in W4)

class _WebWindow:
    """Stands in for an attach client whose web window is open (rust/PROTOCOL.md §6.3 ``0x84`` / ``0x04``):
    one view connection that reports ``{"event":"window","state":"open"}`` and answers every ``0x84`` the way a
    window would (``url`` = the last URL it was sent). It never raises: when the server refuses the view
    connection or the frames, ``error`` says why and the backend simply sees no window."""

    def __init__(self, env, url):
        import struct
        import threading
        self.struct, self.url, self.ops, self.error = struct, url, [], None
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(10)
            self.sock.connect(str(runtime_of(env) / 'ctl.sock'))
            hello = {'view': 1, 'cols': 80, 'rows': 24, 'term': 'xterm-256color', 'colorterm': 'truecolor'}
            self.sock.sendall((json.dumps(hello) + '\n').encode())
            line = b''
            while not line.endswith(b'\n'):
                chunk = self.sock.recv(1)
                if not chunk:
                    raise OSError('view handshake: connection closed')
                line += chunk
            if json.loads(line).get('ok') is not True:
                raise OSError('view handshake refused: %r' % line)
            self.sock.settimeout(None)
            self._send({'event': 'window', 'state': 'open'})
            threading.Thread(target=self._serve, daemon=True).start()
            time.sleep(0.3)  # the server reads the event before the manager's control request arrives
        except (OSError, ValueError) as exc:
            self.error = str(exc)

    def _send(self, obj):
        payload = json.dumps(obj).encode()
        self.sock.sendall(self.struct.pack('>BI', 0x04, len(payload)) + payload)

    def _read(self, n):
        out = b''
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise OSError('closed')
            out += chunk
        return out

    def _serve(self):
        try:
            while True:
                ty, n = self.struct.unpack('>BI', self._read(5))
                payload = self._read(n)
                if ty != 0x84:
                    continue
                cmd = json.loads(payload)
                op = cmd.get('op')
                self.ops.append((op, cmd.get('url')))
                if op == 'page':
                    continue
                if op in ('open', 'goto') and cmd.get('url'):
                    self.url = cmd['url']
                reply = {'seq': cmd.get('seq'), 'ok': True}
                if op == 'url':
                    reply['url'] = self.url
                self._send(reply)
        except (OSError, ValueError) as exc:
            self.error = self.error or str(exc)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


@milestone(3)
def test_browser_goto_url_reload_via_web_commands_when_window_present(impl):
    """With a window on some viewer, the three browser methods go through ``web.goto`` / ``web.url`` /
    ``web.reload`` (design §4.3) and report what the window did, not the registry's view."""
    mgr = new_mgr(impl)
    run, url = make_run(impl)
    mgr.claim_run(str(run))
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    w = spawn(mgr, 'worker-1')
    hist = mgr.arrange(s, [w], url)
    assert hist
    win = _WebWindow(impl, url)
    try:
        jump = url + '?fleet-jump=child'
        assert mgr.browser_goto(hist, jump) is True, win.error
        assert ('goto', jump) in win.ops
        assert mgr.browser_url(hist) == jump  # the window's URL, not the registry's
        assert mgr.reload_browser(hist) is True
        assert ('reload', None) in win.ops
    finally:
        win.close()


@milestone(3)
def test_browser_fallbacks_unchanged_without_window(impl):
    """No window anywhere (no viewer, or a viewer that never reported one): the server says ``no_window`` and the
    three methods return exactly what they return today (= the unchanged test above)."""
    mgr = new_mgr(impl)
    run, url = make_run(impl)
    mgr.claim_run(str(run))
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    w = spawn(mgr, 'worker-1')
    hist = mgr.arrange(s, [w], url)
    assert hist
    reply = ctl(impl, 'web.url')
    assert (reply.get('ok'), reply.get('code')) == (False, 'no_window'), reply
    assert ctl(impl, 'web.goto', url=url).get('code') == 'no_window'
    assert mgr.browser_goto(hist, url + '?fleet-jump=child') is False
    assert mgr.browser_url(hist) == url
    assert mgr.reload_browser(hist) is True


class _SilentWebWindow(_WebWindow):
    """A viewer that reports a window but never answers a relayed ``0x84`` (a hung ``fleetmux-web``)."""

    def _serve(self):
        try:
            while True:
                ty, n = self.struct.unpack('>BI', self._read(5))
                payload = self._read(n)
                if ty == 0x84:
                    self.ops.append((json.loads(payload).get('op'), None))
        except (OSError, ValueError) as exc:
            self.error = self.error or str(exc)


@milestone(3)
def test_browser_methods_fall_back_when_the_window_does_not_answer(impl):
    """W4 (brief-m25 §3.5): a window that does not answer within 3 s is ``web_timeout``; the three methods then
    return today's values and never raise."""
    mgr = new_mgr(impl)
    run, url = make_run(impl)
    mgr.claim_run(str(run))
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    w = spawn(mgr, 'worker-1')
    hist = mgr.arrange(s, [w], url)
    assert hist
    win = _SilentWebWindow(impl, url)
    try:
        assert win.error is None, win.error
        t0 = time.monotonic()
        assert ctl(impl, 'web.url').get('code') == 'web_timeout'
        assert 2.5 <= time.monotonic() - t0 < 10
        assert mgr.browser_goto(hist, url + '?fleet-jump=child') is False
        assert mgr.browser_url(hist) == url
        assert mgr.reload_browser(hist) is True
        assert ('goto', None) in win.ops and ('url', None) in win.ops and ('reload', None) in win.ops
        assert ctl(impl, 'web.eval', js='1').get('code') == 'not_implemented'
    finally:
        win.close()


@milestone(3)
def test_web_page_exchange_sends_bytes_only_when_the_copy_differs(impl):
    """PROTOCOL.md §4.21 / §6.3: ``{"op":"page","path","have"}`` gets the page with ``b64`` when the viewer's copy
    differs (or it has none), without ``b64`` when ``have`` matches the server's size / mtime."""
    import base64
    import struct
    mgr = new_mgr(impl)
    run, url = make_run(impl)
    mgr.claim_run(str(run))
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    assert mgr.arrange(s, [], url)
    path = url[len('file://'):]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(10)
    try:
        sock.connect(str(runtime_of(impl) / 'ctl.sock'))
        sock.sendall((json.dumps({'view': 1, 'cols': 80, 'rows': 24, 'term': 'xterm-256color',
                                  'colorterm': 'truecolor'}) + '\n').encode())
        rfile = sock.makefile('rb')
        assert json.loads(rfile.readline()).get('ok') is True

        def ask(have):
            payload = json.dumps({'seq': 7, 'op': 'page', 'path': path, 'have': have}).encode()
            sock.sendall(struct.pack('>BI', 0x04, len(payload)) + payload)
            while True:
                ty, n = struct.unpack('>BI', rfile.read(5))
                body = rfile.read(n)
                if ty == 0x84:
                    return json.loads(body)

        first = ask(None)
        page = first['page']
        assert first['op'] == 'page' and first.get('seq') == 7 and page['path'] == path
        assert base64.b64decode(page['b64']) == Path(path).read_bytes()
        assert (page['size'], page['mtime']) == (os.stat(path).st_size, os.stat(path).st_mtime_ns)
        again = ask({'size': page['size'], 'mtime': page['mtime']})['page']
        assert 'b64' not in again and (again['size'], again['mtime']) == (page['size'], page['mtime'])
    finally:
        sock.close()


# ---------------------------------------------------------------- S2b: keep the server for the sidebar's ↺
def test_close_panes_keeps_the_server_and_clears_the_panes(impl):
    mgr = new_mgr(impl)
    sup = spawn(mgr, 'supervisor', type_='supervisor')
    w = spawn(mgr, 'worker-1')
    pids = [pane(impl, x)['pid'] for x in (sup, w)]
    srv = _read_json(runtime_of(impl) / 'server.json')['pid']
    assert mgr.close_panes() is True
    assert wait_for(lambda: not any(alive(p) for p in pids)), 'a pane process outlived close_panes'
    assert alive(srv) and server_up(impl), 'close_panes must keep the fleetmux server for the sidebar ↺'
    assert mgr.session_exists() is False
    assert surfaces(impl) == {}


# ---------------------------------------------------------------- S1c: bash tabs are no fleet agents
def _shell_row(pane_id='zzzz-1', title='bash-1'):
    """A ``pane.list`` row for a sidebar bash (S1c, plan §14.3; user 2026-11-09: a tab of the root page's worker
    area that the root fleet registers, but no agent)."""
    return {'pane': pane_id, 'title': title, 'type': 'shell', 'group': 'workers', 'index': 0}


def test_session_exists_ignores_shell_panes(impl, monkeypatch):
    """A server holding only bash pages is not a live fleet session (plan §14.5-7): the closer's
    ``all_gone`` must not wait on the server while a bash page is still open."""
    mgr = new_mgr(impl)
    assert mgr.get_or_create_session() is True
    assert server_up(impl)
    monkeypatch.setattr(mgr, '_list', lambda: [_shell_row()])
    assert mgr.session_exists() is False, (
        'session_exists() must be False for pane.list == [%r] (only a type "shell" bash page)'
        % (_shell_row(),))


def test_close_panes_keeps_a_shell_pane_and_the_server(impl, monkeypatch):
    """Closing the fleet kills its own panes but never a bash page with a program in the foreground; the
    server stays for both the sidebar's ↺ and the bash page (plan §14.5-7)."""
    mgr = new_mgr(impl)
    w = spawn(mgr, 'worker-1')
    sh = spawn(mgr, 'bash-1', command=BUSY_SHELL_CMD, type_='shell')
    wait_busy(impl, sh)
    worker_pid = pane(impl, w)['pid']
    shell_pid = pane(impl, sh)['pid']
    srv = _read_json(runtime_of(impl) / 'server.json')['pid']
    real_list = mgr._list
    monkeypatch.setattr(mgr, '_list', lambda: real_list() + [_shell_row('zzzz-2', 'bash-2')])
    assert mgr.close_panes() is True
    assert wait_for(lambda: not alive(worker_pid)), (
        'the fleet worker pane (pid %d) outlived close_panes' % worker_pid)
    assert alive(shell_pid), (
        'the bash page (pid %d, type "shell") was killed by close_panes' % shell_pid)
    assert alive(srv) and server_up(impl), (
        'the fleetmux server (pid %d) must stay up after close_panes while a bash page lives' % srv)
    assert mgr.session_exists() is False, (
        'session_exists() must be False when only the bash page (pid %d) is left' % shell_pid)


def test_close_all_keeps_the_server_while_a_shell_pane_lives(impl, monkeypatch):
    """``close_all`` stops the fleet, then leaves the server running while a busy bash page lives
    (plan §14.5-7): the server exits by itself per §13.4 once the page is gone."""
    mgr = new_mgr(impl)
    w = spawn(mgr, 'worker-1')
    sh = spawn(mgr, 'bash-1', command=BUSY_SHELL_CMD, type_='shell')
    wait_busy(impl, sh)
    worker_pid = pane(impl, w)['pid']
    shell_pid = pane(impl, sh)['pid']
    srv = _read_json(runtime_of(impl) / 'server.json')['pid']
    real_list = mgr._list
    monkeypatch.setattr(mgr, '_list', lambda: real_list() + [_shell_row('zzzz-2', 'bash-2')])
    assert mgr.close_all() is True
    assert wait_for(lambda: not alive(worker_pid)), (
        'the fleet worker pane (pid %d) outlived close_all' % worker_pid)
    assert alive(shell_pid), (
        'the bash page (pid %d, type "shell") was killed by close_all' % shell_pid)
    assert alive(srv) and server_up(impl), (
        'close_all ended the fleetmux server (pid %d) while the bash page (pid %d) still lives '
        '(plan §14.5-7: the server keeps running)' % (srv, shell_pid))
    assert mgr.session_exists() is False, (
        'session_exists() must be False when only the bash page (pid %d) is left' % shell_pid)


# ---------------------------------------------------------------- user 2026-11-09: every sidebar bash is mapped into
# the main fleet's worker area and managed by it -- the root manager registers it (``type: shell``), keeps it in the
# root page's ``workers`` group behind the workers, the history page lists it; it is no agent (no slot, no work).
def test_adopt_shells_registers_a_root_bash_tab_into_the_workers_group(impl):
    mgr = new_mgr(impl)
    w = spawn(mgr, 'worker-1')
    sh = bash_tab(impl, ['sleep', '600'], group='lower')['pane']  # anywhere on the root page
    assert sh not in surfaces(impl)
    assert mgr.adopt_shells() is True, mgr.last_error
    entry = surfaces(impl).get(sh)
    assert entry and entry.get('type') == 'shell' and entry.get('label') == 'bash-1', surfaces(impl)
    assert groups(impl)['workers'] == [w, sh], groups(impl)
    assert mgr.adopt_shells() is False, 'a second pass with nothing new must report no change'
    assert mgr.shell_slots() == [(sh, 'bash-1', 'bash-1')], mgr.shell_slots()
    assert ctl(impl, 'pane.kill', pane=sh)['ok']
    assert wait_for(lambda: pane(impl, sh) is None)
    assert mgr.adopt_shells() is True, 'a bash that went away must be forgotten'
    assert sh not in surfaces(impl) and mgr.shell_slots() == [], surfaces(impl)
    assert w in surfaces(impl), 'adopt_shells touched a worker entry'


def test_arrange_adopts_bash_tabs_behind_the_workers(impl):
    """A bash opened before the fleet came up (no fleet running) is taken in when the next fleet arranges its page."""
    mgr = new_mgr(impl)
    run, url = make_run(impl)
    mgr.claim_run(str(run))
    s = spawn(mgr, 'supervisor-1', type_='supervisor')
    sh = bash_tab(impl, ['sleep', '600'])['pane']
    w1 = spawn(mgr, 'worker-1')
    w2 = spawn(mgr, 'worker-2')
    assert mgr.arrange(s, [w1, w2], url)
    assert groups(impl)['workers'] == [w1, w2, sh], groups(impl)
    assert surfaces(impl).get(sh, {}).get('type') == 'shell', surfaces(impl)


def test_close_panes_forgets_the_idle_bash_it_closes_and_keeps_the_busy_one(impl):
    """Registered bash tabs: the idle one is closed and leaves the registry, the busy one is kept (and stays
    registered, the next fleet's page shows it)."""
    mgr = new_mgr(impl)
    worker_pid = pane(impl, spawn(mgr, 'worker-1'))['pid']
    idle = bash_tab(impl, ['sleep', '600'], title='bash-1')
    busy = bash_tab(impl, BUSY_SHELL, title='bash-2')
    assert mgr.adopt_shells() is True
    assert mgr.close_panes() is True, mgr.last_error
    assert wait_for(lambda: not alive(worker_pid))
    assert wait_for(lambda: not alive(idle['pid'])), 'the idle bash tab outlived close_panes'
    assert alive(busy['pid']), 'close_panes killed the registered bash tab running a program'
    assert idle['pane'] not in surfaces(impl), 'the closed idle bash stayed in the registry'
    assert busy['pane'] in surfaces(impl), 'the kept busy bash left the registry'
    assert mgr.kept_bash_pages == ['bash-2'], mgr.kept_bash_pages


# ---------------------------------------------------------------- closing a fleet closes its idle bash pages
# (user 2026-11-08): a bash page whose shell sits at its prompt (PTY foreground group == the shell's pgid, pane
# record ``fg_pgid``) goes with the fleet; one with a program in the foreground stays, and so does the server.
BUSY_SHELL = ['bash', '-c', 'set -m; sleep 600; :']  # job control: the sleep gets a foreground group of its own
BUSY_SHELL_CMD = shlex.join(BUSY_SHELL)


def bash_tab(env, argv, title='bash-1', group='workers'):
    """A sidebar bash as the server makes it (user 2026-11-09): a ``shell`` tab in the root page's ``workers``
    group, not yet in the fleet's registry (the daemon's next tick or ``arrange`` takes it in)."""
    reply = ctl(env, 'pane.spawn', argv=argv, type='shell', title=title, group=group)
    assert reply['ok'], reply
    if argv is BUSY_SHELL:
        wait_busy(env, reply['pane'])
    return reply


def wait_busy(env, pane_id):
    """Until the busy shell's job runs on its PTY (it holds the terminal from before its exec)."""
    row = pane(env, pane_id)
    assert wait_for(lambda: 'sleep' in tty_comms(row['tty'])), 'the bash tab never started its job'


def server_gone(env, srv_pid) -> bool:
    return wait_for(lambda: not alive(srv_pid) and not server_up(env), timeout=5.0)


def test_close_all_closes_idle_bash_pages_and_ends_the_server(impl):
    mgr = new_mgr(impl)
    worker_pid = pane(impl, spawn(mgr, 'worker-1'))['pid']
    srv = _read_json(runtime_of(impl) / 'server.json')['pid']
    shell_pid = bash_tab(impl, ['sleep', '600'])['pid']
    assert mgr.close_all() is True, mgr.last_error
    assert wait_for(lambda: not alive(worker_pid)), 'the fleet worker pane outlived close_all'
    assert wait_for(lambda: not alive(shell_pid)), (
        'the idle bash page (pid %d) outlived close_all' % shell_pid)
    assert server_gone(impl, srv), (
        'close_all left the fleetmux server (pid %d) running with no bash page left' % srv)


def test_close_all_keeps_a_busy_bash_page_and_the_server(impl):
    mgr = new_mgr(impl)
    worker_pid = pane(impl, spawn(mgr, 'worker-1'))['pid']
    srv = _read_json(runtime_of(impl) / 'server.json')['pid']
    idle_pid = bash_tab(impl, ['sleep', '600'], title='bash-1')['pid']
    busy_pid = bash_tab(impl, BUSY_SHELL, title='bash-2')['pid']
    assert mgr.close_all() is True, mgr.last_error
    assert wait_for(lambda: not alive(worker_pid))
    assert wait_for(lambda: not alive(idle_pid)), 'the idle bash page (pid %d) outlived close_all' % idle_pid
    assert alive(busy_pid), 'close_all killed the bash page running a program (pid %d)' % busy_pid
    assert alive(srv) and server_up(impl), 'close_all ended the server while a busy bash page lives'
    assert mgr.kept_bash_pages == ['bash-2'], mgr.kept_bash_pages


def test_close_panes_closes_idle_bash_pages_and_keeps_busy_ones(impl):
    """The sidebar × path (↺ later): idle bash pages go too, a busy one stays, the server always stays."""
    mgr = new_mgr(impl)
    worker_pid = pane(impl, spawn(mgr, 'worker-1'))['pid']
    srv = _read_json(runtime_of(impl) / 'server.json')['pid']
    idle_pid = bash_tab(impl, ['sleep', '600'], title='bash-1')['pid']
    busy_pid = bash_tab(impl, BUSY_SHELL, title='bash-2')['pid']
    assert mgr.close_panes() is True, mgr.last_error
    assert wait_for(lambda: not alive(worker_pid))
    assert wait_for(lambda: not alive(idle_pid)), 'the idle bash page (pid %d) outlived close_panes' % idle_pid
    assert alive(busy_pid), 'close_panes killed the bash page running a program (pid %d)' % busy_pid
    assert alive(srv) and server_up(impl), 'close_panes must keep the server for the sidebar ↺'
    assert mgr.kept_bash_pages == ['bash-2'], mgr.kept_bash_pages


def test_close_keeps_a_bash_page_whose_row_has_no_fg_pgid(impl, monkeypatch):
    """An older server sends no ``fg_pgid``: the bash page cannot be told idle, so it is kept (never guessed idle),
    and with it the server."""
    mgr = new_mgr(impl)
    spawn(mgr, 'worker-1')
    srv = _read_json(runtime_of(impl) / 'server.json')['pid']
    shell_pid = bash_tab(impl, ['sleep', '600'])['pid']
    real_call = mgr._call

    def old_server(cmd, **args):
        reply = real_call(cmd, **args)
        for row in [reply] + list(reply.get('panes') or []):
            if isinstance(row, dict):
                row.pop('fg_pgid', None)
        return reply

    monkeypatch.setattr(mgr, '_call', old_server)
    assert mgr.close_all() is True, mgr.last_error
    assert alive(shell_pid), 'a bash page without fg_pgid (pid %d) was killed' % shell_pid
    assert alive(srv) and server_up(impl)
    assert mgr.kept_bash_pages == ['bash-1'], mgr.kept_bash_pages


def test_pane_record_reports_the_foreground_group(impl):
    """``fg_pgid`` (PROTOCOL.md §4.0): the shell's own pgid at its prompt, the job's group while one runs."""
    mgr = new_mgr(impl)
    assert mgr.get_or_create_session() is True
    idle = bash_tab(impl, ['sleep', '600'], title='bash-1')
    busy = bash_tab(impl, BUSY_SHELL, title='bash-2')
    assert pane(impl, idle['pane']).get('fg_pgid') == idle['pgid']
    fg = pane(impl, busy['pane']).get('fg_pgid')
    assert isinstance(fg, int) and fg != busy['pgid'], pane(impl, busy['pane'])
    rows = {r['pane']: r for r in ctl(impl, 'pane.list', all=True)['panes']}
    assert rows[idle['pane']].get('fg_pgid') == idle['pgid'], rows[idle['pane']]


# ---------------------------------------------------------------- S1c docs: bash pages (plan §14 / §14.5)
def _doc(*parts):
    return ROOT.joinpath(*parts).read_text(encoding='utf-8')


def _lines_with(text, needle):
    return '\n'.join(line for line in text.splitlines() if needle in line) or '(no line with %r)' % needle


def test_protocol_1_5_documents_the_shell_pane_type():
    """Plan §14.3 / §22.2; user 2026-11-09: the `shell` pane type is the sidebar's bash, a tab of the root page's
    `workers` group that the root fleet registers but never treats as a worker."""
    proto = _doc('rust', 'PROTOCOL.md')
    start = proto.index('### 1.5')
    table = proto[start:proto.index('## 2.', start)]
    row = [line for line in table.splitlines() if line.startswith('| pane type |')]
    assert row, 'PROTOCOL §1.5 must keep a pane type row; the table was:\n' + table
    assert '`shell`' in row[0], (
        'the PROTOCOL §1.5 pane type row must list `shell` (plan §14.3); it reads:\n' + row[0])
    assert 'server' in row[0] and 'registry' in row[0] and '`workers`' in row[0], (
        'the `shell` type must say the server spawns the pane into the `workers` group and the fleet registers it; it '
        'reads:\n' + row[0])
    for gone in ("never in the fleet's registry", 'alone on a page of its own', '_bash-'):
        assert gone not in row[0], (
            'user 2026-11-09 voids %r: every new bash is mapped into the main fleet\'s worker area; it reads:\n%s'
            % (gone, row[0]))
    assert 'bash' in row[0].lower(), (
        'the `shell` type must say it is the sidebar bash page (plan §14.2); it reads:\n' + row[0])


def test_the_readme_documents_bash_pages():
    """Plan §14.2 / §14.5-6 / §14.5-7: the README's bash-page contract (entry points, ownership, cards, each bash
    on a page of its own (user 2026-11-08), close with the foreground check, Python cleanup, the shell-only
    placeholder) and S3 still for switching."""
    readme = _doc('README.md')
    assert '新建 bash：S1c 开放' not in readme, (
        'README still carries the S1b placeholder notice:\n' + _lines_with(readme, 'S1c 开放'))
    assert '* **Bash pages (S1c)**' in readme, (
        'README must have a `* **Bash pages (S1c)**` bullet (plan §14):\n' + _lines_with(readme, 'S1c'))
    start = readme.index('* **Bash pages (S1c)**')
    bullet = readme[start:readme.index('\n* **Keys**', start)]
    for needle in ('`+`', 'double click', '`C-b c`', '300 ms', '`$SHELL -l`', 'type `shell`', 'root page',
                   '`workers` group', "fleet's registry", 'no worker',
                   'bash-<n>', '终端 · 运行中', '`×`', 'foreground process group',
                   '关闭 bash 页', 'session_exists', 'close_panes', 'close_all', 'no pane other than `shell`',
                   '切换 fleet：S3 开放'):
        assert needle in bullet, (
            'the README bash-pages bullet must state %r (plan §14.2 / §14.5-6/7); it reads:\n%s' % (needle, bullet))
    for gone in ('a page of its own', '`_bash-<pane id>`', "never in the fleet's registry"):
        assert gone not in bullet, (
            'user 2026-11-09 voids %r (every new bash is mapped into the main fleet\'s worker area); it reads:\n%s'
            % (gone, bullet))
