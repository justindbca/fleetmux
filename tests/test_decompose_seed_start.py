"""A development fleet started with `fleetmux start --lifecycle dev_task` while a root runs attaches to it (a join stub,
docs/design.md §58) and its work goes in as a job: `fleetmux task --config <stub> <spec>`. That spec is a JSON object
that join passes whole (``merged_spec``, then ``task --job -`` on the root), so ``tree`` and ``by`` reach the root's
``job_submit`` as written (§62): with the switch on, a dev_task job with a tree is taken and its seed becomes the
sub-fleet's tree; without one it is refused with the message naming ``fleetmux tree check``; switch off, as before."""
import json
import uuid

import pytest

from fleetmux import decompose, join, runtime_v2, subfleet
from test_decompose_seed_required import tree_file
from test_escalate import ok_check  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_service_job import DELIVER, GOAL, job_rec, jobs, team
from test_service_split import split_tree  # noqa: F401
from test_start_join import alive, run_main, scan, stubbed  # noqa: F401
from test_subfleet import adopt_by_hook, step

pytestmark = pytest.mark.usefixtures('job_chains_as_before')


def stub_of(tmp_path):
    """The join stub a `start --lifecycle dev_task --roster ... --roles ... --name ...` attach writes (its options)."""
    path, roles = team(tmp_path)
    return {'root_config': '/abs/root/config.json', 'roster': str(path), 'roles': str(roles),
            'lifecycle': 'dev_task', 'name': 'feature-x', 'budget': None, 'jobs': []}


def wired(t, monkeypatch):
    """join's root CLI call answered by the real root fleet ``t.f``: what `task --config <root> --job -` posts."""
    f = t.f
    monkeypatch.setattr(join, '_root', lambda stub: None)

    def root_cli(argv, stdin=None, timeout=join.CLI_TIMEOUT):
        assert argv[:1] == ['task'] and '--job' in argv
        cid = 'join-' + uuid.uuid4().hex[:8]
        runtime_v2.post_event(f.v2.bus, {'kind': 'task', 'request_id': cid, 'spec': json.loads(stdin), 'job': True},
                              cid)
        step(t)
        return 0, json.dumps(runtime_v2.wait_reply(f.v2.bus, cid, timeout=2)), ''
    monkeypatch.setattr(join, 'fleet_cli', root_cli)


def test_the_stub_passes_tree_and_by_to_the_root(stubbed, monkeypatch, capsys):  # noqa: F811
    stubbed.replies['task'] = (0, json.dumps({'status': 'accepted', 'job_id': 'job-abc', 'task_id': 'job-abc'}), '')
    spec = {'goal': 'build it', 'deliver': DELIVER, 'tree': '/abs/tree.json', 'by': 'orchestrator'}
    assert run_main(['task', '--config', str(stubbed.path), '-'], monkeypatch, json.dumps(spec)) == 0
    sent = json.loads(stubbed.calls[-1][1])
    assert sent['tree'] == '/abs/tree.json' and sent['by'] == 'orchestrator' and sent['lifecycle'] == 'dev_task'


def test_a_started_dev_task_job_with_a_tree_is_taken_and_seeds_its_sub_fleet(split_tree, decompose_on, tmp_path,
                                                                             monkeypatch):
    t, f = split_tree, split_tree.f
    wired(t, monkeypatch)
    spec = {'goal': GOAL, 'deliver': DELIVER, 'tree': tree_file(tmp_path), 'by': 'orchestrator'}
    jid = join.submit('/abs/root/config.json', join.merged_spec(stub_of(tmp_path), spec))
    assert jobs(f)[jid]['tree'] == spec['tree'] and jobs(f)[jid]['lifecycle'] == 'dev_task'
    f.tick()
    step(t)
    f.tick()
    step(t)
    key, _ = job_rec(f, jid)
    cf, _ = adopt_by_hook(t, monkeypatch, subfleet.record(f.run_dir / 'sub' / key))
    seed = cf.tree_state()['seed']
    assert seed['by'] == 'orchestrator'
    assert [b['id'] for b in seed['blocks']] == [key + '.0', key + '.1', key + '.2', key + '.3']
    assert [n['id'] for n in decompose.level_nodes(cf.tree_state())] == [b['id'] for b in seed['blocks']]


def test_a_started_dev_task_job_without_a_tree_is_refused(split_tree, decompose_on, tmp_path, monkeypatch):
    t, f = split_tree, split_tree.f
    wired(t, monkeypatch)
    with pytest.raises(join.JoinError) as exc:
        join.submit('/abs/root/config.json', join.merged_spec(stub_of(tmp_path), {'goal': GOAL, 'deliver': DELIVER}))
    assert 'fleetmux tree check' in str(exc.value) and 'the root refused the job' in str(exc.value)
    assert not jobs(f)


def test_switch_off_a_started_dev_task_job_without_a_tree_is_taken_as_before(split_tree, tmp_path, monkeypatch):
    t, f = split_tree, split_tree.f
    assert not decompose.enabled()
    wired(t, monkeypatch)
    jid = join.submit('/abs/root/config.json', join.merged_spec(stub_of(tmp_path), {'goal': GOAL, 'deliver': DELIVER}))
    assert jobs(f)[jid]['lifecycle'] == 'dev_task' and jobs(f)[jid]['tree'] is None
