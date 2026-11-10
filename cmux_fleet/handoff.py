"""Handoff notes: the contract between a retiring agent session and its successor.

A handoff note has eight ``## `` sections. The model is only asked to fill three of them
("Done", "In progress", "Next steps") into a small blank form; the daemon owns the full
structure and merges the model's text in. Weak models cannot edit an existing structured
file in place without duplicating or deleting headings, so they never see the full note.

Also here: ``read_usage`` (context usage from a Codex rollout file) and the prompt that
asks a session to write its handoff.
"""
from __future__ import annotations

import json
import re
import time
from typing import Dict, List, Optional, Tuple

SECTIONS = [
    ('Goal', 'Goal (user wording)'),
    ('Acceptance criteria', 'Acceptance criteria'),
    ('Done', 'Done (facts)'),
    ('In progress', 'In progress'),
    ('Next steps', 'Next steps (in order)'),
    ('Needs a human decision', 'Needs a human decision'),
    ('Key files', 'Key files / commands'),
    ('Risks', 'Risks / reservations'),
]
LITE_KEYS = ['Done', 'In progress', 'Next steps']  # the model writes only these three
NEXT_KEY = 'Next steps'
NEXT_PLACEHOLDER = '(model did not fill this in: derive the next step from Goal + Done first)'

DUMP_PROMPT = (
    '[SYSTEM HANDOFF] Hard instruction, no questions, no negotiation. Context is almost full '
    '({used}/{window}). Stop what you are doing and edit this file: {path} . It contains only '
    'three `## ` headings; write your content UNDER each heading (keep the heading lines exactly '
    'as they are, do not delete, repeat or rename them), at most 10 lines per section: '
    '(1) `## Done (facts)`: what is finished, one item per line, with file paths or commands. '
    '(2) `## In progress`: the one thing you were in the middle of; write "none" if nothing. '
    '(3) `## Next steps (in order)`: what to do next, one step per line, with commands and file '
    'paths, so that a fresh session that knows nothing can follow it. '
    'When done reply exactly "handoff written" and stop.'
)


# Mid-task rescue (advisor plan §3.2): same three sections, but the reason is a new configuration.
RESCUE_PROMPT = DUMP_PROMPT.replace(
    'Context is almost full ({used}/{window}). ',
    'The supervisor is handing your current task to a successor session with a new configuration. ')

# Named sub-fleet return (design §29): the session that supervised sub-fleet <name> hands over to the
# slot's next generation. The caller replaces <name>; {path} / {used} / {window} are formatted as above.
SUBFLEET_RETURN_PROMPT = DUMP_PROMPT.replace(
    'Context is almost full ({used}/{window}). ',
    'Sub-fleet <name> is down and your role as its supervisor is over; a fresh session of this slot takes '
    'over as the main fleet\'s worker. Write what it needs to know about that sub-fleet. ')


# Long task split (design §44): the supervisor approved splitting <task> with a model of its choice; the
# slot's next generation (on that model) becomes the sub-fleet's supervisor. The caller replaces <task>.
SPLIT_PROMPT = DUMP_PROMPT.replace(
    'Context is almost full ({used}/{window}). ',
    'The supervisor approved splitting <task>: a new session of this slot (on the model the supervisor picked, or '
    'the same model when none was picked) takes over and becomes the supervisor of sub-fleet <task>. Write what it '
    'needs: what is done, the approved split and what remains of each piece. ')


def read_usage(path: str, window_fallback: int) -> Tuple[Optional[int], int]:
    """Latest rollout context usage in a bounded 64KB tail; unknown is never zero."""
    from cmux_fleet.midturn import codex_rollout_usage
    return codex_rollout_usage(path, window_fallback)


def midturn_progress(message_id, instruction):
    """The machine fallback must identify the unfinished instruction, not claim idle."""
    return ('Mid-turn continuation of ' + message_id + ': ' + ' '.join(instruction.split())[:1000]
            + '. Re-check the interrupted step and any partial artifacts before continuing.')


