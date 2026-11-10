"""First-run discovery (docs/design.md §52): probes, ranking, default pair, privacy, and how `start` uses it."""
import http.server
import json
import os
import socket
import stat
import threading
from types import SimpleNamespace

import pytest

from cmux_fleet import cli_pairs, discover, fleet, runtime_v2

CFG = {'supervisor': ['astra', 'fable', 'opus', 'sol'], 'worker': ['opus', 'sol', 'deepseek', 'glm', 'kimi', 'qwen3.8'],
       'mode': 'task', 'supervisor_pi_thinking': 'high', 'cli_order': ['claude', 'pi', 'opencode'],
       'only_via': {'claude': 'claude', 'fable': 'claude', 'opus': 'claude', 'sonnet': 'claude', 'haiku': 'claude',
                    'gpt': 'pi'},
       'report_only': ['codex', 'ollama']}
CLAUDE_MODELS = ['fable', 'haiku', 'opus', 'sonnet']
SECRET_EMAIL, SECRET_ORG, SECRET_KEY = 'someone@example.invalid', 'Secret Org', 'sk-test-DO-NOT-LEAK'


def fake_bin(d, name, script):
    path = d / name
    path.write_text('#!/bin/sh\n' + script)
    path.chmod(0o755)
    return path


def fake_machine(tmp_path, claude_logged_in=True, pi_version='1.5.0', with_pi=True):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    fake_bin(bindir, 'claude', 'case "$1" in --version) echo "2.1.316 (Claude Code)";; auth) echo \'{"loggedIn": %s, '
             '"authMethod": "claude.ai", "email": "%s", "orgId": "org-123", "orgName": "%s"}\';; esac\n'
             % ('true' if claude_logged_in else 'false', SECRET_EMAIL, SECRET_ORG))
    if with_pi:
        fake_bin(bindir, 'pi', 'case "$1" in --version) echo "%s";; --list-models) cat <<EOF\n'
                 'provider      model                     context\n'
                 'openai-codex  gpt-6-astra               400K\n'
                 'openai-codex  gpt-6-sol                 272K\n'
                 'openai-codex  gpt-6.1-sol               500K\n'
                 'gw-up         Qwen3.8-Flash-Next        400K\n'
                 'gw-down       deepseek-v9               100K\n'
                 'EOF\n;; esac\n' % pi_version)
    fake_bin(bindir, 'opencode', 'case "$1" in --version) echo "2.2.5";; models) printf "opencode-go/glm-5.3\\n'
             'opencode-go/kimi-k3\\nopencode-go/claude-haiku-5-5\\nopencode-go/gpt-6-luna\\nopencode-go/beam-free\\n";; esac\n')
    fake_bin(bindir, 'codex', 'case "$1" in --version) echo "codex-cli 0.164.0";; login) echo "Not logged in";; esac\n')
    return bindir


@pytest.fixture
def gateway():
    class Quiet(http.server.BaseHTTPRequestHandler):
        seen = []

        def do_GET(self):
            Quiet.seen.append(dict(self.headers))
            self.send_response(401)  # no key sent: answering at all means online
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(('127.0.0.1', 0), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], Quiet.seen
    srv.shutdown()


def closed_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def pi_dir(tmp_path, up_port):
    d = tmp_path / 'pi'
    d.mkdir()
    (d / 'models.json').write_text(json.dumps({'providers': {
        'openai-codex': {'modelOverrides': {}},
        'gw-up': {'baseUrl': 'http://127.0.0.1:%d/v1' % up_port, 'apiKey': SECRET_KEY, 'models': []},
        'gw-down': {'baseUrl': 'http://127.0.0.1:%d/v1' % closed_port(), 'apiKey': SECRET_KEY, 'models': []}}}))
    (d / 'auth.json').write_text('{"never": "read"}')
    return d


def probe(tmp_path, gateway, **kw):
    bindir = fake_machine(tmp_path, **kw)
    return discover.run(cfg=CFG, search=str(bindir), pi_dir=pi_dir(tmp_path, gateway[0]),
                        pairs=cli_pairs.load_pairs(), claude_models=CLAUDE_MODELS)


