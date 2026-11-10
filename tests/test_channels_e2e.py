"""Channels end to end (brief v2 §6.17 / §4-1; integrator-1).

In-process RuntimeV2 (step() / tick() driven) on the repo's private Unix-socket test Redis, the P0
``FakeChannel`` with fake transports, fake cmux screens. The actor's ``handle_inbound`` / ``outbound_scan`` /
``process_one`` are called turn by turn in the test; the actor restart case uses this test's own child process.
No TCP listener, no real network, no credentials, no user program, nothing deleted.

Phase A2: red until the packages land (P1b–P1g, P6b–P6c, P7a–P7b); the import of a package that is not there yet
happens inside the test body, so a missing package is a FAILED test, never a collection ERROR. Only
``test_fleet_without_channels_has_no_channel_keys_audits_or_actor`` is a baseline and green now.
At the checkpoint integrator-1 may adjust harness wiring (marked "harness"), never the assertions.
"""
import inspect
import json
import os
import signal
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest
import redis

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import event, fx, receive, result, session, settle  # noqa: F401  (fixture + helpers)
from test_v2_codex_claude import dispatch_one, mixed  # noqa: F401  (fixture + helper)
from test_watchdog import DIALOG_RM_LITERAL, keys, wd  # noqa: F401  (fixture + helpers)
from test_channels_contract import Audit, FakeChannel, _fake_transport, config_dir  # noqa: F401
from cmux_fleet import channels, fleet
from cmux_fleet.channels import envelope

ALLOWED = 'u-1'


# ---------------------------------------------------------------- harness
def unix_client(r, **override):
    """A new client on the same private Redis Unix socket with the pool's own options. A Unix-socket pool keeps the
    socket as ``path``, which redis.Redis() takes as ``unix_socket_path``; ``override`` (e.g. ``db=1``) wins."""
    kwargs = dict(r.connection_pool.connection_kwargs)
    kwargs['unix_socket_path'] = kwargs.pop('path')
    kwargs.update(override)
    return redis.Redis(**kwargs)


def own_client(f):
    """A second client on the same private Redis (the fixture pool allows one connection)."""
    return unix_client(f.r)


def ask_fleet(f, timeout=5):
    """harness: ``submit`` for actor.handle_inbound in process. actor.submit_operator_input runs in a thread on
    its own client while this thread steps the in-process RuntimeV2 (there is no daemon process here)."""
    from cmux_fleet.channels import actor

    def submit(text, origin):
        box, r2 = {}, own_client(f)
        t = threading.Thread(target=lambda: box.update(
            out=actor.submit_operator_input(f.cfg, r2, text, origin, timeout=timeout)))
        t.start()
        deadline = time.time() + timeout + 1
        while t.is_alive() and time.time() < deadline:
            f.v2.step(10)
            time.sleep(0.01)
        t.join(1)
        return box.get('out', {'status': 'pending'})
    return submit


def channel(f, account='bot-1', allow=ALLOWED):
    """A FakeChannel on the run's private Redis store (actor.RedisStore), with its own audit and transport."""
    from cmux_fleet.channels import actor
    audit, transport = Audit(), _fake_transport()
    ch = FakeChannel(channels.Account('fake', account, [allow], {'PUSH_TO': allow}), transport=transport,
                     store=actor.RedisStore(f.r, 'fake', account), audit=audit)
    return ch, transport, audit


def inbound(f, ch, text, *, item_id, sender=ALLOWED, replies=None, post=None):
    from cmux_fleet.channels import actor
    replies = [] if replies is None else replies
    raw = {'id': item_id, 'chat': 'c-1', 'from': sender, 'text': text}
    out = ch.inbound(raw, accept=lambda env: actor.handle_inbound(
        f.r, env, submit=ask_fleet(f), reply=lambda e, t: replies.append(t), post=post))
    return out, replies


def events_of(f, kind):
    return [json.loads(fields['data']) for _, fields in f.r.xrange(f.v2.events)   # runtime_v2.post_event's stream
            if json.loads(fields['data']).get('kind') == kind]


def channel_audits(f):
    return [fields for _, fields in f.r.xrange(f.v2.bus.audit_key) if fields.get('event', '').startswith('channel.')]


def push_out(f, entries, chans, *, policy=None):
    """One outbound round: scan results / task / escalations, route, send everything queued.
    policy (test-contract M184): None -> as before; otherwise actor._guards(...) as actor.outbound_loop applies it:
    the route guard before routing, the queue guard handed to queue.process_one(guard=)."""
    from cmux_fleet.channels import actor, queue
    actor.outbound_scan(f.r, entries)
    accounts = {(c.type, c.account.name): c.account for c in chans}
    route_guard, guard = actor._guards(f.r, policy, actor._safe_audit(f.r, None),
                                       {(c.type, c.account.name): c for c in chans}, entries, accounts)
    while f.r.llen(channels.OUTQ):
        item = json.loads(f.r.rpop(channels.OUTQ))
        if route_guard is not None:
            item = route_guard(item)
            if item is None:
                continue  # blocked: the guard routed its notice instead
        target = actor.resolve_target(f.r, item, entries, accounts)
        if target:
            queue.route(f.r, dict(item, **target), target)
    sent = []
    for c in chans:
        key = channels.OUTQ_ACCOUNT + c.type + ':' + c.account.name
        while f.r.llen(key):
            sent.append(queue.process_one(f.r, lambda it: c.send_item(it) if hasattr(c, 'send_item')
                                          else c.send_text(it['conversation'], it.get('text') or '',
                                                           reply_ref=it.get('reply_ref')), sleep=lambda s: None,
                                          block=0, key=key, guard=guard))
    return sent


def entries_for(*accounts):
    return [{'type': 'fake', 'account': a, 'push': list(channels.DEFAULT_PUSH) + ['escalation']} for a in accounts]


@pytest.fixture
def fake_type():
    undo = channels.register('fake', FakeChannel, 'fake')
    yield
    undo()


