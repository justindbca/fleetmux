"""fleetmux history: the runs the sidebar's ↺ list can still resume (plan §18.3 / §18.6).

``tmp_path`` only: no fleetmux server, no Redis, no daemon. Every run is a ``<home>/runs/<id>/`` directory
with the ``config.json`` / ``result.json`` the real start / finalize write; ``fleet.daemon_running`` is
monkeypatched for the one run whose daemon is supposed to be alive.
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmux_fleet import fleet, fleetmux_history  # noqa: E402

HOME = 'home'
STATE = 'state'


class FakeMgr:
    """``kv_get`` answers what was written; ``writes`` is the kv.set log (a real kv.set redraws the server)."""

    def __init__(self):
        self.kv = {}
        self.writes = []

    def kv_get(self, key):
        return self.kv.get(key)

    def _kv_set(self, key, value):
        self.kv[key] = value
        self.writes.append((key, value))
        return True


def make_run(tmp_path, name, *, resumable=True, closed_via='fleetmux-sidebar', closed_by='user',
             closed_at=1790000000, backend='fleetmux', state_dir=STATE, session=None,
             goal='ship it', fleet_name='fmx-demo', result=True):
    """One run directory under ``<tmp_path>/home/runs/``: its config.json and (unless asked not to) result.json."""
    run = tmp_path / HOME / 'runs' / name
    run.mkdir(parents=True)
    cfg = {'run': str(run), 'python': '/py/bin/python', 'backend': backend,
           'fleet_home': str(tmp_path / HOME), 'cmux_state_dir': str(tmp_path / state_dir),
           'private_config': 'none'}
    if fleet_name:
        cfg['name'] = fleet_name
    if session is not None:
        cfg['session'] = session
    fleet.save(run / 'config.json', cfg)
    if result:
        res = {'status': 'running', 'goal': goal, 'resumable': resumable,
               'closed_by': closed_by, 'closed_at': closed_at}
        if closed_via is not None:
            res['closed_via'] = closed_via
        fleet.save(run / 'result.json', res)
    return run


def listed(tmp_path):
    return fleetmux_history.restorable_runs(tmp_path / HOME, str(tmp_path / STATE))


def test_lists_sidebar_closed_and_timeout_resumable_runs(tmp_path, monkeypatch):
    """§18.6: a sidebar × close and a lifecycle timeout stop are both listed, everything else is not."""
    make_run(tmp_path, 'fleet-sidebar', closed_at=1790000100)
    make_run(tmp_path, 'fleet-timeout', closed_via=None, closed_by='timeout', closed_at=1790000200)
    make_run(tmp_path, 'fleet-normal', closed_via=None, closed_by='auto')  # completed: nothing to resume
    make_run(tmp_path, 'fleet-other-via', closed_via='cmux')
    make_run(tmp_path, 'fleet-not-resumable', resumable=False)
    make_run(tmp_path, 'fleet-live', closed_at=1790000300)
    make_run(tmp_path, 'fleet-other-state', state_dir='state-other')
    make_run(tmp_path, 'fleet-cmux-backend', backend='cmux')
    make_run(tmp_path, 'fleet-other-session', session='cmux-fleet:child')
    make_run(tmp_path, 'fleet-no-config', result=True)
    (tmp_path / HOME / 'runs' / 'fleet-no-config' / 'config.json').unlink()
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: Path(run).name == 'fleet-live')

    got = listed(tmp_path)

    assert [r['id'] for r in got] == ['fleet-timeout', 'fleet-sidebar'], \
        'only sidebar-closed and timeout-stopped resumable runs of this runtime belong in the list: %r' % (got,)
    assert [r['line1'] for r in got] == ['到点停 · 可恢复', '已关闭 · 可恢复'], \
        'the card text tells a timeout stop from a sidebar close: %r' % (got,)
    assert [r['title'] for r in got] == ['fmx-demo', 'fmx-demo'], got


def test_newest_first_and_at_most_16(tmp_path, monkeypatch):
    """§18.3: newest ``closed_at`` first, and never more runs than the sidebar has cards (16)."""
    for i in range(18):
        make_run(tmp_path, 'fleet-%02d' % i, closed_at=1790000000 + i)
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: False)

    got = listed(tmp_path)

    assert [r['id'] for r in got] == ['fleet-%02d' % i for i in range(17, 1, -1)], \
        'the 16 newest runs, newest first: %r' % ([r['id'] for r in got],)


def test_resumed_run_drops_out(tmp_path, monkeypatch):
    """§18.3: a resumed run loses its result.json, so it is no longer a history entry."""
    make_run(tmp_path, 'fleet-a', closed_at=1790000100)
    resumed = make_run(tmp_path, 'fleet-b', closed_at=1790000200)
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: False)
    assert [r['id'] for r in listed(tmp_path)] == ['fleet-b', 'fleet-a'], listed(tmp_path)

    (resumed / 'result.json').unlink()

    assert [r['id'] for r in listed(tmp_path)] == ['fleet-a'], \
        'the resumed run must drop out of the history: %r' % (listed(tmp_path),)


def test_restorable_kv_has_the_resume_command(tmp_path, monkeypatch):
    """§18.3: kv ``restorable`` carries exactly the ``start --resume`` argv / cwd / env of the run."""
    monkeypatch.setenv('CMUX_FLEET_FLEETMUX', '/opt/fleetmux')
    monkeypatch.setenv('CMUX_FLEET_FLEETMUX_THEME', str(tmp_path / 'theme.toml'))
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    monkeypatch.delenv('CMUX_FLEET_CONFIG', raising=False)
    run = make_run(tmp_path, 'fleet-a', closed_at=1790000000.5)
    monkeypatch.setattr(fleet, 'daemon_running', lambda r: False)

    kv = fleetmux_history.restorable_kv(listed(tmp_path))

    assert kv == {'v': 1, 'runs': [{
        'id': 'fleet-a', 'title': 'fmx-demo', 'closed_at': 1790000000,
        'argv': ['/py/bin/python', str(Path(fleet.__file__).resolve()), 'start', '--resume', str(run)],
        'cwd': str(run),
        'env': {'CMUX_FLEET_HOME': str(tmp_path / HOME), 'CMUX_FLEET_STATE': str(tmp_path / STATE),
                'CMUX_FLEET_FLEETMUX': '/opt/fleetmux',
                'CMUX_FLEET_FLEETMUX_THEME': str(tmp_path / 'theme.toml'), 'PATH': '/usr/bin:/bin'},
    }]}, 'kv restorable must be the exact resume command: %r' % (kv,)


def test_cards_are_grey_and_name_the_close_time(tmp_path, monkeypatch):
    """§18.2: each history run becomes a grey ``closed`` card whose second line is the close time."""
    make_run(tmp_path, 'fleet-a', closed_at=1790000000)
    monkeypatch.setattr(fleet, 'daemon_running', lambda r: False)
    when = time.strftime('%m-%d %H:%M', time.localtime(1790000000))

    cards = fleetmux_history.cards(listed(tmp_path))

    assert cards == [{'id': 'fleet-a', 'title': 'fmx-demo', 'state': 'closed',
                      'line1': '已关闭 · 可恢复', 'line2': '关闭于 ' + when}], \
        'the history card must be grey, closed and dated: %r' % (cards,)


def test_push_writes_restorable_and_sidebar_and_skips_an_unchanged_write(tmp_path, monkeypatch):
    """§18.4: ``push`` writes both kv keys once, and a second identical push writes nothing."""
    make_run(tmp_path, 'fleet-a', closed_at=1790000000)
    monkeypatch.setattr(fleet, 'daemon_running', lambda r: False)
    runs = listed(tmp_path)
    mgr = FakeMgr()

    assert fleetmux_history.push(mgr, runs) is True, 'the first push must write: %r' % (mgr.writes,)
    assert json.loads(mgr.kv['restorable']) == fleetmux_history.restorable_kv(runs), mgr.kv['restorable']
    assert json.loads(mgr.kv['sidebar']) == {'v': 1, 'current': 'fleet-a',
                                             'fleets': fleetmux_history.cards(runs)}, mgr.kv['sidebar']
    assert [key for key, _ in mgr.writes] == ['restorable', 'sidebar'], mgr.writes

    before = list(mgr.writes)
    assert fleetmux_history.push(mgr, runs) is False, 'an unchanged push must not redraw the server'
    assert mgr.writes == before, mgr.writes
    assert fleetmux_history.push(mgr, []) is False, '0 cards is not a legal kv sidebar (parse rejects it)'
    assert mgr.writes == before, 'an empty history writes nothing at all: %r' % (mgr.writes,)


# ------------------------------------------------------------------ S2c ②: the `gui` entry point (§18.2 / §18.7-2)
class GuiMgr(FakeMgr):
    """A stand-in fleetmux manager: the runtime dir `gui` execs on, and a session that starts on demand."""

    def __init__(self, runtime):
        super().__init__()
        self.runtime_root = Path(runtime)
        self.created = False
        self.last_error = ''

    def get_or_create_session(self):
        self.created = True
        return True


def gui_world(tmp_path, monkeypatch):
    """The `gui` subcommand's outside world: a desktop, a usable backend, a fake manager, a recorded execvp."""
    from cmux_fleet import cmux_manager
    monkeypatch.setattr(fleet, 'HOME', tmp_path / HOME)
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    monkeypatch.setattr(fleet, 'has_desktop', lambda: True)
    monkeypatch.setattr(fleet, 'gui_problem', lambda: None)
    monkeypatch.setattr(cmux_manager, 'STATE_DIR', tmp_path / STATE)
    mgr = GuiMgr(tmp_path / STATE / 'fleetmux' / 'cmux-fleet')
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: mgr)
    execs = []
    monkeypatch.setattr(fleet.os, 'execvp', lambda file, argv: execs.append((file, list(argv))))
    return mgr, execs


