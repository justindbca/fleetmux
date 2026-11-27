"""`fleetmux demo --no-view` end to end, as a person runs it: one rejected claim, one accepted claim, the task
completed, everything cleaned up, nothing outside the demo's own directory touched.

The real product runs (start, task, daemon, v2 bus, Claude Stop-hook delivery, the artifact check, the audit stream);
the terminal server is tests/fake_fleetmux.py (the protocol on real PTYs, no cargo build), the agents are the
demo's scripted actors. The caller's FLEETMUX_HOME / FLEETMUX_STATE point at directories of this test, which must
come out unchanged; the caller's ~/.claude.json and ~/.claude/projects must not name the demo's directory. The
caller's HOME is a stand-in of this test with a synthetic Claude config (other projects only): the operator's
real ~/.claude.json and ~/.claude/projects are never read."""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FAKE_FLEETMUX = ROOT / 'tests' / 'fake_fleetmux.py'
SYNTHETIC_PROJECT = '/Users/someone/work/other-project'  # a project the stand-in Claude config already knows
BANNER = 'scripted agents, no model calls — the acceptance is real fleetmux code'
REASON = 'artifact not written: worker-1/test_slug.py'


def snapshot(d):
    return sorted((str(p.relative_to(d)), p.stat().st_mtime_ns, p.stat().st_size) for p in d.rglob('*'))


@pytest.fixture
def outer(tmp_path):
    """A clean machine as far as the demo can tell: a stand-in HOME (no agent CLI, no login, no fleet), a PATH with
    only the demo's programs (redis-server, tmux, git; python3.14 for the fake terminal server) and /usr/bin:/bin,
    and the caller's fleet directories (stand-ins for a fleet already running here). All must come out unchanged."""
    fh, st, home, path = tmp_path / 'outer-home', tmp_path / 'outer-state', tmp_path / 'home', tmp_path / 'bin'
    tmux_tmp = tmp_path / 'tmux-default'  # where a tmux server started without -S would put its socket
    tmux_tmp.mkdir()
    (fh / 'runs').mkdir(parents=True)
    st.mkdir()
    home.mkdir()
    (home / '.zshrc').write_text('# stand-in\n')
    seed_claude_config(home)
    path.mkdir()
    for name in ('redis-server', 'tmux', 'git', 'python3.14'):
        found = shutil.which(name)
        assert found, name + ' is needed by this test'
        (path / name).symlink_to(found)
    (fh / 'latest.json').write_text('{"run": "/somewhere/else"}\n')
    # built, not inherited: no BASH_ENV / ENV / ZDOTDIR / XDG_CONFIG_HOME / TMUX reaches the demo
    env = {'HOME': str(home), 'PATH': str(path) + ':/usr/bin:/bin', 'SHELL': '/bin/sh', 'LANG': 'en_US.UTF-8',
           'TMUX_TMPDIR': str(tmux_tmp),
           'TMPDIR': os.environ.get('TMPDIR', '/tmp'), 'USER': os.environ.get('USER', ''),
           'FLEETMUX_HOME': str(fh), 'FLEETMUX_STATE': str(st), 'FLEETMUX_BIN': str(FAKE_FLEETMUX),
           'FLEETMUX_DISCOVERY': 'off', 'PYTHONDONTWRITEBYTECODE': '1'}
    return fh, st, env, home


def demo_dirs():
    return {p for p in Path('/tmp').iterdir() if p.name.startswith('fmxd-')}


def own_processes(root):
    out = subprocess.run(['ps', '-U', str(os.getuid()), '-o', 'pid=,command='], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if str(root) in line and 'ps -U' not in line]


def seed_claude_config(home):
    """A synthetic Claude Code config in the stand-in HOME, as a machine with one other project has it."""
    (home / '.claude' / 'projects').mkdir(parents=True, exist_ok=True)
    for p in (home / '.claude' / 'projects').iterdir():
        p.rmdir()
    (home / '.claude' / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', SYNTHETIC_PROJECT)).mkdir()
    (home / '.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True, 'projects': {
        SYNTHETIC_PROJECT: {'hasTrustDialogAccepted': True}}}, indent=2) + '\n')


def operator_home_untouched_by(home, root):
    """The caller's Claude config names nothing of the demo: no trust entry, no transcript directory."""
    claude_json = home / '.claude.json'
    assert claude_json.is_file()  # seeded by the fixture: the check below has something to look at
    assert str(root) not in claude_json.read_text(errors='replace')
    slug = re.sub(r'[^A-Za-z0-9]', '-', str(root))
    projects = home / '.claude' / 'projects'
    assert projects.is_dir()
    assert not [p for p in projects.iterdir() if slug in p.name]



