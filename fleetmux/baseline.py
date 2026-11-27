"""The switch baseline: which feature switches a fleet of each shape runs with on this machine.

The baseline is the ``baseline`` block of this machine's fleet-superpowers.yaml (fleetmux/fleet_superpowers.py),
next to ``models`` / ``machines`` / ``workflows``. A person writes it; no code path writes it (it is no hot block,
never flushed back, and a release install does not touch the file). It is read fresh whenever the file's stamp
changes::

    baseline:
      version: 1
      check_every_s: 300            # the daemon check interval
      machines:
        <machine>:                  # the key of this machine's ``machines`` entry (``source: self``)
          root_service: {supervisor_split: true, worker_idle_reclaim_min: 30, lease_reclaim: on}
          job: {lease_reclaim: on}  # shapes: root_service, job, named, attached, split
      exceptions:                   # reason and until are required; an exception past until is checked again
        - {machine: <machine>, shape: root_service, fleet: <name>, switch: worker_idle_reclaim_min, value: off,
           reason: "...", until: 2030-01-31}

A block that cannot be used (bad YAML inside it, an unknown key, an exception without reason or until) turns
off the baseline only: ``load`` returns the error instead of raising, the other blocks of the file stay readable,
and start / resume run as they would without a baseline.

``Block.resolve`` is the one precedence rule; start, start --resume and the daemon check all use it:

1. a switch under a live exception takes the exception's value by default and is neither reported nor applied;
2. a new run: a typed flag starts as typed; against the baseline it is reported as a deviation;
3. a resume: a typed flag in CONTINUE_APPLIED applies as before; against the baseline it is reported;
4. a resume: a typed flag outside CONTINUE_APPLIED is ignored as before (the baseline applies);
5. everything else takes the baseline value.
"""
import collections
import datetime
import json
import os
import re
import sys
from pathlib import Path

import yaml

from . import fleet_superpowers as fs

KEY = 'baseline'
VERSION = 1
DEFAULT_CHECK_EVERY_S = 300
SHAPES = ('root_service', 'job', 'named', 'attached', 'split')
RECORD = 'baseline-start.json'  # <run>/baseline-start.json: what start / resume took and reported (read by the daemon)
TOP_KEYS = ('version', 'check_every_s', 'machines', 'exceptions')
EXCEPTION_KEYS = ('machine', 'shape', 'fleet', 'switch', 'value', 'reason', 'until')
KIT_FLEET_RE = r'[a-z][a-z0-9-]*'  # = fleet.KIT_FLEET_RE
DATE_RE = r'\d{4}-\d{2}-\d{2}'

# where: 'roster' (a key of the run's roster file) or 'config' (a key of the run's config.json).
# reload: 'hot' (changes apply while the run is up) or 'restart' (read when the daemon starts).
# restore: 'auto' (the daemon may set it back through an existing writer) or 'report' (only reported).
# resume_apply: start --resume may write the baseline value into the run's config.json.
# compare: 'value' (equal values) or 'set' (set / not set: a worker kit path differs per machine).
# continue_applied: start --resume applies the typed flag today (fleet.CONTINUE_APPLIED).
Switch = collections.namedtuple('Switch', 'key where reload restore resume_apply compare flag continue_applied')
REGISTRY = collections.OrderedDict((s.key, s) for s in (
    Switch('advisor', 'roster', 'hot', 'auto', False, 'value', 'advisor', True),  # roster.write_advisor
    Switch('max_busy', 'roster', 'hot', 'report', False, 'value', 'max_busy', False),  # no roster writer for it
    Switch('supervisor_split', 'config', 'restart', 'report', True, 'value', 'supervisor_split', False),
    Switch('worker_idle_reclaim_min', 'config', 'restart', 'report', True, 'value', 'worker_idle_reclaim_min', False),
    Switch('lease_reclaim', 'config', 'restart', 'report', True, 'value', 'lease_reclaim', True),
    Switch('worker_kit', 'config', 'restart', 'report', False, 'set', 'worker_kit', False),
    Switch('worker_domains', 'config', 'restart', 'report', False, 'value', 'worker_domains', False),
    Switch('kit_fleet', 'config', 'restart', 'report', False, 'value', 'kit_fleet', False),
))

Exception_ = collections.namedtuple('Exception_', 'machine shape fleet switch value reason until')
Decision = collections.namedtuple('Decision', 'switch value baseline source deviation apply new_run')
Loaded = collections.namedtuple('Loaded', 'block error')


