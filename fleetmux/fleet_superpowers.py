"""The resource inventory ``fleet-superpowers`` (docs/distributed-development.md, "Resource inventory").

One YAML file per machine, ``<data root>/fleet-superpowers/fleet-superpowers.yaml``, next to ``fleets/``. The data
root is ``paths.default_home()`` (docs/design.md section 50.1): like the private ``fleet.yaml`` and ``quota.json``,
the file is per machine and does not follow ``FLEETMUX_HOME``. ``$FLEETMUX_SUPERPOWERS`` names another file (the
test suite points it at a temporary path). Blocks: ``models``, ``machines``, ``quota``, ``workflows``; the
top-level ``hot`` mapping holds the write-back interval of the hot blocks (fleetmux/fleet_superpowers_hot.py) and
the interval of the daemon refresh of this machine's entry (fleetmux/fleet_superpowers_refresh.py ``daemon_tick``).

The top-level ``baseline`` block (the switch baseline, fleetmux/baseline.py) is written by people only: no code
path writes it, and a broken baseline block never stops ``read()`` from returning the other blocks.

Read side: every reader reads the file fresh, never creates it, and returns empty blocks when the file or a block
is missing. A file that is not valid YAML, or a block of the wrong shape, raises ``InventoryError``. Every reader
takes ``path`` to read another file instead of the default one.

Write side: a write changes one entry (``machines.<name>``, ``workflows.<name>``) or one hot block and leaves every
other byte of the file as it was: the new entry is spliced into the text, and the result is parsed back and
compared before it is written. A block written in flow style (``machines: {a: ...}``) is refused, not rewritten.
Every write takes an exclusive lock on ``<file>.lock``, writes a temporary file in the same directory (mode 0600)
and renames it over the file.
"""
import contextlib
import datetime
import fcntl
import json
import os
import re
import tempfile
import time
from pathlib import Path

import yaml

from . import paths

BLOCKS = ('models', 'machines', 'quota', 'workflows')
DIRECTORY = 'fleet-superpowers'
FILE = 'fleet-superpowers.yaml'
DEFAULT_BIN = '~/fleetmux-current/bin/fleetmux'
ENV = 'FLEETMUX_SUPERPOWERS'
HOT = 'hot'  # top-level settings: {write_back_seconds: N, refresh_seconds: N}
DEFAULT_REFRESH = 600  # seconds between daemon refreshes of machines.<self>, when the file sets no hot.refresh_seconds
LOCK_TIMEOUT = 10.0  # seconds a writer waits for the file lock
SOURCES = ('self', 'copy')
ADDRESS_KINDS = ('lan', 'overlay', 'public')
HOST_KEYS = ('subfleet', 'supervisor_slot', 'supervisor_role', 'roster')
SHA = re.compile(r'[0-9a-f]{7,40}\Z')
ANSWERED = ('2xx', '3xx', '4xx', '5xx', 'none')  # models[].answered: the HTTP class a probe saw
MODEL_SOURCES = ('card', 'listed')  # models[].source: copied from a model card, or listed by the CLI
OLD_QUOTA = None  # None: brake.QUOTA_FILE or <data root>/quota.json, resolved on use (tests point it elsewhere)


class _Loader(yaml.SafeLoader):
    """Safe YAML without the timestamp type: an unquoted ``2030-01-01T10:00:00+00:00`` stays a string."""


_Loader.yaml_implicit_resolvers = {
    first: [r for r in rows if r[0] != 'tag:yaml.org,2002:timestamp']
    for first, rows in yaml.SafeLoader.yaml_implicit_resolvers.items()}


def load_yaml(text):
    """``yaml.safe_load`` with timestamps kept as strings (raises ``yaml.YAMLError``)."""
    return yaml.load(text, Loader=_Loader)


class InventoryError(ValueError):
    """The inventory file exists but cannot be used as it is."""


def directory():
    """``<data root>/fleet-superpowers`` (the directory of ``$FLEETMUX_SUPERPOWERS`` when set)."""
    return file_path().parent


def file_path():
    """``<data root>/fleet-superpowers/fleet-superpowers.yaml``, or ``$FLEETMUX_SUPERPOWERS`` when set."""
    env = os.environ.get(ENV)
    if env:
        return Path(env).expanduser()
    return paths.default_home() / DIRECTORY / FILE


