"""Skip policy (tests/skip_policy.py): a skipped test does not make a run green.

Every case runs a pytest sub-session in its own directory (``pytester``, in a subprocess) with the plugin loaded by
``-p skip_policy`` and its own ``tests/skip_allowlist.yaml`` / ``tests/skip_baseline.yaml``; nothing here reads or
changes the repository's lists except the two tests at the end that check them.
"""
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import skip_policy

pytest_plugins = ['pytester']

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
SUMMARY = re.compile(r'^skip-policy: total=(\d+) allowlisted=(\d+) not_allowed=(\d+) new_vs_main=(\d+)$', re.M)


def run(pytester, monkeypatch, files, allow=(), sites=(), args=()):
    """Write ``files`` under tests/, the allowlist and the baseline, then run pytest on tests/ in a subprocess."""
    monkeypatch.setenv('PYTHONPATH', str(TESTS) + os.pathsep + os.environ.get('PYTHONPATH', ''))
    pytester.makeini('[pytest]\ntestpaths = tests\n')
    tests = pytester.path / 'tests'
    tests.mkdir(exist_ok=True)
    for name, body in files.items():
        (tests / name).write_text(body)
    (tests / 'skip_allowlist.yaml').write_text(yaml.safe_dump(list(allow)))
    (tests / 'skip_baseline.yaml').write_text(yaml.safe_dump({'rev': 'base', 'command': 'test', 'sites': list(sites)}))
    return pytester.runpytest_subprocess('-p', 'skip_policy', '-p', 'no:cacheprovider', '-q', *args)


def summary(result):
    m = SUMMARY.findall(result.stdout.str())
    assert m, result.stdout.str()
    return tuple(int(x) for x in m[-1])


SKIPS_ONE = '''
import pytest

def test_ok():
    pass

def test_needs_redis():
    pytest.skip('redis-server unavailable')
'''


def test_a_skip_outside_the_allowlist_fails_the_run(pytester, monkeypatch):
    r = run(pytester, monkeypatch, {'test_a.py': SKIPS_ONE})
    assert r.ret == 1
    out = r.stdout.str()
    assert 'skips not allowed' in out and 'tests/test_a.py::test_needs_redis' in out and 'redis-server unavailable' in out
    assert summary(r) == (1, 0, 1, 1)


def test_an_allowlisted_skip_keeps_the_run_green(pytester, monkeypatch):
    allow = [{'file': 'tests/test_a.py', 'reason': 'redis-server', 'why': 'test'}]
    r = run(pytester, monkeypatch, {'test_a.py': SKIPS_ONE}, allow=allow)
    assert r.ret == 0, r.stdout.str()
    assert summary(r) == (1, 1, 0, 1) and 'skips not allowed' not in r.stdout.str()


def test_an_allowlist_entry_matches_only_its_own_file(pytester, monkeypatch):
    allow = [{'file': 'tests/test_b.py', 'reason': 'redis-server', 'why': 'test'}]
    r = run(pytester, monkeypatch, {'test_a.py': SKIPS_ONE, 'test_b.py': 'def test_b():\n    pass\n'}, allow=allow)
    assert r.ret == 1 and summary(r) == (1, 0, 1, 1)


def test_a_skipif_mark_is_judged_like_a_skip_call(pytester, monkeypatch):
    body = 'import pytest\n\n@pytest.mark.skipif(True, reason="tmux unavailable")\ndef test_t():\n    pass\n'
    r = run(pytester, monkeypatch, {'test_m.py': body})
    assert r.ret == 1 and 'tmux unavailable' in r.stdout.str() and summary(r) == (1, 0, 1, 1)


def test_a_skip_in_a_fixture_of_another_module_is_judged(pytester, monkeypatch):
    helper = 'import pytest\n\n@pytest.fixture\ndef server():\n    pytest.skip("redis-server unavailable")\n'
    body = 'from helper_fix import server  # noqa: F401\n\ndef test_t(server):\n    pass\n'
    r = run(pytester, monkeypatch, {'helper_fix.py': helper, 'test_f.py': body})
    assert r.ret == 1 and 'tests/helper_fix.py' in r.stdout.str() and summary(r)[2] == 1
    allow = [{'file': 'tests/helper_fix.py', 'reason': 'redis-server unavailable', 'why': 'test'}]
    r = run(pytester, monkeypatch, {'helper_fix.py': helper, 'test_f.py': body}, allow=allow)
    assert r.ret == 0 and summary(r)[:3] == (1, 1, 0)


