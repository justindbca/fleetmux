"""Section 44 split sub-fleets: the supervisor walks the supervisor chain too (docs/design.md §44, §58 "Model chains").

D27: an approve without supervisor_profile no longer keeps the
slot's own model: the program picks the first usable Claude entry of discovery.supervisor (job_chain.subfleet_supervisor,
the same pick as a job's supervisor). An approve with supervisor_profile keeps its card. No usable entry: the program
refuses the approve and tells the supervisor (D28); no sub-fleet, no default model. Only private test Redis, fake
cmux, fake CLIs and a test discovery cache."""
import json

import pytest
import yaml

from test_long_split import audit, escalate_m0, rotate_for_split, ttree  # noqa: F401  (ttree: fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_subfleet import ask, hook, no_launch, step, supervisor_answers, svc, tree  # noqa: F401  (fixtures)
from test_named_subfleet_chain import named_chain_as_before  # noqa: F401  (fixture)
from fleetmux import discover, fleet, named_subfleet, profile_launch, subfleet
from fleetmux.agent_registry import AgentRegistry

ASTRA, SOL = 'openai-codex/gpt-6-astra', 'openai-codex/gpt-6.1-sol'
CACHE = {'version': 1, 'gateways': {}, 'clis': {
    'claude': {'installed': True, 'version_ok': True, 'logged_in': True, 'models': ['fable', 'haiku', 'opus', 'sonnet']},
    'pi': {'installed': True, 'version_ok': True, 'logged_in': True, 'models': [ASTRA, SOL]}}}


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    path = tmp_path / 'discovered.yaml'
    path.write_text(yaml.safe_dump(CACHE))
    monkeypatch.setenv(discover.CACHE_ENV, str(path))
    monkeypatch.setattr(subfleet, 'default_launch', no_launch)
    monkeypatch.setattr(fleet.Fleet, 'preflight_profile', lambda self, launch, cli_change_ok=False: None)
    return path


def wall(f, model):
    f.r.hset(fleet.WALLS, mapping={'claude:' + model: json.dumps({'cli': 'claude', 'model': model, 'scope': 'model_pool',
                                                                  'account': 'claude', 'at': 0, 'agent': 'worker-9'})})


def approve(t, f, asked, **extra):
    ok = {'status': 'RUNNING', 'completion': 10, 'reason': 'split', 'dispatch': [],
          'escalations': [dict({'task': 'M0', 'decision': 'approve'}, **extra)]}
    assert hook(t, f, 'supervisor', json.dumps(ok), prompt=asked)['status'] == 'committed'
    return f.escalation_state()['M0']


def delegate(t, f):
    """Rotation to the pick, READY, spawn: returns the child config."""
    rotate_for_split(t, f)
    f.tick()
    f.tick()
    assert f.escalation_state()['M0']['status'] == 'delegated'
    return fleet.load(f.run_dir / 'sub' / 'M0' / 'config.json')


def test_approve_without_card_takes_fable_and_records_why_astra_was_skipped(ttree, capsys):
    t, f = ttree, ttree.f
    rec = approve(t, f, escalate_m0(t, f))
    assert rec['status'] == 'rotating'
    launch = json.loads(rec['launch'])
    assert launch['profile_id'] == 'claude-fable' and launch['overrides']['worker_model'] == 'fable'
    cfg = delegate(t, f)
    sup = cfg['model_chain']['supervisor']
    assert (sup['cli'], sup['model'], sup['source'], sup['profile']) == ('claude', 'fable', 'discovery', 'claude-fable')
    assert sup['skipped'][0]['pattern'] == 'astra' and 'not claude' in sup['skipped'][0]['reason']
    item = next(v for k, v in subfleet.tree(f.cfg).items() if k.startswith('M0'))  # what `status` shows
    assert item['model_chain']['supervisor']['model'] == 'fable'


def test_approve_without_card_falls_to_opus_while_fable_is_walled(ttree):
    t, f = ttree, ttree.f
    wall(f, 'fable')
    rec = approve(t, f, escalate_m0(t, f))
    assert json.loads(rec['launch'])['overrides']['worker_model'] == 'opus'
    cfg = delegate(t, f)
    sup = cfg['model_chain']['supervisor']
    assert sup['model'] == 'opus'
    assert {r['pattern']: r['reason'] for r in sup['skipped']}['fable'] == 'model-pool wall'


def test_approve_with_a_card_keeps_the_card(ttree):
    t, f = ttree, ttree.f
    rec = approve(t, f, escalate_m0(t, f), supervisor_profile='claude-haiku')
    assert rec['supervisor_profile'] == 'claude-haiku' and json.loads(rec['launch'])['profile_id'] == 'claude-haiku'
    cfg = delegate(t, f)
    assert cfg['model_chain']['supervisor']['source'] == 'card'


def test_no_usable_entry_refuses_the_approve_and_starts_nothing(ttree):
    t, f = ttree, ttree.f
    wall(f, 'fable')
    wall(f, 'opus')  # the chain's Claude entries; astra and sol run pi
    before = f.agent('worker-1').get('runtime_launch', '')
    rec = approve(t, f, escalate_m0(t, f))
    assert rec['status'] == 'escalated' and not rec.get('launch')
    refused = audit(f, 'escalate.approve_refused')
    assert refused and 'fable' in refused[-1]['reason'] and 'opus' in refused[-1]['reason']
    assert not (f.run_dir / 'sub' / 'M0').exists()
    assert f.agent('worker-1').get('runtime_launch', '') == before and not f.agent('worker-1').get('split_launch')
    f.tick()
    step(t)
    assert any('[escalate] approve of M0 refused by the program' in m['body']['text']
               for m in queued_or_given(t, f))


