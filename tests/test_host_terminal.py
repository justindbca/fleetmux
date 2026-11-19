"""Bare `fleetmux` in a host terminal attaches in place (docs/design.md section 50, "Bare `fleetmux`", item 2).

A host terminal is a terminal that is itself a pane of another program (a Herdr terminal): one of the variables in
fleet.yaml#bare.host_terminal_env is set (non-empty). There, with stdin and stdout at a terminal and outside a
fleetmux pane, bare `fleetmux` execs `fleetmux attach --runtime <root runtime>` instead of raising or opening the
desktop window, also when the window is open. Everywhere else it behaves as before.

This file covers a live root fleet (the `world` of tests/test_open_window.py: tmp HOME, every process stubbed);
tests/test_host_terminal_bare.py covers the start of the bare root, tests/test_host_terminal_auto_view.py `start`'s
automatic view. Every test sets or removes the variables itself, so the runner's own terminal does not matter.
"""
import pytest
import yaml

from fleetmux import fleet, fleet_config, gui_keep, open_window  # noqa: E402
from test_open_window import BIN, GUI_PID, gui_window, no_real_process, world  # noqa: E402,F401
from test_open_window_bare import private  # noqa: E402

HOST_VARS = ('HC_TERMINAL_ID', 'HC_SCOPE', 'HC_TERMINAL_SOCKET', 'MY_HOST_PANE')


@pytest.fixture
def host(world, monkeypatch):
    """`world` in an ordinary terminal: no host variable, stdin / stdout at a terminal, not in a fleetmux pane
    (the pane check records its calls)."""
    for name in HOST_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(fleet, 'interactive_terminal', lambda: True)
    world['pane_checks'] = []
    monkeypatch.setattr(fleet, 'inside_fleetmux_pane', lambda: world['pane_checks'].append(1) and False)
    return world


def attach(w):
    return [(BIN, [BIN, 'attach', '--runtime', str(w['root_mgr'].runtime_root)])]


def window_path_taken(w, open_pid=None):
    """Today's desktop path: a raise of the open window, else one window started; never an exec."""
    assert w['execs'] == [], 'no attach outside the host-terminal rule: %r' % (w['execs'],)
    if open_pid:
        assert w['raised'] == [open_pid] and w['spawned'] == [], (w['raised'], w['spawned'])
    else:
        assert [s[0] for s in w['spawned']] == [gui_window(w)], w['spawned']


# ---- H1: a listed variable + a terminal: attach, also with the window open ---------------------------------------

def test_host_terminal_with_a_tty_execs_attach(host, monkeypatch):
    monkeypatch.setenv('HC_TERMINAL_ID', 't1')
    fleet.main(['gui'])
    assert host['execs'] == attach(host), host['execs']
    assert host['spawned'] == [] and host['raised'] == [], (host['spawned'], host['raised'])


def test_host_terminal_attaches_even_with_a_window_open(host, monkeypatch):
    monkeypatch.setenv('HC_TERMINAL_ID', 't1')
    monkeypatch.setattr(gui_keep, 'gui_running', lambda runtime: GUI_PID)
    fleet.main(['gui'])
    assert host['execs'] == attach(host), host['execs']
    assert host['raised'] == [] and host['spawned'] == [], (host['raised'], host['spawned'])


# ---- H2: no terminal, no listed variable, an empty list, inside a pane: as before --------------------------------

@pytest.mark.parametrize('open_pid', [None, GUI_PID])
def test_host_terminal_without_a_tty_keeps_the_window_path(host, monkeypatch, open_pid):
    monkeypatch.setenv('HC_TERMINAL_ID', 't1')
    monkeypatch.setattr(fleet, 'interactive_terminal', lambda: False)
    monkeypatch.setattr(gui_keep, 'gui_running', lambda runtime: open_pid)
    fleet.main(['gui'])
    window_path_taken(host, open_pid)


