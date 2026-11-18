"""The one resolver finds a pi that only lives in a private directory (docs/design.md §50.3).

Incident: pi sat under a private npm prefix (~/tools/npm/bin, `#!/usr/bin/env node`) that neither the
ssh PATH nor the interactive login shell's PATH had; node lived in a Homebrew bin that ssh's PATH lacked too. The
previous release had found both and recorded them in its installed.json, but the next install did not use that
record and failed with `FAIL pi missing`. Here the same machine is built on a temporary HOME: nothing reads or
writes the real HOME, ~/fleetmux-current or any release directory.
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest

from fleetmux import discover, setup_install

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check_deps_find_pi', ROOT / 'scripts/check_deps.py')
check_deps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_deps)

PKG = 'lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js'
DEPS = ('python: {min: "3.0"}\npython_packages: req.txt\nprograms:\n'
        '  pi: {cmd: [pi, --version], min: "1.2.0"}\n'
        '  node: {cmd: [node, --version], min: "22.0.0", with: pi}\n')


def _exe(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _link(link, target):
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    return link


@pytest.fixture
def studio(tmp_path, monkeypatch):
    """A temp HOME shaped like that host: pi only in a private npm prefix, node only in a Homebrew-like bin, neither on
    the process PATH nor on the login shell's PATH, and a previous release (2ea7fc29) behind ~/fleetmux-current."""
    home = tmp_path / 'home'
    prefix = home / 'tools' / 'npm'
    cli = _exe(prefix / PKG, '#!/usr/bin/env node\n// pi\n')
    pi_link = _link(prefix / 'bin' / 'pi', cli)
    cellar = _exe(home / 'homebrew' / 'Cellar' / 'node' / '22.11.0' / 'bin' / 'node',
                  '#!/bin/sh\nif [ "$1" = --version ]; then echo v22.11.0; else echo 1.5.0; fi\n')
    node_link = _link(home / 'homebrew' / 'bin' / 'node', cellar)
    # The process PATH and the login shell's PATH are an empty directory: a host with a real node in /usr/bin (apt,
    # NodeSource, a container) must not make these tests pass or fail by accident.
    empty = tmp_path / 'empty-bin'
    empty.mkdir()
    shell = _exe(tmp_path / 'fake-shell', '#!/bin/sh\nprintf "__FLEET_PATH__%%s__END__" "%s"\n' % empty)
    release = home / 'fleetmux-releases' / '2ea7fc29'
    (release / 'bin').mkdir(parents=True)
    _link(home / 'fleetmux-current', release)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('SHELL', str(shell))
    monkeypatch.setenv('PATH', str(empty))
    monkeypatch.delenv(discover.OFF_ENV, raising=False)
    # the default record location, resolved against the temp HOME (conftest points it elsewhere for other tests)
    monkeypatch.setattr(discover, 'CURRENT', Path('~/fleetmux-current'), raising=False)
    monkeypatch.setattr(discover, 'OWN_INSTALLED', tmp_path / 'no-own-release' / 'installed.json', raising=False)
    monkeypatch.setattr(discover, '_SEARCH', [])
    monkeypatch.setattr(check_deps, 'SEARCH', None)
    monkeypatch.setattr(check_deps, 'FOUND', {})
    monkeypatch.setattr(check_deps, 'LOCATED', {}, raising=False)
    (tmp_path / 'deps.yaml').write_text(DEPS)
    (tmp_path / 'req.txt').write_text('')
    real = check_deps.check
    monkeypatch.setattr(check_deps, 'check', lambda backend=None: real(tmp_path, backend, plat='linux'))
    out_json = tmp_path / 'out' / 'deps.json'
    out_json.parent.mkdir()
    monkeypatch.setenv('FLEETMUX_DEPS_JSON', str(out_json))
    real_pi, real_node = os.path.realpath(cli), os.path.realpath(cellar)
    return {'home': home, 'tmp': tmp_path, 'release': release, 'installed': release / 'bin' / 'installed.json',
            'pi_bin': str(pi_link.parent), 'node_bin': str(node_link.parent), 'pi': real_pi, 'node': real_node,
            'out': out_json, 'cellar_bin': os.path.dirname(real_node), 'empty': str(empty)}


