"""P8o worker sandbox and memory switch (brief v2.2 §8.4 test_security_sandbox.py; C5 / C15 / C16 / C18 / U8 / U9).

Red in A2' (stubs raise; claude_argv not wired yet) except the [baseline] test. Pure calls with injected
``platform_name`` / ``which`` / ``codex_capability`` and a temporary HOME; no CLI is run, nothing is probed,
no credential path is ever allowed for reading (C16). ``failIfUnavailable`` / ``onFailure`` are asserted only as
written configuration: whether this machine's Claude honours them is an unverified real-machine item.
"""
import copy
import json
from pathlib import Path
import sys

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx  # noqa: F401  (fixture)
from fleetmux import channels, fleet, sandbox
from fleetmux.channels import security

BUSINESS = ('futu', 'moomoo', 'ibkr', 'stte', 'macmini', 'studio', 'notify_bus', 'order_gate', 'oc_', 'ou_')
RUN = '/tmp/R'
SOCK = '/tmp/R/redis.sock'


def policy(**sb):
    pol, errors = security.parse_security('')
    assert not errors
    pol = copy.deepcopy(pol)
    pol['sandbox'].update(sb)
    return pol


def cfg_for(**over):
    cfg = dict(run=RUN, redis_socket=SOCK, python=sys.executable, workers=['worker-1'], mode='service',
               supervisor_cli='claude', worker_cli='claude', transport_version=2, allow_development_v2=True,
               v2_lease_ms=300000)
    cfg.update(over)
    return cfg


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / 'home'
    h.mkdir()
    monkeypatch.setenv('HOME', str(h))
    monkeypatch.setenv(channels.CONFIG_ENV, str(tmp_path / 'cfgdir'))
    return h


def test_claude_settings_native_mode_shape(home, tmp_path):
    roster = tmp_path / 'roster.yaml'
    roster.write_text('workers:\n  - name: worker-1\n    writable: [' + str(tmp_path / 'shared') + ']\n')
    pol = policy(mode='claude_native', allow_domains=['example.org'], deny_read=[str(tmp_path / 'private')])
    s = sandbox.claude_settings(pol, cfg_for(roster=str(roster)), 'worker-1')
    sb = s['sandbox']
    assert sb['enabled'] is True and sb['allowUnsandboxedCommands'] is False
    assert sb['failIfUnavailable'] is True                                   # C5 (configuration only)
    assert 'example.org' in sb['network']['allowedDomains']
    assert SOCK in sb['network']['allowUnixSockets']
    deny = sb['filesystem']['denyRead']
    assert str(tmp_path / 'private') in deny and str(home / '.ssh') in deny and str(home / '.codex' / 'auth.json') in deny
    allow = sb['filesystem']['allowWrite']
    assert str(fleet.agent_cwd(cfg_for(), 'worker-1')) in allow and RUN + '/workspace' in allow
    assert str(tmp_path / 'shared') in allow                                 # roster writable: root
    perms = s['permissions']['deny']
    assert all('Read(' + p + ')' in perms for p in deny)
    assert 'WebFetch(domain:*)' not in perms
    empty = sandbox.claude_settings(policy(mode='claude_native'), cfg_for(), 'worker-1')
    assert 'WebFetch(domain:*)' in empty['permissions']['deny']


def test_memory_off_sets_autoMemoryEnabled_false_and_env_for_supervisor_and_workers(tmp_path):
    pol = policy(memory='off')
    for name, over in (('supervisor', {}), ('worker-1', {}), ('worker-1', {'advisor': True}), (fleet.ADVISOR, {})):
        cfg = cfg_for(run=str(tmp_path / ('r-' + name + str(len(over)))), security=pol, claude='/bin/false', **over)
        cwd = fleet.agent_cwd(cfg, name)
        cwd.mkdir(parents=True, exist_ok=True)
        fleet.claude_argv(Path(cfg['run']) / 'config.json', cfg, name, 0, 'p')
        data = json.loads((cwd / '.fleet-settings-g0.json').read_text())
        assert data.get('autoMemoryEnabled') is False, name
        assert data['env'].get('CLAUDE_CODE_DISABLE_AUTO_MEMORY') == '1', name
    assert 'CLAUDE_CODE_DISABLE_AUTO_MEMORY' not in fleet.CLAUDE_ENV        # only in settings, never the launch env


