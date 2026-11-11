"""Delete dialogs approved from the phone (P6b records + two steps, P6c the watchdog's user exit).

brief v2 §6.14 + approve-1 test-plan §4.2 / S2 / S3 and the rulings R1-R14: pending records under
``channels.PENDING`` / ``PENDING_SLOT`` (``p-`` + 8 hex), step one without a nonce (``approve.handle_command``),
step two / reject through the ``dialog_user_answer`` event (``RuntimeV2.user_dialog_answer``), the press only on
the watchdog's next tick after the full fresh-screen check (``Watchdog.answer_user``), approve keys only from an
explicit registration: the ``watchdog_prompts.user_approve`` section of the run's explicit ``--prompt-table`` file
(default empty; never ``config/fleet.yaml``; ``YES_CURSOR`` is never used to infer Enter; no entry -> reject only),
never an automatic resume after the key (contracts §8 Q1 / Q2, reviewed 6b879bd), limits ``approve_confirm_s`` /
``approve_ttl_s`` configurable. The nonce never reaches Redis, audits, the
supervisor or exec.

Real private Redis + the `wd` fleet (fake cmux screens, ``FakeMgr.send_key`` records keys): no key reaches a
real pane, nothing is deleted, no network, no credentials (the config dir is a temp dir), no listener.
"""
import hashlib
import json
import os
import re
import time
import uuid

import pytest
import yaml

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn, dispatch_one, mixed  # noqa: F401  (fixtures / helpers)
from test_watchdog import (DIALOG_RM, DIALOG_RM_LITERAL, READ_FAR, WORKING, audits, escalations, keys, mark_rm,  # noqa: F401
                           move_transcript, rule, waiting, waits_for_the_user, wd)
from test_channels_contract import FakeChannel
from fleetmux import channels, fleet, fleet_config, runtime_v2, subfleet, watchdog
from fleetmux.channels import approve, envelope

PID_RE = r'p-[0-9a-f]{8}'
NONCE_IN = re.compile(r'/approve (p-[0-9a-f]{8}) ([0-9a-f]{16})')
FIELDS = {'id', 'slot', 'generation', 'session_id', 'scope', 'digest', 'command', 'tail', 'screen', 'paths',
          'keys_approve', 'pause_reason', 'created', 'confirm_until', 'expires', 'status', 'nonce_hash', 'sender',
          'conversation', 'decision'}
TAIL = 'Bash command\n  rm -rf build/tmp\n\nDangerous rm on possibly-empty variable\nDo you want to proceed?'
DIGEST = hashlib.sha256(TAIL.encode()).hexdigest()[:16]
NO_CURSOR = DIALOG_RM.replace('❯ 1. Yes\n  2. No', '  1. Yes\n❯ 2. No')   # a delete without a visible yes cursor
TABLE_KEYS = ['Tab', 'Enter']                                              # what the user_approve entry registers


# ---------------------------------------------------------------------------------------------- helpers
@pytest.fixture
def fake_account(tmp_path, monkeypatch):
    """The fake channel registered and its account file (ALLOW=u-1) in a temp config dir."""
    monkeypatch.setenv(channels.CONFIG_ENV, str(tmp_path / 'fleetmux-config'))
    path = channels.account_path('fake', 'contract-1')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('ALLOW=u-1\n', encoding='utf-8')
    path.chmod(0o600)
    undo = channels.register('fake', FakeChannel, 'fake')
    yield
    undo()


@pytest.fixture
def r(private_redis_v2, fake_account):  # noqa: F811
    private_redis_v2.flushdb()
    return private_redis_v2


@pytest.fixture
def wdc(wd, fake_account):  # noqa: F811
    """The `wd` fleet with one channel configured (cfg['channels'] as `start` pins it)."""
    f = wd
    f.cfg['channels'] = [{'type': 'fake', 'account': 'contract-1', 'push': list(channels.DEFAULT_PUSH)}]
    return f


def user_table(f, tmp_path, entries=None):
    """A prompt table whose user_approve section registers TABLE_KEYS for the dangerous-rm dialog."""
    data = fleet_config.block('watchdog_prompts')
    data['user_approve'] = [{'name': 'claude_dangerous_rm', 'match': ['dangerous rm'], 'keys': list(TABLE_KEYS)}] \
        if entries is None else entries
    path = tmp_path / 'prompts.yaml'
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')
    f.cfg['prompt_table'] = str(path)
    f.v2.watchdog = watchdog.Watchdog(f.v2)
    return path


def env(text, *, sender='u-1', conversation='c-1', private=True, verified=True):
    return envelope.make('fake', 'contract-1', conversation=conversation, message_id=uuid.uuid4().hex,
                         sender=sender, text=text, private=private, verified=verified,
                         reply_ref={'chat': conversation})


def as_text(x):
    return envelope.card_text(x) if isinstance(x, dict) else str(x)


def matches(text, which, **known):
    """``text`` is the fixed receipt ``which`` (envelope.RECEIPTS), whatever fills its {fields}."""
    pattern = re.escape(envelope.RECEIPTS[which])
    for name in re.findall(r'\\\{(\w+)\\\}', pattern):
        value = re.escape(str(known[name])) if name in known else '.*'
        pattern = pattern.replace('\\{' + name + '\\}', value)
    return re.fullmatch(pattern, text, re.S) is not None


