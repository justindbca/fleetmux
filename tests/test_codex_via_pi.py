"""Codex / FlashNext via pi, Claude only via the official CLI (user decision 2026-10-13).

1. per-agent pi model / thinking (--supervisor-pi-model, roster pi_model, --pi-model fallback, hot reload)
2. pi supervisor + official Claude Code workers (task and service) through the v2 daemon
3. default pair = pi supervisor on openai-codex/gpt-6-astra (thinking high) + official Claude workers;
   old --supervisor-profile fleet-astra maps to it or is refused, never a silent Codex TUI
4. night pair = pi Astra supervisor + pi FlashNext workers via the per-agent models

Real private Redis v2 + real Claude Stop hook entry; Pi bridges driven through the v2 events they
post; fake cmux and CLIs, no model. The real TUI check is scripts/smoke_pair.sh pi claude <mode>.
"""
import json
import os
import sys
import uuid
from pathlib import Path

import pytest
import yaml

import hook_sim
from test_fleet import FakeMgr
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import event, receive, session, settle
from test_v2_codex_claude import answer_turn, no_message_files, typed
from test_v2_codex_pi import pi_ready
from cmux_fleet import claude_hook, cli_pairs, cmux_manager, codex_queue, fleet, pi_adapter, runtime_v2
from cmux_fleet import fleet_config

ASTRA = 'openai-codex/gpt-6-astra'
FLASH = 'flashnext/Qwen3.8-Flash-Next-Uncensored-oQ4e-100K-MTP'
WORKERS = ['worker-1', 'worker-2']


def pair_status(sup, wrk, mode):
    return cli_pairs.load_pairs().lookup(sup, wrk, mode)['status']


def smoke_args(mode='task'):
    """--lifecycle that lets pi+claude start today: smoke while the row is unverified."""
    if pair_status('pi', 'claude', mode) == 'verified':
        return ['--lifecycle', 'dev_task' if mode == 'task' else 'service']
    return ['--lifecycle', 'smoke'] + (['--mode', 'service'] if mode == 'service' else [])


# ------------------------------------------------------------------ 3. pair table default
def test_shipped_default_is_pi_astra_supervisor_with_claude_workers():
    table = cli_pairs.load_pairs()
    assert table.default == {'supervisor_cli': 'pi', 'worker_cli': 'claude', 'mode': 'task',
                             'supervisor_pi_model': ASTRA, 'supervisor_pi_thinking': 'high'}
    for mode in ('task', 'service'):
        row = table.lookup('pi', 'claude', mode)
        assert row and row['adapter_missing'] == [] and row['status'] in ('verified', 'unverified')
        if row['status'] == 'verified':
            assert 'smoke_pair.sh pi claude ' + mode in row['verified_by'] and 'PASS' in row['verified_by']
    # 5. the official Codex TUI supervisor stays an option
    for mode in ('task', 'service'):
        assert table.lookup('codex', 'claude', mode)['status'] == 'verified'


@pytest.mark.parametrize('default,match', [
    ({'supervisor_cli': 'pi', 'worker_cli': 'claude', 'mode': 'task', 'surprise': 1}, 'unknown default key'),
    ({'supervisor_cli': 'pi', 'worker_cli': 'claude', 'mode': 'task', 'supervisor_pi_model': 'astra'}, 'provider/model'),
    ({'supervisor_cli': 'pi', 'worker_cli': 'claude', 'mode': 'task', 'supervisor_pi_thinking': 'huge'}, 'thinking'),
    ({'supervisor_cli': 'codex', 'worker_cli': 'claude', 'mode': 'task', 'supervisor_pi_model': ASTRA}, 'supervisor_cli: pi'),
    ({'supervisor_cli': 'pi', 'worker_cli': 'claude', 'mode': 'task', 'supervisor_profile': 'fleet-astra'}, 'Codex profile')])
def test_default_keys_are_validated(tmp_path, default, match):
    path = tmp_path / 'pairs.yaml'
    rows = [dict(supervisor_cli=s, worker_cli='claude', mode='task', status='verified', adapter_missing=[])
            for s in ('pi', 'codex')]
    path.write_text(yaml.safe_dump({'version': 1, 'default': default, 'pairs': rows}))
    with pytest.raises(ValueError, match=match):
        cli_pairs.load_pairs(path)


def test_bare_yaml_off_thinking_reads_as_off(tmp_path):
    path = tmp_path / 'pairs.yaml'
    path.write_text('version: 1\ndefault: {supervisor_cli: pi, worker_cli: claude, mode: task, '
                    'supervisor_pi_model: a/b, supervisor_pi_thinking: off}\n'
                    'pairs:\n  - {supervisor_cli: pi, worker_cli: claude, mode: task, status: verified}\n')
    assert cli_pairs.load_pairs(path).default['supervisor_pi_thinking'] == 'off'


# ------------------------------------------------------------------ 1/3. supervisor resolution
def parsed(argv, monkeypatch):
    seen = []
    monkeypatch.setattr(fleet, 'start', seen.append)
    fleet.main(['start'] + argv)
    monkeypatch.undo()
    return seen[0]


def resolved(argv, monkeypatch):
    args = parsed(argv, monkeypatch)
    fleet.resolve_supervisor(args, cli_pairs.load_pairs().default)
    return args


def supervisor_cfg(args):
    """What the supervisor actually runs: its own entry over the run-wide --pi-model / --pi-thinking."""
    return pi_adapter.for_agent({'pi_model': args.pi_model, 'pi_thinking': args.pi_thinking,
                                 'pi_models': args.pi_models, 'pi_thinkings': args.pi_thinkings}, 'supervisor')


def test_default_start_without_pi_flags_is_pi_astra_high(monkeypatch):
    args = resolved([], monkeypatch)
    assert (args.supervisor_cli, args.worker_cli) == ('pi', 'claude')
    assert args.pi_models == {'supervisor': ASTRA} and args.pi_thinkings == {'supervisor': 'high'}
    assert args.pi_thinking == 'high'                      # unset --pi-thinking = discovery.worker_thinking (11-11)
    cfg = supervisor_cfg(args)
    assert (cfg['pi_model'], cfg['pi_thinking']) == (ASTRA, 'high')


