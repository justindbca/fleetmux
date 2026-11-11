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
import uuid

import pytest

import test_fleet  # first: puts the checkout on sys.path
import hook_sim  # noqa: E402
from fleetmux import cmux_manager, codex_queue, fleet  # noqa: E402
from fleetmux import env_names, fleet_config  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line('markers', 'real_uappend: the audit chain may call the real chflags (UF_APPEND) in this '
                                       'test; the test must clear the flag (chflags nouappend) before it ends')


@pytest.fixture(autouse=True)
def _network_up(monkeypatch):
    """Fleet.network_up never touches the real network in tests: online, unless a test sets it on its fleet."""
    monkeypatch.setattr(fleet.Fleet, 'network_up', lambda self, timeout=2.0: True)


@pytest.fixture(autouse=True)
def _no_real_uappend(request, monkeypatch):
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
    monkeypatch.setattr(auditlog, 'default_chflags', lambda: fake_chflags)
    yield calls


@pytest.fixture(autouse=True)
def isolated_fleet_namespace(monkeypatch, tmp_path):
    """No test may inherit the operator's live fleet namespace.

    A pytest run started from a fleet worker inherits FLEETMUX_HOME / FLEETMUX_STATE, and `fleet` binds
    HOME / LATEST (and cmux_manager binds STATE_DIR / CMUX) at import time.  A test that reaches a `save(LATEST, …)`
    — e.g. `start --resume`, which re-pins LATEST from the run's config — then writes the live
    `<live home>/latest.json` and points the operator's next `start --resume` at a temporary redis socket
    (operator report: pytest-8260/test_resume_keeps_the_hot_edit0 wrote ~/.cmux-fleet-layout-finish/latest.json).
    Every test therefore runs against a per-test fleet home / state dir, both in the environment and in the
    already-imported module globals.  A test that wants a different namespace still calls monkeypatch.setenv /
    setattr itself and wins; teardown restores the previous values.
    """
    home = tmp_path / 'fleet-home'
    state = tmp_path / 'cmux-state'
    monkeypatch.delenv('FLEETMUX_CONFIG', raising=False)
    for _new, legacy in env_names.PAIRS:  # a legacy name (copied to its new name at import) must not differ from
        monkeypatch.delenv(legacy, raising=False)  # the new one in any subprocess a test starts
    monkeypatch.setenv('FLEETMUX_HOME', str(home))
    monkeypatch.setenv('FLEETMUX_STATE', str(state))
    monkeypatch.setattr(fleet, 'HOME', home)
    monkeypatch.setattr(fleet, 'LATEST', home / 'latest.json')
    monkeypatch.setattr(cmux_manager, 'STATE_DIR', state)
    return {'home': home, 'state': state}


@pytest.fixture(autouse=True)
def no_first_run_discovery(monkeypatch):
    """`start` / `gui` never probe the test machine's CLIs (docs/design.md §52); test_discover turns it back on."""
    monkeypatch.setenv('FLEETMUX_DISCOVERY', 'off')


@pytest.fixture(autouse=True)
def no_private_config(monkeypatch, tmp_path_factory):
    """The operator's ~/.cmux-fleet/fleet.yaml never leaks into a test; a test sets its own."""
    monkeypatch.setenv(fleet_config.USER_ENV, str(tmp_path_factory.getbasetemp() / 'no-private-fleet.yaml'))
    monkeypatch.setattr(fleet_config, '_pinned', None)  # nor a run's pin (start / pin_paths) from an earlier test


@pytest.fixture(autouse=True)
def isolated_fleetmux_config_dir(monkeypatch, tmp_path):
    """No test reads or writes the operator's ~/.config/fleetmux (channel credentials, whitelists, leases,
    security.yaml): FLEETMUX_CONFIG_DIR points at a per-test path that is not created here, so a test sees
    no account file, lease or security.yaml unless it writes one. Subprocesses inherit it. A test or module
    fixture that sets its own directory with monkeypatch.setenv runs after this one and wins; HOME is untouched."""
    monkeypatch.setenv('FLEETMUX_CONFIG_DIR', str(tmp_path / 'fleetmux-config-isolated'))


@pytest.fixture(autouse=True)
def fake_codex_queue(monkeypatch):
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

    monkeypatch.setattr(codex_queue, 'enqueue', fake_enqueue)
    monkeypatch.setattr(codex_queue, 'deliver', deliver)
    yield
    for queued, sent in logs:
        codex_panes = {pane for pane, _ in queued if pane}
        typed = [(pane, text[:60]) for pane, text in sent if pane in codex_panes]
        assert not typed, 'a Codex pane got a paste: ' + repr(typed)


@pytest.fixture(autouse=True)
def _waiting_hook_stand_in(request, monkeypatch):
    if getattr(request.module, 'HOOK_CHANNEL', False):
        yield
        return
    hook_sim.install(monkeypatch)
    yield
    hook_sim.reset()


@pytest.fixture(autouse=True)
def _no_machine_wide_redis_sweep(monkeypatch):
    """start / --resume sweep every Redis of this user; tests must never touch a real fleet's
    (tests/test_orphan_redis.py calls reap_orphan_redis directly, scoped to its own root)."""
    from fleetmux import fleet
    monkeypatch.setattr(fleet, 'sweep_orphan_redis', lambda keep=None: None)


@pytest.fixture(autouse=True)
def _start_resolves_its_default_backend_as_on_a_mac_with_cmux(monkeypatch):
    """The suite's `start` tests were written where no --backend means cmux (a Mac with cmux installed)
    and stub cmux's probe. Pin that host for backend selection only, so they take the same path on any
    machine: CI's Mac has no cmux binary, and Linux defaults to WezTerm (design §5). The default rule
    itself is tested with explicit platforms and its own `cmux_installed` / `unavailable_reason` stubs
    (test_backend_selection, test_backend_interface, test_wezterm_fleet_wiring)."""
    from fleetmux.backends import base
    real = fleet.resolve_backend
    monkeypatch.setattr(fleet, 'resolve_backend',
                        lambda requested=None, platform=None: real(requested, 'darwin' if platform is None else platform))
    monkeypatch.setattr(base, 'cmux_installed', lambda: True)
