"""Control-tab docking is layout only: no process restart or forced window closure."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmux_fleet import cmux_manager as cm


@pytest.fixture
def layout(tmp_path, monkeypatch):
    mgr = cm.CmuxSessionManager('test')
    mgr.meta_path = tmp_path / 'state.json'
    mgr._save_meta({'workspace': 'agents', 'control_workspace': 'control-ws', 'surfaces': {
        'sup': {'type': 'supervisor'}, 'worker': {'type': 'worker'}, 'control': {'type': 'daemon'},
        'history': {'type': 'history'},
    }})
    live = {'agents': {'sup': {'pane_id': 'left'}, 'worker': {'pane_id': 'right'},
                       'history': {'pane_id': 'bottom-left'}},
            'control-ws': {'control': {'pane_id': 'control-pane'}}}
    calls = []
    state = {'fail_move': False}
    monkeypatch.setattr(mgr, '_surfaces', lambda ws: dict(live.get(ws, {})))

    def run(*args):
        calls.append(args)
        if args[0] == 'move-surface':
            if state['fail_move']:
                return SimpleNamespace(returncode=1, stdout='', stderr='move failed')
            sid = args[args.index('--surface') + 1]
            source = next(ws for ws in live if sid in live[ws])
            dest = args[args.index('--workspace') + 1]
            live[source].pop(sid)
            live[dest][sid] = {'pane_id': args[args.index('--pane') + 1]}
        if args[0] == 'close-workspace':
            assert not live[args[args.index('--workspace') + 1]]
            assert '--force' not in args
        return SimpleNamespace(returncode=0, stdout='OK', stderr='')
    monkeypatch.setattr(cm, '_run', run)
    return mgr, live, calls, state


def test_control_becomes_background_tab_in_history_pane(layout):
    mgr, live, calls, _ = layout
    assert mgr.dock_control('history')
    assert mgr._meta()['control_workspace'] == 'agents'
    assert mgr._where('control') == 'agents'
    assert live['agents']['control']['pane_id'] == live['agents']['history']['pane_id'] == 'bottom-left'
    assert live['agents']['sup']['pane_id'] == 'left'
    assert live['agents']['worker']['pane_id'] == 'right'
    assert calls[-1] == ('focus-panel', '--workspace', 'agents', '--panel', 'history')
    assert not any(c[0] in ('new-split', 'split-off', 'close-surface') for c in calls)


def test_control_docking_is_idempotent(layout):
    mgr, live, calls, _ = layout
    assert mgr.dock_control('history') and mgr.dock_control('history')
    assert set(live['agents']) == {'sup', 'worker', 'control', 'history'}
    assert sum(c[0] == 'close-workspace' for c in calls) == 1


def test_failed_move_keeps_control_workspace_and_registry(layout):
    mgr, live, calls, state = layout
    state['fail_move'] = True
    assert not mgr.dock_control('history')
    assert mgr._meta()['control_workspace'] == 'control-ws'
    assert 'control' in live['control-ws']
    assert not any(c[0] == 'close-workspace' for c in calls)


def test_never_close_source_workspace_with_someone_elses_tab(layout):
    mgr, live, calls, _ = layout
    live['control-ws']['unrelated'] = {'pane_id': 'extra'}
    assert mgr.dock_control('history')
    assert 'unrelated' in live['control-ws']
    assert not any(c[0] == 'close-workspace' for c in calls)


def test_no_owned_control_does_not_move_unrelated_surface(layout):
    mgr, live, calls, _ = layout
    meta = mgr._meta()
    meta['surfaces']['control']['type'] = 'other'
    mgr._save_meta(meta)
    assert not mgr.dock_control('history')
    assert not calls


# acceptance 10/13 P1-2: resume must recognise the native Claude binary (claude.exe), never a bare shell
@pytest.mark.parametrize('comm,args,live', [
    ('/opt/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe', '', True),
    ('/usr/local/bin/codex', '', True),
    ('node', 'node /opt/lib/node_modules/@anthropic-ai/claude-code/cli.js --name f1-worker-1', True),
    ('-zsh', '', False),
    ('node', 'node /srv/app/server.js', False),
    ('/bin/bash', '', False),
])
def test_cli_process_matches_native_claude_and_npm_node_only(tmp_path, monkeypatch, comm, args, live):
    mgr = cm.CmuxSessionManager('test')
    monkeypatch.setattr(mgr, '_where', lambda pane: 'ws')
    monkeypatch.setattr(mgr, '_surfaces', lambda ws: {'p': {'tty': 'ttys030'}})

    def ps(argv, **kw):
        out = ('-zsh\n' + comm + '\n') if argv[-1] == 'comm=' else ('-zsh\n' + args + '\n')
        return SimpleNamespace(returncode=0, stdout=out, stderr='')
    monkeypatch.setattr(cm.subprocess, 'run', ps)
    from cmux_fleet.fleet import RESUME_CLIS
    assert mgr.has_cli_process('p', RESUME_CLIS) is live
