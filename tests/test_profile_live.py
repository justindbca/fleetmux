"""Live profile lifecycle, failure fencing, and per-generation launch mapping."""
import json
import shlex
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from cmux_fleet import fleet, pi_adapter, profile_launch
from cmux_fleet.agent_registry import AgentRegistry
from test_fleet import decision, fx, received, traffic, turn
from test_profile_integration import dispatch
from test_pi import pfx


@pytest.fixture
def live(fx, monkeypatch, tmp_path):
    fx.finish('cancelled', 'replace fixture task')
    fx.cfg.update(allow_profile_live_switch=True, allow_unsandboxed_pi=True,
                  pi_model='local/default', pi_home=str(tmp_path / 'pi-home'))
    Path(fx.cfg['pi_home']).mkdir()
    monkeypatch.setattr(fleet, 'CLAUDE_JSON', tmp_path / 'claude.json')
    monkeypatch.setattr(fx, 'preflight_profile', lambda launch: None)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: fx.r)
    fx.submit({'goal': 'implement dynamic router', 'auto_agents': True, 'apply_profiles_live': True})
    return fx


def ready(f, **extra):
    info = f.agent('worker-1')
    f.handle(dict(turn('worker-1', 'READY', int(info['generation'])), **extra))


def test_switch_waits_for_new_generation_ready_and_preserves_slot(live):
    f = live
    old = f.agent('worker-1')
    dispatch(f)
    assert f.agent('worker-1')['current_profile'] == ''
    queued = f.r.lrange('fx:inbox:worker-1', 0, -1)
    sent = traffic(f.mgr)
    f.deliver('worker-1')
    info = f.agent('worker-1')
    assert info['state'] == 'starting' and info['profile_state'] == 'switching'
    assert info['runtime_profile'] == 'flashnext-coder' and info['current_profile'] == ''
    assert info['generation'] == '1' and info['label'] == 'worker-1.1'
    assert f.resolve('worker-1') == f.resolve('worker-1.1') == 'worker-1'
    assert f.r.hgetall('fx:agent:flashnext-coder') == {}
    assert old['pane'] in f.mgr.killed
    assert f.mgr.tabbed[-1] == (info['pane'], old['pane'])
    assert f.mgr.spawned[-1]['metadata']['cli_type'] == 'pi'
    assert '--generation 1' in f.mgr.spawned[-1]['cli_command']
    f.deliver('worker-1')
    f.handle(turn('worker-1', 'READY', 0))
    assert f.agent('worker-1') == info  # old READY cannot satisfy new startup
    assert traffic(f.mgr) == sent and f.r.lrange('fx:inbox:worker-1', 0, -1) == queued
    ready(f)
    assert f.agent('worker-1')['profile_state'] == 'ready'
    assert f.agent('worker-1')['current_profile'] == 'flashnext-coder'
    assert f.r.llen('fx:inbox:worker-1') == 1
    f.deliver('worker-1')
    assert f.agent('worker-1')['profile_state'] == 'applied-live'
    assert f.agent('worker-1')['state'] == 'busy'
    assert f.agent('worker-1')['last_msg'].startswith('[Applied runtime profile: flashnext-coder]')
    assert '.inbox/' in f.mgr.sent[-1][1]
    before = f.agent('worker-1')
    f.handle(turn('worker-1', 'old result', 0))
    assert f.agent('worker-1') == before


@pytest.mark.parametrize('state', ['busy', 'handoff', 'starting', 'dead'])
def test_switch_never_runs_outside_idle(live, state):
    live.setf('worker-1', state=state)
    dispatch(live)
    count = live.mgr.n
    live.deliver('worker-1')
    assert live.mgr.n == count and live.agent('worker-1')['generation'] == '0'
    assert live.agent('worker-1')['profile_state'] == 'pending'


def test_config_permission_alone_remains_metadata(live):
    live.finish('cancelled', 'replace task')
    live.submit({'goal': 'implement router', 'auto_agents': True})
    dispatch(live)
    live.deliver('worker-1')
    assert live.agent('worker-1')['generation'] == '0'
    assert live.agent('worker-1')['profile_state'] == 'applied-metadata'
    assert live.agent('worker-1')['current_profile'] == live.agent('worker-1')['runtime_profile'] == ''


