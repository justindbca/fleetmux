"""max_concurrent / hosts on the real dispatch path (model_usage; docs/design.md §12 模型特点卡)."""
import json

import pytest

from cmux_fleet import fleet, model_usage, profile_launch
from cmux_fleet.agent_registry import AgentRegistry
from cmux_fleet.assignment import assign_profiles
from cmux_fleet.planner import build_dispatch_plan
from cmux_fleet.router import route_task
from test_fleet import fx  # noqa: F401  (fixture)
from test_profile_integration import dispatch
from test_profile_live import live  # noqa: F401  (fixture)

REG = AgentRegistry()


def launch(profile):
    return profile_launch.encode_launch({'profile_id': profile, 'overrides': {}, 'role_text': ''})


def agents(**slots):
    """slot=profile id ('' = fleet default) or ('profile', state)."""
    out = {}
    for slot, spec in slots.items():
        profile, state = spec if isinstance(spec, tuple) else (spec, 'idle')
        out[slot.replace('_', '-')] = {'state': state, 'runtime_launch': launch(profile) if profile else ''}
    return out


PI = {'worker_cli': 'pi', 'pi_model': REG.model('flashnext').model}
CLAUDE = {'worker_cli': 'claude', 'worker_model': 'opus'}


def test_fleet_default_maps_only_on_an_exact_cli_and_model_match():
    assert model_usage.default_model(PI, REG) == 'flashnext'
    assert model_usage.default_model(CLAUDE, REG) == 'opus'
    assert model_usage.default_model({'worker_cli': 'pi', 'pi_model': 'local/default'}, REG) is None
    assert model_usage.default_model({'worker_cli': 'codex', 'profile': 'fleet-astra'}, REG) is None  # not guessed
    assert model_usage.default_model({'worker_cli': 'claude'}, REG) is None


def test_same_model_under_different_profiles_counts_as_one_model():
    usage = model_usage.Usage.from_agents(agents(w1='claude-coder', w2='claude-reviewer', w3=''), CLAUDE, REG)
    assert usage.counts() == {'opus': 3}
    assert usage.slots['w1'] == ('claude-coder', 'opus') and usage.slots['w3'] == (None, 'opus')


def test_one_slot_counts_once_and_dead_slots_hold_nothing():
    usage = model_usage.Usage.from_agents(
        agents(w1='flashnext-coder', w2=('flashnext-coder', 'dead'), w3=('flashnext-coder', 'starting')), PI, REG)
    assert usage.holders('flashnext') == ['w1', 'w3']  # a starting successor already holds its model
    card = REG.get('flashnext-coder')
    assert usage.admit('w1', card) is None  # same-slot reuse / next generation of a holder: free
    assert usage.admit('w3', card) is None
    usage = model_usage.Usage.from_agents(agents(w1='flashnext-coder', w2='flashnext-coder', w3='claude-coder'), PI, REG)
    assert 'max_concurrent 2 for model flashnext reached (running on w1, w2)' == usage.admit('w3', card)


def test_unknown_or_unmapped_runtimes_are_not_counted():
    usage = model_usage.Usage.from_agents(
        agents(w1='', w2='researcher', w3='gone-card'), {'worker_cli': 'pi', 'pi_model': 'local/x'}, REG)
    assert usage.slots == {'w1': (None, None), 'w2': ('researcher', 'researcher'), 'w3': ('gone-card', None)}
    assert usage.counts() == {'researcher': 1}


def test_planning_occupancy_frees_a_model_already_running_on_a_reusable_slot():
    usage = model_usage.Usage.from_agents(agents(w1='flashnext-coder', w2='flashnext-coder', w3=''), CLAUDE, REG)
    assert usage.counts() == {'flashnext': 2, 'opus': 1}
    assert usage.for_planning(['w1', 'w2', 'w3']) == {'flashnext': 1, 'opus': 0}
    # Only a retiring slot runs it: nothing to reuse, the model is full for this plan.
    assert usage.for_planning(['w3']) == {'flashnext': 2, 'opus': 0}
    assert route_task('implement it', REG, required_capability='code-edit',
                      occupancy=usage.for_planning(['w3'])).agent_id == 'claude-coder'
    with pytest.raises(ValueError, match=r'flashnext-coder \(max_concurrent 2 reached for model flashnext\)'):
        route_task('implement it', REG, required_capability='code-edit', occupancy=usage.for_planning(['w3']),
                   role='material')  # opus does not list "material": nothing left, every reason is named
    assert route_task('implement it', REG, required_capability='code-edit',
                      occupancy=usage.for_planning(['w1', 'w3'])).agent_id == 'flashnext-coder'


