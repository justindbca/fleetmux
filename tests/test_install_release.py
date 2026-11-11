"""scripts/install_release.sh: one release = one application SHA, and the installer that lands it is a separate
tool identity.

The contract these tests pin (M50, Linux/UTM delivery):

- the old CLI stays exactly `<sha> <tarball> [--test] [--activate]`; `FLEETMUX_PYTHON` still wins;
- the interpreter must satisfy `deps.yaml python.min` (3.14): a host with only `python3.12` stops with a message
  instead of silently building the release venv on 3.12, while `python3.14` on PATH or `$HOME/.local/bin/python3.14`
  is picked up (UTM: `/home/user/.local/bin/python3.14` = 3.14.10);
- `--bin-dir DIR` (an already-built bin dir of the same application SHA: `fleetmux`, `fleetmux-web`,
  `SHA256SUMS`, `manifest.json`) installs both binaries into `<release>/bin`, each checked against its
  `SHA256SUMS` line, and refuses when the manifest's `source_sha` is not the `<sha>` being installed;
- `<release>/bin` comes first on PATH for `scripts/check_deps.py`, so the dependency check sees the release's own
  verified binaries, not whatever `~/.local/bin` happens to link to;
- `installed.json` records the application `source_sha`, the separate installer `tool_sha`, the two binary
  hashes/sizes and the installer's own hash — and says it is not a native PASS / receipt;
- every failure path (mismatch, missing file, failed dependency check) exits non-zero, leaves `current` alone and
  does not leave a half-installed release behind;
- a `--test` failure keeps the complete suite log outside the release dir and prints its path (P1-01);
- `<sha>` is validated as hex and the release dir is created with a bare `mkdir`, so cleanup only ever removes the
  directory this run created and a non-hex `<sha>` can never reach outside `$RELEASES` (P1-02);
- `manifest.json` is parsed as JSON and `installed.json` is written by `json.dump` and re-parsed, so quotes /
  backslashes in flags and paths stay valid JSON (记录-01/02);
- success prints the explicit start command with `<release>/bin` first on PATH (console script first, `python -m`
  fallback) and records it as `start_command` (P1-03);
- only `--activate` moves `~/fleetmux-current`, and an existing release dir is never overwritten.

The tests need no network, no root and no real UTM box: `python3.14` and the release `.venv` are shell stubs.
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'install_release.sh'
APP_SHA = '35670632e0b923e56dc4fa357189711dbd39328a'
OTHER_SHA = '0123456789abcdef0123456789abcdef01234567'
TOOL_SHA = 'fedcba9876543210fedcba9876543210fedcba98'
FLEETMUX = b'#!/bin/sh\necho "fleetmux 0.1.0 (protocol 1)"\n'
FLEETMUX_WEB = b'#!/bin/sh\necho "fleetmux-web 0.1.0 (protocol 1)"\n'


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_exe(path, text):
    path.write_text(text)
    path.chmod(0o755)
    return path


PY_TEMPLATE = '''#!/bin/sh
[ -n "$FAKE_PY_LOG" ] && printf 'PY=%s ARGS=%s\\n' "$0" "$*" >> "$FAKE_PY_LOG"
case "$1" in
  -c)
    # the version probe needs no real work; a JSON snippet (the installed.json re-parse) runs on the real interpreter
    case "$2" in
      *json*) code=$2; shift 2; exec "${FAKE_REAL_PY:?}" -c "$code" "$@" ;;
    esac
    exit __RC__ ;;
  -)
    shift; exec "${FAKE_REAL_PY:?}" - "$@" ;;
  -m)
    [ "$2" = venv ] || exit 0
    [ -n "$FAKE_PY_MARK" ] && : > "$FAKE_PY_MARK"
    d=$3
    mkdir -p "$d/bin"
    cat > "$d/bin/python" <<'PY'
#!/bin/sh
if [ "$1" = "-c" ]; then
  case "$2" in
    *json*) code=$2; shift 2; exec "${FAKE_REAL_PY:?}" -c "$code" "$@" ;;
  esac
  exit 0
fi
[ -n "$FAKE_PY_LOG" ] && printf 'PATH=%s ARGS=%s\\n' "$PATH" "$*" >> "$FAKE_PY_LOG"
case "$*" in
  *pytest*)
    if [ "${FAKE_TEST_RC:-0}" = 0 ]; then echo "3 passed";
    else echo "collected 3 items";
         echo "FAILED tests/test_native_anchor.py::test_anchor - AssertionError: anchor moved";
         echo "1 failed, 2 passed"; fi
    exit "${FAKE_TEST_RC:-0}" ;;
esac
exit "${FAKE_CHECK_DEPS_RC:-0}"
PY
    printf '#!/bin/sh\\nexit 0\\n' > "$d/bin/pip"
    chmod 755 "$d/bin/python" "$d/bin/pip"
    if [ -z "${FAKE_NO_ENTRY:-}" ]; then
      printf '#!/bin/sh\\nprintf \"ENTRY_PATH=%%s\\\\n\" \"$PATH\"\\n' > "$d/bin/fleetmux-py"; chmod 755 "$d/bin/fleetmux-py"
    fi
    exit 0 ;;
esac
exit 0
'''


def make_python(bin_dir, name='python3.14', version_ok=True):
    """A stub host bin dir: `uname` says Linux (so the macOS-only candidates are skipped on every test host) and
    the interpreter stub answers the version check with `-c` and writes a stub .venv on `-m venv`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    uname = bin_dir / 'uname'
    if not uname.exists():
        write_exe(uname, '#!/bin/sh\ncase "$1" in -s) echo Linux ;; -m) echo aarch64 ;; *) echo Linux ;; esac\n')
    return write_exe(bin_dir / name, PY_TEMPLATE.replace('__RC__', '0' if version_ok else '1'))


