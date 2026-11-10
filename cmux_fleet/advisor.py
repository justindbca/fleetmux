"""Fleet advisor: one short official-Claude consultation before each dispatch (advisor plan §3.1).

This module packs the advisor input, runs the one-shot advisor process (injectable for tests),
validates its configuration sheet and decides how the daemon applies it. It holds no Redis
state and never starts a worker: the daemon (fleet.py) does that through the existing Phase 5
respawn path or a domain hot-load. The advisor only ever sees what the supervisor wrote
(goal / instruction / advise_note), never a worker conversation.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from cmux_fleet import fleet_config

HERE = Path(__file__).resolve().parent
PROMPT = HERE / 'agents' / 'advisor.md'
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
DEFAULT_EFFORT = 'high'  # user 2026-11-11 「默认worker也是high」
VERDICTS = ('enough', 'enough_simple', 'not_enough', 'overkill', 'untested', 'reserve')
TIMEOUT = 90
ATTEMPTS = 2  # one retry, then the fallback sheet (§8)
MODEL, EFFORT = 'fable', 'high'  # user boundary 2: Fable / high / official CLI, never pi
# Resident advisor (docs/design.md §27, v2 task mode): one official Claude tab next to the supervisor.
# It is a slot of the fleet (bus, hooks, watchdog, handoff) but never a worker: no dispatch, no report.
SLOT = 'advisor'
RESIDENT_NOTE = '''

## Resident session (the fleet's advisor tab)

You run as a resident session in the fleet's advisor tab instead of once per call. Your first turn: reply
exactly READY. After that every message is one consultation from the daemon: a first line
`[Advisor consultation <ticket>]`, then the input JSON object described above (it replaces stdin). Answer
each consultation on its own input; earlier consultations in this session are history, not input. Reply with
the one JSON object only. A `[Advisor correction <ticket>]` message says why the daemon refused your last
sheet and repeats the input: reply again with one corrected sheet. Use no tool except Read, and only for a
file the daemon names (a handoff note).
'''


class AdviceError(ValueError):
    """The advisor did not produce a usable configuration sheet."""


def load_matrix(path=None):
    """Capability matrix block (data, re-read per consultation). Unreadable -> {} plus the error text."""
    try:
        data = fleet_config.block('capability_matrix', path) or {}
    except Exception as exc:
        return {}, str(exc)
    return (data, None) if isinstance(data, dict) else ({}, 'capability matrix must be a mapping')


def runtime_model(cfg, cli):
    if cli == 'claude':
        return cfg.get('worker_model') or 'sonnet'
    return {'pi': cfg.get('pi_model'), 'opencode': cfg.get('opencode_model'), 'dsh': cfg.get('dsh_model'),
            'codex': cfg.get('codex_model')}.get(cli) or ''


def runtime_effort(cfg, cli):
    return (cfg.get('worker_effort') or '') if cli == 'claude' else ''


def fingerprint(cli, model, effort):
    """The launch fingerprint: (model, effort). Effort only exists for Claude."""
    return (model or '', (effort or '') if cli == 'claude' else '')


def launch_cards(registry, cli):
    """model -> registry card that launches it on this CLI (the static `<cli>-<model>` card first)."""
    cards = {}
    for card in sorted(registry, key=lambda c: (c.id != cli + '-' + (c.model or ''), c.id)):
        if card.cli == cli and card.model and card.model not in cards:
            cards[card.model] = card
    return cards


def build_input(*, kind, goal, instruction, advise_note, slot, models, matrix, catalog):
    """Everything the advisor may see. No worker conversation, no other worker's output."""
    return {'kind': kind, 'goal': goal or '', 'instruction': instruction, 'advise_note': advise_note or '',
            'slot': slot, 'models': models, 'capability_matrix': matrix, 'domains': catalog}


def argv(cfg, run_dir):
    return [cfg.get('claude') or shutil.which('claude') or 'claude', '-p', '--model', MODEL, '--effort', EFFORT,
            '--setting-sources', '', '--strict-mcp-config', '--disable-slash-commands',
            '--settings', json.dumps({'autoMemoryEnabled': False}),
            '--system-prompt-file', str(Path(run_dir) / 'advisor.md'), '--output-format', 'json']


