"""Job model chains (docs/design.md §58 "Model chains"): fleetmux/job_chain.py and fleet.yaml#job_subfleet.

Every test passes its own discovery block, probe cache, pair table and card registry. The probe cache path points
at a missing file, so nothing reads this machine's discovered.yaml."""
import types

import pytest

import test_fleet  # noqa: F401  (puts the checkout on sys.path)
from fleetmux import brake, cli_pairs, discover, fleet_config, job_chain

ASTRA = 'openai-codex/gpt-6-astra'
SOL = 'openai-codex/gpt-6.1-sol'
FLASH = 'workbuddy-ds/workbuddy-ai/deepseek-v4.1-flash-sg'
FREE = 'opencode/deepseek-v4.1-flash-free'

DISC = {'supervisor': ['astra', 'fable', 'opus', 'sol', 'deepseek flash'],
        'worker': ['opus', 'sol', 'sonnet', 'terra', 'deepseek flash'],
        'mode': 'task', 'supervisor_pi_thinking': 'high', 'worker_thinking': 'high',
        'cli_order': ['claude', 'pi', 'opencode'],
        'only_via': {'claude': 'claude', 'fable': 'claude', 'opus': 'claude', 'sonnet': 'claude', 'haiku': 'claude',
                     'gpt': 'pi'}}

JOB_CFG = {'workers': 2, 'supervisor_profile': None, 'worker_profiles': [], 'hours': 4,
           'supervisor': None, 'worker': None}


def cli_row(models, **kw):
    return dict({'installed': True, 'version_ok': True, 'logged_in': True, 'path': '/bin/x', 'models': models}, **kw)


def cache(**changes):
    """A probe cache: claude (card aliases), pi (Codex + WorkBuddy gateways), opencode (free flash)."""
    clis = {'claude': cli_row(['fable', 'haiku', 'opus', 'sonnet']),
            'pi': cli_row([ASTRA, SOL, FLASH]),
            'opencode': cli_row([FREE])}
    gateways = {'openai-codex': {'online': True}, 'workbuddy-ds': {'online': True}}
    for name, row in changes.items():
        if name == 'gateways':
            gateways.update(row)
        else:
            clis[name] = row
    return {'version': 1, 'clis': clis, 'gateways': gateways}


def pairs(**status):
    """Pair table: claude supervisor x claude / pi workers (task) verified unless ``status`` says otherwise."""
    rows = [{'supervisor_cli': 'claude', 'worker_cli': w, 'mode': 'task', 'status': status.get(w, 'verified'),
             'adapter_missing': []} for w in ('claude', 'pi')]
    return cli_pairs.PairTable({'version': 1, 'pairs': rows}, 'test')


def registry(*cards):
    cards = cards or (('claude-coder', 'opus'), ('claude-fable', 'fable'), ('claude-opus', 'opus'),
                      ('claude-reviewer', 'opus'), ('claude-sonnet', 'sonnet'), ('flashnext-coder', 'flashnext/q'))
    return types.SimpleNamespace(cards=tuple(types.SimpleNamespace(id=i, cli='pi' if '/' in m else 'claude', model=m)
                                             for i, m in cards))


def wall(cli, model, scope='model_pool'):
    return {'cli': cli, 'model': model, 'scope': scope, 'account': brake.account(cli, model), 'agent': 'worker-1'}


@pytest.fixture(autouse=True)
def no_host_cache(tmp_path, monkeypatch):
    monkeypatch.setenv(discover.CACHE_ENV, str(tmp_path / 'no-discovered.yaml'))


def submit(spec, rows=None, cached=None, job_cfg=None, refused=None):
    return job_chain.submit_problem(dict({'goal': 'g'}, **spec), rows, dict(JOB_CFG, **(job_cfg or {})), DISC,
                                    cache() if cached is None else cached, refused)


# ------------------------------------------------------------------ fleet.yaml#job_subfleet

def private(tmp_path, monkeypatch, text):
    path = tmp_path / 'fleet.yaml'
    path.write_text(text)
    monkeypatch.setenv(fleet_config.USER_ENV, str(path))


def test_job_subfleet_block_accepts_supervisor_and_worker_keys(tmp_path, monkeypatch):
    assert fleet_config.job_subfleet()['supervisor'] is None and fleet_config.job_subfleet()['worker'] is None
    private(tmp_path, monkeypatch, 'job_subfleet:\n  supervisor: [astra, fable]\n  worker: [astra]\n')
    got = fleet_config.job_subfleet()
    assert got['supervisor'] == ['astra', 'fable'] and got['worker'] == ['astra'] and got['workers'] == 2


