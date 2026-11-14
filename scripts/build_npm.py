#!/usr/bin/env python3
"""Build the npm packages of one release from its GitHub Release assets.

Steps:
1. Download the release assets: `gh release download v<version> -R justindbca/fleetmux -D ASSETS`.
2. Run: `scripts/build_npm.py --version <version> --assets ASSETS --out OUT`.
3. Publish the platform packages first, then the main package:
   `for d in OUT/fleetmux-*-*; do npm publish "$d" --access public; done; npm publish OUT/fleetmux`.

Packages:
- `@fleetmux/<os>-<cpu>` (one per platform): the release's `fleetmux` and `fleetmux-web` binaries (sha256-checked
  against the release's `.sha256` files) and their license files in `bin/`. npm installs only the one that matches
  the machine (`os`, `cpu`, `libc`).
- `fleetmux`: the `fleetmux` command (bin/fleetmux.js), which runs the binary of the installed platform package.

Like `cargo install fleetmux`, the npm packages install the terminal only; fleet commands also need the Python
package of the same version (`uv tool install fleetmux`).
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from build_wheels import die, release_files

ROOT = Path(__file__).resolve().parents[1]
REPO = 'https://github.com/justindbca/fleetmux'
SCOPE = '@fleetmux'
# npm os, npm cpu, npm libc (None = any), release triple of `fleetmux`, release triple of `fleetmux-web`
TARGETS = [
    ('darwin', 'arm64', None, 'aarch64-apple-darwin', 'aarch64-apple-darwin'),
    ('darwin', 'x64', None, 'x86_64-apple-darwin', 'x86_64-apple-darwin'),
    ('linux', 'x64', 'glibc', 'x86_64-unknown-linux-musl', 'x86_64-unknown-linux-gnu'),
    ('linux', 'arm64', 'glibc', 'aarch64-unknown-linux-musl', 'aarch64-unknown-linux-gnu'),
]
DESCRIPTION = 'fleetmux: run AI agent teams. A supervisor agent leads worker agents, and the program accepts the work.'
LAUNCHER = r"""#!/usr/bin/env node
// `fleetmux` command of the npm package: runs the binary of the installed @fleetmux/<os>-<cpu> package.
'use strict';
const { spawnSync } = require('child_process');
const path = require('path');

const pkg = '@fleetmux/' + process.platform + '-' + process.arch;
let binary;
try {
  binary = path.join(path.dirname(require.resolve(pkg + '/package.json')), 'bin', 'fleetmux');
} catch (e) {
  process.stderr.write('fleetmux: ' + pkg + ' is not installed. Supported platforms: ' +
                       '__PLATFORMS__. Reinstall without --no-optional, or see ' +
                       '__REPO__#install\n');
  process.exit(127);
}
const env = Object.assign({}, process.env);
if (!env.FLEETMUX_BIN) env.FLEETMUX_BIN = binary;  // the Python CLI finds fleetmux-web beside it
// Ctrl-C and Ctrl-\ reach the whole process group; let the binary handle them and report its own exit.
process.on('SIGINT', () => {});
process.on('SIGQUIT', () => {});
const r = spawnSync(binary, process.argv.slice(2), { stdio: 'inherit', env });
if (r.error) {
  process.stderr.write('fleetmux: cannot run ' + binary + ': ' + r.error.message + '\n');
  process.exit(127);
}
if (r.signal) {
  process.removeAllListeners(r.signal);
  process.kill(process.pid, r.signal);
}
process.exit(r.status === null ? 1 : r.status);
"""
README = """# fleetmux

Run AI agent teams. A supervisor agent leads worker agents (Claude Code, Codex, Pi, opencode), and the program,
not the model, accepts the work.

## Install

```bash
npm install -g fleetmux
```

This installs the `fleetmux` terminal with the prebuilt binaries of the GitHub release {version}
(macOS on Apple silicon and Intel, Linux x64 and arm64 with glibc 2.39+).

