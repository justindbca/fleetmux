"""Action confirmation from the phone (P8k; brief v2.2 §8.4, contracts §8 "动作确认" + C1-C8 / D1 / D3 as frozen).

approve-1 P8kl-test-plan.md §1 / §5 and P8-child-confirm-routing.md §12 (the single source of names, signatures
and closed outcome / reason sets): ``approve.register_action`` -> ``Register``, ``approve.consume_ticket`` ->
``Consume`` (``bool()`` raises, callers compare ``.outcome``), step one through ``approve.handle_command``, the
second step through ``approve.answer_action`` (ISSUE: nonce / identity / ``oob.check`` in Python, then the record
version re-checked atomically before the ticket ``fx:guard:ticket:<key_hash>`` is written), the stop epoch
``fx:service.stop_epoch`` (``^(0|[1-9][0-9]{0,15})$`` and < 2**53, malformed -> ``epoch_malformed``).

Real private Redis (``private_redis_v2``) + the P0 FakeChannel account in a temp config dir; the epoch is raised
with ``HINCRBY fx:service stop_epoch 1`` exactly as P8m publishes it (the test never calls P8m). No network, no
credentials, no listener, no key reaches a real pane, nothing is deleted.
"""
import copy
import hashlib
import json
import re

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn, dispatch_one, mixed  # noqa: F401  (fixtures / helpers)
from test_watchdog import (DIALOG_RM, audits, escalations, keys, waiting, wd)  # noqa: F401
from test_channel_approve import (PID_RE, env, fake_account, mark_rm_with_channels, nonce_of, phone, r,  # noqa: F401
                                  record, redis_dump, said, user_table, wdc)
from cmux_fleet import channels
from cmux_fleet.channels import approve, envelope, oob, security

H = 'a' * 32
RUN_ID = 'run-p8k'
SLOT = 'worker-1'
REQ = 'rq-1'
POLICY = security.parse_security('')[0]
HEX16 = re.compile(r'\b[0-9a-f]{16}\b')
CONSUME_REASONS = {'stale_call', 'stopped', 'no_agent', 'old_generation', 'other_request', 'binding',
                   'not_approved', 'expired', 'epoch_malformed'}                                    # C8.1
REGISTER_REASONS = {'stale_call', 'stopped', 'no_agent', 'old_generation', 'other_request', 'exhausted',
                    'epoch_malformed'}                                                              # C8.2
BAD_EPOCHS = ['-1', '1.5', '1e3', 'inf', 'nan', '01', ' 1', '', '9007199254740992', '12345678901234567']
OK_EPOCHS = ['0', '7', '9007199254740991']


# ---------------------------------------------------------------------------------------------- helpers
class Audit(list):
    """An ``audit(event, **fields)`` callable that keeps every call."""

    def __call__(self, event, **fields):
        self.append((event, fields))

    def events(self, name, **known):
        return [f for e, f in self if e == name and all(f.get(k) == v for k, v in known.items())]


def policy_with(**confirm):
    p = copy.deepcopy(POLICY)
    p['confirm'].update(confirm)
    return p


def seed_agent(rr, slot=SLOT, generation=0, request=REQ):
    rr.hset('fx:agent:' + slot, mapping={'generation': str(generation), 'request': request, 'state': 'busy'})


def bump(rr):
    rr.hincrby('fx:service', 'stop_epoch', 1)


def reg(rr, **kw):
    args = dict(slot=SLOT, generation=0, request_id=REQ, run_id=RUN_ID, cls='delete', tier='normal', action_hash=H,
                action_text='rm -rf build/tmp', call_epoch=0, oob_modes='desk')
    args.update(kw)
    return approve.register_action(rr, **args)


def expect(**kw):
    e = {'scope': 'local', 'run_id': RUN_ID, 'slot': SLOT, 'generation': 0, 'request_id': REQ, 'action_hash': H}
    e.update(kw)
    return e


