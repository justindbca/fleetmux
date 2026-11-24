"""Job sub-fleet models come from model chains the program reads from config (docs/design.md §58 "Model chains").

The program, not the model, picks the order of supervisor and worker models.
Incident (root fleet ops, f95f6642): a job roster row `{name: astra-1, pi_model: openai-codex/gpt-6-astra}` was
accepted at submit, refused when the sub-fleet came up (the child inherited the root's `worker_cli: claude`), and the
slot then ran the default Claude model. Now: the chains (job spec > fleet.yaml#job_subfleet > fleet.yaml#discovery)
are resolved at submit and written into the child config at start; nothing falls back to a default model.

Only private test Redis, fake cmux, fake CLIs and a test discovery cache (never the operator's)."""
import json
from pathlib import Path

import pytest
import yaml

from test_escalate import ok_check  # noqa: F401  (fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_service_escalate import escalations, result, svc  # noqa: F401
from test_service_split import capture_child_specs, finish_child, queued_to, split_tree  # noqa: F401
from test_service_job import GOAL, job_rec, jobs, submit
from test_subfleet import adopt_by_hook, child_daemon, given, hook, step
from fleetmux import discover, fleet, fleet_config, join, profile_launch, subfleet
from fleetmux.agent_registry import AgentRegistry

ASTRA, SOL, TERRA = 'openai-codex/gpt-6-astra', 'openai-codex/gpt-6.1-sol', 'openai-codex/gpt-6.1-terra'
DS_PI = 'workbuddy-ds/workbuddy-ai/deepseek-v4.1-flash-sg'
CACHE = {'version': 1, 'gateways': {}, 'clis': {
    'claude': {'installed': True, 'version_ok': True, 'logged_in': True, 'models': ['fable', 'haiku', 'opus', 'sonnet']},
    'pi': {'installed': True, 'version_ok': True, 'logged_in': True, 'models': [ASTRA, SOL, TERRA, DS_PI]}}}
POOL_WALL = "You've reached your Opus limit"


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    """The discovery cache of this machine: claude (fable, haiku, opus, sonnet) and pi (Astra, Sol, Terra, DeepSeek)."""
    path = tmp_path / 'discovered.yaml'
    path.write_text(yaml.safe_dump(CACHE))
    monkeypatch.setenv(discover.CACHE_ENV, str(path))
    return path


def private(tmp_path, monkeypatch, **job_subfleet):
    """The private fleet.yaml overrides the job_subfleet block as a whole."""
    path = tmp_path / 'private-fleet.yaml'
    path.write_text(yaml.safe_dump({'job_subfleet': job_subfleet}))
    monkeypatch.setenv(fleet_config.USER_ENV, str(path))
    return path


def pi_ok(f, tmp_path):
    """The root fleet may run pi workers (§51 gate): a pi binary and --allow-unsandboxed-pi."""
    pi = tmp_path / 'bin' / 'pi'
    pi.parent.mkdir(exist_ok=True)
    pi.write_text('#!/bin/sh\necho 1.5.0\n')
    pi.chmod(0o755)
    (tmp_path / 'pi-agent').mkdir(exist_ok=True)
    f.cfg.update(pi=str(pi), allow_unsandboxed_pi=True, pi_agent_dir=str(tmp_path / 'pi-agent'))


def roster_file(tmp_path, rows, name='roster.yaml'):
    path = tmp_path / name
    path.write_text(yaml.safe_dump({'workers': rows}))
    return path


def refused(f, spec, cid):
    answer = submit(f, spec, cid=cid)
    assert answer['status'] == 'rejected', answer
    return answer['reason']


def drive(t, spec, cid='cmd-job'):
    """Submit job ``spec`` and run it until its sub-fleet is delegated: rotation (handoff note, READY of the next
    generation) when the slot's model is not the chain's pick, in place otherwise. Returns (job id, M<n>, child cfg)."""
    f = t.f
    answer = submit(f, spec, t=t, cid=cid)
    assert answer['status'] == 'accepted', answer
    jid = answer['job_id']
    f.tick()
    key, rec = job_rec(f, jid)
    assert rec, jobs(f)[jid]
    if rec['status'] == 'rotating':
        f.tick()
        step(t)
        ask = given(t, f, rec['slot'])[-1]
        assert hook(t, f, rec['slot'], 'handoff written', prompt=ask)['status'] in ('committed', 'handoff')
        step(t)
        assert hook(t, f, rec['slot'], 'READY', prompt='startup')['status'] == 'ready'
    for _ in range(2):
        f.tick()
        step(t)
    key, rec = job_rec(f, jid)
    assert rec['status'] == 'delegated', escalations(f)
    return jid, key, fleet.load(f.run_dir / 'sub' / key / 'config.json')