def record(world, rows):
    world['installed'].write_text(json.dumps({'release_sha': '2ea7fc29', 'dependencies': rows}, indent=2))


def new_rows(w):
    return [{'name': 'pi', 'have': '1.5.0', 'need': '>=1.2.0', 'ok': True, 'path': w['pi'], 'dir': w['pi_bin'],
             'interpreter': 'node', 'interpreter_path': w['node'], 'interpreter_dir': w['node_bin']},
            {'name': 'node', 'have': '22.11.0', 'need': '>=22.0.0', 'ok': True, 'path': w['node'],
             'dir': w['node_bin']}]


def old_rows(w):
    """The row shape 2ea7fc29 wrote: the realpath only (pi's is the npm package's cli.js)."""
    return [{'name': 'pi', 'have': '1.5.0', 'need': '>=1.2.0', 'ok': True, 'path': w['pi']},
            {'name': 'node', 'have': '22.11.0', 'need': '>=22.0.0', 'ok': True, 'path': w['node']}]


def run_check(capsys):
    code = check_deps.main([])
    return code, capsys.readouterr().out


def snapshot(root):
    out = {}
    for base, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(base, n)
            st = os.lstat(p)
            out[os.path.relpath(p, root)] = (st.st_mode, st.st_size, st.st_mtime_ns,
                                            os.readlink(p) if os.path.islink(p) else None)
    return out


def test_studio_check_passes_from_the_previous_release_record(studio, capsys):
    record(studio, new_rows(studio))
    code, out = run_check(capsys)
    assert code == 0, out
    assert 'ok    pi 1.5.0 (need >=1.2.0) at ' + studio['pi'] in out
    assert 'ok    node 22.11.0 (need >=22.0.0) at ' + studio['node'] in out
    rows = {r['name']: r for r in json.loads(studio['out'].read_text())}
    assert rows['pi']['path'] == new_rows(studio)[0]['path']  # the same pi as recorded


def test_studio_check_passes_from_an_old_format_record(studio, capsys):
    record(studio, old_rows(studio))  # 2ea7fc29's rows: no dir, pi's path is the cli.js
    code, out = run_check(capsys)
    assert code == 0, out
    rows = {r['name']: r for r in json.loads(studio['out'].read_text())}
    assert rows['pi']['path'] == studio['pi'] and rows['pi']['ok'] is True
    assert rows['node']['ok'] is True


def test_without_a_record_the_studio_check_still_fails(studio, capsys):
    code, out = run_check(capsys)
    assert code == 1 and 'FAIL  pi missing' in out


def test_pi_probe_finds_node_only_in_a_recorded_dir(studio):
    # pi itself is on PATH; its node is only in a directory a release recorded.
    record(studio, [{'name': 'node', 'have': '22.11.0', 'need': '>=22.0.0', 'ok': True, 'path': studio['node'],
                     'dir': studio['node_bin']}])
    os.environ['PATH'] = studio['pi_bin'] + ':' + studio['empty']
    check_deps.SEARCH = check_deps.search(warn=lambda line: None)
    assert check_deps.probe(['pi', '--version']) == (1, 5, 0)


def test_pi_probe_without_the_recorded_node_dir_is_missing(studio):
    os.environ['PATH'] = studio['pi_bin'] + ':' + studio['empty']
    check_deps.SEARCH = check_deps.search(warn=lambda line: None)
    assert check_deps.probe(['pi', '--version']) is None  # `env: node: No such file or directory`


def test_found_rows_record_dir_and_interpreter_dir(studio, capsys):
    os.environ['PATH'] = studio['pi_bin'] + ':' + studio['node_bin'] + ':' + studio['empty']
    code, out = run_check(capsys)
    assert code == 0, out
    rows = {r['name']: r for r in json.loads(studio['out'].read_text())}
    assert rows['pi']['dir'] == studio['pi_bin'] and rows['pi']['path'] == studio['pi']
    assert rows['pi']['interpreter'] == 'node'
    assert rows['pi']['interpreter_path'] == studio['node'] and rows['pi']['interpreter_dir'] == studio['node_bin']
    assert rows['node']['dir'] == studio['node_bin'] and 'interpreter' not in rows['node']