def test_live_requires_assignments(live):
    live.finish('cancelled', 'replace task')
    with pytest.raises(ValueError, match='requires auto_agents'):
        live.submit({'goal': 'do work', 'apply_profiles_live': True})


@pytest.mark.parametrize('failure', ['retire', 'tab', 'spawn'])
def test_failure_blocks_and_late_ready_cannot_resurrect(live, monkeypatch, failure):
    f = live
    old = f.agent('worker-1')['pane']
    if failure == 'retire':
        kill = f.mgr.kill_pane
        monkeypatch.setattr(f.mgr, 'kill_pane', lambda pane: False if pane == old else kill(pane))
    elif failure == 'tab':
        monkeypatch.setattr(f.mgr, 'tab_into', lambda *args: False)
    else:
        def fail(*args, **kwargs):
            raise RuntimeError('spawn unavailable')
        monkeypatch.setattr(f.mgr, 'spawn_interactive_cli', fail)
    dispatch(f)
    f.deliver('worker-1')
    assert f.task()['status'] == f.agent('worker-1')['profile_state'] == 'blocked'
    assert f.agent('worker-1')['state'] == 'dead'
    assert f.agent('worker-1')['launch_ready'] == '0'
    assert not traffic(f.mgr)
    before = f.agent('worker-1')
    ready(f)
    assert f.agent('worker-1') == before
    assert old in f.mgr.alive


@pytest.mark.parametrize('text,flags', [('not ready', {}), ('READY', {'failed': True}), ('READY', {'aborted': True})])
@pytest.mark.parametrize('profile', ['flashnext-coder', 'claude-reviewer'])
def test_strict_ready_and_outcomes_for_each_switched_cli(live, profile, text, flags):
    dispatch(live, profile)
    live.deliver('worker-1')
    live.handle(dict(turn('worker-1', text, 1), **flags))
    assert live.task()['status'] == 'blocked'
    assert live.agent('worker-1')['current_profile'] == ''
    assert live.agent('worker-1')['state'] == 'dead'


def test_ready_timeout_cannot_be_disabled_by_stall_setting(live):
    dispatch(live)
    live.deliver('worker-1')
    live.cfg['stall_timeout'] = 0
    live.setf('worker-1', profile_ready_deadline=time.time() - 1)
    live.tick()
    assert live.task()['status'] == 'blocked'
    ready(live)
    assert live.agent('worker-1')['state'] == 'dead'


@pytest.mark.parametrize('change', ['assignment', 'permission', 'registry', 'task'])
def test_ready_revalidates_queue_before_delivery(live, change, monkeypatch):
    dispatch(live)
    live.deliver('worker-1')
    ready(live)
    if change == 'assignment':
        live.r.hset(fleet.TASK, mapping={'profile_assignments': '{"assignments":[]}'})
    elif change == 'permission':
        live.cfg['allow_profile_live_switch'] = False
    elif change == 'registry':
        registry = AgentRegistry()
        registry._cards = ()
        monkeypatch.setattr(fleet, 'AgentRegistry', lambda *args: registry)
    else:
        live.r.hset(fleet.TASK, mapping={'id': 'different-task'})
    live.deliver('worker-1')
    assert live.task()['status'] == 'blocked' and not traffic(live.mgr)


def test_registry_model_drift_does_not_claim_applied(live, monkeypatch):
    dispatch(live)
    live.deliver('worker-1')
    ready(live)
    registry = AgentRegistry()
    registry._cards = tuple(replace(c, model='local/changed') if c.cli == 'pi' else c for c in registry)
    monkeypatch.setattr(fleet, 'AgentRegistry', lambda *args: registry)
    live.deliver('worker-1')
    assert live.task()['status'] == 'blocked'
    assert 'changed since spawn' in live.task()['reason'] and not traffic(live.mgr)


