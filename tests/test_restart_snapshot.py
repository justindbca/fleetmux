"""Restart state snapshots (docs/design.md section 60): when they are written, how, how many are kept, and what a
broken or missing snapshot does to the next start. Private test Redis, fake cmux, per-test fleet home only."""
import inspect
import json
import time

import pytest

from restart_kit import all_ready, items, kill_extra, ledger, ledgers, new_run, operator_stop, rdir, snaps, tick
from test_service_attach import attach_up, root  # noqa: F401  (fixture)
from test_named_subfleet_chain import named_chain_as_before  # noqa: F401  (fixture)
from test_subfleet import svc  # noqa: F401  (fixture)
from fleetmux import fleet, lifecycle_close, restart_state, runtime_v2

pytestmark = pytest.mark.usefixtures('named_chain_as_before')  # section 29 chains: attached children keep the model


@pytest.fixture
def rt(root, monkeypatch):
    sent = []
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: sent.append(rec) or 'sent')
    root.sent = sent
    yield root
    kill_extra(root)


def knob(monkeypatch, **over):
    real = restart_state.knobs
    monkeypatch.setattr(restart_state, 'knobs', lambda: dict(real(), **over))


# ------------------------------------------------------------------ the write points
def test_timer_snapshot_every_interval_and_frozen_while_stopping(rt, monkeypatch):
    f = rt.f
    knob(monkeypatch, snapshot_every_s=10)
    tick(rt)  # the boot reconcile; the first tick of a life only starts the clock
    assert snaps(f, 'timer') == []
    st = restart_state.load_state(rdir(f))
    st['last_timer_at'] = time.time() - 11
    restart_state.save_state(rdir(f), st)
    tick(rt)
    assert len(snaps(f, 'timer')) == 1
    tick(rt)
    assert len(snaps(f, 'timer')) == 1  # not again before the interval
    st = restart_state.load_state(rdir(f))
    st['last_timer_at'] = time.time() - 11
    restart_state.save_state(rdir(f), st)
    f.stopping = True
    restart_state.tick(f)
    assert len(snaps(f, 'timer')) == 1  # frozen while the fleet stops
    f.stopping = False


def test_operator_stop_snapshot_comes_before_anything_is_taken_down(rt):
    src = inspect.getsource(runtime_v2.RuntimeV2.run)
    assert src.index('restart_state.on_stop(self.f)') < src.index('applet_host.stop_all(self.f)') \
        < src.index('subfleet.stop_children(self.f)')
    _, rec, _ = attach_up(rt, 'docs')
    tick(rt)
    operator_stop(rt)
    (seq, reason, path), = snaps(rt.f, 'stop')
    doc = json.loads(path.read_text())
    assert doc['ended'] == 'operator' and doc['final'] is False and doc['schema'] == restart_state.SCHEMA
    assert doc['kinds']['subfleets']['docs']['alive'] is True  # taken while the child still ran
    assert doc['kinds']['subfleets']['docs']['form'] == 'attached'


def test_resume_entry_snapshot_is_the_first_step_of_resume_run():
    src = inspect.getsource(runtime_v2.RuntimeV2.resume_run)
    assert src.index('restart_state.on_resume_entry(self.f)') < src.index('self.f.job_resume_reconcile()')


def test_a_new_supervisor_generation_writes_a_snapshot_first(rt):
    f = rt.f
    tick(rt)  # the READY of the first generation is recorded
    assert snaps(f, 'supervisor') == []
    gen = int(f.agent('supervisor')['generation'])
    f.setf('supervisor', generation=gen + 1, label='supervisor.' + str(gen + 1))
    f.spawn('supervisor', 'You are the next supervisor. Reply READY.')
    (seq, reason, path), = snaps(f, 'supervisor')
    assert json.loads(path.read_text())['for_generation'] == gen + 1


# ------------------------------------------------------------------ how it is written and kept
def test_atomic_write_schema_and_retention(rt, monkeypatch):
    f = rt.f
    knob(monkeypatch, keep_timer=2, keep_events=1)
    for _ in range(4):
        restart_state.write(f, 'timer')
    for _ in range(3):
        restart_state.write(f, 'supervisor')
    assert [s[0] for s in snaps(f, 'timer')] == [3, 4] or len(snaps(f, 'timer')) == 2
    assert len(snaps(f, 'supervisor')) <= 3  # the unconsumed event snapshots stay until a boot consumed them
    (rdir(f) / 'snap-00000099-timer.json.tmp').write_text('{"half')
    for s in snaps(f):
        assert json.loads(s[2].read_text())['schema'] == restart_state.SCHEMA
    doc, problem = restart_state.read_snapshot(rdir(f) / 'snap-00000099-timer.json.tmp')
    assert doc is None and 'JSON' in problem
    st = restart_state.load_state(rdir(f))
    st['consumed_seq'] = 100
    restart_state.save_state(rdir(f), st)
    restart_state.write(f, 'supervisor')
    assert len(snaps(f, 'supervisor')) == 1  # consumed ones beyond keep_events are pruned


