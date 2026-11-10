"""The no-typing batch on the advisor line (integ/no-typing-batch-on-deploy).

Every Claude generation the advisor starts (first spawn released by the first sheet, rescue respawn)
keeps the Stop-hook channel and DISABLE_AUTOUPDATER next to the advisor's own launch fields; the
advisor's one-shot Claude never self-updates either; the decision keys of both features coexist.
"""
import json
import os
from pathlib import Path
import sys

import pytest

from test_advisor import Runner, v2adv, v2adv2  # noqa: F401  (fixtures)
from test_advisor_rescue import consult, respawn_sheet, v2_worked
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from cmux_fleet import advisor, claude_hook, fleet

AGENTS = Path(fleet.__file__).resolve().parent / 'agents'


def generation_launch(f, slot, generation):
    """(argv, settings) the actor would exec for this generation, from its own snapshot."""
    snap = fleet.load(f.run_dir / 'runtime' / (slot + '-g' + str(generation) + '.json'))
    argv = fleet.claude_argv(f.path, snap, slot, generation, 'READY?')
    return argv, json.loads(Path(argv[argv.index('--settings') + 1]).read_text())


def assert_hooked_and_advised(f, slot, generation):
    argv, settings = generation_launch(f, slot, generation)
    # advisor launch fields
    assert argv[argv.index('--setting-sources') + 1] == 'project' and '--strict-mcp-config' in argv
    assert argv[argv.index('--effort') + 1] == 'high' and settings['autoMemoryEnabled'] is False
    # hook channel: blocking Stop hook + rewake fallback + StopFailure, block cap off, the declaration
    stop = settings['hooks']['Stop'][0]['hooks']
    assert stop[0]['command'].endswith('claude-stop ' + str(f.path) + ' ' + slot + ' ' + str(generation))
    assert stop[1].get('asyncRewake') is True and 'StopFailure' in settings['hooks']
    assert stop[0]['timeout'] == claude_hook.hook_timeout(f.cfg)
    assert settings['env'] == {'DISABLE_AUTOUPDATER': '1', 'CLAUDE_CODE_STOP_HOOK_BLOCK_CAP': '0'}
    assert argv[argv.index('--append-system-prompt') + 1] == claude_hook.DECLARATION
    # the tab env and the fleet review rules
    tab = [s for s in f.mgr.spawned if s['metadata']['cli_type'] == 'claude'][-1]
    assert tab['env_vars']['DISABLE_AUTOUPDATER'] == '1'
    cwd = fleet.agent_cwd(f.cfg, slot)
    assert (cwd / 'REVIEWER.md').read_text() == (AGENTS / 'reviewer.md').read_text()
    assert 'read REVIEWER.md in your current directory' in (cwd / 'CLAUDE.md').read_text()


def test_first_generation_released_by_the_advisor_keeps_the_hook_channel(v2adv):  # noqa: F811
    from test_v2_codex_claude import answer_turn, submit, typed
    f = v2adv
    submit(f)
    assert answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 5, 'reason': 'go', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Research the semis rotation into worker-1/notes.md', 'id': 'M0'}]}))['status'] == 'committed'
    f.pump()  # the first sheet releases generation 0
    assert_hooked_and_advised(f, 'worker-1', 0)
    assert answer_turn(f, 'worker-1', 'READY', startup=True)['status'] == 'ready'
    f.pump()
    assert 'semis rotation' in typed(f, 'worker-1')[-1]                # handed over by the Stop hook
    assert not [t for p, t in f.mgr.sent if p == f.agent('worker-1')['pane']]  # never typed


