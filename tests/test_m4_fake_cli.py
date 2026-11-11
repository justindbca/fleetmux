"""Self-tests of the M4 end-to-end fixtures (e2e-1 plan r3 §3.1; design-1/m4-design.md §8.4): the native fake
CLI on a real fleetmux PTY, driven only through product code (claude_input.visible_editor / send,
FleetmuxSessionManager.has_cli_process, codex_queue.enqueue / bound, fleet.completed_turn), and the world builder.

Every test builds its own world under ``~/.m4e/fm4-e2e/<id>/`` (kept, never removed), stops what it started
through the product (``close_all``), then proves from the read-only ledger that nothing of the world is left;
only if something is left does it signal verified pids of its own world, and then it fails.
"""
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from _fleetmux_milestone import milestone  # noqa: E402
from fleetmux import claude_input, codex_queue, fleet  # noqa: E402
from fleetmux.backends import fleetmux as fm  # noqa: E402
from m4e import world as m4world  # noqa: E402
from m4e.inventory import Ledger  # noqa: E402

pytestmark = milestone(4)
HOOK_CHANNEL = True  # the real hook commands run in the fake CLI's subprocesses: no in-process stand-in
REAL_ENQUEUE = codex_queue.enqueue  # bound at import, before conftest's per-test fake replaces it


def wait_for(what, predicate, limit=30.0, every=0.2):
    deadline = time.monotonic() + limit
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(every)
    raise AssertionError('timed out after %.0f s waiting for %s (last: %r)' % (limit, what, last))


@pytest.fixture
def fm_world(monkeypatch):
    for problem in (not m4world.FLEETMUX_REAL.is_file() and 'fleetmux binary not built: ' + str(m4world.FLEETMUX_REAL),
                    not m4world.SHIM.is_file() and 'fake CLI shim not built: ' + str(m4world.SHIM)):
        if problem:
            pytest.fail(problem + ' (the m4 gate builds both; missing fixtures fail, never skip)')
    w = m4world.World()
    w.fake_home()
    w.binaries()
    monkeypatch.setenv(fm.BIN_ENV, str(m4world.FLEETMUX_REAL))
    mgr = fm.FleetmuxSessionManager('m4e-selftest', state_dir=w.s)
    ledger = Ledger(w)
    env = SimpleNamespace(world=w, mgr=mgr, ledger=ledger, panes=[])
    print('m4e self-test world: ' + str(w.dir))
    yield env
    try:
        mgr.close_all()  # the product's stop path: kill the registered panes, then server.shutdown
    finally:
        left = ledger.wait_clean(30)
        (w.ev / 'residue.json').write_text(json.dumps(dict(left, ledger=ledger.dump()), indent=1, default=str))
        if not left['clean']:
            hit = ledger.fallback()
            (w.ev / 'fallback.json').write_text(json.dumps(hit, indent=1, default=str))
            pytest.fail('m4e self-test left processes / sockets after close_all: ' + json.dumps(left, default=str)[:2000])


def spawn_claude(env, name, prompt, settings):
    w = env.world
    cwd = w.dir / 'cwd' / name
    cwd.mkdir(parents=True)
    path = cwd / 'settings.json'
    path.write_text(json.dumps(settings))
    cmd = shlex.join([str(w.claude), '--dangerously-skip-permissions', '--model', 'm4e', '--name', name,
                      '--settings', str(path), '--', prompt])
    res = env.mgr.spawn_interactive_cli(name, cmd, metadata={'type': 'worker', 'cli_type': 'claude', 'label': name},
                                        start_directory=str(cwd), env_vars={'HOME': str(w.h)})
    assert res.success, res.error
    env.ledger.refresh()
    return res.pane_id, cwd


def transcript_rows(env, cwd):
    out = []
    for path in sorted((env.world.h / '.claude' / 'projects').glob('*/*.jsonl'), key=lambda p: p.stat().st_mtime):
        lines = path.read_text().splitlines()
        if lines and json.loads(lines[0]).get('cwd') == str(cwd):
            out += [json.loads(line) for line in lines]
    return out


