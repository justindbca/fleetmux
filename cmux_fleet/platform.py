"""Platform seams: the few places where macOS and Linux differ.

`keep_awake_prefix()` is the only one this module owns today (design §6): the daemon should keep the
machine awake while a fleet runs, but the tool differs per platform and Linux boxes may not have one.
Callers prepend the returned list to a command; an empty list means "run it as is".
"""
import shutil
import subprocess
import sys

PROBE_TIMEOUT = 5  # seconds; systemd-inhibit asks logind over D-Bus, which answers at once or not at all


def keep_awake_prefix(platform=None, which=shutil.which, run=subprocess.run):
    """Command prefix that keeps the machine awake for the lifetime of the spawned command.

    macOS: `caffeinate -i` (idle sleep only, as before). Linux: `systemd-inhibit --what=idle:sleep`
    when systemd-inhibit is on PATH **and actually works here**: it is probed with `true` first, because
    polkit refuses the inhibitor outside an active local session (started over ssh, on a headless
    server: "Failed to inhibit: Access denied", exit 1), and a daemon wrapped in a refused inhibitor
    dies at once. Anything else, the tool missing or refused: no prefix (`[]`) — the daemon still runs,
    the machine may sleep. `platform` / `which` / `run` are injectable so tests do not depend on the
    host actually being macOS or Linux.
    """
    platform = sys.platform if platform is None else platform
    if platform == 'darwin':
        tool = which('caffeinate')
        return [tool, '-i'] if tool else []
    if platform.startswith('linux'):
        tool = which('systemd-inhibit')
        if not tool:
            return []
        try:
            probe = run([tool, '--what=idle:sleep', '--why=cmux-fleet probe', 'true'], capture_output=True,
                        text=True, timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return []
        return [tool, '--what=idle:sleep', '--why=cmux-fleet'] if probe.returncode == 0 else []
    return []
