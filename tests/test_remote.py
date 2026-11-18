"""`fleetmux remote` / `fleetmux view` (design.md section 55): the fleet runs on another machine of yours."""
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

import test_fleet  # noqa: F401  (puts the checkout on sys.path)
from fleetmux import fleet, fleet_config, fleet_superpowers, remote


def _private(tmp_path, monkeypatch, block):
    user = tmp_path / 'fleet.yaml'
    user.write_text(yaml.safe_dump({'remote': block}))
    monkeypatch.setenv(fleet_config.USER_ENV, str(user))


# -- fleet.yaml#remote and the machines block of fleet-superpowers -------------------------------------

MACHINES = {
    'field': {'source': 'self', 'reach': [], 'fleetmux': {'bin': '~/fleetmux-current/bin/fleetmux'}},
    'home': {'source': 'copy', 'reach': [{'via': 'lan', 'ssh': 'user@home.lan.example'},
                                         {'via': 'overlay', 'ssh': 'user@home.example'}],
             'fleetmux': {'bin': '/opt/fleetmux/bin/fleetmux'}},
}


def test_shipped_block_has_only_default_and_no_hosts():
    shipped = yaml.safe_load(fleet_config.SHIPPED.read_text())['remote']
    assert shipped == {'default': '', 'reach_order': list(remote.REACH_ORDER)} and 'hosts' not in shipped
    assert remote.REACH_ORDER == ('thunderbolt', 'lan', 'overlay', 'tunnel', 'tunnel-overseas')
    assert not hasattr(remote, 'HOST_KEYS')
    assert 'remote' in fleet_config.BLOCKS and 'remote' in fleet_config.USER_BLOCKS


def test_host_comes_from_the_machines_block():
    assert remote.host('home', MACHINES) == {
        'name': 'home', 'reach': ['user@home.lan.example', 'user@home.example'], 'bin': '/opt/fleetmux/bin/fleetmux'}
    no_bin = {'home': {'reach': [{'via': 'lan', 'ssh': 'home-alias'}]}}
    assert remote.host('home', no_bin)['bin'] == remote.DEFAULT_BIN
    with pytest.raises(ValueError, match="no machine 'attic'.*field, home"):
        remote.host('attic', MACHINES)
    with pytest.raises(ValueError, match='machines.field.reach is empty'):
        remote.host('field', MACHINES)


def test_default_comes_from_the_private_file_or_the_only_other_machine(tmp_path, monkeypatch):
    assert remote.host(None, MACHINES)['name'] == 'home'  # field is self: home is the only other machine
    two = dict(MACHINES, office={'source': 'copy', 'reach': [{'via': 'lan', 'ssh': 'office'}]})
    with pytest.raises(ValueError, match='remote.default'):
        remote.host(None, two)
    _private(tmp_path, monkeypatch, {'default': 'office'})
    assert remote.host(None, two)['reach'] == ['office']


def test_a_leftover_hosts_table_is_refused(tmp_path, monkeypatch):
    _private(tmp_path, monkeypatch, {'default': 'home', 'hosts': {'home': {'ssh': 'home.example'}}})
    with pytest.raises(ValueError, match='fleet.yaml#remote.hosts.*fleet-superpowers'):
        remote.host(None, MACHINES)


@pytest.mark.parametrize('entry', ['home', {'reach': 'user@home.example'}, {'reach': [{'via': 'lan'}]},
                                   {'reach': [{'ssh': '-oProxyCommand=x'}]}, {'reach': [{'ssh': 'a b'}]},
                                   {'reach': [{'ssh': 'home'}], 'fleetmux': {'bin': ''}}])
def test_a_bad_machine_entry_is_refused(entry):
    with pytest.raises(ValueError, match='machines.home'):
        remote.host('home', {'home': entry})


# -- reach: one fixed order of route kinds ---------------------------------------------------------------

def _home(*rows):
    return {'home': {'source': 'copy', 'reach': [{'via': v, 'ssh': s} for v, s in rows]}}


def test_entries_are_tried_by_kind_not_by_list_position():
    found = remote.host('home', _home(('tunnel', 'relay'), ('lan', 'home-lan'), ('overlay', 'home-ovl')))
    assert found['reach'] == ['home-lan', 'home-ovl', 'relay']
    found = remote.host('home', _home(('tunnel', 'relay'), ('lan', 'home-lan'), ('thunderbolt', 'home-tb')))
    assert found['reach'] == ['home-tb', 'home-lan', 'relay']
    found = remote.host('home', _home(('tunnel-overseas', 'relay-far'), ('tunnel', 'relay-near')))
    assert found['reach'] == ['relay-near', 'relay-far']  # domestic relay before overseas relay