@pytest.mark.parametrize('text, why', [
    ('  supervisor: [fable]\n  supervisor_profile: claude-opus\n', 'supervisor (a model chain) and supervisor_profile'),
    ('  worker: [opus]\n  worker_profiles: [claude-opus, claude-opus]\n', 'worker (a model chain) and worker_profiles'),
])
def test_job_subfleet_block_refuses_chain_and_card_together(tmp_path, monkeypatch, text, why):
    private(tmp_path, monkeypatch, 'job_subfleet:\n' + text)
    with pytest.raises(ValueError, match=why.replace('(', r'\(').replace(')', r'\)')):
        fleet_config.job_subfleet()


def test_shipped_fable_max_concurrent_is_unlimited_astra_and_sol_stay_one():
    """D24: the fable cap of 1 was an initial rating without a measurement."""
    cards = fleet_config.block('models')
    assert cards['fable']['max_concurrent'] is None
    assert cards['astra']['max_concurrent'] == 1 and cards['sol']['max_concurrent'] == 1


@pytest.mark.parametrize('value', ['[]', 'astra', '[astra, 3]', '[""]', '{a: 1}'])
def test_job_subfleet_block_refuses_bad_chain_shape(tmp_path, monkeypatch, value):
    private(tmp_path, monkeypatch, 'job_subfleet:\n  worker: ' + value + '\n')
    with pytest.raises(ValueError, match=r'job_subfleet\.worker must be a non-empty list of model patterns'):
        fleet_config.job_subfleet()


# ------------------------------------------------------------------ effective: layers

def test_effective_takes_discovery_chains_when_job_subfleet_is_null():
    got = job_chain.effective({'goal': 'g'}, JOB_CFG, DISC)
    assert got['supervisor'] == DISC['supervisor'] and got['worker'] == DISC['worker']
    assert got['supervisor_profile'] is None and got['worker_profiles'] == []
    assert got['source'] == {'supervisor': 'discovery', 'worker': 'discovery', 'supervisor_profile': None,
                             'worker_profiles': None}


def test_effective_job_subfleet_chain_beats_discovery():
    got = job_chain.effective({'goal': 'g'}, dict(JOB_CFG, supervisor=['astra', 'fable'], worker=['sonnet']), DISC)
    assert got['supervisor'] == ['astra', 'fable'] and got['worker'] == ['sonnet']
    assert got['source']['supervisor'] == 'job_subfleet' and got['source']['worker'] == 'job_subfleet'


def test_effective_spec_chain_beats_job_subfleet():
    got = job_chain.effective({'goal': 'g', 'worker': ['astra'], 'supervisor': ['opus']},
                              dict(JOB_CFG, supervisor=['fable'], worker=['sonnet']), DISC)
    assert got['worker'] == ['astra'] and got['supervisor'] == ['opus']
    assert got['source']['worker'] == 'spec' and got['source']['supervisor'] == 'spec'


@pytest.mark.parametrize('key, value', [('worker', []), ('supervisor', []), ('worker', 'astra'), ('worker', ['astra', 3]),
                                        ('supervisor', [' ']), ('worker', {'astra': 1})])
def test_effective_refuses_empty_list_and_non_string_patterns(key, value):
    with pytest.raises(ValueError, match='job spec ' + key + ' must be a non-empty list of model patterns'):
        job_chain.effective({'goal': 'g', key: value}, JOB_CFG, DISC)


def test_effective_value_none_counts_as_not_given():
    got = job_chain.effective({'goal': 'g', 'worker': None, 'supervisor_profile': None},
                              dict(JOB_CFG, worker=['sonnet']), DISC)
    assert got['worker'] == ['sonnet'] and got['source']['worker'] == 'job_subfleet'


def test_effective_spec_chain_voids_config_card():
    got = job_chain.effective({'goal': 'g', 'supervisor': ['fable'], 'worker': ['astra']},
                              dict(JOB_CFG, supervisor_profile='claude-opus', worker_profiles=['claude-sonnet'] * 2),
                              DISC)
    assert got['supervisor'] == ['fable'] and got['supervisor_profile'] is None
    assert got['worker'] == ['astra'] and got['worker_profiles'] == []
    assert got['source']['supervisor_profile'] is None and got['source']['worker_profiles'] is None


