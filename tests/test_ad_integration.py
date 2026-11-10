"""A x D integration: pair-table default drives start, smoke runs task and service pairs,
and the Claude supervisor's UserPromptSubmit hook fails closed (design.md §5 queue)."""
import io
import json
import sys
import time
from contextlib import redirect_stdout

import pytest
import yaml

from cmux_fleet import cli_pairs, cmux_manager, fleet, lifecycle, runtime_v2
from cmux_fleet import fleet_config


class Reached(Exception):
    pass


profiles = []  # (supervisor_profile, profile) per start, filled by capture_start


def capture_start(monkeypatch):
    """Run the real start argument path up to provisioning; return the cfgs the pair check saw."""
    seen = []
    profiles.clear()
    real = runtime_v2.v2_cli_problem

    def check(cfg):
        seen.append({k: cfg.get(k) for k in ('supervisor_cli', 'worker_cli', 'mode', 'lifecycle')})
        profiles.append((cfg.get('supervisor_profile'), cfg.get('profile')))
        return real(cfg)
    monkeypatch.setattr(runtime_v2, 'v2_cli_problem', check)

    def reached():
        raise Reached
    monkeypatch.setattr(cmux_manager, 'cmux_available', reached)
    return seen


# ------------------------------------------------------------------ default combination
def test_start_without_cli_flags_uses_pair_table_default(monkeypatch):
    # 2026-10-13 default: pi supervisor (Astra, thinking high) + official Claude workers; the pair is
    # smoke-only until its real smoke flips the row (tests/test_codex_via_pi.py covers the models).
    table = cli_pairs.load_pairs()
    assert (table.default['supervisor_cli'], table.default['worker_cli']) == ('pi', 'claude')
    life = 'dev_task' if table.lookup('pi', 'claude', 'task')['status'] == 'verified' else 'smoke'
    seen = capture_start(monkeypatch)
    with pytest.raises(Reached):
        fleet.main(['start', '--lifecycle', life, '--allow-development-v2', '--allow-unsandboxed-pi'])
    assert seen == [{'supervisor_cli': 'pi', 'worker_cli': 'claude', 'mode': 'task', 'lifecycle': life}]


def test_start_default_follows_an_edited_table(tmp_path, monkeypatch):
    table = fleet_config.block('cli_pairs')
    table['default'] = {'supervisor_cli': 'pi', 'worker_cli': 'pi', 'mode': 'task'}
    path = tmp_path / 'cli_pairs.yaml'
    path.write_text(yaml.safe_dump(table))
    monkeypatch.setattr(cli_pairs, 'DEFAULT_PATH', path)
    seen = capture_start(monkeypatch)
    with pytest.raises(Reached):
        fleet.main(['start', '--lifecycle', 'dev_task', '--allow-development-v2', '--allow-unsandboxed-pi'])
    assert (seen[0]['supervisor_cli'], seen[0]['worker_cli']) == ('pi', 'pi')


def test_explicit_cli_flags_override_the_default(monkeypatch):
    seen = capture_start(monkeypatch)
    with pytest.raises(Reached):
        fleet.main(['start', '--lifecycle', 'smoke', '--supervisor-cli', 'codex', '--worker-cli', 'pi',
                    '--allow-development-v2'])
    assert (seen[0]['supervisor_cli'], seen[0]['worker_cli']) == ('codex', 'pi')


def test_one_explicit_cli_fills_only_the_other_from_default(monkeypatch):
    seen = capture_start(monkeypatch)
    with pytest.raises(Reached):   # claude+claude/task is verified by its real smoke
        fleet.main(['start', '--lifecycle', 'dev_task', '--supervisor-cli', 'claude', '--allow-development-v2'])
    assert (seen[0]['supervisor_cli'], seen[0]['worker_cli']) == ('claude', 'claude')


@pytest.mark.parametrize('extra,expected', [
    ([], (None, 'fleet')),                                            # table default names no Codex profile now
    (['--supervisor-profile', 'mine'], ('mine', 'fleet')),             # explicit supervisor profile wins
    (['--profile', 'shared'], (None, 'shared')),                       # explicit shared profile wins
])
def test_explicit_codex_supervisor_keeps_its_profile_flags(monkeypatch, extra, expected):
    # The official Codex TUI supervisor stays optional (brief item 5): only with --supervisor-cli codex.
    capture_start(monkeypatch)
    with pytest.raises(Reached):
        fleet.main(['start', '--lifecycle', 'dev_task', '--allow-development-v2', '--supervisor-cli', 'codex',
                    '--worker-cli', 'claude'] + extra)
    assert profiles == [expected]


def test_non_codex_supervisor_gets_no_codex_profile(monkeypatch):
    capture_start(monkeypatch)
    with pytest.raises(Reached):
        fleet.main(['start', '--lifecycle', 'dev_task', '--supervisor-cli', 'pi', '--worker-cli', 'pi',
                    '--allow-development-v2', '--allow-unsandboxed-pi'])
    assert profiles == [(None, 'fleet')]


def test_default_supervisor_profile_must_be_a_name(tmp_path):
    table = fleet_config.block('cli_pairs')
    table['default']['supervisor_profile'] = 5
    path = tmp_path / 'cli_pairs.yaml'
    path.write_text(yaml.safe_dump(table))
    with pytest.raises(ValueError, match='supervisor_profile'):
        cli_pairs.load_pairs(path)


