"""The Rust crates publish to crates.io under the workspace version.

- the internal path dependencies carry the workspace version (cargo publish needs it, and a version bump
  must move them together);
- every published crate has a description; ft-probe is never published.
"""
import pathlib

import pytest

tomllib = pytest.importorskip('tomllib')

RUST = pathlib.Path(__file__).resolve().parents[1] / 'rust'
PUBLISHED = {'proto': 'fleetmux-proto', 'term': 'fleetmux-term', 'layout': 'fleetmux-layout',
             'app': 'fleetmux', 'web': 'fleetmux-web'}


def load(path):
    return tomllib.loads(path.read_text())


def test_internal_path_dependencies_carry_the_workspace_version():
    ws = load(RUST / 'Cargo.toml')['workspace']
    version = ws['package']['version']
    assert ws['package']['publish'] is True
    internal = {k: v for k, v in ws['dependencies'].items() if isinstance(v, dict) and 'path' in v}
    assert set(internal) == {'fleetmux-proto', 'fleetmux-term', 'fleetmux-layout'}
    for name, dep in internal.items():
        assert dep.get('version') == version, name
    web_proto = load(RUST / 'crates/web/Cargo.toml')['dependencies']['fleetmux-proto']
    assert web_proto.get('version') == version


def test_published_crates_have_metadata_and_probe_stays_private():
    for crate, name in PUBLISHED.items():
        pkg = load(RUST / 'crates' / crate / 'Cargo.toml')['package']
        assert pkg['name'] == name
        assert pkg.get('description'), name
        assert pkg.get('repository') == {'workspace': True}, name
    assert load(RUST / 'crates/probe/Cargo.toml')['package']['publish'] is False
