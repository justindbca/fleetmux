"""Transport v2 CLI pair table: the cli_pairs block of config/fleet.yaml decides who may run."""
from pathlib import Path

import pytest
import yaml

from fleetmux import cli_pairs, runtime_v2, transport_inventory
from fleetmux import fleet_config

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path, pairs, default=None):
    path = tmp_path / 'cli_pairs.yaml'
    path.write_text(yaml.safe_dump({'version': 1, 'default': default or {}, 'pairs': pairs}))
    return path


def row(sup, wrk, mode, status='verified', missing=(), **extra):
    return dict(supervisor_cli=sup, worker_cli=wrk, mode=mode, status=status,
                adapter_missing=list(missing), verified_by=None, **extra)


def test_shipped_table_rows_and_default():
    table = cli_pairs.load_pairs()
    # 2026-10-13 user decision: pi supervisor on Astra/high + official Claude workers (tests/test_codex_via_pi.py)
    assert table.default == {'supervisor_cli': 'pi', 'worker_cli': 'claude', 'mode': 'task',
                             'supervisor_pi_model': 'openai-codex/gpt-6-astra', 'supervisor_pi_thinking': 'high'}
    assert table.lookup('codex', 'claude', 'task')['status'] == 'verified'
    # Real smokes (scripts/smoke_pair.sh, 2026-10-12 @8a31c3c7) flipped these rows; each names its run.
    smoked = [('codex', 'pi', 'task'), ('codex', 'pi', 'service'), ('codex', 'claude', 'service'),
              ('claude', 'pi', 'task'), ('claude', 'pi', 'service'), ('claude', 'claude', 'task'),
              ('claude', 'claude', 'service')]
    for key in smoked:
        assert table.lookup(*key)['status'] == 'verified', key
        assert 'smoke_pair.sh' in table.lookup(*key)['verified_by'] and 'PASS' in table.lookup(*key)['verified_by']
    for mode in ('task', 'service'):
        assert table.lookup('pi', 'pi', mode)['status'] == 'verified'
    # 2026-11-03 user decision: Opus 5.5 replaces the walled Astra supervisor of a Claude-worker service fleet
    assert table.lookup('claude', 'codex', 'task') is None
    assert not [key for key, r in table.rows.items() if r['adapter_missing']]
    assert table.lookup('codex', 'codex', 'task')['status'] == 'unverified'   # no real smoke yet
    assert table.lookup('codex', 'codex', 'task')['verified_by'] is None
    assert table.lookup('pi', 'codex', 'task') is None


def test_verified_runs_and_default_pair_runs():
    assert cli_pairs.pair_problem({'supervisor_cli': 'codex', 'worker_cli': 'claude', 'mode': 'task'}) is None
    assert cli_pairs.pair_problem({'supervisor_cli': 'pi', 'worker_cli': 'pi', 'mode': 'service'}) is None


def test_unverified_runs_only_under_smoke_lifecycle():
    cfg = {'supervisor_cli': 'codex', 'worker_cli': 'codex', 'mode': 'task'}   # the shipped unverified row
    assert '--lifecycle smoke' in cli_pairs.pair_problem(cfg)
    assert cli_pairs.pair_problem(dict(cfg, lifecycle='dev_task'))
    assert cli_pairs.pair_problem(dict(cfg, lifecycle='smoke')) is None
    assert cli_pairs.pair_problem({'supervisor_cli': 'codex', 'worker_cli': 'codex', 'lifecycle': 'smoke'}) is None


def test_smoked_pairs_run_without_smoke():
    for sup, wrk, mode in [('codex', 'pi', 'task'), ('codex', 'pi', 'service'), ('codex', 'claude', 'service'),
                           ('claude', 'pi', 'task'), ('claude', 'pi', 'service'), ('claude', 'claude', 'task')]:
        assert cli_pairs.pair_problem({'supervisor_cli': sup, 'worker_cli': wrk, 'mode': mode,
                                       'lifecycle': 'dev_task' if mode == 'task' else 'service'}) is None


def test_adapter_missing_refused_even_for_smoke(tmp_path):
    # No shipped row lacks an adapter after A2 (tui_cards) and A3 (Claude supervisor): use a stub table.
    table = cli_pairs.load_pairs(write(tmp_path, [
        row('claude', 'pi', 'task', 'unverified', ['claude_supervisor']),
        row('codex', 'claude', 'service', 'unverified', ['codex_service_renderer'])]))
    reason = cli_pairs.pair_problem({'supervisor_cli': 'claude', 'worker_cli': 'pi', 'lifecycle': 'smoke'}, table)
    assert reason and 'adapter missing: claude_supervisor' in reason
    reason = cli_pairs.pair_problem({'supervisor_cli': 'codex', 'worker_cli': 'claude', 'mode': 'service',
                                     'lifecycle': 'smoke'}, table)
    assert reason and 'adapter missing: codex_service_renderer' in reason