def slot_launch(f, slot='worker-1'):
    return json.loads(f.agent(slot).get('runtime_launch') or '{}')


# ------------------------------------------------------------------ brief §5.3: the six required cases
def test_roster_row_with_pi_model_is_refused_at_submit_and_points_to_worker_chain(svc, ok_check, tmp_path):
    """Incident replay: the 11-21 / 11-22 roster row with pi_model / pi_thinking."""
    f = svc
    path = roster_file(tmp_path, [{'name': 'astra-1', 'pi_model': ASTRA, 'pi_thinking': 'high'}])
    why = refused(f, {'goal': GOAL, 'roster': str(path)}, 'cmd-pi-row')
    assert 'astra-1' in why and 'pi_model' in why and 'pi_thinking' in why
    assert '"worker": ["astra"]' in why and why.endswith('Nothing was submitted.')
    assert not jobs(f)


def test_spec_worker_astra_writes_pi_worker_cli_and_pi_model_into_child_config(split_tree, tmp_path):
    t, f = split_tree, split_tree.f
    pi_ok(f, tmp_path)
    jid, key, cfg = drive(t, {'goal': GOAL, 'worker': ['astra']})
    assert cfg['worker_cli'] == 'pi' and cfg['pi_model'] == ASTRA
    assert cfg['pi_thinking'] == (fleet_config.block('discovery').get('worker_thinking') or 'high')
    assert cfg['pi_home'] == str(f.run_dir / 'sub' / key / 'pi-home')  # its own pi home (§51)
    assert cfg['job_chain']['worker'] == ['astra']
    assert cfg['model_chain']['workers']['worker-1']['cli'] == 'pi'
    assert cfg['model_chain']['workers']['worker-1']['model'] == ASTRA
    assert cfg['model_chain']['workers']['worker-1']['source'] == 'spec'


def test_config_supervisor_chain_skips_astra_with_reason_and_starts_fable(split_tree, tmp_path, monkeypatch):
    t, f = split_tree, split_tree.f
    private(tmp_path, monkeypatch, supervisor=['astra', 'fable'])
    jid, key, cfg = drive(t, {'goal': GOAL})
    launch = slot_launch(f)
    assert launch['overrides'] == {'worker_cli': 'claude', 'worker_model': 'fable'}
    assert launch['profile_id'] == 'claude-fable'  # the card of that model (D16: id claude-<model> first)
    assert cfg['supervisor_model'] == 'fable'
    sup = cfg['model_chain']['supervisor']
    assert (sup['cli'], sup['model'], sup['source'], sup['profile']) == ('claude', 'fable', 'job_subfleet', 'claude-fable')
    assert sup['skipped'][0]['pattern'] == 'astra' and 'not claude' in sup['skipped'][0]['reason']
    # D16: once adopted, the supervisor runs the sub-fleet supervisor rules, not the card's role file (worker.md)
    cf, _ = adopt_by_hook(t, monkeypatch, subfleet.record(f.run_dir / 'sub' / key))
    step(t)
    scope = (Path(cf.cfg['supervisor_cwd']) / 'AGENTS.md').read_text()
    assert '# Worker: do exactly the one instruction' not in scope


def test_chain_without_usable_entry_is_refused_at_submit(svc, ok_check, cache):
    f = svc
    why = refused(f, {'goal': GOAL, 'worker': ['gemini']}, 'cmd-w')
    assert 'worker chain ["gemini"]' in why and 'gemini' in why and why.endswith('Nothing was submitted.')
    why = refused(f, {'goal': GOAL, 'supervisor': ['astra', 'sol']}, 'cmd-s')
    assert 'no usable Claude entry' in why and 'astra' in why and 'sol' in why
    cache.unlink()  # D21: no discovery cache; only the model cards count, and terra has no card
    why = refused(f, {'goal': GOAL, 'worker': ['terra']}, 'cmd-nc')
    assert 'no discovery cache' in why and 'fleetmux setup' in why
    assert not jobs(f)


