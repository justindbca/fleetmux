"""Lifecycle modes: what "done" means for a run and how it is closed (config/fleet.yaml#lifecycle_modes).

A lifecycle (``start --lifecycle <mode>``) is orthogonal to the transport mode. Its ``base`` maps it
onto ``cfg['mode']`` (task | service), which everything below the daemon keeps reading unchanged;
``cfg['lifecycle']`` carries the lifecycle name itself (the cli_pairs table reads it to admit
unverified pairs under ``smoke`` only). The table is re-read whenever its mtime changes, so an edit
(e.g. ``hold_hours``) takes effect on the next read without restarting the daemon.
"""
import copy
import importlib
import importlib.util
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

from cmux_fleet import fleet_config

MODE_NAMES = ('service', 'dev_task', 'program', 'scheduled', 'review_loop', 'smoke')
BASE_MODES = ('task', 'service')
DEFAULT_PATH = None  # None = the lifecycle_modes block of fleet.yaml; a path = that file (blocks or a bare table)
CLOCK_ENV = 'CMUX_FLEET_GLOBAL_CLOCK'  # path to global_clock.py (or its directory)

_lock = threading.Lock()
_cache = {}  # resolved path -> (mtime_ns, table)


def _validate(table):
    """Raise ValueError unless ``table`` is a usable mode table."""
    if not isinstance(table, dict) or table.get('version') != 1:
        raise ValueError('fleet.yaml#lifecycle_modes: version must be 1')
    modes = table.get('modes')
    if not isinstance(modes, dict):
        raise ValueError('fleet.yaml#lifecycle_modes: modes must be a mapping')
    missing = [m for m in MODE_NAMES if m not in modes]
    unknown = sorted(set(modes) - set(MODE_NAMES))
    if missing or unknown:
        raise ValueError('fleet.yaml#lifecycle_modes: modes must be exactly ' + ', '.join(MODE_NAMES)
                         + (' (missing: ' + ', '.join(missing) + ')' if missing else '')
                         + (' (unknown: ' + ', '.join(unknown) + ')' if unknown else ''))
    for name, body in modes.items():
        if not isinstance(body, dict) or body.get('base') not in BASE_MODES:
            raise ValueError('fleet.yaml#lifecycle_modes: modes.' + name + '.base must be one of ' + ', '.join(BASE_MODES))
        bases = body.get('bases', [body['base']])
        if not isinstance(bases, list) or body['base'] not in bases or any(b not in BASE_MODES for b in bases):
            raise ValueError('fleet.yaml#lifecycle_modes: modes.' + name + '.bases must be a list of ' + ', '.join(BASE_MODES)
                             + ' that includes its base')
    if not isinstance(table.get('common', {}), dict):
        raise ValueError('fleet.yaml#lifecycle_modes: common must be a mapping')
    return table


def load_modes(path=None):
    """The mode table, re-read when the file's mtime changes. A broken edit keeps the last good
    table (with a warning on stderr); with no good table yet it raises ValueError."""
    path = path or DEFAULT_PATH
    p = Path(path).expanduser().resolve() if path else None
    with _lock:
        cached = _cache.get(p)
        try:
            mtime = fleet_config.stamp(p)
        except OSError as exc:
            if cached:
                print('fleet.yaml#lifecycle_modes unreadable, keeping last good table: ' + str(exc), file=sys.stderr)
                return copy.deepcopy(cached[1])
            raise ValueError('fleet.yaml#lifecycle_modes unreadable: ' + str(exc)) from exc
        if cached and cached[0] == mtime:
            return copy.deepcopy(cached[1])
        try:
            table = _validate(fleet_config.block('lifecycle_modes', p))
        except Exception as exc:
            if cached:
                print('fleet.yaml#lifecycle_modes rejected, keeping last good table: ' + str(exc), file=sys.stderr)
                return copy.deepcopy(cached[1])
            raise ValueError(str(exc)) from exc
        _cache[p] = (mtime, table)
        return copy.deepcopy(table)


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(out.get(k), dict) and isinstance(v, dict) else copy.deepcopy(v)
    return out


