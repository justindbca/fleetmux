"""Skip policy: a skipped test does not make a run green.

Loaded by ``tests/conftest.py`` (``pytest_plugins``), so it holds for every run of this suite: the full suite, one
file, one test. Every skip of the run is collected, from the run phase (``pytest.skip`` in a test or a fixture,
``skip`` / ``skipif`` marks, ``importorskip`` in a test) and from collection (a module-level ``pytest.skip`` or
``importorskip``), plus the skips a wrapping test reports from a Node test runner (``node_skips``).

A skip is allowed only when ``tests/skip_allowlist.yaml`` has an entry for it: ``file`` (the test file, or the file
whose code called skip) and ``reason`` (a substring of the skip reason), with ``why`` saying why it is a real skip
and not a missing dependency. A skip without an entry makes the run fail (exit status 1, unless it already failed)
and is listed under "skips not allowed". A missing program or package (redis-server, tmux, node, a built binary ...)
is never an allowed reason: install it. There is no switch that lets such a run pass.

Every run also compares its skips with ``tests/skip_baseline.yaml``, the skip sites of main (file + reason pattern,
generated from the source at a revision, see ``baseline`` below) and lists the skip sites main does not have.
The last line of the terminal summary is always::

    skip-policy: total=<skips> allowlisted=<n> not_allowed=<n> new_vs_main=<skip sites not in the baseline>

``--skip-report FILE`` also writes these numbers and every skip as JSON.

Baseline: ``python tests/skip_policy.py baseline --rev <rev> > tests/skip_baseline.yaml`` reads every ``tests/*.py``
and ``tests/*.mjs`` of ``<rev>`` from git (no test is run) and lists each skip call or mark with the file it is in
and its reason as an fnmatch pattern ('*' for a part computed at run time). A mark returned by a helper function
(``milestone(n)``) is listed for every file that calls the helper, since such a skip is reported at the test.
"""
import argparse
import ast
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ALLOWLIST = 'tests/skip_allowlist.yaml'
BASELINE = 'tests/skip_baseline.yaml'
ENTRY_KEYS = ('file', 'reason', 'why')
_STATE = pytest.StashKey()
_INTERNAL = (os.sep + '_pytest' + os.sep, os.sep + 'pluggy' + os.sep, str(Path(__file__).resolve()))


# ---------------------------------------------------------------- lists

class ListError(Exception):
    pass


def normalize(reason):
    """A skip reason as compared: no ``Skipped: `` prefix, whitespace runs as one space."""
    reason = str(reason or '')
    if reason.startswith('Skipped: '):
        reason = reason[len('Skipped: '):]
    return ' '.join(reason.split())


def load_allowlist(path, root):
    """The entries of an allowlist file; any problem (missing file, entry without ``file`` / ``reason`` / ``why``,
    an unknown key, a file that does not exist under ``root``) raises ListError."""
    path, root = Path(path), Path(root)
    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ListError('%s: cannot be read: %s' % (path, exc))
    if doc is None:
        doc = []
    if not isinstance(doc, list):
        raise ListError('%s: must be a list of entries {file, reason, why}' % path)
    out = []
    for n, e in enumerate(doc, 1):
        if not isinstance(e, dict):
            raise ListError('%s: entry %d is not a mapping' % (path, n))
        extra = sorted(set(e) - set(ENTRY_KEYS))
        if extra:
            raise ListError('%s: entry %d has unknown key(s) %s' % (path, n, ', '.join(map(str, extra))))
        for key in ENTRY_KEYS:
            if not isinstance(e.get(key), str) or not e[key].strip():
                raise ListError("%s: entry %d needs a non-empty '%s'" % (path, n, key))
        if not (root / e['file']).is_file():
            raise ListError('%s: entry %d names %s, which does not exist' % (path, n, e['file']))
        out.append({'file': e['file'], 'reason': normalize(e['reason']), 'why': e['why'].strip()})
    return out


