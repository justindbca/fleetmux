"""§53「直连 worker」 (user 2026-11-10): a chat message can reach a worker without the supervisor.

- ``@worker-1 text`` / ``worker-1: text`` (the user's own words) -> a service request with ``to`` (Fleet.request ->
  assign, no supervisor turn); the thread under that message is bound to the worker.
- A reply typed inside a thread bound to a worker (``reply_ref.in_thread``) goes back to that worker.
- A pushed result answered by a worker binds its origin's thread to it; every pushed result ends with one hint line
  on how to reach a worker directly.
- Slack: replies go into the thread under the user's message; a result is also broadcast to the chat itself.

Real private Redis + the in-process v2 service fleet (``fx``: pi supervisor, worker-1 / worker-2), fake channels.
"""
import json
import time
import uuid

from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_runtime_v2 import fx  # noqa: F401
from test_channels_contract import Audit, FakeChannel, config_dir, fake_registered  # noqa: F401
from test_channel_actor_inbound import drive, events, fresh, recorder, replies_sink
from test_channel_actor_outbound import ENTRY, publish, queued
from fleetmux import channels
from fleetmux.channels import envelope

ACCOUNT = 'contract-1'


def env_of(text, *, thread=None, in_thread=False, mid=None):
    ref = {'chat': 'c-1'}
    if thread:
        ref['thread_ts'] = thread
    if in_thread:
        ref['in_thread'] = True
    return envelope.make('fake', ACCOUNT, conversation='c-1', message_id=mid or uuid.uuid4().hex[:12],
                         sender='u-1', text=text, reply_ref=ref)


def fleet_direct(actor, f, timeout=5):
    client = fresh(f)
    answers = []

    def direct(worker, text, origin):
        answers.append(drive(f, lambda: actor.submit_direct(f.cfg, client, worker, text, origin, timeout=timeout)))
        return answers[-1]
    direct.answers = answers
    return direct


def test_parse_address():
    from fleetmux.channels import actor
    assert actor.parse_address('@dev-1 看下日志') == ('dev-1', '看下日志')
    assert actor.parse_address('  @dev-1：看下日志') == ('dev-1', '看下日志')
    assert actor.parse_address('dev-2: 改一下 README') == ('dev-2', '改一下 README')
    assert actor.parse_address('dev-2：改一下') == ('dev-2', '改一下')
    assert actor.parse_address('@dev-1.2 接着做') == ('dev-1.2', '接着做')
    assert actor.parse_address('@dev-1') == ('dev-1', '')
    for plain in ('帮我看下 @dev-1 的结果', '@dev 你好', 'dev-1 你好', '@alice hi', ''):
        assert actor.parse_address(plain) == (None, plain), plain


def test_at_worker_skips_supervisor_and_binds_thread(fx, fake_registered):
    from fleetmux.channels import actor
    f = fx
    direct, reply = fleet_direct(actor, f), replies_sink()
    submit = recorder()
    env = env_of('@worker-1 跑一下单测', thread='111.1')
    assert actor.handle_inbound(f.r, env, submit=submit, reply=reply, direct=direct) == 'persisted'
    assert submit.calls == []  # the supervisor path is not taken
    assert events(f) == []  # no operator_input
    answer = direct.answers[-1]
    assert answer['status'] == 'accepted' and answer['id'] == actor.request_id(env)
    req = f.r.hgetall('fx:req:' + answer['id'])
    assert req['status'] == 'dispatched' and req['worker'] == 'worker-1'
    spec = json.loads(req['spec'])
    assert spec['to'] == 'worker-1' and spec['payload'] == {'message': '跑一下单测'}
    assert spec['reply'] == {'required': ['reply'], 'allowed': ['reply']} and 'operator' not in req
    assert json.loads(f.r.get(channels.ORIGIN + answer['id'])) == envelope.origin_from(env)
    assert f.r.get(actor.THREAD + 'fake:' + ACCOUNT + ':111.1') == 'worker-1'
    assert reply.texts[-1] == envelope.receipt('direct_accepted', worker='worker-1', id=answer['id'], busy='')