def validate_lifecycle(name, path=None):
    """The lifecycle name, or ValueError listing the valid ones."""
    if name not in MODE_NAMES or name not in load_modes(path)['modes']:
        raise ValueError('unknown --lifecycle ' + repr(name) + '; choose one of: ' + ', '.join(MODE_NAMES))
    return name


def get_mode(name, path=None):
    """The effective policy of one lifecycle: ``common`` deep-merged with ``modes.<name>``."""
    table = load_modes(path)
    validate_lifecycle(name, path)
    return dict(_merge(table.get('common') or {}, table['modes'][name]), name=name)


def mode_policy(name, key, default=None, path=None):
    """One policy value of a lifecycle (re-read on every call: hot reload)."""
    value = get_mode(name, path).get(key)
    return default if value is None else value


def base_mode(name, path=None):
    """The transport mode (cfg['mode']) a lifecycle runs on: 'task' or 'service'."""
    return get_mode(name, path)['base']


def require_mode_on_start(path=None):
    return bool(load_modes(path).get('require_mode_on_start', True))


def resolve_start(lifecycle, mode=None, path=None):
    """(lifecycle, cfg_mode) for ``start``. ``mode`` is an explicit --mode or None.

    Raises ValueError when --lifecycle is missing (and required), unknown, or contradicts --mode.
    Without a lifecycle (only when the table allows it) the old --mode behaviour is kept."""
    if not lifecycle:
        if require_mode_on_start(path):
            raise ValueError('start requires --lifecycle <mode>; choose one of: ' + ', '.join(MODE_NAMES))
        return None, mode or 'task'
    validate_lifecycle(lifecycle, path)
    base = base_mode(lifecycle, path)
    bases = get_mode(lifecycle, path).get('bases') or [base]  # e.g. smoke checks task and service pairs
    if mode and mode not in bases:
        raise ValueError('--lifecycle ' + lifecycle + ' runs on --mode ' + ' | '.join(bases) + ', not --mode ' + mode
                         + ' (drop --mode; the lifecycle decides it)')
    return lifecycle, mode or base


def _clock_module():
    """The operator's global_clock module (``is_trading_day(market, date)``), from $CMUX_FLEET_GLOBAL_CLOCK or sys.path."""
    location = os.environ.get(CLOCK_ENV)
    if location:
        p = Path(location).expanduser()
        if p.is_dir():
            p = p / 'global_clock.py'
        spec = importlib.util.spec_from_file_location('global_clock', p)
        if not spec or not p.is_file():
            raise ImportError(CLOCK_ENV + '=' + location + ': global_clock.py not found')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return importlib.import_module('global_clock')


def clock_gate_problem(lifecycle, clock=None, now=None, path=None):
    """None when the lifecycle may launch now; otherwise the refusal reason.

    Only lifecycles with ``clock_gate: true`` (scheduled) are gated. ``clock`` is an object with
    ``is_trading_day(market, date)`` (default: global_clock); fails closed when it is unavailable."""
    if not lifecycle or not mode_policy(lifecycle, 'clock_gate', False, path):
        return None
    market = mode_policy(lifecycle, 'clock_market', 'US', path)
    try:
        clock = clock or _clock_module()
    except Exception as exc:
        return ('--lifecycle ' + lifecycle + ' has clock_gate on but global_clock is unavailable (' + str(exc)
                + '); set ' + CLOCK_ENV + ' to its global_clock.py')
    if now is None:
        try:
            from zoneinfo import ZoneInfo
            tz = (clock.market_info(market) or {}).get('tz') if hasattr(clock, 'market_info') else None
            now = datetime.now(ZoneInfo(tz)) if tz else datetime.now()
        except Exception:
            now = datetime.now()
    day = now.date() if isinstance(now, datetime) else now
    try:
        trading = clock.is_trading_day(market, day)
    except Exception as exc:
        return '--lifecycle ' + lifecycle + ': global_clock failed for ' + market + ' ' + str(day) + ': ' + str(exc)
    if not trading:
        return ('--lifecycle ' + lifecycle + ': ' + market + ' market closed on ' + str(day)
                + ' (clock_gate); nothing launched')
    return None