def said(replies, which, **known):
    return any(matches(as_text(x), which, **known) for x in replies)


def nonce_of(replies):
    for x in replies:
        buttons = x.get('buttons', []) if isinstance(x, dict) else []
        for text in [b['command'] for b in buttons] + [as_text(x)]:
            m = NONCE_IN.search(text)
            if m:
                return m.group(2)
    return ''


def record(rr, pid):
    return rr.hgetall(channels.PENDING + pid)


def pending_id(f, slot='worker-1'):
    pid = f.r.get(channels.PENDING_SLOT + slot)
    assert pid and re.fullmatch(PID_RE, pid), pid
    return pid


def outq(f):
    return [json.loads(x) for x in f.r.lrange(channels.OUTQ, 0, -1)]


def replies_in_outq(f):
    return [i for i in outq(f) if i['event'] == 'reply']


def redis_dump(rr):
    """Every value in the run's Redis as one string (strings, hashes, lists, sets, zsets, streams)."""
    out = []
    for key in rr.scan_iter('*'):
        kind = rr.type(key)
        if kind == 'string':
            out.append(rr.get(key) or '')
        elif kind == 'hash':
            out.append(json.dumps(rr.hgetall(key), ensure_ascii=False))
        elif kind == 'list':
            out.append(json.dumps(rr.lrange(key, 0, -1), ensure_ascii=False))
        elif kind == 'set':
            out.append(json.dumps(sorted(rr.smembers(key)), ensure_ascii=False))
        elif kind == 'zset':
            out.append(json.dumps(rr.zrange(key, 0, -1), ensure_ascii=False))
        elif kind == 'stream':
            out.append(json.dumps(rr.xrange(key), ensure_ascii=False, default=str))
    return '\n'.join(out)


def register_one(rr, slot='worker-1', keys_approve=('Enter',), ttl_s=None, pause_reason='worker-1: dialog not answered'):
    kw = {} if ttl_s is None else {'ttl_s': ttl_s}
    return approve.register(rr, slot, {'generation': '0', 'thread': 'th-1', 'label': slot},
                            {'current_delivery': 'd-1'}, 'd-1', 'unknown:dangerous rm', TAIL, DIGEST,
                            '/run/watchdog/worker-1-prompt-unknown.txt', list(keys_approve), ['build/tmp'],
                            pause_reason, **kw)


def step_one(rr, pid, replies, *, post=None, **env_kw):
    calls = []
    post = post or (lambda ev: calls.append(ev) or {'status': 'accepted'})
    e = env('/approve ' + pid, **env_kw)
    approve.handle_command(rr, e, envelope.parse_command(e['text']), post=post, reply=replies.append)
    return calls


def phone(f, decision='approve', **env_kw):
    """The whole phone side for the slot's current pending record: step one + step two (or one reject), the
    second step posted straight to RuntimeV2.user_dialog_answer (the daemon's handler)."""
    pid = pending_id(f)
    replies = []
    post = f.v2.user_dialog_answer
    if decision == 'approve':
        step_one(f.r, pid, replies, post=post, **env_kw)
        nonce = nonce_of(replies)
        assert nonce, replies
        e = env('/approve ' + pid + ' ' + nonce, **env_kw)
    else:
        nonce = ''
        e = env('/reject ' + pid + ' 不要删', **env_kw)
    approve.handle_command(f.r, e, envelope.parse_command(e['text']), post=post, reply=replies.append)
    return pid, nonce, replies


def mark_rm_with_channels(f, screen=DIALOG_RM):
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = screen
    f.v2.watchdog.tick(time.time())
    waiting(f, 'worker-1')
    return pending_id(f)


def user_answered(f):
    return audits(f, 'dialog.user_answered')


# ====================================================================================== P6b: records + two steps
def test_register_writes_pending_hash_fields_ttl_and_slot_index(r):
    before = time.time()
    pid = register_one(r)
    assert re.fullmatch(PID_RE, pid)
    rec = record(r, pid)
    assert FIELDS <= set(rec), FIELDS - set(rec)
    assert rec['id'] == pid and rec['slot'] == 'worker-1' and rec['status'] == 'pending'
    assert rec['generation'] == '0' and rec['session_id'] == 'th-1' and rec['scope'] == 'd-1'
    assert rec['digest'] == DIGEST and 'rm -rf build/tmp' in rec['tail'] and len(rec['tail']) <= 2000
    assert json.loads(rec['keys_approve']) == ['Enter'] and json.loads(rec['paths']) == ['build/tmp']
    assert rec['pause_reason'] == 'worker-1: dialog not answered' and rec['nonce_hash'] == ''
    assert before <= float(rec['created']) <= time.time()
    assert float(rec['expires']) == pytest.approx(float(rec['created']) + channels.APPROVE_TTL_S, abs=1)
    assert channels.APPROVE_TTL_S - 10 < r.ttl(channels.PENDING + pid) <= channels.APPROVE_TTL_S
    assert r.get(channels.PENDING_SLOT + 'worker-1') == pid
    newer = register_one(r, ttl_s=600)                              # R7: the limit is configurable
    assert newer != pid and record(r, pid)['status'] == 'stale'
    assert r.get(channels.PENDING_SLOT + 'worker-1') == newer
    rec = record(r, newer)
    assert float(rec['expires']) == pytest.approx(float(rec['created']) + 600, abs=1)
    assert 0 < r.ttl(channels.PENDING + newer) <= 600


