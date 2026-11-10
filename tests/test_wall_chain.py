"""Usage wall -> the next usable model on the role's chain (user 2026-11-11: 「所以对于supervisor，撞墙前优先级依次是
Astra, fable, opus sol deepseek flash,对于worker 撞墙前依次是opus, sol,sonnet,terra,deepseek flash」; docs/design.md §23).
The chain is fleet.yaml#discovery read against a test discovery cache (never the operator's); v1 fleets on a fake
Redis / fake cmux, the supervisor switch on the real-Redis v2 fixture."""
import json

import pytest
import yaml

from test_brake import CLAUDE_ACCOUNT_WALL, REAL_FABLE_WALL, busy_worker, control, make  # noqa: F401
from test_brake import isolated_claude_trust, pools_file  # noqa: F401  (autouse fixtures)
from test_fleet import turn
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import event, fx, receive, req, session  # noqa: F401
from cmux_fleet import brake, discover, fleet, runtime_v2

ASTRA, SOL, TERRA = 'openai-codex/gpt-6-astra', 'openai-codex/gpt-6.1-sol', 'openai-codex/gpt-6.1-terra'
DS_PI, DS_FREE = 'workbuddy-ds/workbuddy-ai/deepseek-v4.1-flash-sg', 'opencode/deepseek-v4.1-flash-free'
CACHE = {'version': 1, 'gateways': {}, 'clis': {
    'claude': {'installed': True, 'version_ok': True, 'logged_in': True, 'models': ['fable', 'haiku', 'opus', 'sonnet']},
    'pi': {'installed': True, 'version_ok': True, 'logged_in': True, 'models': [ASTRA, SOL, TERRA, DS_PI]},
    'opencode': {'installed': True, 'version_ok': True, 'logged_in': True, 'models': [DS_FREE]}}}
WORKER_CHAIN = [('claude', 'opus'), ('pi', SOL), ('claude', 'sonnet'), ('pi', TERRA), ('pi', DS_PI), ('opencode', DS_FREE)]
SUPERVISOR_CHAIN = [('pi', ASTRA), ('claude', 'fable'), ('claude', 'opus'), ('pi', SOL), ('pi', DS_PI)]
PI_ACCOUNT_WALL = '429 Too Many Requests: usage limit reached for this account'


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    path = tmp_path / 'discovered.yaml'
    path.write_text(yaml.safe_dump(CACHE))
    monkeypatch.setenv(discover.CACHE_ENV, str(path))
    return path


@pytest.fixture
def pi_bin(tmp_path):
    path = tmp_path / 'bin' / 'pi'
    path.parent.mkdir(exist_ok=True)
    path.write_text('#!/bin/sh\necho 1.5.0\n')
    path.chmod(0o755)
    (tmp_path / 'pi-home').mkdir()
    return {'pi': str(path), 'pi_home': str(tmp_path / 'pi-home'), 'allow_unsandboxed_pi': True}


def log(f, kind):
    return [json.loads(x) for x in f.r.lrange(fleet.LOG, 0, -1) if json.loads(x)['kind'] == kind]


# ------------------------------------------------------------------ the chain (pure)
def test_chain_is_the_discovery_order_re_ranked_from_the_cache_listed_models_only():
    assert discover.chain('worker')[0] == WORKER_CHAIN
    assert discover.chain('supervisor')[0] == SUPERVISOR_CHAIN  # opencode never supervises


def test_chain_without_a_cache_uses_the_model_cards_and_the_runs_own_models(cache):
    cache.unlink()
    entries = discover.chain('worker', [('pi', SOL), ('claude', 'claude-opus-5-5')])[0]
    # claude-opus-5-5 sits where the opus card already is: one entry per place, never the same model twice
    assert entries == [('claude', 'opus'), ('pi', SOL), ('claude', 'sonnet'), ('opencode', DS_FREE)]


