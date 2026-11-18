"""What the relay commands need from this machine, behind one object (the backend):

node_name()            this machine's name: the `source: self` entry of the fleet-superpowers inventory
params()               the `relay` section of the fleet config (params.Settings)
store(job)             the state remote (hubstore.HubStore)
workspace(job)         worktrees, snapshots, salvage (hubstore.GitWorkspace), beside the project clone
make_executor(job)     a launch.Executor for this machine's root fleet (only inside the relay process it starts)
start_spec(job)        (seg, record, worktree, takeover) -> the rendered dev_task spec
judge(job)             (nodes, read_ok) -> {node: online | leaving | lost} (liveness.Observer)
machine()              this machine's measured capabilities (node.json `machine`)
peer_sync(job)         nodes -> None: the peers' capabilities into this machine's inventory (R20)
head(path)             the commit checked out at `path`
observations(job)      {node: seconds since this machine saw its node record change}
hub(job)               the state remote's address, for `status`
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import fleetmux

APPLET_CONFIG = 'FLEETMUX_APPLET_CONFIG'    # the host (root) fleet's config.json, set by the daemon for its applets


class Unavailable(RuntimeError):
    """The relay cannot reach its state layer on this build or machine."""


class _NoExecutor:
    """One-shot commands never start or stop sub-fleets; `run` outside the root fleet cannot either."""

    def _refuse(self, *_):
        raise Unavailable('only the relay process started by the root fleet starts or stops sub-fleets')

    start = stop = running = _refuse


class GitBackend:
    def __init__(self, *, state_dir, settings=None, env=None, run=subprocess.run, measure=None):
        from . import params
        from .clock import Clock
        from .errors import ConfigError
        self.env = dict(os.environ if env is None else env)
        try:
            self.settings = settings if settings is not None else params.load()
        except ConfigError as exc:
            raise Unavailable('relay configuration: %s' % exc) from None
        self.state_dir = Path(state_dir)
        self.run = run
        self.clock = Clock()
        self._cache = {}
        self._machine = None
        self._measure = measure                # (disk_path, toolchains, always_on) -> machine block; None = real

    def use_hot(self, store):
        """Read the hot values from `store` (the host Redis under the root fleet) from now on."""
        from . import params
        self.settings = params.Settings(self.settings.block, store)
        self._cache.clear()                        # objects built with the old settings are rebuilt
        return self.settings

    def _memo(self, key, make):
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]

    def _search(self):
        from fleetmux import discover
        return self._memo('path', lambda: os.pathsep.join(discover.search_path()))

    # ------------------------------------------------------------------ this machine
    def node_name(self):
        from fleetmux import fleet_superpowers as fs
        try:
            name = fs.self_name()
        except fs.InventoryError as exc:
            raise Unavailable(str(exc)) from None
        if not name:
            raise Unavailable('the inventory has no `source: self` machine: run '
                              '`fleetmux superpowers refresh --name <name>` once on this machine')
        return name

    def params(self):
        return self.settings

    def machine(self):
        """Measured now (at most once per beat_s); toolchains and always_on from this machine's inventory entry."""
        from fleetmux import fleet_superpowers as fs
        from .nodestatus import measure_machine
        now = self.clock.mono()
        if self._machine is None or now - self._machine[0] >= self.settings.hot('beat_s'):
            entry = fs.machine(self.node_name()) or {}
            self.state_dir.mkdir(parents=True, exist_ok=True)
            measure = self._measure or (lambda path, toolchains, always_on: measure_machine(
                path, toolchains=toolchains, always_on=always_on, run=self.run,
                probe_timeout_s=self.settings.probe_timeout_s))
            self._machine = (now, measure(self.state_dir, entry.get('toolchains') or {}, bool(entry.get('always_on'))))
        return dict(self._machine[1])

    def head(self, path):
        return self._git().out('rev-parse', 'HEAD', cwd=Path(path))

    def score(self):
        return None                               # placement's weights (relay.hot.score)

    # ------------------------------------------------------------------ one job
    def _repo(self, job):
        path = self.state_dir / 'jobs' / (job + '.json')
        try:
            return Path(json.loads(path.read_text())['repo'])
        except (OSError, ValueError, KeyError):
            raise Unavailable('this machine has not joined job %s: run `fleetmux relay join`' % job) from None

    def _git(self):
        from .gitio import Git
        return self._memo('git', lambda: Git(shutil.which('git', path=self._search()) or 'git',
                                             lambda: self.settings.hot('git_timeout_s')))

    def _hub(self, job):
        from . import repo as cache_repo
        from .hub import Hub

        def make():
            git = self._git()
            cache = cache_repo.ensure_cache(git, self.state_dir / job / 'cache.git')
            origin = git.out('remote', 'get-url', 'origin', cwd=self._repo(job))
            return Hub(job=job, git=git, cache=cache, code_remote=origin, state_remote=self.settings.state_remote,
                       settings=self.settings, clock=self.clock)
        return self._memo(('hub', job), make)

    def store(self, job):
        from .beats import Beats
        from .hubstore import HubStore

        def make():
            hub = self._hub(job)
            node = self.node_name()
            return HubStore(hub, Beats(hub, node=node, fleet={'machine': node}, clock=self.clock,
                                       settings=self.settings))
        return self._memo(('store', job), make)

    def workspace(self, job):
        from .hubstore import GitWorkspace

        def make():
            repo = self._repo(job)
            return GitWorkspace(self._hub(job), job=job, root=repo.parent / (repo.name + '-relay'),
                                state_dir=self.state_dir, settings=self.settings, run=self.run)
        return self._memo(('ws', job), make)

    def make_executor(self, job):
        from .launch import FleetmuxExecutor
        root = self.env.get(APPLET_CONFIG)
        if not root:
            return _NoExecutor()
        fleet_py = Path(fleetmux.__file__).resolve().parent / 'fleet.py'
        return FleetmuxExecutor([sys.executable, str(fleet_py)], root_config=root, run=self.run,
                                timeout_s=self.settings.hot('git_timeout_s'))

    def start_spec(self, job):
        from .hubstore import start_spec
        return start_spec(self.workspace(job), job, relay_python=sys.executable)

    def judge(self, job):
        from .hubstore import observer_judge
        from .liveness import Observer
        return observer_judge(self._memo(('obs', job), lambda: Observer(self.settings, self.node_name(),
                                                                         self.clock, None, job=job)),
                              self.store(job))

    def peer_sync(self, job):
        from .inventory import sync_peers
        node = self.node_name()
        return lambda nodes: sync_peers(nodes, node)

    def observations(self, job):
        return {}

    def hub(self, job):
        return self._hub(job).state_remote


def default_backend(state_dir):
    return GitBackend(state_dir=state_dir)
