"""Outbound secret scanning (package P8e; brief v2.2 §8.2 C2 / §8.4; sec-2 test-plan §1, survey §2-§3).

A2' red tests (brief §8.3): ``channels/secrets.py`` is a bare stub (construction works, bodies raise), so every test
here FAILS until P8e lands, except the baseline ``test_no_policy_guard_is_identity`` (no ``security.yaml`` -> the
item itself, nothing read, nothing written).

Credential shapes are assembled at run time (S4): the source of this file never carries a full credential shape.
Fakes only: the repo's private unix-socket Redis, the v2 ``fx`` fleet, the P0 ``FakeChannel`` and ``Audit``,
injected ``env`` / ``read_file``. No network, no real credential, no listener.

Builtin rule names pinned here: aws, github, slack, telegram, openai, anthropic, google, jwt, pem, bearer, generic;
exact values: ``value:<account>`` (accounts), ``value:...`` (env / file / glob), ``canary`` (P8h).
"""
import base64
import binascii
import json
import urllib.parse
from pathlib import Path

import pytest
import yaml

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from test_runtime_v2 import fx  # noqa: F401  (fixture: a v2 service fleet on that Redis)
from test_channels_contract import (Audit, FakeChannel, _fake_transport, config_dir,  # noqa: F401
                                    fake_registered)
from fleetmux import channels
from fleetmux.channels import base, envelope, secrets, security

TOK = 'fake-token-ABC123xyz'


def make_policy(text=''):
    policy, errors = security.parse_security(text)
    assert errors == [], errors
    return policy


def policy_with(**sections):
    return make_policy(yaml.safe_dump(sections))


def variants_of(value):
    raw = value.encode()
    return {
        'base64': base64.b64encode(raw).decode().rstrip('='),
        'urlsafe': base64.urlsafe_b64encode(raw).decode().rstrip('='),
        'hex': binascii.hexlify(raw).decode(),
        'url': urllib.parse.quote(value, safe=''),
    }


def shapes():
    """One runtime-built sample per builtin rule (S4: never spelled out in this source)."""
    return {
        'aws': 'AK' + 'IA' + 'Q' * 16,
        'github': 'gh' + 'p_' + 'x' * 36,
        'slack': 'xo' + 'xb-' + '1' * 11 + '-' + '2' * 12 + '-' + 'a' * 24,
        'telegram': '1' * 9 + ':' + 'A' * 35,
        'openai': 's' + 'k-' + 'proj-' + 'B' * 40,
        'anthropic': 's' + 'k-' + 'ant-' + 'api03-' + 'C' * 40,
        'google': 'AI' + 'za' + 'D' * 35,
        'jwt': 'ey' + 'J' + 'h' * 20 + '.' + 'ey' + 'J' + 'p' * 20 + '.' + 's' * 20,
        'pem': '-----BEGIN ' + 'RSA PRIVATE' + ' KEY-----',
        'bearer': 'Author' + 'ization: ' + 'Bearer ' + 'E' * 24,
        'generic': 'api' + '_key = ' + 'F' * 16,
    }


def write_account(config, name='bot1', extra=''):
    d = config / 'channels'
    d.mkdir(parents=True, exist_ok=True)
    path = d / ('telegram-' + name + '.env')
    path.write_text('BOT_TOKEN=' + TOK + '\nALLOW=111\n' + extra, encoding='utf-8')
    path.chmod(0o600)
    return path


def scanner_for(policy, config, **kw):
    write_account(config)
    return secrets.Scanner(policy, **kw)


def leaked(blob, value=TOK):
    blob = blob if isinstance(blob, str) else json.dumps(blob, ensure_ascii=False, default=str)
    return value in blob or variants_of(value)['base64'] in blob


# ====================================================================== 1
def test_builtin_rules_hit_known_shapes():
    policy = policy_with(secrets={'values_from': []})
    s = secrets.Scanner(policy)
    for rule, sample in shapes().items():
        hits = s.scan('please keep this safe: ' + sample + ' thanks')
        assert [h['rule'] for h in hits] == [rule], (rule, hits)
    negatives = ['commit ' + 'a1b2c3d4' * 5, 'uuid 2f1c6b9e-4a7d-4c3e-9b1a-0e8f7d6c5b4a', 'version 2.4.10',
                 'key fx:channel:pending:p-0123abcd', 'ticket fx:guard:ticket:' + '0123456789abcdef' * 2]
    for text in negatives:
        assert s.scan(text) == [], text
    assert len(secrets.RULES) == 11