def test_colon_form_and_busy_note(fx, fake_registered):
    from fleetmux.channels import actor
    f = fx
    f.r.hset('fx:agent:worker-2', 'state', 'busy')
    direct, reply = fleet_direct(actor, f), replies_sink()
    actor.handle_inbound(f.r, env_of('worker-2: 看看'), submit=recorder(), reply=reply, direct=direct)
    answer = direct.answers[-1]
    assert answer['status'] == 'accepted' and answer['busy'] is True
    assert reply.texts[-1] == envelope.receipt('direct_accepted', worker='worker-2', id=answer['id'],
                                               busy=actor.DIRECT_BUSY)


def test_unknown_worker_is_rejected_with_the_fleet_reason(fx, fake_registered):
    from fleetmux.channels import actor
    f = fx
    direct, reply, submit = fleet_direct(actor, f), replies_sink(), recorder()
    env = env_of('@worker-9 在吗')
    assert actor.handle_inbound(f.r, env, submit=submit, reply=reply, direct=direct) == 'persisted'
    assert submit.calls == []
    assert direct.answers[-1]['status'] == 'rejected'
    assert reply.texts[-1].startswith('Fleet 输入被拒绝：') and 'worker-1' in reply.texts[-1]
    assert f.r.keys(channels.ORIGIN + '*') == []


def test_thread_reply_goes_to_its_bound_worker(fx, fake_registered):
    from fleetmux.channels import actor
    f = fx
    f.r.set(actor.THREAD + 'fake:' + ACCOUNT + ':222.2', 'worker-2')
    direct, submit = recorder({'status': 'accepted', 'id': 'ch-x'}), recorder()

    def as_direct(worker, text, origin):
        return direct(worker + '|' + text, origin)
    actor.handle_inbound(f.r, env_of('再补个测试', thread='222.2', in_thread=True), submit=submit,
                         reply=replies_sink(), direct=as_direct)
    assert [c[0] for c in direct.calls] == ['worker-2|再补个测试'] and submit.calls == []
    # the same thread root, but the message is the thread's first one (not a reply in it): the supervisor
    actor.handle_inbound(f.r, env_of('新问题', thread='222.2'), submit=submit, reply=replies_sink(), direct=as_direct)
    assert len(submit.calls) == 1 and len(direct.calls) == 1
    # an explicit name wins over the thread binding
    actor.handle_inbound(f.r, env_of('@worker-1 你来', thread='222.2', in_thread=True), submit=submit,
                         reply=replies_sink(), direct=as_direct)
    assert direct.calls[-1][0] == 'worker-1|你来'
    # an unbound thread: the supervisor
    actor.handle_inbound(f.r, env_of('问一下', thread='333.3', in_thread=True), submit=submit,
                         reply=replies_sink(), direct=as_direct)
    assert len(submit.calls) == 2


def test_no_direct_binding_answers_with_a_receipt(fx, fake_registered):
    from fleetmux.channels import actor
    f = fx
    reply, submit = replies_sink(), recorder()
    assert actor.handle_inbound(f.r, env_of('@worker-1 hi'), submit=submit, reply=reply) == 'persisted'
    assert submit.calls == []
    assert reply.texts[-1] == envelope.receipt('direct_unavailable', worker='worker-1')


def test_result_binds_thread_and_ends_with_hint(private_redis_v2, fake_registered):
    from fleetmux.channels import actor
    r = private_redis_v2
    r.flushdb()
    env = env_of('q', thread='444.4', mid='m-444')
    rec = {'id': 'r-w', 'status': 'ok', 'at': time.time(), 'worker': 'worker-1.1', 'result': {'reply': '做完了'}}
    r.set(channels.ORIGIN + 'r-w', json.dumps(envelope.origin_from(env)), ex=channels.ORIGIN_TTL)
    r.set('fx:result:r-w', json.dumps(rec, ensure_ascii=False))
    r.rpush('fx:results', json.dumps(rec, ensure_ascii=False))
    publish(r, {'id': 'r-s', 'worker': 'supervisor'})
    assert actor.outbound_scan(r, [ENTRY], workers=lambda: ['worker-1', 'worker-2']) == 2
    texts = {i['request_id']: i['text'] for i in queued(r)}
    assert texts['r-w'].startswith('做完了\n\n—— 在本帖里回复 = 接着找 worker-1；新消息开头写 @worker-1 直接派活')
    assert texts['r-s'].startswith('answer r-s\n\n—— 要直接派活，消息开头写 @worker-1 之类（可点名：worker-1、worker-2）')
    assert r.get(actor.THREAD + 'fake:' + ACCOUNT + ':444.4') == 'worker-1'
    assert r.keys(actor.THREAD + '*') == [actor.THREAD + 'fake:' + ACCOUNT + ':444.4']  # the supervisor binds nothing