@pytest.mark.parametrize('env', [{}, {'HC_SCOPE': 'scope-1'}, {'HC_TERMINAL_ID': ''}])
def test_no_listed_variable_keeps_the_window_path(host, monkeypatch, env):
    """An ordinary terminal; a terminal-less `hc exec` (HC_SCOPE only); an empty value: none of them is a hit, and
    the ancestor scan for a fleetmux pane is not even run."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    fleet.main(['gui'])
    window_path_taken(host)
    assert host['pane_checks'] == [], 'an ordinary terminal runs no extra process scan'


def test_empty_list_turns_it_off(host, monkeypatch):
    private(host['tmp'], monkeypatch, {'bare': {'host_terminal_env': []}})
    monkeypatch.setenv('HC_TERMINAL_ID', 't1')
    fleet.main(['gui'])
    window_path_taken(host)


def test_inside_a_fleetmux_pane_the_variable_is_ignored(host, monkeypatch):
    """Panes inherit the environment of a fleet started in a host terminal: the root's own sidebar bash must not
    attach the server inside its own pane."""
    monkeypatch.setenv('HC_TERMINAL_ID', 't1')
    monkeypatch.setattr(fleet, 'inside_fleetmux_pane', lambda: True)
    fleet.main(['gui'])
    window_path_taken(host)


def test_ssh_still_attaches_in_a_host_terminal_and_without_a_tty(host, monkeypatch):
    monkeypatch.setenv('HC_TERMINAL_ID', 't1')
    monkeypatch.setenv('SSH_CONNECTION', '10.0.0.2 50000 10.0.0.1 22')
    monkeypatch.setattr(fleet, 'interactive_terminal', lambda: False)
    fleet.main(['gui'])
    assert host['execs'] == attach(host), host['execs']


# ---- H3: the list is fleet.yaml#bare.host_terminal_env ------------------------------------------------------------

def test_shipped_list_is_hc_terminal_id():
    shipped = yaml.safe_load(fleet_config.SHIPPED.read_text())['bare']
    assert shipped['host_terminal_env'] == ['HC_TERMINAL_ID'], shipped
    assert fleet_config.BARE['host_terminal_env'] == ['HC_TERMINAL_ID'], fleet_config.BARE
    assert fleet_config.host_terminal_env() == ('HC_TERMINAL_ID',)


def test_private_list_replaces_the_shipped_one(host, monkeypatch):
    private(host['tmp'], monkeypatch, {'bare': {'host_terminal_env': ['MY_HOST_PANE']}})
    assert fleet_config.host_terminal_env() == ('MY_HOST_PANE',)
    monkeypatch.setenv('HC_TERMINAL_ID', 't1')
    fleet.main(['gui'])
    window_path_taken(host)
    host['spawned'].clear()
    monkeypatch.delenv('HC_TERMINAL_ID')
    monkeypatch.setenv('MY_HOST_PANE', '1')
    fleet.main(['gui'])
    assert host['execs'] == attach(host), host['execs']


def test_private_bare_block_without_the_key_takes_the_default(host, monkeypatch):
    private(host['tmp'], monkeypatch, {'bare': {'name': 'x'}})
    assert fleet_config.host_terminal_env() == ('HC_TERMINAL_ID',)
    assert fleet_config.bare()['host_terminal_env'] == ['HC_TERMINAL_ID']
    monkeypatch.setenv('HC_TERMINAL_ID', 't1')
    fleet.main(['gui'])
    assert host['execs'] == attach(host), host['execs']


BAD_LISTS = ['HC_TERMINAL_ID', None, {'a': 1}, [1], [''], ['A=B'], ['HC-ID'], ['X Y']]


@pytest.mark.parametrize('value', BAD_LISTS)
def test_bad_host_terminal_env_is_refused(value, tmp_path, monkeypatch):
    """Refused by its own reader; `bare()` (the start settings) does not check it, so a bad list cannot stop the
    ssh / no-desktop start (the list is read only where a window could open)."""
    private(tmp_path, monkeypatch, {'bare': {'host_terminal_env': value}})
    with pytest.raises(ValueError, match='host_terminal_env'):
        fleet_config.host_terminal_env()
    assert fleet_config.bare()['name'] == 'main'


@pytest.mark.parametrize('block', [5, ['HC_TERMINAL_ID'], 'x'])
def test_a_bare_block_that_is_not_a_mapping_is_refused(block, tmp_path, monkeypatch):
    private(tmp_path, monkeypatch, {'bare': block})
    with pytest.raises(ValueError, match='bare'):
        fleet_config.host_terminal_env()


@pytest.mark.parametrize('value', BAD_LISTS)
@pytest.mark.parametrize('where', ['desktop', 'no-tty', 'window-open'])
def test_a_bad_list_is_refused_before_the_live_root_is_shown(host, monkeypatch, value, where):
    """Where a window could open: refused before any exec / raise / spawn."""
    private(host['tmp'], monkeypatch, {'bare': {'host_terminal_env': value}})
    monkeypatch.setenv('HC_TERMINAL_ID', 't1')
    if where == 'no-tty':
        monkeypatch.setattr(fleet, 'interactive_terminal', lambda: False)
    elif where == 'window-open':
        monkeypatch.setattr(gui_keep, 'gui_running', lambda runtime: GUI_PID)
    with pytest.raises(SystemExit) as exc:
        fleet.main(['gui'])
    assert 'bare' in str(exc.value.code) and 'host_terminal_env' in str(exc.value.code), exc.value.code
    assert host['execs'] == [] and host['spawned'] == [] and host['raised'] == []


@pytest.mark.parametrize('value', BAD_LISTS)
@pytest.mark.parametrize('why', ['ssh', 'no-desktop', 'no-web'])
def test_a_bad_list_does_not_stop_the_ssh_or_no_desktop_path(host, monkeypatch, value, why):
    """P4: without a window here the path is exactly the one before this rule: attach; the list is not read."""
    private(host['tmp'], monkeypatch, {'bare': {'host_terminal_env': value}})
    monkeypatch.setenv('HC_TERMINAL_ID', 't1')
    if why == 'ssh':
        monkeypatch.setenv('SSH_CONNECTION', '10.0.0.2 50000 10.0.0.1 22')
    elif why == 'no-desktop':
        monkeypatch.setattr(fleet, 'has_desktop', lambda: False)
    else:
        monkeypatch.setattr(fleet, 'fleetmux_web_bin', lambda binary: None)
    fleet.main(['gui'])
    assert host['execs'] == attach(host), host['execs']
    assert host['spawned'] == [] and host['raised'] == [], (host['spawned'], host['raised'])


def test_the_rule_stays_in_open_window_and_window_here_is_untouched(host, monkeypatch):
    """`window_here` is shared with `start`'s automatic view: the host-terminal rule is not folded into it."""
    monkeypatch.setenv('HC_TERMINAL_ID', 't1')
    assert fleet.window_here(BIN), 'window_here keeps the desktop rule only'
    assert callable(open_window.in_host_terminal) and open_window.in_host_terminal()
