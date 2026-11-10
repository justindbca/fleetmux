"""Peer notices between workers of a contract task (docs/design.md §21).

Two channels (contract spec §4.3): Claude Code workers message each other with Claude Code's own
SendMessage (invisible to the daemon); any pair with a Pi / Codex end goes through the fleet
broker ``peer`` (this module), which validates the notice, appends it to the target worker's
inbox as ``[peer] ...`` and copies the supervisor. Peers exchange facts only.

Everything for the supervisor (``[peer CC]``, ``[peer violation]``, lines of a turn no delivery
asked for) is appended to its next business message, like the ``[Roster change]`` note: a
standalone message would force a decision reply that can count as a bad reply.
"""
import re
import shlex
import time
from pathlib import Path

from cmux_fleet import contracts, runtime_v2, subfleet

HERE = Path(__file__).resolve().parent
NOTICE_MAX = 600
RATE, WINDOW = 20, 3600
NOTES = 'fx:peer:notes'      # supervisor notes waiting for its next business message
SENT = 'fx:peer:sent:'       # + slot: timestamps of accepted broker sends (rate limit)
FIRST_LINE = re.compile(r'^\[iface-notice\] (IF-\S+) (\d+) -> (\d+) \| (new|compatible|breaking|ready|correction)'
                        r' \| (UNAPPROVED|APPROVED|READY)$')
FROM_RE = re.compile(r'\bfrom\s+([^\s;,()]+)')
TRAILER = ('\nA peer notice (facts only; do not reply to the sender). Act on it per CONTRACTS.md; reply in the '
           'AGENTS.md format (Done / Evidence / Not done / Blocked) when done, citing it under Peer notices:.')
ERR_TO = 'peer: --to must be a worker in this fleet tree or consumers:<IF-id>'
ERR_TEXT = 'peer: notice must be <= ' + str(NOTICE_MAX) + ' chars and start with the fixed first line'
ERR_READY = 'peer: only the producer may announce READY'
ERR_RATE = 'peer: rate limit ' + str(RATE) + '/hour reached'


def _fleet():
    from cmux_fleet import fleet
    return fleet


def prefix(cfg):
    """Session-name prefix shared by the whole fleet tree: f<root run token>-."""
    return 'f' + subfleet.token(cfg) + '-'


def command(f, slot):
    """{{FLEET_PEER}} for one worker: the sender is baked in, like {{FLEET_CHROME}}'s config."""
    return shlex.join([f.cfg['python'], str(Path(_fleet().__file__).resolve()), 'peer', '--config', str(f.path),
                       '--from', slot])


def doc(f, role, name):
    """Peer section of CONTRACTS.md: 5-A for Claude Code workers, 5-B for Pi / Codex, a short note for
    the supervisor. Only Claude workers get the prefix and the SendMessage rows."""
    fl = _fleet()
    if role == 'supervisor':
        part = 'contracts_peer_supervisor.md'
    else:
        part = ('contracts_peer_claude.md' if fl.agent_cli(f.runtime_cfg(name), name) == 'claude'
                else 'contracts_peer_broker.md')
    text = (HERE / 'agents' / part).read_text()
    if role != 'supervisor':
        text = text.replace('{{FLEET_PEER_PREFIX}}', prefix(f.cfg)).replace('{{FLEET_PEER}}', command(f, name))
    return '\n' + text


def runtime(f):
    """slot -> (cli, session) for contracts.json consumers_runtime; session only for Claude workers."""
    fl = _fleet()
    out = {}
    for w in f.active_workers():
        cli = fl.agent_cli(f.runtime_cfg(w), w)
        out[w] = (cli, fl.claude_session_name(f.cfg, w) if cli == 'claude' else None)
    return out


def note(f, text):
    f.r.rpush(NOTES, text)


def drain(f):
    """Pending supervisor notes, appended to its next business message (dropped outside contract tasks)."""
    if f.r.llen(NOTES) == 0:
        return ''
    items = f.r.lrange(NOTES, 0, -1)
    f.r.delete(NOTES)
    return '\n' + '\n'.join(items) if f.contract_state() is not None else ''


def _target(f, sender, to):
    """A worker slot of this fleet from a slot name, a label or its Claude session name."""
    fl = _fleet()
    slot = f.resolve(to)
    if slot not in f.active_workers():
        slot = next((w for w in f.active_workers() if fl.claude_session_name(f.cfg, w) == to), None)
    if slot and slot != sender:
        return slot, None
    if not slot and to.startswith(prefix(f.cfg)):
        return None, ('peer: ' + to + ' is in another fleet of this tree; cross-fleet delivery is not implemented: '
                      'say it in your report')
    return None, ERR_TO


