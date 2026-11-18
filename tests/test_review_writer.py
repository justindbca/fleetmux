"""Delivery gate: who wrote the review verdict, and where it is read.

1. Who reviews is the supervisor's decision: the gate checks only the verdict (PASS, zero open P0/P1), never the
   reviewer's model or a ``reviewer`` field. The writing slot and its model are recorded for the audit only.
2. One rule for where a verdict is read (gate, review cap, review_loop): the workspace root, else the directory of
   the worker the supervisor dispatched the review to ("review": true), else exactly one match among all workers.
   The review dispatch names the absolute path; the REVIEWER.md template documents the JSON the gate reads.
Private test Redis, fake cmux and fake Pi agents (test_runtime_v2 fixture); a temporary git repository.
"""
import json

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx, receive, settle  # noqa: F401  (fixture)
from test_service_escalate import audits  # noqa: F401
from test_service_split import queued_to
from test_lifecycle_close import (as_task, deliver_spec, git, make_reviewer, notes, repo, submit,  # noqa: F401
                                  table)
from fleetmux import fleet, lifecycle_deliver, runtime_v2

REVIEW = 'review/verdict.json'


def spec_for(repo, tmp_path):
    return dict(deliver_spec(repo, tmp_path), review_file=REVIEW)


def verdict_at(f, slot, verdict='PASS', **extra):
    path = f.workspace() / slot / REVIEW
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict({'round': 1, 'verdict': verdict, 'issues': []}, **extra)))
    return path


def complete(f, spec):
    """The supervisor's COMPLETED through the v2 settle path; the artifact is the verdict file it names."""
    path = spec['deliver']['review_file']
    if not (f.workspace() / path).exists():
        path = next(str(p.relative_to(f.workspace())) for p in f.workspace().glob('*/' + REVIEW))
    submit(f, dict(spec, artifacts=[{'path': path}]))
    f.pump(); env = receive(f, 'supervisor')
    settle(f, 'supervisor', env, {'status': 'COMPLETED', 'completion': 100, 'dispatch': [], 'reason': 'verified'})
    assert f.task()['status'] == 'completed'


def gate(f):
    return json.loads(f.r.hget('fx:lifecycle', 'gate:' + f.task()['id']))


# ------------------------------------------------------------------ 1. the gate never checks who reviewed
def test_pass_without_reviewer_field_in_the_reviewer_slot_directory_passes(fx, table, notes, repo, tmp_path):
    f = as_task(fx, 'dev_task')
    make_reviewer(f, 'worker-2')  # worker-2 runs the claude-reviewer card (its model goes in the record)
    verdict_at(f, 'worker-2')     # the reviewer wrote it in its own directory, no "reviewer" field
    complete(f, {'goal': 'ship', 'deliver': spec_for(repo, tmp_path)})
    f.tick()
    rec = gate(f)
    assert rec['ok'], rec.get('problem')
    assert rec['review']['slot'] == 'worker-2' and rec['review']['model'] == 'opus'  # audit only
    assert rec['review']['path'] == str(f.workspace() / 'worker-2' / REVIEW)
    assert git(repo, 'rev-parse', 'main') == git(repo, 'rev-parse', 'feat/x')


@pytest.mark.parametrize('field', [None, 'sonnet', 'whoever'])
def test_any_reviewer_the_supervisor_picked_passes_on_a_clean_verdict(fx, table, notes, repo, tmp_path, field):
    f = as_task(fx, 'dev_task')  # worker-1 runs the fleet default (no model card), not a reviewer slot by name
    verdict_at(f, 'worker-1', **({'reviewer': field} if field else {}))
    complete(f, {'goal': 'ship', 'deliver': dict(spec_for(repo, tmp_path), review_file='worker-1/' + REVIEW)})
    f.tick()
    rec = gate(f)
    assert rec['ok'], rec.get('problem')
    assert rec['review']['slot'] == 'worker-1' and rec['review']['model'] is None