def test_default_pair_is_astra_supervisor_with_opus_workers(tmp_path, gateway):
    r = probe(tmp_path, gateway)
    d = r['default']
    assert (d['supervisor_cli'], d['supervisor_pi_model'], d['supervisor_pi_thinking']) == ('pi', 'openai-codex/gpt-6-astra', 'high')
    assert (d['worker_cli'], d['worker_model']) == ('claude', 'opus')
    assert r['supervisors'][:3] == ['pi:openai-codex/gpt-6-astra', 'claude:fable', 'claude:opus']


def test_worker_ranking_follows_user_order_newest_version_first(tmp_path, gateway):
    r = probe(tmp_path, gateway)
    w = r['workers']
    assert w[:3] == ['claude:opus', 'pi:openai-codex/gpt-6.1-sol', 'pi:openai-codex/gpt-6-sol']
    assert w.index('opencode:opencode-go/glm-5.3') < w.index('opencode:opencode-go/kimi-k3') < w.index('pi:gw-up/Qwen3.8-Flash-Next')
    assert w[-1] == 'opencode:opencode-go/beam-free'  # unlisted models come last
    assert 'pi:openai-codex/gpt-6-astra' not in w and 'claude:fable' not in w  # supervisor-only


def test_only_via_keeps_claude_on_claude_and_gpt_on_pi(tmp_path, gateway):
    r = probe(tmp_path, gateway)
    everything = r['workers'] + r['supervisors']
    assert not any(c.startswith('opencode:') and ('claude' in c or 'gpt' in c) for c in everything)


def test_offline_gateway_models_are_skipped_and_said(tmp_path, gateway):
    r = probe(tmp_path, gateway)
    assert r['gateways'] == {'gw-up': {'online': True}, 'gw-down': {'online': False}}
    assert not any('deepseek-v9' in c for c in r['workers'])
    assert {'cli': 'pi', 'model': 'gw-down/deepseek-v9', 'why': 'gateway offline'} in r['skipped']
    assert all('Authorization' not in h and SECRET_KEY not in json.dumps(h) for h in gateway[1])


def test_nothing_private_is_kept_printed_or_cached(tmp_path, gateway, monkeypatch):
    monkeypatch.setenv(discover.CACHE_ENV, str(tmp_path / 'cache' / 'discovered.yaml'))
    r = probe(tmp_path, gateway)
    path = discover.save(r)
    text = path.read_text() + discover.render(r) + json.dumps(r)
    for secret in (SECRET_EMAIL, SECRET_ORG, 'org-123', SECRET_KEY, '127.0.0.1'):
        assert secret not in text
    assert r['clis']['claude']['auth_method'] == 'claude.ai'
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert discover.load() == r


def test_logged_out_too_old_and_missing_are_reported(tmp_path, gateway):
    r = probe(tmp_path, gateway, claude_logged_in=False, pi_version='1.0.0')
    assert r['clis']['claude']['logged_in'] is False and r['clis']['pi']['version_ok'] is False
    assert r['clis']['ollama'] == {'installed': False}
    assert r['clis']['codex']['logged_in'] is False
    fixes = '\n'.join(r['fixes'])
    assert 'claude auth login' in fixes and 'pi 1.0.0 is older than' in fixes and 'codex login' in fixes
    assert r['default'] is None and 'no default pair' in fixes  # no claude, no pi: no verified pair left


def test_pick_skips_unverified_pairs_and_says_why():
    table = cli_pairs.PairTable({'version': 1, 'pairs': [
        {'supervisor_cli': 'pi', 'worker_cli': 'opencode', 'mode': 'task', 'status': 'unverified', 'adapter_missing': []},
        {'supervisor_cli': 'pi', 'worker_cli': 'pi', 'mode': 'task', 'status': 'verified', 'adapter_missing': []}]})
    choice, notes = discover.pick([('pi', 'openai-codex/gpt-6-astra')],
                                  [('opencode', 'opencode-go/glm-5.3'), ('pi', 'gw/Qwen3.8')], table, 'task')
    assert choice == (('pi', 'openai-codex/gpt-6-astra'), ('pi', 'gw/Qwen3.8'))
    assert notes == ['opencode-go/glm-5.3 via opencode: pi supervisor + opencode workers (task) is unverified']


