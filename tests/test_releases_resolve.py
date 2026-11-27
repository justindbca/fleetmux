"""``releases.resolve``: a release named by its version (``version:<v>``), the form rollout's registry commands have
(``{version}`` is the tag without its prefix). The installer keeps the index: it writes
``"version"`` into ``<release>/bin/installed.json`` and points ``$FLEETMUX_RELEASES/versions/<v>`` at ``../<sha>``.
This side only reads that interface. Every case uses a temporary FLEETMUX_RELEASES; ~/fleetmux-releases is never
read."""
import json
import os
from pathlib import Path

import pytest

from upgrade_kit import make_release
from fleetmux import releases


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / 'fleetmux-releases'
    root.mkdir()
    monkeypatch.setenv('FLEETMUX_RELEASES', str(root))
    return root


def installed(rel, version):
    (Path(rel) / 'bin' / 'installed.json').write_text(json.dumps({'release_sha': Path(rel).name, 'version': version}))


def index(root, version, sha):
    (root / 'versions').mkdir(exist_ok=True)
    os.symlink('../' + sha, root / 'versions' / version)


def test_resolve_follows_the_version_link_to_the_release(root):
    rel = make_release(root.parent, 'bbbbbbb2')
    installed(rel, '0.3.2')
    index(root, '0.3.2', 'bbbbbbb2')
    assert releases.resolve('version:0.3.2') == rel.resolve()


def test_resolve_refuses_an_unknown_version(root):
    with pytest.raises(ValueError, match=r'version 0\.9\.9: no .*versions/0\.9\.9'):
        releases.resolve('version:0.9.9')


def test_resolve_refuses_a_release_whose_installed_json_names_another_version(root):
    rel = make_release(root.parent, 'bbbbbbb2')
    installed(rel, '0.3.1')
    index(root, '0.3.2', 'bbbbbbb2')
    with pytest.raises(ValueError, match=r'installed\.json says version 0\.3\.1'):
        releases.resolve('version:0.3.2')


def test_resolve_refuses_a_release_without_installed_json(root):
    make_release(root.parent, 'bbbbbbb2')
    index(root, '0.3.2', 'bbbbbbb2')
    with pytest.raises(ValueError, match=r'installed\.json'):
        releases.resolve('version:0.3.2')


def test_resolve_refuses_a_link_that_leaves_the_releases_root(root, tmp_path):
    elsewhere = make_release(tmp_path / 'other', 'ccccccc3')
    installed(elsewhere, '0.3.2')
    (root / 'versions').mkdir()
    os.symlink(str(elsewhere), root / 'versions' / '0.3.2')
    with pytest.raises(ValueError, match=r'not a release under'):
        releases.resolve('version:0.3.2')


@pytest.mark.parametrize('bad', ['version:', 'version:../x', 'version:a/b'])
def test_resolve_refuses_a_malformed_version(root, bad):
    with pytest.raises(ValueError, match='version'):
        releases.resolve(bad)


def test_resolve_leaves_other_values_alone(root):
    assert releases.resolve('/some/dir') is None and releases.resolve('bbbbbbb2') is None
