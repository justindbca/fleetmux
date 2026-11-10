"""Model cards (design v2 §3) + card fields read by the router and the handoff check (design.md §12)."""
import json
from types import SimpleNamespace

import pytest

from cmux_fleet import fleet
from cmux_fleet.agent_registry import AgentRegistry, AgentRegistryError
from cmux_fleet.planner import build_dispatch_plan
from cmux_fleet.router import route_task

SEVEN = ['astra', 'fable', 'flashnext', 'flashnext-dsh', 'haiku', 'opencode-flash-free', 'opus', 'sol', 'sonnet',
         'workbuddy-ds-dsh']  # + opencode free tier, dsh (FlashNext, WorkBuddy DeepSeek)


def test_default_registry_has_seven_model_cards_and_the_same_four_profiles():
    registry = AgentRegistry()
    assert [m.id for m in registry.models] == SEVEN
    assert [c.id for c in registry] == ['claude-coder', 'claude-fable', 'claude-haiku', 'claude-opus', 'claude-reviewer',
                                        'claude-sonnet', 'flashnext-coder', 'researcher']  # + four advisor launch cards
    for model in registry.models:
        assert model.judgement and model.quota_pool and model.roles_allowed and model.evidence
        assert model.night_ok is not None and model.measured is None  # measured: bookkeeper fills it
    assert registry.model('opus').night_ok is False and registry.model('flashnext').judgement == 'low-mid'
    assert registry.model('flashnext').cli == 'pi' and registry.model('astra').cli == 'codex'


def test_profiles_inherit_unset_fields_from_their_model_card():
    registry = AgentRegistry()
    fx = registry.get('flashnext-coder')
    assert (fx.model_card, fx.handoff_ratio, fx.hosts, fx.max_concurrent, fx.context_window) == \
        ('flashnext', 0.5, ('studio',), 2, 100000)
    coder = registry.get('claude-coder')
    assert coder.model_card == 'opus' and coder.night_ok is False and 'implementer' in coder.roles_allowed
    assert registry.get('claude-reviewer').roles_allowed == registry.model('opus').roles_allowed
    researcher = registry.get('researcher')
    assert researcher.model_card is None and researcher.hosts is None and researcher.handoff_ratio is None
    json.dumps(registry.as_list(), ensure_ascii=False)  # `agents list` output stays JSON


def card(**extra):
    return dict({'id': 'x-card', 'display_name': 'X', 'cli': 'claude', 'capabilities': ['code-edit']}, **extra)


def write(directory, data, name='x.json'):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(data))


@pytest.mark.parametrize('extra,match', [
    ({'handoff_ratio': 0}, 'handoff_ratio'), ({'handoff_ratio': 1.5}, 'handoff_ratio'),
    ({'handoff_ratio': True}, 'handoff_ratio'), ({'judgement': 'great'}, 'judgement'),
    ({'night_ok': 'yes'}, 'night_ok'), ({'max_concurrent': 0}, 'max_concurrent'),
    ({'context_window': -1}, 'context_window'), ({'hosts': []}, 'hosts'), ({'domains': 'fleet'}, 'domains'),
    ({'roles_allowed': ['a'], 'roles_denied': ['a']}, 'both allowed and denied'),
    ({'measured': [1]}, 'measured'), ({'evidence': ' '}, 'evidence'), ({'quota_pool': 3}, 'quota_pool'),
    ({'model_card': 'nope'}, 'model_card nope not found'),
    ({'unverified': ['colour']}, 'unverified lists unknown fields: colour'),
])
def test_bad_card_fields_are_rejected(tmp_path, extra, match):
    write(tmp_path, card(**extra))
    with pytest.raises(AgentRegistryError, match=match):
        AgentRegistry(tmp_path)


def test_model_card_must_match_the_profile_cli_and_is_validated_too(tmp_path):
    write(tmp_path / 'models', {'id': 'm', 'display_name': 'M', 'cli': 'pi'})
    write(tmp_path, card(model_card='m'))
    with pytest.raises(AgentRegistryError, match='differs from model card'):
        AgentRegistry(tmp_path)
    write(tmp_path / 'models', {'id': 'm', 'display_name': 'M', 'cli': 'claude', 'judgement': 'meh'})
    with pytest.raises(AgentRegistryError, match='judgement'):
        AgentRegistry(tmp_path)


def test_profile_value_overrides_the_model_card(tmp_path):
    write(tmp_path / 'models', {'id': 'm', 'display_name': 'M', 'cli': 'claude', 'handoff_ratio': .5,
                                'max_concurrent': 1})
    write(tmp_path, card(model_card='m', handoff_ratio=.7))
    got = AgentRegistry(tmp_path).get('x-card')
    assert got.handoff_ratio == .7 and got.max_concurrent == 1


def registry_of(tmp_path, *cards):
    for item in cards:
        write(tmp_path, item, item['id'] + '.json')
    return AgentRegistry(tmp_path)