def test_same_kind_keeps_list_order_and_an_unknown_kind_goes_last():
    found = remote.host('home', _home(('jump', 'old-jump'), ('overlay', 'ovl-b'), ('pigeon', 'bird'),
                                      ('overlay', 'ovl-a'), ('lan', 'home-lan')))
    assert found['reach'] == ['home-lan', 'ovl-b', 'ovl-a', 'old-jump', 'bird']


def test_the_first_kind_that_connects_is_used_and_later_ones_are_not_tried(monkeypatch):
    target = remote.host('home', _home(('tunnel', 'relay'), ('overlay', 'home-ovl'), ('lan', 'home-lan')))
    ssh = _Ssh({'home-lan': 255, 'home-ovl': 0, 'relay': 0})
    monkeypatch.setattr(remote.subprocess, 'call', ssh)
    assert remote.run(target, ['status']) == 0
    assert ssh.calls == [remote.probe_argv('home-lan'), remote.probe_argv('home-ovl'),
                         remote.ssh_argv(dict(target, ssh='home-ovl'), ['status'])]


def test_the_order_is_a_setting_of_the_remote_block(tmp_path, monkeypatch):
    rows = _home(('lan', 'home-lan'), ('overlay', 'home-ovl'))
    _private(tmp_path, monkeypatch, {'default': 'home'})  # a private block without the key: the built-in order
    assert remote.reach_order() == remote.REACH_ORDER
    assert remote.host(None, rows)['reach'] == ['home-lan', 'home-ovl']
    _private(tmp_path, monkeypatch, {'reach_order': ['overlay', 'lan']})
    assert remote.host('home', rows)['reach'] == ['home-ovl', 'home-lan']
    for bad in ([], 'lan', ['lan', 'lan'], ['lan', '']):
        _private(tmp_path, monkeypatch, {'reach_order': bad})
        with pytest.raises(ValueError, match='fleet.yaml#remote.reach_order'):
            remote.reach_order()


# -- reach: probe in order, then run once ---------------------------------------------------------------

HOME = {'name': 'home', 'reach': ['user@home.lan.example', 'user@home.example'], 'bin': remote.DEFAULT_BIN}


class _Ssh:
    """A stubbed ssh: `codes` maps a destination to the exit code of its probe; the command exits `cmd`."""

    def __init__(self, codes, cmd=0):
        self.codes, self.cmd, self.calls = codes, cmd, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[-1] == 'true':
            assert kw.get('stdin') == subprocess.DEVNULL  # a probe never eats a piped `request -`
            return self.codes[argv[-2]]
        return self.cmd


def test_probe_is_ssh_true_with_a_connect_timeout(monkeypatch):
    ssh = _Ssh({'user@home.lan.example': 0})
    monkeypatch.setattr(remote.subprocess, 'call', ssh)
    assert remote.pick(HOME) == 'user@home.lan.example'
    assert ssh.calls == [['ssh', '-o', 'ConnectTimeout=5', '--', 'user@home.lan.example', 'true']]


def test_a_dead_first_reach_falls_through_to_the_next(monkeypatch):
    ssh = _Ssh({'user@home.lan.example': 255, 'user@home.example': 0})
    monkeypatch.setattr(remote.subprocess, 'call', ssh)
    assert remote.run(HOME, ['status']) == 0
    assert ssh.calls == [remote.probe_argv('user@home.lan.example'), remote.probe_argv('user@home.example'),
                         remote.ssh_argv(dict(HOME, ssh='user@home.example'), ['status'])]


def test_exit_255_from_the_command_is_never_rerun(monkeypatch, capsys):
    ssh = _Ssh({'user@home.lan.example': 0, 'user@home.example': 0}, cmd=255)
    monkeypatch.setattr(remote.subprocess, 'call', ssh)
    assert remote.run(HOME, ['operator', 'hello']) == 255
    commands = [c for c in ssh.calls if c[-1] != 'true']
    assert commands == [remote.ssh_argv(dict(HOME, ssh='user@home.lan.example'), ['operator', 'hello'])]
    assert len(ssh.calls) == 2  # one probe, one command; the second reach entry is never touched


