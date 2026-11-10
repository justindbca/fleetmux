"""exec push channel (P6a): brief v2 §6.13 + approve-1 test-plan §4.1 / S1 (rulings R1 R2 R12).

The adapter hands every outbound item, as JSON on stdin, to the command in the account file's ``COMMAND=``
(``shlex.split`` -> argv, ``shell=False``). Real child processes here are only ``sys.executable`` running a
dump script written into ``tmp_path``; no network, no credentials, no listener, nothing deleted.
"""
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from cmux_fleet import channels
from cmux_fleet.channels import base, envelope


def _ok_transport():
    def transport(argv, stdin, timeout):
        transport.calls.append((list(argv), stdin, timeout))
        return 0, 'ex-1\n'
    transport.calls = []
    return transport


# Read by tests/test_channels_contract.py (the adapter contract rows red:P6-exec).
CONTRACT = {
    'account': {'ALLOW': 'u-1', 'COMMAND': '/usr/local/bin/fake-relay --to push-1', 'TIMEOUT_S': '30'},
    'conversation': 'push-1',
    'transport': _ok_transport,
    'inbound_ok': None,
    'inbound_text': None,
    'inbound_foreign': None,
    'inbound_group': None,
}

DUMP = r'''
import json, os, sys, time
out = sys.argv[1]
mode = sys.argv[2]
data = sys.stdin.buffer.read().decode('utf-8')
with open(os.path.join(out, 'stdin.jsonl'), 'a', encoding='utf-8') as fh:
    fh.write(data.strip() + '\n')
with open(os.path.join(out, 'argv.json'), 'w', encoding='utf-8') as fh:
    json.dump(sys.argv, fh)
with open(os.path.join(out, 'env.json'), 'w', encoding='utf-8') as fh:
    json.dump({'FLEETMUX_TEST_EXEC_ENV': os.environ.get('FLEETMUX_TEST_EXEC_ENV')}, fh)
if mode == 'id':
    sys.stdout.write('ex-42\nnoise line\n')
elif mode == 'json-id':
    sys.stdout.write(json.dumps({'message_id': 'ex-7'}) + '\n')
elif mode == 'fail':
    sys.stderr.write('relay said no\n')
    sys.exit(3)
elif mode == 'sleep':
    time.sleep(5)
'''


class Audit(list):
    def __call__(self, event, **fields):
        self.append((event, fields))


class Recording(subprocess.Popen):
    """subprocess.Popen that records how it was called (subprocess.run builds one too)."""
    calls = []

    def __init__(self, *args, **kwargs):
        Recording.calls.append((args, kwargs))
        super().__init__(*args, **kwargs)


@pytest.fixture
def popen_calls(monkeypatch):
    Recording.calls = []
    monkeypatch.setattr(subprocess, 'Popen', Recording)
    return Recording.calls


@pytest.fixture
def dump(tmp_path):
    script = tmp_path / 'dump.py'
    script.write_text(DUMP, encoding='utf-8')
    out = tmp_path / 'out'
    out.mkdir()
    return script, out


def command(dump, mode='ok', *extra):
    script, out = dump
    return shlex.join([sys.executable, str(script), str(out), mode]) + (' ' + ' '.join(extra) if extra else '')


def build(command_line, *, timeout=None, transport=None, extra_values=''):
    text = 'ALLOW=u-1\nCOMMAND=' + command_line + '\n' + ('TIMEOUT_S=' + str(timeout) + '\n' if timeout else '') + extra_values
    account = channels.parse_account_text('exec', 'relay-1', text)
    cls = channels.adapter_class('exec')
    audit = Audit()
    ch = cls(account, transport=transport, store=base.MemoryStore(), audit=audit)
    return ch, audit


def stdin_items(dump):
    path = dump[1] / 'stdin.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def approval_card():
    return envelope.card('删除确认 worker-1', 'rm -rf build/tmp',
                         [{'label': '批准', 'command': '/approve p-0123abcd 0123456789abcdef'},
                          {'label': '拒绝', 'command': '/reject p-0123abcd'}], ref='p-0123abcd')


