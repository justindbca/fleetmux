"""Worker and contract role texts with the tree on (docs/design.md §62): a split request's JSON asks for delta_t /
delta_c (worker.md, contracts_worker.md); the contract supervisor's dispatch[].leaf line says the tree judges leaf.
Off: the shipped texts, word for word."""
import pytest

from fleetmux import decompose, fleet
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_v2_codex_claude import mixed  # noqa: F401

AGENTS = fleet.HERE / 'agents'
DELTA = '"delta_t": <minutes the whole task finishes earlier>, "delta_c": <extra session minutes'


@pytest.mark.parametrize('name', ['worker.md', 'contracts_worker.md'])
def test_the_split_request_json_asks_for_both_accounts(name):
    shipped = (AGENTS / name).read_text()
    text = decompose.worker_swap(shipped)
    assert DELTA in text and '"workers": 2}' not in text
    assert text.count(DELTA) == shipped.count('"workers": 2}') == 1


def test_the_contract_supervisor_leaf_line_points_at_the_tree():
    text = decompose.contracts_swap((AGENTS / 'contracts_supervisor.md').read_text())
    assert '"leaf": true` on an item' not in text and '`dispatch[].leaf`: not used while the decomposition tree' in text


def test_on_a_task_worker_gets_the_accounts_in_its_role_text(mixed, decompose_on):
    f = mixed
    assert DELTA in f.role_scope('worker-1', f.runtime_cfg('worker-1'), {})


def test_off_the_worker_role_text_keeps_the_shipped_line(mixed):
    f = mixed
    text = f.role_scope('worker-1', f.runtime_cfg('worker-1'), {})
    line = next(x for x in (AGENTS / 'worker.md').read_text().splitlines() if x.startswith('| Your task is too big'))
    assert line in text and DELTA not in text
