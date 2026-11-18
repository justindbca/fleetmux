"""Worker roster YAML: which worker slots this fleet runs, and their optional role files.

    workers:
      - name: exit-1          # required; same rules as --worker-names (a new slot ends with -<number>)
        role: roles/exit.md   # optional; relative to this file's directory
      - name: reentry-1
        window: 1000000       # optional handoff overrides for this worker (docs/design.md §22)
        handoff_ratio: 0.7
        profile: flashnext-coder  # optional agent card id: this slot's next generation runs that card
        fresh: always         # optional always | off (default off): a clean session before every dispatch (§45)
      - name: night-1
        pi_model: flashnext/<local-model>  # optional, pi workers only:
        pi_thinking: off      # this slot's own pi model / thinking (not with profile); default --pi-model
    handoff:                  # optional: per CLI / CLI:model (exact runtime model string)
      claude:opus: {window: 1000000}
      codex: {ratio: 0.6}
    split:                    # optional long task split (docs/design.md §44); a row's own `split:` overrides it
      check_minutes: 40       # busy this long -> look at the worker's last window_minutes (0 = never)
      window_minutes: 10
      min_calls: 15           # tool calls in that window ...
      min_distinct: 2         # ... on this many different files / commands = the workflow keeps moving
      restore_model: true     # the slot runs its own model again after its sub-fleet returns
    advisor: off              # optional, default off: on = consult the advisor before every dispatch
    max_busy: 5               # optional: at most this many workers busy at once (0 = no cap); hot, like the
                              # rest of this file; removing the key goes back to the start's --max-busy
    subfleets:                # optional named sub-fleets (docs/design.md §29); worker ``exit-1`` supervises one
      - {name: ops, supervisor_slot: exit-1, supervisor_role: roles/ops-sup.md, roster: ops.yaml, autostart: false}
    tabs:                     # optional companion tabs (docs/design.md §31): terminal programs (no agents)
      - {name: shell, command: python3 -m http.server 8895, cwd: ., env: {PORT: '8895'}}
                              # opened with this fleet, as tabs after control
      - {name: exec, command: python3 exec.py, restart: on-exit,      # optional: reopened when it exits
         backoff: {first_s: 5, max_s: 300}, crashloop: {restarts: 5, minutes: 10}}  # (defaults shown)
    applets:                  # optional applet processes (docs/applet-interface.md section 1); service fleets only
      - {name: applet-rollout-1, command: ["{python}", "-m", "rollout.applet"], cwd: .., durable_state: true}
    import_claude_md:         # optional project rules (user 2026-11-11): each file's text is copied into every
      - ~/projects/app/CLAUDE.md  # agent's AGENTS.md / CLAUDE.md at each spawn (copied, not an @import: a worker
                              # cwd answers Claude Code's "external imports" dialog with No)
    import_mcp:               # optional: these servers of that mcp json are merged into each Claude agent's mcp.json
      - {from: ~/projects/app/.mcp.json, servers: [memory]}

The daemon re-reads the file while running (hot add / graceful remove; advisor on / off without
a restart, also via `fleet advisor on|off` or the supervisor's decision key). Optional `channels:` (docs/design.md
§53, contracts §8): `[{type, account, push?}]`, at most 8, validated by `channels.parse_channels`; only a local
service fleet may have it (start / hot reload refuse it in task mode, a named sub-fleet's roster never has it), and
credentials / whitelists never live here (they are in `channels.config_dir()`). A file with any
problem is rejected as a whole, with one reason per problem; the running roster stays.
A changed `profile` (or `pi_model` / `pi_thinking`) swaps that slot to its next generation once it is idle (same slot name,
docs/design.md §25 night switch); the card itself is resolved and checked by the daemon.
"""
import json
import math
import os
import re
from pathlib import Path

from fleetmux import session_env

NAME_RE = r'[a-z][a-z0-9-]{0,30}'  # same as fleet.WORKER_NAME_RE: no dots (dots mark generations)
PROFILE_RE = r'[a-z][a-z0-9-]{0,63}'  # same as agent_registry.AGENT_ID_RE
# A new worker slot's name ends with -<number> (docs/design.md §3, 2026-10-28): generations read proto-1 ->
# proto-1.1 like worker-1 -> worker-1.1. Checked only where a slot is first created (start, an added row);
# names a run already has are never re-checked, so resume and the stored roster keep working.
SLOT_SUFFIX_RE = r'.*-[0-9]+'


def suffix_errors(names, keep=(), where=None):
    """One message per name in ``names`` (not in ``keep``) without the -<number> suffix. ``where(i, name)``
    labels the entry (default: the name)."""
    return [(where(i, n) if where else n) + ': a worker name must end with -<number> so its generations read '
            + n + '-1, ' + n + '-1.1, ...; rename it ' + n + '-1'
            for i, n in enumerate(names) if n not in keep and not re.fullmatch(SLOT_SUFFIX_RE, n)]


def row_errors(names, keep=()):
    """suffix_errors for the rows of a roster that passed parse_entries (its order is the file's)."""
    return suffix_errors(names, keep, lambda i, n: 'workers[' + str(i) + '] (' + n + ')')


def subfleet_suffix_errors(entries, known=()):
    """row_errors of each sub-fleet roster that is new or retargeted (not in ``known``, the entries this run
    already declared): its workers become slots when it comes up."""
    seen = {(e['name'], e['roster']) for e in known or ()}
    errors = []
    for i, entry in enumerate(entries):
        if (entry['name'], entry['roster']) not in seen:
            errors += ['subfleets[' + str(i) + '] (' + entry['name'] + '): sub-fleet roster ' + entry['roster']
                       + ': ' + e for e in row_errors(load_entries(entry['roster'])[0])]
    return errors


def parse(raw, base):
    """Return (workers, roles): ordered names and {name: absolute role path}. ValueError lists every problem."""
    workers, roles, _ = parse_entries(raw, base)
    return workers, roles