def test_place_maps_a_full_model_name_onto_its_chain_entry():
    entries, patterns = discover.chain('worker')
    assert discover.place(entries, patterns, 'claude', 'claude-opus-5-5') == 0
    assert discover.place(entries, patterns, 'pi', SOL) == 1
    assert discover.place(entries, patterns, 'claude', 'haiku') is None  # not on the chain: start at its head


def test_wall_action_skips_walled_accounts_pools_and_refused_pairs():
    walls = [{'cli': 'claude', 'model': 'opus', 'account': 'claude', 'scope': 'account'}]
    below = WORKER_CHAIN[1:]
    assert brake.wall_action('claude', 'opus', below, walls)['to'] == SOL
    refuse_pi = lambda c, m: 'not verified' if c == 'pi' else None  # noqa: E731
    action = brake.wall_action('claude', 'opus', below, walls, refuse_pi)
    assert action['action'] == 'switch' and (action['to_cli'], action['to']) == ('opencode', DS_FREE)
    assert action['skipped'][:2] == ['pi:' + SOL + ' (not verified)', 'claude:sonnet (account wall)']
    pool = [{'cli': 'claude', 'model': 'opus', 'account': 'claude', 'scope': 'model_pool'}]
    assert brake.wall_action('claude', 'opus', below, pool, refuse_pi)['to'] == 'sonnet'  # a pool wall skips one
    refused = [{'cli': 'pi', 'model': SOL, 'account': 'pi:openai-codex', 'scope': 'refused'}]
    assert brake.wall_action('claude', 'opus', below, refused)['skipped'] == ['pi:' + SOL + ' (refused)']
    assert brake.wall_action('opencode', DS_FREE, [], walls)['action'] == 'stop'  # bottom of the chain


def test_a_bridge_cli_account_is_its_provider():
    assert brake.account('pi', SOL) == brake.account('pi', ASTRA) == 'pi:openai-codex'
    assert brake.account('pi', DS_PI) == 'pi:workbuddy-ds' and brake.account('claude', 'opus') == 'claude'
    walls = [{'cli': 'pi', 'model': SOL, 'account': 'pi:openai-codex', 'scope': 'account'}]
    assert brake.wall_action('pi', SOL, WORKER_CHAIN[2:], walls)['to'] == 'sonnet'
    assert brake.wall_action('pi', SOL, WORKER_CHAIN[3:], walls)['to'] == DS_PI  # terra skipped, deepseek not


def test_channel_wording_round_trips_for_a_cli_change():
    tui = ('[Resumed after a usage wall] Route it.\nReply with the service decision JSON from AGENTS.md.\n'
           '\nYour final reply must be exactly one JSON object with the keys: reply (allowed keys: reply). '
           'No text outside the JSON object.')
    tool = runtime_v2.channel_wording(tui, True)
    assert 'Use fleet_decision to route this request.' in tool
    assert tool.endswith('\nUse fleet_result with a business JSON object with keys: reply (allowed keys: reply).')
    assert runtime_v2.channel_wording(tool, False) == tui
    assert runtime_v2.channel_wording(tui, False) == tui  # already worded for that CLI: unchanged


