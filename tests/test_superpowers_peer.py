"""``fleetmux superpowers sync / save --also / load / unload`` (fleetmux/superpowers_peer.py).

Two machines in one process: ``field`` is this machine (the session's temporary inventory), ``home`` is a second
inventory file. A stubbed ssh runs the far ``fleetmux superpowers ...`` command against the home file in process.
No real machine, no real fleet, no network. Git repositories are made in ``tmp_path``.
"""
import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from fleetmux import fleet, fleet_superpowers as fs, fleet_superpowers_cli as cli, remote
from fleetmux import superpowers_peer as peer

NOW = '2030-01-04T08:00:00+00:00'


class World:
    """The field machine (local) and the home machine (reached over a stubbed ssh)."""

    def __init__(self, tmp_path, monkeypatch, probe=0):
        self.tmp, self.mp = tmp_path, monkeypatch
        self.home_file = tmp_path / 'home' / 'fleet-superpowers.yaml'
        self.home_file.parent.mkdir()
        self.probes, self.far_calls = [], []
        self.probe_code = probe
        fs.write_self({'hostname': 'field.example', 'reach': []}, name='field')
        fs.add_copy('home', 'user@home.example', now=NOW)
        fs.write_self({'hostname': 'home.example', 'reach': [{'via': 'lan', 'ssh': 'user@home.example'}]},
                      name='home', path=self.home_file)
        fs.add_copy('field', 'user@field.example', now=NOW, path=self.home_file)
        monkeypatch.setattr(remote.subprocess, 'call', self.probe)
        monkeypatch.setattr(peer, '_ssh', self.ssh)

    def probe(self, argv, **kw):
        assert argv[-1] == 'true' and kw.get('stdin') == subprocess.DEVNULL
        self.probes.append(argv[-2])
        if isinstance(self.probe_code, dict):
            return self.probe_code.get(argv[-2], 0)
        return self.probe_code

    def ssh(self, argv, input, capture):
        """The far command, run against the home inventory."""
        assert argv[:2] == ['ssh', '--'] and argv[2] == 'user@home.example'
        words = shlex.split(argv[3])
        assert words[0] == '$HOME/fleetmux-current/bin/fleetmux' and words[1] == 'superpowers'
        self.far_calls.append(words[1:])
        out, err = io.StringIO(), io.StringIO()
        old = os.environ.get(fs.ENV), sys.stdin
        os.environ[fs.ENV] = str(self.home_file)
        sys.stdin = io.StringIO(input or '')
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(words[2:])
        finally:
            os.environ[fs.ENV], sys.stdin = old[0], old[1]
        return code, out.getvalue() if capture else None


def main(argv, stdin=''):
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), pytest.raises(SystemExit) as stop:
            fleet.main(['superpowers'] + argv)
    finally:
        sys.stdin = old
    return stop.value.code, out.getvalue(), err.getvalue()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'COMMANDS', dict(cli.COMMANDS))
    return World(tmp_path, monkeypatch)


def fake_refresh():
    entry = dict(fs.machine('field'), updated_at=NOW, clis={'pi': {'version': '1.5.0', 'logged_in': True}})
    fs.write_self(entry)
    return 'field', entry


# -- sync ------------------------------------------------------------------------------------------------

def test_sync_swaps_sections_both_ways(world, monkeypatch):
    from fleetmux import fleet_superpowers_refresh
    monkeypatch.setattr(fleet_superpowers_refresh, 'refresh', fake_refresh)
    code, out, err = main(['sync', 'home'])
    assert code == 0, err
    here = fs.machine('home')
    assert here['source'] == 'copy' and here['hostname'] == 'home.example' and here['copied_at']
    there = fs.machine('field', path=world.home_file)
    assert there['source'] == 'copy' and there['clis'] == {'pi': {'version': '1.5.0', 'logged_in': True}}
    assert fs.self_name(world.home_file) == 'home'  # the far self entry is untouched
    assert world.far_calls == [['superpowers', 'show', '--self', '--json'], ['superpowers', 'import', '-']]


@pytest.mark.parametrize('theirs, warned', [('bbbb222', True), ('aaaa1110', False)])
def test_sync_warns_when_the_installed_releases_differ(world, monkeypatch, theirs, warned):
    from fleetmux import fleet_superpowers_refresh

    def refresh():
        entry = dict(fs.machine('field'), fleetmux={'bin': fs.DEFAULT_BIN, 'release': 'aaaa111'})
        fs.write_self(entry)
        return 'field', entry
    monkeypatch.setattr(fleet_superpowers_refresh, 'refresh', refresh)
    home = fs.machine('home', path=world.home_file)
    fs.write_self(dict(home, fleetmux={'bin': fs.DEFAULT_BIN, 'release': theirs}), name='home', path=world.home_file)
    code, _, err = main(['sync', 'home'])
    assert code == 0, err
    line = ('superpowers: warning: field has release aaaa111 installed, home has bbbb222; install the same release '
            'on both before `load`')
    assert (line in err) is warned and ('warning' in err) is warned


def test_sync_stops_when_the_far_side_does_not_connect(world, monkeypatch):
    from fleetmux import fleet_superpowers_refresh
    monkeypatch.setattr(fleet_superpowers_refresh, 'refresh', fake_refresh)
    world.probe_code = 255
    code, _, err = main(['sync', 'home'])
    assert code == 1 and 'no reach entry of home connects' in err
    assert world.far_calls == []


def test_sync_tries_added_reach_entries_by_kind(world, monkeypatch):
    from fleetmux import fleet_superpowers_refresh
    monkeypatch.setattr(fleet_superpowers_refresh, 'refresh', fake_refresh)
    fs.remove_reach('home', 'user@home.example')
    fs.add_copy('home', 'user@home.example', via='tunnel')
    fs.add_copy('home', 'home-dead', via='lan')  # listed after the tunnel, tried before it
    world.probe_code = {'home-dead': 255}
    assert peer.reach('home')[1] == 'user@home.example'  # load and its git fetch pick the same way
    assert world.probes == ['home-dead', 'user@home.example']
    world.probes.clear()
    code, _, err = main(['sync', 'home'])
    assert code == 0, err
    assert world.probes == ['home-dead', 'user@home.example']  # one probe serves both far calls
    assert len(world.far_calls) == 2


