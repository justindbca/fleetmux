"""Claude Code input box: read-only recognition, and the refusal every backend's send goes through.

Until 2026-11-09 this module pasted fleet text into an idle, empty Claude editor (§33.7). The user then
ruled「任何场景都不用 claude 输入框打字」(docs/design.md §48): ``send`` now refuses for every Claude pane;
``visible_editor`` stays for read-only screen checks.
"""
from dataclasses import dataclass
import re
import unicodedata


SGR = re.compile(r'\x1b\[[0-9;]*m')
RULE = re.compile(r'[─━-]{8,}(?: \S+ [─━-]+)?')
BUSY = re.compile(r'esc to (?:interrupt|cancel)|running.{0,30}hooks?|'
                  r'^\s*[✢✳✶✻✽✼✺∗·*]\s.*[….]|\d+ background tasks?', re.I | re.M)


@dataclass(frozen=True)
class EditorView:
    text: str
    idle: bool
    width: int
    exact: bool


def required(manager, pane):
    """Fleet records cli_type at spawn, including adopted panes and raw /clear."""
    return manager._meta().get('surfaces', {}).get(str(pane), {}).get('cli_type') == 'claude'


def visible_editor(screen):
    """Recognize the bottom ruled Claude prompt; None means unobservable, not empty.

    Preserve all body rows for whitespace-insensitive comparison by send().
    Strip only terminal SGR and right-hand cell padding from the body.
    """
    rows = SGR.sub('', screen or '').rstrip('\n').split('\n')
    rules = [i for i, row in enumerate(rows) if RULE.fullmatch(row.rstrip(' '))]
    if len(rules) < 2:
        return None
    top, bottom = rules[-2:]
    body = rows[top + 1:bottom]
    footer = [r for r in rows[bottom + 1:] if r.strip()]
    footer_rows = [re.split(r'\s{2,}', r.strip(), maxsplit=1)[0] for r in footer]
    if not body or not footer_rows:
        return None
    first = body[0].rstrip(' ')
    if first == '❯':
        value = ''
    elif first.startswith(('❯ ', '❯\xa0')):
        value = first[2:]
    else:
        return None
    width = len(rows[bottom].rstrip(' '))
    for row in body[1:]:
        if not row.startswith('  '):
            return None
        value += '\n' + row[2:].rstrip(' ')
    if '\x1b' in value:
        return None
    # Slash commands replace the normal footer with a menu and wrapped descriptions.
    menu = (value.startswith('/') and re.match(r'^/\S+\s{2,}', footer[0].lstrip())
            and all(re.match(r'^/\S+\s{2,}', r.lstrip())
                    or (r.startswith(' ' * (len(footer[0]) - len(footer[0].lstrip()) + 2))
                        and not r.lstrip().startswith('/')) for r in footer))
    # A shell, clipped screen or historical prompt followed by new output is not
    # the current editor. Right-aligned footer status is not part of the whitelist.
    if not menu:
        if len(footer_rows) > 4 or not all(re.fullmatch(
                r'(?:\? for shortcuts|⏵⏵ bypass permissions on(?: \(shift\+tab to cycle\))?'
                r'(?: · esc to (?:interrupt|cancel))?|esc to (?:interrupt|cancel)|'
                r'UserPromptSubmit operation blocked by hook[:…]?.*|Fleet[:：].*)'
                r'(?: · ← (?:\d+ agents?|for agents))?', r) for r in footer_rows):
            return None
        if not any(r.startswith('? for shortcuts') or 'bypass permissions on' in r
                   or r.startswith(('esc to interrupt', 'esc to cancel')) for r in footer_rows):
            return None
    exact = not (len(body) != 1 or '[Pasted text' in value or '[Image #' in value or '…' in value
                 or any(display_width(row.rstrip(' ')) >= width - 1 for row in body))
    surrounding = '\n'.join(rows[max(0, top - 8):top] + footer)
    return EditorView(value, not bool(BUSY.search(surrounding)), width, exact)


def display_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'WF' else 1
               for c in text)


def send(manager, pane, text, paste):
    """Refuse: fleet never types into a Claude Code input box (user 2026-11-09, docs/design.md §48).

    Every backend routes a Claude pane here (``required``). Claude agents take deliveries through their
    Stop-hook mailbox and their first prompt as a CLI argument; configs that needed the input box are
    refused at start (runtime_v2.typed_claude_problem). This is the last line: ``paste`` is never called,
    no key is sent, the caller sees False with the reason in ``last_error``.
    """
    manager.last_error = ('Claude input held: fleet never types into a Claude Code input box '
                          '(docs/design.md §48); nothing pasted, no key sent')
    return False