def det_pid(rec_fields, seq):
    """C2: p-id = 'p-' + sha1(B + '\\0' + seq)[:8], B = the full binding joined with \\0."""
    order = ('kind', 'scope', 'run_id', 'slot', 'generation', 'request_id', 'action_hash', 'epoch', 'route_epoch',
             'root_request', 'tier', 'oob_modes')
    b = '\0'.join(str(rec_fields.get(k, '')) for k in order)
    return 'p-' + hashlib.sha1((b + '\0' + str(seq)).encode()).hexdigest()[:8]


def ev_for(rec, nonce, *, decision='approve', sender='u-1', conversation='c-1', channel='fake', account='contract-1',
           code=None):
    e = {'kind': 'dialog_user_answer', 'pending_id': rec['id'], 'decision': decision, 'nonce': nonce,
         'channel': channel, 'account': account, 'sender': sender, 'conversation': conversation,
         'private': True, 'verified': True}
    if code is not None:
        e['code'] = code
    return e


def first_step(rr, pid, **env_kw):
    replies = []
    e = env('/approve ' + pid, **env_kw)
    approve.handle_command(rr, e, envelope.parse_command(e['text']), post=lambda ev: {'status': 'accepted'},
                           reply=replies.append)
    return replies, nonce_of(replies)


def approved_ticket(rr, audit, **reg_kw):
    """Register, step one, step two (ISSUE) -> (pid, nonce, ISSUE result)."""
    seed_agent(rr)
    res = reg(rr, **reg_kw)
    assert res.outcome == 'registered', res
    _, nonce = first_step(rr, res.p_id)
    out = approve.answer_action(rr, record(rr, res.p_id), ev_for(record(rr, res.p_id), nonce), policy=POLICY,
                                audit=audit)
    return res.p_id, nonce, out


def ticket_keys(rr):
    return sorted(rr.scan_iter(channels.GUARD_TICKET + '*'))