def test_review_problem_reads_only_the_verdict():
    v = {'verdict': 'PASS', 'round': 1, 'issues': [], 'reviewer': 'sonnet'}
    assert lifecycle_deliver.review_problem(v) is None
    v = dict(v, issues=[{'id': 'P1-1', 'severity': 'P1', 'title': 'x'}])
    assert lifecycle_deliver.review_problem(v) == 'review PASS still lists 0 P0 / 1 P1'


def test_the_mode_table_names_no_reviewer_model():
    from fleetmux import lifecycle
    assert 'reviewer' not in lifecycle.mode_policy('dev_task', 'review_step')


# ------------------------------------------------------------------ 2. one rule for where it is read
def test_load_verdict_reads_the_root_then_only_the_dispatched_review_slot(tmp_path):
    slots = {'models': {'worker-1': None, 'worker-2': None}, 'review_slot': 'worker-2'}
    other = tmp_path / 'worker-1' / REVIEW
    other.parent.mkdir(parents=True)
    other.write_text(json.dumps({'verdict': 'PASS'}))
    v, writer, problem = lifecycle_deliver.load_verdict(tmp_path, REVIEW, slots)
    assert v is None and writer is None  # another worker's directory is not searched once a review was dispatched
    assert problem == ('review verdict not written: looked for ' + str(tmp_path / REVIEW) + ', '
                       + str(tmp_path / 'worker-2' / REVIEW) + ' (the review was dispatched to worker-2)')
    mine = tmp_path / 'worker-2' / REVIEW
    mine.parent.mkdir(parents=True)
    mine.write_text(json.dumps({'verdict': 'FAIL', 'round': 2}))
    v, writer, problem = lifecycle_deliver.load_verdict(tmp_path, REVIEW, slots)
    assert (v['verdict'], writer, problem) == ('FAIL', 'worker-2', None)
    (tmp_path / REVIEW).parent.mkdir(parents=True)
    (tmp_path / REVIEW).write_text(json.dumps({'verdict': 'PASS'}))
    v, writer, _ = lifecycle_deliver.load_verdict(tmp_path, REVIEW, slots)
    assert v['verdict'] == 'PASS' and writer == 'worker-2'  # the root first


def test_without_a_review_dispatch_exactly_one_worker_match_is_accepted(tmp_path):
    slots = {'models': {'worker-1': None, 'worker-2': None, 'worker-3': None}, 'review_slot': None}
    v, _, problem = lifecycle_deliver.load_verdict(tmp_path, REVIEW, slots)
    assert v is None and problem.startswith('no review was dispatched and no verdict was found: looked for ')
    assert str(tmp_path / 'worker-3' / REVIEW) in problem
    for w in ('worker-1', 'worker-3'):
        (tmp_path / w / REVIEW).parent.mkdir(parents=True)
        (tmp_path / w / REVIEW).write_text(json.dumps({'verdict': 'PASS'}))
    v, _, problem = lifecycle_deliver.load_verdict(tmp_path, REVIEW, slots)
    assert v is None and 'is in several worker directories' in problem
    assert str(tmp_path / 'worker-1' / REVIEW) in problem and str(tmp_path / 'worker-3' / REVIEW) in problem
    (tmp_path / 'worker-1' / REVIEW).unlink()
    v, writer, problem = lifecycle_deliver.load_verdict(tmp_path, REVIEW, slots)
    assert (v['verdict'], writer, problem) == ('PASS', 'worker-3', None)


def test_review_cap_reads_the_worker_directory(fx, table, notes, repo, tmp_path):
    f = as_task(fx, 'dev_task')
    make_reviewer(f, 'worker-2')
    submit(f, {'goal': 'ship', 'deliver': spec_for(repo, tmp_path)})
    verdict_at(f, 'worker-2', 'FAIL', round=3, issues=[{'id': 'P1-1', 'severity': 'P1', 'title': 'x'}])
    f.tick()
    assert f.task()['status'] == 'blocked' and 'dev_task review: round 3' in f.task()['reason']


