"""Process / socket ledger of one m4e world (e2e-1 plan r3 §6) and the control-socket reads of the E2 (§6.5).

Read-only by default: ``snapshot`` / ``refresh`` / ``residue`` only look (``ps``, the runtime files, ``find -type
s``). The only signalling code is ``Ledger.signal``: it signals a pid only after three checks against this
world (command line names the world dir, OS start time equals the recorded one, and - when ``ps -E`` shows the
environment - the run's M4E_RUN marker). There is no ``fleetmux`` cleanup command and no pattern kill here.
"""
import json
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

PS_ROW = re.compile(r'\s*(\d+)\s+(\d+)\s+(\d+)\s+(\w{3} \w{3}\s+\d+ [\d:]{8} \d{4})\s+(.*)')


def ps():
    """{pid: {pid, ppid, pgid, start, command}} of every process (``ps -axww``; ``lstart`` = start time)."""
    done = subprocess.run(['ps', '-axww', '-o', 'pid=,ppid=,pgid=,lstart=,command='], capture_output=True,
                          text=True, timeout=30, env=dict(os.environ, LC_ALL='C', LANG='C'))
    out = {}
    for line in done.stdout.splitlines():
        m = PS_ROW.match(line)
        if m:
            pid = int(m.group(1))
            out[pid] = {'pid': pid, 'ppid': int(m.group(2)), 'pgid': int(m.group(3)), 'start': m.group(4),
                        'command': m.group(5)}
    return out


def marked(marker):
    """pids whose environment carries ``M4E_RUN=<marker>`` (macOS ``ps -E``; Linux /proc/*/environ)."""
    found = set()
    if Path('/proc').is_dir():
        for env in Path('/proc').glob('[0-9]*/environ'):
            try:
                if ('M4E_RUN=' + marker).encode() in env.read_bytes().split(b'\0'):
                    found.add(int(env.parent.name))
            except OSError:
                pass
        return found
    done = subprocess.run(['ps', '-axwwE', '-o', 'pid=,command='], capture_output=True, text=True, timeout=30,
                          env=dict(os.environ, LC_ALL='C', LANG='C'))
    for line in done.stdout.splitlines():
        if ' M4E_RUN=' + marker in line or line.rstrip().endswith('M4E_RUN=' + marker):
            try:
                found.add(int(line.split(None, 1)[0]))
            except (ValueError, IndexError):
                pass
    return found


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def sockets(root):
    done = subprocess.run(['find', str(root), '-type', 's'], capture_output=True, text=True, timeout=60)
    return sorted(line for line in done.stdout.splitlines() if line)