def test_a_255_is_not_run_again(world, monkeypatch):
    monkeypatch.setattr(peer, '_ssh', lambda argv, input, capture: world.far_calls.append(argv) or (255, ''))
    with pytest.raises(fs.InventoryError, match='exited 255'):
        peer._far_ok('home', ['superpowers', 'show', '--self', '--json'], capture=True)
    assert len(world.far_calls) == 1


# -- save --also -----------------------------------------------------------------------------------------

WORKFLOW_ROOT = {
    'kind': 'workflow', 'summary': 'Route field Claude sessions through home.',
    'applies_when': ['Claude sessions fail on the field machine.'],
    'source': {'repo': None, 'ref': None, 'from': 'home'},
    'applets': [{'machine': 'field', 'fleet': 'field', 'roster_rows': 'teams/field-applet.yaml'}],
    'saved_on': ['home', 'field'],
}


def test_save_also_writes_both_machines(world, tmp_path):
    workflow = dict(WORKFLOW_ROOT, source={'repo': '/srv/applets/route', 'ref': '4d5e6f7', 'from': 'home'})
    f = tmp_path / 'r.yaml'
    f.write_text(yaml.safe_dump({'route': workflow}))
    code, out, err = main(['save', str(f), '--also', 'home'])
    assert code == 0, err
    assert out.splitlines() == ['saved route', 'saved route on home']
    assert fs.workflow('route') == workflow
    assert fs.workflow('route', path=world.home_file) == workflow
    assert world.far_calls == [['superpowers', 'save', '-']]


def test_save_without_also_is_local_only(world, tmp_path):
    workflow = dict(WORKFLOW_ROOT, source={'repo': '/srv/applets/route', 'ref': '4d5e6f7', 'from': 'home'})
    f = tmp_path / 'r.yaml'
    f.write_text(yaml.safe_dump({'route': workflow}))
    assert main(['save', str(f)])[0] == 0
    assert fs.workflow('route', path=world.home_file) is None and world.far_calls == []


# -- load / unload ---------------------------------------------------------------------------------------

ROSTER = ('# field roster\nworkers:\n  - {name: onsite-1, role: roles/liaison.md}   # the liaison\n'
          '  - {name: onsite-2, role: roles/liaison.md}\n\nmax_busy: 2\n')


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo)] + list(args), check=True, capture_output=True, text=True).stdout


def make_repo(tmp_path):
    repo = tmp_path / 'applets' / 'route'
    (repo / 'teams').mkdir(parents=True)
    (repo / 'route_field.py').write_text('print(1)\n')
    (repo / 'route.yaml').write_text('field: {port: 17890}\n')
    (repo / 'teams' / 'field-applet.yaml').write_text(
        'applets:\n  - name: applet-tunnel-1\n    command: ["{python}", "route_field.py"]\n    cwd: ..\n'
        '    params: {file: ../route.yaml, block: field}\n')
    (repo / 'teams' / 'home.yaml').write_text('workers: []\n')
    (repo / 'teams' / 'host.md').write_text('host\n')
    git(repo, 'init', '-q')
    git(repo, '-c', 'user.name=user', '-c', 'user.email=user@field.example', 'add', '.')
    git(repo, '-c', 'user.name=user', '-c', 'user.email=user@field.example', 'commit', '-qm', 'applets')
    return repo, git(repo, 'rev-parse', '--short', 'HEAD').strip()


def make_fleet(tmp_path, name, roster_inside=True):
    run = tmp_path / ('run-' + name)
    run.mkdir()
    roster = (run / 'roster.yaml') if roster_inside else (tmp_path / (name + '-roster.yaml'))
    roster.write_text(ROSTER)
    cfg = run / 'config.json'
    cfg.write_text(json.dumps({'run': str(run), 'roster': str(roster), 'redis_socket': '/tmp/fleetmux-t.sock'}))
    entry = fs.machine('field')
    fleets = [r for r in entry.get('fleets') or [] if r['name'] != name]
    fs.write_self(dict(entry, fleets=fleets + [{'name': name, 'config': str(cfg), 'running': True}]))
    return run, roster


class Mini:
    """The few Redis reads of `applet status` and the sub-fleet mirror."""

    def __init__(self, hashes):
        self.hashes = hashes

    def exists(self, k):
        return k in self.hashes

    def hgetall(self, k):
        return dict(self.hashes.get(k, {}))

    def hget(self, k, f):
        return self.hashes.get(k, {}).get(f)

    def smembers(self, k):
        return set()

    def scan_iter(self, match=None, count=None):
        return iter(())


class Host:
    """The host daemons as load / unload see them. A roster write starts every applet the roster declares (a new
    pid in the host's record ``<run>/applets/<name>/pid``) and stops every one it no longer declares (the process
    gone, its keys cleared), ``lag`` daemon ticks later; one tick is one pause of the waiting command. A sub-fleet
    row starts the applets of its mirror in a new child run directory."""

    def __init__(self, redis, tmp_path):
        self.redis, self.tmp = redis, tmp_path
        self.alive, self.running, self.queue = set(), {}, []
        self.lag, self.never_stop, self.ticks, self.next_pid = 0, False, 0, 70000

    def wrote(self, path, text):
        data = yaml.safe_load(text) or {}
        names = [r['name'] for r in data.get('applets') or []]
        subs = [r['name'] for r in data.get('subfleets') or []]
        self.queue.append([self.lag, Path(path).parent, names, subs])
        self.step()

    def tick(self, *_):
        self.ticks += 1
        for item in self.queue:
            item[0] -= 1
        self.step()

    def step(self):
        while self.queue and self.queue[0][0] <= 0:
            _, run, names, subs = self.queue.pop(0)
            self.apply(run, names, subs)

    def start(self, run, name):
        self.next_pid += 1
        rd = run / 'applets' / name
        rd.mkdir(parents=True, exist_ok=True)
        (rd / 'pid').write_text(str(self.next_pid))
        self.alive.add(self.next_pid)
        return self.next_pid

    def apply(self, run, names, subs):
        hashes = self.redis.hashes
        for name in names:
            if (run, name) not in self.running:
                self.running[(run, name)] = ('fx:applet:', [self.start(run, name)])
                dict.setdefault(hashes, 'fx:applet:' + name, {}).setdefault('state', 'up')
        for sub in subs:
            if (run, sub) not in self.running:
                h = dict.setdefault(hashes, 'fx:subfleet:' + sub, {})
                child = self.tmp / ('child-%s-%d' % (sub, self.next_pid))
                h['run_dir'] = str(child)
                found = json.loads(h.get('applets') or '{}')
                self.running[(run, sub)] = ('fx:subfleet:', [self.start(child, n) for n in found])
        for (r, name), (prefix, pids) in list(self.running.items()):
            if r != run or name in names or name in subs or self.never_stop:
                continue
            del self.running[(r, name)]
            self.alive.difference_update(pids)
            dict.pop(hashes, prefix + name, None)

    def pid(self, run, name='applet-tunnel-1'):
        return int((Path(run) / 'applets' / name / 'pid').read_text())


