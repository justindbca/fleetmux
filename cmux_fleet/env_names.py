"""New FLEETMUX_* names for the CMUX_FLEET_* environment variables (docs/design.md §50).

Both names work, for good. The new one wins: ``apply()`` copies its value over the old name in this process's
environment, so every reader (they all read the old name) and every child process sees one value. Both set and
different: the new value is used and stderr gets one warning line. An empty value counts as unset.
"""
import os
import sys

PAIRS = (('FLEETMUX_HOME', 'CMUX_FLEET_HOME'), ('FLEETMUX_STATE', 'CMUX_FLEET_STATE'),
         ('FLEETMUX_CONFIG', 'CMUX_FLEET_CONFIG'))


def apply(environ=None, err=None):
    """Make each old name carry the new name's value (``environ`` defaults to os.environ)."""
    environ = os.environ if environ is None else environ
    for new, old in PAIRS:
        value = environ.get(new)
        if not value:
            continue
        if environ.get(old) and environ[old] != value:
            print('fleetmux: %s=%s and %s=%s differ; using %s' % (new, value, old, environ[old], new),
                  file=err or sys.stderr)
        environ[old] = value
