"""Brake: one budget card per task / service, split by quota pool, plus one wall table.

Budget card (task spec ``budget`` or service config ``budget``)::

    {"pool": "pool-a",     # an account id or alias from the pools file (see below)
     "max_usd": 40,        # meter usd_equiv only: equivalent USD spent since the card started
     "max_week_pct": 85,   # meter week_pct only: the pool's weekly quota % (absolute)
     "max_calls": 200,     # deliveries to any agent
     "max_rounds": 60,     # accepted supervisor decisions
     "max_hours": 6,       # soft line (pause); the task ``hours`` stays the hard budget_exhausted
     "max_busy": 2}        # concurrency cap for workers (tighter of this and --max-busy)

Pools (accounts) are the ``accounts`` block of the operator's private fleet.yaml, never this repository
(``$CMUX_FLEET_CONFIG`` or ``~/.fleetmux/fleet.yaml``; format: the comment above ``accounts`` in
config/fleet.yaml). Each account
has an ``id``, optional ``aliases``, a ``meter`` (usd_equiv | week_pct | calls | none) and optional ``caps``.

At a line the daemon pauses (stop delivering and rotating, keep everything resumable) and
notifies. Pool numbers come from ``quota.json`` (the quota treasurer, design v1 batch 1.7);
missing, unreadable or stale means ``unknown``: shown, never trips. Walls (the walls block of fleet.yaml): the
next usable model on the role's chain (fleet.yaml#discovery, user 2026-11-11), or stop.

Pure functions; fleet.py keeps the counters in Redis and calls these.
"""
import json
import math
import re
import time
from pathlib import Path

from cmux_fleet import fleet_config, paths

HERE = Path(__file__).resolve().parent
QUOTA_FILE = None  # None: paths.default_home() / 'quota.json', resolved on use
QUOTA_STALE = 3600  # seconds: an older pool reading is unknown
METER_LINE = {'usd_equiv': ('max_usd', 'cash_used_usd'), 'week_pct': ('max_week_pct', 'week_used_pct')}
INT_LIMITS = ('max_calls', 'max_rounds', 'max_busy')
NUM_LIMITS = ('max_usd', 'max_week_pct', 'max_hours')
# (limit, counter) pairs that pause the fleet; max_busy only caps concurrency.
LINES = (('max_calls', 'calls'), ('max_rounds', 'rounds'), ('max_hours', 'hours'),
         ('max_usd', 'usd'), ('max_week_pct', 'week_pct'))
TURN_WALL_MAX = 400  # a turn text longer than this is a report, not a wall message
DEFAULT_USD = {'day': 40, 'night': 10}  # master design v2 ★3 default cash cap per scene (USD), usd_equiv pools
MODEL_KEY = {'claude': 'worker_model', 'codex': 'codex_model', 'pi': 'pi_model', 'opencode': 'opencode_model',
             'dsh': 'dsh_model'}  # launch override per CLI
SUPERVISOR_MODEL_KEY = {'claude': 'supervisor_model', 'codex': 'supervisor_profile', 'pi': 'pi_model'}
WALL_RESUME = ('[Resumed after a usage wall] Your previous turn on this instruction was cut off by a usage wall '
               'before it reported. Files you already wrote stay. Check your directory first and continue from where '
               'it stopped; do not redo finished steps.\n\n')
GATE_TAIL = 25  # screen lines the watchdog wall gate reads (same bottom-of-screen window as F's prompt table)


_POOLS = {}


def pool_accounts(path=None):
    """{id or alias: account} from the accounts block (fleet.yaml + the private file), cached by stamp."""
    try:
        stamp = fleet_config.stamp(path)
    except OSError:
        return {}
    cached = _POOLS.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    out = {}
    source = fleet_config.source(path)
    for acct in fleet_config.block('accounts', path) or []:
        if not isinstance(acct, dict) or not acct.get('id'):
            raise ValueError(source + ': every account needs an id')
        if acct.get('meter', 'none') not in tuple(METER_LINE) + ('calls', 'none'):
            raise ValueError(source + ': account ' + acct['id'] + ' meter must be usd_equiv / week_pct / calls / none')
        for name in [acct['id']] + list(acct.get('aliases') or []):
            if name in out:
                raise ValueError(source + ': pool name ' + name + ' is used twice')
            out[name] = acct
    _POOLS[str(path)] = (stamp, out)
    return out