def test_a_module_level_importorskip_fails_the_run(pytester, monkeypatch):
    body = 'import pytest\n\npytest.importorskip("no_such_module_for_skip_policy")\n\ndef test_x():\n    pass\n'
    r = run(pytester, monkeypatch, {'test_i.py': body})
    assert r.ret == 1, r.stdout.str()
    out = r.stdout.str()
    assert 'tests/test_i.py' in out and "could not import 'no_such_module_for_skip_policy'" in out
    assert summary(r) == (1, 0, 1, 1)


def test_a_module_level_skip_can_be_allowlisted(pytester, monkeypatch):
    body = 'import pytest\n\npytest.skip("deferred: waits for other work", allow_module_level=True)\n'
    allow = [{'file': 'tests/test_d.py', 'reason': 'deferred: waits', 'why': 'test'}]
    r = run(pytester, monkeypatch, {'test_d.py': body, 'test_ok.py': 'def test_ok():\n    pass\n'}, allow=allow)
    assert r.ret == 0 and summary(r) == (1, 1, 0, 1)


def test_skips_beyond_the_baseline_are_listed(pytester, monkeypatch):
    body = ('import pytest\n\ndef test_r():\n    pytest.skip("redis-server unavailable")\n\n'
            'def test_t():\n    pytest.skip("tmux unavailable at /usr/bin/tmux")\n')
    allow = [{'file': 'tests/test_s.py', 'reason': 'unavailable', 'why': 'test'}]
    sites = [{'file': 'tests/test_s.py', 'reason': 'redis-server unavailable'},
             {'file': 'tests/test_other.py', 'reason': 'tmux unavailable at *'}]
    r = run(pytester, monkeypatch, {'test_s.py': body}, allow=allow, sites=sites)
    assert r.ret == 0 and summary(r) == (2, 2, 0, 1)
    out = r.stdout.str()
    listed = out[out.index('not in the main baseline'):]
    assert 'tmux unavailable at /usr/bin/tmux' in listed and 'redis-server unavailable' not in listed
    sites[1]['file'] = 'tests/test_s.py'                        # a pattern with '*' covers the computed part
    r = run(pytester, monkeypatch, {'test_s.py': body}, allow=allow, sites=sites)
    assert summary(r) == (2, 2, 0, 0) and 'not in the main baseline' not in r.stdout.str()


def test_the_summary_line_is_printed_without_skips_too(pytester, monkeypatch):
    r = run(pytester, monkeypatch, {'test_ok.py': 'def test_ok():\n    pass\n'})
    assert r.ret == 0 and summary(r) == (0, 0, 0, 0)
    assert r.stdout.lines[-1].startswith('skip-policy: total=') or SUMMARY.search(r.stdout.str())


def test_a_failing_run_stays_failed(pytester, monkeypatch):
    allow = [{'file': 'tests/test_a.py', 'reason': 'redis-server', 'why': 'test'}]
    r = run(pytester, monkeypatch, {'test_a.py': SKIPS_ONE + '\ndef test_bad():\n    assert False\n'}, allow=allow)
    assert r.ret == 1 and summary(r)[2] == 0


def test_a_partial_run_is_judged_on_its_own_skips(pytester, monkeypatch):
    files = {'test_a.py': SKIPS_ONE, 'test_clean.py': 'def test_ok():\n    pass\n'}
    r = run(pytester, monkeypatch, files, args=('tests/test_clean.py',))
    assert r.ret == 0 and summary(r) == (0, 0, 0, 0)
    r = run(pytester, monkeypatch, files, args=('tests/test_a.py::test_needs_redis',))
    assert r.ret == 1 and summary(r) == (1, 0, 1, 1)