# ---------------------------------------------------------------- 1
def test_inbound_to_result_round_trip_via_origin(fx, fake_type):
    f = fx
    ch, transport, _ = channel(f)
    out, replies = inbound(f, ch, '让 worker-1 回 OK', item_id=11)
    assert [e['text'] for e in out] == ['让 worker-1 回 OK']
    rid = json.loads(f.r.xrange(f.v2.prefix + ':reply:' + events_of(f, 'operator_input')[0]['request_id'])[0][1]
                     ['data'])['id']
    origin = json.loads(f.r.get(channels.ORIGIN + rid))
    assert origin['conversation'] == 'c-1' and origin['reply_ref'] == {'chat': 'c-1'}
    f.pump(); sup = receive(f, 'supervisor')
    settle(f, 'supervisor', sup, {'status': 'RUNNING', 'dispatch': [{'to': 'worker-1', 'instruction': 'Reply OK in the reply field.'}],
                                  'skip': []})
    f.pump(); worker = receive(f, 'worker-1')
    settle(f, 'worker-1', worker, {'reply': 'OK'})
    assert result(f, rid)['result'] == {'reply': 'OK'}
    transport.sent.clear()
    push_out(f, entries_for('bot-1'), [ch])
    texts = [(op, d) for op, d in transport.sent if op == 'send_text']
    assert len(texts) == 1
    assert texts[0][1]['to'] == 'c-1' and texts[0][1]['reply_ref'] == {'chat': 'c-1'} and 'OK' in texts[0][1]['text']


# ---------------------------------------------------------------- 2
def test_mid_request_text_reaches_supervisor_as_second_request(fx, fake_type):
    f = fx
    ch, _, _ = channel(f)
    inbound(f, ch, '让 worker-1 整理报告', item_id=21)
    f.pump(); first = receive(f, 'supervisor')
    inbound(f, ch, '另外把结论写短一点', item_id=22)  # while the first request is still open
    inputs = events_of(f, 'operator_input')
    assert len(inputs) == 2 and inputs[0]['request_id'] != inputs[1]['request_id']
    settle(f, 'supervisor', first, {'status': 'RUNNING', 'dispatch': [], 'skip': []})
    f.pump(); second = receive(f, 'supervisor')
    assert second['message_id'] != first['message_id']
    assert '结论写短' in json.dumps(second['body'], ensure_ascii=False)  # its own delivery, not merged into the first


# ---------------------------------------------------------------- 3
def test_outsider_dropped_end_to_end(fx, fake_type):
    f = fx
    ch, transport, audit = channel(f)
    out, replies = inbound(f, ch, '帮我删库', item_id=31, sender='u-9')
    assert out == [] and replies == []
    assert audit.events().count('channel.drop_policy') == 1
    assert transport.sent == [] and events_of(f, 'operator_input') == []


# ---------------------------------------------------------------- 4
def test_second_fleet_same_credential_refused_by_lease(private_redis_v2, tmp_path, config_dir):  # noqa: F811
    from cmux_fleet.channels import lease
    r1 = private_redis_v2
    r1.flushdb()
    r2 = unix_client(r1, db=1)  # the second fleet's Redis (same socket, isolated DB 1)
    r2.flushdb()
    entries = [{'type': 'fake', 'account': 'shared', 'push': ['result']},
               {'type': 'fake', 'account': 'only-b', 'push': ['result']}]
    audit_a, audit_b = Audit(), Audit()

    def status(r):
        return lambda type_, account, **fields: r.hset(channels.STATUS + type_ + ':' + account, mapping=fields)
    try:
        first = lease.acquire_all(entries[:1], run_id='run-a', run=str(tmp_path / 'a'), audit=audit_a,
                                  status=status(r1))
        assert first[('fake', 'shared')] is not None
        second = lease.acquire_all(entries, run_id='run-b', run=str(tmp_path / 'b'), audit=audit_b,
                                   status=status(r2))
        assert second[('fake', 'shared')] is None and second[('fake', 'only-b')] is not None
        busy = [f for e, f in audit_b if e == 'channel.lease_busy']
        assert len(busy) == 1 and busy[0].get('holder', {}).get('run_id', busy[0].get('run_id')) == 'run-a'
        assert r2.hget(channels.STATUS + 'fake:shared', 'state') == 'lease_busy'
    finally:
        for got in (locals().get('first') or {}, locals().get('second') or {}):
            for held in got.values():
                if held is not None:
                    held.release()
        r2.flushdb()


# ---------------------------------------------------------------- 5
def test_actor_process_killed_is_respawned(private_redis_v2, tmp_path):  # noqa: F811
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    config = str(tmp_path / 'config.json')
    plans = actor.plan_processes({'channels': [{'type': 'fake', 'account': 'a'}, {'type': 'fake', 'account': 'b'}]},
                                 config)
    assert len(plans) == 1 and plans[0][-3:] == ['channel', 'actor', config]  # C13: one actor per fleet
    sock = r.connection_pool.connection_kwargs['path']
    child = ('import os, sys, time, redis\n'
             'redis.Redis(unix_socket_path=sys.argv[1]).hset(sys.argv[2], "pid", os.getpid())\n'
             'time.sleep(120)\n')
    procs, now, audit = [], [1000.0], Audit()

    def spawn():  # harness: this test's own child stands in for `channel actor <config>`
        procs.append(subprocess.Popen([sys.executable, '-c', child, sock, channels.ACTOR]))
        return procs[-1]

    def pid_after(old):
        deadline = time.time() + 5
        while time.time() < deadline:
            got = r.hget(channels.ACTOR, 'pid')
            if got and got != old:
                return got
            time.sleep(0.05)
        return None
    sup = actor.Supervisor(spawn=spawn, clock=lambda: now[0], audit=audit)
    try:
        sup.tick()
        first = pid_after(None)
        assert first == str(procs[0].pid)
        os.kill(procs[0].pid, signal.SIGKILL)  # only this test's own child
        procs[0].wait(5)
        sup.tick()  # notices the exit, schedules the restart
        now[0] += 31  # past the 30 s backoff
        sup.tick()
        second = pid_after(first)
        assert second is not None and second != first and second == str(procs[-1].pid)
        assert 'channel.actor.restarted' in audit.events()
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
            p.wait(5)