# ====================================================================== 2
def test_exact_values_from_accounts_env_file_glob_loaded_and_variants_hit(config_dir, tmp_path):
    not_secret = ('ADDRESS=me@example.test\nSMTP_HOST=smtp.example.test:465\nBASE_URL=https://acct.fake\n'
                  'USER_ID=u-1234567\nBOT_ID=b-7654321\nAPP_ID=cli_fake_app\nPUSH_TO=5550001\n'
                  'COMMAND=/bin/relay --x\nTRUSTED_AUTHSERV=mx.example.test\nSHORT_TOKEN=abc12\n')
    write_account(config_dir, extra=not_secret)
    one = tmp_path / 'f.env'
    one.write_text('K=fake-file-value-888\n', encoding='utf-8')
    (tmp_path / 'g').mkdir()
    (tmp_path / 'g' / 'a.env').write_text('A=fake-glob-value-one\n', encoding='utf-8')
    (tmp_path / 'g' / 'b.env').write_text('B=fake-glob-value-two\n', encoding='utf-8')
    env = {'X_KEY': 'fake-env-value-777'}
    reads = []

    def read_file(path):
        reads.append(str(path))
        return open(path, encoding='utf-8').read()
    values_from = ['accounts', 'env:X_KEY', 'file:' + str(one), 'glob:' + str(tmp_path / 'g' / '*.env')]
    for variants, encoded_hit in ((['base64', 'hex', 'url'], {'base64', 'urlsafe', 'hex', 'url'}),
                                  (['base64'], {'base64', 'urlsafe'})):
        policy = policy_with(secrets={'values_from': values_from, 'variants': variants})
        s = secrets.Scanner(policy, env=env, read_file=read_file)
        assert str(one) in reads
        hits = s.scan('done ' + TOK)
        assert [h['rule'] for h in hits] == ['value:bot1']
        # (a) mandatory normalisation (F5): never configurable
        assert s.scan('done ' + TOK.replace('-', '')), variants
        assert s.scan('done fake-token- ABC 123xyz'), variants
        # (b) configurable encodings
        for name, enc in variants_of(TOK).items():
            assert bool(s.scan('blob ' + enc + ' end')) == (name in encoded_hit or enc == TOK), (variants, name)
        for value in ('fake-env-value-777', 'fake-file-value-888', 'fake-glob-value-one', 'fake-glob-value-two'):
            got = s.scan('x ' + value + ' y')
            assert got and got[0]['rule'].startswith('value:'), value
    # S7: non-credential account keys are not in the value set, nor values under 6 characters
    for line in not_secret.splitlines():
        value = line.split('=', 1)[1]
        assert s.scan('cfg ' + value + ' end') == [], line
        assert s.scan('cfg ' + base64.b64encode(value.encode()).decode().rstrip('=') + ' end') == [], line


# ====================================================================== 3
def test_guard_blocks_whole_item_and_sends_fixed_notice(config_dir, private_redis_v2):
    from fleetmux.channels import queue
    r = private_redis_v2
    r.flushdb()
    policy = make_policy()
    s = scanner_for(policy, config_dir)
    item = envelope.outbound('result', target='origin', request_id='r1', text='done ' + TOK)
    got, audit = [], Audit()
    assert secrets.guard(item, policy, scanner=s, notify=got.append, audit=audit) is None
    assert len(got) == 1
    notice = got[0]
    assert notice['text'] == envelope.receipt('secret_blocked', rule='value:bot1', id=item['id'])
    for key in ('event', 'target', 'request_id', 'channel', 'account', 'conversation', 'reply_ref', 'user_only'):
        assert notice[key] == item[key], key
    assert notice['id'] != item['id']
    assert set(notice) <= envelope.OUT_KEYS and envelope.outbound_problem(notice) == ''
    # anti-recursion by content (S9): stable across processes, no shared state
    assert secrets.is_notice(notice) is True and secrets.is_notice(item) is False
    again = []
    assert secrets.guard(notice, policy, scanner=s, notify=again.append, audit=audit) is notice
    assert again == []

    class HitsEverything:
        def scan(self, text):
            return [{'rule': 'value:bot1', 'span': (0, 1), 'redacted': '***'}]
    other, audit2 = [], Audit()
    assert secrets.guard(notice, policy, scanner=HitsEverything(), notify=other.append, audit=audit2) is None
    assert other == [] and audit2.events() == ['security.secret_blocked']
    # the notice dead-letters alone; the original never reaches a queue
    r.lpush(channels.OUTQ, envelope.dumps(notice))
    sends = []

    def send(it):
        sends.append(it['id'])
        raise OSError('fake send down')
    for _ in range(3):
        queue.process_one(r, send, sleep=lambda s: None, block=0)
    dead = r.lrange(channels.DEAD, 0, -1)
    assert dead and all(notice['id'] in d for d in dead)
    assert not leaked(json.dumps([notice] + dead + [list(a) for a in audit], ensure_ascii=False, default=str))


