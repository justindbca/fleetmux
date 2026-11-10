"""Seam of the advisor phase 2 (roster switch, rescue, one launch record per slot) with the deploy-side
card gate (P2-9) and the night rosters: a slot's launch record changes only when a generation starts.

The advisor process is always a fake runner: no test starts `claude`."""
import json
import shutil
from pathlib import Path

import pytest

from test_advisor import Runner, afx, item, report, send_decision, supervisor_texts  # noqa: F401
from test_advisor_rescue import consult, rescue_decision, respawn_sheet, worked
from test_fleet import decision, fx, turn  # noqa: F401
from cmux_fleet import fleet, night, roster


def fable_wait(f):
    """worker-1 takes the only fable ticket, worker-2's fable sheet waits for it."""
    f.advisor_runner = Runner({'model': 'fable', 'effort': 'high', 'domains': ['research_web']})
    send_decision(f, [item(), item('worker-2', 'Summarize the advisor plan into worker-2/sum.md')])
    f.pump()
    two = f.agent('worker-2')
    assert two['generation'] == '0' and 'max_concurrent 1 for model fable' in two['capacity_wait']
    return two


def test_a_waiting_sheet_is_not_staged_as_the_slot_record(afx):
    f = afx
    before = f.agent('worker-2').get('runtime_launch', '')
    two = fable_wait(f)
    assert json.loads(two['advice_wait'])['sheet']['model'] == 'fable'
    assert two.get('advice_domains', '') == '' and two.get('runtime_launch', '') == before
    assert not two.get('advice_ticket')  # no "[advisor] … respawn" note before the switch really happens
    assert not any('worker-2' in n and 'respawn' in n for n in f.r.lrange(fleet.ADVICE_NOTES, 0, -1))


def test_advisor_off_drops_a_waiting_sheet_and_delivers_on_the_running_runtime(afx):
    f = afx
    before = f.agent('worker-2').get('runtime_launch', '')
    domains = f.agent('worker-2').get('installed_domains', '')
    fable_wait(f)
    assert f.set_advisor(False, 'user') is None
    two = f.agent('worker-2')
    assert two['advice_wait'] == '' and two['capacity_wait'] == '' and two['profile_state'] == ''
    f.pump()
    two = f.agent('worker-2')
    assert two['state'] == 'busy' and two['generation'] == '0' and two.get('runtime_launch', '') == before
    assert 'advisor plan' in two['last_msg'] and two.get('installed_domains', '') == domains  # record unchanged


def test_new_task_clears_a_waiting_sheet(afx):
    f = afx
    fable_wait(f)
    f.setf('worker-2', advice_domains='["research_web"]')
    f.finish('blocked', 'operator stop')
    f.handle({'kind': 'task', 'request_id': 'r2', 'spec': {'goal': 'another task'}})
    two = f.agent('worker-2')
    assert two['advice_wait'] == '' and two['capacity_wait'] == '' and two['advice_domains'] == ''


def test_rescue_respawn_to_a_full_model_does_nothing_and_counts_no_relay(afx):
    f = afx
    worked(f)  # worker-1 ran M0 on sonnet
    f.advisor_runner = Runner({'model': 'fable', 'effort': 'high', 'domains': []})
    send_decision(f, [item('worker-2', 'Summarize the advisor plan into worker-2/sum.md')])
    f.pump()
    assert json.loads(f.agent('worker-2')['runtime_launch'])['profile_id'] == 'claude-fable'  # holds the ticket
    f.advisor_runner = Runner(respawn_sheet(model='fable'))
    f.on_supervisor(rescue_decision(consult()))
    f.pump()
    one = f.agent('worker-1')
    assert one['state'] == 'idle' and one['generation'] == '0' and not one.get('rescue_relay')
    assert not one.get('advice_domains') and not one.get('rescue_launch')
    assert f.r.hget(fleet.RESCUE_RELAYS, f.task()['id'] + '|M0|worker-1') is None
    assert any('[advisor] respawn M0 (worker-1) 未执行' in t and 'max_concurrent 1 for model fable' in t
               for t in supervisor_texts(f))


def test_rescue_reassign_to_a_full_model_keeps_the_preset_for_the_redispatch(afx):
    f = afx
    worked(f)
    f.advisor_runner = Runner({'model': 'fable', 'effort': 'high', 'domains': []})
    send_decision(f, [item('worker-1', 'Fix the notes of worker-1/notes.md')])  # worker-1 -> fable (ticket)
    f.pump()
    f.handle(turn('worker-1', 'READY', 1))
    f.pump()
    report(f, 'worker-1', 1)
    assert json.loads(f.agent('worker-1')['runtime_launch'])['profile_id'] == 'claude-fable'
    f.advisor_runner = Runner({'action': 'reassign', 'to': 'worker-2', 'model': 'fable', 'effort': 'high',
                               'domains': [], 'reason': '换槽'})
    f.on_supervisor(rescue_decision(consult()))
    f.pump()
    two = f.agent('worker-2')
    assert two['generation'] == '0' and two['state'] == 'idle' and not two.get('advice_domains')
    assert json.loads(two['advice_preset'])['sheet']['model'] == 'fable'
    assert any('[advisor] M0 → worker-2' in t for t in supervisor_texts(f))


