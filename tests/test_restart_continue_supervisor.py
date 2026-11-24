"""A continued run takes a new supervisor card (docs/design.md section 60, D6): the operator stops a service root
that holds open jobs to change its supervisor and starts it again. The run is continued (section 58); its root
supervisor is always a new generation on resume, so the changed supervisor model / profile applies; an option that
cannot apply (another supervisor CLI, worker options) is printed by start and listed in the reconcile ledger."""
import json

import pytest

from restart_kit import items, kill_extra, tick
from test_escalate import ok_check  # noqa: F401  (fixture)
from test_job_queue_restart import all_ready, continue_root, explicit_stop, jobs
from test_job_queue_restart_start import START, home, make_run, rows  # noqa: F401  (fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_service_escalate import result  # noqa: F401
from test_service_job import start_job
from test_service_split import split_tree  # noqa: F401
from test_subfleet import step
from fleetmux import fleet, lifecycle_close, restart_state


def test_start_applies_the_supervisor_profile_to_the_continued_run_and_names_the_rest(home, monkeypatch, capsys):
    run = make_run(home.path, rows=rows('job-a'))
    cfg = fleet.load(run / 'config.json')
    cfg.update(supervisor_cli='codex', supervisor_profile='old')
    seen = []
    monkeypatch.setattr(fleet, 'resume', lambda r, args: seen.append(fleet.continue_supervisor(cfg, args)) or True)
    fleet.main(START + ['--supervisor-profile', 'fast', '--workers', '3'])
    assert cfg['supervisor_profile'] == 'fast'
    assert cfg['supervisor_continue']['keys'] == ['supervisor_profile']  # the daemon drops the old launch once
    assert '--workers' in seen[0] and not [o for o in seen[0] if o.startswith('--supervisor')]
    out = capsys.readouterr().out
    assert 'not applied' in out and '--workers' in out


def test_another_supervisor_cli_is_not_applied_and_said_so(home, monkeypatch, capsys):
    run = make_run(home.path, rows=rows('job-a'))
    cfg = fleet.load(run / 'config.json')
    cfg.update(supervisor_cli='claude', supervisor_model='opus')
    seen = []
    monkeypatch.setattr(fleet, 'resume', lambda r, args: seen.append(fleet.continue_supervisor(cfg, args)) or True)
    fleet.main(START + ['--supervisor-model', 'astra'])  # START types --supervisor-cli codex
    assert cfg['supervisor_model'] == 'opus' and seen and seen[0][0].startswith('--supervisor-cli codex')
    warn = 'stop --final cancels the open jobs, so to keep them wait until they are done, then change the CLI'
    assert warn in seen[0][0]  # the ledger item text (restart_not_applied)
    assert warn in capsys.readouterr().out  # printed by start


@pytest.mark.usefixtures('job_chains_as_before')
def test_open_job_and_a_new_supervisor_profile_after_stop_and_start(split_tree, monkeypatch):
    t = split_tree
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: 'sent')
    jid, key = start_job(t)
    gen = int(t.f.agent('supervisor')['generation'])
    restart_state.on_stop(t.f)
    explicit_stop(t)
    cfg = fleet.load(t.f.path)  # what `start` writes through continue_supervisor before the daemon resumes
    cfg.update(supervisor_profile='fast', restart_not_applied=['--supervisor-cli claude (needs a new run)'])
    fleet.save(t.f.path, cfg)
    f = continue_root(t)
    all_ready(t)
    for _ in range(3):
        f.tick()
        step(t)
    assert int(f.agent('supervisor')['generation']) > gen  # a new generation ...
    assert f.runtime_cfg('supervisor').get('supervisor_profile') == 'fast'  # ... on the new card
    assert jid in jobs(f) and result(f, jid) is None  # the open job goes on
    assert [i['key'] for i in items(f, 'start')] == ['option --supervisor-cli claude (needs a new run)']
    kill_extra(t)


