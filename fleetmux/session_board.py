#!/usr/bin/env python3
"""Cross-session board + collision notices (docs/contracts.md §7).

Independent Claude Code and pi sessions each keep one JSON record on a shared board
(default ~/.fleetmux/board, override FLEETMUX_BOARD_DIR): which repository / worktree /
branch they touched, which files, which git actions, and the user's latest prompt.
After every write the session compares itself with the other online records; two sessions
collide when they share a repository (by `git rev-parse --git-common-dir`, so worktrees of
one repository are one repository) and either edited the same repository-relative file, or
merged into the same branch within the merge window (sharing a worktree or a branch alone is
not a collision; it only shows as background in the notice).
First come, first served: the session that started on the repository first keeps going, the
later one is told to leave the colliding files alone until the fleet supervisor releases it.
Each session is told about a given collision once, and again only when it grows
(a new rule or a new common file). Nothing is stopped or merged by this module.

Standard library only. Entry points:
  session_board.py hook claude        Claude Code hook; hook JSON on stdin
  session_board.py hook pi '<json>'   pi extension (extensions/session-board.ts); prints notices
  session_board.py list [--json]      read-only: online sessions, collisions, pending releases
  session_board.py note <session> <text> [--from <sender>]   leave a note, delivered once
Hook failures are swallowed (exit 0, nothing printed) and written to <board>/errors.log;
list / note failures go to stderr with exit 1.
"""
from __future__ import annotations

import os
import sys

if __name__ == '__main__' and sys.path and os.path.realpath(sys.path[0] or '.') == os.path.dirname(os.path.realpath(__file__)):
    sys.path.pop(0)  # run as a script (or via a symlink): fleetmux/platform.py must not shadow the stdlib module

import contextlib
import fcntl
import json
import re
import shlex
import subprocess
import time
import traceback

TTL_S = 7200            # a record not updated for this long is offline (FLEETMUX_BOARD_TTL_S)
MERGE_WINDOW_S = 1800   # rule ④: both merged into the same branch within this window (FLEETMUX_BOARD_MERGE_WINDOW_S)
GOAL_CHARS = 160
MAX_TOUCHES = 300
MAX_GIT_EVENTS = 100
MAX_INBOX = 50
MAX_COLLISIONS = 100
PRESENCE_OPS = ('commit', 'merge', 'checkout')


def board_dir():
    return os.environ.get('FLEETMUX_BOARD_DIR') or os.path.expanduser('~/.fleetmux/board')


def _seconds(name, default):
    try:
        value = float(os.environ[name])
        return value if value > 0 else default
    except (KeyError, ValueError):
        return default


# ------------------------------------------------------------------ git facts
_GIT_CACHE = {}


def git_info(path):
    """{repo, worktree, branch} of the repository holding `path` (file or directory, need not exist), or None.

    repo = realpath of `git rev-parse --git-common-dir`; one rev-parse per directory per process;
    the branch is read from the worktree's HEAD file each time (it changes with checkout)."""
    d = os.path.realpath(path)
    while not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    if d not in _GIT_CACHE:
        try:
            out = subprocess.run(['git', '-C', d, 'rev-parse', '--path-format=absolute', '--show-toplevel',
                                  '--git-dir', '--git-common-dir'], capture_output=True, text=True, timeout=3)
            lines = out.stdout.splitlines()
            _GIT_CACHE[d] = lines if out.returncode == 0 and len(lines) == 3 else None
        except (OSError, subprocess.SubprocessError):
            _GIT_CACHE[d] = None
    facts = _GIT_CACHE[d]
    if not facts:
        return None
    top, git_dir, common = facts
    return {'repo': os.path.realpath(common), 'worktree': os.path.realpath(top), 'branch': _branch(git_dir)}


def _branch(git_dir):
    try:
        with open(os.path.join(git_dir, 'HEAD')) as fh:
            head = fh.read().strip()
    except OSError:
        return ''
    return head[len('ref: refs/heads/'):] if head.startswith('ref: refs/heads/') else ''  # '' = detached


