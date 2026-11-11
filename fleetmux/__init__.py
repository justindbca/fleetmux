"""fleetmux fleet commands (package fleetmux; command `fleetmux`, long-term alias `cmux-fleet`): supervisor + N
worker agents coordinated by a daemon."""
__version__ = '0.1.0'

# Before any submodule reads the environment (several bind paths at import time): new and legacy names agree.
from fleetmux import env_names as _env_names  # noqa: E402

_env_names.apply()
