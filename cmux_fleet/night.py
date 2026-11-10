"""Night switch (docs/design.md §25): same worker slots, another agent card per slot.

`start --roster day.yaml --night-roster night.yaml` names both rosters; they must list the same
worker names (a retired name can never come back, so a swap that renamed slots could not be undone).
`cmux-fleet night on|off` only writes <run>/night.json atomically (the /sleep trigger file). The daemon
reads it at the top of each tick, reloads the roster it points to, and every slot whose `profile`
changed becomes its next generation once idle (in-flight turns finish first). When the last pending
slot is READY, the supervisor gets one breakpoint brief (resume's summary: what is verified / done /
still missing, queued work kept on its slot). The supervisor itself is never switched here.

The brake only receives a placeholder: fx:night {on, brake_card} for the budget code to read.
"""
import importlib
import json
import os
import time
from pathlib import Path

STATE_FILE = 'night.json'
NIGHT = 'fx:night'  # on=1|0, brake_card=night|day, since: read by the brake (placeholder until it exists)


def state_path(run):
    return Path(run) / STATE_FILE


def read_state(run):
    """{'on': bool, ...}; a missing or unreadable file means day."""
    try:
        data = json.loads(state_path(run).read_text())
    except (OSError, ValueError):
        return {'on': False}
    return data if isinstance(data, dict) and isinstance(data.get('on'), bool) else {'on': False}


def write_state(run, on, source='cli'):
    """Atomic write: the daemon never sees half a file."""
    data = {'on': bool(on), 'brake_card': 'night' if on else 'day', 'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'source': source}
    path = state_path(run)
    tmp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    os.replace(tmp, path)
    return data


def roster_for(cfg):
    """The roster file the daemon should follow now (None: this run has no roster)."""
    if cfg.get('roster_night') and read_state(cfg['run'])['on']:
        return cfg['roster_night']
    return cfg.get('roster')


def same_slots(day, night):
    """Both rosters must name the same slots; ValueError otherwise."""
    if set(day) != set(night):
        raise ValueError('the night roster must list the same worker names as the day roster '
                         '(day: ' + ', '.join(day) + '; night: ' + ', '.join(night) + ')')


def optional_module(name):
    """cmux_fleet.<name>, or None while the feature that adds it is not merged."""
    try:
        return importlib.import_module('cmux_fleet.' + name)
    except ModuleNotFoundError as exc:
        if exc.name != 'cmux_fleet.' + name:
            raise
        return None


def pair_module():
    """Feature A's combination table (cmux_fleet.cli_pairs), or None before A is merged."""
    return optional_module('cli_pairs')


def pair_refusal(cfg, table=None):
    """Why a slot's merged config may not run its (supervisor_cli, worker_cli, mode) pair, or None.

    Uses A's interface (A-interface.md v1.2): pair_problem decides (adopt exception first, then
    forbidden / unlisted, adapter_missing even under smoke, unverified only with lifecycle smoke);
    PairTable.lookup only labels the refusal. Before A is merged: runtime_v2.v2_cli_problem."""
    pairs = pair_module()
    if pairs is None:
        from cmux_fleet import runtime_v2
        return runtime_v2.v2_cli_problem(cfg)
    table = table or pairs.load_pairs()
    problem = pairs.pair_problem(cfg, table)
    if problem is None:
        return None
    sup, wrk = cfg.get('supervisor_cli') or 'codex', cfg.get('worker_cli') or 'codex'
    mode = cfg.get('mode') or 'task'
    row = table.lookup(sup, wrk, mode)
    verdict = 'unlisted' if row is None else 'adapter_missing' if row['adapter_missing'] else row['status']
    return sup + '+' + wrk + '/' + mode + ' [' + verdict + ']: ' + problem


def resolve_launches(cfg, profiles, workers=None):
    """{slot: launch} for each roster profile, or ValueError listing every problem.

    With ``workers`` (the whole roster) and feature C's model cards present, the roster must also
    fit the card limits (model_usage.check_slots: max_concurrent per model across slots, hosts).
    Without C nothing here counts slots: max_concurrent is not enforced.

    Before a swap each slot's merged config passes the CLI pair check (pair_refusal on one table
    snapshot per roster, so every slot is judged alike), then runtime_v2.validate_config (which,
    after A, re-reads the same table as a second gate). Nothing is loosened here."""
    from cmux_fleet import profile_launch, runtime_v2
    from cmux_fleet.agent_registry import AgentRegistry, AgentRegistryError
    if not profiles:
        return {}
    try:
        registry = AgentRegistry(cfg.get('agent_registry') or None)
    except AgentRegistryError as exc:
        raise ValueError('agent registry: ' + str(exc)) from exc
    pairs, table = pair_module(), None
    if pairs is not None:
        try:
            table = pairs.load_pairs()
        except (OSError, ValueError) as exc:
            raise ValueError('cli pair table: ' + str(exc)) from exc
    errors, launches = [], {}
    for slot, profile in profiles.items():
        try:
            launch = profile_launch.resolve_launch(registry.get(profile), registry, cfg)
            merged = dict(cfg, **launch['overrides'])
            if merged.get('transport_version', 1) == 2:
                problem = pair_refusal(merged, table)
                if problem:
                    raise ValueError(problem)
            runtime_v2.validate_config(merged)
        except KeyError:
            errors.append(slot + ': unknown agent profile ' + profile)
            continue
        except ValueError as exc:  # ProfileRuntimeError is a ValueError
            errors.append(slot + ': profile ' + profile + ': ' + str(exc))
            continue
        launches[slot] = launch
    usage = optional_module('model_usage')
    if usage is not None and workers is not None:
        errors += [e for e in usage.check_slots(cfg, workers, profiles, registry) if e not in errors]
    if errors:
        raise ValueError('\n'.join(errors))
    return launches


def check_rosters(cfg, day_path, night_path):
    """Validate a day/night roster pair before start or `night on|off`. Returns both parsed rosters."""
    from cmux_fleet import roster
    parsed = {}
    for which, path in (('day', day_path), ('night', night_path)):
        try:
            workers, roles, profiles = roster.load_entries(path)
            resolve_launches(cfg, profiles, workers)
        except (OSError, ValueError) as exc:
            raise ValueError(which + ' roster ' + str(path) + ' rejected:\n' + str(exc)) from exc
        parsed[which] = (workers, roles, profiles)
    same_slots(parsed['day'][0], parsed['night'][0])
    if roster.advisor(Path(day_path).read_bytes()) != roster.advisor(Path(night_path).read_bytes()):
        raise ValueError('the day and night rosters must set the same "advisor" (on / off): the advisor is per run, '
                         'a night on / off must not turn it on or off')
    day = {e['name']: e for e in roster.load_subfleets(day_path)}
    nite = {e['name']: e for e in roster.load_subfleets(night_path)}
    if day != nite:  # paths compared resolved: each file's relative paths may differ, the targets may not
        diff = sorted(n for n in set(day) | set(nite) if day.get(n) != nite.get(n))
        raise ValueError('the day and night rosters must declare the same "subfleets:" (named sub-fleets are per '
                         'run, a night on / off must not add, drop or change one); differing: ' + ', '.join(diff))
    if roster.load_channels(day_path) != roster.load_channels(night_path):  # §53: channels are per run too
        raise ValueError('the day and night rosters must declare the same "channels:" (channels are per run, '
                         'a night on / off must not add, drop or change one)')
    return parsed