@pytest.fixture
def loaded(world, tmp_path, monkeypatch):
    repo, ref = make_repo(tmp_path)
    workflow = dict(WORKFLOW_ROOT, source={'repo': str(repo), 'ref': ref, 'from': 'home'})
    fs.save_workflow('route', workflow)
    run, roster = make_fleet(tmp_path, 'field')
    redis = Mini({'fx:applet:applet-tunnel-1': {'state': 'up'}})
    monkeypatch.setattr(peer, 'connect', lambda cfg: redis)
    host = Host(redis, tmp_path)
    real_write = peer._write
    monkeypatch.setattr(peer, '_write', lambda path, text: (real_write(path, text), host.wrote(path, text))[0])
    monkeypatch.setattr(peer, 'alive', lambda pid: pid in host.alive, raising=False)
    monkeypatch.setattr(peer, '_sleep', host.tick, raising=False)
    return {'repo': repo, 'ref': ref, 'run': run, 'roster': roster, 'redis': redis, 'host': host}


def test_load_adds_root_applet_rows_with_no_durable_state(loaded):
    code, out, err = main(['load', 'route', '--poll', '0'])
    assert code == 0, err
    text = loaded['roster'].read_text()
    assert text.startswith(ROSTER)  # every byte before the new block kept
    rows = yaml.safe_load(text)['applets']
    assert [r['name'] for r in rows] == ['applet-tunnel-1']
    assert rows[0]['durable_state'] is False
    assert rows[0]['cwd'] == str(loaded['repo'].resolve())
    assert rows[0]['params'] == {'file': str((loaded['repo'] / 'route.yaml').resolve()), 'block': 'field'}
    from fleetmux import roster as roster_mod
    assert [e['name'] for e in roster_mod.load_applets(loaded['roster'])] == ['applet-tunnel-1']


def test_load_then_unload_restores_the_roster_byte_for_byte(loaded):
    before = loaded['roster'].read_bytes()
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    assert loaded['roster'].read_bytes() != before
    code, out, err = main(['unload', 'route'])
    assert code == 0, err
    assert loaded['roster'].read_bytes() == before
    assert not (loaded['run'] / peer.RECORDS / 'route.json').exists()


def test_load_appends_to_an_existing_applets_block(loaded):
    text = ROSTER + 'applets:\n  - {name: applet-old-1, command: [x], cwd: /}\n# tail comment\n'
    loaded['roster'].write_text(text)
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    after = loaded['roster'].read_text()
    assert [r['name'] for r in yaml.safe_load(after)['applets']] == ['applet-old-1', 'applet-tunnel-1']
    assert after.endswith('# tail comment\n')
    assert main(['unload', 'route'])[0] == 0
    assert loaded['roster'].read_text() == text


def test_load_refuses_a_roster_outside_the_run(world, tmp_path, monkeypatch):
    repo, ref = make_repo(tmp_path)
    fs.save_workflow('route', dict(WORKFLOW_ROOT, source={'repo': str(repo), 'ref': ref, 'from': 'home'}))
    _, roster = make_fleet(tmp_path, 'field', roster_inside=False)
    code, _, err = main(['load', 'route'])
    assert code == 1 and 'outside its run directory' in err
    assert roster.read_text() == ROSTER


def test_load_refuses_a_missing_commit_when_the_source_does_not_connect(loaded, world):
    fs.save_workflow('route', dict(WORKFLOW_ROOT, source={'repo': str(loaded['repo']), 'ref': 'deadbeef1234', 'from': 'home'}))
    world.probe_code = 255
    code, _, err = main(['load', 'route'])
    assert code == 1 and 'deadbeef1234 is missing' in err and 'home does not connect' in err
    assert world.probes == ['user@home.example']
    assert loaded['roster'].read_text() == ROSTER


def commit(repo, message='later'):
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=user', '-c', 'user.email=user@field.example', 'commit', '-qm', message)
    return git(repo, 'rev-parse', '--short', 'HEAD').strip()


def test_load_accepts_a_later_commit_that_only_adds_the_workflow_file(loaded):
    """A workflow stored in the applet repository can never name the commit that holds it."""
    (loaded['repo'] / 'workflows').mkdir()
    (loaded['repo'] / 'workflows' / 'route.yaml').write_text(yaml.safe_dump({'route': fs.workflow('route')}))
    commit(loaded['repo'], 'workflow')
    code, _, err = main(['load', 'route', '--poll', '0'])
    assert code == 0, err
    assert [r['name'] for r in yaml.safe_load(loaded['roster'].read_text())['applets']] == ['applet-tunnel-1']


@pytest.mark.parametrize('changed', ['route_field.py', 'route.yaml', 'teams/field-applet.yaml'])
def test_load_refuses_a_later_commit_that_changes_an_applet_file(loaded, changed):
    (loaded['repo'] / changed).write_text((loaded['repo'] / changed).read_text() + '# changed\n')
    commit(loaded['repo'])
    code, _, err = main(['load', 'route'])
    assert code == 1 and changed in err and 'checkout ' + loaded['ref'] in err
    assert loaded['roster'].read_text() == ROSTER


