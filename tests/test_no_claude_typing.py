"""docs/design.md §48 (user 2026-11-09:「修吧，检查一下，任何场景都不用 claude 输入框打字」).

1. `operator`: operator text reaches the supervisor over the fleet bus (operator_input), never its input box;
   pi / hook-channel Claude supervisors accept, a Codex supervisor is refused with its reason.
2. A dialog wait's pause lifts itself once the dialog is gone, only when it is still the sole pause.
3. No code path types text into a Claude Code input box: the configs that needed it are refused at start,
   the call sites hold instead of typing, and every backend's Claude send refuses.

Real private Redis + fake panes; no model, GUI or production socket.
"""
import io
import json
import time
import uuid

import pytest

from cmux_fleet import claude_input, clear_after_result, fleet, roster, runtime_v2
from test_fleet import traffic
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx, audits, cards, req, receive  # noqa: F401  (fixture + helpers)
from test_claude_supervisor import drive
from test_claude_supervisor_hook import supervisor  # noqa: F401  (fixture)
from test_claude_hook_delivery import delivered, typed_any
from test_v2_codex_claude import mixed  # noqa: F401  (fixture)


# ------------------------------------------------------------------ 1. operator command
def test_operator_pi_supervisor_is_accepted_and_queued_without_typing(fx):
    f = fx
    ans = drive(f, lambda: fleet.operator_send(f.cfg, '让 worker-1 回复 OK', wait=5))
    assert ans['status'] == 'accepted' and ans['id'].startswith('ui-'), ans
    f.pump()
    sup = receive(f, 'supervisor')
    assert sup is not None and '让 worker-1 回复 OK' in sup['body']['text']
    assert not traffic(f.mgr)
    assert [e for e in audits(f, 'operator_input') if json.loads(e['detail']).get('source') == 'cli']


def test_operator_claude_supervisor_is_accepted_through_its_hook_mailbox(supervisor):
    f = supervisor
    ans = drive(f, lambda: fleet.operator_send(f.cfg, 'delegate the marker to worker-1', wait=5))
    assert ans['status'] == 'accepted', ans
    got = delivered(f, f.ready_hook)
    assert 'delegate the marker' in got['reason']
    assert typed_any(f, 'supervisor') == []


def test_operator_codex_supervisor_is_refused_with_its_reason(mixed):
    f = mixed
    ans = fleet.operator_send(f.cfg, 'please look at worker-1', wait=5)
    assert ans['status'] == 'rejected' and 'Codex supervisor has no operator queue' in ans['reason'], ans
    # the daemon gives the same reason for the raw event (e.g. a client that skips the local check)
    info, rid = f.agent('supervisor'), 'ui-' + uuid.uuid4().hex
    runtime_v2.post_event(f.v2.bus, dict(kind='operator_input', agent='supervisor', run_id=f.v2.bus.run_id,
                                         generation=int(info['generation']), thread=info.get('thread', ''),
                                         text='hi', request_id=rid), 'op-raw')
    f.v2.step(10)
    reply = json.loads(f.r.xrange(f.v2.prefix + ':reply:' + rid)[0][1]['data'])
    assert reply['status'] == 'rejected' and 'Codex supervisor has no operator queue' in reply['reason']
    assert not f.mgr.sent


def test_operator_old_generation_is_refused_with_its_own_reason(fx):
    f = fx
    info = f.agent('supervisor')
    runtime_v2.post_event(f.v2.bus, dict(kind='operator_input', agent='supervisor', run_id=f.v2.bus.run_id,
                                         generation=int(info['generation']) + 7, thread='other',
                                         text='hi', request_id='ui-old'), 'op-old')
    f.v2.step(10)
    reply = json.loads(f.r.xrange(f.v2.prefix + ':reply:ui-old')[0][1]['data'])
    assert reply['status'] == 'rejected' and 'old supervisor session' in reply['reason']


def test_operator_cli_refusal_exits_2(mixed, capsys):
    with pytest.raises(SystemExit) as out:
        fleet.main(['operator', '--config', str(mixed.path), 'hello'])
    assert out.value.code == 2 and json.loads(capsys.readouterr().out)['status'] == 'rejected'


def test_operator_cli_reads_stdin_and_exits_0_when_accepted(fx, capsys, monkeypatch):
    monkeypatch.setattr('sys.stdin', io.StringIO('from stdin'))
    code = drive(fx, lambda: _exit_code(lambda: fleet.main(['operator', '--config', str(fx.path), '--wait', '5'])))
    printed = json.loads(capsys.readouterr().out)
    assert code == 0 and printed['status'] == 'accepted', printed


def _exit_code(fn):
    try:
        fn()
    except SystemExit as exc:
        return exc.code
    return None


# ------------------------------------------------------------------ 2. dialog pause lifts itself
def dialog(f, slot='worker-1', before=None):
    """worker-1 is mid-delivery and stops at a dialog only the user answers (Dangerous rm, 11-09 shape)."""
    req(f, 'a', to=slot)
    f.pump()
    env = receive(f, slot)
    scope = env['delivery_id']
    if before:
        before()
    f.v2.transaction('t-' + uuid.uuid4().hex, lambda: f.v2.prompt_wait(slot, scope, 'dialog Dangerous rm', '/tmp/s'))
    return env, scope