def load_baseline(path):
    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ListError('%s: cannot be read: %s' % (path, exc))
    if not isinstance(doc, dict) or not isinstance(doc.get('sites'), list):
        raise ListError('%s: must be a mapping with rev, command and sites' % path)
    sites = []
    for n, s in enumerate(doc['sites'], 1):
        if not (isinstance(s, dict) and isinstance(s.get('file'), str) and isinstance(s.get('reason'), str)):
            raise ListError('%s: site %d needs file and reason' % (path, n))
        sites.append({'file': s['file'], 'reason': s['reason']})
    return str(doc.get('rev') or ''), sites


# ---------------------------------------------------------------- one run

class Skip:
    def __init__(self, nodeid, file, line, reason, origin=None, kind='pytest'):
        self.nodeid, self.file, self.line, self.reason = nodeid, file, int(line or 0), normalize(reason)
        self.origin = origin or file                      # the file whose code called skip (a fixture, a helper)
        self.kind = kind
        self.allowlisted = False
        self.new = False

    def files(self):
        return {self.nodeid.split('::')[0], self.file, self.origin}


class State:
    def __init__(self, root, allow, rev, sites):
        self.root, self.allow, self.rev, self.sites = root, allow, rev, sites
        self.skips = []

    def rel(self, path):
        p = Path(path)
        try:
            return p.resolve().relative_to(self.root).as_posix()
        except (ValueError, OSError):
            return str(path)

    def allowed(self, skip):
        files = skip.files()
        return any(e['file'] in files and e['reason'] in skip.reason for e in self.allow)

    def in_baseline(self, skip):
        files = skip.files()
        return any(s['file'] in files and fnmatch.fnmatchcase(skip.reason, s['reason']) for s in self.sites)

    def add(self, skip):
        skip.allowlisted = self.allowed(skip)
        skip.new = not self.in_baseline(skip)
        self.skips.append(skip)
        return skip

    def numbers(self):
        new_sites = {(s.origin, s.reason) for s in self.skips if s.new}
        return {'total': len(self.skips), 'allowlisted': sum(s.allowlisted for s in self.skips),
                'not_allowed': sum(not s.allowlisted for s in self.skips), 'new_vs_main': len(new_sites)}


def _origin(excinfo, state):
    """The last frame of the traceback outside pytest / pluggy / this file: where the skip was called."""
    try:
        entries = list(excinfo.traceback)
    except Exception:
        return None
    for entry in reversed(entries):
        path = str(entry.path)
        if not any(x in path for x in _INTERNAL):
            return state.rel(path), entry.lineno + 1
    return None


def _location(report, state):
    lr = report.longrepr
    if isinstance(lr, tuple) and len(lr) == 3:
        return state.rel(lr[0]), lr[1], lr[2]
    return report.nodeid.split('::')[0], 0, str(getattr(lr, 'reprcrash', None) and lr.reprcrash.message or lr)


# ---------------------------------------------------------------- pytest hooks

def pytest_addoption(parser):
    parser.addoption('--skip-report', metavar='FILE', default=None,
                     help='skip policy: also write the skip numbers and every skip of the run as JSON to FILE')


def pytest_configure(config):
    root = Path(str(config.rootpath)).resolve()
    try:
        allow = load_allowlist(root / ALLOWLIST, root)
        rev, sites = load_baseline(root / BASELINE)
    except ListError as exc:
        raise pytest.UsageError('skip policy: %s' % exc)
    state = config.stash[_STATE] = State(root, allow, rev, sites)
    config.pluginmanager.register(Policy(config, state), 'skip_policy_run')


