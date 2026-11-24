"""scripts/install.sh: the one-command user install. Source from a local tarball (FLEETMUX_SOURCE), binaries from a
local release tree (FLEETMUX_RELEASE_BASE=file://...), Python from uv; no GitHub access."""
import os
import shutil
import signal
import subprocess
import tarfile
from pathlib import Path

import pytest

from test_install_fleetmux import release_tree

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'install.sh'
VERSION = '9.9.9'


def source_tarball(path):
    """The checkout's tracked files under one top-level directory, as GitHub's tag archives are laid out."""
    files = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True).stdout.split(b'\0')
    with tarfile.open(path, 'w:gz') as tar:
        for name in filter(None, (f.decode() for f in files)):
            if (ROOT / name).exists() and not name.startswith('rust/'):
                tar.add(ROOT / name, 'fleetmux-%s/%s' % (VERSION, name), recursive=False)
    return path


def run(tmp_path, **env):
    # UV_PYTHON_DOWNLOADS=never: the Python comes from the host. A host without it fails in seconds with
    # "uv cannot provide Python", instead of downloading CPython from GitHub into the temp HOME.
    full = dict(os.environ, HOME=str(tmp_path / 'home'), FLEETMUX_VERSION=VERSION, UV_PYTHON_DOWNLOADS='never',
                FLEETMUX_RELEASES=str(tmp_path / 'releases'), FLEETMUX_BIN_DIR=str(tmp_path / 'bin'), **env)
    full.pop('DISPLAY', None)
    full.pop('WAYLAND_DISPLAY', None)
    # Own process group: a timeout kills install.sh and its uv / curl children, not only bash.
    proc = subprocess.Popen(['bash', str(SCRIPT)], env=full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=600)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def test_a_failed_install_removes_its_release_dir_and_links_nothing(tmp_path):
    bad = tmp_path / 'broken.tgz'
    bad.write_bytes(b'not a tarball')
    done = run(tmp_path, FLEETMUX_SOURCE=str(bad))
    assert done.returncode != 0
    assert not (tmp_path / 'releases' / ('v' + VERSION)).exists()
    assert not (tmp_path / 'bin' / 'fleetmux').exists()


def test_a_local_source_needs_an_explicit_version(tmp_path):
    env = dict(os.environ, FLEETMUX_SOURCE=str(tmp_path / 'x.tgz'), FLEETMUX_RELEASES=str(tmp_path / 'r'))
    env.pop('FLEETMUX_VERSION', None)
    done = subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 1 and 'FLEETMUX_VERSION' in done.stderr


@pytest.mark.skipif(not shutil.which('uv'), reason='needs uv (it provides the Python of deps.yaml)')
def test_install_builds_a_release_the_fleetmux_binary_can_forward_to(tmp_path):
    www = tmp_path / 'www'
    release_tree(www, VERSION)  # only the release's own binaries: the deps.yaml floor is not on offer
    done = run(tmp_path, FLEETMUX_SOURCE=str(source_tarball(tmp_path / 'src.tgz')),
               FLEETMUX_RELEASE_BASE='file://' + str(www))
    assert done.returncode == 0, done.stdout + done.stderr
    rel = tmp_path / 'releases' / ('v' + VERSION)
    # forward.rs: a release dir holds .venv/bin/python + fleetmux/fleet.py, and the link resolves into its bin/
    assert (rel / '.venv/bin/python').exists() and (rel / 'fleetmux/fleet.py').exists()
    assert (tmp_path / 'bin' / 'fleetmux').resolve() == (rel / 'bin' / 'fleetmux').resolve()
    got = subprocess.run([str(rel / 'bin' / 'fleetmux'), '--version'], capture_output=True, text=True, timeout=20).stdout
    assert got.split()[:2] == ['fleetmux', VERSION]  # the binary of the release installed, not programs.fleetmux.min
    assert oct((rel / '.runtime' / 'redis').stat().st_mode & 0o777) == '0o700'
    cli = subprocess.run([str(rel / '.venv/bin/python'), str(rel / 'fleetmux/fleet.py'), 'start', '--help'],
                         capture_output=True, text=True, timeout=120)
    assert cli.returncode == 0 and 'usage: fleetmux start' in cli.stdout
    assert 'dependency check' in done.stdout

    again = run(tmp_path, FLEETMUX_SOURCE=str(tmp_path / 'src.tgz'), FLEETMUX_RELEASE_BASE='file://' + str(www))
    assert again.returncode == 0 and 'already installed' in again.stdout