def test_effective_spec_card_voids_config_chain():
    got = job_chain.effective({'goal': 'g', 'supervisor_profile': 'claude-opus',
                               'worker_profiles': ['claude-sonnet', 'claude-sonnet']},
                              dict(JOB_CFG, supervisor=['fable'], worker=['astra']), DISC)
    assert got['supervisor'] is None and got['supervisor_profile'] == 'claude-opus'
    assert got['worker'] is None and got['worker_profiles'] == ['claude-sonnet', 'claude-sonnet']
    assert got['source'] == {'supervisor': None, 'worker': None, 'supervisor_profile': 'spec', 'worker_profiles': 'spec'}


def test_effective_config_card_beats_discovery_chain():
    got = job_chain.effective({'goal': 'g'}, dict(JOB_CFG, supervisor_profile='claude-opus'), DISC)
    assert got['supervisor'] is None and got['supervisor_profile'] == 'claude-opus'
    assert got['source']['supervisor_profile'] == 'job_subfleet'


@pytest.mark.parametrize('chain, key', [({'worker': ['astra']}, 'worker'),
                                        ({'overrides': {'worker_model': 'sonnet'}}, 'overrides.worker_model')])
def test_effective_refuses_spec_worker_chain_with_worker_profiles(chain, key):
    """D23: worker_profiles cover every slot, so a chain next to them would never be used."""
    spec = dict({'goal': 'g', 'worker_profiles': ['claude-sonnet', 'claude-opus']}, **chain)
    with pytest.raises(ValueError, match='job spec sets both ' + key.replace('.', r'\.') + r' \(a model chain\) and '
                                         r'worker_profiles \(cards\)'):
        job_chain.effective(spec, JOB_CFG, DISC)
    text = submit(spec)
    assert key + ' (a model chain) and worker_profiles (cards)' in text and text.endswith('Nothing was submitted.')


def test_effective_roster_drops_config_worker_cards():
    got = job_chain.effective({'goal': 'g', 'roster': '/abs/roster.yaml'},
                              dict(JOB_CFG, worker_profiles=['claude-sonnet', 'claude-sonnet']), DISC)
    assert got['worker'] == DISC['worker'] and got['worker_profiles'] == []


def test_effective_overrides_worker_model_is_a_spec_worker_chain():
    got = job_chain.effective({'goal': 'g', 'overrides': {'worker_model': 'sonnet'}}, dict(JOB_CFG, worker=['opus']),
                              DISC)
    assert got['worker'] == ['sonnet'] and got['source']['worker'] == 'spec'


def test_effective_refuses_worker_chain_with_overrides_worker_model():
    with pytest.raises(ValueError, match=r'both worker \(a model chain\) and overrides\.worker_model'):
        job_chain.effective({'goal': 'g', 'worker': ['astra'], 'overrides': {'worker_model': 'sonnet'}}, JOB_CFG, DISC)


def test_effective_refuses_same_layer_supervisor_chain_and_card():
    with pytest.raises(ValueError, match=r'both supervisor \(a model chain\) and supervisor_profile'):
        job_chain.effective({'goal': 'g', 'supervisor': ['fable'], 'supervisor_profile': 'claude-opus'}, JOB_CFG, DISC)
    with pytest.raises(ValueError, match=r'job_subfleet sets both supervisor'):
        job_chain.effective({'goal': 'g'}, dict(JOB_CFG, supervisor=['fable'], supervisor_profile='claude-opus'), DISC)


# ------------------------------------------------------------------ submit

INCIDENT_ROW = {'name': 'astra-1', 'pi_model': ASTRA, 'pi_thinking': 'high'}


def test_submit_refuses_roster_row_with_pi_model_and_points_at_worker_chain():
    """Incident 11-21 / 11-22: the roster asked for Astra on a row; the job was accepted and ran on Sonnet."""
    text = submit({'roster': '/x/superpower-mining-plan.yaml'}, rows=[INCIDENT_ROW])
    assert text.startswith('job roster /x/superpower-mining-plan.yaml: row astra-1 sets pi_model, pi_thinking.')
    assert '"worker": ["astra"]' in text and 'job_subfleet.worker' in text and text.endswith('Nothing was submitted.')


