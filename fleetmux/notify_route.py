"""Human notices must have somewhere to go (docs/design.md section 24 "No route for human notices").

A root fleet reaches a human through the run's ``channels`` (an entry that pushes run_closed or daemon_down), the tg
command (``$FLEETMUX_TG_CMD`` or the lifecycle mode's ``notify_commands.tg``) or the initiator's mailbox (parent
leg). A root without a lifecycle never calls ``lifecycle_close.notify``, so only its channels reach a human. With
none of these, ``route_problem`` names it; ``start``, the daemon start and ``status`` say so. Every notice for a human
that left on no leg is counted in ``<run>/notify-undelivered.json`` ({count, last: {event, text, at}}; the full text
of the latest one). A fleet with a parent reports upwards and is never checked here.

Switch: ``fleet.yaml#daemon_watch.no_route_alarm`` (bool, default true). Off: no warning, no count.
"""
import json
import os
import sys
import time
from pathlib import Path

from fleetmux import fleet_config

DEFAULTS = {'no_route_alarm': True}
FILE = 'notify-undelivered.json'
UNSENT = ('no_channel', 'no_initiator')
PROBLEM = ('human notices have nowhere to go: no channels entry that pushes run_closed or daemon_down, no '
           'FLEETMUX_TG_CMD or notify_commands.tg, no initiator')
NO_LIFECYCLE = ' (this run has no lifecycle, so only channels are tried)'


def knobs():
    """(knobs, problem): the switch of fleet.yaml#daemon_watch over DEFAULTS; a bad value keeps the default."""
    try:
        block = fleet_config.block('daemon_watch') or {}
    except (OSError, ValueError, KeyError) as exc:
        return dict(DEFAULTS), 'fleet.yaml#daemon_watch unreadable: ' + str(exc)[:200]
    out, bad = dict(DEFAULTS), []
    for key in DEFAULTS:
        if key in block:
            if type(block[key]) is bool:
                out[key] = block[key]
            else:
                bad.append(key)
    return out, ('fleet.yaml#daemon_watch: ' + ', '.join(bad) + ' must be true or false') if bad else None


def on():
    return knobs()[0]['no_route_alarm']


def path(run):
    return Path(run) / FILE


def _policy(cfg, policy):
    if policy is not None:
        return policy
    try:
        from fleetmux import lifecycle
        return lifecycle.get_mode(cfg['lifecycle']) or {}
    except (ValueError, KeyError, TypeError, OSError):
        return {}


def route_problem(cfg, policy=None):
    """None when a root fleet has at least one route to a human (or is not a root, or the switch is off), else the
    text that says so. ``policy``: the lifecycle mode (read from ``cfg['lifecycle']`` when None)."""
    from fleetmux import lifecycle_close
    if cfg.get('parent_config') or lifecycle_close.NOTIFIER or not on():
        return None
    from fleetmux.channels import DEFAULT_PUSH
    # Without a lifecycle, a notice reaches channels only through RuntimeV2.channel_alarm (event daemon_down).
    events = ('run_closed', 'daemon_down') if cfg.get('lifecycle') else ('daemon_down',)
    for e in cfg.get('channels') or []:
        if isinstance(e, dict):
            push = e.get('push') if e.get('push') is not None else DEFAULT_PUSH
            if any(x in push for x in events):
                return None
    if cfg.get('lifecycle'):  # managed: lifecycle_close.notify also tries the tg command and the parent leg
        policy = _policy(cfg, policy)
        if os.environ.get(lifecycle_close.TG_ENV) or (policy.get('notify_commands') or {}).get('tg'):
            return None
        if cfg.get('initiator') and policy.get('notify_parent_session', True):
            return None
    return PROBLEM + ('' if cfg.get('lifecycle') else NO_LIFECYCLE)


def read(run):
    try:
        data = json.loads(path(run).read_text())
    except (OSError, ValueError):
        return {'count': 0, 'last': None}
    return data if isinstance(data, dict) else {'count': 0, 'last': None}


def record_undelivered(cfg, event, text):
    """Count one notice for a human that left on no leg (root, switch on). Keeps the full text of the latest."""
    if cfg.get('parent_config') or not on():
        return
    run = cfg['run']
    data = read(run)
    data = {'count': int(data.get('count') or 0) + 1, 'last': {'event': event, 'text': text, 'at': time.time()}}
    tmp = path(run).with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path(run))


def count_unrouted(cfg, event, text):
    """A notice that has no channel to try at all (a root without a lifecycle): counted when no route exists."""
    if route_problem(cfg):
        record_undelivered(cfg, event, text)


def unmanaged_send(f, event, text):
    """A notice for a human at a root without a lifecycle (ruling 28): send it through the run's channels with
    RuntimeV2.channel_alarm (event daemon_down), record it in notifications.jsonl, count it when no channel took
    it ('no_channel'). Transport v1 has no channel_alarm: counted when no route exists. Switch off: nothing."""
    if f.cfg.get('parent_config') or not on():
        return None
    if getattr(f, 'v2', None) is None:
        count_unrouted(f.cfg, event, text)
        return None
    from fleetmux import lifecycle_close
    status = f.v2.channel_alarm('[fleet ' + lifecycle_close.fleet_name(f) + '] ' + text)
    with open(Path(f.cfg['run']) / 'notifications.jsonl', 'a', encoding='utf-8') as fh:
        fh.write(json.dumps({'event': event, 'channel': 'channels', 'text': text, 'status': status,
                             'run': f.cfg.get('run'), 'at': time.time()}, ensure_ascii=False) + '\n')
    if status == 'no_channel':
        record_undelivered(f.cfg, event, text)
    return status


def summary(cfg):
    """status: {route, problem, undelivered, last}, or None when a route exists and nothing was lost."""
    if cfg.get('parent_config') or not on():
        return None
    problem = route_problem(cfg)
    data = read(cfg['run'])
    if not problem and not data.get('count'):
        return None
    return {'route': 'none' if problem else 'ok', 'problem': problem, 'undelivered': int(data.get('count') or 0),
            'last': data.get('last')}


def status_line(info):
    """One stderr line for status (None: nothing to say)."""
    if not info:
        return None
    last = info.get('last') or {}
    return ('human notices: ' + ('NOWHERE TO GO; ' if info['route'] == 'none' else '') + 'undelivered '
            + str(info['undelivered']) + (', latest ' + str(last.get('event')) + ' at '
                                         + time.strftime('%Y-%m-%d %H:%M', time.localtime(float(last.get('at') or 0)))
                                         if last else '')
            + ('. ' + info['problem'] if info.get('problem') else ''))


def start_warning(cfg):
    """start / start --resume: one stderr line when a root has no route to a human."""
    problem = route_problem(cfg)
    if problem:
        print('warning: ' + problem, file=sys.stderr)
    return problem


def startup_alarm(f):
    """The daemon start: audit notify.no_route once when a root has no route to a human."""
    problem = route_problem(f.cfg)
    if problem:
        f.log('notify.no_route', level='warn', problem=problem)
    return problem
