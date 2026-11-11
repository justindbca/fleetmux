"""Lifecycle hold and service idling (lifecycle design §2 row 1, §3):

* ``blocked`` / ``budget_exhausted``: the run stays exactly as it is (tabs, sessions, Redis) for
  ``on_<status>.hold_hours`` (default 12) counted from result.json ``at``, then ``then:
  stop_keep_resumable``: every runtime is retired, Redis is shut down with its AOF directory kept, and
  result.json says ``resumable: true`` so ``start --resume`` reopens the task (docs/design.md §17).
  The values are re-read every tick, so an edit of hold_hours applies on the next tick.
* service: a worker idle for ``worker_idle_reclaim_min`` minutes is retired (the supervisor stays);
  work queued for it later starts its next generation. ``idle_stop_hours`` (null = never) stops the
  whole run after that long without requests or busy agents.
"""
import json
import math
import time
from pathlib import Path

# The durable reason a run that failed before its first READY leaves in result.json (A42-LINUX-RESTORE-FAILURE-04).
STARTUP_FAILURE = 'startup failed before READY'


def _empty_list(value):
    """True only for a genuinely empty artifact list (absent / ``[]``). ``{}``, a string or a number is not an
    empty list: an unknown shape must never be read as "no artifacts"."""
    return value is None or value == '' or (isinstance(value, list) and not value)


def _artifacts(task):
    """The task's declared artifacts: ``[]`` when it declares none, else a non-empty list. The hash stores them
    as a JSON string; any shape that is not a JSON list (``{}``, a string, a number, unreadable) counts as
    non-empty (never a placeholder)."""
    raw = task.get('artifacts')
    if raw is None or raw == '':
        return []
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str):
        return ['<unreadable>']
    try:
        value = json.loads(raw)
    except ValueError:
        return ['<unreadable>']
    return value if isinstance(value, list) else ['<unreadable>']


def deadline_value(task):
    """The task's deadline as a usable number, or None. A non-empty string that is not a positive finite number
    (``soon``, ``0``, ``-5``, ``nan``, ``inf``) is NOT a usable deadline: it never makes a record "a real task"."""
    raw = task.get('deadline')
    if raw is None or raw == '':
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def no_task(task, result):
    """True only for a run that provably never submitted a task: a pre-READY startup failure closed from the
    fleetmux sidebar (``resumable: true``, the startup-failure reason, and no task identity anywhere).

    Strict on purpose (A42-LINUX-RESTORE-FAILURE-04): an id-less or deadline-less record that is *not* this exact
    shape is a damaged real task. It must be refused by :func:`reopen`, never read as "nothing was ever submitted"
    and never reopened without ``--extend-hours``.
    """
    if not isinstance(result, dict) or result.get('resumable') is not True:
        return False
    if STARTUP_FAILURE not in str(result.get('reason') or ''):
        return False
    if result.get('task_id') or result.get('goal') or not _empty_list(result.get('artifacts')):
        return False
    if not isinstance(task, dict):
        return False
    if task.get('id') or task.get('goal') or task.get('deadline') or task.get('lifecycle_spec'):
        return False
    if task.get('status') not in (None, '', 'blocked', 'budget_exhausted'):
        return False
    return not _artifacts(task)


SIDEBAR_VIA = 'fleetmux-sidebar'  # result.json closed_via of a sidebar x close (§12.1 / §13.5)


