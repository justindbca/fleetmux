"""One configuration file with one block per table (fleetmux/config/fleet.yaml).

The shipped file holds the defaults. The operator's private file has the same format
(``$FLEETMUX_CONFIG``, else ``~/.fleetmux/fleet.yaml``) and is never part of this repository:
a top-level block there replaces the shipped block as a whole, and only ``USER_BLOCKS`` may appear
there (cli_pairs is the real-smoke gate and watchdog_prompts the dialog whitelist; those, the cards
and the matrix change only in the shipped file).

An explicit path (``--prompt-table``, a test table) may be either this format or a bare table:
a file whose top-level keys are all block names is read as blocks, anything else is the table itself.
"""
import contextlib
import copy
import fcntl
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from fleetmux import paths

HERE = Path(__file__).resolve().parent
SHIPPED = HERE / 'config' / 'fleet.yaml'
USER_ENV = 'FLEETMUX_CONFIG'
USER_FILE = None  # None: paths.default_home() / 'fleet.yaml' (never follows FLEETMUX_HOME), resolved on use
BLOCKS = ('cli_pairs', 'lifecycle_modes', 'walls', 'watchdog_prompts', 'capability_matrix', 'models',
          'profiles', 'accounts', 'tools', 'pi_context', 'daemon_watch', 'fresh_dispatch', 'dsh',
          'discovery', 'remote', 'session_env', 'job_subfleet', 'gui', 'bare', 'relay', 'restart_resume', 'decompose',
          'ui')
USER_BLOCKS = ('accounts', 'tools', 'walls', 'lifecycle_modes', 'pi_context', 'daemon_watch', 'fresh_dispatch',
               'discovery', 'remote', 'job_subfleet', 'gui', 'bare', 'relay', 'restart_resume', 'decompose', 'ui')

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
    """The private file the environment names ($FLEETMUX_CONFIG, else the default), as config.json records
    it: its absolute path, or NONE when no such file exists. Ignores any pin (``start`` resolves it here)."""
    path = Path(os.environ.get(USER_ENV) or USER_FILE or paths.default_home() / 'fleet.yaml').expanduser()
    return str(path.resolve()) if path.is_file() else NONE


def user_path():
    """The private file this process reads, or None when the run recorded that it has none."""
    if _pinned is not None:
        return None if _pinned == NONE else Path(_pinned)
    return Path(os.environ.get(USER_ENV) or USER_FILE or paths.default_home() / 'fleet.yaml').expanduser()


def write_target():
    """The private file a write-back goes to: the one this process reads, else (a run that recorded none) the one the
    environment names ($FLEETMUX_CONFIG, else the default); it is created when missing."""
    return user_path() or Path(os.environ.get(USER_ENV) or USER_FILE or paths.default_home() / 'fleet.yaml').expanduser()


LOCK_TIMEOUT = 10.0  # seconds a writer of the private file waits for its lock


@contextlib.contextmanager
def user_file_lock(path=None, timeout=LOCK_TIMEOUT):
    """Exclusive lock on ``<private file>.lock``: every writer of the private file (the decompose and relay
    write-backs) reads, changes and replaces it under this lock, so neither loses the other's change."""
    target = Path(path) if path is not None else write_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target.with_name(target.name + '.lock'), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise ValueError(str(target) + ' is locked by another writer') from None
                time.sleep(.05)
        yield target
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def _splice(text, name, value):
    """``text`` with its top-level block ``name`` replaced by ``value`` (appended when absent); every other line,
    comments included, stays as it is."""
    import yaml
    lines = text.splitlines(keepends=True)
    top = [i for i, line in enumerate(lines) if re.match(r'[A-Za-z_][\w-]*\s*:', line)]
    start = next((i for i in top if re.match(re.escape(name) + r'\s*:', lines[i])), None)
    dumped = yaml.safe_dump({name: value}, sort_keys=False, allow_unicode=True, default_flow_style=False)
    if start is None:
        return text + ('' if not text or text.endswith('\n') else '\n') + dumped
    end = next((i for i in top if i > start), len(lines))
    while end - 1 > start and not lines[end - 1].startswith((' ', '\t')) and (
            not lines[end - 1].strip() or lines[end - 1].startswith('#')):
        end -= 1  # blank lines and column-0 comments in front of the next block belong to it
    return ''.join(lines[:start]) + dumped + ''.join(lines[end:])