def test_the_json_report_lists_every_skip(pytester, monkeypatch):
    report = pytester.path / 'skips.json'
    allow = [{'file': 'tests/test_a.py', 'reason': 'redis-server', 'why': 'test'}]
    r = run(pytester, monkeypatch, {'test_a.py': SKIPS_ONE}, allow=allow, args=('--skip-report', str(report)))
    assert r.ret == 0
    doc = json.loads(report.read_text())
    assert (doc['total'], doc['allowlisted'], doc['not_allowed'], doc['new_vs_main']) == (1, 1, 0, 1)
    (item,) = doc['items']
    assert item['nodeid'] == 'tests/test_a.py::test_needs_redis' and item['file'] == 'tests/test_a.py'
    assert item['reason'] == 'redis-server unavailable' and item['allowlisted'] is True and item['line'] > 0


@pytest.mark.parametrize('entry, word', [
    ({'file': 'tests/test_a.py', 'reason': 'redis'}, 'why'),
    ({'file': 'tests/test_a.py', 'why': 'w'}, 'reason'),
    ({'reason': 'r', 'why': 'w'}, 'file'),
    ({'file': 'tests/test_a.py', 'reason': 'r', 'why': ''}, 'why'),
    ({'file': 'tests/test_a.py', 'reason': 'r', 'why': 'w', 'extra': 1}, 'extra'),
    ({'file': 'tests/no_such_file.py', 'reason': 'r', 'why': 'w'}, 'no_such_file'),
])
def test_a_bad_allowlist_entry_is_refused_when_loaded(pytester, monkeypatch, entry, word):
    r = run(pytester, monkeypatch, {'test_a.py': SKIPS_ONE}, allow=[entry])
    assert r.ret == pytest.ExitCode.USAGE_ERROR
    assert 'skip_allowlist.yaml' in r.stderr.str() and word in r.stderr.str()


def test_a_missing_allowlist_is_refused(pytester, monkeypatch):
    run(pytester, monkeypatch, {'test_a.py': 'def test_ok():\n    pass\n'})
    (pytester.path / 'tests' / 'skip_allowlist.yaml').unlink()
    r = pytester.runpytest_subprocess('-p', 'skip_policy', '-p', 'no:cacheprovider', '-q')
    assert r.ret == pytest.ExitCode.USAGE_ERROR and 'skip_allowlist.yaml' in r.stderr.str()


NODE_WRAPPER = '''
import skip_policy

TAP = """TAP version 13
ok 1 - works
ok 2 - bridge refuses # SKIP macOS has sandbox-exec
# skipped 1
"""

def test_wrapper(request):
    assert skip_policy.node_skips(request, 'tests/backend.mjs', TAP) == []
'''


def test_node_test_runner_skips_are_judged_by_the_wrapping_test(pytester, monkeypatch):
    files = {'test_w.py': NODE_WRAPPER, 'backend.mjs': '// fixture\n'}
    r = run(pytester, monkeypatch, files)
    assert r.ret == 1 and 'macOS has sandbox-exec' in r.stdout.str() and summary(r) == (1, 0, 1, 1)
    assert '1 failed' in r.stdout.str()                             # the wrapping test itself fails too
    allow = [{'file': 'tests/backend.mjs', 'reason': 'macOS has sandbox-exec', 'why': 'test'}]
    r = run(pytester, monkeypatch, files, allow=allow)
    assert r.ret == 0 and summary(r) == (1, 1, 0, 1)


def test_tap_skip_lines_are_parsed():
    out = ('ok 1 - a\nok 2 - b # SKIP needs x\n    ok 1 - nested # SKIP\nnot ok 3 - c\n'
           'ok 4 - d # skip lower case reason\n# skipped 3\n')
    assert skip_policy.tap_skips(out) == ['needs x', '', 'lower case reason']