# ---------------------------------------------------------------------------------------------- P8k 1-10
@pytest.mark.parametrize('scope', ['local', 'routed'])
def test_register_action_writes_kind_action_fields_and_reuses_same_hash(r, scope):
    seed_agent(r)
    kw = {}
    if scope == 'routed':
        k = approve.route_hash(RUN_ID, H)
        assert re.fullmatch(r'[0-9a-f]{32}', k) and k != H
        kw = {'route': approve.Route(hops=(('/p/config.json', SLOT, 0, 't-1'),), cfg_paths=('/c', '/p'),
                                     root_cfg_path='/p', policies=(POLICY, POLICY), taint=None, r=r, run_id=RUN_ID,
                                     task_id='t-1', policy=POLICY, oob_modes='desk', root_request=''),
              'route_epoch': 0}
    res = reg(r, **kw)
    assert res.outcome == 'registered' and res.reason == '' and re.fullmatch(PID_RE, res.p_id)
    with pytest.raises(TypeError):
        bool(res)
    rec = record(r, res.p_id)
    assert rec['kind'] == 'action' and rec['scope'] == scope and rec['run_id'] == RUN_ID
    assert rec['tier'] == 'normal' and rec['action_hash'] == H and rec['action_text'] == 'rm -rf build/tmp'
    assert rec['request_id'] == REQ and rec['slot'] == SLOT and rec['generation'] == '0'
    assert rec['epoch'] == '0' and rec['oob_modes'] == 'desk' and rec['status'] == 'pending'
    assert not {'route_hash', 'chain', 'route_run_id', 'route_run'} & set(rec)
    assert 0 < r.ttl(channels.PENDING + res.p_id) <= channels.APPROVE_TTL_S
    assert res.p_id == det_pid(rec, 0)
    assert not list(r.scan_iter(channels.PENDING_SLOT + '*'))                       # ruling #9: no pending_slot
    # full binding equal -> reused, same p-id; any field different -> a new record
    again = reg(r, **kw)
    assert again.outcome == 'reused' and again.p_id == res.p_id
    seed_agent(r, request='rq-2')
    other = reg(r, request_id='rq-2', **kw)
    assert other.outcome == 'registered' and other.p_id != res.p_id
    # epoch raised: the old call is refused, a new call binds the new epoch
    bump(r)
    assert reg(r, request_id='rq-2', **kw) == approve.Register('refused', '', 'stale_call')
    fresh = reg(r, request_id='rq-2', call_epoch=1, **kw)
    assert fresh.outcome == 'registered' and record(r, fresh.p_id)['epoch'] == '1'
    # an unhandled /stop refuses
    r.hset(channels.STOP, mapping={'level': 'pause', 'by': 'fake:contract-1:u-1', 'at': '1'})
    assert reg(r, request_id='rq-2', call_epoch=1, **kw) == approve.Register('refused', '', 'stopped')
    r.delete(channels.STOP)
    if scope == 'local':
        r.delete('fx:agent:' + SLOT)                                                # no default generation 0
        assert reg(r, call_epoch=1) == approve.Register('refused', '', 'no_agent')
        seed_agent(r, generation=2, request='rq-2')
        assert reg(r, request_id='rq-2', call_epoch=1).reason == 'old_generation'
        assert reg(r, generation=2, call_epoch=1).reason == 'other_request'
        seed_agent(r, generation=2, request='')                                     # empty request on a live slot
        assert reg(r, generation=2, request_id='', call_epoch=1).outcome == 'registered'
    # 8 occupied seq slots -> exhausted, never overwritten
    seed_agent(r, generation=5, request='rq-x')
    fields = {'kind': 'action', 'scope': scope, 'run_id': RUN_ID, 'slot': SLOT, 'generation': 5,
              'request_id': 'rq-x', 'action_hash': H, 'epoch': 1, 'route_epoch': '' if scope == 'local' else 0,
              'root_request': '', 'tier': 'normal', 'oob_modes': 'desk'}
    for seq in range(8):
        r.hset(channels.PENDING + det_pid(fields, seq), mapping={'kind': 'action', 'status': 'done'})
    assert reg(r, generation=5, request_id='rq-x', call_epoch=1, **kw).reason == 'exhausted'
    # malformed epoch -> epoch_malformed, zero writes (D3)
    for bad in BAD_EPOCHS:
        r.hset('fx:service', 'stop_epoch', bad)
        before = redis_dump(r)
        out = reg(r, generation=5, request_id='rq-x', call_epoch=1, **kw)
        assert out == approve.Register('refused', '', 'epoch_malformed') and redis_dump(r) == before
        with pytest.raises(approve.EpochError):
            approve.current_epoch(r)
    for ok in OK_EPOCHS:
        r.hset('fx:service', 'stop_epoch', ok)
        assert approve.current_epoch(r) == int(ok)


@pytest.mark.parametrize('cls', list(security.CLASSES))
def test_action_card_title_by_class_body_from_action_text_marks_untrusted(r, cls):
    rec = {'id': 'p-0123abcd', 'kind': 'action', 'scope': 'local', 'tier': 'normal', 'cls': cls, 'class': cls,
           'action_hash': H, 'action_text': 'rm -rf build/tmp', 'slot': SLOT, 'request_id': REQ, 'oob_modes': 'desk'}
    card = approve.action_card(rec)
    text = envelope.card_text(card)
    assert envelope.card_problem(card) == ''
    assert card['title'].startswith('确认') and 'rm -rf build/tmp' in text and SLOT in text and REQ in text
    commands = [b['command'] for b in card['buttons']]
    assert commands == ['/approve p-0123abcd', '/reject p-0123abcd']
    assert not HEX16.search(text) and not any(HEX16.search(c) for c in commands)     # no nonce before step one
    assert envelope.RECEIPTS['untrusted_notice'] not in text
    r.set(channels.UNTRUSTED + REQ, 'web_page')
    marked = approve.action_card(dict(rec, untrusted='1'))
    assert envelope.RECEIPTS['untrusted_notice'] in envelope.card_text(marked)
    routed = envelope.card_text(approve.action_card(dict(rec, scope='routed', route_task='t-1',
                                                         root_request='rq-root')))
    assert 't-1' in routed and 'rq-root' in routed


