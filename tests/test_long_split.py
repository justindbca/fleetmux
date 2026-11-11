"""Long task split (docs/design.md §44): the §34 probe spots a long, flowing turn, nudges the worker to
propose a split, the supervisor approves with models, the slot hands off to a new generation on
``supervisor_profile`` that a sub-fleet then adopts, and the original model comes back on return."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from test_contracts import cf, inbox, report, sup  # noqa: F401  (fixture + helpers)
from test_escalate import escalate, ok_check  # noqa: F401
from test_fleet import FakeMgr, FakeRedis, cfx  # noqa: F401
from test_fleet import received  # noqa: E402
from test_subfleet import adopt_by_hook, child_daemon, given, hook, no_launch, parent_hook, step, stop_child_like_daemon, tree  # noqa: F401
from test_v2_codex_claude import answer_turn, dispatch_one, mixed  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from fleetmux import control_view, fleet, midturn, profile_launch, roster, subfleet
from fleetmux.agent_registry import AgentRegistry

SPLIT_YAML = ('workers:\n  - {name: worker-1, split: {check_minutes: 90, min_calls: 5}}\n  - name: worker-2\n'
              'split:\n  check_minutes: 30\n  window_minutes: 5\n  restore_model: false\n')


# ------------------------------------------------------------------ config (§2.1)
def test_roster_split_table_and_worker_override_resolve_in_order(tmp_path):
    roster.parse_entries(SPLIT_YAML.encode(), tmp_path)  # valid
    table, per_worker = roster.split(SPLIT_YAML.encode())
    assert table == {'check_minutes': 30, 'window_minutes': 5, 'restore_model': False}
    assert per_worker == {'worker-1': {'check_minutes': 90, 'min_calls': 5}}
    values, source = midturn.resolve_split(table, per_worker['worker-1'])
    assert values == {'check_minutes': 90, 'window_minutes': 5, 'min_calls': 5, 'min_distinct': 2,
                      'restore_model': False}
    assert source == ('check_minutes: worker; window_minutes: roster; min_calls: worker; min_distinct: default; '
                      'restore_model: roster')
    assert midturn.resolve_split({}, None)[0] == midturn.SPLIT_DEFAULTS == {
        'check_minutes': 40, 'window_minutes': 10, 'min_calls': 15, 'min_distinct': 2, 'restore_model': True}


@pytest.mark.parametrize('text,error', [
    ('split: {check_minutes: -1}', 'split.check_minutes must be a number >= 0'),
    ('split: {check_minutes: "40"}', 'split.check_minutes must be a number >= 0'),
    ('split: {window_minutes: 0}', 'split.window_minutes must be a number > 0'),
    ('split: {min_calls: 0}', 'split.min_calls must be an integer >= 1'),
    ('split: {min_distinct: 1.5}', 'split.min_distinct must be an integer >= 1'),
    ('split: {restore_model: "yes"}', 'split.restore_model must be true or false'),
    ('split: {check_every: 3}', 'split: unknown key check_every'),
    ('split: 40', 'split must be a mapping'),
])
def test_bad_split_values_reject_the_whole_roster(tmp_path, text, error):
    with pytest.raises(ValueError) as exc:
        roster.parse_entries(('workers:\n  - name: worker-1\n' + text + '\n').encode(), tmp_path)
    assert error in str(exc.value)
    with pytest.raises(ValueError) as exc:  # the same rules for a worker's own split:
        roster.parse_entries(('workers:\n  - {name: worker-1, ' + text + '}\n').encode(), tmp_path)
    assert 'workers[0]: ' + error in str(exc.value)


def write_roster(f, text, n=[0]):
    import os
    path = f.run_dir / 'roster.yaml'
    path.write_text(text)
    n[0] += 1
    os.utime(path, ns=(1_700_000_000_000_000_000 + n[0] * 10**9,) * 2)
    f.cfg['roster'] = str(path)
    f.roster_tick()


def test_split_settings_hot_reload_show_in_status_and_bad_values_keep_the_old(cfx):
    assert cfx.split_settings('worker-1') == midturn.SPLIT_DEFAULTS  # no roster: built-in defaults
    assert cfx.agent('worker-1')['split_source'].startswith('check_minutes: default')
    write_roster(cfx, SPLIT_YAML)
    a1, a2 = cfx.agent('worker-1'), cfx.agent('worker-2')  # published at reload, before any delivery
    assert a1['split_check_minutes'] == '90' and a1['split_min_calls'] == '5' and a1['split_window_minutes'] == '5'
    assert a2['split_check_minutes'] == '30' and a2['split_restore_model'] == 'False'
    assert a2['split_source'].startswith('check_minutes: roster; window_minutes: roster; min_calls: default')
    write_roster(cfx, SPLIT_YAML.replace('check_minutes: 30', 'check_minutes: -5'))
    rejected = [json.loads(x) for x in cfx.r.l['fx:log'] if 'roster.rejected' in x][-1]
    assert any('split.check_minutes must be a number >= 0' in e for e in rejected['errors'])
    assert cfx.split_settings('worker-2')['check_minutes'] == 30  # the old values stay
    write_roster(cfx, 'workers:\n  - {name: worker-1, split: {check_minutes: 0}}\n  - name: worker-2\n')
    assert cfx.split_settings('worker-1')['check_minutes'] == 0 and cfx.split_settings('worker-2')['check_minutes'] == 40
    for k in ('split_check_minutes', 'split_source', 'split_last'):
        assert not control_view._secret_key(k)


# ------------------------------------------------------------------ transcript activity, per CLI
def stamp(ago):
    return datetime.fromtimestamp(time.time() - ago, timezone.utc).isoformat().replace('+00:00', 'Z')


def claude_row(ago, name, inp):
    return {'type': 'assistant', 'timestamp': stamp(ago),
            'message': {'content': [{'type': 'tool_use', 'id': 't', 'name': name, 'input': inp}]}}


def codex_row(ago, text, kind='custom_tool_call'):
    payload = {'type': kind, 'name': 'exec', 'input' if kind == 'custom_tool_call' else 'arguments': text}
    return {'type': 'response_item', 'timestamp': stamp(ago), 'payload': payload}


def pi_row(ago, name, args):
    return {'type': 'message', 'timestamp': stamp(ago),
            'message': {'role': 'assistant', 'content': [{'type': 'toolCall', 'id': 't', 'name': name, 'arguments': args}]}}


def rows_for(cli):
    """3 calls outside a 10-minute window, then 6 inside: 3 edits of 2 files, 3 commands (2 distinct)."""
    if cli == 'claude':
        old = [claude_row(900, 'Bash', {'command': 'ls'})] * 3
        new = [claude_row(300, 'Edit', {'file_path': '/w/a.py'}), claude_row(250, 'Write', {'file_path': '/w/b.py'}),
               claude_row(200, 'Edit', {'file_path': '/w/a.py'}), claude_row(150, 'Bash', {'command': 'pytest -q'}),
               claude_row(100, 'Bash', {'command': 'pytest -q'}), claude_row(50, 'Read', {'file_path': '/w/c.py'})]
        side = dict(claude_row(40, 'Bash', {'command': 'subagent'}), isSidechain=True)  # never counted
        return old + new + [side]
    if cli == 'codex':
        old = [codex_row(900, 'ls')] * 3
        patch = '*** Begin Patch\n*** Update File: {}\n@@\n-x\n+y\n*** End Patch'
        new = [codex_row(300, patch.format('/w/a.py')), codex_row(250, patch.format('/w/b.py')),
               codex_row(200, patch.format('/w/a.py')), codex_row(150, '{"cmd": "pytest -q"}', 'function_call'),
               codex_row(100, '{"cmd": "pytest -q"}', 'function_call'), codex_row(50, 'cat /w/c.py')]
        return old + new
    old = [pi_row(900, 'bash', {'command': 'ls'})] * 3
    new = [pi_row(300, 'edit', {'path': '/w/a.py'}), pi_row(250, 'write', {'path': '/w/b.py'}),
           pi_row(200, 'edit', {'path': '/w/a.py'}), pi_row(150, 'bash', {'command': 'pytest -q'}),
           pi_row(100, 'bash', {'command': 'pytest -q'}), pi_row(50, 'read', {'path': '/w/c.py'})]
    return old + new


@pytest.mark.parametrize('cli', ['claude', 'codex', 'pi'])
def test_activity_counts_tool_calls_and_edited_files_inside_the_window(tmp_path, cli):
    path = tmp_path / 'session.jsonl'
    path.write_text('\n'.join(json.dumps(r) for r in rows_for(cli)) + '\n{"type": "assist')  # cut last line
    got = midturn.activity(cli, path, time.time() - 600)
    assert got == {'calls': 6, 'distinct': 4, 'files': 2}  # a.py, b.py, pytest -q, c.py


def test_activity_without_a_tool_call_record_is_unknown(tmp_path):
    path = tmp_path / 'x.jsonl'
    path.write_text(json.dumps(claude_row(10, 'Bash', {'command': 'ls'})) + '\n')
    assert midturn.activity('opencode', path, 0) is None and midturn.activity('dsh', path, 0) is None
    assert midturn.activity('claude', tmp_path / 'missing.jsonl', 0) is None
    assert midturn.activity('claude', path, time.time() + 5) == {'calls': 0, 'distinct': 0, 'files': 0}


def test_probe_says_unknown_for_a_cli_without_a_session_file(tmp_path):
    probe = midturn.Probe()
    probe.update({'worker-1': dict(cli='opencode', generation=0, message_id='m1', thread='', path='', directory='',
                                   window=200000, override=None, split_since=time.time() - 600)})
    probe.poll()
    assert probe.take()['worker-1']['activity'] is None  # -> split.unknown, never a nudge


def test_probe_reports_activity_when_due_even_if_the_file_did_not_change(tmp_path):
    path = tmp_path / 't.jsonl'
    path.write_text('\n'.join(json.dumps(r) for r in rows_for('claude')) + '\n')
    probe = midturn.Probe()
    target = dict(cli='claude', generation=0, message_id='m1', thread='', path=str(path), directory='',
                  window=200000, override=None)
    probe.update({'worker-1': target})
    probe.poll()
    assert 'activity' not in probe.take()['worker-1']  # not due: usage only
    probe.update({'worker-1': dict(target, split_since=time.time() - 600)})
    probe.poll()  # same stat: the usage read is skipped, the due check is not
    assert probe.take()['worker-1']['activity'] == {'calls': 6, 'distinct': 4, 'files': 2}


# ------------------------------------------------------------------ nudge (real v2 Redis, Claude worker)
def flowing(f, slot, minutes=41, calls=20):
    path = f.run_dir / (slot + '-tools.jsonl')
    rows = [claude_row(30 + i, 'Edit' if i % 3 == 0 else 'Bash',
                       {'file_path': '/w/f' + str(i % 4) + '.py'} if i % 3 == 0 else {'command': 'step ' + str(i)})
            for i in range(calls)]
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    f.setf(slot, transcript=str(path), busy_since=time.time() - minutes * 60)


def probe_round(f):
    f.midturn_apply()  # target with split_since
    f.midturn_probe.poll()
    f.midturn_apply()  # result -> decision


def supervisor_texts(f):
    return [f.v2.bus.message(m)['body']['text'] for m in f.v2.bus.pending('supervisor')]


@pytest.fixture
def splittable(monkeypatch):
    calls = []
    monkeypatch.setattr(subfleet, 'check', lambda f, slot, task_id, workers, ready=True, **kw:
                        calls.append((slot, task_id, workers, ready)) or None)
    return calls


def test_flowing_worker_gets_one_nudge_through_posttool_and_the_supervisor_hears(mixed, splittable):
    f = mixed
    dispatch_one(f)  # the supervisor's dispatch carries no id: the program gives it M0
    assert '[M0] Write worker-1/part.md' in f.v2.bus.message(f.r.hget(f.v2.bus.lease_key('worker-1'), 'message_id'))['body']['text']
    flowing(f, 'worker-1')
    probe_round(f)
    info = f.agent('worker-1')
    assert info['split_send_pending'] == '1' and info['split_nudged'] == 'M0' and splittable == [('worker-1', 'M0', 1, False)]
    msg = f.v2.bus.message(info['split_message'])
    assert 'midturn_id' not in msg and not info.get('midturn_id')  # never a §34 handoff: no clock pause, no kill
    assert f.mgr.sent == []  # a Claude pane is never typed into
    prompt = fleet.claude_posttool(f.path, 'worker-1', info['generation'])
    assert prompt.startswith('[FLEET LONG-RUNNING fx-') and len(prompt.splitlines()) == 1
    assert '"escalate"' in prompt and '"M0.0"' in prompt and 'kind' in prompt
    assert fleet.claude_posttool(f.path, 'worker-1', info['generation']) is None  # taken once
    up = [x for x in supervisor_texts(f) if '[long-running]' in x]
    assert len(up) == 1 and '[long-running] worker-1 已跑 41 分钟，最近 10 分钟 20 次工具调用、改了 4 个文件' in up[0]
    f.setf('worker-1', split_recheck_at=0)
    probe_round(f)  # at most one nudge per slot per task
    assert len([x for x in supervisor_texts(f) if '[long-running]' in x]) == 1
    assert f.agent('worker-1')['split_send_pending'] == '0'
    # the worker stops at a safe point and reports with the split proposal: the usual §20 path
    answer_turn(f, 'worker-1', 'Done:\n- part one\nEvidence:\n- worker-1/part.md\nNot done: the rest\nBlocked: none\n'
                + escalate(), prompt=msg['body']['text'])
    assert any('worker-1 asks to split M0 into 2 pieces' in x for x in supervisor_texts(f))


def test_the_nudge_names_the_running_task_not_the_last_queued_one(mixed, splittable):
    f = mixed
    dispatch_one(f)  # M0 runs; a later dispatch M3 to the same worker waits in its queue
    items = json.loads(f.r.get(fleet.ITEMS))
    f.r.set(fleet.ITEMS, json.dumps(dict(items, M3={'to': 'worker-1', 'instruction': 'later work for worker-1'})))
    assert f.report_item('worker-1') == 'M3'
    flowing(f, 'worker-1')
    probe_round(f)
    assert f.agent('worker-1')['split_nudged'] == 'M0' and '"M0.0"' in f.agent('worker-1')['split_prompt']


def test_a_registered_nudge_is_never_handed_to_another_delivery(mixed, splittable):
    f = mixed
    dispatch_one(f)
    flowing(f, 'worker-1')
    probe_round(f)
    assert f.agent('worker-1')['split_send_pending'] == '1'
    f.setf('worker-1', msg_id='m-another')  # the nudged turn ended without a tool call; the next one began
    assert fleet.claude_posttool(f.path, 'worker-1', f.agent('worker-1')['generation']) is None


def test_one_slow_thing_is_not_nudged_and_is_looked_at_again_later(mixed, splittable):
    f = mixed
    dispatch_one(f)
    flowing(f, 'worker-1', calls=3)  # one long command: few tool calls
    probe_round(f)
    info = f.agent('worker-1')
    assert not info.get('split_send_pending') and not info.get('split_nudged')
    assert float(info['split_recheck_at']) > time.time() + 39 * 60
    assert 'calls=3' in info['split_last'] and 'slow' in info['split_last']
    assert not [x for x in supervisor_texts(f) if '[long-running]' in x]


def test_not_yet_due_off_or_unsplittable_is_never_nudged(mixed, monkeypatch):
    f = mixed
    dispatch_one(f)
    flowing(f, 'worker-1', minutes=39)
    monkeypatch.setattr(subfleet, 'check', lambda *a, **k: None)
    probe_round(f)
    assert not f.agent('worker-1').get('split_send_pending')  # busy 39 < 40 minutes
    flowing(f, 'worker-1')
    f.split_table = {'check_minutes': 0}  # roster: off for everyone
    probe_round(f)
    assert not f.agent('worker-1').get('split_send_pending')
    f.split_table = {}
    monkeypatch.setattr(subfleet, 'check', lambda *a, **k: 'max_children 3: this fleet already runs M1, M2, M3')
    probe_round(f)
    info = f.agent('worker-1')
    assert not info.get('split_send_pending') and 'not splittable: max_children 3' in info['split_last']


# ------------------------------------------------------------------ approve with models (§2 step 4)
def plain_m0(f):
    f.handle({'kind': 'task', 'request_id': 'r1', 'spec': {'goal': 'research report'}})
    sup(f, {'status': 'RUNNING', 'completion': 1, 'dispatch': [
        {'id': 'M0', 'to': 'worker-1', 'instruction': 'Write worker-1/x.md with the findings'}]})
    report(f, 'worker-1', escalate())
    assert 'worker-1 asks to split M0' in inbox(f, 'supervisor')[-1]


def approve(f, **extra):
    sup(f, {'status': 'RUNNING', 'completion': 2, 'dispatch': [],
            'escalations': [dict({'task': 'M0', 'decision': 'approve'}, **extra)]})


@pytest.mark.parametrize('extra,error', [
    ({'supervisor_profile': 5}, 'escalations: supervisor_profile must be an agent card id'),
    ({'worker_profiles': 'claude-opus'}, 'escalations: worker_profiles must be a list of agent card ids'),
    ({'worker_profiles': ['claude-opus'], 'workers': 2}, 'escalations: worker_profiles has 1 entries but workers is 2'),
    ({'decision': 'reject', 'supervisor_profile': 'claude-opus'}, 'only an approve picks models'),
])
def test_profile_shape_errors_are_bad_replies(cf, ok_check, extra, error):
    plain_m0(cf)
    approve(cf, **extra)
    assert error in inbox(cf, 'supervisor')[-1] and cf.task()['bad_replies'] == '1'
    assert cf.escalation_state()['M0']['status'] == 'escalated'


def fable_on_worker_2(f):
    registry = AgentRegistry()
    launch = profile_launch.resolve_launch(registry.get('claude-fable'), registry, f.cfg)
    f.setf('worker-2', runtime_launch=profile_launch.encode_launch(launch))


@pytest.mark.parametrize('extra,reason', [
    ({'supervisor_profile': 'nope'}, 'supervisor_profile nope: unknown agent profile nope'),
    ({'supervisor_profile': 'flashnext-coder'}, 'supervisor_profile flashnext-coder runs pi: only an official '
                                                'Claude Code session can become a sub-fleet supervisor'),
    ({'supervisor_profile': 'claude-fable'}, 'supervisor_profile claude-fable: max_concurrent 1 for model fable'),
    ({'worker_profiles': ['claude-fable', 'claude-fable']}, 'model fable: 2 slots (worker-1, worker-2) exceed '
                                                            'max_concurrent 1'),
    ({'worker_profiles': ['claude-opus', 'nope']}, 'worker-2: unknown agent profile nope'),
])
def test_invalid_models_refuse_the_approve_with_the_reason_and_keep_it_open(cf, ok_check, monkeypatch, extra, reason):
    monkeypatch.setattr(fleet.Fleet, 'preflight_profile', lambda self, launch, cli_change_ok=False: None)
    plain_m0(cf)
    fable_on_worker_2(cf)
    approve(cf, **extra)
    text = inbox(cf, 'supervisor')[-1]
    assert '[escalate] approve of M0 refused by the program: ' in text and reason in text
    assert cf.task()['bad_replies'] == '0'  # a refusal, not a malformed reply
    assert cf.escalation_state()['M0']['status'] == 'escalated'
    assert not (cf.run_dir / 'subfleet-requests').exists() or not list((cf.run_dir / 'subfleet-requests').glob('sf-*'))


def test_approve_with_valid_models_is_recorded(cf, ok_check, monkeypatch):
    monkeypatch.setattr(fleet.Fleet, 'preflight_profile', lambda self, launch, cli_change_ok=False: None)
    plain_m0(cf)
    approve(cf, supervisor_profile='claude-opus', worker_profiles=['claude-haiku', 'claude-sonnet'])
    rec = cf.escalation_state()['M0']
    assert rec['status'] == 'rotating' and rec['supervisor_profile'] == 'claude-opus'
    assert rec['spec']['worker_profiles'] == ['claude-haiku', 'claude-sonnet'] and rec['spec']['workers'] == 2
    assert json.loads(rec['launch'])['profile_id'] == 'claude-opus'
    assert json.loads(cf.agent('worker-1')['split_restore']) == {'launch': ''}  # back to the run default


def test_reopened_split_keeps_the_original_model_and_a_reject_restores_it(cf, ok_check, monkeypatch):
    monkeypatch.setattr(fleet.Fleet, 'preflight_profile', lambda self, launch, cli_change_ok=False: None)
    plain_m0(cf)
    approve(cf, supervisor_profile='claude-opus')
    reopen = lambda: cf.save_escalations(dict(cf.escalation_state(), M0=dict(cf.escalation_state()['M0'],  # noqa: E731
                                                                              status='escalated')))
    reopen()  # rotated, then the spawn was refused: the request is open again
    cf.setf('worker-1', runtime_launch='{"profile_id":"claude-opus"}')
    approve(cf, supervisor_profile='claude-sonnet')
    assert json.loads(cf.agent('worker-1')['split_restore']) == {'launch': ''}  # still the slot's own model
    reopen()
    sup(cf, {'status': 'RUNNING', 'completion': 3, 'dispatch': [], 'escalations': [{'task': 'M0', 'decision': 'reject'}]})
    info = cf.agent('worker-1')
    assert not info['split_restore'] and json.loads(info['split_launch']) == {'launch': ''}  # its next generation


def test_status_shows_the_split_block(tmp_path, monkeypatch):
    from test_control_view_v2 import Reader, Store, seed
    store = Store()
    cfg = {'run': str(tmp_path / 'run-test'), 'redis_socket': '/tmp/fleetmux-test.sock', 'workers': ['worker-1']}
    monkeypatch.setattr(control_view, '_open_client', lambda cfg, *, blocking=False: Reader(store, blocking))
    prefix = 'fx:v2:run:run-test'
    seed(store, prefix)
    store.hashes.setdefault('fx:agent:worker-1', {}).update(
        split_check_minutes='40', split_window_minutes='10', split_min_calls='15', split_min_distinct='2',
        split_restore_model='True', split_source='check_minutes: default', split_last='calls=3 slow')
    split = control_view.snapshot(cfg)['slots']['worker-1']['split']
    assert split['check_minutes'] == '40' and split['last'] == 'calls=3 slow' and split['source'] == 'check_minutes: default'


# ------------------------------------------------------------------ auto task ids (§2 step 8)
def test_dispatch_without_id_gets_the_next_free_id_and_explicit_ids_are_unchanged(cf):
    cf.handle({'kind': 'task', 'request_id': 'r1', 'spec': {'goal': 'research report'}})
    sup(cf, {'status': 'RUNNING', 'completion': 1, 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Write worker-1/a.md with the findings'},
        {'id': 'M1', 'to': 'worker-2', 'instruction': 'Write worker-2/b.md with the findings'},
        {'to': 'worker-3', 'instruction': 'Write worker-3/c.md with the findings'}]})
    assert '] [M0] Write worker-1/a.md' in inbox(cf, 'worker-1')[-1]
    assert '] [M1] Write worker-2/b.md' in inbox(cf, 'worker-2')[-1]
    assert '] [M2] Write worker-3/c.md' in inbox(cf, 'worker-3')[-1]
    assert set(json.loads(cf.r.get(fleet.ITEMS))) == {'M0', 'M1', 'M2'}
    cf.cfg['task_id'] = 'M4'  # inside sub-fleet M4 the ids are M4.<n>
    assert cf.next_item_id([]) == 'M4.0'


# ------------------------------------------------------------------ v2 tree: approve with models end to end
@pytest.fixture
def ttree(tree, monkeypatch):
    """``tree`` with the dual-display primitives every real backend has (cmux / fleetmux; fakes from
    test_subfleet_tmux): a spawn that asks for a fleet tmux session gets one, every other tab is a plain pane."""
    import test_subfleet_tmux as tt
    from test_subfleet import WorldMgr
    for name, fn in (('tmux_view_info', tt.tmux_view_info), ('attach_view', tt.attach_view),
                     ('detach_view', tt.detach_view), ('_meta', tt.meta)):
        monkeypatch.setattr(WorldMgr, name, fn, raising=False)
    tree.world.views, tree.world.detached, tree.world.tmux_owner = {}, [], {}
    spawn = WorldMgr.spawn_interactive_cli

    def spawn_cli(self, **kw):
        res = spawn(self, **kw)
        if kw.get('tmux_session'):
            self.w.tmux_owner[res.pane_id] = kw['tmux_session']
        return res
    monkeypatch.setattr(WorldMgr, 'spawn_interactive_cli', spawn_cli)
    return tree


def escalate_m0(t, f):
    """worker-1 runs M0 and asks to split it; returns the supervisor's [escalate] prompt."""
    decision = {'status': 'RUNNING', 'completion': 5, 'reason': 'go', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Research five sources and write worker-1/m0.md'}]}
    assert hook(t, f, 'supervisor', json.dumps(decision))['status'] == 'committed'
    step(t)
    work = given(t, f, 'worker-1')[-1]
    assert '[M0] Research five sources' in work  # auto id
    text = 'Done:\n- read the goal\nEvidence:\n- listed\nNot done: all of it\nBlocked: none\n' + escalate()
    assert hook(t, f, 'worker-1', text, prompt=work)['status'] == 'committed'
    step(t)
    return received(t.world, f.agent('supervisor')['pane'])[-1]


def rotate_for_split(t, f):
    """The approved split's handoff: the worker writes its note, its next generation starts and answers READY."""
    f.tick()  # idle slot: the handoff turn starts (the worker writes its note)
    assert f.agent('worker-1')['state'] == 'handoff'
    step(t)
    ask = given(t, f, 'worker-1')[-1]
    assert 'sub-fleet M0' in ask
    old = f.agent('worker-1')['pane']
    assert hook(t, f, 'worker-1', 'handoff written', prompt=ask)['status'] in ('committed', 'handoff')
    step(t)
    info = f.agent('worker-1')
    assert info['generation'] == '1' and old not in t.world.alive
    assert f.mgr.spawned[-1]['tmux_session'] == 'worker-1-g1'  # the new generation runs in the fleet's tmux
    assert hook(t, f, 'worker-1', 'READY', prompt='startup')['status'] == 'ready'
    return info


def assert_mapped(t, f, rec):
    """Plan fleetmux-worker-area-mapping §2: the parent keeps a live tab of the sub-fleet supervisor, no placeholder."""
    assert rec['view'] == 1 and rec['placeholder'] == '', rec
    assert not [w for w in getattr(f.mgr, 'windows', []) if w[0].startswith('placeholder-')]
    assert f.mgr.owns(rec['pane']) and f.agent('worker-1')['pane'] == rec['pane']
    assert subfleet.record(rec['run'])['view'] == 1


def test_approve_with_models_rotates_the_worker_then_adopts_it_and_restores_on_return(ttree, monkeypatch):
    t, f = ttree, ttree.f
    monkeypatch.setattr(subfleet, 'default_launch', no_launch)
    monkeypatch.setattr(fleet.Fleet, 'preflight_profile', lambda self, launch, cli_change_ok=False: None)
    assert not f.cfg.get('allow_profile_live_switch')  # the split path never needs the §12 live switch
    original = f.agent('worker-1').get('runtime_launch', '')
    asked = escalate_m0(t, f)
    ok = {'status': 'RUNNING', 'completion': 10, 'reason': 'split', 'dispatch': [], 'escalations': [
        {'task': 'M0', 'decision': 'approve', 'supervisor_profile': 'claude-opus',
         'worker_profiles': ['claude-haiku', 'claude-sonnet']}]}
    assert hook(t, f, 'supervisor', json.dumps(ok), prompt=asked)['status'] == 'committed'
    assert f.task()['bad_replies'] == '0' and f.escalation_state()['M0']['status'] == 'rotating'
    assert f.agent('worker-1')['tmux_view'] == '1'  # its next generations start in the fleet's tmux
    info = rotate_for_split(t, f)
    assert json.loads(info['runtime_launch'])['profile_id'] == 'claude-opus'  # the supervisor's pick
    f.tick()  # the new generation is READY: the spawn request is written
    f.tick()  # ... and executed: the new pane becomes sub-fleet M0's supervisor
    assert f.escalation_state()['M0']['status'] == 'delegated' and f.agent('worker-1')['state'] == 'delegated'
    rec = subfleet.record(f.run_dir / 'sub' / 'M0')
    assert rec['pane'] == info['pane'] and rec['generation'] == 1
    assert_mapped(t, f, rec)
    ccfg = fleet.load(Path(rec['run']) / 'config.json')
    assert ccfg['worker_profiles'] == {'worker-1': 'claude-haiku', 'worker-2': 'claude-sonnet'}
    # the pane's hooks are the parent's worker-1 g1 ones: routed to the child's supervisor while delegated
    route = subfleet.delegated_route(f.cfg, 'worker-1', 1)
    assert route[1:] == ('supervisor', 0) and route[0]['run'] == rec['run']
    cf, _ = adopt_by_hook(t, monkeypatch, rec)  # §48: the role change comes through that hook, never typed
    assert [json.loads(cf.agent(w)['runtime_launch'])['profile_id'] for w in cf.workers] == ['claude-haiku', 'claude-sonnet']
    view = cf.agent('supervisor')['pane']  # the child's second client tab of the same tmux session
    assert view != rec['pane'] and t.world.views[view]['tmux_session'] == 'worker-1-g1'
    assert not received(t.world, view)
    cf.v2.finish('completed', 'merged')
    cf.write_result()
    f.tick()
    stop_child_like_daemon(cf)
    assert t.world.detached == [view]  # the child only disconnected its client
    f.tick()
    assert subfleet.record(rec['run'])['state'] == 'returned'
    back = f.agent('worker-1')
    assert back['generation'] == '2' and back.get('runtime_launch', '') == original  # restore_model (default)
    assert not back.get('split_restore')
    assert f.mgr.spawned[-1]['tmux_session'] == 'worker-1-g2'  # the slot stays in the fleet's tmux
    assert subfleet.delegated_route(f.cfg, 'worker-1', 1)[1] == 'worker-1'  # hooks back on the parent slot


def test_approve_without_models_rotates_a_plain_pane_into_tmux_first_and_no_parent_profiles_leak(ttree, monkeypatch):
    t, f = ttree, ttree.f
    monkeypatch.setattr(subfleet, 'default_launch', no_launch)
    original = f.agent('worker-1').get('runtime_launch', '')
    asked = escalate_m0(t, f)
    ok = {'status': 'RUNNING', 'completion': 10, 'reason': 'split', 'dispatch': [],
          'escalations': [{'task': 'M0', 'decision': 'approve'}]}
    assert hook(t, f, 'supervisor', json.dumps(ok), prompt=asked)['status'] == 'committed'
    rec = f.escalation_state()['M0']
    assert rec['status'] == 'rotating' and not rec.get('launch')  # same model: a handoff into tmux only
    assert f.agent('worker-1')['tmux_view'] == '1' and not f.agent('worker-1').get('split_restore')
    info = rotate_for_split(t, f)
    assert info.get('runtime_launch', '') == original
    f.cfg['worker_profiles'] = {'worker-1': 'claude-opus'}  # the parent's own per-slot profile (same slot name)
    f.tick()
    f.tick()
    assert f.escalation_state()['M0']['status'] == 'delegated' and f.agent('worker-1')['state'] == 'delegated'
    rec = subfleet.record(f.run_dir / 'sub' / 'M0')
    assert rec['pane'] == info['pane']
    assert_mapped(t, f, rec)
    ccfg = fleet.load(f.run_dir / 'sub' / 'M0' / 'config.json')
    assert ccfg['worker_profiles'] == {}


def test_approve_without_models_adopts_a_slot_already_in_tmux_in_place(ttree, monkeypatch):
    t, f = ttree, ttree.f
    monkeypatch.setattr(subfleet, 'default_launch', no_launch)
    sid = f.agent('worker-1')['pane']
    f.setf('worker-1', tmux_view=1)  # e.g. back from an earlier sub-fleet: the slot stayed in the fleet's tmux
    t.world.tmux_owner[sid] = 'worker-1-g0'
    asked = escalate_m0(t, f)
    gen = f.agent('worker-1')['generation']
    ok = {'status': 'RUNNING', 'completion': 10, 'reason': 'split', 'dispatch': [],
          'escalations': [{'task': 'M0', 'decision': 'approve'}]}
    assert hook(t, f, 'supervisor', json.dumps(ok), prompt=asked)['status'] == 'committed'
    assert f.escalation_state()['M0']['status'] == 'approved'  # no rotation
    f.tick()
    assert f.escalation_state()['M0']['status'] == 'delegated' and f.agent('worker-1')['generation'] == gen
    rec = subfleet.record(f.run_dir / 'sub' / 'M0')
    assert rec['pane'] == sid
    assert_mapped(t, f, rec)


# ------------------------------------------------------------------ an approved split takes hold of the slot
TRIAL_REPORT = ('Done:\n- mod01..mod17 built and committed\nEvidence:\n- worker-1/toy\nNot done: mod18..mod40\n'
                'Blocked: the shared RELEASES.md is outside my directory\n')


def audit(f, kind):
    return [a for _, a in f.r.xrange(f.v2.bus.audit_key) if a.get('event') == kind]


@pytest.mark.parametrize('order', ['approve first', 'ruling first'])
def test_an_approved_split_wins_over_a_continue_ruling_in_the_same_decision(ttree, monkeypatch, order):
    """Live trial fleet-20261108-205242-43db976b, 20:55:27: one decision approved the split of M0 and rejected
    blocked:worker-1 with "skip that line and continue". The ruling went to worker-1 at once, it read "[M0] reject"
    as the split being rejected and did the remaining 23 modules itself. Now the split holds the slot: the ruling
    on the same task is superseded (logged, the supervisor told once), nothing reaches the worker, the handoff
    starts on the next tick, in either order inside the decision."""
    t, f = ttree, ttree.f
    monkeypatch.setattr(subfleet, 'default_launch', no_launch)
    monkeypatch.setattr(fleet.Fleet, 'preflight_profile', lambda self, launch, cli_change_ok=False: None)
    decision = {'status': 'RUNNING', 'completion': 5, 'reason': 'go', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Build forty modules in worker-1/toy, one commit each'}]}
    assert hook(t, f, 'supervisor', json.dumps(decision))['status'] == 'committed'
    step(t)
    work = given(t, f, 'worker-1')[-1]
    assert hook(t, f, 'worker-1', TRIAL_REPORT + escalate(), prompt=work)['status'] == 'committed'
    step(t)
    asked = received(t.world, f.agent('supervisor')['pane'])[-1]
    assert f.escalation_state()['blocked:worker-1']['item'] == 'M0'
    answers = [{'task': 'M0', 'decision': 'approve', 'workers': 2, 'supervisor_profile': 'claude-opus',
                'worker_profiles': ['claude-haiku', 'claude-haiku']},
               {'task': 'blocked:worker-1', 'decision': 'reject',
                'note': 'skip that line and continue; the release log is ruled on after the split'}]
    if order == 'ruling first':
        answers.reverse()
    ok = {'status': 'RUNNING', 'completion': 45, 'reason': 'split', 'dispatch': [], 'escalations': answers}
    before = len(given(t, f, 'worker-1'))
    assert hook(t, f, 'supervisor', json.dumps(ok), prompt=asked)['status'] == 'committed'
    assert f.task()['bad_replies'] == '0'
    esc = f.escalation_state()
    assert esc['M0']['status'] == 'rotating' and 'blocked:worker-1' not in esc
    step(t)
    assert len(given(t, f, 'worker-1')) == before  # nothing reached the worker
    assert not [m for m in f.v2.bus.pending('worker-1')]
    superseded, = audit(f, 'blocked.superseded')
    assert superseded['slot'] == 'worker-1' and superseded['task'] == 'blocked:worker-1'
    # the supervisor hears it once, on its next message (no turn of its own)
    f.queue('supervisor', '[worker-2 report] something later')
    f.queue('supervisor', '[worker-2 report] once more')
    later, more = [x for x in supervisor_texts(f) if '[worker-2 report]' in x]
    assert 'blocked:worker-1' in later and 'superseded by the approved split of M0' in later
    assert 'superseded' not in more
    rotate_for_split(t, f)  # the idle slot hands off on the next tick, as approved


def test_an_approve_tells_a_busy_worker_to_stop_through_posttool(mixed, splittable):
    """The approve reaches a worker that is in a turn at once, through the §44 PostToolUse channel (program text,
    one line, taken once), so it starts no new work before the handoff."""
    f = mixed
    dispatch_one(f)  # worker-1 runs M0
    f.save_escalations({'M0': {'slot': 'worker-1', 'instruction': 'Write worker-1/part.md with the findings.',
                               'proposal': [], 'workers': 2, 'status': 'escalated', 'asked_at': time.time()}})
    f.queue('supervisor', '[escalate] worker-1 asks to split M0 into 2 pieces')  # the supervisor's turn to answer
    f.pump()
    body = {'status': 'RUNNING', 'completion': 20, 'reason': 'r', 'dispatch': [],
            'escalations': [{'task': 'M0', 'decision': 'approve'}]}
    assert answer_turn(f, 'supervisor', json.dumps(body))['status'] == 'committed'
    assert f.task()['bad_replies'] == '0' and f.escalation_state()['M0']['status'] in ('approved', 'rotating')
    info = f.agent('worker-1')
    assert info['split_send_pending'] == '1'
    prompt = fleet.claude_posttool(f.path, 'worker-1', info['generation'])
    assert prompt.startswith(midturn.SPLIT_PREFIX) and len(prompt.splitlines()) == 1
    assert 'approved the split of M0' in prompt and 'start nothing new' in prompt and '"escalate"' not in prompt
    assert fleet.claude_posttool(f.path, 'worker-1', info['generation']) is None  # taken once


def test_no_dispatch_to_a_slot_while_its_approved_split_hands_over(cf, ok_check, monkeypatch):
    monkeypatch.setattr(fleet.Fleet, 'preflight_profile', lambda self, launch, cli_change_ok=False: None)
    plain_m0(cf)
    approve(cf, supervisor_profile='claude-opus')
    assert cf.escalation_state()['M0']['status'] == 'rotating'
    sup(cf, {'status': 'RUNNING', 'completion': 3, 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Also write worker-1/extra.md with notes'}]})
    assert cf.task()['bad_replies'] == '1'
    assert 'hands worker-1 over to sub-fleet M0' in inbox(cf, 'supervisor')[-1]
    assert not [x for x in inbox(cf, 'worker-1') if 'extra.md' in x]


def test_worker_report_board_marks_the_slot_of_an_approved_split(cf, ok_check):
    """缺陷 4: the task-mode [worker report] tail shows the held split on the slot's line (worker_board)."""
    plain_m0(cf)
    approve(cf)
    status = cf.escalation_state()['M0']['status']
    assert status in ('approved', 'rotating')
    sup(cf, {'status': 'RUNNING', 'completion': 3, 'dispatch': [
        {'to': 'worker-2', 'instruction': 'Write worker-2/y.md with the notes'}]})
    report(cf, 'worker-2')
    text = inbox(cf, 'supervisor')[-1]
    board = text[text.index('Worker status now'):]
    line = next(x for x in board.splitlines() if x.startswith('- worker-1:'))
    assert 'split M0 ' + status in line and 'do not route' in line, line
