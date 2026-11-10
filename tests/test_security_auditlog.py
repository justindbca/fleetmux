"""Append-only audit chain (package P8g; brief v2.2 §8.2 E1 / §8.4; sec-2 test-plan §3, survey §9 / §10.1).

A2' red tests (brief §8.3): ``channels/auditlog.py`` is a bare stub, so every test FAILS until P8g lands, except the
baseline ``test_audit_log_disabled_writes_nothing`` (no policy / ``audit_log.enabled: off`` -> nothing written).

``uappend`` is on by default (supervisor ruling 10): only ``chain-*.jsonl`` after creating it, never ``.writer.lock``;
unsupported / failing -> exactly one ``security.audit_chain reason=uappend_unsupported`` per Chain instance. S10: the
real ``os.chflags`` is never called here, every test injects a fake. The chain is tamper-evident, not tamper-proof:
a rewrite of the whole chain or a truncated tail is not claimed to be detected.

Fakes only: tmp dirs, injected clocks, the repo's private unix-socket Redis, the v2 ``fx`` fleet.
"""
import errno
import hashlib
import io
import json
import os
import stat
import time
import types
from pathlib import Path

import pytest
import yaml

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from test_runtime_v2 import fx  # noqa: F401  (fixture: a v2 service fleet on that Redis)
from test_channels_contract import Audit, config_dir  # noqa: F401
from cmux_fleet import channels, fleet
from cmux_fleet.channels import auditlog, secrets, security

TOK = 'fake-token-ABC123xyz'
DAY1 = time.mktime((2026, 11, 9, 12, 0, 0, 0, 0, -1))
DAY2 = DAY1 + 86400


def make_policy(text=''):
    policy, errors = security.parse_security(text)
    assert errors == [], errors
    return policy


def policy_with(**sections):
    return make_policy(yaml.safe_dump(sections))


class FakeChflags:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def __call__(self, path, flags):
        self.calls.append((str(path), flags))
        if self.error:
            raise self.error


class Clock:
    def __init__(self, t=DAY1):
        self.t = t

    def __call__(self):
        return self.t


def lines(path):
    return [json.loads(x) for x in Path(path).read_text(encoding='utf-8').splitlines() if x.strip()]


def chain_files(d):
    return sorted(Path(d).glob('chain-*.jsonl'))


def write_n(chain, n, start=0):
    for i in range(n):
        chain.append({'event': 'channel.in', 'n': start + i})


# ====================================================================== 1
def test_chain_lines_have_seq_prev_hash_and_verify_passes(tmp_path):
    fake = FakeChflags()
    chain = auditlog.Chain(tmp_path, clock=Clock(), chflags=fake)
    write_n(chain, 5)
    [path] = chain_files(tmp_path)
    rows = lines(path)
    assert len(rows) == 5
    assert all(set(row) == {'seq', 'prev', 'record', 'hash'} for row in rows)
    assert [row['seq'] for row in rows] == [1, 2, 3, 4, 5]
    assert rows[0]['prev'] == ''
    for i, row in enumerate(rows):
        if i:
            assert row['prev'] == rows[i - 1]['hash']
        assert auditlog.canonical(row['record']) == json.dumps(row['record'], sort_keys=True, separators=(',', ':'),
                                                               ensure_ascii=False)
        assert row['hash'] == hashlib.sha256((row['prev'] + auditlog.canonical(row['record'])).encode()).hexdigest()
    assert auditlog.verify(path) == (True, None)


# ====================================================================== 2
def test_tamper_middle_line_detected_at_that_seq(tmp_path):
    for case in ('edit', 'garbage', 'delete'):
        d = tmp_path / case
        chain = auditlog.Chain(d, clock=Clock(), chflags=FakeChflags())
        write_n(chain, 5)
        [path] = chain_files(d)
        raw = path.read_text(encoding='utf-8').splitlines()
        if case == 'edit':
            row = json.loads(raw[2])
            row['record']['n'] = 99
            raw[2] = json.dumps(row)
        elif case == 'garbage':
            raw[2] = '{not json'
        else:
            del raw[2]
        os.chmod(path, 0o600)
        path.write_text('\n'.join(raw) + '\n', encoding='utf-8')
        ok, bad = auditlog.verify(path)
        assert ok is False
        assert bad == (4 if case == 'delete' else 3), case