def test_submit_refuses_roster_row_with_only_pi_thinking():
    text = submit({'roster': '/x/r.yaml'}, rows=[{'name': 'w1'}, {'name': 'w2', 'pi_thinking': 'low'}])
    assert 'row w2 sets pi_thinking.' in text


def test_submit_keeps_roster_rows_with_profile_cards():
    rows = [{'name': 'w1', 'profile': 'claude-sonnet'}, {'name': 'w2', 'profile': 'claude-opus'}]
    # every slot has a card: the worker chain is not looked at, even one that resolves to nothing
    assert submit({'roster': '/x/r.yaml', 'worker': ['terra']}, rows=rows) is None


def test_submit_refuses_spec_supervisor_chain_with_supervisor_profile():
    text = submit({'supervisor': ['fable'], 'supervisor_profile': 'claude-opus'})
    assert 'both supervisor (a model chain) and supervisor_profile' in text and text.endswith('Nothing was submitted.')


def test_submit_refuses_worker_chain_without_usable_entry_naming_each_pattern():
    text = submit({'worker': ['astra', 'terra']}, cached=cache(gateways={'openai-codex': {'online': False}}))
    lines = text.splitlines()
    assert lines[0] == 'job worker chain ["astra", "terra"] (from spec) has no usable entry on this machine:'
    assert lines[1] == '  - astra: pi:' + ASTRA + ': gateway openai-codex offline'
    assert lines[2].startswith('  - terra: no model on this machine matches')
    assert lines[-1] == 'Nothing was submitted.'


def test_submit_refuses_supervisor_chain_without_claude_entry():
    text = submit({'supervisor': ['astra', 'sol']})
    assert text.splitlines()[0] == ('job supervisor chain ["astra", "sol"] (from spec) has no usable Claude entry on '
                                    'this machine:')
    assert '  - astra: pi:' + ASTRA + ': runs pi, not claude: a sub-fleet supervisor is an adopted official ' \
           'Claude Code pane (subfleet.check)' in text


def test_submit_names_logged_out_and_old_cli_reasons():
    text = submit({'worker': ['astra', 'sonnet']},
                  cached=cache(pi=cli_row([ASTRA], logged_in=False), claude=cli_row(['sonnet'], version_ok=False,
                                                                                    version='1.0', min='2.1')))
    assert 'astra: pi:' + ASTRA + ': pi is not logged in' in text
    assert 'sonnet: claude:sonnet: claude 1.0 is below 2.1' in text


def test_submit_without_cache_refuses_astra_and_says_run_setup():
    text = submit({'worker': ['astra']}, cached={})
    assert 'no discovery cache' in text and 'run `fleetmux setup`' in text and 'astra' in text


def test_submit_passes_refused_callback_and_refuses_when_nothing_left():
    gate = (lambda c, m: 'worker_cli: pi needs the parent fleet started with --allow-unsandboxed-pi'
            if c == 'pi' else None)
    text = submit({'worker': ['astra']}, refused=gate)
    assert 'astra: pi:' + ASTRA + ': worker_cli: pi needs the parent fleet started with --allow-unsandboxed-pi' in text
    assert submit({'worker': ['astra', 'opus']}, refused=gate) is None


def test_submit_skips_worker_chain_when_cards_cover_every_slot():
    assert submit({'worker_profiles': ['claude-sonnet', 'claude-opus']}, job_cfg={'worker': ['terra']}) is None


def test_submit_with_card_slots_skips_pi_entries():
    rows = [{'name': 'w1', 'profile': 'claude-sonnet'}, {'name': 'w2'}]
    text = submit({'roster': '/x/r.yaml', 'worker': ['astra']}, rows=rows)
    assert 'astra: pi:' + ASTRA + ': the job has card slots and a pi sub-fleet takes no cards (design §51)' in text
    assert submit({'roster': '/x/r.yaml', 'worker': ['astra', 'sonnet']}, rows=rows) is None


def test_submit_accepts_a_job_without_chains():
    assert submit({}) is None


# ------------------------------------------------------------------ pick_worker

def pick_worker(patterns, **kw):
    kw.setdefault('cached', cache())
    kw.setdefault('pairs', pairs())
    return job_chain.pick_worker(patterns, kw.pop('supervisor_cli', 'claude'), kw.pop('walled', ()), DISC, **kw)