def test_explicit_run_wide_pi_flags_beat_the_yaml_default(monkeypatch):
    # No agent-specific model: a typed --pi-model / --pi-thinking wins over default.supervisor_pi_*.
    args = resolved(['--pi-model', FLASH, '--pi-thinking', 'low'], monkeypatch)
    assert args.supervisor_cli == 'pi' and args.pi_models == {} and args.pi_thinkings == {}
    cfg = supervisor_cfg(args)
    assert (cfg['pi_model'], cfg['pi_thinking']) == (FLASH, 'low')
    # Each flag is resolved on its own: only --pi-model typed -> thinking still from the YAML default.
    args = resolved(['--pi-model', FLASH], monkeypatch)
    cfg = supervisor_cfg(args)
    assert (cfg['pi_model'], cfg['pi_thinking']) == (FLASH, 'high')
    # Only --pi-thinking typed -> model still from the YAML default.
    args = resolved(['--pi-thinking', 'low'], monkeypatch)
    cfg = supervisor_cfg(args)
    assert (cfg['pi_model'], cfg['pi_thinking']) == (ASTRA, 'low')


def test_agent_specific_and_mapped_profile_beat_explicit_pi_flags(monkeypatch):
    args = resolved(['--supervisor-pi-model', 'other/model', '--supervisor-pi-thinking', 'medium',
                     '--pi-model', FLASH, '--pi-thinking', 'low'], monkeypatch)
    cfg = supervisor_cfg(args)
    assert (cfg['pi_model'], cfg['pi_thinking']) == ('other/model', 'medium')
    args = resolved(['--supervisor-profile', 'fleet-astra', '--pi-model', FLASH, '--pi-thinking', 'low'], monkeypatch)
    cfg = supervisor_cfg(args)
    assert (cfg['pi_model'], cfg['pi_thinking']) == (ASTRA, 'high')


def test_explicit_pi_supervisor_falls_back_to_pi_model_not_the_table_default(monkeypatch):
    args = resolved(['--supervisor-cli', 'pi', '--worker-cli', 'pi', '--pi-model', FLASH], monkeypatch)
    assert args.pi_models == {} and args.pi_thinkings == {}
    cfg = pi_adapter.for_agent({'pi_model': FLASH, 'pi_thinking': 'off', 'pi_models': {}, 'pi_thinkings': {}}, 'supervisor')
    assert (cfg['pi_model'], cfg['pi_thinking']) == (FLASH, 'off')


def test_supervisor_flags_beat_pi_model_and_table_default(monkeypatch):
    args = resolved(['--supervisor-pi-model', 'other/model', '--supervisor-pi-thinking', 'medium',
                     '--pi-model', FLASH], monkeypatch)
    assert args.pi_models == {'supervisor': 'other/model'} and args.pi_thinkings == {'supervisor': 'medium'}
    args = resolved(['--supervisor-pi-model', 'other/model'], monkeypatch)   # thinking still from the default
    assert args.pi_thinkings == {'supervisor': 'high'}
    args = resolved(['--supervisor-pi-model', 'other/model', '--pi-thinking', 'low'], monkeypatch)
    assert args.pi_thinkings == {} and supervisor_cfg(args)['pi_thinking'] == 'low'  # typed flag beats the default


def effective(args, name='supervisor'):
    cfg = pi_adapter.for_agent({'pi_model': args.pi_model, 'pi_thinking': args.pi_thinking,
                                'pi_models': args.pi_models, 'pi_thinkings': args.pi_thinkings}, name)
    return cfg['pi_model'], cfg['pi_thinking']


# The whole precedence table (worker-1 implementation.md §3, revised 2026-10-13; a typed flag is never
# overridden by the table default): model --supervisor-pi-model / mapped old profile > --pi-model >
# default (CLI defaulted only); thinking --supervisor-pi-thinking / mapped profile > --pi-thinking > default > off.
@pytest.mark.parametrize('argv,expected', [
    ([], (ASTRA, 'high')),
    (['--pi-model', FLASH], (FLASH, 'high')),
    (['--pi-model', FLASH, '--pi-thinking', 'low'], (FLASH, 'low')),
    (['--pi-thinking', 'low'], (ASTRA, 'low')),
    (['--supervisor-pi-model', 'o/m'], ('o/m', 'high')),
    (['--supervisor-pi-model', 'o/m', '--pi-model', FLASH], ('o/m', 'high')),
    (['--supervisor-pi-model', 'o/m', '--supervisor-pi-thinking', 'medium', '--pi-model', FLASH,
      '--pi-thinking', 'low'], ('o/m', 'medium')),
    (['--supervisor-cli', 'pi', '--pi-model', FLASH], (FLASH, 'high')),
    (['--supervisor-cli', 'pi', '--worker-cli', 'pi', '--pi-model', FLASH], (FLASH, 'high')),
    (['--supervisor-profile', 'fleet-astra', '--pi-model', FLASH], (ASTRA, 'high')),
    (['--profile', 'fleet-astra', '--pi-model', FLASH, '--pi-thinking', 'low'], (ASTRA, 'high')),
])
def test_supervisor_pi_model_precedence_table(monkeypatch, argv, expected):
    args = resolved(argv, monkeypatch)
    assert args.supervisor_cli == 'pi' and effective(args) == expected


def test_workers_without_own_model_take_the_run_wide_pi_model(monkeypatch):
    args = resolved(['--supervisor-cli', 'pi', '--worker-cli', 'pi', '--supervisor-pi-model', ASTRA,
                     '--supervisor-pi-thinking', 'high', '--pi-model', FLASH], monkeypatch)
    assert effective(args) == (ASTRA, 'high')
    assert effective(args, 'worker-1') == (FLASH, 'high')    # unset --pi-thinking = worker_thinking high (11-11)
    assert args.pi_models == {'supervisor': ASTRA}           # workers carry no own entry