def pool_account(name, path=None):
    """The account a budget's pool name (id or alias) refers to, or None."""
    return pool_accounts(path).get(name) if name is not None else None


def pool_meter(name, path=None):
    return (pool_account(name, path) or {}).get('meter')


def validate_budget(raw):
    """Returns a normalized copy, None for no card, or raises ValueError."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        raise ValueError('budget must be a non-empty JSON object')
    known = ('pool',) + INT_LIMITS + NUM_LIMITS
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ValueError('budget may only have ' + ' / '.join(known) + ' (got ' + ', '.join(unknown) + ')')
    pool = raw.get('pool')
    if pool is not None and pool_account(pool) is None:
        raise ValueError('budget pool ' + str(pool) + ' is not an account id or alias in the accounts block of ' + fleet_config.source())
    for k in INT_LIMITS:
        if k in raw and (type(raw[k]) is not int or raw[k] < 1):
            raise ValueError('budget ' + k + ' must be a positive integer')
    for k in NUM_LIMITS:
        v = raw.get(k)
        if k in raw and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0):
            raise ValueError('budget ' + k + ' must be a positive number')
    if raw.get('max_week_pct', 0) > 100:
        raise ValueError('budget max_week_pct must be at most 100')
    for meter, (limit, _) in METER_LINE.items():
        if limit in raw and pool_meter(pool) != meter:
            raise ValueError('budget ' + limit + ' applies only to a pool with meter ' + meter
                             + ' (usd_equiv: walls + equivalent USD; week_pct: walls + weekly quota %)')
    return dict(raw)


def effective(budget, night=False):
    """The card the brake evaluates: a usd_equiv pool card without max_usd gets the pool's cap
    (caps.day_usd / caps.night_usd, else the master-design default day 40 / night 10 USD per scene;
    night = B's fx:night brake_card). A caller's own max_usd always wins."""
    if not budget or pool_meter(budget.get('pool')) != 'usd_equiv' or 'max_usd' in budget:
        return budget
    scene = 'night' if night else 'day'
    caps = pool_account(budget['pool']).get('caps') or {}
    return dict(budget, max_usd=caps.get(scene + '_usd', DEFAULT_USD[scene]))


def parse_raise(items, budget):
    """``resume --raise max_calls=400``: returns the budget with those limits replaced (validated)."""
    out = dict(budget or {})
    for item in items or []:
        name, sep, value = str(item).partition('=')
        if not sep or name not in INT_LIMITS + NUM_LIMITS:
            raise ValueError('--raise takes LIMIT=VALUE with LIMIT one of ' + ', '.join(INT_LIMITS + NUM_LIMITS))
        try:
            out[name] = int(value) if name in INT_LIMITS else float(value)
        except ValueError:
            raise ValueError('--raise ' + name + ' needs a number') from None
    return validate_budget(out) if out else None


def read_quota(path=None):
    """The treasurer's quota.json, or {} when missing / unreadable (never raises)."""
    try:
        data = json.loads(Path(path or QUOTA_FILE or paths.default_home() / 'quota.json').expanduser().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def pool_reading(quota, pool, now=None):
    """The number a pool's card watches (cash_used_usd / week_used_pct), or None = unknown.
    quota.json rows are keyed by account id (a row under the name the card used is read too)."""
    acct = pool_account(pool)
    if (acct or {}).get('meter') not in METER_LINE:
        return None
    rows = quota.get('pools') if isinstance(quota.get('pools'), dict) else {}
    row = rows.get(acct['id'], rows.get(pool))
    if not isinstance(row, dict):
        return None
    at = row.get('at', quota.get('at'))
    value = row.get(METER_LINE[acct['meter']][1])
    if (isinstance(at, bool) or not isinstance(at, (int, float)) or (now or time.time()) - at > QUOTA_STALE
            or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
        return None
    return float(value)


def counters(state, quota, now=None):
    """Current values from the brake hash (calls / rounds / started / usd_base) and quota.json."""
    now = now or time.time()
    budget = json.loads(state.get('budget') or 'null') or {}
    pool = budget.get('pool')
    out = {'calls': int(state.get('calls') or 0), 'rounds': int(state.get('rounds') or 0),
           'hours': round((now - float(state.get('started') or now)) / 3600, 3), 'usd': None, 'week_pct': None}
    reading = pool_reading(quota, pool, now)
    meter = pool_meter(pool)
    if meter == 'usd_equiv' and reading is not None and state.get('usd_base') not in (None, ''):
        out['usd'] = round(reading - float(state['usd_base']), 2)
    elif meter == 'week_pct':
        out['week_pct'] = reading
    return out


def trips(budget, values):
    """Lines reached: [{'limit', 'value', 'cap'}]. Unknown values never trip."""
    return [{'limit': limit, 'value': values.get(counter), 'cap': budget[limit]}
            for limit, counter in LINES
            if (budget or {}).get(limit) is not None and values.get(counter) is not None
            and values[counter] >= budget[limit]]


def describe(found):
    return '; '.join(t['limit'] + ' ' + str(t['value']) + '/' + str(t['cap']) for t in found)


# ------------------------------------------------------------------ walls
_TABLE = {}


def wall_table(path=None):
    """The walls block compiled, cached by stamp."""
    stamp = fleet_config.stamp(path)
    cached = _TABLE.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    raw = fleet_config.block('walls', path) or {}
    table = {'account': [re.compile(p, re.I) for p in raw.get('account') or []],
             'model_pool': [re.compile(p, re.I) for p in raw.get('model_pool') or []],
             'notices': [re.compile(p, re.I) for p in raw.get('notices') or []],
             'same_cli_downgrade': dict(raw.get('same_cli_downgrade') or {}),
             'aliases': dict(raw.get('aliases') or {}), 'default': dict(raw.get('default') or {})}
    _TABLE[str(path)] = (stamp, table)
    return table


def classify_wall(text, table=None):
    """None, or {'scope': 'account' | 'model_pool', 'line': <matched line>}. Account wins.
    A usage reminder line (walls.notices, e.g. "You've used 78% of your weekly limit") is never a wall (§48.4)."""
    table = table or wall_table()
    lines = [x.strip() for x in (text or '').splitlines() if x.strip()]
    lines = [x for x in lines if not any(p.search(x) for p in table.get('notices') or [])]
    for scope in ('account', 'model_pool'):
        for line in lines:
            if any(p.search(line) for p in table[scope]):
                return {'scope': scope, 'line': line[:300]}
    return None


def screen_wall(screen, lines=GATE_TAIL, table=None):
    """Wall on the bottom of a live screen (watchdog gate, E before F), or None."""
    rows = (screen or '').rstrip().splitlines()
    return classify_wall('\n'.join(rows[-lines:]), table)


def account(cli, model):
    """The quota account behind a (cli, model): an official CLI is one login (claude, codex); a bridge CLI (pi /
    opencode / dsh) carries one login per provider, the part of the model id before the first '/'."""
    if cli in ('pi', 'opencode', 'dsh') and '/' in (model or ''):
        return cli + ':' + model.split('/', 1)[0]
    return cli


def wall_action(cli, model, below, walled=(), refused=None):
    """What the brake decides for a wall (user 2026-11-11): the first usable model further down the role's chain,
    or stop. ``below``: [(cli, model)] after the walled one; ``walled``: this run's wall records ({cli, model,
    account, scope}; an account record skips every model of that account, any other only its own model);
    ``refused(cli, model)``: why the pair table refuses that model here, or None."""
    dead = {w.get('account') for w in walled if w.get('scope') == 'account'}
    spent = {(w.get('cli'), w.get('model')): w.get('scope') for w in walled if w.get('scope') != 'account'}
    skipped = []
    for c, m in below:
        why = ('account wall' if account(c, m) in dead
               else {'model_pool': 'model-pool wall'}.get(spent[(c, m)], spent[(c, m)]) if (c, m) in spent
               else refused(c, m) if refused else None)
        if not why:
            return {'action': 'switch', 'cli': cli, 'from': model, 'to_cli': c, 'to': m, 'skipped': skipped}
        skipped.append(c + ':' + m + ' (' + why + ')')
    return {'action': 'stop', 'cli': cli, 'from': model, 'skipped': skipped,
            'advice': ('no usable model left below this one on the chain (fleet.yaml#discovery): '
                       'wait for the reset or restart the slot by hand')}
