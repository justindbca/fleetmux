"""Bare `fleetmux` in a host terminal with no root fleet: start the bare root, then attach at once (docs/design.md
section 50, "Bare `fleetmux`", item 5, "The window").

The `world` of tests/test_open_window_bare.py: no root anywhere, `fleet.run_start` a fake that writes latest.json,
the clock of the wait a fake, every process stubbed, the user's home under tmp.
"""
import pytest

from fleetmux import fleet, gui_keep  # noqa: E402
from test_open_window_bare import (BIN, DAEMON_GUI_PID, fresh_runtime, no_real_process, private,  # noqa: E402,F401
                                   world)

HOST_VARS = ('HC_TERMINAL_ID', 'HC_SCOPE', 'HC_TERMINAL_SOCKET')


@pytest.fixture
def host(world, monkeypatch):
    for name in HOST_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HC_TERMINAL_ID', 't1')
    monkeypatch.setattr(fleet, 'interactive_terminal', lambda: True)
    monkeypatch.setattr(fleet, 'inside_fleetmux_pane', lambda: False)
    return world


def test_no_root_in_host_terminal_starts_then_attaches_without_waiting(host):
    fleet.main(['gui'])
    assert len(host['starts']) == 1, host['starts']
    argv, env = host['starts'][0]
    assert argv[:2] == ['--lifecycle', 'service'] and '--no-view' not in argv, argv
    assert host['execs'] == [(BIN, [BIN, 'attach', '--runtime', fresh_runtime(host)])], host['execs']
    assert host['clock'].slept == [], 'no wait for the daemon window: %r' % (host['clock'].slept,)
    assert host.get('scanned') is None and host['order'] == ['start'], (host.get('scanned'), host['order'])
    assert host['spawned'] == [] and host['raised'] == [], (host['spawned'], host['raised'])


def test_no_root_in_host_terminal_without_a_tty_waits_for_the_daemon_window(host, monkeypatch):
    monkeypatch.setattr(fleet, 'interactive_terminal', lambda: False)
    host['gui_pids'] = [None, DAEMON_GUI_PID]
    fleet.main(['gui'])
    assert host['execs'] == [], host['execs']
    assert host['raised'] == [DAEMON_GUI_PID] and host['spawned'] == [], (host['raised'], host['spawned'])
    assert host['clock'].slept, 'the daemon window is waited for as before'


def test_no_root_with_an_empty_list_waits_for_the_daemon_window(host, monkeypatch):
    private(host['tmp'], monkeypatch, {'bare': {'host_terminal_env': []}})
    host['gui_pids'] = [DAEMON_GUI_PID]
    fleet.main(['gui'])
    assert host['execs'] == [] and host['raised'] == [DAEMON_GUI_PID], (host['execs'], host['raised'])


@pytest.mark.parametrize('value', ['HC_TERMINAL_ID', None, [1], ['A=B']])
def test_a_bad_list_is_refused_before_any_start(host, monkeypatch, value):
    private(host['tmp'], monkeypatch, {'bare': {'host_terminal_env': value}})
    with pytest.raises(SystemExit) as exc:
        fleet.main(['gui'])
    assert 'bare' in str(exc.value.code) and 'host_terminal_env' in str(exc.value.code), exc.value.code
    assert host['starts'] == [] and host['execs'] == [] and host['spawned'] == []


@pytest.mark.parametrize('value', ['HC_TERMINAL_ID', None, [1], ['A=B']])
@pytest.mark.parametrize('why', ['ssh', 'no-desktop'])
def test_a_bad_list_does_not_stop_the_ssh_or_no_desktop_start(host, monkeypatch, value, why):
    """P4: over ssh / without a desktop the start-then-attach path is the one before this rule."""
    private(host['tmp'], monkeypatch, {'bare': {'host_terminal_env': value}})
    if why == 'ssh':
        monkeypatch.setenv('SSH_CONNECTION', '10.0.0.2 50000 10.0.0.1 22')
    else:
        monkeypatch.setattr(fleet, 'has_desktop', lambda: False)
        monkeypatch.setattr(fleet, 'gui_problem', lambda: 'gui needs a desktop -- use attach instead')
    fleet.main(['gui'])
    assert len(host['starts']) == 1, host['starts']
    assert host['execs'] == [(BIN, [BIN, 'attach', '--runtime', fresh_runtime(host)])], host['execs']
    assert host['clock'].slept == [] and host['spawned'] == [] and host['raised'] == []


def test_the_root_daemon_still_keeps_its_window(host):
    """The started root's daemon keeps its desktop window (gui.keep_open): attach here does not turn it off."""
    from fleetmux import gui_keep as keep
    assert keep.settings()['keep_open'] is True
    fleet.main(['gui'])
    argv, env = host['starts'][0]
    assert not [a for a in argv if a in ('--no-view', '--attach', '--gui')], argv
    assert gui_keep is keep