def gone(f, slot='worker-1'):
    f.v2.watchdog.clear_prompt_wait(slot, 'activity')  # what the watchdog does once the turn moves on


def pmark(f, slot='worker-1'):
    return json.loads(f.agent(slot).get('prompt_pause') or 'null')


def test_dialog_gone_lifts_its_own_pause_and_dispatch_resumes(fx):
    f = fx
    dialog(f)
    assert f.paused() and pmark(f)['at'] == f.r.hget(fleet.SERVICE, 'at')
    f.tick()
    assert f.paused() and pmark(f)  # the dialog is still there
    gone(f)
    f.tick()
    assert not f.paused(), f.r.hget(fleet.SERVICE, 'reason')
    resumed = audits(f, 'execution.auto_resumed')
    assert len(resumed) == 1 and json.loads(resumed[0]['detail'])['source'] == 'prompt_wait'
    assert f.agent('worker-1').get('prompt_pause', '') == ''
    for slot in ('worker-1', 'supervisor'):
        assert any(c['metadata']['event_id'].startswith('ui-autoresume-') for c in cards(f, slot)), slot
    req(f, 'b', to='worker-2')  # queued work flows again
    f.pump()
    assert receive(f, 'worker-2')['request_id'] == 'b'
    f.tick()
    assert len(audits(f, 'execution.auto_resumed')) == 1


@pytest.mark.parametrize('later', ['operator', 'other_hold', 'other_dialog', 'recover'])
def test_dialog_gone_keeps_a_pause_set_after_it(fx, later):
    f = fx
    dialog(f)
    at = f.r.hget(fleet.SERVICE, 'at')
    if later == 'operator':
        f.pause('paused by operator')
    elif later == 'other_hold':
        f.v2.transaction('t-' + uuid.uuid4().hex, lambda: f.v2.hold('worker-2', 'bridge disconnected'))
    elif later == 'other_dialog':
        f.v2.transaction('t-' + uuid.uuid4().hex, lambda: f.v2.prompt_wait('worker-2', 'g0', 'dialog', '/tmp/s2'))
    else:
        f.v2.recover()
    assert f.r.hget(fleet.SERVICE, 'at') != at
    gone(f)
    if later == 'other_dialog':
        gone(f, 'worker-2')
    f.tick()
    assert f.paused() and not audits(f, 'execution.auto_resumed')
    assert f.agent('worker-1').get('prompt_pause', '') == ''


def test_dialog_gone_keeps_a_pause_set_before_it(fx):
    f = fx
    dialog(f, before=lambda: f.pause('paused by operator'))
    assert f.agent('worker-1').get('prompt_pause', '') == ''  # never marked: the pause was not its own
    gone(f)
    f.tick()
    assert f.paused() and not audits(f, 'execution.auto_resumed')


def test_dialog_gone_with_a_brake_tripped_meanwhile_stays_paused(fx):
    f = fx
    dialog(f)
    f.brake_trip('wall', 'worker-2 hit a wall', agent='worker-2')  # already paused: at untouched
    gone(f)
    f.tick()
    assert f.paused() and not audits(f, 'execution.auto_resumed')
    assert audits(f, 'execution.auto_resume_refused')


def test_dialog_still_waiting_on_another_slot_keeps_the_pause(fx):
    f = fx
    dialog(f)
    f.v2.watchdog.tick = lambda now=None: None  # worker-2 is idle here: keep its wait as set
    f.setf('worker-2', prompt_wait='g0', prompt_route='user')  # e.g. armed under the same pause
    gone(f)
    f.tick()
    assert f.paused() and pmark(f)  # kept for a later tick
    f.setf('worker-2', prompt_wait='', prompt_route='')
    f.tick()
    assert not f.paused()


def test_supervisor_ruled_dialog_never_pauses_so_never_arms(fx):
    f = fx
    before = f.paused()
    f.setf('worker-1', prompt_wait='g0', prompt_route='supervisor')
    f.tick()
    assert f.paused() == before and f.agent('worker-1').get('prompt_pause', '') == ''


# ------------------------------------------------------------------ 3. no typing into Claude
NEW = dict(transport_version=2, allow_development_v2=True, supervisor_cli='claude', worker_cli='claude',
           mode='task')


@pytest.mark.parametrize('cfg, needle', [
    (dict(NEW, claude_hook_delivery=False), 'claude_hook_delivery: false'),
    (dict(NEW, mode='service', lifecycle='service', clear_after_result=True, claude_hook_delivery=False),
     'clear_after_result: true'),
    (dict(NEW, supervisor_cli='pi', worker_cli='pi', claude_hook_delivery=False), 'claude_hook_delivery: false'),
    (dict(NEW, run='/r'), 'claude_supervisor_hook_delivery'),
    (dict(NEW, transport_version=1), 'transport v2'),
    (dict(NEW, run='/r', adopt={'pane': 'p', 'slot': 'worker-1'}), 'adopt.hook'),
    (dict(NEW, run='/r', adopt={'pane': 'p', 'slot': 'worker-1', 'hook': False}), 'adopt.hook'),
])
def test_configs_that_type_into_claude_are_refused(cfg, needle):
    with pytest.raises(ValueError) as err:
        runtime_v2.validate_config(cfg)
    assert needle in str(err.value) and '§48' in str(err.value)


