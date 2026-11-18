"""The coordinator role (docs/relay.md, "Coordinator"): one task record named `coordinator`. Its holder's
supervisor is the main supervisor: it gets the peer notices (online, idle, gone) and the unplaceable notices.

It is light, needs no toolchain, starts no sub-fleet, and prefers always-on machines. When its holder leaves or is
lost, the next machine in order takes it, so no single machine is the main supervisor for good. Configuration
(`relay.coordinator`): `enabled` (create the record at init), `prefer` (a fixed machine first), `takeover` (false
keeps the role on its holder even when that holder is gone).
"""
from . import jobfile, knobs, placement

NAME = 'coordinator'
DEFAULTS = {'enabled': True, 'prefer': '', 'takeover': True}


def conf(params):
    """`relay.coordinator` (a cold key of the relay block), with the defaults for keys it leaves out."""
    raw = getattr(params, 'coordinator', None)
    if raw is None and isinstance(getattr(params, 'block', None), dict):
        raw = params.block.get('coordinator')
    if raw is None:
        try:
            raw = knobs.get(params, 'coordinator')
        except (KeyError, TypeError):
            raw = None
    raw = raw or {}
    out = dict(DEFAULTS)
    out.update({k: raw[k] for k in DEFAULTS if k in raw})
    return out


def is_role(seg):
    return seg == NAME


def record(job, c, *, base):
    """The role's task record; whether it may move (`takeover`) is read from the configuration."""
    return jobfile.new_record(job, NAME, {'title': 'coordinate the job', 'weight': 'light', 'needs': [],
                                          'depends_on': [], 'prefer': c['prefer'] or None}, base=base)


def job_records(doc, params, *, base):
    """All records `relay init` creates: segments, merge, and the coordinator when enabled."""
    out = jobfile.initial_records(doc, base=base)
    c = conf(params)
    if c['enabled']:
        out[NAME] = record(doc['job'], c, base=base)
    return out


def rank(rec, nodes, judged, params, score=None):
    """Online machines for the role: `prefer` first, then always-on ones, then by capability score."""
    score = score or placement.score
    cands = placement.candidates(nodes, judged, {})
    cands.sort(key=lambda c: (not c['machine'].get('always_on'), -score(c['machine'], params), c['id']))
    names = [c['id'] for c in cands]
    if rec.get('prefer') in names:
        names = [rec['prefer']] + [n for n in names if n != rec['prefer']]
    return names


def takeable(rec, judged, c):
    """The role may move: its holder is gone (lost or leaving) and the configuration allows takeover."""
    if not c['takeover']:
        return False
    owner = rec.get('owner')
    return rec.get('state') in ('working', 'handed_over') and judged.get(owner) in ('lost', 'leaving')


def holder(table):
    rec = table.get(NAME)
    if rec and rec.get('state') == 'working':
        return rec.get('owner')
    return None
