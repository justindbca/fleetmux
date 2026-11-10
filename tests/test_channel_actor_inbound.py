"""P1d channel actor, inbound (brief v2 §6.5; core-1 test-plan §4, §4.1, §6 and the supervisor rulings 5 / 6 / 9 / 10).

A2 red tests: every test imports ``cmux_fleet.channels.actor`` (and ``approve``) inside its body, so a missing actor
fails the test (FAILED), never the collection (ERROR). Real private unix-socket Redis, the in-process RuntimeV2 of
the ``fx`` / ``service`` fixtures (pi / Codex supervisors), the P0 ``FakeChannel`` with fake transports. No real
network, no credentials, no listener; the config dir is a temp dir (autouse ``config_dir``).

Seams pinned here (contracts §8 「入站确认」 3 / 4 / 6, test-plan §1):
- ``actor.handle_inbound(r, env, *, submit, reply, post=None, status=None)`` is the accept callback of
  ``Channel.poll`` / ``Channel.inbound`` and the one owner of ``seen:<message_id>`` in the account store; it returns
  'persisted' / 'duplicate' or raises ``channels.Retryable`` (unconfirmed: forget, no ack).
- ``submit(text, origin) -> answer dict``; the default is ``actor.submit_operator_input(cfg, r, text, origin,
  timeout=…)``, whose request id is derived from ``<type>:<account>:<message_id>`` (one id per message).
- ``reply(env, text)`` sends a receipt back.
"""
import json
import re
import threading
import time
import uuid

import pytest
import redis

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from test_runtime_v2 import fx  # noqa: F401  (fixture: v2 service fleet, pi supervisor)
from test_v2_codex_service import service  # noqa: F401  (fixture: v2 service fleet, Codex supervisor)
from test_channels_contract import Audit, FakeChannel, config_dir, fake_registered  # noqa: F401
from cmux_fleet import channels
from cmux_fleet.channels import envelope

ACCOUNT = 'contract-1'
# fleet.py Fleet.handle, verbatim (main §48 split the old single reason into these two)
OLD_SESSION = 'this input belongs to an old supervisor session (generation {old}, current {new})'
NOT_READY = 'supervisor is not ready (state {state})'


# ---------------------------------------------------------------------------------------------- helpers
def env_of(text='看一下 worker-1', mid=None, **kw):
    return envelope.make('fake', ACCOUNT, conversation='c-1', message_id=mid or uuid.uuid4().hex[:12],
                         sender='u-1', text=text, reply_ref={'chat': 'c-1'}, **kw)


def fresh(f):
    """A second client on the run's Redis: the fixture client allows one connection, the daemon steps on it."""
    return redis.Redis(unix_socket_path=f.cfg['redis_socket'], decode_responses=True)


def events(f, kind='operator_input'):
    out = []
    for _, fields in f.r.xrange(f.v2.bus.prefix + ':events'):
        ev = json.loads(fields['data'])
        if ev.get('kind') == kind:
            out.append(ev)
    return out


def audits(f, name):
    return [fields for _, fields in f.r.xrange(f.v2.bus.audit_key) if fields.get('event') == name]


def seen_key(env):
    return channels.STORE + env['channel'] + ':' + env['account'] + ':seen:' + env['message_id']


def drive(f, fn, before_step=None):
    """Run ``fn`` (it posts an event and blocks on the daemon's reply) in a thread while the daemon steps here.
    ``before_step`` runs once, after the event is on the stream and before the daemon reads it."""
    box = {}
    n = f.r.xlen(f.v2.bus.prefix + ':events')
    thread = threading.Thread(target=lambda: box.setdefault('value', fn()))
    thread.start()
    deadline = time.time() + 20
    if before_step:
        while f.r.xlen(f.v2.bus.prefix + ':events') == n and thread.is_alive() and time.time() < deadline:
            time.sleep(0.01)
        before_step()
    while thread.is_alive() and time.time() < deadline:
        f.v2.step(20)
    thread.join(2)
    assert not thread.is_alive()
    return box.get('value')


def fleet_submit(actor, f, before_step=None, timeout=5):
    """The default submit (actor.submit_operator_input on its own client), answered by the in-process daemon."""
    client = fresh(f)
    answers = []

    def submit(text, origin):
        answers.append(drive(f, lambda: actor.submit_operator_input(f.cfg, client, text, origin, timeout=timeout),
                             before_step))
        return answers[-1]
    submit.answers = answers
    return submit


def recorder(answer=None):
    calls = []

    def submit(text, origin):
        calls.append((text, origin))
        return dict(answer or {'status': 'accepted', 'id': 'ui-test'})
    submit.calls = calls
    return submit