def test_chain_walled_after_submit_keeps_job_queued_with_reason_and_no_default(split_tree):
    t, f = split_tree, split_tree.f
    answer = submit(f, {'goal': GOAL, 'worker': ['opus']}, t=t)
    assert answer['status'] == 'accepted', answer
    jid = answer['job_id']
    f.r.hset(fleet.WALLS, mapping={'claude:opus': json.dumps({'cli': 'claude', 'model': 'opus', 'scope': 'model_pool',
                                                             'account': 'claude', 'at': 0, 'agent': 'worker-2'})})
    gen = f.agent('worker-1')['generation']
    f.tick()
    job = jobs(f)[jid]
    assert job['state'] == 'queued' and 'model-pool wall' in job['waiting'] and 'opus' in job['waiting']
    assert job_rec(f, jid) == (None, None) and not job.get('hot_slot')  # no split, no added worker
    assert f.agent('worker-1')['generation'] == gen and f.agent('worker-1')['state'] == 'idle'
    assert fleet.job_rows(f.r)[0]['waiting'] == job['waiting']
    f.r.hdel(fleet.WALLS, 'claude:opus')  # the quota came back: the job starts on its own chain
    f.tick()
    assert job_rec(f, jid)[1] is not None


def test_child_roster_rejected_at_start_fails_the_job_and_does_not_run_on(split_tree, tmp_path):
    """D18 main path: the parent checks the job roster against the child config before the child starts. The
    caller's file changed after submit (the child copies it at start); the job fails at once, no retry."""
    t, f = split_tree, split_tree.f
    path = roster_file(tmp_path, [{'name': 'coder-1'}, {'name': 'tester-1'}])
    answer = submit(f, {'goal': GOAL, 'roster': str(path), 'supervisor': ['sonnet'], 'worker': ['opus']}, t=t)
    assert answer['status'] == 'accepted', answer
    jid = answer['job_id']
    path.write_text(yaml.safe_dump({'workers': [{'name': 'coder-1', 'pi_model': ASTRA}, {'name': 'tester-1'}]}))
    for _ in range(3):
        f.tick()
        step(t)
    got = result(f, jid)
    assert got and got['status'] == 'failed' and 'roster' in got['error'] and 'coder-1' in got['error']
    assert jid not in jobs(f) and not subfleet.active_children(f.run_dir)
    assert f.agent('worker-1')['state'] in ('idle', 'starting')
    assert not queued_to(f, 'supervisor', '[Roster rejected]')


def test_child_first_roster_read_rejected_fails_the_child_task(split_tree, tmp_path):
    """D18 backstop: the child daemon's first roster read refuses the roster (its run copy changed after the parent
    checked it). The child task fails at once; the supervisor gets no [Roster rejected] line to wave it through.
    Later hot edits that the reload refuses keep the old behaviour (only the supervisor is told)."""
    t, f = split_tree, split_tree.f
    path = roster_file(tmp_path, [{'name': 'coder-1'}, {'name': 'tester-1'}])
    jid, key, cfg = drive(t, {'goal': GOAL, 'roster': str(path), 'supervisor': ['sonnet'], 'worker': ['opus']})
    Path(cfg['roster']).write_text(yaml.safe_dump({'workers': [{'name': 'coder-1', 'pi_model': ASTRA},
                                                               {'name': 'tester-1'}]}))
    cf = child_daemon(t, subfleet.record(f.run_dir / 'sub' / key))
    for _ in range(3):
        cf.tick()
        step(t)
    task = cf.task()
    assert task['status'] == 'failed' and 'roster' in task['reason'] and 'coder-1' in task['reason']
    assert not cf.r.hget(fleet.ROSTER, 'rejected')
    assert not queued_to(cf, 'supervisor', '[Roster rejected]')


