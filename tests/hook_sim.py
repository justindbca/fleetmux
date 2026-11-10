"""In-process stand-in for a Claude worker's *waiting* Stop hook (the hook-channel tests' helper).

The hook channel stays on in every test. Only what cannot run inside a pytest process is replaced:

* ``fleet.claude_hook_next`` (the hook's blocking BLPOP wait) becomes one non-blocking pass through
  the real ``fleet.hook_session`` (same liveness check, same re-validation against the lease, same
  bind write). With nothing queued, the session counts as *armed*: a hook is waiting.
* ``claude_hook.post`` still writes the real mailbox entry; when that slot/generation is armed the
  waiting hook takes it at once, exactly as the real hook's BLPOP would, and the hand-over is
  recorded for ``texts()``.

``claude_stop()`` runs the real ``fleet.claude_stop`` (settle first, then the wait above) and
returns what the turn's settlement answered, which is what legacy tests assert on. Claude Code then
writes the delivery as an isMeta "Stop hook feedback" user row (measured on 2.1.280); ``user_row()``
writes that row for a hook-delivered prompt and a plain row for anything typed or launched.

tests/test_claude_hook_delivery.py runs the real hook command in subprocesses instead (``HOOK_CHANNEL``).
"""
import json
import os

from cmux_fleet import claude_hook, fleet

FEEDBACK = 'Stop hook feedback:\n'
_armed = {}      # mailbox key -> (cfg, slot, generation, thread)
_handed = []     # (mailbox key, slot, generation, text)
_settled = []    # tui_turn answers of the current claude_stop call


def reset():
    _armed.clear()
    del _handed[:]
    del _settled[:]


def _take(r, cfg, slot, generation, thread, k):
    """The waiting hook takes the head entry (or nothing). Returns its text or None."""
    _, alive, accept = fleet.hook_session(cfg, r, slot, generation, thread, os.getppid())
    if not alive():
        _armed.pop(k['mailbox'], None)
        return None
    while True:
        raw = r.lpop(k['mailbox'])
        if not raw:
            return None
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        text = accept(entry) if isinstance(entry, dict) else None
        if text:
            _armed.pop(k['mailbox'], None)
            _handed.append((k['mailbox'], slot, generation, text))
            return text


def next_delivery(cfg, name, generation, thread, deadline, transcript='', own=None):
    """Replacement for fleet.claude_hook_next: hand over what is queued, else stay armed (``cfg`` / ``name`` /
    ``generation`` are already the routed target; the real wait's re-routing from ``own`` is not simulated).

    Like the real wait, it first yields (takes nothing, stays unarmed) to an operator line queued in
    this session's transcript (claude_hook.OperatorQueue / yielding)."""
    r = fleet.connect(cfg)
    try:
        k = fleet.hook_keys(cfg, r, name, generation)
        queue = claude_hook.OperatorQueue(transcript) if transcript else None
        if queue is not None and claude_hook.yielding(r, k, thread, queue):
            _armed.pop(k['mailbox'], None)
            return None
        text = _take(r, cfg, name, generation, thread, k)
        if text:
            return claude_hook.block(text)
        _, alive, _ = fleet.hook_session(cfg, r, name, generation, thread, os.getppid())
        if alive():
            _armed[k['mailbox']] = (cfg, name, generation, thread)
        return None
    finally:
        close = getattr(r, 'close', None)
        if close:
            close()


def post(real_post):
    def posted(r, k, entry):
        real_post(r, k, entry)
        waiting = _armed.get(k['mailbox'])
        if not waiting:
            return False
        cfg, slot, generation, thread = waiting
        _take(r, cfg, slot, generation, thread, k)
        return True
    return posted


def install(monkeypatch):
    reset()
    monkeypatch.setattr(fleet, 'claude_hook_next', next_delivery)
    monkeypatch.setattr(claude_hook, 'post', post(claude_hook.post))
    real_turn = fleet.tui_turn

    def tui_turn(*args, **kwargs):
        answer = real_turn(*args, **kwargs)
        _settled.append(answer)
        return answer
    monkeypatch.setattr(fleet, 'tui_turn', tui_turn)


def claude_stop(cfg_path, slot, generation, raw):
    """The real Stop hook entry; returns the settlement answer (v2), or True once the v1 turn event
    is pushed, as the pre-hook-channel entry did. ``False``: background tasks, nothing done."""
    del _settled[:]
    out = fleet.claude_stop(cfg_path, slot, generation, raw)
    if isinstance(out, bool):
        return out
    return _settled[-1] if _settled else True


def texts(slot, prefix=None, generation=None):
    """Delivery texts the slot's hook handed to Claude, oldest first."""
    return [t for key, s, g, t in _handed if s == slot and (prefix is None or key.startswith(prefix + ':'))
            and (generation is None or g == generation)]


def handed_to(f, slot):
    """Hook hand-overs to ``slot``'s current generation in fleet ``f`` (v2 bus prefix, or v1 'fx')."""
    prefix = f.v2.bus.prefix if getattr(f, 'v2', None) else 'fx'
    return texts(slot, prefix, int(f.agent(slot).get('generation') or 0))


def user_row(prompt, hooked):
    """The transcript row that started a turn: the measured hook feedback row, or a plain prompt."""
    if hooked:
        return {'type': 'user', 'isMeta': True, 'message': {'role': 'user', 'content': FEEDBACK + prompt + '\n'}}
    return {'type': 'user', 'message': {'role': 'user', 'content': prompt}}


def was_handed(slot, prompt):
    return any(t == prompt for _, s, _, t in _handed if s == slot)