def test_assignment_prefers_the_slot_already_running_the_model():
    plan = build_dispatch_plan('implement dynamic router', REG)
    assert [s.agent_id for s in plan.subtasks] == ['flashnext-coder', 'claude-reviewer']
    plain = assign_profiles(plan, ['w1', 'w2', 'w3'], REG)['assignments']
    assert [a['worker'] for a in plain] == ['w1', 'w2']
    reused = assign_profiles(plan, ['w1', 'w2', 'w3'], REG,
                             running={'w1': 'opus', 'w2': None, 'w3': 'flashnext'})['assignments']
    assert [(a['agent_id'], a['worker']) for a in reused] == [('flashnext-coder', 'w3'), ('claude-reviewer', 'w1')]


def test_planner_passes_each_step_role_to_the_router(tmp_path):
    # A card that denies "reviewer" is never picked for the review step, even when it ranks first.
    for card in REG:
        data = card.to_dict()
        data.pop('model_card')  # self-contained copies (no models/ dir here)
        if card.id == 'claude-reviewer':
            continue
        if card.id == 'flashnext-coder':
            data.update(capabilities=['code-edit', 'test-fix', 'planning', 'code-review'], risk_rank=0,
                        roles_denied=['reviewer'])
        (tmp_path / (card.id + '.json')).write_text(json.dumps({k: v for k, v in data.items() if v is not None}))
    (tmp_path / 'worker.md').write_text('x')
    (tmp_path / 'other-reviewer.json').write_text(json.dumps({
        'id': 'other-reviewer', 'display_name': 'R', 'cli': 'claude', 'capabilities': ['code-review'],
        'risk_rank': 5}))
    registry = AgentRegistry(tmp_path)
    plan = build_dispatch_plan('implement dynamic router', registry)
    assert [s.agent_id for s in plan.subtasks] == ['flashnext-coder', 'other-reviewer']


def test_roster_check_counts_slots_per_model_and_checks_hosts():
    cfg = dict(CLAUDE)
    assert model_usage.check_slots(cfg, ['a', 'b', 'c'], {'a': 'flashnext-coder', 'b': 'flashnext-coder'}, REG) == []
    errors = model_usage.check_slots(cfg, ['a', 'b', 'c'], {s: 'flashnext-coder' for s in 'abc'}, REG)
    assert errors == ['model flashnext: 3 slots (a, b, c) exceed max_concurrent 2']
    errors = model_usage.check_slots(dict(cfg, host='macmini'), ['a', 'b'], {'a': 'flashnext-coder', 'b': 'nope'}, REG)
    assert errors == ['a: profile flashnext-coder runs only on studio, this fleet is on macmini',
                      'b: unknown agent profile nope']
    # Fleet-default slots of a capped model count toward the cap of the card-carrying slots.
    assert model_usage.check_slots(PI, ['a', 'b', 'c'], {'a': 'flashnext-coder'}, REG) == [
        'model flashnext: 3 slots (a, b, c) exceed max_concurrent 2']


def test_over_cap_reports_a_default_fleet_above_the_model_ticket_count():
    usage = model_usage.Usage.from_agents(agents(w1='', w2='', w3=''), PI, REG)
    assert model_usage.over_cap(usage, REG) == ['model flashnext runs on 3 slots (w1, w2, w3), max_concurrent 2']
    assert model_usage.over_cap(model_usage.Usage.from_agents(agents(w1='', w2=''), PI, REG), REG) == []


def logs(f, kind):
    return [e for e in map(json.loads, f.r.lrange(fleet.LOG, 0, -1)) if e['kind'] == kind]


def add_slots(f, **slots):
    for slot, profile in slots.items():
        slot = slot.replace('_', '-')
        f.workers.append(slot)
        f.desired.append(slot)
        f.setf(slot, state='idle', runtime_launch=launch(profile) if profile else '', generation=0, label=slot)


