"""Session environment from an applet (docs/distributed-development.md, section "Session environment from an applet").

An applet entry may declare ``session_env: {port, vars, clis}``: the loopback port it serves, the variables a
session needs to use it (for example ``HTTPS_PROXY``), and the CLIs that take them. This module holds all the
logic; the launch and daemon code only call it.

- ``errors`` / ``conflicts``: validate the declaration (the roster is refused as a whole on any error).
- ``update_config`` / ``carry``: the root writes the union of all declarations into its run ``config.json``
  as ``session_env``; a child config copies the key at up.
- ``routes`` / ``environ`` / ``apply`` / ``settings_env``: the final environment of one CLI (rules 1, 2, 4, 5).
- ``wait``: the launch wait of ``fleet.actor`` (fail closed, ``route_wait=1`` on the slot hash).
- ``held`` / ``route_closed``: what the daemon checks before a startup timeout, a StopFailure re-delivery or a
  watchdog rotation.

Without a ``session_env`` key in the run config every function here is a no-op.
"""
import os
import re
import socket
import time

from fleetmux import awake, fleet_config

KEYS = ('port', 'vars', 'clis')
PROXY_VARS = ('ALL_PROXY', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY')  # cleared in upper and lower case (rule 2)
LOOPBACK = ('127.0.0.1', 'localhost', '::1')  # always exempt (rule 1)
ENV_NAME_RE = r'[A-Za-z_][A-Za-z0-9_]*'
SETTINGS = {'verified_clis': ['claude'], 'wait_interval_s': 2, 'probe_timeout_s': 1}  # without the block


def settings():
    """``fleet.yaml#session_env``: verified_clis (list of CLI names), wait_interval_s, probe_timeout_s."""
    block = fleet_config.block('session_env')
    out = dict(SETTINGS, **(block if isinstance(block, dict) else {}))
    clis = out['verified_clis']
    if not isinstance(clis, list) or not all(isinstance(c, str) and c for c in clis):
        raise ValueError('fleet.yaml#session_env.verified_clis must be a list of CLI names')
    for name in ('wait_interval_s', 'probe_timeout_s'):
        value = out[name]
        if type(value) not in (int, float) or not 0 < value <= 600:
            raise ValueError('fleet.yaml#session_env.' + name + ' must be a number in (0, 600], got ' + repr(value))
    return out


def errors(value, verified=None):
    """Problems of one applet's ``session_env`` value (empty list = valid)."""
    verified = settings()['verified_clis'] if verified is None else verified
    if not isinstance(value, dict) or set(value) != set(KEYS):
        return ['session_env must be a mapping with exactly the keys ' + ', '.join(KEYS)]
    out = []
    port = value['port']
    if type(port) is not int or not 1 <= port <= 65535:
        out.append('session_env.port must be a port number (1-65535)')
    names = value['vars']
    if not isinstance(names, dict) or not names:
        out.append('session_env.vars must be a non-empty mapping of variable names to strings')
    else:
        for k, v in names.items():
            if not (isinstance(k, str) and re.fullmatch(ENV_NAME_RE, k)):
                out.append('session_env.vars: bad variable name ' + repr(k))
            elif not isinstance(v, str) or '\0' in v or '\n' in v:
                out.append('session_env.vars.' + k + ' must be a one-line string')
    clis = value['clis']
    if not isinstance(clis, list) or not clis or not all(isinstance(c, str) for c in clis) or len(set(clis)) != len(clis):
        out.append('session_env.clis must be a non-empty list of distinct CLI names')
    else:
        out += ['session_env.clis: ' + c + ' is not in fleet.yaml#session_env.verified_clis ('
                + ', '.join(verified) + ')' for c in clis if c not in verified]
    return out


def conflicts(entries):
    """A session never mixes two routes: at most one applet in a roster routes a given CLI."""
    seen, out = {}, []
    for entry in entries:
        value = entry.get('session_env')
        clis = value.get('clis') if isinstance(value, dict) else None
        for cli in clis if isinstance(clis, list) else []:
            if cli in seen:
                out.append('applets ' + seen[cli] + ' and ' + entry['name'] + ': both route ' + cli
                           + ' through session_env (one route per CLI)')
            else:
                seen[cli] = entry['name']
    return out


def _declared(applets):
    return [{'applet': a['name'], 'port': a['session_env']['port'], 'vars': dict(a['session_env']['vars']),
             'clis': list(a['session_env']['clis'])} for a in applets if a.get('session_env')]


def tree(applets, subfleets):
    """The union of the declarations of this roster's applets and of its sub-fleets' roster files."""
    from fleetmux import roster  # roster validates through this module
    out = _declared(applets)
    for sub in subfleets or []:
        try:
            more = _declared(roster.load_applets(sub['roster'])) if sub.get('roster') else []
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue  # an unreadable child roster is the child daemon's to reject
        out += [d for d in more if d not in out]
    return out


def update_config(cfg, applets, subfleets):
    """Set ``cfg['session_env']`` on the root (no key when empty). -> True when it changed. A child keeps the
    key its config got at up."""
    if cfg.get('parent_config'):
        return False
    new = tree(applets, subfleets)
    if new == (cfg.get('session_env') or []):
        return False
    if new:
        cfg['session_env'] = new
    else:
        cfg.pop('session_env', None)
    return True


def carry(stored, cfg):
    """``stored`` (a config dict about to be written) with ``cfg``'s ``session_env`` key, or without one."""
    out = dict(stored)
    out.pop('session_env', None)
    if cfg.get('session_env'):
        out['session_env'] = cfg['session_env']
    return out


def routes(cfg, cli):
    """The declarations that route ``cli``: it is in their ``clis`` and in ``verified_clis`` (rule 4)."""
    declared = (cfg or {}).get('session_env')
    if not declared or cli not in settings()['verified_clis']:
        return []
    return [d for d in declared if cli in d.get('clis', ())]


def route_vars(found):
    """Declared variables plus ``NO_PROXY`` and ``no_proxy`` = the applet's value + loopback (rule 1)."""
    out, exempt = {}, []
    for d in found:
        for k, v in d['vars'].items():
            if k.upper() == 'NO_PROXY':
                exempt += [x.strip() for x in v.split(',') if x.strip()]
            else:
                out[k] = v
    exempt = list(dict.fromkeys(exempt + list(LOOPBACK)))
    out['NO_PROXY'] = out['no_proxy'] = ','.join(exempt)
    return out


def environ(cfg, cli, base):
    """The environment ``cli`` runs with, from ``base`` (never changed). Unrouted: ``base`` itself. Routed:
    inherited proxy variables removed in both cases (rule 2), then the route's variables set."""
    found = routes(cfg, cli)
    if not found:
        return base
    out = {k: v for k, v in base.items() if k.upper() not in PROXY_VARS}
    out.update(route_vars(found))
    return out


def apply(cfg, cli, env=None):
    """Rules 1 and 2 on the launched process's own environment (``os.environ`` in ``fleet.actor``, just before
    exec). The daemon never calls this (rule 5)."""
    env = os.environ if env is None else env
    new = environ(cfg, cli, dict(env))
    if new is not env and routes(cfg, cli):
        env.clear()
        env.update(new)


def settings_env(cfg, cli, env):
    """The ``env`` of a Claude ``--settings`` file: ``env`` plus the route's variables when routed."""
    found = routes(cfg, cli)
    return dict(env, **route_vars(found)) if found else env


def run_kwargs(cfg, cli):
    """``{'env': ...}`` for a one-shot ``subprocess.run`` of ``cli`` when routed, else ``{}``."""
    return {'env': environ(cfg, cli, dict(os.environ))} if routes(cfg, cli) else {}


def port_open(port, timeout):
    try:
        socket.create_connection(('127.0.0.1', int(port)), timeout=timeout).close()
        return True
    except OSError:
        return False


def route_open(cfg, cli, timeout=None):
    """Every port that routes ``cli`` answers on loopback (True when unrouted)."""
    timeout = settings()['probe_timeout_s'] if timeout is None else timeout
    return all(port_open(p, timeout) for p in sorted({d['port'] for d in routes(cfg, cli)}))


def wait(cfg, cli, r, slot_key, generation, *, ready_s, interval=None, timeout=None, sleep=None):
    """Launch wait (fail closed, rule 3): before exec, hold while the route port is closed. Sets
    ``route_wait=1`` on the slot hash while held; clears it and moves ``startup_deadline`` to now + ``ready_s``
    once the port answers. Raises when this generation is retired or the slot is dead meanwhile."""
    if not routes(cfg, cli):
        return False
    conf = settings()
    interval = conf['wait_interval_s'] if interval is None else interval
    info = r.hgetall(slot_key)
    waited = False
    while not route_open(cfg, cli, timeout):
        if generation != int(info.get('generation', -1)) or info.get('state') == 'dead':
            raise RuntimeError('actor generation was retired or blocked while it waited for the route port')
        if not waited:
            r.hset(slot_key, mapping={'route_wait': 1})
            waited = True
        (sleep or time.sleep)(interval)
        info = r.hgetall(slot_key)
    if waited or info.get('route_wait') == '1':
        fields = {'route_wait': 0}
        if 'startup_deadline' in info:
            fields['startup_deadline'] = awake.of(r).deadline(ready_s)  # the launcher is not the daemon: its clock
        r.hset(slot_key, mapping=fields)
    return waited


def _cli(f, slot):
    from fleetmux import fleet
    try:
        return fleet.agent_cli(f.runtime_cfg(slot), slot)
    except (ValueError, KeyError, AttributeError):
        return None


def held(f, slot, info):
    """The daemon's check before a startup timeout: a routed slot held by its launch wait (like
    ``advice_hold``: no startup timeout, no retry spent, no new generation)."""
    return info.get('route_wait') == '1' and bool(routes(f.cfg, _cli(f, slot)))


def route_closed(f, slot, info=None, now=None):
    """The daemon's check before a StopFailure re-delivery or a watchdog rotation of a running slot. A routed
    slot whose port is closed is held: ``route_wait=1`` set, True returned (start nothing, re-deliver nothing).
    Once the port answers, ``route_wait`` is cleared and False returned. One probe per port per
    ``wait_interval_s`` (callers ask every tick)."""
    found = routes(f.cfg, _cli(f, slot))
    if not found:
        return False
    info = f.agent(slot) if info is None else info
    now = time.time() if now is None else now
    conf = settings()
    cache = f.__dict__.setdefault('route_probe', {})
    closed = False
    for port in sorted({d['port'] for d in found}):
        at, ok = cache.get(port, (0, None))
        if ok is None or now - at >= conf['wait_interval_s']:
            ok = port_open(port, conf['probe_timeout_s'])
            cache[port] = (now, ok)
        closed = closed or not ok
    if closed and info.get('route_wait') != '1':
        f.setf(slot, route_wait=1)
    elif not closed and info.get('route_wait') == '1' and info.get('state') != 'starting':
        f.setf(slot, route_wait=0)  # a starting slot's flag is its launch wait's (actor) to clear
    return closed


def hold_delivery(f, slot, info):
    """The delivery pump's check before it hands queued work to an idle slot: True while the slot is routed and
    its port is closed (``route_wait=1``; the queue stays as it is, in order; no attempt, no generation, no
    timeout of the slot). The first pump after the port answers clears the flag and delivers as usual."""
    return route_closed(f, slot, info)
