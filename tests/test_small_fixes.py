"""Regression tests for a batch of small fixes: each test failed before its fix."""
import json
import subprocess

import pytest

from fleetmux import channels, fleet, session_board
from fleetmux.backends import fleetmux as fleetmux_backend, wezterm as wezterm_backend
from fleetmux.channels import telegram


def _proposal(*ids):
    return [{'id': i, 'instruction': 'do something long enough', 'output': 'x'} for i in ids]


class _EscalateStub:
    cfg = {}

    def escalation_item(self, task_id, state):
        return 'worker-1', 'instruction', 'running'

    def label(self, name):
        return name


@pytest.mark.parametrize('ids', [('M0.0', 'junk.7'), ('M0.0', 'M0.x.y'), ('M0.0', 'M1.0')])
def test_escalate_refuses_a_proposal_with_any_misshapen_id(ids):
    with pytest.raises(ValueError, match='proposal ids must be distinct'):
        fleet.Fleet.escalate_check(_EscalateStub(), 'worker-1', {'escalate': 'too big', 'proposal': _proposal(*ids)},
                                   {'tasks': {}})


def test_escalation_decisions_are_spelled_in_the_past_tense():
    assert fleet.DECIDED == {'approve': 'approved', 'revise': 'revised', 'reject': 'rejected'}
    assert set(fleet.DECIDED) == set(fleet.ESCALATION_DECISIONS)


def test_claude_user_prompts_skips_rows_of_an_unexpected_shape(tmp_path):
    transcript = tmp_path / 't.jsonl'
    rows = ['[]', json.dumps({'type': 'user', 'origin': 'x', 'message': {'content': 'hi'}}),
            json.dumps({'type': 'user', 'message': 'not an object'})]
    transcript.write_text('\n'.join(rows) + '\n', encoding='utf-8')
    assert [str(p) for p in fleet.claude_user_prompts(str(transcript))] == ['hi']


@pytest.mark.parametrize('backend', [fleetmux_backend, wezterm_backend])
def test_backends_recognise_the_native_claude_exe(backend):
    assert backend._exe('/opt/claude/bin/claude.exe') == 'claude'
    assert backend._exe('claude') == 'claude'


def test_a_clean_poll_clears_a_network_failure():
    account = channels.Account('telegram', 'a', ['111'], {'BOT_TOKEN': '123456789:' + 'A' * 35})
    calls = []

    def transport(method, url, *, headers=None, body=None, timeout=30):
        calls.append(url)
        if len(calls) == 1:
            raise OSError('network down once')
        return 200, json.dumps({'ok': True, 'result': []}).encode()

    ch = telegram.TelegramChannel(account, transport=transport)
    ch.poll(0)
    assert ch.health()['state'] == 'reconnecting'
    ch.poll(0)
    assert ch.health()['state'] == 'ok'


def test_two_sessions_colliding_again_after_a_quiet_ttl_are_both_told(tmp_path, monkeypatch):
    monkeypatch.setenv('FLEETMUX_BOARD_DIR', str(tmp_path / 'board'))
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    path = str(repo / 'a.py')

    def event(sid, **kw):
        return dict(session_id=sid, cwd=str(repo), **kw)

    ttl, t0 = 7200, 1_000_000.0
    session_board.handle('claude', event('A', event='tool', path=path), now=t0)
    assert session_board.handle('claude', event('B', event='tool', path=path), now=t0 + 60)
    session_board.handle('claude', event('A', event='prompt', prompt='x'), now=t0 + 120)
    t1 = t0 + 3 * ttl
    for t in range(int(t0) + 3600, int(t1), 3600):  # both stay alive, neither touches the repo
        session_board.handle('claude', event('A', event='prompt', prompt='x'), now=t)
        session_board.handle('claude', event('B', event='prompt', prompt='x'), now=t)
    session_board.handle('claude', event('B', event='tool', path=path), now=t1)
    assert session_board.handle('claude', event('A', event='tool', path=path), now=t1 + 60)