class BaselineError(ValueError):
    """The baseline block cannot be used as written."""


# -- values ---------------------------------------------------------------------------------------------------

def _bool(key, v):
    if v in (True, 'on', 'true'):
        return True
    if v in (False, 'off', 'false'):
        return False
    raise BaselineError(key + ' must be true / false (on / off)')


def _count(key, v):
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise BaselineError(key + ' must be an integer, 0 or above')
    return v


def _reclaim(key, v):
    if v in (None, False, 'off'):  # YAML 1.1 reads a bare off as false: reclaim off = None, as in config.json
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise BaselineError(key + ' must be minutes (a number, 0 or above) or off')
    return v


def _on_off(key, v):
    if v in (True, 'on'):
        return 'on'
    if v in (False, 'off'):
        return 'off'
    raise BaselineError(key + ' must be on or off')


def _kit(key, v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip():
        return v.strip()
    raise BaselineError(key + ' must be true / false (set or not) or a directory path')


def _domains(key, v):
    if not isinstance(v, list) or not all(isinstance(d, str) and d.strip() for d in v):
        raise BaselineError(key + ' must be a list of domain names ([] for none)')
    return [d.strip() for d in v]


def _kit_fleet(key, v):
    if v is None or v is False:
        return None
    if not isinstance(v, str) or not re.fullmatch(KIT_FLEET_RE, v):
        raise BaselineError(key + ' must be a fleet name matching ' + KIT_FLEET_RE)
    return v


NORMALISE = {'advisor': _bool, 'max_busy': _count, 'supervisor_split': _bool, 'worker_idle_reclaim_min': _reclaim,
             'lease_reclaim': _on_off, 'worker_kit': _kit, 'worker_domains': _domains, 'kit_fleet': _kit_fleet}


def normalise(key, value):
    """``value`` of switch ``key`` in the run-config form (BaselineError when it is not a value of that switch)."""
    return NORMALISE[key](key, value)


def same(key, a, b):
    """True when ``a`` and ``b`` (normalised) are the same setting of switch ``key``."""
    if REGISTRY[key].compare == 'set':
        return bool(a) == bool(b)
    if key == 'worker_domains':
        return sorted(a or []) == sorted(b or [])
    return a == b


# -- the block --------------------------------------------------------------------------------------------------

def _until(value):
    """``until`` as written: a date (YYYY-MM-DD, valid through that whole day) or an ISO date and time."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        value = value.isoformat()
    if not isinstance(value, str) or not value.strip():
        raise BaselineError('until must be a date (YYYY-MM-DD) or an ISO date and time')
    value = value.strip()
    try:
        if re.fullmatch(DATE_RE, value):
            datetime.date.fromisoformat(value)
        else:
            datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        raise BaselineError('until must be a date (YYYY-MM-DD) or an ISO date and time, not ' + repr(value)) from exc
    return value


def expired(until, now=None):
    """True once ``until`` has passed (a date: after its last day; a time: compared as given)."""
    now = now or datetime.datetime.now()
    if re.fullmatch(DATE_RE, until):
        return now.date() > datetime.date.fromisoformat(until)
    when = datetime.datetime.fromisoformat(until)
    if when.tzinfo is not None and now.tzinfo is None:
        now = now.astimezone()
    elif when.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return now >= when


def _exception(i, row):
    at = 'exceptions[' + str(i) + ']'
    if not isinstance(row, dict):
        raise BaselineError(at + ' must be a mapping')
    extra = sorted(set(map(str, row)) - set(EXCEPTION_KEYS))
    if extra:
        raise BaselineError(at + ': unknown key(s) ' + ', '.join(extra) + ' (allowed: ' + ', '.join(EXCEPTION_KEYS)
                            + ')')
    for need in ('machine', 'shape', 'switch', 'value', 'reason', 'until'):
        if need not in row:
            raise BaselineError(at + ': ' + need + ' is required')
    if not isinstance(row['reason'], str) or not row['reason'].strip():
        raise BaselineError(at + ': reason must be a non-empty text')
    if not isinstance(row['shape'], str) or row['shape'] not in SHAPES:
        raise BaselineError(at + ': shape must be one of ' + ', '.join(SHAPES))
    if not isinstance(row['switch'], str) or row['switch'] not in REGISTRY:
        raise BaselineError(at + ': unknown switch ' + str(row['switch']) + ' (known: ' + ', '.join(REGISTRY) + ')')
    fleet = row.get('fleet')
    if fleet is not None and (not isinstance(fleet, str) or not fleet.strip()):
        raise BaselineError(at + ': fleet must be a fleet name')
    try:
        value = normalise(row['switch'], row['value'])
        until = _until(row['until'])
    except BaselineError as exc:
        raise BaselineError(at + ': ' + str(exc)) from exc
    return Exception_(str(row['machine']), row['shape'], fleet, row['switch'], value, row['reason'].strip(), until)


class Block:
    """A validated baseline block."""

    def __init__(self, raw):
        if not isinstance(raw, dict):
            raise BaselineError('baseline must be a mapping')
        extra = sorted(set(map(str, raw)) - set(TOP_KEYS))
        if extra:
            raise BaselineError('baseline: unknown key(s) ' + ', '.join(extra) + ' (allowed: ' + ', '.join(TOP_KEYS)
                                + ')')
        if raw.get('version') != VERSION:
            raise BaselineError('baseline.version must be ' + str(VERSION))
        every = raw.get('check_every_s', DEFAULT_CHECK_EVERY_S)
        if isinstance(every, bool) or not isinstance(every, (int, float)) or every <= 0:
            raise BaselineError('baseline.check_every_s must be a number of seconds above 0')
        self.version, self.check_every_s = VERSION, every
        self.raw = raw  # as written (the daemon's Redis read cache keeps it)
        machines = raw.get('machines') or {}
        if not isinstance(machines, dict):
            raise BaselineError('baseline.machines must be a mapping of machine name to shapes')
        self.machines = {}
        for machine, shapes in machines.items():
            at = 'baseline.machines.' + str(machine)
            shapes = shapes or {}
            if not isinstance(shapes, dict):
                raise BaselineError(at + ' must be a mapping of shape to switches')
            out = {}
            for shape, switches in shapes.items():
                if shape not in SHAPES:
                    raise BaselineError(at + ': unknown shape ' + str(shape) + ' (shapes: ' + ', '.join(SHAPES) + ')')
                switches = switches or {}
                if not isinstance(switches, dict):
                    raise BaselineError(at + '.' + shape + ' must be a mapping of switch to value')
                table = {}
                for key, value in switches.items():
                    if key not in REGISTRY:
                        raise BaselineError(at + '.' + shape + ': unknown switch ' + str(key) + ' (known: '
                                            + ', '.join(REGISTRY) + ')')
                    try:
                        table[key] = normalise(key, value)
                    except BaselineError as exc:
                        raise BaselineError(at + '.' + shape + ': ' + str(exc)) from exc
                out[shape] = table
            self.machines[str(machine)] = out
        rows = raw.get('exceptions') or []
        if not isinstance(rows, list):
            raise BaselineError('baseline.exceptions must be a list')
        self.exceptions = [_exception(i, row) for i, row in enumerate(rows)]

    def table(self, machine, shape):
        """{switch: value} of ``shape`` on ``machine``; {} when the block has no such entry."""
        return dict((self.machines.get(machine) or {}).get(shape) or {})

    def exception(self, machine, shape, fleet, switch, now=None):
        """The live exception for this switch (first match), or None. An exception without ``fleet`` covers every
        fleet of that shape on that machine."""
        for row in self.exceptions:
            if (row.machine == machine and row.shape == shape and row.switch == switch
                    and row.fleet in (None, fleet) and not expired(row.until, now)):
                return row
        return None

    def resolve(self, machine, shape, fleet, current, typed, resume=False, now=None):
        """{switch: Decision} for every switch the baseline or a live exception names (module doc, precedence).

        ``current``: {switch: normalised value as the run would have it today} (the typed value for typed ones);
        ``typed``: the switches whose flag the operator typed. Decision.value is the value to run with; ``apply``
        (resume only): write it into the run config; ``deviation``: report it; ``new_run``: it cannot be applied
        on a resume, the fleet needs a new run."""
        table = self.table(machine, shape)
        keys = [k for k in REGISTRY if k in table or self.exception(machine, shape, fleet, k, now)]
        out = {}
        for key in keys:
            sw, base, have = REGISTRY[key], table.get(key), current.get(key)
            exc = self.exception(machine, shape, fleet, key, now)
            if exc is not None:  # 1
                value = have if key in typed or resume else exc.value
                out[key] = Decision(key, value, base, 'exception', False, False, False)
            elif key in typed and (not resume or sw.continue_applied):  # 2, 3
                out[key] = Decision(key, have, base, 'typed', not same(key, have, base), False, False)
            elif resume:  # 4, 5: the run keeps its config unless resume may apply the baseline
                if sw.where != 'config':  # roster switches: the daemon's check
                    out[key] = Decision(key, have, base, 'baseline', False, False, False)
                elif same(key, have, base):
                    out[key] = Decision(key, have, base, 'baseline', False, False, False)
                elif sw.resume_apply:
                    out[key] = Decision(key, base, base, 'baseline', False, True, False)
                else:
                    out[key] = Decision(key, have, base, 'baseline', True, False, True)
            else:  # 5
                out[key] = Decision(key, base, base, 'baseline', False, False, False)
        return out


_cache = {}  # path -> (stamp, Loaded)


def block_text(text):
    """The lines of the top-level ``baseline:`` block in ``text`` ('' when there is none)."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(KEY + r'\s*:', line)), None)
    if start is None:
        return ''
    end = start + 1
    while end < len(lines) and not re.match(r'[^\s#]', lines[end]):
        end += 1
    return ''.join(lines[start:end])


def _stamp(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def load(path=None):
    """Loaded(block, error) of the inventory's baseline block: (None, None) without a block, (None, why) when the
    block cannot be used (never raises). Read again when the file's stamp changes."""
    path = Path(path) if path else fs.file_path()
    stamp = _stamp(path)
    hit = _cache.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    loaded = _load(path, stamp)
    _cache[str(path)] = (stamp, loaded)
    return loaded


def _load(path, stamp):
    if stamp is None:
        return Loaded(None, None)
    try:
        text = path.read_text()
    except OSError as exc:
        return Loaded(None, 'cannot read ' + str(path) + ': ' + str(exc))
    own = block_text(text)
    if not own:
        try:  # a flow-style or odd file: the parsed top level still decides
            data = fs.load_yaml(text)
        except yaml.YAMLError:
            return Loaded(None, None)
        if not isinstance(data, dict) or KEY not in data:
            return Loaded(None, None)
        raw = data[KEY]
    else:
        try:
            data = fs.load_yaml(own)
        except yaml.YAMLError as exc:
            return Loaded(None, 'baseline block of ' + str(path) + ' is not valid YAML: ' + ' '.join(str(exc).split()))
        raw = data.get(KEY) if isinstance(data, dict) else None
    try:
        return Loaded(Block(raw), None)
    except Exception as exc:  # noqa: BLE001 - any shape problem turns the baseline off, never the caller
        return Loaded(None, 'baseline block of ' + str(path) + ' rejected: ' + str(exc))


def machine_name(path=None):
    """This machine's key: the ``machines`` entry with ``source: self``; None when there is none (or the file
    cannot be read: no baseline then)."""
    try:
        return fs.self_name(path)
    except (fs.InventoryError, OSError):
        return None


def fleet_name(cfg):
    """The fleet an exception's ``fleet`` names: a root's ``name``; a sub-fleet's own name (its roster entry), else
    its task id (a sub-fleet's config carries its root's ``name``)."""
    if not cfg.get('parent_config'):
        return cfg.get('name')
    named = cfg.get('named')
    return (named.get('name') if isinstance(named, dict) else None) or cfg.get('task_id') or cfg.get('name')


def shape_of(cfg):
    """The shape of a run config: root_service, job, named, attached, split; None for a root task fleet."""
    if not cfg.get('parent_config'):
        return 'root_service' if cfg.get('mode') == 'service' else None
    if cfg.get('baseline_shape') in SHAPES:  # subfleet.child_config marks it (keys inherited from a parent do not)
        return cfg['baseline_shape']
    if cfg.get('job'):
        return 'job'
    named = cfg.get('named')
    if isinstance(named, dict):
        return 'attached' if named.get('attached') else 'named'
    return 'split'


# -- start / resume / attach ------------------------------------------------------------------------------------

def context(path=None):
    """(block, error, machine) for this machine; block None when there is nothing to apply."""
    loaded = load(path)
    return loaded.block, loaded.error, machine_name(path)


def _show(value):
    return 'off' if value is None else str(value)


def _row(machine, shape, fleet, d, why=None):
    row = {'switch': d.switch, 'value': d.value, 'baseline': d.baseline, 'shape': shape, 'machine': machine,
           'fleet': fleet, 'restore': REGISTRY[d.switch].restore, 'reload': REGISTRY[d.switch].reload,
           'new_run': d.new_run}
    if why:
        row['why'] = why
    return row


def _deviation_line(row):
    tail = ' (needs a new run)' if row.get('new_run') else ''
    return ('start: deviates from baseline: ' + row['switch'] + ' ' + _show(row['value']) + ', baseline '
            + _show(row['baseline']) + ' (' + row['shape'] + ' on ' + str(row['machine']) + ', no exception)'
            + (': ' + row['why'] if row.get('why') else '') + tail)


def save_record(run, record):
    """<run>/baseline-start.json (the daemon's first check reports its deviations)."""
    path = Path(run) / RECORD
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True))
    os.replace(tmp, path)
    return path


