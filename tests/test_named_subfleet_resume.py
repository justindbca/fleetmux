"""§29 x service resume (Codex Astra r1 P0-01): an accepted external sub-fleet op whose approval was still
queued for the supervisor when the daemon stopped. ``start --resume`` cancels every undelivered supervisor
message (RuntimeV2.clear_slot) and replays REQQ (requeue_requests); the op lives in its own OPQ, so it was
left awaiting forever, with no message, no result, and the same id refused as a duplicate.

The reviewer's offline reproduction input, with the real request / queue / clear_slot / requeue_requests /
tick and the repository's in-memory Source + effect executor (no Lua, no process, no Redis server)."""
import json

import pytest

from test_runtime_v2_planning import plan  # noqa: F401  (fixture)
from test_clear_after_result import apply_stub
from fleetmux import fleet, named_subfleet, roster

RID = 'review-up-1'


def accepted_then_resumed(plan, monkeypatch, tmp_path):  # noqa: F811
    f, v, r, env = plan
    f.cfg.update(worker_cli='claude',  # default pi supervisor + official Claude worker
                 adopted_supervisor_hook_delivery=True)  # every new start pins it (§19 / §48)
    f.path = tmp_path / 'config.json'
    (tmp_path / 'child.yaml').write_text('workers:\n  - name: ops-w1\n')
    (tmp_path / 'sup.md').write_text('Supervise ops.')
    parent = tmp_path / 'parent.yaml'
    parent.write_text('workers:\n  - name: worker-1\nsubfleets:\n  - {name: ops, supervisor_slot: worker-1, '
                      'supervisor_role: sup.md, roster: child.yaml, autostart: false, mode: service}\n')
    roster.load_entries(parent)
    entries = roster.load_subfleets(parent)
    f.cfg.update(roster=str(parent), subfleets=entries)
    named_subfleet.declare(f, entries)
    fleet.save(f.path, f.cfg)
    r.delete(fleet.REQ + 'r1', fleet.REQQ, v.bus.lease_key('worker-1'))
    assert f.request(dict(id=RID, op='subfleet.up', name='ops', reason='start daily service')) == RID
    assert f.r.hget(named_subfleet.OP + RID, 'status') == 'awaiting'
    apply_stub(plan, monkeypatch)
    before = v.bus.pending('supervisor')
    assert len(before) == 1 and not r.llen(fleet.REQQ)
    # resume_run's own reconciliation: cancel undelivered supervisor messages, then replay
    assert v.clear_slot('supervisor', False)
    v.transaction('review-resume', lambda: v.requeue_requests({}))
    for _ in range(3):
        named_subfleet.tick(f)
    return f, v, r, before


def test_resume_asks_the_supervisor_again_for_an_awaiting_external_op(plan, monkeypatch, tmp_path):  # noqa: F811
    f, v, r, before = accepted_then_resumed(plan, monkeypatch, tmp_path)
    assert v.bus.message(before[0])['outcome'] == 'cancelled'
    after = v.bus.pending('supervisor')
    assert len(after) == 1 and after != before, 'the approval is asked again after resume'
    assert RID in v.bus.message(after[0])['body']['text'] and '"subfleet"' in v.bus.message(after[0])['body']['text']
    assert r.hget(named_subfleet.OP + RID, 'status') == 'awaiting' and r.lrange(named_subfleet.OPQ, 0, -1) == [RID]


def test_a_resent_id_of_an_open_op_is_in_progress_not_a_duplicate(plan, monkeypatch, tmp_path):  # noqa: F811
    f, v, r, _ = accepted_then_resumed(plan, monkeypatch, tmp_path)
    with pytest.raises(named_subfleet.InProgress) as exc:
        f.request(dict(id=RID, op='subfleet.up', name='ops', reason='retry after resume'))
    assert exc.value.rid == RID and exc.value.status == 'awaiting'
    assert len(v.bus.pending('supervisor')) == 1, 'a retry never queues a second approval'
    with pytest.raises(ValueError, match='duplicate request id'):  # same id, another op: still refused
        f.request(dict(id=RID, op='subfleet.down', name='ops'))


def test_the_requeued_approval_reaches_a_final_result(plan, monkeypatch, tmp_path):  # noqa: F811
    f, v, r, _ = accepted_then_resumed(plan, monkeypatch, tmp_path)
    v.transaction('review-decide', lambda: named_subfleet.decide(f, {'id': RID, 'decision': 'reject', 'note': 'not today'}))
    res = json.loads(r.get(fleet.RESULT + RID))
    assert res['status'] == 'rejected' and 'not today' in res['reason']
    assert not r.lrange(named_subfleet.OPQ, 0, -1) and not r.hgetall(named_subfleet.OP + RID)
    with pytest.raises(ValueError, match='duplicate request id'):  # finished: the caller reads its result
        f.request(dict(id=RID, op='subfleet.up', name='ops'))
