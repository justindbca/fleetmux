"""Cancelled by the operator stays cancelled across a restart (docs/design.md section 60): a job cancelled with
`task --cancel`, an attached sub-fleet taken down with its stub's `stop` (also while the root is down), and
everything after `stop --final`. Private test Redis, fake cmux, per-test fleet home."""
import json
from types import SimpleNamespace

import pytest

from restart_kit import items, kill_extra, ledger, ledgers, new_run, operator_stop, tick
from test_escalate import ok_check  # noqa: F401  (fixture)
from test_job_queue_restart import all_ready, continue_root, explicit_stop, jobs
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_named_subfleet_chain import named_chain_as_before  # noqa: F401  (fixture)
from test_service_attach import attach_up, root  # noqa: F401  (fixture)
from test_service_escalate import result
from test_subfleet import svc  # noqa: F401  (fixture of root)
from test_service_job import start_job
from test_service_split import split_tree  # noqa: F401
from test_subfleet import bring_up, step, write_roster
from fleetmux import fleet, join, lifecycle_close, named_subfleet, restart_state


@pytest.fixture
def rt(root, monkeypatch):
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: 'sent')
    yield root
    kill_extra(root)


@pytest.fixture
def jt(split_tree, monkeypatch):
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: 'sent')
    yield split_tree
    kill_extra(split_tree)


@pytest.mark.usefixtures('job_chains_as_before')
def test_a_cancelled_running_job_is_not_resumed_and_is_listed_as_cancelled(jt):
    t = jt
    jid, key = start_job(t)
    t.f.job_cancel(jid, by='operator')
    restart_state.on_stop(t.f)
    explicit_stop(t)
    f = continue_root(t)
    all_ready(t)
    for _ in range(4):
        f.tick()
        step(t)
    assert jobs(f).get(jid, {}).get('state') != 'running'
    assert not [i for i in items(f, 'job') if i['key'] == jid]  # nothing to redo
    led = ledger(f) or (ledgers(f) or [{}])[-1]
    assert any(c['key'] == jid for c in led.get('cancelled', [])) or not jobs(f).get(jid)


@pytest.mark.usefixtures('named_chain_as_before')
def test_stub_stop_while_the_root_is_down_writes_a_tombstone_and_it_does_not_come_back(rt, capsys):
    rid, _, _ = attach_up(rt, 'docs')
    tick(rt)
    operator_stop(rt)
    stub = {'kind': 'service', 'child': 'docs', 'root_config': str(rt.f.path), 'root_home': str(fleet.HOME),
            'attach_id': rid}
    assert join.service_cli(SimpleNamespace(cmd='stop'), fleet.HOME / 'joins' / 'docs.json', stub) == 0
    assert '"cancelled": true' in capsys.readouterr().out
    f = new_run(rt)
    tick(rt)
    assert 'docs' not in named_subfleet.attached(f)
    led = ledgers(f)
    assert led and any(c['key'] == 'docs' for c in led[-1]['cancelled'])


@pytest.mark.usefixtures('named_chain_as_before')
def test_stop_final_brings_nothing_back_and_says_so_quietly(rt):
    attach_up(rt, 'docs')
    tick(rt)
    operator_stop(rt, final=True)
    assert (restart_state.root_dir(rt.f.cfg) / 'cancels' / 'subfleet-docs.json').exists()
    f = new_run(rt)
    tick(rt, 2)
    assert 'docs' not in named_subfleet.attached(f) and ledgers(f) == []
    st = restart_state.load_state(restart_state.fleet_dir(f))
    assert 'stop --final' in st.get('final_note', '')


@pytest.mark.usefixtures('job_chains_as_before')
def test_stop_final_cancels_open_jobs(jt):
    t = jt
    jid, key = start_job(t)
    t.f.stop_final = True
    restart_state.on_stop(t.f)
    assert jobs(t.f).get(jid, {}).get('cancel') or result(t.f, jid)


@pytest.mark.usefixtures('named_chain_as_before')
def test_stub_stop_while_the_root_is_down_holds_for_a_continued_run_too(rt, capsys):
    rid, _, _ = attach_up(rt, 'docs')
    tick(rt)
    operator_stop(rt)
    stub = {'kind': 'service', 'child': 'docs', 'root_config': str(rt.f.path), 'root_home': str(fleet.HOME),
            'attach_id': rid}
    assert join.service_cli(SimpleNamespace(cmd='stop'), fleet.HOME / 'joins' / 'docs.json', stub) == 0
    tomb = restart_state.root_dir(rt.f.cfg) / 'cancels' / 'subfleet-docs.json'
    assert tomb.exists()
    f = continue_root(rt)  # the same run continued (daemon --resume), not a new run
    tick(rt, 2)
    assert 'docs' not in named_subfleet.attached(f)
    assert 'docs' not in named_subfleet.declared(f)
    assert 'root was down' in json.loads(tomb.read_text())['how']  # how it was cancelled is kept


@pytest.fixture
def st(svc, monkeypatch):
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: 'sent')
    yield svc
    kill_extra(svc)


@pytest.mark.usefixtures('named_chain_as_before')
def test_a_roster_row_removed_while_the_root_is_down_is_cancelled_not_lost_and_never_redone(st):
    """D14: the operator stops the root, deletes the sub-fleet's roster row, starts again: the ledger lists it as
    cancelled (removed from the roster), no lost item, nothing to rule; a redo cannot bring it back."""
    bring_up(st, 'ops')  # the roster sub-fleet ops runs
    tick(st)
    operator_stop(st)
    write_roster(st, declare=False)  # the row is gone
    f = new_run(st)
    tick(st, 2)
    assert 'ops' not in named_subfleet.declared(f)
    assert not [i for i in items(f, 'subfleet') if i['key'] == 'ops']  # no lost item, nothing to rule
    led = ledger(f) or (ledgers(f) or [{}])[-1]
    cancelled = [c for c in led.get('cancelled', []) if c['key'] == 'ops']
    assert cancelled and 'removed from the roster' in cancelled[0]['why']
    assert 'ops' not in named_subfleet.attached(f)


@pytest.mark.usefixtures('named_chain_as_before')
def test_a_roster_row_that_is_still_there_is_still_listed_when_its_child_is_not_up(st):
    bring_up(st, 'ops')
    tick(st)
    operator_stop(st)
    f = new_run(st)  # the same roster: the row is still declared
    tick(st, 2)
    assert 'ops' in named_subfleet.declared(f)
    led = ledger(f) or (ledgers(f) or [{}])[-1]
    assert not [c for c in led.get('cancelled', []) if c['key'] == 'ops']
    ops = [i for i in items(f, 'subfleet') if i['key'] == 'ops']
    assert ops and ops[0]['class'] == 'stopped'  # declared but not running now: listed as before


@pytest.mark.usefixtures('named_chain_as_before')
def test_redo_and_restore_take_only_an_attached_row(st):
    f = st.f
    write_roster(st)  # the child roster file exists
    row = {'form': 'roster', 'run_dir': str(f.run_dir / 'sub' / 'gone'), 'alive': False,
           'entry': {'roster': str(st.tmp / 'child.yaml')}, 'files': {'roster': 'workers:\n  - name: ops-1\n'}}
    got = restart_state.restore_attached(f, 'gone', row)  # a roster row of the previous run, not an attach
    assert got['state'] == 'failed' and 'attached' in got['note']
    assert 'gone' not in named_subfleet.attached(f)