def test_forbidden_and_unlisted_refused_even_for_smoke(tmp_path):
    table = cli_pairs.load_pairs(write(tmp_path, [row('pi', 'pi', 'task'),
                                                  row('codex', 'pi', 'task', 'forbidden', reason='night only')]))
    assert cli_pairs.pair_problem({'supervisor_cli': 'codex', 'worker_cli': 'pi', 'lifecycle': 'smoke'},
                                  table) == 'night only'
    assert 'mixed Pi/Codex' in cli_pairs.pair_problem(
        {'supervisor_cli': 'pi', 'worker_cli': 'codex', 'lifecycle': 'smoke'}, table)
    assert cli_pairs.pair_problem({'supervisor_cli': 'pi', 'worker_cli': 'pi', 'mode': 'service'}, table)
    assert cli_pairs.pair_problem({'supervisor_cli': 'pi', 'worker_cli': 'pi'}, table) is None


def test_adopted_claude_subfleet_exception_is_task_or_service(tmp_path):
    # Gate logic on a stub table where claude+claude is still unverified (shipped row is verified now).
    table = cli_pairs.load_pairs(write(tmp_path, [row('claude', 'claude', 'task', 'unverified')]))
    base = {'supervisor_cli': 'claude', 'worker_cli': 'claude', 'mode': 'task'}
    assert '--lifecycle smoke' in cli_pairs.pair_problem(base, table)
    assert cli_pairs.pair_problem(dict(base, lifecycle='smoke'), table) is None
    assert cli_pairs.pair_problem(dict(base, adopt={'pane': 'x'}), table) is None
    # design.md §29 (design change): an adopted service sub-fleet (named sub-fleet) passes too
    assert cli_pairs.pair_problem(dict(base, adopt={'pane': 'x'}, mode='service'), table) is None
    assert cli_pairs.pair_problem(dict(base, adopt={'pane': 'x'}, mode='service')) is None
    assert cli_pairs.pair_problem(dict(base, mode='service'), table)   # stub: not listed without adopt
    assert cli_pairs.pair_problem(dict(base, mode='service')) is None   # shipped: verified 2026-11-03


def test_table_is_reread_on_every_check(tmp_path, monkeypatch):
    path = write(tmp_path, [row('codex', 'pi', 'task', 'unverified')])
    monkeypatch.setattr(cli_pairs, 'DEFAULT_PATH', path)
    cfg = dict(transport_version=2, allow_development_v2=True, supervisor_cli='codex', worker_cli='pi')
    with pytest.raises(ValueError, match='lifecycle smoke'):
        runtime_v2.validate_config(cfg)
    path.write_text(yaml.safe_dump({'version': 1, 'pairs': [row('codex', 'pi', 'task')]}))
    assert runtime_v2.validate_config(cfg) == 2
    with pytest.raises(ValueError, match='allow-development-v2'):
        runtime_v2.validate_config(dict(cfg, allow_development_v2=False))


def test_smoke_does_not_relax_development_opt_in():
    cfg = dict(transport_version=2, supervisor_cli='codex', worker_cli='pi', lifecycle='smoke')
    with pytest.raises(ValueError, match='allow-development-v2'):
        runtime_v2.validate_config(cfg)


@pytest.mark.parametrize('data,match', [
    ({'pairs': []}, 'version'),
    ({'version': 1, 'pairs': [row('gemini', 'pi', 'task')]}, 'bad cli pair'),
    ({'version': 1, 'pairs': [row('pi', 'pi', 'task', 'maybe')]}, 'status'),
    ({'version': 1, 'pairs': [row('pi', 'pi', 'task'), row('pi', 'pi', 'task')]}, 'duplicate'),
    ({'version': 1, 'default': {'supervisor_cli': 'codex', 'worker_cli': 'claude', 'mode': 'task'},
      'pairs': [row('pi', 'pi', 'task')]}, 'default pair')])
def test_malformed_table_rejected(tmp_path, data, match):
    path = tmp_path / 'bad.yaml'
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match=match):
        cli_pairs.load_pairs(path)


def test_inventory_uses_same_rule_and_wording():
    assert transport_inventory.v2_cli_problem is runtime_v2.v2_cli_problem
    cfg = {'supervisor_cli': 'codex', 'worker_cli': 'pi', 'mode': 'task'}
    assert transport_inventory.v2_cli_problem(cfg) == cli_pairs.pair_problem(cfg)


def test_yaml_ships_as_package_data():
    tomllib = pytest.importorskip('tomllib')
    data = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    globs = data['tool']['setuptools']['package-data']['fleetmux']
    shipped = {p for g in globs for p in (ROOT / 'fleetmux').glob(g)}
    assert fleet_config.SHIPPED in shipped
