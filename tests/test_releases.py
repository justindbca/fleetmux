"""Which release runs where (fleetmux/releases.py, docs/design.md section 56 "Releases").

The CLI release, the installed release (``~/fleetmux-current``, here a link under ``tmp_path``) and the release a
run's daemon runs (``<run>/release``). No real fleet, daemon or link is touched.
"""
import contextlib
import fcntl
import io
import json
from types import SimpleNamespace

import pytest
import yaml

from fleetmux import fleet, fleet_superpowers as fs, fleet_superpowers_cli as cli
from fleetmux import fleet_superpowers_refresh as rf, releases


@pytest.fixture
def cli_release(monkeypatch):
    monkeypatch.setattr(releases, '_own', ['aaaa111'])
    monkeypatch.setattr(releases, '_warned', set())


def make_run(tmp_path, name, release=None, python=None):
    run = tmp_path / ('run-' + name)
    run.mkdir()
    if release:
        releases.record(run, release)
    cfg = {'run': str(run), 'name': name, 'fleet_home': str(tmp_path / ('home-' + name)), 'mode': 'service'}
    if python:
        cfg['python'] = python
    (run / 'config.json').write_text(json.dumps(cfg))
    return run, cfg


def install(tmp_path, monkeypatch, release):
    """``~/fleetmux-current`` -> ``<tmp>/fleetmux-releases/<release>``."""
    target = tmp_path / 'fleetmux-releases' / release
    target.mkdir(parents=True)
    link = tmp_path / 'fleetmux-current'
    link.symlink_to(target)
    monkeypatch.setattr(releases, 'LINK', str(link))
    return link


@contextlib.contextmanager
def daemon_lock(run):
    """Hold ``<run>/daemon.lock`` as a running daemon does."""
    with (run / 'daemon.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


# -- where the releases come from ------------------------------------------------------------------------

def test_running_release_is_the_daemon_record_then_the_python_path(tmp_path):
    run, cfg = make_run(tmp_path, 'a', release='bbbb222',
                        python=str(tmp_path / 'fleetmux-releases' / 'cccc333' / '.venv/bin/python'))
    assert releases.running(cfg) == 'bbbb222'  # a resume from another release rewrote the record
    (run / releases.FILE).unlink()
    assert releases.running(cfg) == 'cccc333'  # a daemon from before the record
    assert releases.running({'run': str(run), 'python': '/usr/bin/python3'}) is None


def test_an_unknown_release_removes_the_old_record(tmp_path, monkeypatch):
    run, cfg = make_run(tmp_path, 'a', release='bbbb222')
    monkeypatch.setattr(releases, '_own', [None])  # a resume from a checkout git cannot read
    releases.record(run)
    assert not (run / releases.FILE).exists() and releases.running(cfg) is None


def test_installed_release_follows_the_link(tmp_path, monkeypatch):
    assert releases.installed() is None  # no link (tests/conftest.py)
    install(tmp_path, monkeypatch, 'dddd444')
    assert releases.installed() == 'dddd444'


def test_same_release_accepts_a_longer_sha():
    assert releases.same('24ca55b6', '24ca55b')
    assert not releases.same('24ca55b6', '24ca55c6')
    assert not releases.same(None, None) and not releases.same('v0.2.1', 'v0.2')


def test_refresh_writes_installed_and_running_releases(tmp_path, monkeypatch):
    run, cfg = make_run(tmp_path, 'a', release='bbbb222')
    install(tmp_path, monkeypatch, 'dddd444')
    found = {'instances': [{'run': str(run), 'configs': [str(run / 'config.json')], 'mode': 'service',
                            'observed_running': True}]}
    name, entry = rf.refresh(name='field', discovered={}, found=found, now='2030-01-03T08:00:00+00:00',
                             hostname='field.example', addresses=[], bin_=fs.DEFAULT_BIN, probe=lambda u, p: None)
    assert entry['fleetmux']['release'] == 'dddd444'  # installed, whoever runs refresh
    assert [r['release'] for r in entry['fleets']] == ['bbbb222']  # what the daemon runs


def test_the_daemon_records_its_release(tmp_path, monkeypatch, cli_release):
    run = tmp_path / 'run'
    run.mkdir()
    (run / 'config.json').write_text(json.dumps({'run': str(run)}))

    class Stop(Exception):
        pass

    def no_fleet(*a, **k):
        raise Stop
    monkeypatch.setattr(fleet, 'Fleet', no_fleet)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: None)
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: None)
    with pytest.raises(Stop):
        fleet.daemon(str(run / 'config.json'))
    assert (run / releases.FILE).read_text() == 'aaaa111\n'


# -- CLI vs daemon ---------------------------------------------------------------------------------------

def main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            fleet.main(argv)
            code = 0
        except SystemExit as exc:
            code = exc.code or 0
    return code, out.getvalue(), err.getvalue()


def test_a_command_on_a_fleet_of_another_release_warns_once(tmp_path, monkeypatch, cli_release):
    run, _ = make_run(tmp_path, 'a', release='bbbb222')
    monkeypatch.setattr(fleet, 'status', lambda cfg: print('{}'))
    with daemon_lock(run):
        code, out, err = main(['status', '--config', str(run / 'config.json')])
        assert code == 0 and out == '{}\n'
        lines = [l for l in err.splitlines() if 'release' in l]
        assert lines == ['fleetmux: warning: this command is release aaaa111, the daemon of %s runs release '
                         'bbbb222; restart the fleet with its usual start command to run aaaa111, or use '
                         '~/fleetmux-releases/bbbb222/bin/fleetmux' % run]
        assert not releases.warn_cli(json.loads((run / 'config.json').read_text()))  # once per process


