"""Dependency advance (04-design section 7). A `blocked` segment becomes `pending` once every segment it depends
on is `delivered`; it starts from the first dependency's delivered commit (`base`) and merges the others
(`merge_in`) as its first step. Every machine computes this; the change is pushed by compare-and-swap, so one
push lands and the others are refused without retry. The merge segment is an ordinary segment that depends on all
others; the job is done when it is delivered."""
from . import records as R

CLAIMABLE = ('pending', 'reclaimed')
MERGE = 'merge'


def claimable(rec):
    return rec.get('state') in CLAIMABLE


def _commit(rec):
    return (rec.get('delivered') or {}).get('commit') if rec and rec.get('state') == 'delivered' else None


def waiting_on(table, seg):
    """Dependencies of `seg` that are not delivered yet (unknown ones included)."""
    return [d for d in table[seg].get('depends_on') or [] if not _commit(table.get(d))]


def advance(table):
    """{seg: new record} for every blocked segment whose dependencies are all delivered."""
    out = {}
    for seg in sorted(table):
        rec = table[seg]
        if rec.get('state') != 'blocked' or waiting_on(table, seg):
            continue
        commits = [_commit(table[d]) for d in rec.get('depends_on') or []]
        if commits:
            out[seg] = R.unblock(rec, base=commits[0], merge_in=commits[1:])
        else:
            out[seg] = R.unblock(rec, base=rec.get('base'))
    return out


def job_done(table):
    return (table.get(MERGE) or {}).get('state') == 'delivered'
