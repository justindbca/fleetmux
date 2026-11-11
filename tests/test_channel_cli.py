"""`fleetmux channel login|test|send|status|actor` and the FLEETMUX_TG_CMD takeover (package P1g; brief §6.8,
docs/design.md §53, docs/contracts.md §8; v2 process model: ONE actor per fleet, `channel actor <config.json>`).

Fakes only: an in-test adapter module ``fleetmux_test_adapter`` (``ADAPTER`` = a FakeChannel subclass whose
transport records every call; ``LOGIN`` only where a test defines it) registered as type ``fake``; the repo's
private test Redis; a temp config dir. No network, no real token, no listener. ``actor`` / ``cli`` are imported
inside the tests so a package that has not landed fails the test instead of breaking collection.

JSON shapes fixed here for P1g: ``channel send`` prints ``{"status": queued|sent|failed}``; ``channel login`` prints
``{"status", "path"}``; ``channel status --json`` prints ``{"channels": [{"type", "account", "state", "holder",
"last_in", "last_out", "last_error", "restarts", "queue", "dead", ...}], ...}``.
"""
import argparse
import inspect
import io
import json
import os
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_lifecycle_close import as_task, fx, table  # noqa: F401  (fx / table: fixtures)
from test_channels_contract import FakeChannel
from test_pi import mock_start
from fleetmux import channels, fleet, lifecycle, lifecycle_close
from fleetmux.channels import base, envelope

TOKEN = 'fake-token-123'
ACCOUNT_TEXT = 'ALLOW=u-1\nPUSH_TO=u-1\nTOKEN=' + TOKEN + '\n'
MODULE = 'fleetmux_test_adapter'


# ====================================================================== fixtures and helpers
@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    d = tmp_path / 'cfgA'
    monkeypatch.setenv(channels.CONFIG_ENV, str(d))
    return d


class RecorderBase(FakeChannel):
    """FakeChannel; with no transport given it records into the test's ``rec`` (sends, fleet names, inits)."""
    rec = None

    def __init__(self, account, **kwargs):
        self.rec.inits.append(sorted(kwargs))
        super().__init__(account, **kwargs)
        self.fleet_name = getattr(self, 'fleet_name', None)
        if self.transport is None:
            self.transport = self._record
        self.rec.instances.append(self)

    def start(self):
        """No optional dependency here (FakeChannel.start reports one missing on purpose): inbound may start."""

    def _record(self, op, data):
        if op == 'poll':
            return []
        hook = self.rec.hooks.get(op)
        if hook:
            out = hook(self, data)
            if out is not None:
                self.rec.sent.append((op, data, self.fleet_name))
                return out
        self.rec.sent.append((op, data, self.fleet_name))
        return 'm-' + str(len(self.rec.sent))

    def check(self):
        self.rec.checks.append(self.fleet_name)
        hook = self.rec.hooks.get('check')
        return hook(self) if hook else base.SendResult(ok=True)


@pytest.fixture
def rec(monkeypatch):
    """The adapter module ``fleetmux_test_adapter`` (ADAPTER = Recorder), registered as type ``fake``."""
    state = SimpleNamespace(sent=[], inits=[], instances=[], checks=[], hooks={}, logins=[])
    cls = type('Recorder', (RecorderBase,), {'__module__': MODULE, 'rec': state})
    mod = types.ModuleType(MODULE)
    mod.ADAPTER = cls
    monkeypatch.setitem(sys.modules, MODULE, mod)
    undo = channels.register('fake', cls, 'fake')
    state.cls, state.module = cls, mod
    yield state
    undo()


def texts(rec):
    return [(d['to'], d['text'], name) for op, d, name in rec.sent if op == 'send_text']


def write_account(text=ACCOUNT_TEXT, type_='fake', account='contract-1'):
    path = channels.account_path(type_, account)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(text)
    return path


def socket_of(r):
    return r.connection_pool.connection_kwargs['path']


def write_cfg(tmp_path, redis_socket, **extra):
    run = tmp_path / 'run'
    run.mkdir(parents=True, exist_ok=True)
    cfg = dict(run=str(run), token='tok-fleet', redis_socket=redis_socket, transport_version=2,
               channels=[{'type': 'fake', 'account': 'contract-1', 'push': list(channels.DEFAULT_PUSH)}])
    cfg.update(extra)
    path = run / 'config.json'
    path.write_text(json.dumps(cfg))
    return str(path), cfg


def invoke(argv, stdin=''):
    """(exit code, stdout, stderr) of ``cli.run``; an argparse exit counts as its code."""
    from fleetmux.channels import cli
    out, err = io.StringIO(), io.StringIO()
    try:
        code = cli.run(argv, stdin=io.StringIO(stdin), stdout=out, stderr=err)
    except SystemExit as exc:
        code = exc.code
    return code, out.getvalue(), err.getvalue()