# Today's (feat/channels) no-policy settings, byte for byte, for python 'PYX' and the run at {RUN}.
BASELINE = {
    'worker': '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "PYX {FLEET} claude-stop {RUN}/config.json worker-1 0", "timeout": 86400}, {"type": "command", "timeout": 86400, "asyncRewake": true, "rewakeMessage": "Fleet delivery:", "command": "PYX {FLEET} claude-rewake {RUN}/config.json worker-1 0"}]}], "StopFailure": [{"hooks": [{"type": "command", "timeout": 30, "command": "PYX {FLEET} claude-stop-failure {RUN}/config.json worker-1 0"}, {"type": "command", "timeout": 86400, "asyncRewake": true, "rewakeMessage": "Fleet delivery:", "command": "PYX {FLEET} claude-rewake {RUN}/config.json worker-1 0"}]}], "PostToolUse": [{"hooks": [{"type": "command", "timeout": 10, "command": "PYX {FLEET} claude-posttool {RUN}/config.json worker-1 0"}]}]}, "env": {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "0"}, "skipDangerousModePermissionPrompt": true}',
    'supervisor': '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "PYX {FLEET} claude-stop {RUN}/config.json supervisor 0", "timeout": 210}]}], "PostToolUse": [{"hooks": [{"type": "command", "timeout": 10, "command": "PYX {FLEET} claude-posttool {RUN}/config.json supervisor 0"}]}], "UserPromptSubmit": [{"hooks": [{"type": "command", "timeout": 60, "command": "PYX {FLEET} claude-prompt {RUN}/config.json supervisor 0"}]}]}, "env": {"DISABLE_AUTOUPDATER": "1"}, "skipDangerousModePermissionPrompt": true}',
    'advised': '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "PYX {FLEET} claude-stop {RUN}/config.json worker-1 0", "timeout": 86400}, {"type": "command", "timeout": 86400, "asyncRewake": true, "rewakeMessage": "Fleet delivery:", "command": "PYX {FLEET} claude-rewake {RUN}/config.json worker-1 0"}]}], "StopFailure": [{"hooks": [{"type": "command", "timeout": 30, "command": "PYX {FLEET} claude-stop-failure {RUN}/config.json worker-1 0"}, {"type": "command", "timeout": 86400, "asyncRewake": true, "rewakeMessage": "Fleet delivery:", "command": "PYX {FLEET} claude-rewake {RUN}/config.json worker-1 0"}]}], "PostToolUse": [{"hooks": [{"type": "command", "timeout": 10, "command": "PYX {FLEET} claude-posttool {RUN}/config.json worker-1 0"}]}]}, "env": {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "0"}, "skipDangerousModePermissionPrompt": true, "autoMemoryEnabled": false}',
    'advisor': '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "PYX {FLEET} claude-stop {RUN}/config.json advisor 0", "timeout": 86400}, {"type": "command", "timeout": 86400, "asyncRewake": true, "rewakeMessage": "Fleet delivery:", "command": "PYX {FLEET} claude-rewake {RUN}/config.json advisor 0"}]}], "StopFailure": [{"hooks": [{"type": "command", "timeout": 30, "command": "PYX {FLEET} claude-stop-failure {RUN}/config.json advisor 0"}]}]}, "autoMemoryEnabled": false, "env": {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "0"}}',
    'clear': '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "exec PYX {FLEET} claude-stop {RUN}/config.json worker-1 0", "timeout": 210}]}], "PostToolUse": [{"hooks": [{"type": "command", "timeout": 10, "command": "PYX {FLEET} claude-posttool {RUN}/config.json worker-1 0"}]}], "SessionStart": [{"hooks": [{"type": "command", "timeout": 10, "command": "exec PYX {FLEET} claude-clear {RUN}/config.json worker-1 0"}], "matcher": "clear"}], "StopFailure": [{"hooks": [{"type": "command", "timeout": 10, "command": "exec PYX {FLEET} claude-stop-failure {RUN}/config.json worker-1 0"}]}]}, "env": {"DISABLE_AUTOUPDATER": "1"}, "skipDangerousModePermissionPrompt": true}',
}


