"""Model chains of a job's sub-fleet (docs/design.md §58 "Model chains"; user 2026-11-22: the program reads the
configuration and picks the job's supervisor and worker models itself).

Pure functions: no run directory, no Redis. Every chain is resolved with ``discover.chain`` (probe cache, only_via,
cli_order); walls are skipped with ``brake.wall_action``; the pair table decides which worker CLIs may run under the
job's Claude supervisor. ``explain`` only says why a pattern resolves to nothing; it never picks."""
import json

from fleetmux import brake, cli_pairs, discover, fleet_config

LAYERS = ('spec', 'job_subfleet', 'discovery')
MODE = 'task'  # a job sub-fleet always runs in task mode (subfleet.child_config)
WORKER_CLIS = ('claude', 'pi')  # design §51: a sub-fleet's workers run claude or pi
SUPERVISOR_WALL = 'supervisor wall: stop (adopted pane cannot rotate)'
NOT_CLAUDE = ('runs {cli}, not claude: a sub-fleet supervisor is an adopted official Claude Code pane '
              '(subfleet.check)')
CARD_SLOTS = 'the job has card slots and a pi sub-fleet takes no cards (design §51)'
NOT_WORKER = 'a sub-fleet runs claude or pi workers only (design §51)'
NOTHING = ' Nothing was submitted.'


def _disc(disc):
    return fleet_config.block('discovery') or {} if disc is None else disc


# ------------------------------------------------------------------ layers

def effective(spec, job_cfg=None, disc=None):
    """The chains and cards a job runs with: spec > job_subfleet > discovery. A higher layer's chain voids a lower
    layer's card and the other way round; one layer may not give a chain and a card for the same role (only roster
    rows mix: a row with a profile runs its card, the other rows the chain). A value
    of None counts as not given; [] is an error (ValueError)."""
    job_cfg = fleet_config.job_subfleet() if job_cfg is None else job_cfg
    disc = _disc(disc)
    spec_sup = fleet_config.check_chain(spec.get('supervisor'), 'job spec supervisor', 'left out')
    spec_card = spec.get('supervisor_profile')
    if spec_card is not None and not isinstance(spec_card, str):
        raise ValueError('supervisor_profile must be an agent card id')
    if spec_sup and spec_card:
        raise ValueError('job spec sets both supervisor (a model chain) and supervisor_profile (a card); give one of '
                         'them')
    spec_wrk = fleet_config.check_chain(spec.get('worker'), 'job spec worker', 'left out')
    overrides = spec.get('overrides') if isinstance(spec.get('overrides'), dict) else {}
    if overrides.get('worker_model') is not None:
        if spec_wrk:
            raise ValueError('job spec sets both worker (a model chain) and overrides.worker_model; give one of them')
        spec_wrk = fleet_config.check_chain([overrides['worker_model']], 'job spec overrides.worker_model', 'left out')
    spec_cards = spec.get('worker_profiles') or []
    if not isinstance(spec_cards, list):
        raise ValueError('worker_profiles must be a list of agent card ids')
    if spec_wrk and spec_cards:  # D23: the cards cover every slot; the chain would never be used
        key = 'worker' if spec.get('worker') is not None else 'overrides.worker_model'
        raise ValueError('job spec sets both ' + key + ' (a model chain) and worker_profiles (cards); give one of them')
    cfg_sup = fleet_config.check_chain(job_cfg.get('supervisor'), 'fleet.yaml#job_subfleet.supervisor')
    cfg_card = job_cfg.get('supervisor_profile')
    if cfg_sup and cfg_card:
        raise ValueError('fleet.yaml#job_subfleet sets both supervisor (a model chain) and supervisor_profile (a '
                         'card); set one of them to null')
    cfg_wrk = fleet_config.check_chain(job_cfg.get('worker'), 'fleet.yaml#job_subfleet.worker')
    cfg_cards = [] if spec.get('roster') else list(job_cfg.get('worker_profiles') or [])  # a roster names the cards

    out = {'supervisor': None, 'worker': None, 'supervisor_profile': None, 'worker_profiles': [],
           'source': {'supervisor': None, 'worker': None, 'supervisor_profile': None, 'worker_profiles': None}}
    for layer, chain, card in (('spec', spec_sup, spec_card), ('job_subfleet', cfg_sup, cfg_card)):
        if chain:
            out['supervisor'], out['source']['supervisor'] = chain, layer
            break
        if card:
            out['supervisor_profile'], out['source']['supervisor_profile'] = card, layer
            break
    else:
        out['supervisor'], out['source']['supervisor'] = list(disc.get('supervisor') or []), 'discovery'
    if cfg_wrk and cfg_cards:  # fleet_config.job_subfleet refuses this too; a hand-made job_cfg may still carry it
        raise ValueError('fleet.yaml#job_subfleet sets both worker (a model chain) and worker_profiles (cards); '
                         'set worker to null or worker_profiles to []')
    for layer, chain, cards in (('spec', spec_wrk, spec_cards), ('job_subfleet', cfg_wrk, cfg_cards)):
        if chain:
            out['worker'], out['source']['worker'] = chain, layer
            break
        if cards:
            out['worker_profiles'], out['source']['worker_profiles'] = list(cards), layer
            break
    else:
        out['worker'], out['source']['worker'] = list(disc.get('worker') or []), 'discovery'
    return out


