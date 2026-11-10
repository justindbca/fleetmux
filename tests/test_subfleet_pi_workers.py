"""§51: a named sub-fleet entry may run its workers on pi (worker_cli: pi + pi_model), and may give each
worker a clean session after every published result (fresh_after_result). Claude stays the default."""
import json
from pathlib import Path

import pytest

from test_runtime_v2_planning import plan, settle  # noqa: F401  (fixture)
from test_fresh_dispatch import worked
from test_fleet import fx  # noqa: F401  (fixture)
from test_subfleet import ask, result, supervisor_answers, svc, write_roster  # noqa: F401  (fixture)
from cmux_fleet import fleet, named_subfleet, roster, runtime_v2, subfleet

PI = '    worker_cli: pi\n    pi_model: workbuddy-ds/workbuddy-ai/deepseek-v4.1-flash-sg\n'


def entries(tmp_path, extra, child='workers:\n  - name: a\n'):
    (tmp_path / 'sup.md').write_text('x')
    (tmp_path / 'child.yaml').write_text(child)
    path = tmp_path / 'r.yaml'
    path.write_text('workers:\n  - name: worker-1\nsubfleets:\n  - name: ops\n    supervisor_slot: worker-1\n'
                    '    supervisor_role: sup.md\n    roster: child.yaml\n' + extra)
    return path


def test_entry_takes_pi_workers_with_their_model_and_the_fresh_switch(tmp_path):
    [e] = roster.load_subfleets(entries(tmp_path, PI + '    pi_thinking: off\n    fresh_after_result: true\n'))
    assert (e['worker_cli'], e['pi_model'], e['pi_thinking'], e['fresh_after_result']) == (
        'pi', 'workbuddy-ds/workbuddy-ai/deepseek-v4.1-flash-sg', 'off', True)
    [plain] = roster.load_subfleets(entries(tmp_path, ''))
    assert not {'worker_cli', 'pi_model', 'pi_thinking', 'fresh_after_result'} & set(plain)


@pytest.mark.parametrize('extra, want', [
    ('    worker_cli: pi\n', 'worker_cli: pi needs pi_model'),
    ('    worker_cli: opencode\n', 'worker_cli must be one of claude, pi'),
    ('    pi_model: a/b\n', 'pi_model / pi_thinking need worker_cli: pi'),
    ('    worker_cli: pi\n    pi_model: nomodel\n', 'pi_model must be a pi provider/model string'),
    (PI + '    pi_thinking: loud\n', 'pi_thinking must be one of'),
    ('    fresh_after_result: 1\n', 'fresh_after_result must be true or false'),
])
def test_bad_pi_entries_reject_the_roster(tmp_path, extra, want):
    with pytest.raises(ValueError, match=want):
        roster.load_entries(entries(tmp_path, extra))


def test_pi_entry_rows_cannot_switch_to_a_card(tmp_path):
    """A profile card would move a slot off pi (a mixed-CLI v2 run): refused at roster load."""
    with pytest.raises(ValueError, match='cannot set profile'):
        roster.load_entries(entries(tmp_path, PI, child='workers:\n  - {name: a, profile: claude-opus}\n'))


def test_child_worker_cfg_runs_the_entrys_cli_never_the_parents():
    parent = dict(fleet.DEFAULTS, worker_cli='pi', pi_model='p/parent', pi_models={'x': 'p/x'}, pi_home='/parent')
    cfg = subfleet.child_worker_cfg(parent, {'name': 'ops'})
    assert cfg['worker_cli'] == 'claude' and 'pi_model' not in cfg and 'pi_home' not in cfg and 'pi_models' not in cfg
    cfg = subfleet.child_worker_cfg(dict(fleet.DEFAULTS, worker_cli='claude'),
                                    {'name': 'ops', 'worker_cli': 'pi', 'pi_model': 'p/m', 'fresh_after_result': True})
    assert (cfg['worker_cli'], cfg['pi_model'], cfg['fresh_after_result']) == ('pi', 'p/m', True)


def pi_parent(t, allow=True):
    agent = t.tmp / 'pi-agent'
    agent.mkdir()
    (agent / 'models.json').write_text('{"providers": {}}')
    (agent / 'auth.json').write_text('{}')
    binary = t.tmp / 'pi'
    binary.write_text('#!/bin/sh\necho 1.2.0\n')
    binary.chmod(0o755)
    t.f.cfg.update(pi=str(binary), pi_agent_dir=str(agent), allow_unsandboxed_pi=allow)


def test_named_pi_child_starts_with_pi_workers_and_its_own_pi_home(svc):
    t, f = svc, svc.f
    pi_parent(t)
    f.cfg['roster'] = str(write_roster(t, extra=PI + '    fresh_after_result: true\n'))
    f.roster_tick()
    rid, answer = ask(t, 'up')
    assert answer['status'] == 'accepted', answer
    supervisor_answers(t, rid)
    subfleet.tick(f)
    assert named_subfleet.state(f, 'ops')['state'] == 'starting'
    rec = subfleet.record(f.run_dir / 'sub' / 'ops')
    cfg = fleet.load(Path(rec['run']) / 'config.json')
    assert (cfg['worker_cli'], cfg['pi_model'], cfg['fresh_after_result']) == (
        'pi', 'workbuddy-ds/workbuddy-ai/deepseek-v4.1-flash-sg', True)
    assert cfg['supervisor_cli'] == 'claude'  # the adopted parent pane
    home = Path(cfg['pi_home'])
    assert home == Path(rec['run']) / 'pi-home' and (home / 'models.json').exists()
    assert runtime_v2.validate_config(cfg) == 2


def test_named_pi_child_without_the_unsandboxed_pi_switch_is_rejected_by_program(svc):
    t, f = svc, svc.f
    pi_parent(t, allow=False)
    f.cfg['roster'] = str(write_roster(t, extra=PI))
    f.roster_tick()
    rid, answer = ask(t, 'up')
    assert answer['status'] == 'accepted', answer
    res = result(f, rid)
    assert res['status'] == 'rejected' and '--allow-unsandboxed-pi' in res['reason'], res
    assert not subfleet.active_children(f.run_dir)


@pytest.mark.parametrize('on', [True, False])
def test_fresh_after_result_marks_the_worker_for_a_clean_next_session(plan, on):  # noqa: F811
    f, v, r, env = plan
    f.cfg['fresh_after_result'] = on
    settle(plan)
    assert json.loads(f.r.get(fleet.RESULT + 'r1'))['status'] == 'ok'
    assert (f.agent('worker-1').get('fresh_pending') == '1') is on
    assert (f.agent('worker-1').get('result_fresh') == '1') is on


def test_a_result_fresh_mark_rotates_the_idle_worker_instead_of_being_dropped(fx):  # noqa: F811
    """Studio 11-10: the idle pump took the publish mark for a cancelled `fresh` dispatch and cleared it, so
    mem-1 answered its second request in the same session. A result mark rotates the idle slot at once."""
    worked(fx)
    old = fx.agent('worker-1')['pane']
    fx.setf('worker-1', fresh_pending=1, result_fresh=1)
    fx.pump()
    info = fx.agent('worker-1')
    assert info['generation'] == '1' and info['label'] == 'worker-1.1' and info['state'] == 'starting'
    assert info.get('fresh_pending') == '0' and info.get('result_fresh') == '0'
    assert old in fx.mgr.killed