def parse_entries(raw, base):
    """Return (workers, roles, profiles); profiles is {name: agent card id} for rows that set one."""
    try:
        import yaml
    except ImportError as exc:
        raise ValueError('PyYAML is required for --roster (pip install pyyaml)') from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError('invalid YAML: ' + ' '.join(str(exc).split())) from exc
    if (not isinstance(data, dict) or 'workers' not in data
            or not set(data) <= {'workers', 'limits', 'handoff', 'advisor', 'subfleets', 'tabs', 'split', 'max_busy',
                                 'channels', 'applets'} | set(IMPORT_KEYS)
            or not isinstance(data['workers'], list)):
        raise ValueError('the roster must be a mapping with only a "workers" list '
                         '(and optional "limits", "handoff", "advisor", "subfleets", "tabs", "split", "max_busy")')
    parse_limits(data.get('limits'))
    advisor_value(data)
    if not data['workers']:
        raise ValueError('the roster needs at least one worker')
    errors, workers, roles, profiles = handoff_errors(data) + split_errors(data.get('split')), [], {}, {}
    errors += max_busy_errors(data)
    for i, item in enumerate(data['workers']):
        where = 'workers[' + str(i) + ']'
        if not isinstance(item, dict) or not set(item) <= {'name', 'role', 'profile', 'writable', 'split', 'fresh'} | set(WORKER_HANDOFF) | set(PI_KEYS) or 'name' not in item:
            errors.append(where + ': must be a mapping with "name" and optional "role", "profile", "writable", "window",'
                          ' "handoff_ratio", "split", "fresh" only (pi workers also "pi_model", "pi_thinking")')
            continue
        errors += [where + ': ' + e for e in split_errors(item.get('split'))]
        errors += [where + ': ' + e for e in pi_errors(item)]
        roots = item.get('writable')
        if 'writable' in item and not (isinstance(roots, list) and roots
                                       and all(isinstance(r, str) and r.strip() for r in roots)):
            errors.append(where + ': writable must be a non-empty list of paths (extra roots this worker may write)')
        name = item['name']
        if not isinstance(name, str) or not re.fullmatch(NAME_RE, name) or name in ('supervisor', 'advisor'):
            errors.append(where + ': name must match ' + NAME_RE + ' and not be "supervisor" or "advisor" (no dots)')
            continue
        if name in workers:
            errors.append(where + ': duplicate worker name ' + name)
            continue
        workers.append(name)
        if 'fresh' in item and not _fresh_ok(item['fresh']):
            errors.append(where + ' (' + name + '): fresh must be "always" or "off" (got ' + repr(item['fresh']) + ')')
        if item.get('profile') is not None:
            profile = item['profile']
            if not isinstance(profile, str) or not re.fullmatch(PROFILE_RE, profile):
                errors.append(where + ' (' + name + '): profile must be an agent card id matching ' + PROFILE_RE)
                continue
            profiles[name] = profile
        if item.get('role') is not None:
            role = item['role']
            path = (Path(base) / Path(role).expanduser()) if isinstance(role, str) and role.strip() else None
            if path is None or not path.is_file():
                errors.append(where + ' (' + name + '): role file not found: ' + str(role))
                continue
            roles[name] = str(path.resolve())
    errors += subfleet_errors(data.get('subfleets'), base, workers)
    errors += tab_entries(data.get('tabs'), base)[1]
    errors += channel_entries(data.get('channels'))[1]
    errors += applet_entries(data.get('applets'), base, workers, [s.get('name') for s in data.get('subfleets') or []
                                                                  if isinstance(s, dict)])[1]
    errors += import_errors(data, base)
    if errors:
        raise ValueError('\n'.join(errors))
    return workers, roles, profiles


CLIS = ('claude', 'codex', 'pi', 'opencode', 'dsh')  # handoff table keys; opencode / dsh = worker-only CLIs
PI_KEYS = ('pi_model', 'pi_thinking')
THINKING = ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')  # = pi_adapter.THINKING


def _thinking(value):
    return 'off' if value is False else value  # YAML 1.1 reads a bare `off` as false


def pi_errors(item):
    """A row's own pi model / thinking: a provider/model string, a known level, never next to a profile."""
    errors, model = [], item.get('pi_model')
    if 'pi_model' in item and (not isinstance(model, str) or '/' not in model.strip('/') or model != model.strip()):
        errors.append('pi_model must be a pi provider/model string')
    if 'pi_thinking' in item and _thinking(item['pi_thinking']) not in THINKING:
        errors.append('pi_thinking must be one of ' + ', '.join(THINKING))
    if item.get('profile') is not None and any(k in item for k in PI_KEYS):
        errors.append('pi_model / pi_thinking cannot be combined with profile (the card sets the model)')
    return errors


def writable_roots(raw):
    """{name: [extra writable roots]} from an already validated roster (user 2026-11-03)."""
    import yaml
    data = yaml.safe_load(raw)
    return {i['name']: list(i['writable']) for i in data['workers'] if i.get('writable')}


def _fresh_ok(value):
    return value is False or (isinstance(value, str) and value in ('always', 'off'))  # YAML 1.1: bare off = false


def fresh(raw):
    """{name: 'always'} for the rows of an already validated roster that set `fresh: always` (docs/design.md §45)."""
    import yaml
    data = yaml.safe_load(raw)
    return {i['name']: 'always' for i in data['workers'] if i.get('fresh') == 'always'}


def pi_settings(raw):
    """({name: pi model}, {name: pi thinking}) from an already validated roster."""
    import yaml
    data = yaml.safe_load(raw)
    return ({i['name']: i['pi_model'] for i in data['workers'] if i.get('pi_model')},
            {i['name']: _thinking(i['pi_thinking']) for i in data['workers'] if 'pi_thinking' in i})


def pi_cli_problem(pi_models, pi_thinkings, worker_cli):
    """Roster pi settings only make sense on pi worker slots (this run's worker CLI)."""
    rows = sorted(set(pi_models) | set(pi_thinkings))
    if rows and worker_cli != 'pi':
        return ('pi_model / pi_thinking set for ' + ', '.join(rows) + ' but this run\'s workers run '
                + str(worker_cli) + ', not pi (use a profile card to switch a slot to pi)')
    return None
WORKER_HANDOFF = {'window': 'window', 'handoff_ratio': 'ratio'}  # per-worker key -> resolved field


def _bad_window(v):
    return isinstance(v, bool) or not isinstance(v, int) or v < 1


def _bad_ratio(v):
    return isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v < 1