# ---------------------------------------------------------------------------------------------- brief v2 §6.13
def test_contract_dict_and_adapter_class_RECEIVES_false():
    assert {'account', 'conversation', 'transport', 'inbound_ok', 'inbound_text', 'inbound_foreign',
            'inbound_group'} <= set(CONTRACT)
    assert all(CONTRACT[k] is None for k in ('inbound_ok', 'inbound_text', 'inbound_foreign', 'inbound_group'))
    cls = channels.adapter_class('exec')
    assert issubclass(cls, base.Channel) and cls.type == 'exec'
    # R1: structured cards and files, no buttons, no in-place update, never a token carrier, never receives
    assert cls.RECEIVES is False and cls.CARRIES_TOKENS is False
    assert cls.SUPPORTS_CARDS is True and cls.SUPPORTS_FILES is True
    assert cls.SUPPORTS_BUTTONS is False and cls.SUPPORTS_UPDATE is False
    assert 100 <= cls.TEXT_LIMIT <= 100000
    ch, _ = build(CONTRACT['account']['COMMAND'], transport=_ok_transport())
    assert ch.inbound({'anything': 1}) == [] and ch.poll(0) == []
    assert ch.health()['state'] in channels.INBOUND_STATES


def test_send_item_writes_outbound_json_on_stdin_exit0_ok(dump):
    ch, _ = build(command(dump))
    item = envelope.outbound('result', target='explicit', channel='exec', account='relay-1', conversation='push-1',
                             text='worker-1 finished: OK', request_id='r-1')
    got = ch.send_item(item)
    assert got.ok, got.error
    sent, = stdin_items(dump)
    assert envelope.outbound_problem(sent) == ''
    assert sent['event'] == 'result' and sent['text'] == 'worker-1 finished: OK' and sent['request_id'] == 'r-1'
    assert sent['id'] == item['id']


def test_nonzero_timeout_missing_command_fail_without_raise(dump, tmp_path):
    cases = [build(command(dump, 'fail')),                                   # exit 3
             build(command(dump, 'sleep'), timeout=1),                       # killed after TIMEOUT_S
             build(str(tmp_path / 'no-such-relay') + ' --x 1'),              # cannot start
             build("/usr/local/bin/relay --x 'unbalanced")]                  # shlex refuses: never a shell
    for ch, audit in cases:
        got = ch.send_text('push-1', 'hello')
        assert got.ok is False and got.error
        assert ch.last_error
        assert 'unbalanced' not in got.error and 'unbalanced' not in ch.last_error  # the value is never echoed


def test_stdout_first_line_is_message_id(dump):
    ch, _ = build(command(dump, 'id'))
    got = ch.send_text('push-1', 'hello')
    assert got.ok and got.message_id == 'ex-42'
    ch, _ = build(command(dump, 'json-id'))
    got = ch.send_text('push-1', 'hello')
    assert got.ok and got.message_id == 'ex-7'


def test_argv_from_COMMAND_shlex_not_shell(dump, tmp_path, popen_calls):
    side = tmp_path / 'SIDE-EFFECT'
    line = command(dump, 'ok') + " --x 'a b;touch " + str(side) + "' '$HOME' '~/x' '*'"
    ch, _ = build(line)
    got = ch.send_text('push-1', 'hello')
    assert got.ok, got.error
    argv = json.loads((dump[1] / 'argv.json').read_text(encoding='utf-8'))
    assert argv == shlex.split(line)[1:]                       # exactly shlex.split, word for word
    assert 'a b;touch ' + str(side) in argv and '$HOME' in argv and '~/x' in argv and '*' in argv
    assert not side.exists()                                       # no shell: ';' ran nothing
    assert popen_calls, 'the command must run as a child process'
    for args, kwargs in popen_calls:
        assert not kwargs.get('shell') and isinstance(args[0] if args else kwargs.get('args'), list)
    assert channels.exec_argv(ch.account.values['COMMAND']) == shlex.split(line)