def repo_name(repo):
    base = os.path.basename(repo.rstrip('/'))
    if base == '.git':
        return os.path.basename(os.path.dirname(repo.rstrip('/')))
    return base[:-4] if base.endswith('.git') else base


# ------------------------------------------------------------------ Bash command parsing (best effort)
_SEPARATORS = {'&&', '||', ';', '|', '&', '(', ')', ';;', '|&'}
_ASSIGNMENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
_GIT_OPTS_WITH_VALUE = {'-c', '--git-dir', '--work-tree', '--namespace', '--exec-path', '--super-prefix',
                        '--config-env'}


def _lex(text):
    lex = shlex.shlex(text, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def _logical_lines(command):
    """Token lists per logical line: a quoted string may span lines; here-document bodies are skipped."""
    buf, heredoc = '', None
    for line in command.split('\n'):
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        buf = buf + '\n' + line if buf else line
        try:
            tokens = _lex(buf)
        except ValueError:
            continue  # an open quote: the next line continues it
        buf = ''
        for i, tok in enumerate(tokens):
            if tok in ('<<', '<<-') and i + 1 < len(tokens):
                heredoc = tokens[i + 1]
        yield tokens


def _segments(command):
    for tokens in _logical_lines(command):
        seg = []
        for tok in tokens:
            if tok in _SEPARATORS:
                if seg:
                    yield seg
                seg = []
            else:
                seg.append(tok)
        if seg:
            yield seg


def _resolve(base, arg):
    if '$' in arg or '`' in arg:
        return None
    arg = os.path.expanduser(arg)
    if os.path.isabs(arg):
        return os.path.normpath(arg)
    return None if base is None else os.path.normpath(os.path.join(base, arg))


def _switch_target(rest):
    if '--' in rest:
        return None  # `git checkout -- file`: restores files, not a branch switch
    for i, word in enumerate(rest):
        if word in ('-b', '-B', '-c', '-C', '--orphan') and i + 1 < len(rest):
            return rest[i + 1]
    plain = [w for w in rest if not w.startswith('-')]
    return plain[0] if plain else None


def parse_git(command, cwd):
    """[(dir, op, target)] for git commit / merge / checkout|switch / worktree add in a shell command.

    Follows `cd <dir>` between segments and `git -C <dir>`; target is the branch switched to /
    created (None for commit and merge: their target is the worktree's branch when recorded)."""
    ops, cur = [], cwd
    for seg in _segments(command):
        words = seg
        while words and _ASSIGNMENT.match(words[0]):
            words = words[1:]
        if not words:
            continue
        if words[0] == 'cd':
            cur = _resolve(cur, words[1] if len(words) > 1 else '~') if (len(words) < 2 or words[1] != '-') else None
            continue
        if os.path.basename(words[0]) != 'git':
            continue
        d, i = cur, 1
        while i < len(words) and words[i].startswith('-'):
            if words[i] == '-C' and i + 1 < len(words):
                d = _resolve(d, words[i + 1])
                i += 2
            elif words[i] in _GIT_OPTS_WITH_VALUE:
                i += 2
            else:
                i += 1
        if i >= len(words) or d is None:
            continue
        sub, rest = words[i], words[i + 1:]
        if sub == 'commit' and '--dry-run' not in rest:
            ops.append((d, 'commit', None))
        elif sub == 'merge' and not {'--abort', '--quit'} & set(rest):
            ops.append((d, 'merge', None))
        elif sub in ('checkout', 'switch'):
            target = _switch_target(rest)
            if target:
                ops.append((d, 'checkout', target))
        elif sub == 'worktree' and rest[:1] == ['add']:
            args = rest[1:]
            target = next((args[j + 1] for j, w in enumerate(args) if w in ('-b', '-B') and j + 1 < len(args)), None)
            plain = [w for j, w in enumerate(args) if not w.startswith('-') and (j == 0 or args[j - 1] not in ('-b', '-B'))]
            target = target or (plain[0] if plain else None)
            ops.append((d, 'worktree_add', target))
    return ops


# ------------------------------------------------------------------ records
def _safe(text):
    return re.sub(r'[^A-Za-z0-9_.-]', '_', text)[:120]


def _record_path(board, agent, sid):
    return os.path.join(board, _safe(agent) + '-' + _safe(sid) + '.json')


@contextlib.contextmanager
def _locked(path, wait=1):
    """Serialise one session's own writers (Claude runs parallel tool calls -> parallel PostToolUse hooks)
    and `note`, which writes into another session's record."""
    fd = os.open(path + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    break  # never hang a session on the board
                time.sleep(.01)
        yield
    finally:
        os.close(fd)


def _load(path):
    try:
        with open(path) as fh:
            rec = json.load(fh)
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None


def _save(path, rec):
    tmp = os.path.join(os.path.dirname(path), '.' + os.path.basename(path) + '.' + str(os.getpid()) + '.tmp')
    with open(tmp, 'w') as fh:
        json.dump(rec, fh, ensure_ascii=False)
    os.replace(tmp, path)


def online(rec, now, ttl):
    if rec.get('ended_at') or now - float(rec.get('updated_at') or 0) > ttl:
        return False
    pid = rec.get('pid')
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            pass  # exists, not ours
    return True


def _others(board, own, now, ttl):
    try:
        entries = list(os.scandir(board))
    except OSError:
        return []
    out = []
    for entry in entries:
        if not entry.name.endswith('.json') or entry.name.startswith('.') or entry.path == own:
            continue
        try:
            if entry.stat().st_mtime < now - ttl - 60:
                continue  # cheap pre-filter: stale records are not parsed
        except OSError:
            continue
        rec = _load(entry.path)
        if rec and online(rec, now, ttl):
            out.append(rec)
    return out


# Claude feeds background-task / teammate notices through UserPromptSubmit as one tag pair
# (`<task-notification>…</task-notification>`); those are not the user's words.
_MACHINE_PROMPT = re.compile(r'<([A-Za-z][\w-]*)\b[^>]*>.*</\1>', re.S)


def _machine_prompt(text):
    text = text.strip()
    return not text or bool(_MACHINE_PROMPT.fullmatch(text))


def _goal(text):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= GOAL_CHARS else text[:GOAL_CHARS] + '…'


def _add_touch(rec, path, cwd, now, ttl):
    full = path if os.path.isabs(path) else os.path.join(cwd, path)
    info = git_info(full)
    if not info:
        return
    rel = os.path.relpath(os.path.realpath(full), info['worktree'])
    if rel.startswith('..'):
        return
    since = rec.setdefault('since', {})
    if info['repo'] not in since or not any(t['repo'] == info['repo'] and now - t['ts'] <= ttl for t in rec['touches']):
        since[info['repo']] = now  # first edit in this repository, or the first after a quiet TTL: (re)started here
    touches = [t for t in rec['touches']
               if not (t['repo'] == info['repo'] and t['worktree'] == info['worktree'] and t['path'] == rel)]
    touches.append(dict(info, path=rel, ts=now))
    rec['touches'] = touches[-MAX_TOUCHES:]


def _add_git(rec, command, cwd, now):
    for d, op, target in parse_git(command, cwd):
        info = git_info(d)
        if info:
            rec['git_events'] = (rec['git_events'] + [dict(info, op=op, target=target or info['branch'], ts=now)])[-MAX_GIT_EVENTS:]


# ------------------------------------------------------------------ detection
def _footprint(rec, now, ttl, window):
    """Per repo: {(worktree, branch)} present, {files}, {merge targets}; only within the TTL / merge window."""
    seen = {}
    marks = [t for t in rec.get('touches', []) if now - t['ts'] <= ttl]
    marks += [e for e in rec.get('git_events', []) if e['op'] in PRESENCE_OPS and now - e['ts'] <= ttl]
    latest = {}
    for m in sorted(marks, key=lambda m: m['ts']):
        latest[(m['repo'], m['worktree'])] = m['branch']
    for (repo, worktree), branch in latest.items():
        seen.setdefault(repo, {'at': set(), 'files': set(), 'merges': set()})['at'].add((worktree, branch))
    for t in rec.get('touches', []):
        if now - t['ts'] <= ttl:
            seen.setdefault(t['repo'], {'at': set(), 'files': set(), 'merges': set()})['files'].add(t['path'])
    for e in rec.get('git_events', []):
        if e['op'] == 'merge' and now - e['ts'] <= window and e.get('target'):
            seen.setdefault(e['repo'], {'at': set(), 'files': set(), 'merges': set()})['merges'].add(e['target'])
    return seen


def collisions(me, other, now, ttl=TTL_S, window=MERGE_WINDOW_S):
    """{repo: {'rules', 'files', 'targets', 'theirs', 'shared'}} where `me` and `other` collide.

    Only ③ the same repository-relative file and ④ the same merge target count; `theirs` (the other side's
    (worktree, branch) in that repository) and `shared` (worktrees both are on) are background for the notice."""
    mine, theirs = _footprint(me, now, ttl, window), _footprint(other, now, ttl, window)
    hits = {}
    for repo in mine.keys() & theirs.keys():
        a, b = mine[repo], theirs[repo]
        files, targets = a['files'] & b['files'], a['merges'] & b['merges']
        rules = {name for name, hit in (('file', files), ('merge', targets)) if hit}
        if rules:
            hits[repo] = {'rules': rules, 'files': files, 'targets': targets, 'theirs': b['at'],
                          'shared': {w for w, _ in a['at']} & {w for w, _ in b['at']}}
    return hits


def _key(rec):
    return f"{rec.get('agent')}:{rec.get('session_id')}"


def _start(rec, repo, now, ttl):
    """When `rec` started on `repo`: its first edit there (kept across re-edits, reset after a quiet TTL);
    a session with no live edit there (a merge-only collision) counts from its first git action there."""
    fresh = [t['ts'] for t in rec.get('touches', []) if t['repo'] == repo and now - t['ts'] <= ttl]
    if fresh:
        since = (rec.get('since') or {}).get(repo)
        return since if since is not None else min(fresh)  # records written before `since` existed: oldest edit
    events = [e['ts'] for e in rec.get('git_events', []) if e['repo'] == repo and now - e['ts'] <= ttl]
    return min(events) if events else float(rec.get('updated_at') or now)


def first_of(a, b, repo, now, ttl=TTL_S):
    """The record that came first to `repo`; equal start times go to the smaller session_id."""
    order = sorted((a, b), key=lambda r: (_start(r, repo, now, ttl), str(r.get('session_id')), _key(r)))
    return order[0]


def _shown(path):
    home = os.path.expanduser('~')
    return '~' + path[len(home):] if path.startswith(home + os.sep) else path


def _who(rec):
    who = f"{rec.get('agent', '?')} 会话「{rec.get('name') or rec.get('session_id')}」"
    return who + (f"（{rec['page']} 页）" if rec.get('page') else '')


def _names(items, cap=5):
    items = sorted(items)
    return '、'.join(items[:cap]) + (f" 等 {len(items)} 个" if len(items) > cap else '')


def notice_text(other, repo, hit, now, *, first, since=None):
    """The collision notice for one side. `first`: this side came first (it keeps going); otherwise it waits.
    `since`: when the other side started on the repository (the later side is told how long ago)."""
    name, files, targets = repo_name(repo), hit['files'], ', '.join(sorted(hit['targets']))
    background = []
    for worktree, branch in sorted(hit['theirs']):
        where = ('同工作树 ' if worktree in hit['shared'] else '对方在工作树 ') + _shown(worktree)
        background.append(where + (f"（分支 {branch}）" if branch else ''))
    tail = (f"背景：{'；'.join(background)}。" if background else '')
    goal = other.get('goal')
    tail += f"对方目标：{'「' + goal + '」' if goal else '未登记'}。"
    if first:
        what = (f"{name} 的 {_names(files)}" + (f"（双方都在往 {targets} 合并）" if targets else '') if files
                else f"{name} 的合并目标 {targets}（都在往 {targets} 合并）")
        return (f"⚠ 撞车：{_who(other)}也碰了 {what}。你先开始，由你继续；对方会等你完成。"
                f"完成后监工会协调对方接着做。{tail}")
    minutes = max(0, int((now - (now if since is None else since)) // 60))
    what = (f"先开始动 {name} 的 {_names(files)}" + (f"（双方都在往 {targets} 合并）" if targets else '') if files
            else f"先开始往 {name} 的 {targets} 合并")
    hold = ('你先别再改这些文件' + (f"、也先别往 {targets} 合并" if targets else '') if files
            else f"你先别往 {targets} 合并")
    return (f"⚠ 撞车：{_who(other)}{what}（{minutes} 分钟前），按先到先得由它继续。"
            f"{hold}，可以做别的；等它完成，由监工协调后再继续。{tail}")


def _new_notices(rec, others, now, ttl, window):
    notified = rec.setdefault('notified', {})
    pairs = rec.setdefault('collisions', {})
    out = []
    for other in sorted(others, key=lambda o: str(o.get('session_id'))):
        for repo, hit in sorted(collisions(rec, other, now, ttl, window).items()):
            key = f"{_key(other)}|{repo}"
            pair = pairs.get(key)
            if not pair or now - pair['ts'] > ttl:  # a new collision (or the same two again after a quiet TTL)
                pair = {'repo': repo, 'other': _key(other), 'first': _key(first_of(rec, other, repo, now, ttl)),
                        'files': [], 'targets': [], 'at': now}
                notified.pop(key, None)
            pair.update(files=sorted(set(pair['files']) | hit['files']),
                        targets=sorted(set(pair['targets']) | hit['targets']), ts=now)
            pairs[key] = pair
            told = notified.get(key) or {'rules': [], 'files': []}
            if hit['rules'] <= set(told['rules']) and hit['files'] <= set(told['files']):
                continue
            notified[key] = {'rules': sorted(hit['rules'] | set(told['rules'])),
                             'files': sorted(hit['files'] | set(told['files']))}
            first = pair['first'] == _key(rec)
            out.append(notice_text(other, repo, hit, now, first=first,
                                   since=None if first else _start(other, repo, now, ttl)))
    if len(pairs) > MAX_COLLISIONS:
        for key in sorted(pairs, key=lambda k: pairs[k]['ts'])[:len(pairs) - MAX_COLLISIONS]:
            del pairs[key]
    return out


def note_text(item, now):
    minutes = max(0, int((now - float(item.get('ts') or now)) // 60))
    return f"【进度板留言】来自 {item.get('from') or '?'}（{minutes} 分钟前）：{item.get('text', '')}"


# ------------------------------------------------------------------ one event
def handle(agent, ev, now=None, deliver=True):
    """Apply one session event to the board and return the new notices for that session: notes left for it
    (`note`, only when `deliver`: the caller shows the result) first, then new collisions.

    ev: {event: start|prompt|tool|check|end, session_id, cwd, [name], [pid], [page], [prompt], [path], [command]}."""
    sid = ev.get('session_id')
    if not isinstance(sid, str) or not sid:
        return []
    now = time.time() if now is None else now
    ttl = _seconds('FLEETMUX_BOARD_TTL_S', TTL_S)
    window = _seconds('FLEETMUX_BOARD_MERGE_WINDOW_S', MERGE_WINDOW_S)
    cwd = ev.get('cwd') if isinstance(ev.get('cwd'), str) and ev.get('cwd') else os.getcwd()
    board = board_dir()
    os.makedirs(board, exist_ok=True)
    path = _record_path(board, agent, sid)
    event = ev.get('event')
    with _locked(path):
        rec = _load(path) or {'session_id': sid, 'agent': agent, 'goal': '', 'ended_at': None,
                              'touches': [], 'git_events': [], 'notified': {}}
        rec['cwd'] = cwd
        rec['updated_at'] = now
        rec['name'] = (ev.get('name') if isinstance(ev.get('name'), str) and ev.get('name') else None) \
            or rec.get('name') or f"{os.path.basename(cwd.rstrip('/')) or cwd}#{sid[:8]}"
        if isinstance(ev.get('pid'), int):
            rec['pid'] = ev['pid']
        page = ev.get('page') or os.environ.get('FLEETMUX_PAGE')
        if page:
            rec['page'] = str(page)
        notices = []
        if event == 'end':
            rec['ended_at'] = now
        else:
            if event == 'start':
                rec['ended_at'] = None
            if event == 'prompt' and isinstance(ev.get('prompt'), str) and not _machine_prompt(ev['prompt']):
                rec['goal'] = _goal(ev['prompt'])
            if event == 'tool':
                if isinstance(ev.get('path'), str) and ev['path']:
                    _add_touch(rec, ev['path'], cwd, now, ttl)
                if isinstance(ev.get('command'), str) and ev['command']:
                    _add_git(rec, ev['command'], cwd, now)
            if deliver:
                for item in rec.get('inbox') or []:
                    if not item.get('delivered_at'):
                        notices.append(note_text(item, now))
                        item['delivered_at'] = now
            notices += _new_notices(rec, _others(board, path, now, ttl), now, ttl, window)
        _save(path, rec)
    return notices


# ------------------------------------------------------------------ list / note (the fleet supervisor's side)
def _finished(rec, repo, now, ttl):
    """(done, since when) for the first side of a collision: ended, process gone, or no edit / git action in
    `repo` for a TTL (then it finished when that TTL ran out)."""
    if rec is None:
        return True, 0.0
    if rec.get('ended_at'):
        return True, float(rec['ended_at'])
    updated = float(rec.get('updated_at') or 0)
    if not online(rec, now, ttl) and now - updated <= ttl:
        return True, updated  # the process is gone
    marks = [m['ts'] for m in rec.get('touches', []) + rec.get('git_events', []) if m['repo'] == repo]
    last = min(max(marks), updated) if marks else updated
    if now - last > ttl:
        return True, last + ttl
    return False, None


def _brief(rec, key):
    agent, _, sid = key.partition(':')
    return {'agent': agent, 'session_id': sid, 'name': (rec or {}).get('name') or sid}


def board_state(now=None):
    """Read-only view of the board: online sessions, collisions whose later side is still online, and the
    pending ones (first side done, later side not released by a note since)."""
    now = time.time() if now is None else now
    ttl = _seconds('FLEETMUX_BOARD_TTL_S', TTL_S)
    board = board_dir()
    live = {}
    try:
        entries = list(os.scandir(board))
    except OSError:
        entries = []
    for entry in entries:
        if not entry.name.endswith('.json') or entry.name.startswith('.'):
            continue
        try:
            if entry.stat().st_mtime < now - ttl - 60:
                continue
        except OSError:
            continue
        rec = _load(entry.path)
        if rec and online(rec, now, ttl):
            live[_key(rec)] = rec
    sessions = [{'agent': r.get('agent'), 'session_id': r.get('session_id'), 'name': r.get('name'),
                 'goal': r.get('goal') or '', 'cwd': r.get('cwd'), 'page': r.get('page'),
                 'updated_at': r.get('updated_at')}
                for r in sorted(live.values(), key=lambda r: -float(r.get('updated_at') or 0))]
    pairs = {}
    for key, rec in live.items():
        for c in (rec.get('collisions') or {}).values():
            ident = (c['repo'], frozenset((key, c['other'])))
            pair = pairs.setdefault(ident, {'repo': c['repo'], 'first': c['first'], 'at': c['at'], 'files': set(),
                                            'targets': set(), 'views': set()})
            if c['at'] < pair['at']:
                pair.update(first=c['first'], at=c['at'])
            pair['files'] |= set(c['files'])
            pair['targets'] |= set(c['targets'])
            pair['views'].add(key)
    out = []
    for (repo, both), pair in sorted(pairs.items(), key=lambda kv: kv[1]['at']):
        first_key = pair['first']
        later_key = next(iter(both - {first_key}), None)
        later = live.get(later_key)
        if later is None:
            continue  # nobody is waiting any more
        first = live.get(first_key)
        if first is None:
            agent, _, sid = first_key.partition(':')
            first = _load(_record_path(board, agent, sid))
        done, finished_at = _finished(first, repo, now, ttl)
        if done and later_key not in pair['views']:
            continue  # the later side never heard of it: nothing to release
        released = ([{'from': n.get('from'), 'ts': n.get('ts'), 'text': n.get('text')}
                     for n in later.get('inbox') or [] if float(n.get('ts') or 0) >= finished_at] if done else [])
        if released and now - max(float(r['ts'] or 0) for r in released) > ttl:
            continue  # released long ago: settled
        out.append({'repo': repo, 'repo_name': repo_name(repo), 'files': sorted(pair['files']),
                    'targets': sorted(pair['targets']), 'first': _brief(first, first_key),
                    'later': _brief(later, later_key), 'first_done': done, 'released': released,
                    'pending': done and not released})
    return {'sessions': sessions, 'collisions': out, 'pending': [p for p in out if p['pending']]}


def _ago(ts, now):
    return f"{max(0, int((now - float(ts or now)) // 60))} 分钟前"


def board_text(state, now=None):
    now = time.time() if now is None else now

    def who(b):
        return f"{b['agent']}「{b['name']}」（{b['agent']}:{b['session_id']}）"

    def what(p):
        return '、'.join(p['files']) + ('；' if p['files'] and p['targets'] else '') + \
            ('都在往 ' + ', '.join(p['targets']) + ' 合并' if p['targets'] else '')

    lines = [f"在线会话（{len(state['sessions'])}）：" if state['sessions'] else '在线会话：无']
    for s in state['sessions']:
        page = f"（{s['page']} 页）" if s.get('page') else ''
        goal = f"目标「{s['goal']}」" if s['goal'] else '目标未登记'
        lines.append(f"  - {s['agent']}「{s['name']}」{page}（{s['agent']}:{s['session_id']}）{goal}，"
                     f"{_ago(s['updated_at'], now)}更新")
    lines.append(f"撞车（{len(state['collisions'])}）：" if state['collisions'] else '撞车：无')
    for p in state['collisions']:
        status = '先到者已完成' if p['first_done'] else '先到者进行中'
        if p['released']:
            last = p['released'][-1]
            status += f"，已由 {last['from']} 放行（{_ago(last['ts'], now)}）"
        elif p['first_done']:
            status += '，未放行'
        lines.append(f"  - {p['repo_name']}：{what(p)} ｜ 先到 {who(p['first'])} → 后到 {who(p['later'])} ｜ {status}")
    lines.append(f"待协调（{len(state['pending'])}）：" if state['pending'] else '待协调：无')
    for p in state['pending']:
        lines.append(f"  - {p['repo_name']}：{what(p)} ｜ 先到 {who(p['first'])} 已完成，后到 {who(p['later'])} "
                     f"还没收到放行 → note {p['later']['session_id']} \"<放行，必要时附协调指示>\"")
    return '\n'.join(lines)


def note(target, text, sender='supervisor', now=None):
    """Append a note to one session's inbox; that session's next hook / check delivers it once.
    `target`: session_id, agent:session_id or name; a name on several sessions must be told apart by id."""
    now = time.time() if now is None else now
    ttl = _seconds('FLEETMUX_BOARD_TTL_S', TTL_S)
    board = board_dir()
    by_id, by_name = [], []
    try:
        entries = list(os.scandir(board))
    except OSError:
        entries = []
    for entry in entries:
        if not entry.name.endswith('.json') or entry.name.startswith('.'):
            continue
        rec = _load(entry.path)
        if not rec:
            continue
        if target in (rec.get('session_id'), _key(rec)):
            by_id.append((entry.path, rec))
        elif target == rec.get('name'):
            by_name.append((entry.path, rec))
    found = by_id or by_name
    if len(found) > 1:
        found = [f for f in found if online(f[1], now, ttl)] or found
    if not found:
        raise LookupError(f"no session {target!r} on the board ({board})")
    if len(found) > 1:
        raise LookupError(f"{target!r} matches several sessions, give one id: " +
                          ', '.join(f"{_key(r)}（{r.get('name')}）" for _, r in found))
    path = found[0][0]
    with _locked(path, wait=5):
        rec = _load(path) or found[0][1]
        rec['inbox'] = ((rec.get('inbox') or []) + [{'from': sender, 'text': text, 'ts': now}])[-MAX_INBOX:]
        _save(path, rec)
    return _key(rec)


# ------------------------------------------------------------------ entry points
CONTEXT_EVENTS = ('SessionStart', 'UserPromptSubmit', 'PostToolUse')
EDIT_TOOLS = ('Edit', 'Write', 'MultiEdit', 'NotebookEdit')


def claude_hook(raw):
    """Claude Code hook JSON (stdin) -> stdout text: hookSpecificOutput.additionalContext, or '' when nothing new."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('hook input is not a JSON object')
    name = data.get('hook_event_name')
    ev = {'session_id': data.get('session_id'), 'cwd': data.get('cwd')}
    if name == 'SessionStart':
        ev['event'] = 'start'
    elif name == 'UserPromptSubmit':
        ev.update(event='prompt', prompt=data.get('prompt'))
    elif name == 'PostToolUse':
        ev['event'] = 'tool'
        tool, tool_input = data.get('tool_name'), data.get('tool_input')
        if isinstance(tool_input, dict) and tool in EDIT_TOOLS:   # Read / Grep / Glob carry paths too: not edits
            ev['path'] = tool_input.get('file_path') or tool_input.get('notebook_path')
        elif isinstance(tool_input, dict) and tool == 'Bash':
            ev['command'] = tool_input.get('command')
    elif name == 'SessionEnd':
        ev['event'] = 'end'
    else:
        ev['event'] = 'check'
    notices = handle('claude', ev, deliver=name in CONTEXT_EVENTS)
    if not notices or name not in CONTEXT_EVENTS:
        return ''
    text = '\n'.join(notices)
    return json.dumps({'systemMessage': text,
                       'hookSpecificOutput': {'hookEventName': name, 'additionalContext': text}}, ensure_ascii=False)


def _log_error(argv):
    try:
        board = board_dir()
        os.makedirs(board, exist_ok=True)
        log = os.path.join(board, 'errors.log')
        if os.path.exists(log) and os.path.getsize(log) > 1_000_000:
            os.replace(log, log + '.1')
        with open(log, 'a') as fh:
            fh.write(time.strftime('%Y-%m-%dT%H:%M:%S ') + ' '.join(argv[:2]) + '\n' + traceback.format_exc() + '\n')
    except Exception:
        pass


USAGE = ("usage: session_board.py hook claude < hook.json | session_board.py hook pi '<json>'\n"
         "       session_board.py list [--json] | session_board.py note <session> <text> [--from <sender>]\n")


def _command(argv):
    """list / note: the supervisor runs these by hand, so errors are shown (stderr, exit 1), not swallowed."""
    import argparse
    parser = argparse.ArgumentParser(prog='session_board.py ' + argv[0])
    if argv[0] == 'list':
        parser.add_argument('--json', action='store_true')
    else:
        parser.add_argument('target')
        parser.add_argument('text', nargs='+')
        parser.add_argument('--from', dest='sender', default='supervisor')
    args = parser.parse_args(argv[1:])
    try:
        if argv[0] == 'list':
            state = board_state()
            sys.stdout.write((json.dumps(state, ensure_ascii=False, indent=1) if args.json else board_text(state)) + '\n')
        else:
            sys.stdout.write(f"noted for {note(args.target, ' '.join(args.text), args.sender)}\n")
    except Exception as exc:
        sys.stderr.write(f"session_board.py {argv[0]}: {exc}\n")
        return 1
    return 0


def main(argv):
    if argv[:1] in (['list'], ['note']):
        return _command(argv)
    if len(argv) < 2 or argv[0] != 'hook' or argv[1] not in ('claude', 'pi'):
        sys.stderr.write(USAGE)
        return 2
    out = ''
    try:
        if argv[1] == 'claude':
            out = claude_hook(sys.stdin.read())
        else:
            ev = json.loads(argv[2] if len(argv) > 2 else '')
            out = '\n'.join(handle('pi', ev)) if isinstance(ev, dict) else ''
    except Exception:
        _log_error(argv)
        out = ''
    if out:
        sys.stdout.write(out + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