def test_supervisor_pi_flags_need_a_pi_supervisor(monkeypatch):
    args = parsed(['--supervisor-cli', 'codex', '--supervisor-pi-model', ASTRA], monkeypatch)
    with pytest.raises(ValueError, match='need a pi supervisor'):
        fleet.resolve_supervisor(args, cli_pairs.load_pairs().default)


@pytest.mark.parametrize('flag', ['--supervisor-profile', '--profile'])
def test_old_fleet_astra_profile_maps_to_pi_astra_with_a_notice(monkeypatch, capsys, flag):
    args = resolved([flag, 'fleet-astra'], monkeypatch)
    assert args.supervisor_cli == 'pi' and args.supervisor_profile is None
    assert args.pi_models == {'supervisor': ASTRA} and args.pi_thinkings == {'supervisor': 'high'}
    err = capsys.readouterr().err
    assert 'old Codex profile' in err and ASTRA in err and '--supervisor-cli codex' in err


def test_unknown_old_profile_is_refused_with_the_new_usage(monkeypatch):
    args = parsed(['--supervisor-profile', 'fleet-sol'], monkeypatch)
    with pytest.raises(ValueError) as exc:
        fleet.resolve_supervisor(args, cli_pairs.load_pairs().default)
    assert '--supervisor-cli codex --supervisor-profile fleet-sol' in str(exc.value)
    assert '--supervisor-pi-model' in str(exc.value)
    args = parsed(['--supervisor-profile', 'fleet-astra', '--supervisor-pi-model', 'other/model'], monkeypatch)
    with pytest.raises(ValueError, match='pass only --supervisor-pi-model'):
        fleet.resolve_supervisor(args, cli_pairs.load_pairs().default)
    args = parsed(['--supervisor-cli', 'pi', '--supervisor-profile', 'fleet-astra'], monkeypatch)
    with pytest.raises(ValueError, match='a pi supervisor takes --supervisor-pi-model'):
        fleet.resolve_supervisor(args, cli_pairs.load_pairs().default)


def test_explicit_codex_supervisor_keeps_the_official_codex_tui(monkeypatch):
    args = resolved(['--supervisor-cli', 'codex', '--supervisor-profile', 'fleet-astra', '--worker-cli', 'claude'],
                    monkeypatch)
    assert args.supervisor_cli == 'codex' and args.supervisor_profile == 'fleet-astra'
    assert args.pi_models == {} and args.pi_thinkings == {}


# ------------------------------------------------------------------ 1. per-agent resolver + argv
def test_for_agent_order_and_idempotence():
    cfg = {'pi_model': FLASH, 'pi_thinking': 'off', 'pi_models': {'supervisor': ASTRA, 'worker-2': 'x/y'},
           'pi_thinkings': {'supervisor': 'high'}}
    assert pi_adapter.for_agent(cfg, 'supervisor')['pi_model'] == ASTRA
    assert pi_adapter.for_agent(cfg, 'worker-1')['pi_model'] == FLASH            # --pi-model fallback
    assert pi_adapter.for_agent(cfg, 'worker-1')['pi_thinking'] == 'off'
    assert pi_adapter.for_agent(cfg, 'worker-2')['pi_model'] == 'x/y'
    card = pi_adapter.for_agent(cfg, 'worker-2', {'pi_model': 'card/model'})   # profile card wins
    assert card['pi_model'] == 'card/model' and 'worker-2' not in card['pi_models']
    once = pi_adapter.for_agent(cfg, 'supervisor')
    assert pi_adapter.for_agent(once, 'supervisor') == once
    assert cfg['pi_model'] == FLASH                                               # input not mutated


def pi_cfg(tmp_path, **extra):
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True, exist_ok=True)
    return dict(run=str(run), python=sys.executable, pi='/bin/pi', pi_home=str(run / 'pi-home'),
                allow_unsandboxed_pi=True, transport_version=2, allow_development_v2=True, mode='task',
                supervisor_cli='pi', worker_cli='pi', workers=list(WORKERS), pi_model=FLASH, pi_thinking='off',
                pi_models={'supervisor': ASTRA}, pi_thinkings={'supervisor': 'high'}, **extra)


def flag(argv, name):
    return argv[argv.index(name) + 1]


def test_night_pair_argv_differs_per_agent(tmp_path):
    cfg = pi_cfg(tmp_path)
    sup = pi_adapter.argv(fleet.agent_cfg(cfg, 'supervisor'), 'supervisor', 0, 'p', tmp_path, label='supervisor-1')
    wrk = pi_adapter.argv(fleet.agent_cfg(cfg, 'worker-1'), 'worker-1', 0, 'p', tmp_path, label='worker-1')
    assert (flag(sup, '--model'), flag(sup, '--thinking'), flag(sup, '--name')) == (ASTRA, 'high', 'supervisor-1')
    assert (flag(wrk, '--model'), flag(wrk, '--thinking'), flag(wrk, '--name')) == (FLASH, 'off', 'worker-1')
    assert 'fleet_decision' in flag(sup, '--tools') and 'fleet_result' in flag(wrk, '--tools')


