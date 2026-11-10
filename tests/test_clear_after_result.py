"""Sandbox-only guards/effect plans. Lua/PEL application is tested separately with real Redis."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

from test_runtime_v2_planning import plan  # noqa: F401
from cmux_fleet import clear_after_result as clear, cmux_manager, fleet, runtime_v2, watchdog

# §48 (user 2026-11-09:「任何场景都不用 claude 输入框打字」): these tests exercised a channel that typed into a
# Claude input box; it is refused at start now (tests/test_no_claude_typing.py covers the refusal).
RETIRED = pytest.mark.skip(reason='docs/design.md §48: fleet never types into a Claude input box (2026-11-09)')


IDLE = 'Done.\n────────────────────\n❯ \n────────────────────\n  ⏵⏵ bypass permissions on · ? for shortcuts'
SLOT = 'worker-1'


def enable(plan):
    f, v, r, env = plan
    f.cfg.update(worker_cli='claude', lifecycle='service', clear_after_result=True)
    f.setf(SLOT, state='busy', request='r1', pane='pane-1', ctx_used=90000, ctx_window=200000)
    f.mgr.alive.add('pane-1')
    return f, v, r, env


def event(env, **extra):
    return dict(kind='settled', agent=SLOT, run_id=env['run_id'], generation=env['generation'],
                thread=env['session_id'], delivery=env, hook_pid=12345,
                candidates=[{'tool': 'fleet_result', 'value': {'reply': 'OK'}}],
                failed=False, aborted=False, unfinished=False, **extra)


def clear_event(f, **extra):
    info = f.agent(SLOT)
    return dict(kind='cleared', agent=SLOT, run_id='unit', generation=0, old_thread=info['thread'],
                clear_token=info.get('clear_token', ''), thread='new-session',
                transcript='/tmp/new-session.jsonl', hook_pid=12346, **extra)


def apply_stub(plan, monkeypatch):
    """Apply planned commands to the in-memory source; deliberately does NOT emulate Lua guards."""
    f, v, r, _ = plan
    def apply(commands):
        for op, key, *args in commands:
            if op == 'HSET':
                r.hset(key, mapping=dict(zip(args[::2], args[1::2])))
            elif op == 'SET':
                r.set(key, args[0])
            elif op == 'DEL':
                r.delete(key, *args)
            elif op == 'RPUSH':
                r.rpush(key, args[0])
            elif op == 'LREM':
                r.lrem(key, args[0], args[1])
            elif op == 'LTRIM':
                r.ltrim(key, int(args[0]), int(args[1]))
            elif op not in ('XADD', 'XACK'):
                raise AssertionError(op)
    apply(v.effects.commands)
    v.effects = None
    v.context = None
    f.r = r
    monkeypatch.setattr(r, 'xadd', lambda *a, **kw: None, raising=False)
    def transaction(identity, action, **kw):
        v.effects = runtime_v2.Effects(r)
        v.context = {'intent_id': identity}
        f.r = v.effects
        try:
            answer = action()
            commands = v.effects.commands
        finally:
            f.r = r
            v.effects = None
            v.context = None
        apply(commands)
        return answer
    monkeypatch.setattr(v, 'transaction', transaction)


def idle_twice(v, now=None):
    now = time.time() if now is None else now
    v.clear_tick(now)
    v.clear_tick(now + 0.5)


@pytest.mark.parametrize('changes', [dict(lifecycle='smoke'), dict(lifecycle='dev_task', mode='task'),
    dict(worker_cli='pi'), dict(worker_cli='codex'), dict(transport_version=1)])
@RETIRED
def test_config_rejects_unsupported_combinations(changes):
    cfg = dict(clear_after_result=True, lifecycle='service', mode='service', worker_cli='claude', transport_version=2)
    with pytest.raises(ValueError, match='clear-after-result requires'):
        runtime_v2.validate_config(dict(cfg, **changes))


@pytest.mark.parametrize('value', [0, -1, True, None, '20', float('nan'), float('inf')])
def test_invalid_clear_timeout(value):
    with pytest.raises(ValueError, match='clear_timeout'):
        clear.validate({'clear_timeout': value})


@pytest.mark.parametrize('argv,match', [
    (['--lifecycle', 'dev_task'], 'clear-after-result'),
    (['--lifecycle', 'smoke'], 'clear-after-result'),
    (['--lifecycle', 'service', '--worker-cli', 'pi'], 'clear-after-result'),
    (['--lifecycle', 'service', '--worker-cli', 'codex'], 'clear-after-result'),
    (['--lifecycle', 'service', '--transport-version', '1'], '^2$'),
])
def test_start_rejects_before_provisioning(monkeypatch, argv, match):
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: pytest.fail('must reject before provisioning'))
    with pytest.raises(SystemExit, match=match):
        fleet.main(['start', '--clear-after-result', '--allow-development-v2'] + argv)


@RETIRED
def test_start_accepts_service_claude_before_provisioning(monkeypatch):
    class Reached(Exception):
        pass
    def reached():
        raise Reached
    monkeypatch.setattr(cmux_manager, 'cmux_available', reached)
    with pytest.raises(Reached):
        fleet.main(['start', '--lifecycle', 'service', '--worker-cli', 'claude', '--clear-after-result',
                    '--no-claude-hook-delivery', '--allow-development-v2'])


@RETIRED
def test_start_refuses_clear_with_claude_hook_delivery(monkeypatch):
    # The Stop-hook channel (default on) blocks inside every Stop hook: /clear could never be pasted.
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: pytest.fail('must reject before provisioning'))
    with pytest.raises(SystemExit, match='--no-claude-hook-delivery'):
        fleet.main(['start', '--lifecycle', 'service', '--worker-cli', 'claude', '--clear-after-result',
                    '--allow-development-v2'])


@RETIRED
@pytest.mark.parametrize('hook', [True, None])
def test_config_refuses_clear_with_claude_hook_delivery(hook):
    cfg = dict(clear_after_result=True, lifecycle='service', mode='service', worker_cli='claude', transport_version=2,
               allow_development_v2=True)
    if hook is not None:
        cfg['claude_hook_delivery'] = hook
    with pytest.raises(ValueError, match='claude_hook_delivery: false'):
        runtime_v2.validate_config(cfg)
    runtime_v2.validate_config(dict(cfg, claude_hook_delivery=False))
    runtime_v2.validate_config(dict(cfg, clear_after_result=False))  # switch off: hook channel stays legal


def test_hook_channel_is_off_whenever_clear_is_on():
    from cmux_fleet import claude_hook
    cfg = dict(worker_cli='claude', transport_version=2, mode='service')
    assert claude_hook.enabled(cfg, SLOT, claude=True)
    assert claude_hook.enabled(dict(cfg, clear_after_result=False), SLOT, claude=True)
    assert not claude_hook.enabled(dict(cfg, clear_after_result=True), SLOT, claude=True)


def test_switch_off_keeps_hook_channel_settings(tmp_path):
    cfg = dict(run=str(tmp_path), workers=[SLOT], python=sys.executable, transport_version=2,
               worker_cli='claude', mode='service')
    fleet.agent_cwd(cfg, SLOT).mkdir(parents=True)
    def settings(c):
        argv = fleet.claude_argv(tmp_path / 'config.json', c, SLOT, 3, 'READY')
        return json.loads(Path(argv[argv.index('--settings') + 1]).read_text()), argv
    (default, argv_default), (off, argv_off) = settings(cfg), settings(dict(cfg, clear_after_result=False))
    assert default == off and argv_default == argv_off
    assert set(default['hooks']) == {'Stop', 'StopFailure', 'PostToolUse'} and len(default['hooks']['Stop'][0]['hooks']) == 2


@RETIRED
def test_resume_refuses_clear_with_claude_hook_delivery(tmp_path, monkeypatch):
    from types import SimpleNamespace
    run = tmp_path / 'run'
    run.mkdir()
    fleet.save(run / 'config.json', dict(run=str(run), python=sys.executable, workers=[SLOT], lifecycle='service',
               mode='service', supervisor_cli='codex', worker_cli='claude', transport_version=2,
               allow_development_v2=True, clear_after_result=True))
    monkeypatch.setattr(fleet, 'daemon_running', lambda path: pytest.fail('must reject before reattaching'))
    with pytest.raises(SystemExit, match='cannot resume .*--no-claude-hook-delivery'):
        fleet.resume(run, SimpleNamespace(max_attempts=None, roster=None, advisor=False))


@pytest.mark.parametrize('on,slot', [(False, SLOT), (True, 'supervisor'), (True, SLOT)])
def test_worker_only_opt_in_hooks(tmp_path, on, slot):
    cfg = dict(run=str(tmp_path), workers=[SLOT], python=sys.executable, transport_version=2,
               worker_cli='claude', mode='service', clear_after_result=on, claude_hook_delivery=False,
               claude_supervisor_hook_delivery=True)
    fleet.agent_cwd(cfg, slot).mkdir(parents=True)
    argv = fleet.claude_argv(tmp_path / 'config.json', cfg, slot, 3, 'READY')
    hooks = json.loads(Path(argv[argv.index('--settings') + 1]).read_text())['hooks']
    if on and slot == SLOT:
        assert hooks['SessionStart'][0]['matcher'] == 'clear'
        for key, verb in [('Stop', 'claude-stop'), ('SessionStart', 'claude-clear'), ('StopFailure', 'claude-stop-failure')]:
            cmd = hooks[key][0]['hooks'][0]['command']
            assert cmd.startswith('exec ') and verb in cmd and cmd.endswith('worker-1 3')
    elif slot == 'supervisor':  # clear is worker-only; v2 supervisor delivery still requires hooks
        assert set(hooks) == {'Stop', 'PostToolUse', 'StopFailure'}
        assert hooks['Stop'][0]['hooks'][1]['asyncRewake'] is True
        assert not hooks['Stop'][0]['hooks'][0]['command'].startswith('exec ')
    else:  # no opt-in clear hooks; the mid-turn PostToolUse hook (design §34) is on every Claude agent
        assert set(hooks) == {'Stop', 'PostToolUse'}
        assert not hooks['Stop'][0]['hooks'][0]['command'].startswith('exec ')


@pytest.mark.parametrize('screen', ['', '❯', IDLE.replace('❯ ', '❯ /clear'), IDLE + '\nEsc to interrupt',
    IDLE + '\n2 background tasks', IDLE.replace('Done.', 'Running Stop hooks…'), IDLE.replace('Done.', 'Thinking…'), IDLE.replace('❯ ', '❯ 1. Yes'), IDLE.replace('────────────────────', '')])
def test_ambiguous_tui_never_authorizes_clear(screen):
    assert not clear.idle_prompt(screen)


# Real Studio screen (Claude Code + fleet --name, 2026-10-14): the session name sits in the upper rule.
NAMED_IDLE = """⏺ {"answer": {"ok": true}}