def replies_sink():
    out = []

    def reply(env, text):
        out.append(text)
    reply.texts = out
    return reply


# ---------------------------------------------------------------------------------------------- the 17 tests
def test_submit_posts_operator_input_with_current_generation_thread_and_source(fx, fake_registered):
    from cmux_fleet.channels import actor
    f = fx
    sup = f.agent('supervisor')
    reply, env = replies_sink(), env_of('让 worker-1 回复 OK')
    assert actor.handle_inbound(f.r, env, submit=fleet_submit(actor, f), reply=reply) == 'persisted'
    posted = events(f)
    assert len(posted) == 1
    ev = posted[0]
    assert ev['agent'] == 'supervisor' and ev['text'] == '让 worker-1 回复 OK'
    assert ev['generation'] == int(sup['generation']) and ev['thread'] == sup['thread']
    assert ev['source'] == 'channel:fake:' + ACCOUNT and ev['run_id'] == f.v2.bus.run_id
    assert reply.texts[-1].startswith('Fleet：指令已排队（ui-')


def test_accepted_writes_origin_last_and_mirror(fx, fake_registered):
    from cmux_fleet.channels import actor
    f = fx
    submit, env = fleet_submit(actor, f), env_of('查一下昨晚的结果')
    actor.handle_inbound(f.r, env, submit=submit, reply=replies_sink())
    rid = submit.answers[-1]['id']
    assert submit.answers[-1]['status'] == 'accepted' and rid.startswith('ui-')
    assert json.loads(f.r.get(channels.ORIGIN + rid)) == envelope.origin_from(env)  # the actor writes it (C8)
    assert 0 < f.r.ttl(channels.ORIGIN + rid) <= channels.ORIGIN_TTL
    last = f.r.hgetall(channels.LAST)
    assert set(last) == {'channel', 'account', 'conversation', 'reply_ref', 'sender', 'at'}
    assert (last['channel'], last['account'], last['conversation'], last['sender']) == ('fake', ACCOUNT, 'c-1', 'u-1')
    assert json.loads(last['reply_ref']) == {'chat': 'c-1'}
    mirror = json.loads(channels.last_path().read_text(encoding='utf-8'))  # same six fields as the hash
    assert set(mirror) == set(last)
    assert {k: str(mirror[k]) for k in ('channel', 'account', 'conversation', 'sender', 'at')} == {
        k: last[k] for k in ('channel', 'account', 'conversation', 'sender', 'at')}
    reply_ref = mirror['reply_ref']
    assert (json.loads(reply_ref) if isinstance(reply_ref, str) else reply_ref) == {'chat': 'c-1'}


def test_rejected_reason_relayed_verbatim_and_forgets(fx, fake_registered):
    from cmux_fleet.channels import actor
    f = fx
    gen = int(f.agent('supervisor')['generation'])

    def stale():  # the session moved on between the post and the daemon's read: the input belongs to an old one
        f.r.hset('fx:agent:supervisor', 'generation', str(gen + 1))
    reply, env = replies_sink(), env_of('还在吗')
    try:
        out = actor.handle_inbound(f.r, env, submit=fleet_submit(actor, f, before_step=stale), reply=reply)
    finally:
        f.r.hset('fx:agent:supervisor', 'generation', str(gen))
    assert reply.texts[-1] == envelope.receipt('rejected', reason=OLD_SESSION.format(old=gen, new=gen + 1))
    assert out == 'persisted'  # a definite rejection is final: the cursor may move past it
    assert not f.r.exists(seen_key(env))  # forgotten: a redelivery may try again
    assert f.r.keys(channels.ORIGIN + '*') == []


