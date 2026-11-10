"""Opt-in real cmux smoke of the lifecycle workspace closer (lifecycle design §3 「关工作区」, §5 row 6).

Run inside cmux: python3 tests/lifecycle_close_smoke.py --run
Uses its own registry and run directory in a temp dir; agents and the daemon are `sleep` terminals with a
unique marker (no model, no Redis). It checks, on real cmux:
  1. a registry claimed by another run is refused (nothing closed);
  2. the detached closer (`fleet.py lifecycle-close <cfg>`, started with a new session like
     lifecycle_close.after_daemon) closes every surface of this run and its workspaces;
  3. `ps` shows no process with the marker left;
  4. no workspace that existed before is closed, and the user's selected workspace is restored.
"""
import argparse
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmux_fleet import cmux_manager as cm  # noqa: E402
from cmux_fleet import fleet, lifecycle_close  # noqa: E402


def tree():
    p = cm._run('--json', '--id-format', 'uuids', 'tree', '--all')
    if p.returncode:
        raise RuntimeError(p.stderr)
    return json.loads(p.stdout)


def workspaces(snapshot):
    return {ws['id']: ws for window in snapshot['windows'] for ws in window['workspaces']}


def structure(ws):
    return sorted((s['id'], pane['id']) for pane in ws['panes'] for s in pane['surfaces'])


def marked(marker):
    out = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True).stdout
    return [line.strip() for line in out.splitlines() if marker in line and 'ps -axo' not in line]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', required=True)
    parser.parse_args()
    if not cm.cmux_available():
        raise SystemExit('run inside cmux')
    before = tree()
    original = {wid: structure(ws) for wid, ws in workspaces(before).items()}
    selected = before.get('active', {}).get('workspace_id')
    marker = 'lcsmoke' + uuid.uuid4().hex[:8]
    report = {'marker': marker}
    with tempfile.TemporaryDirectory(prefix='cmux-lifecycle-smoke-') as tmp:
        root = Path(tmp)
        run = root / 'run'
        (run / 'prompts').mkdir(parents=True)
        mgr = cm.CmuxSessionManager('lifecycle-smoke-' + marker, agents_title='Fleet smoke ' + marker,
                                    control_title='Fleet control smoke ' + marker, state_dir=root / 'state')
        cmd = sys.executable + ' -c "import time; time.sleep(600)" ' + marker  # marker in argv for ps
        panes = []
        for name in ('supervisor', 'worker-1', 'worker-2'):
            res = mgr.spawn_interactive_cli(window_name='fx-' + name, cli_command=cmd,
                                            output_file=str(run / 'prompts' / (name + '.terminal.log')),
                                            start_directory=str(run))
            assert res.success, res.error
            panes.append(res.pane_id)
        daemon = mgr.spawn_window('control', cmd, metadata={'type': 'daemon'}, start_directory=str(run))
        assert daemon.success, daemon.error
        time.sleep(2)
        report['spawned'] = len(marked(marker))
        assert report['spawned'] >= 4, marked(marker)
        ours = {mgr._meta()['workspace'], mgr._meta()['control_workspace']}
        cfg = {'run': str(run), 'session': mgr.session_name, 'cmux_state_dir': str(root / 'state'),
               'agents_title': mgr.agents_title, 'control_title': mgr.control_title}
        fleet.save(run / 'config.json', cfg)
        fleet.save(run / 'result.json', {'status': 'blocked', 'mode': 'smoke', 'closed_by': 'auto'})
        # 1. foreign registry: refused
        mgr.claim_run(str(root / 'someone-else'))
        refused = lifecycle_close.close_workspace(run / 'config.json', wait=1)
        assert refused.startswith('kept: cmux registry belongs'), refused
        assert all(mgr.get_pane_status(p) is not None for p in panes)
        report['foreign_registry'] = refused
        # 2. ours: the detached closer, new session, like after_daemon
        mgr.claim_run(str(run))
        log = open(run / 'lifecycle-close.log', 'a')
        proc = subprocess.Popen([sys.executable, str(Path(fleet.__file__).resolve()), 'lifecycle-close',
                                 str(run / 'config.json')], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True, cwd=str(run))
        assert proc.wait(timeout=120) == 0
        out = fleet.load(run / 'result.json')
        report['workspace'] = out.get('workspace')
        report['closer_log'] = (run / 'lifecycle-close.log').read_text().strip()
        assert out['workspace'] == 'closed', out
        after = tree()
        now = workspaces(after)
        report['our_workspaces_left'] = sorted(ours & set(now))
        assert not (ours & set(now)), 'our workspace is still open'
        # 3. no residue
        time.sleep(1)
        report['residue'] = marked(marker)
        assert not report['residue'], report['residue']
        # 4. everything that existed before is untouched
        # (other fleets keep working meanwhile, so tabs inside them may change; none may be closed)
        report['other_workspaces_closed'] = [wid for wid in original if wid not in now]
        report['other_workspaces_with_tab_changes'] = [wid for wid, s in original.items()
                                                       if wid in now and structure(now[wid]) != s]
        assert not report['other_workspaces_closed'], report['other_workspaces_closed']
        if selected:
            cm._run('select-workspace', '--workspace', selected)
        report['ok'] = True
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