# ------------------------------------------------------------------ explain

def explain(pattern, role, disc=None, cached=None):
    """None when ``pattern`` resolves to at least one entry of ``discover.chain`` for ``role``; else why not, one
    line. Only explains: the pick is ``discover.chain``'s (a test keeps the two in step)."""
    disc = _disc(disc)
    order = list(disc.get('cli_order') or [])
    data = discover.load() if cached is None else cached

    def why(cli, model):
        via = [v for p, v in (disc.get('only_via') or {}).items() if discover.matches(p, model)]
        if cli not in order:
            return cli + ' is not in discovery.cli_order'
        if via and cli not in via:
            return 'only_via sends it to ' + ', '.join(via)
        if role == 'supervisor' and cli not in cli_pairs.CLIS:
            return cli + ' cannot run a supervisor'
        return None

    reasons = []
    if data and isinstance(data.get('clis'), dict):
        gateways = data.get('gateways') or {}
        for cli, row in data['clis'].items():
            for model in [m for m in (row or {}).get('models') or [] if discover.matches(pattern, m)]:
                provider = model.split('/', 1)[0]
                reason = (why(cli, model) if cli not in order else
                          cli + ' is not installed' if not row.get('installed') else
                          cli + ' ' + str(row.get('version')) + ' is below ' + str(row.get('min'))
                          if not row.get('version_ok') else
                          cli + ' is not logged in' if row.get('logged_in') is False else
                          why(cli, model) or ('gateway ' + provider + ' offline' if cli == 'pi' and gateways.get(
                              provider, {}).get('online') is False else None))
                if reason is None:
                    return None
                reasons.append(cli + ':' + model + ': ' + reason)
        if reasons:
            return '; '.join(reasons)
        have = [c for c, r in data['clis'].items() if (r or {}).get('installed')]
        return ('no model on this machine matches (cached CLIs: ' + ', '.join(have)
                + '; `fleetmux setup` probes again)')
    for card in (fleet_config.block('models') or {}).values():
        cli, model = card.get('cli'), card.get('model')
        if model and discover.matches(pattern, model):
            reason = why(cli, model)
            if reason is None:
                return None
            reasons.append(cli + ':' + model + ': ' + reason)
    return ('no discovery cache: only the model cards were considered (run `fleetmux setup`)'
            + (': ' + '; '.join(reasons) if reasons else ': no card model matches'))


# ------------------------------------------------------------------ picks

def _pick(role, patterns, walled, disc, cached, own_reason):
    """The first entry of ``role``'s chain that no wall and no ``own_reason(cli, model)`` refuses, with the skipped
    entries before it (and the patterns before it that resolve to nothing). Returns (entry or None, skipped)."""
    patterns = list(patterns)
    data = discover.load() if cached is None else cached
    entries = discover.chain(role, (), dict(disc, **{role: patterns}), data)[0]
    mine = {}

    def refused(cli, model):
        reason = own_reason(cli, model)
        if reason:
            mine[(cli, model)] = reason
        return reason

    action = brake.wall_action(None, None, entries, list(walled), refused)
    reasons = {(r['cli'], r['model']): r['reason'] for r in action.get('skipped_rows') or []}
    reasons.update(mine)
    chosen = (action['to_cli'], action['to']) if action['action'] == 'switch' else None
    upto = entries.index(chosen) if chosen else len(entries)
    rows = [(discover.first_match(patterns, m), i, {'pattern': patterns[discover.first_match(patterns, m)],
                                                      'cli': c, 'model': m, 'reason': reasons.get((c, m))})
            for i, (c, m) in enumerate(entries[:upto])]
    last = discover.first_match(patterns, chosen[1]) if chosen else len(patterns)
    for i, pattern in enumerate(patterns[:last]):
        if not any(discover.first_match(patterns, m) == i for _, m in entries):
            reason = explain(pattern, role, disc, data)
            if reason:
                rows.append((i, -1, {'pattern': pattern, 'cli': None, 'model': None, 'reason': reason}))
    skipped = [row for _, _, row in sorted(rows, key=lambda r: (r[0], r[1]))]
    return chosen, skipped


