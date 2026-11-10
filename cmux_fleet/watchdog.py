"""Watchdog for transport v2 (docs/design.md §24): one of five states per slot, per check.

  idle       no delivery in flight and not starting: never acted on, however long (E1).
  working    a delivery is in flight and something moved: the transcript / rollout, the screen, or the
             CPU time of the pane's process tree, within stall_window; and the transcript within
             stall_timeout (the hard limit: a spinning screen does not hide a dead stream for ever).
  prompting  the screen shows a confirmation dialog. Whitelisted (prompt table) and verified -> save
             the screen and press its keys; anything else -> save the screen, mark the slot
             prompt_wait, pause dispatch, press nothing (the lease stays received: once a human
             answers in the pane the turn settles normally). Deletes / moves are never pressed.
  stalled    in flight, and either all three signals still for stall_window or the transcript still for
             stall_timeout: Escape, at most stall_escapes times spread over stall_grace. Transcript
             activity after that -> working again (the screen does not count there: Escape changes it).
  frozen     stalled for stall_grace, or every Escape left the screen unchanged, or `ps` shows
             the pane's processes in uninterruptible wait on two checks in a row: save the
             screen, kill this fleet's own pane (its process tree), confirm the delivery
             stopped, and start the next generation in place from a skeleton handoff note.
             The cancelled work is never re-run automatically; the supervisor decides.

Screen capture, keys, kills and spawns are side effects: they run outside any Redis intent.
Only settlements (hold, queue, publish) go through RuntimeV2.transaction.
"""
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from cmux_fleet import advisor, fleet_config

STATES = ('idle', 'working', 'stalled', 'frozen', 'prompting')
DEFAULT_TABLE = None  # None = the watchdog_prompts block of fleet.yaml; a path = that file (blocks or a bare table)
KEYS = frozenset(('Enter', 'Escape', 'Tab', 'Up', 'Down', 'Left', 'Right', 'Space',
                  'y', 'n', '1', '2', '3'))
# How an answered dialog's concrete operation is proven to be inside an authorisation the run already has.
VERIFY = {
    'own_cwd': 'every path on the dialog is the slot\'s own working directory (trusted at spawn)',
    'paths_in_run': 'every path on the dialog is inside the slot directory or the run workspace (--add-dir)',
}
DEFAULTS = {'stall_grace': 300, 'stall_escapes': 3, 'watch_interval': 5, 'capture_fail_alarm': 3,
            'dialog_escalation_timeout': 600,  # seconds the supervisor has to rule on a dialog it received
            # §24 stall window: transcript, screen and process-tree CPU all still this long -> stalled (0 = off).
            'stall_window': 300,
            # CPU the process tree must gain within one window to count as moving, in % of one core. Not 0: an
            # idle Claude TUI (text pasted, Enter never pressed) still burns 0.6-1.5 % (measured 2026-10-14).
            'stall_cpu_pct': 5}
# The supervisor cannot rule on a worker's dialog now (plan F' 8.3 step 5): the user gets it.
SUPERVISOR_AWAY = ('held', 'dead', 'starting', 'retired', 'delegated', 'handoff', 'clearing', 'walled', 'reclaimed')
YES_CURSOR = re.compile(r'❯\s*(?:1\.\s*)?yes\b')
# Slot states the watchdog never touches: someone else owns the next step.
HANDS_OFF = ('held', 'dead', 'retired', 'delegated', 'handoff', 'clearing')
# §24 Stop deferred by background tasks: Claude Code writes the hook result to the transcript ~1 s after the Stop
# hook returns, so a move within this many seconds of the mark is that write, not the task waking the turn.
BG_WAIT_GRACE = 30
# §35: a slot whose bridge link is lost is restarted at most this many times per window; then held + paused.
LINK_MAX_RESTARTS, LINK_WINDOW_S = 3, 1800


def setting(cfg, name):
    value = cfg.get(name, DEFAULTS[name])
    if type(value) not in (int, float) or value < 0:
        raise ValueError(name + ' must be a non-negative number')
    return value


# ---------------------------------------------------------------- delete classifier (plan F' 8.1)
# A dialog that may lose data is the user's call, always: never answered by the program or the supervisor.
# Strict on purpose: a non-delete dialog misread as a delete only asks the user once more; the reverse is
# not allowed. So this list only grows (fleet.yaml#watchdog_prompts `delete_markers:` adds, nothing removes), and
# a move / rename (mv) counts as a delete (user, 2026-10-14).
DELETE_MARKERS = [
    'rm', 'rmdir', 'unlink', 'del', 'delete', 'remove', 'rimraf', 'shred', 'trash', 'purge', 'wipe', 'erase',
    'truncate', 'drop table', 'drop database', 'drop schema', 'git clean', 'git reset --hard', 'git push --force',
    'git push -f', 'git branch -d', 'git checkout --', 'git restore', 'dd if=', '> /dev/null', '>/dev/null',
    'mv', 'move', 'rename', 'move-item', 'rename-item', 'remove-item',
    '删除', '清空', '移除', '清理', '移动', '改名', '重命名',
]


def _marker_re(phrase):
    """Case-insensitive, word-bounded at ASCII word edges (CJK phrases match as substrings); a single English
    word also matches its inflections (delete -> deleted / deleting, rm -> rms)."""
    word = phrase.lower()
    if re.fullmatch(r'[a-z]+', word):
        body = (re.escape(word[:-1]) + '(?:e|es|ed|ing)' if word.endswith('e') and len(word) > 2
                else re.escape(word) + '(?:s|es|ed|ing|d)?')
    else:
        body = re.escape(word)
    head = r'(?<![a-z0-9_])' if re.match(r'[a-z0-9_]', word) else ''
    tail = r'(?![a-z0-9_])' if re.search(r'[a-z0-9_]$', word) else ''
    return re.compile(head + body + tail)


def dialog_class(table, prompt, screen):
    """'delete' | 'other' for the dialog at the bottom of `screen`; None when the screen is empty (unreadable:
    the caller hands it to the user). Any delete word anywhere in the last tail_lines rows makes it a delete."""
    if (prompt or {}).get('name') == 'dangerous_rm' or (prompt or {}).get('verify') == 'rm_in_cwd':
        return 'delete'
    text = screen_tail(screen, table['tail_lines']).lower()
    if not text.strip():
        return None
    patterns = table.get('delete_markers') or [_marker_re(x) for x in DELETE_MARKERS]
    return 'delete' if any(p.search(text) for p in patterns) else 'other'


CLASS_TITLES = {'delete': '删除确认', 'send_out': '外发确认', 'config': '改配置确认', 'install': '安装确认',
                'secret_read': '读密钥确认', 'real_money': '真实资金操作确认'}


def classify_dialog(table, prompt, screen, policy):
    """P8j: dialog_class widened to the action classes under a policy (U3 order, U7 routing). None when the screen
    is unreadable; 'other' when no class whose tier is not 'off' matches; no policy -> dialog_class unchanged.
    The delete words of dialog_class plus guard's text rules on each line of the screen tail."""
    base = dialog_class(table, prompt, screen)
    if base is None or policy is None:
        return base
    from cmux_fleet import guard
    found = {'delete'} if base == 'delete' else set()
    for line in screen_tail(screen, table['tail_lines']).splitlines():
        line = line.strip().lstrip('❯>$#').strip()
        if line:
            found |= guard.hits('Bash', {'command': line}, policy, {})
    got = guard.choose(found, policy)
    return got[0] if got else 'other'