@pytest.mark.parametrize('slot,model,thinking', [('supervisor', ASTRA, 'high'), ('worker-2', FLASH, 'off')])
def test_actor_execs_pi_with_the_slot_model(tmp_path, monkeypatch, slot, model, thinking):
    cfg = pi_cfg(tmp_path)
    path = tmp_path / 'config.json'
    fleet.save(path, cfg)

    class R:
        def hgetall(self, key):
            return {'generation': '0', 'state': 'starting', 'launch_ready': '1', 'label': slot}
    monkeypatch.setattr(fleet, 'connect', lambda c: R())
    monkeypatch.setattr(fleet.os, 'chdir', lambda p: None)
    monkeypatch.setattr(fleet.os, 'environ', dict(os.environ))
    seen = []

    def execvp(prog, argv):
        seen.append(argv)
        raise SystemExit(0)
    monkeypatch.setattr(fleet.os, 'execvp', execvp)
    with pytest.raises(SystemExit):
        fleet.actor(path, slot, 'prompt', 0)
    assert (flag(seen[0], '--model'), flag(seen[0], '--thinking'), flag(seen[0], '--name')) == (model, thinking, slot)
    # The prepared exec argv is recorded (pi rewrites its process title). It proves what was about to be
    # exec'd, not that the process started on that model: acceptance still needs the live pi footer.
    record = json.loads((Path(cfg['run']) / 'prompts' / (slot + '-g0.argv.json')).read_text())
    assert record['argv'] == seen[0] and record['slot'] == slot and record['generation'] == 0


# ------------------------------------------------------------------ start up to provisioning
class Reached(Exception):
    pass


def to_provisioning(monkeypatch, *, cmux=False):
    """Record the validated start args; stop at the cmux check (cmux=False) or after the pi checks."""
    seen = []
    real = runtime_v2.validate_config
    # vars(args) is the live namespace: later start() steps (roster pi rows) still show in it.
    monkeypatch.setattr(runtime_v2, 'validate_config', lambda cfg: (seen.append(cfg), real(cfg))[1])
    if not cmux:
        monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: (_ for _ in ()).throw(Reached()))
        return seen
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    monkeypatch.setattr(fleet.shutil, 'which', lambda name: '/usr/bin/' + os.path.basename(str(name)))

    def run(argv, *a, **k):
        if argv[-1:] == ['--version']:
            raise Reached()   # past every model/permission check: pi --version is next
        raise AssertionError(argv)
    monkeypatch.setattr(fleet.subprocess, 'run', run)
    return seen


def test_default_start_reaches_provisioning_as_pi_astra_plus_claude(monkeypatch):
    seen = to_provisioning(monkeypatch, cmux=True)
    with pytest.raises(Reached):
        fleet.main(['start', '--allow-development-v2', '--allow-unsandboxed-pi', '--workers', '2'] + smoke_args())
    cfg = seen[-1]
    assert (cfg['supervisor_cli'], cfg['worker_cli'], cfg['mode']) == ('pi', 'claude', 'task')
    assert cfg['pi_models'] == {'supervisor': ASTRA} and cfg['pi_thinkings'] == {'supervisor': 'high'}
    assert not cfg.get('pi_model')                    # no --pi-model needed for the default


def test_default_start_still_needs_the_unsandboxed_pi_acknowledgement(monkeypatch):
    to_provisioning(monkeypatch, cmux=True)
    with pytest.raises(SystemExit, match='pi has no sandbox'):
        fleet.main(['start', '--allow-development-v2', '--workers', '2'] + smoke_args())


def test_unverified_default_pair_runs_only_under_smoke(monkeypatch):
    if pair_status('pi', 'claude', 'task') == 'verified':
        pytest.skip('pi+claude/task verified by a real smoke')
    to_provisioning(monkeypatch)
    with pytest.raises(SystemExit, match='lifecycle smoke'):
        fleet.main(['start', '--allow-development-v2', '--allow-unsandboxed-pi', '--lifecycle', 'dev_task'])


def test_service_mode_pi_claude_reaches_provisioning(monkeypatch):
    seen = to_provisioning(monkeypatch)
    with pytest.raises(Reached):
        fleet.main(['start', '--allow-development-v2', '--allow-unsandboxed-pi', '--supervisor-cli', 'pi',
                    '--worker-cli', 'claude', '--supervisor-pi-model', ASTRA] + smoke_args('service'))
    assert seen[-1]['mode'] == 'service' and seen[-1]['supervisor_cli'] == 'pi'


def test_old_profile_start_never_becomes_a_codex_supervisor(monkeypatch):
    seen = to_provisioning(monkeypatch)
    with pytest.raises(Reached):
        fleet.main(['start', '--allow-development-v2', '--allow-unsandboxed-pi', '--supervisor-profile',
                    'fleet-astra'] + smoke_args())
    assert seen[-1]['supervisor_cli'] == 'pi' and seen[-1]['pi_models'] == {'supervisor': ASTRA}
    monkeypatch.undo()
    to_provisioning(monkeypatch)
    with pytest.raises(SystemExit, match='--supervisor-cli codex --supervisor-profile fleet-x'):
        fleet.main(['start', '--allow-development-v2', '--allow-unsandboxed-pi', '--supervisor-profile', 'fleet-x']
                   + smoke_args())


def test_night_pair_start_per_agent_models(monkeypatch):
    seen = to_provisioning(monkeypatch, cmux=True)
    with pytest.raises(Reached):
        fleet.main(['start', '--allow-development-v2', '--allow-unsandboxed-pi', '--lifecycle', 'dev_task',
                    '--supervisor-cli', 'pi', '--worker-cli', 'pi', '--supervisor-pi-model', ASTRA,
                    '--supervisor-pi-thinking', 'high', '--pi-model', FLASH, '--workers', '2'])
    cfg = seen[-1]
    assert cfg['pi_model'] == FLASH and cfg['pi_models'] == {'supervisor': ASTRA}
    assert fleet.agent_cfg(cfg, 'worker-2')['pi_model'] == FLASH
    assert fleet.agent_cfg(cfg, 'supervisor')['pi_thinking'] == 'high'


def test_all_pi_start_without_a_worker_model_names_the_gap(monkeypatch):
    to_provisioning(monkeypatch, cmux=True)
    with pytest.raises(SystemExit, match='no model for: workers'):
        fleet.main(['start', '--allow-development-v2', '--allow-unsandboxed-pi', '--lifecycle', 'dev_task',
                    '--supervisor-cli', 'pi', '--worker-cli', 'pi', '--supervisor-pi-model', ASTRA])
    with pytest.raises(SystemExit, match='no model for: supervisor, workers'):
        fleet.main(['start', '--allow-development-v2', '--allow-unsandboxed-pi', '--lifecycle', 'dev_task',
                    '--supervisor-cli', 'pi', '--worker-cli', 'pi'])