# ---------------------------------------------------------------- 6
def test_task_done_and_run_closed_reach_last_interaction(fx, fake_type, monkeypatch):
    from cmux_fleet.channels import actor
    f = fx
    ch, _, _ = channel(f)
    f.cfg['channels'] = entries_for('bot-1')
    actor.remember_last(f.r, envelope.make('fake', 'bot-1', conversation='c-1', message_id='61:c-1', sender=ALLOWED,
                                           text='hi', reply_ref={'chat': 'c-1'}, at=1762650000))
    # task part: a completed task becomes a task_done item that lands on the last interaction
    f.service = False; f.cfg['mode'] = 'task'
    event(f, dict(kind='task', request_id='new-task', spec={'goal': 'say done'}))
    f.pump(); env = receive(f, 'supervisor')
    (f.workspace() / 'done.md').write_text('done: said done')  # test-contract M109: one artifact for independent verification
    settle(f, 'supervisor', env, {'status': 'COMPLETED', 'completion': 100, 'dispatch': [], 'reason': 'trivial',
                                  'artifacts': [{'path': 'done.md', 'contains': ['done']}]})
    assert f.task()['status'] == 'completed'
    actor.outbound_scan(f.r, f.cfg['channels'])
    items = [json.loads(x) for x in f.r.lrange(channels.OUTQ, 0, -1)]
    done = [i for i in items if i['event'] == 'task_done']
    assert len(done) == 1
    target = actor.resolve_target(f.r, done[0], f.cfg['channels'], {('fake', 'bot-1'): ch.account})
    assert (target['channel'], target['account'], target['conversation']) == ('fake', 'bot-1', 'c-1')
    f.r.delete(channels.OUTQ)
    # service, no lifecycle: the explicit stop tail queues run_closed, then drains the actor (bounded), before Redis
    # is closed; a drain that runs out of time does not hold the stop (§5i I-4)
    f.service = True; f.cfg['mode'] = 'service'; f.cfg['lifecycle'] = None
    seen = []

    def stop_all(drain_s=5):  # harness: stands in for P1f's Fleet.channel_stop_all; records the order
        seen.append({'drain_s': drain_s, 'queued': [json.loads(x)['event'] for x in f.r.lrange(channels.OUTQ, 0, -1)],
                     'redis_ok': bool(f.r.ping())})
        return {'drained': False, 'left': 1, 'in_flight': 'out-1'}
    monkeypatch.setattr(f, 'channel_stop_all', stop_all, raising=False)
    f.stopping = f.explicit_stop = True
    f.v2.close()
    f.v2.reader = f.v2.bus.blocking_client(1000)
    f.run()
    queued = [json.loads(x) for x in f.r.lrange(channels.OUTQ, 0, -1)]
    closed = [i for i in queued if i['event'] == 'run_closed']
    assert len(closed) == 1 and closed[0]['target'] == 'last'
    assert f.r.exists(channels.PUSHED + 'run_closed:' + f.v2.bus.run_id)
    assert len(seen) == 1 and seen[0]['drain_s'] == 5 and 'run_closed' in seen[0]['queued'] and seen[0]['redis_ok']
    assert json.loads((f.run_dir / 'result.json').read_text())  # the timed-out drain did not hold the stop


# ---------------------------------------------------------------- 7
def test_delete_dialog_two_step_approval_presses_once_and_screen_change_refuses(wd, fake_type, monkeypatch):
    from cmux_fleet.channels import actor
    f = wd
    f.cfg['channels'] = entries_for('bot-1')
    ch, _, _ = channel(f)
    path = channels.account_path('fake', 'bot-1')  # setup (M139): the account file approve._allowed re-reads
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('ALLOW=' + ALLOWED + '\nPUSH_TO=' + ALLOWED + '\n', encoding='utf-8')
    path.chmod(0o600)
    from test_channel_approve import user_table  # setup (M139): user_approve prompt table -> one approve key
    user_table(f, path.parent, [{'name': 'claude_dangerous_rm', 'match': ['dangerous rm'], 'keys': ['Enter']}])
    # Single writer (ruling 18): every write to fx:channel:pending* happens with channels.approve on the call
    # stack (helpers allowed). Reads are not recorded; no source is scanned.
    writes, WRITE = [], {'SET', 'SETEX', 'SETNX', 'HSET', 'HSETNX', 'HMSET', 'HDEL', 'DEL', 'UNLINK', 'EXPIRE',
                         'PEXPIRE', 'PERSIST', 'RENAME', 'EVAL', 'EVALSHA'}

    def spy(orig):
        def run(self, *args, **kw):
            cmd = str(args[0]).upper()
            names = [a.decode() if isinstance(a, bytes) else str(a) for a in args[1:]]
            if cmd in WRITE and any(n.startswith((channels.PENDING, channels.PENDING_SLOT)) for n in names):
                writes.append((cmd, any(fr.frame.f_globals.get('__name__') == 'cmux_fleet.channels.approve'
                                        for fr in inspect.stack()[1:])))
            return orig(self, *args, **kw)
        return run
    monkeypatch.setattr(redis.Redis, 'execute_command', spy(redis.Redis.execute_command))
    monkeypatch.setattr(redis.client.Pipeline, 'execute_command', spy(redis.client.Pipeline.execute_command))
    replies = []

    def press_round(screen, step_two=True, tick_offset=0):
        pane = f.agent('worker-1')['pane']
        f.mgr.screens[pane] = screen
        f.v2.watchdog.tick(time.time() + tick_offset)  # delete dialog -> prompt_wait -> approve.register -> user_only card
        cards = [json.loads(x) for x in f.r.lrange(channels.OUTQ, 0, -1)]
        card = [c for c in cards if c['event'] == 'prompt_wait' and c.get('user_only')][-1]
        p_id = card['card']['ref']
        assert p_id.startswith('p-') and len(p_id) == 10
        inbound(f, ch, '/approve ' + p_id, item_id=len(replies) + 70, replies=replies,
                post=f.v2.user_dialog_answer)  # step 1
        confirm = [json.loads(x) for x in f.r.lrange(channels.OUTQ, 0, -1)][0]
        nonce = confirm['card']['buttons'][0]['command'].split()[-1]
        assert len(nonce) == 16
        if step_two:
            inbound(f, ch, '/approve ' + p_id + ' ' + nonce, item_id=len(replies) + 80, replies=replies,
                    post=f.v2.user_dialog_answer)
        f.r.delete(channels.OUTQ)
        return pane, p_id

    dispatch_one(f)
    pane, _ = press_round(DIALOG_RM_LITERAL)
    for _ in range(3):
        f.v2.step(10)
    f.v2.watchdog.tick(time.time() + 1)  # fresh-screen checks, then exactly one key press
    assert len(keys(f, 'worker-1')) == 1
    # second round: the screen tail changes between the answer and the press -> no key, a `changed` reply
    before = len(keys(f, 'worker-1'))
    # timing (M151): this round's first tick comes after round one's last tick (time.time() + 1 above)
    pane, _ = press_round(DIALOG_RM_LITERAL.replace('build/tmp', 'build/other'), tick_offset=1.5)
    f.mgr.screens[pane] = DIALOG_RM_LITERAL.replace('build/tmp', 'build/third')
    for _ in range(3):
        f.v2.step(10)
    f.v2.watchdog.tick(time.time() + 2)
    assert len(keys(f, 'worker-1')) == before
    assert any('已变化' in r for r in replies) or \
        any(json.loads(x).get('event') == 'reply' and '已变化' in (json.loads(x).get('text') or '')
            for x in f.r.lrange(channels.OUTQ, 0, -1))  # the `changed` receipt's fixed text (M151)
    assert writes and all(by_approve for _, by_approve in writes)


