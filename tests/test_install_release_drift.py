"""install_release.sh --activate names the running fleets still on an older release and says when their daemons
switch them (docs/design.md section 56, "Release drift").

The installer runs against the stub world of tests/test_install_release.py (fake HOME, fake interpreter, fake
binaries); only the stub venv's ``-m fleetmux.release_drift`` call is passed to the real interpreter with this
checkout on PYTHONPATH. The "running fleets" are run directories under the fake HOME whose daemon lock this test
holds. No real fleet, link or fleet.yaml is read.
"""
import fcntl
import json
from pathlib import Path

import pytest

import test_install_release as base
from test_install_release import APP_SHA, OTHER_SHA, make_bin_dir, run, world  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parent.parent
HINT_CALL = 'fleetmux.release_drift'


def make_python(bin_dir):
    """The installer's stub interpreter, except that its venv runs ``-m fleetmux.release_drift`` for real."""
    template = base.PY_TEMPLATE.replace('case "$*" in\n',
                                        'case "$*" in\n  *' + HINT_CALL + '*) exec "${FAKE_REAL_PY:?}" "$@" ;;\n', 1)
    assert template != base.PY_TEMPLATE
    bin_dir.mkdir(parents=True, exist_ok=True)
    base.write_exe(bin_dir / 'uname',
                   '#!/bin/sh\ncase "$1" in -s) echo Linux ;; -m) echo aarch64 ;; *) echo Linux ;; esac\n')
    return base.write_exe(bin_dir / 'python3.14', template.replace('__RC__', '0'))


def fleet(home, name, release, live, locks):
    """A fleet home with latest.json naming a run whose daemon runs ``release``; ``live``: hold its daemon lock."""
    fhome = home / '.fleetmux' / 'fleets' / name
    run_dir = fhome / 'runs' / 'fleet-1'
    run_dir.mkdir(parents=True)
    cfg = {'run': str(run_dir), 'name': name, 'fleet_home': str(fhome)}
    (run_dir / 'config.json').write_text(json.dumps(cfg))
    (run_dir / 'release').write_text(release + '\n')
    (fhome / 'latest.json').write_text(json.dumps(dict(cfg, config=str(run_dir / 'config.json'))))
    if live:
        fh = (run_dir / 'daemon.lock').open('a+')
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(fh)
    return run_dir


@pytest.fixture
def locks():
    held = []
    yield held
    for fh in held:
        fh.close()


def release_dir(world, name, switch=True):
    """An installed release directory beside the one being activated; ``switch``: it has the release drift code."""
    d = world['tmp'] / 'fleetmux-releases' / name / 'fleetmux'
    d.mkdir(parents=True, exist_ok=True)
    if switch:
        (d / 'release_drift.py').write_text('')
    return d.parent


def install(world, locks, activate=True):  # noqa: F811
    world['releases'] = world['tmp'] / 'fleetmux-releases'  # a release is named by its directory there
    release_dir(world, 'old1234')  # alpha's release carries the automatic switch
    make_python(world['bin'])
    fleet(world['home'], 'alpha', 'old1234', True, locks)
    fleet(world['home'], 'beta', OTHER_SHA, True, locks)       # already on the release being activated
    fleet(world['home'], 'gamma', 'old1234', False, locks)     # not running: no daemon holds its lock
    built = make_bin_dir(world['tmp'] / 'built', source_sha=OTHER_SHA)
    return run(world, [OTHER_SHA, world['tarball'], '--bin-dir', built] + (['--activate'] if activate else []),
               env_extra={'PYTHONPATH': str(ROOT), 'FLEETMUX_CONFIG': ''})


def test_5_activate_lists_running_fleets_on_an_older_release_and_the_automatic_switch(world, locks):  # noqa: F811
    done = install(world, locks)
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert 'release ready:' in out
    tail = out[out.index('release ready:'):]
    assert 'fleet alpha runs release old1234' in tail
    assert 'beta' not in tail and 'gamma' not in tail
    assert 'switches it to release ' + OTHER_SHA + ' by itself within 25 minutes' in tail


def test_5_install_without_activate_prints_no_hint(world, locks):  # noqa: F811
    done = install(world, locks, activate=False)
    assert done.returncode == 0, done.stderr
    assert 'fleet alpha runs release' not in done.stdout
    assert not [line for line in base.logged(world) if HINT_CALL in line]


def line_of(text, name):
    found = [line for line in text.splitlines() if line.startswith('fleet ' + name + ' ')]
    assert len(found) == 1, text
    return found[0]


def test_p1_activate_promises_the_switch_only_for_releases_that_have_it(world, locks):  # noqa: F811
    old = release_dir(world, 'old5678', switch=False)   # a release from before the automatic switch
    delta = fleet(world['home'], 'delta', 'old5678', True, locks)
    epsilon = fleet(world['home'], 'epsilon', 'old9999', True, locks)  # its release directory is gone
    done = install(world, locks)
    assert done.returncode == 0, done.stderr
    tail = done.stdout[done.stdout.index('release ready:'):]
    alpha = line_of(tail, 'alpha')
    assert 'switches it to release ' + OTHER_SHA + ' by itself within 25 minutes' in alpha
    for name, run_dir, why in (('delta', delta, 'no fleetmux/release_drift.py in ' + str(old)),
                               ('epsilon', epsilon, 'release directory not found')):
        text = line_of(tail, name)
        assert 'will not switch by itself' in text and why in text, text
        # the one command that moves it : the installed release's own `fleetmux upgrade`
        cmd = text[text.index('bin/fleetmux upgrade'):]
        assert cmd.split()[:3] == ['bin/fleetmux', 'upgrade', '--to'] and OTHER_SHA in cmd.split()[3], text
        assert cmd.split()[4:6] == ['--config', str(run_dir / 'config.json')], text
        assert 'start.sh' not in text, text
        assert 'minutes' not in text and 'by itself within' not in text, text