def test_no_reach_entry_connects_is_255_and_runs_nothing(monkeypatch, capsys):
    ssh = _Ssh({'user@home.lan.example': 255, 'user@home.example': 255})
    monkeypatch.setattr(remote.subprocess, 'call', ssh)
    assert remote.run(HOME, ['status']) == 255
    assert all(c[-1] == 'true' for c in ssh.calls) and len(ssh.calls) == 2
    err = capsys.readouterr().err
    assert 'user@home.lan.example' in err and 'user@home.example' in err


def test_the_connect_timeout_is_a_parameter(monkeypatch):
    assert remote.probe_argv('home-alias', timeout=9)[1:3] == ['-o', 'ConnectTimeout=9']
    assert remote.CONNECT_TIMEOUT_S == 5


# -- the ssh command -----------------------------------------------------------------------------------

HOME_HOST = {'name': 'home', 'ssh': 'user@home.example', 'bin': remote.DEFAULT_BIN, 'reach': ['user@home.example']}


def test_no_command_is_the_view_with_a_terminal():
    argv = remote.ssh_argv(HOME_HOST, [])
    assert argv == ['ssh', '-t', '--', 'user@home.example', '"$HOME"/fleetmux-current/bin/fleetmux view']


def test_a_command_runs_without_a_terminal_so_stdin_stays_clean():
    argv = remote.ssh_argv(HOME_HOST, ['task', '-'])
    assert argv == ['ssh', '--', 'user@home.example', '"$HOME"/fleetmux-current/bin/fleetmux task -']