# ---------------------------------------------------------------- 8
def split_round(f, rid):
    """harness: P7 (service split, reusing §44) from the worker's escalate to the sub-fleet's end, on split-1's
    approved v2.1 flow (tests/test_service_split.py #13 / #16). Real code all the way: the worker's escalate is
    settled by the runtime (service_split_intake), the supervisor approves M<n> (escalation_apply writes the sf
    request), subfleet.process_requests + escalation_tick run as in a tick (accepted -> ``ok`` + ``delegated``), and
    the child's end goes through Fleet.split_finished, the P7 step of subfleet.collect (``<rid>.done``). Stand-ins
    only for what this one-fleet Pi fixture cannot host: the child process (spawn_child writes the child's record;
    two real v2 fleets run it in test_service_split.py) and subfleet.check's slot-kind refusal of a Pi pane (as
    test_escalate.ok_check does for its fake fleet; every other limit is checked for real)."""
    from cmux_fleet import subfleet
    if not hasattr(f, 'service_split_intake') or not hasattr(f, 'split_settle_accepted'):
        pytest.fail('red: P7a / P7b not landed (service split intake / settle)')
    real_check = subfleet.check

    def check(f_, slot, task_id, workers, ready=True, **kw):  # a Pi pane: the slot-kind refusal is not under test
        reason = real_check(f_, slot, task_id, workers, ready, **kw)
        slot_kind = ('only official Claude Code', 'pi worker panes cannot be handed over')
        return reason if reason and not any(x in reason for x in slot_kind) else None

    def spawn_child(f_, slot, task_id, goal, workers, **kw):  # the child process: its record, nothing launched
        run = f_.run_dir / 'sub' / task_id
        (run / 'workspace').mkdir(parents=True)
        rec = {'task_id': task_id, 'slot': slot, 'run': str(run), 'state': 'running', 'attempt': 1,
               'workers': workers, 'created': time.time(), 'goal': goal, 'hours': kw.get('hours')}
        subfleet.save_record(run, rec)
        return rec
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(subfleet, 'check', check)
        mp.setattr(subfleet, 'spawn_child', spawn_child)
        # the supervisor routes the request to worker-1, which answers with the §44 escalate shape
        f.pump(); sup = receive(f, 'supervisor')
        settle(f, 'supervisor', sup, {'status': 'RUNNING', 'skip': [], 'dispatch': [
            {'to': 'worker-1', 'instruction': 'Do the big job from the operator message.'}]})
        f.pump(); worker = receive(f, 'worker-1')
        settle(f, 'worker-1', worker, {'escalate': 'too big for one worker: two independent parts', 'workers': 2,
                                       'proposal': [{'instruction': 'Do the first half into part-a.md', 'output': 'part-a.md'},
                                                    {'instruction': 'Do the second half into part-b.md', 'output': 'part-b.md'}]})
        key = next(k for k, row in json.loads(f.r.get(fleet.ITEMS) or '{}').items() if row.get('request') == rid)
        assert f.escalation_state()[key]['status'] == 'escalated' and f.r.hget(fleet.REQ + rid, 'status') == 'dispatched'
        # the supervisor gets the [escalate] note (bound to rid) and approves M<n>
        f.pump(); note = receive(f, 'supervisor')
        assert 'asks to split ' + key in note['body']['text']
        settle(f, 'supervisor', note, {'status': 'RUNNING', 'dispatch': [], 'skip': [],
                                       'escalations': [{'task': key, 'decision': 'approve'}]})
        assert f.escalation_state()[key]['status'] == 'approved'
        # the daemon tick: the spawn runs and is accepted, then the request is answered ok + delegated
        subfleet.process_requests(f)
        f.escalation_tick()
        assert f.escalation_state()[key]['status'] == 'delegated'
        # the sub-fleet ends (its result.json) and is collected: the follow-up result <rid>.done
        run = f.run_dir / 'sub' / key
        fleet.save(run / 'result.json', {'status': 'completed', 'reason': 'both parts merged'})
        child = dict(subfleet.record(run), outcome='completed', reason='both parts merged')
        assert rid + '.done' in f.split_finished(child, run)