def test_job_without_chain_follows_discovery_chains(split_tree):
    """A job that names no chain and no card takes fleet.yaml#discovery (brief §2.1, D15).

    Difference from before this change: the sub-fleet supervisor used to keep the slot's own model (adopted in place)
    and the sub-fleet workers ran the root's default worker model. Now the supervisor is the first usable Claude entry
    of discovery.supervisor (Astra runs pi, so Fable on most machines). Fable has no concurrency cap (D24), so jobs
    that start together all get Fable; when Fable hit a wall or is refused (a quota), the next entry, Opus, supervises
    (D13, D26). The workers run the first usable entry of discovery.worker (Opus). This costs one rotation of the slot,
    unless it already runs the picked model (then it is adopted in place)."""
    t, f = split_tree, split_tree.f
    jid, key, cfg = drive(t, {'goal': GOAL})
    assert slot_launch(f)['overrides'] == {'worker_cli': 'claude', 'worker_model': 'fable'}
    assert cfg['worker_cli'] == 'claude' and cfg['worker_model'] == 'opus'
    chain = cfg['model_chain']
    assert chain['supervisor']['source'] == 'discovery' and chain['supervisor']['skipped'][0]['pattern'] == 'astra'
    assert all(w['source'] == 'discovery' and w['model'] == 'opus' for w in chain['workers'].values())


# ------------------------------------------------------------------ rulings D3 / D5 / D10 / D11 / D12 / D14 / D15
def test_spec_supervisor_chain_with_supervisor_profile_is_refused(svc, ok_check):
    why = refused(svc, {'goal': GOAL, 'supervisor': ['fable'], 'supervisor_profile': 'claude-opus'}, 'cmd-both')
    assert 'sets both supervisor' in why and 'give one of them' in why


def test_config_block_with_both_supervisor_profile_and_chain_refuses_jobs(svc, ok_check, tmp_path, monkeypatch):
    private(tmp_path, monkeypatch, supervisor=['fable'], supervisor_profile='claude-opus')
    why = refused(svc, {'goal': GOAL}, 'cmd-cfg-both')
    assert 'job_subfleet sets both supervisor' in why and 'set one of them to null' in why
    assert not jobs(svc)


def test_config_supervisor_profile_keeps_its_card_and_a_spec_chain_overrides_it(split_tree, tmp_path, monkeypatch):
    t, f = split_tree, split_tree.f
    private(tmp_path, monkeypatch, supervisor_profile='claude-opus')
    jid, key, cfg = drive(t, {'goal': GOAL})
    assert slot_launch(f)['profile_id'] == 'claude-opus'
    sup = cfg['model_chain']['supervisor']
    assert sup['profile'] == 'claude-opus' and sup['source'] == 'card'
    finish_child(t, key, 'completed')
    jid, key, cfg = drive(t, {'goal': GOAL, 'supervisor': ['haiku']}, cid='cmd-job-2')  # the spec chain wins
    assert cfg['model_chain']['supervisor']['model'] == 'haiku'
    assert cfg['model_chain']['supervisor']['source'] == 'spec'


def test_empty_chain_list_is_refused(svc, ok_check):
    for key in ('worker', 'supervisor'):
        why = refused(svc, {'goal': GOAL, key: []}, 'cmd-empty-' + key)
        assert key in why and '[]' in why


def test_roster_profile_row_keeps_its_card_and_other_rows_take_the_chain(split_tree, tmp_path):
    t, f = split_tree, split_tree.f
    pi_ok(f, tmp_path)
    path = roster_file(tmp_path, [{'name': 'coder-1', 'profile': 'claude-haiku'}, {'name': 'tester-1'}])
    jid, key, cfg = drive(t, {'goal': GOAL, 'roster': str(path), 'worker': ['astra', 'opus']})
    assert cfg['worker_profiles'] == {'coder-1': 'claude-haiku'}
    assert cfg['worker_cli'] == 'claude' and cfg['worker_model'] == 'opus'  # D14: no pi next to a card slot
    workers = cfg['model_chain']['workers']
    assert workers['coder-1']['source'] == 'card' and workers['coder-1']['profile'] == 'claude-haiku'
    assert workers['tester-1']['model'] == 'opus'
    assert workers['tester-1']['skipped'][0]['pattern'] == 'astra' and 'card' in workers['tester-1']['skipped'][0]['reason']


def test_root_worker_cli_and_worker_model_do_not_override_the_chain(split_tree, tmp_path):
    t, f = split_tree, split_tree.f
    pi_ok(f, tmp_path)
    f.cfg.update(pi_models={'worker-1': SOL, 'worker-2': SOL}, pi_thinkings={'worker-1': 'low'})
    jid, key, cfg = drive(t, {'goal': GOAL, 'worker': ['astra'], 'supervisor': ['sonnet']})
    assert cfg['worker_cli'] == 'pi' and cfg['pi_model'] == ASTRA
    assert not cfg.get('pi_models') and not cfg.get('pi_thinkings')  # the root's per-slot pi tables never leak