# ====================================================================== 3
def test_file_opened_append_only_never_truncated_mode_0600(tmp_path, monkeypatch):
    opened, order = [], []
    real_open = os.open

    def spy_open(path, flags, mode=0o777, *a, **k):
        opened.append((str(path), flags))
        order.append(('open', str(path)))
        return real_open(path, flags, mode, *a, **k)
    monkeypatch.setattr(os, 'open', spy_open)
    monkeypatch.setattr(os, 'chflags', lambda *a, **k: pytest.fail('the real chflags must never run in tests'),
                        raising=False)
    fake = FakeChflags()

    def chflags(path, flags):
        order.append(('chflags', str(path)))
        fake(path, flags)
    audit = Audit()
    chain = auditlog.Chain(tmp_path, clock=Clock(), chflags=chflags, audit=audit)
    write_n(chain, 2)
    [path] = chain_files(tmp_path)
    flags = [fl for p, fl in opened if p == str(path)]
    assert flags and all(fl & os.O_APPEND and fl & os.O_WRONLY and fl & os.O_CREAT and not fl & os.O_TRUNC
                         for fl in flags)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = auditlog.Chain(tmp_path, clock=Clock(), chflags=chflags, audit=audit)
    write_n(again, 1, start=2)
    assert [row['seq'] for row in lines(path)] == [1, 2, 3]
    # uappend default on: once per chain file, after it was created, never the lock or the directory
    assert [p for p, _ in fake.calls] == [str(path)] and all(fl & stat.UF_APPEND for _, fl in fake.calls)
    assert order.index(('open', str(path))) < order.index(('chflags', str(path)))
    assert not any(p.endswith('.writer.lock') or p == str(tmp_path) for p, _ in fake.calls)
    # uappend off -> never called
    off = FakeChflags()
    write_n(auditlog.Chain(tmp_path / 'off', clock=Clock(), chflags=off, uappend=False), 2)
    assert off.calls == []
    # unsupported: ENOTSUP, or no chflags at all (Linux) -> writes go on, one audit row per Chain instance
    for name, chf in (('enotsup', FakeChflags(OSError(errno.ENOTSUP, 'not supported'))), ('linux', None)):
        a = Audit()
        clock = Clock()
        c = auditlog.Chain(tmp_path / name, clock=clock, chflags=chf, audit=a)
        write_n(c, 5)
        clock.t = DAY2
        write_n(c, 1, start=5)
        assert sum(len(lines(p)) for p in chain_files(tmp_path / name)) == 6
        assert all(auditlog.verify(p)[0] for p in chain_files(tmp_path / name))
        reasons = [f.get('reason') for e, f in a if e == 'security.audit_chain']
        assert reasons.count('uappend_unsupported') == 1, name