def handoff_errors(data):
    """Validate the handoff table and per-worker overrides (docs/design.md §22)."""
    errors, table = [], data.get('handoff')
    if table is not None and not isinstance(table, dict):
        return ['handoff must be a mapping of "<cli>" or "<cli>:<model>" to {window?, ratio?}']
    for k, v in (table or {}).items():
        where = 'handoff[' + str(k) + ']'
        if not isinstance(k, str) or k.split(':', 1)[0] not in CLIS or k.endswith(':'):
            errors.append(where + ': key must be one of ' + ', '.join(CLIS) + ' or "<cli>:<model>"')
        elif not isinstance(v, dict) or not v or not set(v) <= {'window', 'ratio', 'midturn_ratio'}:
            errors.append(where + ': must be a mapping with "window", "ratio" and/or "midturn_ratio" only')
        else:
            errors += [where + ': window must be a positive integer'] * ('window' in v and _bad_window(v['window']))
            errors += [where + ': ratio must be a number between 0 and 1'] * ('ratio' in v and _bad_ratio(v['ratio']))
            errors += [where + ': midturn_ratio must be a number between 0 and 1'] * (
                'midturn_ratio' in v and _bad_ratio(v['midturn_ratio']))
    for i, item in enumerate(data.get('workers') or []):
        if isinstance(item, dict):
            where = 'workers[' + str(i) + ']'
            errors += [where + ': window must be a positive integer'] * ('window' in item and _bad_window(item['window']))
            errors += [where + ': handoff_ratio must be a number between 0 and 1'] * (
                'handoff_ratio' in item and _bad_ratio(item['handoff_ratio']))
    return errors


def handoff(raw):
    """(table, per_worker) from an already validated roster: {key: {window?, ratio?}}, {name: {window?, ratio?}}."""
    import yaml
    data = yaml.safe_load(raw)
    workers = {}
    for item in data['workers']:
        fields = {WORKER_HANDOFF[k]: item[k] for k in WORKER_HANDOFF if k in item}
        if fields:
            workers[item['name']] = fields
    return dict(data.get('handoff') or {}), workers


def max_busy_errors(data):
    """Validate the optional top-level `max_busy:` (an integer >= 0; 0 = no cap)."""
    v = data.get('max_busy')
    if 'max_busy' in data and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
        return ['max_busy must be an integer >= 0 (0 = no cap), got ' + repr(v)]
    return []


def max_busy(raw):
    """The roster's `max_busy:` from an already validated roster, or None when the file does not set it."""
    import yaml
    return yaml.safe_load(raw).get('max_busy')


SPLIT_KEYS = ('check_minutes', 'window_minutes', 'min_calls', 'min_distinct', 'restore_model')  # = midturn.SPLIT_DEFAULTS