def resident_argv(cfg, run_dir, settings):
    """The resident advisor tab: argv() without -p / --output-format, only the Read tool (handoff notes
    under <run>/handoffs readable without a permission dialog); ``settings`` is the generation's file with
    the fleet Stop hook and autoMemoryEnabled false (one --settings only)."""
    out = argv(cfg, run_dir)
    out.remove('-p')
    i = out.index('--output-format')
    del out[i:i + 2]
    out[out.index('--settings') + 1] = str(settings)
    return out + ['--tools', 'Read', '--add-dir', str(Path(run_dir) / 'handoffs')]


def run_claude(payload, *, cfg, run_dir, timeout=TIMEOUT):
    """One-shot official Claude CLI; cwd fixed to <run>/advisor/ (one transcript directory per run)."""
    run_dir = Path(run_dir)
    prompt = run_dir / 'advisor.md'
    if not prompt.exists():
        prompt.write_text(PROMPT.read_text())
    cwd = run_dir / 'advisor'
    cwd.mkdir(parents=True, exist_ok=True)
    # Never self-update (fleet.CLAUDE_ENV; 2026-10-14 01:36 an isolated config's auto-update broke the install).
    done = subprocess.run(argv(cfg, run_dir), input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                          text=True, timeout=timeout, cwd=str(cwd), env=dict(os.environ, DISABLE_AUTOUPDATER='1'))
    if done.returncode != 0:
        raise AdviceError('advisor exited ' + str(done.returncode) + ': ' + (done.stderr or done.stdout)[-500:])
    try:
        return json.loads(done.stdout)
    except ValueError as exc:
        raise AdviceError('advisor output is not JSON: ' + done.stdout[-300:]) from exc


def sheet_text(envelope):
    """The configuration sheet object inside `--output-format json`'s `result` text."""
    if not isinstance(envelope, dict) or envelope.get('is_error'):
        raise AdviceError('advisor returned an error: ' + json.dumps(envelope, ensure_ascii=False)[:300])
    text = envelope.get('result')
    if not isinstance(text, str) or '{' not in text:
        raise AdviceError('advisor result has no JSON object')
    try:
        sheet = json.loads(text[text.index('{'):text.rindex('}') + 1])
    except ValueError as exc:
        raise AdviceError('advisor sheet is not valid JSON: ' + str(exc)) from exc
    if not isinstance(sheet, dict):
        raise AdviceError('advisor sheet must be a JSON object')
    return sheet


def validate(raw, *, cli, models, current, catalog, whitelist, matrix, cards):
    """Normalized sheet {model, effort, domains, reason, capability?, capability_gap?, effort_ignored?}.

    current = the slot's running fingerprint; cards = model -> registry card for this CLI."""
    model = raw.get('model')
    if model not in models:
        raise AdviceError('model must be one of ' + ', '.join(models) + ' for a ' + cli + ' slot')
    sheet = {'model': model}
    if cli == 'claude':
        effort = raw.get('effort', DEFAULT_EFFORT)
        if effort not in EFFORTS:
            raise AdviceError('effort must be one of ' + ', '.join(EFFORTS))
        sheet['effort'] = effort
    else:
        sheet['effort'] = ''
        if raw.get('effort'):
            sheet['effort_ignored'] = str(raw['effort'])[:20]
    domains = raw.get('domains', [])
    if not isinstance(domains, list) or not all(isinstance(d, str) for d in domains) or len(set(domains)) != len(domains):
        raise AdviceError('domains must be a list of distinct domain names')
    if cli != 'claude' and domains:
        raise AdviceError('a ' + cli + ' slot takes no domains (only Claude slots load domain kits)')
    known = set(catalog)
    allowed = known if whitelist is None else known & set(whitelist)
    unknown = [d for d in domains if d not in allowed]
    if unknown:
        raise AdviceError('domains not in the domain table / this card\'s kit.domains: ' + ', '.join(unknown))
    sheet['domains'] = domains
    reason = raw.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        raise AdviceError('reason must cite the capability matrix')
    sheet['reason'] = ' '.join(reason.split())[:500]
    capability = raw.get('capability')
    if capability is not None:
        domain, _, task = str(capability).partition('/')
        row = (matrix.get(domain) or {}).get(task) if isinstance(matrix.get(domain), dict) else None
        if not isinstance(row, dict):
            raise AdviceError('capability ' + str(capability) + ' is not a row of the capability matrix')
        verdict = (row.get(model) or {}).get('verdict', 'untested')
        if verdict in ('reserve', 'overkill'):  # advisor.md forbids both; enforced here, not only in the prompt
            raise AdviceError('verdict ' + verdict + ' for ' + capability + ' × ' + model)
        if verdict == 'untested':
            tested = [c.get('verdict') for m, c in row.items() if isinstance(c, dict) and c.get('verdict') != 'untested']
            if any(v != 'not_enough' for v in tested):
                raise AdviceError('untested cell ' + capability + ' × ' + model + ' while a tested model is enough')
            sheet['capability_gap'] = True
        sheet['capability'] = capability
    if fingerprint(cli, model, sheet['effort']) != current and model not in cards:
        raise AdviceError('no registry card launches model ' + model + ' on ' + cli)
    return sheet