def test_matches_whole_tokens_only():
    assert discover.matches('qwen3.8', 'flashnext/Qwen3.8-Flash-Next')
    assert discover.matches('sol', 'openai-codex/gpt-6.1-sol')
    assert not discover.matches('sol', 'opencode-go/solar-pro')
    assert not discover.matches('opus', 'magnum-opusx')


def test_spaced_pattern_matches_words_in_order():
    assert discover.matches('deepseek flash', 'workbuddy-ds/workbuddy-ai/deepseek-v4.1-flash-sg')
    assert discover.matches('deepseek flash', 'opencode-go/deepseek-v4.1-flash')
    assert not discover.matches('deepseek flash', 'opencode-go/deepseek-v4-pro')
    assert not discover.matches('deepseek flash', 'opencode-go/qwen3.8-flash')
    assert not discover.matches('flash deepseek', 'opencode-go/deepseek-v4.1-flash')
    assert not discover.matches('', 'opencode-go/deepseek-v4.1-flash')


def test_repo_order_is_the_user_chain_of_2026_11_11():
    """fleet.yaml#discovery = the user's 11-11 order: supervisors astra > fable > opus > sol > deepseek flash,
    workers opus > sol > sonnet > terra > deepseek flash, everything else after them."""
    from cmux_fleet import fleet_config
    cfg = fleet_config.block('discovery', fleet_config.SHIPPED)  # the repo file, not the operator's private one
    pool = [('claude', 'fable'), ('claude', 'haiku'), ('claude', 'opus'), ('claude', 'sonnet'),
            ('pi', 'openai-codex/gpt-6-astra'), ('pi', 'openai-codex/gpt-6.1-sol'), ('pi', 'openai-codex/gpt-6.1-terra'),
            ('pi', 'openai-codex/gpt-6-luna'), ('pi', 'workbuddy-ds/workbuddy-ai/deepseek-v4.1-flash-sg'),
            ('opencode', 'opencode-go/deepseek-v4-pro'), ('opencode', 'opencode-go/glm-5.3')]
    order = cfg['cli_order']
    sups = discover.rank([c for c in pool if c[0] in cli_pairs.CLIS], cfg['supervisor'], order)
    assert [c[1] for c in sups] == ['openai-codex/gpt-6-astra', 'fable', 'opus', 'openai-codex/gpt-6.1-sol',
                                    'workbuddy-ds/workbuddy-ai/deepseek-v4.1-flash-sg']
    sup_only = [c for c in sups if not any(discover.matches(p, c[1]) for p in cfg['worker'])]
    assert [c[1] for c in sup_only] == ['openai-codex/gpt-6-astra', 'fable']
    workers = discover.rank([c for c in pool if c not in sup_only], cfg['worker'], order, rest=True)
    assert [c[1] for c in workers][:5] == ['opus', 'openai-codex/gpt-6.1-sol', 'sonnet', 'openai-codex/gpt-6.1-terra',
                                           'workbuddy-ds/workbuddy-ai/deepseek-v4.1-flash-sg']
    assert {c[1] for c in workers[5:]} == {'haiku', 'openai-codex/gpt-6-luna', 'opencode-go/deepseek-v4-pro',
                                           'opencode-go/glm-5.3'}


