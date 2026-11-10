"""fleetmux fleet commands (package cmux_fleet; command `fleetmux`, long-term alias `cmux-fleet`): supervisor + N
worker agents coordinated by a daemon."""
__version__ = '0.1.0'

# Before any submodule reads the environment (several bind paths at import time): FLEETMUX_* -> CMUX_FLEET_*.
from cmux_fleet import env_names as _env_names  # noqa: E402

_env_names.apply()
