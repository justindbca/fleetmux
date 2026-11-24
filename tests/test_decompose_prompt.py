"""The supervisor's role text with the decomposition tree on (docs/design.md §62): the "Leaf blocks" section and the
dispatch[].id / dispatch[].leaf format lines are replaced by the tree section; off, the shipped text stays; the Pi
fleet_decision schema carries tree / tree_drop / cause / decompose_tune."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from fleetmux import decompose, fleet
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_pi_v2_contract import PI_ROOT
from test_v2_codex_claude import mixed  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('name', ['supervisor.md', 'supervisor_v2.md'])
def test_the_tree_section_replaces_leaf_blocks_in_both_supervisor_files(name):
    text = decompose.prompt_swap((fleet.HERE / 'agents' / name).read_text())
    assert '### Decomposition tree (`tree`)' in text and '### Leaf blocks' not in text
    assert 'Add `"leaf": true` to a dispatch item' not in text and '`dispatch[].leaf`' not in text
    assert '- `dispatch[].id`: required: the id of a block of the tree' in text
    assert 'decompose_tune' in text and 'read `review` in tree.json' in text
    assert 'first reply: the tree only' in text.lower()
    assert '### Cross-session collisions (the session board)' in text  # the next section is kept


def test_on_the_task_supervisor_gets_the_tree_section_and_workers_do_not(mixed, decompose_on):
    f = mixed
    sup = f.role_scope('supervisor', f.runtime_cfg('supervisor'), {})
    assert '### Decomposition tree (`tree`)' in sup and '### Leaf blocks' not in sup
    assert 'Decomposition tree' not in f.role_scope('worker-1', f.runtime_cfg('worker-1'), {})


def test_off_the_role_text_is_the_shipped_file(mixed):
    f = mixed
    sup = f.role_scope('supervisor', f.runtime_cfg('supervisor'), {})
    assert '### Leaf blocks (`dispatch[].leaf`)' in sup and 'Decomposition tree' not in sup


def test_the_pi_schema_source_declares_the_tree_keys():
    src = (ROOT / 'fleetmux/pi_extension_v2.ts').read_text()
    for key in ('tree: Type.Optional(Type.Array(', 'tree_drop: Type.Optional(Type.Array(Type.String()))',
                'decompose_tune: Type.Optional(Type.Array(', 'cause: Type.Optional(Type.Union(['):
        assert key in src, key


def test_the_pi_schema_takes_the_tree_keys(tmp_path):
    node = shutil.which('node')
    if not node or not (PI_ROOT / 'node_modules/typebox').exists():
        pytest.skip('installed Pi/Node dependencies needed for actual TS contract harness')
    shutil.copy(ROOT / 'fleetmux/pi_extension_v2.ts', tmp_path / 'extension.ts')
    shutil.copy(ROOT / 'tests/test_decompose_pi_schema_v2.mjs', tmp_path / 'harness.mjs')
    (tmp_path / 'node_modules').symlink_to(PI_ROOT / 'node_modules', target_is_directory=True)
    env = dict(os.environ, FLEETMUX_PI_SLOT='supervisor', FLEETMUX_PI_MODE='task', FLEETMUX_PI_BRIDGE='["x","supervisor","0"]')
    result = subprocess.run([node, '--experimental-strip-types', str(tmp_path / 'harness.mjs'), str(tmp_path / 'extension.ts')],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'decompose schema contract passed' in result.stdout


def test_the_tree_section_covers_the_whole_round():
    text = (ROOT / 'fleetmux/agents/decompose_supervisor.md').read_text()
    for needle in ('How to cut, in this order', 'The two accounts', 'closed',  # plan 2.4, 2.5 (both conditions)
                   'goes to the user as a summary',  # the opening summary
                   '`tree_drop`', '`cause`', 'returns_alert',  # tree keys and what a cause counts
                   '[overrun]', 'Re-estimate', 'merge', 'extract', 'split',  # what to do on the overrun note
                   'read `review` in tree.json', 'parallel fill', 'only when the COMPLETED passes verification'):
        assert needle in text, needle
    assert text.isascii()


def test_the_pi_schema_and_python_take_the_same_tune_params_and_nulls():
    src = (ROOT / 'fleetmux/pi_extension_v2.ts').read_text()
    tune = src[src.index('decompose_tune: Type.Optional('):]
    tune = tune[:tune.index('reason: Type.String')]
    assert all('Type.Literal("' + p + '")' in tune for p in decompose.TUNE_PARAMS), tune
    tree_entry = src[src.index('const treeEntry'):src.index('decision: Type.Optional(Type.Object')]
    for key in ('serial_reason', 'split_from'):  # Python takes null for both
        part = tree_entry[tree_entry.index(key + ':'):]
        assert 'Type.Null()' in part[:part.index('\n')], key