@pytest.fixture
def world(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    tarball = tmp_path / 'src.tgz'
    with tarfile.open(tarball, 'w:gz') as tar:
        for name, text in (('pyproject.toml', '[project]\nname = "fleetmux"\n'),
                           ('requirements.txt', 'redis==7.3.0\n'),
                           ('requirements-dev.txt', 'pytest\n'),
                           ('scripts/check_deps.py', 'raise SystemExit(0)\n')):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return {'tmp': tmp_path, 'home': home, 'releases': tmp_path / 'releases', 'tarball': tarball,
            'bin': tmp_path / 'fakebin', 'log': tmp_path / 'python.log'}


def make_bin_dir(path, source_sha=APP_SHA, manifest_sha=None, tamper=None, drop=(), manifest_text=None):
    """An already-built `--bin-dir`: two executables, SHA256SUMS and the build manifest of one source SHA."""
    path.mkdir(parents=True, exist_ok=True)
    if 'fleetmux' not in drop:
        write_exe(path / 'fleetmux', FLEETMUX.decode())
    if 'fleetmux-web' not in drop:
        write_exe(path / 'fleetmux-web', FLEETMUX_WEB.decode())
    sums = {}
    for name, data in (('fleetmux', FLEETMUX), ('fleetmux-web', FLEETMUX_WEB)):
        if name not in drop:
            sums[name] = sha256(data if tamper != name else data + b'tampered')
    if 'SHA256SUMS' not in drop:
        (path / 'SHA256SUMS').write_text(''.join('%s  %s\n' % (sums[n], n) for n in sums))
    if 'manifest.json' not in drop:
        manifest = {'label': 'final', 'platform': 'utm-linux', 'source_sha': manifest_sha or source_sha,
                    'same_sha_build': True,
                    'binaries': {n: {'sha256': sums[n], 'bytes': len(FLEETMUX if n == 'fleetmux' else FLEETMUX_WEB)}
                                 for n in sums}}
        (path / 'manifest.json').write_text(manifest_text if manifest_text is not None
                                            else json.dumps(manifest, indent=1) + '\n')
    return path


HOST_DIRS = ('/usr/bin', '/bin')


def host_tools(world):
    """The host's /usr/bin and /bin as one dir of symlinks, minus every `python*`: behind the test's own PATH the
    installer must find no interpreter at all. A host python3.14 there (Ubuntu 26.04 ships /usr/bin/python3.14)
    would otherwise be picked up and turn the 'only python3.12' case into a real install."""
    tools = world['tmp'] / 'host-tools'
    if not tools.is_dir():
        tools.mkdir()
        for d in HOST_DIRS:
            for entry in os.scandir(d):
                link = tools / entry.name
                if not entry.name.startswith('python') and not os.path.lexists(link):
                    link.symlink_to(entry.path)
    return tools


def run(world, args, env_extra=None, path=None, python=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ('FLEETMUX_PYTHON', 'FLEETMUX_RELEASES', 'FLEETMUX_TOOL_SHA')}
    env.update(HOME=str(world['home']), FLEETMUX_RELEASES=str(world['releases']),
               FAKE_PY_LOG=str(world['log']), FAKE_REAL_PY=sys.executable)
    env.update(env_extra or {})
    if python is not None:
        env['FLEETMUX_PYTHON'] = str(python)
    env['PATH'] = (path if path is not None else str(world['bin'])) + os.pathsep + str(host_tools(world))
    return subprocess.run(['bash', str(SCRIPT)] + [str(a) for a in args], capture_output=True, text=True,
                          env=env, timeout=120)


def logged(world):
    text = world['log'].read_text() if world['log'].exists() else ''
    return [line for line in text.splitlines() if line]


def check_deps_path(world):
    """The PATH the stub `.venv/bin/python` saw when the installer ran `scripts/check_deps.py`."""
    line = next(l for l in logged(world) if 'scripts/check_deps.py' in l)
    return line.split('PATH=', 1)[1].split(' ARGS=', 1)[0]


def release(world, sha=APP_SHA):
    return world['releases'] / sha


def test_install_without_bin_dir_keeps_the_old_contract(world):
    make_python(world['bin'])
    done = run(world, [APP_SHA, world['tarball']])
    assert done.returncode == 0, done.stderr
    rel = release(world)
    assert (rel / '.venv' / 'bin' / 'python').exists()  # the stub .venv the installer made
    assert (rel / '.runtime').is_dir() and (rel / '.runtime').stat().st_mode & 0o777 == 0o700
    assert not (rel / 'bin').exists()  # no --bin-dir: nothing invented
    assert not (world['home'] / 'fleetmux-current').exists()  # --activate is opt-in
    assert not check_deps_path(world).startswith(str(rel / 'bin'))


def test_install_verifies_and_installs_binaries_with_provenance(world):
    make_python(world['bin'])
    built = make_bin_dir(world['tmp'] / 'built')
    done = run(world, [APP_SHA, world['tarball'], '--bin-dir', built, '--tool-sha', TOOL_SHA])
    assert done.returncode == 0, done.stderr
    rel = release(world)
    assert (rel / 'bin' / 'fleetmux').read_bytes() == FLEETMUX
    assert (rel / 'bin' / 'fleetmux-web').read_bytes() == FLEETMUX_WEB
    assert os.access(rel / 'bin' / 'fleetmux', os.X_OK)
    assert (rel / 'bin' / 'manifest.json').read_bytes() == (built / 'manifest.json').read_bytes()
    assert (rel / 'bin' / 'SHA256SUMS').read_bytes() == (built / 'SHA256SUMS').read_bytes()
    assert check_deps_path(world).startswith(str(rel / 'bin') + os.pathsep)  # the release's own binaries first
    installed = json.loads((rel / 'bin' / 'installed.json').read_text())
    assert installed['release_sha'] == APP_SHA and installed['source_sha'] == APP_SHA
    assert installed['tool_sha'] == TOOL_SHA  # tool commit != application SHA
    assert installed['binaries']['fleetmux'] == {'sha256': sha256(FLEETMUX), 'bytes': len(FLEETMUX)}
    assert installed['binaries']['fleetmux-web'] == {'sha256': sha256(FLEETMUX_WEB), 'bytes': len(FLEETMUX_WEB)}
    assert installed['installer_sha256'] == sha256(SCRIPT.read_bytes())
    assert installed['python'].endswith('python3.14')
    assert 'PASS' in installed['note'] and 'receipt' in installed['note']  # not an acceptance claim
    assert not (world['home'] / 'fleetmux-current').exists()


@pytest.mark.parametrize('tamper, drop, needle', [
    ('fleetmux-web', (), 'sha256'),
    ('fleetmux', (), 'sha256'),
    (None, ('fleetmux-web',), 'fleetmux-web'),
    (None, ('SHA256SUMS',), 'SHA256SUMS'),
    (None, ('manifest.json',), 'manifest.json'),
])
def test_install_refuses_unverifiable_binaries_before_creating_the_release(world, tamper, drop, needle):
    make_python(world['bin'])
    built = make_bin_dir(world['tmp'] / 'built', tamper=tamper, drop=drop)
    done = run(world, [APP_SHA, world['tarball'], '--bin-dir', built])
    assert done.returncode != 0
    assert needle in done.stderr, done.stderr
    assert not release(world).exists()  # nothing half-installed
    assert not (world['home'] / 'fleetmux-current').exists()


def test_install_refuses_a_manifest_for_another_source_sha(world):
    make_python(world['bin'])
    built = make_bin_dir(world['tmp'] / 'built', manifest_sha=OTHER_SHA)
    done = run(world, [APP_SHA, world['tarball'], '--bin-dir', built])
    assert done.returncode != 0
    assert 'source_sha' in done.stderr and OTHER_SHA in done.stderr
    assert not release(world).exists()


def test_install_takes_a_short_sha_that_prefixes_the_manifest_source_sha(world):
    # 2026-11-10: a 40-char release dir pushed macOS private Redis sockets past 103 bytes (19 start tests failed
    # in the release's own --test run); a short <sha> keeps the path short and must still bind to the build.
    make_python(world['bin'])
    built = make_bin_dir(world['tmp'] / 'built')
    done = run(world, [APP_SHA[:7].upper(), world['tarball'], '--bin-dir', built])
    assert done.returncode == 0, done.stderr
    installed = json.loads((release(world, APP_SHA[:7]) / 'bin' / 'installed.json').read_text())
    assert installed['release_sha'] == APP_SHA[:7] and installed['source_sha'] == APP_SHA


def test_install_refuses_a_short_sha_that_does_not_prefix_the_manifest(world):
    make_python(world['bin'])
    built = make_bin_dir(world['tmp'] / 'built', manifest_sha=OTHER_SHA)
    done = run(world, [APP_SHA[:7], world['tarball'], '--bin-dir', built])
    assert done.returncode != 0
    assert 'source_sha' in done.stderr and OTHER_SHA in done.stderr
    assert not release(world, APP_SHA[:7]).exists()


def test_install_refuses_an_existing_release_without_touching_it(world):
    make_python(world['bin'])
    rel = release(world)
    rel.mkdir(parents=True)
    (rel / 'marker').write_text('keep me\n')
    built = make_bin_dir(world['tmp'] / 'built')
    done = run(world, [APP_SHA, world['tarball'], '--bin-dir', built])
    assert done.returncode == 1
    assert 'release exists' in done.stderr
    assert sorted(p.name for p in rel.iterdir()) == ['marker']


def test_install_moves_current_only_with_activate(world):
    make_python(world['bin'])
    built = make_bin_dir(world['tmp'] / 'built')
    current = world['home'] / 'fleetmux-current'
    done = run(world, [APP_SHA, world['tarball'], '--bin-dir', built])
    assert done.returncode == 0, done.stderr
    assert not current.exists()  # installing alone never moves the default symlink
    other = make_bin_dir(world['tmp'] / 'built-other', source_sha=OTHER_SHA)
    done = run(world, [OTHER_SHA, world['tarball'], '--bin-dir', other, '--activate'])
    assert done.returncode == 0, done.stderr
    assert current.is_symlink() and Path(os.readlink(current)) == release(world, OTHER_SHA)
    assert str(release(world, OTHER_SHA)) in done.stdout and 'current: none ->' in done.stdout


def test_install_picks_python314_on_path_and_never_falls_back_to_312(world):
    python = make_python(world['bin'])
    done = run(world, [APP_SHA, world['tarball']])
    assert done.returncode == 0, done.stderr
    assert any(line.startswith('PY=%s' % python) for line in logged(world))

    # A host that only has 3.12 must stop, not build the release venv on 3.12.
    only312 = world['tmp'] / 'only312'
    make_python(only312, name='python3.12')
    world['log'].unlink(missing_ok=True)
    world['releases'].rename(world['tmp'] / 'releases-old')
    done = run(world, [APP_SHA, world['tarball']], path=str(only312))
    assert done.returncode != 0
    assert '3.14' in done.stderr and 'FLEETMUX_PYTHON' in done.stderr
    assert not release(world).exists()
    assert logged(world) == []  # the 3.12 stub was never even asked


def test_install_honours_the_fleetmux_python_override(world):
    custom = write_exe(world['tmp'] / 'custom-python', PY_TEMPLATE.replace('__RC__', '0'))
    marker = world['tmp'] / 'custom-used'
    done = run(world, [APP_SHA, world['tarball']], env_extra={'FAKE_PY_MARK': str(marker)}, python=custom)
    assert done.returncode == 0, done.stderr
    assert marker.exists()  # this interpreter built the venv
    assert any(line.startswith('PY=%s' % custom) for line in logged(world))

    bad = write_exe(world['tmp'] / 'old-python', PY_TEMPLATE.replace('__RC__', '1'))
    done = run(world, [OTHER_SHA, world['tarball']], python=bad)
    assert done.returncode != 0
    assert 'FLEETMUX_PYTHON' in done.stderr
    assert not release(world, OTHER_SHA).exists()


def test_failed_dependency_check_removes_the_partial_release(world):
    make_python(world['bin'])
    built = make_bin_dir(world['tmp'] / 'built')
    done = run(world, [APP_SHA, world['tarball'], '--bin-dir', built],
               env_extra={'FAKE_CHECK_DEPS_RC': '1'})
    assert done.returncode != 0
    assert 'deps.yaml not satisfied' in done.stderr
    assert not release(world).exists()
    assert not (world['home'] / 'fleetmux-current').exists()


def test_failed_suite_keeps_the_complete_log_outside_the_release(world):
    """P1-01: a red --test install used to delete its own log with the release dir."""
    make_python(world['bin'])
    built = make_bin_dir(world['tmp'] / 'built')
    done = run(world, [APP_SHA, world['tarball'], '--bin-dir', built, '--test'],
               env_extra={'FAKE_TEST_RC': '1'})
    assert done.returncode != 0
    assert 'full suite failed: not releasing' in done.stderr
    marker = 'complete test log kept at: '
    assert marker in done.stderr, done.stderr
    log = Path(done.stderr.split(marker, 1)[1].splitlines()[0].strip())
    rel = release(world)
    assert log.is_file()
    assert rel not in log.parents  # outside the dir the failure cleanup removes
    text = log.read_text()
    assert 'FAILED tests/test_native_anchor.py::test_anchor' in text and '1 failed, 2 passed' in text
    assert not rel.exists()  # the failed release is still removed ...
    assert log.is_file()     # ... and the whole log survives it


def test_a_passing_suite_prints_the_kept_log_path(world):
    make_python(world['bin'])
    done = run(world, [APP_SHA, world['tarball'], '--test'])
    assert done.returncode == 0, done.stderr
    marker = 'complete test log: '
    assert marker in done.stdout, done.stdout
    log = Path(done.stdout.split(marker, 1)[1].splitlines()[0].strip())
    assert log.parent == world['releases'] / '.logs'
    assert log.read_text().strip() == '3 passed'


def test_invalid_sha_is_refused_and_cannot_reach_outside_the_releases_dir(world):
    """P1-02: <sha> is the release dir name, so only hex is accepted (the old rm -rf reached ../canary)."""
    make_python(world['bin'])
    canary = world['tmp'] / 'canary'
    canary.mkdir()
    (canary / 'keep.txt').write_text('owner data\n')
    done = run(world, ['../canary', world['tarball']])
    assert done.returncode == 2
    assert 'invalid <sha>' in done.stderr
    assert (canary / 'keep.txt').read_text() == 'owner data\n'
    done = run(world, [APP_SHA + '/x', world['tarball']])
    assert done.returncode == 2
    assert not (world['releases'] / APP_SHA).exists()


def test_short_and_upper_case_hex_shas_keep_the_old_cli_working(world):
    """记录-03: the documented usage passes 40 hex, but an older short SHA (no --bin-dir) must still install."""
    make_python(world['bin'])
    short = '9c20a32f'
    done = run(world, [short, world['tarball']])
    assert done.returncode == 0, done.stderr
    assert (world['releases'] / short / '.venv' / 'bin' / 'python').exists()
    done = run(world, [APP_SHA.upper(), world['tarball']])
    assert done.returncode == 0, done.stderr
    names = os.listdir(world['releases'])  # the host fs may be case-insensitive, so compare the dir name itself
    assert APP_SHA in names and APP_SHA.upper() not in names


def test_a_release_dir_this_run_did_not_create_is_never_removed(world):
    """P1-02: CREATED is only set by the run whose bare `mkdir` succeeded."""
    make_python(world['bin'])
    built = make_bin_dir(world['tmp'] / 'built')
    done = run(world, [APP_SHA, world['tarball'], '--bin-dir', built])
    assert done.returncode == 0, done.stderr
    rel = release(world)
    installed = (rel / 'bin' / 'installed.json').read_text()
    (rel / 'marker').write_text('first install\n')
    done = run(world, [APP_SHA, world['tarball'], '--bin-dir', built],
               env_extra={'FAKE_CHECK_DEPS_RC': '1'})
    assert done.returncode == 1
    assert 'release exists, not touched' in done.stderr
    assert (rel / 'bin' / 'installed.json').read_text() == installed
    assert (rel / 'marker').read_text() == 'first install\n'


def test_installed_json_escapes_quotes_and_backslashes_and_parses(world):
    """记录-02: a --tool-sha with a quote used to write invalid JSON while the install exited 0."""
    make_python(world['bin'])
    built = make_bin_dir(world['tmp'] / 'built')
    releases = world['tmp'] / 'rel "eases\\with\\backslashes'
    tool = 'fedcba98"x\\y'
    done = run(world, [APP_SHA, world['tarball'], '--bin-dir', built, '--tool-sha', tool],
               env_extra={'FLEETMUX_RELEASES': str(releases)})
    assert done.returncode == 0, done.stderr
    installed = json.loads((releases / APP_SHA / 'bin' / 'installed.json').read_text())
    assert installed['tool_sha'] == tool
    assert str(releases / APP_SHA / 'bin') in installed['start_command']
    assert installed['start_command'] in done.stdout


def test_a_pretty_printed_manifest_is_parsed_as_json(world):
    """记录-01: the sed regex refused a valid pretty-printed manifest; the manifest is JSON, so parse it."""
    make_python(world['bin'])
    pretty = ('{\n  "label": "final",\n  "platform": "utm-linux",\n  "source_sha":\n'
              '    "%s",\n  "same_sha_build": true\n}\n' % APP_SHA)
    json.loads(pretty)
    built = make_bin_dir(world['tmp'] / 'built', manifest_text=pretty)
    done = run(world, [APP_SHA, world['tarball'], '--bin-dir', built])
    assert done.returncode == 0, done.stderr
    assert json.loads((release(world) / 'bin' / 'installed.json').read_text())['source_sha'] == APP_SHA


def test_a_manifest_that_is_not_json_is_refused_with_the_parse_error(world):
    make_python(world['bin'])
    built = make_bin_dir(world['tmp'] / 'built', manifest_text='{"source_sha": "%s",}\n' % APP_SHA)
    done = run(world, [APP_SHA, world['tarball'], '--bin-dir', built])
    assert done.returncode != 0
    assert 'not valid JSON' in done.stderr, done.stderr
    assert not release(world).exists()


def test_success_prints_the_explicit_start_command_with_the_release_bin_first(world):
    """P1-03: a bare `fleetmux` in a normal shell resolves to ~/.local/bin, i.e. another build."""
    make_python(world['bin'])
    built = make_bin_dir(world['tmp'] / 'built')
    done = run(world, [APP_SHA, world['tarball'], '--bin-dir', built])
    assert done.returncode == 0, done.stderr
    rel = release(world)
    line = next(l for l in done.stdout.splitlines() if l.startswith('start (release bin first on PATH):'))
    assert line.endswith('PATH=%s/bin:$PATH %s/.venv/bin/fleetmux-py --help' % (rel, rel)), line
    installed = json.loads((rel / 'bin' / 'installed.json').read_text())
    assert installed['start_command'] == line.split(': ', 1)[1]
    # no console script in the venv -> the documented `python -m` fallback, never a bare `fleetmux`
    other = make_bin_dir(world['tmp'] / 'built-other', source_sha=OTHER_SHA)
    done = run(world, [OTHER_SHA, world['tarball'], '--bin-dir', other], env_extra={'FAKE_NO_ENTRY': '1'})
    assert done.returncode == 0, done.stderr
    rel2 = release(world, OTHER_SHA)
    line2 = next(l for l in done.stdout.splitlines() if l.startswith('start (release bin first on PATH):'))
    assert line2.endswith('PATH=%s/bin:$PATH %s/.venv/bin/python -m fleetmux.fleet --help' % (rel2, rel2)), line2


def test_start_command_is_executable_when_the_release_path_has_spaces_and_quotes(world):
    """P1-03: a raw `<rel>/bin:$PATH <rel>/.venv/bin/fleetmux-py` concatenation breaks on such a path."""
    make_python(world['bin'])
    built = make_bin_dir(world['tmp'] / 'built')
    releases = world['tmp'] / "rel eases 'q' \"dq\""
    done = run(world, [APP_SHA, world['tarball'], '--bin-dir', built],
               env_extra={'FLEETMUX_RELEASES': str(releases)})
    assert done.returncode == 0, done.stderr
    rel = releases / APP_SHA
    line = next(l for l in done.stdout.splitlines() if l.startswith('start (release bin first on PATH):'))
    cmd = line.split(': ', 1)[1]
    assert ':$PATH ' in cmd  # the caller's PATH is kept; only <release>/bin is prepended
    env = {**os.environ, 'PATH': str(world['bin']) + os.pathsep + '/usr/bin:/bin'}
    out = subprocess.run(['bash', '-c', cmd], capture_output=True, text=True, env=env)
    assert out.returncode == 0, (cmd, out.stderr)  # the printed line runs as-is
    first, _, rest = out.stdout.strip().partition(os.pathsep)
    assert first == 'ENTRY_PATH=' + str(rel / 'bin')  # release bin is first on PATH
    assert str(world['bin']) in rest                  # and the normal PATH is still behind it
    assert json.loads((rel / 'bin' / 'installed.json').read_text())['start_command'] == cmd


def test_a_nested_releases_root_is_created_and_only_the_leaf_is_atomic(world):
    """P1-02: `mkdir -p` is allowed for the container root; the release leaf is still a bare mkdir."""
    make_python(world['bin'])
    nested = world['tmp'] / 'a42' / 'releases' / 'utm'
    assert not nested.exists()
    done = run(world, [APP_SHA, world['tarball']], env_extra={'FLEETMUX_RELEASES': str(nested)})
    assert done.returncode == 0, done.stderr
    rel = nested / APP_SHA
    assert (rel / '.venv' / 'bin' / 'python').exists()
    # a leaf that already exists is refused and left exactly as it was (the leaf mkdir is not -p)
    (rel / 'marker').write_text('keep me\n')
    done = run(world, [APP_SHA, world['tarball']], env_extra={'FLEETMUX_RELEASES': str(nested)})
    assert done.returncode == 1
    assert 'release exists, not touched' in done.stderr
    assert (rel / 'marker').read_text() == 'keep me\n'
    assert (rel / '.venv' / 'bin' / 'python').exists()