def test_live_switch_waits_while_the_model_is_full_then_switches(live):
    f = live
    add_slots(f, worker_2='flashnext-coder', worker_3='flashnext-coder')
    dispatch(f)
    f.deliver('worker-1')
    info = f.agent('worker-1')
    assert info['state'] == 'idle' and info['generation'] == '0'  # no successor spawned
    assert info['profile_state'] == 'pending' and 'max_concurrent 2 for model flashnext' in info['profile_reason']
    assert f.r.llen('fx:inbox:worker-1') == 1 and f.task()['status'] == 'running'
    f.deliver('worker-1')
    assert len(logs(f, 'profile.capacity_wait')) == 1  # logged once while the reason is unchanged
    f.setf('worker-3', state='dead')  # a ticket frees up
    f.deliver('worker-1')
    info = f.agent('worker-1')
    assert info['state'] == 'starting' and info['runtime_profile'] == 'flashnext-coder'
    assert info['generation'] == '1' and info['capacity_wait'] == ''


def test_two_switches_in_one_tick_see_each_other(live):
    f = live
    add_slots(f, worker_2='', worker_3='flashnext-coder')
    dispatch(f)
    f.deliver('worker-1')  # takes the second flashnext ticket; its runtime_launch is set before spawn
    assert f.agent('worker-1')['runtime_profile'] == 'flashnext-coder'
    # Any other slot deciding in the same tick already sees worker-1 holding the model.
    assert f.card_gate('worker-2', REG.get('flashnext-coder'), REG) == (
        'wait', 'max_concurrent 2 for model flashnext reached (running on worker-1, worker-3)')
    assert f.card_gate('worker-1', REG.get('flashnext-coder'), REG) is None  # its own next generation


def test_live_switch_to_a_card_for_another_host_blocks_the_task(live):
    f = live
    f.cfg['host'] = 'macbook'
    dispatch(f)
    f.deliver('worker-1')
    assert f.task()['status'] == 'blocked'
    assert 'runs only on studio, this fleet is on macbook' in f.task()['reason']
    assert f.agent('worker-1')['generation'] == '0'


def test_submit_plans_against_current_occupancy(fx):
    f = fx
    f.finish('cancelled', 'replace fixture task')
    add_slots(f, worker_2='flashnext-coder', worker_3='flashnext-coder')
    f.desired.remove('worker-2')
    f.desired.remove('worker-3')  # both FlashNext slots are retiring: none can be reused
    f.submit({'goal': 'implement dynamic router', 'auto_agents': True})
    plan = json.loads(f.task()['dispatch_plan'])
    assert [s['agent_id'] for s in plan['subtasks']] == ['claude-coder', 'claude-reviewer']
    f.finish('cancelled', 'again')
    f.desired.append('worker-3')  # worker-3 can be reused: FlashNext stays eligible and takes it
    f.submit({'goal': 'implement dynamic router', 'auto_agents': True})
    task = f.task()
    assert [s['agent_id'] for s in json.loads(task['dispatch_plan'])['subtasks']][0] == 'flashnext-coder'
    first = json.loads(task['profile_assignments'])['assignments'][0]
    assert (first['agent_id'], first['worker']) == ('flashnext-coder', 'worker-3')


# -- capacity boundaries: failed successor, old generation still alive, fleet default

def test_an_old_generation_holds_its_model_until_its_tab_is_gone():
    # worker-1 switches flashnext -> claude-coder; its old FlashNext tab is not confirmed gone yet.
    slots = agents(w1=('claude-coder', 'starting'), w2='flashnext-coder', w3='')
    slots['w1'].update(retired_pane='old-1', retired_launch=launch('flashnext-coder'))
    usage = model_usage.Usage.from_agents(slots, CLAUDE, REG)
    assert usage.holders('flashnext') == ['w1', 'w2'] and usage.holders('opus') == ['w1', 'w3']
    assert usage.counts() == {'flashnext': 2, 'opus': 2}
    assert 'running on w1, w2' in usage.admit('w3', REG.get('flashnext-coder'))  # no early release
    assert usage.for_planning(['w1']) == {'flashnext': 2, 'opus': 2}  # a slot mid-switch is not reusable
    # Once cmux says the old tab is gone, the ticket is free.
    usage = model_usage.Usage.from_agents(slots, CLAUDE, REG, alive=lambda pane: pane != 'old-1')
    assert usage.holders('flashnext') == ['w2'] and usage.admit('w3', REG.get('flashnext-coder')) is None
    # A plain handoff keeps the launch: the retiring tab holds the same model, counted once.
    slots = agents(w1='flashnext-coder')
    slots['w1'].update(retired_pane='old-1')
    assert model_usage.Usage.from_agents(slots, PI, REG).counts() == {'flashnext': 1}