def lock_path(path=None):
    path = Path(path) if path else file_path()
    return path.with_name(path.name + '.lock')


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def _load_text(path):
    try:
        return path.read_text()
    except FileNotFoundError:
        return ''
    except OSError as exc:
        raise InventoryError('cannot read %s: %s' % (path, exc)) from exc


def _parse(text, path):
    try:
        data = load_yaml(text)
    except yaml.YAMLError as exc:
        raise InventoryError('%s is not valid YAML: %s' % (path, exc)) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise InventoryError('%s: the top level must be a mapping' % path)
    return data


def _without_baseline(text, path):
    """The file parsed without its ``baseline`` block, when that block alone is not valid YAML (the switch
    baseline, fleetmux/baseline.py, reports its own error: a broken baseline never hides the other blocks).
    Re-raises the original error when the file is broken elsewhere too."""
    from . import baseline
    own = baseline.block_text(text)
    try:
        if not own:
            raise ValueError
        data = _parse(text.replace(own, '', 1), path)
    except (InventoryError, ValueError):
        return _parse(text, path)  # raises the original error
    return data


def read(path=None):
    """The whole file as a dict. The four blocks are always present (``{}`` when missing or empty); other
    top-level keys are kept as they are."""
    path = Path(path) if path else file_path()
    text = _load_text(path)
    try:
        data = _parse(text, path)
    except InventoryError:
        data = _without_baseline(text, path)
    out = {name: data.get(name) or {} for name in BLOCKS}
    for name in BLOCKS:
        if not isinstance(out[name], dict):
            raise InventoryError('%s: block %s must be a mapping' % (path, name))
    out.update((k, v) for k, v in data.items() if k not in BLOCKS)
    return out


def _entry(name, entry, path):
    """One machine entry with ``reach`` (ordered list, ``[]`` when missing) and ``fleetmux.bin`` (``DEFAULT_BIN``
    when missing) filled in. Every other key is returned as written."""
    if entry is None:
        entry = {}
    if not isinstance(entry, dict):
        raise InventoryError('%s: machines.%s must be a mapping' % (path, name))
    out = dict(entry)
    reach = entry.get('reach') or []
    if not isinstance(reach, list) or not all(isinstance(r, dict) for r in reach):
        raise InventoryError('%s: machines.%s.reach must be a list of {via, ssh}' % (path, name))
    out['reach'] = [dict(r) for r in reach]
    tool = entry.get('fleetmux') or {}
    if not isinstance(tool, dict):
        raise InventoryError('%s: machines.%s.fleetmux must be a mapping' % (path, name))
    out['fleetmux'] = dict(tool, bin=tool.get('bin') or DEFAULT_BIN)
    return out


def machines(path=None):
    """The ``machines`` block: ``{name: entry}`` in file order, each entry as ``machine()`` returns it."""
    path = Path(path) if path else file_path()
    return {str(name): _entry(name, entry, path) for name, entry in read(path)['machines'].items()}


def machine(name, path=None):
    """One machine entry, or ``None`` when ``machines`` has no entry of that name."""
    return machines(path).get(name)


def self_name(path=None):
    """The name of the one entry with ``source: self``, or ``None`` when there is none. Two or more raise."""
    found = [name for name, entry in machines(path).items() if entry.get('source') == 'self']
    if len(found) > 1:
        raise InventoryError('more than one machine has source: self: %s' % ', '.join(found))
    return found[0] if found else None


def workflows(path=None):
    """The ``workflows`` block: ``{name: entry}`` in file order, as written."""
    return dict(read(path)['workflows'])


def workflow(name, path=None):
    """One ``workflows`` entry as written, or ``None``."""
    return workflows(path).get(name)


def read_block(block, path=None):
    """One of the hot blocks (``models``, ``quota``) as written; ``{}`` when missing."""
    if block not in ('models', 'quota'):
        raise InventoryError('not a hot block: %s' % block)
    return read(path)[block]


def write_back_seconds(path=None, default=300):
    """``hot.write_back_seconds`` of the file, or ``default``."""
    hot = read(path).get(HOT) or {}
    value = hot.get('write_back_seconds') if isinstance(hot, dict) else None
    return value if _number(value) and value > 0 else default