# ------------------------------------------------------------------ workers (v1 fleet)
def test_claude_account_wall_moves_a_worker_to_the_next_cli_on_the_chain(tmp_path, pi_bin):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='claude-opus-5-5', **pi_bin)
    sent = busy_worker(f)
    f.handle(dict(turn('worker-1', CLAUDE_ACCOUNT_WALL), msg_id='m1'))
    req_ = json.loads(f.agent('worker-1')['wall_downgrade'])
    assert (req_['cli'], req_['from'], req_['to_cli'], req_['to']) == ('claude', 'claude-opus-5-5', 'pi', SOL)
    assert json.loads(f.r.hget(fleet.WALLS, 'claude:opus'))['scope'] == 'account'  # recorded on its chain entry
    f.tick()
    cfg = f.runtime_cfg('worker-1')
    assert f.agent('worker-1')['generation'] == '1' and fleet.agent_cli(cfg, 'worker-1') == 'pi'
    assert cfg['pi_model'] == SOL and f.paused() and f.r.lrange('fx:inbox:worker-1', 0, -1) == [sent]
    assert cfg['pi_thinking'] == 'high'  # user 2026-11-11: a worker moved to pi thinks at high, not the run's off
    f.handle(turn('worker-1', 'READY', generation=1))
    f.tick()
    assert not f.paused() and f.agent('worker-1')['wall_downgrade'] == ''
    assert log(f, 'brake.downgraded')[-1]['to'] == SOL and log(f, 'brake.downgraded')[-1]['cli'] == 'pi'
    # The same account walls the next Claude worker: it skips sonnet too (this run's wall record).
    f.on_wall('worker-1', CLAUDE_ACCOUNT_WALL, 'turn')  # now on pi: a Claude wording still means account-level
    assert json.loads(f.agent('worker-1')['wall_downgrade'])['to'] == DS_PI  # pi:openai-codex walled, sonnet skipped


def test_a_refused_cli_change_takes_the_next_model_and_a_spent_chain_stops(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='opus')  # no --allow-unsandboxed-pi
    busy_worker(f)
    f.handle(dict(turn('worker-1', REAL_FABLE_WALL), msg_id='m1'))  # pool wall on opus: next is sol (pi)
    assert json.loads(f.agent('worker-1')['wall_downgrade'])['to'] == SOL
    f.tick()  # pi preflight refuses sol -> sonnet, same CLI, no preflight
    assert f.agent('worker-1')['generation'] == '1' and f.runtime_cfg('worker-1')['worker_model'] == 'sonnet'
    assert log(f, 'brake.switch_refused')[0]['to'] == 'pi:' + SOL
    assert json.loads(f.r.hget(fleet.WALLS, 'pi:' + SOL))['scope'] == 'refused'
    assert json.loads(f.agent('worker-1')['wall'])['to'] == 'sonnet'  # the slot's record follows the re-pick
    f.handle(turn('worker-1', 'READY', generation=1))
    f.tick()
    assert not f.paused()
    f.pump()
    f.setf('worker-1', msg_id='m2')
    f.handle(dict(turn('worker-1', CLAUDE_ACCOUNT_WALL, generation=1), msg_id='m2'))
    f.tick()  # sonnet: account; terra / deepseek on pi and opencode refused by the preflight
    wall = json.loads(f.agent('worker-1')['wall'])
    assert wall['action'] == 'stop' and f.agent('worker-1')['wall_downgrade'] == '' and f.paused()
    assert f.agent('worker-1')['generation'] == '1' and log(f, 'brake.switch_exhausted')
    assert wall['skipped'] == ['pi:' + TERRA + ' (refused)', 'pi:' + DS_PI + ' (refused)',
                               'opencode:' + DS_FREE + ' (refused)']  # opencode: v1 has no opencode workers


def test_a_switch_already_overtaken_by_another_launch_is_dropped_on_the_old_cli_and_model(tmp_path, pi_bin):
    claude = tmp_path / 'bin' / 'claude'   # the switch back checks the binary; CI hosts have no claude
    claude.write_text('#!/bin/sh\nexit 0\n')
    claude.chmod(0o755)
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='opus', claude=str(claude), **pi_bin)
    busy_worker(f)
    f.handle(dict(turn('worker-1', REAL_FABLE_WALL), msg_id='m1'))
    f.tick()  # opus -> sol (pi)
    f.handle(turn('worker-1', 'READY', generation=1))
    f.tick()
    f.pump()
    f.setf('worker-1', msg_id='m2')
    f.handle(dict(turn('worker-1', REAL_FABLE_WALL, generation=1), msg_id='m2'))  # pool wall on sol
    assert json.loads(f.agent('worker-1')['wall_downgrade'])['to'] == 'sonnet'
    f.tick()  # still on pi:sol (the CLI the request names): not superseded, switched back to claude
    assert not log(f, 'brake.downgrade_superseded') and f.agent('worker-1')['generation'] == '2'
    assert fleet.agent_cli(f.runtime_cfg('worker-1'), 'worker-1') == 'claude'