def test_a_dead_slot_holds_its_model_only_while_a_tab_of_it_is_alive():
    slots = agents(w1=('flashnext-coder', 'dead'), w2='flashnext-coder')
    slots['w1']['pane'] = 'p-1'
    assert model_usage.Usage.from_agents(slots, PI, REG).holders('flashnext') == ['w2']
    assert model_usage.Usage.from_agents(slots, PI, REG, alive=lambda p: True).holders('flashnext') == ['w1', 'w2']
    assert model_usage.Usage.from_agents(slots, PI, REG, alive=lambda p: False).holders('flashnext') == ['w2']


def test_a_failed_switch_rolls_back_the_new_models_reservation(live, monkeypatch):
    f = live
    add_slots(f, worker_2='flashnext-coder')
    old_launch = f.agent('worker-1').get('runtime_launch', '')
    dispatch(f)
    monkeypatch.setattr(f.mgr, 'tab_into', lambda *args: False)  # the successor cannot be placed
    f.deliver('worker-1')
    info = f.agent('worker-1')
    assert info['state'] == 'dead' and f.task()['status'] == 'blocked'
    assert info['runtime_launch'] == old_launch  # rolled back once the new tab was closed
    usage = f.model_usage(REG)
    assert usage.holders('flashnext') == ['worker-2']  # the failed generation holds no ticket
    # The old tab was never closed: it stays recorded and holds what it ran before (the fixture's
    # Codex default is unmapped, so nothing is counted for it, and certainly not FlashNext).
    assert info['retired_pane'] and f.mgr.get_pane_status(info['retired_pane'])
    assert 'worker-1' not in usage.retiring and 'worker-1' not in usage.slots
    assert f.card_gate('worker-3', REG.get('flashnext-coder'), REG) is None


def test_a_failed_switch_whose_new_tab_survives_keeps_holding(live, monkeypatch):
    f = live
    add_slots(f, worker_2='flashnext-coder')
    dispatch(f)
    monkeypatch.setattr(f.mgr, 'tab_into', lambda *args: False)
    monkeypatch.setattr(f.mgr, 'kill_pane', lambda pane: False)  # nothing can be closed
    f.deliver('worker-1')
    info = f.agent('worker-1')
    assert info['state'] == 'dead' and json.loads(info['runtime_launch'])['profile_id'] == 'flashnext-coder'
    assert f.model_usage(REG).holders('flashnext') == ['worker-1', 'worker-2']  # its tab still runs FlashNext
    assert 'running on worker-1, worker-2' in f.card_gate('worker-3', REG.get('flashnext-coder'), REG)[1]


def test_switch_away_from_a_capped_model_keeps_its_ticket_until_the_old_tab_closes(live, monkeypatch):
    f = live
    f.cfg.update(worker_cli='claude', worker_model='opus')
    add_slots(f, worker_2='flashnext-coder')
    f.setf('worker-1', runtime_launch=launch('flashnext-coder'))
    old = f.agent('worker-1')['pane']
    seen = {}
    real_kill = f.mgr.kill_pane

    def kill(pane):  # observe the counts while the old FlashNext tab is still alive
        if pane == old:
            seen['during'] = f.model_usage(REG).holders('flashnext')
        return real_kill(pane)
    monkeypatch.setattr(f.mgr, 'kill_pane', kill)
    f.switch_profile('worker-1', profile_launch.resolve_launch(REG.get('claude-coder'), REG, f.cfg))
    assert seen['during'] == ['worker-1', 'worker-2']
    info = f.agent('worker-1')
    assert info['retired_pane'] == '' and info['retired_launch'] == ''
    assert f.model_usage(REG).holders('flashnext') == ['worker-2']


def test_the_fleet_default_is_capped_like_a_card():
    assert model_usage.default_problem(PI, ['w1', 'w2'], REG) is None
    assert model_usage.default_problem(PI, ['w1', 'w2', 'w3'], REG) == (
        'fleet default worker model flashnext: 3 worker slots (w1, w2, w3) exceed max_concurrent 2')
    assert 'runs only on studio, this fleet is on macmini' in model_usage.default_problem(
        dict(PI, host='macmini'), ['w1'], REG)
    assert model_usage.default_problem(CLAUDE, ['w%d' % i for i in range(9)], REG) is None  # opus: 按名额
    assert model_usage.default_problem({'worker_cli': 'pi', 'pi_model': 'local/x'}, ['a', 'b', 'c'], REG) is None
    fable = {'worker_cli': 'claude', 'worker_model': REG.model('fable').model}
    assert 'max_concurrent 1' in model_usage.default_problem(fable, ['w1', 'w2'], REG)
    # A roster of only default slots is capped by the default model's own card.
    assert model_usage.check_slots(PI, ['a', 'b', 'c'], {}, REG) == [
        'model flashnext: 3 slots (a, b, c) exceed max_concurrent 2']
    assert model_usage.check_slots(PI, ['a', 'b'], {}, REG) == []


