#!/usr/bin/env python3
"""Download the agent CLIs pinned in deps.yaml `agent_installs` into the container, then check deps.yaml.

The image carries fleetmux but not the agent CLIs. `fleetmux-docker up` runs this inside the container: the CLIs land
in the agents volume ($FLEETMUX_AGENTS, default /opt/agents), which survives `down` / `up` and image upgrades, so they
are downloaded once. A CLI already at its pinned version is left alone; after a pin changes, `up` installs the new
one. Exit 0 when every pinned CLI is in place and deps.yaml is satisfied, 1 otherwise."""
import hashlib
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_deps  # noqa: E402

PREFIX = Path(os.environ.get('FLEETMUX_AGENTS', '/opt/agents'))
ARCH = {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}


def at_pin(name, item, pin):
    exe = PREFIX / 'bin' / name
    have = check_deps.probe([str(exe)] + item['cmd'][1:]) if exe.exists() else None
    need = check_deps.version(pin['version'])
    return have is not None and have[:len(need)] == need


def install_npm(pin):
    subprocess.run(['npm', 'install', '-g', '--prefix', str(PREFIX), '--cache', str(PREFIX / '.npm-cache'),
                    '--no-fund', '--no-audit', '%s@%s' % (pin['npm'], pin['version'])], check=True)


def install_tarball(name, pin):
    arch = ARCH[platform.machine()]
    url = pin['url'].format(version=pin['version'], arch=arch)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'download.tar.gz'
        urllib.request.urlretrieve(url, path)
        got = hashlib.sha256(path.read_bytes()).hexdigest()
        if got != pin['sha256'][arch]:
            raise ValueError('sha256 mismatch for %s: got %s, deps.yaml pins %s' % (url, got, pin['sha256'][arch]))
        with tarfile.open(path) as tar:
            tar.extract(tar.getmember(name), Path(tmp) / 'out', filter='data')
        (PREFIX / 'bin').mkdir(parents=True, exist_ok=True)
        new = PREFIX / 'bin' / ('.%s.new' % name)
        new.write_bytes((Path(tmp) / 'out' / name).read_bytes())
        new.chmod(0o755)
        new.replace(PREFIX / 'bin' / name)


def main():
    spec = yaml.safe_load((ROOT / 'deps.yaml').read_text())
    failed = []
    for name, pin in spec['agent_installs'].items():
        if at_pin(name, spec['programs'][name], pin):
            print('ok       %s %s' % (name, pin['version']))
            continue
        print('install  %s %s' % (name, pin['version']), flush=True)
        try:
            install_npm(pin) if 'npm' in pin else install_tarball(name, pin)
        except (OSError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
            failed.append(name)
            print('FAIL     %s: %s' % (name, exc), file=sys.stderr)
    os.environ.pop(check_deps.DEFER_ENV, None)  # the full check, CLIs included
    rc = check_deps.main([])
    if failed:
        print('not installed: ' + ', '.join(failed), file=sys.stderr)
    return 1 if failed or rc else 0


if __name__ == '__main__':
    sys.exit(main())