def refresh_seconds(path=None, default=DEFAULT_REFRESH):
    """``hot.refresh_seconds`` of the file (0 = the daemons never refresh), or ``default``."""
    hot = read(path).get(HOT) or {}
    value = hot.get('refresh_seconds') if isinstance(hot, dict) else None
    return value if _number(value) and value >= 0 else default


# -- validation ------------------------------------------------------------------------------------------

def _text(v):
    return isinstance(v, str) and v.strip() != ''


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _stamp(v):
    if not isinstance(v, str):
        return False
    try:
        return datetime.datetime.fromisoformat(v).utcoffset() is not None
    except ValueError:
        return False


def _ssh(v):
    return _text(v) and not v.startswith('-') and not any(c.isspace() for c in v)


def _rows(value, keys_ok):
    return isinstance(value, list) and all(isinstance(r, dict) and keys_ok(r) for r in value)


def validate_machine(name, entry):
    """Problems of one ``machines`` entry against the schema (docs/distributed-development.md); ``[]`` = valid.
    Keys are optional except ``source``; keys the schema does not name are allowed."""
    at = 'machines.%s' % name
    if not isinstance(entry, dict):
        return [at + ' must be a mapping']
    out = []
    if entry.get('source') not in SOURCES:
        out.append(at + '.source must be self or copy')
    if 'copied_at' in entry and entry.get('source') != 'copy':
        out.append(at + '.copied_at is for copy entries only')
    for key in ('updated_at', 'copied_at'):
        if key in entry and not _stamp(entry[key]):
            out.append('%s.%s must be ISO 8601 with an offset' % (at, key))
    if 'hostname' in entry and not _text(entry['hostname']):
        out.append(at + '.hostname must be a string')
    if 'addresses' in entry and not _rows(entry['addresses'], lambda r: r.get('kind') in ADDRESS_KINDS
                                          and _text(r.get('address'))):
        out.append(at + '.addresses must be a list of {kind: lan|overlay|public, address}')
    reach = entry.get('reach', [])
    if reach is not None and not isinstance(reach, list):
        out.append(at + '.reach must be a list of {via, ssh}')
    else:
        for i, r in enumerate(reach or []):
            if not isinstance(r, dict) or not _ssh(r.get('ssh')):
                out.append('%s.reach[%d].ssh must be a Host alias or user@host (no spaces, no leading -)' % (at, i))
            elif not _text(r.get('via')):
                out.append('%s.reach[%d].via must name the route kind (fleet.yaml#remote.reach_order)' % (at, i))
    tool = entry.get('fleetmux')
    if tool is not None and (not isinstance(tool, dict) or any(k in tool and not _text(tool[k])
                                                               for k in ('bin', 'release'))):
        out.append(at + '.fleetmux must be {bin, release} strings')
    clis = entry.get('clis')
    if clis is not None and not (isinstance(clis, dict) and all(
            isinstance(v, dict) and v.get('logged_in') in (True, False, None)
            and (v.get('version') is None or isinstance(v.get('version'), str)) for v in clis.values())):
        out.append(at + '.clis must map cli -> {version, logged_in}')
    if 'models' in entry and not _rows(entry['models'], lambda r: _text(r.get('id')) and _text(r.get('cli'))
                                       and isinstance(r.get('reachable'), bool)
                                       and (r.get('checked_at') is None or _stamp(r['checked_at']))
                                       and r.get('answered', 'none') in ANSWERED
                                       and r.get('source', 'listed') in MODEL_SOURCES
                                       and (r.get('via') is None or _text(r['via']))):
        out.append(at + '.models must be a list of {id, cli, source, reachable, checked_at, via, answered}')
    if 'can_start_claude' in entry and not isinstance(entry['can_start_claude'], bool):
        out.append(at + '.can_start_claude must be true or false')
    if 'fleets' in entry and not _rows(entry['fleets'], lambda r: _text(r.get('name'))
                                       and isinstance(r.get('running', False), bool)
                                       and isinstance(r.get('root', True), bool)
                                       and all(r.get(k) is None or isinstance(r[k], str)
                                               for k in ('home', 'config', 'mode', 'release', 'parent', 'sub'))):
        out.append(at + '.fleets must be a list of {name, home, config, mode, running, release, root, parent, sub}')
    from . import fleet_superpowers_capability as capability  # hardware, always_on, toolchains
    out += capability.problems(at, entry)
    return out