def queued(r, key=channels.OUTQ):
    return [json.loads(x) for x in r.lrange(key, 0, -1)]


def file_state(path):
    path = Path(path)
    return (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None


# ====================================================================== channel send
def test_send_queues_when_actor_beat_fresh_else_direct(private_redis_v2, rec, tmp_path, monkeypatch):  # noqa: F811
    from fleetmux.channels import actor, cli
    r = private_redis_v2
    r.flushdb()
    write_account()
    c, _ = write_cfg(tmp_path, socket_of(r))
    direct = []
    monkeypatch.setattr(actor, 'send_direct', lambda cfg, item: direct.append(item) or {'status': 'sent'})
    assert cli.BEAT_FRESH_S == 30 and cli.BEAT_FRESH_S < 90  # R5: its own constant, not P1f's 90 s restart
    assert inspect.signature(cli.send).parameters['beat_fresh_s'].default == cli.BEAT_FRESH_S
    for n, age in enumerate((0, cli.BEAT_FRESH_S - 1), 1):  # fresh -> queued for the actor
        r.hset(channels.ACTOR, mapping={'pid': '1', 'beat': str(time.time() - age)})
        code, out, _ = invoke(['channel', 'send', '--config', c, '--text', 'hi'])
        assert (code, json.loads(out)) == (0, {'status': 'queued'})
        assert r.llen(channels.OUTQ) == n and queued(r)[0]['event'] == 'manual' and not direct
    for stale in (True, False):  # stale beat, then no actor hash at all -> direct send
        if stale:
            r.hset(channels.ACTOR, 'beat', str(time.time() - cli.BEAT_FRESH_S - 1))
        else:
            r.delete(channels.ACTOR)
        before = len(direct)
        code, out, _ = invoke(['channel', 'send', '--config', c, '--text', 'hi'])
        assert (code, json.loads(out)) == (0, {'status': 'sent'})
        assert len(direct) == before + 1 and r.llen(channels.OUTQ) == 2


def test_send_without_redis_uses_last_json_then_first(rec, tmp_path, monkeypatch):
    allow_two = 'ALLOW=u-1,u-2\nPUSH_TO=u-2\nTOKEN=' + TOKEN + '\n'
    c, _ = write_cfg(tmp_path, str(tmp_path / 'no-such.sock'), name='fleet-a')
    write_account(allow_two)  # directory A: with last.json
    last = channels.last_path()
    last.parent.mkdir(parents=True, exist_ok=True)
    last.write_text(json.dumps({'channel': 'fake', 'account': 'contract-1', 'conversation': 'u-1', 'reply_ref': {}}))
    code, out, err = invoke(['channel', 'send', '--config', c, '--text', 'hi'])
    assert (code, json.loads(out)) == (0, {'status': 'sent'}), err
    assert texts(rec) == [('u-1', 'hi', 'fleet-a')]  # I2: the fleet name is set by P1e's send_direct
    monkeypatch.setenv(channels.CONFIG_ENV, str(tmp_path / 'cfgB'))  # directory B: no last.json
    write_account(allow_two)
    code, out, err = invoke(['channel', 'send', '--config', c, '--text', 'again'])
    assert (code, json.loads(out)) == (0, {'status': 'sent'}), err
    assert texts(rec)[-1][:2] == ('u-2', 'again')  # the first channel's PUSH_TO


def test_send_channel_and_to_flags_make_explicit_target(private_redis_v2, rec, tmp_path):  # noqa: F811
    r = private_redis_v2
    r.flushdb()
    write_account()
    c, _ = write_cfg(tmp_path, socket_of(r))
    r.hset(channels.ACTOR, mapping={'pid': '1', 'beat': str(time.time())})
    code, _, err = invoke(['channel', 'send', '--config', c, '--channel', 'fake:contract-1', '--to', '555',
                           '--text', 'hi'])
    assert code == 0, err
    item = queued(r)[0]
    assert item['target'] == 'explicit'
    assert (item['channel'], item['account'], item['conversation']) == ('fake', 'contract-1', '555')
    invoke(['channel', 'send', '--config', c, '--channel', 'fake:contract-1', '--to', '555', '--reply-to', '7',
            '--text', 'hi'])
    assert queued(r)[0]['reply_ref'] == {'message_id': '7'}
    invoke(['channel', 'send', '--config', c, '--channel', 'fake:contract-1', '--to', '555', '--reply-to',
            '{"chat_id":"9"}', '--text', 'hi'])
    assert queued(r)[0]['reply_ref'] == {'chat_id': '9'}


def test_send_text_file_dash_reads_stdin(private_redis_v2, rec, tmp_path):  # noqa: F811
    r = private_redis_v2
    r.flushdb()
    write_account()
    c, _ = write_cfg(tmp_path, socket_of(r))
    r.hset(channels.ACTOR, mapping={'pid': '1', 'beat': str(time.time())})
    for flag in ('--text-file', '--text'):
        code, _, err = invoke(['channel', 'send', '--config', c, flag, '-'], stdin='多行\n正文')
        assert code == 0, err
        assert queued(r)[0]['text'] == '多行\n正文'


def test_send_file_flag_makes_file_item(private_redis_v2, rec, tmp_path, monkeypatch):  # noqa: F811
    r = private_redis_v2
    r.flushdb()
    write_account()
    c, _ = write_cfg(tmp_path, socket_of(r))
    r.hset(channels.ACTOR, mapping={'pid': '1', 'beat': str(time.time())})
    (tmp_path / 'r.txt').write_text('report')
    monkeypatch.chdir(tmp_path)
    code, _, err = invoke(['channel', 'send', '--config', c, '--file', 'r.txt'])
    assert code == 0, err
    item = queued(r)[0]
    assert item['file'] == str((tmp_path / 'r.txt').resolve()) and item['text'] is None
    assert envelope.outbound_problem(item) == ''
    code, _, _ = invoke(['channel', 'send', '--config', c, '--file', 'missing.txt'])
    assert code == 2 and r.llen(channels.OUTQ) == 1


def test_send_failure_exit_1_stderr_masked_usage_error_exit_2(rec, tmp_path):
    write_account()
    c, _ = write_cfg(tmp_path, str(tmp_path / 'no-such.sock'))  # no Redis: direct send
    rec.hooks['send_text'] = lambda ch, data: base.SendResult(ok=False, error='HTTP 401 for ' + TOKEN + ' url')
    code, out, err = invoke(['channel', 'send', '--config', c, '--text', 'hi'])
    assert code == 1 and json.loads(out) == {'status': 'failed'}
    assert '***' in err and TOKEN not in err and TOKEN not in out
    assert invoke(['channel', 'send', '--text', 'hi'])[0] == 2  # neither --config nor --channel/--to
    (tmp_path / 'r.txt').write_text('x')
    assert invoke(['channel', 'send', '--config', c, '--text', 'hi', '--file', str(tmp_path / 'r.txt')])[0] == 2


# ====================================================================== channel status / test
def status_row(data, type_='fake', account='contract-1'):
    rows = [x for x in data['channels'] if (x['type'], x['account']) == (type_, account)]
    assert len(rows) == 1, data
    return rows[0]


def test_status_json_lists_state_holder_last_queue_dead(private_redis_v2, rec, tmp_path):  # noqa: F811
    from fleetmux.channels import lease
    r = private_redis_v2
    r.flushdb()
    write_account()
    c, _ = write_cfg(tmp_path, socket_of(r))
    r.hset(channels.STATUS + 'fake:contract-1', mapping={
        'state': 'running', 'last_in': '1762650000', 'last_out': '1762650001', 'last_error': 'none', 'restarts': '2'})
    r.hset(channels.ACTOR, mapping={'pid': str(os.getpid()), 'beat': str(time.time())})
    held = lease.acquire('fake', 'contract-1', run_id='r-held', run=str(tmp_path / 'run'))
    try:
        for n in range(2):
            r.lpush(channels.OUTQ_ACCOUNT + 'fake:contract-1', json.dumps(envelope.outbound('manual', text=str(n))))
        mine = dict(envelope.outbound('manual', text='x'), channel='fake', account='contract-1')
        other = dict(envelope.outbound('manual', text='y'), channel='fake', account='other-1')
        r.lpush(channels.DEAD, json.dumps(mine), json.dumps(other))
        code, out, err = invoke(['channel', 'status', '--config', c, '--json'])
        assert code == 0, err
        row = status_row(json.loads(out))
        assert row['state'] == 'running' and row['holder']['run_id'] == 'r-held'
        assert (str(row['last_in']), str(row['last_out']), row['last_error'], int(row['restarts'])) == (
            '1762650000', '1762650001', 'none', 2)
        assert (row['queue'], row['dead']) == (2, 1)
        r.hset(channels.STATUS + 'fake:contract-1', 'state', 'needs_login')
        code, out, _ = invoke(['channel', 'status', '--config', c])
        assert '重新 channel login fake contract-1' in out
        code, out, err = invoke(['channel', 'status', '--json'])  # no --config: this machine's account files
        assert code == 0, err
        row = status_row(json.loads(out))
        assert row['holder']['run_id'] == 'r-held'
        code, text, _ = invoke(['channel', 'status'])
        for shown in (out, text):
            assert 'contract-1' in shown and TOKEN not in shown and 'u-1' not in shown
    finally:
        if held is not None:
            held.release()


def test_test_runs_check_or_sends_one_message(rec):
    write_account()
    code, _, _ = invoke(['channel', 'test', 'fake', 'contract-1'])
    assert code == 0 and len(rec.checks) == 1 and not rec.sent
    rec.hooks['check'] = lambda ch: base.SendResult(ok=False, error='down')
    assert invoke(['channel', 'test', 'fake', 'contract-1'])[0] == 1

    def auth(ch):
        raise channels.ChannelError('auth', 'HTTP 401 ' + TOKEN)
    rec.hooks['check'] = auth
    code, out, err = invoke(['channel', 'test', 'fake', 'contract-1'])
    assert code == 1 and 'needs_login' in err and TOKEN not in err + out
    rec.hooks.pop('check')
    code, _, err = invoke(['channel', 'test', 'fake', 'contract-1', '--to', 'u-1', '--text', 'hi'])
    assert code == 0, err
    assert [(to, text) for to, text, _ in texts(rec)] == [('u-1', 'hi')]
    # I2: no cfg -> no fleet name, never the account name standing in for it
    for name in rec.checks + [n for _, _, n in texts(rec)]:
        assert name in ('', None) and name != 'contract-1'


# ====================================================================== channel login
def account_values(type_='fake', account='contract-1'):
    a = channels.load_account(type_, account)
    return a.values, set(a.allow)


def assert_quiet(out, err, *secrets):
    assert set(json.loads(out)) == {'status', 'path'}
    for s in secrets:
        assert s not in out and s not in err


def test_login_writes_0600_file_set_allow_from_stdin_never_prints_values(rec):
    path = channels.account_path('fake', 'contract-1')
    for with_login in (False, True):  # R2: --set / --from-stdin never call LOGIN
        called = []
        if with_login:
            rec.module.LOGIN = lambda name, io_, transport: called.append(name) or {'TOKEN': 'tok-login-value'}
            path.unlink()
        code, out, err = invoke(['channel', 'login', 'fake', 'contract-1', '--set', 'TOKEN=tok-t1-value',
                                 '--allow', 'u-1'])
        assert code == 0, err
        assert_quiet(out, err, 'tok-t1-value')
        assert (path.stat().st_mode & 0o777) == 0o600 and (path.parent.stat().st_mode & 0o777) == 0o700
        assert account_values() == ({'TOKEN': 'tok-t1-value'}, {'u-1'})
        path.write_text('TOKEN=tok-old-value\nKEEP=1\nALLOW=u-old\n')
        code, out, err = invoke(['channel', 'login', 'fake', 'contract-1', '--set', 'TOKEN=tok-new-value'])
        assert code == 0, err
        assert_quiet(out, err, 'tok-new-value', 'tok-old-value')
        assert account_values() == ({'TOKEN': 'tok-new-value', 'KEEP': '1'}, {'u-old'})  # merged
        code, out, err = invoke(['channel', 'login', 'fake', 'contract-1', '--allow', 'u-1', '--allow', 'u-2',
                                 '--set', 'KEEP=1'])
        assert code == 0, err
        assert account_values() == ({'TOKEN': 'tok-new-value', 'KEEP': '1'}, {'u-1', 'u-2'})  # ALLOW replaced
        code, out, err = invoke(['channel', 'login', 'fake', 'contract-1', '--from-stdin'], stdin='TOKEN=tok-stdin-value\r\n')
        assert code == 0, err
        assert_quiet(out, err, 'tok-stdin-value')
        assert account_values()[0] == {'TOKEN': 'tok-stdin-value', 'KEEP': '1'} and b'\r' not in path.read_bytes()
        assert not called


def test_login_calls_adapter_LOGIN_when_defined(rec):
    path = channels.account_path('fake', 'contract-1')
    calls, answer = [], {}

    def LOGIN(account_name, io_, transport):
        calls.append((account_name, io_, transport))
        io_.print('二维码链接 https://example.invalid/qr')
        if isinstance(answer.get('value'), Exception):
            raise answer['value']
        return dict(answer.get('value', {}))
    rec.module.LOGIN = LOGIN
    write_account('TOKEN=tok-old-value\nKEEP=1\n')
    answer['value'] = {'TOKEN': 'tok-from-login', 'ALLOW': 'u-9'}
    code, out, err = invoke(['channel', 'login', 'fake', 'contract-1'])
    assert code == 0, err
    name, io_, transport = calls[-1]
    assert name == 'contract-1' and transport is None
    assert callable(getattr(io_, 'print', None)) and callable(getattr(io_, 'readline', None))
    assert '二维码链接' in err and '二维码链接' not in out and set(json.loads(out)) == {'status', 'path'}
    assert account_values() == ({'TOKEN': 'tok-from-login', 'KEEP': '1'}, {'u-9'})
    code, _, err = invoke(['channel', 'login', 'fake', 'contract-1', '--allow', 'u-1'])
    assert code == 0 and account_values()[1] == {'u-1'}  # --allow wins over LOGIN's ALLOW
    before = file_state(path)
    answer['value'] = {}  # already bound: success, nothing written
    assert invoke(['channel', 'login', 'fake', 'contract-1'])[0] == 0 and file_state(path) == before
    answer['value'] = channels.ChannelError('auth', 'qr expired')
    code, out, err = invoke(['channel', 'login', 'fake', 'contract-1'])
    assert code == 1 and file_state(path) == before and 'tok-from-login' not in err + out
    # R2: only --allow still calls LOGIN; {} then writes nothing at all (not even --allow)
    other = channels.account_path('fake', 'contract-2')
    answer['value'] = {}
    n = len(calls)
    assert invoke(['channel', 'login', 'fake', 'contract-2', '--allow', 'u-1'])[0] == 0
    assert len(calls) == n + 1 and not other.exists()
    assert invoke(['channel', 'login', 'fake', 'contract-1', '--allow', 'u-7'])[0] == 0
    assert file_state(path) == before
    # I2: login has no cfg -> no fleet name; the account name never stands in for it
    assert all(getattr(i, 'fleet_name', None) in ('', None) for i in rec.instances)
    assert all(getattr(c[1], 'fleet_name', None) != 'contract-1' for c in calls)


# ====================================================================== parser
BASE_COMMANDS = set('actor advisor agents chrome claude-clear claude-posttool claude-prompt claude-rewake claude-stop '
                    'claude-stop-failure daemon gui lifecycle-close logs night notify pause peer pi-event plan request '
                    'results resume start status stop subfleet task'.split())  # 4cd1156 (unchanged at 26392b9)
BASE_START_DESTS = set(
    'advisor allow_development_v2 allow_profile_live_switch allow_unsandboxed_pi attach backend brake_notify budget '
    'chrome_min_interval chrome_script claude claude_hook_delivery claude_window clear_after_result clear_timeout cmd '
    'cmux_bin cmux_state_dir codex codex_home compact_limit dsh dsh_home dsh_model extend_hours fleet_home gui '
    'handoff_ratio handoff_timeout host initiator kit_fleet lifecycle max_attempts max_busy mode name night_roster '
    'opencode opencode_model opencode_window pi pi_agent_dir pi_model pi_thinking profile prompt_table '
    'redis_runtime_dir redis_server request_timeout resume roles roster runs_dir stall_cpu_pct stall_escapes '
    'stall_grace stall_timeout stall_window supervisor_cli supervisor_model supervisor_pi_model '
    'supervisor_pi_thinking supervisor_profile transport_version window worker_cli worker_domains '
    'worker_idle_reclaim_min worker_kit worker_model worker_names worker_profile workers'.split())


def commands(parser):
    sub = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    return sub[0].choices


def test_cli_parser_existing_subcommands_unchanged():
    p = fleet.cli()
    assert len(BASE_COMMANDS) == 28 and len(BASE_START_DESTS) == 74
    assert BASE_COMMANDS <= set(commands(p))
    assert BASE_START_DESTS <= set(vars(p.parse_args(['start', '--request-timeout', '5'])))
    assert set(vars(p.parse_args(['request', 'x.json', '--config', 'c']))) == {'cmd', 'config', 'spec'}
    assert set(vars(p.parse_args(['logs', '--config', 'c', '--json']))) == {
        'after', 'cmd', 'config', 'event', 'follow', 'generation', 'json', 'level', 'limit', 'request', 'slot'}


def test_cli_parser_adds_channel_with_five_subcommands(tmp_path, monkeypatch):
    p = fleet.cli()
    assert set(commands(p)) - BASE_COMMANDS == {'channel', 'claude-pretool', 'home', 'operator', 'setup', 'tab'}  # main §48 / §52 / §50.1 / §31
    assert set(commands(commands(p)['channel'])) == {'login', 'test', 'send', 'status', 'actor', 'audit', 'approve'}
    assert p.parse_args(['channel', 'actor', 'c.json']).cfg == 'c.json'
    with pytest.raises(SystemExit) as exc:
        p.parse_args(['channel', 'actor', 'c.json', 'fake', 'a1'])  # v2: one positional only
    assert exc.value.code == 2
    assert p.parse_args(['start', '--channel-user-resume', 'off']).channel_user_resume == 'off'
    assert p.parse_args(['start', '--channel-user-resume', 'maybe']).channel_user_resume == 'maybe'  # P6c checks
    assert p.parse_args(['start']).channel_user_resume is None
    # the contract skeleton's arguments (test_red_p1_cli_has_the_channel_subcommands) still parse
    a = p.parse_args(['channel', 'send', '--config', 'c.json', '--event', 'daemon_down', '--text', 'down'])
    assert (a.cmd, a.channel_cmd, a.event, a.text) == ('channel', 'send', 'daemon_down', 'down')
    a = p.parse_args(['channel', 'login', 'telegram', 'fleet-bot-1', '--allow', '111', '--set', 'PUSH_TO=111'])
    assert (a.type, a.account, a.allow, a.set) == ('telegram', 'fleet-bot-1', ['111'], ['PUSH_TO=111'])
    assert p.parse_args(['channel', 'status', '--config', 'c.json', '--json']).json
    # R1: start writes the flag into the run config (mock_start runs to the end, service lifecycle)
    for n, value in enumerate(('off', 'prompt_only', None)):
        d = tmp_path / ('start-' + str(n))
        d.mkdir()
        args, _ = mock_start(d, monkeypatch)
        args[args.index('--lifecycle') + 1] = 'service'
        fleet.main(args + (['--channel-user-resume', value] if value else []))
        cfg = fleet.load(d / 'latest.json')
        if value:
            assert cfg['channel_user_resume'] == value
        else:
            assert 'channel_user_resume' not in cfg


# ====================================================================== lifecycle_close.notify takeover
def notify_fleet(tmp_path, push):
    class F:
        service = True
        notifier = None
        cfg = {'run': str(tmp_path), 'lifecycle': 'smoke', 'initiator': None,
               'channels': [{'type': 'telegram', 'account': 'fleet-bot-1', 'push': push}]}

        def task(self):
            return {}

        def log(self, *a, **k):
            pass
    return F()


def test_lifecycle_notify_uses_send_direct_when_channels(monkeypatch, tmp_path):
    from fleetmux.channels import actor
    sent = []
    monkeypatch.setattr(actor, 'send_direct', lambda cfg, item: sent.append(item) or {'status': 'sent'})
    out = tmp_path / 'tg.txt'
    monkeypatch.setenv(lifecycle_close.TG_ENV, sys.executable + ' -c "open(%r, \'w\').write(\'ran\')"' % str(out))
    recs = lifecycle_close.notify(notify_fleet(tmp_path, ['run_closed']), 'closed', '收尾完成')
    tg = [x for x in recs if x['channel'] == 'tg'][0]
    assert tg['status'] == 'sent' and len(sent) == 1
    assert (sent[0]['event'], sent[0]['target'], sent[0]['text']) == ('run_closed', 'last', '收尾完成')
    assert not out.exists()  # the env command did not run
    parent_with = [x['status'] for x in recs if x['channel'] == 'parent']
    recs = lifecycle_close.notify(notify_fleet(tmp_path, ['result']), 'closed', '收尾完成')
    assert [x['status'] for x in recs if x['channel'] == 'tg'] == ['no_channel'] and len(sent) == 1
    assert [x['status'] for x in recs if x['channel'] == 'parent'] == parent_with  # parent path unchanged
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: 'injected')
    recs = lifecycle_close.notify(notify_fleet(tmp_path, ['run_closed']), 'closed', '收尾完成')
    assert {x['status'] for x in recs} == {'injected'} and len(sent) == 1  # injection still wins


