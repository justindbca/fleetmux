"""Recheck of a dev_task delivery gate: ``fleetmux task --recheck <task id | job id>``.

A dev_task task that the delivery gate (lifecycle_deliver) judged blocked can have the gate run again after the
files are fixed (a test fixed, a branch rebased), without a new task or job and without dispatching any work.

* Targets: the run's own dev_task task while its daemon holds it, or a job (``job-...``) of this fleet whose
  sub-fleet ended blocked by its gate. Anything else is refused: a running task or job, a task blocked by
  anything other than a failed gate (supervisor BLOCKED, a hard timeout), cancelled, timed out.
* Split in two, so the daemon never runs the test command inside an event:
  1. ``compute`` (the CLI process) reruns the whole gate from files: the deliver spec comes from the task (own
     task) or from the gate record in the sub-fleet's result.json (job: its Redis is gone). A record written
     before the review removal may still carry ``slots`` / ``review`` / ``spec.review_file``: ignored.
  2. ``apply`` (the daemon, event ``recheck``) checks the target again, re-reads done_when from git + the queue
     for a passing record, stores the new gate record and, on pass, completes: the own task turns completed (the
     next tick closes the run like any completed dev_task); a job's result is published again as ok.
"""
import json
from pathlib import Path

from fleetmux import lifecycle_close, lifecycle_deliver, subfleet

RECHECKED = 'delivery gate passed on recheck'


def _fleet():
    from fleetmux import fleet
    return fleet


def gate_failed(raw):
    """The gate record when it exists and failed, else None."""
    try:
        rec = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return rec if isinstance(rec, dict) and rec.get('ok') is False else None


def own_problem(cfg, task, gate_raw):
    """Why the run's own task cannot be rechecked, else None."""
    if cfg.get('lifecycle') != 'dev_task':
        return 'only a dev_task run has a delivery gate (this run: ' + str(cfg.get('lifecycle')) + ')'
    status = task.get('status') or 'unknown'
    if status != 'blocked':
        return ('task ' + task['id'] + ' is ' + status + ': only a task the delivery gate blocked can be rechecked'
                + (' (the gate runs when it completes)' if status == 'running' else ''))
    if gate_failed(gate_raw) is None:
        return ('task ' + task['id'] + ' is blocked by ' + repr(task.get('reason') or 'its supervisor')
                + ', not by a failed delivery gate: nothing to recheck')
    return None


def job_run(run_dir, jid):
    """The sub-fleet run directory of job ``jid`` (its record names the job), or None."""
    sub = Path(run_dir) / 'sub'
    if not sub.is_dir():
        return None
    for d in sorted(sub.iterdir()):
        if d.is_dir() and subfleet.record(d).get('job') == jid:
            return d
    return None


def job_problem(run_dir, jid):
    """(child run, its result.json, problem): why job ``jid`` cannot be rechecked, else problem None."""
    d = job_run(run_dir, jid)
    if d is None:
        return None, None, 'no finished sub-fleet of ' + jid + ' in this fleet (still queued, or not its job)'
    rec = subfleet.record(d)
    if _fleet().daemon_running(d) or rec.get('state') in ('starting', 'running', 'stopping'):
        return d, None, 'job ' + jid + ' is running (sub-fleet ' + rec.get('task_id', d.name) + '): nothing to recheck'
    res = subfleet.child_result(d) or {}
    gate = gate_failed(res.get('delivery'))
    if res.get('status') != 'blocked' or gate is None:
        return d, res, ('job ' + jid + ' ended ' + str(res.get('status') or 'without a result') + ', not blocked by a '
                        'failed delivery gate: nothing to recheck')
    if not isinstance(gate.get('spec'), dict):
        return d, res, ('the gate record of ' + jid + ' does not keep its deliver spec (it ran on an older release); '
                        'submit the job again')
    return d, res, None


def compute(cfg, ident, r):
    """The CLI half: rerun the gate of ``ident`` from files. Returns the ``recheck`` event; ValueError when refused."""
    fleet = _fleet()
    task = r.hgetall(fleet.TASK)
    if task.get('id') and task['id'] == ident:
        problem = own_problem(cfg, task, r.hget(lifecycle_close.LIFE, 'gate:' + ident))
        if problem:
            raise ValueError(problem)
        spec = lifecycle_close.task_spec(task).get('deliver')
        rec = lifecycle_deliver.deliver(spec, Path(cfg['run']) / 'workspace', task_id=ident, run=cfg['run'])
        return {'kind': 'recheck', 'id': ident, 'delivery': rec}
    d, res, problem = job_problem(cfg['run'], ident)
    if problem:
        raise ValueError(problem)
    rec = lifecycle_deliver.deliver(res['delivery']['spec'], d / 'workspace', task_id=res.get('task_id', ''),
                                    run=str(d))
    return {'kind': 'recheck', 'id': ident, 'delivery': rec}


