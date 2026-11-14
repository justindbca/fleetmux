"""`fleetmux setup --install`: finds what a fleet needs, plans apt / brew / Node.js tarball / npm steps for what is
missing, never touches what is there, asks first, and links node and pi beside the `fleetmux` command.
Also: `start` names the command when binaries are missing, and fleet commands on a fresh home say so cleanly."""
import hashlib
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
from fleetmux import setup_install  # noqa: E402


def _exe(d, name, out):
    p = d / name
    p.write_text('#!/bin/sh\necho "%s"\n' % out)
    p.chmod(0o755)
    return p


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    (tmp_path / 'home').mkdir()
    return tmp_path / 'home'


def test_packaged_deps_equal_the_repository_deps():
    assert setup_install.DEPS_FILE.read_bytes() == (ROOT / 'deps.yaml').read_bytes()
    assert setup_install.deps()['agent_installs']['pi']['npm'] == '@earendil-works/pi-coding-agent'


def test_check_reports_ok_missing_and_old(tmp_path):
    b = tmp_path / 'bin'
    b.mkdir()
    _exe(b, 'redis-server', 'Redis server v=7.2.4 sha=0')
    _exe(b, 'git', 'git version 2.43.0')
    _exe(b, 'node', 'v18.19.1')
    rows = {r['name']: r for r in setup_install.check(str(b))}
    assert rows['redis-server']['state'] == 'ok' and rows['redis-server']['version'] == '7.2.4'
    assert rows['git']['state'] == 'ok'
    assert rows['tmux']['state'] == 'missing' and rows['tmux']['path'] is None
    assert rows['node']['state'] == 'old' and rows['node']['version'] == '18.19.1'
    assert rows['pi']['state'] == 'missing'


def _rows(**states):
    return [{'name': n, 'path': None if states.get(n, 'ok') == 'missing' else '/x/' + n, 'version': '1.0.0',
             'min': '22.0.0', 'state': states.get(n, 'ok')} for n in setup_install.NEEDED]