def test_card_has_two_buttons_when_key_known_only_reject_otherwise(r):
    pid = register_one(r)
    c = approve.card(record(r, pid))
    assert envelope.card_problem(c) == '' and c['ref'] == pid
    assert [b['command'] for b in c['buttons']] == ['/approve ' + pid, '/reject ' + pid]
    assert 'rm -rf build/tmp' in c['text'] and 'build/tmp' in c['text']
    bare = register_one(r, slot='worker-2', keys_approve=())
    c = approve.card(record(r, bare))
    assert envelope.card_problem(c) == '' and [b['command'] for b in c['buttons']] == ['/reject ' + bare]
    assert '没有登记的同意键，请在电脑上处理' in c['text']


@pytest.mark.parametrize('confirm_s', [None, 45])
def test_first_step_moves_to_confirming_with_nonce_hash_only(r, confirm_s):
    pid = register_one(r)
    replies, calls = [], []
    e = env('/approve ' + pid)
    t0 = time.time()
    kw = {} if confirm_s is None else {'confirm_s': confirm_s}
    approve.handle_command(r, e, envelope.parse_command(e['text']), post=lambda ev: calls.append(ev),
                           reply=replies.append, **kw)
    t1 = time.time()
    assert calls == []                                             # step one never reaches the daemon
    rec = record(r, pid)
    nonce = nonce_of(replies)
    assert re.fullmatch(r'[0-9a-f]{16}', nonce)
    assert rec['status'] == 'confirming' and rec['nonce_hash'] == hashlib.sha256(nonce.encode()).hexdigest()
    window = channels.APPROVE_CONFIRM_S if confirm_s is None else confirm_s
    assert t0 + window - 1 <= float(rec['confirm_until']) <= t1 + window + 1   # R7: from step one, not created
    assert rec['sender'] == 'u-1' and rec['conversation'] == 'c-1'
    assert nonce not in redis_dump(r)                              # only its hash is stored
    card, = [x for x in replies if isinstance(x, dict)]
    commands = [b['command'] for b in card['buttons']]
    assert commands == ['/approve ' + pid + ' ' + nonce, '/reject ' + pid]
    assert all(len(c.encode()) <= envelope.COMMAND_MAX_BYTES for c in commands)
    assert '确认删除' in as_text(card)


def test_first_step_on_missing_or_expired_record_replies_pending_gone(r):
    replies = []
    step_one(r, 'p-deadbeef', replies)
    assert said(replies, 'pending_gone', id='p-deadbeef')
    pid = register_one(r)
    r.hset(channels.PENDING + pid, 'expires', time.time() - 1)     # past the record's total limit
    before = record(r, pid)
    replies = []
    step_one(r, pid, replies)
    assert said(replies, 'pending_gone', id=pid) and record(r, pid) == before and not nonce_of(replies)
    r.hset(channels.PENDING + pid, mapping={'expires': time.time() + 600, 'status': 'done'})
    replies = []
    step_one(r, pid, replies)
    assert said(replies, 'pending_gone', id=pid) and record(r, pid)['status'] == 'done'


def test_second_step_posts_dialog_user_answer_event(r):
    pid = register_one(r)
    replies = []
    step_one(r, pid, replies)
    nonce = nonce_of(replies)
    calls = []
    e = env('/approve ' + pid + ' ' + nonce)
    approve.handle_command(r, e, envelope.parse_command(e['text']),
                           post=lambda ev: calls.append(ev) or {'status': 'accepted'}, reply=replies.append)
    ev, = calls
    assert ev == {'kind': 'dialog_user_answer', 'pending_id': pid, 'decision': 'approve', 'nonce': nonce,
                  'channel': 'fake', 'account': 'contract-1', 'sender': 'u-1', 'conversation': 'c-1'}
    assert said(replies, 'answer_taken')


def test_reject_posts_event_without_nonce_single_step(r):
    pid = register_one(r)
    replies, calls = [], []
    e = env('/reject ' + pid + ' 不要删')
    approve.handle_command(r, e, envelope.parse_command(e['text']),
                           post=lambda ev: calls.append(ev) or {'status': 'accepted'}, reply=replies.append)
    ev, = calls
    assert ev['kind'] == 'dialog_user_answer' and ev['pending_id'] == pid and ev['decision'] == 'reject'
    assert ev['nonce'] == '' and ev['sender'] == 'u-1' and ev['conversation'] == 'c-1'
    assert not nonce_of(replies) and said(replies, 'answer_taken')   # no confirmation card for a reject


