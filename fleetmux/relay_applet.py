"""The relay as a built-in applet of the root fleet (docs/applets.md, "Built-in applets").

With ``relay.enabled: true`` in the relay block of fleetmux/config/fleet.yaml (default false; a private fleet.yaml
may set it), the daemon of a root transport v2 service fleet declares the applet ``applet-relay-1`` itself, with no
roster row. From then on it is an ordinary applet of that fleet: the daemon starts it, restarts it after a crash,
and stops it when the switch is turned off or the fleet stops. Nothing outside the fleet (no cron, no system
service) keeps it running. The switch is read when the daemon starts (and resumes) and whenever the roster is
reloaded; a change takes effect at the next daemon restart or roster reload.

The process is the relay's own entry, ``python -m fleetmux.relay run --node <name>``, where ``<name>`` is this
machine's name: the ``source: self`` entry of the fleet-superpowers inventory (``fleetmux superpowers refresh
--name <name>`` writes it once). As any applet it writes its heartbeat through the applet SDK
(docs/applet-interface.md section 2.4); ``loop_heartbeat`` gives the relay loop a beat it drives itself, so a hung
loop stops beating and the host sees a stall.

Known limitation: every root transport v2 service fleet of a machine with the switch on starts its own relay. Run
one root service fleet per machine with ``relay.enabled: true``.
"""
import os
import time
NAME = 'applet-relay-1'
NAMES = (NAME,)
ENTRY = ['{python}', '-m', 'fleetmux.relay', 'run', '--node']  # + this machine's name

def enabled():
    """``relay.enabled`` is true, read through the one relay accessor ``fleet_config.relay()`` (shipped block with
    the private one laid over it, validated). Fail-safe off: any error (no block, an unreadable file, a block that
    fails validation) or a value other than True reads as switched off, so a broken file never starts the relay."""
    from fleetmux import fleet_config
    try:
        return getattr(fleet_config.relay(), 'enabled', False) is True
    except Exception:  # noqa: BLE001 - the daemon must keep running; off is the safe reading
        return False


def machine_name():
    """This machine's name: the ``source: self`` entry of the fleet-superpowers inventory, or None."""
    from fleetmux import fleet_superpowers as fs
    try:
        return fs.self_name()
    except fs.InventoryError:
        return None


def hosts(cfg):
    """True for the one fleet per machine that hosts the relay: a root (no parent) transport v2 service fleet."""
    cfg = cfg or {}
    return (not cfg.get('parent_config') and cfg.get('mode') == 'service'
            and cfg.get('transport_version') == 2)


def specs(cfg, workers):
    """The built-in applet entries of the fleet with run config ``cfg``: ``[]`` unless it hosts the relay and the
    switch is on. Each entry is validated and filled like a roster row (roster.applet_entries). ValueError when
    it cannot be built: no machine name yet, or no worker row for the applet lease policy."""
    if not hosts(cfg) or not enabled():
        return []
    name = machine_name()
    if not name:
        raise ValueError('built-in relay applet: this machine has no name in the fleet-superpowers inventory; '
                         'run `fleetmux superpowers refresh --name <name>` once')
    from pathlib import Path
    from fleetmux import roster
    entries, errors = roster.applet_entries([{'name': NAME, 'command': ENTRY + [name]}], Path(cfg['run']),
                                            list(workers or []))
    if errors:
        raise ValueError('built-in relay applet: ' + '; '.join(errors))
    return entries


def reserved(entries):
    """Problems of roster applet entries that take a built-in applet's name."""
    return [e['name'] + ': the name is reserved for the built-in relay applet (relay.enabled)'
            for e in entries or [] if e.get('name') in NAMES]


def compose(current, cfg, workers):
    """``current`` applet entries without built-in names, plus the built-in ones of ``cfg``."""
    return [e for e in current or [] if e.get('name') not in NAMES] + specs(cfg, workers)


class LoopHeartbeat:
    """The applet heartbeat driven by the relay main loop (no heartbeat thread). Call ``tick()`` on every loop pass
    (and while waiting, once a second is enough): it writes ``hb_at`` at most once per ``heartbeat_s``. While the
    loop does not come round, nothing beats, so the host judges a hung loop as stalled after ``stale_after_s``."""

    def __init__(self, applet, every_s=None, clock=time.monotonic):
        self.applet = applet
        self.every_s = float(applet.heartbeat_s if every_s is None else every_s)
        self._clock = clock
        self._last = None

    def tick(self):
        now = self._clock()
        if self._last is not None and now - self._last < self.every_s:
            return False
        self.applet.beat()
        self._last = now
        return True


def loop_heartbeat(env=None):
    """For the relay entry started by the host (``FLEETMUX_APPLET_*`` set): the applet SDK without its heartbeat
    thread (``Applet.from_env(start=False)``), one beat at once, and a ``LoopHeartbeat`` for the loop. None when
    the entry runs outside the host (typed by hand). The entry handles SIGTERM itself."""
    env = os.environ if env is None else env
    if not env.get('FLEETMUX_APPLET_NAME'):
        return None
    from fleetmux.applet import Applet
    beat = LoopHeartbeat(Applet.from_env(dict(env), start=False))
    beat.tick()
    return beat
