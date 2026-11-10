"""One configuration file with one block per table (cmux_fleet/config/fleet.yaml).

The shipped file holds the defaults. The operator's private file has the same format
(``$CMUX_FLEET_CONFIG``, else ``~/.fleetmux/fleet.yaml``; legacy ``~/.cmux-fleet/fleet.yaml`` until migrated) and is never part of this repository:
a top-level block there replaces the shipped block as a whole, and only ``USER_BLOCKS`` may appear
there (cli_pairs is the real-smoke gate and watchdog_prompts the dialog whitelist; those, the cards
and the matrix change only in the shipped file).

An explicit path (``--prompt-table``, a test table) may be either this format or a bare table:
a file whose top-level keys are all block names is read as blocks, anything else is the table itself.
"""
import copy
import os
import threading
from pathlib import Path

from cmux_fleet import paths

HERE = Path(__file__).resolve().parent
SHIPPED = HERE / 'config' / 'fleet.yaml'
USER_ENV = 'CMUX_FLEET_CONFIG'
USER_FILE = None  # None: paths.default_home() / 'fleet.yaml' (never follows FLEETMUX_HOME), resolved on use
BLOCKS = ('cli_pairs', 'lifecycle_modes', 'walls', 'watchdog_prompts', 'capability_matrix', 'models',
          'profiles', 'accounts', 'tools', 'pi_context', 'daemon_watch', 'fresh_dispatch', 'dsh',
          'discovery')
USER_BLOCKS = ('accounts', 'tools', 'walls', 'lifecycle_modes', 'pi_context', 'daemon_watch', 'fresh_dispatch',
               'discovery')

NONE = 'none'  # recorded when there is no private file

_lock = threading.Lock()
_cache = {}
_pinned = None  # the run's private file (config.json `private_config`): absolute path or NONE; None = not pinned


def pin(value):
    """Read ``value`` as the private file from now on in this process: an absolute path, NONE (no private
    file), or None (back to the environment). Set from config.json so every process of a run reads the same
    file as ``start`` did, whatever environment cmux gave it."""
    global _pinned
    _pinned = value


def from_env():
    """The private file the environment names ($CMUX_FLEET_CONFIG, else the default), as config.json records
    it: its absolute path, or NONE when no such file exists. Ignores any pin (``start`` resolves it here)."""
    path = Path(os.environ.get(USER_ENV) or USER_FILE or paths.default_home() / 'fleet.yaml').expanduser()
    return str(path.resolve()) if path.is_file() else NONE


def user_path():
    """The private file this process reads, or None when the run recorded that it has none."""
    if _pinned is not None:
        return None if _pinned == NONE else Path(_pinned)
    return Path(os.environ.get(USER_ENV) or USER_FILE or paths.default_home() / 'fleet.yaml').expanduser()


def _stat(path):
    st = path.stat()
    return st.st_mtime_ns, st.st_size


def stamp(path=None):
    """Change stamp of what block(name, path) reads; raises OSError when the file it needs is missing."""
    if path is not None:
        return _stat(Path(path).expanduser())
    user = user_path()
    try:
        user_stamp = None if user is None else _stat(user)
    except OSError:
        user_stamp = None
    return _stat(SHIPPED), str(user), user_stamp


def _read(path):
    import yaml
    try:
        data = yaml.safe_load(Path(path).expanduser().read_text(encoding='utf-8'))
    except yaml.YAMLError as exc:
        raise ValueError(str(path) + ' is not valid YAML: ' + str(exc).splitlines()[0]) from None
    return {} if data is None else data


def _is_blocks(data):
    return isinstance(data, dict) and bool(data) and set(data) <= set(BLOCKS)


def load():
    """Shipped blocks with the private file's blocks laid over them (cached on both files' stamps)."""
    key = stamp()
    with _lock:
        cached = _cache.get('merged')
        if cached and cached[0] == key:
            return cached[1]
        shipped = _read(SHIPPED)
        if not _is_blocks(shipped):
            raise ValueError(str(SHIPPED) + ': top-level keys must be blocks: ' + ', '.join(BLOCKS))
        merged = dict(shipped)
        if key[2] is not None:
            user = _read(user_path())
            if not isinstance(user, dict):
                raise ValueError(str(user_path()) + ' must be a mapping of blocks')
            extra = sorted(set(user) - set(USER_BLOCKS))
            if extra:
                raise ValueError(str(user_path()) + ': block ' + ', '.join(extra) + ' may not be overridden here '
                                 + '(allowed: ' + ', '.join(USER_BLOCKS) + ')')
            merged.update(user)
        _cache['merged'] = (key, merged)
        return merged


def block(name, path=None):
    """One table: from the merged configuration, or from an explicit file (blocks or a bare table)."""
    if name not in BLOCKS:
        raise KeyError(name)
    if path is None:
        return copy.deepcopy(load().get(name))
    data = _read(path)
    return data.get(name) if _is_blocks(data) else data


def source(path=None):
    """Where a block came from, for messages."""
    return str(path) if path is not None else str(SHIPPED) + ' (+ ' + str(user_path() or 'no private file') + ')'


def fresh_hint_ratio():
    """fresh_dispatch.fresh_hint_ratio (docs/design.md §37): a reused worker above this share of its window
    earns the supervisor one hint line. A number in (0, 1]; 0.3 without the block."""
    value = (block('fresh_dispatch') or {}).get('fresh_hint_ratio', 0.3)
    if type(value) not in (int, float) or not 0 < value <= 1:
        raise ValueError('fleet.yaml#fresh_dispatch.fresh_hint_ratio must be a number in (0, 1], got ' + repr(value))
    return value


def dsh_default_model():
    """dsh.default_model (docs/design.md §41): the provider/model a dsh worker runs without --dsh-model."""
    value = (block('dsh') or {}).get('default_model')
    if not isinstance(value, str) or value.count('/') < 1 or value != value.strip():
        raise ValueError('fleet.yaml#dsh.default_model must be provider/model, got ' + repr(value))
    return value