def test_overrides_worker_model_is_a_one_entry_worker_chain(split_tree, tmp_path):
    """D10: `start --worker-model X` forwarded by a join stub = the spec chain [X]."""
    t, f = split_tree, split_tree.f
    why = refused(f, {'goal': GOAL, 'worker': ['opus'], 'overrides': {'worker_model': 'haiku'}}, 'cmd-wm-both')
    assert 'overrides.worker_model' in why
    jid, key, cfg = drive(t, {'goal': GOAL, 'supervisor': ['sonnet'], 'overrides': {'worker_model': 'haiku'}})
    assert cfg['worker_cli'] == 'claude' and cfg['worker_model'] == 'haiku'
    assert cfg['model_chain']['workers']['worker-1']['source'] == 'spec'


def test_pi_pick_needs_allow_unsandboxed_pi(svc, ok_check):
    f = svc
    f.cfg['allow_unsandboxed_pi'] = False
    why = refused(f, {'goal': GOAL, 'worker': ['astra']}, 'cmd-pi-gate')
    assert 'allow-unsandboxed-pi' in why or 'pi binary' in why
    answer = submit(f, {'goal': GOAL, 'worker': ['astra', 'opus']}, cid='cmd-pi-next')
    assert answer['status'] == 'accepted', answer  # astra is skipped with its reason, opus remains


# ------------------------------------------------------------------ trace (brief §2.7, D8, D9, D17)
def test_model_chain_in_child_config_result_and_status_row(split_tree):
    t, f = split_tree, split_tree.f
    jid, key, cfg = drive(t, {'goal': GOAL, 'supervisor': ['astra', 'fable'], 'worker': ['opus']})
    row = fleet.job_rows(f.r)[0]
    assert row['model_chain']['supervisor']['model'] == 'fable'
    assert row['model_chain']['workers']['worker-1']['model'] == 'opus'
    assert row['model_chain'] == cfg['model_chain']
    finish_child(t, key, 'completed')
    child = fleet.load(f.run_dir / 'sub' / key / 'result.json')
    assert child['model_chain']['supervisor']['skipped'][0]['pattern'] == 'astra'
    assert result(f, jid)['result']['model_chain']['workers']['worker-1']['model'] == 'opus'


def test_worker_wall_in_job_child_walks_the_job_chain_and_is_recorded(split_tree, monkeypatch):
    t, f = split_tree, split_tree.f
    jid, key, cfg = drive(t, {'goal': GOAL, 'supervisor': ['sonnet'], 'worker': ['opus', 'haiku']})
    assert cfg['job_chain'] == {'supervisor': ['sonnet'], 'worker': ['opus', 'haiku']}
    cf, _ = adopt_by_hook(t, monkeypatch, subfleet.record(f.run_dir / 'sub' / key))
    data = cf.wall_chains_load()  # D9: the job's patterns replace fleet.yaml#discovery's
    assert data['patterns']['worker'] == ['opus', 'haiku']
    assert data['worker'] == [['claude', 'opus'], ['claude', 'haiku']] or data['worker'] == [('claude', 'opus'),
                                                                                           ('claude', 'haiku')]
    action = cf.wall_decide('worker-1', cf.runtime_cfg('worker-1'), {'scope': 'model_pool', 'line': POOL_WALL})
    assert action['action'] == 'switch' and (action['to_cli'], action['to']) == ('claude', 'haiku')
    req = dict(action, key='worker_model', from_key='worker_model', source='turn', at='a1')
    cf.wall_switched('worker-1', req)
    chain = cf.model_chain()
    assert chain['workers']['worker-1']['switched'][-1]['from'] == 'claude:opus'
    assert chain['workers']['worker-1']['switched'][-1]['to'] == 'claude:haiku'
    # D8: the adopted supervisor only stops on a wall, and the trace says so
    stop = cf.on_wall('supervisor', POOL_WALL, 'turn')
    assert stop is not None
    assert cf.model_chain()['supervisor']['switched'][-1]['reason'] == 'supervisor wall: stop (adopted pane cannot rotate)'