def test_an_option_this_supervisor_cli_does_not_read_is_not_printed_as_applied(home, monkeypatch, capsys):
    run = make_run(home.path, rows=rows('job-a'))
    cfg = fleet.load(run / 'config.json')
    cfg.update(supervisor_cli='codex', supervisor_profile='old', supervisor_model='opus')
    seen = []
    monkeypatch.setattr(fleet, 'resume', lambda r, args: seen.append(fleet.continue_supervisor(cfg, args)) or True)
    fleet.main(START + ['--supervisor-model', 'sonnet'])  # a codex supervisor does not read --supervisor-model
    out = capsys.readouterr().out
    assert cfg['supervisor_model'] == 'opus' and 'supervisor_continue' not in cfg
    assert 'next supervisor generation runs --supervisor-model' not in out
    assert any(o.startswith('--supervisor-model sonnet') for o in seen[0]) and 'not applied' in out


@pytest.mark.usefixtures('job_chains_as_before')
def test_open_job_and_a_new_supervisor_model_win_over_the_old_launch_record(split_tree, monkeypatch):
    t = split_tree
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: 'sent')
    jid, key = start_job(t)
    cli = fleet.load(t.f.path).get('supervisor_cli') or 'codex'
    k, value = ('supervisor_model', 'sonnet') if cli == 'claude' else ('supervisor_profile', 'fast')
    wall = json.dumps({'profile_id': 'wall-card', 'overrides': {k: 'old-wall-card'}, 'role_text': ''})
    t.f.setf('supervisor', runtime_launch=wall)  # a usage-wall fallback card the old generation ran
    assert t.f.runtime_cfg('supervisor')[k] == 'old-wall-card'
    restart_state.on_stop(t.f)
    explicit_stop(t)
    cfg = fleet.load(t.f.path)  # what `start` writes through continue_supervisor (driven for real below)
    cfg.update({k: value, 'supervisor_continue': {'id': 'c1', 'keys': [k]}})
    fleet.save(t.f.path, cfg)
    f = continue_root(t)
    all_ready(t)
    for _ in range(3):
        f.tick()
        step(t)
    assert f.runtime_cfg('supervisor')[k] == value  # not the old launch record's card
    assert jid in jobs(f) and result(f, jid) is None
    assert any(x.get('event') == 'restart.supervisor_continue' for _, x in f.r.xrange(f.v2.bus.audit_key))
    kill_extra(t)


@pytest.mark.usefixtures('job_chains_as_before')
def test_real_start_resume_writes_the_supervisor_change_to_the_config_the_daemon_reads(split_tree, monkeypatch,
                                                                                        tmp_path):
    """fleet.resume (what `start` calls for a continued run) up to the daemon launch, with only the launch stubbed:
    the config on disk carries the new supervisor value, the change id and the options it did not apply."""
    from types import SimpleNamespace
    from fleetmux import cmux_manager
    t = split_tree
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: 'sent')
    jid, key = start_job(t)
    restart_state.on_stop(t.f)
    explicit_stop(t)
    launched = []
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: SimpleNamespace(
        spawn_window=lambda *a, **k: launched.append(a) or SimpleNamespace(success=True)))
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    monkeypatch.setattr(fleet, 'daemon_running', lambda path: False)
    cli = fleet.load(t.f.path).get('supervisor_cli') or 'codex'
    k, value = ('supervisor_model', 'sonnet') if cli == 'claude' else ('supervisor_profile', 'fast')
    args = SimpleNamespace(resume=str(t.f.run_dir), extend_hours=None, max_attempts=None, roster=None, **{k: value})
    args.workers = 7  # a start option a continued run does not apply
    assert fleet.resume(t.f.run_dir, args) is True and len(launched) == 1
    saved = fleet.load(t.f.path)
    assert saved[k] == value and saved['supervisor_continue']['keys'] == [k]
    assert any(o.startswith('--workers') for o in saved['restart_not_applied'])
    f = continue_root(t)  # the daemon resumes from that config
    all_ready(t)
    for _ in range(3):
        f.tick()
        step(t)
    assert f.runtime_cfg('supervisor')[k] == value and jid in jobs(f)
    assert [i['key'] for i in items(f, 'start')] == ['option --workers']
    kill_extra(t)