class Policy:
    """The hooks of one run, bound to its config."""

    def __init__(self, config, state):
        self.config, self.state = config, state

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, item, call):
        report = yield
        if report.skipped and not hasattr(report, 'wasxfail') and call.excinfo is not None:
            report.skip_origin = _origin(call.excinfo, self.state)
        return report

    def pytest_runtest_logreport(self, report):
        if not report.skipped or hasattr(report, 'wasxfail'):
            return
        file, line, reason = _location(report, self.state)
        origin = getattr(report, 'skip_origin', None)
        self.state.add(Skip(report.nodeid, file, line, reason, origin[0] if origin else None))

    def pytest_collectreport(self, report):
        if report.skipped:
            file, line, reason = _location(report, self.state)
            self.state.add(Skip(report.nodeid or file, file, line, reason, kind='collect'))

    def pytest_sessionfinish(self, session, exitstatus):
        state = self.state
        n = state.numbers()
        if n['not_allowed'] and session.exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        path = self.config.getoption('--skip-report')
        if path:
            doc = dict(n, rev=state.rev, items=[
                {'nodeid': s.nodeid, 'file': s.file, 'line': s.line, 'origin': s.origin, 'reason': s.reason,
                 'kind': s.kind, 'allowlisted': s.allowlisted, 'new_vs_main': s.new} for s in state.skips])
            Path(path).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n')

    def pytest_terminal_summary(self, terminalreporter, exitstatus, config):
        state, tr = self.state, terminalreporter
        bad = [s for s in state.skips if not s.allowlisted]
        if bad:
            tr.write_line('skip-policy: skips not allowed by %s (each one fails this run; a missing dependency is '
                          'installed, not allowlisted):' % ALLOWLIST)
            for s in bad:
                at = '%s:%d' % (s.file, s.line)
                if s.origin not in (s.file, None):
                    at += ' (skip called in %s)' % s.origin
                tr.write_line('  %s  %s  %s' % (s.nodeid, at, s.reason))
        new = sorted({(s.origin, s.reason) for s in state.skips if s.new})
        if new:
            tr.write_line('skip-policy: skip sites not in the main baseline (%s, rev %s):'
                          % (BASELINE, state.rev or '?'))
            for origin, reason in new:
                tr.write_line('  %s  %s' % (origin, reason))
        tr.write_line('skip-policy: total=%(total)d allowlisted=%(allowlisted)d not_allowed=%(not_allowed)d '
                      'new_vs_main=%(new_vs_main)d' % state.numbers())


# ---------------------------------------------------------------- skips inside a Node test runner

TAP_SKIP = re.compile(r'^[ \t]*ok \d+ - [^\n]*?#[ \t]*SKIP\b[ \t]*([^\n]*?)[ \t]*$', re.I | re.M)


def tap_skips(output):
    """Reasons of the skipped tests in TAP output (``node --test --test-reporter=tap``)."""
    return [m.group(1) for m in TAP_SKIP.finditer(output or '')]


def node_skips(request, file, output):
    """Judge the skips a Node test runner reported for ``file`` (repo-relative) inside the test ``request`` runs:
    each one is counted by the policy like a pytest skip. Returns the reasons that are not allowlisted, so the
    wrapping test asserts ``== []`` and fails too."""
    state = request.config.stash.get(_STATE, None)
    if state is None:
        root = Path(str(request.config.rootpath)).resolve()
        state = State(root, load_allowlist(root / ALLOWLIST, root), *load_baseline(root / BASELINE))
    out = []
    for reason in tap_skips(output):
        s = state.add(Skip(request.node.nodeid, file, 0, reason, kind='node'))
        if not s.allowlisted:
            out.append(s.reason)
    return out


# ---------------------------------------------------------------- baseline from the source at a revision

SKIP_CALLS = {'skip': 0, 'importorskip': None, 'skipTest': 0}       # name -> positional index of the reason
MARK_CALLS = {'mark.skip': 0, 'mark.skipif': 1, 'unittest.skip': 0, 'unittest.skipIf': 1, 'unittest.skipUnless': 1}
MJS_SKIP = re.compile(r"""\bskip:\s*([^}]*)""")          # the option's value; its last string literal is the reason
MJS_STRING = re.compile(r"""(['"])([^'"]+)\1""")


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return '.'.join(reversed(parts))


def _escape(text):
    return ''.join('[%s]' % c if c in '*?[]' else c for c in text)