def test_load_refuses_a_ref_that_is_not_an_ancestor_of_head(loaded):
    repo = loaded['repo']
    (repo / 'x.txt').write_text('x\n')
    other = commit(repo, 'other line')
    git(repo, 'reset', '-q', '--hard', loaded['ref'])
    (repo / 'y.txt').write_text('y\n')
    commit(repo, 'this line')
    fs.save_workflow('route', dict(fs.workflow('route'), source=dict(fs.workflow('route')['source'], ref=other)))
    code, _, err = main(['load', 'route'])
    assert code == 1 and 'does not contain ' + other in err and 'checkout ' + other in err
    assert loaded['roster'].read_text() == ROSTER


def test_load_refuses_a_dirty_applet_file(loaded):
    (loaded['repo'] / 'route_field.py').write_text('print(2)\n')
    code, _, err = main(['load', 'route'])
    assert code == 1 and 'route_field.py' in err and 'uncommitted' in err
    assert loaded['roster'].read_text() == ROSTER


def test_load_refuses_an_applet_file_that_is_not_in_source_ref(loaded):
    repo = loaded['repo']
    git(repo, 'rm', '-q', '--cached', 'route.yaml')  # still on disk, untracked: not what source.ref holds
    git(repo, '-c', 'user.name=user', '-c', 'user.email=user@field.example', 'commit', '-qm', 'untrack')
    ref = git(repo, 'rev-parse', '--short', 'HEAD').strip()
    fs.save_workflow('route', dict(fs.workflow('route'), source=dict(fs.workflow('route')['source'], ref=ref)))
    code, _, err = main(['load', 'route'])
    assert code == 1 and 'route.yaml' in err and 'not in ' + ref in err
    assert loaded['roster'].read_text() == ROSTER


def test_load_refuses_when_git_cannot_compare(loaded, monkeypatch):
    real = peer._git

    def broken(repo, *args):
        if args[:1] == ('diff',):
            return subprocess.CompletedProcess(args, 128, '', 'fatal: bad object')
        return real(repo, *args)
    monkeypatch.setattr(peer, '_git', broken)
    code, _, err = main(['load', 'route'])
    assert code == 1 and 'bad object' in err
    assert loaded['roster'].read_text() == ROSTER


def test_pinned_paths_are_what_the_applets_run(loaded):
    repo = loaded['repo']
    (repo / 'pkg').mkdir()
    (repo / 'pkg' / '__init__.py').write_text('')
    (repo / 'teams' / 'home.yaml').write_text(
        'applets:\n  - name: applet-proxy-1\n    command: ["{python}", "-m", "pkg.applet"]\n    cwd: ..\n')
    workflow = dict(fs.workflow('route'), applets=[
        {'machine': 'field', 'fleet': 'field', 'roster_rows': 'teams/field-applet.yaml'},
        {'machine': 'field', 'fleet': 'field', 'host': {'subfleet': 'route-home', 'supervisor_slot': 'route-host-1',
                                                        'supervisor_role': 'teams/host.md',
                                                        'roster': 'teams/home.yaml'}},
        {'machine': 'home', 'fleet': 'main', 'roster_rows': 'teams/other.yaml'}])
    assert peer.pinned_paths(workflow, 'field', repo) == [
        'pkg', 'route.yaml', 'route_field.py', 'teams/field-applet.yaml', 'teams/home.yaml', 'teams/host.md']


def test_load_reports_why_an_applet_is_not_up(loaded):
    seq = iter([{'state': 'starting'}])  # before the rows are written; then the new applet crashloops

    class Seq(dict):
        def get(self, k, default=None):
            if k == 'fx:applet:applet-tunnel-1':
                return next(seq, {'state': 'crashloop'})
            return dict.get(self, k, default)
    loaded['redis'].hashes = Seq(loaded['redis'].hashes)
    code, _, err = main(['load', 'route', '--poll', '0', '--wait', '5'])
    assert code == 1 and 'applet-tunnel-1 crashloop' in err


def test_load_times_out_with_the_last_state(loaded):
    loaded['redis'].hashes['fx:applet:applet-tunnel-1'] = {'state': 'starting'}
    code, _, err = main(['load', 'route', '--wait', '0', '--poll', '0'])
    assert code == 1 and 'not up after 0s' in err and 'applet-tunnel-1 starting' in err


def test_load_twice_is_refused(loaded):
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    code, _, err = main(['load', 'route', '--poll', '0'])
    assert code == 1 and 'already loaded' in err


def test_load_refuses_an_entry_of_another_kind_and_names_the_fix(loaded):
    fs.save_workflow('route', dict(fs.workflow('route'), kind='note'))
    code, _, err = main(['load', 'route', '--poll', '0'])
    assert code == 1 and 'kind: note' in err and 'kind: workflow' in err
    assert loaded['roster'].read_text() == ROSTER


def test_host_item_adds_the_supervisor_row_and_the_subfleet_row(loaded):
    workflow = dict(fs.workflow('route'), applets=[{'machine': 'field', 'fleet': 'field', 'host': {
        'subfleet': 'route-home', 'supervisor_slot': 'route-host-1', 'supervisor_role': 'teams/host.md',
        'roster': 'teams/home.yaml'}}])
    fs.save_workflow('route', workflow)
    loaded['redis'].hashes['fx:subfleet:route-home'] = {'applets': json.dumps({'applet-proxy-1': 'up'})}
    before = loaded['roster'].read_bytes()
    code, _, err = main(['load', 'route', '--poll', '0'])
    assert code == 0, err
    data = yaml.safe_load(loaded['roster'].read_text())
    repo = loaded['repo']
    assert data['workers'][-1] == {'name': 'route-host-1', 'role': str(repo / 'teams/host.md')}
    assert data['subfleets'] == [{'name': 'route-home', 'supervisor_slot': 'route-host-1',
                                  'supervisor_role': str(repo / 'teams/host.md'),
                                  'roster': str(repo / 'teams/home.yaml'), 'autostart': True, 'mode': 'service'}]
    assert main(['unload', 'route'])[0] == 0
    assert loaded['roster'].read_bytes() == before