def test_rescue_respawn_successor_keeps_the_hook_channel_and_is_never_typed_into(v2adv2, monkeypatch):  # noqa: F811
    from test_v2_codex_claude import answer_turn, typed
    f = v2adv2
    monkeypatch.setattr(f, 'preflight_profile', lambda launch: None)
    v2_worked(f)
    f.advisor_runner = Runner(respawn_sheet())
    old = f.agent('worker-1')['pane']
    assert answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 30, 'reason': 'consult',
                                                    'dispatch': [], 'consult': [consult()]}))['status'] == 'committed'
    f.pump()
    assert 'new configuration' in typed(f, 'worker-1')[-1]          # the handoff request: through the hook
    assert answer_turn(f, 'worker-1', 'handoff written')['status'] == 'committed'
    f.v2.step(10)
    assert f.agent('worker-1')['generation'] == '1' and old in f.mgr.killed
    assert_hooked_and_advised(f, 'worker-1', 1)
    assert answer_turn(f, 'worker-1', 'READY', startup=True, generation=1)['status'] == 'ready'
    f.pump()
    assert 'Research the semis rotation into worker-1/notes.md' in typed(f, 'worker-1')[-1]
    worker_panes = {old, f.agent('worker-1')['pane']}
    assert not [t for p, t in f.mgr.sent if p in worker_panes]          # neither generation was typed into


def test_actor_exports_the_autoupdater_off_for_an_advised_snapshot(tmp_path, monkeypatch):
    cfg = dict(run=str(tmp_path / 'run'), workers=['worker-1'], supervisor_cli='codex', worker_cli='claude',
               claude='/nonexistent/claude', python=sys.executable, advisor=True, worker_effort='high')
    path = tmp_path / 'config.json'
    fleet.save(path, cfg)
    fleet.agent_cwd(cfg, 'worker-1').mkdir(parents=True)

    class R:
        def hgetall(self, key):
            return {'generation': '0', 'state': 'starting', 'launch_ready': '1', 'label': 'worker-1'}
    monkeypatch.setattr(fleet, 'connect', lambda c: R())
    monkeypatch.setattr(fleet.os, 'chdir', lambda p: None)
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)
    monkeypatch.setattr(fleet.os, 'environ', {k: v for k, v in os.environ.items() if k != 'DISABLE_AUTOUPDATER'})
    seen = []

    def execvp(prog, argv):
        seen.append((dict(fleet.os.environ), argv))
        raise SystemExit(0)
    monkeypatch.setattr(fleet.os, 'execvp', execvp)
    with pytest.raises(SystemExit):
        fleet.actor(path, 'worker-1', 'prompt', 0)
    env, argv = seen[0]
    assert env['DISABLE_AUTOUPDATER'] == '1' and env['CLAUDE_CODE_STOP_HOOK_BLOCK_CAP'] == '0'
    assert argv[argv.index('--effort') + 1] == 'high' and '--append-system-prompt' in argv


def test_the_advisor_one_shot_claude_never_self_updates(tmp_path, monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen.update(kw)
        return type('Done', (), {'returncode': 0, 'stdout': '{}', 'stderr': ''})
    monkeypatch.setattr(advisor.subprocess, 'run', run)
    monkeypatch.delenv('DISABLE_AUTOUPDATER', raising=False)
    advisor.run_claude({'goal': 'g'}, cfg={'claude': '/bin/claude'}, run_dir=tmp_path)
    assert seen['env']['DISABLE_AUTOUPDATER'] == '1' and seen['env'].get('PATH') == os.environ.get('PATH')


def test_advisor_and_withdraw_keys_coexist():
    workers = ['worker-1', 'worker-2']
    done = {'status': 'COMPLETED', 'completion': 100, 'reason': 'ok', 'dispatch': [], 'advisor': 'off',
            'withdraw': ['worker-2']}
    parsed = fleet.parse_decision(json.dumps(done), workers, True)
    assert parsed['advisor'] == 'off' and parsed['withdraw'] == ['worker-2']
    with pytest.raises(ValueError, match='withdraw is only for COMPLETED'):
        fleet.parse_decision(json.dumps({'status': 'RUNNING', 'completion': 5, 'dispatch': [], 'advisor': 'on',
                                         'consult': [consult()], 'withdraw': ['worker-1']}), workers, True)
    for name in ('supervisor.md', 'supervisor_v2.md'):
        text = (AGENTS / name).read_text()
        assert '- `advisor`: optional `"on"` / `"off"`' in text and '- `withdraw`: optional, only with `COMPLETED`' in text
    ts = (AGENTS.parent / 'pi_extension_v2.ts').read_text()
    assert 'advisor: Type.Optional(' in ts and 'withdraw: Type.Optional(Type.Array(Type.String()))' in ts
