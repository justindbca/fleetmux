"""Read-only fleet transport inventory; never connects to Redis or controls processes.

May be piped to a remote `python3 -` for a read-only inventory. Only whitelisted
configuration/process metadata is emitted; message contents and credentials are
not read. Discovery coverage is reported, not assumed to include every machine.
"""
import json
from pathlib import Path
import shlex
import socket
import subprocess

from cmux_fleet.runtime_v2 import v2_cli_problem


def inventory():
    home = Path.home()
    roots, seen = [], set()  # ~/.fleetmux and ~/.fleetmux/fleets/* (§50.1); a migrated ~/.cmux-fleet links there
    for path in ([home/'.fleetmux'] + sorted((home/'.fleetmux'/'fleets').glob('*')) + sorted(home.glob('.cmux-fleet*'))
                 + sorted(Path('/tmp').glob('cmux-fleet*'))):
        if path.is_dir() and path.resolve() not in seen:
            seen.add(path.resolve())
            roots.append(path)
    configs = set()
    warnings = []
    process_rows = []
    process_result = subprocess.run(['ps', '-axww', '-o', 'pid=,ppid=,args='], capture_output=True, text=True)
    if process_result.returncode:
        warnings.append('process enumeration unavailable')
    for line in process_result.stdout.splitlines():
        row = line.strip().split(None, 2)
        if len(row) != 3:
            continue
        pid, ppid, command = row
        try:
            args = shlex.split(command)
        except ValueError:
            continue
        for i, arg in enumerate(args):
            if Path(arg).name in ('fleet.py', 'cmux-fleet') and len(args) > i+2 and args[i+1] in ('daemon', 'actor'):
                cfg = Path(args[i+2]).expanduser()
                if cfg.is_absolute() and cfg.suffix == '.json':
                    configs.add(cfg)
                    process_rows.append({'pid': int(pid), 'ppid': int(ppid), 'kind': args[i+1], 'config': str(cfg)})
            elif Path(arg).name == 'agent_bridge.py' and len(args) > i+3:
                cfg = Path(args[i+1]).expanduser()
                if cfg.is_absolute() and cfg.suffix == '.json':
                    configs.add(cfg)
                    process_rows.append({'pid': int(pid), 'ppid': int(ppid), 'kind': 'bridge',
                                         'config': str(cfg), 'slot': args[i+2]})
        if args and Path(args[0]).name == 'opencode' and '--standalone' in args:
            # opencode worker TUI (fleet plugin in-process); its bridge child is listed as kind 'bridge'
            index = args.index('--standalone')
            process_rows.append({'pid': int(pid), 'ppid': int(ppid), 'kind': 'opencode',
                                 'cwd': args[index+1] if index+1 < len(args) else ''})
        if '--patch' in args:  # a dsh worker: `dsh --profile dsh-tui --patch <run>/dsh-tui-home/<slot>/g<N>/...`
            index = args.index('--patch')
            if index+1 < len(args) and '/dsh-tui-home/' in args[index+1]:
                run = Path(args[index+1].split('/dsh-tui-home/', 1)[0])
                if run.is_absolute():
                    configs.add(run/'config.json')
                    process_rows.append({'pid': int(pid), 'ppid': int(ppid), 'kind': 'dsh', 'run': str(run)})
        if '--session-dir' in args:
            index = args.index('--session-dir')
            if index+1 < len(args) and '/pi-sessions/' in args[index+1]:
                run = Path(args[index+1].split('/pi-sessions/', 1)[0])
                if run.is_absolute():
                    configs.add(run/'config.json')
                    process_rows.append({'pid': int(pid), 'ppid': int(ppid), 'kind': 'pi', 'run': str(run)})
    for root in roots:
        configs.update(root.glob('runs/*/config.json'))
        configs.update(root.glob('*/runs/*/config.json'))
        configs.update(root.glob('config.json'))
        latest = root/'latest.json'
        if latest.is_file():
            try:
                data = json.loads(latest.read_text())
                if data.get('config'):
                    configs.add(Path(data['config']).expanduser())
                elif data.get('run'):
                    configs.add(Path(data['run'])/'config.json')
            except (OSError, ValueError, TypeError):
                warnings.append('unreadable latest pointer: ' + str(latest))
    runs = {}
    for path in sorted(configs):
        try:
            cfg = json.loads(path.read_text())
        except (OSError, ValueError):
            warnings.append('unreadable referenced config: ' + str(path))
            continue
        if not isinstance(cfg, dict) or not all(key in cfg for key in ('run', 'redis_socket', 'workers')):
            continue
        run = str(Path(cfg['run']).expanduser())
        version = cfg.get('transport_version', 1)
        processes = [p for p in process_rows if p.get('config') == str(path) or p.get('run') == run]
        messages = list(Path(run).glob('agents/*/.inbox/msg-*.md'))
        messages += list(Path(run).glob('workspace/*/.inbox/msg-*.md'))
        item = runs.setdefault(run, {'run': run, 'configs': [], 'processes': [], 'transport_version': version,
            'transport_explicit': 'transport_version' in cfg, 'mode': cfg.get('mode', 'task'),
            'supervisor_cli': cfg.get('supervisor_cli', 'codex'), 'worker_cli': cfg.get('worker_cli', 'codex'),
            'lifecycle': cfg.get('lifecycle'), 'adopt': bool(cfg.get('adopt')),
            'workers': cfg['workers'], 'roles_path': cfg.get('roles'),
            'redis_socket': cfg['redis_socket'], 'socket_exists': Path(cfg['redis_socket']).exists(),
            'cmux_state_dir': cfg.get('cmux_state_dir'), 'message_file_count': len(messages),
            'message_file_latest_mtime': max((p.stat().st_mtime for p in messages), default=None)})
        item['configs'].append(str(path))
        item['processes'].extend(p for p in processes if p not in item['processes'])
        item['observed_running'] = bool(item['processes'])
        problem = v2_cli_problem(item)
        item['v2_cli_supported'] = problem is None
        item['v2_cli_reason'] = problem or ''
    registry_paths = set((home/'.local/state/cmux-fleet').glob('*.json'))
    for root in roots:
        registry_paths.update((root/'state').glob('*.json'))
    unique = {}  # one entry per file: the legacy state dir may link to ~/.fleetmux/state
    for path in sorted(registry_paths):
        unique.setdefault(path.resolve(), path)
    registries = []
    for path in sorted(unique.values()):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and 'surfaces' in data:
            registries.append({'path': str(path), 'workspace': data.get('workspace'),
                'control_workspace': data.get('control_workspace'),
                'surface_ids': list(data.get('surfaces', {})),
                'note': 'registry alone does not establish liveness'})
    return {'host': socket.gethostname(), 'coverage': {'home': str(home),
        'searched_roots': [str(p) for p in roots], 'process_scan': process_result.returncode == 0,
        'redis_connected': False, 'message_contents_read': False},
        'instances': list(runs.values()), 'registries': registries, 'warnings': warnings}


if __name__ == '__main__':
    print(json.dumps(inventory(), ensure_ascii=False, indent=2))
