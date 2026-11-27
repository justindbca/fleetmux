"""A real fleet for the hook keepalive / unarmed-mailbox tests: fleetmux/demo.py's World (isolated HOME, private Redis,
the real daemon process, scripted Claude actors that run the real Stop / asyncRewake hook commands) on the fake
terminal server tests/fake_fleetmux.py. Not a test module itself (no test_ prefix)."""
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from fleetmux import claude_hook, demo, fleet, runtime_v2

ROOT = Path(__file__).resolve().parents[1]
SLOTS = ('supervisor', 'worker-1', 'worker-2')


def world_paths():
    found = {n: shutil.which(n) for n in ('redis-server', 'tmux', 'git')}
    missing = [n for n, v in found.items() if not v]
    if missing:
        pytest.skip('needs ' + ', '.join(missing))
    found['fleetmux'] = str(ROOT / 'tests' / 'fake_fleetmux.py')
    return found


class HookWorld:
    def __init__(self):
        self.w = demo.World(world_paths())
        self.cfg = {}

    # ------------------------------------------------------------------ life cycle
    def start(self):
        self.w.start()
        self.cfg = fleet.load(self.w.cfg_path)
        self.r = self.w.redis()
        self.bus = runtime_v2.bus_for(self.cfg, self.r)
        assert self.wait(lambda: all(self.agent(s).get('runtime_ready') == '1' for s in SLOTS), 90), \
            'the demo fleet never got READY: ' + json.dumps({s: self.agent(s).get('state') for s in SLOTS})
        assert self.wait(lambda: all(self.r.exists(self.keys(s)['armed']) for s in SLOTS), 30), 'hooks never armed'
        return self

    def close(self):
        try:
            self.r.close()
        except Exception:  # noqa: BLE001
            pass
        left = demo.cleanup(self.w)
        assert not left, left

    # ------------------------------------------------------------------ reads
    def agent(self, slot):
        return self.r.hgetall('fx:agent:' + slot)

    def workspace(self):
        return fleet.agent_cwd(self.cfg, 'worker-1').parent

    def keys(self, slot, generation=None):
        gen = int(self.agent(slot).get('generation') or 0) if generation is None else generation
        return claude_hook.keys(self.bus.prefix, slot, gen)

    def audit(self, event=None):
        rows = [row for _, row in self.r.xrange(self.bus.audit_key)]
        return [row for row in rows if event is None or row.get('event') == event]

    @staticmethod
    def wait(cond, timeout, step=0.5):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(step)
        return bool(cond())

    # ------------------------------------------------------------------ the hooks the actors started
    def hook_pids(self, slot):
        """Stop and asyncRewake hook processes of ``slot`` (children of the scripted actor this test started)."""
        out = subprocess.run(['ps', '-U', str(os.getuid()), '-o', 'pid=,command='], capture_output=True,
                             text=True).stdout
        pids = []
        for line in out.splitlines():
            pid, _, cmd = line.strip().partition(' ')
            argv = cmd.split()
            if str(self.w.cfg_path) not in argv or slot not in argv:
                continue
            if 'claude-stop' in argv or 'claude-rewake' in argv:
                pids.append(int(pid))
        return pids

    def end_hooks(self, slot):
        """Leave ``slot``'s session idle with no hook process at all (as after Claude Code killed both at their
        timeout): SIGTERM its Stop hook and its rewake fallback, then wait until the armed mark is gone."""
        pids = self.hook_pids(slot)
        assert pids, 'no hook process of ' + slot
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        assert self.wait(lambda: not self.hook_pids(slot), 15), 'hooks of ' + slot + ' did not exit'
        assert self.wait(lambda: not claude_hook.live(self.r, self.keys(slot)), claude_hook.ARMED_TTL + 5), \
            'armed / stopping mark of ' + slot + ' stayed'

    def set_config(self, **fields):
        """Change the run's config.json (read by every hook at run time)."""
        fleet.save(self.w.cfg_path, dict(fleet.load(self.w.cfg_path), **fields))


@pytest.fixture
def hook_world():
    world = HookWorld()
    try:
        yield world.start()
    finally:
        world.close()