def test_result_without_worker_list_has_no_hint(private_redis_v2, fake_registered):
    from fleetmux.channels import actor
    r = private_redis_v2
    r.flushdb()
    publish(r, {'id': 'r1', 'worker': 'worker-1'})
    assert actor.outbound_scan(r, [ENTRY]) == 1
    assert queued(r)[0]['text'] == 'answer r1'


def test_result_in_a_thread_is_sent_with_broadcast():
    from fleetmux.channels import actor

    class Rec(FakeChannel):
        def __init__(self):
            super().__init__(channels.Account('fake', ACCOUNT, ['u-1']), transport=lambda op, data: [])
            self.refs = []

        def send_text(self, conversation, text, *, reply_ref=None, key=None):
            self.refs.append(reply_ref)
    ch = Rec()
    actor._send_item(ch, envelope.outbound('result', text='x', target='explicit', channel='fake', account=ACCOUNT,
                                           conversation='c-1', reply_ref={'thread_ts': '1.1'}, request_id='r'))
    actor._send_item(ch, envelope.outbound('reply', text='y', target='explicit', channel='fake', account=ACCOUNT,
                                           conversation='c-1', reply_ref={'thread_ts': '1.1'}))
    assert ch.refs == [{'thread_ts': '1.1', 'broadcast': True}, {'thread_ts': '1.1'}]


def test_slack_broadcast_flag_reaches_the_api():
    from test_channel_slack import DM, FakeSlackAPI, make
    api = FakeSlackAPI()
    ch = make(api)
    assert ch.send_text(DM, 'result', reply_ref={'channel': DM, 'thread_ts': '9.9', 'broadcast': True}).ok
    assert ch.send_text(DM, 'receipt', reply_ref={'channel': DM, 'thread_ts': '9.9'}).ok
    posts = api.named('chat.postMessage')
    assert posts[0]['params'].get('reply_broadcast') in (True, 'true', 'True')
    assert 'reply_broadcast' not in posts[1]['params']
    assert all(p['params'].get('thread_ts') == '9.9' for p in posts)


def test_pushes_to_last_go_top_level_not_into_the_last_thread():
    from fleetmux.channels import actor

    class Rec(FakeChannel):
        def __init__(self):
            super().__init__(channels.Account('fake', ACCOUNT, ['u-1']), transport=lambda op, data: [])
            self.refs = []

        def send_text(self, conversation, text, *, reply_ref=None, key=None):
            self.refs.append(reply_ref)

        def send_card(self, conversation, card, *, reply_ref=None, key=None):
            self.refs.append(reply_ref)
    ch = Rec()
    ref = {'channel': 'D1', 'thread_ts': '5.5', 'in_thread': True}
    for event in ('task_done', 'daemon_down', 'run_closed'):
        actor._send_item(ch, envelope.outbound(event, text='x', target='explicit', channel='fake', account=ACCOUNT,
                                               conversation='c-1', reply_ref=dict(ref)))
    actor._send_item(ch, envelope.outbound('prompt_wait', card={'title': 't', 'text': 'x', 'buttons': [], 'ref': 'r'},
                                           target='explicit', channel='fake', account=ACCOUNT, conversation='c-1',
                                           reply_ref=dict(ref)))
    assert ch.refs == [{'channel': 'D1'}] * 4