def test_night_roster_pi_models_replace_pi_model(monkeypatch, tmp_path):
    path = tmp_path / 'night.yaml'
    path.write_text('workers:\n  - {name: worker-1, pi_model: ' + FLASH + ', pi_thinking: off}\n'
                    '  - {name: worker-2, pi_model: ' + FLASH + '}\n')
    seen = to_provisioning(monkeypatch, cmux=True)
    with pytest.raises(Reached):
        fleet.main(['start', '--allow-development-v2', '--allow-unsandboxed-pi', '--lifecycle', 'dev_task',
                    '--supervisor-cli', 'pi', '--worker-cli', 'pi', '--supervisor-pi-model', ASTRA,
                    '--supervisor-pi-thinking', 'high', '--roster', str(path)])
    cfg = seen[-1]
    assert not cfg.get('pi_model')                                  # no run-wide --pi-model needed
    assert cfg['pi_models'] == {'supervisor': ASTRA, 'worker-1': FLASH, 'worker-2': FLASH}
    assert cfg['pi_thinkings'] == {'supervisor': 'high', 'worker-1': 'off'}
    assert fleet.agent_cfg(cfg, 'worker-2')['pi_thinking'] == 'high'  # unset row -> run-wide default high (11-11)


@pytest.mark.parametrize('rows,cli,match', [
    ('  - {name: worker-1, pi_model: ' + FLASH + '}\n', 'claude', 'not pi'),
    ('  - {name: worker-1, pi_model: ' + FLASH + ', profile: flashnext-coder}\n', 'pi', 'cannot be combined with profile'),
    ('  - {name: worker-1, pi_model: astra}\n', 'pi', 'provider/model'),
    ('  - {name: worker-1, pi_thinking: loud}\n', 'pi', 'pi_thinking must be one of')])
def test_bad_roster_pi_rows_refuse_start_before_provisioning(monkeypatch, tmp_path, rows, cli, match):
    path = tmp_path / 'r.yaml'
    path.write_text('workers:\n' + rows)
    to_provisioning(monkeypatch)
    with pytest.raises(SystemExit, match=match):
        fleet.main(['start', '--allow-development-v2', '--allow-unsandboxed-pi', '--supervisor-cli', 'pi',
                    '--worker-cli', cli, '--supervisor-pi-model', ASTRA, '--pi-model', FLASH, '--roster', str(path)]
                   + (smoke_args() if cli == 'claude' else ['--lifecycle', 'dev_task']))


# ------------------------------------------------------------------ 2. pi supervisor + Claude workers (daemon)
def build(r, tmp_path, mode, worker_cli='claude', roster=None):
    r.flushdb()
    run = tmp_path / ('run-' + uuid.uuid4().hex)
    (run / 'workspace').mkdir(parents=True)
    (run / 'pi-home').mkdir()
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'], python=sys.executable,
               workers=list(WORKERS), mode=mode, supervisor_cli='pi', worker_cli=worker_cli, pi='pi',
               pi_model=FLASH if worker_cli == 'pi' else None, pi_thinking='off',
               pi_models={'supervisor': ASTRA}, pi_thinkings={'supervisor': 'high'},
               pi_home=str(run / 'pi-home'), allow_unsandboxed_pi=True, codex_home=str(tmp_path / 'codex-home'),
               transport_version=2, allow_development_v2=True, lifecycle='smoke', stall_timeout=0,
               v2_lease_ms=300000, max_busy=0)
    if roster:
        cfg['roster'] = str(roster)
    path = run / 'config.json'
    fleet.save(path, cfg)
    f = fleet.Fleet(path, r, FakeMgr(), sessions=tmp_path / 'codex-home' / 'sessions')
    f.test_claude = tmp_path / 'claude-projects'
    f.test_threads = {w: str(uuid.uuid4()) for w in WORKERS}
    real_trust = fleet.trust_claude_dir
    fleet.trust_claude_dir = lambda cwd: None  # never touch the operator's ~/.claude.json
    f.test_restore = lambda: setattr(fleet, 'trust_claude_dir', real_trust)
    f.start_all()
    r.set(f.v2.prefix + ':boot', 'running')
    pi_ready(f, 'supervisor')
    for w in WORKERS:
        if worker_cli == 'pi':
            pi_ready(f, w)
        else:
            assert answer_turn(f, w, 'READY', startup=True)['status'] == 'ready'
    return f


@pytest.fixture
def pi_claude(private_redis_v2, tmp_path):  # noqa: F811
    f = build(private_redis_v2, tmp_path, 'task')
    yield f
    f.test_restore()
    f.v2.close()


@pytest.fixture
def pi_claude_service(private_redis_v2, tmp_path):  # noqa: F811
    f = build(private_redis_v2, tmp_path, 'service')
    yield f
    f.test_restore()
    f.v2.close()


def test_pi_claude_ready_and_spawn_cli_split(pi_claude):
    f = pi_claude
    for slot in f.names:
        assert f.agent(slot)['state'] == 'idle' and f.agent(slot)['runtime_ready'] == '1'
    assert not runtime_v2.tui_slot(f.cfg, 'supervisor') and runtime_v2.tui_slot(f.cfg, 'worker-1')
    by_type = {}
    for s in f.mgr.spawned:
        by_type.setdefault(s['metadata']['cli_type'], []).append(s)
    assert sorted(by_type) == ['claude', 'pi'] and len(by_type['pi']) == 1 and len(by_type['claude']) == 2
    sup = by_type['pi'][0]
    assert sup['metadata']['profile'] == ASTRA                       # the supervisor's own model
    assert 'PI_CODING_AGENT_DIR' in sup['env_vars'] and 'CODEX_HOME' not in sup['env_vars']
    for s in by_type['claude']:
        assert not any(k.startswith('PI_') for k in s['env_vars'])   # Claude never goes through pi
    assert 'fleet_result' not in (fleet.agent_cwd(f.cfg, 'worker-1') / 'CLAUDE.md').read_text()
    assert 'fleet_decision' in (fleet.agent_cwd(f.cfg, 'supervisor') / 'AGENTS.md').read_text()


