"""``fleet.py`` entry for the M4 end-to-end test (e2e-1 plan F1): the real ``fleet.main()`` with one difference.

``start`` / ``start --resume`` sweep every ``redis-server unixsocket:`` of this user for abandoned fleets
(fleet.sweep_orphan_redis -> reap_orphan_redis, design §13). A test must never shut down another fleet's Redis,
so here the sweep is limited to this world's Redis root (``reap_orphan_redis(root=…)``, the same scoping
tests/test_orphan_redis.py uses). Child daemons, hooks and the lifecycle closer are started by the product as
``cmux_fleet/fleet.py`` itself and never sweep. Every other byte of behaviour is the product's.

Runs only inside an m4e world: ``$M4E_LAUNCH_ROOT`` must name the world directory.
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from cmux_fleet import fleet  # noqa: E402


def main():
    root = os.environ.get('M4E_LAUNCH_ROOT')
    if not root or not (Path(root) / 'r').is_dir():
        raise SystemExit('tests/m4e/launch.py: $M4E_LAUNCH_ROOT must be an m4e world directory (has r/)')
    redis_root = (Path(root) / 'r').resolve()

    def sweep_this_world_only(keep=None):
        try:
            fleet.reap_orphan_redis(keep=keep, root=redis_root)
        except Exception as exc:  # the product's own rule: housekeeping never blocks a start
            print('orphan Redis sweep skipped:', str(exc)[:200])

    fleet.sweep_orphan_redis = sweep_this_world_only
    sys.argv = [str(REPO / 'cmux_fleet' / 'fleet.py')] + sys.argv[1:]
    fleet.main()


if __name__ == '__main__':
    main()