# ====================================================================== 4
def test_audit_secret_blocked_has_rule_item_event_no_match_text(config_dir):
    policy = make_policy()
    s = scanner_for(policy, config_dir)
    item = envelope.outbound('result', target='origin', request_id='r1', text='done ' + TOK)
    audit = Audit()
    assert secrets.guard(item, policy, scanner=s, notify=lambda n: None, audit=audit) is None
    rows = [f for e, f in audit if e == 'security.secret_blocked']
    assert len(rows) == 1
    fields = rows[0]
    assert {'rule', 'item_id', 'kind'} <= set(fields)
    assert fields['rule'] == 'value:bot1' and fields['item_id'] == item['id'] and fields['kind'] == 'result'
    assert not {'span', 'text', 'match'} & set(fields)
    blob = json.dumps(fields, ensure_ascii=False, default=str)
    assert TOK not in blob and all(v not in blob for v in variants_of(TOK).values())


# ====================================================================== 5
def test_scanner_values_never_in_repr_or_exception(config_dir):
    policy = policy_with(secrets={'values_from': ['accounts', 'env:X_KEY', 'file:/f']})
    write_account(config_dir)
    s = secrets.Scanner(policy, env={'X_KEY': 'fake-env-value-777'}, read_file=lambda p: 'K=fake-file-value-888\n')
    for text in (repr(s), str(s)):
        assert 'Scanner(' in text and 'rules=11' in text
        for value in (TOK, 'fake-env-value-777', 'fake-file-value-888'):
            assert value not in text
    hit = s.scan('x ' + TOK)
    assert hit and hit[0]['redacted'] == '***'

    def unreadable(path):
        raise OSError('fake-file-value-888 unreadable')
    errors = []
    try:
        bad = secrets.Scanner(policy, env={}, read_file=unreadable)
        errors.append(repr(bad))
        errors.append(str(getattr(bad, 'errors', '')))
    except Exception as exc:  # noqa: BLE001 - whichever way it reports, the value must not be in it
        errors.append(repr(exc) + str(exc))
    assert errors and all('fake-file-value-888' not in e for e in errors)


# ====================================================================== 6
def test_outbound_loop_send_direct_and_process_one_all_pass_through_guard(fx, fake_registered, config_dir):
    from fleetmux.channels import actor, queue
    f, r = fx, fx.r
    run = Path(f.cfg['run'])
    write_account(config_dir)
    acct_path = config_dir / 'channels' / 'fake-contract-1.env'
    acct_path.write_text('ALLOW=u-1\nTOKEN=' + TOK + '\n', encoding='utf-8')
    f.cfg['channels'] = [{'type': 'fake', 'account': 'contract-1'}]
    f.cfg['security'] = make_policy()
    transport = _fake_transport()
    ch = FakeChannel(channels.parse_account_text('fake', 'contract-1', acct_path.read_text()), transport=transport,
                     store=base.MemoryStore(), audit=Audit(), inbox_dir=run / 'inbox' / 'files')
    chans = {('fake', 'contract-1'): ch}
    target = dict(target='explicit', channel='fake', account='contract-1', conversation='c-1')
    originals = [envelope.outbound('result', text='one ' + TOK, **target) for _ in range(4)]
    # ① outbound_loop
    r.lpush(channels.OUTQ, envelope.dumps(originals[0]))
    stops = iter([False, True])
    actor.outbound_loop(r, chans, stop=lambda: next(stops, True), policy=f.cfg['security'])
    # ② send_direct
    got = actor.send_direct(f.cfg, originals[1], make_channel=lambda *a, **k: ch)
    assert got['status'] == 'failed' and got['reason'] == 'secret_blocked'
    # ③ process_one with the actor's guard, before throttle
    throttle_before = sorted(r.scan_iter('fx:channel:throttle:*'))
    r.lpush(channels.OUTQ_ACCOUNT + 'fake:contract-1', envelope.dumps(originals[2]))
    sends = []
    scanner = secrets.Scanner(f.cfg['security'])
    res = queue.process_one(r, lambda it: sends.append(it['id']), key=channels.OUTQ_ACCOUNT + 'fake:contract-1',
                            block=0, guard=lambda it: secrets.guard(it, f.cfg['security'], scanner=scanner,
                                                                    notify=lambda n: None, audit=Audit()))
    assert res == {'status': 'blocked', 'id': originals[2]['id']} and sends == []
    assert sorted(r.scan_iter('fx:channel:throttle:*')) == throttle_before
    # ④ drain shares the same path
    r.lpush(channels.OUTQ, envelope.dumps(originals[3]))
    actor.drain(r, chans, deadline=10 ** 12, policy=f.cfg['security'])
    texts = [d.get('text', '') for op, d in transport.sent if op == 'send_text']
    joined = ''.join(texts)  # FakeChannel TEXT_LIMIT = 50: one notice arrives as several send_text chunks
    notes = ['Fleet：' + x for x in joined.split('Fleet：')[1:]]
    assert texts and joined.startswith('Fleet：') and all(n.startswith('Fleet：一条出站消息含疑似密钥') for n in notes)
    audit_rows = [row for _, row in r.xrange(f.v2.bus.audit_key)]
    for it in originals:
        assert not [a for a in audit_rows if a.get('id') == it['id']
                    and a.get('event') in ('channel.out', 'channel.send_failed', 'channel.dead')]
    stores = [json.dumps(audit_rows, ensure_ascii=False), json.dumps(r.lrange(channels.DEAD, 0, -1)),
              json.dumps(r.lrange(channels.OUTQ, 0, -1))]
    notes = run / 'notifications.jsonl'
    if notes.exists():
        stores.append(notes.read_text())
    assert not any(leaked(s) for s in stores)


