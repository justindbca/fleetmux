"""Copy the other machines' measured capabilities into this machine's inventory (R20).

Each machine publishes its measured block in its node record (`machine`). On join and on every round, each
machine stores its peers' blocks as `machines.<peer>` of its own fleet-superpowers inventory, through the
inventory's own write path (`import_copy`). A copy keeps its other fields (hostname, reach, clis ...); only
`hardware`, `always_on` and `toolchains` are refreshed. This machine's own entry (`source: self`) is never written.
"""
from fleetmux import fleet_superpowers as fs

HARDWARE = ('cores', 'perf_cores', 'mem_gb', 'disk_free_gb')


def section(machine):
    """The inventory fields of a node record's `machine` block."""
    return {'hardware': {k: machine.get(k) for k in HARDWARE}, 'always_on': bool(machine.get('always_on')),
            'toolchains': dict(machine.get('toolchains') or {})}


def sync_peers(nodes, me, *, path=None):
    """Write the peers of `nodes` ({node: node record}) that changed; returns the names written."""
    current = fs.read(path)['machines']
    written = []
    for node in sorted(nodes):
        machine = (nodes[node] or {}).get('machine') or {}
        have = current.get(node)
        if node == me or not machine or (isinstance(have, dict) and have.get('source') == 'self'):
            continue
        new = section(machine)
        have = have if isinstance(have, dict) else {}
        if all(have.get(k) == v for k, v in new.items()):
            continue
        entry = {k: v for k, v in have.items() if k not in ('source', 'copied_at')}
        entry.update(new)
        fs.import_copy(node, entry, path=path)
        written.append(node)
    return written