def line(row):
    """One skipped row as text: '<pattern>: <cli>:<model>: <reason>'."""
    return (row['pattern'] + ': ' + (row['cli'] + ':' + row['model'] + ': ' if row.get('cli') else '')
            + str(row['reason']))


def _none_reason(role, patterns, skipped):
    return ('no usable entry on the ' + role + ' chain ' + json.dumps(list(patterns)) + ': '
            + ('; '.join(line(r) for r in skipped) or 'it resolves to no model on this machine'))


def pick_worker(patterns, supervisor_cli='claude', walled=(), disc=None, cached=None, pairs=None, refused=None,
                card_slots=False):
    """The job's worker model: the first worker-chain entry with no wall that runs claude or pi, pairs verified
    with ``supervisor_cli`` (task mode), is not pi when the job has card slots, and that ``refused`` lets through.
    ``overrides`` go straight into the sub-fleet config."""
    disc = _disc(disc)
    pairs = pairs or cli_pairs.load_pairs()
    thinking = disc.get('worker_thinking') or 'high'

    def own(cli, model):
        if cli not in WORKER_CLIS:
            return NOT_WORKER
        if card_slots and cli == 'pi':
            return CARD_SLOTS
        row = pairs.lookup(supervisor_cli, cli, MODE) or {}
        if row.get('status') != 'verified' or row.get('adapter_missing'):
            return (supervisor_cli + ' supervisor + ' + cli + ' workers (' + MODE + ') is '
                    + (('verified, adapter missing' if row.get('status') == 'verified' else row['status'])
                       if row else 'not in the pair table'))
        return refused(cli, model) if refused else None

    chosen, skipped = _pick('worker', patterns, walled, disc, cached, own)
    if not chosen:
        return {'entry': None, 'overrides': {}, 'skipped': skipped, 'reason': _none_reason('worker', patterns, skipped)}
    cli, model = chosen
    overrides = ({'worker_cli': 'pi', 'pi_model': model, 'pi_thinking': thinking} if cli == 'pi'
                 else {'worker_cli': 'claude', 'worker_model': model})
    return {'entry': {'cli': cli, 'model': model, 'thinking': thinking}, 'overrides': overrides, 'skipped': skipped,
            'reason': None}


def card_for(model, registry):
    """The Claude card that runs ``model``: id claude-<model> first, else the first by id; None without one."""
    ids = sorted(c.id for c in registry.cards if c.cli == 'claude' and c.model == model)
    return 'claude-' + model if 'claude-' + model in ids else (ids[0] if ids else None)


def pick_supervisor(patterns, walled=(), disc=None, cached=None, registry=None, refused=None):
    """The job's sub-fleet supervisor: the first supervisor-chain entry with no wall that runs official Claude Code
    and that ``refused`` lets through, with its card (or None) and the launch overrides that card would give."""
    disc = _disc(disc)

    def own(cli, model):
        if cli != 'claude':
            return NOT_CLAUDE.format(cli=cli)
        return refused(cli, model) if refused else None

    chosen, skipped = _pick('supervisor', patterns, walled, disc, cached, own)
    if not chosen:
        return {'entry': None, 'profile': None, 'overrides': {}, 'skipped': skipped,
                'reason': _none_reason('supervisor', patterns, skipped)}
    if registry is None:
        from fleetmux.agent_registry import AgentRegistry
        registry = AgentRegistry()
    model = chosen[1]
    return {'entry': {'cli': 'claude', 'model': model, 'thinking': None}, 'profile': card_for(model, registry),
            'overrides': {'worker_cli': 'claude', 'worker_model': model}, 'skipped': skipped, 'reason': None}