def test_user_dialog_answer_accepts_only_matching_nonce_sender_conversation_within_time(wdc, tmp_path):
    f = wdc
    user_table(f, tmp_path)                                        # an explicit registration gives step one a nonce
    pid = mark_rm_with_channels(f)
    replies = []
    step_one(f.r, pid, replies, post=f.v2.user_dialog_answer)
    nonce = nonce_of(replies)
    key = channels.PENDING + pid
    good = {'kind': 'dialog_user_answer', 'pending_id': pid, 'decision': 'approve', 'nonce': nonce,
            'channel': 'fake', 'account': 'contract-1', 'sender': 'u-1', 'conversation': 'c-1'}
    for bad in (dict(good, nonce='0' * 16), dict(good, sender='u-9'), dict(good, conversation='c-9'),
                dict(good, pending_id='p-deadbeef')):
        before = record(f.r, pid)
        got = f.v2.user_dialog_answer(bad)
        assert got['status'] == 'stale' and got.get('reason') and record(f.r, pid) == before, bad
    saved = record(f.r, pid)
    for field, value in (('confirm_until', time.time() - 1),                 # the confirm window is over
                         ('expires', time.time() - 1),                        # R7: the total limit still binds
                         ('status', 'pending')):                              # not in the confirming state
        f.r.hset(key, field, value)
        before = record(f.r, pid)
        got = f.v2.user_dialog_answer(good)
        assert got['status'] == 'stale' and record(f.r, pid) == before, field
        f.r.hset(key, field, saved[field])
    assert f.v2.user_dialog_answer(good) == {'status': 'accepted'}
    rec = record(f.r, pid)
    assert rec['status'] == 'answered' and rec['decision'] == 'approve'
    assert f.v2.user_dialog_answer(good)['status'] == 'stale'                 # a nonce is used once


def test_user_dialog_answer_never_touches_fx_escalations_or_keys(wdc, tmp_path):
    f = wdc
    user_table(f, tmp_path)                                        # an explicit registration gives step one a nonce
    mark_rm_with_channels(f)
    before = f.r.get('fx:escalations')
    pid, _, replies = phone(f)
    assert said(replies, 'answer_taken') and record(f.r, pid)['status'] == 'answered'
    assert f.r.get('fx:escalations') == before and keys(f, 'worker-1') == []   # the press is the watchdog's


def test_handle_branch_routes_dialog_user_answer_to_runtime(wdc, monkeypatch):
    f = wdc
    seen = []
    monkeypatch.setattr(f.v2, 'user_dialog_answer', lambda ev: seen.append(ev) or {'status': 'accepted'})
    ev = {'kind': 'dialog_user_answer', 'request_id': 'dua-1', 'pending_id': 'p-0123abcd', 'decision': 'reject',
          'nonce': '', 'channel': 'fake', 'account': 'contract-1', 'sender': 'u-1', 'conversation': 'c-1'}
    f.handle(ev, _v2_internal=True)
    assert len(seen) == 1 and seen[0]['pending_id'] == 'p-0123abcd'
    assert json.loads(f.r.get('fx:reply:dua-1')) == {'status': 'accepted'}


def test_mark_done_then_late_step_replies_pending_gone(r):
    pid = register_one(r)
    replies = []
    step_one(r, pid, replies)
    nonce = nonce_of(replies)
    approve.mark_done(r, 'worker-1', 'activity')                   # the human answered at the desk
    assert record(r, pid)['status'] == 'done'
    late, calls = [], []
    for text in ('/approve ' + pid, '/approve ' + pid + ' ' + nonce):
        e = env(text)
        approve.handle_command(r, e, envelope.parse_command(text),
                               post=lambda ev: calls.append(ev) or {'status': 'stale', 'reason': 'done'},
                               reply=late.append)
    assert sum(1 for x in late if matches(as_text(x), 'pending_gone', id=pid)) == 2
    assert record(r, pid)['status'] == 'done'


def test_nonce_appears_only_in_user_reply_not_audit_or_control(wdc, tmp_path):
    f = wdc
    user_table(f, tmp_path)
    mark_rm_with_channels(f)
    pid, nonce, replies = phone(f)
    f.v2.watchdog.tick(time.time() + 1)                            # the press, its audit and its reply item
    assert nonce and any(nonce in json.dumps(x, ensure_ascii=False) for x in replies)
    dump = redis_dump(f.r)
    assert nonce not in dump                                       # audits, ui / notice cards, inboxes, outq
    assert all(nonce not in t for t in f.v2.pending_text('supervisor'))
    nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
    for row in audits(f):                                          # neither the nonce nor its hash in any audit
        assert nonce not in json.dumps(row, ensure_ascii=False) and nonce_hash not in json.dumps(row)
    assert all(nonce not in json.dumps(i, ensure_ascii=False) for i in outq(f))


# ====================================================================================== P6c: the watchdog's user exit
# Audit events after the dispatch for a delete dialog on a fleet without channels, taken on the frozen base
# 60ac389 (feat/channels) before P6c: two ticks, the second presses nothing and adds nothing.
BASELINE_EVENTS = ['agent.state', 'watch.prompt_unknown', 'execution.prompt_wait', 'service.paused']


