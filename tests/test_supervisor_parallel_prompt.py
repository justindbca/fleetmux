"""Supervisor prompts must ask for as much parallel dispatch as possible to cut development time (task mode v1 / v2)."""
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[1] / 'fleetmux' / 'agents'


def test_task_supervisors_require_parallel_dispatch():
    for name in ('supervisor.md', 'supervisor_v2.md'):
        text = (AGENTS / name).read_text()
        assert 'Parallel by default' in text, name
        assert 'give every idle worker its own piece' in text, name
        assert 'Never give the same file to two workers in one round' in text, name
