"""Named sub-fleets pick their supervisor from the supervisor chain
(docs/design.md §29 "Supervisor model", D27 / D28).

An entry with no explicit model (no supervisor_model, no card on the supervisor slot's root roster row) gets the first
usable Claude entry of fleet.yaml#discovery.supervisor (job_chain.subfleet_supervisor): Astra on pi is skipped with
its reason, an unusable Fable falls through to Opus. A slot that already runs the pick is adopted in place; any other
slot rotates to it first. No usable entry: the up is refused with every entry's reason and no child starts.

The probe cache is a stub file (FLEETMUX_DISCOVERED); this machine's cache is never read."""
import json

import pytest
import yaml

from test_subfleet import result
from fleetmux import brake, discover, fleet, fleet_config, named_subfleet, profile_launch, subfleet

ASTRA = 'openai-codex/gpt-6-astra'
SKIP_ASTRA = 'runs pi, not claude: a sub-fleet supervisor is an adopted official Claude Code pane (subfleet.check)'


def probe_cache(path, claude=('fable', 'haiku', 'opus', 'sonnet')):
    row = {'installed': True, 'version_ok': True, 'logged_in': True, 'path': '/bin/x'}
    data = {'version': 1, 'clis': {'claude': dict(row, models=list(claude)),
                                   'pi': dict(row, models=[ASTRA, 'openai-codex/gpt-6.1-sol'])},
            'gateways': {'openai-codex': {'online': True}}}
    path.write_text(yaml.safe_dump(data))


@pytest.fixture
def named_chain_as_before(tmp_path, monkeypatch):
    """For named sub-fleet tests about something else: the supervisor chain names only the run's default worker model,
    so the supervisor slot (which runs it) is adopted in place, as before chains. The private fleet.yaml's discovery
    block is the shipped one with that chain; no probe cache, so the model cards are the pool."""
    model = fleet.DEFAULTS['worker_model']
    disc = dict(fleet_config.block('discovery') or {}, supervisor=[model])
    path = tmp_path / 'private-named-chain-as-before.yaml'
    path.write_text(yaml.safe_dump({'discovery': disc}))
    monkeypatch.setenv(fleet_config.USER_ENV, str(path))
    monkeypatch.setenv(discover.CACHE_ENV, str(tmp_path / 'no-discovered.yaml'))
    return model


@pytest.fixture
def tree(tmp_path, monkeypatch):
    cache = tmp_path / 'discovered.yaml'
    probe_cache(cache)
    monkeypatch.setenv(discover.CACHE_ENV, str(cache))
    monkeypatch.setattr(fleet.Fleet, 'preflight_profile', lambda self, launch, **kw: None)  # no claude binary here
    from test_subfleet_named_tmux import service_tree  # here: that module imports named_chain_as_before from this one
    t = service_tree(tmp_path, monkeypatch)
    t.cache = cache
    yield t
    for _ in t.gen:
        pass


def approve_up(t):
    from test_subfleet_named_tmux import approve_up as up
    return up(t)


def run_on(f, slot, model):
    launch = {'profile_id': '', 'role_text': '', 'overrides': {'worker_cli': 'claude', 'worker_model': model}}
    f.setf(slot, runtime_launch=profile_launch.encode_launch(launch))


def chain_record(f, name='ops'):
    raw = named_subfleet.state(f, name).get('model_chain')
    return json.loads(raw)['supervisor'] if raw else None


def test_entry_without_model_takes_fable_and_records_the_astra_skip(tree):
    f = tree.f
    run_on(f, 'worker-1', 'fable')
    approve_up(tree)
    assert named_subfleet.live(f), 'the child did not start'
    got = chain_record(f)
    assert (got['cli'], got['model'], got['source'], got['profile']) == ('claude', 'fable', 'discovery', 'claude-fable')
    assert got['skipped'][0] == {'pattern': 'astra', 'cli': 'pi', 'model': ASTRA, 'reason': SKIP_ASTRA}


def test_slot_already_on_the_pick_is_adopted_in_place_without_rotation(tree):
    f = tree.f
    run_on(f, 'worker-1', 'claude-fable-5-2')  # the same chain place as the cached alias "fable"
    approve_up(tree)
    info = f.agent('worker-1')
    assert named_subfleet.live(f) and info['generation'] == '0' and not info.get('split_launch')
    assert info['state'] == 'delegated'


def test_slot_on_another_model_rotates_to_the_pick_before_the_child_starts(tree):
    f = tree.f
    approve_up(tree)  # worker-1 runs the fleet default (sonnet): not the pick
    info = f.agent('worker-1')
    assert not named_subfleet.live(f) and info['state'] == 'handoff'
    assert json.loads(json.loads(info['split_launch'])['launch'])['overrides'] == {'worker_cli': 'claude', 'worker_model': 'fable'}
    assert chain_record(f)['model'] == 'fable'


