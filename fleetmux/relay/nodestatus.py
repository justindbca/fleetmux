"""What a machine measures about itself for its node record: cores, performance cores, memory and free disk (both
in GiB, 2^30 bytes, under the keys mem_gb / disk_free_gb, like the superpowers inventory). The
always-on flag and the toolchains come from this machine's inventory entry (fleetmux superpowers) and are passed
in; nothing is installed or changed."""
from __future__ import annotations

import os
import shutil
import subprocess

GIB = 2 ** 30              # unit conversion only (bytes -> GiB, as the superpowers inventory), not a threshold
DIGITS = 1                 # GiB values are rounded to one decimal
PERF_CORES_ARGV = ["sysctl", "-n", "hw.perflevel0.physicalcpu"]


def measure_machine(disk_path, *, toolchains: dict, always_on: bool, probe_timeout_s: float,
                    run=subprocess.run) -> dict:
    """`probe_timeout_s` = relay.probe_timeout_s (limit of the local performance-core probe)."""
    cores = os.cpu_count() or 0
    perf = cores
    try:
        proc = run(PERF_CORES_ARGV, capture_output=True, text=True, timeout=probe_timeout_s)
        if proc.returncode == 0 and proc.stdout.strip().isdigit():
            perf = int(proc.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        mem = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / GIB, DIGITS)
    except (ValueError, OSError, AttributeError):
        mem = None
    try:
        free = round(shutil.disk_usage(disk_path).free / GIB, DIGITS)
    except OSError:
        free = None
    return {"cores": cores, "perf_cores": perf, "mem_gb": mem, "disk_free_gb": free, "always_on": bool(always_on),
            "toolchains": dict(toolchains or {})}