def test_split_delegated_and_done_pushed_to_origin(fx, fake_type):
    f = fx
    ch, transport, _ = channel(f)
    # this channel only: a real channel's limit (base.Channel 4000), not FakeChannel's 50, so the delegated reply
    # naming a 35-char ui- request id goes out as one message, as it would on a real channel (cf. bdee6a3)
    from cmux_fleet.channels import base as channel_base
    ch.TEXT_LIMIT = channel_base.Channel.TEXT_LIMIT
    inbound(f, ch, '把大活拆给子 fleet', item_id=81)
    rid = json.loads(f.r.xrange(f.v2.prefix + ':reply:' + events_of(f, 'operator_input')[0]['request_id'])[0][1]
                     ['data'])['id']
    origin = json.loads(f.r.get(channels.ORIGIN + rid))
    split_round(f, rid)
    from cmux_fleet.channels import actor
    entries = entries_for('bot-1')
    delegated = result(f, rid)
    assert delegated['status'] == 'ok' and delegated['result']['delegated'].startswith('M')  # closed on accepted
    assert rid + '.done' in delegated['result']['reply']  # the reply names the follow-up id
    done = json.loads(f.r.get(fleet.RESULT + rid + '.done'))
    assert envelope.origin_id(done['id']) == rid and done['status'] in ('ok', 'failed')
    assert json.loads(f.r.get(channels.ORIGIN + rid)) == origin  # the origin outlived the delegated push
    f.r.delete(channels.OUTQ)
    actor.outbound_scan(f.r, entries)
    items = [json.loads(x) for x in reversed(f.r.lrange(channels.OUTQ, 0, -1))]
    assert [(i['event'], i['target'], i['request_id']) for i in items] == [('result', 'origin', rid),
                                                                          ('result', 'origin', rid + '.done')]
    transport.sent.clear()
    push_out(f, entries, [ch])
    texts = [d for op, d in transport.sent if op == 'send_text']
    assert len(texts) == 2  # the delegated result, then <rid>.done; each exactly once
    for d in texts:  # both go back to the original conversation, replying to the original message
        assert d['to'] == origin['conversation'] == 'c-1' and d['reply_ref'] == origin['reply_ref'] == {'chat': 'c-1'}
    assert delegated['result']['delegated'] in texts[0]['text']
    expected = done['result']['reply'] if done['status'] == 'ok' else done['error']
    assert expected and expected in texts[1]['text']
    push_out(f, entries, [ch])  # a second round pushes nothing again
    assert len([d for op, d in transport.sent if op == 'send_text']) == 2


# ---------------------------------------------------------------- 9
def test_status_command_round_trip(fx, fake_type):
    f = fx
    ch, _, _ = channel(f)
    out, replies = inbound(f, ch, '/status', item_id=91)
    assert events_of(f, 'operator_input') == []  # a command never becomes operator input
    assert replies and all(slot in replies[-1] for slot in f.names)
    assert all(str(f.agent(slot).get('state')) in replies[-1] for slot in f.names)


# ---------------------------------------------------------------- 10
@pytest.fixture(params=['pi', 'claude-hook-task', 'claude-hook-service'])
def any_supervisor(request, monkeypatch):
    """One independent fleet per param (never two Redis in one test): the pi supervisor (`fx`), or the Claude
    hook supervisor built the same way as tests/test_claude_supervisor_hook.py::supervisor (task / service)."""
    if request.param == 'pi':
        yield request.getfixturevalue('fx')
        return
    from test_claude_supervisor import event as cs_event, make_fleet, pi_session
    from test_claude_hook_delivery import HookMgr, step, stop
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)
    f = make_fleet(request.getfixturevalue('private_redis_v2'), request.getfixturevalue('tmp_path'), 'pi',
                   request.param.rsplit('-', 1)[1])
    f.mgr = HookMgr()
    f.test_hooks, f.test_procs, f.test_panes = [], [], {}
    try:
        f.start_all()
        f.r.set(f.v2.prefix + ':boot', 'running')
        for slot in f.workers:
            meta = pi_session(f, slot)
            cs_event(f, dict(meta, kind='bridge_session', request_id='register-' + slot))
            f.v2.bus.register_slot(slot, 0, meta['thread'])
            cs_event(f, dict(meta, kind='startup', text='READY', failed=False, aborted=False))
            cs_event(f, dict(meta, kind='availability', idle=True))
        f.ready_hook = stop(f, 'supervisor', 'READY', active=False, user_prompt='startup')
        assert step(f, lambda: f.agent('supervisor').get('runtime_ready') == '1')
        yield f
    finally:
        for hook in f.test_hooks:  # only this test's own hook processes
            hook.kill()
        for proc in f.test_procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        f.v2.close()


def test_supervisor_thread_matches_handle_for_pi_and_claude_hook(any_supervisor, fake_type):
    """The operator_input that actor.submit_operator_input posts carries the supervisor's current generation /
    thread, and Fleet.handle accepts it (not `rejected: thread`): pi supervisor and Claude hook supervisor
    (task and service), each in its own fixture."""
    from cmux_fleet.channels import actor
    f = any_supervisor
    origin = envelope.origin_from(envelope.make('fake', 'bot-1', conversation='c-1', message_id='101:c-1',
                                                sender=ALLOWED, text='hi', at=1762650000))
    out = ask_fleet(f)('看一下 worker-1', origin)
    assert out.get('status') == 'accepted', out
    ev = events_of(f, 'operator_input')[-1]
    info = f.agent('supervisor')
    assert int(ev['generation']) == int(info['generation']) and ev.get('thread') == info.get('thread')
    assert ev['source'] == 'channel:fake:bot-1' and actor  # noqa: B015


# ---------------------------------------------------------------- 11 [baseline]
def test_fleet_without_channels_has_no_channel_keys_audits_or_actor(fx):
    f = fx
    assert not f.cfg.get('channels')
    event(f, dict(kind='request', request_id='cmd-r1',
                  spec=dict(id='r1', to='worker-1', payload={'message': 'hi'},
                            reply={'required': ['reply'], 'allowed': ['reply']})))
    for _ in range(3):
        f.tick()
        f.v2.step(10)
    assert f.r.keys(channels.KEY_PREFIX + '*') == []
    assert channel_audits(f) == []
    assert not f.r.exists(channels.ACTOR)