def test_discovery_probe_finds_pi_and_node_from_the_record(studio):
    record(studio, new_rows(studio))
    row = discover.probe_cli('pi', '1.2.0', discover.search_path(), [])
    assert row['installed'] is True and row['path'] == studio['pi']
    assert row['version'] == '1.5.0' and row['version_ok'] is True
    assert row['dir'] == studio['pi_bin'] and row['interpreter_dir'] == studio['node_bin']


def test_a_discovery_record_is_a_source_too(studio):
    from fleetmux import fleet_superpowers
    fleet_superpowers.write_block('models', {'clis': {'pi': dict(new_rows(studio)[0], installed=True)}})
    dirs = discover.search_path().split(os.pathsep)
    assert studio['pi_bin'] in dirs and studio['node_bin'] in dirs


def test_search_path_appends_records_after_login_and_process_dedup_existing_only(studio):
    rows = new_rows(studio) + [{'name': 'sh', 'path': os.path.realpath('/bin/sh'), 'dir': '/bin'},
                               {'name': 'gone', 'path': str(studio['home'] / 'gone' / 'gone'),
                                'dir': str(studio['home'] / 'gone')}]
    record(studio, rows)
    os.environ['PATH'] = '/bin:' + studio['empty'] + ':/usr/sbin'
    dirs = discover.search_path().split(os.pathsep)
    assert dirs[:3] == [studio['empty'], '/bin', '/usr/sbin']  # login shell, then process; the empty dir kept once
    assert dirs[3:] == [studio['pi_bin'], studio['node_bin']]  # then the records; /bin not repeated, gone dropped


def test_a_stale_new_format_record_is_dropped(studio):
    other = _exe(studio['home'] / 'other' / 'pi', '#!/bin/sh\necho 9.9.9\n')
    rows = new_rows(studio)
    rows[0]['dir'] = str(other.parent)  # the dir exists, but its pi is not the recorded one
    record(studio, rows[:1])
    dirs = discover.search_path().split(os.pathsep)
    assert str(other.parent) not in dirs


def test_records_never_add_a_release_bin_dir(studio):
    fmux = _exe(studio['release'] / 'bin' / 'fleetmux', '#!/bin/sh\necho fleetmux 0.2.2\n')
    record(studio, [{'name': 'fleetmux', 'path': str(fmux), 'dir': str(fmux.parent)}] + new_rows(studio))
    dirs = discover.search_path().split(os.pathsep)
    assert str(fmux.parent) not in dirs and studio['pi_bin'] in dirs


def test_current_path_wins_over_a_stale_record(studio, capsys):
    record(studio, new_rows(studio))
    mine = _exe(studio['home'] / 'mine' / 'pi', '#!/bin/sh\necho 1.6.0\n')
    os.environ['PATH'] = str(mine.parent) + ':' + studio['empty']
    code, out = run_check(capsys)
    assert code == 0 and 'ok    pi 1.6.0 (need >=1.2.0) at ' + os.path.realpath(mine) in out


@pytest.mark.parametrize('how', ['dir removed', 'pi removed from its dir'])
def test_missing_program_names_where_it_was_last_found(studio, capsys, how):
    record(studio, new_rows(studio))
    os.unlink(studio['pi_bin'] + '/pi')
    if how == 'dir removed':
        os.rmdir(studio['pi_bin'])
    code, out = run_check(capsys)
    assert code == 1
    line = next(l for l in out.splitlines() if l.startswith('FAIL  pi'))
    assert line.startswith('FAIL  pi missing (need >=1.2.0)')
    assert 'last found at ' + studio['pi'] in line and os.path.realpath(studio['installed']) in line
    rows = {r['name']: r for r in json.loads(studio['out'].read_text())}
    assert rows['pi']['last_found'] == studio['pi']
    assert discover.last_found('pi').startswith('last found at ' + studio['pi'])