def test_no_policy_settings_identical_to_today(tmp_path):  # [baseline]
    fleet_py = str(Path(fleet.__file__).resolve())
    kinds = {'worker': ('worker-1', {}), 'supervisor': ('supervisor', {}), 'advised': ('worker-1', {'advisor': True}),
             'advisor': (fleet.ADVISOR, {}), 'clear': ('worker-1', {'clear_after_result': True})}
    for kind, (name, over) in kinds.items():
        run = tmp_path / kind
        cfg = cfg_for(run=str(run), python='PYX', claude='/bin/false', redis_socket='/tmp/cmux-fleet-x.sock', **over)
        cwd = fleet.agent_cwd(cfg, name)
        cwd.mkdir(parents=True, exist_ok=True)
        fleet.claude_argv(run / 'config.json', cfg, name, 0, 'p')
        got = (cwd / '.fleet-settings-g0.json').read_text()
        assert got == BASELINE[kind].replace('{FLEET}', fleet_py).replace('{RUN}', str(run)), kind
    assert sandbox.claude_settings(None, cfg_for(), 'worker-1') == {}


def test_builtin_deny_read_list_has_no_business_words(home):
    deny = getattr(sandbox, 'BUILTIN_DENY_READ')
    paths = [str(p) for p in (deny() if callable(deny) else deny)]
    assert paths
    for p in paths:
        assert p.startswith(str(home)) or p.startswith(str(channels.config_dir())), p
    source = (Path(sandbox.__file__)).read_text().lower()
    assert not [w for w in BUSINESS if w in source]


def test_codex_flags_network_off_when_no_allow_domains_else_proxy_domains_when_supported(monkeypatch):
    monkeypatch.setattr(sandbox, 'codex_capability', lambda cfg, what: True)
    flags = ' '.join(sandbox.codex_flags(policy(), cfg_for()))
    assert 'sandbox_workspace_write.network_access=false' in flags
    flags = ' '.join(sandbox.codex_flags(policy(allow_domains=['example.org']), cfg_for()))
    assert 'features.network_proxy.domains.allow' in flags and 'example.org' in flags
    monkeypatch.setattr(sandbox, 'codex_capability', lambda cfg, what: False)
    flags = ' '.join(sandbox.codex_flags(policy(allow_domains=['example.org']), cfg_for()))
    assert 'network_access=false' in flags and 'network_proxy' not in flags
    # the fragment comes after actor_argv's own network_access=true, so it wins (real-machine item 5)
    argv = fleet.actor_argv(Path(RUN) / 'config.json', cfg_for(worker_cli='codex', security=policy(mode='claude_native')), 'worker-1', 0, 'p')
    joined = ' '.join(argv)
    assert joined.rfind('network_access=false') > joined.find('network_access=true')


def test_os_wrap_macos_profile_denies_home_read_allows_cwd_write_443_only_and_redis_socket(home, tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, 'platform_name', lambda: 'darwin')
    monkeypatch.setattr(sandbox, 'which', lambda name: '/usr/bin/sandbox-exec' if name == 'sandbox-exec' else None)
    cwd = tmp_path / 'cwd'
    argv = sandbox.os_wrap(['claude', '--x'], policy(mode='os'), str(cwd), cfg_for())
    assert argv[:3] == ['/usr/bin/sandbox-exec', '-p', argv[2]] and argv[-2:] == ['claude', '--x']
    profile = argv[2]
    assert '(deny file-read* (subpath "' + str(home) + '"))' in profile
    assert '(allow file-write* (subpath "' + str(cwd) + '")' in profile
    assert '(allow network-outbound (remote ip "*:443"))' in profile
    assert '(deny network-outbound (remote ip "localhost:*"))' in profile
    assert '(allow network-outbound (literal "' + SOCK + '"))' in profile
    for secret in ('.claude.json', '.codex/auth.json', str(channels.config_dir())):     # C16: no read exemption
        assert '(allow file-read* (literal "' + str(home / secret) not in profile
        assert '(allow file-read* (subpath "' + secret not in profile
    monkeypatch.setattr(sandbox, 'which', lambda name: None)                            # C5: fail closed
    with pytest.raises(Exception):
        sandbox.os_wrap(['claude'], policy(mode='os'), str(cwd), cfg_for())


