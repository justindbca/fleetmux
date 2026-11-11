"""scripts/install_fleetmux.sh (brief-m03 §3.5, B19 ci-release): the prebuilt fleetmux binary pinned by
deps.yaml, downloaded from the release base and checked against its sha256 before it is installed.

The contract these tests pin (B19 implements it):

- the version is ``programs.fleetmux.min`` of the repo's own ``deps.yaml``;
- the host's target triple comes from ``uname -s`` / ``uname -m``: Darwin arm64 -> ``aarch64-apple-darwin``,
  Darwin x86_64 -> ``x86_64-apple-darwin``, Linux x86_64 -> ``x86_64-unknown-linux-musl``, Linux aarch64 ->
  ``aarch64-unknown-linux-musl`` (the four release targets, design §8 item 5);
- ``$FLEETMUX_RELEASE_BASE/v<ver>/fleetmux-<ver>-<triple>.tar.gz`` holds one top-level ``fleetmux``
  executable; ``<same URL>.sha256`` is one ``sha256sum`` line ``<hex>  fleetmux-<ver>-<triple>.tar.gz``;
- ``--dest DIR`` installs ``DIR/fleetmux`` (executable) and exits 0; a sha256 mismatch exits non-zero,
  says ``sha256`` on stderr and leaves nothing in ``DIR`` (no binary, no partial download);
- ``--print-target`` prints only the triple, exits 0 and touches no network.

Stage A: the script is a stub (``exit 2``), so all three are red until B19.
"""
import functools
import hashlib
import http.server
import io
import os
import subprocess
import tarfile
import threading
from pathlib import Path

import pytest
import yaml

from _fleetmux_milestone import milestone

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'install_fleetmux.sh'
TRIPLES = {('Darwin', 'arm64'): 'aarch64-apple-darwin', ('Darwin', 'x86_64'): 'x86_64-apple-darwin',
           ('Linux', 'x86_64'): 'x86_64-unknown-linux-musl', ('Linux', 'aarch64'): 'aarch64-unknown-linux-musl'}


def pinned_version():
    return str(yaml.safe_load((ROOT / 'deps.yaml').read_text())['programs']['fleetmux']['min'])


