"""The root page keeps its history pane (docs/design.md section 2 and section 17 "Windows").

A resume, or the daemon later, lays the page out with only the live workers: a worker that is delegated,
retired or dead, or whose pane is no longer on the server, is left out of ``arrange``, so one stale pane id
never leaves the page without its history pane. The fake manager's ``arrange`` is as strict as the fleetmux
backend's (backends/fleetmux.py ``arrange``: any pane not on the server -> None).
"""
import sys
import uuid

import pytest

from test_resume import (ResumeMgr, answer_turn, crash_and_resume, managed, new_fleet)  # noqa: F401  (fixture)
from fleetmux import fleet


class StrictMgr(ResumeMgr):
    """ResumeMgr whose ``arrange`` fails as the real backends do when one pane is not on the server."""
    def __init__(self):
        super().__init__()
        self.arrange_calls, self.fail_arrange = [], False

    def arrange(self, sup, workers, url):
        self.arrange_calls.append((sup, list(workers), url))
        if self.fail_arrange or sup not in self.alive or any(w not in self.alive for w in workers):
            return None
        return super().arrange(sup, workers, url)


@pytest.fixture
def strict_run(managed, tmp_path, monkeypatch):  # noqa: F811
    rcfg, clients = managed
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)  # never touch ~/.claude.json
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    cfg = dict(rcfg, run=str(run), python=sys.executable, workers=['worker-1', 'worker-2'], mode='task',
               supervisor_cli='codex', worker_cli='claude', codex_home=str(tmp_path / 'codex-home'),
               allow_development_v2=True, stall_timeout=0, v2_lease_ms=300000, max_busy=0, max_attempts=2)
    fleet.save(run / 'config.json', cfg)
    state = dict(rcfg=rcfg, clients=clients, path=run / 'config.json', mgr=StrictMgr(),
                 sessions=tmp_path / 'codex-home' / 'sessions', claude=tmp_path / 'claude-projects',
                 threads={s: str(uuid.uuid4()) for s in ['supervisor', 'worker-1', 'worker-2']})
    f = new_fleet(state, clients[0])
    f.start_all()
    f.r.set(f.v2.prefix + ':boot', 'running')
    for slot in f.names:
        assert answer_turn(f, slot, 'READY', startup=True)['status'] == 'ready'
    state['f'] = f
    yield state
    state['f'].v2.close()


def history_pane(f):
    return f.r.hget(fleet.SERVICE, 'history_pane')


def audits(f, event):
    out = []
    for _, fields in f.r.xrange(f.v2.bus.audit_key):
        fields = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                  for k, v in fields.items()}
        if fields.get('event') == event:
            out.append(fields)
    return out


def tick(f):
    f.history_next = 0  # the throttle is reset: every tick checks the history pane
    f.tick()


def confirm(f):
    """Two consecutive checks, one interval apart: only the second may rebuild (one failed read never does)."""
    tick(f)
    tick(f)


# ------------------------------------------------------------------ resume
def test_delegated_worker_with_a_stale_pane_does_not_cost_the_page_its_history(strict_run):
    """Section 0 of the brief: the server restarted while worker-2 was lent to a sub-fleet."""
    state, mgr = strict_run, strict_run['mgr']
    state['f'].setf('worker-2', state='delegated', pane='stale-pane')
    mgr.alive.clear()  # the server restarted: no pane of this run is left
    f = crash_and_resume(state)
    hist = history_pane(f)
    assert hist and mgr.get_pane_status(hist) is not None
    sup, workers, _ = mgr.arranged
    assert sup == f.agent('supervisor')['pane'] and workers == [f.agent('worker-1')['pane']]


def test_all_workers_alive_whole_page_resume_places_every_worker(strict_run):
    state, mgr = strict_run, strict_run['mgr']
    mgr.alive.clear()
    f = crash_and_resume(state)
    sup, workers, _ = mgr.arranged
    assert sup == f.agent('supervisor')['pane'] and workers == [f.agent(w)['pane'] for w in f.workers]
    assert mgr.get_pane_status(history_pane(f)) is not None


@pytest.mark.parametrize('lost', ['', 'gone-pane'])
def test_page_kept_but_history_pane_lost_is_rebuilt_on_resume(strict_run, lost):
    state, mgr = strict_run, strict_run['mgr']
    mgr.alive.discard(history_pane(state['f']))
    state['f'].r.hset(fleet.SERVICE, 'history_pane', lost)
    before = len(mgr.arrange_calls)
    f = crash_and_resume(state)
    hist = history_pane(f)
    assert len(mgr.arrange_calls) == before + 1
    assert hist and mgr.get_pane_status(hist) is not None and mgr.docked[-1] == hist