def test_check_changes_nothing_on_the_machine(studio, capsys):
    record(studio, old_rows(studio))
    before = snapshot(studio['home'])
    sp = Path(os.environ['FLEETMUX_SUPERPOWERS'])
    code, _ = run_check(capsys)
    assert code == 0
    assert snapshot(studio['home']) == before  # no shell config, link, PATH file or install in HOME
    assert not sp.exists()  # reading the discovery record never writes (no migration)


def test_setup_install_check_uses_the_record(studio):
    record(studio, new_rows(studio))
    rows = {r['name']: r for r in setup_install.check(discover.search_path())}
    assert rows['pi']['state'] in ('ok', 'old') and rows['pi']['version'] == '1.5.0'
    assert rows['node']['version'] == '22.11.0'


def test_start_pi_gets_its_interpreter_dir(studio):
    record(studio, new_rows(studio))
    assert discover.run_dirs(studio['pi_bin'] + '/pi') == [studio['node_bin']]
    os.environ['PATH'] = studio['node_bin'] + ':' + studio['empty']
    assert discover.run_dirs(studio['pi_bin'] + '/pi') == []  # node already on the process PATH
    os.environ[discover.OFF_ENV] = 'off'
    assert discover.run_dirs(studio['pi_bin'] + '/pi') == []  # tests and scripted starts: no lookup


def test_start_pins_the_pi_dirs_for_its_panes():
    from fleetmux import fleet
    cfg = {'pi_path_dirs': ['/opt/node/bin']}
    assert fleet.pane_path(cfg, '/usr/bin:/bin', pi=True) == '/usr/bin:/bin:/opt/node/bin'
    assert fleet.pane_path(cfg, '/usr/bin:/bin', pi=False) == '/usr/bin:/bin'
    assert fleet.pane_path({}, '/usr/bin:/bin', pi=True) == '/usr/bin:/bin'


def test_setup_install_names_where_a_missing_pi_was_last_found(studio):
    import io
    record(studio, new_rows(studio))
    os.unlink(studio['pi_bin'] + '/pi')
    out = io.StringIO()
    setup_install.main(out=out, search=discover.search_path(), mgr=None, bin_dir=studio['tmp'] / 'links', tty=False,
                       run=lambda *a, **k: pytest.fail('nothing may run'))
    line = next(l for l in out.getvalue().splitlines() if l.strip().startswith('pi '))
    assert 'missing' in line and 'last found at ' + studio['pi'] in line


@pytest.fixture
def request_cleanup():
    done = []
    yield done
    for fn in done:
        fn()


def test_start_runs_pi_with_and_pins_its_interpreter_dir(tmp_path, monkeypatch, request_cleanup):
    """Through `fleet start` (all mocked, no fleet starts): the dirs run_dirs gives are on `pi --version`'s PATH and
    pinned in the saved run config, which every pane's cfg (runtime_cfg -> pi_adapter.for_agent) copies."""
    from types import SimpleNamespace
    import test_pi
    from fleetmux import fleet
    args, _ = test_pi.mock_start(tmp_path, monkeypatch)
    import shutil
    import tempfile
    short = Path(tempfile.mkdtemp(prefix='cfp-', dir='/tmp')).resolve()  # Unix socket length limit (as test_pi)
    request_cleanup.append(lambda: shutil.rmtree(short, ignore_errors=True))
    monkeypatch.setattr(fleet.redis_runtime, 'DEFAULT_ROOT', short / 'redis')
    monkeypatch.setattr(fleet.discover, 'run_dirs', lambda program: ['/opt/node/bin'] if program == '/bin/pi' else [])
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw.get('env')))
        return SimpleNamespace(stdout='1.2.0\n', returncode=0)
    monkeypatch.setattr(fleet.subprocess, 'run', run)
    fleet.main(args)
    env = next(e for a, e in seen if a == ['/bin/pi', '--version'])
    assert env['PATH'].split(os.pathsep)[-1] == '/opt/node/bin'
    cfg = fleet.load(tmp_path / 'latest.json')
    assert cfg['pi_path_dirs'] == ['/opt/node/bin']
    assert fleet.pane_path(fleet.pi_adapter.for_agent(cfg, 'supervisor'), '/usr/bin', pi=True) == '/usr/bin:/opt/node/bin'
