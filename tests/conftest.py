"""Suite-wide test defaults: neither no-typing channel ever types into a pane.

Codex: a fake `codex queue` for every test (Codex deliveries never type into a pane).
The real CLI is never run by the unit tests. A queued delivery is recorded in its own log,
`mgr.queued` (FakeMgr / test_subfleet's World), never in `mgr.sent`: `sent` stays the record of real
`send_prompt_to_idle` pastes only, so "nothing was typed" assertions keep their meaning. Its turn is
started in the rollout the way the real TUI does it (test_fleet.codex_rows). A test that installs its
own `codex_queue.enqueue` stub keeps it: then only the real deliver runs.
Guard (every test): a pane that received a queued Codex delivery must never also get a paste.

Claude: Claude workers get their deliveries through the Stop-hook channel (fleetmux/claude_hook.py) in
every test. A real hook blocks waiting for the next delivery, which an in-process test cannot do,
so tests/hook_sim.py stands in for that wait only (see its docstring). A module that runs the real
hook command in subprocesses sets ``HOOK_CHANNEL = True`` and gets no stand-in.
"""
import os
import uuid
from pathlib import Path

import pytest

import test_fleet  # first: puts the checkout on sys.path
import hook_sim  # noqa: E402
from fleetmux import cmux_manager, codex_queue, fleet  # noqa: E402
from fleetmux import fleet_config, gui_keep, home_migrate  # noqa: E402


def pytest_configure(config):
    # The fleet-superpowers inventory never resolves to ~/.fleetmux for the whole session, also between tests,
    # in threads and in subprocesses: a per-test monkeypatch restores this value, never "unset".
    import tempfile
    from fleetmux import fleet_superpowers
    guard = Path(tempfile.mkdtemp(prefix='fleetmux-test-superpowers-'))
    os.environ['FLEETMUX_SUPERPOWERS'] = str(guard / 'fleet-superpowers.yaml')
    fleet_superpowers.OLD_QUOTA = guard / 'no-quota.json'
    config.addinivalue_line('markers', 'real_uappend: the audit chain may call the real chflags (UF_APPEND) in this '
                                       'test; the test must clear the flag (chflags nouappend) before it ends')


@pytest.fixture(autouse=True)
def _awake_clock():
    """Every test starts with a fresh process awake clock (fleetmux/awake.py): no sleep measured by an earlier
    test leaks into this one. Its monotonic clock is ``time.time`` (looked up per call), so awake time equals
    Unix time and a test that moves ``time.time`` moves both; a sleep is simulated only with ``fake_awake``."""
    import time
    from fleetmux import awake
    awake.reset(lambda: time.time(), lambda: time.time())
    yield
    awake.reset()


class FakeAwake:
    """A controllable machine: ``run(s)`` = awake seconds pass (wall and monotonic move), ``sleep(s)`` = the lid
    is closed (wall moves, monotonic does not)."""

    def __init__(self, wall=None):
        import time
        self.w, self.m = float(time.time() if wall is None else wall), 1000.0

    def wall(self):
        return self.w

    def mono(self):
        return self.m

    def run(self, seconds):
        self.w += seconds
        self.m += seconds

    def sleep(self, seconds):
        self.w += seconds


@pytest.fixture
def fake_awake():
    """The process awake clock on a FakeAwake machine (``.machine``); returns the machine."""
    from fleetmux import awake
    machine = FakeAwake()
    awake.reset(machine.wall, machine.mono, boot=1)
    return machine


@pytest.fixture
def _isolation_mp():
    """The suite-wide guards' own MonkeyPatch (T-31: no test reaches the real machine or a live fleet).

    A test's ``monkeypatch.undo()`` undoes everything on the shared ``monkeypatch`` instance; the autouse guards below
    patch through this separate instance instead, so an undo never brings back the real ``~/.fleetmux`` scan root,
    private fleet.yaml, fleet home / state dir, window launcher, codex CLI, chflags or Redis sweep
    (tests/test_conftest_isolation.py). It is set up before the shared ``monkeypatch`` (every conftest autouse fixture
    that also uses ``monkeypatch`` requests this one first), so teardown undoes the test's own patches first and these
    last: a test that sets its own value still wins, and its undo returns to the isolated value, never the real one."""
    with pytest.MonkeyPatch.context() as mp:
        yield mp