def test_plan_apt_brew_and_no_manager():
    rows = _rows(**{'redis-server': 'missing', 'tmux': 'missing', 'node': 'missing', 'pi': 'missing'})
    steps, notes = setup_install.plan(rows, 'apt', Path('/b'))
    assert steps == [('system', ['sudo', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y', 'redis-server', 'tmux']), ('node',), ('pi',)]
    assert notes == []
    steps, _ = setup_install.plan(rows, 'brew', Path('/b'))
    assert steps[0] == ('system', ['brew', 'install', 'redis', 'tmux'])
    steps, notes = setup_install.plan(rows, None, Path('/b'))
    assert steps == [('node',), ('pi',)] and 'redis-server, tmux' in notes[0]


def test_plan_never_touches_an_old_program():
    steps, notes = setup_install.plan(_rows(node='old', pi='missing', git='old'), 'apt', Path('/b'))
    assert steps == []
    assert any(n.startswith('node ') and 'not changed' in n for n in notes)
    assert any(n.startswith('git ') for n in notes)
    assert any('pi is not installed' in n for n in notes)


def test_nothing_missing_installs_nothing(tmp_path):
    sysdir = tmp_path / 'sys'
    sysdir.mkdir()
    for n, v in (('redis-server', 'v=7.0.15'), ('git', '2.43.0'), ('tmux', 'tmux 3.4'), ('node', 'v22.1.0'),
                 ('pi', '1.5.0')):
        _exe(sysdir, n, v)
    out = io.StringIO()
    code = setup_install.main(search=str(sysdir), mgr='apt', bin_dir=tmp_path / 'bin',
                              run=lambda *a, **k: pytest.fail('ran a command'), get=lambda url: pytest.fail('downloaded'),
                              tty=False, out=out)
    assert code == 0 and 'nothing to install' in out.getvalue()


def test_without_a_terminal_and_without_yes_it_only_prints_the_plan(tmp_path, home):
    calls = []
    out = io.StringIO()
    code = setup_install.main(search=str(tmp_path / 'empty'), mgr='apt', bin_dir=tmp_path / 'bin',
                              run=lambda *a, **k: calls.append(a), get=lambda url: pytest.fail('downloaded'),
                              tty=False, out=out)
    assert code == 1 and calls == []
    text = out.getvalue()
    assert 'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y redis-server git tmux' in text and '--yes' in text
    assert not (tmp_path / 'bin').exists()


def test_node_asset_picks_the_machine_tarball():
    sums = ('aa  node-v22.23.5-darwin-arm64.tar.gz\nbb  node-v22.23.5-linux-arm64.tar.gz\n'
            'cc  node-v22.23.5-linux-arm64.tar.xz\ndd  node-v22.23.5-linux-x64.tar.gz\n')
    assert setup_install.node_asset(sums, 'Linux', 'aarch64') == ('node-v22.23.5-linux-arm64.tar.gz', 'bb')
    assert setup_install.node_asset(sums, 'Linux', 'x86_64') == ('node-v22.23.5-linux-x64.tar.gz', 'dd')
    assert setup_install.node_asset(sums, 'Darwin', 'arm64')[0] == 'node-v22.23.5-darwin-arm64.tar.gz'


def _node_tarball(name):
    buf = io.BytesIO()
    top = name[:-len('.tar.gz')]
    with tarfile.open(fileobj=buf, mode='w:gz') as t:
        for exe in ('node', 'npm', 'npx'):
            data = b'#!/bin/sh\necho v22.23.5\n'
            info = tarfile.TarInfo('%s/bin/%s' % (top, exe))
            info.size, info.mode = len(data), 0o755
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_yes_installs_node_and_pi_and_links_them_beside_fleetmux(tmp_path, home):
    name = 'node-v22.23.5-%s-%s.tar.gz' % ({'darwin': 'darwin', 'linux': 'linux'}[sys.platform],
                                            {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x64'}[os.uname().machine])
    tarball = _node_tarball(name)
    files = {'SHASUMS256.txt': ('%s  %s\n' % (hashlib.sha256(tarball).hexdigest(), name)).encode(), name: tarball}
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'npx').write_text('not ours')
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if argv[0].endswith('/npm'):
            prefix = Path(argv[argv.index('--prefix') + 1])
            (prefix / 'bin').mkdir(parents=True)
            _exe(prefix / 'bin', 'pi', '1.5.0')
        return subprocess.CompletedProcess(argv, 0)

    search = str(tmp_path / 'sys')
    (tmp_path / 'sys').mkdir()
    for n, v in (('redis-server', 'v=7.0.15'), ('git', '2.43.0'), ('tmux', 'tmux 3.4')):
        _exe(tmp_path / 'sys', n, v)
    out = io.StringIO()
    code = setup_install.main(yes=True, search=search, mgr='apt', bin_dir=bin_dir, run=run,
                              get=lambda url: files[url.rsplit('/', 1)[1]], out=out)
    assert code == 0, out.getvalue()
    node_dir = home / '.fleetmux' / 'tools' / name[:-len('.tar.gz')]
    assert os.readlink(bin_dir / 'node') == str(node_dir / 'bin' / 'node')
    assert os.readlink(bin_dir / 'npm') == str(node_dir / 'bin' / 'npm')
    assert (bin_dir / 'npx').read_text() == 'not ours'  # a file that is not ours is left alone
    assert os.readlink(bin_dir / 'pi') == str(home / '.fleetmux' / 'tools' / 'npm' / 'bin' / 'pi')
    assert calls == [[str(node_dir / 'bin' / 'npm'), 'install', '-g', '--prefix', str(home / '.fleetmux' / 'tools' / 'npm'),
                      '@earendil-works/pi-coding-agent@1.5.0']]
    assert 'sudo' not in out.getvalue().split('steps:')[1]  # system packages were all there


def test_a_bad_node_checksum_stops(tmp_path, home, monkeypatch):
    name = 'node-v22.23.5-linux-arm64.tar.gz'
    files = {'SHASUMS256.txt': ('0' * 64 + '  ' + name + '\n').encode(), name: b'x'}
    monkeypatch.setattr(setup_install.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(setup_install.platform, 'machine', lambda: 'aarch64')
    with pytest.raises(SystemExit, match='sha256 mismatch'):
        setup_install.install_node(tmp_path / 'bin', io.StringIO(), get=lambda url: files[url.rsplit('/', 1)[1]])
    assert not (tmp_path / 'bin').exists()


def test_a_failed_system_step_stops_before_node(tmp_path, home):
    out = io.StringIO()
    code = setup_install.main(yes=True, search=str(tmp_path), mgr='apt', bin_dir=tmp_path / 'bin',
                              run=lambda argv, **kw: subprocess.CompletedProcess(argv, 100),
                              get=lambda url: pytest.fail('downloaded'), out=out)
    assert code == 2 and 'failed' in out.getvalue()


def _fleet(args, home):
    env = dict(os.environ, HOME=str(home), FLEETMUX_DISCOVERY='off')
    for k in ('FLEETMUX_HOME', 'FLEETMUX_STATE', 'FLEETMUX_CONFIG'):
        env.pop(k, None)
    return subprocess.run([sys.executable, str(ROOT / 'fleetmux' / 'fleet.py')] + args, capture_output=True,
                          text=True, env=env, cwd=str(home))


def test_fleet_commands_on_a_fresh_home_say_no_fleet_has_run(home):
    for cmd in (['status'], ['results'], ['stop']):
        done = _fleet(cmd, home)
        assert done.returncode == 1, done
        assert 'no fleet has run here yet' in done.stderr and 'Traceback' not in done.stderr, done.stderr
    done = _fleet(['status', '--config', str(home / 'nope.json')], home)
    assert 'nope.json does not exist' in done.stderr and 'Traceback' not in done.stderr


def test_start_names_setup_install_when_binaries_are_missing():
    text = (ROOT / 'fleetmux' / 'fleet.py').read_text()
    assert "Install them with `fleetmux setup --install`" in text