def test_the_remote_shell_sees_the_arguments_as_typed(tmp_path):
    """ssh joins its arguments and the remote shell splits them again: run the string the way sshd does."""
    stub = tmp_path / 'my releases' / 'bin' / 'fleetmux'
    stub.parent.mkdir(parents=True)
    stub.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done\n')
    stub.chmod(0o755)
    args = ['operator', '{"text": "it\'s $HOME; ok"}', 'two words', '*']
    line = remote.ssh_argv(dict(HOME_HOST, bin='~/my releases/bin/fleetmux'), args)[-1]
    out = subprocess.run(['sh', '-c', line], env=dict(os.environ, HOME=str(tmp_path)), cwd=str(tmp_path),
                         capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == args


def test_run_passes_the_exit_code_and_explains_an_older_release(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(remote, 'pick', lambda target: 'user@home.example')
    monkeypatch.setattr(remote.subprocess, 'call', lambda argv: calls.append(argv) or 2)
    assert remote.run(HOME_HOST, []) == 2
    assert calls == [remote.ssh_argv(HOME_HOST, [])]
    assert 'older release' in capsys.readouterr().err  # argparse on the far side: invalid choice 'view'
    assert remote.run(HOME_HOST, ['status']) == 2
    assert capsys.readouterr().err == ''  # a fleet command's own exit 2 needs no comment


def test_cli_remote_reads_the_machines_block_and_exits_with_ssh(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(remote, '_machines', lambda: {'home': {'reach': [{'via': 'lan', 'ssh': 'home.example'}]}})
    ssh = _Ssh({'home.example': 0})
    monkeypatch.setattr(remote.subprocess, 'call', ssh)
    with pytest.raises(SystemExit) as stop:
        fleet.main(['remote', 'status', '--config', 'x.json'])
    assert stop.value.code == 0
    assert ssh.calls[-1] == ['ssh', '--', 'home.example', '"$HOME"/fleetmux-current/bin/fleetmux status --config x.json']
    with pytest.raises(SystemExit) as stop:
        fleet.main(['remote', '--host', 'attic'])
    assert stop.value.code == 2 and "no machine 'attic'" in capsys.readouterr().err


# -- fleetmux view -------------------------------------------------------------------------------------

class _Mgr:
    runtime_root = Path('/state/fleetmux/tok')


def _view_env(tmp_path, monkeypatch, latest=None, alive=True):
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    if latest is not None:
        (tmp_path / 'latest.json').write_text(json.dumps(latest))
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: alive)
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: _Mgr())
    monkeypatch.setattr('fleetmux.backends.fleetmux.fleetmux_bin', lambda: '/rel/bin/fleetmux')
    execs = []
    monkeypatch.setattr(fleet.os, 'execvp', lambda file, argv: execs.append(list(argv)))
    return execs


def test_view_attaches_to_the_live_run(tmp_path, monkeypatch):
    execs = _view_env(tmp_path, monkeypatch, {'run': str(tmp_path / 'run'), 'backend': 'fleetmux'})
    fleet.view()
    assert execs == [['/rel/bin/fleetmux', 'attach', '--runtime', '/state/fleetmux/tok']]


def test_view_without_a_running_fleet_is_one_line(tmp_path, monkeypatch):
    execs = _view_env(tmp_path, monkeypatch)  # no latest.json at all
    with pytest.raises(SystemExit) as stop:
        fleet.view()
    assert stop.value.code == fleet.VIEW_NO_RUN and not execs
    execs = _view_env(tmp_path, monkeypatch, {'run': str(tmp_path / 'run'), 'backend': 'fleetmux'}, alive=False)
    with pytest.raises(SystemExit) as stop:
        fleet.view()
    assert stop.value.code == fleet.VIEW_NO_RUN and not execs


def test_view_names_the_backend_it_needs(tmp_path, monkeypatch):
    execs = _view_env(tmp_path, monkeypatch, {'run': str(tmp_path / 'run'), 'backend': 'cmux'})
    with pytest.raises(SystemExit) as stop:
        fleet.view()
    assert '--backend fleetmux' in str(stop.value.code) and 'cmux' in str(stop.value.code) and not execs


# -- the inventory reader (fleetmux/fleet_superpowers.py) ---------------------------------------------

INVENTORY = """\
machines:
  field:
    source: self
    reach: []
  home:
    source: copy
    reach:
      - {via: lan, ssh: user@home.lan.example}
      - {via: overlay, ssh: user@home.example}
"""


def _inventory(tmp_path, monkeypatch, text):
    """A temporary HOME holding the inventory; the real ~/.fleetmux is never read."""
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('FLEETMUX_SUPERPOWERS', raising=False)
    path = tmp_path / '.fleetmux' / 'fleet-superpowers' / 'fleet-superpowers.yaml'
    path.parent.mkdir(parents=True)
    path.write_text(text)
    assert fleet_superpowers.file_path() == path
    return path


def _cli_remote(argv):
    with pytest.raises(SystemExit) as stop:
        fleet.main(['remote'] + argv)
    return stop.value.code


def test_cli_remote_reads_the_inventory_file(tmp_path, monkeypatch):
    _inventory(tmp_path, monkeypatch, INVENTORY)
    ssh = _Ssh({'user@home.lan.example': 255, 'user@home.example': 0})
    monkeypatch.setattr(remote.subprocess, 'call', ssh)
    assert _cli_remote(['status']) == 0  # home is the only other machine: no --host needed
    assert ssh.calls[-1] == ['ssh', '--', 'user@home.example',
                             '"$HOME"/fleetmux-current/bin/fleetmux status']


def test_cli_remote_unknown_machine_is_one_line_and_exit_2(tmp_path, monkeypatch, capsys):
    path = _inventory(tmp_path, monkeypatch, INVENTORY)
    monkeypatch.setattr(remote.subprocess, 'call', lambda *a, **k: pytest.fail('ssh must not run'))
    assert _cli_remote(['--host', 'attic', 'status']) == 2
    err = capsys.readouterr().err
    assert "no machine 'attic'" in err and 'field, home' in err and str(path) in err


def test_cli_remote_empty_reach_is_one_line_and_exit_2(tmp_path, monkeypatch, capsys):
    _inventory(tmp_path, monkeypatch, INVENTORY)
    monkeypatch.setattr(remote.subprocess, 'call', lambda *a, **k: pytest.fail('ssh must not run'))
    assert _cli_remote(['--host', 'field', 'status']) == 2
    assert 'machines.field.reach is empty' in capsys.readouterr().err


def test_cli_remote_bad_inventory_is_one_line_and_exit_2(tmp_path, monkeypatch, capsys):
    path = _inventory(tmp_path, monkeypatch, 'machines: [not, a, mapping]\n')
    monkeypatch.setattr(remote.subprocess, 'call', lambda *a, **k: pytest.fail('ssh must not run'))
    assert _cli_remote(['--host', 'home', 'status']) == 2
    err = capsys.readouterr().err
    assert err.startswith('fleetmux remote: fleet-superpowers inventory: ') and str(path) in err


def test_cli_remote_without_an_inventory_says_how_to_add_a_machine(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HOME', str(tmp_path))  # no inventory file at all
    assert _cli_remote(['--host', 'home', 'status']) == 2
    err = capsys.readouterr().err
    assert "no machine 'home'" in err and 'superpowers add' in err