def write_user_block(name, value, path=None):
    """Replace block ``name`` of the private file (never the shipped one) under the private file's lock: re-read,
    splice that one block, check the result parses to the same other blocks, write a 0600 temporary file and
    rename it over the file. Returns the path."""
    import yaml
    if name not in USER_BLOCKS:
        raise ValueError('block ' + name + ' may not be written to the private file')
    target = Path(path).expanduser() if path is not None else write_target()
    if target.resolve() == SHIPPED.resolve():
        raise ValueError('a block is written back only to the private fleet.yaml, never the shipped one')
    with user_file_lock(target):
        text = target.read_text(encoding='utf-8') if target.is_file() else ''
        old = (yaml.safe_load(text) or {}) if text.strip() else {}
        new_text = _splice(text, name, value)
        new = yaml.safe_load(new_text) or {}
        if not isinstance(old, dict) or new.get(name) != value or {k: v for k, v in new.items() if k != name} != {
                k: v for k, v in old.items() if k != name}:
            raise ValueError('cannot replace block ' + name + ' of ' + str(target) + ' in place')
        fd, tmp = tempfile.mkstemp(prefix=target.name + '.', dir=target.parent)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(new_text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    return target


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


JOB_SUBFLEET = {'workers': 2, 'supervisor_profile': None, 'worker_profiles': [], 'hours': 4,
                'supervisor': None, 'worker': None}


def check_chain(value, where, absent='null'):
    """A model chain (docs/design.md §58): None, or a non-empty list of non-empty pattern strings; ValueError else."""
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(p, str) and p.strip() for p in value):
        raise ValueError(where + ' must be a non-empty list of model patterns (e.g. ["astra"]) or ' + absent
                         + ', got ' + repr(value))
    return list(value)


def job_subfleet():
    """job_subfleet (docs/design.md §58): the sub-fleet a job on a service fleet runs in, before the job spec's own
    overrides. Keys missing from the block take the built-in values; a bad value is refused (ValueError)."""
    raw = block('job_subfleet') or {}
    if not isinstance(raw, dict) or set(raw) - set(JOB_SUBFLEET):
        raise ValueError('fleet.yaml#job_subfleet must be a mapping with only: ' + ', '.join(JOB_SUBFLEET))
    out = dict(JOB_SUBFLEET, **raw)
    for key in ('supervisor', 'worker'):
        out[key] = check_chain(out[key], 'fleet.yaml#job_subfleet.' + key)
    if out['supervisor'] is not None and out['supervisor_profile'] is not None:
        raise ValueError('fleet.yaml#job_subfleet sets both supervisor (a model chain) and supervisor_profile (a card); '
                         'set one of them to null')
    if out['worker'] is not None and out['worker_profiles']:
        raise ValueError('fleet.yaml#job_subfleet sets both worker (a model chain) and worker_profiles (cards); '
                         'set worker to null or worker_profiles to []')
    out['worker_profiles'] = list(out['worker_profiles'] or [])
    workers, hours = out['workers'], out['hours']
    if type(workers) is not int or workers < 1:
        raise ValueError('fleet.yaml#job_subfleet.workers must be a positive integer, got ' + repr(workers))
    if type(hours) not in (int, float) or not 0 < hours < 1000:
        raise ValueError('fleet.yaml#job_subfleet.hours must be a number of hours in (0, 1000), got ' + repr(hours))
    if out['supervisor_profile'] is not None and not isinstance(out['supervisor_profile'], str):
        raise ValueError('fleet.yaml#job_subfleet.supervisor_profile must be an agent card id or null')
    if not all(isinstance(x, str) for x in out['worker_profiles']):
        raise ValueError('fleet.yaml#job_subfleet.worker_profiles must be a list of agent card ids')
    return out


BARE = {'enabled': True, 'name': 'main', 'lifecycle': 'service', 'home': '~/.fleetmux/fleets/{name}', 'roster': None,
        'workers': None, 'start_args': [], 'window_wait_s': 30, 'window_poll_s': 0.5,
        'host_terminal_env': ['HC_TERMINAL_ID']}
# start flags the bare block may not carry in start_args: the ones bare sets itself, the views (gui_keep must stay
# on), and the CLI / model choice (that is fleet.yaml#discovery's default pair).
BARE_OWN_FLAGS = ('--lifecycle', '--mode', '--name', '--resume', '--no-join', '--fleet-home', '--cmux-state-dir',
                  '--roster', '--workers', '--worker-names', '--kit-fleet', '--no-view', '--gui', '--attach')
