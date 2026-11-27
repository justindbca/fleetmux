"""Hook keepalive across a machine sleep (brief idle-reclaim-prereq 3.1 / 4.1).

Claude Code kills a Stop hook (and its asyncRewake fallback) at ``timeout`` seconds of wall time, sleep included.
On macOS ``time.monotonic()`` stops while the machine sleeps, so a keepalive deadline measured on it came after
the kill: an idle session lost its hook after a night with the lid closed. Here the hook process's
``time.monotonic`` runs ten times slower than real time (a stand-in for the hours it missed asleep) and the hook
must still hand Claude its keepalive before Claude Code's (wall-clock) timeout.

Real fleet: tests/hook_world.py (demo World, real daemon, scripted actors running the real hooks). The hook under
test runs in this process (``fleet.claude_stop`` / ``fleet.claude_rewake``, the entry points of the CLI)."""
import json
import threading
import time

from hook_world import hook_world  # noqa: F401  (fixture)
from fleetmux import claude_hook, fleet

HOOK_CHANNEL = True  # the real Stop / rewake hooks run (tests/conftest.py: no hook_sim stand-in)

TIMEOUT = 20  # claude_hook_timeout of the run: Claude Code kills the hook after this many wall seconds
SLOW = 0.1   # the hook's time.monotonic advances this fast (it "sleeps" 90 % of the time)
KILLED = 'killed by Claude Code at its hook timeout'


def lagging_monotonic(monkeypatch):
    real = time.monotonic
    zero = real()
    monkeypatch.setattr(time, 'monotonic', lambda: zero + (real() - zero) * SLOW)


def run_hook(fn, *args):
    """Run one hook entry point the way Claude Code does, with its wall-clock timeout. (result, wall seconds), or
    (KILLED, None) when Claude Code would have killed it first."""
    box = {}

    def go():
        started = time.time()
        try:
            box['result'] = fn(*args)
        except BaseException as exc:  # noqa: BLE001  (re-raised in the test thread)
            box['error'] = exc
        box['took'] = time.time() - started
    th = threading.Thread(target=go, daemon=True)  # a red run leaves it blocked: never keep pytest waiting
    th.start()
    th.join(TIMEOUT)
    if th.is_alive():
        return KILLED, None
    if 'error' in box:
        raise box['error']
    return box.get('result'), box.get('took')


def payload(world, slot):
    info = world.agent(slot)
    return json.dumps({'session_id': info['thread'], 'transcript_path': info['transcript'], 'hook_event_name': 'Stop',
                       'stop_hook_active': False, 'last_assistant_message': 'READY'})


def test_stop_hook_keepalive_beats_claude_code_timeout_when_the_hook_clock_lags(hook_world, monkeypatch):  # noqa: F811
    w = hook_world
    w.set_config(claude_hook_timeout=TIMEOUT)
    w.end_hooks('worker-2')  # the actor's own hook would hold the armed mark (one waiting hook per slot)
    info = w.agent('worker-2')
    lagging_monotonic(monkeypatch)
    got, took = run_hook(fleet.claude_stop, str(w.w.cfg_path), 'worker-2', info['generation'], payload(w, 'worker-2'))
    assert got != KILLED, ('no keepalive within Claude Code\'s ' + str(TIMEOUT) + ' s hook timeout: the hook '
                             'measured its deadline on a clock that stops while the machine sleeps')
    assert got == claude_hook.block(claude_hook.KEEPALIVE), got
    assert took < TIMEOUT - 2, took


def test_rewake_keepalive_beats_claude_code_timeout_when_the_hook_clock_lags(hook_world, monkeypatch):  # noqa: F811
    w = hook_world
    w.set_config(claude_hook_timeout=TIMEOUT)
    w.end_hooks('supervisor')  # only the asyncRewake fallback waits: the supervisor's keepalive comes from it
    info = w.agent('supervisor')
    lagging_monotonic(monkeypatch)
    got, took = run_hook(fleet.claude_rewake, str(w.w.cfg_path), 'supervisor', info['generation'],
                         payload(w, 'supervisor'))
    assert got != KILLED, ('no rewake keepalive within Claude Code\'s ' + str(TIMEOUT) + ' s hook timeout: the '
                             'asyncRewake hook measured its deadline on a clock that stops while the machine sleeps')
    assert got == claude_hook.KEEPALIVE, got
    assert took < TIMEOUT - 2, took


def test_hook_clock_counts_sleep_and_the_keepalive_point_is_half_the_timeout():
    # The clock behind the deadline is never behind time.monotonic (which stops in sleep on macOS).
    assert claude_hook.clock() >= time.monotonic() - 1
    cfg = {'claude_hook_timeout': 86400}
    assert claude_hook.deadline(cfg, 1000.0) == 1000.0 + 43200
    assert claude_hook.deadline({'claude_hook_timeout': 20}, 0.0) == 10