# ====================================================================== 4
def test_day_rollover_continues_prev_hash_across_files(tmp_path):
    clock = Clock()
    chain = auditlog.Chain(tmp_path, clock=clock, chflags=FakeChflags())
    write_n(chain, 2)
    clock.t = DAY2
    write_n(chain, 2, start=2)
    first, second = chain_files(tmp_path)
    a, b = lines(first), lines(second)
    assert len(a) == 2 and len(b) == 2
    assert b[0]['prev'] == a[-1]['hash'] and b[0]['seq'] == 3
    assert auditlog.verify(first) == (True, None) and auditlog.verify(second) == (True, None)
    restarted = auditlog.Chain(tmp_path, clock=clock, chflags=FakeChflags())
    write_n(restarted, 1, start=4)
    assert lines(second)[-1]['seq'] == 5 and lines(second)[-1]['prev'] == lines(second)[-2]['hash']
    # crash mid-line: a new instance never truncates, continues from the last whole line, verify reports the bad line
    data = second.read_bytes()
    cut = data[:len(data) - (len(data.splitlines()[-1]) // 2) - 1]
    os.chmod(second, 0o600)
    second.write_bytes(cut)
    size = second.stat().st_size
    write_n(auditlog.Chain(tmp_path, clock=clock, chflags=FakeChflags()), 1, start=9)
    assert second.read_bytes()[:size] == cut
    ok, bad = auditlog.verify(second)
    assert ok is False and bad == 5


# ====================================================================== 5
def test_mirror_xreads_audit_stream_from_last_id_and_persists_cursor_filtered_by_events(fx, tmp_path, monkeypatch):
    f, r = fx, fx.r
    run = Path(f.cfg['run'])
    for i in range(3):
        f.v2.audit('channel.in', n=i)
    for i in range(2):
        f.v2.audit('watch.tick', n=i)
    last = r.xrevrange(f.v2.bus.audit_key, count=1)[0][0]
    policy = policy_with(audit_log={'events': ['channel.*']})
    scanner = secrets.Scanner(policy)
    chain = auditlog.Chain(tmp_path / 'a', chflags=FakeChflags())
    total = 0
    while True:
        got = auditlog.mirror(r, run, policy, scanner=scanner, chain=chain, count=2)
        assert got <= 2
        if not got:
            break
        total += got
    rows = [row for p in chain_files(tmp_path / 'a') for row in lines(p)]
    assert [row['record']['event'] for row in rows] == ['channel.in'] * 3
    assert r.type(channels.AUDITLOG) == 'hash' and r.hget(channels.AUDITLOG, 'last_id') == last
    restarted = auditlog.Chain(tmp_path / 'a', chflags=FakeChflags())
    assert auditlog.mirror(r, run, policy, scanner=scanner, chain=restarted) == 0
    assert sum(len(lines(p)) for p in chain_files(tmp_path / 'a')) == 3
    # crash between the chain write and the cursor: no duplicate line on the next call
    f.v2.audit('channel.out', n=9)
    real_hset = r.hset
    calls = []

    def flaky(*a, **k):
        if not calls:
            calls.append(1)
            raise ConnectionError('fake crash before the cursor moved')
        return real_hset(*a, **k)
    monkeypatch.setattr(r, 'hset', flaky)
    with pytest.raises(ConnectionError):
        auditlog.mirror(r, run, policy, scanner=scanner, chain=restarted)
    auditlog.mirror(r, run, policy, scanner=scanner, chain=restarted)
    assert sum(len(lines(p)) for p in chain_files(tmp_path / 'a')) == 4
    # single writer: a held lock -> the second chain cannot write
    holder = auditlog.Chain(tmp_path / 'b', chflags=FakeChflags())
    assert holder.acquire() is True
    other = auditlog.Chain(tmp_path / 'b', chflags=FakeChflags())
    assert other.acquire() is False
    r.delete(channels.AUDITLOG)
    assert auditlog.mirror(r, run, policy, scanner=scanner, chain=other) == 0
    assert chain_files(tmp_path / 'b') == []
    holder.close()
    assert other.acquire() is True
    # daemon side: without channels: the daemon pump mirrors on its own tick
    f.cfg['security'] = make_policy()
    for _ in range(3):
        f.tick()
    assert chain_files(run / 'audit')


# ====================================================================== 6
def test_records_pass_secret_scanner_before_write(fx, config_dir, tmp_path):
    f, r = fx, fx.r
    d = config_dir / 'channels'
    d.mkdir(parents=True, exist_ok=True)
    (d / 'telegram-bot1.env').write_text('BOT_TOKEN=' + TOK + '\nALLOW=111\n', encoding='utf-8')
    policy = make_policy()
    f.v2.audit('channel.send_failed', error='call failed ' + TOK)
    chain = auditlog.Chain(tmp_path, chflags=FakeChflags())
    auditlog.mirror(r, Path(f.cfg['run']), policy, scanner=secrets.Scanner(policy), chain=chain)
    rows = [row for p in chain_files(tmp_path) for row in lines(p)]
    hit = [row for row in rows if row['record'].get('event') == 'channel.send_failed']
    assert len(hit) == 1
    assert hit[0]['record']['error'] == '***' and hit[0]['record']['secret_blocked'] == 1
    assert all(TOK not in json.dumps(row, ensure_ascii=False) for row in rows)
    assert all(auditlog.verify(p) == (True, None) for p in chain_files(tmp_path))


# ====================================================================== 7
def test_cli_channel_audit_verify_exit_codes(tmp_path):
    from cmux_fleet.channels import cli
    run = tmp_path / 'run'
    chain = auditlog.Chain(run / 'audit', chflags=FakeChflags())
    write_n(chain, 3)
    cfg = run / 'config.json'
    cfg.write_text(json.dumps({'run': str(run), 'security': make_policy()}), encoding='utf-8')
    try:
        args = fleet.cli().parse_args(['channel', 'audit', '--config', str(cfg), '--verify'])
    except SystemExit:
        pytest.fail('red: P8g `channel audit` subcommand not registered in fleet.cli()')

    def call(argv):
        out, err = io.StringIO(), io.StringIO()
        return cli.run(argv, stdout=out, stderr=err), out.getvalue() + err.getvalue()
    assert cli.run(args, stdout=io.StringIO(), stderr=io.StringIO()) == 0
    code, text = call(['audit', '--config', str(cfg)])
    assert code == 0 and 'chain-' in text and '3' in text
    [path] = chain_files(run / 'audit')
    raw = path.read_text(encoding='utf-8').splitlines()
    row = json.loads(raw[1])
    row['record']['n'] = 42
    raw[1] = json.dumps(row)
    os.chmod(path, 0o600)
    path.write_text('\n'.join(raw) + '\n', encoding='utf-8')
    code, text = call(['audit', '--config', str(cfg), '--verify'])
    assert code == 1 and '2' in text


# ====================================================================== 8 [baseline]
def test_audit_log_disabled_writes_nothing(fx, monkeypatch):
    f, r = fx, fx.r
    f.channel_spawn = lambda argv: types.SimpleNamespace(pid=0, poll=lambda: None, terminate=lambda: None, kill=lambda: None, wait=lambda timeout=None: 0)  # test-contract M98: no real 'channel actor' child
    run = Path(f.cfg['run'])

    def never(*a, **k):
        pytest.fail('audit off must not build a Scanner or read an account')
    monkeypatch.setattr(secrets, 'Scanner', never)
    monkeypatch.setattr(channels, 'load_account', never)
    fake = FakeChflags()
    for policy in (None, policy_with(audit_log={'enabled': 'off'})):
        for with_channels in (False, True):
            f.cfg.pop('security', None)
            f.cfg.pop('channels', None)
            if policy is not None:
                f.cfg['security'] = policy
            if with_channels:
                f.cfg['channels'] = [{'type': 'fake', 'account': 'contract-1'}]
            f.v2.audit('channel.in', n=1)
            assert auditlog.mirror(r, run, policy, scanner=None, chain=None) == 0
            assert auditlog.daemon_pump(f) == 0
            for _ in range(3):
                f.tick()
            assert not (run / 'audit').exists()
            assert not list(run.rglob('.writer.lock'))
            assert r.exists(channels.AUDITLOG) == 0
            events = [row.get('event') for _, row in r.xrange(f.v2.bus.audit_key)]
            assert 'security.audit_chain' not in events
    assert fake.calls == []


# ====================================================================== 9 (F-1, supervisor M169)
def test_daemon_pump_honours_audit_log_uappend_off(fx, monkeypatch):
    """``audit_log.uappend: off`` -> the daemon's chain never sets the flag; the default (on) still does once per
    new chain file. The Chain is spied with a fake chflags forced in: the real one never runs here."""
    f, r = fx, fx.r
    run = Path(f.cfg['run'])
    real_chain, built = auditlog.Chain, []

    def spy(*a, **k):
        fake = FakeChflags()
        k['chflags'] = fake
        built.append(fake)
        return real_chain(*a, **k)
    monkeypatch.setattr(auditlog, 'Chain', spy)
    f.v2.audit('channel.in', n=1)
    for case, sections in (('off', {'dir': 'audit-off', 'uappend': 'off'}), ('on', {'dir': 'audit-on'})):
        f.__dict__.pop('_audit_pump', None)
        r.delete(channels.AUDITLOG)
        built.clear()
        f.cfg['security'] = policy_with(audit_log=sections)
        assert auditlog.daemon_pump(f) > 0, case
        assert chain_files(run / sections['dir']), case
        calls = [c for fake in built for c in fake.calls]
        if case == 'off':
            assert calls == []
        else:
            assert [p for p, _ in calls] == [str(p) for p in chain_files(run / 'audit-on')]
        f._audit_pump['chain'].close()