def test_unconfirmed_when_wait_reply_times_out(fx, fake_registered):
    """Unconfirmed -> receipt with the request id, forget, Retryable, no ack; a retry reuses the same request id
    (one operator_input on the stream: post_event's producer key). Ruling 9: the seen TTL is only the claim's
    cache, so a retry after it lapses still posts nothing new. Ruling 10: after a generation change the re-post
    conflicts; with no answer under that id it is still unconfirmed (no ack, no new id, nothing re-posted); a
    definite answer found under the same id later settles it under the original protocol."""
    from cmux_fleet.channels import actor
    f = fx
    client = fresh(f)
    asked = []

    def submit(text, origin):  # the default submit, nobody steps the daemon: it never answers
        asked.append(origin['message_id'])
        return actor.submit_operator_input(f.cfg, client, text, origin, timeout=0.1)
    acked = []

    class Acking(FakeChannel):
        def _ack(self, raw):
            acked.append(raw['id'])
    ch = Acking(channels.Account('fake', ACCOUNT, ['u-1']), transport=lambda op, data: [])
    reply = replies_sink()
    raw = {'id': 9, 'chat': 'c-1', 'from': 'u-1', 'text': '帮我看一下'}
    env = ch.parse_inbound(raw)[0]

    def accept(e):
        return actor.handle_inbound(f.r, e, submit=submit, reply=reply)
    assert ch.inbound(raw, accept=accept) == [] and acked == []  # Retryable: not final, not acked
    first = reply.texts[-1]
    m = re.fullmatch(re.escape(envelope.receipt('unconfirmed', id='X')).replace('X', '(.+)'), first)
    assert m, first
    rid = m.group(1)
    assert not f.r.exists(seen_key(env)) and f.r.keys(channels.ORIGIN + '*') == []
    assert ch.health()['state'] == 'retrying'
    with pytest.raises(channels.Retryable):
        accept(dict(env))  # retried: the same request id, nothing new on the stream
    assert reply.texts[-1] == first and len(events(f)) == 1
    f.r.delete(seen_key(env))  # ruling 9: the claim lapsed (SEEN_TTL) - still no second execution
    with pytest.raises(channels.Retryable):
        accept(dict(env))
    assert len(events(f)) == 1
    # ruling 10: the supervisor moved to a new generation; the stored event no longer matches
    sup = f.agent('supervisor')
    f.r.hset('fx:agent:supervisor', mapping={'generation': str(int(sup['generation']) + 1), 'thread': 'thread-new'})
    try:
        assert ch.inbound(dict(raw), accept=accept) == [] and acked == []  # (b) no answer under rid: no ack
        assert '上次投递结果未确认，本次无法重投' in reply.texts[-1]
        assert 'rejected' not in reply.texts[-1] and '请重发' not in reply.texts[-1]
        assert not f.r.exists(seen_key(env)) and len(events(f)) == 1  # no second event, no new id
        n = len(asked)
        f.r.xadd(f.v2.bus.prefix + ':reply:' + rid, {'data': json.dumps({'status': 'accepted', 'id': 'ui-late'})})
        out = ch.inbound(dict(raw), accept=accept)  # (c) a definite answer under the same id settles it
        assert [e['text'] for e in out] == ['帮我看一下'] and acked == [9]
        assert reply.texts[-1] == envelope.receipt('accepted', id='ui-late')
        assert len(events(f)) == 1 and len(asked) == n + 1  # looked up, not posted again
        assert json.loads(f.r.get(channels.ORIGIN + 'ui-late')) == envelope.origin_from(env)
    finally:
        f.r.hset('fx:agent:supervisor', mapping={'generation': sup['generation'], 'thread': sup['thread']})


def test_files_paths_appended_one_per_line(private_redis_v2, tmp_path, fake_registered):
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    paths = []
    for name in ('a.png', 'b.pdf'):
        p = tmp_path / 'inbox' / ('m1-' + name)
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b'x')
        paths.append(str(p.resolve()))
    env = env_of('看这两个文件', files=[{'name': 'a.png', 'path': paths[0], 'size': 1},
                                        {'name': 'b.pdf', 'path': paths[1], 'size': 1}])
    submit = recorder()
    actor.handle_inbound(r, env, submit=submit, reply=replies_sink(), audit=Audit())
    assert len(submit.calls) == 1
    text = submit.calls[0][0]
    assert text.splitlines()[0] == '看这两个文件' and text.splitlines()[-2:] == paths


def test_rejected_files_each_get_file_refused_receipt_no_post(private_redis_v2, fake_registered):
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    rejected = [{'name': 'big.zip', 'reason': 'over 20 MB'}, {'name': 'v.mp4', 'reason': '微信第一期只收文字'}]
    submit, reply = recorder(), replies_sink()
    actor.handle_inbound(r, env_of('', rejected_files=rejected), submit=submit, reply=reply, audit=Audit())
    assert reply.texts == [envelope.receipt('file_refused', name=x['name'], reason=x['reason']) for x in rejected]
    assert submit.calls == []  # empty text after the refusals: nothing is posted


def test_text_plus_paths_over_32000_rejected_before_post(private_redis_v2, tmp_path, fake_registered):
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    p = tmp_path / 'f.txt'
    p.write_text('x')
    env = env_of('长' * (envelope.TEXT_MAX - 3), files=[{'name': 'f.txt', 'path': str(p.resolve()), 'size': 1}])
    submit, reply = recorder(), replies_sink()
    actor.handle_inbound(r, env, submit=submit, reply=reply, audit=Audit())
    assert submit.calls == []
    assert reply.texts[-1].startswith(envelope.receipt('rejected', reason='')) and '32000' in reply.texts[-1]