def validate_workflow(name, entry):
    """Problems of one ``workflows`` entry; only ``kind: workflow`` is checked against the workflow schema (other
    designs may add other kinds). The workflow is never run here."""
    at = 'workflows.%s' % name
    if not isinstance(entry, dict) or not _text(entry.get('kind')):
        return [at + ' must be a mapping with a kind']
    if entry['kind'] != 'workflow':
        return []
    out = []
    if not _text(entry.get('summary')):
        out.append(at + '.summary must be one sentence')
    if not (isinstance(entry.get('applies_when'), list) and entry['applies_when']
            and all(_text(s) for s in entry['applies_when'])):
        out.append(at + '.applies_when must be a list of strings')
    check = entry.get('check')
    if check is not None and not (isinstance(check, dict) and _text(check.get('machine'))
                                  and isinstance(check.get('argv'), list) and check['argv']
                                  and all(_text(a) for a in check['argv'])):
        out.append(at + '.check must be {machine, argv: [strings]}')
    source = entry.get('source') if isinstance(entry.get('source'), dict) else {}
    if not _text(source.get('repo')):
        out.append(at + '.source.repo must be a path')
    if not (isinstance(source.get('ref'), str) and SHA.match(source['ref'])):
        out.append(at + '.source.ref must be a commit sha')
    if not _text(source.get('from')):
        out.append(at + '.source.from must be a machine name')
    applets = entry.get('applets')
    if not (isinstance(applets, list) and applets):
        out.append(at + '.applets must be a non-empty list')
        applets = []
    for i, item in enumerate(applets):
        where = '%s.applets[%d]' % (at, i)
        if not isinstance(item, dict):
            out.append(where + ' must be a mapping')
            continue
        for key in ('machine', 'fleet'):
            if not _text(item.get(key)):
                out.append('%s.%s must be a name' % (where, key))
        host, rows = item.get('host'), item.get('roster_rows')
        if (host is None) == (rows is None):
            out.append(where + ' needs exactly one of host and roster_rows')
        elif host is not None and not (isinstance(host, dict) and all(_text(host.get(k)) for k in HOST_KEYS)):
            out.append(where + '.host must be {' + ', '.join(HOST_KEYS) + '}')
        elif rows is not None and not _text(rows):
            out.append(where + '.roster_rows must be a path')
    saved = entry.get('saved_on')
    if not (isinstance(saved, list) and all(_text(s) for s in saved)):
        out.append(at + '.saved_on must be a list of machine names')
    return out


def validate(data, require_self=False):
    """Problems of a whole file (as ``read()`` returns it); ``[]`` = valid. More than one ``source: self`` is
    always a problem; none is one only with ``require_self`` (a fresh machine has copies before its first
    refresh)."""
    out = []
    if not isinstance(data, dict):
        return ['the top level must be a mapping']
    for block in BLOCKS:
        if not isinstance(data.get(block) or {}, dict):
            out.append('block %s must be a mapping' % block)
    hot = data.get(HOT)
    if hot is not None:
        if not isinstance(hot, dict):
            out.append('hot must be a mapping')
        elif 'write_back_seconds' in hot and not (_number(hot['write_back_seconds'])
                                                  and hot['write_back_seconds'] > 0):
            out.append('hot.write_back_seconds must be a number above 0')
        if isinstance(hot, dict) and 'refresh_seconds' in hot and not (_number(hot['refresh_seconds'])
                                                                       and hot['refresh_seconds'] >= 0):
            out.append('hot.refresh_seconds must be a number, 0 or above')
    if out:
        return out
    found = data.get('machines') or {}
    for name, entry in found.items():
        out += validate_machine(name, entry)
    selves = [n for n, e in found.items() if isinstance(e, dict) and e.get('source') == 'self']
    if len(selves) > 1:
        out.append('more than one machine has source: self: ' + ', '.join(map(str, selves)))
    elif not selves and require_self:
        out.append('no machine has source: self (run `fleetmux superpowers refresh --name <name>`)')
    for name, entry in (data.get('workflows') or {}).items():
        out += validate_workflow(name, entry)
    return out


