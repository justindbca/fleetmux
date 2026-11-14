"""npm packages: scripts/build_npm.py puts the release binaries in one @fleetmux/<os>-<cpu> package per platform,
and the `fleetmux` package's launcher runs the binary of the installed one."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import build_npm  # noqa: E402


def _release(assets, program, version, triple, body, sha_ok=True):
    tar = assets / ('%s-%s-%s.tar.gz' % (program, version, triple))
    with tarfile.open(tar, 'w:gz') as t:
        for name, data, mode in ((program, body, 0o755), ('THIRD-PARTY-LICENSES', b'licenses', 0o644)):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(tar.read_bytes()).hexdigest() if sha_ok else '0' * 64
    (assets / (tar.name + '.sha256')).write_text('%s  %s\n' % (digest, tar.name))


def _assets(tmp_path, core_body=None, sha_ok=True):
    assets = tmp_path / 'assets'
    assets.mkdir()
    for _os, _cpu, _libc, core, web in build_npm.TARGETS:
        _release(assets, 'fleetmux', '9.9.9', core, core_body or b'core-' + core.encode(), sha_ok)
        _release(assets, 'fleetmux-web', '9.9.9', web, b'web-' + web.encode())
    return assets


def test_build_npm_makes_one_package_per_platform_and_the_main_package(tmp_path):
    out = tmp_path / 'out'
    assert build_npm.main(['--version', '9.9.9', '--assets', str(_assets(tmp_path)), '--out', str(out)]) == 0
    linux = out / 'fleetmux-linux-x64'
    meta = json.loads((linux / 'package.json').read_text())
    assert (meta['name'], meta['version'], meta['os'], meta['cpu'], meta['libc']) == \
        ('@fleetmux/linux-x64', '9.9.9', ['linux'], ['x64'], ['glibc'])
    assert (linux / 'bin' / 'fleetmux').read_bytes() == b'core-x86_64-unknown-linux-musl'
    assert (linux / 'bin' / 'fleetmux-web').read_bytes() == b'web-x86_64-unknown-linux-gnu'
    assert (linux / 'bin' / 'fleetmux').stat().st_mode & 0o777 == 0o755
    assert (linux / 'bin' / 'THIRD-PARTY-LICENSES-fleetmux-web').read_bytes() == b'licenses'
    assert 'libc' not in json.loads((out / 'fleetmux-darwin-arm64' / 'package.json').read_text())
    main = json.loads((out / 'fleetmux' / 'package.json').read_text())
    assert main['name'] == 'fleetmux' and main['bin'] == {'fleetmux': 'bin/fleetmux.js'}
    assert main['optionalDependencies'] == {
        '@fleetmux/darwin-arm64': '9.9.9', '@fleetmux/darwin-x64': '9.9.9',
        '@fleetmux/linux-x64': '9.9.9', '@fleetmux/linux-arm64': '9.9.9'}
    assert (out / 'fleetmux' / 'bin' / 'fleetmux.js').stat().st_mode & 0o777 == 0o755
    assert sorted(p.name for p in out.iterdir()) == [
        'fleetmux', 'fleetmux-darwin-arm64', 'fleetmux-darwin-x64', 'fleetmux-linux-arm64', 'fleetmux-linux-x64']


def test_build_npm_refuses_a_sha256_mismatch(tmp_path):
    with pytest.raises(SystemExit, match='sha256 mismatch'):
        build_npm.main(['--version', '9.9.9', '--assets', str(_assets(tmp_path, sha_ok=False)),
                        '--out', str(tmp_path / 'out')])


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_launcher_runs_the_platform_binary(tmp_path):
    script = b'#!/bin/sh\necho "args=$* bin=$FLEETMUX_BIN"\nexit 3\n'
    out = tmp_path / 'out'
    build_npm.main(['--version', '9.9.9', '--assets', str(_assets(tmp_path, core_body=script)), '--out', str(out)])
    plat = 'fleetmux-%s-%s' % (sys.platform, {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x64'}[os.uname().machine])
    modules = tmp_path / 'node_modules'
    shutil.copytree(out / 'fleetmux', modules / 'fleetmux')
    shutil.copytree(out / plat, modules / '@fleetmux' / plat.replace('fleetmux-', ''))
    env = {k: v for k, v in os.environ.items() if k != 'FLEETMUX_BIN'}
    r = subprocess.run(['node', str(modules / 'fleetmux' / 'bin' / 'fleetmux.js'), 'status', '--json'],
                       capture_output=True, text=True, env=env)
    binary = modules / '@fleetmux' / plat.replace('fleetmux-', '') / 'bin' / 'fleetmux'
    assert r.returncode == 3
    assert r.stdout.strip() == 'args=status --json bin=%s' % binary


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_launcher_without_the_platform_package_exits_127(tmp_path):
    out = tmp_path / 'out'
    build_npm.main(['--version', '9.9.9', '--assets', str(_assets(tmp_path)), '--out', str(out)])
    r = subprocess.run(['node', str(out / 'fleetmux' / 'bin' / 'fleetmux.js')], capture_output=True, text=True)
    assert r.returncode == 127 and 'is not installed' in r.stderr