def test_supervisor_of_a_sub_fleet_keeps_stop(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude')
    f.cfg['parent_config'] = str(tmp_path / 'parent.json')
    assert not f.wall_chained('supervisor') and f.wall_chained('worker-1')
    f.on_wall('supervisor', REAL_FABLE_WALL, 'turn')
    assert json.loads(f.agent('supervisor')['wall'])['action'] == 'stop'


# ------------------------------------------------------------------ supervisor (v2, pi -> claude)
def test_supervisor_wall_moves_to_the_next_supervisor_cli_and_rewords_its_queue(fx, tmp_path):
    f = fx  # pi supervisor + pi workers, service mode
    claude_bin = tmp_path / 'bin' / 'claude'
    claude_bin.parent.mkdir(exist_ok=True)
    claude_bin.write_text('#!/bin/sh\nexit 1\n')
    claude_bin.chmod(0o755)
    f.cfg.update(pi_models={'supervisor': ASTRA}, claude=str(claude_bin), claude_supervisor_hook_delivery=True)
    req(f, 'r1', to=None)
    f.pump()
    env = receive(f, 'supervisor')
    event(f, dict(session(f, 'supervisor'), kind='candidate', delivery=env,
                  candidate={'tool': 'model', 'error': PI_ACCOUNT_WALL}))
    event(f, dict(session(f, 'supervisor'), kind='settled', delivery=env, candidates=[], failed=True, aborted=False,
                  unfinished=False))
    assert f.paused() and f.v2.depth('supervisor') == 1
    pending = f.v2.pending_text('supervisor')[0]
    assert 'Use fleet_decision' in pending  # worded for the pi supervisor at queue time
    wanted = json.loads(f.agent('supervisor')['wall_downgrade'])
    assert (wanted['to_cli'], wanted['to'], wanted['key']) == ('claude', 'fable', 'supervisor_model')
    old = f.agent('supervisor')['pane']
    f.tick()
    info = f.agent('supervisor')
    cfg = f.runtime_cfg('supervisor')
    assert info['generation'] == '1' and info['label'] == 'supervisor-1.1' and old in f.mgr.killed
    assert fleet.agent_cli(cfg, 'supervisor') == 'claude' and cfg['supervisor_model'] == 'fable'
    assert 'supervisor-1.1' not in f.r.hgetall(fleet.NAMES)  # worker routing names only
    reworded = f.v2.pending_text('supervisor')[0]
    assert 'Use fleet_decision' not in reworded and 'from AGENTS.md' in reworded
    assert any(x['event'] == 'queued.reworded' for _, x in f.r.xrange(f.v2.bus.audit_key))
    assert f.paused()
    f.setf('supervisor', state='idle', runtime_ready=1)  # the new generation's READY
    f.tick()
    assert not f.paused()
    done = [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x['event'] == 'brake.downgraded']
    assert done and done[-1]['cli'] == 'claude' and done[-1]['to'] == 'fable'


def test_supervisor_skips_a_cli_whose_pair_with_the_workers_is_not_verified(fx):
    f = fx  # pi supervisor (Astra) + pi workers, service; worker-1 now runs opencode (as after its own wall switch)
    f.cfg.update(pi_models={'supervisor': ASTRA})
    f.setf('worker-1', runtime_launch=json.dumps({'profile_id': 'x', 'role_text': '',
                                                  'overrides': {'worker_cli': 'opencode', 'opencode_model': DS_FREE}}))
    assert 'claude supervisor + opencode workers (service) is not in the pair table' in f.wall_refused('supervisor', 'claude')
    assert f.wall_refused('supervisor', 'pi') is None  # pi + opencode and pi + pi service rows are verified
    action = f.wall_decide('supervisor', f.runtime_cfg('supervisor'), {'scope': 'account', 'line': 'x'})
    assert (action['action'], action['to_cli'], action['to']) == ('switch', 'pi', DS_PI)
    assert [s.split(' (')[0] for s in action['skipped']] == ['claude:fable', 'claude:opus', 'pi:' + SOL]
    assert action['skipped'][-1].endswith('(account wall)')  # Astra's account (pi:openai-codex) is Sol's too


# ------------------------------------------------------------------ chains live in Redis (user 2026-11-11, T-47)
def test_daemon_start_loads_both_chains_into_redis_and_walls_read_only_that(fx, cache, monkeypatch):
    f = fx
    f.r.delete(fleet.WALL_CHAINS)
    monkeypatch.setattr(f, 'capture_terminal_theme', lambda: None)
    monkeypatch.setattr(f.v2, 'run', lambda: None)  # the daemon loop itself is not under test
    f.run()  # start and resume both go through Fleet.run
    data = json.loads(f.r.get(fleet.WALL_CHAINS))
    assert [tuple(x) for x in data['worker']] == WORKER_CHAIN and [tuple(x) for x in data['supervisor']] == SUPERVISOR_CHAIN
    assert data['pairs']['pi|claude'] == 'verified' and data['mode'] == 'service'
    cache.unlink()  # the files are never read again while the run is up

    def no_file(*args, **kwargs):
        raise AssertionError('a wall read the config files')
    monkeypatch.setattr(fleet.fleet_config, 'block', no_file)
    monkeypatch.setattr(fleet.discover, 'load', no_file)
    monkeypatch.setattr(fleet.cli_pairs, 'load_pairs', no_file)
    f.cfg.update(pi_models={'supervisor': ASTRA})
    action = f.wall_decide('supervisor', f.runtime_cfg('supervisor'), {'scope': 'account', 'line': 'x'})
    assert (action['to_cli'], action['to']) == ('claude', 'fable')


def test_a_run_without_the_key_resolves_the_chains_once_on_its_first_wall(tmp_path, pi_bin, cache):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='opus', **pi_bin)
    assert f.r.get(fleet.WALL_CHAINS) is None  # an old run resumed under this code
    busy_worker(f)
    f.handle(dict(turn('worker-1', REAL_FABLE_WALL), msg_id='m1'))
    assert [tuple(x) for x in json.loads(f.r.get(fleet.WALL_CHAINS))['worker']] == WORKER_CHAIN
    cache.write_text(yaml.safe_dump(dict(CACHE, clis={'claude': CACHE['clis']['claude']})))  # a later probe
    f.on_wall('worker-1', REAL_FABLE_WALL, 'turn')
    assert json.loads(f.agent('worker-1')['wall_downgrade'])['to'] == SOL  # still the chain in Redis


def test_worker_default_thinking_is_high(tmp_path):
    """User 2026-11-11 「默认worker也是high」: fleet.yaml#discovery.worker_thinking = high; it fills the run-wide pi
    thinking and a Claude worker's --effort when nothing names its own; an explicit level still wins."""
    from cmux_fleet import fleet_config
    assert (fleet_config.block('discovery', fleet_config.SHIPPED) or {}).get('worker_thinking') == 'high'
    assert fleet.worker_thinking() == 'high'
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='claude-opus-5-5')
    snap = fleet.load(f.path)
    worker = fleet.claude_argv(f.path, snap, 'worker-1', 0, 'hi')
    assert worker[worker.index('--effort') + 1] == 'high'
    named = fleet.claude_argv(f.path, dict(snap, worker_effort='low', advisor=True), 'worker-1', 0, 'hi')
    assert named[named.index('--effort') + 1] == 'low'  # an advised slot keeps the advisor's level
    assert '--effort' not in fleet.claude_argv(f.path, snap, 'supervisor', 0, 'hi')
    assert snap.get('pi_thinking') in (None, 'high')  # a start without --pi-thinking records high, never off
