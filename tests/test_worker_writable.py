"""Roster `writable:` (user 2026-11-03): a worker may be granted extra writable roots beyond its own directory
(a dev worker's repo, an acceptance worker's receipt directory, a test VM). Without the grant its AGENTS.md
forbids every write outside its own directory, which a strict model obeys even against its role text."""
import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx  # noqa: F401  (fixture)
from fleetmux import roster


def write(tmp_path, body):
    path = tmp_path / 'roster.yaml'
    path.write_text(body)
    return path


def test_roster_accepts_a_list_of_writable_roots(tmp_path):
    path = write(tmp_path, 'workers:\n  - {name: worker-1, writable: [/data/receipts, "utm-linux:~/a42/v2"]}\n'
                           '  - {name: worker-2}\n')
    assert roster.load(path)[0] == ['worker-1', 'worker-2']
    assert roster.writable_roots(path.read_bytes()) == {'worker-1': ['/data/receipts', 'utm-linux:~/a42/v2']}


@pytest.mark.parametrize('value', ['/data', '[]', '[""]', '[1]'])
def test_roster_rejects_a_bad_writable_value(tmp_path, value):
    with pytest.raises(ValueError, match='writable'):
        roster.load(write(tmp_path, 'workers:\n  - {name: worker-1, writable: ' + value + '}\n'))


def test_agents_md_names_the_granted_roots_only_for_that_worker(fx, tmp_path):  # noqa: F811
    f = fx
    f.cfg['roster'] = str(write(tmp_path, 'workers:\n  - {name: worker-1, writable: [/data/receipts, '
                                          '"utm-linux:~/a42/v2"]}\n  - {name: worker-2}\n'))
    granted = f.role_scope('worker-1', f.runtime_cfg('worker-1'), {})
    assert 'Do not write outside your own directory' in granted
    assert '`/data/receipts`' in granted and '`utm-linux:~/a42/v2`' in granted
    assert 'granted' in granted and '{{' not in granted
    plain = f.role_scope('worker-2', f.runtime_cfg('worker-2'), {})
    assert '/data/receipts' not in plain and 'granted' not in plain


def test_agents_md_without_a_roster_is_unchanged(fx):  # noqa: F811
    f = fx
    f.cfg.pop('roster', None)
    scope = f.role_scope('worker-1', f.runtime_cfg('worker-1'), {})
    assert 'Do not write outside your own directory' in scope and 'granted' not in scope