def test_the_baseline_generator_reads_skip_sites_at_a_revision(tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'tests').mkdir(parents=True)
    (repo / 'tests' / 'gate.py').write_text(
        'import pytest\n\ndef milestone(n):\n    return pytest.mark.skipif(True, reason="M%d not in gate %s" % (n, 1))\n')
    (repo / 'tests' / 'test_x.py').write_text('''import pytest
from gate import milestone

RETIRED = pytest.mark.skip(reason='retired by [design] 48')
REAL = '/x'
tomllib = pytest.importorskip('tomllib')

@milestone(4)
def test_m():
    pass

@pytest.mark.skipif(not REAL, reason='needs ' 'zsh')
def test_z():
    pytest.skip('real binary not built: ' + str(REAL) + ' (build it)')
    pytest.skip(f'Node {REAL} required')
    pytest.skip(reason='kw form')
''')
    (repo / 'tests' / 'x.mjs').write_text("test('a', { skip: 'needs sandbox' }, fn);\n"
                                          "test('b', { skip: process.platform === 'darwin' && 'has it' }, fn);\n")
    (repo / 'tests' / 'notes.txt').write_text("pytest.skip('not python')\n")
    git = ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t']
    subprocess.run(git + ['init', '-q'], check=True)
    subprocess.run(git + ['add', '.'], check=True)
    subprocess.run(git + ['commit', '-q', '-m', 'x'], check=True)
    (repo / 'tests' / 'test_x.py').write_text('import pytest\npytest.skip("worktree only")\n')   # not committed
    out = subprocess.run([sys.executable, str(TESTS / 'skip_policy.py'), 'baseline', '--rev', 'HEAD',
                          '--repo', str(repo)], capture_output=True, text=True, check=True).stdout
    doc = yaml.safe_load(out)
    assert doc['rev'] == subprocess.run(git + ['rev-parse', '--short', 'HEAD'], capture_output=True,
                                        text=True).stdout.strip()
    assert 'skip_policy.py baseline --rev' in doc['command']
    got = {(s['file'], s['reason']) for s in doc['sites']}
    assert got == {
        ('tests/gate.py', 'M* not in gate *'),
        ('tests/test_x.py', 'M* not in gate *'),
        ('tests/test_x.py', 'retired by [[]design[]] 48'),
        ('tests/test_x.py', "could not import 'tomllib'*"),
        ('tests/test_x.py', 'needs zsh'),
        ('tests/test_x.py', 'real binary not built: * (build it)'),
        ('tests/test_x.py', 'Node * required'),
        ('tests/test_x.py', 'kw form'),
        ('tests/x.mjs', 'needs sandbox'),
        ('tests/x.mjs', 'has it'),
    }, got
    assert out == subprocess.run([sys.executable, str(TESTS / 'skip_policy.py'), 'baseline', '--rev', 'HEAD',
                                  '--repo', str(repo)], capture_output=True, text=True).stdout   # stable order


# ---------------------------------------------------------------- the repository's own lists

MISSING_ENV = re.compile(r'redis|tmux|\b[Nn]ode\b|\buv\b|zsh|real fleetmux|fleetmux binary|\bPi\b|pi-ai|typebox|'
                         r'M\d+ not in gate|[Mm]ilestone|[Pp]ython ?3\.12|sandbox cannot bind|not built|not installed|'
                         r'unavailable')


# The one milestone exception: the user's decision keeps the @milestone(4) tests out of this
# delivery (they are not run). Each file that applies milestone(4) has its own entry with exactly this reason.
M4_REASON = 'M4 not in gate'
M4_DECISION = 'user decision (milestone 4 not in this delivery)'