# ---------------------------------------------------------------- prompt table
def parse_table(raw):
    """Validate a prompt table (YAML text or an already-loaded dict). Raises ValueError."""
    if isinstance(raw, (str, bytes)):
        import yaml
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ValueError('prompt table is not valid YAML: ' + str(exc).splitlines()[0])
    else:
        data = raw
    if not isinstance(data, dict):
        raise ValueError('prompt table must be a mapping')
    unknown = set(data) - {'tail_lines', 'max_hits', 'prompts', 'unknown_markers', 'delete_markers', 'user_approve'}
    if unknown:
        raise ValueError('unknown prompt table keys: ' + ', '.join(sorted(unknown)))
    tail, hits = data.get('tail_lines', 25), data.get('max_hits', 3)
    if type(tail) is not int or not 1 <= tail <= 500:
        raise ValueError('tail_lines must be an integer 1-500')
    if type(hits) is not int or not 1 <= hits <= 20:
        raise ValueError('max_hits must be an integer 1-20')
    prompts, names = [], set()
    for entry in data.get('prompts') or []:
        if not isinstance(entry, dict) or set(entry) - {'name', 'match', 'any', 'keys', 'verify'}:
            raise ValueError('a prompt entry has exactly name / match / keys / verify (and optional any)')
        name = entry.get('name')
        if not isinstance(name, str) or not name or name in names:
            raise ValueError('prompt names must be unique non-empty strings')
        names.add(name)
        for field in ('match', 'any'):
            value = entry.get(field, [] if field == 'any' else None)
            if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
                raise ValueError(name + ': ' + field + ' must be a list of non-empty strings')
        if not entry['match']:
            raise ValueError(name + ': match must not be empty')
        keys = entry.get('keys')
        if not isinstance(keys, list) or not keys or not all(k in KEYS for k in keys):
            raise ValueError(name + ': keys must be a non-empty list of ' + ', '.join(sorted(KEYS)))
        if entry.get('verify') not in VERIFY:
            # Matching text is not an authorisation: every answered dialog names how its operation is checked.
            raise ValueError(name + ': verify must be one of ' + ', '.join(sorted(VERIFY)))
        prompts.append({'name': name, 'match': [x.lower() for x in entry['match']],
                        'any': [x.lower() for x in entry.get('any', [])], 'keys': list(keys),
                        'verify': entry['verify']})
    markers = data.get('unknown_markers') or []
    if not isinstance(markers, list) or not all(isinstance(x, str) and x.strip() for x in markers):
        raise ValueError('unknown_markers must be a list of non-empty strings')
    extra = data.get('delete_markers') or []
    if not isinstance(extra, list) or not all(isinstance(x, str) and x.strip() for x in extra):
        raise ValueError('delete_markers must be a list of non-empty strings')
    # The built-in delete words are always in force: the table can only add to them, never remove one.
    return {'tail_lines': tail, 'max_hits': hits, 'prompts': prompts,
            'unknown_markers': [x.lower() for x in markers],
            'delete_markers': [_marker_re(x) for x in DELETE_MARKERS + [x.strip() for x in extra]],
            'user_approve': parse_user_approve(data.get('user_approve'))}


def parse_user_approve(entries):
    """The optional ``user_approve`` section (P6c; contracts §8 Q1): ``[{name, match: [...], keys: [...]}]``, the
    operator's explicit registration of the keys that approve a delete dialog from the phone. Only the user route
    reads it (the pending record's ``keys_approve``); the program route (``check``) and the supervisor route
    (``answer_dialog``) never do. Default empty."""
    out, names = [], set()
    if entries is None:
        return out
    if not isinstance(entries, list):
        raise ValueError('user_approve must be a list of {name, match, keys} entries')
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'name', 'match', 'keys'}:
            raise ValueError('a user_approve entry has exactly name / match / keys')
        name = entry['name']
        if not isinstance(name, str) or not name or name in names:
            raise ValueError('user_approve names must be unique non-empty strings')
        names.add(name)
        match = entry['match']
        if not isinstance(match, list) or not match or not all(isinstance(x, str) and x.strip() for x in match):
            raise ValueError('user_approve ' + name + ': match must be a non-empty list of non-empty strings')
        keys = entry['keys']
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and k in KEYS for k in keys):
            raise ValueError('user_approve ' + name + ': keys must be a non-empty list of ' + ', '.join(sorted(KEYS)))
        out.append({'name': name, 'match': [x.lower() for x in match], 'keys': list(keys)})
    return out


def user_approve_keys(table, tail):
    """The keys of the first ``user_approve`` entry whose every ``match`` string is on ``tail``; [] when none is
    (the card then offers reject only). Never inferred from the screen (no YES_CURSOR, no default Enter)."""
    text = (tail or '').lower()
    for entry in table.get('user_approve') or []:
        if all(m in text for m in entry['match']):
            return list(entry['keys'])
    return []


