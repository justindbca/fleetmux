"""`fleetmux cancel <job id>`: stop one job of the running root fleet, never the root (docs/omnigent.md).

A job (docs/design.md section 58) runs in its own task-mode sub-fleet. Cancelling it stops that sub-fleet with
``stop --config <sub-fleet config>``, after checking that the sub-fleet's ``parent_config`` is this root: the
same path a join stub's ``stop`` takes (join.stop_job), addressed by job id. The root collects the stopped
sub-fleet as usual and publishes the job ``failed`` (outcome ``cancelled``).

Nothing is ever sent to the root itself: no ``stop`` and no new daemon op, so this works on any running root,
including one started from an older release. A job that has no sub-fleet yet (queued / starting) cannot be
stopped this way: it is reported as pending; run the command again once ``status`` shows its sub-fleet, or pass
``--wait`` to retry every RETRY_S seconds.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

from fleetmux import join

JOB_ID = re.compile(r'job-[0-9a-f]{10}\Z')
RETRY_S = 15


def _fleet():
    from fleetmux import fleet
    return fleet


def cancel(root_config, jid):
    """Stop job ``jid`` of the root fleet ``root_config``. Returns a dict: ``state`` (``stopping`` / ``ended`` /
    the job's state), ``stopped`` (a stop reached its sub-fleet), ``pending`` (no sub-fleet yet), ``reason``.
    JoinError when the id is not a job id, the root is not running, or the job is unknown."""
    if not isinstance(jid, str) or not JOB_ID.fullmatch(jid):
        raise join.JoinError('not a job id: ' + repr(jid) + ' (expected job-<10 hex digits>)')
    stub = {'root_config': str(root_config)}
    join._root(stub)
    row, item = join.job_view(stub, jid)
    if row is None:  # no longer open on the root: it ended, or the root never knew it
        code, out = join.root_result(stub, jid)
        if code == 0:
            return {'job': jid, 'state': 'ended', 'stopped': False, 'status': out.get('status'),
                    'reason': 'the job already ended; nothing was stopped'}
        raise join.JoinError('the root has no open job ' + jid + ' and no result for it')
    state = row.get('state') or 'queued'
    if not item or not item.get('run'):
        return {'job': jid, 'state': state, 'stopped': False, 'pending': True,
                'reason': 'job ' + jid + ' is ' + state + ' and has no sub-fleet yet, so it was not stopped; run '
                          '`fleetmux cancel ' + jid + '` again once `fleetmux status` shows its sub-fleet (or add '
                          '--wait SECONDS)'}
    child_config = Path(item['run']) / 'config.json'
    try:
        parent = _fleet().load(child_config).get('parent_config')
    except (OSError, ValueError) as exc:
        raise join.JoinError('the sub-fleet config ' + str(child_config) + ' is unreadable: ' + str(exc)) from exc
    if not parent or join._resolved(parent) != join._resolved(root_config):
        raise join.JoinError(str(child_config) + ' is not a sub-fleet of ' + str(root_config) + '; not stopped')
    code, out, err = join.fleet_cli(['stop', '--config', str(child_config)])
    answer = join.parse_json(out)
    stopped = code == 0
    return {'job': jid, 'state': 'stopping' if stopped else state, 'stopped': stopped,
            'subfleet': row.get('subfleet') or '', 'child_config': str(child_config),
            'reason': ('its sub-fleet is stopping; the result follows as `fleetmux result ' + jid + '`') if stopped
            else 'the stop of its sub-fleet failed: ' + (err.strip() or out.strip() or 'exit code ' + str(code)),
            **({'answer': answer} if answer else {})}


def root_config_of(config):
    """The root config to act on: ``config`` (a root config.json or a join stub), else the running root."""
    if config:
        data = _fleet().load(config)
        if join.is_stub(data):
            return data[join.KEY]['root_config']
        return str(config)
    root = join.find_root(_fleet().LATEST.parent)
    if root is None:
        raise join.JoinError('no running root fleet on this machine (start one with `fleetmux start`)')
    return root['config']


def main(argv):
    p = argparse.ArgumentParser(prog='fleetmux cancel', description='Stop one job of the running root fleet (its '
                                'sub-fleet only); the root fleet and its other jobs keep running.')
    p.add_argument('job', help='the job id (job-...) that `fleetmux task` printed')
    p.add_argument('--config', help='the root fleet config.json or a join stub (default: the running root fleet)')
    p.add_argument('--wait', type=float, default=0, help='while the job has no sub-fleet yet, retry every '
                   + str(RETRY_S) + ' s for up to this many seconds (default 0: answer at once)')
    args = p.parse_args(argv)
    try:
        root = root_config_of(args.config)
        deadline = time.monotonic() + max(args.wait, 0)
        while True:
            out = cancel(root, args.job)
            if not out.get('pending') or time.monotonic() + RETRY_S > deadline:
                break
            time.sleep(RETRY_S)
    except (join.JoinError, OSError, ValueError) as exc:
        sys.stderr.write('fleetmux cancel: ' + str(exc) + '\n')
        return 2
    print(json.dumps(out, ensure_ascii=False))
    if out.get('pending'):
        return 3
    return 0 if out.get('stopped') or out.get('state') == 'ended' else 2
