"""Milestone marker for the fleetmux Python tests (brief-m03 §3.1 item 3).

``@milestone(n)`` skips a test whose milestone M<n> is not part of the gate being run:
``scripts/fleetmux_gate.sh <phase>`` exports ``FLEETMUX_GATE=<phase>``, and ``RANK`` says which milestones
each phase covers. No ``FLEETMUX_GATE`` (CI's final state, a reviewer checking a red test) = ``m3`` = run every milestone up to M3;
``@milestone(4)`` tests run only under ``FLEETMUX_GATE=m4`` (``scripts/fleetmux_gate.sh m4``).
M0 tests carry no marker: every gate runs them.
"""
import os

import pytest

RANK = {'p0': 0, 'a': 0, 'm0': 0, 'm12': 2, 'm3': 3, 'm4': 4}


def milestone(n: int):
    gate = os.environ.get('FLEETMUX_GATE') or 'm3'
    return pytest.mark.skipif(RANK[gate] < n, reason='M%d not in gate %s' % (n, gate))