# ================================================================ A2' (brief v2.2 §8.4, P8 security; integrator-1)
# Red until the P8 packages land (P8b-P8o); every P8 name that P0 / P8a does not provide is imported inside the
# test body, so a missing package is a FAILED test, never a collection ERROR. ``test_security_no_yaml_fleet_identical``
# is the [基线] and green now. At the checkpoint integrator-1 may adjust harness wiring (marked "harness"), never the
# assertions. The §4.1 start --security mapping is not wired here (see integrator-1/p8-start-test-mapping.md):
# these tests pin the parsed policy into the run config the way `start` does (contracts §8 「安全包」).
def fixed(which):
    """The fixed (field-free) head of a receipt text."""
    return envelope.RECEIPTS[which].split('{')[0]


def pin_policy(f, text):
    """harness: what `start` does with security.yaml -- parse once, pin into the run config.json (no hot reload)."""
    from cmux_fleet.channels import security
    policy, errors = security.parse_security(text)
    assert errors == [] and policy
    f.cfg['security'] = policy
    fleet.save(Path(f.cfg['run']) / 'config.json', f.cfg)
    return policy


def p8p_start(m, d):
    """harness (P8p, core-2 P8-start-plan §4.1): the real `start` with test_pi.mock_start's fakes, all inside the
    MonkeyPatch context ``m`` (mock_start patches subprocess.run and fleet.LATEST); a fresh config dir, not created."""
    from test_pi import mock_start
    d.mkdir(parents=True, exist_ok=True)
    cfgdir = d / 'fleetmux-config'
    m.setenv(channels.CONFIG_ENV, str(cfgdir))
    args, _ = mock_start(d, m)
    return args, cfgdir


def p8p_run(args, d):
    """harness: one `start` -> the run config.json it wrote (mock_start points fleet.LATEST at d / latest.json)."""
    fleet.main(args)
    return fleet.load(d / 'latest.json')


def sec_inbound(f, env, *, stop=None, replies=None, post=None):
    """handle_inbound the way inbound_loop binds it for a run with a policy (contracts §8 Z9). ``post``: approve's
    step-two event (setup M191: f.v2.user_dialog_answer, as the delete-dialog row binds it; None -> as before)."""
    from cmux_fleet.channels import actor, security
    replies = [] if replies is None else replies
    audit = actor.channel_audit(f.r, f.cfg, env['channel'], env['account'])
    out = actor.handle_inbound(f.r, env, submit=ask_fleet(f), reply=lambda e, t: replies.append(t),
                               policy=security.policy(f.cfg), stop=stop, audit=audit, post=post)
    return out, replies


def sec_env(text, n, **extra):
    return envelope.make('fake', 'bot-1', conversation='c-1', message_id=str(n) + ':c-1', sender=ALLOWED, text=text,
                         reply_ref={'chat': 'c-1'}, at=1762650000 + n, **extra)


def pretool(f, slot, tool, tool_input, timeout=60):
    """The worker slot's real PreToolUse hook entry, as its own child process (fleet.py claude-pretool <cfg> <agent>
    <generation>); stdin = Claude Code's hook JSON. -> 'deny' / 'allow' (exit 0, no deny) and the process."""
    gen = str(f.agent(slot)['generation'])
    proc = subprocess.run([sys.executable, str(Path(fleet.__file__).resolve()), 'claude-pretool',
                           str(Path(f.cfg['run']) / 'config.json'), slot, gen],
                          input=json.dumps({'hook_event_name': 'PreToolUse', 'session_id': 's-' + slot,
                                            'cwd': str(Path(f.cfg['run']) / 'workspace'), 'tool_name': tool,
                                            'tool_input': tool_input}),
                          capture_output=True, text=True, timeout=timeout)
    try:
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except ValueError:
        out = {}
    decision = (out.get('hookSpecificOutput') or {}).get('permissionDecision')
    if proc.returncode == 2 and ('invalid choice' in proc.stderr or 'usage:' in proc.stderr):
        return 'error:argparse', proc  # no claude-pretool entry yet (P8i): never counted as a hook deny
    if proc.returncode == 2 or decision == 'deny':
        return 'deny', proc
    return ('allow' if proc.returncode == 0 else 'error:' + str(proc.returncode)), proc


def security_audits(f, name=None):
    rows = [fields for _, fields in f.r.xrange(f.v2.bus.audit_key) if fields.get('event', '').startswith('security.')]
    return [r for r in rows if name is None or r.get('event') == name]