def split_errors(value):
    """Validate a `split:` table, top level or a worker's own (docs/design.md §44)."""
    if value is None:
        return []
    if not isinstance(value, dict):
        return ['split must be a mapping of ' + ', '.join(SPLIT_KEYS)]
    errors = ['split: unknown key ' + str(k) for k in value if k not in SPLIT_KEYS]
    num = [k for k in ('check_minutes', 'window_minutes') if k in value]
    for k in num:
        v = value[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 or (
                k == 'window_minutes' and v == 0):
            errors.append('split.' + k + (' must be a number >= 0 (0 = never)' if k == 'check_minutes'
                                          else ' must be a number > 0'))
    for k in ('min_calls', 'min_distinct'):
        if k in value and (isinstance(value[k], bool) or not isinstance(value[k], int) or value[k] < 1):
            errors.append('split.' + k + ' must be an integer >= 1')
    if 'restore_model' in value and not isinstance(value['restore_model'], bool):
        errors.append('split.restore_model must be true or false')
    return errors


def split(raw):
    """(table, per_worker) from an already validated roster: the top-level `split:` and each row's own."""
    import yaml
    data = yaml.safe_load(raw)
    return (dict(data.get('split') or {}),
            {i['name']: dict(i['split']) for i in data['workers'] if isinstance(i.get('split'), dict)})


LIMIT_KEYS = ('max_depth', 'max_children', 'max_agents_total', 'child_workers_max', 'split_budget_h',
              'job_hot_add_max')  # §58: job_hot_add_max may be 0 (no worker is added for jobs)


def parse_limits(value):
    """Optional sub-fleet limits (docs/design.md §19): positive integers, known keys only."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('limits must be a mapping of ' + ', '.join(LIMIT_KEYS))
    bad = [k for k, v in value.items() if k not in LIMIT_KEYS or isinstance(v, bool) or not isinstance(v, int)
           or v < (0 if k == 'job_hot_add_max' else 1)]
    if bad:
        raise ValueError('limits: unknown key or not a positive integer: ' + ', '.join(map(str, bad)))
    return dict(value)


IMPORT_KEYS = ('import_claude_md', 'import_mcp')


def _import_path(base, value):
    return (Path(base) / Path(value).expanduser()).resolve()


def import_errors(data, base):
    """The optional top-level project imports (user 2026-11-11): ``import_claude_md`` = files whose text is copied
    into every agent's AGENTS.md / CLAUDE.md; ``import_mcp`` = ``[{from: <mcp json>, servers: [names]}]`` merged
    into each Claude agent's mcp.json. Paths are relative to the roster's directory; ``~`` is expanded."""
    errors = []
    if 'import_claude_md' in data:
        paths = data['import_claude_md']
        if not (isinstance(paths, list) and paths and all(isinstance(p, str) and p.strip() for p in paths)):
            errors.append('import_claude_md must be a non-empty list of file paths')
        else:
            errors += ['import_claude_md: file not found: ' + p for p in paths
                       if not _import_path(base, p).is_file()]
    if 'import_mcp' in data:
        entries = data['import_mcp']
        if not (isinstance(entries, list) and entries):
            return errors + ['import_mcp must be a non-empty list of {from: <mcp json file>, servers: [<name>, ...]}']
        for i, entry in enumerate(entries):
            where = 'import_mcp[' + str(i) + ']'
            if (not isinstance(entry, dict) or set(entry) != {'from', 'servers'} or not isinstance(entry['from'], str)
                    or not (isinstance(entry['servers'], list) and entry['servers']
                            and all(isinstance(n, str) and n for n in entry['servers']))):
                errors.append(where + ': must be {from: <mcp json file>, servers: [<name>, ...]}')
                continue
            path = _import_path(base, entry['from'])
            try:
                servers = json.loads(path.read_text()).get('mcpServers')
                if not isinstance(servers, dict):
                    raise ValueError('no "mcpServers" mapping')
            except (OSError, ValueError, AttributeError) as exc:
                errors.append(where + ': cannot read ' + str(path) + ': ' + str(exc)[:200])
                continue
            missing = [n for n in entry['servers'] if not isinstance(servers.get(n), dict)]
            if missing:
                errors.append(where + ': not in ' + str(path) + ' mcpServers: ' + ', '.join(missing))
    return errors


def imports(raw, base):
    """(claude_md_paths, mcp_servers) of an already validated roster: absolute file paths in order, and
    {server name: entry} (the first entry wins a duplicate name). ([], {}) without the keys."""
    import yaml
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        return [], {}
    paths = [str(_import_path(base, p)) for p in data.get('import_claude_md') or []]
    servers = {}
    for entry in data.get('import_mcp') or []:
        found = json.loads(_import_path(base, entry['from']).read_text())['mcpServers']
        for name in entry['servers']:
            servers.setdefault(name, found[name])
    return paths, servers


SERVICE_ONLY = 'channels: 只挂本机常驻 service fleet (mode service, not a task fleet, not a sub-fleet, transport v2)'


def channel_entries(value):
    """(entries, errors) of the optional top-level ``channels:`` (channels.parse_channels: push filled in)."""
    from fleetmux import channels as channels_mod
    return channels_mod.parse_channels(value)


def channels(raw):
    """The ``channels:`` entries of an already validated roster ([] when absent)."""
    import yaml
    data = yaml.safe_load(raw)
    return channel_entries(data.get('channels') if isinstance(data, dict) else None)[0]


def has_channels(raw):
    """True when the roster declares a ``channels:`` key at all (even an empty list)."""
    import yaml
    data = yaml.safe_load(raw)
    return isinstance(data, dict) and data.get('channels') is not None


def load_channels(path):
    return channels(Path(path).expanduser().read_bytes())


def load_limits(path):
    import yaml
    data = yaml.safe_load(Path(path).expanduser().read_bytes())
    return parse_limits(data.get('limits') if isinstance(data, dict) else None)


def load(path):
    path = Path(path).expanduser().resolve()
    return parse(path.read_bytes(), path.parent)


def load_entries(path):
    path = Path(path).expanduser().resolve()
    return parse_entries(path.read_bytes(), path.parent)


ADVISOR_VALUES = {True: True, False: False, 'on': True, 'off': False}  # YAML 1.1 reads bare on/off as booleans


def advisor_value(data):
    """The roster's advisor switch (advisor plan §2.1): True = on. Missing = off."""
    value = data.get('advisor', False) if isinstance(data, dict) else False
    if not isinstance(value, (bool, str)) or value not in ADVISOR_VALUES:
        raise ValueError('advisor must be on or off')
    return ADVISOR_VALUES[value]


def advisor(raw):
    """advisor_value of an already validated roster file's bytes."""
    import yaml
    return advisor_value(yaml.safe_load(raw))


def with_advisor(text, on):
    """The roster text with its top-level advisor line set (other lines and comments untouched)."""
    line = 'advisor: ' + ('on' if on else 'off')
    if re.search(r'(?m)^advisor:.*$', text):
        return re.sub(r'(?m)^advisor:.*$', line, text, count=1)
    return text + ('' if not text or text.endswith('\n') else '\n') + line + '\n'


def write_advisor(path, on, workers=()):
    """Set advisor on/off in the roster file; a missing file is created from ``workers`` (a run started
    without --roster). The result must still be a valid roster; written atomically. Returns the path."""
    path = Path(path).expanduser()
    if path.exists():
        text = path.read_text()
    else:
        text = ('# Created by fleetmux for the advisor switch: the workers of this run.\n'
                '# Edits apply while the run is running, like any roster.\nworkers:\n'
                + ''.join('  - name: ' + w + '\n' for w in workers))
    text = with_advisor(text, on)
    parse(text.encode(), path.parent)  # never write a roster the daemon would reject
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(text)
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------------ named sub-fleets (docs/design.md §29)
SUBFLEET_KEYS = {'name', 'supervisor_slot', 'supervisor_role', 'autostart', 'mode', 'roster', 'claude',
                 'worker_model', 'timeouts', 'max_busy', 'drain_s', 'clear_after_result', 'clear_timeout',
                 'claude_hook_delivery', 'worker_domains', 'worker_cli', 'pi_model', 'pi_thinking',
                 'fresh_after_result'}
SUBFLEET_MODES = ('service', 'task')
SUBFLEET_WORKER_CLIS = ('claude', 'pi')  # §51: opencode / dsh need start-time checks a sub-fleet never runs
DRAIN_S = 600
TIMEOUT_KEYS = {'stall': 'stall_timeout', 'stall_window': 'stall_window',  # entry timeouts -> fleet config key
                'request': 'request_timeout'}


def _positive(v):
    return not isinstance(v, bool) and isinstance(v, (int, float)) and v > 0


def subfleet_entries(value, base, workers):
    """(entries, errors) of the optional ``subfleets:`` list. Each entry is a dict with every key resolved:
    paths absolute, ``timeouts`` split into stall_timeout / stall_window / request_timeout, drain_s defaulted."""
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ['subfleets must be a list of mappings']
    entries, errors, names, slots = [], [], set(), set()
    for i, item in enumerate(value):
        where = 'subfleets[' + str(i) + ']'
        if not isinstance(item, dict) or not set(item) <= SUBFLEET_KEYS:
            errors.append(where + ': must be a mapping with keys ' + ', '.join(sorted(SUBFLEET_KEYS)) + ' only')
            continue
        name = item.get('name')
        if not isinstance(name, str) or not re.fullmatch(NAME_RE, name) or name == 'supervisor':
            errors.append(where + ': name must match ' + NAME_RE + ' and not be "supervisor"')
            continue
        where += ' (' + name + ')'
        bad = []
        if name in names:
            bad.append('duplicate sub-fleet name')
        if name in workers:
            bad.append('name is also a worker name')
        slot = item.get('supervisor_slot')
        if slot not in workers:
            bad.append('supervisor_slot must be one of the workers: ' + ', '.join(workers))
        elif slot in slots:
            bad.append('supervisor_slot ' + slot + ' already supervises another sub-fleet')
        out = {'name': name, 'supervisor_slot': slot, 'autostart': item.get('autostart', False),
               'mode': item.get('mode', 'service'), 'drain_s': item.get('drain_s', DRAIN_S)}
        for key in ('supervisor_role', 'roster'):
            path = item.get(key)
            path = (Path(base) / Path(path).expanduser()) if isinstance(path, str) and path.strip() else None
            if path is None or not path.is_file():
                bad.append(key + ' file not found: ' + str(item.get(key)))
            else:
                out[key] = str(path.resolve())
        if type(out['autostart']) is not bool:
            bad.append('autostart must be true or false')
        if out['mode'] not in SUBFLEET_MODES:
            bad.append('mode must be one of ' + ', '.join(SUBFLEET_MODES))
        for key in ('claude', 'worker_model'):
            if key in item:
                if not isinstance(item[key], str) or not item[key].strip():
                    bad.append(key + ' must be a non-empty string')
                else:
                    out[key] = str(Path(base) / Path(item[key]).expanduser()) if (
                        key == 'claude' and '/' in item[key]) else item[key]
        timeouts = item.get('timeouts')
        if timeouts is not None:
            if not isinstance(timeouts, dict) or not set(timeouts) <= set(TIMEOUT_KEYS) \
                    or not all(_positive(v) for v in timeouts.values()):
                bad.append('timeouts must be a mapping of stall / stall_window / request to positive seconds')
            else:
                out.update({TIMEOUT_KEYS[k]: v for k, v in timeouts.items()})
        if 'max_busy' in item:
            if isinstance(item['max_busy'], bool) or not isinstance(item['max_busy'], int) or item['max_busy'] < 0:
                bad.append('max_busy must be a non-negative integer')
            else:
                out['max_busy'] = item['max_busy']
        for key in ('clear_after_result', 'claude_hook_delivery'):  # §28 / §5 switches of the child itself
            if key in item:
                if type(item[key]) is not bool:
                    bad.append(key + ' must be true or false')
                elif item[key] is (key == 'clear_after_result'):  # §48: both would type into a Claude input box
                    bad.append(key + ': ' + str(item[key]).lower() + ' is retired (typed into the Claude input box; '
                               'user 2026-11-09, docs/design.md §48)')
                else:
                    out[key] = item[key]
        if 'worker_domains' in item:  # the child's own --worker-domains floor; [] = none (not written: the parent's)
            wd = item['worker_domains']
            if not isinstance(wd, list) or not all(isinstance(d, str) and d.strip() for d in wd):
                bad.append('worker_domains must be a list of domain names ([] for none)')
            else:
                out['worker_domains'] = list(dict.fromkeys(d.strip() for d in wd))
        bad += subfleet_cli_errors(item, out)
        if 'clear_timeout' in item:
            if not _positive(item['clear_timeout']) or not math.isfinite(item['clear_timeout']):
                bad.append('clear_timeout must be a positive number of seconds')
            else:
                out['clear_timeout'] = item['clear_timeout']
        if not _positive(out['drain_s']):
            bad.append('drain_s must be a positive number of seconds')
        if 'roster' in out and not bad:
            try:
                profiles = load_entries(out['roster'])[2]
                if has_channels(Path(out['roster']).read_bytes()):
                    raise ValueError(SERVICE_ONLY)
                if out.get('worker_cli') == 'pi' and profiles:  # a card would switch a slot off pi (§51)
                    bad.append('worker_cli: pi sub-fleet roster rows cannot set profile (' + ', '.join(sorted(profiles))
                               + '): every worker runs the entry\'s pi_model')
            except (OSError, ValueError) as exc:
                bad.append('sub-fleet roster ' + out['roster'] + ' rejected: ' + ' '.join(str(exc).split()))
        names.add(name)
        if slot in workers:
            slots.add(slot)
        if bad:
            errors += [where + ': ' + b for b in bad]
        else:
            entries.append(out)
    return entries, errors


def subfleet_cli_errors(item, out):
    """§51: the entry's own worker CLI (claude, the default, or pi with its pi_model / pi_thinking) and the
    per-result fresh switch; valid values are copied into ``out``, the errors returned."""
    bad, cli = [], item.get('worker_cli', 'claude')
    if cli not in SUBFLEET_WORKER_CLIS:
        bad.append('worker_cli must be one of ' + ', '.join(SUBFLEET_WORKER_CLIS))
    elif 'worker_cli' in item:
        out['worker_cli'] = cli
    if cli == 'pi':
        if not item.get('pi_model'):
            bad.append('worker_cli: pi needs pi_model (a pi provider/model string)')
        errors = pi_errors({k: item[k] for k in PI_KEYS if k in item})
        bad += errors
        if not errors:
            out.update({k: _thinking(item[k]) if k == 'pi_thinking' else item[k] for k in PI_KEYS if k in item})
    elif any(k in item for k in PI_KEYS):
        bad.append('pi_model / pi_thinking need worker_cli: pi')
    if 'fresh_after_result' in item:
        if type(item['fresh_after_result']) is not bool:
            bad.append('fresh_after_result must be true or false')
        else:
            out['fresh_after_result'] = item['fresh_after_result']
    return bad


def subfleet_errors(value, base, workers):
    return subfleet_entries(value, base, workers)[1]


def subfleets(raw, base):
    """Validated ``subfleets:`` entries of an already validated roster (empty list without the section)."""
    import yaml
    data = yaml.safe_load(raw)
    workers = [w['name'] for w in data['workers']]
    return subfleet_entries(data.get('subfleets'), base, workers)[0]


def load_subfleets(path):
    path = Path(path).expanduser().resolve()
    return subfleets(path.read_bytes(), path.parent)


# ------------------------------------------------------------------ companion tabs (docs/design.md §31)
TAB_KEYS = {'name', 'command', 'cwd', 'env', 'restart', 'backoff', 'crashloop'}
TAB_RESTART = 'on-exit'  # the one value of ``restart:`` (absent = opened once, §31)
TAB_BACKOFF = {'first_s': 5, 'max_s': 300}  # seconds before the 1st restart, doubling up to max_s
TAB_CRASHLOOP = {'restarts': 5, 'minutes': 10}  # this many restarts within the window = stop restarting
TAB_NAME_RE = r'[a-z][a-z0-9-]*'
ENV_NAME_RE = r'[A-Za-z_][A-Za-z0-9_]*'


def tab_entries(value, base):
    """(entries, errors) of the optional ``tabs:`` list: each entry {name, command, cwd (absolute), env}, plus
    ``restart: {first_s, max_s, restarts, window_s}`` for an entry with ``restart: on-exit`` (§31 keepalive).
    ``cwd`` is relative to the roster's directory (``~`` expanded), default that directory, and must exist
    (the tab runs ``cd <cwd> && exec ...``)."""
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ['tabs must be a list of mappings']
    entries, errors, names = [], [], set()
    for i, item in enumerate(value):
        where = 'tabs[' + str(i) + ']'
        if not isinstance(item, dict) or not set(item) <= TAB_KEYS:
            errors.append(where + ': must be a mapping with keys ' + ', '.join(sorted(TAB_KEYS)) + ' only')
            continue
        name = item.get('name')
        if not isinstance(name, str) or not re.fullmatch(TAB_NAME_RE, name):
            errors.append(where + ': name must match ' + TAB_NAME_RE)
            continue
        where += ' (' + name + ')'
        bad = ['duplicate tab name'] if name in names else []
        names.add(name)
        command = item.get('command')
        if not isinstance(command, str) or not command.strip():
            bad.append('command must be a non-empty string')
        cwd = item.get('cwd', '.')
        path = (Path(base) / Path(cwd).expanduser()) if isinstance(cwd, str) and cwd.strip() else None
        if path is None or not path.is_dir():
            bad.append('cwd directory not found: ' + str(cwd))
        env = item.get('env') or {}
        if not isinstance(env, dict) or not all(isinstance(k, str) and re.fullmatch(ENV_NAME_RE, k)
                                                and isinstance(v, str) for k, v in env.items()):
            bad.append('env must map variable names to strings (quote numbers: PORT: "8895")')
        restart, more = tab_restart(item)
        bad += more
        if bad:
            errors += [where + ': ' + b for b in bad]
        else:
            entry = {'name': name, 'command': command, 'cwd': str(path.resolve()), 'env': dict(env)}
            if restart:
                entry['restart'] = restart
            entries.append(entry)
    return entries, errors


def _positive(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < float('inf')


def tab_restart(item):
    """(restart settings or None, errors) of one tab entry: ``restart: on-exit`` with optional
    ``backoff: {first_s, max_s}`` and ``crashloop: {restarts, minutes}`` (defaults TAB_BACKOFF / TAB_CRASHLOOP)."""
    if 'restart' not in item:
        extra = [k for k in ('backoff', 'crashloop') if k in item]
        return None, ([', '.join(extra) + ' need restart: ' + TAB_RESTART] if extra else [])
    if item['restart'] != TAB_RESTART:
        return None, ['restart must be ' + TAB_RESTART + ' (or left out: opened once)']
    errors, out = [], {}
    for key, defaults in (('backoff', TAB_BACKOFF), ('crashloop', TAB_CRASHLOOP)):
        value = item.get(key) or {}
        if not isinstance(value, dict) or not set(value) <= set(defaults):
            errors.append(key + ' must be a mapping with ' + ', '.join(sorted(defaults)) + ' only')
            continue
        out.update(defaults, **value)
    if errors:
        return None, errors
    if not (_positive(out['first_s']) and _positive(out['max_s']) and out['first_s'] <= out['max_s']):
        errors.append('backoff first_s / max_s must be positive seconds, first_s <= max_s')
    if not (isinstance(out['restarts'], int) and not isinstance(out['restarts'], bool) and out['restarts'] >= 1
            and _positive(out['minutes'])):
        errors.append('crashloop restarts must be an integer >= 1 and minutes a positive number')
    if errors:
        return None, errors
    return {'first_s': float(out['first_s']), 'max_s': float(out['max_s']), 'restarts': out['restarts'],
            'window_s': float(out['minutes']) * 60}, []


def tabs(raw, base):
    """Validated ``tabs:`` entries of an already validated roster (empty list without the section)."""
    import yaml
    return tab_entries(yaml.safe_load(raw).get('tabs'), base)[0]


# ------------------------------------------------------------------ applets (docs/applet-interface.md section 1)
APPLET_KEYS = {'name', 'command', 'cwd', 'env', 'params', 'autostart', 'interface', 'heartbeat_s', 'stale_after_s',
               'stop_grace_s', 'detach_wait_s', 'lease', 'review', 'notify', 'session_env', 'durable_state', 'waits'}
APPLET_NAME_RE = r'applet-[a-z][a-z0-9]*(-[a-z0-9]+)*-[0-9]+'
APPLET_NAME_MAX = 48
APPLET_DEFAULTS = {'autostart': True, 'interface': 1, 'heartbeat_s': 10, 'stale_after_s': 60, 'stop_grace_s': 30,
                   'detach_wait_s': 60, 'durable_state': True}
APPLET_LEASE = {'max_active': 2, 'ttl_s': 1800, 'ttl_max_s': 7200, 'max_renewals': 3, 'renew_margin_s': 300,
                'renew_grace_s': 0, 'prepare_timeout_s': 300, 'eligible': None, 'decision_wait_s': 600,
                'hot_add_max': 2, 'hot_add_like': None}
APPLET_REVIEW = {'max_rounds': 3, 'timeout_s': 1800}
APPLET_NOTIFY = {'max_per_hour': 6}
APPLET_WAITS = ('request_lease', 'renew', 'send', 'release', 'notify')  # SDK calls with a local bound
APPLET_WAIT_GRACE_S = 60  # past the host's own bound (the SDK's ESCALATE_GRACE_S): the host times out on its tick


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _count(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def applet_lease(value, workers):
    """(lease policy with every key of applet-interface.md section 1.2, errors). ``eligible`` defaults to
    ``workers``."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or not set(value) <= set(APPLET_LEASE):
        return None, ['lease must be a mapping with keys ' + ', '.join(sorted(APPLET_LEASE)) + ' only']
    out = dict(APPLET_LEASE, **value)
    if out['eligible'] is None:
        out['eligible'] = list(workers)
    if out['hot_add_like'] is None and isinstance(out['eligible'], list) and out['eligible']:
        out['hot_add_like'] = out['eligible'][0]
    bad = []
    eligible = out['eligible']
    if not (isinstance(eligible, list) and eligible and all(isinstance(n, str) and n in workers for n in eligible)
            and len(set(eligible)) == len(eligible)):
        bad.append('lease.eligible must be a non-empty list of distinct worker names of this roster: '
                   + ', '.join(workers))
    if not (_count(out['max_active']) and out['max_active'] >= 1):
        bad.append('lease.max_active must be an integer >= 1')
    if not _count(out['hot_add_max']):
        bad.append('lease.hot_add_max must be an integer >= 0')
    if not (isinstance(out['hot_add_like'], str) and out['hot_add_like'] in workers):
        bad.append('lease.hot_add_like must be a worker name of this roster')
    if not (_number(out['ttl_max_s']) and out['ttl_max_s'] >= 60):
        bad.append('lease.ttl_max_s must be a number >= 60')
    elif not (_number(out['ttl_s']) and 60 <= out['ttl_s'] <= out['ttl_max_s']):
        bad.append('lease.ttl_s must be a number from 60 to ttl_max_s')
    elif not (_number(out['renew_margin_s']) and 0 < out['renew_margin_s'] < out['ttl_s']):
        bad.append('lease.renew_margin_s must be a number > 0 and < ttl_s')
    if not _count(out['max_renewals']):
        bad.append('lease.max_renewals must be an integer >= 0')
    if not (_number(out['renew_grace_s']) and out['renew_grace_s'] >= 0):
        bad.append('lease.renew_grace_s must be a number >= 0')
    if not (_number(out['prepare_timeout_s']) and out['prepare_timeout_s'] > 0):
        bad.append('lease.prepare_timeout_s must be a number > 0')
    if not (_number(out['decision_wait_s']) and 10 <= out['decision_wait_s'] <= 86400):
        bad.append('lease.decision_wait_s must be a number from 10 to 86400')
    return (None, bad) if bad else (out, [])


def applet_review(value):
    """(review bounds with every key of applet-interface.md section 1.3, errors)."""
    value = {} if value is None else value
    if not isinstance(value, dict) or not set(value) <= set(APPLET_REVIEW):
        return None, ['review must be a mapping with keys ' + ', '.join(sorted(APPLET_REVIEW)) + ' only']
    out, bad = dict(APPLET_REVIEW, **value), []
    if not (_count(out['max_rounds']) and out['max_rounds'] >= 1):
        bad.append('review.max_rounds must be an integer >= 1')
    if not (_number(out['timeout_s']) and 60 <= out['timeout_s'] <= 86400):
        bad.append('review.timeout_s must be a number from 60 to 86400')
    return (None, bad) if bad else (out, [])


def applet_waits(value, lease, review):
    """(local bounds of the SDK calls, applet-interface.md section 1.1, errors). Defaults: the host's own bound of
    each call plus APPLET_WAIT_GRACE_S, so the host answers first while it runs."""
    value = {} if value is None else value
    if not isinstance(value, dict) or not set(value) <= set(APPLET_WAITS):
        return None, ['waits must be a mapping with keys ' + ', '.join(APPLET_WAITS) + ' only']
    grace = APPLET_WAIT_GRACE_S
    out = {'request_lease': lease['decision_wait_s'] + grace, 'renew': lease['decision_wait_s'] + grace,
           'send': lease['prepare_timeout_s'] + lease['ttl_max_s'] + review['timeout_s'] + grace,
           'release': grace, 'notify': grace}
    out.update(value)
    bad = ['waits.' + k + ' must be a number > 0' for k in APPLET_WAITS if not (_number(out[k]) and out[k] > 0)]
    return (None, bad) if bad else (out, [])


def _applet_fields(item, base, workers):
    """(entry without name, problems) of one ``applets:`` row."""
    bad, out = [], dict(APPLET_DEFAULTS)
    command = item.get('command')
    if not (isinstance(command, list) and command and all(isinstance(c, str) and c for c in command)):
        bad.append('command must be a non-empty list of non-empty strings (run with no shell)')
    cwd = item.get('cwd', '.')
    path = (Path(base) / Path(cwd).expanduser()) if isinstance(cwd, str) and cwd.strip() else None
    if path is None or not path.is_dir():
        bad.append('cwd directory not found: ' + str(cwd))
    env = item.get('env') or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and re.fullmatch(ENV_NAME_RE, k)
                                            and not k.startswith('FLEETMUX_') and isinstance(v, str)
                                            for k, v in env.items()):
        bad.append('env must map variable names (not starting with FLEETMUX_) to strings')
    params = item.get('params')
    if params is not None:
        ok = (isinstance(params, dict) and set(params) == {'file', 'block'} and isinstance(params['file'], str)
              and params['file'].strip() and isinstance(params['block'], str) and params['block'].strip())
        if not ok:
            bad.append('params must be {file: <path>, block: <name>}')
        elif not (Path(base) / Path(params['file']).expanduser()).is_file():
            bad.append('params file not found: ' + params['file'])
        else:
            params = {'file': str((Path(base) / Path(params['file']).expanduser()).resolve()),
                      'block': params['block']}
    for key in ('autostart', 'durable_state'):
        out[key] = item.get(key, out[key])
        if not isinstance(out[key], bool):
            bad.append(key + ' must be true or false')
    out['interface'] = item.get('interface', out['interface'])
    if not (_count(out['interface']) and out['interface'] >= 1):
        bad.append('interface must be an integer >= 1')
    for key in ('heartbeat_s', 'stale_after_s', 'stop_grace_s'):
        out[key] = item.get(key, out[key])
        if not (_number(out[key]) and out[key] > 0):
            bad.append(key + ' must be a number > 0')
    if not bad and out['stale_after_s'] <= out['heartbeat_s']:
        bad.append('stale_after_s must be greater than heartbeat_s')
    out['detach_wait_s'] = item.get('detach_wait_s', out['detach_wait_s'])
    if not (_number(out['detach_wait_s']) and out['detach_wait_s'] >= 0):
        bad.append('detach_wait_s must be a number >= 0')
    lease, more = applet_lease(item.get('lease'), workers)
    bad += more
    review, more = applet_review(item.get('review'))
    bad += more
    waits, more = applet_waits(item.get('waits'), lease, review) if lease and review else (None, [])
    bad += more
    notify = dict(APPLET_NOTIFY, **(item.get('notify') or {})) if isinstance(item.get('notify') or {}, dict) else None
    if notify is None or set(notify) != set(APPLET_NOTIFY) or not _count(notify['max_per_hour']):
        bad.append('notify must be {max_per_hour: <integer >= 0>}')
    if bad:
        return None, bad
    out.update(command=list(command), cwd=str(path.resolve()), env=dict(env), params=params, lease=lease,
               review=review, notify=notify, waits=waits)
    if 'session_env' in item:  # accepted as is; validated by the session environment code
        out['session_env'] = item['session_env']
    return out, []


def applet_entries(value, base, workers, others=()):
    """(entries, errors) of the optional ``applets:`` list (docs/applet-interface.md section 1). Each entry has
    every key of section 1.1 resolved (``cwd`` and ``params.file`` absolute, defaults filled in, ``params`` None
    when absent) plus ``lease`` and ``review`` with every key of sections 1.2 and 1.3, ``waits`` with every key and
    ``durable_state``; ``session_env`` is kept
    as written when present. ``others`` are further names an applet may not take (sub-fleet names)."""
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ['applets must be a list of mappings']
    entries, errors, names = [], [], set()
    for i, item in enumerate(value):
        where = 'applets[' + str(i) + ']'
        if not isinstance(item, dict) or not set(item) <= APPLET_KEYS or 'name' not in item:
            errors.append(where + ': must be a mapping with "name" and keys ' + ', '.join(sorted(APPLET_KEYS))
                          + ' only')
            continue
        name = item['name']
        if not (isinstance(name, str) and re.fullmatch(APPLET_NAME_RE, name) and len(name) <= APPLET_NAME_MAX):
            errors.append(where + ': name must match ' + APPLET_NAME_RE + ' (at most ' + str(APPLET_NAME_MAX)
                          + ' characters)')
            continue
        where += ' (' + name + ')'
        bad = ['duplicate applet name'] if name in names else []
        names.add(name)
        if name in workers or name in others:
            bad.append('name is also a worker or sub-fleet name')
        entry, more = _applet_fields(item, base, workers)
        bad += more
        if 'session_env' in item:  # docs/distributed-development.md: refused whole on an unverified CLI
            bad += session_env.errors(item['session_env'])
        if bad:
            errors += [where + ': ' + b for b in bad]
        else:
            entries.append(dict(entry, name=name))
    return entries, errors + session_env.conflicts(entries)


def applets(raw, base):
    """Validated ``applets:`` entries of an already validated roster (empty list without the section)."""
    import yaml
    data = yaml.safe_load(raw)
    workers = [w['name'] for w in data['workers']]
    others = [s.get('name') for s in data.get('subfleets') or [] if isinstance(s, dict)]
    return applet_entries(data.get('applets'), base, workers, others)[0]


def load_applets(path):
    path = Path(path).expanduser().resolve()
    return applets(path.read_bytes(), path.parent)


# ------------------------------------------------------------------ the run's own roster (distributed-development.md)
RUN_COPY = 'roster.yaml'


def copy_into_run(source, run):
    """``start --roster <file outside the run>`` (docs/distributed-development.md open question 5): the run's own copy
    ``<run>/roster.yaml``, which the daemon follows from then on; the source is never read or written again.
    Paths the roster resolves against its own directory (``role``, ``subfleets[].supervisor_role`` / ``roster`` /
    ``claude``, ``tabs[].cwd``, ``applets[].cwd`` / ``params.file``, ``import_claude_md``, ``import_mcp[].from``) are
    written as absolute paths against the source's directory, so they resolve the same; that rewrite drops YAML
    comments. A roster with no such path is copied byte for byte. A file already inside the run is returned as it is."""
    import yaml
    source, run = Path(source).expanduser().resolve(), Path(run).resolve()
    if source.is_relative_to(run):
        return source
    raw = source.read_bytes()
    data = yaml.safe_load(raw)
    target = run / RUN_COPY
    if _absolute_paths(data, source.parent):
        target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding='utf-8')
    else:
        target.write_bytes(raw)
    return target


def _absolute_paths(data, base):
    """Rewrite the base-relative paths of a validated roster in place; True when any value changed."""
    changed = []

    def fix(item, key, default=None):
        value = item.get(key, default)
        if isinstance(value, str) and value.strip():
            new = str(Path(base) / Path(value).expanduser())
            if new != item.get(key):
                item[key] = new
                changed.append(key)

    for item in data.get('workers') or []:
        if isinstance(item, dict) and 'role' in item:
            fix(item, 'role')
    for item in data.get('subfleets') or []:
        if isinstance(item, dict):
            fix(item, 'supervisor_role')
            fix(item, 'roster')
            if isinstance(item.get('claude'), str) and '/' in item['claude']:
                fix(item, 'claude')
    for block in ('tabs', 'applets'):
        for item in data.get(block) or []:
            if isinstance(item, dict):
                fix(item, 'cwd', '.')  # absent: the roster's own directory
                if block == 'applets' and isinstance(item.get('params'), dict):
                    fix(item['params'], 'file')
    paths = data.get('import_claude_md')
    for i, value in enumerate(paths if isinstance(paths, list) else []):
        if isinstance(value, str) and value.strip() and str(Path(base) / Path(value).expanduser()) != value:
            paths[i] = str(Path(base) / Path(value).expanduser())
            changed.append('import_claude_md')
    for item in data.get('import_mcp') or []:
        if isinstance(item, dict):
            fix(item, 'from')
    return bool(changed)