class TableCache:
    """Hot reload by (mtime, size). A bad edit keeps the previous table and is audited once."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.stamp = None
        self.table = None

    def get(self, audit):
        try:
            stamp = fleet_config.stamp(self.path)
        except OSError as exc:
            stamp = ('missing', str(exc))
        if stamp == self.stamp and self.table is not None:
            return self.table
        self.stamp = stamp
        try:
            table = parse_table(fleet_config.block('watchdog_prompts', self.path))
        except (OSError, ValueError) as exc:
            audit('watch.table_rejected', level='error', error=str(exc)[:500], path=fleet_config.source(self.path))
            if self.table is None:
                self.table = parse_table({})  # nothing whitelisted: every dialog is held; no user_approve key
            return self.table
        if self.path is None:
            table['user_approve'] = []  # contracts §8 Q1: registered only in an explicit --prompt-table file
        if self.table is not None:
            audit('watch.table_reloaded', level='info', path=fleet_config.source(self.path),
                  prompts=[p['name'] for p in table['prompts']])
        self.table = table
        return table


def screen_tail(screen, lines):
    rows = (screen or '').rstrip().splitlines()
    return '\n'.join(rows[-lines:])


# §24 2026-11-09: marker text alone is not a dialog (an agent's own report naming "Dangerous rm" paused a fleet).
# A Claude Code / pi dialog takes the place of the CLI's input box (pi: interactive-mode.js showTopExtensionDialog
# swaps the editor out; Claude: every saved dialog screen), so a screen whose bottom is still that input box has
# no dialog on it, whatever the text above says. Other CLIs (codex / opencode / dsh: no saved screens) keep the
# plain text match.
INPUT_BOX_CLIS = ('claude', 'pi')
BOX_RULE_RE = re.compile(r'^\s*─{2,}(?:.*─)?\s*$')  # a rule, maybe labelled: "──── f91472563-approve-1 ─", "── ⠋ Working ──"
# A numbered / arrow cursor or a key hint: dialog furniture (Claude "❯ 1. Yes" / "Esc to cancel", pi "→ Yes" / "↑↓ navigate").
DIALOG_ROW_RE = re.compile(r'^\s*(?:[❯›>→]\s*\d+\.|→\s)|esc to cancel|enter to confirm|esc cancel|↑↓')
BOX_ROWS, FOOTER_ROWS = 12, 8


def input_box(rows, cli):
    """True when the bottom rows are the CLI's own input box: two rules at most BOX_ROWS apart, at most FOOTER_ROWS
    non-empty rows (status lines) below, no dialog furniture from the box down (a pi dialog is boxed by two rules
    too), and for Claude the box opens with its "❯" prompt. Anything else: False (the text match stands)."""
    rules = [i for i, row in enumerate(rows) if row.count('─') >= 10 and BOX_RULE_RE.match(row)]
    if len(rules) < 2:
        return False
    top, bottom = rules[-2], rules[-1]
    inside = [row.strip() for row in rows[top + 1:bottom] if row.strip()]
    if (not 1 <= bottom - top - 1 <= BOX_ROWS or sum(1 for row in rows[bottom + 1:] if row.strip()) > FOOTER_ROWS
            or any(DIALOG_ROW_RE.search(row) for row in rows[top:])):
        return False
    return cli == 'pi' or bool(inside) and inside[0].startswith('❯')


def match_prompt(table, screen, cli=None):
    """(whitelisted entry or None, unknown marker or None) for the bottom of the screen. `cli` (the slot's CLI):
    for claude / pi, a screen that ends in that CLI's input box shows no dialog (§24 2026-11-09)."""
    text = screen_tail(screen, table['tail_lines']).lower()
    if not text or cli in INPUT_BOX_CLIS and input_box(text.splitlines(), cli):
        return None, None
    for entry in table['prompts']:
        if all(m in text for m in entry['match']) and (not entry['any'] or any(a in text for a in entry['any'])):
            return entry, None
    for marker in table['unknown_markers']:
        if marker in text:
            return None, marker
    return None, None


PATH_RE = re.compile(r'(?<![\w$.~/-])(~?/[^\s"\'`<>|;&()]+)')


def _real(path):
    return Path(os.path.realpath(os.path.expanduser(path.rstrip('?.,:!'))))


def _inside(path, root, strict=False):
    return path == root and not strict or root in path.parents


def unwrap_paths(text):
    """Re-join a long path the TUI wrapped over several lines (Claude 2.1.280 wraps at its box width).

    A run of consecutive lines that are each one space-free token, the first starting with '/' or '~/',
    is one path. Joining two separate paths by mistake yields a path that is in no grant: held, safe.
    """
    out, block = [], []
    for row in text.splitlines() + ['']:
        token = row.strip()
        if token and ' ' not in token and (block or token.startswith(('/', '~/'))):
            block.append(token)
            continue
        if block:
            out.append(''.join(block))
            block = []
        out.append(row)
    return '\n'.join(out[:-1])


RULE_RE = re.compile(r'^\s*─{10,}\s*$')


def dialog_box(text):
    """The rows below the last full-width rule: the Claude dialog's own box, without the shell lines above it.

    S1a 2026-10-13: the pane's `exec /bin/bash <run>/prompts/<slot>.sh` line sat above the trust box and was
    read as a dialog path, so a trust dialog for the slot's own directory was held. No rule: the whole tail.
    A rule below the dialog's path leaves no path to read: held, safe.
    """
    rows = text.splitlines()
    rules = [i for i, row in enumerate(rows) if RULE_RE.match(row)]
    return '\n'.join(rows[rules[-1] + 1:]) if rules else text


def authorize(entry, screen, lines, cwd, workspace):
    """(True, why) only when the dialog's concrete operation is inside an existing grant; else (False, why).

    A dialog whose scope cannot be read off the screen is never authorised: the caller holds it.
    """
    text = screen_tail(screen, lines)
    cwd, workspace = _real(str(cwd)), _real(str(workspace))
    kind = entry.get('verify')
    if kind in ('own_cwd', 'paths_in_run'):
        # own_cwd (trust dialogs) reads only the dialog box; paths_in_run keeps the whole tail (layout not captured).
        scope = dialog_box(text) if kind == 'own_cwd' else text
        paths = [_real(m) for m in PATH_RE.findall(unwrap_paths(scope))]
        if not paths:
            return False, 'no path on the dialog: its scope cannot be checked'
        roots = (cwd,) if kind == 'own_cwd' else (cwd, workspace)
        bad = [str(p) for p in paths if not any((p == r) if kind == 'own_cwd' else _inside(p, r) for r in roots)]
        if bad:
            return False, 'path outside the granted area: ' + ', '.join(bad[:3])
        return True, ', '.join(sorted({str(p) for p in paths}))[:300]
    return False, 'no verification rule'


def uninterruptible(stats):
    """macOS `ps` STAT 'U' (Linux 'D'): a process stuck in the kernel (E5: autofs read)."""
    return any(s[:1] in ('U', 'D') for s in stats or ())


def classify(*, active, in_flight, age, stall_timeout, prev, since, now, grace, escapes, max_escapes,
             screen_same, proc_frozen, prompt=None, unknown=None, prompt_hits=0, max_hits=3, moved=False,
             quiet=0, stall_window=0):
    """Pure state decision. `since` = when the current stall began; `moved` = transcript activity after it.

    `age` = seconds since the transcript / rollout last moved (the stall_timeout hard limit); `quiet` = seconds
    the transcript, the screen and the process-tree CPU have all been still (the stall_window rule).
    """
    if not active:
        return 'idle'
    if in_flight and proc_frozen >= 2:
        return 'frozen'
    if prompt is not None and prompt_hits < max_hits:
        return 'prompting'
    if unknown is not None:
        return 'prompting'
    if not in_flight:
        return 'working'  # starting without a delivery: the READY deadline governs it
    stuck_dialog = prompt is not None  # answered max_hits times and still there
    if prev == 'stalled' and not stuck_dialog:
        if moved:
            return 'working'
        if now - since >= grace or (escapes >= max_escapes and screen_same):
            return 'frozen'
        return 'stalled'
    if (stuck_dialog or (stall_timeout > 0 and age >= stall_timeout)
            or (stall_window > 0 and quiet >= stall_window)):
        if prev == 'stalled' and (now - since >= grace or (escapes >= max_escapes and screen_same)):
            return 'frozen'
        return 'stalled'
    return 'working'


# ---------------------------------------------------------------- driver
class Watchdog:
    def __init__(self, runtime):
        self.rt = runtime
        self.f = runtime.f
        self.cache = TableCache(self.f.cfg.get('prompt_table') or DEFAULT_TABLE)
        self.next_check = {}

    def audit(self, event, **fields):
        self.rt.audit(event, **fields)

    def set_state(self, slot, info, state, **evidence):
        prev = info.get('watch_state') or 'idle'
        if prev != state:
            self.audit('agent.state', slot=slot, level='warn' if state in ('stalled', 'frozen', 'prompting') else 'info',
                       detail=dict({'from': prev, 'to': state, 'generation': info.get('generation', '')}, **evidence))
        self.f.setf(slot, watch_state=state)

    def save_screen(self, slot, info, screen, tag):
        folder = self.f.run_dir / 'watchdog'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / '{}-g{}-{}-{}.txt'.format(slot, info.get('generation', '0'), int(time.time() * 1000), tag)
        path.write_text(screen or '', encoding='utf-8')
        return str(path)

    def capture(self, slot, info, pane):
        read = getattr(self.f.mgr, 'capture_output', None)
        screen = read(pane, 200) if read else ''
        if screen:
            if info.get('capture_fail', '0') != '0':
                self.f.setf(slot, capture_fail=0)
            return screen
        fails = int(info.get('capture_fail') or 0) + 1
        self.f.setf(slot, capture_fail=fails)
        if fails == int(setting(self.f.cfg, 'capture_fail_alarm')):
            # E6: thousands of silent capture failures hid a quota wall. Say it once per streak.
            self.audit('watch.capture_fail', slot=slot, level='error', count=fails,
                       error='screen capture failed ' + str(fails) + ' times in a row; dialogs are invisible')
        return ''

    def proc_stats(self, pane):
        probe = getattr(self.f.mgr, 'process_states', None)
        return probe(pane) if probe else []

    def proc_cpu(self, pane):
        """CPU seconds of the pane's process tree; None when it cannot be read."""
        probe = getattr(self.f.mgr, 'process_cpu', None)
        return probe(pane) if probe else None

    def quiet_for(self, slot, info, now, act, digest, cpu):
        """Seconds the transcript, the screen and the process-tree CPU have all been still (§24 stall window).

        The baseline lives in slot fields (like esc_digest / esc_at). Any signal moving, or one that cannot be
        read (empty screen, no CPU probe), restarts the window: an unreadable signal never proves stillness.
        CPU gain is summed per check over positive steps only (a child exiting lowers the total; that step is 0).
        """
        cfg = self.f.cfg
        window = float(setting(cfg, 'stall_window'))
        since = float(info.get('quiet_since') or 0)
        gain = float(info.get('quiet_gain') or 0)
        last = info.get('quiet_cpu', '')
        if cpu is not None and last != '':
            gain += max(0.0, cpu - float(last))
        moved = (not since or not digest or cpu is None or act > float(info.get('quiet_act') or 0)
                 or digest != info.get('quiet_digest') or gain > window * float(setting(cfg, 'stall_cpu_pct')) / 100)
        if moved:
            self.f.setf(slot, quiet_since=now, quiet_act=act, quiet_digest=digest,
                        quiet_cpu='' if cpu is None else cpu, quiet_gain=0)
            return 0
        self.f.setf(slot, quiet_cpu=cpu, quiet_gain=gain)
        return now - since

    def cli(self, slot):
        from cmux_fleet.fleet import agent_cli
        return agent_cli(self.f.cfg, slot)

    def authorized(self, slot, info, prompt, screen, table):
        """This run's own pane, and the dialog's concrete operation inside a grant the run already has."""
        from cmux_fleet import subfleet
        from cmux_fleet.fleet import agent_cwd
        owns = getattr(self.f.mgr, 'owns', None)
        if owns is not None and not owns(info['pane']):
            return False, 'pane ' + info['pane'] + ' is not in this fleet\'s registry'
        if subfleet.adopted(self.f.cfg, slot, info.get('generation')):
            return False, 'adopted pane belongs to the parent fleet'
        return authorize(prompt, screen, table['tail_lines'], agent_cwd(self.f.cfg, slot),
                         Path(self.f.cfg['run']) / 'workspace')

    def tick(self, now=None):
        now = time.time() if now is None else now
        table = self.cache.get(self.audit)
        interval = float(setting(self.f.cfg, 'watch_interval'))
        for slot in list(self.f.names):
            info = self.f.agent(slot)
            if info.get('link_lost') == '1' and info.get('state') == 'held' and info.get('pane'):
                self.link_restart(slot, info, now)
        for slot in list(self.f.names):
            info = self.f.agent(slot)
            state, pane = info.get('state'), info.get('pane')
            if state in HANDS_OFF or info.get('handoff_id') or not pane:
                if info.get('prompt_wait'):  # held / retired / handed off: the dialog wait is someone else's now
                    self.user_done(slot, 'hands_off')
                    self.clear_prompt_wait(slot, 'hands_off')
                continue
            lease = self.f.r.hgetall(self.rt.bus.lease_key(slot))
            in_flight = bool(lease.get('current_delivery')) and lease.get('status') == 'received'
            active = in_flight or state == 'starting'
            scope = lease.get('current_delivery') or ('startup-g' + str(info.get('generation', '0')))
            if not active:
                if info.get('prompt_wait'):  # the delivery settled / the startup READY came
                    self.user_done(slot, 'inactive')
                    self.clear_prompt_wait(slot, 'inactive')
                if (info.get('watch_state', 'idle') != 'idle' or info.get('stall_escapes', '0') != '0'
                        or info.get('quiet_since', '0') != '0' or info.get('bg_wait')):
                    self.set_state(slot, info, 'idle')
                    self.f.setf(slot, stall_escapes=0, watch_since=0, proc_frozen=0, prompt_hits=0, quiet_since=0,
                                bg_wait='')
                continue
            if now < self.next_check.get(slot, 0):
                continue
            self.next_check[slot] = now + interval
            if info.get('prompt_wait'):
                if info['prompt_wait'] != scope:  # that delivery settled, a new one / generation came
                    self.drop_dialog(slot, info, 'identity')
                    self.user_done(slot, 'scope_changed')
                    self.clear_prompt_wait(slot, 'scope_changed')
                elif info.get('prompt_channel') in ('opencode', 'dsh'):
                    # opencode / dsh permission request: answered only by a permission_reply card (supervisor) or by
                    # the user in the pane. The activity log moves on any plugin event, so it never clears the
                    # wait; no screen read, no stall ladder, not a single key until the delivery settles.
                    if info.get('prompt_route') == 'supervisor':
                        self.supervised(slot, info, lease, scope, table, now, activity=False)
                    continue
                elif info.get('prompt_route') == 'supervisor':
                    if self.supervised(slot, info, lease, scope, table, now):
                        continue
                elif self.f.last_activity(info) <= float(info.get('prompt_wait_at') or 0):
                    # A human answers this dialog (B-2): no screen read, no stall ladder, not a single key,
                    # whether or not the dialog is still in the captured tail. With channels, the user may have
                    # answered from the phone: answer_user presses only after the full fresh-screen check.
                    if self.f.cfg.get('channels'):
                        self.answer_user(slot, info, lease, scope, table)
                    continue
                else:  # the transcript moved: the human answered and the agent went on
                    self.user_done(slot, 'activity')
                    self.clear_prompt_wait(slot, 'activity')
                    self.set_state(slot, info, 'working')
                info = self.f.agent(slot)
            self.check(slot, info, lease, in_flight, table, now)

    def link_restart(self, slot, info, now):
        """§35: the bridge link is lost. Restart the session the way a frozen one is (kill + next generation
        from a handoff note); the cancelled work goes back to the supervisor / caller, never re-run here."""
        if self.f.paused():
            return  # nothing is (re)started while the run is paused; retried once it resumes
        count, first = (info.get('link_restarts') or '0:0').split(':')
        count, first = (0, now) if now - float(first) > LINK_WINDOW_S else (int(count), float(first))
        if count >= LINK_MAX_RESTARTS:
            text = (slot + ': bridge link lost ' + str(count) + ' times within ' + str(LINK_WINDOW_S // 60)
                    + ' min; not restarted again. Check Redis / the bridge, then resume.')
            self.f.setf(slot, link_lost='capped')
            self.audit('link.restart_capped', slot=slot, level='error', error=text)
            self.rt.transaction('link-capped-' + uuid.uuid4().hex, lambda: self.rt.hold(slot, text))
            return
        self.f.setf(slot, link_lost='restarting', link_restarts=str(count + 1) + ':' + str(first))
        screen = self.capture(slot, info, info['pane']) or ''
        ok = self.rotate(slot, 'bridge link lost', screen)
        self.f.setf(slot, link_lost='0' if ok else 'failed')
        self.audit('link.restarted' if ok else 'link.restart_failed', slot=slot, level='error',
                   restarts=count + 1, label=self.f.label(slot))

    # ------------------------------------------------------------ plan F' 8.3: non-delete dialog -> supervisor
    def dialog_key(self, slot, info):
        from cmux_fleet.fleet import DIALOG_KEY
        return DIALOG_KEY + slot + ':g' + str(info.get('generation', '0'))

    def supervisor_away(self):
        """Why the supervisor cannot rule on a worker's dialog now ('' when it can)."""
        if not self.f.running():  # task mode between tasks (service mode always runs)
            return 'no running task'
        if self.f.paused():
            return 'dispatch is paused'
        sup = self.f.agent('supervisor')
        if sup.get('state') in SUPERVISOR_AWAY or sup.get('handoff_id') or sup.get('prompt_wait'):
            return 'supervisor is ' + (sup.get('state') or 'missing') + (' at a dialog' if sup.get('prompt_wait') else '')
        return ''

    def escalate(self, slot, info, lease, scope, prompt, unknown, screen, path, table, paused_ok=False):
        """Hand a non-delete dialog to the supervisor (plan F' 8.3). False: it goes to the user instead.
        ``paused_ok``: the pause is the delete wait this dialog replaced (P6c hand-back), as in ``supervised``."""
        from cmux_fleet import subfleet
        owns = getattr(self.f.mgr, 'owns', None)
        if slot == 'supervisor':
            return False  # nobody can rule on the supervisor's own dialog but the user
        if (owns is not None and not owns(info['pane'])) or subfleet.adopted(self.f.cfg, slot, info.get('generation')):
            return False  # keys are never pressed on a pane this fleet does not own
        away = self.supervisor_away()
        if away and not (paused_ok and away == 'dispatch is paused'):
            self.audit('dialog.to_human', slot=slot, level='warn', reason='supervisor_unavailable', detail=away)
            return False
        tail = screen_tail(screen, table['tail_lines'])
        if prompt is not None:
            keys = list(prompt['keys'])
        else:  # an unknown dialog: Enter only when the cursor visibly sits on Yes
            keys = ['Enter'] if YES_CURSOR.search(tail.lower()) else None
        rec = {'kind': 'dialog', 'slot': slot, 'generation': str(info.get('generation', '0')),
               'session_id': info.get('thread', ''), 'scope': scope, 'delivery_id': lease.get('current_delivery', ''),
               'prompt': prompt['name'] if prompt is not None else 'unknown:' + unknown,
               'screen': path, 'digest': hashlib.sha256(tail.encode()).hexdigest()[:16],
               'keys_approve': keys, 'keys_reject': ['Escape']}
        self.rt.transaction('watch-dialog-' + uuid.uuid4().hex, lambda: self.rt.prompt_escalate(slot, rec, tail))
        return True

    def drop_dialog(self, slot, info, reason):
        """Pop this slot's dialog escalation (if any) and audit why it is stale."""
        from cmux_fleet.fleet import DIALOG_KEY
        prefix = DIALOG_KEY + slot + ':g'  # any generation of this slot
        esc = self.f.escalation_state()
        stale = [k for k in esc if k.startswith(prefix)]
        if stale:
            self.rt.transaction('watch-dialog-' + uuid.uuid4().hex, lambda: self.f.save_escalations(
                {k: v for k, v in esc.items() if k not in stale}))
            self.audit('dialog.stale', slot=slot, level='info', reason=reason, task=', '.join(stale))

    def to_user(self, slot, info, scope, rec, why, reason, user=None):
        """A supervisor-routed dialog falls back to the user (plan F' 8.3 step 4 / 5). ``user``: the pending
        record's data (``user_record``) for a reclassified delete in a run with channels (P6c)."""
        def act():
            esc = self.f.escalation_state()
            esc.pop(self.dialog_key(slot, info), None)
            self.f.save_escalations(esc)
            self.audit('dialog.' + ('reclassified_delete' if why == 'reclassified_delete' else 'to_human'), slot=slot,
                       level='warn', reason=why, task=self.dialog_key(slot, info))
            self.rt.prompt_wait(slot, scope, reason, (rec or {}).get('screen', ''),
                                kind='delete' if why == 'reclassified_delete' else 'other')
        self.rt.transaction('watch-dialog-' + uuid.uuid4().hex, act)
        if why == 'reclassified_delete':
            self.push_user(slot, user)

    def supervised(self, slot, info, lease, scope, table, now, activity=True):
        """One tick of a dialog waiting for the supervisor. True: done with this slot this tick.
        ``activity=False`` (opencode permission): transcript activity is not an answer."""
        key = self.dialog_key(slot, info)
        esc = self.f.escalation_state()
        rec = esc.get(key)
        if rec is None or rec.get('scope') != scope:  # e.g. a new task cleared fx:escalations
            self.to_user(slot, info, scope, rec, 'record_gone', 'dialog escalation record gone; handed to the user')
            return True
        if activity and self.f.last_activity(info) > float(info.get('prompt_wait_at') or 0):
            # A human answered in the pane meanwhile: nothing to press any more.
            self.drop_dialog(slot, info, 'activity')
            self.clear_prompt_wait(slot, 'activity')
            self.set_state(slot, info, 'working')
            return False
        if rec.get('status') == 'answered':
            if rec.get('channel') == 'permission_reply':  # answered by Fleet.permission_answer, never by keys
                return True
            return self.answer_dialog(slot, info, scope, key, rec, table)
        away = self.supervisor_away()
        if away and away != 'dispatch is paused':
            self.to_user(slot, info, scope, rec, 'supervisor_unavailable', 'supervisor unavailable (' + away
                         + '); dialog handed to the user')
            return True
        if not float(rec.get('delivered_at') or 0):
            # The clock starts when the supervisor receives the [dialog] message, not while it is queued.
            if self.rt.bus.status(rec.get('message_id') or '') not in ('', 'queued'):
                rec['delivered_at'] = now
                esc[key] = rec
                self.rt.transaction('watch-dialog-' + uuid.uuid4().hex, lambda: self.f.save_escalations(esc))
            return True
        limit = float(setting(self.f.cfg, 'dialog_escalation_timeout'))
        if now - float(rec['delivered_at']) >= limit:
            minutes = str(int(limit // 60))
            self.to_user(slot, info, scope, rec, 'supervisor_timeout', 'supervisor did not rule on the dialog within '
                         + minutes + ' min; handed to the user')
        return True

    def answer_dialog(self, slot, info, scope, key, rec, table):
        """Press the supervisor's answer, only after every check passes on a fresh screen (plan F' 8.3 step 4)."""
        from cmux_fleet import subfleet
        pane = info['pane']
        screen = self.capture(slot, info, pane)
        if not screen or self.f.wall_gate(slot, screen) is not None:
            return True  # nothing readable / a wall is the brake's: try again next tick
        owns = getattr(self.f.mgr, 'owns', None)
        if (str(info.get('generation', '0')) != rec['generation'] or info.get('thread', '') != rec['session_id']
                or scope != rec['scope'] or (owns is not None and not owns(pane))
                or subfleet.adopted(self.f.cfg, slot, info.get('generation'))):
            self.drop_dialog(slot, info, 'identity')
            self.clear_prompt_wait(slot, 'identity')
            return True
        tail = screen_tail(screen, table['tail_lines'])
        prompt, unknown = match_prompt(table, screen, self.cli(slot))
        if prompt is None and unknown is None:  # the dialog is gone (answered in the pane, or the agent moved on)
            self.drop_dialog(slot, info, 'dialog_gone')
            self.clear_prompt_wait(slot, 'dialog_gone')
            self.set_state(slot, info, 'working')
            return False
        same = (prompt['name'] if prompt is not None else 'unknown:' + unknown) == rec['prompt']
        if not same or hashlib.sha256(tail.encode()).hexdigest()[:16] != rec['digest']:
            # Another dialog (or this one changed): never press the old answer; classify and route it afresh.
            self.drop_dialog(slot, info, 'dialog_changed')
            self.clear_prompt_wait(slot, 'dialog_changed')
            return False
        if dialog_class(table, prompt, screen) != 'other':
            user = self.user_record(slot, info, scope, prompt, unknown, screen, rec.get('screen', ''), table)
            self.to_user(slot, info, scope, rec, 'reclassified_delete', 'dialog ' + rec['prompt'] + ' now reads as a '
                         'delete / move; handed to the user, nothing pressed', user=user)
            return True
        if rec['decision'] == 'approve' and not rec.get('keys_approve'):
            self.to_user(slot, info, scope, rec, 'no_safe_key', 'the supervisor approved dialog ' + rec['prompt']
                         + ' but no safe key is known; handed to the user')
            self.rt.queue('supervisor', '[dialog] ' + key + ': no safe key to press for approve; handed to the user.')
            return True
        keys = rec['keys_approve'] if rec['decision'] == 'approve' else rec.get('keys_reject') or ['Escape']
        sent = [k for k in keys if k in KEYS and self.f.mgr.send_key(pane, k)]
        if prompt is not None:
            hits = int(info.get('prompt_hits') or 0) if info.get('prompt_scope') == scope else 0
            self.f.setf(slot, prompt_scope=scope, prompt_hits=hits + 1)
        self.audit('watch.prompt_answered', slot=slot, level='warn', prompt=rec['prompt'], keys=sent, screen=rec['screen'],
                   authorized='supervisor:' + rec['decision'], task=key, delivery_id=rec.get('delivery_id', ''))
        self.drop_dialog(slot, info, 'answered')
        self.clear_prompt_wait(slot, 'answered')
        self.set_state(slot, info, 'working')
        return True

    def clear_prompt_wait(self, slot, reason):
        info = self.f.agent(slot)
        self.f.setf(slot, prompt_wait='', prompt_wait_at=0, prompt_route='', prompt_channel='', oc_permission='')
        self.audit('watch.prompt_cleared', slot=slot, level='info', reason=reason, scope=info.get('prompt_wait', ''))

    def wait_for_user(self, slot, scope, reason, path, kind, user=None):
        """Hand a dialog to the user (plan F B-1 / F' 8.2): mark the slot, pause dispatch, press nothing.
        The lease is untouched: once the human answers, the turn ends and settles normally. A delete dialog in a
        run with channels also gets the user's phone card (P6c, ``user`` = ``user_record``)."""
        self.rt.transaction('watch-prompt-' + uuid.uuid4().hex, lambda: self.rt.prompt_wait(
            slot, scope, reason + '; nothing pressed, screen saved to ' + path, path, kind=kind))
        if kind not in ('other', None):
            self.push_user(slot, user, kind)

    # ------------------------------------------------------------ P6c: delete dialogs answered from the phone
    def user_record(self, slot, info, scope, prompt, unknown, screen, path, table):
        """The data ``RuntimeV2.push_user_dialog`` registers (None without channels: nothing is built)."""
        if not self.f.cfg.get('channels'):
            return None
        tail = screen_tail(screen, table['tail_lines'])
        paths = []
        for p in PATH_RE.findall(unwrap_paths(tail)):
            if p not in paths:
                paths.append(p)
        return {'info': dict(info), 'lease': self.f.r.hgetall(self.rt.bus.lease_key(slot)), 'scope': scope,
                'prompt': prompt['name'] if prompt is not None else 'unknown:' + (unknown or ''),
                'tail': tail, 'digest': hashlib.sha256(tail.encode()).hexdigest()[:16], 'screen': path,
                'keys_approve': user_approve_keys(table, tail), 'paths': paths[:20]}

    def dialog_kind(self, table, prompt, screen):
        """P8j: the multi-class classify_dialog under a policy; dialog_class (delete / other) without one."""
        from cmux_fleet.channels import security
        pol = security.policy(self.f.cfg)
        return dialog_class(table, prompt, screen) if pol is None else classify_dialog(table, prompt, screen, pol)

    def push_user(self, slot, user, kind='delete'):
        """Register the pending record and queue the user-only card, after the prompt_wait transaction is applied:
        the pause reason it records is the one prompt_wait just set. Runs outside the transaction because the
        record is written with EXISTS / EVAL / a pipeline, which the effect overlay does not carry."""
        if user is not None and self.f.cfg.get('channels'):
            if kind != 'delete':  # P8j (M178): registered with its class, so the queued card has the class title
                user = dict(user, dialog_class=kind, title=CLASS_TITLES.get(kind, kind))
            self.rt.push_user_dialog(slot, user)

    def user_done(self, slot, reason):
        """The slot's dialog wait ended without a phone answer being pressed: its pending record is done."""
        if self.f.cfg.get('channels'):
            from cmux_fleet.channels import approve
            approve.mark_done(self.f.r, slot, reason)

    def user_reply(self, rec, which, **fields):
        """One reply item to whoever answered on the phone (user-only: it names the pending id)."""
        from cmux_fleet import channels
        from cmux_fleet.channels import envelope
        where = {k: rec.get(k, '') for k in ('channel', 'account', 'conversation')}
        target = 'explicit' if all(where.values()) else 'last'
        item = envelope.outbound('reply', target=target, text=envelope.receipt(which, id=rec.get('id', ''), **fields),
                                 user_only=True, **(where if target == 'explicit' else {}))
        self.f.r.lpush(channels.OUTQ, json.dumps(item, ensure_ascii=False))

    def answer_user(self, slot, info, lease, scope, table):
        """Press the phone's answer to this slot's delete dialog, only after every check passes on a fresh screen
        (contracts §8 删除确认框的用户出口; the seven checks of answer_dialog). Acts only on an ``answered`` record;
        a pending / confirming one waits. Never resumes dispatch (Q2): the reply says the fleet is still paused."""
        from cmux_fleet import channels, subfleet
        from cmux_fleet.channels import approve
        r = self.f.r
        pid = r.get(channels.PENDING_SLOT + slot)
        rec = r.hgetall(channels.PENDING + pid) if pid else {}
        if not rec or rec.get('status') != 'answered' or rec.get('kind', 'dialog') != 'dialog' \
                or rec.get('slot') != slot:
            return
        decision = rec.get('decision')

        def refuse(status, checked, why, which='changed'):
            """answered -> ``status`` (compare-and-set), audit, one reply. False: the record moved meanwhile."""
            if not approve._set_if(r, pid, ('answered',), {'status': status}):
                return False
            self.user_audit(rec, info, checked, why)
            if which == 'changed':
                self.user_reply(rec, which, reason=why)
            else:
                self.user_reply(rec, which)
            return True

        try:
            expires = float(rec.get('expires') or 0)
        except ValueError:
            expires = 0
        if expires <= time.time():
            refuse('expired', 'expired', 'the record expired', which='expired')
            return
        pane = info['pane']
        screen = self.capture(slot, info, pane)
        if not screen or self.f.wall_gate(slot, screen) is not None:
            return  # nothing readable / a wall is the brake's: the record waits, try again next tick
        owns = getattr(self.f.mgr, 'owns', None)
        if owns is not None and not owns(pane):
            refuse('stale', 'not_owned', 'pane not in this fleet')
            return
        if subfleet.adopted(self.f.cfg, slot, info.get('generation')):
            refuse('stale', 'adopted', 'adopted pane')
            return
        if (str(info.get('generation', '0')) != rec.get('generation') or info.get('thread', '') != rec.get('session_id')
                or scope != rec.get('scope')):
            refuse('stale', 'identity', 'generation / session / delivery changed')
            return
        tail = screen_tail(screen, table['tail_lines'])
        prompt, unknown = match_prompt(table, screen, self.cli(slot))
        if prompt is None and unknown is None:  # the dialog is gone: answered in the pane or the agent moved on
            approve.mark_done(r, slot, 'dialog_gone')
            self.user_audit(rec, info, 'dialog_gone', 'the dialog is no longer on screen')
            self.clear_prompt_wait(slot, 'dialog_gone')
            self.set_state(slot, info, 'working')
            return
        label = prompt['name'] if prompt is not None else 'unknown:' + unknown
        kind = dialog_class(table, prompt, screen)
        if label != rec.get('prompt') or hashlib.sha256(tail.encode()).hexdigest()[:16] != rec.get('digest'):
            if not refuse('stale', 'dialog_changed', 'the dialog on screen changed'):
                return
            path = self.save_screen(slot, info, screen, 'prompt-' + (prompt['name'] if prompt else 'unknown'))
            if kind == 'other':  # no longer a delete: back to the supervisor route (or the user's pane)
                self.clear_prompt_wait(slot, 'dialog_changed')
                if not self.escalate(slot, info, lease, scope, prompt, unknown, screen, path, table, paused_ok=True):
                    self.wait_for_user(slot, scope, 'dialog ' + label + ' changed and is no longer a delete', path,
                                       kind)
            else:  # still a delete: a new record and card for what is on the screen now
                self.push_user(slot, self.user_record(slot, info, scope, prompt, unknown, screen, path, table))
            return
        if kind == 'other':  # same text, but the delete words are gone from the table's view (cannot happen today)
            refuse('stale', 'reclassified', 'no longer a delete dialog')
            return
        if decision == 'approve':
            try:
                keys = json.loads(rec.get('keys_approve') or '[]')
            except ValueError:
                keys = []
            keys = [k for k in keys if isinstance(k, str) and k in KEYS] if isinstance(keys, list) else []
            if not keys:
                refuse('stale', 'no_key', 'no registered approve key')
                return
        elif decision == 'reject':
            keys = ['Escape']
        else:
            refuse('stale', 'decision', 'unknown decision')
            return
        if not approve._set_if(r, pid, ('answered',), {'status': 'done', 'done_reason': 'pressed'}):
            return  # someone else took it meanwhile: press nothing
        sent = [k for k in keys if self.f.mgr.send_key(pane, k)]
        self.user_audit(rec, info, 'ok', 'pressed ' + str(len(sent)) + ' key(s)')
        self.clear_prompt_wait(slot, 'user_answered')
        self.set_state(slot, self.f.agent(slot), 'working')
        self.user_reply(rec, 'answered', decision=approve.DECISION_TEXT.get(decision, decision),
                        reason=r.hget('fx:service', 'reason') or '',
                        resume='cmux-fleet resume --config ' + str(Path(self.f.cfg['run']) / 'config.json'))

    def user_audit(self, rec, info, checked, reason):
        self.audit('dialog.user_answered', slot=rec.get('slot', ''), level='warn', pending_id=rec.get('id', ''),
                   generation=str(info.get('generation', '')), decision=rec.get('decision', ''),
                   channel=rec.get('channel', ''), account=rec.get('account', ''), sender=rec.get('sender', ''),
                   checked=checked, reason=reason)

    def check(self, slot, info, lease, in_flight, table, now):
        pane = info['pane']
        cfg = self.f.cfg
        screen = self.capture(slot, info, pane)
        if screen and self.f.wall_gate(slot, screen) is not None:
            # E before F (docs/design.md §23): a usage wall is the brake's. No key, no hold, no kill this
            # tick; a wall menu (e.g. "❯ 1. Yes, switch…") is never a dialog to answer.
            return
        digest = hashlib.sha256(screen.encode()).hexdigest()[:16] if screen else ''
        frozen_seen = int(info.get('proc_frozen') or 0) + 1 if uninterruptible(self.proc_stats(pane)) else 0
        self.f.setf(slot, proc_frozen=frozen_seen)
        prompt, unknown = match_prompt(table, screen, self.cli(slot))
        scope = lease.get('current_delivery') or ('startup-g' + str(info.get('generation', '0')))
        hits = int(info.get('prompt_hits') or 0) if info.get('prompt_scope') == scope else 0
        prev = info.get('watch_state') or 'idle'
        since = float(info.get('watch_since') or 0)
        stall_timeout = float(cfg.get('stall_timeout') or 0)
        act = self.f.last_activity(info)
        age = now - act if in_flight else 0
        # §24: a Stop of this delivery deferred by background tasks, transcript still since -> waiting, not quiet.
        # The stall_timeout hard limit (age), U/D and dialogs still apply.
        waiting = (in_flight and bool(info.get('bg_wait')) and info['bg_wait'] == scope
                   and act <= float(info.get('bg_wait_act') or 0) + BG_WAIT_GRACE)
        if info.get('bg_wait') and not waiting:
            self.f.setf(slot, bg_wait='')
        if waiting:
            quiet = 0
            self.f.setf(slot, quiet_since=0)
        else:
            quiet = self.quiet_for(slot, info, now, act, digest, self.proc_cpu(pane)) if in_flight else 0
        moved = prev == 'stalled' and act > since
        escapes = int(info.get('stall_escapes') or 0)
        state = classify(active=True, in_flight=in_flight, age=age, stall_timeout=stall_timeout, prev=prev,
                         since=since, now=now, grace=float(setting(cfg, 'stall_grace')), escapes=escapes,
                         max_escapes=int(setting(cfg, 'stall_escapes')),
                         screen_same=bool(digest) and digest == info.get('esc_digest'), proc_frozen=frozen_seen,
                         prompt=prompt, unknown=unknown, prompt_hits=hits, max_hits=table['max_hits'], moved=moved,
                         quiet=quiet, stall_window=float(setting(cfg, 'stall_window')))
        if state == 'prompting' and prompt is not None:
            path = self.save_screen(slot, info, screen, 'prompt-' + prompt['name'])
            allowed, reason = self.authorized(slot, info, prompt, screen, table)
            kind = self.dialog_kind(table, prompt, screen)
            if allowed and kind != 'other':
                # Never press keys on anything that may delete / move data, even on a whitelisted dialog.
                allowed, reason = False, 'the screen shows a delete / move: the user decides'
            if not allowed:
                # Keep the scene: nothing pressed, a human reads the saved screen and answers in the pane.
                self.set_state(slot, info, state, prompt=prompt['name'], authorized=False)
                self.audit('watch.prompt_unverified', slot=slot, level='error', prompt=prompt['name'], reason=reason,
                           screen=path, delivery_id=lease.get('current_delivery', ''), dialog_class=kind or 'unread')
                if kind == 'other' and self.escalate(slot, info, lease, scope, prompt, None, screen, path, table):
                    return
                user = self.user_record(slot, info, scope, prompt, None, screen, path, table) if kind not in ('other', None) else None
                self.wait_for_user(slot, scope, 'dialog ' + prompt['name'] + ' not answered: ' + reason, path, kind,
                                   user=user)
                return
            sent = [k for k in prompt['keys'] if self.f.mgr.send_key(pane, k)]
            self.f.setf(slot, prompt_scope=scope, prompt_hits=hits + 1)
            self.set_state(slot, info, state, prompt=prompt['name'])
            self.audit('watch.prompt_answered', slot=slot, level='warn', prompt=prompt['name'], keys=sent,
                       hits=hits + 1, screen=path, authorized=reason, delivery_id=lease.get('current_delivery', ''))
        elif state == 'prompting':
            path = self.save_screen(slot, info, screen, 'prompt-unknown')
            kind = self.dialog_kind(table, None, screen)
            self.set_state(slot, info, state, marker=unknown)
            self.audit('watch.prompt_unknown', slot=slot, level='error', marker=unknown, screen=path,
                       dialog_class=kind or 'unread')
            if kind == 'other' and self.escalate(slot, info, lease, scope, None, unknown, screen, path, table):
                return
            user = self.user_record(slot, info, scope, None, unknown, screen, path, table) if kind not in ('other', None) else None
            self.wait_for_user(slot, scope, 'unknown confirmation dialog on screen (' + unknown + ')', path, kind,
                               user=user)
        elif state == 'working':
            if prev != 'working' or escapes:
                self.f.setf(slot, stall_escapes=0, watch_since=0, esc_digest='')
            self.set_state(slot, info, state, **({'resumed_after_s': int(now - since)} if prev == 'stalled' else {}))
        elif state == 'stalled':
            if prev != 'stalled':
                since, escapes = now, 0
                self.f.setf(slot, watch_since=now, stall_escapes=0)
                self.set_state(slot, info, state, idle_s=int(age), quiet_s=int(quiet),
                               dialog=prompt['name'] if prompt else '')
            spacing = float(setting(cfg, 'stall_grace')) / max(1, int(setting(cfg, 'stall_escapes')))
            last = float(info.get('esc_at') or 0) if prev == 'stalled' else 0
            if escapes < int(setting(cfg, 'stall_escapes')) and now - last >= spacing:
                self.f.mgr.send_key(pane, 'Escape')
                self.f.setf(slot, stall_escapes=escapes + 1, esc_at=now, esc_digest=digest)
                self.audit('watch.escape', slot=slot, level='warn', escapes=escapes + 1, idle_s=int(age))
        elif state == 'frozen':
            why = ('processes in uninterruptible wait' if frozen_seen >= 2 else
                   str(escapes) + ' Escape presses left the screen unchanged' if escapes and digest == info.get('esc_digest')
                   else 'no activity for ' + str(int(now - since)) + ' s after the first Escape')
            self.set_state(slot, info, state, reason=why)
            self.rotate(slot, why, screen)

    # ------------------------------------------------------------ frozen: kill + next generation
    def rotate(self, slot, why, screen, reason=None):
        """Kill this fleet's own frozen session and start the next generation in its place."""
        from cmux_fleet import history, subfleet
        from cmux_fleet.fleet import NAMES, SUPERVISOR_LABEL
        f, rt = self.f, self.rt
        info = f.agent(slot)
        pane, generation = info['pane'], int(info.get('generation') or 0)
        if info.get('prompt_wait'):
            return False  # a confirmation dialog never authorizes a kill, including mid-turn fallback
        path = self.save_screen(slot, info, screen, 'frozen')
        if f.paused():
            # Nothing is (re)started while the run is paused (closed market, operator hold).
            rt.transaction('watch-frozen-' + uuid.uuid4().hex, lambda: rt.hold(
                slot, 'frozen (' + why + ') while the run is paused; not restarted, screen saved to ' + path))
            return False
        if subfleet.adopted(f.cfg, slot, info.get('generation')):
            # The parent fleet owns this pane: never kill it from here.
            rt.transaction('watch-frozen-' + uuid.uuid4().hex, lambda: rt.hold(
                slot, 'frozen (' + why + '); adopted pane belongs to the parent fleet, not killed'))
            return False
        owns = getattr(f.mgr, 'owns', None)
        if owns is not None and not owns(pane):
            # R18 / default #13: only a pane this fleet's cmux registry proves it spawned is killed.
            rt.transaction('watch-frozen-' + uuid.uuid4().hex, lambda: rt.hold(
                slot, 'frozen (' + why + '); pane ' + pane + ' is not in this fleet\'s registry, not killed'))
            return False
        if slot == advisor.SLOT:
            # §27: one advisor fault branch (retire, next generation by the supervisor, queued consultations fall
            # back on failure); its in-flight consultation is cancelled and that slot falls back. Never raises.
            f.advisor_restart('frozen: ' + why)
            self.next_check.pop(slot, None)
            return f.agent(slot).get('state') == 'starting'
        lease = f.r.hgetall(rt.bus.lease_key(slot))
        msg = rt.bus.message(lease['message_id']) if lease.get('message_id') else {}
        is_midturn = bool(msg.get('midturn_id'))
        if reason == 'midturn' and not is_midturn:
            return False  # stale timeout; never turn it into an ordinary frozen kill
        revive = self.revive_summary(info, msg) if slot == 'supervisor' and not is_midturn else None
        # §43: an official Claude worker first continues its own session (claude --resume) and gets the cancelled
        # work again (lost-slot rules: attempt-capped, replay false never). A resumed generation that freezes
        # again, a missing transcript, or work that may not be replayed: the fresh successor below, as before.
        thread = (rt.resumable(slot, info) if slot in f.workers and not is_midturn and rt.redeliverable(slot, msg)
                  else '')
        hid = msg['midturn_id'] if is_midturn else 'frozen-' + uuid.uuid4().hex[:8]
        note = f.run_dir / 'handoffs' / (slot + '-g' + str(generation) + '-' + hid + '.md')
        note.parent.mkdir(parents=True, exist_ok=True)
        base = '' if thread else f.skeleton(slot, dict(info, handoff_id=hid))  # a resumed session keeps its context
        if is_midturn:
            from cmux_fleet import handoff
            draft = Path(info.get('midturn_draft') or '')
            if draft.is_file() and draft.stat().st_mtime_ns > int(info.get('midturn_mtime') or 0):
                note.write_text(handoff.merge_partial(base, draft.read_text()))
                if not handoff.validate(str(note))[0]:
                    note.write_text(base)
            else:
                note.write_text(base)
        elif not thread:
            note.write_text(base)
        snapshot = note.with_suffix('.terminal.txt')
        f.mgr.capture_to(pane, str(snapshot))
        prev = f.label(slot)
        label = (SUPERVISOR_LABEL if slot == 'supervisor' else slot) + '.' + str(generation + 1)
        # Order (same as respawn / handoff, keeps the layout): start the successor launch-held, put it in
        # the old tab's place, kill the old process tree, confirm retirement, only then open the launch
        # gate. Until then the successor cannot run work: it is 'starting' (pump delivers to 'idle'
        # only), the old lease still holds the delivery (offer needs an idle lease; register_slot
        # refuses 'busy'), and its READY is refused while launch_ready=0. Every failure below removes
        # the successor again, so at most one session of this slot is ever left.
        # A resumed successor's CLI (claude --resume) may only start once the launch gate opens below, after the
        # old process tree is killed and confirmed gone: two processes never write one session.
        f.setf(slot, generation=generation + 1, label=label, handoff_id='', watch_state='frozen',
               stall_escapes=0, watch_since=0, esc_digest='', proc_frozen=0, prompt_hits=0, quiet_since=0, bg_wait='',
               prev_label='' if slot == 'supervisor' else prev,
               **({'resume_session': thread, 'resume_generation': generation + 1} if thread else {}))
        if slot != 'supervisor':
            f.r.hset(NAMES, mapping={label: slot})
        try:
            new = f.spawn(slot, rt.resume_prompt(slot, 'it froze: ' + why).format(label=label) if thread else
                          '[Same-task takeover] You are ' + label + ' (taking over from ' + prev + ', which '
                          'froze and was stopped). Read the handoff note ' + str(note) + '. In this turn only read '
                          'the note, do no task work, and make your final reply exactly READY.'
                          + (' ' + revive.pop('text') if revive else ''), launch_held=True)
        except RuntimeError as exc:
            return self.abort_rotation(slot, info, label, f.agent(slot).get('pane'), path,
                                       'the next generation did not start (' + str(exc)[:300] + ')')
        if not f.mgr.tab_into(new, pane):
            return self.abort_rotation(slot, info, label, new, path, 'the next generation could not take the old tab')
        if not f.mgr.kill_pane(pane) or f.mgr.get_pane_status(pane) is not None:
            return self.abort_rotation(slot, info, label, new, path, 'the frozen session ' + prev + ' did not exit')
        rt.confirm_retired(slot)  # the process tree is gone: its delivery is cancelled, execution stopped
        if is_midturn:
            rt.transaction('midturn-kill-' + msg['midturn_id'], lambda: rt.midturn_split(
                slot, msg, msg, retired=True, note=str(note), previous=prev))
        if thread:  # queued before the gate opens; the successor gets it after its READY (pump: idle only)
            rt.redeliver(slot, msg, 'after the session of ' + prev + ' froze')
        f.setf(slot, launch_ready=1, retired_pane='')
        history.record(f.run_dir, slot=slot, label=prev, generation=generation, handoff='' if thread else str(note),
                       snapshot=str(snapshot), source='midturn' if is_midturn else 'frozen-resume' if thread
                       else 'frozen', timed_out=True)
        f.refresh_history()
        self.audit('watch.frozen_restarted', slot=slot, level='error', prev=prev, label=label, reason=why,
                   screen=path, handoff='' if thread else str(note), message_id=msg.get('message_id', ''),
                   resumed=thread)
        if revive:
            self.audit('supervisor.revived', slot=slot, level='error', prev=prev, label=label, reason=why, **revive)
        self.next_check.pop(slot, None)
        if not is_midturn and not thread:
            self.tell(slot, prev, label, why, msg)
        return True

    def revive_summary(self, info, msg):
        """§36: what the logs say about a supervisor about to be revived, for its successor's first message and the
        `supervisor.revived` audit: its last sign of activity, the in-flight message and whether it was applied,
        the last decisions the daemon committed for it."""
        from cmux_fleet.runtime_v2 import clock
        since = self.f.last_activity(info)
        stuck = int(time.time() - since) if since else None
        rows = self.rt.bus.r.xrevrange(self.rt.bus.audit_key, count=500)
        decisions = [(int(row.get('at') or sid.split('-')[0]) / 1000, ' '.join(str(row.get('detail', '')).split())[:160])
                     for sid, row in rows if row.get('event') == 'committed' and row.get('slot') == 'supervisor'][:3]
        applied = msg.get('status') == 'committed'
        text = ('[Revive summary] The previous generation was last active at ' + clock(since) + ' ('
                + str(stuck) + ' s before this restart). '
                + (('In-flight message ' + msg['message_id'] + ' (' + str(msg.get('kind') or 'message') + ') was '
                    + ('applied.' if applied else 'NOT applied: it may be partly acted on; check the state before '
                       'acting on it again.')) if msg.get('message_id') else 'No message was in flight.')
                + ' Last decisions the daemon applied: '
                + ('; '.join(clock(at) + ' ' + d for at, d in decisions) or 'none') + '.')
        return dict(text=text, stuck_since=clock(since), stuck_s=stuck, in_flight=msg.get('message_id', ''),
                    applied=applied if msg.get('message_id') else None,
                    decisions=[clock(at) + ' ' + d for at, d in decisions])

    def abort_rotation(self, slot, old, label, new, screen, why):
        """Undo a failed rotation: remove the successor, give the slot back to the frozen session, hold it.

        The old delivery is never cancelled here (only confirm_retired does that, after a confirmed
        kill), so the held slot still shows the work as uncertain and dispatch stays paused.
        """
        from cmux_fleet.fleet import NAMES
        f, rt = self.f, self.rt
        gone = not new or new == old.get('pane') or (f.mgr.kill_pane(new) and f.mgr.get_pane_status(new) is None)
        self.audit('watch.rotate_failed', slot=slot, level='error', reason=why, old_pane=old.get('pane', ''),
                   new_pane=new or '', successor_removed=gone, screen=screen)
        if not gone:
            # Both sessions may still be alive. The slot is held (its delivery uncertain, dispatch paused) and
            # stays launch-held: the successor's READY is refused, so it can never be offered work.
            text = ('watchdog could not stop the frozen session of ' + slot + ' (' + why + ') nor remove its '
                    'successor ' + str(new) + '; two sessions may hold slot ' + slot + '. Close one by hand.')
            f.setf(slot, launch_ready=0, retired_pane=old.get('pane', ''))
            rt.transaction('watch-frozen-' + uuid.uuid4().hex, lambda: rt.hold(slot, text))
            rt.finish('blocked', text)
            return False
        f.setf(slot, **{k: v for k, v in old.items() if k != 'watch_state'})
        f.setf(slot, watch_state='frozen', launch_ready=old.get('launch_ready', '1'),
               resume_session=old.get('resume_session', ''), resume_generation=old.get('resume_generation', ''))
        if slot != 'supervisor':
            f.r.hdel(NAMES, label)
        rt.transaction('watch-frozen-' + uuid.uuid4().hex, lambda: rt.hold(
            slot, 'frozen; ' + why + '. Successor removed, frozen session left as it was (screen ' + screen + ')'))
        return False

    def tell(self, slot, prev, label, why, msg):
        """The cancelled work is not re-run: the supervisor (or the request's caller) decides."""
        from cmux_fleet.fleet import REQ
        f, rt = self.f, self.rt
        if not msg or not f.running():
            return
        if slot == 'supervisor':
            text = (msg.get('body') or {}).get('text', '')
            note = ('\n\n[watchdog] The previous supervisor generation ' + prev + ' froze while handling this message ('
                    + why + ') and was restarted. It may have acted on it already: check the current state before '
                    'dispatching again.')
            rt.queue('supervisor', text + note, request_id=msg.get('request_id'), kind=msg.get('kind'))
            return
        rid = msg.get('request_id')
        if f.service:
            if rid and f.r.hget(REQ + rid, 'status') == 'dispatched':
                rt.transaction('watch-publish-' + uuid.uuid4().hex, lambda: rt.publish(
                    rid, 'stalled', worker=label, error=prev + ' froze (' + why + '); restarted, the request was not re-run'))
            return
        if msg.get('kind') not in ('worker', 'continuation'):
            return
        f.queue('supervisor', '[' + label + ' report (took over from ' + prev + ')]\nFROZEN: ' + prev + ' stopped '
                'responding (' + why + '). The watchdog killed its session and started ' + label + ' from a '
                'handoff note. Its instruction was cancelled and NOT re-run; it may be partly done. Check the '
                'workspace, then dispatch again or reply BLOCKED if a human is needed.'
                '\n\nReply with the JSON format from AGENTS.md.')


def ps_seconds(text):
    """`ps -o time=` as seconds: macOS [H:]MM:SS.cc, Linux [DD-]HH:MM:SS."""
    text = text.strip()
    days, _, text = text.rpartition('-')
    total = 0.0
    for part in text.split(':'):
        total = total * 60 + float(part)
    return (int(days) if days else 0) * 86400 + total


def pane_tty_cpu(ttys):
    """Cumulative CPU seconds of the processes on these ttys and all their descendants; None when unreadable.

    Descendants by ppid, not by tty: Claude Code / Codex run their tools in a session of their own (tty '??'), so
    a pytest under the Bash tool is invisible to `ps -t`. One `ps -ax` per call.
    """
    import subprocess
    names = {t[len('/dev/'):] if t.startswith('/dev/') else t for t in ttys or () if t}
    if not names:
        return None
    try:
        p = subprocess.run(['ps', '-axo', 'pid=,ppid=,tty=,time='], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    kids, cpu, todo = {}, {}, []
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) != 4 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid = int(parts[0])
        try:
            cpu[pid] = ps_seconds(parts[3])
        except ValueError:
            continue
        kids.setdefault(int(parts[1]), []).append(pid)
        if parts[2] in names:
            todo.append(pid)
    if not todo:
        return None
    seen = set()
    while todo:
        pid = todo.pop()
        if pid not in seen:
            seen.add(pid)
            todo += kids.get(pid, [])
    return round(sum(cpu.get(pid, 0.0) for pid in seen), 2)


def pane_tty_stats(tty):
    """`ps` STAT of every process on a tty (cmux surface). Empty when ps fails."""
    import subprocess
    if not tty:
        return []
    p = subprocess.run(['ps', '-t', os.path.basename(tty), '-o', 'stat='], capture_output=True, text=True)
    return [line.strip() for line in p.stdout.splitlines() if line.strip()] if p.returncode == 0 else []
