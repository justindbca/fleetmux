"""`fleetmux acp` (fleetmux/acp_server.py): a fake ACP client drives the server over pipes.

Submission, status and results are stubbed (FakeOps): no test reaches a running fleet. One test runs the real
`fleet.py acp` in a subprocess against an empty, isolated fleet home (no root fleet: the error path).
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fleetmux import acp_server

JID = 'job-0123456789'
ROOT = {'config': '/fleets/root/run/config.json', 'mode': 'service'}
FAST = dict(poll_s=0.01, heartbeat_s=10.0, cancel_retry_s=0.01)


class FakeOps:
    """Stands in for the root fleet: find_root / submit / view / result / cancel, all recorded."""

    def __init__(self, root=ROOT, views=None, results=None, cancels=None, submit_error=None, beats=None,
                 daemon_alive=True):
        self.root = root
        self.views = list(views or [])        # (row, item) per poll; the last one repeats
        self.beats = list(beats or [])        # the root daemon's heartbeat age (s) per poll; the last one repeats
        self.daemon_alive = daemon_alive      # what the daemon.lock probe answers
        self.probes = []
        self.results = list(results or [])    # (code, data) per poll; the last one repeats
        self.cancels = list(cancels or [])    # dicts per cancel call; the last one repeats
        self.submit_error = submit_error
        self.submitted, self.cancelled, self.calls = [], [], []

    def find_root(self):
        self.calls.append('find_root')
        return self.root

    def submit(self, root, spec):
        self.calls.append('submit')
        if self.submit_error:
            raise acp_server.JoinError(self.submit_error)
        self.submitted.append((root, spec))
        return JID

    def _next(self, items, default):
        if not items:
            return default
        return items.pop(0) if len(items) > 1 else items[0]

    def view(self, root, jid):
        row, item = self._next(self.views, ({'id': jid, 'state': 'running', 'subfleet': 'M1', 'slot': 'worker-2'},
                                            {'state': 'running', 'run': '/r/sub/M1'}))
        return row, item, self._next(self.beats, 1.0)

    def daemon_running(self, root):
        self.probes.append(root)
        return self.daemon_alive

    def result(self, root, jid):
        return self._next(self.results, (3, {}))

    def cancel(self, root, jid):
        self.cancelled.append((root, jid))
        return self._next(self.cancels, {'job': jid, 'state': 'stopping', 'stopped': True})


class Client:
    """A fake ACP client: newline-delimited JSON-RPC over two pipes to an in-process server."""

    def __init__(self, ops, **kw):
        c2s_r, c2s_w = os.pipe()
        s2c_r, s2c_w = os.pipe()
        self.to_server = os.fdopen(c2s_w, 'w', buffering=1)
        self.from_server = os.fdopen(s2c_r, 'r')
        self.server = acp_server.AcpServer(ops, **{**FAST, **kw})
        self.inbox = queue.Queue()
        self.ids = 0
        srv_in, srv_out = os.fdopen(c2s_r, 'r'), os.fdopen(s2c_w, 'w', buffering=1)

        def serve():
            self.server.serve(srv_in, srv_out)
            srv_out.close()
        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.from_server:
            self.inbox.put(json.loads(line))
        self.inbox.put(None)

    def send(self, method, params=None, notify=False):
        msg = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if not notify:
            self.ids += 1
            msg['id'] = self.ids
        self.to_server.write(json.dumps(msg) + '\n')
        return msg.get('id')

    def until(self, rid, timeout=10):
        """Every message up to and including the response to ``rid``."""
        out, deadline = [], time.time() + timeout
        while True:
            msg = self.inbox.get(timeout=max(deadline - time.time(), 0.01))
            assert msg is not None, 'server closed: ' + json.dumps(out)[:2000]
            out.append(msg)
            if msg.get('id') == rid and 'method' not in msg:
                return out

    def call(self, method, params=None):
        return self.until(self.send(method, params))[-1]

    def session(self):
        self.call('initialize', {'protocolVersion': 1, 'clientCapabilities': {}})
        return self.call('session/new', {'cwd': '/tmp', 'mcpServers': []})['result']['sessionId']

    def prompt(self, sid, text, timeout=10):
        rid = self.send('session/prompt', {'sessionId': sid, 'prompt': [{'type': 'text', 'text': text}]})
        return self.until(rid, timeout)


def chunks(msgs, kind='agent_message_chunk'):
    return [m['params']['update']['content']['text'] for m in msgs
            if m.get('method') == 'session/update' and m['params']['update'].get('sessionUpdate') == kind]


def final(msgs):
    return msgs[-1]['result']['stopReason']


DONE_OK = (0, {'id': JID, 'status': 'ok', 'result': {
    'job': JID, 'outcome': 'completed', 'reason': 'all checks passed', 'run': '/r/sub/M1',
    'workspace': '/r/sub/M1/workspace', 'artifacts': ['worker-1/result.md']}})


# ------------------------------------------------------------------ handshake and unsupported methods
def test_initialize_session_new_and_authenticate():
    c = Client(FakeOps())
    init = c.call('initialize', {'protocolVersion': 1, 'clientCapabilities': {'fs': {}}})['result']
    assert init['protocolVersion'] == 1
    assert init['agentCapabilities']['loadSession'] is False
    assert init['authMethods'] == []
    assert c.call('authenticate', {'methodId': 'none'})['result'] == {}
    sid = c.call('session/new', {'cwd': '/tmp', 'mcpServers': [{'name': 'x'}]})['result']['sessionId']
    assert isinstance(sid, str) and sid


@pytest.mark.parametrize('method', ['session/set_model', 'session/set_config_option', 'session/load', 'x/unknown'])
def test_unsupported_methods_answer_an_error_not_a_crash(method):
    c = Client(FakeOps())
    sid = c.session()
    answer = c.call(method, {'sessionId': sid, 'modelId': 'm', 'configId': 'c', 'value': 'v'})
    assert answer['error']['code'] == -32601
    assert 'not supported' in answer['error']['message']
    assert c.call('authenticate', {})['result'] == {}  # still serving


def test_bad_json_line_gets_a_parse_error_and_the_server_keeps_serving():
    c = Client(FakeOps())
    c.to_server.write('{not json\n')
    msg = c.inbox.get(timeout=5)
    assert msg['error']['code'] == -32700
    assert c.call('authenticate', {})['result'] == {}


# ------------------------------------------------------------------ prompt -> job -> result
def test_prompt_submits_a_job_streams_progress_and_ends_with_the_program_check():
    ops = FakeOps(views=[({'id': JID, 'state': 'queued', 'waiting': 'no idle worker'}, None),
                         ({'id': JID, 'state': 'running', 'subfleet': 'M1', 'slot': 'worker-2'},
                          {'state': 'running', 'run': '/r/sub/M1'})],
                  results=[(3, {}), (3, {}), (3, {}), DONE_OK])
    c = Client(ops)
    msgs = c.prompt(c.session(), 'Survey three vendor APIs')
    assert final(msgs) == 'end_turn'
    (root, spec), = ops.submitted
    assert root == ROOT['config']
    assert spec['goal'].startswith('Survey three vendor APIs')
    assert spec['artifacts'] == [{'path': 'worker-1/result.md', 'min_chars': 300}]
    assert acp_server.DEFAULT_NOTE in spec['goal']
    text = chunks(msgs)
    assert any(JID in t and 'accepted' in t for t in text)
    assert any('queued' in t and 'no idle worker' in t for t in text)
    assert any('running' in t and 'M1' in t for t in text)
    last = ''.join(text).rstrip().split('\n\n')[-1]  # the verdict is the last paragraph
    assert 'OK' in last and JID in last and '/r/sub/M1/workspace' in last and '/r/sub/M1' in last


@pytest.mark.parametrize('status,word', [('blocked', 'BLOCKED'), ('failed', 'FAILED')])
def test_blocked_and_failed_results_name_the_failed_checks(status, word):
    done = (0, {'id': JID, 'status': status, 'error': 'job sub-fleet M1 ended ' + status,
                'result': {'outcome': status, 'reason': 'artifact below requirements: worker-1/result.md',
                           'run': '/r/sub/M1', 'workspace': '/r/sub/M1/workspace'}})
    c = Client(FakeOps(results=[(3, {}), done]))
    msgs = c.prompt(c.session(), 'Do the thing')
    assert final(msgs) == 'end_turn'
    last = ''.join(chunks(msgs)).rstrip().split('\n\n')[-1]
    assert word in last and 'artifact below requirements: worker-1/result.md' in last
    assert 'ended ' + status in last


def test_heartbeat_while_the_state_does_not_change():
    ops = FakeOps(results=[(3, {})] * 40 + [DONE_OK])
    c = Client(ops, heartbeat_s=0.05)
    msgs = c.prompt(c.session(), 'Long job')
    beats = chunks(msgs, 'agent_thought_chunk')
    assert len(beats) >= 2 and all(JID in b for b in beats)
    assert final(msgs) == 'end_turn'


def test_default_heartbeat_interval_is_inside_the_omnigent_idle_deadline():
    assert 60 <= acp_server.HEARTBEAT_S <= 120 and acp_server.POLL_S < acp_server.HEARTBEAT_S


def test_result_md_excerpt_comes_before_the_verdict(tmp_path):
    (tmp_path / 'worker-1').mkdir()
    (tmp_path / 'worker-1' / 'result.md').write_text('THE ANSWER ' * 40)
    done = (0, {'id': JID, 'status': 'ok', 'result': {'outcome': 'completed', 'reason': 'ok',
                                                     'run': str(tmp_path.parent), 'workspace': str(tmp_path)}})
    c = Client(FakeOps(results=[done]))
    text = ''.join(chunks(c.prompt(c.session(), 'Answer it')))
    assert 'THE ANSWER' in text
    assert text.index('THE ANSWER') < text.rindex('OK')
    assert 'THE ANSWER' not in text.rstrip().split('\n\n')[-1]


# ------------------------------------------------------------------ errors: no root, bad fenced JSON
def test_no_root_fleet_answers_a_visible_error_with_the_start_command_and_submits_nothing():
    ops = FakeOps(root=None)
    c = Client(ops)
    msgs = c.prompt(c.session(), 'Anything')
    text = chunks(msgs)
    assert text and text[0].startswith('ERROR')
    assert 'fleetmux start' in text[0]
    assert final(msgs) == 'end_turn'
    assert ops.submitted == [] and 'submit' not in ops.calls


def test_a_refused_job_is_a_visible_error():
    c = Client(FakeOps(submit_error='the root refused the job: no worker runs Claude Code'))
    msgs = c.prompt(c.session(), 'Anything')
    assert chunks(msgs)[0].startswith('ERROR') and 'Claude Code' in chunks(msgs)[0]
    assert final(msgs) == 'end_turn'


def test_trailing_fenced_json_sets_job_keys_and_leaves_the_goal():
    ops = FakeOps(results=[DONE_OK])
    c = Client(ops)
    c.prompt(c.session(), 'Write the report\n\n```json\n{"hours": 2, "workers": 1, "lifecycle": "smoke"}\n```\n')
    (_, spec), = ops.submitted
    assert spec['hours'] == 2 and spec['workers'] == 1 and spec['lifecycle'] == 'smoke'
    assert spec['goal'].startswith('Write the report') and '```' not in spec['goal']


def test_declared_artifacts_replace_the_default_and_its_note():
    ops = FakeOps(results=[DONE_OK])
    c = Client(ops)
    arts = [{'path': 'worker-2/r.md', 'min_chars': 800, 'contains': ['Conclusion']}]
    c.prompt(c.session(), 'Report\n```json\n' + json.dumps({'artifacts': arts}) + '\n```')
    (_, spec), = ops.submitted
    assert spec['artifacts'] == arts and acp_server.DEFAULT_NOTE not in spec['goal']


def test_empty_artifacts_count_as_not_declared():
    ops = FakeOps(results=[DONE_OK])
    c = Client(ops)
    c.prompt(c.session(), 'Report\n```json\n{"artifacts": []}\n```')
    (_, spec), = ops.submitted
    assert spec['artifacts'] == [acp_server.DEFAULT_ARTIFACT]


def test_a_json_block_inside_the_goal_is_not_a_declaration():
    ops = FakeOps(results=[DONE_OK])
    c = Client(ops)
    c.prompt(c.session(), 'Parse this:\n```json\n{"bogus": 1}\n```\nand write a summary')
    (_, spec), = ops.submitted
    assert '"bogus"' in spec['goal'] and 'bogus' not in spec


@pytest.mark.parametrize('block,needle', [
    ('{"bogus": 1}', 'bogus'),
    ('{"overrides": {}}', 'overrides'),
    ('{"goal": "x"}', 'goal'),
    ('[1, 2]', 'object'),
    ('{"hours": ', 'JSON'),
    ('{"roster": "/abs/roster.yaml"}', 'artifacts'),
])
def test_invalid_fenced_json_is_a_visible_error_listing_the_allowed_keys(block, needle):
    ops = FakeOps()
    c = Client(ops)
    msgs = c.prompt(c.session(), 'Goal text\n```json\n' + block + '\n```')
    text = chunks(msgs)
    assert text[0].startswith('ERROR') and needle in text[0]
    assert 'hours' in text[0] and 'artifacts' in text[0]  # the allowed keys are listed
    assert final(msgs) == 'end_turn' and ops.submitted == [] and 'find_root' not in ops.calls


def test_an_empty_goal_is_an_error():
    ops = FakeOps()
    c = Client(ops)
    msgs = c.prompt(c.session(), '```json\n{"hours": 1}\n```')
    assert chunks(msgs)[0].startswith('ERROR') and ops.submitted == []


# ------------------------------------------------------------------ Omnigent's prompt wrapper (0.22.0)
SYSTEM = ('You are a helpful coding agent running through Omnigent.\n\nExample:\n```json\n{"bogus": true}\n```\n\n'
          + acp_server.OMNIGENT_LAST_SYSTEM_PART)


def test_first_turn_system_prompt_is_stripped_before_looking_for_fenced_json():
    ops = FakeOps(results=[DONE_OK])
    c = Client(ops)
    c.prompt(c.session(), SYSTEM + '\n\nBuild the export\n```json\n{"hours": 3}\n```')
    (_, spec), = ops.submitted
    assert spec['goal'].startswith('Build the export') and 'Omnigent' not in spec['goal']
    assert spec['hours'] == 3


def test_first_turn_system_prompt_only_json_is_not_taken_as_a_declaration():
    ops = FakeOps(results=[DONE_OK])
    c = Client(ops)
    msgs = c.prompt(c.session(), SYSTEM + '\n\nBuild the export')
    (_, spec), = ops.submitted
    assert spec['goal'].startswith('Build the export') and 'bogus' not in spec and 'bogus' not in spec['goal']
    assert not chunks(msgs)[0].startswith('ERROR')


def test_first_turn_with_replayed_history_keeps_only_the_latest_user_message():
    history = ('Conversation so far:\nuser: old goal\n```json\n{"bogus": 1}\n```\nassistant: done\n\n'
               'Respond to the latest user message, using the conversation above as context.')
    ops = FakeOps(results=[DONE_OK])
    c = Client(ops)
    c.prompt(c.session(), SYSTEM + '\n\n' + history + '\n\nuser: New goal\n```json\n{"workers": 2}\n```')
    (_, spec), = ops.submitted
    assert spec['goal'].startswith('New goal') and 'old goal' not in spec['goal']
    assert spec['workers'] == 2 and 'bogus' not in spec


def test_history_without_a_system_prompt_is_stripped_too():
    history = ('Conversation so far:\nuser: old\n\n'
               'Respond to the latest user message, using the conversation above as context.')
    ops = FakeOps(results=[DONE_OK])
    c = Client(ops)
    c.prompt(c.session(), history + '\n\nuser: Fresh goal')
    assert ops.submitted[0][1]['goal'].startswith('Fresh goal')


def test_later_turns_are_taken_as_typed():
    ops = FakeOps(results=[DONE_OK])
    c = Client(ops)
    sid = c.session()
    c.prompt(sid, SYSTEM + '\n\nFirst goal')
    second = 'Second goal mentioning ' + acp_server.OMNIGENT_LAST_SYSTEM_PART + '\n\nand more'
    c.prompt(sid, second)
    assert ops.submitted[0][1]['goal'].startswith('First goal')
    assert ops.submitted[1][1]['goal'].startswith(second)


def test_no_wrapper_text_is_taken_whole():
    ops = FakeOps(results=[DONE_OK])
    c = Client(ops)
    c.prompt(c.session(), 'Plain goal\n\nwith two paragraphs')
    assert ops.submitted[0][1]['goal'].startswith('Plain goal\n\nwith two paragraphs')


# ------------------------------------------------------------------ cancel and process exit
def _wait_for(pred, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_cancel_stops_only_this_job_and_ends_the_turn_cancelled():
    ops = FakeOps(results=[(3, {})])
    c = Client(ops)
    sid = c.session()
    rid = c.send('session/prompt', {'sessionId': sid, 'prompt': [{'type': 'text', 'text': 'Long'}]})
    assert _wait_for(lambda: ops.submitted)
    c.send('session/cancel', {'sessionId': sid}, notify=True)
    msgs = c.until(rid)
    assert final(msgs) == 'cancelled'
    assert ops.cancelled == [(ROOT['config'], JID)]
    assert any('stopping' in t and JID in t for t in chunks(msgs))


def test_cancel_of_a_queued_job_says_it_is_not_stopped_yet_and_retries_by_job_id():
    ops = FakeOps(views=[({'id': JID, 'state': 'queued'}, None)], results=[(3, {})],
                  cancels=[{'job': JID, 'state': 'queued', 'stopped': False, 'pending': True}] * 3
                  + [{'job': JID, 'state': 'stopping', 'stopped': True}])
    c = Client(ops)
    sid = c.session()
    rid = c.send('session/prompt', {'sessionId': sid, 'prompt': [{'type': 'text', 'text': 'Long'}]})
    assert _wait_for(lambda: ops.submitted)
    c.send('session/cancel', {'sessionId': sid}, notify=True)
    msgs = c.until(rid)
    assert final(msgs) == 'cancelled'
    text = ''.join(chunks(msgs))
    assert 'still queued' in text and 'not been stopped' in text
    assert 'fleetmux cancel ' + JID in text
    assert _wait_for(lambda: len(ops.cancelled) >= 4)  # retried until its sub-fleet could be stopped
    assert all(j == JID for _, j in ops.cancelled)
    assert _wait_for(lambda: JID not in c.server.pending_cancels)
    n = len(ops.cancelled)
    time.sleep(0.1)
    assert len(ops.cancelled) == n  # stopped: no more retries


def test_cancel_registry_is_by_job_id_not_by_session():
    ops = FakeOps(results=[(3, {})], cancels=[{'job': JID, 'state': 'queued', 'stopped': False, 'pending': True}])
    c = Client(ops, cancel_retry_s=3600)
    sid = c.session()
    rid = c.send('session/prompt', {'sessionId': sid, 'prompt': [{'type': 'text', 'text': 'Long'}]})
    assert _wait_for(lambda: ops.submitted)
    c.send('session/cancel', {'sessionId': sid}, notify=True)
    c.until(rid)
    assert list(c.server.pending_cancels) == [JID]
    assert c.server.pending_cancels[JID] == ROOT['config']


def test_cancel_without_a_running_prompt_is_ignored():
    ops = FakeOps()
    c = Client(ops)
    sid = c.session()
    c.send('session/cancel', {'sessionId': sid}, notify=True)
    assert c.call('authenticate', {})['result'] == {}
    assert ops.cancelled == []


def test_process_exit_does_not_withdraw_the_job():
    ops = FakeOps(results=[(3, {})])
    c = Client(ops)
    sid = c.session()
    c.send('session/prompt', {'sessionId': sid, 'prompt': [{'type': 'text', 'text': 'Long'}]})
    assert _wait_for(lambda: ops.submitted)
    c.to_server.close()  # Omnigent closed the session: stdin EOF
    c.thread.join(timeout=5)
    assert not c.thread.is_alive()
    time.sleep(0.1)
    assert ops.cancelled == [] and len(ops.submitted) == 1


# ------------------------------------------------------------------ the root daemon dies under a running turn
STUCK = [({'id': JID, 'state': 'starting', 'slot': 'worker-1', 'subfleet': 'M0'}, None)]


def test_root_daemon_gone_ends_the_turn_with_a_visible_error_and_keeps_the_job():
    """Smoke 2026-11-23 (dev-1/smoke_hang.md): the root daemon exited after accepting the job; Redis kept
    answering, the job stayed `starting` and the turn hung for 33+ minutes without a final state."""
    ops = FakeOps(views=STUCK, beats=[1.0, 2.0, 400.0], daemon_alive=False)
    c = Client(ops)
    msgs = c.prompt(c.session(), 'Write a short note', timeout=5)
    assert final(msgs) == 'end_turn'
    last = chunks(msgs)[-1]
    assert last.startswith('ERROR') and 'daemon' in last and JID in last
    assert 'fleetmux result ' + JID in last
    assert '--resume /fleets/root/run' in last  # how to bring the root back
    assert ops.probes == [ROOT['config']]
    assert ops.cancelled == []  # nothing withdrawn: the job stays recorded in the root


def test_stale_beat_but_daemon_lock_held_waits_then_gives_up_at_the_hung_limit():
    ops = FakeOps(views=STUCK, beats=[200.0, 300.0, 1000.0], daemon_alive=True)
    c = Client(ops)
    msgs = c.prompt(c.session(), 'Write a short note', timeout=5)
    assert final(msgs) == 'end_turn'
    last = chunks(msgs)[-1]
    assert last.startswith('ERROR') and 'heartbeat' in last and JID in last
    assert ops.cancelled == []


def test_stale_beat_with_a_live_daemon_is_not_an_error_while_under_the_hung_limit():
    ops = FakeOps(views=STUCK, beats=[200.0], daemon_alive=True, results=[(3, {})] * 5 + [DONE_OK])
    c = Client(ops)
    msgs = c.prompt(c.session(), 'Write a short note', timeout=5)
    assert final(msgs) == 'end_turn'
    assert not any(t.startswith('ERROR') for t in chunks(msgs))
    assert 'OK' in chunks(msgs)[-1]


def test_fresh_or_unknown_heartbeat_never_probes_the_daemon_lock():
    ops = FakeOps(views=STUCK, beats=[None, 1.0, None], results=[(3, {})] * 6 + [DONE_OK])
    c = Client(ops)
    assert final(c.prompt(c.session(), 'Write a short note', timeout=5)) == 'end_turn'
    assert ops.probes == []


def test_daemon_limits_leave_room_for_the_roots_own_watchdog():
    assert acp_server.DAEMON_STALE_S >= 120 and acp_server.DAEMON_HUNG_S > acp_server.DAEMON_STALE_S


def test_ops_view_reads_row_and_daemon_heartbeat_from_one_status_call(monkeypatch):
    status = {'jobs': [{'id': JID, 'state': 'starting', 'subfleet': 'M0'}],
              'subfleets': {'M0': {'state': 'running', 'run': '/r/sub/M0'}},
              'daemon': {'heartbeat_at': 1.0, 'heartbeat_age_s': 2183.8}}
    calls = []

    def fake_cli(argv, stdin=None, timeout=None):
        calls.append(argv)
        return 0, 'fleetmux: a notice line\n' + json.dumps(status), ''
    monkeypatch.setattr(acp_server.join, 'fleet_cli', fake_cli)
    row, item, age = acp_server.Ops().view(ROOT['config'], JID)
    assert row['state'] == 'starting' and item == {'state': 'running', 'run': '/r/sub/M0'} and age == 2183.8
    assert calls == [['status', '--config', ROOT['config']]]
    status['daemon'] = {'heartbeat_at': None}
    assert acp_server.Ops().view(ROOT['config'], 'job-ffffffffff')[0::2] == (None, None)


def test_run_dir_is_the_configs_run_and_the_lock_probe_never_creates_a_lock(tmp_path):
    run = tmp_path / 'elsewhere' / 'run'
    run.mkdir(parents=True)
    config = tmp_path / 'cfg' / 'config.json'
    config.parent.mkdir()
    config.write_text(json.dumps({'run': str(run)}))
    assert acp_server.run_dir(str(config)) == run  # as join._root reads it, not the config's parent
    assert acp_server.Ops().daemon_running(str(config)) is False
    assert not (run / 'daemon.lock').exists() and not (config.parent / 'daemon.lock').exists()
    (run / 'daemon.lock').write_text('')
    assert acp_server.Ops().daemon_running(str(config)) is False  # a lock file nobody holds


# ------------------------------------------------------------------ the real command, no root fleet
def test_real_fleetmux_acp_without_a_root_fleet(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('FLEETMUX_', 'OMNIGENT_'))}
    env.update(FLEETMUX_HOME=str(tmp_path / 'home'), FLEETMUX_STATE=str(tmp_path / 'state'),
               FLEETMUX_CONFIG_DIR=str(tmp_path / 'config'), FLEETMUX_JOIN_ROOT=str(tmp_path / 'scan'))
    fleet_py = Path(acp_server.__file__).resolve().parent / 'fleet.py'
    lines = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': 1}},
             {'jsonrpc': '2.0', 'id': 2, 'method': 'session/new', 'params': {'cwd': str(tmp_path), 'mcpServers': []}}]
    proc = subprocess.Popen([sys.executable, str(fleet_py), 'acp'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env, cwd=str(tmp_path))
    try:
        for msg in lines:
            proc.stdin.write(json.dumps(msg) + '\n')
        proc.stdin.flush()
        out = [json.loads(proc.stdout.readline()) for _ in range(2)]
        sid = out[1]['result']['sessionId']
        proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 3, 'method': 'session/prompt', 'params': {
            'sessionId': sid, 'prompt': [{'type': 'text', 'text': 'hello'}]}}) + '\n')
        proc.stdin.flush()
        msgs = []
        while not msgs or msgs[-1].get('id') != 3:
            msgs.append(json.loads(proc.stdout.readline()))  # every stdout line is JSON-RPC
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert out[0]['result']['protocolVersion'] == 1
    text = chunks(msgs)
    assert text[0].startswith('ERROR') and 'fleetmux start' in text[0]
    assert msgs[-1]['result']['stopReason'] == 'end_turn'