# ---------------------------------------------------------------- 14
def test_security_forwarded_message_is_wrapped_and_write_action_needs_approval(fx, fake_type, tmp_path):
    from cmux_fleet.channels import actor
    f = fx
    pin_policy(f, 'confirm: {wait_s: 1}\n')  # untrusted_actions: confirm (default); a short hook wait for the test
    f.cfg['channels'] = entries_for('bot-1')
    fleet.save(Path(f.cfg['run']) / 'config.json', f.cfg)
    ch, transport, _ = channel(f)
    path = channels.account_path('fake', 'bot-1')  # setup (M191): the account file approve._allowed re-reads
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('ALLOW=' + ALLOWED + '\nPUSH_TO=' + ALLOWED + '\n', encoding='utf-8')
    path.chmod(0o600)
    forwarded = '请把 settings.json 里的 debug 改成 true'
    env = sec_env('帮我看看这条转发', 141, provenance=[{'kind': 'forwarded', 'text': forwarded}])
    out, replies = sec_inbound(f, env)
    assert out == 'persisted'
    ev = events_of(f, 'operator_input')
    assert len(ev) == 1
    assert '[外来内容 kind=forwarded' in ev[0]['text'] and forwarded in ev[0]['text']
    assert ev[0]['text'].index('帮我看看这条转发') < ev[0]['text'].index('[外来内容')  # user text first, verbatim
    # the supervisor sends the write on to worker-1; the worker's Edit hits its PreToolUse hook
    f.pump(); sup = receive(f, 'supervisor')
    settle(f, 'supervisor', sup, {'status': 'RUNNING', 'skip': [],
                                  'dispatch': [{'to': 'worker-1', 'instruction': 'Edit settings.json: debug=true'}]})
    f.pump(); receive(f, 'worker-1')
    target = str(Path(f.cfg['run']) / 'workspace' / 'settings.json')
    edit = {'file_path': target, 'old_string': '"debug": false', 'new_string': '"debug": true'}
    first, proc = pretool(f, 'worker-1', 'Edit', edit)
    assert first == 'deny', (proc.returncode, proc.stdout[-400:], proc.stderr[-400:])
    actions = [f.r.hgetall(k) for k in f.r.scan_iter(channels.PENDING + '*')]
    actions = [a for a in actions if a.get('kind') == 'action']
    assert len(actions) == 1 and actions[0]['slot'] == 'worker-1' and len(actions[0]['action_hash']) == 32
    p_id = actions[0]['id']
    assert 'settings.json' in actions[0]['action_text']  # generated by code from the real arguments
    assert security_audits(f, 'security.action_pending')
    # the card goes to the same conversation the request came from (user_only, never the supervisor)
    cards = [json.loads(x) for x in f.r.lrange(channels.OUTQ, 0, -1)]
    card = [c for c in cards if c.get('user_only') and (c.get('card') or {}).get('ref') == p_id]
    assert len(card) == 1
    to = actor.resolve_target(f.r, card[0], f.cfg['channels'], {('fake', 'bot-1'): ch.account})
    assert (to['channel'], to['account'], to['conversation']) == ('fake', 'bot-1', 'c-1')
    f.r.delete(channels.OUTQ)
    # two steps: /approve <p-id> -> confirm card with a one-time nonce; /approve <p-id> <nonce> -> ticket
    sec_inbound(f, sec_env('/approve ' + p_id, 142), replies=replies, post=f.v2.user_dialog_answer)
    confirm = [json.loads(x) for x in f.r.lrange(channels.OUTQ, 0, -1)]
    nonce = [c for c in confirm if (c.get('card') or {}).get('ref') == p_id][-1]['card']['buttons'][0]['command'] \
        .split()[-1]
    assert len(nonce) == 16
    sec_inbound(f, sec_env('/approve ' + p_id + ' ' + nonce, 143), replies=replies, post=f.v2.user_dialog_answer)
    for _ in range(3):
        f.v2.step(10)  # RuntimeV2.user_dialog_answer is the only ticket writer
    assert len(list(f.r.scan_iter('fx:guard:ticket:*'))) == 1
    assert any(a.get('stage') == 'answered' for a in security_audits(f, 'security.action_approved'))
    second, proc = pretool(f, 'worker-1', 'Edit', edit)
    assert second == 'allow', (proc.returncode, proc.stdout[-400:], proc.stderr[-400:])
    assert list(f.r.scan_iter('fx:guard:ticket:*')) == []  # one ticket, one use
    assert {a.get('stage') for a in security_audits(f, 'security.action_approved')} >= {'answered', 'consumed',
                                                                                       'allowed'}
    # P8p (plan §4.1 ④): the real `start --security <relative path>` pins THAT file, not the default one, and
    # records it as an absolute security_source -- the same policy this test pinned above
    with pytest.MonkeyPatch.context() as m:
        d = tmp_path / 'p8p-14'
        args, cfgdir = p8p_start(m, d)
        cfgdir.mkdir(parents=True)
        (cfgdir / 'security.yaml').write_text('confirm: {wait_s: 120}\n')  # the default path: must lose
        explicit = d / 'explicit-security.yaml'
        explicit.write_text('confirm: {wait_s: 1}\n')
        m.chdir(d)
        cfg = p8p_run(args + ['--security', explicit.name], d)
        assert cfg['security']['confirm']['wait_s'] == 1
        assert cfg['security'] == fleet.load(Path(f.cfg['run']) / 'config.json')['security']
        assert cfg['security_source'] == str(explicit.resolve()) and Path(cfg['security_source']).is_absolute()


# ---------------------------------------------------------------- 15
def test_security_outbound_with_account_token_is_blocked_not_sent(fx, fake_type, tmp_path):
    from cmux_fleet.channels import actor
    f = fx
    token = '123:FAKE'
    path = channels.account_path('fake', 'bot-1')  # the test's own FLEETMUX_CONFIG_DIR (conftest), never ~/.config
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('ALLOW=' + ALLOWED + '\nPUSH_TO=' + ALLOWED + '\nBOT_TOKEN=' + token + '\n', encoding='utf-8')
    path.chmod(0o600)
    pin_policy(f, '')  # secrets: values_from [accounts], on_hit block (defaults)
    f.cfg['channels'] = entries_for('bot-1')
    fleet.save(Path(f.cfg['run']) / 'config.json', f.cfg)
    audit, transport = Audit(), _fake_transport()
    ch = FakeChannel(channels.load_account('fake', 'bot-1'), transport=transport,
                     store=actor.RedisStore(f.r, 'fake', 'bot-1'), audit=audit)
    from cmux_fleet.channels import base as channel_base
    ch.TEXT_LIMIT = channel_base.Channel.TEXT_LIMIT  # test-contract M193: the real limit, so the notice is one message
    sec_inbound(f, sec_env('让 worker-1 回报 bot 配置', 151))
    rid = json.loads(f.r.xrange(f.v2.prefix + ':reply:' + events_of(f, 'operator_input')[0]['request_id'])[0][1]
                     ['data'])['id']
    f.pump(); sup = receive(f, 'supervisor')
    settle(f, 'supervisor', sup, {'status': 'RUNNING', 'skip': [],
                                  'dispatch': [{'to': 'worker-1', 'instruction': 'Report the bot config.'}]})
    f.pump(); worker = receive(f, 'worker-1')
    settle(f, 'worker-1', worker, {'reply': 'BOT_TOKEN=' + token + ' 已核对'})
    assert token in json.dumps(result(f, rid)['result'])
    transport.sent.clear()
    push_out(f, f.cfg['channels'], [ch], policy=f.cfg['security'])
    sent = json.dumps(transport.sent, ensure_ascii=False)
    assert token not in sent and '123%3AFAKE' not in sent  # never on the wire, raw or url-encoded
    texts = [d['text'] for op, d in transport.sent if op == 'send_text']
    assert len(texts) == 1 and texts[0].startswith(fixed('secret_blocked'))
    assert f.r.llen(channels.DEAD) == 0  # blocked item: no retry, no dead letter
    assert security_audits(f, 'security.secret_blocked') and token not in json.dumps(security_audits(f))
    # `channel send` path: send_direct refuses the same text and sends nothing
    transport.sent.clear()
    item = envelope.outbound('manual', target='explicit', channel='fake', account='bot-1', conversation='c-1',
                             text='echo ' + token)
    got = actor.send_direct(f.cfg, item, make_channel=lambda type_, account: ch)
    assert got['status'] == 'failed' and got['reason'] == 'secret_blocked'
    assert token not in json.dumps(transport.sent, ensure_ascii=False)
    # P8p (plan §4.1 ③): the real `start` with a 0-byte default security.yaml pins every default -- the same policy
    # this test pinned above, so the block it proved is what an empty file turns on
    from cmux_fleet.channels import security
    with pytest.MonkeyPatch.context() as m:
        d = tmp_path / 'p8p-15'
        args, cfgdir = p8p_start(m, d)
        cfgdir.mkdir(parents=True)
        (cfgdir / 'security.yaml').write_bytes(b'')
        cfg = p8p_run(args, d)
        assert cfg['security'] == security.DEFAULTS
        assert cfg['security'] == fleet.load(Path(f.cfg['run']) / 'config.json')['security']
        assert cfg['security_source'] == str((cfgdir / 'security.yaml').resolve())
        assert Path(cfg['security_source']).is_absolute()