@pytest.fixture(autouse=True)
def _network_up(_isolation_mp):
    """Fleet.network_up never touches the real network in tests: online, unless a test sets it on its fleet."""
    _isolation_mp.setattr(fleet.Fleet, 'network_up', lambda self, timeout=2.0: True)


@pytest.fixture(autouse=True)
def _no_installed_release(_isolation_mp, tmp_path):
    """No test reads the operator's ``~/fleetmux-current``: the installed release is unknown unless a test links
    ``releases.LINK`` itself."""
    from fleetmux import releases
    _isolation_mp.setattr(releases, 'LINK', str(tmp_path / 'no-fleetmux-current'))


@pytest.fixture(autouse=True)
def no_real_window(_isolation_mp):
    """No test opens a real `fleetmux gui` window: the daemon's keep-open part (fleetmux/gui_keep.py) is a no-op in
    Fleet.tick, no window is open, and the launcher fails loudly. tests/test_gui_keep.py calls the real tick."""
    def refuse(argv, log, env):
        raise AssertionError('tests never open a real window: ' + ' '.join(argv))
    _isolation_mp.setattr(gui_keep, 'tick', lambda f, now=None: None)
    _isolation_mp.setattr(gui_keep, 'gui_running', lambda runtime: False)
    _isolation_mp.setattr(gui_keep, 'spawn', refuse)


@pytest.fixture(autouse=True)
def _no_real_uappend(request, _isolation_mp):
    """F-2 (supervisor M169): an audit chain built without an explicit ``chflags`` (``auditlog.daemon_pump`` through
    ``Fleet.security_tick``, with the default ``audit_log.uappend: on``) would set the real UF_APPEND flag on chain
    files under the pytest temp dir, which pytest then cannot delete (``PytestWarning: (rm_rf) error removing
    .../audit``). Every test gets a recording no-op instead (success, so no ``uappend_unsupported`` audit row and
    no behaviour change); tests that pass their own ``chflags`` keep it. Opt out with ``@pytest.mark.real_uappend``."""
    if request.node.get_closest_marker('real_uappend'):
        yield None
        return
    from fleetmux.channels import auditlog
    calls = []

    def fake_chflags(path, flags):
        calls.append((str(path), flags))
    _isolation_mp.setattr(auditlog, 'default_chflags', lambda: fake_chflags)
    yield calls


@pytest.fixture(autouse=True)
def no_old_data_guard(_isolation_mp, tmp_path):
    """The start-up guard never sees the operator's own pre-fleetmux data (tests/test_home_migrate.py points it at a
    temp HOME). On ``_isolation_mp``, so a test's ``monkeypatch.undo()`` does not bring the real HOME back."""
    _isolation_mp.setattr(home_migrate, '_home', lambda: tmp_path / 'no-old-data')


@pytest.fixture(autouse=True)
def isolated_fleet_namespace(_isolation_mp, tmp_path):
    """No test may inherit the operator's live fleet namespace.

    A pytest run started from a fleet worker inherits FLEETMUX_HOME / FLEETMUX_STATE, and `fleet` binds
    HOME / LATEST (and cmux_manager binds STATE_DIR / CMUX) at import time.  A test that reaches a `save(LATEST, …)`
    — e.g. `start --resume`, which re-pins LATEST from the run's config — then writes the live
    `<live home>/latest.json` and points the operator's next `start --resume` at a temporary redis socket
    (operator report: pytest-8260/test_resume_keeps_the_hot_edit0 wrote the latest.json of a live fleet home).
    Every test therefore runs against a per-test fleet home / state dir, both in the environment and in the
    already-imported module globals.  A test that wants a different namespace still calls monkeypatch.setenv /
    setattr itself and wins; teardown restores the previous values.
    """
    home = tmp_path / 'fleet-home'
    state = tmp_path / 'cmux-state'
    _isolation_mp.delenv('FLEETMUX_CONFIG', raising=False)
    for name in [k for k in os.environ if k.startswith(home_migrate.OLD_ENV)]:  # set by an older release's
        _isolation_mp.delenv(name)  # pane: the start-up guard would stop every human command
    _isolation_mp.setenv('FLEETMUX_HOME', str(home))
    _isolation_mp.setenv('FLEETMUX_STATE', str(state))
    _isolation_mp.setattr(fleet, 'HOME', home)
    _isolation_mp.setattr(fleet, 'LATEST', home / 'latest.json')
    _isolation_mp.setattr(cmux_manager, 'STATE_DIR', state)
    return {'home': home, 'state': state}