def two_machines(loaded, world, home_release, field_release='aaaa111', releases_code=0):
    """The workflow on home (fleet main) and field; the field run's daemon runs ``field_release``. The stubbed
    far side answers `superpowers releases` with ``home_release`` and every other command with exit 0. Returns
    the far commands seen, as word lists."""
    from fleetmux import releases
    workflow = dict(fs.workflow('route'), applets=[{'machine': 'home', 'fleet': 'main', 'roster_rows': 'x.yaml'}]
                  + fs.workflow('route')['applets'])
    fs.save_workflow('route', workflow)
    releases.record(loaded['run'], field_release)
    seen = []

    def ssh(argv, input, capture):
        words = shlex.split(argv[3])[1:]
        seen.append(words)
        if words[:2] == ['superpowers', 'releases']:
            if releases_code:
                return releases_code, ''
            return 0, json.dumps({'machine': 'home', 'fleets': [{'fleet': 'main', 'release': home_release}]})
        return 0, ''
    world.mp.setattr(peer, '_ssh', ssh)
    return seen


def test_far_half_is_one_command_per_machine(loaded, world):
    seen = two_machines(loaded, world, 'aaaa111')
    code, out, err = main(['load', 'route', '--poll', '0'])
    assert code == 0, err
    assert seen == [['superpowers', 'releases', '--json', '--fleet', 'main'],
                    ['superpowers', 'load', 'route', '--machine', 'home']]
    assert out.splitlines() == ['loaded route on home', 'loaded route on field']
    assert yaml.safe_load(loaded['roster'].read_text())['applets'][0]['name'] == 'applet-tunnel-1'
    assert 'release' not in err


def test_load_refuses_host_fleets_on_different_releases(loaded, world):
    seen = two_machines(loaded, world, 'bbbb222')
    before = loaded['roster'].read_bytes()
    code, out, err = main(['load', 'route', '--poll', '0'])
    assert code == 1 and out == ''
    assert 'run different releases' in err and '--allow-mixed-release' in err
    assert '  home\tmain\tbbbb222' in err and '  field\tfield\taaaa111' in err
    assert seen == [['superpowers', 'releases', '--json', '--fleet', 'main']]  # nothing loaded anywhere
    assert loaded['roster'].read_bytes() == before


def test_load_refuses_a_far_side_without_the_releases_command(loaded, world):
    seen = two_machines(loaded, world, None, releases_code=2)
    code, _, err = main(['load', 'route', '--poll', '0'])
    assert code == 1 and '  home\tmain\tunknown\t`fleetmux superpowers releases` exited 2' in err
    assert len(seen) == 1


def test_load_of_one_far_machine_still_compares_every_machine(loaded, world):
    seen = two_machines(loaded, world, 'bbbb222')
    code, _, err = main(['load', 'route', '--machine', 'home', '--poll', '0'])
    assert code == 1 and '  home\tmain\tbbbb222' in err and '  field\tfield\taaaa111' in err
    assert seen == [['superpowers', 'releases', '--json', '--fleet', 'main']]


def test_load_of_one_far_machine_refuses_one_without_the_releases_command(loaded, world):
    two_machines(loaded, world, None, releases_code=2)
    code, _, err = main(['load', 'route', '--machine', 'home', '--poll', '0'])
    assert code == 1 and '  home\tmain\tunknown' in err


def test_a_far_machine_that_does_not_connect_is_listed_as_unknown(loaded, world):
    two_machines(loaded, world, 'aaaa111')
    world.probe_code = 255
    code, _, err = main(['load', 'route', '--poll', '0'])
    assert code == 1 and '  home\tmain\tunknown\tno reach entry of home connects' in err


def test_allow_mixed_release_prints_the_table_and_loads(loaded, world):
    seen = two_machines(loaded, world, 'bbbb222')
    code, out, err = main(['load', 'route', '--poll', '0', '--allow-mixed-release'])
    assert code == 0, err
    assert 'loading route with mixed releases (--allow-mixed-release)' in err
    assert '  home\tmain\tbbbb222' in err and '  field\tfield\taaaa111' in err
    assert seen[1] == ['superpowers', 'load', 'route', '--machine', 'home']  # the far half needs no flag
    assert out.splitlines() == ['loaded route on home', 'loaded route on field']


def test_the_far_half_does_not_compare_again(loaded, world):
    from fleetmux import releases
    releases.record(loaded['run'], 'aaaa111')
    world.mp.setattr(peer, 'far', lambda *a, **k: pytest.fail('the far half never calls back'))
    code, _, err = main(['load', 'route', '--machine', 'field', '--poll', '0'])
    assert code == 0, err


def test_releases_lists_what_each_host_fleet_runs(loaded):
    from fleetmux import releases
    releases.record(loaded['run'], 'aaaa111')
    code, out, err = main(['releases', '--fleet', 'field', '--fleet', 'nowhere', '--json'])
    assert code == 0, err
    found = json.loads(out)
    assert found['machine'] == 'field' and found['installed'] is None
    assert found['fleets'][0] == {'fleet': 'field', 'config': str(loaded['run'] / 'config.json'),
                                  'release': 'aaaa111'}
    assert found['fleets'][1]['fleet'] == 'nowhere' and 'no fleet nowhere' in found['fleets'][1]['error']
    assert json.loads(main(['releases', '--json'])[1])['fleets'] == []  # no daemon holds the run's lock
    with (loaded['run'] / 'daemon.lock').open('a+') as lock:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert [r['release'] for r in json.loads(main(['releases', '--json'])[1])['fleets']] == ['aaaa111']


def test_unload_never_touches_the_far_side_by_itself(loaded, world):
    seen = two_machines(loaded, world, 'aaaa111')
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    seen.clear()
    assert main(['unload', 'route'])[0] == 0
    assert seen == []
    assert main(['unload', 'route', '--machine', 'home'])[0] == 0
    assert seen == [['superpowers', 'unload', 'route', '--machine', 'home']]