def test_codex_slot_with_native_subagents_keeps_its_sheet_until_the_switch(fx, tmp_path, monkeypatch):
    """switch_profile defers (no spawn) while a codex slot's native subagents run: the next tick must not
    deliver to the old runtime as if advised; the sheet waits in advice_wait and applies later."""
    from cmux_fleet import agent_registry
    reg = tmp_path / 'agents'
    shutil.copytree(Path(agent_registry.__file__).parent / 'agents', reg)
    (reg / 'codex-terra.json').write_text(json.dumps({
        'id': 'codex-terra', 'display_name': 'Codex Terra', 'cli': 'codex', 'capabilities': ['web-research'],
        'role_file': 'worker.md', 'model': 'gpt-6.2-terra'}))

    class Reg(agent_registry.AgentRegistry):
        def __init__(self, directory=None):
            super().__init__(reg)
    monkeypatch.setattr(fleet, 'AgentRegistry', Reg)
    f = fx
    f.cfg.update(codex_model='gpt-6.1-sol')
    monkeypatch.setattr(f, 'preflight_profile', lambda launch, **kw: None)
    children = {'on': True}
    monkeypatch.setattr(fleet, 'active_children', lambda thread, sessions: children['on'])
    assert f.set_advisor(True, 'user') is None
    f.advisor_runner = Runner({'model': 'gpt-6.2-terra', 'domains': []})
    before = f.agent('worker-1').get('runtime_launch', '')
    f.on_supervisor(decision())
    f.pump()
    f.pump()
    info = f.agent('worker-1')
    assert info['state'] == 'idle' and info['generation'] == '0' and f.r.llen('fx:inbox:worker-1') == 1
    assert json.loads(info['advice_wait'])['sheet']['model'] == 'gpt-6.2-terra'
    assert info.get('runtime_launch', '') == before and not info.get('advice_ticket')
    assert len(f.advisor_runner.calls) == 1
    children['on'] = False
    f.pump()
    info = f.agent('worker-1')
    assert info['generation'] == '1' and info['state'] == 'starting' and info['advice_wait'] == ''
    assert json.loads(info['runtime_launch'])['profile_id'] == 'codex-terra' and info['advice_ticket']
    assert len(f.advisor_runner.calls) == 1  # never re-advised


def test_advisor_switch_writes_day_and_night_rosters_and_they_must_agree(tmp_path):
    day, nite = tmp_path / 'day.yaml', tmp_path / 'night.yaml'
    for p in (day, nite):
        p.write_text('workers:\n  - name: worker-1\n')
    run = tmp_path / 'run'
    run.mkdir()
    cfg = {'run': str(run), 'roster': str(day), 'roster_night': str(nite)}
    assert fleet.roster_file(cfg) == day
    night.write_state(run, True)
    assert fleet.roster_file(cfg) == nite
    assert fleet.advisor_rosters(cfg) == [nite, day]
    for path in fleet.advisor_rosters(cfg):
        roster.write_advisor(path, True)
    assert roster.advisor(day.read_bytes()) and roster.advisor(nite.read_bytes())
    night.check_rosters({}, day, nite)
    roster.write_advisor(nite, False)  # a hand edit of one file only
    with pytest.raises(ValueError, match='same "advisor"'):
        night.check_rosters({}, day, nite)
    assert fleet.roster_file({'run': str(run)}) == run / 'roster.yaml'


def test_start_with_a_roster_already_on_checks_the_kit_before_provisioning(monkeypatch, tmp_path):
    from test_advisor import fake_start
    from test_kit import make_kit
    from cmux_fleet import cmux_manager
    argv = fake_start(monkeypatch, tmp_path)
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: pytest.fail('bad kit must be refused first'))
    kit = make_kit(tmp_path / 'kit')
    (kit / 'domains' / 'fleet' / 'RULES.md').unlink()
    own = tmp_path / 'team.yaml'
    own.write_text('workers:\n  - name: worker-1\nadvisor: on\n')  # online at start without --advisor
    with pytest.raises(SystemExit, match='--worker-kit rejected'):
        fleet.main(argv + ['--roster', str(own), '--worker-kit', str(kit)])
