"""dsh bridge plugin (cmux_fleet/dsh_bridge.mjs, design §41 v1) against a fake dsh-TUI cordis ctx and a fake
agent_bridge.py (tests/js): followup / claimed correlation, turn ends, fleet_result, human turns, approvals,
rebinding, the startup retry. Not a TUI or model test; the real smoke is scripts/smoke_pair.sh pi dsh task."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

HARNESS = Path(__file__).with_name('js') / 'dsh_plugin_harness.mjs'
PLUGIN = Path(__file__).resolve().parents[1] / 'cmux_fleet' / 'dsh_bridge.mjs'


def harness(scenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('node is not installed')
    out = subprocess.run([node, str(HARNESS), str(PLUGIN), scenario], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip(), out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def of(out, kind):
    return [f for f in out['frames'] if f['kind'] == kind]


def test_startup_delivery_and_fleet_result_settle_like_pi():
    out = harness('happy')
    (init,) = of(out, 'init')
    assert init['thread'] == '2d746058-3749-4cbc-8426-01643fe1a614' and init['transcript'].endswith(init['thread'] + '.jsonl')
    assert out['startup_text'] == '⏵ 启动检查\nREADY' and out['transcript'] is True
    assert out['delivered_text'] == 'do m1' and len(of(out, 'started')) == 1
    assert out['concluded'] is True and out['tool'].startswith('候选已记录')
    (settled,) = of(out, 'settled')
    assert settled['candidates'] == [{'tool': 'fleet_result', 'value': {'done': ['wrote ok.txt'], 'evidence': ['ok.txt 2 bytes'],
                                                                        'not_done': 'none', 'blocked': 'none'}}]
    assert (settled['failed'], settled['aborted'], settled['ctx_used'], settled['ctx_window']) == (False, False, 1234, 400000)
    assert out['bridge_has_key'] is False  # the API key is DSH's alone


def test_a_late_commit_reports_idle_again():
    """design §47: settled answered late (busy daemon) is no fault; the late completed frame reopens the slot."""
    out = harness('late_settle')
    assert len(of(out, 'settled')) == 1 and not of(out, 'hold')
    assert out['idle_after'] == out['idle_before'] + 1


def test_a_human_turn_never_settles_and_refuses_a_delivery_meanwhile():
    out = harness('manual')
    idle = [f['idle'] for f in of(out, 'availability')]
    assert idle[:2] == [True, False] and idle[-1] is True
    assert len(of(out, 'reject_delivery')) == 1 and not of(out, 'settled')
    assert 'No active fleet delivery' in out['tool']


def test_a_turn_mixing_the_delivery_with_human_input_is_held():
    out = harness('mixed')
    (hold,) = of(out, 'hold')
    assert 'mixed fleet m3 with other input ["human-2"]' in hold['reason'] and not of(out, 'settled')


@pytest.mark.parametrize('scenario, failed, aborted', [('aborted', False, True), ('error', True, False)])
def test_turn_end_reasons(scenario, failed, aborted):
    out = harness(scenario)
    (settled,) = of(out, 'settled')
    assert (settled['failed'], settled['aborted'], settled['candidates']) == (failed, aborted, [])
    errors = [f['candidate'] for f in of(out, 'candidate')]
    assert (errors[0]['tool'] == 'model' and 'upstream 429' in errors[0]['error']) if failed else errors == []


def test_another_model_fails_the_delivery_by_the_model_lock():
    out = harness('model_lock')
    assert of(out, 'candidate')[0]['candidate']['error'] == 'model_lock: expected wb/deep-1, got other/m'
    assert of(out, 'settled')[0]['failed'] is True


def test_a_handoff_turn_takes_no_fleet_result():
    out = harness('handoff')
    assert 'No active fleet delivery' in out['tool'] and of(out, 'settled')[0]['candidates'] == []


def test_a_delete_approval_is_the_users_and_a_fleet_ruling_is_refused():
    out = harness('approval_delete')
    (perm,) = of(out, 'permission')
    assert perm['class'] == 'delete' and perm['request']['resources'] == ['rm -rf /outside/build']
    (refused,) = of(out, 'permission_refused')
    assert refused['held_for_user'] is True
    (resolved,) = of(out, 'permission_resolved')
    assert (resolved['reply'], resolved['decided_by'], out['answer']) == ('reject', 'tui_user', 'rejected')


def test_another_approval_takes_the_supervisors_ruling():
    out = harness('approval_other')
    (perm,) = of(out, 'permission')
    assert perm['class'] == 'other' and perm['request'] == {
        'id': 'call_1', 'sessionID': '2d746058-3749-4cbc-8426-01643fe1a614', 'action': 'write',
        'resources': ['/outside/x.txt'], 'message': 'escalate', 'metadata': {'file_path': '/outside/x.txt', 'content': 'x'}}
    (resolved,) = of(out, 'permission_resolved')
    assert (resolved['reply'], resolved['decided_by'], out['answer']) == ('once', 'fleet', 'allowed-once')


def test_a_new_root_agent_retires_the_bridge_instead_of_rebinding():
    out = harness('rebind')
    assert of(out, 'shutdown') and len(of(out, 'init')) == 1


def test_after_a_rejected_startup_the_typed_nudge_turn_is_the_startup_turn():
    out = harness('startup_retry')
    assert [f['text'] for f in of(out, 'startup')] == ['⏵ 启动检查\nREADY', 'READY']
    assert of(out, 'availability')[-1]['idle'] is True