# ---------------------------------------------------------------- 16
def test_security_stop_from_phone_pauses_before_daemon_steps_and_escapes_workers(fx, fake_type):
    import functools
    from cmux_fleet.channels import actor, stop
    f = fx
    pin_policy(f, '')  # stop.level interrupt (default)
    f.cfg['channels'] = entries_for('bot-1')
    f.channel_spawn = lambda argv: types.SimpleNamespace(pid=0, poll=lambda: None, terminate=lambda: None, kill=lambda: None, wait=lambda timeout=None: 0)  # test-contract M98: no real 'channel actor' child
    fleet.save(Path(f.cfg['run']) / 'config.json', f.cfg)
    event(f, dict(kind='request', request_id='cmd-s16', spec=dict(id='s16', to='worker-1', payload={'message': 'hi'},
                                                                  reply={'required': ['reply'], 'allowed': ['reply']})))
    f.pump(); receive(f, 'worker-1')
    assert f.agent('worker-1').get('state') in ('busy', 'starting')
    idle = [s for s in f.workers if s != 'worker-1']
    before = {s: len(keys(f, s)) for s in f.workers}
    audit = actor.channel_audit(f.r, f.cfg, 'fake', 'bot-1')
    handle = functools.partial(stop.handle, f.r, f.cfg, audit=audit)
    out, replies = sec_inbound(f, sec_env('/stop 别动了', 161), stop=handle)
    # the actor side alone, before any daemon step: paused, flag up, receipt sent, nothing to the supervisor
    service = f.r.hgetall(fleet.SERVICE)
    assert service.get('paused') == '1' and service.get('reason', '').startswith('channel stop by ')
    flag = f.r.hgetall(channels.KEY_PREFIX + 'stop')
    assert flag.get('level') == 'interrupt' and flag.get('rank') == '2' and len(flag.get('id', '')) == 32
    epoch = flag['epoch']
    assert replies and replies[-1].startswith(fixed('stopped'))
    assert events_of(f, 'operator_input') == []
    assert {s: len(keys(f, s)) for s in f.workers} == before  # the actor never presses keys
    # the daemon's security_tick: Escape on the busy worker only, once per stop request
    f.security_tick()
    assert keys(f, 'worker-1')[before['worker-1']:] == ['Escape']
    assert all(len(keys(f, s)) == before[s] for s in idle)
    daemon = [a for a in security_audits(f, 'security.stop') if a.get('stage') == 'daemon']
    assert daemon and daemon[-1].get('epoch') == epoch and 'worker-1' in daemon[-1].get('sent', '')
    assert not f.r.exists(channels.KEY_PREFIX + 'stop')  # released after done + audited (Z6)
    f.security_tick()
    f.tick()
    assert keys(f, 'worker-1')[before['worker-1']:] == ['Escape']  # no second press for the same request
    assert f.r.hget(fleet.SERVICE, 'paused') == '1'  # stays paused until a human resumes


# ---------------------------------------------------------------- 17 [基线]
def test_security_no_yaml_fleet_identical(fx, tmp_path):
    from cmux_fleet.channels import security
    f = fx
    assert 'security' not in f.cfg and security.policy(f.cfg) is None
    event(f, dict(kind='request', request_id='cmd-r17',
                  spec=dict(id='r17', to='worker-1', payload={'message': 'hi'},
                            reply={'required': ['reply'], 'allowed': ['reply']})))
    for _ in range(3):
        f.tick()
        f.v2.step(10)
    assert f.r.keys('fx:guard:*') == []
    argv = fleet.claude_argv(Path(f.cfg['run']) / 'config.json', f.cfg, 'worker-1', 1, 'hi')
    settings = json.loads(Path(argv[argv.index('--settings') + 1]).read_text())
    assert 'PreToolUse' not in settings.get('hooks', {}) and 'sandbox' not in settings
    assert security_audits(f) == []
    assert list(Path(f.cfg['run']).rglob('chain-*.jsonl')) == []
    # P8p (plan §4.1 ① ②): a run started by the real `start` without security.yaml has neither key and never
    # creates the config dir; with a BROKEN default file, --no-security starts the same and never looks at the dir
    with pytest.MonkeyPatch.context() as m:
        d = tmp_path / 'p8p-17'
        args, cfgdir = p8p_start(m, d)
        cfg = p8p_run(args, d)
        assert 'security' not in cfg and 'security_source' not in cfg
        assert not cfgdir.exists()
        cfgdir.mkdir(parents=True)
        (cfgdir / 'security.yaml').write_text('zz_unknown_key: 1\nsandbox: {mode: seatbelt}\n')

        def looked(*a, **k):
            raise AssertionError('--no-security looked at the config dir')
        m.setattr(channels, 'config_dir', looked)
        cfg = p8p_run(args + ['--no-security'], d)
        assert 'security' not in cfg and 'security_source' not in cfg
