"""Shared helpers for the relay tests (no tests here).

The hub (the state and code remote) is one local bare repo; each machine is one temporary directory with its own
project clone, cache repo, state dir and fake clock. No network, no ssh, no Redis, no real ~/.fleetmux."""
from __future__ import annotations

import copy
import hashlib
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from fleetmux import fleet_config  # noqa: E402


def shipped_block() -> dict:
    return copy.deepcopy(yaml.safe_load(fleet_config.SHIPPED.read_text())['relay'])


def block(**hot) -> dict:
    """The shipped relay block with `hot` keys replaced."""
    b = shipped_block()
    b['hot'].update(copy.deepcopy(hot))
    return b


def settings(**hot):
    from fleetmux.relay import params
    return params.load(block(**hot))


# ----------------------------------------------------------------------------- git helpers

def git(*args, cwd=None, env=None, check=True) -> str:
    full = dict(os.environ)
    full.update({'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com', 'GIT_COMMITTER_NAME': 't',
                 'GIT_COMMITTER_EMAIL': 't@example.com', 'GIT_CONFIG_NOSYSTEM': '1', 'LC_ALL': 'C'})
    if env:
        full.update(env)
    proc = subprocess.run(['git', *args], cwd=cwd, env=full, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f'git {args}: {proc.stderr}')
    return proc.stdout.strip()


def write_files(root: Path, files: dict) -> None:
    """files: relative path -> str | bytes | ("exec", content) | ("link", target) | None (delete)."""
    for rel, content in files.items():
        p = Path(root) / rel
        if content is None:
            if p.is_symlink() or p.exists():
                p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_symlink():
            p.unlink()
        if isinstance(content, tuple) and content[0] == 'link':
            os.symlink(content[1], p)
            continue
        mode_exec = isinstance(content, tuple) and content[0] == 'exec'
        data = content[1] if mode_exec else content
        if isinstance(data, str):
            data = data.encode()
        p.write_bytes(data)
        if mode_exec:
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def bare(path: Path) -> Path:
    path = Path(path)
    git('init', '-q', '--bare', '-b', 'main', str(path))
    return path


def plain_repo(path: Path, files: dict | None = None, *, commit: bool = True) -> Path:
    """A non-bare repo with `files` committed on main (or left untracked)."""
    path = Path(path)
    git('init', '-q', '-b', 'main', str(path))
    git('config', 'user.name', 't', cwd=path)
    git('config', 'user.email', 't@example.com', cwd=path)
    git('config', 'core.autocrlf', 'false', cwd=path)
    if files:
        write_files(path, files)
        if commit:
            git('add', '-A', cwd=path)
            git('commit', '-q', '-m', 'init', cwd=path)
    return path