def fake_uname(bin_dir, system, machine):
    """A ``uname`` on PATH that answers ``-s`` / ``-m`` (and both at once) for the given host."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / 'uname'
    path.write_text('#!/bin/sh\nout=\nfor a in "$@"; do case "$a" in\n'
                    '  -s) out="$out %s" ;; -m) out="$out %s" ;; -sm|-ms) out="$out %s %s" ;;\n'
                    'esac; done\necho ${out:-%s}\n' % (system, machine, system, machine, system))
    path.chmod(0o755)
    return bin_dir


def release_tree(root, version, good_sha=True, extra=None):
    """``root/v<ver>/fleetmux-<ver>-<triple>.tar.gz`` + ``.sha256`` for every target; each tarball's
    ``fleetmux`` prints ``fleetmux <ver> (protocol 1)`` plus its triple, so the test sees which one landed."""
    out = root / ('v' + version)
    out.mkdir(parents=True)
    for triple in TRIPLES.values():
        name = 'fleetmux-%s-%s.tar.gz' % (version, triple)
        body = ('#!/bin/sh\necho "fleetmux %s (protocol 1) %s"\n' % (version, triple)).encode()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tar:
            info = tarfile.TarInfo('fleetmux')
            info.size, info.mode = len(body), 0o755
            tar.addfile(info, io.BytesIO(body))
            for member, text in (extra or {}).items():  # e.g. LICENSE / THIRD-PARTY-LICENSES (release.yml)
                info = tarfile.TarInfo(member)
                info.size = len(text.encode())
                tar.addfile(info, io.BytesIO(text.encode()))
        data = buf.getvalue()
        (out / name).write_bytes(data)
        digest = hashlib.sha256(data if good_sha else data + b'tampered').hexdigest()
        (out / (name + '.sha256')).write_text(digest + '  ' + name + '\n')


@pytest.fixture
def release_server(tmp_path):
    """Serve ``tmp_path/www`` on 127.0.0.1; yields ``(base_url, www_dir, requested_paths)``."""
    www = tmp_path / 'www'
    www.mkdir()
    seen = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            super().do_GET()

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(www)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:%d' % httpd.server_address[1], www, seen
    finally:
        httpd.shutdown()
        httpd.server_close()


def run_script(args, env_extra, bin_dir=None):
    env = dict(os.environ, no_proxy='127.0.0.1,localhost', NO_PROXY='127.0.0.1,localhost', **env_extra)
    if bin_dir is not None:
        env['PATH'] = str(bin_dir) + os.pathsep + env['PATH']
    return subprocess.run(['bash', str(SCRIPT)] + args, capture_output=True, text=True, env=env, timeout=120)


@milestone(2)
def test_install_downloads_pinned_version_from_deps_yaml_and_verifies_sha256(tmp_path, release_server):
    base, www, seen = release_server
    version = pinned_version()
    release_tree(www, version)
    dest = tmp_path / 'dest'
    dest.mkdir()
    bin_dir = fake_uname(tmp_path / 'bin', 'Darwin', 'arm64')
    done = run_script(['--dest', str(dest)], {'FLEETMUX_RELEASE_BASE': base}, bin_dir)
    assert done.returncode == 0, done.stderr
    binary = dest / 'fleetmux'
    assert binary.is_file() and os.access(binary, os.X_OK)
    got = subprocess.run([str(binary), '--version'], capture_output=True, text=True, timeout=20).stdout
    assert got.split()[:2] == ['fleetmux', version]  # the deps.yaml floor, not "latest"
    assert 'aarch64-apple-darwin' in got  # the host's tarball
    name = '/v%s/fleetmux-%s-aarch64-apple-darwin.tar.gz' % (version, version)
    assert name in seen and name + '.sha256' in seen
    assert sorted(p.name for p in dest.iterdir()) == ['fleetmux']  # no tarball / temp left behind


@milestone(2)
def test_install_takes_only_the_binary_from_a_tarball_with_licence_notices(tmp_path, release_server):
    """release.yml ships LICENSE + THIRD-PARTY-LICENSES next to `fleetmux` (design §8 item 12); the installer
    still drops only the executable into --dest."""
    base, www, seen = release_server
    version = pinned_version()
    release_tree(www, version, extra={'LICENSE': 'MIT License\n', 'THIRD-PARTY-LICENSES': 'alacritty_terminal\n'})
    dest = tmp_path / 'dest'
    dest.mkdir()
    bin_dir = fake_uname(tmp_path / 'bin', 'Linux', 'x86_64')
    done = run_script(['--dest', str(dest)], {'FLEETMUX_RELEASE_BASE': base}, bin_dir)
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in dest.iterdir()) == ['fleetmux']
    got = subprocess.run([str(dest / 'fleetmux'), '--version'], capture_output=True, text=True, timeout=20).stdout
    assert got.split()[:2] == ['fleetmux', version]


@milestone(2)
def test_install_refuses_sha256_mismatch_and_leaves_no_binary(tmp_path, release_server):
    base, www, seen = release_server
    version = pinned_version()
    release_tree(www, version, good_sha=False)
    dest = tmp_path / 'dest'
    dest.mkdir()
    bin_dir = fake_uname(tmp_path / 'bin', 'Linux', 'x86_64')
    done = run_script(['--dest', str(dest)], {'FLEETMUX_RELEASE_BASE': base}, bin_dir)
    assert done.returncode not in (0, 2), (done.returncode, done.stderr)  # 2 = usage / the stage-A stub
    assert 'sha256' in done.stderr.lower()
    assert any(p.endswith('x86_64-unknown-linux-musl.tar.gz') for p in seen)  # it did download, then refused
    assert list(dest.iterdir()) == []  # no binary, no partial download


@milestone(2)
def test_install_picks_target_triple_for_host(tmp_path):
    for (system, machine), triple in TRIPLES.items():
        bin_dir = fake_uname(tmp_path / (system + '-' + machine), system, machine)
        # a dead release base: --print-target must not need the network
        done = run_script(['--print-target'], {'FLEETMUX_RELEASE_BASE': 'http://127.0.0.1:9'}, bin_dir)
        assert done.returncode == 0, (system, machine, done.stderr)
        assert done.stdout.strip() == triple, (system, machine)


# ---------------------------------------------------------------- fleetmux-web (brief-m25 §3.6 W5)

WEB_TRIPLES = {('Darwin', 'arm64'): 'aarch64-apple-darwin', ('Linux', 'x86_64'): 'x86_64-unknown-linux-gnu'}


def web_release(root, version):
    """``root/v<ver>/fleetmux-web-<ver>-<triple>.tar.gz`` + ``.sha256`` (gnu on Linux, design §3.5.9): one
    top-level ``fleetmux-web`` that prints its version line and triple."""
    out = root / ('v' + version)
    out.mkdir(parents=True, exist_ok=True)
    for triple in WEB_TRIPLES.values():
        name = 'fleetmux-web-%s-%s.tar.gz' % (version, triple)
        body = ('#!/bin/sh\necho "fleetmux-web %s (protocol 1) %s"\n' % (version, triple)).encode()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tar:
            info = tarfile.TarInfo('fleetmux-web')
            info.size, info.mode = len(body), 0o755
            tar.addfile(info, io.BytesIO(body))
        data = buf.getvalue()
        (out / name).write_bytes(data)
        (out / (name + '.sha256')).write_text(hashlib.sha256(data).hexdigest() + '  ' + name + '\n')


def run_headless_aware(args, base, bin_dir, display):
    env = {k: v for k, v in os.environ.items() if k not in ('DISPLAY', 'WAYLAND_DISPLAY')}
    env.update(no_proxy='127.0.0.1,localhost', NO_PROXY='127.0.0.1,localhost', FLEETMUX_RELEASE_BASE=base)
    if display:
        env['DISPLAY'] = ':0'
    env['PATH'] = str(bin_dir) + os.pathsep + env['PATH']
    return subprocess.run(['bash', str(SCRIPT)] + args, capture_output=True, text=True, env=env, timeout=120)


@milestone(3)
def test_install_fetches_fleetmux_web_on_desktop_and_skips_headless(tmp_path, release_server):
    base, www, seen = release_server
    version = pinned_version()
    release_tree(www, version)
    web_release(www, version)
    web = '/v%s/fleetmux-web-%s-x86_64-unknown-linux-gnu.tar.gz' % (version, version)
    # Linux desktop session: both binaries, the web one checked against its sha256.
    dest = tmp_path / 'desk'
    dest.mkdir()
    bin_dir = fake_uname(tmp_path / 'bin-l', 'Linux', 'x86_64')
    done = run_headless_aware(['--dest', str(dest)], base, bin_dir, display=True)
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in dest.iterdir()) == ['fleetmux', 'fleetmux-web']
    got = subprocess.run([str(dest / 'fleetmux-web'), '--version'], capture_output=True, text=True, timeout=20).stdout
    assert got.split()[:2] == ['fleetmux-web', version] and 'x86_64-unknown-linux-gnu' in got
    assert web in seen and web + '.sha256' in seen
    # Headless Linux: only fleetmux, no request for fleetmux-web, and one line saying why.
    seen.clear()
    dest = tmp_path / 'headless'
    dest.mkdir()
    done = run_headless_aware(['--dest', str(dest)], base, bin_dir, display=False)
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in dest.iterdir()) == ['fleetmux']
    assert not any('fleetmux-web' in p for p in seen)
    assert 'fleetmux-web' in done.stdout + done.stderr
    # macOS is always a desktop session.
    dest = tmp_path / 'mac'
    dest.mkdir()
    done = run_headless_aware(['--dest', str(dest)], base, fake_uname(tmp_path / 'bin-m', 'Darwin', 'arm64'),
                              display=False)
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in dest.iterdir()) == ['fleetmux', 'fleetmux-web']


@milestone(3)
def test_install_refuses_fleetmux_web_sha256_mismatch_and_keeps_existing_web(tmp_path, release_server):
    """Supervisor M99 ruling 1 (W5): a fleetmux-web whose sha256 does not match is not installed, stderr says so,
    exit 1; an earlier DEST/fleetmux-web is left exactly as it was, and the verified fleetmux stays installed."""
    base, www, seen = release_server
    version = pinned_version()
    release_tree(www, version)
    web_release(www, version)
    name = 'fleetmux-web-%s-x86_64-unknown-linux-gnu.tar.gz' % version
    checksum = www / ('v' + version) / (name + '.sha256')
    checksum.write_text('0' * 64 + '  ' + name + '\n')  # this test's own tampered fixture
    dest = tmp_path / 'dest'
    dest.mkdir()
    earlier = b'#!/bin/sh\necho "fleetmux-web 0.0.1 (protocol 1) earlier"\n'
    (dest / 'fleetmux-web').write_bytes(earlier)
    bin_dir = fake_uname(tmp_path / 'bin', 'Linux', 'x86_64')
    done = run_headless_aware(['--dest', str(dest)], base, bin_dir, display=True)
    assert done.returncode == 1, (done.returncode, done.stderr)
    assert 'sha256' in done.stderr and 'fleetmux-web' in done.stderr
    assert (dest / 'fleetmux-web').read_bytes() == earlier  # not removed, not replaced
    assert sorted(p.name for p in dest.iterdir()) == ['fleetmux', 'fleetmux-web']  # no staged temp left
    got = subprocess.run([str(dest / 'fleetmux'), '--version'], capture_output=True, text=True, timeout=20).stdout
    assert got.split()[:2] == ['fleetmux', version]


# ---------------------------------------------------------------- M2.6 xterm.js notices (brief-m26 §3.6 W5, design §3.6.9)

@milestone(3)
def test_release_appends_xterm_notices_to_fleetmux_web():
    """release.yml: the fleetmux-web tarball's THIRD-PARTY-LICENSES (``cargo about`` output) gets the vendored
    xterm.js notices appended (``cat …/THIRD-PARTY-JS.txt >> stage/THIRD-PARTY-LICENSES``) and checked
    (``grep -q '@xterm/xterm 6.0.0'``), both after its ``cargo about generate`` and before its ``tar``; the
    ``fleetmux`` tarball's notices stay as they are."""
    lines = (ROOT / '.github' / 'workflows' / 'release.yml').read_text().splitlines()

    def first(pred, start=0):
        return next(i for i in range(start, len(lines)) if pred(lines[i]))

    web_about = first(lambda l: 'cargo about generate' in l and 'crates/web/Cargo.toml' in l)
    web_tar = first(lambda l: 'tar ' in l and 'fleetmux-web LICENSE THIRD-PARTY-LICENSES' in l, web_about)
    web_part = lines[web_about:web_tar]
    cat = [i for i, l in enumerate(web_part)
           if 'cat rust/crates/web/assets/xterm/THIRD-PARTY-JS.txt' in l and '>> stage/THIRD-PARTY-LICENSES' in l]
    grep = [i for i, l in enumerate(web_part)
            if "grep -q '@xterm/xterm 6.0.0' stage/THIRD-PARTY-LICENSES" in l]
    assert len(cat) == 1 and len(grep) == 1 and cat[0] < grep[0], web_part
    app_about = first(lambda l: 'cargo about generate' in l and 'crates/app/Cargo.toml' in l)
    app_tar = first(lambda l: 'tar ' in l and 'fleetmux LICENSE THIRD-PARTY-LICENSES' in l, app_about)
    assert not any('THIRD-PARTY-JS' in l or '@xterm' in l for l in lines[app_about:app_tar])
