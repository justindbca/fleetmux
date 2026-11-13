"""`fleetmux` command of the platform wheels (scripts/build_wheels.py).

A platform wheel carries the release's `fleetmux` and `fleetmux-web` binaries in `fleetmux/_bin/`. This entry
point points the binary at the Python CLI of the same install (`fleetmux-py` beside this console script) and at
itself, then execs it. Variables the user already set are kept. A source checkout or a release directory does
not use this module: it runs the binary directly.
"""
import os
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent / '_bin'


def main():
    binary = BIN_DIR / 'fleetmux'
    if not os.access(binary, os.X_OK):
        sys.stderr.write('fleetmux: this install has no fleetmux binary (%s); install a platform wheel or use '
                         'scripts/install.sh\n' % binary)
        return 127
    cli = Path(sys.argv[0]).resolve().with_name('fleetmux-py')
    if os.access(cli, os.X_OK):
        os.environ.setdefault('FLEETMUX_PYTHON_CLI', str(cli))
    os.environ.setdefault('FLEETMUX_BIN', str(binary))
    os.execv(str(binary), [str(binary)] + sys.argv[1:])


if __name__ == '__main__':
    sys.exit(main())
