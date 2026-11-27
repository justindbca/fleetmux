"""Test kit for the release switch (fleetmux/release_drift.py) and its manual entry `fleetmux upgrade`
(fleetmux/upgrade.py): fake release directories, a synthetic fleet tree on disk and a fake machine (``FakeOps``, the
``release_drift.Ops`` interface) that records every signal, command and shutdown the switch asks for.

Nothing here starts a daemon, signals a real process or touches a running fleet: the tree lives in ``tmp_path`` and
``fleet.daemon_running`` is answered from ``FakeOps.daemon_running``.
"""
import json
import signal
from pathlib import Path

from fleetmux import fleet, release_drift, upgrade


def make_release(root, name):
    """A release directory with the files the switch checks: .venv/bin/python3.14, fleetmux/fleet.py, bin/fleetmux."""
    rel = Path(root) / 'fleetmux-releases' / name
    for rel_path in ('.venv/bin/python3.14', 'fleetmux/fleet.py', 'bin/fleetmux'):
        p = rel / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('#!/bin/sh\n')
        p.chmod(0o755)
    return rel


def py(rel):
    return str(Path(rel) / '.venv' / 'bin' / 'python3.14')


class Tree:
    """A root service run and its sub-fleets on disk. ``nodes`` maps a short key ('root', 'M1', 'M1.1', 'N' ...) to its
    run directory."""

    def __init__(self, tmp, old_rel, release_file=True, mode='service', backend='fleetmux', start_sh=False):
        self.home = Path(tmp) / 'home'
        self.run = self.home / 'runs' / 'fleet-20260101-000000-abcd1234'
        self.run.mkdir(parents=True)
        self.old = old_rel
        self.cfg = {'run': str(self.run), 'fleet_home': str(self.home), 'python': py(old_rel), 'mode': mode,
                    'transport_version': 2, 'backend': backend, 'redis_socket': str(Path(tmp) / 'r' / 'root.sock'),
                    'cmux_state_dir': str(self.home / 'state')}
        fleet.save(self.run / 'config.json', self.cfg)
        if release_file:
            (self.run / 'release').write_text(Path(old_rel).name + '\n')
        if start_sh:
            (self.home / 'start.sh').write_text('#!/bin/sh\n')
        self.nodes = {'root': self.run}

    @property
    def cfg_path(self):
        return self.run / 'config.json'

    def child(self, key, parent='root', **rec):
        d = self.nodes[parent] / 'sub' / key
        d.mkdir(parents=True)
        cfg = dict(self.cfg, run=str(d), parent_config=str(self.nodes[parent] / 'config.json'),
                   redis_socket=str(Path(self.cfg['redis_socket']).parent / (key + '.sock')), task_id=key)
        cfg.pop('mode')
        fleet.save(d / 'config.json', cfg)
        fleet.save(d / 'subfleet.json', dict({'task_id': key, 'state': 'running', 'slot': 'worker-1',
                                              'created': float(len(self.nodes))}, **rec))
        (d / 'release').write_text(Path(self.old).name + '\n')
        self.nodes[key] = d
        return d

    def sock(self, d):
        return fleet.load(Path(d) / 'config.json')['redis_socket']