def _written_problems(data, keys, value, delete):
    """What a write must not introduce: a bad block shape or ``hot`` setting, a bad written entry, a second
    self entry. Other entries are left to ``check`` (a hand-written entry elsewhere never blocks a write)."""
    out = validate({k: v for k, v in data.items() if k not in ('machines', 'workflows')})  # shapes, hot
    if delete or len(keys) < 2:
        return out
    if keys[0] == 'machines':
        out += validate_machine(keys[1], value)
        selves = [n for n, e in (data.get('machines') or {}).items() if isinstance(e, dict) and e.get('source') == 'self']
        if len(selves) > 1:
            out.append('more than one machine has source: self: ' + ', '.join(map(str, selves)))
    elif keys[0] == 'workflows':
        out += validate_workflow(keys[1], value)
    return out


# -- text splicing -----------------------------------------------------------------------------------------

def _indent(line):
    return len(line) - len(line.lstrip(' '))


def _content(line):
    s = line.strip()
    return s != '' and not s.startswith('#')


def _key_of(line):
    try:
        d = yaml.safe_load(line.strip())
    except yaml.YAMLError:
        return None
    return next(iter(d)) if isinstance(d, dict) and len(d) == 1 else None


def _children(lines, start, end, parent):
    """(indent, {key: [first, end]}) of the block-style mapping in lines[start:end] whose keys sit deeper than
    ``parent``; ``None`` when the text is not a plain block mapping."""
    rows = [i for i in range(start, end) if _content(lines[i])]
    if not rows:
        return parent + 2, {}
    ci = _indent(lines[rows[0]])
    if ci <= parent:
        return None
    out, key = {}, None
    for i in rows:
        if _indent(lines[i]) < ci:
            return None
        if _indent(lines[i]) == ci:
            key = _key_of(lines[i])
            if key is None or key in out or lines[i].lstrip().startswith(('-', '?')):
                return None
            out[key] = [i, i + 1]
        out[key][1] = i + 1
    return ci, out


def _dump(key, value, indent):
    text = yaml.safe_dump({key: value}, sort_keys=False, default_flow_style=False, allow_unicode=False, width=1000)
    return [' ' * indent + line if line.strip() else line for line in text.splitlines(keepends=True)]


def _splice(text, keys, value, delete=False):
    """``text`` with ``keys`` (one or two levels) set to ``value`` (or removed), every other line kept."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'
    top = _children(lines, 0, len(lines), -1)
    if top is None or (top[1] and top[0] != 0):
        raise InventoryError('cannot edit the file in place: the top level must be a block style mapping')
    block = keys[0]
    span = top[1].get(block)
    if len(keys) == 1:
        new = [] if delete else _dump(block, value, 0)
        if span is None:
            return ''.join(lines + new)
        return ''.join(lines[:span[0]] + new + lines[span[1]:])
    name = keys[1]
    if span is None:
        return ''.join(lines + ([] if delete else [block + ':\n'] + _dump(name, value, 2)))
    inline = yaml.safe_load(lines[span[0]].strip())[block] if span[1] == span[0] + 1 else None
    if span[1] == span[0] + 1 and inline not in (None, {}):
        raise InventoryError('cannot edit %s in place: write it in block style (one key per line)' % block)
    if span[1] == span[0] + 1:  # "block:" or "block: {}" alone
        new = [] if delete else _dump(name, value, 2)
        return ''.join(lines[:span[0]] + [block + ':\n'] + new + lines[span[1]:])
    kids = _children(lines, span[0] + 1, span[1], 0)
    if kids is None:
        raise InventoryError('cannot edit %s in place: write it in block style (one key per line)' % block)
    indent, found = kids
    new = [] if delete else _dump(name, value, indent)
    if name in found:
        a, b = found[name]
        return ''.join(lines[:a] + new + lines[b:])
    return ''.join(lines[:span[1]] + new + lines[span[1]:])


# -- locked atomic writes ---------------------------------------------------------------------------------

@contextlib.contextmanager
def _locked(path, timeout):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise InventoryError('%s is locked by another writer' % path) from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def _write_atomic(path, text):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _update(path, change, lock_timeout):
    """Run ``change(raw data) -> (keys, value, delete, result)`` under the lock and write the spliced text."""
    path = Path(path) if path else file_path()
    with _locked(path, LOCK_TIMEOUT if lock_timeout is None else lock_timeout):
        text = _load_text(path)
        raw = _parse(text, path)
        read(path)  # the block shapes
        keys, value, delete, result = change(raw)
        expected = json.loads(json.dumps(raw))
        holder = expected
        for k in keys[:-1]:
            if not isinstance(holder.get(k), dict):
                holder[k] = {}
            holder = holder[k]
        if delete:
            holder.pop(keys[-1], None)
        else:
            holder[keys[-1]] = value
        problems = _written_problems(expected, keys, value, delete)
        if problems:
            raise InventoryError('; '.join(problems))
        new = _splice(text, keys, value, delete)
        if _parse(new, path) != expected:
            raise InventoryError('cannot edit %s in place without changing other entries' % '.'.join(keys))
        _write_atomic(path, new)
        return result


def _plain(value):
    """A JSON-plain deep copy (what YAML gives back), so callers' dicts are never shared or changed."""
    return json.loads(json.dumps(value))


