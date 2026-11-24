"""Switch off (docs/design.md §62 "old and new side by side"): a v2 task fleet behaves as before the tree existed:
automatic M<n> ids, the dispatch leaf on fx:items, no fx:tree and no tree.json, the dispatch audit unchanged, the
supervisor's role text unchanged."""
import json

from fleetmux import decompose, fleet
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_v2_codex_claude import answer_turn, mixed, submit  # noqa: F401


def test_off_a_plain_dispatch_runs_as_before(mixed):
    f = mixed
    submit(f)
    answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 5, 'tree': [{'id': 'M0'}],
                                             'dispatch': [{'to': 'worker-1', 'leaf': True,
                                                           'instruction': 'Write worker-1/part.md with the findings.'}]}))
    f.pump()
    assert json.loads(f.r.get(fleet.ITEMS)) == {'M0': {'to': 'worker-1', 'leaf': True,
                                                       'instruction': 'Write worker-1/part.md with the findings.'}}
    assert f.r.get(decompose.TREE) is None and 'decompose' not in f.task()  # the task hash as before
    assert not (f.run_dir / 'tree.json').exists() and not (f.workspace() / 'tree.json').exists()
    event = [a for _, a in f.r.xrange(f.v2.bus.audit_key) if a.get('event') == 'dispatch'][-1]
    assert set(event) == {'event', 'run_id', 'at', 'count', 'completion'}
    assert f.item_leaf('M0') is True


def test_off_the_role_texts_are_the_shipped_files(mixed):
    f = mixed
    sup = (fleet.agent_cwd(f.cfg, 'supervisor') / 'AGENTS.md').read_text()
    assert '### Leaf blocks (`dispatch[].leaf`)' in sup and 'Decomposition tree' not in sup