def test_pick_worker_astra_on_pi_gives_worker_cli_pi_and_pi_model():
    got = pick_worker(['astra'])
    assert got['entry'] == {'cli': 'pi', 'model': ASTRA, 'thinking': 'high'}
    assert got['overrides'] == {'worker_cli': 'pi', 'pi_model': ASTRA, 'pi_thinking': 'high'}
    assert got['skipped'] == [] and got['reason'] is None


def test_pick_worker_thinking_is_discovery_worker_thinking():
    got = job_chain.pick_worker(['astra'], 'claude', (), dict(DISC, worker_thinking='medium'), cached=cache(),
                                pairs=pairs())
    assert got['overrides']['pi_thinking'] == 'medium' and got['entry']['thinking'] == 'medium'


def test_pick_worker_claude_entry_gives_worker_model():
    got = pick_worker(['opus', 'astra'])
    assert got['entry']['cli'] == 'claude' and got['overrides'] == {'worker_cli': 'claude', 'worker_model': 'opus'}


def test_pick_worker_skips_model_pool_wall_and_records_it():
    """Needs brake.wall_action's skipped_rows (dev-1)."""
    got = pick_worker(['opus', 'astra'], walled=[wall('claude', 'opus')])
    assert got['entry']['model'] == ASTRA
    assert got['skipped'] == [{'pattern': 'opus', 'cli': 'claude', 'model': 'opus', 'reason': 'model-pool wall'}]


def test_pick_worker_account_wall_skips_every_model_of_that_account():
    """Needs brake.wall_action's skipped_rows (dev-1)."""
    got = pick_worker(['astra', 'sol', 'deepseek flash'], walled=[wall('pi', ASTRA, 'account')])
    assert got['entry']['model'] == FLASH
    assert [(r['pattern'], r['reason']) for r in got['skipped']] == [('astra', 'account wall'), ('sol', 'account wall')]


def test_pick_worker_skips_pair_not_verified_with_supervisor_cli():
    got = pick_worker(['astra', 'opus'], pairs=pairs(pi='unverified'))
    assert got['entry']['model'] == 'opus'
    assert got['skipped'] == [{'pattern': 'astra', 'cli': 'pi', 'model': ASTRA,
                               'reason': 'claude supervisor + pi workers (task) is unverified'}]


def test_pick_worker_skips_opencode_and_dsh_entries():
    got = pick_worker(['deepseek flash'], cached=cache(pi=cli_row([]), opencode=cli_row([FREE])))
    assert got['entry'] is None and got['overrides'] == {}
    assert got['skipped'] == [{'pattern': 'deepseek flash', 'cli': 'opencode', 'model': FREE,
                               'reason': 'a sub-fleet runs claude or pi workers only (design §51)'}]


def test_pick_worker_returns_none_with_reason_when_chain_exhausted():
    """Needs brake.wall_action's skipped_rows (dev-1)."""
    got = pick_worker(['astra'], walled=[wall('pi', ASTRA, 'account')])
    assert got['entry'] is None and got['overrides'] == {}
    assert got['reason'] == 'no usable entry on the worker chain ["astra"]: astra: pi:' + ASTRA + ': account wall'


def test_pick_worker_refused_callback_moves_down_and_records_reason():
    got = pick_worker(['opus', 'sonnet'], refused=lambda c, m: 'opus: max_concurrent 1 reached' if m == 'opus' else None)
    assert got['entry']['model'] == 'sonnet'
    assert got['skipped'] == [{'pattern': 'opus', 'cli': 'claude', 'model': 'opus',
                               'reason': 'opus: max_concurrent 1 reached'}]


def test_pick_worker_card_slots_skip_pi_entries():
    got = pick_worker(['astra', 'sonnet'], card_slots=True)
    assert got['entry']['model'] == 'sonnet'
    assert got['skipped'][0]['reason'] == 'the job has card slots and a pi sub-fleet takes no cards (design §51)'


def test_pick_worker_skipped_lists_only_entries_before_the_pick():
    got = pick_worker(['terra', 'astra', 'sonnet', 'nothing-here'])
    assert got['entry']['model'] == ASTRA
    assert [(r['pattern'], r['cli']) for r in got['skipped']] == [('terra', None)]
    assert got['skipped'][0]['reason'].startswith('no model on this machine matches')


# ------------------------------------------------------------------ pick_supervisor