def test_router_filters_host_role_and_concurrency_before_ranking(tmp_path):
    cheap = card(id='cheap', cli='pi', cost_rank=1, latency_rank=1, hosts=['studio'], max_concurrent=1,
                 roles_allowed=['implementer'], roles_denied=['reviewer'])
    dear = card(id='dear', cost_rank=3, latency_rank=3)
    registry = registry_of(tmp_path, cheap, dear)
    text = 'implement the feature'
    assert route_task(text, registry).agent_id == 'cheap'  # no filters: legacy ranking
    assert route_task(text, registry, host='Studio.local').agent_id == 'cheap'
    assert route_task(text, registry, host='macmini').agent_id == 'dear'
    assert route_task(text, registry, role='implementer').agent_id == 'cheap'
    assert route_task(text, registry, role='reviewer').agent_id == 'dear'
    assert route_task(text, registry, role='planner').agent_id == 'dear'  # not in roles_allowed
    assert route_task(text, registry, occupancy={'cheap': 1}).agent_id == 'dear'
    assert route_task(text, registry, occupancy={'cheap': 0}).agent_id == 'cheap'


def test_router_error_names_the_filter_that_emptied_the_candidates(tmp_path):
    registry = registry_of(tmp_path, card(id='only', hosts=['studio'], max_concurrent=2))
    with pytest.raises(ValueError, match=r'only \(host macmini not in studio\)'):
        route_task('implement it', registry, host='macmini')
    with pytest.raises(ValueError, match='max_concurrent 2 reached'):
        route_task('implement it', registry, occupancy={'only': 2})
    with pytest.raises(ValueError, match='role'):
        route_task('implement it', registry, role=' ')


def test_default_registry_routing_unchanged_without_filters_and_host_aware_with_one():
    registry = AgentRegistry()
    assert route_task('fix the failing pytest', registry).agent_id == 'flashnext-coder'
    assert route_task('fix the failing pytest', registry, host='studio').agent_id == 'flashnext-coder'
    assert route_task('fix the failing pytest', registry, host='macmini').agent_id == 'claude-coder'
    assert route_task('fix the failing pytest', registry, role='reviewer').agent_id != 'flashnext-coder'
    plan = build_dispatch_plan('Implement the parser fix', registry, host='macmini')
    assert [s.agent_id for s in plan.subtasks] == ['claude-coder', 'claude-reviewer']


def test_handoff_ratio_follows_the_running_profile_card():
    def stub(runtime_profile, registry=None):
        return SimpleNamespace(cfg={'handoff_ratio': .6}, agent=lambda name: {'runtime_profile': runtime_profile},
                               task=lambda: {'agent_registry': registry})
    assert fleet.Fleet.handoff_ratio(stub('flashnext-coder'), 'worker-1') == .5
    assert fleet.Fleet.handoff_ratio(stub('claude-coder'), 'worker-1') == .6   # opus card: 0.6
    assert fleet.Fleet.handoff_ratio(stub('researcher'), 'worker-1') == .6     # no ratio: fleet default
    assert fleet.Fleet.handoff_ratio(stub(''), 'supervisor') == .6             # no profile: fleet default
    assert fleet.Fleet.handoff_ratio(stub('gone-card'), 'worker-1') == .6


@pytest.mark.parametrize('extra,expected', [([], None), (['--host', ' Studio '], 'studio')])
def test_start_host_is_written_to_config_only_when_given(tmp_path, monkeypatch, extra, expected):
    from test_pi import mock_start
    from cmux_fleet import cmux_manager
    args, _ = mock_start(tmp_path, monkeypatch)
    monkeypatch.setattr(cmux_manager, 'CmuxSessionManager', lambda name, **kw: SimpleNamespace(
        meta_path=tmp_path / 'isolated-state' / 'cmux-fleet.json', session_exists=lambda: False,
        spawn_window=lambda *a, **k: SimpleNamespace(success=True)))
    fleet.main(args + extra)
    assert fleet.load(tmp_path / 'latest.json')['host'] == expected


def test_fields_without_an_outside_fact_are_marked_unverified():
    registry = AgentRegistry()
    marked = {m.id: m.unverified for m in registry.models}
    assert marked['sol'] == marked['astra'] == ('context_window',)
    assert registry.model('sol').context_window is None  # left empty, not guessed
    assert marked['fable'] == ('model',) and marked['haiku'] == ('judgement',)
    assert marked['flashnext'] == ('hosts', 'roles_allowed')
    assert registry.get('flashnext-coder').unverified == ('hosts', 'roles_allowed')  # inherited
    assert marked['opus'] is None
    assert marked['opencode-flash-free'] == ('max_concurrent', 'judgement', 'roles_allowed')
    oc = registry.model('opencode-flash-free')
    assert (oc.cli, oc.context_window, oc.max_concurrent) == ('opencode', 200000, 2)  # free tier: 200K, concurrency untested