def test_a_walled_fable_falls_through_to_opus(tree):
    f = tree.f
    f.r.hset(fleet.WALLS, 'claude:fable', json.dumps({'cli': 'claude', 'model': 'fable', 'scope': 'model_pool',
                                                      'account': brake.account('claude', 'fable'), 'agent': 'x'}))
    approve_up(tree)
    info = f.agent('worker-1')
    assert json.loads(json.loads(info['split_launch'])['launch'])['overrides']['worker_model'] == 'opus'
    got = chain_record(f)
    assert got['model'] == 'opus'
    assert {'pattern': 'fable', 'cli': 'claude', 'model': 'fable', 'reason': 'model-pool wall'} in got['skipped']


def test_an_explicit_supervisor_model_is_used_not_the_chain(tree):
    f = tree.f
    entry = dict(named_subfleet.declared(f)['ops'], supervisor_model='sonnet')
    status, _ = named_subfleet.try_up(f, entry)
    assert status == 'started' and f.agent('worker-1').get('split_launch', '') == ''
    got = chain_record(f)
    assert (got['model'], got['source'], got['skipped']) == ('sonnet', 'entry', [])


def test_a_card_on_the_supervisor_slot_row_is_used_not_the_chain(tree):
    f = tree.f
    f.cfg['worker_profiles'] = {'worker-1': 'claude-sonnet'}
    f.setf('worker-1', roster_profile='claude-sonnet')
    status, _ = named_subfleet.try_up(f, named_subfleet.declared(f)['ops'])
    assert status == 'started' and f.agent('worker-1').get('split_launch', '') == ''
    got = chain_record(f)
    assert (got['source'], got['profile']) == ('card', 'claude-sonnet')


def test_no_usable_entry_refuses_the_up_and_starts_nothing(tree):
    f = tree.f
    probe_cache(tree.cache, claude=('haiku', 'sonnet'))  # neither fable nor opus on this machine
    rid = approve_up(tree)
    res = result(f, rid)
    assert res['status'] == 'rejected', res
    assert 'astra: pi:' + ASTRA + ': ' + SKIP_ASTRA in res['reason']
    assert 'fable: no model on this machine matches' in res['reason']
    assert not named_subfleet.live(f) and not (f.run_dir / 'sub' / 'ops').exists()
    assert f.agent('worker-1').get('split_launch', '') == ''


def test_the_trace_goes_to_the_named_entry_spawn_child_gets(tree, monkeypatch):
    """The child config / subfleet.json / status side is dev-1's (subfleet.child_config); this is the hand-over."""
    f, seen = tree.f, []
    real = subfleet.spawn_child

    def spy(*args, **kwargs):
        seen.append(kwargs.get('named'))
        return real(*args, **kwargs)
    monkeypatch.setattr(subfleet, 'spawn_child', spy)
    run_on(f, 'worker-1', 'fable')
    approve_up(tree)
    assert seen and seen[-1]['model_chain']['supervisor']['model'] == 'fable'
    assert seen[-1]['model_chain']['supervisor'] == chain_record(f)


def wall_on(f, model):
    f.r.hset(fleet.WALLS, 'claude:' + model, json.dumps({'cli': 'claude', 'model': model, 'scope': 'model_pool',
                                                         'account': brake.account('claude', model), 'agent': 'x'}))


def test_walls_on_every_claude_entry_wait_with_the_reason_and_the_timeout_answer_says_why(tree):
    f = tree.f
    for model in ('fable', 'opus'):
        wall_on(f, model)
    rid = approve_up(tree)
    assert result(f, rid) is None and not named_subfleet.live(f)  # waits, not refused
    status, why = named_subfleet.try_up(f, named_subfleet.declared(f)['ops'])
    assert status == 'wait' and why.startswith(named_subfleet.CHAIN_WAIT + 'no usable entry now: ')
    assert 'fable: claude:fable: model-pool wall' in why and 'opus: claude:opus: model-pool wall' in why
    f.r.hset(named_subfleet.OP + rid, mapping={'approved_at': 0})  # SLOT_WAIT is over
    subfleet.tick(f)
    res = result(f, rid)
    assert res['status'] == 'timeout' and res['reason'].startswith('slot busy: ' + named_subfleet.CHAIN_WAIT)


def test_autostart_keeps_a_chain_refusal_visible_in_blocked(tree):
    f = tree.f
    probe_cache(tree.cache, claude=('haiku',))
    entry = dict(named_subfleet.declared(f)['ops'], autostart=True)
    f.subfleet_decl['ops'] = entry
    subfleet.tick(f)
    assert 'no usable supervisor for sub-fleet ops' in named_subfleet.state(f, 'ops')['blocked']
    assert not named_subfleet.live(f)


