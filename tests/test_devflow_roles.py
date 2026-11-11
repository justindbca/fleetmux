"""The devflow role pack (tests first, interface frozen before implementation) stays usable and consistent."""
from pathlib import Path

import pytest

from fleetmux import roster

PACK = Path(__file__).resolve().parents[1] / 'fleetmux' / 'agents' / 'devflow'
# Marker lines the supervisor greps for; the worker role must tell workers to write exactly these.
MARKERS = ['IMPORT_OR_SKIP:', 'ROWS_WITHOUT_WRITER:', 'CONTRACT_TESTS:', '接口缺口', '待用户定']


def test_supervisor_and_worker_share_gate_markers():
    sup = (PACK / 'supervisor.md').read_text()
    wrk = (PACK / 'worker.md').read_text()
    for m in MARKERS:
        assert m in sup, m
        assert m in wrk, m
    assert 'NotImplementedError' in sup and 'NotImplementedError' in wrk
    assert 'importorskip' in sup and 'importorskip' in wrk  # both forbid it


def test_example_roster_parses_and_every_worker_gets_the_devflow_role():
    pytest.importorskip('yaml')  # roster itself requires PyYAML; not a contract test of this pack
    path = PACK / 'roster.example.yaml'
    workers, roles = roster.parse(path.read_text(), path.parent)
    assert workers[0] == 'contract'
    assert len(workers) >= 2
    assert set(roles) == set(workers)
    assert {Path(p).resolve() for p in roles.values()} == {(PACK / 'worker.md').resolve()}


def test_pack_has_no_file_named_after_a_roster_worker():
    # `--roles DIR/<name>.md` would attach a second role to that worker on top of the roster role.
    path = PACK / 'roster.example.yaml'
    names = [line.split(':', 1)[1].strip() for line in path.read_text().splitlines()
             if line.strip().startswith('- name:')]
    assert names
    for n in names:
        assert not (PACK / (n + '.md')).exists(), n
