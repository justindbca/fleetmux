"""Never twice (docs/design.md section 60): a restore never starts a sub-fleet whose earlier daemon still runs,
two reconciles of the same baseline restore once and keep one ledger, and a daemon that dies half way through a
restore does not restore again. Private test Redis, fake cmux, per-test fleet home."""
import pytest

from restart_kit import items, kill_extra, ledgers, new_run, operator_stop, tick
from test_named_subfleet_chain import named_chain_as_before  # noqa: F401  (fixture)
from test_service_attach import attach_up, root  # noqa: F401  (fixture)
from test_subfleet import svc  # noqa: F401
from fleetmux import fleet, lifecycle_close, named_subfleet, restart_state

pytestmark = pytest.mark.usefixtures('named_chain_as_before')


@pytest.fixture
def rt(root, monkeypatch):
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: 'sent')
    yield root
    kill_extra(root)


def test_a_child_whose_earlier_daemon_still_runs_is_not_started_again(rt):
    attach_up(rt, 'docs')
    tick(rt)
    restart_state.on_stop(rt.f)  # the root goes away, its child daemon keeps its lock (still running)
    rt.f.stopped = True
    f = new_run(rt)
    tick(rt)
    it, = items(f, 'subfleet')
    assert it['auto']['state'] == 'failed' and 'still runs' in it['auto']['note']
    assert 'docs' not in named_subfleet.attached(f)


def test_two_reconciles_of_one_baseline_restore_once_and_keep_one_ledger(rt):
    attach_up(rt, 'docs')
    tick(rt)
    operator_stop(rt)
    f = new_run(rt)
    tick(rt)
    first = dict(named_subfleet.attached(f)['docs'])
    again = fleet.Fleet(f.path, f.r, f.mgr, sessions=rt.sessions)  # the same run's daemon starts again (crash)
    restart_state.boot(again)
    assert named_subfleet.attached(f)['docs']['id'] == first['id']  # declared once
    assert len(ledgers(f)) == 1 and len(items(f, 'subfleet')) == 1


def test_a_daemon_dying_inside_the_restore_does_not_restore_twice(rt, monkeypatch):
    attach_up(rt, 'docs')
    tick(rt)
    operator_stop(rt)
    real = restart_state.open_ledger
    monkeypatch.setattr(restart_state, 'open_ledger', lambda *a, **k: (_ for _ in ()).throw(SystemExit('killed')))
    f = new_run(rt)
    with pytest.raises(SystemExit):
        _boot_raw(f)
    monkeypatch.setattr(restart_state, 'open_ledger', real)
    rid = named_subfleet.attached(f)['docs']['id']
    again = fleet.Fleet(f.path, f.r, f.mgr, sessions=rt.sessions)
    restart_state.boot(again)
    assert named_subfleet.attached(f)['docs']['id'] == rid  # the baseline was consumed first: no second attach


def _boot_raw(f):
    """boot() without its catch-all (a process dying, not an error)."""
    d = restart_state.fleet_dir(f)
    st = restart_state.load_state(d)
    doc, problems, end = restart_state.choose_baseline(f, d, st)
    st['consumed_seq'] = max([int(st.get('consumed_seq') or 0)] + [s for s, _, _ in restart_state.snapshots(d)])
    restart_state.save_state(d, st)
    restart_state.reconcile(f, doc, 'restart', end=end, problems=problems)