def subfleet_supervisor(walled=(), disc=None, cached=None, registry=None, refused=None):
    """The supervisor of any sub-fleet that names no model or card of its own (a named sub-fleet, a §44 split;
    user 2026-11-22: every sub-fleet walks the one supervisor chain in order): ``pick_supervisor`` over
    discovery.supervisor, plus ``chain`` (those patterns) and ``source`` ('discovery') for ``trace``."""
    disc = _disc(disc)
    chain = list(disc.get('supervisor') or [])
    pick = pick_supervisor(chain, walled, disc, cached, registry, refused)
    return dict(pick, chain=chain, source='discovery')


def runs_pick(pick, chain, cli, model):
    """True when a slot running (``cli``, ``model``) already runs ``pick``'s entry: the same place on ``chain``
    (claude-opus-5-5 and opus count as one). Such a slot is adopted in place, with no rotation."""
    entry = pick.get('entry')
    if not entry or cli != entry['cli']:
        return False
    return discover.place([(entry['cli'], entry['model'])], list(chain), cli, model) == 0


# ------------------------------------------------------------------ submit

def _slots(spec, roster_rows, eff, job_cfg):
    """(worker slots, slots a card decides)."""
    if roster_rows is not None:
        return len(roster_rows), sum(1 for r in roster_rows if isinstance(r, dict) and r.get('profile'))
    workers = spec.get('workers')
    if type(workers) is not int:
        workers = len(eff['worker_profiles']) or job_cfg.get('workers') or 0
    return workers, len(eff['worker_profiles'])


def submit_problem(spec, roster_rows=None, job_cfg=None, disc=None, cached=None, refused=None):
    """None when the job's chains resolve on this machine, else why the job is refused at submit (one kind of
    problem, the first found). Walls and quotas are not looked at: they only count when the job starts."""
    rows = [r for r in roster_rows or [] if isinstance(r, dict)]
    pi_rows = [r for r in rows if 'pi_model' in r or 'pi_thinking' in r]
    if pi_rows:
        keys = [k for k in ('pi_model', 'pi_thinking') if any(k in r for r in pi_rows)]
        names = ', '.join(str(r.get('name')) for r in pi_rows)
        return ('job roster ' + str(spec.get('roster')) + ': row' + ('s ' if len(pi_rows) > 1 else ' ') + names
                + (' set ' if len(pi_rows) > 1 else ' sets ') + ', '.join(keys) + '. A job takes its worker model '
                'from its worker chain, not from roster rows. Remove pi_model / pi_thinking from the row and put the '
                'model in the job spec: "worker": ["astra"] (or fleet.yaml#job_subfleet.worker).' + NOTHING)
    job_cfg = fleet_config.job_subfleet() if job_cfg is None else job_cfg
    disc = _disc(disc)
    try:
        eff = effective(spec, job_cfg, disc)
    except ValueError as exc:
        return str(exc) + '.' + NOTHING
    data = discover.load() if cached is None else cached
    total, carded = _slots(spec, roster_rows, eff, job_cfg)
    if total > carded:
        chain = eff['worker'] if eff['worker'] is not None else list(disc.get('worker') or [])
        source = eff['source']['worker'] or 'discovery'
        got = pick_worker(chain, 'claude', (), disc, data, None, refused, card_slots=carded > 0)
        if got['entry'] is None:
            return ('job worker chain ' + json.dumps(chain) + ' (from ' + source + ') has no usable entry on this '
                    'machine:\n' + ''.join('  - ' + line(r) + '\n' for r in got['skipped']) + NOTHING.strip())
    if eff['supervisor'] is not None:
        got = pick_supervisor(eff['supervisor'], (), disc, data, _NoCards, refused)
        if got['entry'] is None:
            return ('job supervisor chain ' + json.dumps(eff['supervisor']) + ' (from ' + eff['source']['supervisor']
                    + ') has no usable Claude entry on this machine:\n'
                    + ''.join('  - ' + line(r) + '\n' for r in got['skipped']) + NOTHING.strip())
    return None


class _NoCards:
    """Submit needs no card lookup: the card is chosen when the job starts."""
    cards = ()


# ------------------------------------------------------------------ trace

def trace(pick, source, switched=()):
    """One slot's record for model_chain = {'supervisor': trace, 'workers': {slot: trace}}."""
    entry = pick.get('entry') or {}
    return {'cli': entry.get('cli'), 'model': entry.get('model'), 'source': source, 'profile': pick.get('profile'),
            'skipped': list(pick.get('skipped') or []), 'switched': list(switched)}