def test_lifecycle_notify_env_only_without_channels(fx, table, monkeypatch, tmp_path):  # noqa: F811
    for env in (lifecycle_close.TG_ENV, lifecycle_close.PARENT_ENV):
        monkeypatch.delenv(env, raising=False)
    f = as_task(fx, 'smoke', {'kind': 'launchd', 'id': 'com.example.sector'})
    f.cfg['cmux_state_dir'] = str(tmp_path / 'state')
    recs = lifecycle_close.notify(f, 'closed', 'hello', lifecycle.get_mode('smoke'))
    assert [r['status'] for r in recs] == ['no_channel', 'mailbox']
    box = tmp_path / 'state' / 'mailbox' / 'launchd-com.example.sector.jsonl'
    assert json.loads(box.read_text())['text'] == 'hello'
    out = tmp_path / 'tg.txt'
    monkeypatch.setenv(lifecycle_close.TG_ENV,
                       sys.executable + ' -c "import sys; open(%r, \'w\').write(sys.stdin.read())"' % str(out))
    recs = lifecycle_close.notify(f, 'closed', '收尾完成', lifecycle.get_mode('smoke'))
    assert recs[0]['status'] == 'sent' and out.read_text() == '收尾完成'


# ====================================================================== channel actor (v2: one per fleet)
class FakeClock:
    def __init__(self):
        self.now = 1_762_650_000.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def sleep(self, s):
        with self.lock:
            self.now += max(float(s), 0.01)
        time.sleep(0.002)  # yield to the other threads


