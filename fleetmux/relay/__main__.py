"""`python -m fleetmux.relay <command>`: the relay command line (also `fleetmux relay <command>`)."""
import sys

from fleetmux.relay import cli

if __name__ == '__main__':
    sys.exit(cli.main(sys.argv[1:]))