def test_os_wrap_linux_bwrap_unshare_net_when_no_domains_else_audit_not_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, 'platform_name', lambda: 'linux')
    monkeypatch.setattr(sandbox, 'which', lambda name: '/usr/bin/bwrap' if name == 'bwrap' else None)
    argv = sandbox.os_wrap(['claude'], policy(mode='os'), str(tmp_path), cfg_for())
    assert argv[0] == '/usr/bin/bwrap' and '--unshare-net' in argv
    argv = sandbox.os_wrap(['claude'], policy(mode='os', allow_domains=['example.org']), str(tmp_path), cfg_for())
    assert '--share-net' in argv and '--unshare-net' not in argv
    monkeypatch.setattr(sandbox, 'which', lambda name: None)
    with pytest.raises(Exception):
        sandbox.os_wrap(['claude'], policy(mode='os'), str(tmp_path), cfg_for())


def test_pi_slot_under_native_mode_audits_sandbox_off_once(fx):
    f = fx
    f.cfg['security'] = policy(mode='claude_native')
    sandbox.startup_audits(f)
    sandbox.startup_audits(f)
    rows = [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == 'security.sandbox_off']
    assert sorted(x['slot'] for x in rows) == sorted(f.names) and all(x.get('reason') for x in rows)


def test_allow_domains_empty_denies_webfetch_and_bash_network_but_not_cli_api(home):
    pol = policy(mode='claude_native', allow_domains=[])
    s = sandbox.claude_settings(pol, cfg_for(), 'worker-1')
    assert 'WebFetch(domain:*)' in s['permissions']['deny']
    assert s['sandbox']['network']['allowedDomains'] == []
    assert not [d for d in s['sandbox']['network']['allowedDomains'] if 'anthropic' in d]
    again, errors = security.parse_security('sandbox:\n  mode: claude_native\n  allow_domains: []\n')
    assert again is not None and not errors                 # the sandbox covers Bash only, not the CLI's API traffic


def test_daemon_start_audits_degraded_slots_and_os_mode_refuses_spawn_without_wrapper(fx, monkeypatch):
    """M172: Fleet.run writes the startup sandbox_off rows (policy only); a spawn under sandbox.mode os with no OS
    wrapper writes security.sandbox_off reason=unavailable through the daemon's audit writer and is refused
    before any side effect. No policy / mode off: nothing."""
    f = fx

    def rows():
        return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == 'security.sandbox_off']
    monkeypatch.setattr(f.v2, 'run', lambda: None)        # Fleet.run's start path only, not the loop
    f.run()                                               # no policy: nothing
    assert rows() == []
    sandbox.preflight(f, 'worker-1')
    f.cfg['security'] = policy()                          # mode off: spawn preflight is still a no-op
    sandbox.preflight(f, 'worker-1')
    assert [x for x in rows() if x.get('reason') == 'unavailable'] == []
    f.run()                                               # policy: one row per degraded (pi) slot
    assert sorted(x['slot'] for x in rows()) == sorted(f.names)
    monkeypatch.setattr(sandbox, 'platform_name', lambda: 'darwin')
    monkeypatch.setattr(sandbox, 'which', lambda name: None)
    f.cfg['security'] = policy(mode='os')
    before = dict(f.agent('worker-1'))
    with pytest.raises(sandbox.SandboxUnavailable, match='sandbox-exec not found'):
        f.spawn('worker-1', 'p')
    refused = [x for x in rows() if x.get('reason') == 'unavailable']
    assert len(refused) == 1 and refused[0]['slot'] == 'worker-1'
    assert f.agent('worker-1') == before                  # nothing spawned, nothing recorded for the slot