def test_fifo_switches_to_official_claude_and_reuses_ready_runtime(live, monkeypatch):
    f = live
    dispatch(f)
    dispatch(f, 'claude-reviewer')
    f.deliver('worker-1')
    ready(f)
    f.deliver('worker-1')
    f.handle(dict(turn('worker-1', 'done', 1), msg_id=f.agent('worker-1')['msg_id']))
    f.deliver('worker-1')
    assert f.agent('worker-1')['generation'] == '2'
    assert f.agent('worker-1')['runtime_profile'] == 'claude-reviewer'
    assert f.mgr.spawned[-1]['metadata']['cli_type'] == 'claude'
    assert 'CMUX_FLEET_PI_CALLBACK' not in f.mgr.spawned[-1]['env_vars']
    cfg_path = f.run_dir / 'runtime/worker-1-g2.json'
    assert fleet.load(cfg_path)['worker_model'] == 'opus'
    argv = []
    monkeypatch.setattr(fleet.os, 'chdir', lambda *args: None)
    monkeypatch.setattr(fleet.os, 'execvp', lambda exe, args: argv.append(args))
    fleet.actor(cfg_path, 'worker-1', 'READY', generation=2)
    assert argv[0][0] == 'claude' and '--extension' not in argv[0]
    assert argv[0][argv[0].index('--model') + 1] == 'opus'
    hook = json.loads(Path(argv[0][argv[0].index('--settings') + 1]).read_text())
    assert shlex.split(hook['hooks']['Stop'][0]['hooks'][0]['command'])[-3:] == [str(cfg_path), 'worker-1', '2']
    ready(f)
    f.deliver('worker-1')
    count = f.mgr.n
    f.handle(dict(turn('worker-1', 'review done', 2), msg_id=f.agent('worker-1')['msg_id']))
    dispatch(f, 'claude-reviewer')
    f.deliver('worker-1')
    assert f.mgr.n == count  # same READY runtime needs no respawn