def decide(current, target, installed, domains):
    """respawn (model / effort changed) | hotload (only new domains) | none. Fewer domains never act."""
    if current != target:
        return 'respawn'
    return 'hotload' if [d for d in domains if d not in installed] else 'none'


def consult(payload, runner, check, attempts=ATTEMPTS):
    """(sheet, envelope, errors). Failures (process, timeout, non-JSON, invalid sheet) retry once."""
    errors = []
    for _ in range(attempts):
        try:
            envelope = runner(payload)
            return check(sheet_text(envelope)), envelope, errors
        except (AdviceError, ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError) as exc:
            errors.append(str(exc)[:500])
    return None, None, errors


def usage_fields(envelope):
    """Advisor token / cost accounting from `--output-format json`."""
    usage = (envelope or {}).get('usage') or {}
    fields = {k: int(usage.get(k) or 0) for k in
              ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')}
    fields['total_cost_usd'] = float((envelope or {}).get('total_cost_usd') or 0)
    return fields


def notice(label, sheet, effect):
    shown = sheet['model'] + ('/' + sheet['effort'] if sheet.get('effort') else '')
    return ('[advisor] ' + label + ' → ' + shown + ', domains: [' + ', '.join(sheet['domains']) + ']（'
            + sheet['reason'] + '；' + effect + '）')


# ------------------------------------------------------------------ mid-task rescue (advisor plan §3.2 / §4.2)
ACTIONS = ('respawn', 'reassign', 'give_up')
RELAY_CAP = 2  # same task, same slot: at most worker-N.1 and worker-N.2 (§3.2-5)


def rescue_actions(relays, candidates):
    """What the advisor may choose: no third relay on one slot, no reassign without an idle slot."""
    return [a for a in ACTIONS if not (a == 'respawn' and relays >= RELAY_CAP)
            and not (a == 'reassign' and not candidates)]


def build_rescue_input(*, goal, task, instruction, finding, want, slot, previous_sheet, relays, actions,
                       candidates, matrix, catalog):
    """Everything a rescue consultation may see: the supervisor's consult item, the task goal and the
    original instruction, slot state, the previous sheet and the matrix. Never a worker conversation."""
    return {'kind': 'rescue', 'goal': goal or '', 'task': task, 'instruction': instruction or '',
            'finding': finding, 'want': want or '', 'slot': slot, 'previous_sheet': previous_sheet,
            'relays': relays, 'relay_cap': RELAY_CAP, 'actions': actions, 'reassign_candidates': candidates,
            'capability_matrix': matrix, 'domains': catalog}


def validate_rescue(raw, *, actions, check_slot, candidates):
    """Normalized rescue sheet {action, to?, model?, effort?, domains?, reason, handoff_hint?}.

    check_slot(name, raw) validates the configuration part against that slot's CLI (same rules as
    the pre-dispatch sheet); candidates = slot names a reassign may go to (first = default)."""
    action = raw.get('action')
    if action not in actions:
        raise AdviceError('action must be one of ' + ', '.join(actions) + ' (relay cap / idle slots apply)')
    reason = raw.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        raise AdviceError('reason must cite the finding or the capability matrix')
    if action == 'give_up':
        return {'action': action, 'reason': ' '.join(reason.split())[:500]}
    target = None
    if action == 'reassign':
        target = raw.get('to', candidates[0])
        if target not in candidates:
            raise AdviceError('reassign to must be one of the idle slots: ' + ', '.join(candidates))
    sheet = dict(check_slot(target, raw), action=action)
    if target:
        sheet['to'] = target
    hint = raw.get('handoff_hint', '')
    if not isinstance(hint, str):
        raise AdviceError('handoff_hint must be text')
    if action == 'respawn' and hint.strip():
        sheet['handoff_hint'] = ' '.join(hint.split())[:500]
    return sheet
