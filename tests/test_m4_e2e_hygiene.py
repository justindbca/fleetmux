"""Static self-check of the M4 end-to-end test code (e2e-1 plan r3 §3.2 / §7; design-1/m4-design.md §8.4).

The e2e keeps every directory it creates and signals only verified pids of its own world, so its source must
not contain any delete / move / rename call, pytest's rotating temp dirs, or pattern kills. ``reap`` is banned
as a word (``fleetmux reap``, the cleanup CLI verb); ``reap_orphan_redis(root=…)`` in launch.py is the
product's sweep scoped to the world's own Redis root (design §8.4 constraint 8) and does not match it.
"""
import re
from pathlib import Path

from _fleetmux_milestone import milestone

pytestmark = milestone(4)
TESTS = Path(__file__).resolve().parent
SCANNED = sorted(TESTS.glob('m4e/*.py')) + [TESTS / 'test_m4_fake_cli.py', TESTS / 'test_m4_e2e_studio_ops.py']
BANNED = {
    'rmtree': r'rmtree', 'unlink': r'unlink', 'os.remove': r'os\.remove', 'rmdir': r'rmdir',
    'shutil.move': r'shutil\.move', 'tmp_path': r'tmp_path', '--basetemp': r'--basetemp', 'pkill': r'pkill',
    'killall': r'killall', 'reap (word)': r'\breap\b', 'os.rename': r'os\.rename', '.rename(': r'\.rename\(',
    '.replace(': r'\.replace\(', 'tempfile': r'\btempfile\b', 'killpg': r'killpg', 'os.system': r'os\.system',
}


def test_the_e2e_sources_exist():
    missing = [str(p) for p in SCANNED if not p.is_file()]
    assert not missing, missing
    assert (TESTS / 'm4e' / 'fake_cli.py') in SCANNED and len(SCANNED) >= 9


def test_no_delete_move_rename_or_pattern_kill_in_the_e2e_sources():
    hits = []
    for path in SCANNED:
        for no, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            for name, pattern in BANNED.items():
                if re.search(pattern, line):
                    hits.append('%s:%d [%s] %s' % (path.relative_to(TESTS), no, name, line.strip()[:120]))
    assert not hits, 'banned calls in the e2e code:\n' + '\n'.join(hits)


def test_signals_go_only_through_the_verified_ledger():
    """os.kill appears once: Ledger.signal, after the world-identity checks (plan §4.4)."""
    found = [(p.name, no) for p in SCANNED for no, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1)
             if re.search(r'\bos\.kill\(', line)]
    assert len(found) == 1 and found[0][0] == 'inventory.py', found
    text = (TESTS / 'm4e' / 'inventory.py').read_text(encoding='utf-8')
    body = text[text.index('    def signal(self'):text.index('    def fallback(self')]
    assert body.index('self.verified(pid)') < body.index('os.kill(')


def test_no_listener_and_no_real_cli_on_path():
    """No TCP / HTTP listener in the fixtures (brief-m4 §6), and the fake CLI never imports the product."""
    for path in SCANNED:
        text = path.read_text(encoding='utf-8')
        assert not re.search(r'\.bind\(|\.listen\(|AF_INET|http\.server|socketserver', text), path.name
    fake = (TESTS / 'm4e' / 'fake_cli.py').read_text(encoding='utf-8')
    assert 'cmux_fleet' not in re.sub(r'""".*?"""', '', fake, flags=re.S)
