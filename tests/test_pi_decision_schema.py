"""Pi supervisor (task mode) fleet_decision schema accepts dispatch[].id, advise_note, advisor on/off, consult[] and withdraw[]."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_pi_v2_contract import PI_ROOT


def test_pi_supervisor_schema_accepts_id_and_advise_note(tmp_path):
    node = shutil.which('node')
    if not node or not (PI_ROOT / 'node_modules/typebox').exists():
        pytest.skip('installed Pi/Node dependencies needed for actual TS contract harness')
    root = Path(__file__).resolve().parents[1]
    extension = tmp_path / 'extension.ts'
    shutil.copy(root / 'fleetmux/pi_extension_v2.ts', extension)
    harness = tmp_path / 'harness.mjs'
    shutil.copy(root / 'tests/test_pi_decision_schema_v2.mjs', harness)
    (tmp_path / 'node_modules').symlink_to(PI_ROOT / 'node_modules', target_is_directory=True)
    env = dict(os.environ, FLEETMUX_PI_SLOT='supervisor', FLEETMUX_PI_MODE='task',
               FLEETMUX_PI_BRIDGE='["x","supervisor","0"]')
    result = subprocess.run([node, '--experimental-strip-types', str(harness), str(extension)],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'decision schema contract passed' in result.stdout


def test_schema_source_declares_optional_keys():
    src = (Path(__file__).resolve().parents[1] / 'fleetmux/pi_extension_v2.ts').read_text()
    assert 'id: Type.Optional(Type.String()), advise_note: Type.Optional(Type.String())' in src
    assert 'advise_note: Type.Optional(Type.String()), fresh: Type.Optional(Type.Boolean())' in src
    assert 'advisor: Type.Optional(Type.Union([Type.Literal("on"), Type.Literal("off")]))' in src
    assert 'consult: Type.Optional(Type.Array(consult))' in src
    assert 'finding: Type.String({ minLength: 1 })' in src
    assert 'escalations: Type.Optional(Type.Array(escalation))' in src
    # §44: an approve may pick the models of the split (supervisor_profile / worker_profiles)
    assert 'supervisor_profile: Type.Optional(Type.String()), worker_profiles: Type.Optional(Type.Array(Type.String()))' in src
    # §46: an approve of a sub-fleet report may narrow the paths granted (a pi top supervisor answers reports)
    assert 'paths: Type.Optional(Type.Array(Type.String({ minLength: 1 })))' in src
    # M15: the task-mode schema carries the declared dependency wait (never the service schema)
    assert 'waiting: Type.Optional(Type.Object(' in src
    assert 'for_hours: Type.Optional(Type.Number({ exclusiveMinimum: 0, maximum: 12 }))' in src


def test_pi_service_supervisor_schema_accepts_escalations(tmp_path):
    node = shutil.which('node')
    if not node or not (PI_ROOT / 'node_modules/typebox').exists():
        pytest.skip('installed Pi/Node dependencies needed for actual TS contract harness')
    root = Path(__file__).resolve().parents[1]
    extension = tmp_path / 'extension.ts'
    shutil.copy(root / 'fleetmux/pi_extension_v2.ts', extension)
    harness = tmp_path / 'harness.mjs'
    shutil.copy(root / 'tests/test_pi_service_schema_v2.mjs', harness)
    (tmp_path / 'node_modules').symlink_to(PI_ROOT / 'node_modules', target_is_directory=True)
    env = dict(os.environ, FLEETMUX_PI_SLOT='supervisor', FLEETMUX_PI_MODE='service',
               FLEETMUX_PI_BRIDGE='["x","supervisor","0"]')
    result = subprocess.run([node, '--experimental-strip-types', str(harness), str(extension)],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'service decision schema contract passed' in result.stdout


def test_pi_service_supervisor_schema_accepts_the_subfleet_answer(tmp_path):
    """design §29: a pi service supervisor answers an external sub-fleet up / down via fleet_decision."""
    node = shutil.which('node')
    if not node or not (PI_ROOT / 'node_modules/typebox').exists():
        pytest.skip('installed Pi/Node dependencies needed for actual TS contract harness')
    root = Path(__file__).resolve().parents[1]
    extension = tmp_path / 'extension.ts'
    shutil.copy(root / 'fleetmux/pi_extension_v2.ts', extension)
    harness = tmp_path / 'harness.mjs'
    shutil.copy(root / 'tests/test_pi_decision_schema_service_v2.mjs', harness)
    (tmp_path / 'node_modules').symlink_to(PI_ROOT / 'node_modules', target_is_directory=True)
    env = dict(os.environ, FLEETMUX_PI_SLOT='supervisor', FLEETMUX_PI_MODE='service',
               FLEETMUX_PI_BRIDGE='["x","supervisor","0"]')
    result = subprocess.run([node, '--experimental-strip-types', str(harness), str(extension)],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'service decision schema contract passed' in result.stdout


def test_service_schema_source_declares_escalations_and_the_blocked_answer():
    src = (Path(__file__).resolve().parents[1] / 'fleetmux/pi_extension_v2.ts').read_text()
    assert 'skip: Type.Optional(Type.Array(Type.Object({ reason: Type.String() }))),\n    escalations: Type.Optional(Type.Array(escalation)),' in src
    assert 'if (!supervisor && active.body.reply && !blockedOnly)' in src


def test_service_schema_source_declares_the_subfleet_answer():
    src = (Path(__file__).resolve().parents[1] / 'fleetmux/pi_extension_v2.ts').read_text()
    assert 'subfleet: Type.Optional(Type.Object({ id: Type.Optional(Type.String()),' in src
    assert 'decision: Type.Union([Type.Literal("approve"), Type.Literal("reject")]), note: Type.Optional(Type.String()) }))' in src


def test_python_service_decision_takes_the_same_subfleet_answer():
    """The daemon-side check matches the schema: approve / reject only, note text, id bound before parsing."""
    from fleetmux import fleet
    ok = fleet.parse_service_decision(json.dumps({'status': 'RUNNING', 'dispatch': [], 'skip': [],
                                                  'subfleet': {'id': 'op-1', 'decision': 'reject'}}), ['worker-1'])
    assert ok['subfleet']['decision'] == 'reject'
    for bad in ({'id': 'op-1', 'decision': 'maybe'}, {'id': 'op-1'}, {'id': 'op-1', 'decision': 'approve', 'note': 3},
                {'decision': 'approve'}, 'approve'):
        with pytest.raises(ValueError):
            fleet.parse_service_decision(json.dumps({'status': 'RUNNING', 'dispatch': [], 'skip': [],
                                                     'subfleet': bad}), ['worker-1'])
