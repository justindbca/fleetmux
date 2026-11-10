"""Opt-in real cmux layout smoke; only temporary sleep terminals, no fleet/model/Redis.

Run inside cmux: python3 tests/cmux_layout_smoke.py --run
Uses its own registry, preserves existing workspaces, and closes only its own surfaces.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmux_fleet import cmux_manager as cm


def tree():
    p = cm._run('--json', '--id-format', 'uuids', 'tree', '--all')
    if p.returncode:
        raise RuntimeError(p.stderr)
    return json.loads(p.stdout)


def workspaces(snapshot):
    return {ws['id']: ws for window in snapshot['windows'] for ws in window['workspaces']}


def structure(ws):
    return {'layout': ws['layout'], 'surfaces': sorted(
        (s['id'], pane['id']) for pane in ws['panes'] for s in pane['surfaces'])}


def check(mgr, supervisor, workers, control, history):
    ws = workspaces(tree())[mgr._meta()['workspace']]
    panes = {s['id']: p['id'] for p in ws['panes'] for s in p['surfaces']}
    left, right, bottom = panes[supervisor], panes[workers[0]], panes[history]
    assert len(ws['panes']) == 3 and len({left, right, bottom}) == 3
    assert all(panes[w] == right for w in workers)
    assert panes[control] == bottom
    assert next(p for p in ws['panes'] if p['id'] == bottom)['selected_surface_id'] == history
    expected = {'direction': 'horizontal', 'children': [
        {'direction': 'vertical', 'children': [{'pane': {'id': left}}, {'pane': {'id': bottom}}]},
        {'pane': {'id': right}}]}

    def topology(node):
        return {k: [topology(c) for c in v] if k == 'children' else v
                for k, v in node.items() if k != 'split'}

    assert topology(ws['layout']) == expected, ws['layout']
    return structure(ws)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', required=True)
    parser.parse_args()
    before = tree()
    original = {wid: structure(ws) for wid, ws in workspaces(before).items()}
    selected = before.get('active', {}).get('workspace_id')
    with tempfile.TemporaryDirectory(prefix='cmux-layout-review-') as tmp:
        root = Path(tmp)
        tag = root.name
        mgr = cm.CmuxSessionManager(tag, agents_title=tag, control_title=tag + '-control', state_dir=root)
        created_workspaces = set()
        try:
            def spawn(label, role):
                result = mgr.spawn_window(label, '/bin/sleep 300', metadata={'type': role})
                assert result.success, result.error
                created_workspaces.update(w for w in (mgr._meta().get('workspace'),
                                                      mgr._meta().get('control_workspace')) if w)
                return result.pane_id

            control = spawn('control', 'daemon')
            supervisor = spawn('supervisor-1', 'supervisor')
            workers = [spawn('worker-' + str(i), 'worker') for i in range(1, 4)]
            page = root / 'history.html'
            page.write_text('<!doctype html><title>layout smoke</title><p>Temporary layout test</p>')
            history = mgr.arrange(supervisor, workers, page.as_uri())
            assert history
            first = check(mgr, supervisor, workers, control, history)
            assert mgr.arrange(supervisor, workers, page.as_uri()) == history
            assert check(mgr, supervisor, workers, control, history) == first
            successor = spawn('worker-1.1', 'worker')
            assert mgr.tab_into(successor, workers[0])
            assert mgr.kill_pane(workers[0])
            workers[0] = successor
            check(mgr, supervisor, workers, control, history)
            successor = spawn('supervisor-1.1', 'supervisor')
            assert mgr.tab_into(successor, supervisor)
            assert mgr.kill_pane(supervisor)
            check(mgr, successor, workers, control, history)
        finally:
            # cmux cannot close the last surface individually. These workspaces are
            # exclusively temporary sleep terminals; never force-close a foreign tab.
            try:
                current = workspaces(tree())
                for wid in created_workspaces:
                    if wid not in current:
                        continue
                    assert wid not in original
                    assert all(s['id'] in mgr._meta()['surfaces']
                               for p in current[wid]['panes'] for s in p['surfaces'])
                    result = cm._run('close-workspace', '--workspace', wid, '--force')
                    assert result.returncode == 0, result.stderr
            finally:
                if selected:
                    cm._run('select-workspace', '--workspace', selected)
        assert cm._wait_until(lambda: set(workspaces(tree())) == set(original)), 'workspace count changed after cleanup'
        after = workspaces(tree())
        assert all(structure(after[wid]) == shape for wid, shape in original.items()), 'existing layout changed'
    print('PASS: three panes; repeat arrange; worker/supervisor succession; history selected; existing layouts unchanged')


if __name__ == '__main__':
    main()