@pytest.mark.parametrize('cfg', [
    NEW,  # start's argv (the hook marker is pinned after this check)
    dict(NEW, run='/r', claude_supervisor_hook_delivery=True),
    dict(NEW, run='/r', adopt={'pane': 'p', 'slot': 'worker-1', 'hook': True}),
    dict(NEW, supervisor_cli='pi', worker_cli='claude', run='/r'),
    dict(NEW, supervisor_cli='codex', worker_cli='claude', run='/r'),
])
def test_hook_channel_configs_still_start(cfg):
    assert runtime_v2.typed_claude_problem(cfg) is None


@pytest.mark.parametrize('flag', ['--no-claude-hook-delivery', '--clear-after-result'])
def test_start_refuses_the_retired_typing_flags_before_provisioning(flag):
    args = fleet.cli().parse_args(['start', '--lifecycle', 'service', '--mode', 'service',
                                   '--allow-development-v2', flag])
    with pytest.raises(ValueError) as err:
        runtime_v2.validate_config(vars(args))
    assert 'retired' in str(err.value)


@pytest.mark.parametrize('entry', [{'clear_after_result': True}, {'claude_hook_delivery': False}])
def test_roster_subfleet_entry_with_a_typing_switch_is_refused(tmp_path, entry):
    (tmp_path / 'sup.md').write_text('x')
    (tmp_path / 'child.yaml').write_text('workers:\n  - name: a\n')
    path = tmp_path / 'roster.yaml'
    key, value = next(iter(entry.items()))
    path.write_text('workers:\n  - name: worker-1\n  - name: worker-2\nsubfleets:\n'
                    '  - {name: ops, supervisor_slot: worker-1, supervisor_role: sup.md, roster: child.yaml, '
                    + key + ': ' + str(value).lower() + '}\n')
    with pytest.raises(ValueError) as err:
        roster.load_entries(path)
    assert key + ': ' + str(value).lower() + ' is retired' in str(err.value)


class Mgr:
    last_error = ''


def test_claude_input_send_never_pastes_or_keys():
    called = []
    mgr = Mgr()
    mgr.send_key = lambda *a: called.append(a) or True
    assert claude_input.send(mgr, 'p', 'hello', lambda: called.append('paste') or True) is False
    assert called == [] and '§48' in mgr.last_error


def test_v2_delivery_to_a_claude_slot_without_hook_holds_instead_of_typing(supervisor, monkeypatch):
    f = supervisor
    assert drive(f, lambda: fleet.operator_send(f.cfg, 'operator says hi', wait=5))['status'] == 'accepted'
    monkeypatch.setattr(type(f.v2), 'hooked', lambda self, slot: False)
    f.pump()
    assert f.agent('supervisor')['state'] == 'held'
    held = audits(f, 'execution.held')
    assert held and '§48' in held[-1]['error']
    assert typed_any(f, 'supervisor') == [] and not f.mgr.sent


def test_startup_nudge_to_a_claude_slot_without_hook_holds_instead_of_typing(supervisor, monkeypatch):
    f = supervisor
    monkeypatch.setattr(type(f.v2), 'hooked', lambda self, slot: False)
    f.v2.startup_nudges.add('supervisor')
    runtime_v2.post_event(f.v2.bus, dict(kind='pause', request_id='nudge-carrier', reason='x'), 'nudge-carrier')
    assert f.v2.step(10)
    assert not f.mgr.sent
    assert any('startup nudge not sent' in x['error'] for x in audits(f, 'execution.held'))


def test_clear_tick_never_types_slash_clear(supervisor, monkeypatch):
    f = supervisor
    monkeypatch.setattr(type(f.v2), 'clear_enabled', lambda self, slot: slot == 'worker-1')
    monkeypatch.setattr(clear_after_result, 'hook_exited', lambda pid: True)
    monkeypatch.setattr(clear_after_result, 'idle_prompt', lambda screen: True)
    monkeypatch.setattr(f.mgr, 'capture_output', lambda *a, **k: '', raising=False)
    f.setf('worker-1', state='clearing', clear_deadline=time.time() + 60, clear_idle_since=time.time() - 1,
           clear_phase='')
    f.v2.clear_tick(time.time())
    assert not f.mgr.sent
    assert any('/clear not typed' in x['error'] for x in audits(f, 'execution.held'))


def test_v1_send_to_a_claude_agent_without_hook_is_refused(supervisor, monkeypatch):
    f = supervisor
    monkeypatch.setattr(fleet.claude_hook, 'enabled', lambda *a, **k: False)
    v2, f.v2 = f.v2, None
    try:
        assert f.send('supervisor', f.agent('supervisor')['pane'], 'do the thing') is False
    finally:
        f.v2 = v2
    assert not f.mgr.sent