def test_the_home_check_catches_a_demo_entry_in_the_synthetic_claude_config(tmp_path):
    """Control for operator_home_untouched_by: it fails on a demo directory in claude.json or projects."""
    home, root = tmp_path / 'home', Path('/tmp/fmxd-control')
    seed_claude_config(home)
    operator_home_untouched_by(home, root)
    leaked = json.loads((home / '.claude.json').read_text())
    leaked['projects'][str(root / 'runs')] = {'hasTrustDialogAccepted': True}
    (home / '.claude.json').write_text(json.dumps(leaked))
    with pytest.raises(AssertionError):
        operator_home_untouched_by(home, root)
    seed_claude_config(home)
    (home / '.claude' / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', str(root / 'runs' / 'w'))).mkdir()
    with pytest.raises(AssertionError):
        operator_home_untouched_by(home, root)

def demo_root(text):
    m = re.search(r'Starting a one-off fleet in (\S+) ', text)
    assert m, text
    return Path(m.group(1))


def test_demo_rejects_then_accepts_and_cleans_up(outer):
    fh, st, env, home = outer
    before, dirs_before = (snapshot(fh), snapshot(st), snapshot(home)), demo_dirs()
    started = time.monotonic()
    done = subprocess.run([sys.executable, str(ROOT / 'fleetmux' / 'fleet.py'), 'demo', '--no-view'], env=env,
                          capture_output=True, text=True, timeout=180)
    took = time.monotonic() - started
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    root = demo_root(out)
    assert out.startswith('=' * len(BANNER) + '\n' + BANNER) and out.rstrip().endswith(BANNER)
    # the step lines: the false claim is rejected with the checker's own words, the fixed one passes
    assert '[scripted]   worker-1 reports: wrote worker-1/slug.py and its test.' in out
    assert '[fleetmux]   checks the claim: REJECTED, ' + REASON in out
    assert out.index('REJECTED, ' + REASON) < out.index('checks the claim: PASS') < out.index('task completed')
    # the summary, read from the audit stream: exactly two verdicts, in order
    summary = out[out.index('Acceptance record (read from the audit stream'):]
    assert re.findall(r'claimed done -> (\w+)', summary) == ['REJECTED', 'PASS']
    assert 'reason: ' + REASON in summary and 'Task status: completed' in summary
    assert took < 60, 'the demo took %.1f s' % took
    # clean: directory gone, nothing of it running, no other demo directory left behind
    assert 'Cleaned up: processes stopped, ' + str(root) + ' removed.' in out and 'Left running' not in out
    assert not root.exists() and not own_processes(root) and demo_dirs() <= dirs_before
    # nothing outside the demo's directory: the caller's HOME and fleet directories, the user's real ~/.claude
    assert (snapshot(fh), snapshot(st), snapshot(home)) == before
    operator_home_untouched_by(home, root)


def interrupt_after(env, marker, delay=0.0):
    """Run the demo, send SIGINT ``delay`` s after the line containing ``marker``; (exit code, output, root)."""
    proc = subprocess.Popen([sys.executable, str(ROOT / 'fleetmux' / 'fleet.py'), 'demo', '--no-view'], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    seen = []
    try:
        for line in proc.stdout:
            seen.append(line)
            if marker in line:
                time.sleep(delay)
                proc.send_signal(signal.SIGINT)
                break
        rest, _ = proc.communicate(timeout=120)
    finally:
        if proc.poll() is None:
            proc.kill()
    out = ''.join(seen) + rest
    return proc.returncode, out, demo_root(out)


def assert_interrupted_clean(code, out, root, before, fh, st, home):
    assert code == 130, out
    assert 'Interrupted: cleaning up.' in out and 'removed.' in out and 'Left running' not in out
    assert out.rstrip().endswith(BANNER)
    deadline = time.monotonic() + 10
    while own_processes(root) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not root.exists() and not own_processes(root), own_processes(root)
    assert (snapshot(fh), snapshot(st), snapshot(home)) == before
    operator_home_untouched_by(home, root)


def test_ctrl_c_while_the_fleet_starts_cleans_up(outer):
    """Interrupted inside `start`: Redis, the terminal server and the daemon may be up before start returns."""
    fh, st, env, home = outer
    before = (snapshot(fh), snapshot(st), snapshot(home))
    code, out, root = interrupt_after(env, 'Starting a one-off fleet', delay=0.4)
    assert 'Task:' not in out, 'the interrupt came after start returned; this test needs it inside start'
    assert_interrupted_clean(code, out, root, before, fh, st, home)


def test_ctrl_c_in_the_middle_cleans_up(outer):
    fh, st, env, home = outer
    before = (snapshot(fh), snapshot(st), snapshot(home))
    code, out, root = interrupt_after(env, 'supervisor -> worker-1')  # the fleet is up and working
    assert_interrupted_clean(code, out, root, before, fh, st, home)


def test_summary_is_read_from_the_audit_stream(outer):
    """Each summary line names the audit record it was read from; with --keep the run's audit stream is kept as
    audit.jsonl, and the lines must be exactly its acceptance.verdict records: same ids, same order, same results,
    same reasons. A summary written from the script cannot know those ids."""
    fh, st, env, home = outer
    done = subprocess.run([sys.executable, str(ROOT / 'fleetmux' / 'fleet.py'), 'demo', '--no-view', '--keep'],
                          env=env, capture_output=True, text=True, timeout=180)
    out = done.stdout + done.stderr
    root = demo_root(out)
    try:
        assert done.returncode == 0, out
        records = [json.loads(line) for line in (root / 'audit.jsonl').read_text().splitlines()]
        verdicts = [r for r in records if r.get('event') == 'acceptance.verdict']
        summary = out[out.index('Acceptance record (read from the audit stream'):out.index('Task status:')]
        shown = re.findall(r'claimed done -> (\w+)\s+\(audit ([^)\s]+)\)', summary)
        assert shown == [('PASS' if str(v['audit']['ok']).lower() == 'true' else 'REJECTED', v['stream_id'])
                         for v in verdicts]
        assert len(shown) == 2
        assert re.findall(r'reason: (.+)', summary) == [v['audit']['problem'] for v in verdicts
                                                        if str(v['audit']['ok']).lower() != 'true']
        assert not own_processes(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