def queued_or_given(t, f):
    from test_service_split import queued_to
    return queued_to(f, 'supervisor', 'approve of M0 refused')


def test_a_slot_already_running_the_pick_is_adopted_in_place(ttree):
    t, f = ttree, ttree.f
    registry = AgentRegistry()
    launch = profile_launch.resolve_launch(registry.get('claude-fable'), registry, f.cfg)
    sid = f.agent('worker-1')['pane']
    f.setf('worker-1', tmux_view=1, runtime_launch=profile_launch.encode_launch(launch))
    t.world.tmux_owner[sid] = 'worker-1-g0'
    asked = escalate_m0(t, f)
    gen = f.agent('worker-1')['generation']
    rec = approve(t, f, asked)
    assert rec['status'] == 'approved' and not rec.get('launch')  # no rotation
    f.tick()
    assert f.escalation_state()['M0']['status'] == 'delegated' and f.agent('worker-1')['generation'] == gen
    cfg = fleet.load(f.run_dir / 'sub' / 'M0' / 'config.json')
    assert cfg['model_chain']['supervisor']['model'] == 'fable'


# ------------------------------------------------------------------ named sub-fleets (section 29): the record of the pick
NAMED_TRACE = {'supervisor': {'cli': 'claude', 'model': 'fable', 'source': 'discovery', 'profile': 'claude-fable',
                              'skipped': [{'pattern': 'astra', 'cli': 'pi', 'model': ASTRA,
                                           'reason': 'runs pi, not claude (test stub)'}], 'switched': []}}


def test_a_named_entry_s_model_chain_goes_into_the_child_config(svc):
    t, f = svc, svc.f
    [entry] = named_subfleet.declared(f).values()
    cfg = subfleet.child_config(f, 'worker-1', 'ops', 1, t.tmp / 'c', 'pane-x', None, {}, 'n',
                                named=dict(entry, model_chain=NAMED_TRACE))
    assert cfg['model_chain'] == NAMED_TRACE
    cfg = subfleet.child_config(f, 'worker-1', 'ops', 1, t.tmp / 'c', 'pane-x', None, {}, 'n', named=entry)
    assert 'model_chain' not in cfg  # nothing picked, nothing recorded


@pytest.mark.usefixtures('named_chain_as_before')  # the slot is adopted in place; the trace below is a stub
def test_a_named_child_s_model_chain_is_in_its_config_record_and_status(svc, monkeypatch):
    t, f = svc, svc.f
    real = subfleet.spawn_child

    def spawn(f_, slot, task_id, *args, named=None, **kw):  # the pick's trace, as named_subfleet passes it
        return real(f_, slot, task_id, *args, named=dict(named, model_chain=NAMED_TRACE) if named else named, **kw)
    monkeypatch.setattr(subfleet, 'spawn_child', spawn)
    rid, answer = ask(t, 'up')
    assert answer['status'] == 'accepted', answer
    supervisor_answers(t, rid)
    subfleet.tick(f)
    rec = subfleet.record(f.run_dir / 'sub' / 'ops')
    assert rec['model_chain'] == NAMED_TRACE  # subfleet.json
    assert fleet.load(f.run_dir / 'sub' / 'ops' / 'config.json')['model_chain'] == NAMED_TRACE
    item = subfleet.tree(f.cfg)['ops']  # the named row of `status`
    assert item['model_chain']['supervisor']['skipped'][0]['reason'] == 'runs pi, not claude (test stub)'


def test_the_card_less_launch_serves_a_named_slot_too(svc):
    """split_supervisor_launch(slot, None, model) depends on no job: a named sub-fleet's slot gets the same launch."""
    t, f = svc, svc.f
    launch, why = f.split_supervisor_launch('worker-1', None, 'haiku')
    assert why is None and launch == {'profile_id': '', 'role_text': '',
                                      'overrides': {'worker_cli': 'claude', 'worker_model': 'haiku'}}



def test_status_shows_why_a_declared_sub_fleet_that_is_not_up_waits(svc, capsys):
    """D28 / §29: a declared sub-fleet waiting for the supervisor chain (wall / quota / rotating) has no subfleet.json
    record before it is up, so `status` shows its reason from fx:subfleet:<name> blocked (roster) and the attach
    record's waiting."""
    t, f = svc, svc.f
    f.r.hset(named_subfleet.HASH + 'ops', mapping={'state': 'down', 'blocked': 'wait for the supervisor chain: fable walled'})
    named_subfleet.save_attached(f, {'docs': {'entry': {}, 'id': 'r1', 'created': 0, 'hot_slot': '',
                                              'waiting': 'wait for the supervisor chain: no usable entry now'}})
    fleet.status(f.cfg)
    out = json.loads(capsys.readouterr().out)
    rows = out['subfleet_waiting']
    assert rows['ops'] == {'state': 'down', 'blocked': 'wait for the supervisor chain: fable walled'}
    assert rows['docs'] == {'state': 'attached', 'waiting': 'wait for the supervisor chain: no usable entry now'}


def test_status_has_no_waiting_block_when_nothing_waits(svc, capsys):
    t, f = svc, svc.f
    fleet.status(f.cfg)
    assert 'subfleet_waiting' not in json.loads(capsys.readouterr().out)