def _typed(value):
    """A flag value the operator typed: argparse defaults are None / False (a typed 0 is typed)."""
    return value is not None and value is not False


def _typed_new_run(args):
    """The baseline switches whose flag this start typed."""
    return {k for k in REGISTRY if _typed(getattr(args, REGISTRY[k].flag, None))}


def _current_from_args(args):
    out = {}
    for key in REGISTRY:
        value = getattr(args, REGISTRY[key].flag, None)
        if value is None or value is False:
            continue
        if key == 'worker_domains' and isinstance(value, str):
            value = [d.strip() for d in value.split(',') if d.strip()]
        try:
            out[key] = normalise(key, value)
        except BaselineError:
            out[key] = value
    return out


def apply_new_run(args, shape, path=None, now=None, out=None):
    """A new run of ``shape``: fill the flags not typed from the baseline and report typed ones against it.
    Mutates ``args``; returns the record for <run>/baseline-start.json, or None when there is no baseline for
    this machine and shape (start runs exactly as without one)."""
    out = out or (lambda text: print(text))
    block, error, machine = context(path)
    if error:
        out('start: baseline ignored: ' + error)
        return {'machine': machine, 'shape': shape, 'error': error, 'deviations': [], 'filled': []}
    if block is None or machine is None or shape is None:
        return None
    fleet = (getattr(args, 'name', None) or '').strip() or None
    typed = _typed_new_run(args)
    current = _current_from_args(args)
    decisions = block.resolve(machine, shape, fleet, current, typed, now=now)
    if not decisions:
        return None
    record = {'machine': machine, 'shape': shape, 'fleet': fleet, 'error': None, 'deviations': [], 'filled': []}
    for key in REGISTRY:  # registry order: the kit before its domains and kit fleet
        d = decisions.get(key)
        if d is None:
            continue
        if d.deviation:
            record['deviations'].append(_row(machine, shape, fleet, d))
            continue
        if key in typed:
            continue
        why = _fill(args, key, d.value)
        if why:
            record['deviations'].append(_row(machine, shape, fleet, d._replace(value=current.get(key)), why))
        else:
            record['filled'].append({'switch': key, 'value': d.value, 'source': d.source})
    for row in record['deviations']:
        out(_deviation_line(row))
    return record


