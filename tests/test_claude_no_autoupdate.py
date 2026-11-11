"""Every Claude Code the fleet starts runs with DISABLE_AUTOUPDATER=1 (no-typing batch, step 3a).

Incident 2026-10-14 01:36: a Claude Code started with an isolated configuration (its own
CLAUDE_CONFIG_DIR, so no autoupdater setting) updated itself and broke the shared install. The fleet
therefore turns the autoupdater off wherever it starts Claude: the actor's exec environment, the
generation's --settings env, the cmux spawn env, and every smoke script that starts a fleet.
"""
import json
import os
from pathlib import Path
import re
import sys

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import mixed  # noqa: F401  (fixture: Codex supervisor + 6 Claude workers)
from fleetmux import fleet

ROOT = Path(__file__).resolve().parents[1]


def exec_claude(tmp_path, monkeypatch, slot, **extra):
    """Run fleet.actor for a Claude slot up to execvp; return (environment, argv) at exec time."""
    # §48: a Claude supervisor runs on transport v2 with its hook mailbox pinned (validate_config refuses others)
    cfg = dict(run=str(tmp_path / 'run'), workers=['worker-1'], supervisor_cli='claude', worker_cli='claude',
               claude='/nonexistent/claude', python=sys.executable, transport_version=2, allow_development_v2=True,
               claude_supervisor_hook_delivery=True, mode='task', **extra)
    path = tmp_path / 'config.json'
    fleet.save(path, cfg)
    fleet.agent_cwd(cfg, slot).mkdir(parents=True)

    class R:
        def hgetall(self, key):
            return {'generation': '0', 'state': 'starting', 'launch_ready': '1', 'label': slot}
    monkeypatch.setattr(fleet, 'connect', lambda c: R())
    monkeypatch.setattr(fleet.os, 'chdir', lambda p: None)
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)  # never touch ~/.claude.json
    env = {k: v for k, v in os.environ.items() if k != 'DISABLE_AUTOUPDATER'}  # the caller's shell may lack it
    monkeypatch.setattr(fleet.os, 'environ', env)
    seen = []

    def execvp(prog, argv):
        seen.append((dict(fleet.os.environ), argv))
        raise SystemExit(0)
    monkeypatch.setattr(fleet.os, 'execvp', execvp)
    with pytest.raises(SystemExit):
        fleet.actor(path, slot, 'prompt', 0)
    return seen[0]


def settings_env(argv):
    return json.loads(Path(argv[argv.index('--settings') + 1]).read_text()).get('env') or {}


@pytest.mark.parametrize('slot,hook', [('supervisor', True), ('worker-1', True)])  # §48: hook channel only
def test_actor_execs_claude_with_the_autoupdater_off(tmp_path, monkeypatch, slot, hook):
    env, argv = exec_claude(tmp_path, monkeypatch, slot)
    assert argv[0] == '/nonexistent/claude'
    assert env['DISABLE_AUTOUPDATER'] == '1'
    s = settings_env(argv)
    assert s['DISABLE_AUTOUPDATER'] == '1'          # also when Claude reads its env from --settings
    assert ('CLAUDE_CODE_STOP_HOOK_BLOCK_CAP' in s) == hook  # hook env unchanged (v2 supervisors too)


@pytest.mark.parametrize('slot,extra', [('supervisor', {}), ('worker-1', {}), ('worker-1', {'advisor': True})])
def test_bypass_consent_rides_on_the_settings_file(tmp_path, monkeypatch, slot, extra):
    """Incident 2026-10-30..11-06 (macmini sector rotation, 12 slots): Claude Code moved the bypass consent
    from ~/.claude.json into ~/.claude/settings.json; advised workers run with --setting-sources project, do
    not read that file and sat on the Bypass Permissions dialog until the startup timeout. --settings is read
    whatever --setting-sources says, so the consent rides there."""
    _env, argv = exec_claude(tmp_path, monkeypatch, slot, **extra)
    assert '--dangerously-skip-permissions' in argv
    if extra.get('advisor') and slot != 'supervisor':
        assert argv[argv.index('--setting-sources') + 1] == 'project'  # the launch that lost user settings
    settings = json.loads(Path(argv[argv.index('--settings') + 1]).read_text())
    assert settings.get('skipDangerousModePermissionPrompt') is True


def test_spawned_claude_tabs_carry_the_autoupdater_off(mixed):  # noqa: F811
    claude = [s for s in mixed.mgr.spawned if s['metadata']['cli_type'] == 'claude']
    assert claude and all(s['env_vars'].get('DISABLE_AUTOUPDATER') == '1' for s in claude)
    codex = [s for s in mixed.mgr.spawned if s['metadata']['cli_type'] == 'codex']
    assert codex and all('DISABLE_AUTOUPDATER' not in s['env_vars'] for s in codex)  # Claude only


@pytest.mark.parametrize('script', sorted(p.name for p in (ROOT / 'scripts').glob('smoke_*.sh')))
def test_every_smoke_script_turns_the_autoupdater_off_before_it_starts_a_fleet(script):
    text = (ROOT / 'scripts' / script).read_text()
    exported = re.search(r'^export DISABLE_AUTOUPDATER=1\b', text, re.M)
    assert exported, script + ' must export DISABLE_AUTOUPDATER=1'
    start = re.search(r'^(?!\s*#).*\bstart\b.*--', text, re.M)  # first non-comment fleet start
    assert start is None or exported.start() < start.start(), script + ': export it before the fleet starts'