def test_join_stub_status_shows_model_chain(split_tree, monkeypatch, capsys):
    t, f = split_tree, split_tree.f
    jid, key, cfg = drive(t, {'goal': GOAL, 'supervisor': ['sonnet'], 'worker': ['opus']})
    capsys.readouterr()
    fleet.status(f.cfg)  # what `fleetmux status --config <root>` prints
    out = capsys.readouterr().out
    monkeypatch.setattr(join, 'fleet_cli', lambda args, **kw: (0, out, ''))
    row, item = join.job_view({'root_config': str(f.path)}, jid)
    assert row['model_chain']['workers']['worker-1']['model'] == 'opus'


def test_card_less_supervisor_launch_counts_its_model(split_tree, fable_capped):
    """D16: a Claude model on the supervisor chain with no card starts with the overrides a card would give, and its
    model card's max_concurrent still counts it (no card is no way around the quota). The cap is a stub (Fable at 1,
    fixture fable_capped); the shipped Fable card has none (D24)."""
    t, f = split_tree, split_tree.f
    launch, why = f.split_supervisor_launch('worker-1', None, 'fable')
    assert why is None and launch == {'profile_id': '', 'role_text': '',
                                      'overrides': {'worker_cli': 'claude', 'worker_model': 'fable'}}
    f.setf('worker-1', runtime_launch=json.dumps(launch))
    assert f.model_usage().slots['worker-1'] == (None, 'fable')  # counted as fable, not as the fleet default
    launch, why = f.split_supervisor_launch('worker-2', None, 'fable')  # stubbed max_concurrent 1, held by worker-1
    assert launch is None and 'fable' in why


# ------------------------------------------------------------------ D24 (Fable has no slot cap) and D13 / D26
def test_two_concurrent_jobs_both_run_fable_supervisors(split_tree):
    """D24: Fable has no concurrency cap; two jobs that start together both get a Fable supervisor."""
    t, f = split_tree, split_tree.f
    one = drive(t, {'goal': GOAL}, cid='cmd-one')
    two = drive(t, {'goal': GOAL + ' (second)'}, cid='cmd-two')
    slots = {job_rec(f, jid)[1]['slot'] for jid, _, _ in (one, two)}
    assert slots == {'worker-1', 'worker-2'}
    for slot in slots:
        assert json.loads(f.agent(slot)['runtime_launch'])['overrides']['worker_model'] == 'fable'
    assert all(cfg['model_chain']['supervisor']['model'] == 'fable' for _, _, cfg in (one, two))


def test_job_without_chain_falls_to_opus_while_fable_is_walled(split_tree):
    """D13 / D26: Fable hit its wall, so the next entry of the
    supervisor chain, Opus, supervises; the trace says why Fable was skipped."""
    t, f = split_tree, split_tree.f
    f.r.hset(fleet.WALLS, mapping={'claude:fable': json.dumps({'cli': 'claude', 'model': 'fable', 'scope': 'model_pool',
                                                              'account': 'claude', 'at': 0, 'agent': 'worker-9'})})
    jid, key, cfg = drive(t, {'goal': GOAL})
    assert json.loads(f.agent('worker-1')['runtime_launch'])['overrides']['worker_model'] == 'opus'
    sup = cfg['model_chain']['supervisor']
    assert sup['model'] == 'opus' and sup['source'] == 'discovery'
    reasons = {r['pattern']: r['reason'] for r in sup['skipped']}
    assert reasons['fable'] == 'model-pool wall' and 'not claude' in reasons['astra']
    assert fleet.job_rows(f.r)[0]['model_chain']['supervisor']['skipped'] == sup['skipped']


def test_job_without_chain_falls_to_opus_while_fable_is_full(split_tree, fable_capped):
    """D13 / D26 with a quota: Fable at a stubbed max_concurrent 1, held by busy worker-2; worker-1's supervisor is
    Opus, and the trace names Fable's quota."""
    t, f = split_tree, split_tree.f
    registry = AgentRegistry()
    launch = profile_launch.resolve_launch(registry.get('claude-fable'), registry, f.cfg)
    f.setf('worker-2', runtime_launch=profile_launch.encode_launch(launch), state='busy')
    jid, key, cfg = drive(t, {'goal': GOAL})
    assert job_rec(f, jid)[1]['slot'] == 'worker-1'
    sup = cfg['model_chain']['supervisor']
    assert sup['model'] == 'opus'
    reasons = {r['pattern']: r['reason'] for r in sup['skipped']}
    assert 'max_concurrent 1' in reasons['fable']

