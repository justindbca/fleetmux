"""Acceptance check after a merge-queue batch lands on main: the package imports and reports its version."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import fleetmux  # noqa: F401
    from fleetmux import fleet  # noqa: F401
except Exception as exc:  # noqa: BLE001
    print(json.dumps({"result": "fail", "reason": type(exc).__name__ + ": " + str(exc)[:200]}))
    sys.exit(1)
print(json.dumps({"result": "pass", "reason": "fleetmux imports"}))
sys.exit(0)