def pick_supervisor(patterns, **kw):
    kw.setdefault('cached', cache())
    kw.setdefault('registry', registry())
    return job_chain.pick_supervisor(patterns, kw.pop('walled', ()), DISC, **kw)


def test_pick_supervisor_skips_astra_on_pi_and_picks_fable_card():
    got = pick_supervisor(['astra', 'fable'])
    assert got['entry'] == {'cli': 'claude', 'model': 'fable', 'thinking': None}
    assert got['profile'] == 'claude-fable' and got['overrides'] == {'worker_cli': 'claude', 'worker_model': 'fable'}
    assert got['skipped'] == [{'pattern': 'astra', 'cli': 'pi', 'model': ASTRA,
                               'reason': 'runs pi, not claude: a sub-fleet supervisor is an adopted official '
                                         'Claude Code pane (subfleet.check)'}]


def test_pick_supervisor_without_card_gives_card_overrides():
    got = pick_supervisor(['fable'], registry=registry(('claude-opus', 'opus')))
    assert got['profile'] is None and got['overrides'] == {'worker_cli': 'claude', 'worker_model': 'fable'}


def test_pick_supervisor_none_when_chain_has_no_claude_entry():
    got = pick_supervisor(['astra', 'sol'])
    assert got['entry'] is None and got['profile'] is None and got['overrides'] == {}
    assert [r['pattern'] for r in got['skipped']] == ['astra', 'sol']
    assert got['reason'].startswith('no usable entry on the supervisor chain ["astra", "sol"]: astra: pi:')


def test_pick_supervisor_prefers_claude_model_card_id():
    assert pick_supervisor(['opus'])['profile'] == 'claude-opus'
    assert pick_supervisor(['opus'], registry=registry(('zz-opus', 'opus'), ('aa-opus', 'opus')))['profile'] == 'aa-opus'


def test_pick_supervisor_refused_callback_moves_down_the_chain():
    """User 2026-11-22: a refused Fable falls through to Opus (the chain goes on in order)."""
    got = pick_supervisor(['fable', 'opus'], refused=lambda c, m: 'fable: busy (test)' if m == 'fable' else None)
    assert got['entry']['model'] == 'opus' and got['profile'] == 'claude-opus'
    assert got['skipped'] == [{'pattern': 'fable', 'cli': 'claude', 'model': 'fable', 'reason': 'fable: busy (test)'}]


# ------------------------------------------------------------------ explain

STATES = {
    'all': cache(),
    'pi logged out': cache(pi=cli_row([ASTRA, SOL, FLASH], logged_in=False)),
    'claude old': cache(claude=cli_row(['opus', 'fable'], version_ok=False, version='1.0', min='2.1')),
    'codex gateway down': cache(gateways={'openai-codex': {'online': False}}),
    'opencode missing': cache(opencode={'installed': False}),
    'gpt on opencode': cache(opencode=cli_row([FREE, 'openai/gpt-6-astra'])),
    'codex cli': dict(cache(), clis=dict(cache()['clis'], codex=cli_row(['gpt-6-astra']))),
    'no cache': {},
}
PATTERNS = ['astra', 'fable', 'opus', 'sol', 'sonnet', 'terra', 'deepseek flash', 'gpt', 'haiku']


@pytest.mark.parametrize('state', sorted(STATES))
@pytest.mark.parametrize('role', ['worker', 'supervisor'])
def test_explain_agrees_with_discover_chain(state, role):
    for pattern in PATTERNS:
        entries = discover.chain(role, (), dict(DISC, **{role: [pattern]}), STATES[state])[0]
        why = job_chain.explain(pattern, role, DISC, STATES[state])
        assert (why is None) == bool(entries), (state, role, pattern, why, entries)


def test_explain_reasons_text():
    assert job_chain.explain('astra', 'worker', DISC, STATES['codex gateway down']) == (
        'pi:' + ASTRA + ': gateway openai-codex offline')
    assert job_chain.explain('openai gpt', 'worker', DISC, cache(pi=cli_row([]), opencode=cli_row(
        ['openai/gpt-6-astra']))) == 'opencode:openai/gpt-6-astra: only_via sends it to pi'
    assert job_chain.explain('astra', 'worker', DISC, STATES['codex cli']) is None  # pi still has it
    assert 'codex:gpt-6-astra: codex is not in discovery.cli_order' in job_chain.explain(
        'astra', 'worker', DISC, dict(cache(pi=cli_row([])), clis=dict(cache(pi=cli_row([]))['clis'],
                                                                         codex=cli_row(['gpt-6-astra']))))
    assert job_chain.explain('terra', 'worker', DISC, cache()) == (
        'no model on this machine matches (cached CLIs: claude, pi, opencode; `fleetmux setup` probes again)')
    assert job_chain.explain('astra', 'worker', DISC, {}).startswith(
        'no discovery cache: only the model cards were considered (run `fleetmux setup`)')