BARE_MODEL_FLAGS = ('--supervisor-cli', '--worker-cli', '--supervisor-model', '--worker-model', '--pi-model',
                    '--supervisor-pi-model', '--profile', '--supervisor-profile', '--worker-profile')


ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def host_terminal_env():
    """bare.host_terminal_env (docs/design.md section 50): the environment variables that mark a host terminal, a
    terminal that is itself a pane of another program; bare `fleetmux` typed there attaches in place. A tuple of
    names, () when off (``[]``); missing from the block = BARE's list. A bad value is refused (ValueError)."""
    raw = block('bare')
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError('fleet.yaml#bare must be a mapping with only: ' + ', '.join(BARE))
    value = raw.get('host_terminal_env', BARE['host_terminal_env'])
    if not isinstance(value, list) or not all(isinstance(n, str) and ENV_NAME.fullmatch(n) for n in value):
        raise ValueError('fleet.yaml#bare.host_terminal_env must be a list of environment variable names '
                         '([] = off), got ' + repr(value))
    return tuple(value)


def bare():
    """bare (docs/design.md section 50): the root fleet bare `fleetmux` starts when no root fleet runs. Keys missing
    from the block take BARE; ``home`` comes back with ``{name}`` filled in and ``~`` expanded. A bad value is refused
    (ValueError), before anything starts. ``host_terminal_env`` is checked only by host_terminal_env(), read where a
    window could open, so a bad list does not stop the ssh / no-desktop start."""
    raw = block('bare') or {}
    if not isinstance(raw, dict) or set(raw) - set(BARE):
        raise ValueError('fleet.yaml#bare must be a mapping with only: ' + ', '.join(BARE))
    out = dict(BARE, **raw)

    def bad(key, want):
        raise ValueError('fleet.yaml#bare.' + key + ' must be ' + want + ', got ' + repr(out[key]))

    if type(out['enabled']) is not bool:
        bad('enabled', 'true or false')
    for key in ('name', 'lifecycle', 'home'):
        if not isinstance(out[key], str) or not out[key].strip():
            bad(key, 'a non-empty string')
    if out['roster'] is not None and (not isinstance(out['roster'], str) or not out['roster'].strip()):
        bad('roster', 'a path or null')
    if out['workers'] is not None and (type(out['workers']) is not int or out['workers'] < 1):
        bad('workers', 'a positive integer or null')
    if out['roster'] is not None and out['workers'] is not None:
        raise ValueError('fleet.yaml#bare: roster replaces workers; set only one of them')
    if not isinstance(out['start_args'], list) or not all(isinstance(a, str) for a in out['start_args']):
        bad('start_args', 'a list of strings')
    for arg in out['start_args']:
        flag = arg.split('=', 1)[0]
        if flag in BARE_OWN_FLAGS:
            raise ValueError('fleet.yaml#bare.start_args: ' + flag + ' is set by bare itself (or would turn the '
                             'window off); use the block\'s own keys')
        if flag in BARE_MODEL_FLAGS:
            raise ValueError('fleet.yaml#bare.start_args: ' + flag + ' is not set here: the models come from '
                             'fleet.yaml#discovery\'s default pair')
    for key in ('window_wait_s', 'window_poll_s'):
        if type(out[key]) not in (int, float) or out[key] <= 0:
            bad(key, 'seconds > 0')
    out['name'] = out['name'].strip()
    out['home'] = os.path.expanduser(out['home'].replace('{name}', out['name']))
    if out['roster'] is not None:
        out['roster'] = os.path.expanduser(out['roster'])
    out['start_args'] = list(out['start_args'])
    return out


def relay(*, hot=None):
    """relay (fleetmux/relay/): the validated relay block as a fleetmux.relay.params.Settings, shipped values with
    the private block laid over them key by key. The one accessor for the daemon (`.enabled`) and the relay code
    (cold attributes, `.coordinator`, `.hot(key)`). `hot` is the hot-value store (default: the block's own values;
    the running relay passes its Redis store). A bad block raises ValueError (relay ConfigError)."""
    from fleetmux.relay import params
    return params.load(hot=hot)


def dsh_default_model():
    """dsh.default_model (docs/design.md §41): the provider/model a dsh worker runs without --dsh-model."""
    value = (block('dsh') or {}).get('default_model')
    if not isinstance(value, str) or value.count('/') < 1 or value != value.strip():
        raise ValueError('fleet.yaml#dsh.default_model must be provider/model, got ' + repr(value))
    return value