# -- the write API (frozen for remote and sync callers) ------------------------------------------------------

def write_self(entry, name=None, path=None, lock_timeout=None, keep_reach=False):
    """Write ``machines.<name>`` whole as this machine's own entry (``source: self``); return the name. ``name``
    defaults to the current self entry; a different name, or the name of a copy, is refused. ``keep_reach``: the
    stored ``reach`` of the self entry, read under the lock, replaces the one in ``entry`` (refresh: an ``add``
    made while it probed is kept)."""
    entry = _plain(entry)

    def change(raw):
        found = raw.get('machines') or {}
        current = [n for n, e in found.items() if isinstance(e, dict) and e.get('source') == 'self']
        target = name or (current[0] if current else None)
        if not target:
            raise InventoryError('no self entry yet: pass the machine name')
        if target in found and target not in current:
            raise InventoryError('%s is a copy of another machine' % target)
        if current and current[0] != target:
            raise InventoryError('this machine is %s, not %s' % (current[0], target))
        entry.pop('copied_at', None)
        if keep_reach and current:
            entry['reach'] = found[target].get('reach') or []
        if keep_reach and current and isinstance(found[target].get('always_on'), bool):
            entry.setdefault('always_on', found[target]['always_on'])  # the owner's setting (capability keys)
        value = dict({'source': 'self'}, **{k: v for k, v in entry.items() if k != 'source'})
        return ('machines', target), value, False, target
    return _update(path, change, lock_timeout)


def import_copy(name, entry, now=None, path=None, lock_timeout=None):
    """Store another machine's section as ``machines.<name>``, replacing any copy whole except ``reach``, marked
    ``source: copy`` and ``copied_at``; return the stored entry. ``reach`` is the union of the copy's entries and
    the section's, in that order, each exact entry once (the picker orders them by kind). The name of this
    machine's self entry is refused."""
    entry = _plain(entry)

    def change(raw):
        found = raw.get('machines') or {}
        current = found.get(name)
        if isinstance(current, dict) and current.get('source') == 'self':
            raise InventoryError('%s is this machine (source: self); its section is written by refresh' % name)
        value = dict({'source': 'copy', 'copied_at': now or now_iso()},
                     **{k: v for k, v in entry.items() if k not in ('source', 'copied_at')})
        ours = current.get('reach') if isinstance(current, dict) else None
        theirs = entry.get('reach')
        if isinstance(ours, list) and (theirs is None or isinstance(theirs, list)):
            merged = []
            for r in ours + (theirs or []):
                if r not in merged:
                    merged.append(r)
            value['reach'] = merged
        return ('machines', name), value, False, value
    return _update(path, change, lock_timeout)


