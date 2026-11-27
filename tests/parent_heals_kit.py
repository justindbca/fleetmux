"""Harness of tests/test_parent_heals_child.py: real fleets (``fleet.py start``, its daemon process, the v2 bus, the
Claude Stop-hook delivery, real sub-fleet daemons) with scripted agents (tests/parent_heals_actor.py on the demo
actor) on the fake terminal server (tests/fake_fleetmux.py, real PTYs, M4 pages).

Isolation is the demo's (fleetmux/demo.py ``World``): one new ``/tmp/fmxd-*`` directory holds HOME, FLEETMUX_HOME,
FLEETMUX_STATE, the Redis runtime root (every fleet of the tree gets its own Redis there), the runs and the actor
launcher; ``FLEETMUX_CONFIG`` names a fleet.yaml inside it. Clean-up is ``demo.cleanup``: the root daemon through
``stop``, every Redis under the world's own root through its socket, the world's own terminal server, then the
directory. Nothing outside the world is read or written; no running fleet is touched.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleetmux import demo  # noqa: E402

FAKE_FLEETMUX = ROOT / 'tests' / 'fake_fleetmux.py'
ACTOR = ROOT / 'tests' / 'parent_heals_actor.py'
TURN_S = 0.5


def programs():
    """redis-server / tmux / git from PATH, fleetmux = the fake terminal server (as tests/test_demo_e2e.py)."""
    os.environ.setdefault('FLEETMUX_BIN', str(FAKE_FLEETMUX))
    paths = {}
    for name in ('redis-server', 'tmux', 'git'):
        found = shutil.which(name)
        if not found:
            raise RuntimeError(name + ' is needed by this test (PATH=/opt/homebrew/opt/redis/bin:$PATH)')
        paths[name] = found
    paths['fleetmux'] = str(FAKE_FLEETMUX)
    return paths


class World(demo.World):
    def __init__(self, fleet_yaml=None):
        super().__init__(programs(), TURN_S)
        self.claude.write_text('#!/bin/sh\n# parent_heals test actor: scripted, no model is called\n'
                               'exec ' + shlex.quote(sys.executable) + ' -I ' + shlex.quote(str(ACTOR)) + ' "$@"\n')
        self.cues_path = self.root / 'cues.json'
        self.cues_path.write_text('[]')
        self.fleet_yaml = self.root / 'fleet.yaml'
        self.fleet_yaml.write_text(yaml.safe_dump(fleet_yaml or {}))
        self.extra_env = {}

    def env(self):
        env = super().env()
        env.update(PHK_CUES=str(self.cues_path), FLEETMUX_CONFIG=str(self.fleet_yaml), **self.extra_env)
        return env

    def cue(self, **cue):
        rows = json.loads(self.cues_path.read_text())
        rows.append(dict({'id': 'c' + str(len(rows)), 'times': 1}, **cue))
        self.cues_path.write_text(json.dumps(rows))

    def start(self, mode='task', workers=2, extra=(), lifecycle=None):
        args = ['start', '--allow-development-v2', '--mode', mode, '--backend', 'fleetmux', '--no-view', '--no-join',
                '--supervisor-cli', 'claude', '--worker-cli', 'claude',
                '--claude', str(self.claude), '--redis-server', self.paths['redis-server'],
                '--redis-runtime-dir', str(self.redis_root), '--fleet-home', str(self.fh),
                '--cmux-state-dir', str(self.state), '--runs-dir', str(self.runs)]
        if '--roster' not in extra:
            args += ['--workers', str(workers)]
        args += ['--lifecycle', lifecycle or ('service' if mode == 'service' else 'smoke')]
        done = self.fleet(*(args + list(extra)), timeout=180)
        latest = self.fh / 'latest.json'
        if done.returncode or not latest.is_file():
            raise RuntimeError('start failed (exit ' + str(done.returncode) + '):\n' + (done.stdout + done.stderr)[-3000:])
        self.cfg = json.loads(latest.read_text())
        self.cfg_path = Path(self.cfg['config'])
        return done

    def signal(self, **sig):
        """A signal for running actors (parent_heals_actor: ``rewall``), applied once by each pane it matches."""
        path = Path(str(self.cues_path) + '.signals')
        rows = json.loads(path.read_text()) if path.exists() else []
        rows.append(dict({'id': 's' + str(len(rows))}, **sig))
        path.write_text(json.dumps(rows))

    def task(self, spec):
        done = self.fleet('task', '--config', str(self.cfg_path), '-', stdin=json.dumps(spec))
        if done.returncode:
            raise RuntimeError('task failed (exit ' + str(done.returncode) + '):\n' + (done.stdout + done.stderr)[-3000:])
        try:
            return json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {'raw': done.stdout}

    def request(self, spec):
        done = self.fleet('request', '--config', str(self.cfg_path), '-', stdin=json.dumps(spec))
        return done

    def resume(self, cfg_path=None):
        return self.fleet('resume', '--config', str(cfg_path or self.cfg_path))

    # -- what happened
    def turns(self, cwd_part=None, kind='turn.start'):
        """Actor log rows (every turn an agent got), optionally of panes whose cwd contains ``cwd_part``."""
        out = []
        try:
            lines = self.actor_log.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            return out
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get('event') == kind and (cwd_part is None or cwd_part in str(row.get('cwd'))):
                out.append(row)
        return out

    def received(self, cwd_part, needle):
        return [r['text'] for r in self.turns(cwd_part) if needle in (r.get('text') or '')]

    def wait_text(self, cwd_part, needle, timeout):
        """Texts some agent whose cwd contains ``cwd_part`` got with ``needle`` in them (waits up to ``timeout``)."""
        return wait(lambda: self.received(cwd_part, needle), timeout)

    def supervisor_dir(self, cfg=None):
        return str(Path((cfg or self.cfg)['run']) / 'workspace' / 'supervisor')

    def events(self, cfg=None, name=None):
        """Audit rows of a fleet (the root by default), read from its own Redis stream."""
        from fleetmux import fleet, runtime_v2
        cfg = cfg or self.cfg
        try:
            r = fleet.connect(cfg, socket_timeout=5)
        except Exception:  # noqa: BLE001  (that fleet's Redis is gone)
            return []
        try:
            bus = runtime_v2.bus_for(cfg, r)
            rows = [row for _, row in r.xrange(bus.audit_key)]
        except Exception:  # noqa: BLE001
            rows = []
        finally:
            r.close()
            r.connection_pool.disconnect()
        return [x for x in rows if name is None or x.get('event') == name]

    def agent(self, slot, cfg=None):
        from fleetmux import fleet
        cfg = cfg or self.cfg
        r = fleet.connect(cfg, socket_timeout=5)
        try:
            return r.hgetall(fleet.key('agent', slot))
        finally:
            r.close()
            r.connection_pool.disconnect()

    def service(self, cfg=None):
        from fleetmux import fleet
        r = fleet.connect(cfg or self.cfg, socket_timeout=5)
        try:
            return r.hgetall('fx:service')
        finally:
            r.close()
            r.connection_pool.disconnect()

    def children(self):
        """Config dicts of this root's sub-fleets (run dirs under <root run>/sub)."""
        out = []
        for path in sorted((Path(self.cfg['run']) / 'sub').glob('*/config.json')):
            try:
                out.append(dict(json.loads(path.read_text()), config=str(path)))
            except (OSError, ValueError):
                continue
        return out

    def close(self):
        return demo.cleanup(self)


def wait(pred, timeout, every=0.5, what=''):
    """Poll ``pred`` until truthy; its last value either way (the test's assert says what is missing)."""
    deadline = time.time() + timeout
    value = pred()
    while not value and time.time() < deadline:
        time.sleep(every)
        value = pred()
    return value


def roster(path, workers, subfleets=None):
    data = {'workers': [{'name': w} for w in workers]}
    if subfleets:
        data['subfleets'] = subfleets
    Path(path).write_text(yaml.safe_dump(data))
    return path


def run_cmd(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, **kw)