# ------------------------------------------------------------------ smoke on task and service
def test_smoke_runs_on_task_or_service_base():
    assert lifecycle.resolve_start('smoke') == ('smoke', 'task')
    assert lifecycle.resolve_start('smoke', 'task') == ('smoke', 'task')
    assert lifecycle.resolve_start('smoke', 'service') == ('smoke', 'service')
    with pytest.raises(ValueError, match='runs on --mode task, not --mode service'):
        lifecycle.resolve_start('dev_task', 'service')
    with pytest.raises(ValueError, match='runs on --mode service, not --mode task'):
        lifecycle.resolve_start('service', 'task')


def test_bases_must_include_base(tmp_path):
    table = fleet_config.block('lifecycle_modes')
    table['modes']['smoke']['bases'] = ['service']
    path = tmp_path / 'fleet_modes.yaml'
    path.write_text(yaml.safe_dump(table))
    with pytest.raises(ValueError, match='bases must be a list'):
        lifecycle.load_modes(path)


@pytest.mark.parametrize('sup,wrk', [('codex', 'pi'), ('codex', 'claude'), ('claude', 'pi')])
def test_unverified_service_pairs_reach_provisioning_only_under_smoke(monkeypatch, tmp_path, sup, wrk):
    # The gate a service pair went through before its real smoke (shipped rows are verified now).
    table = fleet_config.block('cli_pairs')
    for item in table['pairs']:
        if (item['supervisor_cli'], item['worker_cli'], item['mode']) == (sup, wrk, 'service'):
            item.update(status='unverified', verified_by=None, reason=sup + '+' + wrk + ' service unverified')
    path = tmp_path / 'cli_pairs.yaml'
    path.write_text(yaml.safe_dump(table))
    monkeypatch.setattr(cli_pairs, 'DEFAULT_PATH', path)
    seen = capture_start(monkeypatch)
    base = ['start', '--supervisor-cli', sup, '--worker-cli', wrk, '--allow-development-v2', '--mode', 'service']
    with pytest.raises(Reached):
        fleet.main(base + ['--lifecycle', 'smoke'])
    assert seen[-1] == {'supervisor_cli': sup, 'worker_cli': wrk, 'mode': 'service', 'lifecycle': 'smoke'}
    with pytest.raises(SystemExit, match='only with --lifecycle smoke'):
        fleet.main(base + ['--lifecycle', 'service'])


@pytest.mark.parametrize('sup,wrk', [('codex', 'pi'), ('codex', 'claude'), ('claude', 'pi')])
def test_smoked_service_pairs_start_under_service_lifecycle(monkeypatch, sup, wrk):
    seen = capture_start(monkeypatch)
    with pytest.raises(Reached):
        fleet.main(['start', '--supervisor-cli', sup, '--worker-cli', wrk, '--allow-development-v2',
                    '--lifecycle', 'service'])
    assert seen[-1] == {'supervisor_cli': sup, 'worker_cli': wrk, 'mode': 'service', 'lifecycle': 'service'}


# ------------------------------------------------------------------ prompt hook fails closed
def test_hook_timeouts_leave_room_for_the_internal_deadline():
    # Redis connect (3 s) + a few 5 s socket ops + the acceptance wait must end before the
    # hook's own deadline, which itself ends well before Claude Code's hook timeout.
    assert fleet.PROMPT_WAIT + 10 <= fleet.PROMPT_DEADLINE <= fleet.PROMPT_HOOK_TIMEOUT - 15


def test_hook_crash_blocks_instead_of_passing(tmp_path):
    reason = fleet.claude_prompt_main(str(tmp_path / 'missing.json'), 'supervisor', '0',
                                      json.dumps({'prompt': 'do something'}))
    assert reason and reason.startswith('Fleet 输入未受理')


def test_hook_deadline_blocks_instead_of_passing(monkeypatch):
    def slow(*a, **kw):
        time.sleep(5)
        return None
    monkeypatch.setattr(fleet, 'claude_prompt', slow)
    started = time.monotonic()
    reason = fleet.claude_prompt_main('cfg', 'supervisor', '0', '{}', deadline=0.3)
    assert time.monotonic() - started < 2
    assert '超过 0.3 秒' in reason


def test_hook_pass_and_block_decisions_are_kept(monkeypatch):
    monkeypatch.setattr(fleet, 'claude_prompt', lambda *a, **kw: None)
    assert fleet.claude_prompt_main('cfg', 'supervisor', '0', '{}') is None
    monkeypatch.setattr(fleet, 'claude_prompt', lambda *a, **kw: 'queued')
    assert fleet.claude_prompt_main('cfg', 'supervisor', '0', '{}') == 'queued'


def test_hook_wait_is_clamped_inside_the_deadline(monkeypatch):
    waits = []
    monkeypatch.setattr(fleet, 'claude_prompt', lambda *a, wait, **kw: waits.append(wait))
    fleet.claude_prompt_main('cfg', 'supervisor', '0', '{}', deadline=12)
    fleet.claude_prompt_main('cfg', 'supervisor', '0', '{}')
    assert waits == [2, fleet.PROMPT_WAIT]


def test_hook_cli_prints_block_json_and_exits_zero_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'prompt': 'hello'})))
    out = io.StringIO()
    with redirect_stdout(out):
        fleet.main(['claude-prompt', str(tmp_path / 'missing.json'), 'supervisor', '0'])  # no SystemExit
    decision = json.loads(out.getvalue())
    assert decision['decision'] == 'block' and decision['reason'].startswith('Fleet 输入未受理')