def add_copy(name, ssh, via='lan', now=None, path=None, lock_timeout=None):
    """Add the reach entry ``{via, ssh}`` to ``machines.<name>`` (a copy, or this machine's own entry) and return
    the entry. With no entry of that name, create the minimal copy ``{source: copy, copied_at, reach: [{via,
    ssh}]}`` so the first sync can reach that machine; otherwise append to its ``reach`` and change nothing else.
    A ``via`` that is not a route kind of ``remote.reach_order()``, and an entry with the same ``via`` and
    ``ssh``, are refused."""
    from . import remote
    try:
        kinds = remote.reach_order()
    except ValueError as exc:
        raise InventoryError(str(exc)) from exc
    if via not in kinds:
        raise InventoryError('--via %r is not a route kind; use one of: %s (fleet.yaml#remote.reach_order)'
                             % (via, ', '.join(kinds)))
    new = {'via': via, 'ssh': ssh}

    def change(raw):
        current = _reach_entry(raw, name)
        if current is None:
            value = {'source': 'copy', 'copied_at': now or now_iso(), 'reach': [new]}
        else:
            reach = list(current.get('reach') or [])
            if new in reach:
                raise InventoryError('machines.%s already has the reach entry %s (%s)' % (name, ssh, via))
            value = dict(current, reach=reach + [new])
        return ('machines', name), value, False, value
    return _update(path, change, lock_timeout)


def remove_reach(name, ssh, via=None, path=None, lock_timeout=None):
    """Remove the one reach entry of ``machines.<name>`` (a copy, or this machine's own entry) whose ``ssh`` (and
    ``via``, when given) matches; return the entry. No match, or more than one, is refused."""
    def change(raw):
        current = _reach_entry(raw, name)
        if current is None:
            raise InventoryError('no machine %s' % name)
        reach = list(current.get('reach') or [])
        hits = [i for i, r in enumerate(reach) if r.get('ssh') == ssh and via in (None, r.get('via'))]
        if not hits:
            raise InventoryError('machines.%s has no reach entry %s%s' % (name, ssh, ' (%s)' % via if via else ''))
        if len(hits) > 1:
            raise InventoryError('machines.%s has %d reach entries %s: name one with --via' % (name, len(hits), ssh))
        value = dict(current, reach=reach[:hits[0]] + reach[hits[0] + 1:])
        return ('machines', name), value, False, value
    return _update(path, change, lock_timeout)


def _reach_entry(raw, name):
    """The raw entry ``machines.<name>`` (``None`` when missing)."""
    current = (raw.get('machines') or {}).get(name)
    if current is not None and not isinstance(current, dict):
        raise InventoryError('machines.%s is not a mapping' % name)
    return current


def save_workflow(name, entry, path=None, lock_timeout=None):
    """Write ``workflows.<name>`` whole (validated, never run); return it."""
    entry = _plain(entry)

    def change(raw):
        return ('workflows', name), entry, False, entry
    return _update(path, change, lock_timeout)


def delete_workflow(name, path=None, lock_timeout=None):
    """Remove ``workflows.<name>``; ``True`` when it was there."""
    if workflow(name, path) is None:
        return False

    def change(raw):
        return ('workflows', name), None, True, True
    return _update(path, change, lock_timeout)


def write_block(block, value, path=None, lock_timeout=None):
    """Replace a hot block (``models`` or ``quota``) whole; return the file path."""
    if block not in ('models', 'quota'):
        raise InventoryError('not a hot block: %s' % block)
    value = _plain(value)
    target = Path(path) if path else file_path()

    def change(raw):
        return (block,), value, False, target
    return _update(target, change, lock_timeout)


# -- one-time migration of the hot blocks -------------------------------------------------------------------

def old_location(block):
    """Where a hot block lived before this file: ``models`` = the ``fleetmux setup`` cache (discovered.yaml),
    ``quota`` = the treasurer's quota.json."""
    if block == 'models':
        from . import discover
        return discover.cache_path()
    if block == 'quota':
        from . import brake
        return Path(OLD_QUOTA).expanduser() if OLD_QUOTA else brake.quota_file()
    raise InventoryError('not a hot block: %s' % block)


def migrate(block, path=None, old=None):
    """A hot block from this file. When it is empty here, read its old file once and write it in (the old file
    stays where it is); ``{}`` when neither has it."""
    current = read_block(block, path)
    if current:
        return current
    source = Path(old) if old else old_location(block)
    try:
        data = load_yaml(source.read_text())  # quota.json is JSON, a subset of YAML
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict) or not data:
        return {}
    write_block(block, data, path=path)
    return data