def test_pi_supervisor_task_dispatches_to_claude_and_completes(pi_claude):
    f = pi_claude
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='t1', spec={'goal': 'write the parts'}), 't1')
    assert f.v2.step(10)
    f.pump()
    env = receive(f, 'supervisor')
    assert env and 'write the parts' in env['body']['text'] and not typed(f, 'supervisor')
    dispatch = {'status': 'RUNNING', 'completion': 10, 'reason': 'split',
                'dispatch': [{'to': w, 'instruction': 'Write ' + w + '/part.md with the findings.'} for w in WORKERS],
                'artifacts': [{'path': w + '/part.md', 'contains': ['findings']} for w in WORKERS]}
    settle(f, 'supervisor', env, dispatch)
    f.pump()
    for w in WORKERS:
        text, = typed(f, w)
        assert 'Write ' + w + '/part.md' in text and 'fleet_result' not in text
        # Claude workers under a pi supervisor take the Stop-hook channel too: never pasted (no-typing batch)
        assert f.agent(w)['pane'] not in dict(getattr(f.mgr, 'bracketed', []))
        assert text in hook_sim.handed_to(f, w)
        out = f.workspace() / w / 'part.md'
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text('findings')
        report = 'Done:\n- wrote part.md\nEvidence:\n- ' + w + '/part.md\nNot done: none\nBlocked: none'
        assert answer_turn(f, w, report)['status'] == 'committed'
        f.pump()
    reports = []
    for _ in range(4):   # each report reaches the pi supervisor as its own business delivery
        f.pump()
        env = receive(f, 'supervisor')
        if not env:
            break
        reports.append(env['body']['text'])
        last = len(reports) == 2
        settle(f, 'supervisor', env, {'status': 'COMPLETED', 'completion': 100, 'dispatch': [], 'reason': 'verified'}
               if last else {'status': 'RUNNING', 'completion': 60, 'reason': 'waiting', 'dispatch': []})
    assert any('[worker-1 report]' in t or '[worker-2 report]' in t for t in reports)
    assert f.task()['status'] == 'completed'
    assert no_message_files(f)


def test_pi_supervisor_and_hook_claude_workers_each_keep_their_own_channel(pi_claude):
    """No-typing batch routing: a pi supervisor is never on the Codex queue or the Claude hook channel
    (its extension takes deliveries); its Claude workers get theirs only from their Stop hook."""
    f = pi_claude
    assert not codex_queue.is_codex(f.cfg, 'supervisor') and not f.v2.hooked('supervisor')
    assert all(f.v2.hooked(w) and not codex_queue.is_codex(f.cfg, w) for w in WORKERS)
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='t1', spec={'goal': 'route check'}), 't1')
    assert f.v2.step(10)
    f.pump()
    env = receive(f, 'supervisor')
    assert env and not typed(f, 'supervisor') and not f.r.hget(fleet.key('agent', 'supervisor'), 'cq_token')
    assert not f.r.exists(claude_hook.keys(f.v2.bus.prefix, 'supervisor', 0)['mailbox'])
    settle(f, 'supervisor', env, {'status': 'RUNNING', 'completion': 10, 'reason': 'go',
                                  'dispatch': [{'to': 'worker-1', 'instruction': 'Write worker-1/part.md now.'}]})
    f.pump()
    assert [t for t in hook_sim.handed_to(f, 'worker-1') if 'Write worker-1/part.md now.' in t]
    assert not f.mgr.sent and not getattr(f.mgr, 'queued', [])   # nothing pasted, nothing codex-queued


def test_pi_supervisor_service_request_reaches_claude_and_card_is_off_model(pi_claude_service):
    f = pi_claude_service
    spec = dict(id='r1', payload={'message': 'say OK'}, reply={'required': ['reply'], 'allowed': ['reply']})
    event(f, dict(kind='request', request_id='cmd-r1', spec=spec))
    f.pump()
    env = receive(f, 'supervisor')
    route = {'status': 'RUNNING', 'skip': [],
             'dispatch': [{'to': 'worker-1', 'request_id': 'r1', 'instruction': 'Reply OK in the reply field.'}]}
    settle(f, 'supervisor', env, route)
    f.pump()
    assert 'Reply OK in the reply field.' in typed(f, 'worker-1')[-1]
    assert answer_turn(f, 'worker-1', '{"reply": "OK"}')['status'] == 'committed'
    assert json.loads(f.r.get(fleet.RESULT + 'r1'))['status'] == 'ok'
    assert json.loads(f.r.get(fleet.RESULT + 'r1'))['result'] == {'reply': 'OK'}
    f.pump()
    assert not f.v2.depth('supervisor')                     # no mechanical relay turn for the pi supervisor
    assert f.r.xlen(f.v2.prefix + ':ui:supervisor') > 0      # the result is a UI-only card
    assert no_message_files(f)


def test_pi_claude_stale_and_manual_worker_turns_do_not_settle(pi_claude):
    f = pi_claude
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='t1', spec={'goal': 'one part'}), 't1')
    assert f.v2.step(10)
    f.pump()
    env = receive(f, 'supervisor')
    settle(f, 'supervisor', env, {'status': 'RUNNING', 'completion': 10, 'reason': 'go',
                                  'dispatch': [{'to': 'worker-1', 'instruction': 'Write worker-1/part.md.'}]})
    f.pump()
    lease = f.v2.bus.slot_state('worker-1')
    assert answer_turn(f, 'worker-1', 'chatting', prompt='operator typed this')['status'] == 'ignored'
    assert answer_turn(f, 'worker-1', 'late', generation=5)['status'] == 'stale'
    assert f.v2.bus.slot_state('worker-1')['current_delivery'] == lease['current_delivery']