class FakeOps(upgrade.Ops):
    """The machine as the switch sees it, recorded. ``alive_runs``: run dirs whose daemon holds its lock; ``pid_of``:
    one fake pid per alive run; ``argv_of``: the command line of each pid (the old release until a fake start)."""

    tree_window = True  # the start of a release opens the window again (when there was one)

    def __init__(self, tree, to_rel, jobs=None, window=False):
        self.tree, self.to = tree, Path(to_rel)
        self.alive_runs = {str(d) for d in tree.nodes.values()}
        self.pid_of, self.argv_of, self.next_pid = {}, {}, 1000
        self.launcher_of = {}       # pid -> __PYVENV_LAUNCHER__ of its start environment (macOS venv)
        for d in tree.nodes.values():
            self.new_daemon(d, tree.old)
        self.events = []
        self.jobs_source = dict(jobs or {})
        self.read_calls = 0
        self.applet_pids = {}       # name -> (pid, argv0): the root run's applets
        self.redis = {}             # socket -> up
        self.redis_lag = {}         # socket -> polls a Redis still answers after its shutdown
        self.window_pid = 4242 if window else None
        self.on_screen = {4243} if window else set()
        self.server = (3000, tree.old)
        self.supervisor = 'idle'
        self.fail = {}              # 'kill' / 'redis_down' / 'terminal_down' / 'start' -> exception raised once
        self.start_code = 0
        self.start_codes = {}       # release name -> exit code of its start (else start_code)
        self.t = 0.0
        self.started = []           # target release names, in start order
        self.up_requests = []
        self.on_start = []          # callables(target) run right after a fake start (tests break things there)

    # ---- clock
    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s

    # ---- processes
    def new_daemon(self, d, rel):
        self.next_pid += 1
        self.pid_of[str(d)] = self.next_pid
        self.argv_of[self.next_pid] = [py(rel), str(Path(rel) / 'fleetmux' / 'fleet.py'), 'daemon',
                                       str(Path(d) / 'config.json')]

    def runtime(self, cfg):
        return str(Path(cfg.get('cmux_state_dir') or cfg['run']) / 'fleetmux' / 'rt')

    def _procs(self):
        rt = self.runtime(self.tree.cfg)
        out = {}
        for run in self.alive_runs:
            pid = self.pid_of.get(run)
            if pid:
                out[pid] = self.argv_of[pid]
        for pid, argv0 in self.applet_pids.values():
            if pid:
                out[pid] = [argv0, 'applet.py']
        if self.window_pid:
            out[self.window_pid] = [str(Path(self.tree.old) / 'bin' / 'fleetmux'), 'gui', '--runtime', rt]
        if self.server:
            pid, rel = self.server
            out[pid] = [str(Path(rel) / 'bin' / 'fleetmux'), 'server', '--runtime', rt, '--daemonize']
        return out

    def ps(self):
        return sorted((pid, ' '.join(argv)) for pid, argv in self._procs().items())

    def cmdline(self, pid):
        return self._procs().get(pid)

    def launcher(self, pid):
        return self.launcher_of.get(pid) if pid in self._procs() else None

    def alive(self, pid):
        return pid in self._procs()

    def daemon_running(self, run):
        return str(run) in self.alive_runs

    def kill(self, pid, sig):
        run = next((r for r, p in self.pid_of.items() if p == pid and r in self.alive_runs), None)
        if run:
            self.events.append(('kill', run, sig))
            self.check_fail('kill')
            self.alive_runs.discard(run)
            return
        if pid == self.window_pid or (self.server and pid == self.server[0]):
            self.events.append(('terminal_down', self.runtime(self.tree.cfg),
                                'gui' if pid == self.window_pid else 'server'))
            self.check_fail('terminal_down')
            if pid == self.window_pid:
                self.window_pid, self.on_screen = None, set()
            else:
                self.server = None

    def self_detach(self):
        self.events.append(('detach',))

    # ---- applets
    def applets(self, run):
        if str(run) != str(self.tree.run):
            return {}
        return {n: p for n, (p, _) in self.applet_pids.items() if p}

    def stop_applet(self, run, name):
        self.events.append(('stop_applet', name))
        pid = self.applet_pids[name][0]
        self.applet_pids[name] = (None, self.applet_pids[name][1])
        return pid

    # ---- redis / jobs
    def redis_answers(self, sock):
        if sock in self.redis_lag and self.redis_lag[sock] > 0 and not self.redis.get(sock, True):
            self.redis_lag[sock] -= 1
            return True
        return self.redis.get(sock, True)

    def redis_get(self, sock, key):
        assert key == 'fx:jobs', key
        if not self.redis.get(sock, True):
            return None
        if sock == self.tree.cfg['redis_socket']:
            self.read_calls += 1
            self.events.append(('read_jobs',))
            return json.dumps(self.jobs_source)
        return ''

    def redis_hget(self, sock, key, field):
        if not self.redis.get(sock, True):
            return None
        if sock == self.tree.cfg['redis_socket'] and key == 'fx:agent:supervisor' and field == 'state':
            return self.supervisor
        return None

    def redis_shutdown(self, sock):
        run = next((str(d) for d in self.tree.nodes.values() if self.tree.sock(d) == sock), sock)
        self.events.append(('redis_down', run))
        self.check_fail('redis_down')
        self.redis[sock] = False

    # ---- windows
    def windows(self):
        return set(self.on_screen)

    def descendants(self, pid):
        return {pid, pid + 1}

    # ---- commands
    def start(self, argv, env, log_path, timeout):
        self.events.append(('run', list(argv), dict(env or {}), None))
        self.check_fail('start')
        if Path(argv[0]).name == release_drift.START:
            target = Path(env['FLEETMUX_REL'])
        else:
            target = Path(argv[0]).parents[2]  # <release>/.venv/bin/python
        code = self.start_codes.get(target.name, self.start_code)
        if code:
            return code
        self.configs_at_start = {k: fleet.load(Path(d) / 'config.json').get('python')
                                 for k, d in self.tree.nodes.items()}
        self.fake_start(target)
        return 0

    def run_input(self, argv, text, env, timeout):
        self.events.append(('run', list(argv), dict(env or {}), text))
        if 'request' in argv:
            spec = json.loads(text)
            self.up_requests.append(spec)
            target = Path(argv[0]).parents[2]
            d = next(d for d in self.tree.nodes.values() if json.loads((d / 'subfleet.json').read_text()
                                                                         if (d / 'subfleet.json').exists() else '{}')
                     .get('name') == spec['name'])
            self.revive(d, target)
            return 0, json.dumps({'status': 'accepted'})
        return 0, ''

    def revive(self, d, target=None):
        target = Path(target or self.to)
        self.alive_runs.add(str(d))
        self.new_daemon(d, target)
        self.redis[self.tree.sock(d)] = True
        (Path(d) / 'release').write_text(target.name + '\n')
        cfg = fleet.load(Path(d) / 'config.json')
        cfg['python'] = py(target)
        fleet.save(Path(d) / 'config.json', cfg)

    def fake_start(self, target=None):
        """What `start --resume` of release ``target`` does in this tree: the root and every sub-fleet that comes back
        on its own (all but a named sub-fleet without autostart) run it, configs switched."""
        target = Path(target or self.to)
        self.started.append(target.name)
        mirror = self.tree.run / 'jobs.json'
        if mirror.exists():  # section 58: the resumed root takes its job table from <run>/jobs.json
            self.jobs_source = json.loads(mirror.read_text())
        for key, d in self.tree.nodes.items():
            rec = json.loads((d / 'subfleet.json').read_text()) if key != 'root' else {}
            if rec.get('named') and not rec.get('autostart'):
                continue
            if rec.get('state') in ('returned', 'failed', 'cancelled'):
                continue
            self.revive(d, target)
        for name in list(self.applet_pids):
            self.next_pid += 1
            self.applet_pids[name] = (self.next_pid, py(target))
        self.server = (3001 + len(self.started), target)
        if self.window_pid is None and self.tree_window:
            self.window_pid = 5000 + len(self.started)
            self.on_screen = {self.window_pid + 1}
        for hook in self.on_start:
            hook(target)

    def check_fail(self, what):
        exc = self.fail.pop(what, None)
        if exc:
            raise exc