def _number(value):
    """A usable finite number, or None (a non-numeric / non-finite / missing value is not a time)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def closed_idle(result):
    """A healthy task-mode run that sat idle and was closed from the fleetmux sidebar — the 199 native 11/13
    success path (evidence: ``97-healthy-lifecycle-close-complete.json``, kept as
    ``tests/fixtures/restore04-199-healthy-closed-idle.json``).

    Strict and complete on purpose: it never submitted a task (empty task id/goal/artifacts, no deadline) and its
    closed record is self-consistent (status ``stopped``, ``closed_by`` user, ``closed_via`` the sidebar,
    ``resumable``, numeric ``at``/``closed_at``, a non-empty reason). It is a legal ``'none'`` even though its
    reason is not a startup failure; any contradiction around it (a goal, artifacts, another close path, a
    non-stopped status, a non-task base mode) makes it a damaged record, never this shape.
    """
    if not isinstance(result, dict) or result.get('resumable') is not True:
        return False
    if str(result.get('closed_via') or '') != SIDEBAR_VIA or str(result.get('closed_by') or '') != 'user':
        return False
    if str(result.get('status') or '') != 'stopped' or str(result.get('base_mode') or '') != 'task':
        return False
    if str(result.get('task_id') or '') or str(result.get('goal') or ''):
        return False
    if not _empty_list(result.get('artifacts')) or str(result.get('deadline') or ''):
        return False
    if not str(result.get('reason') or '').strip():
        return False
    return _number(result.get('at')) is not None and _number(result.get('closed_at')) is not None


def never_submitted(task, result):
    """A run that provably never submitted a task: the strict pre-READY placeholder, or the healthy closed-idle
    sidebar shape of a run with no task record at all."""
    if no_task(task, result):
        return True
    return (not task) and closed_idle(result)


def _durable_identity(result):
    """The task identity a durable ``result.json`` claims (task id, goal, deadline or artifacts), or ''. Any of it
    makes an empty ``fx:task`` a contradiction: the run did submit something, or the record is damaged."""
    if str(result.get('task_id') or ''):
        return 'task ' + str(result.get('task_id'))
    if str(result.get('goal') or ''):
        return 'a goal'
    if not _empty_list(result.get('artifacts')):
        return 'artifacts'
    if str(result.get('deadline') or ''):
        return 'a deadline'
    return ''


def classify(task, result, present=True, readable=True):
    """What ``start --resume`` is resuming, decided **before** any retire, respawn, result deletion or replay.

    ``'none'``: the run never submitted a task — the strict pre-READY placeholder, or no task record at all with no
    durable result claiming one (``present=False``). ``'task'``: a real identity (non-empty id and a usable
    deadline). Anything else raises ``ValueError`` (fail closed): an empty ``fx:task`` contradicted by a durable
    ``result.json`` (``task_id`` / ``goal`` / ``artifacts`` / ``deadline``), a closed result that describes no task
    without being the strict placeholder, and a ``result.json`` that exists but cannot be read (``readable=False``,
    never treated as empty). No id or deadline is ever invented (A42-LINUX-RESTORE-FAILURE-04 P-04-CLASSIFY).
    """
    task = task if isinstance(task, dict) else {}
    result = result if isinstance(result, dict) else {}
    if not readable:
        raise ValueError('the task record is damaged (result.json exists but cannot be read)')
    if never_submitted(task, result):
        return 'none'
    if not task:
        if not present:
            return 'none'  # a healthy run that never had a task and never wrote a closed result
        claimed = _durable_identity(result)
        if claimed:
            raise ValueError('the task record is damaged (result.json names ' + claimed
                             + ' but no task record exists)')
        raise ValueError('the closed result describes no task but matches neither the strict pre-READY placeholder '
                         'nor a healthy sidebar close; refusing to guess')
    if not str(task.get('id') or ''):
        raise ValueError('the task record is damaged (no id)')
    if deadline_value(task) is None:
        raise ValueError('the task record is damaged (no usable deadline)')
    return 'task'


def clear_no_task(r, run_dir, task, result):
    """Remove a closed record of a run that never submitted a task so it waits for a task again
    (A42-LINUX-RESTORE-FAILURE-04): the strict pre-READY placeholder or the healthy closed-idle sidebar shape.

    Re-verifies the strict shape first: anything else raises ``ValueError`` and nothing is deleted. Then the
    ``fx:task`` hash and the closed ``result.json`` go through the same writers a normal start/stop uses, and the
    closed result is kept verbatim as ``result.closed.json`` (the failure-event evidence; read by nobody).
    Returns the evidence path, or None when there was no closed result to keep.
    """
    from fleetmux.fleet import TASK
    if not never_submitted(task, result):
        raise ValueError('refusing to clear a task record that is neither the strict pre-READY placeholder nor a '
                         'healthy sidebar close')
    path = Path(run_dir) / 'result.json'
    evidence = Path(run_dir) / 'result.closed.json'
    kept = None
    try:
        text = path.read_text()
    except OSError:
        text = None
    if text is not None:
        evidence.write_text(text)
        path.unlink()
        kept = evidence
    r.delete(TASK)
    return kept


def hold_tick(f, policy, task, now):
    from fleetmux import lifecycle_close as close
    from fleetmux.fleet import load
    status, tid = task.get('status'), task['id']
    rule = policy.get('on_' + status) or {}
    try:
        since = float(load(f.run_dir / 'result.json')['at'])
    except (OSError, ValueError, KeyError, TypeError):
        since = now
    hours = rule.get('hold_hours', 12)
    if not close._hget(f, 'held:' + tid):
        close._hset(f, **{'held:' + tid: now})
        f.log('lifecycle.held', status=status, hold_hours=hours)
        close.notify(f, 'held', '[fleet ' + close.fleet_name(f) + '] ' + policy['name'] + ' ' + tid + ' ' + status
                     + '：' + task.get('reason', '') + '。保留现场 ' + str(hours) + ' 小时等人处理，之后自动停机并保留可续跑。'
                     + ' run: ' + f.cfg['run'], policy)
    if hours is None or now - since < float(hours) * 3600:
        return
    then = rule.get('then', 'stop_keep_resumable')
    if then != 'stop_keep_resumable':
        f.log('lifecycle.table_error', error='on_' + status + '.then ' + repr(then) + ' is not supported; holding')
        return
    close.request_close(f, 'timeout', status + ' held ' + str(hours) + ' h without a human; stopped, resumable',
                        resumable=True)


def _lease_idle(f, slot):
    return f.v2.bus.r.hget(f.v2.bus.lease_key(slot), 'status') in (None, 'idle')


def service_tick(f, policy, now):
    from fleetmux import lifecycle_close as close
    from fleetmux.fleet import REQQ
    minutes = policy.get('worker_idle_reclaim_min')
    paused = f.paused()
    for slot in f.workers:
        info = f.agent(slot)
        pending = f.v2.bus.pending(slot)
        if info.get('state') == 'reclaimed':
            if pending and not paused:  # work arrived for a reclaimed worker: its next generation starts
                f.log('lifecycle.respawn', slot=slot, pending=len(pending))
                f.v2.respawn([slot], True)
            continue
        if (minutes is None or paused or info.get('state') != 'idle' or not info.get('pane') or pending
                or not _lease_idle(f, slot) or now - f.last_activity(info) < float(minutes) * 60):
            continue
        if f.v2.retire(slot, 'idle for ' + str(minutes) + ' min (worker_idle_reclaim_min)'):
            f.setf(slot, state='reclaimed', pane='', reclaimed_at=now)
            f.log('lifecycle.reclaimed', slot=slot, idle_min=minutes)
    hours = policy.get('idle_stop_hours')
    active = f.r.llen(REQQ) or any(f.agent(s).get('state') in ('busy', 'starting', 'handoff', 'clearing') for s in f.names)
    last = close._hget(f, 'service_active_at')
    if active or last is None:
        close._hset(f, service_active_at=now)
        return
    if hours is not None and now - float(last) >= float(hours) * 3600:
        close.request_close(f, 'timeout', 'service idle for ' + str(hours) + ' h (idle_stop_hours)')


def reopen(r, task, extend_hours=None, now=None, result=None):
    """start --resume of a ``resumable`` run: the held task runs again. A task stopped for its time budget
    needs ``--extend-hours`` (its deadline already passed: §17 would end it again on the first tick).

    Never writes a task the run did not have (A42-LINUX-RESTORE-FAILURE-04): a run that provably never submitted
    one takes the caller's no-task path, and a damaged/partial record is refused instead of being given a deadline.
    """
    from fleetmux.fleet import TASK
    now = time.time() if now is None else now
    if result is not None and no_task(task, result):
        raise ValueError('this run never submitted a task (' + STARTUP_FAILURE + '); nothing to reopen')
    tid = task.get('id') or ''
    deadline = deadline_value(task) or 0.0
    if not tid or deadline <= 0:
        raise ValueError('the task record is incomplete (' + ('no id' if not tid else 'no usable deadline')
                         + '); refusing to reopen it before a human inspects the run')
    fields = {'status': 'running', 'reason': ''}
    if extend_hours:
        fields['deadline'] = max(deadline, now) + float(extend_hours) * 3600
    elif deadline <= now:
        raise ValueError('task ' + tid + ' is past its time budget; pass --extend-hours H to resume it')
    r.hset(TASK, mapping=fields)
    from fleetmux.lifecycle_close import LIFE
    r.hdel(LIFE, *['held:' + tid, 'gate:' + tid, 'timeout:' + tid])
    return fields