# ====================================================================== 7
def test_card_text_and_file_caption_scanned_too(config_dir, tmp_path):
    policy = make_policy()
    s = scanner_for(policy, config_dir)

    def g(item, **kw):
        return secrets.guard(item, policy, scanner=s, notify=lambda n: None, audit=Audit(), **kw)
    card = envelope.card('t', 'k=' + TOK, [{'label': 'ok', 'command': '/approve p-0123abcd'}])
    assert g(envelope.outbound('result', card=card)) is None
    assert g(envelope.outbound('result', file=str(tmp_path / 'x.bin'), caption=TOK)) is None
    clean = envelope.outbound('result', card=envelope.card('t', 'ok', [
        {'label': 'ok', 'command': '/approve p-0123abcd 0123456789abcdef'}]))
    assert g(clean) is clean
    ref = envelope.outbound('result', text='all good', reply_ref={'context_token': TOK})
    assert g(ref) is ref
    # S8: auto-generated outbox attachments are scanned; failures to scan never let it through
    outbox = tmp_path / 'outbox'
    outbox.mkdir()
    (outbox / 'r1.txt').write_text('long reply ' + TOK, encoding='utf-8')
    assert g(envelope.outbound('result', file=str(outbox / 'r1.txt'), caption='reply')) is None
    (outbox / 'bin.txt').write_bytes(b'\xff\xfe\x00bad')
    assert g(envelope.outbound('result', file=str(outbox / 'bin.txt'), caption='reply')) is None
    (outbox / 'ok.txt').write_text('nothing here', encoding='utf-8')
    fine = envelope.outbound('result', file=str(outbox / 'ok.txt'), caption='reply')
    assert g(fine) is fine
    # a user-named file outside outbox: content not scanned (survey §5.1), only the caption
    other = tmp_path / 'x'
    other.mkdir()
    (other / 'report.txt').write_text(TOK, encoding='utf-8')
    named = envelope.outbound('result', file=str(other / 'report.txt'), caption='report')
    assert g(named) is named


# ====================================================================== 8 [baseline]
def test_no_policy_guard_is_identity(private_redis_v2, monkeypatch, tmp_path):
    r = private_redis_v2
    monkeypatch.setenv(channels.CONFIG_ENV, str(tmp_path / 'does-not-exist'))

    def never(*a, **k):
        pytest.fail('no policy must never touch the config dir or an account file')
    monkeypatch.setattr(channels, 'config_dir', never)
    monkeypatch.setattr(channels, 'load_account', never)
    before = sorted(r.keys('*'))
    item = envelope.outbound('result', target='origin', request_id='r1', text='done ' + TOK)
    calls, audit = [], Audit()
    assert secrets.guard(item, None, scanner=None, notify=calls.append, audit=audit) is item
    assert calls == [] and audit == []
    assert sorted(r.keys('*')) == before
