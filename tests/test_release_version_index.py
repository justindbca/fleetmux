"""scripts/install_release.sh keeps the version index that ``releases.resolve('version:<v>')`` reads.

Rollout's release_install commands only know the release's version (``{version}``, the tag without its prefix), so
an install with ``--bin-dir`` records the version of the release tree (``rust/Cargo.toml``
``[workspace.package] version``) in ``<release>/bin/installed.json`` and points
``$FLEETMUX_RELEASES/versions/<v>`` at ``../<sha>``. The link is published only once the release is complete: a
failed install (suite, baseline check after the switch) never moves it. A tree without a workspace version installs
as before, without an index entry.

The installer runs on the stubs of tests/test_install_release.py; every case uses a temporary FLEETMUX_RELEASES.
"""
import io
import json
import os
import tarfile

import pytest

from test_install_release import (APP_SHA, OTHER_SHA, make_bin_dir, make_python, release, run,  # noqa: F401
                                  world)
from fleetmux import releases

CARGO = '[workspace]\nmembers = ["crates/app"]\n\n[workspace.package]\nversion = "%s"\nedition = "2021"\n'


def tarball_with_version(world, version, name='src-versioned.tgz'):
    """The fixture's source tarball plus rust/Cargo.toml naming ``version`` (None: a Cargo.toml without one)."""
    path = world['tmp'] / name
    files = (('pyproject.toml', '[project]\nname = "fleetmux"\n'),
             ('requirements.txt', 'redis==7.3.0\n'),
             ('requirements-dev.txt', 'pytest\n'),
             ('scripts/check_deps.py', 'raise SystemExit(0)\n'),
             ('rust/Cargo.toml', CARGO % version if version else '[workspace]\nmembers = []\n'))
    with tarfile.open(path, 'w:gz') as tar:
        for fname, text in files:
            data = text.encode()
            info = tarfile.TarInfo(fname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def install(world, sha, version, *extra, env_extra=None):
    make_python(world['bin'])
    built = make_bin_dir(world['tmp'] / ('built-' + sha[:7]), source_sha=sha)
    return run(world, [sha, tarball_with_version(world, version, 'src-%s.tgz' % sha[:7]), '--bin-dir', built]
               + list(extra), env_extra=env_extra)


def link(world, version):
    return world['releases'] / 'versions' / version


def test_install_records_the_version_and_points_the_index_at_the_release(world, monkeypatch):
    done = install(world, APP_SHA, '0.6.0')
    assert done.returncode == 0, done.stderr
    rel = release(world)
    assert json.loads((rel / 'bin' / 'installed.json').read_text())['version'] == '0.6.0'
    assert os.readlink(link(world, '0.6.0')) == '../' + APP_SHA    # relative: the root may be moved as a whole
    monkeypatch.setenv('FLEETMUX_RELEASES', str(world['releases']))
    assert releases.resolve('version:0.6.0') == rel.resolve()     # the reader's side of the interface agrees
    assert 'version index: ' in done.stdout


def test_a_later_install_of_the_same_version_moves_the_index(world):
    assert install(world, APP_SHA, '0.6.0').returncode == 0
    done = install(world, OTHER_SHA, '0.6.0')
    assert done.returncode == 0, done.stderr
    assert os.readlink(link(world, '0.6.0')) == '../' + OTHER_SHA
    assert not [p for p in (world['releases'] / 'versions').iterdir() if p.name != '0.6.0']  # no temp link left


@pytest.mark.parametrize('extra, env, needle', [
    (('--test',), {'FAKE_TEST_RC': '1'}, 'full suite failed'),
    (('--activate',), {'FAKE_BASELINE_RC': '1'}, 'baseline check failed after the switch'),
])
def test_a_failed_install_never_moves_the_index(world, extra, env, needle):
    assert install(world, APP_SHA, '0.6.0').returncode == 0
    done = install(world, OTHER_SHA, '0.6.0', *extra, env_extra=env)
    assert done.returncode != 0 and needle in done.stderr, done.stderr
    assert not release(world, OTHER_SHA).exists()
    assert os.readlink(link(world, '0.6.0')) == '../' + APP_SHA    # still the good release


def test_a_tree_without_a_workspace_version_installs_without_an_index(world):
    done = install(world, APP_SHA, None)
    assert done.returncode == 0, done.stderr
    assert json.loads((release(world) / 'bin' / 'installed.json').read_text())['version'] is None
    assert not (world['releases'] / 'versions').exists()
    assert 'no version index' in done.stdout


def test_an_install_without_bin_dir_writes_no_index(world):
    make_python(world['bin'])
    done = run(world, [APP_SHA, tarball_with_version(world, '0.6.0')])
    assert done.returncode == 0, done.stderr
    assert not (world['releases'] / 'versions').exists()   # no installed.json, so resolve() could not confirm it
