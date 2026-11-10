"""Stateful layout regressions: count panes, not just successful cmux calls."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmux_fleet import cmux_manager as cm
from cmux_fleet import fleet


@pytest.fixture
def scene(tmp_path, monkeypatch):
    mgr = cm.CmuxSessionManager('layout-test', state_dir=tmp_path)
    live = {'agents': {sid: {'pane_id': 'left'} for sid in ('sup', 'w1', 'w2', 'w3')},
            'control-ws': {'control': {'pane_id': 'control-pane'}}}
    mgr._save_meta({'workspace': 'agents', 'control_workspace': 'control-ws', 'surfaces': {
        'sup': {'type': 'supervisor'}, 'control': {'type': 'daemon'},
        **{w: {'type': 'worker'} for w in ('w1', 'w2', 'w3')},
    }})
    calls, splits, selected = [], [], {}
    fail = set()
    monkeypatch.setattr(mgr, '_surfaces', lambda ws: {s: dict(v) for s, v in live.get(ws, {}).items()})

    def run(*args):
        calls.append(args)
        cmd = args[2] if args[0] == '--id-format' else args[0]
        if cmd in fail:
            return SimpleNamespace(returncode=1, stdout='', stderr='injected failure')
        out = 'OK'
        if cmd == 'new-surface':
            sid = 'history-' + str(sum('new-surface' in c for c in calls))
            pane = args[args.index('--pane') + 1]
            live['agents'][sid] = {'pane_id': pane}
            out = 'OK ' + sid
        elif cmd == 'split-off':
            sid = args[args.index('--surface') + 1]
            old = live['agents'][sid]['pane_id']
            new = 'pane-' + sid
            splits.append((old, args[args.index('--surface') + 2], new))
            live['agents'][sid]['pane_id'] = new
        elif cmd == 'move-surface':
            sid = args[args.index('--surface') + 1]
            src = next(ws for ws in live if sid in live[ws])
            dest = args[args.index('--workspace') + 1]
            live[src].pop(sid)
            live[dest][sid] = {'pane_id': args[args.index('--pane') + 1]}
        elif cmd == 'focus-panel':
            sid = args[args.index('--panel') + 1]
            selected[live['agents'][sid]['pane_id']] = sid
        elif cmd == 'close-workspace':
            assert not live[args[args.index('--workspace') + 1]]
            assert '--force' not in args
        return SimpleNamespace(returncode=0, stdout=out, stderr='')

    monkeypatch.setattr(cm, '_run', run)
    return SimpleNamespace(mgr=mgr, live=live, calls=calls, splits=splits, selected=selected, fail=fail)


def arrange(s):
    return s.mgr.arrange('sup', ['w1', 'w2', 'w3'], 'file:///run/history.html')


def assert_layout(s, history):
    live = s.live['agents']
    assert live['sup']['pane_id'] == 'left'
    assert {live[w]['pane_id'] for w in ('w1', 'w2', 'w3')} == {'pane-w1'}
    assert live[history]['pane_id'] == live['control']['pane_id'] == 'pane-' + history
    assert len({v['pane_id'] for v in live.values()}) == 3
    assert s.selected['pane-' + history] == history
    assert s.splits == [('left', 'right', 'pane-w1'), ('left', 'down', 'pane-' + history)]


def test_arrange_twice_keeps_exactly_three_panes_and_one_history(scene):
    history = arrange(scene)
    assert history
    assert_layout(scene, history)
    assert arrange(scene) == history
    assert_layout(scene, history)
    assert sum('new-surface' in c for c in scene.calls) == 1
    assert sum(c[0] == 'close-workspace' for c in scene.calls) == 1


@pytest.mark.parametrize('role', ['sup', 'w1', 'w2'])
def test_successor_stays_in_predecessors_pane_without_splitting(scene, role):
    history = arrange(scene)
    old_pane = scene.live['agents'][role]['pane_id']
    scene.live['agents']['successor'] = {'pane_id': 'left'}
    scene.mgr._tag('successor', type='worker' if role.startswith('w') else 'supervisor')
    assert scene.mgr.tab_into('successor', role)
    assert scene.live['agents']['successor']['pane_id'] == old_pane
    assert len(scene.splits) == 2
    assert_layout(scene, history)


@pytest.mark.parametrize('url', [None, 'file:///another-run/history.html'])
def test_foreign_or_legacy_history_is_not_nested_or_deleted(scene, url):
    scene.live['agents']['old-history'] = {'pane_id': 'old-history-pane'}
    scene.mgr._tag('old-history', type='history', **({'url': url} if url else {}))
    assert arrange(scene) is None
    assert not scene.calls


@pytest.mark.parametrize('failure', ['split-off', 'move-surface'])
def test_worker_placement_failure_stops_before_creating_history(scene, failure):
    scene.fail.add(failure)
    assert arrange(scene) is None
    assert not any('new-surface' in c for c in scene.calls)
    assert 'control' in scene.live['control-ws']


def test_history_split_failure_retries_existing_browser_not_new_nested_split(scene):
    assert scene.mgr.place_right_of('w1', 'sup')
    scene.fail.add('split-off')
    assert arrange(scene) is None
    assert 'control' in scene.live['control-ws']
    scene.fail.clear()
    history = arrange(scene)
    assert_layout(scene, history)
    assert sum('new-surface' in c for c in scene.calls) == 1


def test_control_move_failure_preserves_console_and_retry_does_not_split(scene, monkeypatch):
    # Fail docking only, after worker tabs have been placed.
    for worker in ('w1', 'w2', 'w3'):
        scene.live['agents'][worker]['pane_id'] = 'right'
    real_dock = scene.mgr.dock_control
    monkeypatch.setattr(scene.mgr, 'dock_control', lambda sid: False)
    history = arrange(scene)
    assert history and 'control' in scene.live['control-ws']
    monkeypatch.setattr(scene.mgr, 'dock_control', real_dock)
    assert arrange(scene) == history
    assert len(scene.splits) == 1
    assert scene.live['agents']['control']['pane_id'] == 'pane-' + history


def test_daemon_uses_persisted_registry_even_without_start_environment(tmp_path, monkeypatch):
    default = tmp_path / 'default-state'
    isolated = tmp_path / 'isolated-state'
    monkeypatch.delenv('CMUX_FLEET_STATE', raising=False)
    monkeypatch.setattr(cm, 'STATE_DIR', default)
    old = cm.CmuxSessionManager(fleet.SESSION)
    old._save_meta({'workspace': 'existing-fleet', 'control_workspace': None, 'surfaces': {}})
    isolated_mgr = cm.CmuxSessionManager(fleet.SESSION, state_dir=isolated)
    isolated_mgr._save_meta({'workspace': None, 'control_workspace': 'test-control', 'surfaces': {}})
    before = old.meta_path.read_bytes()
    cfg_path = tmp_path / 'config.json'
    cfg_path.write_text(json.dumps({'run': str(tmp_path), 'cmux_state_dir': str(isolated)}))
    observed = []
    monkeypatch.setattr(fleet, 'connect', lambda cfg: SimpleNamespace(shutdown=lambda: None))
    monkeypatch.setattr(fleet.signal, 'signal', lambda *args: None)

    def make_fleet(path, r, mgr):
        observed.append(mgr)
        return SimpleNamespace(run=lambda: None, r=r, v2=None)

    monkeypatch.setattr(fleet, 'Fleet', make_fleet)
    fleet.daemon(cfg_path)
    assert observed[0].meta_path == isolated_mgr.meta_path
    assert observed[0]._meta()['control_workspace'] == 'test-control'
    assert old.meta_path.read_bytes() == before


@pytest.mark.parametrize('command', ['new-surface', 'split-off', 'move-surface'])
def test_layout_waits_for_tree_after_ok_without_repeating_mutation(scene, monkeypatch, command):
    real_run, real_surfaces = cm._run, scene.mgr._surfaces
    stale = []
    observed_stale = []

    def delayed_run(*args):
        before = {ws: real_surfaces(ws) for ws in scene.live}
        result = real_run(*args)
        if command in args:
            stale.extend([before, before])
        return result

    def surfaces(ws):
        if stale:
            observed_stale.append(ws)
            return stale.pop(0).get(ws, {})
        return real_surfaces(ws)

    monkeypatch.setattr(cm, '_run', delayed_run)
    monkeypatch.setattr(scene.mgr, '_surfaces', surfaces)
    monkeypatch.setattr(cm.time, 'sleep', lambda _: None)
    history = arrange(scene)
    assert history and observed_stale
    assert_layout(scene, history)
    assert sum('new-surface' in c for c in scene.calls) == 1
    assert sum('split-off' in c for c in scene.calls) == 2
    assert sum('move-surface' in c for c in scene.calls) == 3


def test_unconfirmed_close_keeps_ownership_for_retry(scene, monkeypatch):
    wait = cm._wait_until
    monkeypatch.setattr(cm, '_wait_until', lambda check: wait(check, timeout=0))
    assert not scene.mgr.kill_pane('w1')  # mock acknowledges, but tab remains live
    assert 'w1' in scene.mgr._meta()['surfaces']


def test_unconfirmed_worker_split_stops_layout(scene, monkeypatch):
    wait, real_run = cm._wait_until, cm._run
    monkeypatch.setattr(cm, '_wait_until', lambda check: wait(check, timeout=0))
    monkeypatch.setattr(cm, '_run', lambda *a: SimpleNamespace(returncode=0, stdout='OK', stderr='')
                        if a[0] == 'split-off' else real_run(*a))
    assert arrange(scene) is None
    assert not any('new-surface' in c for c in scene.calls)


@pytest.mark.parametrize('failure', ['new-workspace', 'tree', 'ambiguous-title'])
def test_workspace_creation_failure_returns_spawn_error_not_unpack_crash(tmp_path, monkeypatch, failure):
    mgr = cm.CmuxSessionManager(state_dir=tmp_path)
    monkeypatch.setattr(mgr, '_surfaces', lambda ws: {})

    def run(*args):
        cmd = args[2] if args[:2] == ('--id-format', 'uuids') else args[3] if args[0] == '--json' else args[0]
        return SimpleNamespace(returncode=int(cmd == failure), stdout='{}' if cmd == 'tree' else 'OK workspace-id', stderr='')

    monkeypatch.setattr(cm, '_run', run)
    result = mgr.spawn_window('control', 'true', metadata={'type': 'daemon'})
    assert not result.success