def editor(env, pane):
    return claude_input.visible_editor(env.mgr.capture_output(pane))


def test_fake_claude_editor_frame_is_accepted_by_visible_editor(fm_world):
    env = fm_world
    flag = env.world.dir / 'release-stop-hook'
    hook = "while [ ! -e %s ]; do sleep 0.1; done; echo '{}'" % shlex.quote(str(flag))
    pane, cwd = spawn_claude(env, 'editor-1', 'There is no task yet: your final reply must be exactly READY.',
                             {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': hook, 'timeout': 60}]}]}})
    busy = wait_for('the editor while the Stop hook runs', lambda: editor(env, pane))
    assert busy.idle is False and busy.text == '', busy  # `running stop hooks…` above the rule = BUSY
    flag.write_text('go')
    idle = wait_for('an idle empty editor after the hook', lambda: (lambda v: v if v and v.idle else None)(editor(env, pane)))
    assert idle.text == '' and idle.exact, idle
    rows = transcript_rows(env, cwd)
    assert [r['message']['content'] for r in rows if r['type'] == 'user'] == [
        'There is no task yet: your final reply must be exactly READY.']
    assert rows[-1]['type'] == 'assistant' and rows[-1]['message']['content'][0]['text'] == 'READY'


def test_fake_claude_accepts_claude_input_send(fm_world):
    env = fm_world
    seen = env.world.dir / 'session-start.jsonl'
    record = shlex.join([sys.executable, '-c', 'import sys; open(sys.argv[1], "a").write(sys.stdin.read() + "\\n")',
                         str(seen)])
    pane, cwd = spawn_claude(env, 'send-1', 'Your final reply must be exactly READY.',
                             {'hooks': {'SessionStart': [{'matcher': 'clear', 'hooks': [
                                 {'type': 'command', 'command': 'exec ' + record, 'timeout': 10}]}]}})
    wait_for('idle editor', lambda: (lambda v: v if v and v.idle else None)(editor(env, pane)))
    assert env.mgr.send_prompt_to_idle(pane, 'hello m4e', bracketed=True), env.mgr.last_error
    long_text = '[Request m4e-1] Handle this request as your role describes.\nPayload (JSON):\n' + json.dumps(
        {'line': 'x' * 300}) + '\nYour final reply must be exactly one JSON object with the keys: reply. No text outside the JSON object.'
    wait_for('idle again', lambda: (lambda v: v if v and v.idle and not v.text else None)(editor(env, pane)))
    assert env.mgr.send_prompt_to_idle(pane, long_text, bracketed=True), env.mgr.last_error  # collapsed placeholder
    wait_for('reply to the request', lambda: any(
        r['type'] == 'assistant' and 'E2E-OK m4e-1' in r['message']['content'][0]['text'] for r in transcript_rows(env, cwd)))
    typed = [r for r in transcript_rows(env, cwd) if r['type'] == 'user' and r.get('origin')]
    assert [(r['origin']['kind'], r['message']['content']) for r in typed] == [('human', 'hello m4e'), ('human', long_text)]
    wait_for('idle before /clear', lambda: (lambda v: v if v and v.idle and not v.text else None)(editor(env, pane)))
    assert env.mgr.send_prompt_to_idle(pane, '/clear'), env.mgr.last_error  # raw, like clear_after_result
    got = wait_for('SessionStart(clear) hook input', lambda: seen.exists() and seen.read_text().strip())
    data = json.loads(got.splitlines()[-1])
    assert data['source'] == 'clear' and data['hook_event_name'] == 'SessionStart'
    first = transcript_rows(env, cwd)[0]['sessionId']
    assert data['session_id'] != first and Path(data['transcript_path']).name == data['session_id'] + '.jsonl'


def test_fake_cli_comm_is_claude_and_codex(fm_world):
    env = fm_world
    pane, _ = spawn_claude(env, 'comm-1', 'Your final reply must be exactly READY.', {})
    home = env.world.h / '.codex-agents'
    home.mkdir()
    cwd = env.world.dir / 'cwd' / 'codex-1'
    cwd.mkdir(parents=True)
    cmd = shlex.join([str(env.world.codex), '--profile', 'fleet', '--no-alt-screen', 'Reply exactly READY.'])
    res = env.mgr.spawn_interactive_cli('codex-1', cmd, metadata={'type': 'supervisor', 'cli_type': 'codex'},
                                        start_directory=str(cwd), env_vars={'HOME': str(env.world.h), 'CODEX_HOME': str(home)})
    assert res.success, res.error
    env.ledger.refresh()
    assert wait_for('claude on the pane tty', lambda: env.mgr.has_cli_process(pane, ('claude',)))
    assert wait_for('codex on the pane tty', lambda: env.mgr.has_cli_process(res.pane_id, ('codex',)))
    assert not env.mgr.has_cli_process(pane, ('codex',)) and not env.mgr.has_cli_process(res.pane_id, ('claude',))


def test_fake_codex_queue_turn_is_bound(fm_world):
    env = fm_world
    w = env.world
    home = w.h / '.codex-agents'
    home.mkdir()
    cwd = w.dir / 'cwd' / 'supervisor'
    cwd.mkdir(parents=True)
    calls = w.dir / 'notify.jsonl'
    notify = [sys.executable, '-c', 'import sys; open(sys.argv[1], "a").write(sys.argv[-1] + "\\n")', str(calls)]
    cmd = shlex.join([str(w.codex), '--profile', 'fleet', '--no-alt-screen', '-c', 'notify=' + json.dumps(notify),
                      'You are fleet agent supervisor. There is no task yet: your final reply must be exactly READY.'])
    res = env.mgr.spawn_interactive_cli('supervisor', cmd, metadata={'type': 'supervisor', 'cli_type': 'codex'},
                                        start_directory=str(cwd), env_vars={'HOME': str(w.h), 'CODEX_HOME': str(home)})
    assert res.success, res.error
    env.ledger.refresh()
    first = json.loads(wait_for('the READY notify', lambda: calls.exists() and calls.read_text().strip()).splitlines()[0])
    assert first['last-assistant-message'] == 'READY'
    thread = first['thread-id']
    cfg = {'run': str(w.dir), 'supervisor_cwd': str(cwd), 'codex': str(w.codex), 'codex_home': str(home)}
    token = codex_queue.new_token()
    text = codex_queue.marked('[Request m4e-q1] External request m4e-q1 wants sub-fleet tgog up (reason: e2e).', token)
    capability = codex_queue.capability(str(w.codex))
    assert capability == (True, '0.162.0', ''), capability
    queued = REAL_ENQUEUE(cfg, 'supervisor', thread, text)  # the real `codex queue` subprocess (env CODEX_HOME, cwd)
    assert queued['thread'] == thread
    lines = wait_for('the queued turn notify', lambda: calls.exists() and len(calls.read_text().splitlines()) >= 2
                     and calls.read_text().splitlines())
    done = json.loads(lines[1])
    assert json.loads(done['last-assistant-message'])['subfleet'] == {
        'id': 'm4e-q1', 'decision': 'approve', 'note': 'e2e fake supervisor approves tgog'}
    rec = {'cq_delivery': 'd-m4e', 'cq_token': token, 'cq_text': text, 'cq_thread': thread, 'cq_generation': '0',
           'cq_state': 'queued', 'cq_queued_id': queued['queued_id'], 'cq_turn': ''}
    assert codex_queue.bound(rec, thread, done['turn-id'], home / 'sessions', 0) == (True, '')
    assert fleet.completed_turn(str(cwd), done, home / 'sessions')
    assert codex_queue.token_turn(thread, token, home / 'sessions') == {'turn': done['turn-id'], 'complete': True}


def test_m4e_world_builds_with_the_pinned_kit_and_one_patch(monkeypatch):
    """The E2 world before anything starts: pinned kit, completed domain kit, the one memx tab patch, fake HOME,
    fail-closed CLI resolution, socket budget, no matching tab process (plan §4.2 / §4.3)."""
    w = m4world.World().build()
    monkeypatch.setenv('HOME', str(w.h))  # the roster loader expands `~` with the HOME the fleet runs under
    facts = w.preconditions()
    (w.ev / '00-preconditions.json').write_text(json.dumps(facts, indent=1, default=str))
    from fleetmux import kit as kitmod, roster
    kitmod.Kit(str(w.kit))  # the product's own kit validation (start runs it for `advisor: on`)
    original = m4world.git('show', m4world.KIT_REV + ':' + m4world.KIT_PATH + '/memx_roster.yaml').decode()
    copied = (w.fleet_dir / 'memx_roster.yaml').read_text()
    diff = [(a, b) for a, b in zip(original.splitlines(), copied.splitlines()) if a != b]
    assert diff == [('    command: ' + m4world.MEMX_TAB_ORIGINAL, '    command: ' + shlex.join(w.tab_argv))]
    for name in m4world.KIT_FILES:
        if name != 'memx_roster.yaml':
            assert (w.fleet_dir / name).read_bytes() == m4world.git(
                'show', m4world.KIT_REV + ':' + m4world.KIT_PATH + '/' + name), name
    entries = {e['name']: e for e in roster.load_subfleets(w.fleet_dir / 'roster.yaml')}
    assert set(entries) == {'memx', 'tgog', 'mayfly'}
    assert entries['memx']['claude'] == str(w.h / m4world.MEMX_WRAPPER)  # `~` expanded under the fake HOME
    assert facts['cli']['login shell claude'] == str(w.claude)
    assert not (w.kit / 'mcp.json').exists()
    assert all(n <= m4world.SOCKET_MAX for n in facts['sockets'].values()), facts['sockets']


def test_every_login_shell_of_the_world_resolves_claude_and_codex_to_the_fake_cli():
    """CLI isolation (M35 resume): macOS /etc/profile and /etc/zprofile run path_helper, which moves
    /opt/homebrew/bin (a real codex) ahead of the world's bin/. The fake HOME's login files must put bin/ first
    again, so a pane's `bash -lc` / `exec $SHELL -l` finds only the native fake CLI. Only `command -v` runs here:
    no real CLI is ever executed. The world preconditions must then fail closed on neither name."""
    w = m4world.World().build()
    runs = [('env -i bash', ['env', '-i', 'HOME=' + str(w.h), 'PATH=' + w.path, '/bin/bash', '-lc']),
            ('env -i zsh', ['env', '-i', 'HOME=' + str(w.h), 'PATH=' + w.path, '/bin/zsh', '-lc']),
            ('env -i sh', ['env', '-i', 'HOME=' + str(w.h), 'PATH=' + w.path, '/bin/sh', '-lc']),
            ('world env bash', ['/bin/bash', '-lc'])]
    got, bad = {}, {}
    for label, argv in runs:
        env = w.env(marked=False) if label.startswith('world env') else None
        for name in ('claude', 'codex'):
            done = subprocess.run(argv + ['command -v ' + name], env=env, capture_output=True, text=True, timeout=30)
            got[label + ' ' + name] = done.stdout.strip()
            if os.path.realpath(done.stdout.strip() or '/nonexistent') != os.path.realpath(w.bin / name):
                bad[label + ' ' + name] = done.stdout.strip()
    problems = []
    facts = w.cli_resolution(problems)
    (w.ev / '00-cli-isolation.json').write_text(json.dumps({'resolved': got, 'not_fake': bad, 'facts': facts,
                                                            'problems': problems}, indent=1))
    assert not bad, 'login shells resolve a CLI outside the world bin %s: %r' % (w.bin, bad)
    assert facts['login shell claude'] == str(w.claude) and facts['login shell codex'] == str(w.codex), facts
    assert problems == [], problems


# ------------------------------------------------- M46: the fake CLI on v2-delivered texts (no [Request] header)
def v2_delivered(text):
    """What a worker / supervisor receives under v2: ``RuntimeV2.queue`` moves a leading ``[Request <id>]`` into
    metadata and queues ``text[head.end():].lstrip()``. Pinned to the product source, so a change there fails here."""
    import inspect
    from fleetmux import runtime_v2
    src = inspect.getsource(runtime_v2.RuntimeV2.queue)
    assert 'head = REQ_HEAD.match(text)' in src and 'text = text[head.end():].lstrip()' in src, \
        'RuntimeV2.queue header strip changed; update the M46 fake CLI tests'
    head = fleet.REQ_HEAD.match(text)
    return text[head.end():].lstrip() if head else text


def worker_request_text(rid, payload):
    """fleet.Fleet.request_message as dispatched to an official Claude worker (the non-Pi branch)."""
    return fleet.Fleet.request_message(SimpleNamespace(v2=None, cfg={}), rid,
                                       {'payload': payload, 'reply': {'required': ['reply']}})


def approval_text(rid, name):
    asked = []
    from fleetmux import named_subfleet
    named_subfleet.ask(SimpleNamespace(queue=lambda slot, text, **_: asked.append((slot, text))), rid, 'up', name, 'e2e')
    assert len(asked) == 1 and asked[0][0] == 'supervisor', asked
    return asked[0][1]


def test_fake_supervisor_approves_a_subfleet_request_with_or_without_the_request_header():
    from m4e import fake_cli
    text = approval_text('e2e-up-1', 'tgog')
    stripped = v2_delivered(text)
    assert text.startswith('[Request e2e-up-1] ') and stripped.startswith('External request e2e-up-1 wants sub-fleet tgog ')
    for delivered in (text, stripped):
        decision = json.loads(fake_cli.answer(delivered, True))
        assert decision['subfleet']['id'] == 'e2e-up-1' and decision['subfleet']['decision'] == 'approve', decision
        assert decision['subfleet']['note'].endswith(' tgog'), decision
        assert {k: decision[k] for k in ('status', 'dispatch', 'skip')} == fake_cli.IDLE_DECISION, decision


def test_fake_worker_reply_takes_the_header_id_else_the_payload_m4e_rid():
    from m4e import fake_cli
    text = worker_request_text('m44-exit-g1', {'note': 'm4e end-to-end', 'm4e_rid': 'm44-exit-g1'})
    stripped = v2_delivered(text)
    assert '[Request' not in stripped and stripped.startswith('Handle this request as your role describes.')
    for delivered in (text, stripped):
        assert json.loads(fake_cli.answer(delivered, False)) == {'reply': 'E2E-OK m44-exit-g1'}
    # the header wins over the payload when both are present
    mixed = worker_request_text('head-id-1', {'m4e_rid': 'payload-id-1'})
    assert json.loads(fake_cli.answer(mixed, False)) == {'reply': 'E2E-OK head-id-1'}
    assert json.loads(fake_cli.answer(v2_delivered(mixed), False)) == {'reply': 'E2E-OK payload-id-1'}


def test_fake_worker_request_without_any_id_gets_no_success_reply():
    """No header and no m4e_rid: the fake must not answer with an E2E-OK object (the product would accept it)."""
    from m4e import fake_cli
    for payload in ({'note': 'm4e end-to-end'}, {'m4e_rid': ''}, {'m4e_rid': 7}):
        reply = fake_cli.answer(v2_delivered(worker_request_text('m46-no-id', payload)), False)
        assert 'E2E-OK' not in reply and 'm46-no-id' not in reply, (payload, reply)
        try:
            parsed = json.loads(reply)
        except ValueError:
            parsed = None
        assert not isinstance(parsed, dict), (payload, reply)
        assert 'no request id' in reply, (payload, reply)