class Watched:
    """Wraps the actor's Redis client: records blocking pops so a test can prove no ``timeout=0``."""

    def __init__(self, client, calls):
        self._client, self._calls = client, calls

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if name in ('brpop', 'blpop', 'brpoplpush', 'blmove', 'bzpopmin', 'bzpopmax', 'xread', 'xreadgroup'):
            def watched(*args, **kwargs):
                self._calls.append((name, args, kwargs))
                return attr(*args, **kwargs)
            return watched
        return attr


def run_actor(cli, cfg_path, **kwargs):
    box = {}

    def target():
        try:
            box['code'] = cli.actor_main(cfg_path, **kwargs)
        except BaseException as exc:  # noqa: BLE001  (re-raised in the test thread)
            box['error'] = exc
    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, box


def finish(t, box, timeout=5):
    t.join(timeout)
    assert not t.is_alive(), 'actor_main did not return'
    if 'error' in box:
        raise box['error']
    return box['code']


def wait_until(cond, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def lease_busy_audits(r):
    found = []
    for key in r.scan_iter(_type='STREAM'):
        found += [f for _, f in r.xrange(key) if f.get('event') == 'channel.lease_busy']
    return found


def test_channel_actor_entry_acquires_leases_starts_threads_beats_and_exits_on_parent_death(
        private_redis_v2, rec, tmp_path, monkeypatch):  # noqa: F811
    from fleetmux.channels import actor, cli, lease
    r = private_redis_v2
    r.flushdb()
    for a in ('a1', 'a2'):
        write_account(account=a)
    entries = [{'type': 'fake', 'account': a, 'push': list(channels.DEFAULT_PUSH)} for a in ('a1', 'a2')]
    c, cfg = write_cfg(tmp_path, socket_of(r), name='fleet-a', channels=entries)
    # I2: the fleet label is the lifecycle_close.fleet_name chain: name -> token -> run directory name
    for probe in (dict(cfg), {k: v for k, v in cfg.items() if k != 'name'},
                  {k: v for k, v in cfg.items() if k not in ('name', 'token')}):
        want = probe.get('name') or probe.get('token') or Path(probe['run']).name
        assert channels.fleet_label(probe) == want == lifecycle_close.fleet_name(SimpleNamespace(cfg=probe))
    busy = lease.acquire('fake', 'a2', run_id='r-other', run=str(tmp_path / 'other'))
    assert busy is not None
    calls = []

    def inbound_loop(ch, *args, **kwargs):
        calls.append(ch)
        stop = next(x for x in list(args) + list(kwargs.values()) if hasattr(x, 'wait'))
        stop.wait(30)
    monkeypatch.setattr(actor, 'inbound_loop', inbound_loop)
    pops = []
    monkeypatch.setattr(cli, 'connect', lambda cfg_: Watched(__import__('redis').Redis(
        unix_socket_path=cfg_['redis_socket'], decode_responses=True), pops))
    clock, alive = FakeClock(), {'v': True}
    t, box = run_actor(cli, c, parent_alive=lambda: alive['v'], clock=clock, sleep=clock.sleep)
    try:
        assert wait_until(lambda: len(calls) == 1)
        assert calls[0].account.name == 'a1' and calls[0].fleet_name == channels.fleet_label(cfg)
        assert all(set(i) <= {'transport', 'store', 'audit', 'inbox_dir', 'clock'} for i in rec.inits)  # no fleet_name=
        assert wait_until(lambda: r.hget(channels.STATUS + 'fake:a2', 'state') == 'lease_busy')
        busy_rows = lease_busy_audits(r)
        assert len(busy_rows) == 1 and 'r-other' in json.dumps(busy_rows[0])
        actor_hash = r.hgetall(channels.ACTOR)
        assert actor_hash['pid'] == str(os.getpid()) and actor_hash.get('started')
        beats = set()
        assert wait_until(lambda: beats.add(r.hget(channels.ACTOR, 'beat')) or len(beats - {None}) >= 2)
        flipped = clock()
        alive['v'] = False
        assert finish(t, box) == 0
        assert clock() - flipped <= 5 and len(calls) == 1
        again = lease.acquire('fake', 'a1', run_id='r-check', run=str(tmp_path / 'check'))
        assert again is not None
        again.release()
    finally:
        alive['v'] = False
        t.join(5)

    # D1: drain. Inbound stops, queued items go out through P1e (routing + send wrapper), then drained=1, exit 0.
    r.delete(channels.ACTOR)
    routed = []
    real_route = getattr(actor, 'route', None)
    if real_route is not None:
        monkeypatch.setattr(actor, 'route', lambda *a, **k: routed.append(1) or real_route(*a, **k))
    seen, gate, raised = [], threading.Event(), {'n': 0}

    def on_send(ch, data):
        seen.append((dict(r.hgetall(channels.ACTOR)), data['text']))
        if data['text'] == 'one' and not gate.is_set():
            gate.wait(10)  # stuck mid-send: the beat thread must keep going
        return None
    rec.hooks['send_text'] = on_send
    real_send_text = rec.cls.send_text

    def send_text_raising_once(self, conversation, text, **kw):
        if text == 'two' and not raised['n']:
            raised['n'] += 1
            raise OSError('send blew up')
        return real_send_text(self, conversation, text, **kw)
    monkeypatch.setattr(rec.cls, 'send_text', send_text_raising_once)
    r.lpush(channels.OUTQ, json.dumps(envelope.outbound('run_closed', text='two')))
    item = dict(envelope.outbound('manual', target='explicit', channel='fake', account='a1', conversation='u-1',
                                  text='one'), id='out-1')
    r.lpush(channels.OUTQ_ACCOUNT + 'fake:a1', json.dumps(item))
    calls.clear()
    clock, alive = FakeClock(), {'v': True}
    t, box = run_actor(cli, c, parent_alive=lambda: alive['v'], clock=clock, sleep=clock.sleep)
    try:
        assert wait_until(lambda: len(calls) == 1)
        r.hset(channels.ACTOR, 'drain', str(clock() + 5))
        assert wait_until(lambda: any(text == 'one' for _, text in seen))
        stuck_beats = set()
        assert wait_until(lambda: stuck_beats.add(r.hget(channels.ACTOR, 'beat')) or len(stuck_beats - {None}) >= 2)
        gate.set()
        assert finish(t, box) == 0
    finally:
        gate.set()
        alive['v'] = False
        t.join(5)
    assert len(calls) == 1  # inbound stopped, never restarted while draining
    if real_route is not None:
        assert routed
    by_text = {}
    for fields, text in seen:
        by_text.setdefault(text, fields)
    assert set(by_text) == {'one', 'two'}
    assert by_text['one'].get('sending') == 'out-1' and by_text['one'].get('sending_at')
    assert by_text['two'].get('sending') and by_text['two'].get('sending_at')
    assert raised['n'] == 1
    final = r.hgetall(channels.ACTOR)
    assert 'sending' not in final and 'sending_at' not in final and final.get('drained') == '1'
    assert r.llen(channels.OUTQ) == 0 and r.llen(channels.OUTQ_ACCOUNT + 'fake:a1') == 0
    def blocks_forever(p):  # redis-py: brpop(keys) without a timeout IS timeout=0
        name, args, kwargs = p
        if (args[-1:] == (0,)) or kwargs.get('timeout') == 0:  # the original check, kept for every blocking call
            return True
        if 'timeout' in kwargs:
            return kwargs['timeout'] is None
        if name in ('brpop', 'blpop'):
            return len(args) < 2 or args[-1] is None
        return False
    assert not [p for p in pops if blocks_forever(p)]  # never BRPOP timeout=0
    again = lease.acquire('fake', 'a1', run_id='r-check', run=str(tmp_path / 'check'))
    assert again is not None
    again.release()
    busy.release()  # test-contract M152: a2's lease stays held through part 2 (drain), so the == 1 checks stand


def test_send_direct_is_library_call_no_subprocess(rec, tmp_path, monkeypatch):
    import subprocess

    def forbidden(*a, **k):
        pytest.fail('send_direct must be a library call, not a subprocess')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(os, 'system', forbidden)
    write_account()
    c, cfg = write_cfg(tmp_path, str(tmp_path / 'no-such.sock'))
    code, out, err = invoke(['channel', 'send', '--config', c, '--text', 'hi'])
    assert (code, json.loads(out)) == (0, {'status': 'sent'}), err

    class F:
        service = True
        notifier = None

        def task(self):
            return {}

        def log(self, *a, **k):
            pass
    F.cfg = dict(cfg, initiator=None, lifecycle='smoke',
                 channels=[{'type': 'fake', 'account': 'contract-1', 'push': ['run_closed']}])
    recs = lifecycle_close.notify(F(), 'closed', '收尾完成', {'notify_parent_session': False})
    assert [x['status'] for x in recs] == ['sent']
    assert [t[:2] for t in texts(rec)] == [('u-1', 'hi'), ('u-1', '收尾完成')]
