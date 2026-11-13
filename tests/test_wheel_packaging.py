"""PyPI wheels: the package runs without the repository (keys.json packaged, runtime root outside
site-packages), scripts/build_wheels.py adds the release binaries, and the `fleetmux` launcher execs them."""
import base64
import csv
import hashlib
import importlib
import io
import os
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import build_wheels  # noqa: E402
from fleetmux import wheel_launcher  # noqa: E402
from fleetmux.backends import fleetmux as backend  # noqa: E402
from fleetmux import redis_runtime  # noqa: E402


def test_packaged_keys_equal_the_protocol_keys():
    assert backend.KEYS_FILE == ROOT / 'fleetmux' / 'backends' / 'keys.json'
    assert backend.KEYS_FILE.read_bytes() == (ROOT / 'rust' / 'protocol' / 'keys.json').read_bytes()


def test_checkout_keeps_its_runtime_root_beside_the_code():
    assert redis_runtime.DEFAULT_ROOT == ROOT / '.runtime' / 'redis'


def test_installed_package_puts_the_runtime_root_in_the_state_dir(tmp_path, monkeypatch):
    site = tmp_path / 'site-packages'
    (site / 'fleetmux').mkdir(parents=True)
    (site / 'fleetmux' / 'redis_runtime.py').write_text((ROOT / 'fleetmux' / 'redis_runtime.py').read_text())
    monkeypatch.setenv('FLEETMUX_STATE', str(tmp_path / 'state'))
    spec = importlib.util.spec_from_file_location('rr_copy', site / 'fleetmux' / 'redis_runtime.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.DEFAULT_ROOT == tmp_path / 'state' / 'redis-runtime'


def _release(assets, program, version, triple, body, sha_ok=True):
    tar = assets / ('%s-%s-%s.tar.gz' % (program, version, triple))
    with tarfile.open(tar, 'w:gz') as t:
        for name, data, mode in ((program, body, 0o755), ('THIRD-PARTY-LICENSES', b'licenses', 0o644)):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(tar.read_bytes()).hexdigest() if sha_ok else '0' * 64
    (assets / (tar.name + '.sha256')).write_text('%s  %s\n' % (digest, tar.name))


def _base_wheel(path, version):
    di = 'fleetmux-%s.dist-info' % version
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('fleetmux/__init__.py', '')
        z.writestr(di + '/WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        z.writestr(di + '/entry_points.txt', '[console_scripts]\nfleetmux-py = fleetmux.fleet:main\n')
        z.writestr(di + '/RECORD', '')


def test_build_wheels_adds_the_release_binaries(tmp_path):
    assets, out = tmp_path / 'assets', tmp_path / 'out'
    assets.mkdir()
    for core, web, _tag in build_wheels.TARGETS:
        _release(assets, 'fleetmux', '9.9.9', core, b'core-' + core.encode())
        _release(assets, 'fleetmux-web', '9.9.9', web, b'web-' + web.encode())
    base = tmp_path / 'fleetmux-9.9.9-py3-none-any.whl'
    _base_wheel(base, '9.9.9')
    assert build_wheels.main(['--base', str(base), '--assets', str(assets), '--out', str(out)]) == 0
    whl = out / 'fleetmux-9.9.9-py3-none-macosx_11_0_arm64.whl'
    with zipfile.ZipFile(whl) as z:
        assert z.read('fleetmux/_bin/fleetmux') == b'core-aarch64-apple-darwin'
        assert z.read('fleetmux/_bin/fleetmux-web') == b'web-aarch64-apple-darwin'
        assert (z.getinfo('fleetmux/_bin/fleetmux').external_attr >> 16) & 0o777 == 0o755
        di = 'fleetmux-9.9.9.dist-info/'
        assert 'Tag: py3-none-macosx_11_0_arm64' in z.read(di + 'WHEEL').decode()
        assert 'Root-Is-Purelib: false' in z.read(di + 'WHEEL').decode()
        assert build_wheels.ENTRY in z.read(di + 'entry_points.txt').decode()
        rows = {r[0]: r for r in csv.reader(io.StringIO(z.read(di + 'RECORD').decode()))}
        assert set(rows) == set(z.namelist())
        for name in z.namelist():
            if name != di + 'RECORD':
                digest = base64.urlsafe_b64encode(hashlib.sha256(z.read(name)).digest()).rstrip(b'=').decode()
                assert rows[name][1] == 'sha256=' + digest
    assert len(list(out.glob('*.whl'))) == len(build_wheels.TARGETS)


def test_build_wheels_refuses_a_sha256_mismatch(tmp_path):
    assets = tmp_path / 'assets'
    assets.mkdir()
    for core, web, _tag in build_wheels.TARGETS:
        _release(assets, 'fleetmux', '9.9.9', core, b'x', sha_ok=False)
        _release(assets, 'fleetmux-web', '9.9.9', web, b'y')
    base = tmp_path / 'fleetmux-9.9.9-py3-none-any.whl'
    _base_wheel(base, '9.9.9')
    with pytest.raises(SystemExit, match='sha256 mismatch'):
        build_wheels.main(['--base', str(base), '--assets', str(assets), '--out', str(tmp_path / 'out')])


def test_launcher_points_the_binary_at_this_install(tmp_path, monkeypatch):
    bin_dir = tmp_path / '_bin'
    bin_dir.mkdir()
    binary = bin_dir / 'fleetmux'
    binary.write_text('#!/bin/sh\n')
    binary.chmod(0o755)
    scripts = tmp_path / 'bin'
    scripts.mkdir()
    cli = scripts / 'fleetmux-py'
    cli.write_text('#!/bin/sh\n')
    cli.chmod(0o755)
    monkeypatch.setattr(wheel_launcher, 'BIN_DIR', bin_dir)
    monkeypatch.setattr(sys, 'argv', [str(scripts / 'fleetmux'), 'status'])
    monkeypatch.delenv('FLEETMUX_PYTHON_CLI', raising=False)
    monkeypatch.setenv('FLEETMUX_BIN', '/kept')
    seen = {}
    monkeypatch.setattr(os, 'execv', lambda path, args: seen.update(path=path, args=args))
    wheel_launcher.main()
    assert seen == {'path': str(binary), 'args': [str(binary), 'status']}
    assert os.environ['FLEETMUX_PYTHON_CLI'] == str(cli.resolve())
    assert os.environ['FLEETMUX_BIN'] == '/kept'


def test_launcher_without_a_binary_exits_127(tmp_path, monkeypatch):
    monkeypatch.setattr(wheel_launcher, 'BIN_DIR', tmp_path / 'missing')
    assert wheel_launcher.main() == 127