def kills(ops):
    return [(Path(run).name, sig) for kind, *rest in ops.events if kind == 'kill' for run, sig in [rest[:2]]]


def last_json(lines):
    return json.loads(lines[-1])


def run_upgrade(tree, ops, to_rel, **kw):
    lines = []
    code = upgrade.main(['--to', str(to_rel), '--config', str(tree.cfg_path)] + kw.pop('extra', []), ops=ops,
                        out=lines.append, **kw)
    return code, lines


def auto_plan(tree, ops, new, verify_s=30, stop_wait_s=30):
    """The plan the root daemon's release drift writes (release_drift.launch) for this tree: start.sh, the old and the
    new release, the live sub-fleets, the fleetmux server runtime, a backup directory outside the run."""
    run = tree.run
    subs = [str(d) for d in release_drift.sub_runs(run) if ops.daemon_running(d)]
    plan = {'run': str(run), 'config': str(run / 'config.json'), 'fleet_home': str(tree.home),
            'start': str(tree.home / release_drift.START), 'name': 'demo',
            'old': {'release': Path(tree.old).name, 'dir': str(tree.old)},
            'new': {'release': Path(new).name, 'dir': str(new)}, 'replace': [], 'subs': subs, 'applets': [],
            'server_runtime': ops.runtime(tree.cfg), 'stop_wait_s': stop_wait_s, 'verify_s': verify_s,
            'max_failures': 2, 'pythonpath': '', 'at': 1.0,
            'backup_dir': str(tree.home / 'release-drift' / '20260101T000000Z')}
    (run / release_drift.PLAN).write_text(json.dumps(plan))
    return plan


SIGTERM = signal.SIGTERM
