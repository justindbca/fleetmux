"""Opt-in Claude service context clearing; no transport or dispatcher of its own."""
import math
import os
import re


def enabled(cfg, slot):
    return (cfg.get('clear_after_result') is True and slot != 'supervisor'
            and cfg.get('transport_version') == 2 and cfg.get('mode') == 'service'
            and cfg.get('worker_cli') == 'claude')


def validate(cfg):
    flag = cfg.get('clear_after_result', False)
    if type(flag) is not bool:
        raise ValueError('clear_after_result must be a boolean')
    timeout = cfg.get('clear_timeout', 20)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('clear_timeout must be a positive finite number')
    # A named service sub-fleet (docs/design.md §29) runs with lifecycle None by design: its parent owns it.
    service_life = cfg.get('lifecycle') == 'service' or (
        cfg.get('lifecycle') is None and bool(cfg.get('named')) and bool(cfg.get('parent_config')))
    if flag and (not service_life or cfg.get('mode') != 'service'
                 or cfg.get('worker_cli') != 'claude' or cfg.get('transport_version') != 2):
        raise ValueError('--clear-after-result requires --lifecycle service, Claude Code workers '
                         'and the typed Redis v2 delivery path')
    if flag and cfg.get('claude_hook_delivery', True) is not False:
        # The Stop-hook channel (docs/design.md §5, default on) blocks inside every Stop hook for the
        # next item, so the worker never goes idle and /clear could never be pasted (§28).
        raise ValueError('--clear-after-result needs typed delivery, but Claude Stop-hook delivery is on '
                         '(the default): also pass --no-claude-hook-delivery (run config claude_hook_delivery: false)')


def hook_exited(pid):
    """Only ESRCH proves exit. A reused PID delays clearing rather than authorizing it."""
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (ValueError, TypeError, PermissionError):
        return False
    return False


def idle_prompt(screen):
    """Conservative Claude input guard, *after* a validated turn end and hook process exit.

    Require an empty, ruled input box plus its idle footer. Unknown versions, partial
    captures, text already in the box, menus and running/queued indicators fail closed.
    This screen check never settles a delivery or proves that execution has stopped.
    """
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', screen or '')
    rows = [row.strip() for row in text.rstrip().splitlines()[-12:]]
    tail = '\n'.join(rows).lower()
    if re.search(r'esc to (?:interrupt|cancel)|running.{0,30}hooks?|thinking[…\.]|\d+ background tasks?', tail):
        return False
    # Claude Code prints the session name (--name) inside the upper rule: "──── f1a2b3c4-worker-1 ─".
    rule = lambda row: bool(re.fullmatch(r'[─━\-]{8,}(?: \S+ [─━\-]+)?', row))
    for i in range(1, len(rows) - 2):
        if rows[i] != '❯' or not rule(rows[i-1]) or not rule(rows[i+1]):
            continue
        footer = '\n'.join(rows[i+2:]).lower()
        if any(s in footer for s in ('esc', 'interrupt', 'cancel', 'running', 'queued', 'background', 'confirm')):
            return False
        if '? for shortcuts' in footer or 'bypass permissions on' in footer:
            return True
    return False