def test_page_and_history_kept_resume_does_not_arrange(strict_run):
    state, mgr = strict_run, strict_run['mgr']
    before = len(mgr.arrange_calls)
    f = crash_and_resume(state)
    assert len(mgr.arrange_calls) == before and mgr.get_pane_status(history_pane(f)) is not None


# ------------------------------------------------------------------ daemon fallback
@pytest.mark.parametrize('lost', ['', 'gone-pane'])
def test_daemon_rebuilds_a_lost_history_pane_once_and_docks_control(strict_run, lost):
    f, mgr = strict_run['f'], strict_run['mgr']
    mgr.alive.discard(history_pane(f))
    f.r.hset(fleet.SERVICE, 'history_pane', lost)
    before = len(mgr.arrange_calls)
    tick(f)
    assert len(mgr.arrange_calls) == before  # the first lost reading only marks it
    tick(f)
    hist = history_pane(f)
    assert hist and mgr.get_pane_status(hist) is not None and mgr.docked[-1] == hist
    assert len(mgr.arrange_calls) == before + 1
    assert len(audits(f, 'history.rebuilt')) == 1 and not audits(f, 'history.rebuild_failed')
    tick(f)  # alive now: nothing more to do
    assert len(mgr.arrange_calls) == before + 1


def test_daemon_rebuild_leaves_out_delegated_and_vanished_workers(strict_run):
    f, mgr = strict_run['f'], strict_run['mgr']
    f.setf('worker-2', state='delegated')
    mgr.alive.discard(history_pane(f))
    f.r.hset(fleet.SERVICE, 'history_pane', '')
    confirm(f)
    assert mgr.arranged[1] == [f.agent('worker-1')['pane']]
    assert mgr.get_pane_status(history_pane(f)) is not None


def test_failing_rebuild_is_tried_once_and_audited_once(strict_run):
    """A vanished id and arrange keeps failing: one attempt, one record, also after '' is written."""
    f, mgr = strict_run['f'], strict_run['mgr']
    mgr.alive.discard(history_pane(f))
    f.r.hset(fleet.SERVICE, 'history_pane', 'gone-pane')
    mgr.fail_arrange = True
    before = len(mgr.arrange_calls)
    for _ in range(4):
        tick(f)
    assert len(mgr.arrange_calls) == before + 1
    failed = audits(f, 'history.rebuild_failed')
    assert len(failed) == 1 and failed[0]['lost'] == 'gone-pane'


def test_rebuild_rearms_after_a_success(strict_run):
    f, mgr = strict_run['f'], strict_run['mgr']
    for _ in range(2):
        mgr.alive.discard(history_pane(f))
        confirm(f)
        assert mgr.get_pane_status(history_pane(f)) is not None
    assert len(audits(f, 'history.rebuilt')) == 2


def test_all_alive_tick_does_not_arrange(strict_run):
    f, mgr = strict_run['f'], strict_run['mgr']
    before = len(mgr.arrange_calls)
    tick(f)
    tick(f)
    assert len(mgr.arrange_calls) == before and not audits(f, 'history.rebuild_failed')


def test_no_browser_backend_is_left_alone(strict_run):
    """WezTerm: no history surface by design, '' is the normal value."""
    f, mgr = strict_run['f'], strict_run['mgr']
    mgr.supports_browser = False
    f.r.hset(fleet.SERVICE, 'history_pane', '')
    before = len(mgr.arrange_calls)
    tick(f)
    assert len(mgr.arrange_calls) == before and not audits(f, 'history.rebuild_failed')


def test_a_single_lost_reading_never_arranges(strict_run):
    """A backend read that fails once (the pane looks gone, then is seen again) never lays the page out."""
    f, mgr = strict_run['f'], strict_run['mgr']
    hist = history_pane(f)
    before = len(mgr.arrange_calls)
    for _ in range(2):
        mgr.alive.discard(hist)
        tick(f)  # one lost reading
        mgr.alive.add(hist)
        tick(f)  # seen again: the mark is cleared
    assert len(mgr.arrange_calls) == before and history_pane(f) == hist
    assert not audits(f, 'history.rebuilt') and not audits(f, 'history.rebuild_failed')