def broker(f, ev):
    """The ``peer`` broker verb. Returns the reply dict; every call (also a rejection) is logged."""
    state = f.contract_state() if f.running() else None
    sender, to, iface = ev.get('from'), ev.get('to'), ev.get('iface')
    text = None

    def reject(reason):
        if state is not None:
            f.contract_log('peer.rejected', by=sender, to=to, iface=iface, error=reason, text=text)
        else:
            f.log('peer.rejected', by=sender, to=to, error=reason)
        return {'status': 'rejected', 'reason': reason}

    if state is None:
        return reject('peer: interface contracts are off for this task')
    if not isinstance(sender, str) or sender not in f.active_workers() or f.agent(sender).get('state') == 'dead':
        return reject('peer: --from must be a live worker of this fleet')
    if not all(isinstance(x, str) and x for x in (to, iface, ev.get('text_file'))):
        return reject('peer: usage: --to <worker|consumers:<IF-id>> --iface <IF-id> --text-file <file>')
    own = _fleet().agent_cwd(f.cfg, sender).resolve()
    path = Path(ev['text_file']).resolve()
    if not path.is_relative_to(own) or not path.is_file() or path.stat().st_size > 4 * NOTICE_MAX:
        return reject('peer: --text-file must be a file in your own directory')
    text = path.read_text(errors='replace').strip()
    m = FIRST_LINE.match(text.splitlines()[0]) if text else None
    if (len(text) > NOTICE_MAX or not m or m.group(1) != iface
            or _fleet().SUPERVISOR_INSTRUCTION_HEAD.search(text)):
        return reject(ERR_TEXT)
    if to.startswith('consumers:'):
        e = contracts.current(state, to[len('consumers:'):])
        if e is None:
            return reject('peer: ' + to[len('consumers:'):] + ' is not registered')
        slots = [s for s in dict.fromkeys(state['tasks'].get(c, {}).get('to') for c in e['consumers'])
                 if s and s != sender and s in f.active_workers()]
        if not slots:
            return reject('peer: no consumer of ' + e['id'] + ' has a worker yet')
    else:
        slot, error = _target(f, sender, to)
        if error:
            return reject(error)
        slots = [slot]
    cur = contracts.current(state, iface)
    if cur is None and m.group(2, 3, 4, 5) != ('0', '1', 'new', 'UNAPPROVED'):
        return reject('peer: ' + iface + ' is not registered')
    if m.group(5) == 'READY' and state['tasks'].get(cur['producer'], {}).get('to') != sender:
        return reject(ERR_READY)
    now = time.time()
    recent = [t for t in f.r.lrange(SENT + sender, 0, -1) if now - float(t) < WINDOW]
    if len(recent) >= RATE:
        return reject(ERR_RATE)
    f.r.rpush(SENT + sender, str(now))
    f.r.ltrim(SENT + sender, -RATE, -1)
    for slot in slots:
        f.contract_queue(state, slot, '[peer] ' + text + TRAILER)
    f.save_contract(state)
    labels = [f.label(s) for s in slots]
    note(f, '[peer CC] ' + f.label(sender) + ' -> ' + ', '.join(labels) + ':\n' + text)
    f.contract_log('peer', by=sender, to=slots, iface=iface, change=m.group(4), notice=m.group(5), text=text)
    return {'status': 'ok', 'delivered': labels}


def violations(f, slot, line):
    """``[peer violation]`` notes for notices a report says came from outside this fleet tree."""
    known = set(f.r.hgetall(_fleet().NAMES)) | set(f.workers)
    bad = [x for x in (m.group(1).rstrip('.:') for m in FROM_RE.finditer(str(line or '')))
           if not x.startswith(prefix(f.cfg)) and x not in known]
    if not bad:
        return []
    f.contract_log('peer.violation', by=slot, senders=bad, line=str(line))
    return ['[peer violation] ' + f.label(slot) + ' received a peer notice from ' + ', '.join(bad)
            + ' (outside ' + prefix(f.cfg) + '*); only this fleet tree may send peer notices: ' + str(line)]


def unsolicited(f, slot, text):
    """A worker turn that answers no delivery (a peer SendMessage woke it, or the operator typed).
    Never a completion, a bounce or another task's report; its Peer notices / question lines
    are logged and appended to the supervisor's next business message."""
    last = f.r.lrange('fx:replies:' + slot, -1, -1)
    value = runtime_v2.tui_candidates(slot, 'worker', text)
    if last and last[0] in (text[-4000:], runtime_v2.dumps(value[0]['value'])[-4000:] if value else None):
        f.log('turn.duplicate', agent=slot)  # the settled report's Stop fired again
        return
    f.log('turn.unsolicited', agent=slot)
    state = f.contract_state() if f.running() else None
    if state is None:
        return
    fields = contracts.report_fields(text)
    line = str(fields.get('peer_notices', '')).strip()
    question = str(fields.get('question', '')).strip()
    line = '' if line.lower() == 'none' else line
    question = '' if question.lower() == 'none' else question
    f.contract_log('unsolicited', by=slot, peer_notices=line or None, question=question or None)
    if line:
        note(f, '[unsolicited ' + f.label(slot) + ' turn, not a report] Peer notices: ' + line)
        for v in violations(f, slot, line):
            note(f, v)
    if question:
        note(f, '[worker question] ' + f.label(slot) + ' (unsolicited turn): ' + question)
