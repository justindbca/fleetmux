"""R23: the relay package calls no model and starts no agent CLI.

The scan reads every module of `fleetmux/relay/` (whatever slot wrote it) with `ast`: imports of model SDKs or of
fleetmux's agent-launching modules, string literals naming an agent CLI or a model API host, and process calls
whose literal program is an agent CLI all fail it. Other programs (git, fleetmux, system probes) are allowed."""
import ast
from pathlib import Path

import relay_kit  # noqa: F401  (puts the checkout on sys.path)
from fleetmux import relay

PACKAGE = Path(__file__).resolve().parents[1] / 'fleetmux' / 'relay'

BANNED_IMPORTS = ('anthropic', 'openai', 'google.generativeai', 'google.genai', 'litellm', 'claude_agent_sdk',
                  'mistralai', 'cohere', 'ollama',
                  'fleetmux.agent_bridge', 'fleetmux.agent_registry', 'fleetmux.advisor', 'fleetmux.claude_hook',
                  'fleetmux.claude_input', 'fleetmux.codex_queue', 'fleetmux.pi_adapter',
                  'fleetmux.opencode_adapter', 'fleetmux.profile_launch', 'fleetmux.dsh_adapter')
AGENT_CLIS = ('claude', 'codex', 'pi', 'opencode', 'gemini', 'aider', 'ollama', 'cursor-agent')
MODEL_HOSTS = ('api.anthropic.com', 'api.openai.com', 'generativelanguage.googleapis.com')
PROCESS_CALLS = ('run', 'Popen', 'call', 'check_call', 'check_output', 'system', 'execv', 'execvp', 'execvpe',
                 'spawnv', 'spawnvp', 'create_subprocess_exec', 'create_subprocess_shell')


def _banned_module(name):
    return any(name == b or name.startswith(b + '.') for b in BANNED_IMPORTS)


def _program(node):
    """The literal program of a process call's first argument, when it is written out."""
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, (ast.List, ast.Tuple)) and first.elts and isinstance(first.elts[0], ast.Constant):
        return str(first.elts[0].value)
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value.split()[0] if first.value.split() else ''
    return None


def scan(path):
    """Problems in one source file, as 'file:line: what'."""
    tree = ast.parse(Path(path).read_text(), filename=str(path))
    out = []
    for node in ast.walk(tree):
        where = '%s:%d' % (Path(path).name, getattr(node, 'lineno', 0))
        if isinstance(node, ast.Import):
            out += [where + ': imports ' + a.name for a in node.names if _banned_module(a.name)]
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods = [node.module] + [node.module + '.' + a.name for a in node.names]
            out += [where + ': imports ' + m for m in mods if _banned_module(m)][:1]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.strip()
            if text in AGENT_CLIS or Path(text).name in AGENT_CLIS:
                out.append(where + ': names agent CLI ' + repr(text))
            out += [where + ': names model host ' + h for h in MODEL_HOSTS if h in node.value]
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, 'id', '')
            prog = _program(node) if name in PROCESS_CALLS else None
            if prog is not None and Path(prog).name in AGENT_CLIS:
                out.append(where + ': runs ' + repr(prog))
    return out


def test_relay_package_calls_no_model_and_starts_no_agent():
    files = sorted(PACKAGE.rglob('*.py'))
    assert files, 'the relay package is missing'
    problems = [p for f in files for p in scan(f)]
    assert problems == []


def test_guard_catches_planted_violations(tmp_path):
    bad = tmp_path / 'bad.py'
    bad.write_text('import anthropic\nfrom fleetmux import codex_queue\nimport subprocess\n'
                   'subprocess.run(["claude", "-p", "hi"])\nsubprocess.Popen("codex exec x", shell=True)\n'
                   'URL = "https://api.openai.com/v1"\nsubprocess.run(["git", "status"])\n'
                   'subprocess.run(["/usr/local/bin/fleetmux", "status"])\nsubprocess.run(["sysctl", "-n", "hw.ncpu"])\n')
    problems = scan(bad)
    assert any('imports anthropic' in p for p in problems)
    assert any('fleetmux.codex_queue' in p for p in problems)
    assert any("names agent CLI 'claude'" in p for p in problems)
    assert any("runs 'codex'" in p for p in problems)
    assert any('api.openai.com' in p for p in problems)
    assert not any(x in p for p in problems for x in ("'git'", "fleetmux'", "'sysctl'"))


# R26: the program deletes nothing of the user's: no worktree, branch, ref or file. The only removal allowed is
# the state layer's own temporary index files (snapshot.py `_remove`).
DELETE_CALLS = ('rmtree', 'remove', 'unlink', 'rmdir', 'removedirs')
DELETE_ALLOWED = {('snapshot.py', 'unlink')}
GIT_DELETES = (('branch', '-D'), ('branch', '-d'), ('branch', '--delete'), ('worktree', 'remove'),
               ('worktree', 'prune'), ('push', '--delete'), ('push', '-d'), ('update-ref', '-d'), ('clean',),
               ('reset', '--hard'), ('checkout', '--force'), ('checkout', '-f'))


