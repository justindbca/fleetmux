"""A release switch leaves the switch baseline alone (fleetmux/baseline_check.py ``upgrade_report`` /
``upgrade_problems``; ``fleetmux superpowers baseline check [--before <hash>]``, run by scripts/install_release.sh
before and after it points ~/fleetmux-current at the new release).

A running root fleet on a private test Redis, a per-test inventory file and a per-test ~/fleetmux-current link
(tests/conftest.py): no live fleet, no real inventory, no real release directory.
"""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path

import pytest

from test_baseline_check import block, own_inventory, root, write  # noqa: F401  (fixtures)
from test_subfleet import svc  # noqa: F401  (fixture)
from fleetmux import baseline_check, fleet_superpowers_cli, releases
from fleetmux import fleet_superpowers as fs


def machine(f):
    return ('machines:\n  box-a:\n    source: self\n    fleets:\n      - {name: main, config: %s, running: true}\n'
            'models:\n  m1: {cli: claude}\nworkflows:\n  docs-wf:\n    machines: [box-a]\n' % f.path)


@pytest.fixture
def up(root):
    """The root fleet runs (its daemon holds the run lock) and has one workflow loaded."""
    f = root.f
    lock = (f.run_dir / 'daemon.lock').open('a+')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    loads = f.run_dir / 'superpowers-load'
    loads.mkdir()
    (loads / 'docs-wf.json').write_text(json.dumps({'added': {'workers': [{'name': 'docs-1'}]}}))
    yield root
    lock.close()


def switch_release(tmp_path):
    """What install_release.sh --activate does: ~/fleetmux-current moves to another release directory."""
    link = Path(releases.LINK)
    old, new = tmp_path / 'releases' / 'aaaaaaa', tmp_path / 'releases' / 'bbbbbbb'
    old.mkdir(parents=True, exist_ok=True)
    new.mkdir(parents=True, exist_ok=True)
    if not link.is_symlink():
        link.symlink_to(old)
    tmp = link.with_name(link.name + '.new')
    tmp.symlink_to(new)
    os.replace(tmp, link)
    return new


def test_a_release_switch_keeps_the_block_the_workflows_and_the_deviations(up, tmp_path):
    f = up.f
    path = write(block(supervisor_split='true'), machine=machine(f))
    f.tick()  # the daemon has reported supervisor_split: one deviation that is still open
    before_bytes = path.read_bytes()
    before = baseline_check.upgrade_report()
    assert before['hash'] and before['path'] == str(path)
    row, = before['fleets']
    assert row['config'] == str(f.path) and row['workflows'] == ['docs-wf']
    assert [d['switch'] for d in row['deviations']] == ['supervisor_split']
    switch_release(tmp_path)
    after = baseline_check.upgrade_report()
    assert path.read_bytes() == before_bytes and after['hash'] == before['hash']
    assert after['fleets'][0]['workflows'] == ['docs-wf']
    assert baseline_check.upgrade_problems(before['hash'], before, after) == []
    assert fs.read().get('workflows', {}).get('docs-wf')  # the workflows block still loads


def test_a_changed_block_fails_the_check(up):
    f = up.f
    write(block(supervisor_split='true'), machine=machine(f))
    before = baseline_check.upgrade_report()
    write(block(supervisor_split='false'), machine=machine(f))
    problems = baseline_check.upgrade_problems(before['hash'], before, baseline_check.upgrade_report())
    assert problems and 'hash' in problems[0]


def test_a_new_deviation_and_a_lost_workflow_fail_the_check(up):
    f = up.f
    write(block(supervisor_split='true'), machine=machine(f))
    before = baseline_check.upgrade_report()  # nothing reported yet
    f.tick()
    (f.run_dir / 'superpowers-load' / 'docs-wf.json').unlink()
    problems = baseline_check.upgrade_problems(before['hash'], before, baseline_check.upgrade_report())
    assert any('new deviation' in p and 'supervisor_split' in p for p in problems)
    assert any('docs-wf' in p and 'no longer loaded' in p for p in problems)


def test_a_fleet_whose_daemon_is_down_is_left_out(root):
    f = root.f
    write(block(supervisor_split='true'), machine=machine(f))
    f.tick()
    assert baseline_check.upgrade_report()['fleets'] == []


def cli(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = fleet_superpowers_cli.main(argv)
    return code, out.getvalue()


def test_the_cli_prints_the_report_and_fails_on_another_hash(up, tmp_path):
    f = up.f
    write(block(supervisor_split='true'), machine=machine(f))
    f.tick()
    code, text = cli(['baseline', 'check', '--json'])
    assert code == 0
    report = json.loads(text)
    assert report['hash'] and report['fleets'][0]['deviations'][0]['switch'] == 'supervisor_split'
    saved = tmp_path / 'before.json'
    saved.write_text(text)
    code, text = cli(['baseline', 'check', '--before', report['hash'], '--before-report', str(saved)])
    assert code == 0 and report['hash'] in text and 'docs-wf' in text and 'supervisor_split' in text
    code, text = cli(['baseline', 'check', '--before', '0' * 64])
    assert code == 1 and 'hash' in text


def test_the_cli_never_writes_the_inventory(up):
    f = up.f
    path = write(block(supervisor_split='true'), machine=machine(f))
    stamp, data = path.stat().st_mtime_ns, path.read_bytes()
    cli(['baseline', 'check'])
    cli(['baseline', 'check', '--json'])
    assert path.read_bytes() == data and path.stat().st_mtime_ns == stamp


def test_no_baseline_block_has_an_empty_hash_and_is_no_failure(up):
    f = up.f
    write('', machine=machine(f))
    before = baseline_check.upgrade_report()
    assert before['hash'] == baseline_check.block_hash('')
    assert baseline_check.upgrade_problems(before['hash'], before, baseline_check.upgrade_report()) == []
