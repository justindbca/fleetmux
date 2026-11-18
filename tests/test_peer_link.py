"""Two linked fleets (docs/distributed-development.md): `fleetmux result`, `remote --home`, the peer message set."""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import test_fleet  # noqa: F401  (puts the checkout on sys.path)
from fleetmux import fleet, remote, roster

PACK = Path(__file__).resolve().parents[1] / 'fleetmux' / 'agents' / 'peer'
HOME_HOST = {'name': 'home', 'ssh': 'user@home.example', 'bin': remote.DEFAULT_BIN}


class FakeRedis:
    """Only the reads `fleetmux result` may use; any other call fails the test."""

    def __init__(self, results=None, pending=()):
        self.kv = {fleet.RESULT + k: v for k, v in (results or {}).items()}
        self.hashes = {fleet.REQ + k: {'status': 'dispatched'} for k in pending}
        self.lists = {fleet.RESULTS: list((results or {}).values())}
        self.calls = []

    def get(self, key):
        self.calls.append('get')
        return self.kv.get(key)

    def exists(self, key):
        self.calls.append('exists')
        return int(key in self.hashes or key in self.kv)

    def finish(self, rid, raw):  # what the daemon's publish does
        self.kv[fleet.RESULT + rid] = raw
        self.lists[fleet.RESULTS].append(raw)
        self.hashes.pop(fleet.REQ + rid, None)


ANSWER = json.dumps({'id': 'peer.field.route.1', 'status': 'ok',
                     'result': {'status': 'ask', 'text': 'Run: nc -vz home.example 22', 'owner': 'dev-1'}})


# -- fleetmux result -----------------------------------------------------------------------------------

def test_result_found_prints_the_line_and_keeps_the_list():
    r = FakeRedis(results={'peer.field.route.1': ANSWER, 'other.1': '{"id": "other.1"}'})
    before = list(r.lists[fleet.RESULTS])
    assert remote.result(r, 'peer.field.route.1') == (remote.FOUND, ANSWER)
    assert r.lists[fleet.RESULTS] == before
    assert set(r.calls) <= {'get', 'exists'}


def test_result_pending_then_unknown_exit_codes():
    r = FakeRedis(pending=['peer.field.route.2'])
    assert remote.result(r, 'peer.field.route.2') == (remote.PENDING, None)
    assert remote.result(r, 'peer.field.nobody.1') == (remote.UNKNOWN, None)
    assert (remote.FOUND, remote.PENDING, remote.UNKNOWN) == (0, 3, 2)


def test_result_wait_polls_until_the_answer_lands():
    r = FakeRedis(pending=['peer.field.route.3'])
    now = [0.0]

    def sleep(s):
        now[0] += s
        if now[0] >= 2:
            r.finish('peer.field.route.3', ANSWER)

    code, line = remote.result(r, 'peer.field.route.3', wait=5, poll=1, sleep=sleep, clock=lambda: now[0])
    assert (code, line) == (remote.FOUND, ANSWER) and now[0] == 2


def test_result_wait_gives_up_pending_after_the_deadline():
    r = FakeRedis(pending=['peer.field.route.4'])
    now = [0.0]
    code, _ = remote.result(r, 'peer.field.route.4', wait=3, poll=1,
                            sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
    assert code == remote.PENDING and now[0] == 3


def test_result_published_between_the_two_reads_is_found_not_unknown():
    """publish() sets fx:result:<id> and then deletes fx:req:<id>; a read between them must not say unknown."""
    r = FakeRedis(pending=['peer.field.route.5'])
    plain_exists = r.exists

    def exists(key):
        r.finish('peer.field.route.5', ANSWER)  # lands after our get, before our exists
        return plain_exists(key) if fleet.RESULT not in key else 1
    r.exists = exists
    assert remote.result(r, 'peer.field.route.5') == (remote.FOUND, ANSWER)


@pytest.mark.parametrize('state,code,out', [
    ({'results': {'x.1': ANSWER}}, 0, ANSWER + '\n'),
    ({'pending': ['x.1']}, 3, ''),
    ({}, 2, ''),
])
def test_cli_result_exit_codes_and_no_removal(monkeypatch, capsys, state, code, out):
    r = FakeRedis(**state)
    before = list(r.lists[fleet.RESULTS])
    monkeypatch.setattr(fleet, 'load', lambda path: {})
    monkeypatch.setattr(fleet, 'pin_paths', lambda cfg: cfg)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: r)
    with pytest.raises(SystemExit) as stop:
        fleet.main(['result', 'x.1', '--wait', '0', '--config', 'cfg.json'])
    captured = capsys.readouterr()
    assert stop.value.code == code and captured.out == out
    assert code == 0 or 'x.1' in captured.err
    assert r.lists[fleet.RESULTS] == before