def _fill(args, key, value):
    """Set flag ``key`` of a new run to ``value``; the reason when it cannot be set (reported instead)."""
    if key == 'advisor':
        args.advisor = bool(value)
    elif key == 'max_busy':
        args.max_busy = value
    elif key == 'supervisor_split':
        args.supervisor_split = bool(value)
    elif key == 'worker_idle_reclaim_min':
        args.worker_idle_reclaim_min = 'off' if value is None else value
    elif key == 'lease_reclaim':
        args.lease_reclaim = value
    elif key == 'worker_kit':
        if isinstance(value, str):
            args.worker_kit = str(Path(value).expanduser())
        elif value:
            return 'the baseline wants a worker kit and none was given (--worker-kit)'
    elif key == 'worker_domains':
        if value and not getattr(args, 'worker_kit', None):
            return 'worker domains need a worker kit (--worker-kit)'
        args.worker_domains = ','.join(value) if value else None
    elif key == 'kit_fleet':
        if value is None:
            return None
        if not getattr(args, 'worker_kit', None):
            return 'a kit fleet needs a worker kit (--worker-kit)'
        if getattr(args, 'roster', None) or getattr(args, 'workers', None) is not None or getattr(args, 'worker_names', None):
            return 'a kit fleet replaces --roster / --workers / --worker-names, which this start gave'
        args.kit_fleet = value
    return None