# ------------------------------------------------------------------ trace

def test_trace_shape_with_skipped_and_switched():
    pick = pick_supervisor(['astra', 'fable'])
    switched = [{'from': 'claude:fable', 'to': 'claude:opus', 'reason': 'model-pool wall', 'at': 1.0}]
    got = job_chain.trace(pick, 'job_subfleet', switched)
    assert got == {'cli': 'claude', 'model': 'fable', 'source': 'job_subfleet', 'profile': 'claude-fable',
                   'skipped': pick['skipped'], 'switched': switched}
    assert job_chain.trace(pick_worker(['astra'], walled=[wall('pi', ASTRA, 'account')]), 'spec')['cli'] is None


def test_trace_for_a_card_slot():
    got = job_chain.trace({'entry': {'cli': 'claude', 'model': 'sonnet'}, 'profile': 'claude-sonnet', 'skipped': []},
                          'card')
    assert got == {'cli': 'claude', 'model': 'sonnet', 'source': 'card', 'profile': 'claude-sonnet', 'skipped': [],
                   'switched': []}


def test_supervisor_wall_trace_text():
    assert job_chain.SUPERVISOR_WALL == 'supervisor wall: stop (adopted pane cannot rotate)'


# ------------------------------------------------------------------ every sub-fleet (named, §44 split): user 2026-11-22

def test_reason_texts_hold_for_every_sub_fleet():
    assert 'job' not in job_chain.NOT_CLAUDE and 'job' not in job_chain.NOT_WORKER


def test_subfleet_supervisor_walks_the_discovery_chain():
    got = job_chain.subfleet_supervisor((), DISC, cache(), registry())
    assert got['chain'] == DISC['supervisor'] and got['source'] == 'discovery'
    assert got['entry']['model'] == 'fable' and got['profile'] == 'claude-fable'
    assert got['overrides'] == {'worker_cli': 'claude', 'worker_model': 'fable'}
    assert [r['pattern'] for r in got['skipped']] == ['astra']
    assert job_chain.trace(got, got['source'])['source'] == 'discovery'


def test_subfleet_supervisor_falls_through_to_opus_when_fable_is_unusable():
    walled = job_chain.subfleet_supervisor([wall('claude', 'fable')], DISC, cache(), registry())
    assert walled['entry']['model'] == 'opus' and walled['profile'] == 'claude-opus'
    refused = job_chain.subfleet_supervisor((), DISC, cache(), registry(),
                                            refused=lambda c, m: 'busy (test)' if m == 'fable' else None)
    assert refused['entry']['model'] == 'opus'
    assert refused['skipped'][-1] == {'pattern': 'fable', 'cli': 'claude', 'model': 'fable', 'reason': 'busy (test)'}


def test_subfleet_supervisor_none_with_reason_when_no_claude_entry():
    got = job_chain.subfleet_supervisor((), dict(DISC, supervisor=['astra', 'sol']), cache(), registry())
    assert got['entry'] is None and got['overrides'] == {} and got['chain'] == ['astra', 'sol']
    assert got['reason'].startswith('no usable entry on the supervisor chain ["astra", "sol"]: astra: pi:')


def test_runs_pick_matches_the_chain_place_not_the_spelling():
    pick = job_chain.subfleet_supervisor((), DISC, cache(), registry())
    assert job_chain.runs_pick(pick, DISC['supervisor'], 'claude', 'fable')
    assert job_chain.runs_pick(pick, DISC['supervisor'], 'claude', 'claude-fable-5-2')
    assert not job_chain.runs_pick(pick, DISC['supervisor'], 'claude', 'opus')
    assert not job_chain.runs_pick(pick, DISC['supervisor'], 'pi', 'fable')
    assert not job_chain.runs_pick({'entry': None}, DISC['supervisor'], 'claude', 'fable')

