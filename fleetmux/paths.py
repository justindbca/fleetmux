"""Where fleetmux keeps per-user data: one root, ``~/.fleetmux`` (docs/design.md §50.1).

* fleet home   ``~/.fleetmux``                runs/, latest.json, codex-home/, private fleet.yaml, quota.json,
  discovered.yaml, dsh-home/
* state        ``~/.fleetmux/state``          backend window registries, runtimes
* config       ``~/.fleetmux/config``         channel accounts, leases, security.yaml
* launchers    ``~/.fleetmux/launchers.env``  (read by launchers/*.command)
* board        ``~/.fleetmux/board``          (session_board.py)
* a second fleet: ``FLEETMUX_HOME=~/.fleetmux/fleets/<name>``, ``FLEETMUX_STATE=~/.fleetmux/fleets/<name>/state``

Resolution, per item: an explicit environment variable wins, else the path above. Data written under an older
layout is moved here by ``fleetmux home migrate`` (fleetmux/home_migrate.py); until then the human-facing
commands stop with one line saying so (``home_migrate.check``).
"""
import os
from pathlib import Path

ENV = {'home': 'FLEETMUX_HOME', 'state': 'FLEETMUX_STATE', 'config': 'FLEETMUX_CONFIG_DIR',
       'launchers': 'FLEETMUX_LAUNCHER_ENV'}


def root():
    return Path.home() / '.fleetmux'


def default(item):
    """The default path of an item, ignoring the environment."""
    return {'home': root(), 'state': root() / 'state', 'config': root() / 'config',
            'launchers': root() / 'launchers.env'}[item]


def resolve(item):
    value = os.environ.get(ENV[item])
    return Path(value).expanduser() if value else default(item)


def default_home():
    """The default fleet home, never overridden by FLEETMUX_HOME: the private fleet.yaml, quota.json and the
    shared dsh-home live here for every fleet, including per-fleet homes under ~/.fleetmux/fleets/."""
    return default('home')


def home():
    return resolve('home')


def state_dir():
    return resolve('state')


def config_dir():
    return resolve('config')


def launcher_env():
    return resolve('launchers')