def test_delete_dialog_without_channels_identical_to_baseline(wd):  # noqa: F811
    f = wd
    assert 'channels' not in f.cfg
    dispatch_one(f)
    n = len(audits(f))
    f.mgr.screens[f.agent('worker-1')['pane']] = DIALOG_RM
    now = time.time()
    f.v2.watchdog.tick(now)
    f.v2.watchdog.tick(now + 1)
    waits_for_the_user(f, 'worker-1', 'dangerous rm')
    assert keys(f, 'worker-1') == [] and list(f.r.scan_iter(channels.KEY_PREFIX + '*')) == []
    assert [a['event'] for a in audits(f)[n:]] == BASELINE_EVENTS


def test_delete_dialog_with_channels_registers_and_enqueues_user_only_card(wdc):
    f = wdc
    pid = mark_rm_with_channels(f)
    rec = record(f.r, pid)
    assert rec['status'] == 'pending' and rec['slot'] == 'worker-1' and rec['scope'] == f.agent('worker-1')['prompt_wait']
    assert json.loads(audits(f, 'execution.prompt_wait')[-1]['detail'])['dialog_class'] == 'delete'
    item, = outq(f)                                                # exactly one item: the user's card
    assert item['event'] == 'prompt_wait' and item['user_only'] is True and item['card']['ref'] == pid
    assert envelope.outbound_problem(item) == ''
    assert keys(f, 'worker-1') == [] and f.paused()
    assert rec['pause_reason'] == f.r.hget('fx:service', 'reason')


def test_reclassified_delete_from_supervisor_route_also_registers(wdc):
    f = wdc
    dispatch_one(f)
    pane = f.agent('worker-1')['pane']
    f.mgr.screens[pane] = READ_FAR
    f.v2.watchdog.tick(time.time())
    assert f.r.get(channels.PENDING_SLOT + 'worker-1') is None    # a non-delete dialog is the supervisor's
    rule(f, 'approve')
    f.mgr.screens[pane] = 'Bash command\n  rm -rf notes\n' + READ_FAR
    f.v2.watchdog.tick(time.time() + 1)
    f.v2.watchdog.tick(time.time() + 2)
    assert keys(f, 'worker-1') == [] and f.agent('worker-1')['prompt_route'] == 'user'
    pid = pending_id(f)
    cards = [i for i in outq(f) if i['event'] == 'prompt_wait']
    assert len(cards) == 1 and cards[0]['user_only'] and cards[0]['card']['ref'] == pid


def test_answered_approve_presses_user_approve_key_once_after_fresh_checks(wdc, tmp_path):
    f = wdc
    user_table(f, tmp_path)
    pid = mark_rm_with_channels(f)
    assert json.loads(record(f.r, pid)['keys_approve']) == TABLE_KEYS
    phone(f)
    assert keys(f, 'worker-1') == []                               # nothing before the watchdog's tick
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == TABLE_KEYS
    answered, = user_answered(f)
    assert answered['checked'] == 'ok' and answered['decision'] == 'approve' and answered['pending_id'] == pid
    assert f.agent('worker-1')['prompt_wait'] == '' and f.agent('worker-1')['watch_state'] == 'working'
    reply, = replies_in_outq(f)
    assert matches(reply['text'], 'answered')                      # never an automatic resume (§8 Q2)
    f.mgr.screens[f.agent('worker-1')['pane']] = WORKING
    f.v2.watchdog.tick(time.time() + 2)
    assert keys(f, 'worker-1') == TABLE_KEYS                       # pressed once, never again


def test_answered_reject_presses_escape_once(wdc, tmp_path):
    f = wdc
    user_table(f, tmp_path)
    pid = mark_rm_with_channels(f)
    phone(f, 'reject')
    f.v2.watchdog.tick(time.time() + 1)
    f.v2.watchdog.tick(time.time() + 2)
    assert keys(f, 'worker-1') == ['Escape']
    answered, = user_answered(f)
    assert answered['decision'] == 'reject' and answered['checked'] == 'ok' and record(f.r, pid)['status'] != 'answered'


@pytest.mark.parametrize('change', ['generation', 'thread', 'scope'])
def test_generation_thread_scope_change_blocks_press_and_replies_changed(wdc, tmp_path, change):
    f = wdc
    user_table(f, tmp_path)
    pid = mark_rm_with_channels(f)
    phone(f)
    if change == 'thread':
        f.setf('worker-1', thread='another-session')               # the slot's session changed
    elif change == 'generation':
        f.r.hset(channels.PENDING + pid, 'generation', '7')        # the record belongs to another generation
    else:
        f.r.hset(channels.PENDING + pid, 'scope', 'd-another-delivery')   # ... or to another delivery
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == []
    assert record(f.r, pid)['status'] == 'stale'
    assert any(matches(i['text'], 'changed') for i in replies_in_outq(f))