# ------------------------------------------------------------------ what the plan said to measure first (§5)

def test_measure_the_whole_loop_rotation_then_delegation_on_the_pick(tree):
    """Fake world: the handoff turn is ended by the daemon's skeleton (finish_rotation timed out), as when nobody
    takes it; the real turn's duration is not measured here."""
    f = tree.f
    approve_up(tree)
    assert f.agent('worker-1')['state'] == 'handoff' and not named_subfleet.live(f)
    f.finish_rotation('worker-1', timed_out=True)
    info = f.agent('worker-1')
    assert info['generation'] == '1' and not info.get('split_launch')
    assert json.loads(info['runtime_launch'])['overrides']['worker_model'] == 'fable'
    assert 'worker-1' not in f.profile_swaps()  # the roster reconciler leaves the chain's launch alone
    f.setf('worker-1', state='idle', runtime_ready=1)
    subfleet.tick(f)
    assert named_subfleet.live(f) and subfleet.record(f.run_dir / 'sub' / 'ops')['slot'] == 'worker-1'
    assert chain_record(f)['model'] == 'fable'


def test_measure_a_card_a_reload_puts_on_the_slot_row_wins_at_the_next_up(tree):
    f = tree.f
    status, _ = named_subfleet.supervisor_choice(f, 'worker-1', named_subfleet.declared(f)['ops'])
    assert status == 'wait'  # no card: the chain rotates it
    f.setf('worker-1', state='idle', split_launch='')
    f.cfg['worker_profiles'] = {'worker-1': 'claude-opus'}  # what a roster reload with a card on that row sets
    status, trace = named_subfleet.supervisor_choice(f, 'worker-1', named_subfleet.declared(f)['ops'])
    assert status == 'ok' and (trace['source'], trace['profile']) == ('card', 'claude-opus')


# ------------------------------------------------------------------ attached children (Q2 (a))

@pytest.fixture
def root(tmp_path, monkeypatch):
    from test_subfleet import svc as svc_fixture, write_roster
    cache = tmp_path / 'discovered.yaml'
    probe_cache(cache)
    monkeypatch.setenv(discover.CACHE_ENV, str(cache))
    monkeypatch.setattr(fleet.Fleet, 'preflight_profile', lambda self, launch, **kw: None)  # no claude binary here
    gen = svc_fixture.__wrapped__(tmp_path, monkeypatch)
    t = next(gen)
    t.cache = cache
    write_roster(t, declare=False)
    t.f.roster_tick()
    yield t
    for _ in gen:
        pass


def attach(t, name='docs', **extra):
    from test_service_attach import op
    return op(t, 'attach', name, **extra)


def test_attach_without_model_takes_an_idle_worker_already_on_the_pick(root):
    f = root.f
    run_on(f, 'worker-2', 'fable')
    _, answer = attach(root)
    assert answer['status'] == 'accepted', answer
    subfleet.tick(f)
    assert named_subfleet.attached(f)['docs']['entry']['supervisor_slot'] == 'worker-2'
    assert not named_subfleet.attached(f)['docs']['hot_slot']


def test_attach_without_model_adds_a_worker_on_the_pick_and_it_counts_under_that_model(root):
    f = root.f
    _, answer = attach(root)  # worker-1 / worker-2 run the default model, not the pick
    assert answer['status'] == 'accepted', answer
    subfleet.tick(f)
    rec = named_subfleet.attached(f)['docs']
    assert rec['hot_slot'] == 'worker-3' and rec['supervisor_pick']['model'] == 'fable'
    f.roster_tick()  # the reload starts it: init_worker -> added_launch
    launch = json.loads(f.agent('worker-3')['runtime_launch'])
    assert launch == {'profile_id': '', 'role_text': '',
                      'overrides': {'worker_cli': 'claude', 'worker_model': 'fable'}}
    from fleetmux.agent_registry import AgentRegistry
    assert 'worker-3' in f.model_usage(AgentRegistry()).holders('fable')


def test_attach_is_refused_at_entry_when_the_chain_has_no_claude_entry(root):
    probe_cache(root.cache, claude=('haiku', 'sonnet'))
    _, answer = attach(root)
    assert answer['status'] == 'rejected' and 'no usable supervisor for sub-fleet docs' in answer['reason']
    assert 'astra: pi:' + ASTRA + ': ' + SKIP_ASTRA in answer['reason']


# ------------------------------------------------------------------ [M16]: plan §6 items 6, 7, 11, 12, 15

def cap(monkeypatch, *models):
    """These model cards get max_concurrent 1 for this test (the shipped fable / opus cards have no cap)."""
    from fleetmux import fleet_config as fc
    real = fc.block

    def block(name, *args, **kwargs):
        data = real(name, *args, **kwargs)
        if name == 'models' and isinstance(data, dict):
            data = dict(data, **{m: dict(data[m], max_concurrent=1) for m in models if m in data})
        return data
    monkeypatch.setattr(fc, 'block', block)


