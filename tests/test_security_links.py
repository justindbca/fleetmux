"""Outbound link rewriting and preview off (package P8f; brief v2.2 §8.2 C3 / §8.4; sec-2 test-plan §2, survey §6.1).

A2' red tests (brief §8.3): ``channels/links.py`` is a bare stub, so every test FAILS until P8f lands, except the
baseline ``test_allow_star_disables_rewrite`` (``links.allow: ['*']`` or no policy -> ``(text, [])``).

Fakes only: the P0 ``Audit``, the tg-1 ``FakeBotAPI`` (an in-process callable, no network), the private Redis.
The Telegram adapter is built inside the test body, so a stub adapter makes the test FAIL, never ERROR.
"""
from pathlib import Path

import yaml

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from test_runtime_v2 import fx  # noqa: F401  (fixture: a v2 service fleet on that Redis)
from test_channels_contract import Audit, FakeChannel, _fake_transport, config_dir, fake_registered  # noqa: F401
from cmux_fleet import channels
from cmux_fleet.channels import base, envelope, links, security

TOK = 'fake-token-ABC123xyz'


def make_policy(text=''):
    policy, errors = security.parse_security(text)
    assert errors == [], errors
    return policy


def policy_with(**sections):
    return make_policy(yaml.safe_dump(sections))


# ====================================================================== 1
def test_defang_non_allowlisted_urls_bare_domains_and_www():
    for policy in (make_policy(), policy_with(links={'allow': [], 'mode': 'defang'})):
        out, hits = links.rewrite('see https://evil.example/x?d=1 now', policy)
        assert out == 'see evil[.]example/x?d=1 now' and hits == ['evil.example']
        out, hits = links.rewrite('docs at example.com/p and example.com, also example.com.', policy)
        assert out == 'docs at example[.]com/p and example[.]com, also example[.]com.'
        assert hits == ['example.com', 'example.com', 'example.com']
        out, hits = links.rewrite('go www.a.b', policy)
        assert out == 'go www[.]a[.]b' and hits == ['www.a.b']
        for keep in ('cmux_fleet/channels/actor.py', 'docs/design.md', 'see ./notes.md', 'version 2.4.10',
                     'brief v2.2', 'up 0.8%'):
            assert links.rewrite(keep, policy) == (keep, []), keep
        text = 'a https://evil.example/x b example.com c'
        once, _ = links.rewrite(text, policy)
        assert links.rewrite(once, policy) == (once, [])


# ====================================================================== 2
def test_allowlist_exact_and_wildcard_subdomain_pass():
    policy = policy_with(links={'allow': ['github.com', '*.example.org']})
    for keep in ('https://github.com/a', 'github.com', 'https://docs.example.org/b', 'a.b.example.org/c',
                 'https://GitHub.COM/a'):
        assert links.rewrite('x ' + keep + ' y', policy) == ('x ' + keep + ' y', []), keep
    for change in ('https://gist.github.com/x', 'https://example.org/', 'example.org',
                   'https://github.com.evil.example/', 'notgithub.com/x', 'example.org.evil.example'):
        out, hits = links.rewrite('x ' + change + ' y', policy)
        assert out != 'x ' + change + ' y' and hits, change
        assert links.rewrite(out, policy) == (out, []), change


# ====================================================================== 3 [baseline]
def test_allow_star_disables_rewrite():
    policy = policy_with(links={'allow': ['*']})
    for text in ('see https://evil.example/x?d=1', 'bare example.com and www.a.b', '![i](https://evil.example/i.png)'):
        out, hits = links.rewrite(text, policy)
        assert out == text and hits == []
        assert links.rewrite(text, None) == (text, [])
    item = envelope.outbound('result', text='see https://evil.example/x')
    assert links.rewrite_item(item, policy) is item
    assert links.rewrite_item(item, None) is item


# ====================================================================== 4
def test_strip_mode_replaces_with_marker():
    policy = policy_with(links={'mode': 'strip'})
    out, hits = links.rewrite('see https://evil.example/x now', policy)
    assert out == 'see [链接已移除：evil[.]example] now' and hits == ['evil.example']
    assert links.rewrite(out, policy) == (out, [])


# ====================================================================== 5
def test_markdown_images_removed_in_both_modes():
    image = '![a](https://evil.example/i.png)'
    for policy in (make_policy(), policy_with(links={'mode': 'strip'}),
                   policy_with(links={'allow': ['evil.example']})):
        out, _ = links.rewrite('before ' + image + ' after', policy)
        assert 'i.png' not in out and '![' not in out and out.startswith('before ') and out.endswith(' after')
    out, _ = links.rewrite('[t](https://evil.example)', make_policy())
    assert out == '[t](evil[.]example)'