def test_status_answers_from_snapshot_without_operator_input(fx, fake_registered):
    from cmux_fleet.channels import actor
    f = fx
    submit, reply = recorder(), replies_sink()
    actor.handle_inbound(f.r, env_of('/status'), submit=submit, reply=reply)  # no status=: read fx:agent:* via r
    text = reply.texts[-1]
    for slot in f.names:
        assert slot in text and f.agent(slot)['state'] in text
    assert submit.calls == [] and events(f) == []


def test_help_unknown_invalid_receipts(fx, fake_registered):
    from cmux_fleet.channels import actor
    f = fx
    submit, reply = recorder(), replies_sink()
    actor.handle_inbound(f.r, env_of('/help'), submit=submit, reply=reply)
    assert reply.texts[-1] == envelope.receipt('help')
    actor.handle_inbound(f.r, env_of('/bogus'), submit=submit, reply=reply)
    assert reply.texts[-1] == envelope.receipt('unknown_command', command='bogus')
    actor.handle_inbound(f.r, env_of('/approve x'), submit=submit, reply=reply)
    assert reply.texts[-1].startswith(envelope.receipt('invalid_command', reason=''))
    assert submit.calls == [] and events(f) == []


def test_approve_reject_go_to_approve_handle_command(fx, monkeypatch, fake_registered):
    from cmux_fleet.channels import actor, approve
    f = fx
    calls = []

    def handle_command(r, env, cmd, *, post, reply):
        calls.append((env['text'], cmd, post, reply))
        return 'handled ' + cmd['kind'] + ' ' + cmd['id']
    monkeypatch.setattr(approve, 'handle_command', handle_command)
    submit, reply = recorder(), replies_sink()
    actor.handle_inbound(f.r, env_of('/approve p-0123abcd'), submit=submit, reply=reply)
    assert len(calls) == 1 and calls[0][1] == envelope.parse_command('/approve p-0123abcd')
    assert calls[0][2] is not None and callable(calls[0][3])  # post / reply are handed over
    assert reply.texts[-1] == 'handled approve p-0123abcd'
    pressed = env_of('/reject p-0123abcd', action={'command': '/reject p-0123abcd', 'card_message_id': 'm-1'})
    actor.handle_inbound(f.r, pressed, submit=submit, reply=reply)  # a button press goes the same way
    assert len(calls) == 2 and reply.texts[-1] == 'handled reject p-0123abcd'
    assert submit.calls == [] and events(f) == []


def test_codex_supervisor_fixed_refusal_without_posting(service, fake_registered):
    from cmux_fleet.channels import actor
    f = service
    client, reply = fresh(f), replies_sink()

    def submit(text, origin):
        return actor.submit_operator_input(f.cfg, client, text, origin, timeout=0.1)
    actor.handle_inbound(f.r, env_of('改一下计划'), submit=submit, reply=reply)
    assert reply.texts[-1] == envelope.receipt('no_operator_input')
    assert events(f) == []


def test_supervisor_starting_rejection_relayed(fx, fake_registered):
    from cmux_fleet.channels import actor
    f = fx
    state = f.agent('supervisor')['state']
    f.r.hset('fx:agent:supervisor', 'state', 'starting')
    try:
        reply = replies_sink()
        actor.handle_inbound(f.r, env_of('在吗'), submit=fleet_submit(actor, f), reply=reply)
    finally:
        f.r.hset('fx:agent:supervisor', 'state', state)
    assert reply.texts[-1] == envelope.receipt('rejected', reason=NOT_READY.format(state='starting'))  # its own words


def test_inbound_audit_snippet_200_and_masked(fx, fake_registered):
    from cmux_fleet.channels import actor
    f = fx
    env = envelope.make('fake', ACCOUNT, conversation='c-1', message_id=uuid.uuid4().hex[:12], sender='u-1',
                        text='长' * 500, reply_ref={'chat': 'c-1', 'context_token': 'ctx-SECRET-123456'})
    actor.handle_inbound(f.r, env, submit=recorder(), reply=replies_sink())
    rows = audits(f, 'channel.in')
    assert len(rows) == 1
    assert 0 < len(rows[0]['text']) <= 200
    assert 'ctx-SECRET-123456' not in json.dumps(rows[0], ensure_ascii=False)
    assert '***' in json.dumps(rows[0], ensure_ascii=False)


