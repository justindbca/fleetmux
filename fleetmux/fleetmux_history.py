"""The sidebar's restore history: the runs that can still be resumed (plan §18.3 / §18.6 / §18.7).

One scan of ``<FLEETMUX_HOME>/runs/*/result.json`` answers the whole question, once per ``gui`` start and
once per closer run — never a resident process (plan §18.2):

* ``restorable_runs(home, state_dir)`` — the runs worth a grey card: ``result.json`` says
  ``resumable: true`` and the run was closed either from the sidebar (``closed_via == 'fleetmux-sidebar'``)
  or by a lifecycle timeout (``closed_by == 'timeout'``, §18.6); its ``config.json`` is readable, its backend
  is fleetmux and its pinned session / cmux state dir are this window's (otherwise its ``--resume`` would
  connect to another runtime, §18.3); its daemon is gone. Newest ``closed_at`` first, at most
  ``MAX_CARDS``. Each item is the kv ``restorable`` entry plus the card's ``line1``.
* ``cards(runs)`` — the grey ``closed`` cards those runs become (kv ``sidebar``'s ``fleets``).
* ``restorable_kv(runs)`` — kv ``restorable``: ``{"v":1,"runs":[{id,title,closed_at,argv,cwd,env},…]}``.
* ``push(mgr, runs, current=None)`` — write both kv keys, but only the ones that changed (every kv.set
  redraws the server, §18.4); an empty history writes nothing (0 cards is not a legal kv ``sidebar``,
  §18.7-2), the caller decides what that means.

The item itself (the ``start --resume`` argv / cwd / env) comes from ``lifecycle_close.resume_entry``, so the
closer's kv value and the history list can never drift apart.
"""
import json
import os
import time
from pathlib import Path

MAX_CARDS = 16  # = sidebar.rs MAX_FLEETS: kv `sidebar` may not list more cards (§18.3)
SIDEBAR_VIA = 'fleetmux-sidebar'  # result.json closed_via of a sidebar × close (§12.1)
CLOSED_LINE1 = '已关闭 · 可恢复'  # a card the sidebar × closed (§13.3)
TIMEOUT_LINE1 = '到点停 · 可恢复'  # a card the lifecycle timeout stopped, still resumable (§18.6)
CLOSED_AT_FMT = '关闭于 %m-%d %H:%M'  # the card's second line (§18.2)
KV_RESTORABLE, KV_SIDEBAR = 'restorable', 'sidebar'
RESUME_KEYS = ('id', 'title', 'closed_at', 'argv', 'cwd', 'env')  # the kv item's exact fields (§18.3)


def _text(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _line1(res):
    """The card's first line for a resumable result.json, or None when it is not a history entry (§18.6)."""
    if res.get('closed_via') == SIDEBAR_VIA:
        return CLOSED_LINE1
    if res.get('closed_by') == 'timeout':
        return TIMEOUT_LINE1
    return None


def _same_runtime(cfg, state_dir):
    """This run's ``--resume`` must connect to *this* window's fleetmux runtime (§18.1 / §18.3)."""
    from fleetmux import fleet
    if (cfg.get('session') or fleet.SESSION) != fleet.SESSION:
        return False
    have, want = str(cfg.get('cmux_state_dir') or ''), str(state_dir or '')
    # by real path: a run pinned to the legacy ~/.local/state/cmux-fleet is the same runtime once that path is
    # the symlink `home migrate` left to ~/.fleetmux/state (§50.1)
    return have == want or bool(have and want and os.path.realpath(have) == os.path.realpath(want))


def restorable_runs(home, state_dir):
    """The runs a grey card can bring back, newest first (§18.3 + §18.6)."""
    from fleetmux import fleet, lifecycle_close
    runs = []
    for path in sorted(Path(home).glob('runs/*/result.json')):
        res = _read(path)
        if not isinstance(res, dict) or res.get('resumable') is not True:
            continue
        line1 = _line1(res)
        if line1 is None:
            continue
        run = path.parent
        cfg = _read(run / 'config.json')
        if not isinstance(cfg, dict) or fleet.run_backend(cfg) != 'fleetmux' or not _same_runtime(cfg, state_dir):
            continue
        if fleet.daemon_running(run):
            continue  # still running: its daemon owns the sidebar cards
        item = lifecycle_close.resume_entry(cfg, run, res)
        item['line1'] = line1
        runs.append(item)
    runs.sort(key=lambda r: (-r['closed_at'], r['id']))
    return runs[:MAX_CARDS]


def cards(runs):
    """The grey ``closed`` cards of the history, in display order (§18.2)."""
    return [{'id': r['id'], 'title': r['title'], 'state': 'closed',
             'line1': r.get('line1') or CLOSED_LINE1,
             'line2': time.strftime(CLOSED_AT_FMT, time.localtime(r['closed_at']))}
            for r in runs]


def restorable_kv(runs):
    """kv ``restorable``: one resume command per history run, newest first (§18.3)."""
    return {'v': 1, 'runs': [{key: r[key] for key in RESUME_KEYS} for r in runs]}


def push(mgr, runs, current=None):
    """Write kv ``restorable`` and kv ``sidebar`` for the history; skip the writes that change nothing.

    Returns True when something was written. ``current`` (the highlighted card) defaults to the newest run,
    the one the topbar's ↺ and ``^b r`` bring back (§18.2 / §18.7-2).
    """
    if not runs:
        return False  # §18.7-2: an empty card list is not a legal kv sidebar
    get, put = getattr(mgr, 'kv_get', None), getattr(mgr, '_kv_set', None)
    if get is None or put is None:
        return False
    wanted = ((KV_RESTORABLE, restorable_kv(runs)),
              (KV_SIDEBAR, {'v': 1, 'current': current or runs[0]['id'], 'fleets': cards(runs)}))
    wrote = False
    for key, value in wanted:
        text = _text(value)
        if get(key) != text:
            put(key, text)
            wrote = True
    return wrote
