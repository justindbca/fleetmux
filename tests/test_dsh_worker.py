"""dsh (DeepSeek Harness) as a worker CLI (design §41 v1, plan dsh-fleet-worker-20261104 §6): registration,
adapter (model + fleet patches from pi's models.json, no key in any file / argv, per-pane HOME), start
preflight (exact dsh / dsh-tui versions, the endpoint lists the model, default model from fleet.yaml), actor
exec of the dsh-TUI, the fleet_result role and usage from the bridge.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from test_fleet import cfx  # noqa: F401
from test_pi import mock_start
from cmux_fleet import (advisor, agent_bridge, brake, cli_pairs, dsh_adapter, fleet, model_usage, roster,
                        runtime_v2)
from cmux_fleet.agent_registry import AgentRegistry

MODEL = 'flashnext/Qwen3.8-Flash-Next-Uncensored-MLX-Serve-4bit'
KEY = 'sk-test-not-a-real-key'
DSH = {'supervisor_cli': 'pi', 'worker_cli': 'dsh', 'transport_version': 2, 'allow_development_v2': True,
       'dsh_model': MODEL}


def models_dir(tmp_path, key=KEY):
    root = tmp_path / 'user-pi'
    root.mkdir(exist_ok=True)
    (root / 'models.json').write_text(json.dumps({'providers': {'flashnext': {
        'baseUrl': 'http://127.0.0.1:9/v1', 'api': 'openai-completions', 'apiKey': key,
        'models': [{'id': MODEL.split('/', 1)[1], 'name': 'Flash Next', 'contextWindow': 400000, 'maxTokens': 8192,
                    'reasoning': False, 'cost': {'input': 0}}]}}}))
    return root


def tui_home(tmp_path, version=dsh_adapter.TUI_VERSION):
    home = tmp_path / 'dsh-home'
    manifest = home / 'profiles' / 'dsh-tui' / 'node_modules' / '@deepseek-harness-tui' / 'dsh-tui' / 'package.json'
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({'name': '@deepseek-harness-tui/dsh-tui', 'version': version}))
    return home


def cfg_for(tmp_path, **extra):
    return dict(DSH, run=str(tmp_path / 'run'), pi_agent_dir=str(models_dir(tmp_path)), python='/py',
                dsh='/opt/dsh/bin.js', dsh_node='/opt/node', workers=['worker-1'], mode='task',
                dsh_home=str(tui_home(tmp_path)), **extra)


# ---------------------------------------------------------------- registration
def test_dsh_is_a_known_worker_only_bridge_cli():
    assert 'dsh' in cli_pairs.WORKER_CLIS and 'dsh' not in cli_pairs.CLIS and 'dsh' in roster.CLIS
    assert fleet.agent_cli({'worker_cli': 'dsh'}, 'worker-1') == 'dsh'
    assert fleet.is_dsh({'worker_cli': 'dsh'}, 'worker-1') and fleet.is_bridge({'worker_cli': 'dsh'}, 'worker-1')
    with pytest.raises(ValueError, match='unknown supervisor CLI'):
        fleet.agent_cli({'supervisor_cli': 'dsh'}, 'supervisor')
    assert runtime_v2.tui_slot({'transport_version': 2, 'worker_cli': 'dsh'}, 'worker-1') is False
    with pytest.raises(ValueError, match='only as a worker'):
        runtime_v2.validate_config(dict(DSH, supervisor_cli='dsh'))
    with pytest.raises(ValueError, match='only on transport v2'):
        runtime_v2.validate_config(dict(DSH, transport_version=1))
    assert 'dsh workers' in cli_pairs.pair_problem({'supervisor_cli': 'claude', 'worker_cli': 'dsh', 'mode': 'task'})


def test_pi_dsh_task_row_is_unverified_and_needs_the_smoke_lifecycle():
    rows = {k: v for k, v in cli_pairs.load_pairs().rows.items() if k[1] == 'dsh'}
    assert list(rows) == [('pi', 'dsh', 'task')]
    row = rows[('pi', 'dsh', 'task')]
    assert row['adapter_missing'] == []
    cfg = {'supervisor_cli': 'pi', 'worker_cli': 'dsh', 'mode': 'task'}
    if row['status'] == 'unverified':
        assert 'only with --lifecycle smoke' in cli_pairs.pair_problem(cfg)
    else:  # flipped by a human after a real 4/4 smoke
        assert row['status'] == 'verified' and 'scripts/smoke_pair.sh pi dsh task' in row['verified_by']
    assert cli_pairs.pair_problem(dict(cfg, lifecycle='smoke')) is None


def test_model_card_and_model_keys():
    card = AgentRegistry().model('flashnext-dsh')
    assert (card.cli, card.model, card.max_concurrent) == ('dsh', MODEL, 2)
    assert model_usage.default_model({'worker_cli': 'dsh', 'dsh_model': MODEL}, AgentRegistry()) == 'flashnext-dsh'
    assert brake.MODEL_KEY['dsh'] == fleet.MODEL_KEYS['dsh'] == 'dsh_model'
    assert advisor.runtime_model({'dsh_model': MODEL}, 'dsh') == MODEL


def test_bridge_session_is_validated_by_the_dsh_adapter(tmp_path):
    cfg = cfg_for(tmp_path)
    sid = '69dae3ee-04de-4e48-b807-8e969d2757e3'
    good = dsh_adapter.session_dir(cfg, 'worker-1', 0) / (sid + '.jsonl')
    ev = runtime_v2.bridge_event(cfg, 'worker-1', 0, {'thread': sid, 'transcript': str(good), 'ctx_used': 5})
    assert ev['kind'] == dsh_adapter.KIND and ev['agent'] == 'worker-1'
    for thread, path in ((sid, dsh_adapter.session_dir(cfg, 'worker-1', 1) / (sid + '.jsonl')),  # other generation
                         ('ses_abc', good), (sid, good.with_name('x.jsonl'))):
        with pytest.raises(ValueError):
            runtime_v2.bridge_event(cfg, 'worker-1', 0, {'thread': thread, 'transcript': str(path)})
    with pytest.raises(ValueError, match='usage'):
        runtime_v2.bridge_event(cfg, 'worker-1', 0, {'thread': sid, 'transcript': str(good), 'ctx_used': -1})


# ---------------------------------------------------------------- adapter
def patches(cfg, gen=0):
    return [{(r.get('id') or 'insert'): r.get('config', r.get('insert')) for r in yaml.safe_load(p.read_text())}
            for p in dsh_adapter.patch_paths(cfg, 'worker-1', gen)]


def test_model_patch_pins_the_pi_model_everywhere_and_holds_no_key(tmp_path):
    cfg = cfg_for(tmp_path)
    dsh_adapter.prepare(cfg, 'worker-1', 0, real_home=tmp_path / 'nohome')
    model_rows, _ = patches(cfg)
    assert all(KEY not in p.read_text() for p in dsh_adapter.patch_paths(cfg, 'worker-1', 0))
    route = model_rows['llm-pi-ai']['providers']['flashnext']
    assert route['baseURL'] == 'http://127.0.0.1:9/v1' and route['api'] == 'openai-completions'
    assert route['apiKeyEnv'] == dsh_adapter.KEY_ENV
    assert route['models'] == [{'id': MODEL.split('/', 1)[1], 'name': 'Flash Next', 'contextWindow': 400000,
                                'maxTokens': 8192}]  # pi-only keys (cost, reasoning) are not copied
    chosen = {'provider': 'flashnext', 'model': MODEL.split('/', 1)[1]}
    assert model_rows['agent-default-model'] == chosen
    assert model_rows['dsh-tui'] == dict(chosen, backend='dsh', upstreamRetry=False)  # complete pin, no retry seed


def test_fleet_patch_moves_session_and_storage_roots_into_the_generation_and_loads_the_plugin(tmp_path):
    cfg = cfg_for(tmp_path)
    dsh_adapter.prepare(cfg, 'worker-1', 3, real_home=tmp_path / 'nohome')
    _, rows = patches(cfg, 3)
    state = dsh_adapter.pane_home(cfg, 'worker-1', 3) / 'dsh'
    assert rows['session-persistence-jsonl'] == {'root': str(state / 'sessions')}
    assert rows['storage-json'] == rows['dsh-tui-storage-json'] == {'root': str(state / 'storages')}
    assert rows['insert'] == [{'id': dsh_adapter.PLUGIN_ID, 'name': str(dsh_adapter.PLUGIN)}]
    assert dsh_adapter.PLUGIN.is_file() and str(tmp_path / 'dsh-home') not in json.dumps(rows)


def test_pane_home_is_seeded_and_links_only_gitconfig_and_npmrc(tmp_path):
    real = tmp_path / 'real'
    (real / '.ssh').mkdir(parents=True)
    (real / '.gitconfig').write_text('[user]\n\temail = a@b\n')
    cfg = cfg_for(tmp_path)
    home = dsh_adapter.prepare(cfg, 'worker-1', 0, real_home=real)
    assert home == tmp_path / 'run' / 'dsh-tui-home' / 'worker-1' / 'g0'
    assert json.loads((home / '.dsh-tui' / 'onboarding.json').read_text()) == {'completed': True, 'version': 1}
    assert json.loads((home / '.dsh-tui' / 'home.json').read_text()) == {'seen': True}
    assert (home / '.gitconfig').is_symlink() and (home / '.gitconfig').resolve() == (real / '.gitconfig').resolve()
    assert not (home / '.npmrc').exists() and not (home / '.ssh').exists()  # absent upstream / never linked
    assert (home / 'agents').is_dir() and dsh_adapter.session_dir(cfg, 'worker-1', 0).is_dir()
    with pytest.raises(FileExistsError):  # a generation's home is never reused
        dsh_adapter.prepare(cfg, 'worker-1', 0, real_home=real)


def test_keyless_provider_gets_no_api_key_env(tmp_path):
    cfg = cfg_for(tmp_path)
    models_dir(tmp_path, key='')
    route = dsh_adapter.model_patch(cfg)[0]['config']['providers']['flashnext']
    assert 'apiKeyEnv' not in route and dsh_adapter.KEY_ENV not in dsh_adapter.environment(
        tmp_path / 'c.json', cfg, 'worker-1', 0, label='worker-1', startup_prompt='go')


@pytest.mark.parametrize('value, why', [(None, 'required'), ('flashnext', 'required'), ('/x', 'provider/model'),
                                        ('nope/x', 'not in'), ('flashnext/other', 'not a model')])
def test_model_must_be_a_pi_models_json_entry(tmp_path, value, why):
    with pytest.raises(ValueError, match=why):
        dsh_adapter.model_patch(cfg_for(tmp_path, dsh_model=value) if value else dict(cfg_for(tmp_path), dsh_model=None))


def test_environment_and_argv(tmp_path):
    cfg = cfg_for(tmp_path)
    env = dsh_adapter.environment(tmp_path / 'c.json', cfg, 'worker-1', 2, label='worker-1.2', startup_prompt='go')
    home = str(dsh_adapter.pane_home(cfg, 'worker-1', 2))
    assert env[dsh_adapter.KEY_ENV] == KEY and sum(KEY in v for v in env.values()) == 1
    assert {k: env[k] for k in ('HOME', 'DSH_HOME', 'DSH_AGENTS_HOME', 'DSH_TELEMETRY_MODE', 'DSH_TELEMETRY_DISABLED',
                                'DSH_TUI_NO_LAUNCHPAD', 'DSH_PERMISSION_MODE')} == {
        'HOME': home, 'DSH_HOME': str(tmp_path / 'dsh-home'), 'DSH_AGENTS_HOME': home + '/agents',
        'DSH_TELEMETRY_MODE': 'DISABLED', 'DSH_TELEMETRY_DISABLED': '1', 'DSH_TUI_NO_LAUNCHPAD': '1',
        'DSH_PERMISSION_MODE': 'workspace-write'}
    bridge = json.loads(env['CMUX_FLEET_DSH_BRIDGE'])
    assert bridge[0] == '/py' and bridge[1].endswith('agent_bridge.py') and bridge[2:] == [str(tmp_path / 'c.json'), 'worker-1', '2']
    assert (env['CMUX_FLEET_DSH_SLOT'], env['CMUX_FLEET_DSH_GEN'], env['CMUX_FLEET_DSH_MODEL'], env['CMUX_FLEET_DSH_WINDOW'],
            env['CMUX_FLEET_DSH_STARTUP'], env['CMUX_FLEET_DSH_LABEL']) == ('worker-1', '2', MODEL, '400000', 'go', 'worker-1.2')
    assert env['CMUX_FLEET_DSH_SESSION_DIR'] == str(dsh_adapter.session_dir(cfg, 'worker-1', 2))
    assert env['CMUX_FLEET_DSH_USER_HOME'] == str(Path.home())
    model_path, fleet_path = dsh_adapter.patch_paths(cfg, 'worker-1', 2)
    # launcher flags only: `--backend dsh` on argv becomes app args and a startup prompt (measured); the row pins it
    assert dsh_adapter.argv(cfg, 'worker-1', 2) == ['/opt/node', '/opt/dsh/bin.js', '--profile', 'dsh-tui',
                                                    '--patch', str(model_path), '--patch', str(fleet_path)]
    with pytest.raises(ValueError, match='startup prompt'):
        dsh_adapter.environment(tmp_path / 'c.json', cfg, 'worker-1', 2, label='w', startup_prompt=' ')


def versions(monkeypatch, node='v22.22.0', dsh='0.2.1-rc.5', listed=True):
    monkeypatch.setattr(dsh_adapter.subprocess, 'run', lambda argv, **kw: SimpleNamespace(
        stdout=(dsh if argv[-1] == '--version' and len(argv) == 3 else node) + '\n'))
    monkeypatch.setattr(dsh_adapter, 'endpoint_problem', lambda cfg: None if listed else 'endpoint does not list it')


@pytest.mark.parametrize('node, dsh, why', [('v22.22.0', '0.2.1-rc.5', None), ('v22.18.9', '0.2.1-rc.5', 'node >= 22.19.0'),
                                            ('v22.22.0', '0.2.1-rc.6', r'dsh == 0\.2\.1-rc\.5'),
                                            ('v22.22.0', '0.2.1', r'dsh == 0\.2\.1-rc\.5')])
def test_start_problem_locks_the_exact_dsh_version(tmp_path, monkeypatch, node, dsh, why):
    versions(monkeypatch, node, dsh)
    problem = dsh_adapter.start_problem(cfg_for(tmp_path))
    assert problem is None if why is None else __import__('re').search(why, problem)


@pytest.mark.parametrize('version, why', [(None, 'not installed'), ('0.17.2', 'found: 0.17.2')])
def test_start_problem_needs_dsh_tui_installed_in_the_shared_home_and_says_how(tmp_path, monkeypatch, version, why):
    versions(monkeypatch)
    cfg = cfg_for(tmp_path)
    if version:
        tui_home(tmp_path, version)
    else:
        cfg['dsh_home'] = str(tmp_path / 'empty-home')
    problem = dsh_adapter.start_problem(cfg)
    assert why in problem and 'dsh-tui == 0.18.0' in problem
    assert 'dsh plugin --profile dsh-tui add @deepseek-harness-tui/dsh-tui@0.18.0' in problem


def test_start_problem_reports_an_endpoint_that_does_not_list_the_model(tmp_path, monkeypatch):
    versions(monkeypatch, listed=False)
    assert dsh_adapter.start_problem(cfg_for(tmp_path)) == 'endpoint does not list it'


def test_endpoint_problem_reads_the_model_list_with_the_key(tmp_path, monkeypatch):
    seen = {}

    class Response:
        def __init__(self, ids):
            self.body = json.dumps({'data': [{'id': i} for i in ids]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def urlopen(request, timeout):
        seen.update(url=request.full_url, auth=request.get_header('Authorization'))
        return Response(seen.get('ids', []))
    monkeypatch.setattr(dsh_adapter.urllib.request, 'urlopen', urlopen)
    cfg = cfg_for(tmp_path)
    seen['ids'] = ['other']
    problem = dsh_adapter.endpoint_problem(cfg)
    assert 'does not list' in problem and KEY not in problem
    assert seen == {'url': 'http://127.0.0.1:9/v1/models', 'auth': 'Bearer ' + KEY, 'ids': ['other']}
    seen['ids'] = [MODEL.split('/', 1)[1]]
    assert dsh_adapter.endpoint_problem(cfg) is None


def test_bridge_passes_the_plugins_candidates_through(tmp_path, monkeypatch):
    sent = []
    bridge = agent_bridge.AgentBridge.__new__(agent_bridge.AgentBridge)
    bridge.cli, bridge.slot, bridge.cfg, bridge.faulted = 'dsh', 'worker-1', {'mode': 'task'}, False
    bridge.lock = __import__('threading').RLock()
    bridge.idle_event = __import__('threading').Event()
    bridge.active = {'message_id': 'm1', 'body': {'purpose': 'task'}}
    bridge.metadata = lambda data: None
    bridge.event = lambda kind, fields=None, **kw: sent.append((kind, fields)) or {'status': 'held'}
    bridge.fault = lambda reason: None
    value = {'done': ['wrote ok.txt'], 'evidence': ['ok.txt 2 bytes'], 'not_done': 'none', 'blocked': 'none'}
    bridge.command({'kind': 'settled', 'text': 'Done: ignored', 'failed': False, 'aborted': False, 'unfinished': False,
                    'candidates': [{'tool': 'fleet_result', 'value': value}]})
    kind, fields = sent[-1]
    assert kind == 'settled' and fields['candidates'] == [{'tool': 'fleet_result', 'value': value}]


def test_bridge_takes_a_hold_from_the_dsh_plugin():
    sent, marked = [], []
    bridge = agent_bridge.AgentBridge.__new__(agent_bridge.AgentBridge)
    bridge.cli, bridge.slot, bridge.cfg, bridge.faulted = 'dsh', 'worker-1', {'mode': 'task'}, False
    bridge.lock = __import__('threading').RLock()
    bridge.active = {'message_id': 'm1', 'body': {'purpose': 'task'}}
    bridge.metadata = lambda data: None
    bridge.bus = SimpleNamespace(mark_uncertain=lambda slot, env, reason: marked.append(reason))
    bridge.event = lambda kind, fields=None, **kw: sent.append((kind, fields)) or {'status': 'recorded'}
    bridge.command({'kind': 'hold', 'reason': 'turn 2 mixed fleet m1 with other input'})
    assert marked == ['turn 2 mixed fleet m1 with other input'] and bridge.faulted
    assert sent[-1] == ('bridge_fault', {'reason': 'dsh hold: turn 2 mixed fleet m1 with other input'})
    assert not runtime_v2.link_fault(sent[-1][1]['reason'])  # an echo of the hold, not a lost link


# ---------------------------------------------------------------- fleet side
def test_actor_execs_dsh_tui_with_the_key_and_prompt_only_in_its_environment(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    calls = {}
    monkeypatch.setattr(fleet, 'load', lambda path: dict(cfg))
    monkeypatch.setattr(fleet.runtime_v2, 'validate_config', lambda c: 2)
    monkeypatch.setattr(fleet, 'connect', lambda c: SimpleNamespace(
        hgetall=lambda k: {'generation': '0', 'launch_ready': '1', 'label': 'worker-1', 'state': 'starting'}))
    monkeypatch.setattr(fleet, 'agent_cwd', lambda c, n: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(fleet, 'actor_argv', lambda *a, **k: pytest.fail('dsh must never take the Codex argv'))
    monkeypatch.setattr(fleet.os, 'execvp', lambda exe, argv: calls.update(exe=exe, argv=argv))
    env = {}
    monkeypatch.setattr(fleet.os, 'environ', env)
    fleet.actor(tmp_path / 'config.json', 'worker-1', 'start now', generation=0)
    assert calls['argv'] == dsh_adapter.argv(cfg, 'worker-1', 0)
    assert env[dsh_adapter.KEY_ENV] == KEY and env['CMUX_FLEET_DSH_STARTUP'] == 'start now'
    assert env['HOME'] == str(dsh_adapter.pane_home(cfg, 'worker-1', 0))
    assert all(p.is_file() for p in dsh_adapter.patch_paths(cfg, 'worker-1', 0))
    record = (tmp_path / 'run' / 'prompts' / 'worker-1-g0.argv.json').read_text()
    assert KEY not in record and 'start now' not in record and json.loads(record)['argv'] == calls['argv']


def test_dsh_worker_gets_the_fleet_result_role_like_pi(cfx):
    cfx.cfg.update(worker_cli='dsh', dsh_model=MODEL, transport_version=2)
    cfx.v2 = True
    scope = cfx.role_scope('worker-1', cfx.runtime_cfg('worker-1'), {})
    assert 'Final tool: fleet_result' in scope and 'Done: <one item per line' not in scope
    assert dsh_adapter.SCOPE in scope and 'without an OS sandbox' not in scope


def test_usage_reads_the_bridge_reported_tokens_and_window(cfx):
    cfx.cfg.update(worker_cli='dsh', dsh_model=MODEL)
    cfx.setf('worker-1', ctx_used=6658, ctx_window=400000)
    assert cfx.usage('worker-1') == (6658, 400000)
    assert cfx.agent('worker-1')['handoff_source'].startswith('window: measured')


def dsh_start(tmp_path, monkeypatch, version='0.2.1-rc.5', listed=True):
    args, ran = mock_start(tmp_path, monkeypatch)
    models_dir(tmp_path)
    tui_home(tmp_path)

    def run(argv, **kw):
        ran.append(argv)
        out = (version if argv[-1] == '--version' and len(argv) == 3 else 'v22.22.0' if argv[0].endswith('node')
               else '1.1.0')
        return SimpleNamespace(stdout=out + '\n', returncode=0)
    monkeypatch.setattr(fleet.subprocess, 'run', run)
    monkeypatch.setattr(dsh_adapter, 'endpoint_problem', lambda cfg: None if listed else 'the endpoint does not list it')
    args[args.index('--worker-cli') + 1] = 'dsh'
    return args + ['--lifecycle', 'smoke', '--dsh-home', str(tmp_path / 'dsh-home')], ran


def test_start_dsh_workers_records_binaries_model_home_and_max_busy_2(tmp_path, monkeypatch):
    args, ran = dsh_start(tmp_path, monkeypatch)
    fleet.main(args + ['--dsh-model', MODEL])
    cfg = fleet.load(tmp_path / 'latest.json')
    # resolve_binaries records real paths (fnm's per-shell symlink dirs): on a merged-/usr Linux /bin/dsh is /usr/bin/dsh.
    dsh, node = os.path.realpath('/bin/dsh'), os.path.realpath('/bin/node')
    assert (cfg['worker_cli'], cfg['dsh_model'], cfg['max_busy'], cfg['dsh'], cfg['dsh_node'], cfg['dsh_home']) == \
        ('dsh', MODEL, 2, dsh, node, str(tmp_path / 'dsh-home'))
    assert [node, dsh, '--version'] in ran
    assert KEY not in (tmp_path / 'latest.json').read_text()


def test_without_dsh_model_start_takes_and_records_the_fleet_yaml_default(tmp_path, monkeypatch, capsys):
    default = __import__('cmux_fleet.fleet_config', fromlist=['x']).dsh_default_model()
    assert default == 'workbuddy-ds/workbuddy-ai/deepseek-v4.1-flash-sg'
    args, _ = dsh_start(tmp_path, monkeypatch)
    root = models_dir(tmp_path)
    data = json.loads((root / 'models.json').read_text())
    data['providers']['workbuddy-ds'] = {'baseUrl': 'http://127.0.0.1:3425/v1', 'api': 'openai-completions', 'apiKey': KEY,
                                         'models': [{'id': 'workbuddy-ai/deepseek-v4.1-flash-sg', 'contextWindow': 400000,
                                                     'maxTokens': 32768}]}
    (root / 'models.json').write_text(json.dumps(data))
    fleet.main(args)
    assert fleet.load(tmp_path / 'latest.json')['dsh_model'] == default
    assert 'fleet.yaml#dsh.default_model = ' + default in capsys.readouterr().err


def test_workbuddy_dsh_model_card():
    card = AgentRegistry().model('workbuddy-ds-dsh')
    assert (card.cli, card.model, card.max_concurrent, card.context_window) == \
        ('dsh', 'workbuddy-ds/workbuddy-ai/deepseek-v4.1-flash-sg', 2, 400000)


@pytest.mark.parametrize('extra, version, listed, why', [
    (['--dsh-model', 'flashnext/missing'], '0.2.1-rc.5', True, 'not a model of provider'),
    (['--dsh-model', MODEL], '0.2.1-rc.4', True, r'dsh == 0\.2\.1-rc\.5 is required'),
    (['--dsh-model', MODEL], '0.2.1-rc.5', False, 'the endpoint does not list it'),
])
def test_start_refuses_dsh_with_a_bad_model_version_or_endpoint(tmp_path, monkeypatch, extra, version, listed, why):
    args, ran = dsh_start(tmp_path, monkeypatch, version, listed)
    with pytest.raises(SystemExit, match=why):
        fleet.main(args + extra)
    assert not any('--unixsocket' in a for a in ran)  # refused before the private Redis starts


def test_argparse_offers_dsh_for_workers_only(tmp_path, monkeypatch, capsys):
    args, _ = dsh_start(tmp_path, monkeypatch)
    args[args.index('--supervisor-cli') + 1] = 'dsh'
    with pytest.raises(SystemExit):
        fleet.main(args + ['--dsh-model', MODEL])
    assert "invalid choice: 'dsh'" in capsys.readouterr().err