def test_first_step_on_action_replies_confirm_action_card_with_nonce(r):
    seed_agent(r)
    res = reg(r)
    posted = []
    replies = []
    e = env('/approve ' + res.p_id)
    approve.handle_command(r, e, envelope.parse_command(e['text']), post=posted.append, reply=replies.append)
    nonce = nonce_of(replies)
    assert re.fullmatch(r'[0-9a-f]{16}', nonce) and not posted
    assert said(replies, 'confirm_action', action='rm -rf build/tmp')
    rec = record(r, res.p_id)
    assert rec['status'] == 'confirming' and rec['nonce_hash'] == hashlib.sha256(nonce.encode()).hexdigest()
    assert (rec['sender'], rec['conversation'], rec['channel'], rec['account']) == ('u-1', 'c-1', 'fake', 'contract-1')
    assert nonce not in json.dumps(r.lrange(channels.OUTQ, 0, -1))                 # the card goes by reply only


@pytest.mark.parametrize('ttl', [None, 600])
def test_second_step_approve_writes_single_use_ticket_and_wakes_waiter(r, monkeypatch, ttl):
    audit = Audit()
    calls = []
    real = r.execute_command
    monkeypatch.setattr(r, 'execute_command', lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    reg_kw = {} if ttl is None else {'ttl_s': ttl}
    pid, nonce, out = approved_ticket(r, audit, **reg_kw)
    assert r.get(channels.GUARD_TICKET + H) == pid
    assert r.blpop(channels.GUARD_WAIT + H, timeout=1)[1] == 'approve'
    assert audit.events('security.action_approved', stage='answered')
    assert all(c in ('EVAL', 'EVALSHA') for c in calls if c in ('SET', 'GETDEL', 'EVAL', 'EVALSHA'))
    res = approve.consume_ticket(r, H, expect=expect(), call_epoch=0, audit=audit)
    assert res == approve.Consume('consumed', pid, '') and record(r, pid)['status'] == 'done'
    with pytest.raises(TypeError):
        bool(res)
    assert audit.events('security.action_approved', stage='consumed')
    assert not audit.events('security.action_approved', stage='allowed')           # approve never writes allowed
    assert approve.consume_ticket(r, H, expect=expect(), call_epoch=0, audit=audit).outcome == 'absent'
    with pytest.raises(TypeError):
        approve.consume_ticket(r, H, expect={'scope': 'local'}, call_epoch=0, audit=audit)
    # bad binding -> refused once, then absent
    pid2, _, _ = approved_ticket(r, audit, request_id=REQ, action_text='x')
    assert approve.consume_ticket(r, H, expect=expect(run_id='run-other'), call_epoch=0, audit=audit) == \
        approve.Consume('refused', pid2, 'binding')
    assert approve.consume_ticket(r, H, expect=expect(), call_epoch=0, audit=audit).outcome == 'absent'
    # epoch raised after the ticket: old call -> stale_call
    pid3, _, _ = approved_ticket(r, audit, action_text='y')
    bump(r)
    assert approve.consume_ticket(r, H, expect=expect(), call_epoch=0, audit=audit) == \
        approve.Consume('refused', pid3, 'stale_call')
    # unhandled /stop at consume -> stopped
    pid4, _, _ = approved_ticket(r, audit, call_epoch=1, action_text='z')
    r.hset(channels.STOP, mapping={'level': 'pause', 'by': 'x', 'at': '1'})
    assert approve.consume_ticket(r, H, expect=expect(), call_epoch=1, audit=audit) == \
        approve.Consume('refused', pid4, 'stopped')
    r.delete(channels.STOP)
    # a ticket pointing at a record that is not answered -> not_approved; past expires -> expired
    seed_agent(r)
    res5 = reg(r, call_epoch=1, action_text='w')
    r.set(channels.GUARD_TICKET + H, res5.p_id)
    assert approve.consume_ticket(r, H, expect=expect(), call_epoch=1, audit=audit).reason == 'not_approved'
    pid6, _, _ = approved_ticket(r, audit, call_epoch=1, action_text='v')
    late = float(record(r, pid6)['expires']) + 1
    assert approve.consume_ticket(r, H, expect=expect(), call_epoch=1, audit=audit, now=late).reason == 'expired'
    # local generation / delivery / agent
    pid7, _, _ = approved_ticket(r, audit, call_epoch=1, action_text='u')
    seed_agent(r, generation=3)
    assert approve.consume_ticket(r, H, expect=expect(), call_epoch=1, audit=audit).reason == 'old_generation'
    seed_agent(r)
    pid8, _, _ = approved_ticket(r, audit, call_epoch=1, action_text='t')
    seed_agent(r, request='rq-9')
    assert approve.consume_ticket(r, H, expect=expect(), call_epoch=1, audit=audit).reason == 'other_request'
    seed_agent(r)
    approved_ticket(r, audit, call_epoch=1, action_text='s')
    r.delete('fx:agent:' + SLOT)
    assert approve.consume_ticket(r, H, expect=expect(), call_epoch=1, audit=audit).reason == 'no_agent'
    # malformed epoch: refused epoch_malformed, ticket still there, zero writes (D3)
    seed_agent(r)
    approved_ticket(r, audit, call_epoch=1, action_text='q')
    for bad in BAD_EPOCHS:
        r.hset('fx:service', 'stop_epoch', bad)
        before = redis_dump(r)
        out = approve.consume_ticket(r, H, expect=expect(), call_epoch=1, audit=audit)
        assert out == approve.Consume('refused', '', 'epoch_malformed') and redis_dump(r) == before
    assert {f['reason'] for e, f in audit if e == 'security.action_denied'} <= CONSUME_REASONS


def test_reject_wakes_waiter_with_reject_no_ticket(r):
    audit = Audit()
    seed_agent(r)
    res = reg(r)
    rec = record(r, res.p_id)
    out = approve.answer_action(r, rec, ev_for(rec, '', decision='reject'), policy=POLICY, audit=audit)
    assert r.blpop(channels.GUARD_WAIT + H, timeout=1)[1] == 'reject'
    assert not ticket_keys(r) and audit.events('security.action_denied')
    # epoch already raised when the reject arrives -> stale / stopped, still no ticket
    res2 = reg(r, action_text='other')
    bump(r)
    rec2 = record(r, res2.p_id)
    approve.answer_action(r, rec2, ev_for(rec2, '', decision='reject'), policy=POLICY, audit=audit)
    assert record(r, res2.p_id)['status'] == 'stale' and record(r, res2.p_id)['stale_reason'] == 'stopped'
    assert not ticket_keys(r) and out is not None


CASES = ['wrong_nonce', 'other_sender', 'other_conversation', 'past_confirm_until', 'past_expires',
         'epoch_bumped_after_step_one', 'old_epoch_first_step', 'first_step_redone_after_check',
         'identity_changed_after_check', 'stale_call_without_ticket', 'stop_pending_at_consume',
         'dialog_claim_press_after_epoch_bump']


@pytest.mark.parametrize('case', CASES)
def test_wrong_nonce_sender_conversation_or_expired_is_stale_no_ticket(r, monkeypatch, case):
    audit = Audit()
    seed_agent(r)
    if case == 'stale_call_without_ticket':
        bump(r)
        assert approve.consume_ticket(r, H, expect=expect(), call_epoch=0, audit=audit) == \
            approve.Consume('refused', '', 'stale_call')
        assert reg(r, call_epoch=0).reason == 'stale_call'
        return
    if case == 'stop_pending_at_consume':
        r.hset(channels.STOP, mapping={'level': 'pause', 'by': 'x', 'at': '1'})
        assert approve.consume_ticket(r, H, expect=expect(), call_epoch=0, audit=audit) == \
            approve.Consume('refused', '', 'stopped')
        return
    if case == 'dialog_claim_press_after_epoch_bump':
        r.hset(channels.PENDING + 'p-0000aaaa', mapping={'id': 'p-0000aaaa', 'status': 'answered', 'epoch': '0',
                                                         'slot': SLOT, 'decision': 'approve'})
        bump(r)
        assert approve.claim_press(r, 'p-0000aaaa') is False
        assert record(r, 'p-0000aaaa')['status'] == 'stale'
        return
    racing = case in ('first_step_redone_after_check', 'identity_changed_after_check')
    # the race cases use a high-tier record: oob.check is the last Python check before the ISSUE script
    res = reg(r, tier='high', cls='real_money', oob_modes='second_channel') if racing else reg(r)
    pid = res.p_id
    if case == 'old_epoch_first_step':
        bump(r)
        replies, nonce = first_step(r, pid)
        assert not nonce and said(replies, 'refused', id=pid)
        assert not ticket_keys(r)
        return
    replies, nonce = first_step(r, pid)
    rec = record(r, pid)
    ev = ev_for(rec, nonce)
    now = None
    if case == 'wrong_nonce':
        ev['nonce'] = 'f' * 16
    elif case == 'other_sender':
        ev['sender'] = 'u-2'
    elif case == 'other_conversation':
        ev['conversation'] = 'c-2'
    elif case == 'past_confirm_until':
        now = float(rec['confirm_until']) + 1
    elif case == 'past_expires':
        now = float(rec['expires']) + 1
    elif case == 'epoch_bumped_after_step_one':
        bump(r)
    elif racing:
        ev.update(channel='fake2', account='contract-2')

        def change_between_check_and_script(*a, **k):
            if case == 'first_step_redone_after_check':
                first_step(r, pid)                                              # new nonce, new confirm_until
            else:
                r.hset(channels.PENDING + pid, 'conversation', 'c-9')
            return (True, '')
        monkeypatch.setattr(oob, 'check', change_between_check_and_script)
    approve.answer_action(r, rec, ev, policy=POLICY, audit=audit, now=now)
    assert not ticket_keys(r) and r.llen(channels.GUARD_WAIT + H) == 0
    after = record(r, pid)
    assert after['status'] in ('confirming', 'stale')
    if case == 'epoch_bumped_after_step_one':
        assert after['status'] == 'stale' and after['stale_reason'] == 'stopped'


@pytest.mark.parametrize('mode', ['desk', 'second_channel', 'totp'])
def test_high_tier_calls_oob_check_and_refuses_when_false(r, monkeypatch, mode):
    audit = Audit()
    calls = []
    monkeypatch.setattr(oob, 'check', lambda rec, ev, policy, **k: calls.append(rec.get('oob_modes')) or (False, 'no'))
    seed_agent(r)
    pol = policy_with(high=mode)
    res = reg(r, tier='high', cls='real_money', oob_modes=mode)
    replies, nonce = first_step(r, res.p_id)
    rec = record(r, res.p_id)
    out = approve.answer_action(r, rec, ev_for(rec, nonce), policy=pol, audit=audit)
    assert calls == [mode] and not ticket_keys(r) and audit.events('security.oob_refused')
    assert record(r, res.p_id)['status'] == 'confirming' and out is not None
    # True from oob.check but a wrong nonce still issues nothing
    monkeypatch.setattr(oob, 'check', lambda *a, **k: (True, ''))
    approve.answer_action(r, rec, ev_for(rec, 'f' * 16), policy=pol, audit=audit)
    assert not ticket_keys(r)
    approve.answer_action(r, rec, ev_for(rec, nonce), policy=pol, audit=audit)
    assert ticket_keys(r) == [channels.GUARD_TICKET + H]
    # X3: the record's oob_modes decides, not a later policy change
    assert record(r, res.p_id)['oob_modes'] == mode
    # O2 + D1: the root's confirm.high decides the mode; any hop missing / invalid -> policy_refused
    child = policy_with(high='totp' if mode != 'totp' else 'desk')
    eff, why = approve.effective_policy((child, pol))
    assert eff['confirm']['high'] == mode and why == ''
    broken = copy.deepcopy(child)
    del broken['confirm']['high']
    assert approve.effective_policy((broken, pol))[1] == 'policy_refused'
    assert approve.effective_policy((policy_with(high='sms'), pol))[1] == 'policy_refused'
    stricter = copy.deepcopy(child)
    stricter['confirm']['classes']['delete'] = 'high'
    assert approve.effective_policy((stricter, pol))[0]['confirm']['classes']['delete'] == 'high'
    # tier=normal never uses oob: a different sender is refused even if oob.check would say yes
    res_n = reg(r, action_text='normal one')
    _, n_nonce = first_step(r, res_n.p_id)
    rec_n = record(r, res_n.p_id)
    approve.answer_action(r, rec_n, ev_for(rec_n, n_nonce, sender='u-2'), policy=pol, audit=audit)
    assert record(r, res_n.p_id)['status'] == 'confirming'


def test_supervisor_decision_cli_and_channel_send_cannot_create_a_ticket(r):
    from cmux_fleet.channels import cli
    audit = Audit()
    seed_agent(r)
    res = reg(r, tier='high', cls='real_money', oob_modes='desk')
    _, nonce = first_step(r, res.p_id)
    rec = record(r, res.p_id)
    before = record(r, res.p_id)
    # a forged desk event without / with a wrong desk_proof
    for proof in (None, 'deadbeef'):
        ev = ev_for(rec, nonce, channel='desk', account='desk')
        if proof:
            ev['desk_proof'] = proof
        approve.answer_action(r, rec, ev, policy=POLICY, audit=audit)
    assert not ticket_keys(r) and record(r, res.p_id) == before
    # supervisor escalation answers on either key shape write nothing to the guard keys
    r.hset('fx:escalations', mapping={'dialog:' + res.p_id: json.dumps({'decision': 'approve'}),
                                      channels.PENDING + res.p_id: json.dumps({'decision': 'approve'})})
    assert not ticket_keys(r)
    # raising the epoch alone writes no pending / guard key; afterwards nothing can issue a ticket
    keys_before = set(r.scan_iter('fx:channel:pending*')) | set(r.scan_iter('fx:guard:*'))
    bump(r)
    assert set(r.scan_iter('fx:channel:pending*')) | set(r.scan_iter('fx:guard:*')) == keys_before
    approve.answer_action(r, rec, ev_for(rec, nonce), policy=POLICY, audit=audit)
    assert not ticket_keys(r)
    assert approve.consume_ticket(r, H, expect=expect(), call_epoch=0, audit=audit).outcome == 'refused'
    # channel approve on a tier=normal / dialog record -> exit 1, no ticket; malformed epoch -> exit 1
    res_n = reg(r, call_epoch=1, action_text='normal')
    assert cli.run(['channel', 'approve', '--config', '/nonexistent/config.json', res_n.p_id, 'f' * 16]) == 1
    r.hset('fx:service', 'stop_epoch', '-1')
    assert cli.run(['channel', 'approve', '--config', '/nonexistent/config.json', res.p_id, nonce]) == 1
    assert not ticket_keys(r)


def test_dialog_kind_records_untouched_by_action_branch(wdc, tmp_path):
    """[基线] No security policy: P6b's kind=dialog two steps give the same record fields (no epoch, no
    oob_modes), states, receipts, and nothing under fx:guard:*, as with P6b alone. Depends on P6b (A2' red until
    P6b lands; never skipped)."""
    f = wdc
    assert security.policy(f.cfg) is None
    user_table(f, tmp_path)
    pid = mark_rm_with_channels(f)
    rec = record(f.r, pid)
    assert rec.get('kind', 'dialog') == 'dialog' and not {'epoch', 'oob_modes', 'run_id'} & set(rec)
    _, nonce, replies = phone(f)
    assert nonce and replies
    assert record(f.r, pid)['status'] in ('answered', 'done')
    assert not list(f.r.scan_iter('fx:guard:*'))


def test_nonce_and_ticket_never_in_audit_or_supervisor_inbox(r):
    audit = Audit()
    seed_agent(r)
    res = reg(r)
    replies, nonce = first_step(r, res.p_id)
    rec = record(r, res.p_id)
    approve.answer_action(r, rec, ev_for(rec, nonce), policy=POLICY, audit=audit)
    approve.consume_ticket(r, H, expect=expect(), call_epoch=0, audit=audit)
    assert nonce and sum(as_count(x, nonce) for x in replies) == 1                 # the only place it shows
    dump = redis_dump(r)
    assert nonce not in dump and nonce not in json.dumps(audit, default=str)
    nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
    assert nonce_hash not in json.dumps(audit, default=str)
    assert nonce_hash in json.dumps(record(r, res.p_id))                            # only in the pending record


def as_count(x, needle):
    text = envelope.card_text(x) if isinstance(x, dict) else str(x)
    buttons = ' '.join(b['command'] for b in x.get('buttons', [])) if isinstance(x, dict) else ''
    return 1 if needle in text or needle in buttons else 0


# ---------------------------------------------------------------------------------------------- M127 rulings
def test_register_action_queues_its_own_user_only_card_once(r):
    """M127 (a): the hook never reads pending records, so register_action queues the card itself: one user-only
    prompt_wait item carrying action_card for a new record, none for a reused one, none for a refusal."""
    seed_agent(r)
    res = reg(r)
    items = [json.loads(x) for x in r.lrange(channels.OUTQ, 0, -1)]
    assert len(items) == 1
    item, = items
    assert item['event'] == 'prompt_wait' and item['user_only'] is True and envelope.outbound_problem(item) == ''
    assert item['card'] == approve.action_card(record(r, res.p_id))
    assert reg(r).outcome == 'reused' and r.llen(channels.OUTQ) == 1
    bump(r)
    assert reg(r).reason == 'stale_call' and r.llen(channels.OUTQ) == 1


def test_register_action_accepts_untrusted_class_normal_tier_only(r):
    """M127 (b): cls 'untrusted' (an action forced to confirmation only by an untrusted request) besides the six
    classes; tier normal only; any other class is refused before Redis is touched."""
    seed_agent(r)
    res = reg(r, cls='untrusted')
    assert res.outcome == 'registered'
    rec = record(r, res.p_id)
    assert rec['class'] == 'untrusted' and rec['tier'] == 'normal' and rec['untrusted'] == '1'
    card = approve.action_card(rec)
    assert card['title'].startswith('确认') and envelope.RECEIPTS['untrusted_notice'] in envelope.card_text(card)
    before = redis_dump(r)
    for bad in ({'cls': 'untrusted', 'tier': 'high'}, {'cls': 'shell'}):
        with pytest.raises(ValueError):
            reg(r, action_text='other', **bad)
    assert redis_dump(r) == before


# ---------------------------------------------------------------------------------------------- M181
def test_route_root_default_verify_is_the_gate_module(monkeypatch):
    """M181: without ``verify=`` route_root uses ``cmux_fleet.guard.verify_ancestry`` (the gate module; there is no
    ``cmux_fleet.channels.guard``): the default path imports and calls it, and passes its refusal through."""
    from cmux_fleet import guard
    seen = []

    def fake_verify(cfg, *, cfg_path=None, connect=None, load=None):
        seen.append(cfg_path)
        if cfg_path == '/r/root.json':
            return guard.Ancestry('root', '', '', (), ('/r/root.json',), '/r/root.json', (POLICY,), None)
        return guard.Ancestry('refused', 'link_mismatch', '', (), (), '', (), None)
    monkeypatch.setattr(guard, 'verify_ancestry', fake_verify)
    cfg = {'run': '/tmp/run-m181', 'security': POLICY}
    local = approve.route_root(cfg, '/r/root.json')
    assert local.outcome == 'local' and local.reason == '' and local.route.oob_modes == 'desk'
    refused = approve.route_root(cfg, '/r/child.json')
    assert refused == approve.RouteResult('refused', None, 'link_mismatch')
    assert seen == ['/r/root.json', '/r/child.json']
