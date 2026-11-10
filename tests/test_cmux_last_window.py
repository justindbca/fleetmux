"""A fleet must never leave cmux without a window (macmini 2026-10-13: closing the window's last
workspace closed the window; every later start failed with "cmux surface creation failed")."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmux_fleet import cmux_manager as cm


class FakeCmux:
    """Windows -> workspaces -> surfaces, with the cmux 0.64.25 rules seen on macmini:
    close-surface refuses a workspace's last surface; closing a window's last workspace closes the
    window; with no window, new-workspace fails (TabManager not available)."""

    def __init__(self, windows, fail=(), no_windows_text=False):
        self.windows = windows  # {window_id: {workspace_id: {surface_id: {}}}}
        self.no_windows_text = no_windows_text  # answer "No windows" (plain text) instead of [] when empty
        self.fail = set(fail)
        self.calls = []
        self.titles = {}
        self.n = 0

    def where(self, ws):
        return next((w for w, spaces in self.windows.items() if ws in spaces), None)

    def surfaces(self, ws):
        w = self.where(ws)
        return {s: {'pane_id': 'p-' + s, 'tty': None, 'title': s} for s in self.windows[w][ws]} if w else {}

    def close_workspace(self, ws):
        w = self.where(ws)
        if w:
            self.windows[w].pop(ws)
            if not self.windows[w]:
                self.windows.pop(w)  # the window goes with its last workspace

    def ok(self, out='OK'):
        return SimpleNamespace(returncode=0, stdout=out, stderr='')

    def run(self, *args):
        self.calls.append(args)
        words = [a for a in args if a not in ('--json', '--id-format', 'uuids')]
        cmd = words[0]
        if cmd in self.fail:
            return SimpleNamespace(returncode=1, stdout='', stderr='Error: injected ' + cmd)
        opt = lambda name: args[args.index(name) + 1] if name in args else None  # noqa: E731
        if cmd == 'list-windows':
            if self.no_windows_text and not self.windows:
                return self.ok('No windows')
            return self.ok(json.dumps([{'id': w, 'workspace_count': len(s)} for w, s in self.windows.items()]))
        if cmd == 'tree':
            if '--all' in args:
                return self.ok(json.dumps({'windows': [{'id': w, 'workspaces': [{'id': ws} for ws in s]}
                                                       for w, s in self.windows.items()]}))
            ws = opt('--workspace')
            w = self.where(ws) or next((w for w, s in self.windows.items() for x in s if self.titles.get(x) == ws), None)
            spaces = [x for x in self.windows.get(w, {}) if ws in (x, self.titles.get(x))]
            return self.ok(json.dumps({'windows': [{'id': w, 'workspaces': [{'id': x, 'title': self.titles.get(x)}
                                                                             for x in spaces]}] if w else []}))
        if cmd == 'new-window':
            self.n += 1
            self.windows['win-' + str(self.n)] = {'blank-' + str(self.n): {'shell-' + str(self.n): {}}}
            return self.ok('OK win-' + str(self.n))
        if cmd == 'new-workspace':
            w = opt('--window') or next(iter(self.windows), None)
            if w not in self.windows:
                return SimpleNamespace(returncode=1, stdout='', stderr='Error: unavailable: TabManager not available')
            self.n += 1
            ws = 'ws-' + str(self.n)
            self.windows[w][ws] = {'s-' + str(self.n): {}}
            self.titles[ws] = opt('--name')
            return self.ok('OK ' + ws)
        if cmd == 'close-surface':
            ws, sid = opt('--workspace'), opt('--surface')
            w = self.where(ws)
            if w and len(self.windows[w][ws]) == 1:
                return SimpleNamespace(returncode=1, stdout='', stderr='Error: invalid_state: Cannot close the last surface')
            if w:
                self.windows[w][ws].pop(sid, None)
            return self.ok()
        if cmd == 'close-workspace':
            self.close_workspace(opt('--workspace'))
            return self.ok()
        return self.ok()


def manager(tmp_path, monkeypatch, fake, meta=None):
    monkeypatch.setattr(cm, '_run', fake.run)
    monkeypatch.setattr(cm, '_wait_until', lambda check, timeout=3.0: check())
    mgr = cm.CmuxSessionManager('cmux-fleet', state_dir=tmp_path)
    monkeypatch.setattr(mgr, '_surfaces', fake.surfaces)
    if meta:
        mgr._save_meta(meta)
    return mgr


def fleet_meta(ws='fleet-ws'):
    return {'workspace': ws, 'control_workspace': ws, 'run': '/r',
            'surfaces': {'sup': {'type': 'supervisor'}, 'w1': {'type': 'worker'}, 'hist': {'type': 'history'}}}


def destructive(fake):
    return [c for c in fake.calls if c[0] in ('close-surface', 'close-workspace', 'close-window')]


def test_closing_the_windows_only_workspace_leaves_a_blank_workspace_first(tmp_path, monkeypatch):
    fake = FakeCmux({'win': {'fleet-ws': {'sup': {}, 'w1': {}, 'hist': {}}}})
    mgr = manager(tmp_path, monkeypatch, fake, fleet_meta())
    assert mgr.close_all()
    assert list(fake.windows) == ['win'] and 'fleet-ws' not in fake.windows['win']
    first_new = next(i for i, c in enumerate(fake.calls) if 'new-workspace' in c)
    assert first_new < fake.calls.index(destructive(fake)[0])  # the blank workspace exists before any close
    assert fake.calls[first_new][fake.calls[first_new].index('--window') + 1] == 'win'


def test_window_with_someone_elses_workspace_gets_no_placeholder(tmp_path, monkeypatch):
    fake = FakeCmux({'win': {'user-ws': {'tab': {}}, 'fleet-ws': {'sup': {}, 'w1': {}, 'hist': {}}}})
    mgr = manager(tmp_path, monkeypatch, fake, fleet_meta())
    assert mgr.close_all()
    assert fake.windows == {'win': {'user-ws': {'tab': {}}}}
    assert not any('new-workspace' in c for c in fake.calls)


def test_placeholder_failure_touches_nothing(tmp_path, monkeypatch):
    fake = FakeCmux({'win': {'fleet-ws': {'sup': {}, 'w1': {}, 'hist': {}}}}, fail={'new-workspace'})
    mgr = manager(tmp_path, monkeypatch, fake, fleet_meta())
    killed = []
    monkeypatch.setattr(mgr, 'kill_pane', lambda sid: killed.append(sid) or True)
    assert not mgr.close_all()
    assert not killed and not destructive(fake)
    assert fake.windows == {'win': {'fleet-ws': {'sup': {}, 'w1': {}, 'hist': {}}}}
    assert set(mgr._meta()['surfaces']) == {'sup', 'w1', 'hist'}  # ownership kept for a later retry


def test_dock_control_keeps_a_window_whose_only_workspace_is_the_empty_control(tmp_path, monkeypatch):
    fake = FakeCmux({'win-a': {'agents': {'sup': {}, 'hist': {}}}, 'win-b': {'control-ws': {'control': {}}}})
    mgr = manager(tmp_path, monkeypatch, fake, {'workspace': 'agents', 'control_workspace': 'control-ws',
                                                'surfaces': {'sup': {}, 'hist': {'type': 'history'},
                                                             'control': {'type': 'daemon'}}})

    def move(*args):
        if args[0] == 'move-surface':
            fake.calls.append(args)
            fake.windows['win-b']['control-ws'].pop('control')
            fake.windows['win-a']['agents']['control'] = {}
            return fake.ok()
        return FakeCmux.run(fake, *args)
    monkeypatch.setattr(cm, '_run', move)
    monkeypatch.setattr(mgr, '_surfaces', lambda ws: {s: {'pane_id': 'p', 'tty': None} for s in
                                                      fake.windows.get(fake.where(ws), {}).get(ws, {})})
    assert mgr.dock_control('hist')
    assert 'win-b' in fake.windows and 'control-ws' not in fake.windows['win-b']


@pytest.mark.parametrize('text', [False, True])
def test_start_with_no_window_opens_one_then_creates_the_workspace(tmp_path, monkeypatch, text):
    fake = FakeCmux({}, no_windows_text=text)
    mgr = manager(tmp_path, monkeypatch, fake)
    result = mgr.spawn_window('control', 'true', metadata={'type': 'daemon'}, start_directory=str(tmp_path))
    assert result.success, result.error
    names = [c[0] if c[0] != '--id-format' else c[2] for c in fake.calls]
    assert names.index('new-window') < names.index('new-workspace')
    assert mgr._meta()['control_workspace'] in fake.windows['win-1']


def test_no_window_and_new_window_failing_names_the_window_problem(tmp_path, monkeypatch):
    fake = FakeCmux({}, fail={'new-window'})
    mgr = manager(tmp_path, monkeypatch, fake)
    result = mgr.spawn_window('control', 'true', metadata={'type': 'daemon'}, start_directory=str(tmp_path))
    assert not result.success and 'no window' in result.error
    assert not any('new-workspace' in c for c in fake.calls)


def test_new_workspace_failure_carries_cmux_reason(tmp_path, monkeypatch):
    fake = FakeCmux({'win': {'user-ws': {'tab': {}}}}, fail={'new-workspace'})
    mgr = manager(tmp_path, monkeypatch, fake)
    result = mgr.spawn_window('control', 'true', metadata={'type': 'daemon'}, start_directory=str(tmp_path))
    assert not result.success and 'new-workspace failed' in result.error and 'injected' in result.error


@pytest.mark.parametrize('answer', ['OK', '', 'not json'])
def test_unreadable_topology_keeps_the_old_behaviour(tmp_path, monkeypatch, answer):
    """Deliberate: when cmux does not answer tree/list-windows in JSON, closing and spawning go on as before."""
    live = {'ws': {'sup': {}, 'hist': {}}}
    calls = []

    def run(*args):
        calls.append(args)
        if args[0] == 'close-workspace':
            live.pop(args[args.index('--workspace') + 1], None)
        return SimpleNamespace(returncode=0, stdout=answer, stderr='')
    monkeypatch.setattr(cm, '_run', run)
    monkeypatch.setattr(cm, '_wait_until', lambda check, timeout=3.0: check())
    mgr = cm.CmuxSessionManager('cmux-fleet', state_dir=tmp_path)
    monkeypatch.setattr(mgr, '_surfaces', lambda ws: {s: {'tty': None} for s in live.get(ws, {})})
    monkeypatch.setattr(mgr, 'kill_pane', lambda sid: True)
    mgr._save_meta({'workspace': 'ws', 'control_workspace': 'ws', 'surfaces': {'sup': {}, 'hist': {}}})
    assert mgr.close_all() and 'ws' not in live
    assert not any('new-workspace' in c or 'new-window' in c for c in calls)
