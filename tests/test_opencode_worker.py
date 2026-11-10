"""opencode as the fourth worker CLI: registration and daemon-side branches (brief fleet-opencode-worker-20261016).

The plugin / adapter / bridge contract (delivery-id binding inside the plugin, model lock on the assistant
message, permission interception) is covered by worker-1's own tests; here the fleet side: an unknown CLI is
never run as Codex, opencode is worker-only and v2-only, the four pair rows start unverified, the free-tier
200K window drives the handoff check, and `start` takes the model name only from configuration.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_fleet import FakeRedis, cfx  # noqa: F401
from test_pi import mock_start
from cmux_fleet import (advisor, brake, cli_pairs, fleet, model_usage, opencode_adapter, profile_launch, roster,
                        runtime_v2)
from cmux_fleet.agent_registry import AgentRegistry

MODEL = 'opencode/deepseek-v4.1-flash-free'
OC = {'supervisor_cli': 'pi', 'worker_cli': 'opencode', 'transport_version': 2, 'allow_development_v2': True,
      'opencode_model': MODEL}


# ---------------------------------------------------------------- 1. no silent Codex fallback
@pytest.mark.parametrize('cli', ['gemini', 'opencode-cli', 'Codex', 'bash'])
def test_unknown_worker_cli_is_refused_not_run_as_codex(cli):
    with pytest.raises(ValueError, match='unknown worker CLI'):
        fleet.agent_cli({'worker_cli': cli}, 'worker-1')
    with pytest.raises(ValueError, match='unknown CLI'):
        runtime_v2.tui_slot({'worker_cli': cli, 'transport_version': 2}, 'worker-1')
    assert 'unknown worker CLI' in cli_pairs.pair_problem({'supervisor_cli': 'pi', 'worker_cli': cli, 'mode': 'task'})


def test_unset_cli_still_means_the_original_codex_and_opencode_is_recognised():
    assert fleet.agent_cli({}, 'worker-1') == fleet.agent_cli({}, 'supervisor') == 'codex'  # pre-choice configs
    assert fleet.agent_cli({'worker_cli': 'opencode'}, 'worker-1') == 'opencode'
    assert fleet.is_opencode({'worker_cli': 'opencode'}, 'worker-1') and fleet.is_bridge({'worker_cli': 'opencode'}, 'worker-1')
    assert not fleet.is_codex({'worker_cli': 'opencode'}, 'worker-1')
    assert fleet.agent_cli({'worker_cli': 'opencode'}, fleet.ADVISOR) == 'claude'  # the advisor is always Claude


def test_opencode_is_worker_only():
    with pytest.raises(ValueError, match='unknown supervisor CLI'):
        fleet.agent_cli({'supervisor_cli': 'opencode'}, 'supervisor')
    with pytest.raises(ValueError, match='only as a worker'):
        runtime_v2.validate_config(dict(OC, supervisor_cli='opencode'))
    assert 'opencode is worker-only' in cli_pairs.pair_problem({'supervisor_cli': 'opencode', 'worker_cli': 'opencode'})


def test_opencode_slots_run_the_bridge_not_an_official_tui():
    v2 = {'transport_version': 2}
    assert runtime_v2.tui_slot(dict(v2, worker_cli='opencode'), 'worker-1') is False
    assert runtime_v2.tui_slot(dict(v2, worker_cli='pi'), 'worker-1') is False
    assert runtime_v2.tui_slot(dict(v2, worker_cli='claude'), 'worker-1') is True
    assert runtime_v2.tui_slot(dict(v2, worker_cli='codex'), 'worker-1') is True
    assert runtime_v2.tui_slot(dict(v2, supervisor_cli='pi'), 'supervisor') is False


def test_bridge_session_is_validated_by_the_slots_own_adapter(monkeypatch):
    seen = []
    monkeypatch.setattr(opencode_adapter, 'event', lambda cfg, name, gen, data: seen.append(('oc', data['kind'])) or data)
    monkeypatch.setattr(runtime_v2.pi_adapter, 'event', lambda cfg, name, gen, data: seen.append(('pi', data['kind'])) or data)
    runtime_v2.bridge_event({'worker_cli': 'opencode'}, 'worker-1', 0, {'kind': 'bridge_session'})
    runtime_v2.bridge_event({'worker_cli': 'pi'}, 'worker-1', 0, {'kind': 'bridge_session'})
    assert seen == [('oc', 'opencode_session'), ('pi', 'pi_session')]
    with pytest.raises(ValueError, match='only pi / opencode'):
        runtime_v2.bridge_event({'worker_cli': 'codex'}, 'worker-1', 0, {'kind': 'bridge_session'})


def test_opencode_needs_transport_v2():
    with pytest.raises(ValueError, match='only on transport v2'):
        runtime_v2.validate_config(dict(OC, transport_version=1))


def test_actor_execs_the_opencode_adapter_argv_never_codex(tmp_path, monkeypatch):
    cfg = dict(OC, run=str(tmp_path), workers=['worker-1'], python='python3', mode='task')
    calls = {}
    monkeypatch.setattr(fleet, 'load', lambda path: dict(cfg))
    monkeypatch.setattr(fleet.runtime_v2, 'validate_config', lambda c: 2)
    monkeypatch.setattr(fleet, 'connect', lambda c: SimpleNamespace(
        hgetall=lambda k: {'generation': '0', 'launch_ready': '1', 'label': 'worker-1', 'state': 'starting'}))
    monkeypatch.setattr(fleet, 'agent_cwd', lambda c, n: tmp_path)
    monkeypatch.chdir(tmp_path)  # actor chdirs into the agent cwd: keep the suite's cwd restored
    monkeypatch.setattr(fleet.opencode_adapter, 'environment',
                        lambda *a, **k: calls.update(env_kw=k) or {'CMUX_FLEET_OC_SLOT': 'worker-1'})
    monkeypatch.setattr(fleet.opencode_adapter, 'argv', lambda *a, **k: ['opencode', '--standalone', str(tmp_path)])
    monkeypatch.setattr(fleet, 'actor_argv', lambda *a, **k: pytest.fail('opencode must never take the Codex argv'))
    monkeypatch.setattr(fleet.os, 'execvp', lambda exe, argv: calls.update(exe=exe, argv=argv))
    monkeypatch.setattr(fleet.os, 'environ', {})
    launch = opencode_adapter.launch_cwd(cfg, 'worker-1', 0)
    launch.mkdir(parents=True)  # spawn's prepare_instance creates it before the actor runs
    fleet.actor(tmp_path / 'config.json', 'worker-1', 'start', generation=0)
    assert calls['argv'][:2] == ['opencode', '--standalone']
    # the plugin binds the startup turn by startup_prompt; task_dir sets the external_directory allow list
    assert calls['env_kw'] == {'label': 'worker-1', 'startup_prompt': 'start', 'task_dir': tmp_path}
    assert Path(os.getcwd()).resolve() == launch.resolve()  # opencode runs in the empty launch cwd, not the task dir
    assert json.loads((tmp_path / 'prompts' / 'worker-1-g0.argv.json').read_text())['argv'] == calls['argv']


# ---------------------------------------------------------------- registration tables
def test_opencode_pair_rows_are_verified_only_where_a_real_smoke_passed():
    table = cli_pairs.load_pairs()
    rows = {k: v for k, v in table.rows.items() if k[1] == 'opencode'}
    assert sorted(rows) == [('codex', 'opencode', 'service'), ('codex', 'opencode', 'task'),
                            ('pi', 'opencode', 'service'), ('pi', 'opencode', 'task')]
    smoked = {('codex', 'opencode', 'task'), ('pi', 'opencode', 'service')}  # S1 / S2 real smokes, 4/4
    for (sup, wrk, mode), row in rows.items():
        assert row['adapter_missing'] == []
        cfg = {'supervisor_cli': sup, 'worker_cli': wrk, 'mode': mode}
        if (sup, wrk, mode) in smoked:
            assert row['status'] == 'verified' and row['verified_by']
            assert f'scripts/smoke_pair.sh {sup} opencode {mode}: ' in row['verified_by']
            assert '4/4 checks PASS' in row['verified_by']
            assert cli_pairs.pair_problem(cfg) is None  # runs without --lifecycle smoke
        else:
            assert row['status'] == 'unverified' and row['verified_by'] is None
            assert 'only with --lifecycle smoke' in cli_pairs.pair_problem(cfg)
        assert cli_pairs.pair_problem(dict(cfg, lifecycle='smoke')) is None
    assert table.lookup('claude', 'opencode', 'task') is None  # not listed -> refused
    assert cli_pairs.pair_problem({'supervisor_cli': 'claude', 'worker_cli': 'opencode', 'mode': 'task'})


def test_pair_table_refuses_an_opencode_supervisor_row(tmp_path):
    with pytest.raises(ValueError, match='bad cli pair row'):
        cli_pairs.PairTable({'version': 1, 'pairs': [{'supervisor_cli': 'opencode', 'worker_cli': 'pi',
                                                      'mode': 'task', 'status': 'unverified'}]})


def test_model_card_registers_the_free_tier_without_a_code_default():
    card = AgentRegistry().model('opencode-flash-free')
    assert (card.cli, card.model, card.context_window, card.max_concurrent) == ('opencode', MODEL, 200000, 2)
    assert 'deepseek' not in json.dumps(fleet.DEFAULTS) and not hasattr(opencode_adapter, 'MODEL')
    assert model_usage.default_model({'worker_cli': 'opencode', 'opencode_model': MODEL}, AgentRegistry()) == 'opencode-flash-free'
    assert model_usage.default_model({'worker_cli': 'opencode'}, AgentRegistry()) is None


def test_model_keys_per_cli_include_opencode():
    assert brake.MODEL_KEY['opencode'] == fleet.MODEL_KEYS['opencode'] == 'opencode_model'
    assert advisor.runtime_model({'opencode_model': MODEL, 'codex_model': 'gpt'}, 'opencode') == MODEL


def test_profile_card_for_opencode_takes_its_model_from_the_card_or_config():
    card = SimpleNamespace(cli='opencode', profile=None, model=None, role_file=None, id='oc')
    with pytest.raises(Exception, match='requires a model'):
        profile_launch.resolve_launch(card, None, {})
    launch = profile_launch.resolve_launch(card, None, {'opencode_model': MODEL})
    assert launch['overrides'] == {'worker_cli': 'opencode', 'opencode_model': MODEL}


def test_roster_handoff_table_accepts_opencode_keys():
    assert roster.handoff_errors({'handoff': {'opencode': {'window': 200000}, 'opencode:' + MODEL: {'ratio': .5}}}) == []
    assert roster.handoff_errors({'handoff': {'gemini': {'ratio': .5}}})


# ---------------------------------------------------------------- 5. handoff threshold (200K free tier)
def test_builtin_opencode_window_is_200k_not_measured():
    lim = fleet.handoff_limits
    assert lim('opencode', MODEL, {'handoff_ratio': .6}) == (200000, .6, 'window: opencode window; ratio: --handoff-ratio')
    assert lim('opencode', MODEL, {'handoff_ratio': .6, 'opencode_window': 150000})[0] == 150000
    table = {'opencode:' + MODEL: {'ratio': .5}}
    assert lim('opencode', MODEL, {'handoff_ratio': .6}, table)[:2] == (200000, .5)


def test_opencode_usage_reads_bridge_tokens_and_rotates_at_ratio_times_200k(cfx):
    cfx.cfg.update(worker_cli='opencode', opencode_model=MODEL)
    cfx.setf('worker-1', ctx_used=119999, ctx_window=1000000)  # a self-reported 1M window is ignored
    used, window = cfx.usage('worker-1')
    ratio = float(cfx.agent('worker-1')['handoff_ratio'])
    assert (used, window, ratio) == (119999, 200000, .6) and used < ratio * window
    cfx.setf('worker-1', ctx_used=120000)
    used, window = cfx.usage('worker-1')
    assert used >= ratio * window
    assert cfx.agent('worker-1')['handoff_source'] == 'window: opencode window; ratio: --handoff-ratio'


def test_watchdog_activity_of_an_opencode_slot_is_its_bridge_transcript(cfx, tmp_path):
    log = tmp_path / 'ses_1.jsonl'
    log.write_text('{}\n')
    import os
    os.utime(log, (2_000_000_000, 2_000_000_000))
    assert cfx.last_activity({'busy_since': '1', 'transcript': str(log), 'thread': 'ses_1'}) == 2_000_000_000


# ---------------------------------------------------------------- start
def oc_start(tmp_path, monkeypatch, version='opencode v2.0.29'):
    args, ran = mock_start(tmp_path, monkeypatch)

    def run(argv, **kw):
        ran.append(argv)
        out = version if argv[0].endswith('opencode') else '1.1.0'
        return SimpleNamespace(stdout=out + '\n', returncode=0)
    monkeypatch.setattr(fleet.subprocess, 'run', run)
    args[args.index('--worker-cli') + 1] = 'opencode'
    return args + ['--lifecycle', 'smoke'], ran


def test_start_opencode_workers_takes_the_model_from_config_and_defaults_max_busy_2(tmp_path, monkeypatch):
    args, ran = oc_start(tmp_path, monkeypatch)
    fleet.main(args + ['--opencode-model', MODEL])
    cfg = fleet.load(tmp_path / 'latest.json')
    assert (cfg['worker_cli'], cfg['opencode_model'], cfg['opencode_window'], cfg['max_busy']) == \
        ('opencode', MODEL, 200000, 2)
    assert cfg['opencode'] == '/bin/opencode' and ['/bin/opencode', '--version'] in ran


def test_start_keeps_an_explicit_max_busy(tmp_path, monkeypatch):
    args, _ = oc_start(tmp_path, monkeypatch)
    fleet.main(args + ['--opencode-model', MODEL, '--max-busy', '0'])
    assert fleet.load(tmp_path / 'latest.json')['max_busy'] == 0


def test_non_opencode_start_keeps_the_old_max_busy_default(tmp_path, monkeypatch):
    args, _ = mock_start(tmp_path, monkeypatch)
    fleet.main(args)
    cfg = fleet.load(tmp_path / 'latest.json')
    assert cfg['max_busy'] == 0 and 'opencode_model' not in cfg


@pytest.mark.parametrize('extra, version, why', [
    ([], 'opencode v2.0.29', '--opencode-model provider/model is required'),
    (['--opencode-model', 'deepseek-v4.1-flash-free'], 'opencode v2.0.29', '--opencode-model provider/model is required'),
    (['--opencode-model', MODEL], 'opencode v2.0.28', r'opencode >= 2\.0\.29 is required'),
    (['--opencode-model', MODEL], 'garbage', r'opencode >= 2\.0\.29 is required'),
])
def test_start_refuses_opencode_without_model_or_with_an_old_client(tmp_path, monkeypatch, extra, version, why):
    args, ran = oc_start(tmp_path, monkeypatch, version)
    with pytest.raises(SystemExit, match=why):
        fleet.main(args + extra)
    assert not any('--unixsocket' in a for a in ran)  # refused before the private Redis starts


def test_start_refuses_a_missing_opencode_binary(tmp_path, monkeypatch):
    args, _ = oc_start(tmp_path, monkeypatch)
    monkeypatch.setattr(fleet.shutil, 'which', lambda name: None if name in ('codex', 'claude', 'caffeinate', 'opencode')
                        else '/bin/' + name)
    with pytest.raises(SystemExit, match='binaries not found: opencode'):
        fleet.main(args + ['--opencode-model', MODEL])


def test_argparse_offers_opencode_for_workers_only(tmp_path, monkeypatch, capsys):
    args, _ = oc_start(tmp_path, monkeypatch)
    args[args.index('--supervisor-cli') + 1] = 'opencode'
    with pytest.raises(SystemExit):
        fleet.main(args + ['--opencode-model', MODEL])
    assert "invalid choice: 'opencode'" in capsys.readouterr().err


def test_unverified_opencode_pair_needs_the_smoke_lifecycle(tmp_path, monkeypatch):
    args, _ = oc_start(tmp_path, monkeypatch)
    args = args[:-2]  # drop --lifecycle smoke: mock_start's dev_task lifecycle remains
    with pytest.raises(SystemExit, match='only with --lifecycle smoke'):
        fleet.main(args + ['--opencode-model', MODEL])


# 2-4 (daemon side of the plugin contract: delivery binding, model lock, permission routing): tests/test_opencode_daemon.py
