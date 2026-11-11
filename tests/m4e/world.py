"""The isolated world of one M4 end-to-end run (e2e-1 plan r3 §4; design-1/m4-design.md §8.4 constraints 1-9).

``<root>/<id>/`` with ``<root>`` = ``$M4E_ROOT`` or ``~/.m4e/fm4-e2e`` of the real HOME (the supervisor's grant,
M12), ``<id>`` = ``<6 hex>`` (new every run, an existing one fails; short so the child tmux socket path stays
<= 103 bytes under a 28-byte root such as ``~/.m4e/fm4-integ``; the creation time is in ``ev/world-id.json``
and ``ev/MANIFEST.json``). Below it:

    r/      Redis runtime root (--redis-runtime-dir)      s/    state dir (FLEETMUX_STATE, fleetmux runtime)
    h/      fake HOME (codex profile, memx wrapper, tab cwd) home/ fleet home (FLEETMUX_HOME)
    runs/   run directories (--runs-dir)                  bin/  shim copies `claude` / `codex`, tab stub
    kit/    the pinned studio-ops kit + completed domain kit  ev/   evidence (kept)

Nothing here deletes, moves or renames; nothing outside ``<root>/<id>/`` is written. The only edit of the copied
roster is the memx tab command (asserted against the original first).
"""
import hashlib
import io
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
ROOT = Path(os.environ.get('M4E_ROOT') or REAL_HOME / '.m4e' / 'fm4-e2e')
FLEETMUX_REAL = Path(os.environ.get('FLEETMUX_REAL_BIN') or REPO / 'rust/target/debug/fleetmux')
SHIM = REPO / 'rust/target/debug/examples/fake_cli_shim'
FAKE_CLI = Path(__file__).with_name('fake_cli.py')
STUB = Path(__file__).with_name('evermem_shell_stub.py')
LAUNCH = Path(__file__).with_name('launch.py')
PYTHON = sys.executable

# Kit source pin (brief-m4 §5 item 1; design §8.4 constraint 1).
KIT_SRC = Path(os.environ.get('M4E_STUDIO_OPS_SRC') or REAL_HOME / 'Documents/projects/usso-wt-kit-tabs')
KIT_REV = '3dc1fb253ccded9e81021762c48093eb7a587f00'
KIT_PATH = 'config/fleets/studio-ops'
KIT_TREE = '5663168d618ee47a01315e0ddf38325e30c845ce'
ROSTER_MD5 = 'a684df0cfbc69d6f827852a05fd705d8'
KIT_FILES = ('mayfly_roster.yaml', 'memx_roster.yaml', 'roles/mayfly-exit.md', 'roles/mayfly-reentry.md',
             'roles/mayfly-supervisor.md', 'roles/mem-extract.md', 'roles/memx-supervisor.md',
             'roles/ops-supervisor.md', 'roles/ops-worker.md', 'roles/tgog-supervisor.md', 'roles/tgog-worker.md',
             'roster.yaml', 'tgog_roster.yaml')
MEMX_TAB_ORIGINAL = ('/opt/homebrew/bin/python3.14 /Users/j/.fleetmux/fleets/evermem/bin/evermem_fleet_shell.py '
                     '--fleet-home /Users/j/.fleetmux/fleets/ops --subfleet memx --workers mem-1,mem-2,mem-3 '
                     '--port 8895 --log /Users/j/.futures/logs/services/evermem-fleet-shell.jsonl')
MEMX_WRAPPER = '.fleetmux/fleets/evermem/bin/claude-haiku-lite.sh'  # roster.yaml memx `claude:` under the fake HOME
TAB_CWD = '.fleetmux/fleets/evermem'  # memx_roster.yaml tabs[0].cwd under the fake HOME
CODEX_PROFILE = 'fleet'  # fleet.DEFAULTS['profile']: the Codex supervisor's profile file must exist
SOCKET_MAX = 103  # macOS sockaddr_un
DOMAINS = """domains:
  futures:
    summary: 期货 (m4e test kit; mayfly worker_domains)
    triggers: [futures, 期货]
    rules: domains/futures/RULES.md
    skills: []
    agents: []
    mcp: []
"""
SCRUB = ('FLEETMUX_TG_CMD', 'FLEETMUX_PARENT_CMD', 'FLEETMUX_THEME', 'CMUX_BIN', 'CODEX_HOME',
         'FLEETMUX_INITIATOR', 'XPC_SERVICE_NAME', 'TMUX', 'TMUX_PANE', 'FLEETMUX_HOME', 'FLEETMUX_STATE',
         'FLEETMUX_CONFIG', 'PYTHONPATH', 'VIRTUAL_ENV')
SCRUB_PREFIX = ('CLAUDE', 'ANTHROPIC_', 'OPENAI_', 'CMUX_SURFACE', 'CMUX_WORKSPACE', 'FLEETMUX_')