@pytest.fixture(autouse=True)
def no_real_bare_start(_isolation_mp):
    """Bare `fleetmux` / `gui` with no root fleet starts one through `fleet.run_start` (fleetmux/open_window.py): no
    test may reach a real `start`. A test that wants the bare start stubs it; the real function stays reachable as
    ``fleet.run_start.real`` for the test of the function itself."""
    real = fleet.run_start

    def refuse(argv, env):
        raise AssertionError('a test reached the bare root start (fleet.run_start %r); stub it or set '
                             'fleet.yaml#bare.enabled false' % (argv,))

    refuse.real = real
    _isolation_mp.setattr(fleet, 'run_start', refuse)


@pytest.fixture(autouse=True)
def isolated_superpowers(_isolation_mp, tmp_path):
    """The fleet-superpowers inventory (fleetmux/fleet_superpowers.py) is a per-test file that does not exist
    yet: no test reads or writes ~/.fleetmux/fleet-superpowers or migrates the real quota.json. A test that moves
    HOME to locate the default file deletes the variable (and resets OLD_QUOTA) itself."""
    _isolation_mp.setenv('FLEETMUX_SUPERPOWERS', str(tmp_path / 'fleet-superpowers-isolated' / 'fleet-superpowers.yaml'))
    from fleetmux import fleet_superpowers  # its one-time migration never reads the operator's quota.json
    _isolation_mp.setattr(fleet_superpowers, 'OLD_QUOTA', tmp_path / 'fleet-superpowers-isolated' / 'no-quota.json')


@pytest.fixture(autouse=True)
def no_recorded_program_dirs(_isolation_mp, tmp_path):
    """discover.search_path() never reads this machine's own records (~/fleetmux-current's installed.json, the
    release the checkout may sit in): both point at a per-test path that does not exist (docs/design.md §50.3).
    tests/test_find_pi.py builds its own records on a temp HOME."""
    from fleetmux import discover
    _isolation_mp.setattr(discover, 'CURRENT', tmp_path / 'no-fleetmux-current')
    _isolation_mp.setattr(discover, 'OWN_INSTALLED', tmp_path / 'no-own-release' / 'installed.json')


@pytest.fixture(autouse=True)
def no_first_run_discovery(_isolation_mp):
    """`start` / `gui` never probe the test machine's CLIs (docs/design.md §52); test_discover turns it back on."""
    _isolation_mp.setenv('FLEETMUX_DISCOVERY', 'off')


@pytest.fixture(autouse=True)
def no_live_root_discovery(_isolation_mp, tmp_path):
    """`start` never finds the operator's live fleets under ~/.fleetmux (fleetmux/join.py): it searches a per-test
    directory that does not exist, plus the per-test fleet home. Subprocesses inherit it. test_start_join sets
    its own."""
    from fleetmux import join
    _isolation_mp.setenv(join.SCAN_ENV, str(tmp_path / 'no-join-root'))


@pytest.fixture(autouse=True)
def no_private_config(_isolation_mp, tmp_path_factory):
    """The operator's ~/.fleetmux/fleet.yaml never leaks into a test; a test sets its own."""
    _isolation_mp.setenv(fleet_config.USER_ENV, str(tmp_path_factory.getbasetemp() / 'no-private-fleet.yaml'))
    _isolation_mp.setattr(fleet_config, '_pinned', None)  # nor a run's pin (start / pin_paths) from an earlier test


@pytest.fixture(autouse=True)
def isolated_fleetmux_config_dir(_isolation_mp, tmp_path):
    """No test reads or writes the operator's ~/.config/fleetmux (channel credentials, whitelists, leases,
    security.yaml): FLEETMUX_CONFIG_DIR points at a per-test path that is not created here, so a test sees
    no account file, lease or security.yaml unless it writes one. Subprocesses inherit it. A test or module
    fixture that sets its own directory with monkeypatch.setenv runs after this one and wins; HOME is untouched."""
    _isolation_mp.setenv('FLEETMUX_CONFIG_DIR', str(tmp_path / 'fleetmux-config-isolated'))