# ------------------------------------------------------------------ 1/4. roster pi_model hot reload (all-pi night pair)
def write_roster(f, rows):
    path = Path(f.cfg['roster'])
    path.write_text('workers:\n' + ''.join('  - {' + ', '.join(k + ': ' + str(v) for k, v in row.items()) + '}\n'
                                           for row in rows))
    stamp = getattr(f, 'test_stamp', 1_700_000_000_000_000_000) + 1_000_000_000
    f.test_stamp = stamp
    os.utime(path, ns=(stamp, stamp))
    return path


def audits(f, name):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == name]


@pytest.fixture
def night(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    path = tmp_path / 'roster.yaml'
    path.write_text('workers:\n  - {name: worker-1}\n  - {name: worker-2}\n')
    real_run = fleet.subprocess.run
    monkeypatch.setattr(fleet.shutil, 'which', lambda name: '/usr/bin/' + os.path.basename(str(name)))
    monkeypatch.setattr(fleet.subprocess, 'run', lambda argv, *a, **k: (
        fleet.subprocess.CompletedProcess(argv, 0, '1.1.0\n', '') if argv[-1:] == ['--version'] else real_run(argv, *a, **k)))
    f = build(private_redis_v2, tmp_path, 'task', worker_cli='pi', roster=path)
    f.test_stamp = 1_700_000_000_000_000_000
    os.utime(path, ns=(f.test_stamp, f.test_stamp))
    f.roster_tick()
    yield f
    f.test_restore()
    f.v2.close()


def spawned_models(f, slot):
    label_prefix = 'fx-' + slot
    return [s['metadata']['profile'] for s in f.mgr.spawned if s['window_name'].startswith(label_prefix)]


def test_roster_pi_model_hot_change_moves_only_that_slot_to_its_next_generation(night):
    f = night
    assert spawned_models(f, 'worker-2') == [FLASH] and spawned_models(f, 'supervisor') == [ASTRA]
    write_roster(f, [{'name': 'worker-1'}, {'name': 'worker-2', 'pi_model': 'flashnext/other-model', 'pi_thinking': 'low'}])
    f.tick()
    changed, = audits(f, 'roster.changed')
    assert 'pi_model flashnext/other-model' in json.dumps(changed, ensure_ascii=False)
    for _ in range(5):
        f.tick()
    assert f.agent('worker-2')['generation'] == '1' and f.agent('worker-2')['label'] == 'worker-2.1'
    assert spawned_models(f, 'worker-2')[-1] == 'flashnext/other-model'
    assert f.runtime_cfg('worker-2')['pi_thinking'] == 'low'
    assert f.agent('worker-1')['generation'] == '0' and spawned_models(f, 'worker-1') == [FLASH]
    assert fleet.load(f.path)['pi_models'].get('worker-2') == 'flashnext/other-model'


def test_bad_hot_pi_model_is_rejected_whole_and_keeps_the_running_roster(night):
    f = night
    write_roster(f, [{'name': 'worker-1', 'pi_model': 'nomodel'}, {'name': 'worker-2'}])
    f.tick()
    assert audits(f, 'roster.rejected') and not audits(f, 'roster.changed')
    assert f.agent('worker-1')['generation'] == '0' and f.workers == WORKERS
    assert f.runtime_cfg('worker-1')['pi_model'] == FLASH


def test_hot_added_roster_pi_model_counts_against_the_cap(night):
    f = night   # worker-1/2 already run FlashNext (run-wide --pi-model), its card allows 2
    write_roster(f, [{'name': 'worker-1'}, {'name': 'worker-2'}, {'name': 'worker-3', 'pi_model': FLASH}])
    f.tick()
    rejected, = audits(f, 'roster.rejected')
    assert 'exceed max_concurrent' in rejected['errors'] and f.workers == WORKERS
    # The row's own model decides: on another model the same add is not capped by FlashNext.
    write_roster(f, [{'name': 'worker-1'}, {'name': 'worker-2'}, {'name': 'worker-3', 'pi_model': 'other/model'}])
    f.tick()
    assert len(audits(f, 'roster.rejected')) == 1 and 'worker-3' in f.workers
    assert f.runtime_cfg('worker-3')['pi_model'] == 'other/model'


# ------------------------------------------------------------------ 1. per-slot pi models count against model cards
def test_roster_pi_models_count_against_the_model_card_cap():
    from cmux_fleet import model_usage
    from cmux_fleet.agent_registry import AgentRegistry
    registry = AgentRegistry()
    cap = registry.model('flashnext').max_concurrent
    slots = ['worker-' + str(i) for i in range(1, cap + 2)]
    own = {'worker_cli': 'pi', 'pi_models': {w: FLASH for w in slots}}   # roster pi_model, no --pi-model
    assert 'exceed max_concurrent ' + str(cap) in model_usage.default_problem(own, slots, registry)
    assert model_usage.default_problem(own, slots[:cap], registry) is None
    assert any('exceed max_concurrent' in e for e in model_usage.check_slots(own, slots, {}, registry))
    # Same slots split across two models: only the FlashNext ones count against its card.
    mixed = {'worker_cli': 'pi', 'pi_model': 'other/model', 'pi_models': {w: FLASH for w in slots[:cap]}}
    assert model_usage.default_problem(mixed, slots, registry) is None
    usage = model_usage.Usage.from_agents({w: {'state': 'idle'} for w in slots}, own, registry)
    assert usage.holders('flashnext') == sorted(slots)


def test_start_refuses_roster_pi_models_over_the_cap(monkeypatch, tmp_path):
    from cmux_fleet.agent_registry import AgentRegistry
    cap = AgentRegistry().model('flashnext').max_concurrent
    path = tmp_path / 'roster.yaml'
    path.write_text('workers:\n' + ''.join('  - {name: worker-%d, pi_model: %s}\n' % (i, FLASH)
                                           for i in range(1, cap + 2)))
    to_provisioning(monkeypatch, cmux=True)
    with pytest.raises(SystemExit, match='exceed max_concurrent'):
        fleet.main(['start', '--allow-development-v2', '--allow-unsandboxed-pi', '--lifecycle', 'dev_task',
                    '--supervisor-cli', 'pi', '--worker-cli', 'pi', '--supervisor-pi-model', ASTRA,
                    '--roster', str(path)])


# ------------------------------------------------------------------ close_all: cmux refuses a workspace's last surface
def fake_cmux(monkeypatch, live):
    """cmux as observed in the real e2e: close-surface on a workspace's last surface fails with
    invalid_state; close-workspace closes the workspace with whatever is left in it."""
    from types import SimpleNamespace
    calls = []

    def run(*args):
        calls.append(args)
        if args[0] == 'close-surface':
            ws, sid = args[args.index('--workspace') + 1], args[args.index('--surface') + 1]
            if len(live.get(ws, {})) == 1:
                return SimpleNamespace(returncode=1, stdout='', stderr='Error: invalid_state: Cannot close the last surface')
            live[ws].pop(sid, None)
        if args[0] == 'close-workspace':
            live.pop(args[args.index('--workspace') + 1], None)
        return SimpleNamespace(returncode=0, stdout='OK', stderr='')
    monkeypatch.setattr(cmux_manager, '_run', run)
    monkeypatch.setattr(cmux_manager, '_wait_until', lambda check, timeout=3.0: check())
    return calls


def test_close_all_closes_a_workspace_left_with_only_its_own_last_surface(monkeypatch, tmp_path):
    live = {'ws': {'sup': {}, 'w1': {}, 'hist': {}}}
    calls = fake_cmux(monkeypatch, live)
    mgr = cmux_manager.CmuxSessionManager('cmux-fleet', state_dir=tmp_path)
    monkeypatch.setattr(mgr, '_surfaces', lambda ws: {s: {'tty': None} for s in live.get(ws, {})})
    mgr._save_meta({'workspace': 'ws', 'control_workspace': 'ws',
                    'surfaces': {'sup': {}, 'w1': {}, 'hist': {'type': 'history'}}})
    assert mgr.close_all() and 'ws' not in live
    assert mgr._meta()['surfaces'] == {}
    assert sum(c[0] == 'close-workspace' for c in calls) == 1           # same id for both keys: closed once
    assert not any('--force' in c for c in calls)


def test_close_all_never_closes_a_workspace_holding_a_foreign_surface(monkeypatch, tmp_path):
    live = {'ws': {'hist': {}, 'user-tab': {}}}
    calls = fake_cmux(monkeypatch, live)
    mgr = cmux_manager.CmuxSessionManager('cmux-fleet', state_dir=tmp_path)
    monkeypatch.setattr(mgr, '_surfaces', lambda ws: {s: {'tty': None} for s in live.get(ws, {})})
    mgr._save_meta({'workspace': 'ws', 'control_workspace': None, 'surfaces': {'hist': {'type': 'history'}}})
    # hist closes (not the last one); the user's tab keeps the workspace open and untouched
    assert not mgr.close_all() and live == {'ws': {'user-tab': {}}}
    assert not any(c[0] == 'close-workspace' for c in calls)


# S1a 2026-10-13 live capture (worker-1 pane, pi+claude task): the shell's exec line sits above the trust box.
S1A_RUN = '/private/tmp/cfsmoke-s1a/home/runs/fleet-20261013-113045-736c45ee'
S1A_TRUST = (
    'exec /bin/bash ' + S1A_RUN + '/prompts/fx-worker-1-3a4cb2.sh\n'
    'Last login: Tue Oct 13 11:30:46 on ttys040\n'
    'user@host project % exec /bin/bash ' + S1A_RUN + '/prompts/fx-worker-1-3a4cb2.sh\n'
    '🚀 shell banner\n\n' + '─' * 99 + '\n'
    ' Accessing workspace:\n\n {path}\n\n'
    ' Quick safety check: Is this a project you created or one you trust? (Like your own code, a\n'
    ' well-known open source project, or work from your team).\n\n'
    " Claude Code'll be able to read, edit, and execute files here.\n\n Security guide\n\n"
    ' ❯ No, exit\n   Yes, I trust this folder\n\n Enter to confirm · Esc to cancel\n')


def test_trust_dialog_reads_only_its_own_box_not_the_shell_exec_line_above():
    from cmux_fleet import watchdog
    table = watchdog.parse_table(fleet_config.block('watchdog_prompts'))
    own = S1A_RUN + '/workspace/worker-1'
    screen = S1A_TRUST.format(path=own)
    entry = watchdog.match_prompt(table, screen)[0]
    assert entry['name'] == 'trust_folder'
    ok, why = watchdog.authorize(entry, screen, table['tail_lines'], own, S1A_RUN + '/workspace')
    assert ok and why == own
    # still held: a sibling's directory in the box, or a box with no path (only the shell line names one)
    sibling = S1A_TRUST.format(path=S1A_RUN + '/workspace/worker-2')
    assert watchdog.authorize(entry, sibling, 25, own, S1A_RUN + '/workspace')[0] is False
    nopath = S1A_TRUST.replace(' Accessing workspace:\n\n {path}\n\n', '')
    assert watchdog.authorize(entry, nopath, 25, own, S1A_RUN + '/workspace') == (
        False, 'no path on the dialog: its scope cannot be checked')
    # read_outside (paths_in_run) still checks the whole tail
    outside = {'name': 'read_outside', 'verify': 'paths_in_run'}
    assert watchdog.authorize(outside, screen, 25, own, S1A_RUN + '/workspace')[0] is False