def _split_sections(text: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    head: Optional[str] = None
    body: List[str] = []
    for line in text.splitlines():
        if line.startswith('## '):
            if head is not None:
                out.append((head, '\n'.join(body)))
            head, body = line[3:].strip(), []
        elif head is not None:
            body.append(line)
    if head is not None:
        out.append((head, '\n'.join(body)))
    return out


def _body_is_empty(body: str) -> bool:
    real = []
    for ln in body.strip().splitlines():
        s = ln.strip().lstrip('-*0123456789. ').strip()
        if not s or (s.startswith('<') and s.endswith('>')) or 'model did not fill' in s:
            continue
        real.append(s)
    return not real


def _sanitize(s: str) -> str:
    """Strip leading '## ' from free text so it cannot break the section structure."""
    return re.sub(r'(?m)^##\s', '', (s or '').strip())


def partial_template() -> str:
    """The three-section blank form the model fills in."""
    titles = dict(SECTIONS)
    lines: List[str] = []
    for key in LITE_KEYS:
        lines += ['## ' + titles[key], '']
    return '\n'.join(lines).rstrip() + '\n'


def merge_partial(base_text: str, partial_text: str) -> str:
    """Merge the model's three sections into the machine skeleton.

    Headings come from the skeleton only; the model only supplies bodies. Sections the
    model skipped or mangled keep the skeleton text. Better to hand the successor a
    skeleton than to pass "sure, writing it now" along as the next step.
    """
    filled: Dict[str, str] = {}
    for head, body in _split_sections(partial_text):
        for key in LITE_KEYS:
            if key in head and not _body_is_empty(body):
                filled[key] = body.strip()
    if not filled:
        return base_text
    out: List[str] = []
    skip = False
    for line in base_text.splitlines():
        if line.startswith('## '):
            key = next((k for k in LITE_KEYS if k in line[3:]), None)
            out.append(line)
            skip = bool(key and key in filled)
            if skip:
                out.append(filled[key])
            continue
        if not skip:
            out.append(line)
    return '\n'.join(out).rstrip() + '\n'


def validate(path: str) -> Tuple[bool, List[str]]:
    """All eight sections present and 'Next steps' non-empty."""
    problems: List[str] = []
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        return False, ['cannot read handoff: %s' % e]
    sections = _split_sections(text)
    heads = [h for h, _ in sections]
    for key, title in SECTIONS:
        if not any(key in h for h in heads):
            problems.append('missing section: ## ' + title)
    next_body = next((b for h, b in sections if NEXT_KEY in h), '')
    if _body_is_empty(next_body):
        problems.append("'Next steps' is empty or a placeholder")
    return (not problems), problems


def build_skeleton(handoff_id: str, runtime: str, goal: str, done_lines: Optional[List[str]] = None,
                   inprogress: str = '', next_lines: Optional[List[str]] = None, pending: str = 'none',
                   key_files: Optional[List[str]] = None, risks: Optional[List[str]] = None,
                   task: str = '', hop: Optional[int] = None) -> str:
    """Machine-written fallback note; the model's three sections are merged on top if usable."""
    when = time.strftime('%Y-%m-%d %H:%M')
    head = ['# Handoff ' + handoff_id]
    if hop:
        head.append('hop:%d' % hop)
    head += [when, 'runtime:' + runtime + ' (machine skeleton)']
    if task:
        head.append('task:' + task.strip())
    parts = ['  '.join(head)]

    def sect(title: str, body: str) -> None:
        parts.append('## ' + title)
        parts.append(body.rstrip() if body.strip() else '')

    sect('Goal (user wording)', _sanitize(goal) or '(no goal captured)')
    sect('Acceptance criteria', '(not set by the skeleton; derive from the goal)')
    sect('Done (facts)', '\n'.join('- ' + _sanitize(d) for d in (done_lines or [])) or '(no record)')
    sect('In progress', _sanitize(inprogress) or '(none)')
    sect('Next steps (in order)',
         '\n'.join('%d. %s' % (i + 1, _sanitize(n)) for i, n in enumerate(next_lines or [])) or NEXT_PLACEHOLDER)
    sect('Needs a human decision', _sanitize(pending) or 'none')
    sect('Key files / commands', '\n'.join('- ' + _sanitize(k) for k in (key_files or [])) or '(none)')
    sect('Risks / reservations', '\n'.join('- ' + _sanitize(r) for r in (risks or []))
         or '- machine skeleton; the model did not fill its sections')
    return '\n'.join(parts).rstrip() + '\n'


def add_next_step(text: str, line: str) -> str:
    """Append one step to the end of 'Next steps' (after the model's merge, so it is never dropped)."""
    out: List[str] = []
    inside = False
    for ln in text.splitlines():
        if ln.startswith('## '):
            if inside:
                out.append('- ' + _sanitize(line))
            inside = NEXT_KEY in ln[3:]
        out.append(ln)
    if inside:
        out.append('- ' + _sanitize(line))
    return '\n'.join(out).rstrip() + '\n'