def test_actor_fenced_before_exec_and_launch_held_rejects_ready(live, monkeypatch):
    dispatch(live)
    live.deliver('worker-1')
    cfg_path = live.run_dir / 'runtime/worker-1-g1.json'
    with pytest.raises(RuntimeError, match='retired'):
        fleet.actor(cfg_path, 'worker-1', 'READY', generation=0)
    live.setf('worker-1', launch_ready=0)
    ready(live)
    assert live.agent('worker-1')['state'] == 'starting'
    ticks = iter([0, 61])
    monkeypatch.setattr(fleet.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(RuntimeError, match='gate timed out'):
        fleet.actor(cfg_path, 'worker-1', 'READY', generation=1)


def test_preflight_failure_keeps_old_generation_and_never_falls_back(live, monkeypatch):
    def fail(launch):
        raise subprocess.TimeoutExpired('pi --version', 15)
    monkeypatch.setattr(live, 'preflight_profile', fail)
    dispatch(live)
    before = live.agent('worker-1')
    live.deliver('worker-1')
    assert live.task()['status'] == 'blocked'
    assert live.agent('worker-1')['generation'] == before['generation']
    assert live.agent('worker-1')['pane'] == before['pane']
    assert not live.mgr.killed and not traffic(live.mgr)


def test_native_children_defer_switch(live, monkeypatch):
    monkeypatch.setattr(fleet, 'active_children', lambda *args: ['child'])
    dispatch(live)
    live.deliver('worker-1')
    assert live.agent('worker-1')['generation'] == '0'
    assert live.agent('worker-1')['profile_state'] == 'pending'
    assert live.r.llen('fx:inbox:worker-1') == 1


def test_handoff_preserves_applied_runtime_mapping(live):
    dispatch(live, 'claude-reviewer')
    live.deliver('worker-1')
    ready(live)
    live.deliver('worker-1')
    live.handle(dict(turn('worker-1', 'done', 1), msg_id=live.agent('worker-1')['msg_id']))
    live.start_rotation('worker-1', 90000, 100000)
    live.finish_rotation('worker-1', timed_out=True)
    assert live.mgr.spawned[-1]['metadata']['cli_type'] == 'claude'
    assert live.agent('worker-1')['generation'] == '2'
    assert live.agent('worker-1')['current_profile'] == ''
    ready(live)
    assert live.agent('worker-1')['current_profile'] == 'claude-reviewer'


@pytest.mark.parametrize('profile', ['flashnext-coder', 'claude-reviewer', 'researcher'])
def test_launch_mapping_defaults_and_overrides(live, profile):
    registry = AgentRegistry()
    card = registry.get(profile)
    launch = profile_launch.resolve_launch(card, registry, live.cfg)
    cfg = dict(live.cfg, **launch['overrides'])
    cwd = fleet.agent_cwd(cfg, 'worker-1')
    if card.cli == 'pi':
        argv = pi_adapter.argv(cfg, 'worker-1', 3, 'READY', cwd, label='worker-1.3')
        assert argv[argv.index('--model') + 1] == 'flashnext/Qwen3.8-Flash-Next-Uncensored-oQ4e-100K-MTP'
        assert argv[argv.index('--name') + 1] == 'worker-1.3'
    elif card.cli == 'claude':
        argv = fleet.claude_argv(live.path, cfg, 'worker-1', 3, 'READY')
        assert argv[:4] == ['claude', '--dangerously-skip-permissions', '--model', 'opus']
    else:
        launch = profile_launch.resolve_launch(replace(card, model='test-model', profile='test-profile'), registry, live.cfg)
        cfg.update(launch['overrides'])
        argv = fleet.actor_argv(live.path, cfg, 'worker-1', 3, 'READY')
        assert argv[:3] == ['codex', '--profile', 'test-profile']
        assert argv[argv.index('--model') + 1] == 'test-model'
    assert 'Worker' in launch['role_text']
    defaults = profile_launch.resolve_launch(replace(card, model=None, profile=None, role_file=None), registry, live.cfg)
    assert defaults['role_text'] == ''
    if card.cli == 'pi':
        assert defaults['overrides']['pi_model'] == 'local/default'


def test_unknown_cli_unsupported_profile_and_role_escape_block(live, tmp_path):
    registry = AgentRegistry()
    card = registry.get('flashnext-coder')
    for changed in [replace(card, cli='unknown'), replace(card, profile='unsupported'),
                    replace(card, role_file='missing.md'), replace(card, role_file='../fleet.py')]:
        with pytest.raises(ValueError):
            profile_launch.resolve_launch(changed, registry, live.cfg)
    (tmp_path / 'outside.md').write_text('outside')
    root = tmp_path / 'registry'
    root.mkdir()
    (root / 'role.md').symlink_to(tmp_path / 'outside.md')
    registry.directory = root
    with pytest.raises(ValueError, match='inside its registry'):
        profile_launch.resolve_launch(replace(card, role_file='role.md'), registry, live.cfg)


@pytest.mark.parametrize('value', ['true', 1, None])
def test_config_permission_must_be_boolean(live, value):
    cfg = dict(live.cfg, allow_profile_live_switch=value)
    path = live.run_dir / 'invalid.json'
    fleet.save(path, cfg)
    with pytest.raises(ValueError, match='allow_profile_live_switch must be a boolean'):
        fleet.Fleet(path, live.r, live.mgr)


def test_missing_assignments_cannot_downgrade_live_to_metadata(live):
    live.r.hset(fleet.TASK, mapping={'profile_assignments': '{"assignments":[]}'})
    live.on_supervisor(decision(dispatch=[{'to': 'worker-1', 'instruction': 'Do the assigned work and report results'}]))
    assert live.r.llen('fx:inbox:worker-1') == 0
    assert live.agent('worker-1')['profile_state'] == 'blocked'


def test_unconfirmed_idle_after_abort_blocks_switch(live):
    dispatch(live)
    live.setf('worker-1', idle_confirmed=0)
    live.deliver('worker-1')
    assert live.task()['status'] == 'blocked'
    assert 'unconfirmed after an abort' in live.task()['reason']
    assert live.agent('worker-1')['generation'] == '0'


def test_pi_snapshot_callback_works_after_switch_from_codex(live):
    from test_pi import pi_turn
    dispatch(live)
    live.deliver('worker-1')
    ev = pi_turn(live, 'worker-1', 'READY')
    snapshot = live.run_dir / 'runtime/worker-1-g1.json'
    fleet.pi_event(snapshot, 'worker-1', 1, json.dumps(ev))
    live.handle(json.loads(live.r.lpop(fleet.EVENTS)))
    assert live.agent('worker-1')['current_profile'] == 'flashnext-coder'
    live.deliver('worker-1')
    wrong = dict(pi_turn(live, 'worker-1', 'bad delivery'), msg_id='stale-delivery')
    fleet.pi_event(snapshot, 'worker-1', 1, json.dumps(wrong))
    live.handle(json.loads(live.r.lpop(fleet.EVENTS)))
    assert live.agent('worker-1')['state'] == 'busy'


def test_codex_profile_switch_and_actor_override(live, monkeypatch):
    live.finish('cancelled', 'research task')
    live.submit({'goal': 'research sources', 'auto_agents': True, 'apply_profiles_live': True})
    dispatch(live, 'researcher')
    live.deliver('worker-1')
    snapshot = live.run_dir / 'runtime/worker-1-g1.json'
    called = []
    monkeypatch.setattr(fleet.os, 'chdir', lambda *args: None)
    monkeypatch.setattr(fleet.os, 'execvp', lambda exe, args: called.append(args))
    fleet.actor(snapshot, 'worker-1', 'READY', generation=1)
    assert called[0][:3] == ['codex', '--profile', 'fleet']
    ready(live)
    live.deliver('worker-1')
    assert live.agent('worker-1')['profile_state'] == 'applied-live'
    assert '.inbox/' not in received(live.mgr, live.agent('worker-1')['pane'])[-1]  # correct per-slot Codex delivery adapter


def test_created_surface_is_cleaned_when_command_delivery_fails(live, monkeypatch):
    from types import SimpleNamespace
    original = live.mgr.spawn_interactive_cli
    def failed(**kwargs):
        result = original(**kwargs)
        return SimpleNamespace(success=False, pane_id=result.pane_id, error='command delivery failed')
    monkeypatch.setattr(live.mgr, 'spawn_interactive_cli', failed)
    dispatch(live)
    live.deliver('worker-1')
    assert live.agent('worker-1')['pane'] in live.mgr.killed
    assert live.agent('worker-1')['state'] == 'dead'


@pytest.mark.parametrize('condition', ['binary', 'permission', 'version', 'pi_home', 'codex_profile'])
def test_real_preflight_errors_block_before_retirement(live, monkeypatch, tmp_path, condition):
    # Exercise real preflight separately from the deterministic lifecycle fixture.
    monkeypatch.setattr(live, 'preflight_profile', fleet.Fleet.preflight_profile.__get__(live))
    monkeypatch.setattr(fleet.shutil, 'which', lambda binary: None if condition == 'binary' else '/test/cli')
    from types import SimpleNamespace
    monkeypatch.setattr(fleet.subprocess, 'run', lambda *args, **kw: SimpleNamespace(
        stdout='1.0.0' if condition == 'version' else '1.1.0'))
    if condition == 'permission':
        live.cfg['allow_unsandboxed_pi'] = False
    elif condition == 'pi_home':
        live.cfg['pi_home'] = str(tmp_path / 'nonexistent')
    if condition == 'codex_profile':
        monkeypatch.setattr(fleet, 'USER_CODEX_HOME', tmp_path / 'no-user-config')
        live.finish('cancelled', 'replace task')
        live.submit({'goal': 'research sources', 'auto_agents': True, 'apply_profiles_live': True})
        dispatch(live, 'researcher')
    else:
        dispatch(live)
    live.deliver('worker-1')
    assert live.task()['status'] == 'blocked'
    assert live.agent('worker-1')['generation'] == '0' and not live.mgr.killed


def test_launch_gate_opens_only_after_confirmed_retirement(live, monkeypatch):
    old = live.agent('worker-1')['pane']
    original = live.mgr.kill_pane
    def retire(pane):
        if pane == old:
            info = live.agent('worker-1')
            assert info['generation'] == '1' and info['launch_ready'] == '0'
            assert info['current_profile'] == ''
            ready(live)  # even a matching-generation callback cannot bypass the gate
            assert live.agent('worker-1')['state'] == 'starting'
        return original(pane)
    monkeypatch.setattr(live.mgr, 'kill_pane', retire)
    dispatch(live)
    live.deliver('worker-1')
    assert old not in live.mgr.alive
    assert live.agent('worker-1')['launch_ready'] == '1'
    assert live.r.llen('fx:inbox:worker-1') == 1


def test_successful_kill_report_without_disappearance_still_blocks(live, monkeypatch):
    old = live.agent('worker-1')['pane']
    original = live.mgr.kill_pane
    monkeypatch.setattr(live.mgr, 'kill_pane', lambda pane: True if pane == old else original(pane))
    dispatch(live)
    live.deliver('worker-1')
    assert live.task()['status'] == 'blocked'
    assert live.agent('worker-1')['launch_ready'] == '0'


def test_unknown_and_wrong_profiles_do_not_spawn(live):
    count = live.mgr.n
    dispatch(live, 'researcher')  # registered but not assigned in this code-change task
    assert live.agent('worker-1')['profile_state'] == 'blocked'
    assignments = json.loads(live.task()['profile_assignments'])
    assignments['assignments'][0]['agent_id'] = 'missing-card'
    live.r.hset(fleet.TASK, mapping={'profile_assignments': json.dumps(assignments)})
    dispatch(live, 'missing-card')
    assert live.agent('worker-1')['profile_state'] == 'blocked'
    assert live.mgr.n == count and not live.r.llen('fx:inbox:worker-1')


def test_profile_role_file_reaches_spawned_process_scope(live, monkeypatch):
    registry = AgentRegistry()
    registry._cards = tuple(replace(c, role_file='review.md') if c.id == 'claude-reviewer' else c for c in registry)
    root = live.run_dir / 'test-registry'
    root.mkdir()
    (root / 'review.md').write_text('Only inspect code; write findings, never implement changes.')
    registry.directory = root
    monkeypatch.setattr(fleet, 'AgentRegistry', lambda *args: registry)
    dispatch(live, 'claude-reviewer')
    live.deliver('worker-1')
    scope = fleet.agent_cwd(live.cfg, 'worker-1') / 'CLAUDE.md'
    assert 'Only inspect code; write findings' in scope.read_text()
    assert '{{' not in scope.read_text()


def test_pi_operator_input_during_live_task_preserves_gates(pfx, monkeypatch):
    from test_pi import callback, operator_input, pi_turn
    f = pfx
    f.service = False
    f.cfg['allow_profile_live_switch'] = True
    monkeypatch.setattr(f, 'preflight_profile', lambda launch: None)
    f.submit({'goal': 'implement router', 'auto_agents': True, 'apply_profiles_live': True})
    f.pump()
    f.handle(operator_input(f, 'Have worker-1 report its assigned result.'))
    assert json.loads(f.r.get('fx:reply:click1'))['status'] == 'accepted'
    callback(f, pi_turn(f, 'supervisor', decision(dispatch=[{
        'to': 'worker-1', 'instruction': 'Subtask: implementation\nRecommended profile: flashnext-coder\nReply with your result.',
    }])))
    f.pump()
    assert '[Operator instruction]' in f.agent('supervisor')['last_msg']
    assert f.task()['apply_profiles_live'] == 'true'
    assert f.agent('worker-1')['generation'] == '1'
    assert f.agent('worker-1')['state'] == 'starting'
    assert f.r.llen('fx:inbox:worker-1') == 1
    callback(f, pi_turn(f, 'worker-1', 'READY'))
    f.pump()
    callback(f, pi_turn(f, 'worker-1', 'Assigned work finished.'))
    assert f.agent('worker-1')['profile_state'] == 'applied-live'
    assert any('[worker-1.1 report' in text for text in f.r.lrange('fx:inbox:supervisor', 0, -1))