def test_send_text_card_file_wrap_into_manual_items(dump, tmp_path):
    ch, _ = build(command(dump))
    attachment = tmp_path / 'result.txt'
    attachment.write_text('long result\n')
    assert ch.send_text('push-1', 'plain text').ok
    assert ch.send_card('push-1', approval_card()).ok
    assert ch.send_file('push-1', attachment, caption='the result').ok
    assert ch.update_card('push-1', 'ex-1', approval_card()).ok  # SUPPORTS_UPDATE False: a new card item
    items = stdin_items(dump)
    assert [i['event'] for i in items] == ['manual'] * 4
    assert all(envelope.outbound_problem(i) == '' for i in items)
    bodies = [[k for k in ('text', 'card', 'file') if i.get(k) is not None] for i in items]
    assert bodies == [['text'], ['card'], ['file'], ['card']]
    assert items[0]['text'] == 'plain text' and items[0]['conversation'] == 'push-1'
    for card_item in (items[1], items[3]):
        assert isinstance(card_item['card'], dict)                 # structured, not spelled out as text
        assert card_item['card']['title'] == '删除确认 worker-1' and card_item['card']['text'] == 'rm -rf build/tmp'
        assert card_item['card']['buttons'] == [] and card_item['card']['ref'] == ''
    assert items[2]['file'] == str(attachment) and items[2]['caption'] == 'the result'
    raw = (dump[1] / 'stdin.jsonl').read_text(encoding='utf-8')
    assert '/approve' not in raw and 'p-0123abcd' not in raw and '0123456789abcdef' not in raw


def test_timeout_default_30_from_TIMEOUT_S():
    t = _ok_transport()
    ch, _ = build('/usr/local/bin/fake-relay --to push-1', transport=t)
    assert ch.send_text('push-1', 'hello').ok
    argv, stdin, timeout = t.calls[-1]
    assert timeout == channels.EXEC_TIMEOUT_S == 30
    assert argv == ['/usr/local/bin/fake-relay', '--to', 'push-1'] and isinstance(stdin, (bytes, bytearray))
    t = _ok_transport()
    ch, _ = build('/usr/local/bin/fake-relay', timeout=5, transport=t)
    assert ch.send_text('push-1', 'hello').ok and t.calls[-1][2] == 5


def test_env_inherited_no_values_in_argv(dump, monkeypatch):
    monkeypatch.setenv('FLEETMUX_TEST_EXEC_ENV', 'inherited')
    line = command(dump, 'ok')
    ch, _ = build(line, extra_values='RELAY_TOKEN=tok-secret-123456\nPUSH_TO=push-1\n')
    assert ch.send_text('push-1', 'hello').ok
    assert json.loads((dump[1] / 'env.json').read_text())['FLEETMUX_TEST_EXEC_ENV'] == 'inherited'
    argv = json.loads((dump[1] / 'argv.json').read_text(encoding='utf-8'))
    assert argv == shlex.split(line)[1:]                       # only COMMAND's words, no other account value
    assert 'tok-secret-123456' not in json.dumps(argv)
    ch, audit = build(command(dump, 'fail', '--key', 'tok-secret-123456'))
    got = ch.send_text('push-1', 'hello')
    assert not got.ok
    text = json.dumps([got.error, ch.last_error, audit, ch.health()], ensure_ascii=False, default=str)
    assert 'tok-secret-123456' not in text                         # a failing COMMAND is never echoed


# ---------------------------------------------------------------------------------------------- test-plan S1 (R2)
def test_send_item_redacts_card_and_refuses_user_only_items(dump, popen_calls):
    ch, _ = build(command(dump))
    card_item = envelope.outbound('prompt_wait', target='explicit', channel='exec', account='relay-1',
                                  conversation='push-1', card=approval_card())
    got = ch.send_item(card_item)
    assert got.ok, got.error
    sent, = stdin_items(dump)
    assert sent['card']['buttons'] == [] and sent['card']['ref'] == ''
    assert sent['card']['title'] == '删除确认 worker-1' and sent['card']['text'] == 'rm -rf build/tmp'
    raw = (dump[1] / 'stdin.jsonl').read_text(encoding='utf-8')
    assert 'p-0123abcd' not in raw and '0123456789abcdef' not in raw and '/approve' not in raw
    started = len(popen_calls)
    user_only = envelope.outbound('prompt_wait', target='last', card=approval_card(), user_only=True)
    got = ch.send_item(user_only)
    assert got.ok is False and 'user_only' in got.error
    assert len(popen_calls) == started and len(stdin_items(dump)) == 1   # no child process for it
    assert 'user_only' in ch.last_error