✻ Churned for 2s · done 9:38

────────────────────────────────────────────────────────────── feeea440e-worker-1 ─
❯
───────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
"""


def test_idle_prompt_accepts_session_name_in_rule():
    assert clear.idle_prompt(NAMED_IDLE)
    assert not clear.idle_prompt(NAMED_IDLE.replace('❯\n', '❯ /clear\n'))  # text already in the box


def test_idle_tui_and_process_exit_guards(monkeypatch):
    assert clear.idle_prompt(IDLE)
    assert not clear.hook_exited(os.getpid())
    assert not clear.hook_exited('')
    def gone(pid, sig):
        raise ProcessLookupError()
    monkeypatch.setattr(clear.os, 'kill', gone)
    assert clear.hook_exited('12345')
    def inaccessible(pid, sig):
        raise PermissionError()
    monkeypatch.setattr(clear.os, 'kill', inaccessible)
    assert not clear.hook_exited('12345')


@pytest.mark.parametrize('valid', [True, False])
def test_result_plan_clears_without_typing_inside_transaction(plan, valid):
    f, v, r, env = enable(plan)
    ev = event(env)
    if not valid:
        ev['candidates'] = []
    v.handle(ev)
    assert f.agent(SLOT)['state'] == 'clearing'
    assert f.agent(SLOT)['clear_hook_pid'] == '12345'
    assert f.r.hget(v.bus.lease_key(SLOT), 'current_delivery') == ''
    assert json.loads(f.r.get(fleet.RESULT + 'r1'))['status'] == ('ok' if valid else 'invalid')
    assert not f.mgr.sent
    assert r.get(fleet.RESULT + 'r1') is None  # source untouched until intent applies


def test_switch_off_same_settlement_and_failure_audit_only(plan):
    f, v, r, env = plan
    v.handle(dict(event(env), kind='stop_failure', error='refusal'))
    assert f.r.get(fleet.RESULT + 'r1') is None
    assert f.r.hget(v.bus.lease_key(SLOT), 'status') == 'received'
    v.handle(event(env))
    assert f.agent(SLOT)['state'] == 'idle'
    assert 'clear_required' not in f.agent(SLOT)
    assert not f.mgr.sent


@RETIRED
def test_deferred_non_bracketed_paste_exactly_once(plan, monkeypatch):
    f, v, r, env = enable(plan)
    v.handle(event(env))
    apply_stub(plan, monkeypatch)
    monkeypatch.setattr(f.mgr, 'capture_output', lambda *a, **kw: IDLE, raising=False)
    monkeypatch.setattr(clear, 'hook_exited', lambda pid: False)
    idle_twice(v)
    assert not f.mgr.sent
    monkeypatch.setattr(clear, 'hook_exited', lambda pid: True)
    monkeypatch.setattr(f.mgr, 'capture_output', lambda *a, **kw: IDLE + '\nEsc to interrupt', raising=False)
    idle_twice(v)
    assert not f.mgr.sent
    monkeypatch.setattr(f.mgr, 'capture_output', lambda *a, **kw: IDLE, raising=False)
    idle_twice(v)
    assert f.mgr.sent == [('pane-1', '/clear')]
    assert f.mgr.bracketed[-1] == ('pane-1', False)
    idle_twice(v)
    v.deliver(SLOT)
    f.start_rotation(SLOT, 200000, 200000)
    assert f.mgr.sent == [('pane-1', '/clear')]
    assert f.agent(SLOT)['state'] == 'clearing'


@pytest.mark.parametrize('paste_failure', [True, False])
def test_clear_failures_hold_without_next_delivery(plan, monkeypatch, paste_failure):
    f, v, r, env = enable(plan)
    v.handle(event(env))
    apply_stub(plan, monkeypatch)
    if paste_failure:
        monkeypatch.setattr(clear, 'hook_exited', lambda pid: True)
        monkeypatch.setattr(f.mgr, 'capture_output', lambda *a, **kw: IDLE, raising=False)
        monkeypatch.setattr(f.mgr, 'send_prompt_to_idle', lambda *a, **kw: False)
        idle_twice(v)
    else:
        v.clear_tick(float(f.agent(SLOT)['clear_deadline']) + 1)
    assert f.agent(SLOT)['state'] == 'held' and f.paused()
    assert f.agent(SLOT)['clear_required'] == '1'
    v.deliver(SLOT)
    assert not f.mgr.sent
    assert not v.reattachable(SLOT)


def test_stop_failure_terminal_once_without_replay(plan):
    f, v, r, env = enable(plan)
    v.handle(dict(event(env), kind='stop_failure', error='classifier refusal'))
    assert json.loads(f.r.get(fleet.RESULT + 'r1'))['status'] == 'failed'
    assert json.loads(f.r.get(fleet.RESULT + 'r1'))['error'] == 'classifier refusal'
    assert f.agent(SLOT)['state'] == 'clearing'
    assert f.r.hget(v.bus.msg_key(env['message_id']), 'status') == 'committed'
    assert not v.depth(SLOT)
    assert v.context['stop_gate']['consumer'] == env['consumer']


@pytest.mark.parametrize('change', [dict(generation=8), dict(thread='wrong'), dict(delivery=None)])
def test_stop_failure_stale_or_unbound_no_result(plan, change):
    f, v, r, env = enable(plan)
    v.handle(dict(event(env), kind='stop_failure', error='API error', **change))
    assert f.r.get(fleet.RESULT + 'r1') is None
    assert f.r.hget(v.bus.lease_key(SLOT), 'current_delivery') == env['delivery_id']


def test_expired_stop_can_clear_without_using_its_business_result(plan):
    f, v, r, env = enable(plan)
    r.hset(v.bus.msg_key(env['message_id']), mapping={'deadline_at': 1})
    v.handle(event(env))
    assert json.loads(f.r.get(fleet.RESULT + 'r1'))['status'] == 'timeout'
    assert f.r.hget(v.bus.lease_key(SLOT), 'current_delivery') == ''
    assert f.agent(SLOT)['state'] == 'clearing'
    assert v.context['stop_gate']


def test_manual_clear_during_delivery_holds_and_preserves_binding(plan):
    f, v, r, env = enable(plan)
    v.handle(clear_event(f))
    assert f.agent(SLOT)['state'] == 'held'
    assert f.agent(SLOT)['clear_conflict'] == '1'
    assert f.agent(SLOT)['thread'] == env['session_id']
    assert f.r.hget(v.bus.lease_key(SLOT), 'current_delivery') == env['delivery_id']
    v.handle(dict(event(env), kind='turn_ended'))
    assert f.agent(SLOT)['state'] == 'held'


@pytest.mark.parametrize('manual', [True, False])
def test_rebind_plan_updates_bus_and_agent_together_then_waits_for_hook(plan, monkeypatch, manual):
    f, v, r, env = enable(plan)
    r.hset(v.bus.lease_key(SLOT), mapping={'current_delivery': '', 'status': 'idle'})
    f.setf(SLOT, state='idle')
    monkeypatch.setattr(r, 'xpending', lambda *a: {'pending': 0}, raising=False)
    if not manual:
        v.begin_clear(SLOT, hook_pid=12345)
    v.handle(clear_event(f))
    assert f.agent(SLOT)['generation'] == '0'
    assert f.agent(SLOT)['thread'] == 'new-session'
    assert f.agent(SLOT)['ctx_used'] == '0'
    assert f.agent(SLOT)['state'] == 'clearing'
    assert f.r.hget(v.bus.lease_key(SLOT), 'session_id') == 'new-session'
    assert f.r.hget(v.bus.lease_key(SLOT), 'fence') == '2'
    assert v.context['rebind_gate']['old_session'] == env['session_id']
    assert r.hget(v.bus.lease_key(SLOT), 'session_id') == env['session_id']
    apply_stub(plan, monkeypatch)
    monkeypatch.setattr(clear, 'hook_exited', lambda pid: True)
    monkeypatch.setattr(f.mgr, 'capture_output', lambda *a, **kw: IDLE, raising=False)
    idle_twice(v)
    assert f.agent(SLOT)['state'] == 'idle'
    assert f.agent(SLOT)['clear_required'] == '0'
    assert not f.mgr.sent  # manual clear does not trigger a second /clear


def test_rebind_keeps_the_slot_launch_record(plan, monkeypatch):
    # The launch record (runtime_launch + installed_domains = layout) is per slot and replayed by the next
    # generation (§27); a same-generation rebind changes only the session binding, never the record.
    f, v, r, env = enable(plan)
    record = dict(runtime_launch=json.dumps({'profile_id': 'haiku', 'overrides': {'worker_cli': 'claude'}}),
                  installed_domains=json.dumps(['us_options']), runtime_profile='haiku', advice_hold='0')
    f.setf(SLOT, state='idle', **record)
    r.hset(v.bus.lease_key(SLOT), mapping={'current_delivery': '', 'status': 'idle'})
    monkeypatch.setattr(r, 'xpending', lambda *a: {'pending': 0}, raising=False)
    v.begin_clear(SLOT, hook_pid=12345)
    v.handle(clear_event(f))
    apply_stub(plan, monkeypatch)
    monkeypatch.setattr(clear, 'hook_exited', lambda pid: True)
    monkeypatch.setattr(f.mgr, 'capture_output', lambda *a, **kw: IDLE, raising=False)
    idle_twice(v)
    info = f.agent(SLOT)
    assert info['state'] == 'idle' and info['thread'] == 'new-session' and info['generation'] == '0'
    assert {k: info[k] for k in record} == record


def test_rebind_with_residual_pel_is_held(plan, monkeypatch):
    f, v, r, env = enable(plan)
    r.hset(v.bus.lease_key(SLOT), mapping={'current_delivery': '', 'status': 'idle'})
    f.setf(SLOT, state='idle')
    monkeypatch.setattr(r, 'xpending', lambda *a: {'pending': 1}, raising=False)
    v.handle(clear_event(f))
    assert f.agent(SLOT)['state'] == 'held'
    assert f.agent(SLOT)['thread'] == env['session_id']


def test_clearing_watchdog_is_hands_off():
    assert 'clearing' in watchdog.HANDS_OFF


@pytest.mark.parametrize('verb,fn', [('claude-clear', 'claude_clear'), ('claude-stop-failure', 'claude_stop_failure')])
def test_internal_cli_entry_points(monkeypatch, verb, fn):
    calls = []
    monkeypatch.setattr(fleet, fn, lambda *a: calls.append(a))
    monkeypatch.setattr(fleet.sys, 'stdin', __import__('io').StringIO('{"source":"clear"}'))
    fleet.main([verb, '/tmp/cfg.json', SLOT, '7'])
    assert calls == [('/tmp/cfg.json', SLOT, '7', '{"source":"clear"}')]


def test_conflicting_same_generation_clear_holds_instead_of_guessing_session(plan):
    f, v, r, env = enable(plan)
    ev = clear_event(f)
    ev['old_thread'] = 'earlier-session'
    v.handle(ev)
    assert f.agent(SLOT)['state'] == 'held'
    assert f.agent(SLOT)['clear_required'] == f.agent(SLOT)['clear_conflict'] == '1'
    assert f.agent(SLOT)['thread'] == env['session_id']


def test_queued_only_timeout_does_not_clear_another_active_request(plan):
    f, v, r, env = enable(plan)
    v.publish('different-request', 'timeout', worker=SLOT)
    assert f.agent(SLOT)['state'] == 'busy'
    assert 'clear_required' not in f.agent(SLOT)
    assert f.r.hget(v.bus.lease_key(SLOT), 'current_delivery') == env['delivery_id']


def test_clear_cannot_rebind_after_deadline(plan):
    f, v, r, env = enable(plan)
    v.begin_clear(SLOT, hook_pid=12345)
    f.setf(SLOT, clear_deadline=1)
    r.hset(v.bus.lease_key(SLOT), mapping={'current_delivery': '', 'status': 'idle'})
    v.handle(clear_event(f))
    assert f.agent(SLOT)['state'] == 'held'
    assert f.agent(SLOT)['thread'] == env['session_id']


@RETIRED
def test_resume_keeps_saved_clear_switch_and_timeout(tmp_path, monkeypatch):
    from types import SimpleNamespace
    run = tmp_path / 'run'
    run.mkdir()
    cfg = dict(run=str(run), python=sys.executable, workers=[SLOT], lifecycle='service', mode='service',
               supervisor_cli='codex', worker_cli='claude', transport_version=2, allow_development_v2=True,
               clear_after_result=True, clear_timeout=17, claude_hook_delivery=False)
    fleet.save(run / 'config.json', cfg)
    monkeypatch.setattr(fleet, 'daemon_running', lambda path: False)
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: object())
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    commands = []
    mgr = SimpleNamespace(spawn_window=lambda *a, **kw: commands.append(a) or SimpleNamespace(success=True))
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: mgr)
    assert fleet.resume(run, SimpleNamespace(max_attempts=None, roster=None, advisor=False))
    saved = fleet.load(run / 'config.json')
    assert saved['clear_after_result'] and saved['clear_timeout'] == 17
    assert '--resume' in commands[0][1]


@pytest.mark.parametrize('field,value', [('thread', ''), ('transcript', 'relative.jsonl')])
def test_malformed_clear_confirmation_holds_instead_of_leaving_old_session_deliverable(plan, field, value):
    f, v, r, env = enable(plan)
    f.setf(SLOT, state='idle')
    r.hset(v.bus.lease_key(SLOT), mapping={'current_delivery': '', 'status': 'idle'})
    ev = clear_event(f)
    ev[field] = value
    v.handle(ev)
    assert f.agent(SLOT)['state'] == 'held'
    assert f.agent(SLOT)['clear_required'] == '1'
    assert f.agent(SLOT)['thread'] == env['session_id']


def test_confirmed_manual_clear_can_repair_empty_held_slot_without_unpausing(plan, monkeypatch):
    f, v, r, env = enable(plan)
    r.hset(v.bus.lease_key(SLOT), mapping={'current_delivery': '', 'message_id': '', 'status': 'idle'})
    v.hold(SLOT, 'clear timed out')
    monkeypatch.setattr(r, 'xpending', lambda *a: {'pending': 0}, raising=False)
    v.handle(clear_event(f))
    assert f.agent(SLOT)['clear_phase'] == 'confirmed'
    assert f.agent(SLOT)['thread'] == 'new-session'
    apply_stub(plan, monkeypatch)
    monkeypatch.setattr(clear, 'hook_exited', lambda pid: True)
    monkeypatch.setattr(f.mgr, 'capture_output', lambda *a, **kw: IDLE, raising=False)
    idle_twice(v)
    assert f.agent(SLOT)['state'] == 'idle' and f.paused()


@pytest.mark.parametrize('clear,hook,slot,want', [
    (True, False, SLOT, 'typed'), (False, False, SLOT, 'typed'), (False, True, SLOT, 'hook'), (True, False, 'supervisor', 'supervisor')])
def test_every_claude_worker_is_told_where_its_deliveries_come_from(tmp_path, clear, hook, slot, want):
    """10-14 evermem: a typed-delivery worker refused a pasted task ("can't confirm it came from the supervisor")."""
    from cmux_fleet import claude_hook
    cfg = dict(run=str(tmp_path), workers=[SLOT], python=sys.executable, transport_version=2,
               worker_cli='claude', mode='service', clear_after_result=clear, claude_supervisor_hook_delivery=True,
               **({} if hook else {'claude_hook_delivery': False}))
    fleet.agent_cwd(cfg, slot).mkdir(parents=True)
    argv = fleet.claude_argv(tmp_path / 'config.json', cfg, slot, 3, 'READY')
    prompts = [argv[i + 1] for i, a in enumerate(argv) if a == '--append-system-prompt']
    expected = {'typed': [claude_hook.TYPED_DECLARATION], 'hook': [claude_hook.DECLARATION],
                'supervisor': [claude_hook.SUPERVISOR_DECLARATION]}[want]
    assert prompts == expected