@pytest.fixture(autouse=True)
def fake_codex_queue(_isolation_mp):
    test_fleet.CODEX_QUEUE.clear()
    real_deliver = codex_queue.deliver

    def fake_enqueue(cfg, name, thread, text, **kw):
        if not thread:
            raise codex_queue.QueueError('failed: no bound Codex session to queue into')
        return {'queued_id': 'q-' + uuid.uuid4().hex, 'thread': thread}

    logs = []  # (queued, sent) pairs of every manager that took a queued delivery

    def deliver(f, slot, delivery, text, thread=None):
        got = real_deliver(f, slot, delivery, text, thread=thread)
        if codex_queue.enqueue is fake_enqueue:
            box = f.mgr if hasattr(f.mgr, 'sent') else f.mgr.w  # FakeMgr / test_subfleet WorldMgr
            if not hasattr(box, 'queued'):
                box.queued = []
            box.queued.append((f.agent(slot).get('pane'), text))  # what reached the agent (its token line is metadata)
            if not any(q is box.queued for q, _ in logs):
                logs.append((box.queued, box.sent))
            test_fleet.CODEX_QUEUE[got['thread']] = {'turn': 't-' + uuid.uuid4().hex, 'text': got['text'], 'body': text}
            _, rows = test_fleet.codex_rows(got['thread'], got['text'], pop=False)
            test_fleet.append_rollout(f.sessions, got['thread'], fleet.agent_cwd(f.runtime_cfg(slot), slot), rows)
        return got

    _isolation_mp.setattr(codex_queue, 'enqueue', fake_enqueue)
    _isolation_mp.setattr(codex_queue, 'deliver', deliver)
    yield
    for queued, sent in logs:
        codex_panes = {pane for pane, _ in queued if pane}
        typed = [(pane, text[:60]) for pane, text in sent if pane in codex_panes]
        assert not typed, 'a Codex pane got a paste: ' + repr(typed)


@pytest.fixture(autouse=True)
def _waiting_hook_stand_in(_isolation_mp, request, monkeypatch):
    if getattr(request.module, 'HOOK_CHANNEL', False):
        yield
        return
    hook_sim.install(monkeypatch)
    yield
    hook_sim.reset()


@pytest.fixture(autouse=True)
def _no_machine_wide_redis_sweep(_isolation_mp):
    """start / --resume sweep every Redis of this user; tests must never touch a real fleet's
    (tests/test_orphan_redis.py calls reap_orphan_redis directly, scoped to its own root)."""
    from fleetmux import fleet
    _isolation_mp.setattr(fleet, 'sweep_orphan_redis', lambda keep=None: None)


@pytest.fixture(autouse=True)
def _start_never_opens_a_view(_isolation_mp):
    """`start` opens a fleetmux view only for a person at a terminal; `pytest -s` in a terminal is not one."""
    _isolation_mp.setattr(fleet, 'interactive_terminal', lambda: False)


@pytest.fixture(autouse=True)
def _start_resolves_its_default_backend_as_on_a_mac_with_cmux(_isolation_mp):
    """The suite's `start` tests were written where no --backend means cmux (a Mac with cmux installed)
    and stub cmux's probe. Pin that answer for `start` (no flag, no platform) so they take the same path
    on any machine; the real default is fleetmux everywhere (design §33.3). The default rule itself is
    tested with an explicit platform, or by restoring ``fleet.resolve_backend.__wrapped__``
    (test_backend_selection, test_backend_interface, test_wezterm_fleet_wiring)."""
    from fleetmux.backends import base
    real = fleet.resolve_backend

    def pinned(requested=None, platform=None):
        if requested is None and platform is None:
            return real('cmux', 'darwin')
        return real(requested, 'darwin' if platform is None else platform)
    pinned.__wrapped__ = real
    _isolation_mp.setattr(fleet, 'resolve_backend', pinned)
    _isolation_mp.setattr(base, 'cmux_installed', lambda: True)