def _current_from_cfg(cfg):
    out = {}
    for key in REGISTRY:
        if REGISTRY[key].where != 'config':
            continue
        value = cfg.get(key)
        if key == 'kit_fleet' and isinstance(value, dict):
            value = value.get('name')
        if key == 'lease_reclaim':
            value = value or 'on'  # a run without the key runs with the default
        if key == 'supervisor_split':
            value = bool(value)
        if key == 'worker_idle_reclaim_min' and key not in cfg:
            value = _mode_reclaim(cfg)  # no run key: the fleet.yaml mode table decides (lifecycle_close.tick)
        out[key] = value
    return out


def _mode_reclaim(cfg):
    """worker_idle_reclaim_min of the run's lifecycle mode in fleet.yaml#lifecycle_modes (None when unreadable)."""
    from . import lifecycle
    try:
        return lifecycle.get_mode(cfg.get('lifecycle') or cfg.get('mode') or 'service').get('worker_idle_reclaim_min')
    except Exception:  # noqa: BLE001 - an unreadable table: compared as off
        return None


def apply_resume(cfg, args, run, path=None, now=None, out=None):
    """start --resume of a run with config ``cfg``: write the baseline value of every restart switch that resume may
    apply into ``cfg`` (one printed line per change); report typed flags against the baseline and switches that
    need a new run. Mutates ``cfg``; saves and returns the record, or None without a baseline for this run."""
    out = out or (lambda text: print(text))
    block, error, machine = context(path)
    shape = shape_of(cfg)
    if error:
        out('start: baseline ignored: ' + error)
        record = {'machine': machine, 'shape': shape, 'error': error, 'deviations': [], 'applied': []}
        save_record(run, record)
        return record
    if block is None or machine is None or shape is None:
        return None
    fleet = fleet_name(cfg)
    current = _current_from_cfg(cfg)
    typed = set()
    for key in REGISTRY:
        value = getattr(args, REGISTRY[key].flag, None)
        if _typed(value):
            typed.add(key)
            if REGISTRY[key].continue_applied:  # applied by resume as today (config key or roster switch)
                try:
                    current[key] = normalise(key, value)
                except BaselineError:
                    current[key] = value
    decisions = block.resolve(machine, shape, fleet, current, typed, resume=True, now=now)
    if not decisions:
        return None
    record = {'machine': machine, 'shape': shape, 'fleet': fleet, 'error': None, 'deviations': [], 'applied': []}
    for key, d in decisions.items():
        if d.apply:
            before = current.get(key)
            cfg[key] = d.value
            record['applied'].append({'switch': key, 'before': before, 'after': d.value})
            out('start: baseline: ' + key + ' ' + str(before) + ' -> ' + str(d.value))
        elif d.deviation:
            row = _row(machine, shape, fleet, d)
            record['deviations'].append(row)
            out(_deviation_line(row))
    save_record(run, record)
    return record