def test_ensure_probes_once_then_reads_the_cache(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(discover.OFF_ENV, 'on')
    monkeypatch.setenv(discover.CACHE_ENV, str(tmp_path / 'd.yaml'))
    calls = []
    monkeypatch.setattr(discover, 'run', lambda: calls.append(1) or {'probed_at': 'now', 'clis': {}, 'default': None})
    assert discover.ensure()['probed_at'] == 'now' and discover.ensure()['probed_at'] == 'now'
    assert calls == [1] and 'fleet setup' in capsys.readouterr().err


def test_ensure_never_stops_a_start(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(discover.OFF_ENV, 'on')
    monkeypatch.setenv(discover.CACHE_ENV, str(tmp_path / 'd.yaml'))
    monkeypatch.setattr(discover, 'run', lambda: 1 / 0)
    assert discover.ensure() is None and 'discovery failed' in capsys.readouterr().err


def test_ensure_is_off_in_tests_by_default(monkeypatch):
    monkeypatch.setattr(discover, 'run', lambda: pytest.fail('must not probe'))
    assert discover.ensure() is None


FOUND = {'supervisor_cli': 'pi', 'worker_cli': 'claude', 'mode': 'task', 'supervisor_pi_model': 'openai-codex/gpt-6-astra',
         'supervisor_pi_thinking': 'high', 'worker_model': 'opus', 'supervisor': 'pi:x', 'worker': 'claude:opus'}


def started(monkeypatch, argv, found):
    """vars(args) as start hands them to the config check, right after the CLIs and models are resolved."""
    monkeypatch.setattr(discover, 'ensure', lambda: {'default': found} if found else None)
    seen = {}

    def stop(cfg):
        seen.update(cfg)
        raise SystemExit('stop')
    monkeypatch.setattr(runtime_v2, 'validate_config', stop)
    with pytest.raises(SystemExit, match='stop'):
        fleet.main(['start'] + argv)
    return seen


def test_start_without_cli_flags_takes_the_discovered_pair(monkeypatch):
    cfg = started(monkeypatch, [], FOUND)
    assert (cfg['supervisor_cli'], cfg['worker_cli'], cfg['worker_model']) == ('pi', 'claude', 'opus')
    assert (cfg['supervisor_pi_model'], cfg['supervisor_pi_thinking']) == ('openai-codex/gpt-6-astra', 'high')


def test_typed_flags_always_win(monkeypatch):
    cfg = started(monkeypatch, ['--worker-model', 'haiku'], FOUND)
    assert cfg['worker_model'] == 'haiku'
    cfg = started(monkeypatch, ['--worker-cli', 'claude'], dict(FOUND, supervisor_cli='claude', supervisor_model='fable'))
    assert cfg['supervisor_cli'] == 'pi' and cfg['worker_model'] == fleet.DEFAULTS['worker_model']  # table default


def test_without_a_discovered_pair_start_is_unchanged(monkeypatch):
    cfg = started(monkeypatch, [], None)
    default = cli_pairs.load_pairs().default
    assert (cfg['supervisor_cli'], cfg['worker_cli']) == (default['supervisor_cli'], default['worker_cli'])
    assert cfg['worker_model'] == fleet.DEFAULTS['worker_model'] == 'sonnet'
    assert cfg['supervisor_model'] == fleet.DEFAULTS['supervisor_model']


def test_apply_models_fills_only_unset_flags():
    args = SimpleNamespace(supervisor_cli='claude', worker_cli='pi', supervisor_model=None, pi_model='mine/model')
    discover.apply_models(args, {'supervisor_model': 'fable', 'pi_model': 'theirs/model'})
    assert (args.supervisor_model, args.pi_model) == ('fable', 'mine/model')


def test_import_and_probe_need_no_repo_root_files(tmp_path, gateway):
    """An install without scripts/ or deps.yaml (site-packages) must still import, and probe without minimums."""
    import shutil
    import subprocess
    import sys
    pkg = tmp_path / 'site' / 'cmux_fleet'
    shutil.copytree(discover.ROOT / 'cmux_fleet', pkg, ignore=shutil.ignore_patterns('__pycache__'))
    done = subprocess.run([sys.executable, '-c', 'import cmux_fleet.discover as d; print(d.ROOT)'],
                          cwd=tmp_path / 'site', capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    bindir = fake_machine(tmp_path)
    old = discover.ROOT
    try:
        discover.ROOT = tmp_path / 'site'
        r = discover.run(cfg=CFG, search=str(bindir), pi_dir=pi_dir(tmp_path, gateway[0]),
                         pairs=cli_pairs.load_pairs(), claude_models=CLAUDE_MODELS)
    finally:
        discover.ROOT = old
    assert r['clis']['pi']['min'] is None and r['clis']['pi']['version_ok'] is True
    assert r['default']['worker'] == 'claude:opus'