def test_gui_subcommand_pushes_cards_then_execs_fleetmux_gui(tmp_path, monkeypatch):
    """§18.2 steps 2-4: no daemon, two resumable runs → grey cards in kv, then `fleetmux gui`."""
    mgr, execs = gui_world(tmp_path, monkeypatch)
    make_run(tmp_path, 'fleet-old', closed_at=1790000000)
    make_run(tmp_path, 'fleet-timeout', closed_by='timeout', closed_via=None, closed_at=1789000000)
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: False)

    fleet.main(['gui'])

    assert mgr.created, 'gui must start the server when none is up (§18.2 step 2): %r' % (mgr.writes,)
    sidebar = json.loads(mgr.kv['sidebar'])
    assert [c['id'] for c in sidebar['fleets']] == ['fleet-old', 'fleet-timeout'], \
        'gui must write one grey card per resumable run, newest first (§18.2 step 3): %r' % (sidebar,)
    assert sidebar['current'] == 'fleet-old', 'current is the newest closed fleet (§18.2): %r' % (sidebar,)
    assert [c['line1'] for c in sidebar['fleets']] == ['已关闭 · 可恢复', '到点停 · 可恢复'], \
        'a sidebar close and a timeout close read differently (§18.6): %r' % (sidebar,)
    assert [c['state'] for c in sidebar['fleets']] == ['closed', 'closed'], sidebar
    restorable = json.loads(mgr.kv['restorable'])
    assert [r['id'] for r in restorable['runs']] == ['fleet-old', 'fleet-timeout'], \
        'kv restorable must carry one start --resume command per card (§18.3): %r' % (restorable,)
    assert len(execs) == 1, 'gui execs exactly one window (§18.2 step 4): %r' % (execs,)
    file, argv = execs[0]
    assert Path(file).name == 'fleetmux' and Path(argv[0]).name == 'fleetmux', execs
    assert argv[1:] == ['gui', '--runtime', str(mgr.runtime_root)], \
        'the window must watch this HOME\'s runtime (§18.1): %r' % (execs,)