def milestone_4_files():
    """Repo-relative paths of the Python files under tests/ that call ``milestone(4)`` (decorator, ``pytestmark``,
    or ``pytest.param(..., marks=milestone(4))``). Read with ``ast``: text inside strings and docstrings is not a call."""
    out = set()
    for path in sorted(TESTS.rglob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if (isinstance(node, ast.Call) and getattr(node.func, 'id', getattr(node.func, 'attr', None)) == 'milestone'
                    and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == 4):
                out.add(path.relative_to(ROOT).as_posix())
    return out


def test_the_repository_allowlist_loads_and_holds_no_missing_environment_entry():
    entries = skip_policy.load_allowlist(TESTS / 'skip_allowlist.yaml', ROOT)
    assert entries
    m4 = milestone_4_files()
    for e in entries:
        if e['reason'] == M4_REASON and e['file'] in m4:
            continue                                            # the user's M4 decision, checked in the test below
        assert not MISSING_ENV.search(e['reason']), e


def test_each_milestone_4_file_has_exactly_one_allowlist_entry_citing_the_decision():
    entries = skip_policy.load_allowlist(TESTS / 'skip_allowlist.yaml', ROOT)
    m4 = milestone_4_files()
    assert 'tests/test_m4_e2e_ops.py' in m4 and 'tests/test_applet_view.py' in m4       # the scan finds both forms
    assert 'tests/test_skip_policy.py' not in m4 and 'tests/_fleetmux_milestone.py' not in m4
    milestone_entries = [e for e in entries if re.search(r'M\d+ not in gate|[Mm]ilestone', e['reason'])]
    assert all(e['reason'] == M4_REASON for e in milestone_entries), milestone_entries   # no other milestone
    files = [e['file'] for e in milestone_entries]
    assert len(files) == len(set(files)), files                                         # one entry per file
    assert set(files) == m4, (sorted(m4 - set(files)), sorted(set(files) - m4))
    for e in milestone_entries:
        assert M4_DECISION in e['why'] and 'not run' in e['why'], e


def test_a_milestone_mark_on_a_fixture_param_is_matched_at_the_test_file(pytester, monkeypatch):
    body = ('import pytest\n\nM4 = pytest.mark.skipif(True, reason="M4 not in gate m3")\n\n'
            '@pytest.fixture(params=["fake", pytest.param("real", marks=M4)])\ndef variant(request):\n'
            '    return request.param\n\ndef test_v(variant):\n    pass\n\n@M4\ndef test_d():\n    pass\n')
    r = run(pytester, monkeypatch, {'test_p.py': body})
    assert r.ret == 1 and summary(r) == (2, 0, 2, 1)
    allow = [{'file': 'tests/test_p.py', 'reason': M4_REASON, 'why': 'test'}]
    r = run(pytester, monkeypatch, {'test_p.py': body}, allow=allow)
    assert r.ret == 0 and summary(r) == (2, 2, 0, 1), r.stdout.str()


def history_has(rev):
    """True when ROOT is the top of a git checkout that holds ``rev`` (a release tree is a plain extracted copy)."""
    top = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != ROOT.resolve():
        return False
    return subprocess.run(['git', '-C', str(ROOT), 'cat-file', '-e', '%s^{commit}' % rev],
                          capture_output=True).returncode == 0


def test_the_repository_baseline_is_the_generator_output_at_its_revision():
    committed = yaml.safe_load((TESTS / 'skip_baseline.yaml').read_text())
    if not history_has(committed['rev']):
        pytest.skip('the baseline revision is not in the git history of this tree (an installed release copy)')
    fresh = yaml.safe_load(skip_policy.baseline_yaml(ROOT, committed['rev']))
    assert committed['sites'] == fresh['sites'] and committed['rev'] == fresh['rev']


def test_the_plugin_is_loaded_by_the_suite(request):
    assert request.config.pluginmanager.has_plugin('skip_policy')


# ---------------------------------------------------------------- conftest: the installed Pi package

def test_the_pi_package_is_found_from_the_pi_command(tmp_path):
    import conftest
    pkg = tmp_path / 'prefix' / 'lib' / 'node_modules' / '@earendil-works' / 'pi-coding-agent'
    (pkg / 'dist' / 'bundle').mkdir(parents=True)
    (pkg / 'package.json').write_text(json.dumps({'name': '@earendil-works/pi-coding-agent'}))
    (pkg / 'dist' / 'bundle' / 'cli.js').write_text('')
    (tmp_path / 'prefix' / 'bin').mkdir()
    pi = tmp_path / 'prefix' / 'bin' / 'pi'
    pi.symlink_to('../lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js')
    assert conftest.pi_package(lambda name: str(pi) if name == 'pi' else None) == pkg.resolve()
    assert conftest.pi_package(lambda name: None) is None
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'pi').write_text('#!/bin/sh\n')
    assert conftest.pi_package(lambda name: str(other / 'pi')) is None