def test_switching_back_to_the_default_is_gated(live):
    f = live
    f.cfg.update(worker_cli='pi', pi_model=REG.model('flashnext').model)
    add_slots(f, worker_2='flashnext-coder', worker_3='')
    f.setf('worker-1', runtime_launch=launch('claude-coder'))
    assert f.card_gate('worker-1', None, REG) == (
        'wait', 'max_concurrent 2 for model flashnext reached (running on worker-2, worker-3)')
    f.setf('worker-3', state='dead')
    assert f.card_gate('worker-1', None, REG) is None
    f.cfg['pi_model'] = 'local/default'  # unmapped default: not capped, not guessed
    f.setf('worker-3', state='idle')
    assert f.card_gate('worker-1', None, REG) is None


def test_start_without_profiles_refuses_more_default_slots_than_the_card_allows(monkeypatch):
    from cmux_fleet import cmux_manager

    def forbidden():
        pytest.fail('the default-model cap must be checked before cmux/Redis provisioning')
    monkeypatch.setattr(cmux_manager, 'cmux_available', forbidden)
    with pytest.raises(SystemExit, match=r'3 worker slots \(worker-1, worker-2, worker-3\) exceed max_concurrent 2'):
        fleet.main(['start', '--lifecycle', 'dev_task', '--supervisor-cli', 'pi', '--worker-cli', 'pi', '--workers', '3',
                    '--pi-model', REG.model('flashnext').model, '--allow-development-v2'])
    with pytest.raises(SystemExit, match='exceed max_concurrent 1'):
        fleet.main(['start', '--lifecycle', 'dev_task', '--supervisor-cli', 'codex', '--worker-cli', 'claude', '--worker-names', 'a-1,b-1',
                    '--worker-model', REG.model('fable').model, '--allow-development-v2'])


def test_a_sub_fleet_on_a_capped_default_is_refused(fx):
    from cmux_fleet import subfleet
    f = fx
    f.cfg.update(worker_cli='claude', worker_model=REG.model('fable').model)
    reason = subfleet.check(f, 'worker-1', 'M0', 2)
    assert reason and 'max_concurrent 1' in reason


def test_a_failed_switch_away_keeps_the_old_models_ticket_while_the_old_tab_lives(live, monkeypatch):
    f = live
    f.cfg.update(worker_cli='claude', worker_model='opus')
    add_slots(f, worker_2='flashnext-coder')
    f.setf('worker-1', runtime_launch=launch('flashnext-coder'))
    monkeypatch.setattr(f.mgr, 'tab_into', lambda *args: False)
    f.switch_profile('worker-1', profile_launch.resolve_launch(REG.get('claude-coder'), REG, f.cfg))
    info = f.agent('worker-1')
    assert info['state'] == 'dead' and info['runtime_launch'] == launch('flashnext-coder')  # rolled back
    usage = f.model_usage(REG)
    assert usage.holders('flashnext') == ['worker-1', 'worker-2'] and usage.holders('opus') == []
    f.mgr.kill_pane(info['retired_pane'])  # the old tab goes away later: its ticket is released
    assert f.model_usage(REG).holders('flashnext') == ['worker-2']


def test_a_rotation_after_an_earlier_switch_holds_the_switched_model():
    # After a successful switch the hash keeps retired_launch='' (key present); a later rotation whose
    # old tab could not be closed records the launch that tab ran, so it is not mistaken for the default.
    slots = agents(w1=('flashnext-coder', 'dead'), w2='flashnext-coder')
    slots['w1'].update(retired_pane='old-1', retired_launch=launch('flashnext-coder'))
    usage = model_usage.Usage.from_agents(slots, {'worker_cli': 'codex'}, REG, alive=lambda p: p == 'old-1')
    assert usage.holders('flashnext') == ['w1', 'w2']
