"""docker/install_agents.py: pinned agent CLIs land in the agents prefix; a bad checksum installs nothing."""
import hashlib
import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('install_agents', ROOT / 'docker/install_agents.py')
install_agents = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install_agents)

EXE = b'#!/bin/sh\necho "tool 2.2.5"\n'


def tarball(path):
    with tarfile.open(path, 'w:gz') as tar:
        info = tarfile.TarInfo('tool')
        info.size, info.mode = len(EXE), 0o755
        tar.addfile(info, io.BytesIO(EXE))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(install_agents, 'PREFIX', tmp_path / 'agents')
    monkeypatch.setattr(install_agents.platform, 'machine', lambda: 'x86_64')
    return tmp_path / 'agents'


def pin(tmp_path, sha):
    return {'version': '2.2.5', 'url': 'file://' + str(tmp_path / 'tool-{version}-{arch}.tgz'), 'sha256': {'x64': sha}}


def test_tarball_with_the_pinned_checksum_is_installed_and_then_left_alone(tmp_path, prefix):
    sha = tarball(tmp_path / 'tool-2.2.5-x64.tgz')
    install_agents.install_tarball('tool', pin(tmp_path, sha))
    exe = prefix / 'bin' / 'tool'
    assert exe.read_bytes() == EXE and exe.stat().st_mode & 0o777 == 0o755
    assert install_agents.at_pin('tool', {'cmd': ['tool', '--version']}, pin(tmp_path, sha))


def test_checksum_mismatch_installs_nothing(tmp_path, prefix):
    tarball(tmp_path / 'tool-2.2.5-x64.tgz')
    with pytest.raises(ValueError, match='sha256 mismatch'):
        install_agents.install_tarball('tool', pin(tmp_path, '0' * 64))
    assert not (prefix / 'bin' / 'tool').exists()


def test_main_installs_only_what_is_not_at_its_pin(tmp_path, prefix, monkeypatch):
    calls = []
    monkeypatch.setattr(install_agents, 'at_pin', lambda name, item, p: name == 'claude')
    monkeypatch.setattr(install_agents, 'install_npm', lambda p: calls.append(p['npm']))
    monkeypatch.setattr(install_agents, 'install_tarball', lambda name, p: calls.append(name))
    monkeypatch.setattr(install_agents.check_deps, 'main', lambda argv: 0)
    assert install_agents.main() == 0
    assert calls == ['@openai/codex', '@earendil-works/pi-coding-agent', 'opencode']