def test_unload_refuses_a_roster_changed_after_load(loaded):
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    loaded['roster'].write_text(loaded['roster'].read_text() + '# edited\n')
    code, _, err = main(['unload', 'route'])
    assert code == 1 and 'changed after load' in err


def test_commands_are_registered_from_the_extension():
    names = set(cli.parser()._subparsers._group_actions[0].choices)
    assert {'sync', 'save', 'load', 'unload', 'refresh', 'add', 'show', 'import', 'check'} <= names


# -- follow-up cases ---------------------------------------------------------------------------------------

def test_sync_merges_the_owner_reach_with_ours_both_ways(world, monkeypatch):
    from fleetmux import fleet_superpowers_refresh
    monkeypatch.setattr(fleet_superpowers_refresh, 'refresh', fake_refresh)
    fs.add_copy('home', 'home-ovl', via='overlay')  # added here, not declared by home
    fs.add_copy('field', 'field-ovl', via='overlay')  # the owner registers its own way with the same command
    entry = fs.machine('home', path=world.home_file)
    fs.write_self(dict(entry, reach=entry['reach'] + [{'via': 'tunnel', 'ssh': 'home-relay'}]), path=world.home_file)
    for _ in range(2):  # a repeat sync changes nothing
        code, _, err = main(['sync', 'home'])
        assert code == 0, err
        assert fs.machine('home')['reach'] == [{'via': 'lan', 'ssh': 'user@home.example'},
                                               {'via': 'overlay', 'ssh': 'home-ovl'},
                                               {'via': 'tunnel', 'ssh': 'home-relay'}]
        assert fs.machine('field', path=world.home_file)['reach'] == [
            {'via': 'lan', 'ssh': 'user@field.example'}, {'via': 'overlay', 'ssh': 'field-ovl'}]
    assert fs.machine('field')['source'] == 'self'


def test_sync_keeps_our_reach_when_the_far_section_has_none(world, monkeypatch):
    from fleetmux import fleet_superpowers_refresh
    monkeypatch.setattr(fleet_superpowers_refresh, 'refresh', fake_refresh)
    entry = fs.machine('home', path=world.home_file)
    fs.write_self(dict(entry, reach=[]), path=world.home_file)  # home after its own refresh: no inbound reach
    code, _, err = main(['sync', 'home'])
    assert code == 0, err
    assert fs.machine('home')['reach'] == [{'via': 'lan', 'ssh': 'user@home.example'}]
    assert fs.machine('field', path=world.home_file)['source'] == 'copy'  # the import half ran
    assert len(world.far_calls) == 2 and world.probes == ['user@home.example']  # one pick for both calls


def test_sync_refuses_a_far_machine_with_another_self_name(world, monkeypatch):
    from fleetmux import fleet_superpowers_refresh
    monkeypatch.setattr(fleet_superpowers_refresh, 'refresh', fake_refresh)
    text = world.home_file.read_text().replace('  home:\n', '  other:\n', 1)
    world.home_file.write_text(text)
    before = fs.machine('home')
    code, _, err = main(['sync', 'home'])
    assert code == 1 and 'calls itself other' in err
    assert fs.machine('home') == before and world.far_calls == [['superpowers', 'show', '--self', '--json']]


def test_load_follows_the_roster_the_daemon_follows(loaded, tmp_path):
    cfg_path = loaded['run'] / 'config.json'
    cfg = json.loads(cfg_path.read_text())
    del cfg['roster']  # started without --roster: the daemon follows <run>/roster.yaml
    cfg_path.write_text(json.dumps(cfg))
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    assert yaml.safe_load(loaded['roster'].read_text())['applets'][0]['name'] == 'applet-tunnel-1'


def test_every_check_runs_before_any_roster_is_written(loaded, tmp_path):
    _, outside = make_fleet(tmp_path, 'second', roster_inside=False)
    workflow = fs.workflow('route')
    workflow['applets'] = workflow['applets'] + [{'machine': 'field', 'fleet': 'second', 'roster_rows':
                                              'teams/field-applet.yaml'}]
    fs.save_workflow('route', workflow)
    code, _, err = main(['load', 'route'])
    assert code == 1 and 'outside its run directory' in err
    assert loaded['roster'].read_text() == ROSTER  # the first item was not written either


def test_a_dead_fleet_is_refused_before_writing(loaded, monkeypatch):
    def down(cfg):
        raise ConnectionError('no such socket')
    monkeypatch.setattr(peer, 'connect', down)
    code, _, err = main(['load', 'route'])
    assert code == 1 and 'does not answer' in err
    assert loaded['roster'].read_text() == ROSTER


def test_a_stale_crashloop_from_an_earlier_load_is_not_the_answer(loaded):
    seq = iter([{'state': 'crashloop'}, {'state': 'crashloop'}, {'state': 'starting'}, {'state': 'up'}])
    hashes = loaded['redis'].hashes

    class Seq(dict):
        def get(self, k, default=None):
            if k == 'fx:applet:applet-tunnel-1':
                return next(seq, {'state': 'up'})
            return dict.get(self, k, default)
    loaded['redis'].hashes = Seq(hashes)
    code, _, err = main(['load', 'route', '--poll', '0'])
    assert code == 0, err


def test_a_missing_commit_is_fetched_from_the_source_machine(loaded, monkeypatch):
    calls, have = [], {'n': 0}
    real = peer._git

    def git_stub(repo, *args):
        calls.append(args)
        if args[0] == 'cat-file':
            have['n'] += 1
            return subprocess.CompletedProcess(args, 0 if have['n'] > 1 else 1, '', '')
        if args[0] == 'fetch':
            return subprocess.CompletedProcess(args, 0, '', '')
        return real(repo, *args)
    monkeypatch.setattr(peer, '_git', git_stub)
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    assert ('fetch', '--quiet', 'user@home.example:' + str(loaded['repo'])) in calls
    assert [c[0] for c in calls][:3] == ['cat-file', 'fetch', 'cat-file']


# -- source.repo under the home directory (the same workflow on machines with different user names) ----------