def test_gui_subcommand_with_a_live_daemon_only_execs(tmp_path, monkeypatch):
    """§18.2 step 3: a live daemon pushes its own cards, so gui writes no kv at all."""
    mgr, execs = gui_world(tmp_path, monkeypatch)
    live = make_run(tmp_path, 'fleet-live')
    fleet.save(fleet.LATEST, {'run': str(live)})
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: True)

    fleet.main(['gui'])

    assert mgr.kv == {}, 'a live daemon owns the cards: gui must not overwrite kv (§18.2): %r' % (mgr.writes,)
    assert len(execs) == 1, execs
    assert execs[0][1][1:] == ['gui', '--runtime', str(mgr.runtime_root)], execs


def test_gui_subcommand_with_no_history_exits_without_a_server(tmp_path, monkeypatch, capsys):
    """§18.7-2: 0 resumable runs and no daemon → say what to do, exit 1, no server and no window."""
    mgr, execs = gui_world(tmp_path, monkeypatch)
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: False)

    with pytest.raises(SystemExit) as exc:
        fleet.main(['gui'])

    assert exc.value.code == 1, 'an empty history with no daemon exits 1 (§18.7-2): %r' % (exc.value.code,)
    assert not mgr.created and execs == [], \
        'no server may start and no window may open: created=%r execs=%r' % (mgr.created, execs)
    out = capsys.readouterr().out
    assert '这个 HOME 没有可恢复的 fleet；用 start 新起，或 start --resume <run>' in out, \
        'gui must say what to do with no history (§18.7-2): %r' % (out,)