class Ledger:
    """Every process the world's tree started, by pid + OS start time, with where it was learned."""

    def __init__(self, world):
        self.world = world
        self.entries = {}  # pid -> {pid, start, command, sources: [...]}

    def add(self, row, source):
        if row is None:
            return
        entry = self.entries.get(row['pid'])
        if entry and entry['start'] != row['start']:  # a reused pid is a different process
            entry = None
        if entry is None:
            entry = self.entries[row['pid']] = {'pid': row['pid'], 'start': row['start'], 'command': row['command'],
                                                'pgid': row['pgid'], 'sources': []}
        if source not in entry['sources']:
            entry['sources'].append(source)

    def refresh(self):
        """Learn every process of the world now visible: runtime files, pane groups, Redis, tmux, marker, argv."""
        procs = ps()
        w = self.world
        for server in w.s.glob('fleetmux/*/server.json'):
            pid = (read_json(server) or {}).get('pid')
            self.add(procs.get(int(pid)) if pid else None, 'server.json ' + str(server))
        groups = set()
        for panes in w.s.glob('fleetmux/*/panes.json'):
            data = read_json(panes) or {}
            rows = data.get('panes') if isinstance(data, dict) else data
            for row in rows or []:
                if isinstance(row, dict) and row.get('pid'):
                    self.add(procs.get(int(row['pid'])), 'panes.json ' + str(row.get('pane')))
                    if row.get('pgid'):
                        groups.add(int(row['pgid']))
        for p in procs.values():
            if p['pgid'] in groups:
                self.add(p, 'pane group %d' % p['pgid'])
        for pidfile in w.r.rglob('redis.pid'):
            try:
                self.add(procs.get(int(pidfile.read_text().strip())), 'redis.pid ' + str(pidfile))
            except (OSError, ValueError):
                pass
        for sock in w.runs.rglob('tmux.sock'):
            done = subprocess.run(['tmux', '-S', str(sock), 'display-message', '-p', '#{pid}'], capture_output=True,
                                  text=True, timeout=10)
            if done.returncode == 0 and done.stdout.strip().isdigit():
                tmux_pid = int(done.stdout.strip())
                self.add(procs.get(tmux_pid), 'tmux server ' + str(sock))
                for p in procs.values():
                    if p['ppid'] == tmux_pid:
                        self.add(p, 'tmux pane of ' + str(sock))
        for pid in marked(w.marker):
            self.add(procs.get(pid), 'M4E_RUN marker')
        for p in procs.values():
            if str(w.dir) in p['command']:
                self.add(p, 'argv names the world dir')
        # descendants of anything known (hooks, notify callbacks, caffeinate children)
        known = set(self.entries)
        grew = True
        while grew:
            grew = False
            for p in procs.values():
                if p['ppid'] in known and p['pid'] not in known:
                    self.add(p, 'child of %d' % p['ppid'])
                    known.add(p['pid'])
                    grew = True
        return procs

    def alive(self, procs=None):
        procs = ps() if procs is None else procs
        return [e for e in self.entries.values() if e['pid'] in procs and procs[e['pid']]['start'] == e['start']]

    def residue(self):
        """What is left of the world (plan §6.4): live ledger pids, marked or world-naming processes, sockets."""
        procs = self.refresh()
        w = self.world
        left = {'pids': self.alive(procs), 'marked': sorted(marked(w.marker)),
                'argv': sorted(p['pid'] for p in procs.values() if str(w.dir) in p['command']
                               and p['pid'] != os.getpid()),
                'sockets': sockets(w.dir)}
        left['clean'] = not (left['pids'] or left['marked'] or left['argv'] or left['sockets'])
        return left

    def wait_clean(self, limit=60.0):
        deadline = time.monotonic() + limit
        while True:
            left = self.residue()
            if left['clean'] or time.monotonic() >= deadline:
                return left
            time.sleep(1)

    def verified(self, pid):
        """(ok, why): pid is this world's process, by command line, start time and (when visible) marker."""
        procs = ps()
        entry, row = self.entries.get(pid), procs.get(pid)
        if entry is None:
            return False, 'pid %d is not in the ledger' % pid
        if row is None or row['start'] != entry['start']:
            return False, 'pid %d is gone or reused (start %r, ledger %r)' % (pid, row and row['start'], entry['start'])
        if str(self.world.dir) not in row['command']:
            return False, 'pid %d command does not name the world dir: %s' % (pid, row['command'][:200])
        env = subprocess.run(['ps', '-wwE', '-o', 'command=', '-p', str(pid)], capture_output=True, text=True,
                             timeout=10).stdout
        if 'M4E_RUN=' in env and 'M4E_RUN=' + self.world.marker not in env:
            return False, 'pid %d carries another M4E_RUN marker' % pid
        return True, ''

    def signal(self, pid, sig, why):
        """Fault injection / last-resort stop of ONE verified pid of this world; raises if not verified."""
        ok, reason = self.verified(pid)
        if not ok:
            raise AssertionError('refusing to signal: ' + reason)
        os.kill(pid, sig)
        return {'pid': pid, 'sig': int(sig), 'why': why, 'at': time.time(), 'command': self.entries[pid]['command']}

    def fallback(self):
        """After a failed assertion only: SIGTERM, then SIGKILL after 5 s, each verified; the list of what was hit."""
        hit = []
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for e in self.alive():
                try:
                    hit.append(self.signal(e['pid'], sig, 'fallback after a failed assertion'))
                except (AssertionError, ProcessLookupError) as exc:
                    hit.append({'pid': e['pid'], 'skipped': str(exc)})
            if sig == signal.SIGTERM and hit:
                time.sleep(5)
        return hit

    def dump(self):
        return sorted(self.entries.values(), key=lambda e: e['pid'])


# ----------------------------------------------------------------------------------------------- control socket
def ctl(sock_path, *requests, timeout=10):
    """Requests on ONE control connection (a page binding lives on its connection); the replies, in order."""
    out = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(sock_path))
        stream = sock.makefile('rwb')
        for i, req in enumerate(requests, 1):
            stream.write((json.dumps(dict({'id': i}, **req)) + '\n').encode())
            stream.flush()
            line = stream.readline()
            if not line:
                break
            out.append(json.loads(line))
    return out


def daemon_of(procs, cfg_path):
    """The `fleet.py daemon <cfg>` process (with or without --resume) of one config, or None."""
    for p in procs.values():
        parts = p['command'].split()
        if len(parts) >= 4 and parts[1].endswith('fleetmux/fleet.py') and parts[2] == 'daemon' \
                and parts[3] == str(cfg_path) and parts[4:] in ([], ['--resume']):
            return p
    return None