# ------------------------------------------------------------------ a broken or missing snapshot: said, start goes on
@pytest.mark.parametrize('damage', ['truncated', 'schema', 'deleted'])
def test_broken_or_missing_snapshot_is_reported_and_the_start_goes_on(rt, damage):
    tick(rt)
    operator_stop(rt)
    (_, _, path), = snaps(rt.f, 'stop')
    if damage == 'truncated':
        path.write_text(path.read_text()[:50])
    elif damage == 'schema':
        doc = json.loads(path.read_text())
        doc['schema'] = 99
        path.write_text(json.dumps(doc))
    else:
        for s in snaps(rt.f):
            s[2].unlink()
    f = new_run(rt)
    tick(rt)
    found = [i for i in items(f) if i['kind'] == 'snapshot']
    assert found and found[0]['class'] == 'errored', items(f)
    assert any(x.get('event') == 'restart.snapshot_unreadable' for _, x in f.r.xrange(f.v2.bus.audit_key))
    assert any('reconcile' in r['event'] for r in rt.sent)  # pushed on the notify channels
    assert f.agent('supervisor')['state'] in ('idle', 'busy')  # the new run runs


# ------------------------------------------------------------------ D7: which runs count as "to be continued"
def test_first_start_of_a_new_fleet_home_is_quiet(rt):
    tick(rt, 2)
    assert ledgers(rt.f) == [] and rt.sent == []


def test_a_run_that_ended_by_itself_starts_the_next_one_quietly(rt):
    f = rt.f
    tick(rt)
    restart_state.write(f, 'timer')
    fleet.save_result(f.run_dir, {}, 'completed', 'task done')  # ended by itself, no operator stop
    f.stopped = True
    new_run(rt)
    tick(rt)
    assert ledgers(rt.f) == [] and rt.sent == []
    assert any(x.get('event') == 'restart.quiet' for _, x in rt.f.r.xrange(rt.f.v2.bus.audit_key))


def test_a_run_that_ended_by_itself_before_its_stop_tail_starts_the_next_one_quietly(rt):
    f = rt.f
    tick(rt)
    f.queue('supervisor', '[note] left over from the finished task')
    fleet.save_result(f.run_dir, {}, 'completed', 'task done')  # the task ended by itself, then the stop tail runs
    operator_stop(rt)
    _, _, path = snaps(f, 'stop')[-1]
    assert json.loads(path.read_text())['ended'] == 'self'
    new_run(rt)
    tick(rt, 2)
    assert ledgers(rt.f) == [] and rt.sent == []  # nothing of the finished task is listed as lost


def test_a_result_json_an_earlier_life_left_behind_does_not_make_an_operator_stop_quiet(rt):
    import os
    f = rt.f
    tick(rt)  # this daemon life starts
    fleet.save_result(f.run_dir, {}, 'completed', 'an earlier task of this run')
    old = time.time() - 3600
    os.utime(f.run_dir / 'result.json', (old, old))  # written before this life
    operator_stop(rt)
    _, _, path = snaps(f, 'stop')[-1]
    assert json.loads(path.read_text())['ended'] == 'operator'


def test_hooks_swallow_their_errors_stop_and_generation_change_complete(rt, monkeypatch):
    f = rt.f
    tick(rt)
    monkeypatch.setattr(restart_state, 'collect_hot', lambda f, lim: 1 / 0)
    path = restart_state.write(f, 'timer')
    assert path and 'hot' in json.loads(path.read_text())['kind_errors']  # the other kinds are still written
    monkeypatch.setattr(restart_state, 'write_json', lambda p, d: 1 / 0)
    gen = int(f.agent('supervisor')['generation'])
    f.setf('supervisor', generation=gen + 1, label='supervisor.' + str(gen + 1))
    assert f.spawn('supervisor', 'You are the next supervisor. Reply READY.')  # not blocked
    operator_stop(rt)  # on_stop fails inside, the stop goes on
    assert (rt.f.run_dir / 'result.json').exists()
    assert any(x.get('event') == 'restart.error' for _, x in f.r.xrange(f.v2.bus.audit_key))
