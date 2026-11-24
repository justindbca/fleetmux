"""Service splits write their pieces into the sub-fleet's tree (docs/design.md §62): a service fleet keeps no tree of
its own, but a worker's P7 split and a supervisor_split both hand the child a seed: the request's instruction as the
root, the approved pieces (numbered <M<n>>.<i>) as its first-level blocks. Never put to the account."""
import json

import pytest

from fleetmux import decompose
from test_escalate import ok_check  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_service_escalate import escalations, svc  # noqa: F401
from test_service_split import answer_split, asked, opened
from test_supervisor_split import post, route_split

pytestmark = pytest.mark.usefixtures('job_chains_as_before')


def seed_of(f, key):
    rec = escalations(f)[key]
    path = (rec.get('spec') or {}).get('tree')
    if not path:
        specs = [json.loads(p.read_text()) for p in (f.run_dir / 'subfleet-requests').glob('sf-*.json')
                 if not p.name.endswith('.answer.json')]
        path = next(s['tree'] for s in specs if s['task_id'] == key)
    return decompose.seed_from(path, key, 'child goal', key + '.')


def test_a_p7_worker_split_seeds_the_child_tree(svc, ok_check, decompose_on):
    f = svc
    asked(f)
    key, _ = opened(f)
    answer_split(f, key, 'approve')
    child = seed_of(f, key)
    assert child['accepted'] == 0 and [n['id'] for n in decompose.level_nodes(child)] == [key + '.0', key + '.1']
    assert decompose.level_nodes(child)[0]['goal'] == 'Fetch sources one to three into part-a.md'
    assert f.tree_state() is None  # the service fleet itself keeps no tree


def test_a_supervisor_split_seeds_the_child_tree(svc, ok_check, decompose_on):
    f = svc
    f.cfg['supervisor_split'] = True
    post(f)
    assert route_split(f)['status'] == 'committed'
    key = next(k for k, v in escalations(f).items() if v.get('request') == 'r1')
    child = seed_of(f, key)
    assert child['nodes'][0]['goal'] == 'Run the big job of the payload as a sub-fleet.'
    assert [n['id'] for n in decompose.level_nodes(child)] == [key + '.0', key + '.1']