# ====================================================================== 6
def test_card_text_rewritten_buttons_untouched(tmp_path):
    policy = make_policy()
    buttons = [{'label': 'ok', 'command': '/approve p-0123abcd 0123456789abcdef'}]
    card = envelope.card('see evil.example', 'body https://evil.example/x', buttons)
    item = envelope.outbound('result', card=card, reply_ref={'url': 'https://evil.example/ref'})
    audit = Audit()
    out = links.rewrite_item(item, policy, audit=audit)
    assert out['card']['title'] == 'see evil[.]example'
    assert out['card']['text'] == 'body evil[.]example/x'
    assert out['card']['buttons'] == buttons and out['reply_ref'] == item['reply_ref']
    path = str(tmp_path / 'evil.example.txt')
    f = envelope.outbound('result', file=path, caption='at https://evil.example/c')
    got = links.rewrite_item(f, policy, audit=audit)
    assert got['file'] == path and got['caption'] == 'at evil[.]example/c'
    rows = [fields for e, fields in audit if e == 'security.link_rewritten']
    assert rows
    for fields in rows:
        blob = repr(fields)
        assert 'evil.example' in blob and '/x' not in blob and '/c' not in blob


# ====================================================================== 7
def test_telegram_send_and_edit_disable_link_preview_when_policy_off(tmp_path, monkeypatch):
    from test_channel_telegram import USER, FakeBotAPI, as_list, make  # noqa: F401
    from cmux_fleet.channels import actor
    assert base.Channel.link_preview is None
    card = envelope.card('t', 'see https://github.com/x')
    for preview, expect in ((False, True), (None, False), (True, False)):
        api = FakeBotAPI()
        ch = make(api)
        ch.link_preview = preview
        assert ch.send_text(USER, 'see https://github.com/x').ok
        sent = ch.send_card(USER, card)
        assert ch.update_card(USER, sent.message_id, card).ok
        for name in ('sendMessage', 'editMessageText'):
            params = api.named(name)[-1]['params']
            got = params.get('link_preview_options')
            if expect:
                assert as_list(got) == {'is_disabled': True}, (preview, name)
            else:
                assert got is None, (preview, name)
    built = []

    def make_channel(*args, **kw):
        ch = make(FakeBotAPI())
        built.append(ch)
        return ch
    item = envelope.outbound('result', target='explicit', channel='telegram', account='bot1', conversation=USER,
                             text='hello')
    actor.send_direct({'run': str(tmp_path), 'security': make_policy()}, item, make_channel=make_channel)
    actor.send_direct({'run': str(tmp_path)}, item, make_channel=make_channel)
    assert [c.link_preview for c in built] == [False, None]


# ====================================================================== 8
def test_guard_chain_order_secrets_then_links(fx, fake_registered, config_dir):
    from cmux_fleet.channels import actor
    f, r = fx, fx.r
    d = config_dir / 'channels'
    d.mkdir(parents=True, exist_ok=True)
    (d / 'fake-contract-1.env').write_text('ALLOW=u-1\nTOKEN=' + TOK + '\n', encoding='utf-8')
    f.cfg['channels'] = [{'type': 'fake', 'account': 'contract-1'}]
    f.cfg['security'] = make_policy()
    transport = _fake_transport()
    account = channels.parse_account_text('fake', 'contract-1', (d / 'fake-contract-1.env').read_text())
    ch = FakeChannel(account, transport=transport, store=base.MemoryStore(), audit=Audit(),
                     inbox_dir=Path(f.cfg['run']) / 'inbox' / 'files')
    item = envelope.outbound('result', target='explicit', channel='fake', account='contract-1', conversation='c-1',
                             text='see https://evil.example/?k=' + TOK)
    r.lpush(channels.OUTQ, envelope.dumps(item))
    stops = iter([False, True])
    actor.outbound_loop(r, {('fake', 'contract-1'): ch}, stop=lambda: next(stops, True), policy=f.cfg['security'])
    texts = [data.get('text', '') for op, data in transport.sent if op == 'send_text']
    joined = ''.join(texts)  # FakeChannel TEXT_LIMIT = 50: the one notice arrives as several send_text chunks
    assert joined.count('Fleet：') == 1 and joined.startswith('Fleet：一条出站消息含疑似密钥')
    assert not any('evil[.]example' in t or '***' in t for t in texts)
    events = [row.get('event') for _, row in r.xrange(f.v2.bus.audit_key)]
    assert events.count('security.secret_blocked') == 1
    assert events.count('security.link_rewritten') == 0