def _pattern(node):
    """An fnmatch pattern for a reason expression: literal parts kept, computed parts '*'."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _escape(node.value)
    if isinstance(node, ast.JoinedStr):
        return ''.join(_pattern(v) if isinstance(v, ast.Constant) else '*' for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _pattern(node.left) + _pattern(node.right)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Constant) \
            and isinstance(node.left.value, str):
        return re.sub(r'%[-#0 +]*\d*(?:\.\d+)?[sdrifgxXeEc]', '*', _escape(node.left.value).replace('%%', '%'))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'format' \
            and isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str):
        return re.sub(r'\{[^{}]*\}', '*', _escape(node.func.value.value))
    return '*'


def _arg(call, index, *names):
    for kw in call.keywords:
        if kw.arg in names:
            return kw.value
    return call.args[index] if index is not None and len(call.args) > index else None


def _call_site(call):
    """(kind, pattern) of a skip call or skip mark, else None."""
    name = _dotted(call.func)
    last = name.rsplit('.', 1)[-1]
    if name.endswith('importorskip'):
        reason = _arg(call, None, 'reason')
        if reason is not None:
            return 'call', _pattern(reason)
        mod = call.args[0] if call.args else None
        mod = mod.value if isinstance(mod, ast.Constant) and isinstance(mod.value, str) else None
        return 'call', ("could not import '%s'*" % _escape(mod)) if mod else '*'
    for key, index in MARK_CALLS.items():
        if name == key or name.endswith('.' + key):
            reason = _arg(call, index, 'reason')
            return 'mark', _pattern(reason) if reason is not None else '*'
    if last in SKIP_CALLS and (name in ('skip', 'pytest.skip') or last == 'skipTest'):
        reason = _arg(call, SKIP_CALLS[last], 'reason', 'msg')
        return 'call', _pattern(reason) if reason is not None else '*'
    return None


def _git(repo, *args):
    return subprocess.run(['git', '-C', str(repo)] + list(args), capture_output=True, text=True, check=True).stdout


def baseline_sites(repo, rev):
    names = [n for n in _git(repo, 'ls-tree', '-r', '--name-only', rev, '--', 'tests').splitlines()
             if n.endswith(('.py', '.mjs'))]
    sites, helpers, trees = set(), {}, {}
    for name in names:
        text = _git(repo, 'show', '%s:%s' % (rev, name))
        if name.endswith('.mjs'):
            for m in MJS_SKIP.finditer(text):
                strings = MJS_STRING.findall(m.group(1))
                if strings:
                    sites.add((name, _escape(strings[-1][1])))
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        trees[name] = tree
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                found = _call_site(node)
                if found:
                    sites.add((name, ' '.join(found[1].split())))
        for fn in tree.body:                        # a module-level helper that returns a skip mark
            if isinstance(fn, ast.FunctionDef):
                for node in ast.walk(fn):
                    found = isinstance(node, ast.Call) and _call_site(node)
                    if found and found[0] == 'mark':
                        helpers.setdefault(fn.name, set()).add(' '.join(found[1].split()))
    for name, tree in trees.items():
        called = {_dotted(n.func).rsplit('.', 1)[-1] for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for helper in called & set(helpers):
            sites |= {(name, p) for p in helpers[helper]}
    return sorted(sites)


def baseline_yaml(repo, rev):
    short = _git(repo, 'rev-parse', '--short', rev).strip()
    command = 'python tests/skip_policy.py baseline --rev %s > %s' % (short, BASELINE)
    head = ('# Skip sites of main at %s: file + reason pattern (fnmatch; "*" is a part computed at run time).\n'
            '# Generated from the source, no test run: %s\n' % (short, command))
    doc = {'rev': short, 'command': command, 'sites': [{'file': f, 'reason': r} for f, r in baseline_sites(repo, short)]}
    return head + yaml.safe_dump(doc, sort_keys=False, allow_unicode=False, width=200)


def main(argv=None):
    p = argparse.ArgumentParser(prog='skip_policy.py')
    sub = p.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('baseline', help='print the skip sites of a revision as tests/skip_baseline.yaml')
    b.add_argument('--rev', required=True)
    b.add_argument('--repo', default=str(Path(__file__).resolve().parents[1]))
    a = p.parse_args(argv)
    sys.stdout.write(baseline_yaml(a.repo, a.rev))
    return 0


if __name__ == '__main__':
    sys.exit(main())
