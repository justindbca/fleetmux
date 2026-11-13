#!/usr/bin/env python3
"""Build the PyPI platform wheels of one release from its pure wheel and its GitHub Release assets.

Steps:
1. Build the pure wheel from the tagged source: `uv build --wheel -o DIST`.
2. Download the release assets: `gh release download v<version> -R justindbca/fleetmux -D ASSETS`.
3. Run: `scripts/build_wheels.py --base DIST/fleetmux-<version>-py3-none-any.whl --assets ASSETS --out OUT`.

Each platform wheel = the pure wheel + the release's `fleetmux` and `fleetmux-web` binaries (sha256-checked
against the release's `.sha256` files) and their license files in `fleetmux/_bin/`, plus the `fleetmux`
console script (fleetmux/wheel_launcher.py). The platform tags follow the binaries: macOS minimum versions
from the Mach-O headers, glibc 2.39 from the Linux `fleetmux-web` (the Linux `fleetmux` is static musl).
"""
import argparse
import base64
import csv
import hashlib
import io
import re
import sys
import tarfile
import zipfile
from pathlib import Path

# release triple of `fleetmux`, release triple of `fleetmux-web`, wheel platform tag
TARGETS = [
    ('aarch64-apple-darwin', 'aarch64-apple-darwin', 'macosx_11_0_arm64'),
    ('x86_64-apple-darwin', 'x86_64-apple-darwin', 'macosx_10_12_x86_64'),
    ('x86_64-unknown-linux-musl', 'x86_64-unknown-linux-gnu', 'manylinux_2_39_x86_64'),
    ('aarch64-unknown-linux-musl', 'aarch64-unknown-linux-gnu', 'manylinux_2_39_aarch64'),
]
ENTRY = 'fleetmux = fleetmux.wheel_launcher:main'


def die(msg):
    sys.exit('build_wheels: ' + msg)


def release_files(assets, program, version, triple):
    """The files of one release tarball, after checking its sha256 against the release's .sha256 file."""
    tar = assets / ('%s-%s-%s.tar.gz' % (program, version, triple))
    want_line = (assets / (tar.name + '.sha256')).read_text().split()
    if len(want_line) < 1 or hashlib.sha256(tar.read_bytes()).hexdigest() != want_line[0]:
        die('sha256 mismatch: %s' % tar.name)
    out = {}
    with tarfile.open(tar) as t:
        for m in t.getmembers():
            if m.isfile():
                out[Path(m.name).name] = t.extractfile(m).read()
    if program not in out:
        die('%s has no %s' % (tar.name, program))
    return out


def record_line(name, data):
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
    return [name, 'sha256=' + digest, str(len(data))]


def build(base, assets, out, version, triple, web_triple, tag):
    core = release_files(assets, 'fleetmux', version, triple)
    web = release_files(assets, 'fleetmux-web', version, web_triple)
    added = {  # path in the wheel -> (bytes, mode)
        'fleetmux/_bin/fleetmux': (core['fleetmux'], 0o755),
        'fleetmux/_bin/fleetmux-web': (web['fleetmux-web'], 0o755),
        'fleetmux/_bin/THIRD-PARTY-LICENSES-fleetmux': (core['THIRD-PARTY-LICENSES'], 0o644),
        'fleetmux/_bin/THIRD-PARTY-LICENSES-fleetmux-web': (web['THIRD-PARTY-LICENSES'], 0o644),
    }
    dist_info = 'fleetmux-%s.dist-info' % version
    target = out / ('fleetmux-%s-py3-none-%s.whl' % (version, tag))
    rows = []
    with zipfile.ZipFile(base) as src, zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as dst:
        names = src.namelist()
        if dist_info + '/WHEEL' not in names:
            die('%s is not the fleetmux %s wheel' % (base.name, version))
        for info in src.infolist():
            name = info.filename
            if name == dist_info + '/RECORD':
                continue
            data = src.read(name)
            if name == dist_info + '/WHEEL':
                text = data.decode()
                text = re.sub(r'(?m)^Root-Is-Purelib: .*$', 'Root-Is-Purelib: false', text)
                text = re.sub(r'(?m)^Tag: .*$', 'Tag: py3-none-' + tag, text)
                data = text.encode()
            elif name == dist_info + '/entry_points.txt':
                text = data.decode()
                if '[console_scripts]' not in text:
                    die('no [console_scripts] in entry_points.txt')
                data = text.replace('[console_scripts]\n', '[console_scripts]\n' + ENTRY + '\n', 1).encode()
            dst.writestr(info, data)
            rows.append(record_line(name, data))
        for name, (data, mode) in added.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(info, data)
            rows.append(record_line(name, data))
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator='\n')
        writer.writerows(rows)
        writer.writerow([dist_info + '/RECORD', '', ''])
        dst.writestr(dist_info + '/RECORD', buf.getvalue())
    return target


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--base', required=True, type=Path, help='the pure wheel fleetmux-<version>-py3-none-any.whl')
    p.add_argument('--assets', required=True, type=Path, help='directory with the GitHub Release assets')
    p.add_argument('--out', required=True, type=Path)
    a = p.parse_args(argv)
    found = re.fullmatch(r'fleetmux-([0-9][^-]*)-py3-none-any\.whl', a.base.name)
    if not found:
        die('--base must be fleetmux-<version>-py3-none-any.whl')
    a.out.mkdir(parents=True, exist_ok=True)
    for triple, web_triple, tag in TARGETS:
        print(build(a.base, a.assets, a.out, found.group(1), triple, web_triple, tag))
    return 0


if __name__ == '__main__':
    sys.exit(main())
