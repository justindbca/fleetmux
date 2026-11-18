"""`fleetmux tabs` (fleetmux/tabs_cli.py): a running fleet's tab titles and pane text, read from its fleetmux server
without a screen. Read-only: the server sees only hello / page.list / pane.list / pane.read.

The server tests run through test_fleetmux_backend's ``impl`` fixture: ``fake`` = tests/fake_fleetmux.py (runs
everywhere), ``real`` = the built Rust server (skipped when it is not built)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fleetmux_backend import SESSION, impl, new_mgr, py, spawn, wait_for  # noqa: E402,F401
from fleetmux import fleet, join, tabs_cli  # noqa: E402
from fleetmux.backends import fleetmux as fm  # noqa: E402

PAGE = 'job-1'
APPLET_TEXT = 'demo applet ready'


def world(impl, tmp_path):
    """A root page with a supervisor, an applet tab (lower group) and a worker; a sub-fleet page with a worker of
    the same title. Returns (root config, child config, pane ids)."""
    root = new_mgr(impl)
    ids = {'supervisor': spawn(root, 'supervisor-1.1', type_='supervisor'),
           'applet': spawn(root, 'applet-demo-1 up', type_='tab',
                           command=py("import time; print(%r, flush=True); time.sleep(600)" % APPLET_TEXT)),
           'worker': spawn(root, 'worker-1.1')}
    root._call('page.open', page=PAGE, title='job one')
    child = fm.FleetmuxSessionManager('fleetmux_' + PAGE, state_dir=impl.state)
    child.bind_page(SESSION, PAGE)
    ids['job_worker'] = spawn(child, 'worker-1.1')
    base = {'backend': 'fleetmux', 'session': SESSION, 'cmux_state_dir': str(impl.state)}
    root_cfg = tmp_path / 'root' / 'config.json'
    fleet.save(root_cfg, dict(base, run=str(root_cfg.parent)))
    child_cfg = tmp_path / 'child' / 'config.json'
    fleet.save(child_cfg, dict(base, run=str(child_cfg.parent), session='fleetmux_' + PAGE, fleetmux_page=PAGE,
                               fleetmux_root_session=SESSION, parent_config=str(root_cfg)))
    return root_cfg, child_cfg, ids


def listed(capsys, *argv):
    assert tabs_cli.main(list(argv) + ['--json']) == 0
    return json.loads(capsys.readouterr().out)


def test_tabs_lists_every_page_of_a_root_fleet(impl, tmp_path, capsys):
    root_cfg, _, ids = world(impl, tmp_path)
    rows = listed(capsys, '--config', str(root_cfg))
    by_pane = {r['pane']: r for r in rows}
    assert set(by_pane) == set(ids.values())
    applet = by_pane[ids['applet']]
    assert (applet['page'], applet['group'], applet['title'], applet['type']) == ('', 'lower', 'applet-demo-1 up', 'tab')
    assert isinstance(applet['pid'], int) and applet['pid'] > 0
    job = by_pane[ids['job_worker']]
    assert (job['page'], job['page_title'], job['group'], job['title']) == (PAGE, 'job one', 'workers', 'worker-1.1')
    assert [r['pane'] for r in rows][:3] == [ids['supervisor'], ids['applet'], ids['worker']]  # root page first

    assert tabs_cli.main(['--config', str(root_cfg)]) == 0
    text = capsys.readouterr().out
    line = next(ln for ln in text.splitlines() if 'applet-demo-1 up' in ln)
    assert 'lower' in line and 'tab' in line and ids['applet'] in line
    assert 'job one' in text


def test_tabs_on_a_sub_fleet_config_lists_its_own_page(impl, tmp_path, capsys):
    _, child_cfg, ids = world(impl, tmp_path)
    assert [r['pane'] for r in listed(capsys, '--config', str(child_cfg))] == [ids['job_worker']]


def test_tabs_read_prints_a_tab_by_title_prefix_or_pane_id(impl, tmp_path, capsys):
    root_cfg, _, ids = world(impl, tmp_path)

    def shown():
        code = tabs_cli.main(['read', 'applet-demo', '--config', str(root_cfg), '--lines', '20'])
        return code == 0 and APPLET_TEXT in capsys.readouterr().out
    assert wait_for(shown), 'the applet tab never showed its text'
    assert tabs_cli.main(['read', ids['applet'], '--config', str(root_cfg)]) == 0
    assert APPLET_TEXT in capsys.readouterr().out
    assert tabs_cli.main(['read', 'applet-demo', '--config', str(root_cfg), '--lines', '1']) == 0
    assert capsys.readouterr().out == APPLET_TEXT + '\n'  # the blank rows below the text are not counted


def test_tabs_read_refuses_an_unknown_or_ambiguous_target(impl, tmp_path, capsys):
    root_cfg, _, ids = world(impl, tmp_path)
    assert tabs_cli.main(['read', 'no-such-tab', '--config', str(root_cfg)]) == 2
    assert 'no tab' in capsys.readouterr().err
    assert tabs_cli.main(['read', 'worker-1', '--config', str(root_cfg)]) == 2  # one in each page
    err = capsys.readouterr().err
    assert 'several tabs' in err and ids['worker'] in err and ids['job_worker'] in err


def test_tabs_sends_only_read_commands(impl, tmp_path, monkeypatch, capsys):
    if impl.kind != 'fake':
        pytest.skip('the request log is a fake-server knob')
    log = tmp_path / 'requests.log'
    monkeypatch.setenv('FAKE_FM_LOG', str(log))  # read by the server when it starts (the first spawn)
    root_cfg, child_cfg, ids = world(impl, tmp_path)
    mark = len(log.read_text().splitlines())
    listed(capsys, '--config', str(root_cfg))
    listed(capsys, '--config', str(child_cfg))
    assert tabs_cli.main(['read', ids['applet'], '--config', str(root_cfg)]) == 0
    sent = {json.loads(line).get('cmd') for line in log.read_text().splitlines()[mark:]}
    assert sent and sent <= {'hello', 'page.list', 'pane.list', 'pane.read'}, sent


def test_tabs_never_starts_a_server(impl, tmp_path, capsys):
    cfg = tmp_path / 'config.json'
    fleet.save(cfg, {'run': str(tmp_path), 'backend': 'fleetmux', 'session': SESSION,
                     'cmux_state_dir': str(impl.state)})
    assert tabs_cli.main(['--config', str(cfg)]) == 3
    assert 'no fleetmux server answers' in capsys.readouterr().err
    assert not (impl.state / 'fleetmux').exists()


# ------------------------------------------------------------------ no server needed
def test_tabs_refuses_another_backend(tmp_path, capsys):
    cfg = tmp_path / 'config.json'
    fleet.save(cfg, {'run': str(tmp_path), 'backend': 'cmux', 'session': SESSION})
    assert tabs_cli.main(['--config', str(cfg)]) == 2
    assert 'fleetmux backend' in capsys.readouterr().err


def test_tabs_follows_a_join_stub(tmp_path, monkeypatch):
    child = tmp_path / 'child.json'
    fleet.save(child, {'run': str(tmp_path), 'backend': 'fleetmux', 'session': 'x'})
    root = tmp_path / 'root.json'
    fleet.save(root, {'run': str(tmp_path), 'backend': 'fleetmux', 'session': 'y'})
    monkeypatch.setattr(join, 'child_config_path', lambda stub: child)
    service = tmp_path / 'service-stub.json'
    fleet.save(service, {'join': {'kind': 'service', 'child': 'docs', 'root_config': str(root)}})
    assert tabs_cli.config(service)['session'] == 'x'  # a service stub: its sub-fleet
    task = tmp_path / 'task-stub.json'
    fleet.save(task, {'join': {'root_config': str(root), 'jobs': []}})
    assert tabs_cli.config(task)['session'] == 'y'  # a job stub: the root, whose pages hold the jobs


def test_fleet_main_dispatches_tabs(monkeypatch):
    seen = []
    monkeypatch.setattr(tabs_cli, 'main', lambda argv: seen.append(argv) or 0)
    with pytest.raises(SystemExit) as exit_:
        fleet.main(['tabs', 'read', 'x', '--lines', '5'])
    assert exit_.value.code == 0 and seen == [['read', 'x', '--lines', '5']]
