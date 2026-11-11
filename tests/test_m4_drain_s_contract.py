"""brief-m4 §7.2: a named sub-fleet's drain limit is the parent's declared ``drain_s``, not a child-config key.

The phase-A E2 expected ``drain_s`` in the child config (``CHILD_KEYS``), but the product never writes it there
(``subfleet.NAMED_OVERRIDES`` has no ``drain_s``): the parent daemon's ``reload_roster`` persists
``cfg['subfleets']`` and hands the same entries to ``named_subfleet.declare``, and ``named_subfleet.tick`` reads
the down / reclaim limit as ``float((decl.get(name) or {}).get('drain_s') or roster.DRAIN_S)``. These tests drive
that real path in the in-process sub-fleet world of ``tests/test_subfleet.py`` (used unchanged) with two different
declared values, so a limit taken from anywhere else (the default 600 s, the child config, a constant) fails.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _fleetmux_milestone import milestone  # noqa: E402
from fleetmux import fleet, named_subfleet, roster, subfleet  # noqa: E402
from test_subfleet import (ask, bring_up, result, stop_child_like_daemon, supervisor_answers,  # noqa: E402,F401
                           svc, typed_return)

pytestmark = milestone(4)


def redeclare(t, drain_s):
    """Change the roster's ``drain_s`` and let the parent daemon reload it (its own roster_tick -> reload_roster)."""
    path = t.tmp / 'roster.yaml'
    old = path.read_text()
    assert '    drain_s: 60\n' in old, old
    path.write_text(old.replace('    drain_s: 60\n', '    drain_s: %d\n' % drain_s))
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000_000))
    t.f.roster_tick()
    assert named_subfleet.declared(t.f)['ops']['drain_s'] == drain_s, named_subfleet.declared(t.f)
    persisted = {e['name']: e for e in fleet.load(t.f.path).get('subfleets') or []}
    assert persisted['ops']['drain_s'] == drain_s, persisted  # the parent config.json carries the declaration


def drain_timeouts(f):
    return [json.loads(x[1]['data']) if 'data' in x[1] else x[1] for x in f.r.xrange(f.v2.bus.audit_key)
            if x[1].get('event') == 'subfleet.drain_timeout']


@pytest.mark.parametrize('drain_s', [30, 90])
def test_down_waits_exactly_the_parents_declared_drain_s(svc, drain_s):
    t, f = svc, svc.f
    assert drain_s != roster.DRAIN_S
    typed_return(t)  # same-session return: typed path only, as test_drain_timeout_takes_the_child_back_anyway
    redeclare(t, drain_s)
    _, rec, cf = bring_up(t)
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    assert 'drain_s' not in child_cfg, sorted(child_cfg)  # never a child-config key (subfleet.NAMED_OVERRIDES)
    cr = fleet.connect(child_cfg)
    cr.rpush(fleet.REQQ, 'stuck')  # the child is not quiet: only the limit can end the drain
    cr.close()
    rid, _ = ask(t, 'down')
    supervisor_answers(t, rid)
    subfleet.tick(f)
    assert named_subfleet.state(f, 'ops')['state'] == 'draining'
    assert subfleet.record(rec['run'])['state'] == 'running'
    # just inside the declared limit: still draining (the default 600 s would also hold here, the next step not)
    f.r.hset(named_subfleet.HASH + 'ops', mapping={'drain_since': time.time() - (drain_s - 5)})
    subfleet.tick(f)
    assert subfleet.record(rec['run'])['state'] == 'running', 'drain ended before the declared %d s' % drain_s
    assert drain_timeouts(f) == []
    # just past it: the down path takes the child back with the declared value as its limit
    f.r.hset(named_subfleet.HASH + 'ops', mapping={'drain_since': time.time() - (drain_s + 1)})
    subfleet.tick(f)
    assert subfleet.record(rec['run'])['state'] == 'stopping', 'drain still running past the declared %d s' % drain_s
    assert result(f, rid) == dict(result(f, rid), status='timeout', reason='drain exceeded')
    events = drain_timeouts(f)
    assert len(events) == 1 and float(events[0]['drain_s']) == float(drain_s), events
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    assert subfleet.record(rec['run'])['state'] == 'down'
