"""Capability keys of this machine's ``machines`` entry (docs/distributed-development.md, "machines entry schema"):
``hardware`` (cores, perf_cores, mem_gb, disk_free_gb), ``always_on`` and ``toolchains``.

``fleetmux superpowers refresh`` writes them after the rest of the entry (``write``): measured here, stored as
their own keys of ``machines.<self>``. The relay places work by them (heavy tasks on strong machines, no task on
a machine without a needed toolchain or free disk). Toolchains are found on ``discover.search_path()`` and only
asked for their version: nothing is installed or changed. ``always_on`` is measured only as a default; a stored
value (the owner's setting) is kept. Every source is a parameter, so tests never run real system queries.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import fleet_superpowers as fs

KEYS = ('hardware', 'always_on', 'toolchains')
HARDWARE_KEYS = ('cores', 'perf_cores', 'mem_gb', 'disk_free_gb')
# Toolchain name (as job definitions write it in ``needs``) -> the command that prints its version.
TOOLCHAINS = {'rust': ['rustc', '--version'], 'node': ['node', '--version'], 'python': ['python3', '--version'],
              'go': ['go', 'version']}
SYSTEM_TIMEOUT = 5.0  # seconds for one system query or version command
GIB = 1024 ** 3


def _system(argv):
    """stdout of a short system query, or None (missing, failed, timed out)."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=SYSTEM_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _int(text):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def hardware_of(run=_system, platform=sys.platform, disk_path=None, cpu_count=os.cpu_count,
                disk_usage=shutil.disk_usage, meminfo='/proc/meminfo'):
    """{cores, perf_cores, mem_gb, disk_free_gb} of this machine (GiB, one decimal). ``perf_cores`` = cores without
    performance levels; ``disk_free_gb`` is measured on ``disk_path`` (default: the home volume)."""
    cores = perf = mem = None
    if platform == 'darwin':
        cores = _int(run(['sysctl', '-n', 'hw.ncpu']))
        perf = _int(run(['sysctl', '-n', 'hw.perflevel0.physicalcpu']))
        mem = _int(run(['sysctl', '-n', 'hw.memsize']))
    else:
        try:
            for line in Path(meminfo).read_text().splitlines():
                if line.startswith('MemTotal:'):
                    mem = int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
    cores = cores or cpu_count() or 1
    perf = perf if perf and perf <= cores else cores
    free = disk_usage(Path.home() if disk_path is None else disk_path)[2]
    return {'cores': cores, 'perf_cores': perf, 'mem_gb': round((mem or 0) / GIB, 1),
            'disk_free_gb': round(free / GIB, 1)}


def has_battery(run=_system, platform=sys.platform, power_supply='/sys/class/power_supply'):
    """True on a machine with an internal battery (a laptop: it sleeps when the lid closes)."""
    if platform == 'darwin':
        return 'InternalBattery' in (run(['pmset', '-g', 'batt']) or '')
    try:
        return any(p.name.startswith('BAT') for p in Path(power_supply).iterdir())
    except OSError:
        return False


def toolchains_of(search=None, probes=None):
    """{name: version} of the toolchains found on ``search`` (default ``discover.search_path()``, the one resolver;
    nothing when discovery is off). A tool that is missing or prints no version is left out."""
    from . import discover
    if search is None:
        if os.environ.get(discover.OFF_ENV, '').strip().lower() == 'off':
            return {}
        search = discover.search_path()
    out = {}
    for name, argv in (TOOLCHAINS if probes is None else probes).items():
        path = shutil.which(argv[0], path=search)
        have = discover.version(_system([path] + list(argv[1:]))) if path else None
        if have:
            out[name] = '.'.join(map(str, have))
    return out


def measure():
    """The measured capability keys; ``always_on`` is only the default (no battery)."""
    return {'hardware': hardware_of(), 'always_on': not has_battery(), 'toolchains': toolchains_of()}


def write(name, path=None, facts=None, lock_timeout=None):
    """Store the capability keys in ``machines.<name>`` (this machine's own entry) and return what was stored.
    Measures outside the lock; under it keeps every other key and a stored ``always_on``."""
    facts = measure() if facts is None else dict(facts)

    def change(raw):
        current = (raw.get('machines') or {}).get(name)
        if not isinstance(current, dict) or current.get('source') != 'self':
            raise fs.InventoryError('%s is not this machine\'s own entry' % name)
        stored = {k: facts[k] for k in KEYS if k in facts}
        if isinstance(current.get('always_on'), bool):
            stored['always_on'] = current['always_on']
        return ('machines', name), dict(current, **stored), False, stored
    return fs._update(path, change, lock_timeout)


def machine_facts(entry):
    """The flat capability record of a ``machines`` entry, as relay node status carries it (``machine``):
    {cores, perf_cores, mem_gb, disk_free_gb, always_on, toolchains}."""
    entry = entry or {}
    return dict(entry.get('hardware') or {}, always_on=entry.get('always_on'),
                toolchains=dict(entry.get('toolchains') or {}))


def _count(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


def _amount(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def problems(at, entry):
    """Schema problems of the capability keys of one ``machines`` entry (``at`` = ``machines.<name>``)."""
    out = []
    hw = entry.get('hardware')
    if hw is not None:
        if not (isinstance(hw, dict) and all(_count(hw.get(k)) for k in ('cores', 'perf_cores'))
                and all(_amount(hw.get(k)) for k in ('mem_gb', 'disk_free_gb'))):
            out.append(at + '.hardware must be {cores, perf_cores: int >= 1, mem_gb, disk_free_gb: number >= 0}')
        elif hw['perf_cores'] > hw['cores']:
            out.append(at + '.hardware.perf_cores must not exceed hardware.cores')
    if 'always_on' in entry and not isinstance(entry['always_on'], bool):
        out.append(at + '.always_on must be true or false')
    chains = entry.get('toolchains')
    if chains is not None and not (isinstance(chains, dict) and all(
            isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip() for k, v in chains.items())):
        out.append(at + '.toolchains must map toolchain -> version string')
    return out