def test_actor_adds_no_privilege(fx, fake_registered):
    from cmux_fleet.channels import actor
    f = fx
    actor.handle_inbound(f.r, env_of('派个活'), submit=fleet_submit(actor, f), reply=replies_sink())
    posted = events(f)
    assert len(posted) == 1
    assert set(posted[0]) == {'kind', 'agent', 'generation', 'run_id', 'thread', 'text', 'request_id', 'source'}


def test_inbound_loop_adapter_error_audited_and_backs_off(private_redis_v2, fake_registered):
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    polls, sleeps, audit = [], [], Audit()

    def transport(op, data):
        if op == 'poll':
            polls.append(data)
            raise OSError('poll broke')
        return 'm-1'
    ch = FakeChannel(channels.Account('fake', ACCOUNT, ['u-1']), transport=transport, audit=audit)
    now = [1000.0]

    def sleep(s):
        sleeps.append(s)
        now[0] += s
    actor.inbound_loop(ch, r=r, cfg={}, submit=recorder(), reply=replies_sink(), clock=lambda: now[0],
                       sleep=sleep, stop=lambda: len(sleeps) >= 6)
    assert 'channel.error' in audit.events()  # the base audited it; the loop did not die
    assert sleeps == [30, 60, 120, 240, 300, 300]  # 30 s doubling, 5 min cap
    assert len(polls) == 6  # every round polls again


def test_inbound_loop_auth_failure_marks_status_and_long_backoff(private_redis_v2, fake_registered):
    """Ruling 6: the loop decides from health()['state'] only, never by parsing last_error text."""
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()

    def build(name, fail):
        def transport(op, data):
            if op == 'poll' and fail:
                raise channels.ChannelError('auth', 'refused tok-SECRET-123456')  # no 401 / -14 in the text
            return [] if op == 'poll' else 'm-1'
        return FakeChannel(channels.Account('fake', name, ['u-1'], {'TOKEN': 'tok-SECRET-123456'}), transport=transport)
    a, b = build('a', True), build('b', False)
    sleeps_a, sleeps_b = [], []
    actor.inbound_loop(a, r=r, cfg={}, submit=recorder(), reply=replies_sink(), clock=time.time,
                       sleep=sleeps_a.append, stop=lambda: len(sleeps_a) >= 2)
    assert a.health()['state'] == 'needs_login'
    status = r.hgetall(channels.STATUS + 'fake:a')
    assert status['state'] == 'needs_login'
    assert 'tok-SECRET-123456' not in json.dumps(status) and '***' in status.get('error', '')
    assert sleeps_a and all(s >= 300 for s in sleeps_a)
    actor.inbound_loop(b, r=r, cfg={}, submit=recorder(), reply=replies_sink(), clock=time.time,
                       sleep=sleeps_b.append, stop=lambda: len(sleeps_b) >= 1)  # another account is untouched
    assert r.hget(channels.STATUS + 'fake:b', 'state') == 'ok'


def test_duplicate_envelope_never_reaches_submit(private_redis_v2, fake_registered):
    """One dedupe owner on the actor path: handle_inbound as the accept callback (the base default accept is not
    called first, so the first message is not swallowed); the cursor is acked only after the fleet answered."""
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    order = []

    class Acking(FakeChannel):
        def _ack(self, raw):
            order.append(('ack', raw['id']))
    ch = Acking(channels.Account('fake', ACCOUNT, ['u-1']), transport=lambda op, data: [],
                store=actor.RedisStore(r, 'fake', ACCOUNT))

    def submit(text, origin):
        order.append(('submit', text))
        return {'status': 'accepted', 'id': 'ui-1'}

    def accept(e):
        return actor.handle_inbound(r, e, submit=submit, reply=replies_sink(), audit=Audit())
    raw = {'id': 5, 'chat': 'c-1', 'from': 'u-1', 'text': 'once'}
    assert [e['text'] for e in ch.inbound(raw, accept=accept)] == ['once']
    assert order == [('submit', 'once'), ('ack', 5)]  # submitted exactly once, acked after the answer
    assert ch.inbound(dict(raw), accept=accept) == []  # redelivered: a duplicate, not submitted again
    assert order[-1] == ('ack', 5) and [o for o in order if o[0] == 'submit'] == [('submit', 'once')]
    env = ch.parse_inbound(raw)[0]
    assert actor.handle_inbound(r, env, submit=submit, reply=replies_sink(), audit=Audit()) == 'duplicate'  # direct call too
    assert [o for o in order if o[0] == 'submit'] == [('submit', 'once')]