def confirmed(rec, spec):
    """A passing record stays passing only if its sha is the feature branch tip and done_when holds now (git +
    the queue; via rollout, the record's request id for that sha); else a failed copy. The test run is the CLI's:
    the daemon does not repeat them."""
    if not rec.get('ok'):
        return rec
    sha = str(rec.get('sha') or '')
    try:
        tip = lifecycle_deliver._sha(spec['repo'], spec['feature_branch'])
        problem = (None if sha and sha == tip else 'gate record sha ' + (sha[:12] or 'missing') + ' is not the tip of '
                   + spec['feature_branch']) or lifecycle_deliver.done_when_problem(spec, sha, rec)
    except (KeyError, RuntimeError, OSError) as exc:
        problem = 'done_when could not be read: ' + str(exc)
    return dict(rec, ok=False, problem=problem) if problem else rec


def apply(f, ev):
    """The daemon half (event ``recheck``): store the recomputed gate and complete on pass. Returns the reply;
    ValueError when the target cannot be rechecked (checked again here, the CLI's view may be old)."""
    ident, rec = ev.get('id'), ev.get('delivery')
    if not isinstance(ident, str) or not ident or not isinstance(rec, dict) or not isinstance(rec.get('ok'), bool):
        raise ValueError('recheck needs a task or job id and the recomputed gate record')
    task = {} if f.service else f.task()
    if task.get('id') == ident:
        return apply_own(f, task, rec)
    return apply_job(f, ident, rec)


def apply_own(f, task, rec):
    from fleetmux.fleet import TASK
    tid = task['id']
    problem = own_problem(f.cfg, task, lifecycle_close._hget(f, 'gate:' + tid))
    if problem:
        raise ValueError(problem)
    rec = confirmed(rec, lifecycle_close.task_spec(task).get('deliver') or {})
    lifecycle_close._hset(f, **{'gate:' + tid: json.dumps(rec, ensure_ascii=False)})
    status, reason = (('completed', RECHECKED) if rec['ok'] else
                      ('blocked', 'dev_task done_when not met: ' + rec.get('problem', '')))
    f.r.hset(TASK, mapping={'status': status, 'reason': reason})
    # result.json is written once per task: drop the blocked one; the next tick writes the new outcome (and a
    # completed task is then closed like any other)
    (f.run_dir / 'result.json').unlink(missing_ok=True)
    f.log('lifecycle.recheck', task=tid, ok=rec['ok'], problem=rec.get('problem', ''))
    return {'status': 'passed' if rec['ok'] else 'blocked', 'task_id': tid, 'problem': rec.get('problem', ''),
            'delivery': rec}


def apply_job(f, jid, rec):
    fleet = _fleet()
    if jid in f.job_state():
        raise ValueError('job ' + jid + ' is still open (' + str(f.job_state()[jid].get('state')) + '): nothing to '
                         'recheck')
    d, res, problem = job_problem(f.run_dir, jid)
    if problem:
        raise ValueError(problem)
    published = f.r.get(f.v2.prefix + ':public:' + fleet.runtime_v2.digest(jid)) if f.v2 else None
    if published and json.loads(published).get('status') != 'blocked':
        raise ValueError('job ' + jid + ' was published ' + str(json.loads(published).get('status'))
                         + ', not blocked: nothing to recheck')
    rec = confirmed(rec, res['delivery']['spec'])
    res['delivery'] = rec
    if rec['ok']:
        res.update(status='completed', reason=RECHECKED)
    else:
        res['reason'] = 'dev_task done_when not met: ' + rec.get('problem', '')
    fleet.save(d / 'result.json', res)
    f.log('lifecycle.recheck', job=jid, ok=rec['ok'], problem=rec.get('problem', ''))
    if not rec['ok']:
        return {'status': 'blocked', 'job_id': jid, 'problem': rec.get('problem', ''), 'delivery': rec}
    child = subfleet.record(d)
    child.update(outcome='completed', reason=RECHECKED, state='returned')
    subfleet.save_record(d, child)
    f.job_finished(jid, child.get('task_id', d.name), child.get('slot', ''), d, 'completed', RECHECKED, replace=True)
    return {'status': 'passed', 'job_id': jid, 'published': 'ok', 'delivery': rec}


def cli(cfg, ident, wait=30):
    """``fleetmux task --recheck ID``: (exit code, answer). 0 passed, 2 refused or still blocked."""
    fleet = _fleet()
    r = fleet.connect(cfg)
    try:
        event = compute(cfg, ident, r)
    except ValueError as exc:
        return 2, {'status': 'rejected', 'reason': str(exc)}
    finally:
        r.close(); r.connection_pool.disconnect()
    answer = fleet.request(cfg, event, wait=wait)
    return (0 if answer.get('status') == 'passed' else 2), answer