def tree_digest(root: Path, paths=None) -> dict:
    """rel path -> (kind, sha256) for every file under root (skipping any `.git`), or only `paths`.
    kind: 'file', 'exec' or 'link' (sha of the link target)."""
    root = Path(root)
    out = {}
    if paths is None:
        paths = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != '.git']
            for f in filenames:
                if f != '.git':
                    paths.append(os.path.relpath(os.path.join(dirpath, f), root))
            for d in dirnames:
                if os.path.islink(os.path.join(dirpath, d)):
                    paths.append(os.path.relpath(os.path.join(dirpath, d), root))
    for rel in sorted(paths):
        p = root / rel
        if p.is_symlink():
            out[rel] = ('link', hashlib.sha256(os.readlink(p).encode()).hexdigest())
        else:
            kind = 'exec' if os.access(p, os.X_OK) else 'file'
            out[rel] = (kind, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def repo_fingerprint(worktree: Path) -> dict:
    """Everything a snapshot must not change: index bytes + mtime, HEAD, every ref, every object file, every file
    in the work tree (ignored ones too), and the git dir's file listing."""
    gd = Path(git('rev-parse', '--path-format=absolute', '--git-dir', cwd=worktree))
    common = Path(git('rev-parse', '--path-format=absolute', '--git-common-dir', cwd=worktree))
    index = gd / 'index'
    objects = sorted(str(p.relative_to(common / 'objects')) for p in (common / 'objects').rglob('*') if p.is_file())
    return {'index': hashlib.sha256(index.read_bytes()).hexdigest() if index.exists() else None,
            'index_mtime': index.stat().st_mtime_ns if index.exists() else None,
            'head_sym': git('symbolic-ref', '-q', 'HEAD', cwd=worktree, check=False),
            'head': git('rev-parse', '-q', '--verify', 'HEAD', cwd=worktree, check=False),
            'refs': git('for-each-ref', '--format=%(refname) %(objectname)', cwd=worktree),
            'objects': objects,
            'gitdir_files': sorted(str(p.relative_to(common)) for p in common.rglob('*') if p.is_file()),
            'files': tree_digest(worktree)}


def checkout_commit(repo_with_objects: Path, commit: str, dest: Path) -> Path:
    """Write `commit` (objects reachable from `repo_with_objects`) into the plain directory `dest`."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    index = dest.parent / (dest.name + '.tmpindex')
    env = {'GIT_INDEX_FILE': str(index)}
    git(f'--git-dir={repo_with_objects}', f'--work-tree={dest}', '-c', 'core.bare=false', 'read-tree', commit, env=env)
    git(f'--git-dir={repo_with_objects}', f'--work-tree={dest}', '-c', 'core.bare=false', 'checkout-index', '-a',
        '-f', env=env)
    index.unlink()
    return dest


def remote_refs(bare_repo: Path) -> dict:
    out = git('for-each-ref', '--format=%(refname) %(objectname)', cwd=bare_repo)
    return dict(line.split(' ', 1) for line in out.splitlines() if line)


def install_barrier(bare_repo: Path, n: int, tmp: Path) -> Path:
    """A pre-receive hook that holds each push until `n` pushes are waiting, so they reach the ref lock together."""
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    hook = Path(bare_repo) / 'hooks' / 'pre-receive'
    hook.write_text(f"""#!/bin/sh
d="{tmp}"
touch "$d/arrived.$$"
i=0
while [ "$(ls "$d" | grep -c '^arrived')" -lt {n} ] && [ $i -lt 200 ]; do sleep 0.05; i=$((i+1)); done
exit 0
""")
    hook.chmod(0o755)
    return hook


def machine_info(cores=10, mem_gb=32, disk_free_gb=100.0, *, perf_cores=None, always_on=True,
                 toolchains=None) -> dict:
    return {'cores': cores, 'perf_cores': perf_cores if perf_cores is not None else cores, 'mem_gb': mem_gb,
            'disk_free_gb': disk_free_gb, 'always_on': always_on,
            'toolchains': dict(toolchains if toolchains is not None else {'python': '3.14.0'})}


def toolchain_filter(c, task: dict):
    """A stand-in for flow-1's placement filter (candidate_filter hook): a need's tool name must be among the
    machine's toolchains; versions are ignored here."""
    have = set(((c.machine or {}).get('toolchains') or {}))
    missing = [n for n in task.get('needs') or []
               if re.split(r'[<>=]', str(n), maxsplit=1)[0].strip().lower() not in have]
    return 'toolchain:' + ','.join(missing) if missing else None


# ----------------------------------------------------------------------------- fixtures (import into a test module)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_git(tmp_path_factory, monkeypatch):
    """Every git process in a relay test sees only this global config (no user aliases, filters, signing, hooks)."""
    cfg = tmp_path_factory.mktemp('gitcfg') / 'config'
    cfg.write_text('[user]\n\tname = Test\n\temail = test@example.invalid\n'
                   '[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n'
                   '[core]\n\tautocrlf = false\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(cfg))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    for var in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
                'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        monkeypatch.delenv(var, raising=False)


def mk_git(timeout=20):
    from fleetmux.relay.gitio import Git
    return Git('git', lambda: timeout)


@pytest.fixture
def worker_repo(tmp_path):
    """Factory: make(name, files=None, linked=False, commit=True) -> work tree path. The first commit holds
    `.gitignore` (`*.ignored`, `build/`) and `README.md` plus `files`; linked=True returns a `git worktree add`
    tree on branch `wt-<name>`; commit=False leaves HEAD unborn (files stay untracked)."""
    def make(name: str, files: dict | None = None, linked: bool = False, commit: bool = True) -> Path:
        main = tmp_path / 'workers' / name
        main.mkdir(parents=True)
        git('init', '-q', str(main))
        if commit:
            write_files(main, {'.gitignore': '*.ignored\nbuild/\n', 'README.md': f'# {name}\n', **(files or {})})
            git('add', '-A', cwd=main)
            git('commit', '-q', '-m', 'first', cwd=main)
        elif files:
            write_files(main, files)
        if not linked:
            return main
        wt = tmp_path / 'workers' / f'{name}-linked'
        git('worktree', 'add', '-q', '-b', f'wt-{name}', str(wt), cwd=main)
        return wt
    return make


def mk_cache(path: Path) -> Path:
    path = Path(path)
    if not path.exists():
        git('init', '--bare', '-q', str(path))
    return path


# ----------------------------------------------------------------------------- machines on one hub

class Machine:
    """One machine = one temporary directory: a project clone, a cache repo, a state dir and its own fake clock."""

    def __init__(self, name: str, root: Path, hub_path: Path, *, job: str = 'j', state_remote: str = '',
                 hot: dict | None = None):
        from fleetmux.relay import repo
        from fleetmux.relay.clock import FakeClock
        from fleetmux.relay.gitio import Git
        from fleetmux.relay.hub import Hub
        self.name, self.root, self.job = name, Path(root), job
        self.root.mkdir(parents=True, exist_ok=True)
        self.clone = self.root / 'clone'
        git('clone', '-q', str(hub_path), str(self.clone))
        self.state_dir = self.root / 'state'
        self.worktrees = self.root / 'work'
        self.tmp = self.state_dir / 'tmp'
        self.settings = settings(**(hot or {}))
        self.git = Git('git', lambda: self.settings.hot('git_timeout_s'))
        self.cache = repo.ensure_cache(self.git, self.state_dir / job / 'cache.git')
        self.clock = FakeClock(wall=1000.0, mono=1000.0)
        self.hub = Hub(job=job, git=self.git, cache=self.cache, code_remote=str(hub_path),
                       state_remote=state_remote, settings=self.settings, clock=self.clock)


def seed_hub(path: Path, files: dict | None = None) -> tuple:
    """The hub bare repo with one commit on main; returns (hub path, base sha)."""
    hub = bare(path)
    seed = plain_repo(Path(str(path) + '-seed'), files or {'README.md': 'demo\n', 'src/app.py': 'print(1)\n'})
    git('push', '-q', str(hub), 'main', cwd=seed)
    return hub, git('rev-parse', 'HEAD', cwd=seed)


def machines(tmp_path: Path, names=('laptop', 'desktop', 'server'), *, job: str = 'j', files: dict | None = None,
             state_remote: str = '', **hot) -> tuple:
    """(hub path, base sha, {name: Machine}) on one hub."""
    hub, base = seed_hub(Path(tmp_path) / 'hub.git', files)
    ms = {n: Machine(n, Path(tmp_path) / n, hub, job=job, state_remote=state_remote, hot=hot) for n in names}
    return hub, base, ms


@pytest.fixture
def three_machines(tmp_path):
    """Cross-machine fixture for any relay test (port-1 and flow-1): a real local bare repo `hub.git` as the one
    meeting point (seeded with one commit on main) and three temporary machine dirs laptop / desktop / server, each
    with its own clone, cache repo, state dir, FakeClock and Hub. Returns (hub path, base sha, {name: Machine}).
    Import it into the test module: `from relay_kit import isolated_git, three_machines  # noqa: F401`."""
    return machines(tmp_path)


class Rig:
    """One machine's state layer on the shared hub: beats, observer, reclaimer, notices in a list."""

    def __init__(self, m, machine, candidate_filter=None):
        from fleetmux.relay.beats import Beats
        from fleetmux.relay.events import Events, ViewStore
        from fleetmux.relay.liveness import Observer
        from fleetmux.relay.reclaim import Reclaimer
        from fleetmux.relay.state import RelayState
        self.m = m
        self.machine = machine
        self.notices = []
        self.events = Events(self.notices.append, lambda *a, **k: None, ViewStore(m.state_dir / "view-j.json",
                                                                                   m.clock), settings=m.settings)
        self.state = RelayState(m.state_dir / "relay-j.json")
        self.beats = Beats(m.hub, node=m.name, fleet={"name": "main"}, clock=m.clock, settings=m.settings)
        self.observer = Observer(m.settings, m.name, m.clock, self.events, job="j")
        self.reclaimer = Reclaimer(m.settings, m.name, m.hub, self.events, self.state, m.clock, self.observer,
                                   candidate_filter=candidate_filter)

    def beat(self, **kw):
        assert self.beats.push(state=kw.pop("state", "online"), machine=self.machine, **kw).accepted

    def look(self):
        lst = self.m.hub.list()
        self.tasks, self.nodes = self.m.hub.tasks(lst), self.m.hub.nodes(lst)
        self.observer.update(lst, self.tasks, self.nodes)
        return self.tasks

    def step(self):
        self.look()
        return self.reclaimer.step(self.tasks, self.nodes)
