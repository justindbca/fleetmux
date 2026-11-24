"""The submitter gets the review back (docs/design.md §62): when a job's sub-fleet ends, the job's one result (what
`fleetmux result <job>` prints, and the <run>/job-results/<job>.json copy a join stub reads) carries the sub-fleet's
``decompose`` block from its result.json: ``review`` with its ``seed`` section (null when the job brought no tree)
and the closing ``tune``. With the switch off the job result is as before (no ``decompose``)."""
import json

import pytest

from fleetmux import decompose, fleet, remote, subfleet
from test_decompose_review import TUNE
from test_decompose_seed_required import tree_file
from test_escalate import ok_check  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_service_job import GOAL, start_job
from test_service_split import ready_again, split_tree  # noqa: F401
from test_subfleet import adopt_by_hook, step, stop_child_like_daemon

pytestmark = pytest.mark.usefixtures('job_chains_as_before')


def run_job(t, monkeypatch, spec, tune=None):
    """Start the job, let its sub-fleet complete (optionally with an applied tune), collect it; the job's result as
    `fleetmux result <job>` prints it."""
    f = t.f
    f.decompose_load()
    jid, key = start_job(t, spec)
    cf, _ = adopt_by_hook(t, monkeypatch, subfleet.record(f.run_dir / 'sub' / key))
    step(t)
    if tune:
        cf.decompose_load()
        decompose.tune_apply(cf.r, tune, key)
    cf.v2.finish('completed', 'test end: completed')
    cf.write_result()
    f.tick()
    stop_child_like_daemon(cf)
    f.tick()
    step(t)
    ready_again(t)
    return jid, key, cf


def printed(f, jid, capsys):
    capsys.readouterr()
    assert remote.result_cli(f.r, jid) == remote.FOUND
    return json.loads(capsys.readouterr().out)


def test_a_jobs_result_carries_the_review_with_the_seed_and_the_tune(split_tree, decompose_on, tmp_path,
                                                                     monkeypatch, capsys):
    t, f = split_tree, split_tree.f
    jid, key, cf = run_job(t, monkeypatch, {'goal': GOAL, 'tree': tree_file(tmp_path), 'by': 'orchestrator'},
                           tune=[TUNE[0]])
    out = printed(f, jid, capsys)
    dec = out['result']['decompose']
    assert dec == json.loads((cf.run_dir / 'result.json').read_text())['decompose']  # the child's block, whole
    seed = dec['review']['seed']
    assert seed['by'] == 'orchestrator' and [b['id'] for b in seed['blocks']] == [key + '.' + str(i) for i in range(4)]
    assert set(seed) >= {'blocks', 'added', 'counts', 'chain'}
    assert [x['param'] for x in dec['tune']] == [TUNE[0]['param']] and dec['tune'][0]['origin'] == key
    copy = fleet.load(f.run_dir / fleet.JOB_RESULTS / (jid + '.json'))  # a join stub's `result` when Redis is gone
    assert copy['result']['decompose'] == dec


def test_a_job_without_a_tree_gets_a_null_seed_and_no_error(split_tree, decompose_on, monkeypatch, capsys):
    t, f = split_tree, split_tree.f
    jid, _, _ = run_job(t, monkeypatch, {'goal': GOAL})
    out = printed(f, jid, capsys)
    assert out['status'] == 'ok', out
    dec = out['result']['decompose']
    assert dec['review']['seed'] is None and dec['tune'] == []


def test_switch_off_the_job_result_is_as_before(split_tree, monkeypatch, capsys):
    t, f = split_tree, split_tree.f
    assert not decompose.enabled()
    jid, _, _ = run_job(t, monkeypatch, {'goal': GOAL})
    out = printed(f, jid, capsys)
    assert out['status'] == 'ok' and 'decompose' not in out['result']
