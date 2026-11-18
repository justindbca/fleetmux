"""Awake time: the one clock every fleetmux deadline and age is measured on (docs/design.md section 35).

Awake time is wall time minus the time the machine slept. A laptop with its lid closed does not run the fleet,
so the time it slept is not time anybody waited: a deadline counts only awake seconds.

Unit. Every time that fleetmux writes and a deadline or age check reads back (``startup_deadline``, applet lease
``expires_at`` / ``granted_at``, request ``created`` / ``clock_paused_at``, applet ``started_at`` ...) is awake
seconds: Unix seconds minus the fleet's ``slept_s``. ``slept_s`` starts at 0, so on a machine that never slept an
awake time equals the Unix time, and values written before this clock existed stay valid. Times written outside
fleetmux (the applet ``hb_at``, file mtimes) are Unix seconds and are read through ``from_wall``. Log and display
stamps (``at`` of audits and results) stay Unix seconds.

Source. ``slept_s`` is measured as wall delta minus monotonic delta, so it holds every sleep (also the short dark
wakes of a closed laptop) plus any step of the wall clock. The monotonic clock must stop while the machine sleeps:
  * macOS: ``time.monotonic()`` is ``mach_absolute_time()`` (``time.get_clock_info('monotonic')``), the same
    value as ``clock_gettime(CLOCK_UPTIME_RAW)``, which "does not increment while the system is asleep"
    (clock_gettime(3)). ``CLOCK_MONOTONIC`` there does count sleep; Python does not use it.
  * Linux: ``time.monotonic()`` is ``CLOCK_MONOTONIC``, which "does not count time that the system is
    suspended"; ``CLOCK_BOOTTIME`` is the one that does (clock_gettime(2)).
  Checked on a macOS 26 laptop that had slept about 9 days since boot: ``time.monotonic()`` equalled
  ``CLOCK_UPTIME_RAW`` and was that much behind wall-since-``kern.boottime``, which ``CLOCK_MONOTONIC`` matched.

Publication. The fleet daemon owns the clock (``tick``): it folds the measured sleep into ``slept_s`` every tick
and publishes it with the (wall, monotonic) point it is valid at, so any process on the same boot extrapolates
the sleep since then by itself (no lag between a wake and the daemon's next tick). Redis key, per fleet:

  fx:clock   hash  slept_s, wall, mono, boot, gaps (JSON list of [wall_before, wall_after, slept_before,
                   slept_after], the last GAPS_KEEP sleeps longer than GAP_MIN_S; used by ``from_wall``)

A process that cannot read it (the applet SDK while the host Redis is down) keeps measuring locally from its last
read, or from its own start, with the same wall-minus-monotonic rule.

Waits that never leave one process need no fleet value: they use ``mono()`` directly.
"""
import json
import sys
import time

KEY = 'fx:clock'
GAPS_KEEP = 64     # sleeps kept for from_wall; older ones are merged pairwise, never dropped
GAP_MIN_S = 1.0    # a fold that found more sleep than this logs a gap
FOLD_MIN_S = 0.001  # a smaller difference is not folded yet
BOOT_TOLERANCE_S = 5.0


def mono():
    """Seconds on a clock that stops while the machine sleeps (``time.monotonic``, see the module docstring). For
    waits inside one process; never stored."""
    return CLOCK.mono()


def boot_id():
    """Unix time of this boot, rounded: equal for every process of one boot (wall minus a clock that counts
    sleep since boot). 0 when the platform has no such clock."""
    which = {'darwin': 'CLOCK_MONOTONIC', 'linux': 'CLOCK_BOOTTIME'}.get(sys.platform)
    clock_id = getattr(time, which, None) if which else None
    if clock_id is None:
        return 0
    try:
        return round(time.time() - time.clock_gettime(clock_id))
    except OSError:
        return 0