Fleet commands also need the Python package of the same version:

```bash
uv tool install fleetmux=={version}
```

See the [README]({repo}#install) for the system packages and the agent CLIs.
"""


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n')


def common(version):
    return {'version': version, 'description': DESCRIPTION, 'license': 'MIT', 'homepage': REPO,
            'repository': {'type': 'git', 'url': 'git+%s.git' % REPO}}


def build_platform(assets, out, version, os_, cpu, libc, triple, web_triple):
    core = release_files(assets, 'fleetmux', version, triple)
    web = release_files(assets, 'fleetmux-web', version, web_triple)
    d = out / ('fleetmux-%s-%s' % (os_, cpu))
    (d / 'bin').mkdir(parents=True)
    for name, data, mode in (('fleetmux', core['fleetmux'], 0o755), ('fleetmux-web', web['fleetmux-web'], 0o755),
                             ('THIRD-PARTY-LICENSES-fleetmux', core['THIRD-PARTY-LICENSES'], 0o644),
                             ('THIRD-PARTY-LICENSES-fleetmux-web', web['THIRD-PARTY-LICENSES'], 0o644)):
        (d / 'bin' / name).write_bytes(data)
        (d / 'bin' / name).chmod(mode)
    shutil.copy(ROOT / 'LICENSE', d / 'LICENSE')
    meta = {'name': '%s/%s-%s' % (SCOPE, os_, cpu), **common(version), 'os': [os_], 'cpu': [cpu],
            'files': ['bin', 'LICENSE'], 'preferUnplugged': True}
    meta['description'] = 'The %s %s binaries of fleetmux %s. Install `fleetmux` instead.' % (os_, cpu, version)
    if libc:
        meta['libc'] = [libc]
    write_json(d / 'package.json', meta)
    (d / 'README.md').write_text('# %s/%s-%s\n\nThe `fleetmux` and `fleetmux-web` binaries of the fleetmux %s '
                                 'release for %s %s. Install [`fleetmux`](https://www.npmjs.com/package/fleetmux) '
                                 'instead of this package.\n' % (SCOPE, os_, cpu, version, os_, cpu))
    return d


def build_main(out, version):
    d = out / 'fleetmux'
    (d / 'bin').mkdir(parents=True)
    platforms = ', '.join('%s-%s' % (t[0], t[1]) for t in TARGETS)
    launcher = d / 'bin' / 'fleetmux.js'
    launcher.write_text(LAUNCHER.replace('__PLATFORMS__', platforms).replace('__REPO__', REPO))
    launcher.chmod(0o755)
    shutil.copy(ROOT / 'LICENSE', d / 'LICENSE')
    (d / 'README.md').write_text(README.format(version=version, repo=REPO))
    meta = {'name': 'fleetmux', **common(version), 'bin': {'fleetmux': 'bin/fleetmux.js'},
            'files': ['bin', 'LICENSE'], 'engines': {'node': '>=16'},
            'optionalDependencies': {'%s/%s-%s' % (SCOPE, t[0], t[1]): version for t in TARGETS}}
    write_json(d / 'package.json', meta)
    return d


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--version', required=True, help='release version, e.g. 0.2.2')
    p.add_argument('--assets', required=True, type=Path, help='directory with the GitHub Release assets')
    p.add_argument('--out', required=True, type=Path, help='new directory for the package directories')
    a = p.parse_args(argv)
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', a.version):
        die('--version must be X.Y.Z')
    if a.out.exists() and any(a.out.iterdir()):
        die('%s is not empty' % a.out)
    a.out.mkdir(parents=True, exist_ok=True)
    for os_, cpu, libc, triple, web_triple in TARGETS:
        print(build_platform(a.assets, a.out, a.version, os_, cpu, libc, triple, web_triple))
    print(build_main(a.out, a.version))
    return 0


if __name__ == '__main__':
    sys.exit(main())