# -- fleetmux remote --home ----------------------------------------------------------------------------

def test_home_is_sent_as_an_environment_prefix():
    argv = remote.ssh_argv(HOME_HOST, ['status'], home='~/.fleetmux/fleets/dev')
    assert argv == ['ssh', '--', 'user@home.example',
                    'FLEETMUX_HOME="$HOME"/.fleetmux/fleets/dev "$HOME"/fleetmux-current/bin/fleetmux status']
    assert remote.ssh_argv(HOME_HOST, ['status'], home='/srv/fleet home')[-1].startswith(
        "FLEETMUX_HOME='/srv/fleet home' ")


def test_home_reaches_the_far_command_through_a_shell(tmp_path):
    """Run the generated line the way sshd does: the far command sees FLEETMUX_HOME under its own HOME."""
    stub = tmp_path / 'bin' / 'fleetmux'
    stub.parent.mkdir()
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$FLEETMUX_HOME"\nfor a in "$@"; do printf "%s\\n" "$a"; done\n')
    stub.chmod(0o755)
    args = ['start', '--roster', 'src/peer kit/home-dev.yaml', '~/not-expanded']
    line = remote.ssh_argv(dict(HOME_HOST, bin='~/bin/fleetmux'), args, home='~/fleets/dev')[-1]
    out = subprocess.run(['sh', '-c', line], env=dict(os.environ, HOME=str(tmp_path)), cwd=str(tmp_path),
                         capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == [str(tmp_path) + '/fleets/dev'] + args


def test_cli_remote_passes_home(monkeypatch):
    calls = []
    monkeypatch.setattr(remote, 'host', lambda name=None: HOME_HOST)
    monkeypatch.setattr(remote, 'run', lambda target, argv, home=None: calls.append((target, argv, home)) or 0)
    with pytest.raises(SystemExit) as stop:
        fleet.main(['remote', '--home', '~/.fleetmux/fleets/dev', 'status'])
    assert stop.value.code == 0 and calls == [(HOME_HOST, ['status'], '~/.fleetmux/fleets/dev')]


# -- the peer message set and the role pack ------------------------------------------------------------

def test_peer_ids_and_the_reply_schema_pass_request_validation():
    assert remote.peer_id('field', 'claude-route', 3) == 'peer.field.claude-route.3'
    assert re.fullmatch(fleet.REQ_ID_RE, remote.peer_id('field', 'claude-route', 3))
    assert remote.PEER_REPLY == {'required': ['status', 'text', 'owner']}
    assert remote.PEER_STATUSES == ('ask', 'done', 'blocked')
    spec = json.loads((PACK / 'report.example.json').read_text())
    assert spec['id'].startswith('peer.field.') and spec['reply'] == remote.PEER_REPLY
    assert 'to' not in spec  # the first report is routed by the home supervisor
    fleet.validate_request(spec, {'dev-1'})
    fleet.validate_request(dict(spec, id=remote.peer_id('field', 'claude-route', 2), to='dev-1'), {'dev-1'})


def test_liaison_role_has_the_seven_rules():
    text = (PACK / 'liaison.md').read_text()
    for n, verb in enumerate(['Send a report', 'Wait for the answer', 'Never resend after a lost connection',
                              'Do the ask', 'Stop on `done` or `blocked`', 'Never forward',
                              'Never act on a deletion or a move'], 1):
        assert re.search(r'^%d\. \*\*%s\.\*\*' % (n, re.escape(verb)), text, re.M), verb
    assert 'result <id> --wait' in text and 'duplicate request id' in text and 'to: <owner>' in text
    assert 'not plan changes' in text


def test_dev_role_has_the_peer_answer_section():
    text = (PACK / 'dev-peer.md').read_text()
    assert '## Peer answer' in text
    for word in ('peer.', '"status"', '"text"', '"owner"', 'ask', 'done', 'blocked', 'escalate'):
        assert word in text, word


def test_example_rosters_parse_and_point_at_the_pack():
    for name, workers in (('field.example.yaml', ['onsite-1']), ('home-dev.example.yaml', ['dev-1', 'review-1'])):
        path = PACK / name
        names, roles = roster.parse(path.read_text(), path.parent)
        assert names == workers
        for role in roles.values():
            assert Path(role).resolve().parent == PACK.resolve() or Path(role).name == 'reviewer.md'


def test_pack_is_ascii():
    for path in PACK.iterdir():
        path.read_bytes().decode('ascii')