class WorldError(AssertionError):
    """A precondition of the end-to-end test does not hold: the test fails (never skips) with this text."""


def new_id():
    # 6 hex: unique enough per root (2^24); a collision is refused below, never reused.
    return secrets.token_hex(3)


def git(*args, check=True):
    done = subprocess.run(['git', '-C', str(KIT_SRC)] + list(args), capture_output=True, timeout=60)
    if check and done.returncode:
        raise WorldError('git ' + ' '.join(args) + ' in ' + str(KIT_SRC) + ' failed: '
                         + done.stderr.decode('utf-8', 'replace').strip()[-300:])
    return done.stdout


class World:
    def __init__(self, root=ROOT):
        root = Path(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)  # the supervisor's grant (M12): ~/.m4e/fm4-e2e
        self.id = new_id()
        self.created = time.strftime('%Y-%m-%dT%H:%M:%S%z')
        self.dir = root / self.id
        try:
            self.dir.mkdir(mode=0o700)
        except FileExistsError:
            raise WorldError('run directory already exists, refusing to reuse it: ' + str(self.dir))
        self.marker = 'm4e-' + secrets.token_hex(8)  # M4E_RUN: only in the launched trees' environment
        for sub in ('r', 's', 'h', 'home', 'runs', 'bin', 'kit', 'ev', 'ev/fake-cli'):
            (self.dir / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.dir / 'ev' / 'world-id.json').write_text(
            json.dumps({'id': self.id, 'created': self.created, 'root': str(root)}) + '\n', encoding='utf-8')
        self.r, self.s, self.h, self.home = (self.dir / x for x in ('r', 's', 'h', 'home'))
        self.runs, self.bin, self.kit, self.ev = (self.dir / x for x in ('runs', 'bin', 'kit', 'ev'))
        self.fleet_dir = self.kit / 'fleets' / 'studio-ops'
        self.claude, self.codex = self.bin / 'claude', self.bin / 'codex'
        self.stub = self.bin / 'evermem_shell_stub.py'
        self.stub_log = self.ev / 'evermem-stub.jsonl'
        self.private_config = self.dir / 'fleet.yaml'
        self.path = os.pathsep.join([str(self.bin), '/opt/homebrew/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin'])

    # ------------------------------------------------------------------------------------------- build
    def build(self):
        self.extract_kit()
        self.complete_kit()
        self.patch_memx_tab()
        self.fake_home()
        self.binaries()
        self.private_config.write_text('# m4e: no notification channel, no accounts (plan F6)\naccounts: []\n')
        return self

    def extract_kit(self):
        if not (KIT_SRC / '.git').exists():
            raise WorldError('kit source is not a git checkout: ' + str(KIT_SRC) + ' (set M4E_STUDIO_OPS_SRC)')
        git('cat-file', '-e', KIT_REV + '^{commit}')
        tree = git('rev-parse', KIT_REV + ':' + KIT_PATH).decode().strip()
        if tree != KIT_TREE:
            raise WorldError('studio-ops tree of %s is %s, pinned %s' % (KIT_REV, tree, KIT_TREE))
        data = git('archive', '--format=tar', KIT_REV, KIT_PATH)
        prefix = KIT_PATH.rsplit('/', 1)[0] + '/'  # config/fleets/
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            members = []
            for m in tar.getmembers():
                if m.isdir() and prefix.startswith(m.name.rstrip('/') + '/'):
                    continue  # config/, config/fleets/
                if not m.name.startswith(prefix):
                    raise WorldError('unexpected path in the kit archive: ' + m.name)
                m.name = m.name[len(prefix):]
                members.append(m)
            tar.extractall(self.kit / 'fleets', members=members, filter='data')
        files = sorted(str(p.relative_to(self.fleet_dir)) for p in self.fleet_dir.rglob('*') if p.is_file())
        if files != sorted(KIT_FILES):
            raise WorldError('studio-ops files differ from the pin: ' + repr(files))
        md5 = hashlib.md5((self.fleet_dir / 'roster.yaml').read_bytes()).hexdigest()
        if md5 != ROSTER_MD5:
            raise WorldError('roster.yaml md5 %s, pinned %s' % (md5, ROSTER_MD5))
        self.pin = {'source': str(KIT_SRC), 'rev': KIT_REV, 'tree': tree, 'roster_md5': md5, 'files': files}

    def complete_kit(self):
        """The rest of a valid domain kit around the pinned fleet dir (mayfly worker_domains [futures], advisor on);
        shape of tests/test_kit.make_kit, validated by fleetmux.kit.Kit. No mcp.json (plan F23)."""
        (self.kit / 'core').mkdir()
        (self.kit / 'core' / 'CLAUDE.md').write_text('# Core (m4e test kit)\n')
        (self.kit / 'context_domains.yaml').write_text(DOMAINS)
        (self.kit / 'domains' / 'futures').mkdir(parents=True)
        (self.kit / 'domains' / 'futures' / 'RULES.md').write_text('RULES OF futures (m4e test kit)\n')
        (self.kit / 'MANIFEST').write_text(json.dumps(dict(self.pin, completed=[
            'core/CLAUDE.md', 'context_domains.yaml', 'domains/futures/RULES.md']), indent=1) + '\n')

    def patch_memx_tab(self):
        """The one command substitution of the copy (design §8.4 constraint 2): memx tabs[0].command."""
        path = self.fleet_dir / 'memx_roster.yaml'
        text = path.read_text(encoding='utf-8')
        old = '    command: ' + MEMX_TAB_ORIGINAL + '\n'
        parts = text.split(old)
        if len(parts) != 2:
            raise WorldError('memx tab command is not the pinned original (found %d times)' % (len(parts) - 1))
        self.tab_argv = [PYTHON, str(self.stub), '--subfleet', 'memx', '--workers', 'mem-1,mem-2,mem-3',
                         '--log', str(self.stub_log)]
        new = '    command: ' + shlex.join(self.tab_argv) + '\n'
        path.write_text(parts[0] + new + parts[1], encoding='utf-8')
        self.patch = {'file': str(path), 'old': MEMX_TAB_ORIGINAL, 'new': shlex.join(self.tab_argv)}

    def fake_home(self):
        wrapper = self.h / MEMX_WRAPPER
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text('#!/bin/sh\n# m4e: stands in for the Haiku wrapper; the fake CLI only\nexec '
                           + shlex.quote(str(self.claude)) + ' "$@"\n')
        wrapper.chmod(0o755)
        (self.h / TAB_CWD).mkdir(exist_ok=True)
        codex = self.h / '.codex'
        codex.mkdir()
        (codex / (CODEX_PROFILE + '.config.toml')).write_text('# m4e fake Codex profile\nmodel = "m4e-fake"\n')
        (self.h / '.claude').mkdir()
        # macOS /etc/profile and /etc/zprofile run path_helper, which moves /opt/homebrew/bin (a real codex) ahead
        # of bin/: every login shell of the world (bash / sh / zsh) puts the fake CLI's bin/ first again.
        login = '# m4e: the native fake CLI comes first on PATH\nPATH=' + shlex.quote(str(self.bin)) + ':"$PATH"\nexport PATH\n'
        for name in ('.profile', '.bash_profile', '.zprofile'):
            (self.h / name).write_text(login, encoding='utf-8')

    def binaries(self):
        if not (SHIM.is_file() and os.access(SHIM, os.X_OK)):
            raise WorldError('fake CLI shim not built: ' + str(SHIM)
                             + ' (cd rust && cargo build -p ft-probe --example fake_cli_shim)')
        for dst in (self.claude, self.codex):
            shutil.copy2(SHIM, dst)  # a copy, never a link: `ps -o comm=` must show claude / codex (plan F4)
        (self.bin / 'fake_cli.conf').write_text('\n'.join([PYTHON, str(FAKE_CLI), str(self.ev / 'fake-cli')]) + '\n')
        shutil.copy2(STUB, self.stub)

    # ------------------------------------------------------------------------------------------- env
    def env(self, marked=True):
        """Environment of every process the test launches (plan §4.2). ``marked``: carries M4E_RUN."""
        env = {k: v for k, v in os.environ.items()
               if k not in SCRUB and not k.startswith(SCRUB_PREFIX) and k != 'M4E_RUN'}
        env.update(HOME=str(self.h), FLEETMUX_HOME=str(self.home), FLEETMUX_STATE=str(self.s),
                   FLEETMUX_CONFIG=str(self.private_config), FLEETMUX_BIN=str(FLEETMUX_REAL),
                   FLEETMUX_REAL_BIN=str(FLEETMUX_REAL), PATH=self.path, SHELL='/bin/bash',
                   M4E_LAUNCH_ROOT=str(self.dir))
        if marked:
            env['M4E_RUN'] = self.marker
        return env

    def start_argv(self):
        return [PYTHON, str(LAUNCH), 'start', '--allow-development-v2', '--mode', 'service', '--lifecycle', 'service',
                '--backend', 'fleetmux', '--worker-kit', str(self.kit), '--kit-fleet', 'studio-ops',
                '--supervisor-cli', 'codex', '--codex', str(self.codex), '--claude', str(self.claude),
                '--runs-dir', str(self.runs), '--redis-runtime-dir', str(self.r), '--codex-home',
                str(self.h / '.codex-agents'), '--cmux-state-dir', str(self.s), '--fleet-home', str(self.home)]

    def fleet(self, *args, timeout=120, marked=True):
        """``launch.py <args>`` (fleet.main) in the world env, cwd = the repo; CompletedProcess."""
        return subprocess.run([PYTHON, str(LAUNCH)] + list(args), cwd=str(REPO), env=self.env(marked),
                              capture_output=True, text=True, timeout=timeout)

    # ------------------------------------------------------------------------------------------- preconditions
    def preconditions(self):
        """Every check of plan §4.2 / §4.3 before anything starts; WorldError naming all that fail."""
        problems, facts = [], {}
        if sys.version_info < (3, 14):
            problems.append('python >= 3.14 needed, running ' + sys.version.split()[0] + ' (' + PYTHON + ')')
        if not (FLEETMUX_REAL.is_file() and os.access(FLEETMUX_REAL, os.X_OK)):
            problems.append('fleetmux binary not built: ' + str(FLEETMUX_REAL) + ' (cargo build -p fleetmux)')
        if not shutil.which('redis-server', path=self.path):
            problems.append('redis-server not on the test PATH ' + self.path)
        probe = subprocess.run(
            [PYTHON, '-c', 'import json; from fleetmux.backends import fleetmux as f; '
                           'print(json.dumps([f.availability_problem(), f.fleetmux_bin()]))'],
            cwd=str(REPO), env=self.env(marked=False), capture_output=True, text=True, timeout=60)
        try:
            problem, binary = json.loads(probe.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            problem, binary = 'availability probe failed: ' + (probe.stderr or probe.stdout)[-300:], None
        facts['fleetmux'] = {'problem': problem, 'binary': binary}
        if problem:
            problems.append(problem)
        if binary != str(FLEETMUX_REAL):
            problems.append('fleetmux resolves to %s, not the worktree binary %s' % (binary, FLEETMUX_REAL))
        facts['cli'] = self.cli_resolution(problems)
        facts['sockets'] = self.socket_budget(problems)
        probe = subprocess.run([PYTHON, '-c', 'import json, sys; from fleetmux import tabs; '
                                              'print(json.dumps(sorted(tabs.matching(json.loads(sys.argv[1])))))',
                                json.dumps(self.tab_argv)],
                               cwd=str(REPO), env=self.env(marked=False), capture_output=True, text=True, timeout=60)
        facts['tab_matching_before'] = probe.stdout.strip()
        if probe.returncode or probe.stdout.strip() not in ('[]', ''):
            problems.append('tabs.matching(stub argv) before start is not empty: ' + probe.stdout + probe.stderr)
        if problems:
            raise WorldError('m4e preconditions failed:\n- ' + '\n- '.join(problems))
        return facts

    def cli_resolution(self, problems):
        """Fail closed on any slot that could exec a real CLI (plan §4.2): absolute --claude / --codex, the memx
        wrapper, and `command -v claude` / `codex` in the login shell a pane runs (tgog / mayfly children)."""
        out = {'--claude': str(self.claude), '--codex': str(self.codex)}
        wrapper = self.h / MEMX_WRAPPER
        out['memx claude:'] = str(wrapper)
        if str(self.claude) not in wrapper.read_text():
            problems.append('memx wrapper does not exec the fake claude: ' + str(wrapper))
        for name in ('claude', 'codex'):
            done = subprocess.run(['env', '-i', 'HOME=' + str(self.h), 'PATH=' + self.path, 'bash', '-lc',
                                   'command -v ' + name], capture_output=True, text=True, timeout=30)
            got = done.stdout.strip()
            out['login shell ' + name] = got
            if os.path.realpath(got or '/nonexistent') != os.path.realpath(self.bin / name):
                problems.append('a login shell resolves %s to %r, not the fake %s' % (name, got, self.bin / name))
        roster_text = '\n'.join((self.fleet_dir / f).read_text(encoding='utf-8') for f in KIT_FILES if f.endswith('.yaml'))
        if re.search(r'^\s*(?:-\s*)?(?:\{[^}]*\b)?cli\s*:\s*codex', roster_text, re.M) or 'worker_cli: codex' in roster_text:
            problems.append('a roster slot asks for the codex CLI on PATH (a login shell finds the real one)')
        return out

    def socket_budget(self, problems):
        run = self.runs / ('fleet-' + time.strftime('%Y%m%d-%H%M%S-') + 'x' * 8)
        paths = {'ctl.sock': self.s / 'fleetmux' / 'fleetmux' / 'ctl.sock',
                 'redis.sock': self.r / ('x' * 32) / 'redis.sock',
                 'tmux.sock': run / 'tmux.sock',
                 'child tmux.sock': run / 'sub' / 'mayfly' / 'tmux.sock'}
        out = {}
        for name, path in paths.items():
            out[name] = len(str(path).encode())
            if out[name] > SOCKET_MAX:
                problems.append('%s path is %d bytes (> %d): %s' % (name, out[name], SOCKET_MAX, path))
        return out