def test_screen_digest_change_blocks_press_and_reregisters_new_card(wdc, tmp_path):
    f = wdc
    user_table(f, tmp_path)
    pid = mark_rm_with_channels(f)
    phone(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = DIALOG_RM_LITERAL.replace('build/tmp', '../worker-2/out')
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == [] and record(f.r, pid)['status'] == 'stale'
    assert any(matches(i['text'], 'changed') for i in replies_in_outq(f))
    new = pending_id(f)
    assert new != pid and record(f.r, new)['status'] == 'pending'
    cards = [i for i in outq(f) if i['event'] == 'prompt_wait']
    assert [c['card']['ref'] for c in cards][0] == new and len(cards) == 2   # outq is LPUSH: newest first


def test_dialog_gone_marks_done_no_press(wdc, tmp_path):
    f = wdc
    user_table(f, tmp_path)
    pid = mark_rm_with_channels(f)
    phone(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = WORKING           # the dialog is no longer on the screen
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == [] and record(f.r, pid)['status'] == 'done'


@pytest.mark.parametrize('why', ['not_owned', 'adopted', 'wall'])
def test_pane_not_owned_adopted_or_wall_blocks_press(wdc, tmp_path, monkeypatch, why):
    f = wdc
    user_table(f, tmp_path)
    pid = mark_rm_with_channels(f)
    phone(f)
    if why == 'not_owned':
        f.mgr.owns = lambda pane: False
    elif why == 'adopted':
        monkeypatch.setattr(subfleet, 'adopted', lambda cfg, slot='supervisor', generation=None: True)
    else:
        monkeypatch.setattr(f, 'wall_gate', lambda name, screen, source='watch': {'wall': 'usage limit'})
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == []
    assert record(f.r, pid)['status'] in ('answered', 'stale')     # never pressed; a wall waits, the rest refuse
    assert user_answered(f) == [] or user_answered(f)[-1]['checked'] != 'ok'


def test_reclassified_to_other_hands_back_to_supervisor_route(wdc, tmp_path):
    f = wdc
    user_table(f, tmp_path)
    pid = mark_rm_with_channels(f)
    phone(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = READ_FAR          # the same slot now shows a non-delete dialog
    f.v2.watchdog.tick(time.time() + 1)
    f.v2.watchdog.tick(time.time() + 2)
    assert keys(f, 'worker-1') == [] and record(f.r, pid)['status'] == 'stale'
    assert 'dialog:worker-1:g' + f.agent('worker-1')['generation'] in escalations(f)
    assert f.agent('worker-1')['prompt_route'] == 'supervisor'


def test_supervisor_escalations_approve_on_delete_presses_nothing(wdc, tmp_path):
    f = wdc
    user_table(f, tmp_path)
    pid = mark_rm_with_channels(f)
    before = record(f.r, pid)
    rule(f, 'approve', key='dialog:worker-1:g' + f.agent('worker-1')['generation'])
    rule(f, 'approve', key=channels.PENDING + pid)
    for i in range(1, 4):
        f.v2.watchdog.tick(time.time() + i)
    assert keys(f, 'worker-1') == [] and record(f.r, pid) == before
    # O2: with the user_approve registration loaded, the program route and the supervisor route still press nothing
    for i in range(1, 6):
        f.v2.watchdog.tick(time.time() + 60 * i)
    assert keys(f, 'worker-1') == [] and f.agent('worker-1')['prompt_route'] == 'user'
    assert audits(f, 'watch.prompt_answered') == []
    with pytest.raises(ValueError):
        fleet.parse_service_decision(json.dumps({'status': 'RUNNING', 'completion': 1, 'reason': 'x', 'dispatch': [],
                                                 'escalations': [{'task': channels.PENDING + pid,
                                                                  'decision': 'approve'}]}), list(f.cfg['workers']))


def test_human_answered_in_pane_marks_done(wdc):
    f = wdc
    pid = mark_rm_with_channels(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = WORKING
    move_transcript(f, 'worker-1')                                  # the human answered at the desk
    f.v2.watchdog.tick(time.time() + 10)
    assert record(f.r, pid)['status'] == 'done' and keys(f, 'worker-1') == []
    replies = []
    step_one(f.r, pid, replies, post=f.v2.user_dialog_answer)
    assert said(replies, 'pending_gone', id=pid)


def test_user_approve_table_section_hot_loads_bad_entry_keeps_old(wdc, tmp_path):
    f = wdc
    path = user_table(f, tmp_path)
    pid = mark_rm_with_channels(f)
    assert json.loads(record(f.r, pid)['keys_approve']) == TABLE_KEYS
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    data['user_approve'] = [{'name': 'claude_dangerous_rm', 'match': ['dangerous rm'], 'keys': ['rm -rf /']}]
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))             # a new stamp: the cache reloads
    phone(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = DIALOG_RM_LITERAL   # changed screen: no press, a new record
    f.v2.watchdog.tick(time.time() + 1)
    assert audits(f, 'watch.table_rejected') and keys(f, 'worker-1') == []
    new = pending_id(f)
    assert new != pid and json.loads(record(f.r, new)['keys_approve']) == TABLE_KEYS   # the old table is kept


@pytest.mark.parametrize('screen', ['yes_cursor', 'no_cursor'])
def test_fallback_yes_cursor_enter_when_no_table_entry_else_no_key(wdc, screen):
    f = wdc                                                        # default table: no user_approve entry
    assert 'user_approve' not in (fleet_config.block('watchdog_prompts') or {})   # O1: the shipped table registers none
    pid = mark_rm_with_channels(f, DIALOG_RM if screen == 'yes_cursor' else NO_CURSOR)
    card = outq(f)[-1]['card']
    if screen == 'yes_cursor':                                     # '❯ 1. Yes' visible: never inferred (§8 Q1)
        assert json.loads(record(f.r, pid)['keys_approve']) == []
        assert [b['command'] for b in card['buttons']] == ['/reject ' + pid]
    assert json.loads(record(f.r, pid)['keys_approve']) == []      # no registered entry: no approve key
    assert [b['command'] for b in card['buttons']] == ['/reject ' + pid] and '没有登记的同意键，请在电脑上处理' in card['text']
    replies = []
    step_one(f.r, pid, replies, post=f.v2.user_dialog_answer)      # a typed /approve anyway
    assert said(replies, 'no_key', id=pid) and not nonce_of(replies) and record(f.r, pid)['status'] == 'pending'
    f.r.hset(channels.PENDING + pid, mapping={'status': 'answered', 'decision': 'approve'})   # even forged
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == []


def test_channel_user_resume_prompt_only_resumes_when_reason_matches(wdc, tmp_path):
    f = wdc                                                        # §8 Q2: the key never resumes dispatch
    user_table(f, tmp_path)
    pid = mark_rm_with_channels(f)
    assert record(f.r, pid)['pause_reason'] == f.r.hget('fx:service', 'reason')
    phone(f)
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == TABLE_KEYS and f.paused()
    assert audits(f, 'dialog.user_resumed') == []
    reply, = replies_in_outq(f)
    assert matches(reply['text'], 'answered') and '仍暂停' in reply['text']   # says what really happened


@pytest.mark.parametrize('source', ['after', 'before'])
def test_channel_user_resume_prompt_only_keeps_pause_when_other_source(wdc, tmp_path, source):
    f = wdc
    user_table(f, tmp_path)
    if source == 'before':
        dispatch_one(f)                                            # dispatched first: a paused fleet dispatches nothing
        f.pause('operator: maintenance')                           # already paused before the dialog
        f.mgr.screens[f.agent('worker-1')['pane']] = DIALOG_RM
        f.v2.watchdog.tick(time.time())
        waiting(f, 'worker-1')
        pid = pending_id(f)
    else:
        pid = mark_rm_with_channels(f)
    if source == 'after':
        f.pause('operator: maintenance')                           # another pause after it
    phone(f)
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == TABLE_KEYS and f.paused() and audits(f, 'dialog.user_resumed') == []
    reply, = replies_in_outq(f)
    assert matches(reply['text'], 'answered') and '仍暂停' in reply['text']
    assert record(f.r, pid)['status'] in ('answered', 'done')


def test_channel_user_resume_off_never_resumes_and_validate_config_rejects_bad_value(wdc, tmp_path):
    f = wdc
    user_table(f, tmp_path)
    mark_rm_with_channels(f)
    phone(f)
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == TABLE_KEYS and f.paused() and audits(f, 'dialog.user_resumed') == []
    reply, = replies_in_outq(f)
    assert matches(reply['text'], 'answered') and '仍暂停' in reply['text']
    base = {k: v for k, v in f.cfg.items() if k not in ('channel_user_resume', 'approve_confirm_s', 'approve_ttl_s')}
    for good in ({}, {'approve_confirm_s': 45, 'approve_ttl_s': 600}):
        runtime_v2.validate_config(dict(base, **good))
    for bad in ({'approve_confirm_s': 0}, {'approve_confirm_s': -1}, {'approve_confirm_s': 'x'},
                {'approve_ttl_s': float('inf')}, {'approve_ttl_s': True}):
        with pytest.raises(ValueError):
            runtime_v2.validate_config(dict(base, **bad))


def test_only_keys_in_KEYS_sent(wdc, tmp_path):
    f = wdc
    user_table(f, tmp_path)
    pid = mark_rm_with_channels(f)
    f.r.hset(channels.PENDING + pid, 'keys_approve', json.dumps(['Enter', 'rm -rf /', 'q']))   # a forged record
    phone(f)
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == ['Enter'] and all(k in watchdog.KEYS for k in keys(f, 'worker-1'))


def test_audit_user_answered_fields(wdc, tmp_path):
    f = wdc
    user_table(f, tmp_path)
    pid = mark_rm_with_channels(f)
    _, nonce, _ = phone(f)
    nonce_hash = record(f.r, pid)['nonce_hash']
    f.v2.watchdog.tick(time.time() + 1)
    row, = user_answered(f)
    for field in ('pending_id', 'slot', 'generation', 'decision', 'channel', 'account', 'sender', 'checked', 'reason'):
        assert field in row, field
    assert row['pending_id'] == pid and row['slot'] == 'worker-1' and row['channel'] == 'fake'
    assert row['account'] == 'contract-1' and row['sender'] == 'u-1' and row['checked'] == 'ok'
    text = json.dumps(row, ensure_ascii=False)
    assert nonce not in text and (not nonce_hash or nonce_hash not in text)


def test_program_and_supervisor_never_press_delete_regression(wd):  # noqa: F811
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = DIALOG_RM
    f.v2.watchdog.tick(time.time())
    waits_for_the_user(f, 'worker-1', 'dangerous rm')
    rule(f, 'approve', key='dialog:worker-1:g' + f.agent('worker-1')['generation'])
    for i in range(1, 6):
        f.v2.watchdog.tick(time.time() + 60 * i)
    assert keys(f, 'worker-1') == [] and f.agent('worker-1')['prompt_route'] == 'user'
    assert json.loads(audits(f, 'execution.prompt_wait')[-1]['detail'])['dialog_class'] == 'delete'
    assert audits(f, 'watch.prompt_answered') == []


# ====================================================================================== test-plan S2 / S3 (R3)
def test_handle_command_rechecks_whitelist_private_verified(r):
    pid = register_one(r)
    for kw in ({'private': False}, {'verified': False}, {'sender': 'u-9'}):
        before = record(r, pid)
        replies, calls = [], []
        step_one(r, pid, replies, post=lambda ev: calls.append(ev) or {'status': 'accepted'}, **kw)
        e = env('/reject ' + pid, **kw)
        approve.handle_command(r, e, envelope.parse_command(e['text']),
                               post=lambda ev: calls.append(ev) or {'status': 'accepted'}, reply=replies.append)
        assert calls == [] and record(r, pid) == before and not nonce_of(replies), kw
        assert all(matches(as_text(x), 'refused') for x in replies) and replies, kw


@pytest.mark.parametrize('ttl_s', [None, 600])
def test_answered_record_past_expires_presses_nothing(wdc, tmp_path, ttl_s):
    f = wdc
    if ttl_s is not None:
        f.cfg['approve_ttl_s'] = ttl_s
    user_table(f, tmp_path)
    pid = mark_rm_with_channels(f)
    rec = record(f.r, pid)
    assert float(rec['expires']) == pytest.approx(float(rec['created']) + (ttl_s or channels.APPROVE_TTL_S), abs=1)
    phone(f)
    f.r.hset(channels.PENDING + pid, 'expires', time.time() - 1)   # answered, then the total limit ran out
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == [] and record(f.r, pid)['status'] == 'expired'
    assert any(matches(i['text'], 'expired', id=pid) for i in replies_in_outq(f))
    assert user_answered(f)[-1]['checked'] == 'expired'


# ====================================================================================== P6b review fixes (M68)
def test_second_step_without_post_replies_unconfirmed(r):
    """brief v2 §2.1-5: ``post`` None (no daemon link) -> step two / reject reply ``unconfirmed``, record unchanged."""
    pid = register_one(r)
    replies = []
    step_one(r, pid, replies)
    nonce = nonce_of(replies)
    before = record(r, pid)
    for text in ('/approve ' + pid + ' ' + nonce, '/reject ' + pid):
        out = []
        e = env(text)
        approve.handle_command(r, e, envelope.parse_command(text), post=None, reply=out.append)
        assert len(out) == 1 and matches(as_text(out[0]), 'unconfirmed', id=pid), out
    assert record(r, pid) == before


def test_state_writes_never_recreate_an_expired_record(r):
    """Step one, mark_done and the stale mark only write while the record exists: a record gone between the read
    and the write is not recreated as a hash without a TTL."""
    pid = register_one(r)
    r.delete(channels.PENDING + pid)                                # the record's EX ran out
    assert approve.mark_done(r, 'worker-1', 'activity') == pid
    assert not r.exists(channels.PENDING + pid)
    newer = register_one(r)                                         # would mark the old (gone) one stale
    assert not r.exists(channels.PENDING + pid) and record(r, newer)['status'] == 'pending'
    assert approve._set_if(r, pid, approve.LIVE, {'status': 'confirming'}) is False
    assert not r.exists(channels.PENDING + pid)


# ====================================================================================== M173 (P8j hand-off)
def test_dialog_record_kind_and_card_title_by_class(r):
    """M173: every dialog record says kind=dialog; the phone card is titled by the dialog's class -- 删除确认
    only for a delete dialog, the caller's title or a neutral 操作确认 otherwise; step one of a non-delete dialog
    confirms with the neutral confirm_action text, never 确认删除."""
    pid = register_one(r)
    rec = record(r, pid)
    assert rec['kind'] == 'dialog' and rec['dialog_class'] == 'delete' and 'title' not in rec
    assert approve.card(rec)['title'] == '删除确认'
    kw = {'ttl_s': None}
    inst = approve.register(r, 'worker-2', {'generation': '0', 'thread': 'th-2'}, {}, 'd-2', 'unknown:pip install',
                            'Bash command\n  pip install requests\nDo you want to proceed?', 'ab' * 8, '/s', ['Enter'],
                            [], 'paused', dialog_class='install', title='安装确认', **kw)
    rec = record(r, inst)
    assert rec['kind'] == 'dialog' and rec['dialog_class'] == 'install' and approve.card(rec)['title'] == '安装确认'
    assert approve.card(dict(rec, title=''))['title'] == '操作确认'
    replies = []
    step_one(r, inst, replies)
    card, = [x for x in replies if isinstance(x, dict)]
    assert nonce_of(replies) and '确认删除' not in json.dumps(card, ensure_ascii=False)
    assert card['title'] == '安装确认' and matches(card['text'], 'confirm_action', action='pip install requests')