def _strings(node):
    out = []
    for a in node.args:
        items = a.elts if isinstance(a, (ast.List, ast.Tuple)) else [a]
        out += [i.value for i in items if isinstance(i, ast.Constant) and isinstance(i.value, str)]
    return out


def deletions(path):
    tree = ast.parse(Path(path).read_text(), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, 'id', '')
        where = '%s:%d' % (Path(path).name, node.lineno)
        owner = getattr(getattr(fn, 'value', None), 'id', '')
        if name == 'remove' and owner != 'os':              # list.remove / set.remove are not deletions
            name = ''
        if name in DELETE_CALLS and (Path(path).name, name) not in DELETE_ALLOWED:
            out.append(where + ': calls ' + name)
        words = _strings(node)
        for combo in GIT_DELETES:
            if all(w in words for w in combo):
                out.append(where + ': runs git ' + ' '.join(combo))
    return out


def test_relay_package_deletes_nothing():
    problems = [p for f in sorted(PACKAGE.rglob('*.py')) for p in deletions(f)]
    assert problems == []


def test_delete_guard_catches_planted_deletions(tmp_path):
    bad = tmp_path / 'bad.py'
    bad.write_text('import shutil, os, subprocess\nshutil.rmtree(p)\nos.remove(p)\nPath(p).unlink()\n'
                   'subprocess.run(["git", "branch", "-D", b])\ngit("worktree", "remove", w)\n'
                   'self.git.out("push", "origin", "--delete", r)\nsubprocess.run(["git", "status"])\n')
    problems = deletions(bad)
    assert [p.split(': ', 1)[1] for p in problems] == [
        'calls rmtree', 'calls remove', 'calls unlink', 'runs git branch -D', 'runs git worktree remove',
        'runs git push --delete']


def test_nothing_imports_the_removed_needs_module():
    """toolchain.py / placement.py are the only needs parser: no relay module or test imports
    fleetmux.relay.needs (the state layer removed it)."""
    root = PACKAGE.parents[1]
    files = sorted(PACKAGE.rglob('*.py')) + sorted((root / 'tests').glob('test_relay_*.py'))
    bad = []
    for f in files:
        for node in ast.walk(ast.parse(f.read_text(), filename=str(f))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mod = ('.' * node.level) + (node.module or '')
                names = [mod] + [mod.rstrip('.') + '.' + a.name for a in node.names]
            if any(n.endswith('relay.needs') or n in ('.needs', 'needs') or n.endswith('..needs') for n in names):
                bad.append('%s:%d' % (f.name, node.lineno))
    assert bad == []


# ---------------------------------------------------------------- the state layer's guards (test_port_*)
# port-1's rules on the relay package: its modules exist (no needs.py), one hub only (no meeting points, copies,
# arbitration or diverged refs), ASCII text (public repository), and it deletes nothing but its own temp files (R26).
# The R23 guard (no model call, no agent CLI) is flow-1's; integ-1 merges both sets into this file (ruling Q49),
# so every port test here is named test_port_*.

PKG = Path(relay.__file__).resolve().parent
FILES = sorted(PKG.glob("*.py"))


def test_port_package_files_exist():
    names = {p.stem for p in FILES}
    assert {"gitio", "refs", "records", "repo", "hub", "snapshot", "materialize", "checkpoint", "beats",
            "nodestatus", "liveness", "assign", "reclaim", "takeover", "progress", "params", "state",
            "events", "errors", "clock"} <= names
    assert "meeting" not in names          # one hub only
    assert "needs" not in names            # toolchain matching / needs parsing is flow-1's placement (ruling Q4)


def test_port_only_one_hub_no_multi_meeting_point_names():
    words = ("meeting", "arbiter", "diverged", "replicate", "reconcile", "meeting_order")
    hits = [f"{p.name}: {w}" for p in FILES for w in words if w in p.read_text().lower()]
    assert hits == []


def test_port_package_text_is_ascii():
    bad = [f"{p.name}:{n}" for p in FILES for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
           if any(ord(ch) > 127 for ch in line)]
    assert bad == []


def test_port_deletes_only_its_own_temp_files():
    """No recursive delete anywhere; single-file removals only in the snapshot temp index code."""
    bad = []
    for path in FILES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("rmtree", "removedirs", "rmdir"):
                bad.append(f"{path.name}:{node.lineno}: {node.attr}")
            if isinstance(node, ast.Attribute) and node.attr in ("unlink", "remove") and path.name != "snapshot.py":
                bad.append(f"{path.name}:{node.lineno}: {node.attr}")
    assert bad == []