def test_reviewer_md_documents_the_verdict_json_the_gate_reads():
    text = (fleet.HERE / 'agents' / 'reviewer.md').read_text()
    assert '## R6. Verdict JSON' in text and '"verdict": "PASS"' in text and '"status": "open"' in text
    assert 'Verdict file for this run' in text and 'No reviewer field needed' in text


def review_dispatch(f, slot, review=True):
    """The supervisor's real RUNNING decision with one dispatch item marked as the review."""
    f.pump(); env = receive(f, 'supervisor')
    settle(f, 'supervisor', env, {'status': 'RUNNING', 'completion': 50, 'reason': 'review next', 'dispatch': [
        {'to': slot, 'instruction': 'Review the feature branch against main and write the verdict', 'review': review}]})


def test_the_review_dispatch_records_the_slot_and_names_the_path(fx, table, notes, repo, tmp_path):
    f = as_task(fx, 'dev_task')
    submit(f, {'goal': 'ship', 'deliver': spec_for(repo, tmp_path)})
    (prompt,) = queued_to(f, 'supervisor', 'Review verdict: the delivery gate reads')
    assert '"review": true' in prompt['body']['text'] and str(f.workspace() / REVIEW) in prompt['body']['text']
    review_dispatch(f, 'worker-2')
    target = f.workspace() / 'worker-2' / REVIEW
    assert f.task()['review_slot'] == 'worker-2'
    assert '`' + str(target) + '`' in (fleet.agent_cwd(f.cfg, 'worker-2') / 'REVIEWER.md').read_text()
    assert f.reviewer_doc('worker-2').endswith('`' + str(target) + '`\n')  # also what a respawn writes
    assert 'Verdict file for this run\n' not in f.reviewer_doc('worker-1')
    (msg,) = queued_to(f, 'worker-2', '[Review]')
    assert 'to exactly ' + str(target) in msg['body']['text']
    verdict_at(f, 'worker-1', 'FAIL')  # another worker's file is not read once a review was dispatched
    verdict_at(f, 'worker-2')
    f.r.hset(fleet.TASK, mapping={'status': 'completed'})
    f.tick()
    rec = gate(f)
    assert rec['ok'] and rec['review']['slot'] == 'worker-2', rec.get('problem')


def test_review_must_be_a_boolean():
    with pytest.raises(ValueError, match='dispatch.review must be true or false'):
        fleet.parse_decision(json.dumps({'status': 'RUNNING', 'completion': 1, 'dispatch': [
            {'to': 'worker-1', 'instruction': 'Review the feature branch please', 'review': 'yes'}]}), ['worker-1'], False)


@pytest.mark.parametrize('bad', ['/abs/verdict.json', '../outside.json', 'a/../../x.json', ''])
def test_review_file_outside_the_workspace_is_refused_at_submit(fx, table, repo, tmp_path, bad):
    f = as_task(fx, 'dev_task')
    rid = 'bad-' + str(abs(hash(bad)))
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id=rid, spec={
        'goal': 'ship', 'deliver': dict(spec_for(repo, tmp_path), review_file=bad)}), rid)
    assert f.v2.step(10)
    answer = runtime_v2.wait_reply(f.v2.bus, rid, timeout=2)
    assert answer['status'] == 'rejected' and 'review_file must be a relative path inside the shared workspace' in answer['reason']
    assert not f.task().get('id')


def test_a_broken_registry_never_stops_the_gate(fx, table, notes, repo, tmp_path, monkeypatch):
    from fleetmux import agent_registry

    def broken(*a, **k):
        raise RuntimeError('registry unreadable')
    f = as_task(fx, 'dev_task')
    make_reviewer(f, 'worker-2')
    verdict_at(f, 'worker-2')
    complete(f, {'goal': 'ship', 'deliver': spec_for(repo, tmp_path)})
    monkeypatch.setattr(agent_registry, 'AgentRegistry', broken)
    f.tick()
    rec = gate(f)
    assert rec['ok'], rec.get('problem')
    assert rec['review']['slot'] == 'worker-2' and rec['review']['model'] is None  # audit only: left empty