def home_relative(loaded, monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    workflow = dict(WORKFLOW_ROOT, source={'repo': '~/applets/route', 'ref': loaded['ref'], 'from': 'home'})
    fs.save_workflow('route', workflow)


def test_a_home_relative_source_repo_is_expanded_on_this_machine(loaded, monkeypatch, tmp_path):
    home_relative(loaded, monkeypatch, tmp_path)
    code, out, err = main(['load', 'route', '--poll', '0'])
    assert code == 0, err
    rows = yaml.safe_load(loaded['roster'].read_text())['applets']
    assert rows[0]['cwd'] == str(loaded['repo'].resolve())


def test_a_home_relative_source_repo_is_fetched_from_the_origin_users_home(loaded, monkeypatch, tmp_path):
    home_relative(loaded, monkeypatch, tmp_path)
    calls, have = [], {'n': 0}
    real = peer._git

    def git_stub(repo, *args):
        calls.append((str(repo),) + args)
        if args[0] == 'cat-file':
            have['n'] += 1
            return subprocess.CompletedProcess(args, 0 if have['n'] > 1 else 1, '', '')
        if args[0] == 'fetch':
            return subprocess.CompletedProcess(args, 0, '', '')
        return real(repo, *args)
    monkeypatch.setattr(peer, '_git', git_stub)
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    # git-upload-pack on the origin expands ~/ against that user's home (scp-style host:~/path)
    assert (str(loaded['repo']), 'fetch', '--quiet', 'user@home.example:~/applets/route') in calls


# -- two fleets with one name: a sub-fleet inherits its parent's fleet name ---------------------------------

def same_name_rows(tmp_path, rows):
    """``machines.field.fleets`` = ``rows`` of (mode, parent or None, roster text), all named field."""
    fleets = []
    for i, (mode, parent, text) in enumerate(rows):
        run = tmp_path / ('run-same-%d' % i)
        run.mkdir()
        (run / 'roster.yaml').write_text(text)
        cfg = {'run': str(run), 'roster': str(run / 'roster.yaml'), 'mode': mode}
        if parent:
            cfg['parent_config'] = parent
        (run / 'config.json').write_text(json.dumps(cfg))
        fleets.append({'name': 'field', 'config': str(run / 'config.json'), 'mode': mode, 'running': True})
    fs.write_self(dict(fs.machine('field'), fleets=fleets))
    return [tmp_path / ('run-same-%d' % i) / 'roster.yaml' for i in range(len(rows))]


def test_load_picks_the_root_service_fleet_over_its_sub_fleet_of_the_same_name(loaded, tmp_path):
    sub, root = same_name_rows(tmp_path, [('task', str(tmp_path / 'run-same-1' / 'config.json'), ROSTER),
                                          ('service', None, ROSTER)])
    code, out, err = main(['load', 'route', '--poll', '0'])
    assert code == 0, err
    assert sub.read_text() == ROSTER
    assert [r['name'] for r in yaml.safe_load(root.read_text())['applets']] == ['applet-tunnel-1']


def test_load_refuses_when_more_than_one_root_fleet_has_the_name(loaded, tmp_path):
    first, second = same_name_rows(tmp_path, [('service', None, ROSTER), ('service', None, ROSTER)])
    code, out, err = main(['load', 'route', '--poll', '0'])
    assert code != 0 and '2 root service fleets named field' in err
    assert first.read_text() == ROSTER and second.read_text() == ROSTER


def test_load_picks_the_host_by_the_root_field_of_the_rows(loaded, tmp_path):
    """Rows written by refresh carry root / parent / sub; the host choice follows them, not a config read."""
    first, second = same_name_rows(tmp_path, [('service', None, ROSTER), ('service', None, ROSTER)])
    rows = fs.machine('field')['fleets']
    rows[0].update(root=False, parent='field', sub='route-home')
    rows[1].update(root=True)
    fs.write_self(dict(fs.machine('field'), fleets=rows))
    code, out, err = main(['load', 'route', '--poll', '0'])
    assert code == 0, err
    assert first.read_text() == ROSTER
    assert [r['name'] for r in yaml.safe_load(second.read_text())['applets']] == ['applet-tunnel-1']


def test_load_refuses_when_no_fleet_of_the_name_is_a_root_service_fleet(loaded, tmp_path):
    same_name_rows(tmp_path, [('task', '/parent.json', ROSTER), ('task', None, ROSTER)])
    code, out, err = main(['load', 'route', '--poll', '0'])
    assert code != 0 and '2 fleets named field' in err and 'none is a root service fleet' in err


# -- the processes behind load and unload --------------------------------------------------------------------

def test_unload_returns_only_after_the_applet_process_is_gone(loaded):
    """The daemon sees the removed row a few ticks late: unload waits for it instead of returning at once."""
    host = loaded['host']
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    pid = host.pid(loaded['run'])
    host.lag, host.ticks = 3, 0
    peer.unload_here('route')
    assert pid not in host.alive and host.ticks >= 3
    assert loaded['roster'].read_text() == ROSTER
    assert not (loaded['run'] / peer.RECORDS / 'route.json').exists()


def test_unload_fails_at_its_bound_naming_the_applet_still_running(loaded, monkeypatch):
    host = loaded['host']
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    pid = host.pid(loaded['run'])
    host.never_stop = True
    clock = [0.0]
    monkeypatch.setattr(peer, '_clock', lambda: clock[0], raising=False)
    monkeypatch.setattr(peer, '_sleep', lambda s: clock.__setitem__(0, clock[0] + 1), raising=False)
    code, _, err = main(['unload', 'route'])
    # the row's detach_wait_s (60) + stop_grace_s (30) + the margin, all defaults
    assert code == 1 and 'still running after 95s' in err and 'applet-tunnel-1 (pid %d' % pid in err
    assert clock[0] == 95
    assert loaded['roster'].read_text() == ROSTER  # the rows are out; the record stays for a second unload
    host.never_stop = False
    host.apply(loaded['run'], [], [])  # the daemon gets to it after all
    code, _, err = main(['unload', 'route'])
    assert code == 0, err
    assert not (loaded['run'] / peer.RECORDS / 'route.json').exists()


def test_unload_wait_overrides_the_bound(loaded, monkeypatch):
    loaded['host'].never_stop = True
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    clock = [0.0]
    monkeypatch.setattr(peer, '_clock', lambda: clock[0], raising=False)
    monkeypatch.setattr(peer, '_sleep', lambda s: clock.__setitem__(0, clock[0] + 1), raising=False)
    code, _, err = main(['unload', 'route', '--wait', '7'])
    assert code == 1 and 'still running after 7s' in err and clock[0] == 7


def test_load_waits_for_a_new_process_not_a_leftover_up_state(loaded):
    """The hash says up before load (an earlier load): only a process started by this load counts."""
    host = loaded['host']
    host.lag = 2
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    assert host.pid(loaded['run']) in host.alive


def test_unload_then_load_gives_a_new_process(loaded):
    host = loaded['host']
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    first = host.pid(loaded['run'])
    host.lag = 2
    assert main(['unload', 'route'])[0] == 0  # each pause is one stubbed daemon tick: nothing sleeps
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    second = host.pid(loaded['run'])
    assert second != first and second in host.alive and first not in host.alive


def test_load_refuses_while_an_applet_of_the_same_name_still_runs(loaded):
    """Rule: load never stops a process it did not start; it refuses before writing anything."""
    host = loaded['host']
    pid = host.start(loaded['run'], 'applet-tunnel-1')  # e.g. a row removed by hand, still detaching
    code, _, err = main(['load', 'route', '--poll', '0'])
    assert code == 1 and 'applet-tunnel-1 is still running (pid %d' % pid in err
    assert loaded['roster'].read_text() == ROSTER
    assert not (loaded['run'] / peer.RECORDS / 'route.json').exists()


def test_a_host_item_waits_for_the_sub_fleet_applets(loaded):
    workflow = dict(fs.workflow('route'), applets=[{'machine': 'field', 'fleet': 'field', 'host': {
        'subfleet': 'route-home', 'supervisor_slot': 'route-host-1', 'supervisor_role': 'teams/host.md',
        'roster': 'teams/home.yaml'}}])
    fs.save_workflow('route', workflow)
    loaded['redis'].hashes['fx:subfleet:route-home'] = {'applets': json.dumps({'applet-proxy-1': 'up'})}
    host = loaded['host']
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    child = loaded['redis'].hashes['fx:subfleet:route-home']['run_dir']
    pid = host.pid(child, 'applet-proxy-1')
    host.lag, host.ticks = 2, 0
    peer.unload_here('route')
    assert pid not in host.alive and host.ticks >= 2


# -- against the real host tick (applet_host.tick, fake processes) --------------------------------------------

from test_message_bus_v2 import private_redis_v2  # noqa: E402,F401  (fixture: the repo's private test Redis)
from test_runtime_v2 import fx  # noqa: E402,F401  (a v2 service fleet)


class Proc:
    _pid = [8_000_000]

    def __init__(self):
        Proc._pid[0] += 1
        self.pid, self.code = Proc._pid[0], None

    def poll(self):
        return self.code


def test_unload_then_load_restarts_the_applet_on_the_real_host_tick(world, fx, tmp_path, monkeypatch):
    """The field bug: unload returned before the daemon saw the row go, load put it back, the detach was
    cancelled and the old process (old code) kept running. Each pause of the waiting command is one tick."""
    from fleetmux import applet_host, roster as roster_mod
    f = fx
    repo, ref = make_repo(tmp_path)
    fs.save_workflow('route', dict(WORKFLOW_ROOT, source={'repo': str(repo), 'ref': ref, 'from': 'home'}))
    run = Path(f.cfg['run'])
    roster = run / 'roster.yaml'
    roster.write_text(ROSTER)
    fs.write_self(dict(fs.machine('field'), fleets=[{'name': 'field', 'config': str(run / 'config.json'),
                                                     'running': True}]))
    monkeypatch.setattr(peer, 'connect', lambda cfg: f.r)
    procs, clock = [], [1000.0]
    f.applet_clock = lambda: clock[0]
    monkeypatch.setattr(applet_host, 'clear_stale', lambda *a, **k: None)

    def signal_(pid, sig, group=False):
        for p in procs:
            if p.pid == pid and p.code is None:
                p.code = -sig
        return True
    monkeypatch.setattr(applet_host, '_signal', signal_)

    def spawn(cfg, spec, token):
        procs.append(Proc())
        return procs[-1]
    f.applet_spawn = spawn

    def tick(*_):
        clock[0] += 1
        f.applet_specs = roster_mod.load_applets(roster)
        for p in procs:
            if p.code is None:
                f.r.hset(applet_host.key('applet-tunnel-1'), 'hb_at', repr(clock[0]))
        applet_host.tick(f)
    monkeypatch.setattr(peer, '_sleep', tick, raising=False)
    monkeypatch.setattr(peer, 'alive', lambda pid: any(p.pid == pid and p.code is None for p in procs),
                        raising=False)
    real_states, first_load = peer.states, [True]
    monkeypatch.setattr(peer, 'states', lambda cfg, added: (first_load[0] and tick(), real_states(cfg, added))[1])

    peer.load_here('route', poll=0)
    first_load[0] = False  # from here on the daemon ticks only when the command pauses
    pid_file = applet_host.run_dir(f.cfg, 'applet-tunnel-1') / 'pid'
    first = int(pid_file.read_text())
    assert f.r.hget(applet_host.key('applet-tunnel-1'), 'state') == 'up'
    peer.unload_here('route')
    assert all(p.code is not None for p in procs)  # no process left when unload returns
    peer.load_here('route', poll=0)
    second = int(pid_file.read_text())
    assert second != first and [p.pid for p in procs if p.code is None] == [second]


def test_alive_means_a_live_session_leader(tmp_path):
    """The host starts each applet as a session leader; a recycled pid of another process does not count."""
    leader = subprocess.Popen(['sleep', '30'], start_new_session=True)
    member = subprocess.Popen(['sleep', '30'])
    try:
        assert peer.alive(leader.pid) and not peer.alive(member.pid)
    finally:
        for proc in (leader, member):
            proc.kill()
            proc.wait()
    assert not peer.alive(leader.pid) and not peer.alive(None) and not peer.alive(0)