def apply_attach(typed, overrides, service, fleet=None, path=None, now=None, err=None):
    """An attaching start (fleetmux/join.py): the sub-fleet it becomes is ``attached`` (service) or a ``job``
    (task). Fills ``overrides`` for the forwarded switches not typed; reports typed ones against the baseline."""
    err = err or (lambda text: sys.stderr.write(text + '\n'))
    shape = 'attached' if service else 'job'
    block, error, machine = context(path)
    if error:
        err('start: baseline ignored: ' + error)
        return
    if block is None or machine is None:
        return
    current = {}
    for key in typed:
        if key in REGISTRY:
            value = overrides.get(key, typed[key])
            try:
                current[key] = normalise(key, value)
            except BaselineError:
                current[key] = value
    decisions = block.resolve(machine, shape, fleet, current, set(current), now=now)
    for key, d in decisions.items():
        if d.deviation:
            err(_deviation_line(_row(machine, shape, fleet, d)))
            continue
        if key in current or key in ('advisor', 'kit_fleet'):  # typed, or never forwarded to a sub-fleet
            continue
        if key == 'supervisor_split' and not service:
            continue
        if key == 'worker_kit':
            if isinstance(d.value, str):
                overrides[key] = str(Path(d.value).expanduser().resolve())
            continue
        if key == 'worker_domains' and d.value and not overrides.get('worker_kit'):
            continue
        overrides[key] = d.value