class Clock:
    """Awake time of one fleet, as seen by this process. ``wall`` / ``mono`` are injectable (tests)."""

    def __init__(self, wall=None, mono_=None, boot=None):
        self.wall = wall or time.time
        self.mono = mono_ or time.monotonic
        self.boot = boot_id() if boot is None else boot
        self.slept = 0.0   # slept_s at self.ref
        self.ref = None    # (wall, mono) the value of self.slept is valid at; set on first use
        self.gaps = []

    def _ref(self):
        if self.ref is None:
            self.ref = (self.wall(), self.mono())
        return self.ref

    def slept_now(self):
        w0, m0 = self._ref()
        return self.slept + (self.wall() - w0) - (self.mono() - m0)

    def now(self):
        """The awake time now."""
        w0, m0 = self._ref()
        return w0 - self.slept + (self.mono() - m0)

    def since(self, t):
        """Awake seconds since the awake time ``t``."""
        return self.now() - float(t)

    def deadline(self, seconds):
        """The awake time ``seconds`` awake seconds from now."""
        return self.now() + float(seconds)

    def passed(self, deadline):
        return self.now() >= float(deadline)

    def fold(self):
        """Fold the sleep measured since ``ref`` into ``slept`` and log it as a gap when it is a real sleep."""
        w0, m0 = self._ref()
        w, m = self.wall(), self.mono()
        d = (w - w0) - (m - m0)
        if abs(d) < FOLD_MIN_S:
            return  # read noise of the two clocks (or a slow slew): kept until it adds up, so slept stays exact
        if d > GAP_MIN_S:
            self.gaps.append([w0, w, self.slept, self.slept + d])
            while len(self.gaps) > GAPS_KEEP:  # merge the two oldest: stamps inside them stay clamped (from_wall)
                a, b = self.gaps[0], self.gaps[1]
                self.gaps[:2] = [[a[0], b[1], a[2], b[3]]]
        self.slept, self.ref = self.slept + d, (w, m)

    def from_wall(self, t):
        """The awake time of the past Unix time ``t``. A ``t`` inside a fold window that held a sleep (when in the
        window the machine slept is not known) maps into the window's few awake seconds, never to before it:
        an age computed from it never counts the sleep."""
        self.fold()
        t = float(t)
        for w0, w1, s0, s1 in reversed(self.gaps):
            if t >= w1:
                return t - s1
            if t > w0:
                return min(max(t - s0, w0 - s0), w1 - s1)
        return t - (self.gaps[0][2] if self.gaps else self.slept)

    def since_wall(self, t):
        """Awake seconds since the past Unix time ``t``."""
        return self.now() - self.from_wall(t)

    # -------------------------------------------------------------- Redis
    def state(self):
        w, m = self._ref()
        return {'slept_s': repr(self.slept), 'wall': repr(w), 'mono': repr(m), 'boot': str(self.boot),
                'gaps': json.dumps(self.gaps)}

    def load(self, raw):
        """Take a published state. Same boot: its point too (the sleep since it is then measured here). Another
        boot or host: its total and gaps, measured from here on."""
        try:
            slept, w, m = float(raw['slept_s']), float(raw['wall']), float(raw['mono'])
            boot = int(raw.get('boot') or 0)
            gaps = json.loads(raw.get('gaps') or '[]')
        except (KeyError, TypeError, ValueError):
            return False
        same = boot and self.boot and abs(boot - self.boot) <= BOOT_TOLERANCE_S and self.mono() >= m
        if same:
            self.slept, self.ref = slept, (w, m)
        else:
            self.slept, self.ref = slept, (self.wall(), self.mono())
        self.gaps = [list(map(float, g)) for g in gaps if isinstance(g, list) and len(g) == 4]
        return True

    def sync(self, r):
        """Read the fleet's published clock. False (state kept, measured locally) when it is absent or Redis
        cannot be read."""
        try:
            raw = r.hgetall(KEY)
        except Exception:
            return False
        return bool(raw) and self.load(raw)

    def publish(self, r):
        r.hset(KEY, mapping=self.state())


# -------------------------------------------------------------- this process's clock
CLOCK = Clock()
_synced = []  # this process took the fleet's published clock once (join / tick)


def reset(wall=None, mono_=None, boot=None):
    """A fresh process clock (tests)."""
    global CLOCK
    CLOCK = Clock(wall, mono_, boot)
    _synced.clear()
    return CLOCK


def now():
    return CLOCK.now()


def since(t):
    return CLOCK.since(t)


def deadline(seconds):
    return CLOCK.deadline(seconds)


def passed(t):
    return CLOCK.passed(t)


def from_wall(t):
    return CLOCK.from_wall(t)


def since_wall(t):
    return CLOCK.since_wall(t)


def since_wake():
    """Awake seconds since the end of the last sleep this process measured (a gap of ``fold``); None before any."""
    CLOCK.fold()
    if not CLOCK.gaps:
        return None
    w1, s1 = CLOCK.gaps[-1][1], CLOCK.gaps[-1][3]
    return CLOCK.now() - (w1 - s1)


def between_wall(a, b):
    """Awake seconds from the Unix time ``a`` to the Unix time ``b`` (``b`` may be now or later)."""
    return CLOCK.from_wall(b) - CLOCK.from_wall(a)


def join(r):
    """Take the fleet's published clock into this process's clock, once per process (``Fleet`` does it when it
    connects): a process that stamps a deadline before the daemon's first tick (a CLI, a closer, a resumed
    daemon) then stamps it in the fleet's unit, not in Unix time."""
    if not _synced:
        CLOCK.sync(r)
        _synced.append(True)


def tick(r):
    """Daemon side, at the start of every tick: on the first call take the fleet's published clock (a restarted
    daemon goes on from it), then fold the sleep since the last tick and publish."""
    join(r)
    CLOCK.fold()
    CLOCK.publish(r)


def local():
    """A new clock on this process's wall and monotonic clocks, not synced: it measures from its first use."""
    return Clock(CLOCK.wall, CLOCK.mono, CLOCK.boot)


def of(r):
    """The awake clock of the fleet whose Redis is ``r``, for a process that is not its daemon (a CLI, the parent
    of a named sub-fleet). Measured locally from now on when nothing is published."""
    clock = local()
    clock.sync(r)
    return clock