def test_a_command_on_a_fleet_of_the_same_release_says_nothing(tmp_path, monkeypatch, cli_release):
    run, _ = make_run(tmp_path, 'a', release='aaaa1110')
    monkeypatch.setattr(fleet, 'status', lambda cfg: print('{}'))
    with daemon_lock(run):
        code, _, err = main(['status', '--config', str(run / 'config.json')])
    assert code == 0 and 'release' not in err


def test_no_warning_without_a_running_daemon_or_a_known_release(tmp_path, cli_release):
    run, cfg = make_run(tmp_path, 'a', release='bbbb222')
    assert not releases.warn_cli(cfg, stream=io.StringIO())  # nobody holds the daemon lock
    _, cfg = make_run(tmp_path, 'b')
    assert not releases.warn_cli(cfg, stream=io.StringIO(), alive=lambda run: True)


def test_applet_commands_warn_too(tmp_path, monkeypatch, cli_release):
    from fleetmux import applet_cli
    run, _ = make_run(tmp_path, 'a', release='bbbb222')
    err = io.StringIO()
    with daemon_lock(run), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        applet_cli.main(['list', '--config', str(run / 'config.json')])
    assert err.getvalue().count('this command is release aaaa111') == 1


# -- installed but not restarted -------------------------------------------------------------------------

class _NoRedis:
    """A fleet Redis with nothing in it: the awake clock is then measured locally."""
    connection_pool = SimpleNamespace(disconnect=lambda: None)

    def hgetall(self, key):
        return {}

    def scan_iter(self, pattern=None):  # status lists declared sub-fleets that wait (named_subfleet.waiting_rows)
        return iter(())

    def get(self, key):
        return None

    def close(self):
        pass


def stub_status(monkeypatch):
    from fleetmux import applet_host, control_view, subfleet, superpowers_peer
    monkeypatch.setattr(control_view, 'snapshot', lambda cfg: {'mode': 'task'})
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: [])
    monkeypatch.setattr(applet_host, 'tree_rows', lambda cfg: [])
    monkeypatch.setattr(superpowers_peer, 'run_loads', lambda run: {})
    monkeypatch.setattr(fleet, 'connect', lambda cfg: _NoRedis())  # status reads the awake clock (§35)


def test_status_reports_a_fleet_still_on_the_old_release(tmp_path, monkeypatch):
    stub_status(monkeypatch)
    run, cfg = make_run(tmp_path, 'a', release='bbbb222')
    cfg.update(transport_version=2, mode='task', parent_config='x')
    install(tmp_path, monkeypatch, 'dddd444')
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        fleet.status(cfg)
    assert json.loads(out.getvalue())['release'] == {'running': 'bbbb222', 'installed': 'dddd444',
                                                     'restart_needed': True}
    assert err.getvalue() == ('fleet a runs release bbbb222; release dddd444 is installed (~/fleetmux-current): '
                              'restart the fleet with its usual start command to run it\n')


def test_status_of_a_fleet_on_the_installed_release(tmp_path, monkeypatch):
    stub_status(monkeypatch)
    _, cfg = make_run(tmp_path, 'a', release='dddd444')
    cfg.update(transport_version=2, mode='task', parent_config='x')
    install(tmp_path, monkeypatch, 'dddd444')
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        fleet.status(cfg)
    assert json.loads(out.getvalue())['release']['restart_needed'] is False and err.getvalue() == ''


def test_show_self_lists_the_fleets_still_on_the_old_release(tmp_path, monkeypatch):
    old, _ = make_run(tmp_path, 'old', release='bbbb222')
    new, _ = make_run(tmp_path, 'new', release='dddd444')
    install(tmp_path, monkeypatch, 'dddd444')
    gone, _ = make_run(tmp_path, 'gone', release='bbbb222')  # restarted: the row still names the stopped run
    fs.write_self({'source': 'self', 'reach': [], 'fleets': [
        {'name': 'old', 'config': str(old / 'config.json'), 'running': True, 'release': 'stale-row'},
        {'name': 'new', 'config': str(new / 'config.json'), 'running': True, 'release': 'dddd444'},
        {'name': 'gone', 'config': str(gone / 'config.json'), 'running': True, 'release': 'bbbb222'}]},
        name='field')
    out = io.StringIO()
    with daemon_lock(old), daemon_lock(new), contextlib.redirect_stdout(out):
        assert cli.main(['show', '--self']) == 0
    text = out.getvalue()
    assert yaml.safe_load(text)['field']['fleets'][0]['name'] == 'old'  # still the entry
    assert [l for l in text.splitlines() if l.startswith('#')] == [
        '# fleet old runs release bbbb222; release dddd444 is installed (~/fleetmux-current): restart the fleet '
        'with its usual start command to run it']
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(['show', '--self', '--json']) == 0
    assert list(json.loads(out.getvalue())) == ['field']  # sync parses this: no extra line