def test_quota_full_on_the_pick_waits_and_the_next_try_goes_on(tree, monkeypatch):
    """Plan §6 item 6: Fable at its quota (worker-2 holds it) and Opus walled: wait with the reason, never refused;
    once worker-2 lets Fable go, the next try rotates worker-1 to it."""
    f = tree.f
    cap(monkeypatch, 'fable')
    run_on(f, 'worker-2', 'fable')
    wall_on(f, 'opus')
    entry = named_subfleet.declared(f)['ops']
    status, why = named_subfleet.try_up(f, entry)
    assert status == 'wait' and 'max_concurrent 1 for model fable' in why and 'opus: claude:opus: model-pool wall' in why
    run_on(f, 'worker-2', 'sonnet')
    status, why = named_subfleet.try_up(f, entry)
    assert status == 'wait' and why.endswith('worker-1 is rotating to claude:fable first')
    assert f.agent('worker-1')['state'] == 'handoff'


def test_an_external_up_that_waits_on_the_chain_records_the_reason_in_blocked(tree):
    """Plan §6 item 7: the wait reason is kept in fx:subfleet:<name>.blocked while the op is open (its timeout
    answer carries it too: test_walls_on_every_claude_entry_wait_with_the_reason_and_the_timeout_answer_says_why)."""
    f = tree.f
    for model in ('fable', 'opus'):
        wall_on(f, model)
    rid = approve_up(tree)
    assert result(f, rid) is None
    assert named_subfleet.state(f, 'ops')['blocked'].startswith(named_subfleet.CHAIN_WAIT + 'no usable entry now: ')


def test_attach_with_supervisor_model_keeps_the_explicit_model(root):
    """Plan §6 item 11: the chain is not asked; an idle worker on that model supervises."""
    f = root.f
    run_on(f, 'worker-2', 'sonnet')
    run_on(f, 'worker-1', 'haiku')
    _, answer = attach(root, supervisor_model='sonnet')
    assert answer['status'] == 'accepted', answer
    subfleet.tick(f)
    rec = named_subfleet.attached(f)['docs']
    assert rec['entry']['supervisor_slot'] == 'worker-2' and not rec['hot_slot'] and 'supervisor_pick' not in rec


def test_attach_supervisor_model_matches_by_chain_place(root):
    """Plan §6 item 12: a worker on claude-opus-5-5 is a worker on opus: no worker is added for it."""
    f = root.f
    run_on(f, 'worker-1', 'claude-opus-5-5')
    _, answer = attach(root, supervisor_model='opus')
    assert answer['status'] == 'accepted', answer
    subfleet.tick(f)
    rec = named_subfleet.attached(f)['docs']
    assert rec['entry']['supervisor_slot'] == 'worker-1' and not rec['hot_slot']


def test_parent_resume_does_not_pick_the_delegated_supervisor_again(tree, monkeypatch):
    """Plan §6 item 15: the delegated pane belongs to the child; a parent resume brings the sub-fleet back with it."""
    f = tree.f
    run_on(f, 'worker-1', 'fable')
    approve_up(tree)
    assert named_subfleet.live(f)
    wall_on(f, 'fable')  # a new pick would now be Opus
    asked = []
    monkeypatch.setattr(named_subfleet, 'supervisor_choice', lambda *a: asked.append(a) or ('ok', {}))
    subfleet.resume_children(f, launch=lambda *a: None)
    assert asked == [] and f.agent('worker-1').get('split_launch', '') == ''
    assert json.loads(f.agent('worker-1')['runtime_launch'])['overrides']['worker_model'] == 'fable'


def test_attached_slot_rotates_when_the_pick_changes_before_up(root):
    """D35 (Q-B): the attached child got worker-2 (on Fable, the pick); Fable is walled before the up. The program
    rotates worker-2 to the new pick (Opus) instead of placing the child again: same slot, no worker added."""
    f = root.f
    run_on(f, 'worker-2', 'fable')
    _, answer = attach(root)
    assert answer['status'] == 'accepted', answer
    named_subfleet.attach_tick(f)  # the slot only, no up yet
    assert named_subfleet.attached(f)['docs']['entry']['supervisor_slot'] == 'worker-2'
    wall_on(f, 'fable')
    subfleet.tick(f)
    rec = named_subfleet.attached(f)['docs']
    assert rec['entry']['supervisor_slot'] == 'worker-2' and not rec['hot_slot']
    info = f.agent('worker-2')
    assert info['state'] == 'handoff' and not named_subfleet.live(f)
    assert json.loads(json.loads(info['split_launch'])['launch'])['overrides']['worker_model'] == 'opus'
