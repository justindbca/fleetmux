"""用户 10/13：监工 prompt 必须要求尽可能并发派活、减少开发时间（task 模式 v1 / v2 两份）。"""
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[1] / 'cmux_fleet' / 'agents'


def test_task_supervisors_require_parallel_dispatch():
    for name in ('supervisor.md', 'supervisor_v2.md'):
        text = (AGENTS / name).read_text()
        assert 'Parallel by default' in text, name
        assert 'give every idle worker its own piece' in text, name
        assert 'Never give the same file to two workers in one round' in text, name
