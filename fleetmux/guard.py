"""P8i action gate (brief v2.2 §8.2 P8i; contracts §8 「动作确认」 and 「子 fleet 根确认与祖先链」, O1-O8).

``classify`` / ``action_hash`` / ``action_text`` are pure. ``pretool`` is the ``fleet.py claude-pretool`` hook entry:
a run whose pinned config has neither ``security`` nor ``security_taint`` gets no output and exit 0, and the hook
never opens Redis for it. With a policy every failure is a deny (fail closed), never a traceback.

Tickets and action records are read and written only through ``channels.approve`` (P8k): this module never
touches the ticket or record keys itself.
"""
from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import time
from urllib.parse import urlsplit

from fleetmux.channels import security

# O8 (frozen, safety freeze batch 2): the closed refusal set of verify_ancestry.
REFUSE_REASONS = ('cfg_invalid', 'path_mismatch', 'hop_load_failed', 'parent_unreachable', 'not_delegated',
                  'generation_mismatch', 'task_mismatch', 'link_mismatch', 'parent_mismatch', 'root_mismatch',
                  'depth_exceeded', 'taint_not_inherited', 'internal')
# C8.4 (frozen): deny reasons that only the hook writes (never a Consume / Register / RouteResult value).
HOOK_REASONS = ('post_check', 'policy_refused', 'epoch_malformed')

MATCHER = '.*'                      # A2'-3 / C2: every tool call goes through the hook
ORDER = ('real_money', 'delete', 'secret_read', 'config', 'install', 'send_out')   # U3, same tier
RANK = {'high': 2, 'normal': 1}
READ_ONLY = ('Read', 'Grep', 'Glob', 'LS')   # C2 ④: the only tools an untrusted request may run unconfirmed
HIGH_MODES = ('desk', 'second_channel', 'totp')
TEXT_MAX = 300
WAIT_SLICE_S = 1.0


@dataclass(frozen=True)
class Ancestry:
    outcome: str            # 'root' | 'verified' | 'refused'
    reason: str             # '' unless refused; then one of REFUSE_REASONS
    detail: str
    hops: tuple             # child -> root: ((parent_cfg_path, slot, generation, task_id), ...)
    cfg_paths: tuple
    root_cfg_path: str
    policies: tuple
    taint: object           # dict | None (rule T); None = clean

    def __bool__(self):
        raise TypeError('check Ancestry.outcome explicitly')


# ------------------------------------------------------------------ classify: Bash parsing (survey §5.2, C1)
_SEP = {';', '&', '&&', '||', '|', '|&', '(', ')', '\n', ';;', ';&'}
_REDIR = re.compile(r'^[0-9]*[<>&]*[<>][<>&|]*$')
_ASSIGN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
_DYNAMIC = re.compile(r'\$\(|`|<\(|>\(|<<')
_SHELLS = ('sh', 'bash', 'zsh', 'dash', 'ksh', 'fish')
_INTERPRETERS = ('python', 'python2', 'python3', 'node', 'perl', 'ruby', 'php', 'osascript', 'deno', 'bun')
_PIPE_INTERP = re.compile(r'\|&?\s*(?:sudo\s+)?(?:\S*/)?(?:' + '|'.join(_SHELLS) + r'|python[0-9.]*|node|perl|ruby|php)\b')
_FORCED = re.compile(r'(?:^|[;&|(\n])\s*(?:eval\b|\$)')        # U2: eval / a variable as the command word
_WRAPPERS = {'sudo', 'nohup', 'time', 'command', 'exec', 'nice', 'builtin', 'stdbuf', 'doas'}
_DEVNULL = ('/dev/null',)


def _delete_patterns():
    from fleetmux import watchdog
    return [watchdog._marker_re(x) for x in watchdog.DELETE_MARKERS]


_PATTERNS = []


def _scan(text):
    if not _PATTERNS:
        _PATTERNS.extend(_delete_patterns())
    low = (text or '').lower()
    return any(p.search(low) for p in _PATTERNS)


def _tokens(cmd):
    """shlex tokens with shell punctuation as separate tokens; ValueError on an unbalanced quote."""
    lex = shlex.shlex(cmd, posix=True, punctuation_chars='();<>|&\n')
    lex.whitespace = ' \t\r'
    lex.whitespace_split = True
    lex.commenters = ''
    return list(lex)


class _Seg:
    def __init__(self, argv, targets):
        self.argv = argv          # command word + args, wrappers removed
        self.targets = targets    # redirect targets other than /dev/null


def _strip_wrappers(argv):
    i = 0
    while i < len(argv):
        word = argv[i]
        base = os.path.basename(word)
        if _ASSIGN.match(word):
            i += 1
        elif base == 'env':
            i += 1
            while i < len(argv) and (argv[i].startswith('-') or _ASSIGN.match(argv[i])):
                i += 1
        elif base == 'timeout':
            i += 1
            while i < len(argv) and argv[i].startswith('-'):
                i += 1
            i += 1  # the duration
        elif base == 'nice':
            i += 1
            if i < len(argv) and argv[i] == '-n':
                i += 2
            while i < len(argv) and argv[i].startswith('-'):
                i += 1
        elif base in _WRAPPERS:
            i += 1
            while i < len(argv) and argv[i].startswith('-'):
                i += 1
        else:
            break
    return argv[i:]


def _segments(cmd):
    """Simple commands of ``cmd``; raises ValueError when it cannot be parsed."""
    toks, segs, cur, targets, i = _tokens(cmd), [], [], [], 0
    while i < len(toks):
        tok = toks[i]
        if tok in _SEP:
            segs.append(_Seg(_strip_wrappers(cur), targets))
            cur, targets = [], []
        elif _REDIR.match(tok) and tok not in _SEP:
            if cur and cur[-1].isdigit():
                cur.pop()                      # the fd number of '2>' was split off
            target = toks[i + 1] if i + 1 < len(toks) and toks[i + 1] not in _SEP else ''
            i += 1 if target else 0
            if target and target not in _DEVNULL and not target.lstrip('-').isdigit():
                targets.append(target)
        else:
            cur.append(tok)
        i += 1
    segs.append(_Seg(_strip_wrappers(cur), targets))
    return [s for s in segs if s.argv or s.targets]


def _inline_code(argv):
    """(kind, code) for ``bash -c '<code>'`` ('shell') or ``python -c|-e '<code>'`` ('interp'), else None."""
    base = os.path.basename(argv[0])
    flags = ('-c',) if base in _SHELLS else ('-c', '-e', '--eval')
    if base in _SHELLS or re.fullmatch(r'python[0-9.]*', base) or base in _INTERPRETERS:
        for j, a in enumerate(argv[1:], 1):
            if a in flags or (base in _SHELLS and re.fullmatch(r'-[a-z]*c', a)):
                if j + 1 < len(argv):
                    return ('shell' if base in _SHELLS else 'interp'), argv[j + 1]
    return None


def _data_free(argv):
    """argv without the pure-data positions: echo / printf arguments, git commit / tag -m <msg>."""
    base = os.path.basename(argv[0])
    if base in ('echo', 'printf'):
        return argv[:1]
    if base == 'git' and len(argv) > 1 and argv[1] in ('commit', 'tag'):
        out, skip = [], False
        for a in argv:
            if skip:
                skip = False
            elif a in ('-m', '--message'):
                skip = True
            elif a.startswith('--message=') or (a.startswith('-m') and len(a) > 2 and not a.startswith('--')):
                continue
            else:
                out.append(a)
        return out
    return argv


def _delete_hit(cmd, depth=0):
    if depth > 4 or _DYNAMIC.search(cmd) or _PIPE_INTERP.search(cmd) or re.search(r'\bxargs\b', cmd):
        return _scan(cmd) or bool(_FORCED.search(cmd))
    try:
        segs = _segments(cmd)
    except ValueError:
        return _scan(cmd) or bool(_FORCED.search(cmd))
    for seg in segs:
        if any(_scan(t) for t in seg.targets):
            return True
        if not seg.argv:
            continue
        word = seg.argv[0]
        if word.startswith('$') or word == 'eval':
            return True                                    # U2: cannot see what runs
        if word in ('source', '.'):
            if _scan(' '.join(seg.argv)):
                return True
            continue
        inline = _inline_code(seg.argv)
        if inline:
            kind, code = inline
            if (_delete_hit(code, depth + 1) if kind == 'shell' else _scan(code)):
                return True
            continue
        if _scan(' '.join(_data_free(seg.argv))):
            return True
    return False


def _flat_segments(cmd, depth=0):
    """Every simple command, ``bash -c`` code included; on a parse failure a whitespace split per separator."""
    try:
        segs = _segments(cmd)
    except ValueError:
        segs = [_Seg(_strip_wrappers(part.split()), []) for part in re.split(r'[;&|\n()]+', cmd)]
        return [s for s in segs if s.argv]
    out = []
    for seg in segs:
        inline = seg.argv and _inline_code(seg.argv)
        if inline and inline[0] == 'shell' and depth < 4:
            out.extend(_flat_segments(inline[1], depth + 1))
        out.append(seg)
    return out


# ------------------------------------------------------------------ classify: paths
def _home():
    return str(Path.home())


def _real(path, cwd=None):
    p = os.path.expanduser(str(path))
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    return os.path.realpath(p)


def _under(path, root):
    root = os.path.realpath(os.path.expanduser(str(root)))
    return path == root or path.startswith(root.rstrip('/') + '/')


def _config_dir():
    from fleetmux import channels
    return channels.config_dir()


def _is_config_path(path, cfg):
    p, home = _real(path), _home()
    rel = os.path.relpath(p, os.path.realpath(home))
    first = rel.split('/', 1)[0]
    if not rel.startswith('..'):
        if first.startswith('.claude') or first in ('.codex', '.config'):
            return True
        if first in ('.zshrc', '.zshenv', '.zprofile', '.zlogin', '.zlogout', '.bashrc', '.bash_profile',
                     '.bash_login', '.bash_logout', '.profile', '.kshrc', '.inputrc'):
            return True
        if rel == 'Library/LaunchAgents' or rel.startswith('Library/LaunchAgents/'):
            return True
    if os.path.basename(p).startswith('.fleet-settings-'):
        return True
    for key in ('roster', 'roster_night'):
        if isinstance(cfg, dict) and cfg.get(key) and p == _real(cfg[key]):
            return True
    if _under(p, _config_dir()):
        return True
    from fleetmux import paths
    if _under(p, paths.root() / 'launchers.env'):   # was under ~/.config until §50.1 moved it
        return True
    run = isinstance(cfg, dict) and cfg.get('run')
    if run and os.path.basename(p) in ('config.json', 'subfleet.json') and _under(p, run):
        return True                                       # run-tree config (round 11, limited mitigation)
    return False


_SECRET_NAMES = ('*.pem', '*.key', '*id_rsa*', '*id_ed25519*', '*id_ecdsa*', '*secret*', '*.env', '.env.*',
                 '*.p12', '*.pfx')


def _is_secret_path(path):
    p, home = _real(path), os.path.realpath(_home())
    name = os.path.basename(p).lower()
    if any(fnmatch.fnmatch(name, pat) for pat in _SECRET_NAMES):
        return True
    for sub in ('.ssh', '.gnupg', '.aws', '.netrc', '.claude.json', '.codex/auth.json'):
        if _under(p, os.path.join(home, sub)):
            return True
    return _under(p, _config_dir())


_PATHISH = re.compile(r"[~\w./*@+-]*[./~][~\w./*@+-]*")
_READERS = {'cat', 'less', 'more', 'head', 'tail', 'sed', 'awk', 'gawk', 'grep', 'egrep', 'fgrep', 'rg', 'strings',
            'xxd', 'od', 'hexdump', 'base64', 'cp', 'scp', 'rsync', 'tar', 'zip', 'gzip', 'openssl', 'nl', 'bat',
            'view', 'vim', 'vi', 'nano', 'source', '.', 'tac', 'cut', 'sort', 'uniq', 'wc', 'diff', 'cmp', 'jq',
            'yq', 'pbcopy', 'curl', 'wget', 'gpg', 'ssh-keygen'}
_WRITERS = {'cp', 'mv', 'ln', 'install', 'tee', 'touch', 'truncate', 'chmod', 'chown', 'rsync', 'dd'}

_LOCAL = re.compile(r'^(localhost|127(?:\.\d+){0,3}|0\.0\.0\.0|::1|\[::1\])$', re.I)
_NET = {'curl', 'wget', 'nc', 'ncat', 'netcat', 'telnet', 'socat', 'ftp', 'sftp', 'ssh', 'scp', 'rsync', 'http',
        'https', 'xh', 'aria2c'}
_CURL_ARG = {'-H', '--header', '-o', '--output', '-d', '--data', '--data-raw', '--data-binary', '--data-urlencode',
             '-X', '--request', '-u', '--user', '-A', '--user-agent', '-e', '--referer', '-F', '--form', '-T',
             '--upload-file', '-b', '--cookie', '-c', '--cookie-jar', '-O', '--output-document', '-P', '-m',
             '--max-time', '--connect-timeout', '-w', '--write-out', '-K', '--config', '--unix-socket', '-i',
             '-p', '-l', '-F', '-o', '-E', '--cert', '--key', '--cacert', '-x', '--proxy'}
_INSTALLERS = {'pip', 'pip3', 'uv', 'npm', 'pnpm', 'yarn', 'brew', 'apt', 'apt-get', 'cargo', 'gem', 'pipx',
               'poetry', 'bun', 'go', 'conda', 'mamba', 'port', 'dnf', 'yum', 'apk', 'pacman'}
_INSTALL_VERBS = {'install', 'add', 'i', 'get', '-S'}


def _host_of(arg):
    arg = arg.strip()
    if not arg or arg.startswith('-'):
        return None
    if '://' in arg:
        try:
            return urlsplit(arg).hostname or ''
        except ValueError:
            return ''
    m = re.match(r'^(?:[^@/\s]+@)?(\[[0-9a-fA-F:]+\]|[A-Za-z0-9._-]+)(?::|/|$)', arg)
    return m.group(1).strip('[]') if m else None


def _nonlocal(host):
    return bool(host) and not _LOCAL.match(host)


def _send_out_seg(argv):
    base = os.path.basename(argv[0])
    args = argv[1:]
    if base == 'git' and 'push' in args[:3]:
        return True
    if base in ('mail', 'mailx', 'sendmail', 'mutt', 'msmtp'):
        return True
    if base == 'gh' and len(args) >= 2 and args[0] in ('pr', 'release', 'gist') and 'create' in args[1:3]:
        return True
    if base not in _NET:
        return False
    rest, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in _CURL_ARG and base in ('curl', 'wget', 'http', 'https', 'xh'):
            skip = True
        elif not a.startswith('-'):
            rest.append(a)
    if base in ('scp', 'rsync'):
        return any(_nonlocal(_host_of(a)) for a in rest if ':' in a or '@' in a)
    if base in ('ssh', 'sftp', 'ftp', 'telnet', 'nc', 'ncat', 'netcat', 'socat'):
        return bool(rest) and _nonlocal(_host_of(rest[0]))
    return any(_nonlocal(_host_of(a)) for a in rest)


def _install_seg(argv):
    base = os.path.basename(argv[0])
    args = argv[1:]
    if re.fullmatch(r'python[0-9.]*', base) and '-m' in args:
        j = args.index('-m')
        return j + 1 < len(args) and args[j + 1] in ('pip', 'pip3', 'ensurepip') and 'install' in args[j + 2:]
    return base in _INSTALLERS and any(a in _INSTALL_VERBS for a in args[:3])


def _write_targets(seg):
    argv, out = seg.argv, list(seg.targets)
    if not argv:
        return out
    base = os.path.basename(argv[0])
    args = [a for a in argv[1:] if not a.startswith('-')]
    if base in ('sed', 'perl') and any(a == '-i' or a.startswith('-i') or a == '--in-place' for a in argv[1:]):
        out.extend(args)
    elif base == 'dd':
        out.extend(a[3:] for a in argv[1:] if a.startswith('of='))
    elif base in ('cp', 'mv', 'ln', 'install', 'rsync') and args:
        out.append(args[-1])
    elif base in _WRITERS:
        out.extend(args)
    return out


def _bash_classes(cmd, cfg):
    hits = set()
    if _delete_hit(cmd):
        hits.add('delete')
    segs = _flat_segments(cmd)
    dynamic = bool(_DYNAMIC.search(cmd) or _PIPE_INTERP.search(cmd))
    for seg in segs:
        if not seg.argv and not seg.targets:
            continue
        if seg.argv and _send_out_seg(seg.argv):
            hits.add('send_out')
        if seg.argv and _install_seg(seg.argv):
            hits.add('install')
        if any(_is_config_path(t, cfg) for t in _write_targets(seg) if t):
            hits.add('config')
        if seg.argv:
            base = os.path.basename(seg.argv[0])
            inline = _inline_code(seg.argv)
            words = []
            if base in _READERS:
                words = [a for a in seg.argv[1:] if not a.startswith('-')]
            if inline:
                words += _PATHISH.findall(inline[1])
            if any(_is_secret_path(w) for w in words if w):
                hits.add('secret_read')
    if dynamic:                                            # cannot see what runs: every path-like word counts
        words = _PATHISH.findall(cmd)
        if any(_is_secret_path(w) for w in words):
            hits.add('secret_read')
    if re.search(r'\b(?:curl|wget)\b[^|]*\|&?\s*(?:sudo\s+)?(?:\S*/)?(?:' + '|'.join(_SHELLS) + r')\b', cmd):
        hits.add('install')
    sock = isinstance(cfg, dict) and cfg.get('redis_socket')
    if 'redis-cli' in cmd or 'fx:guard' in cmd or (sock and str(sock) in cmd):
        hits.add('config')                                 # U4: limited mitigation, not forgery protection
    return hits


def _domain_allowed(host, allow):
    host = (host or '').lower().rstrip('.')
    for d in allow or ():
        d = str(d).lower().strip()
        if d.startswith('*.'):
            if host.endswith(d[1:]):
                return True
        elif host == d or host.endswith('.' + d):
            return True
    return False


def _params(tool_input):
    return {k: v for k, v in tool_input.items() if k != 'description'} if isinstance(tool_input, dict) else tool_input


def _canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str)


def _paths_of(tool_input):
    if not isinstance(tool_input, dict):
        return []
    out = [tool_input.get(k) for k in ('file_path', 'notebook_path', 'path') if isinstance(tool_input.get(k), str)]
    for e in tool_input.get('edits') or ():
        if isinstance(e, dict) and isinstance(e.get('file_path'), str):
            out.append(e['file_path'])
    return out


def classify(tool_name, tool_input, policy, cfg):
    """(class, tier) of one tool call, or None (C1 matrix, U3 order, U4 / config mitigation)."""
    from fleetmux.channels import canary
    if canary.check_read(tool_name, tool_input, cfg):  # P8h: a pure read of the canary passes, audited in _decide
        return None
    return choose(hits(tool_name, tool_input, policy, cfg), policy)


def choose(found, policy):
    """U3: drop classes whose tier is 'off', keep the strictest tier, then the class order. (class, tier) | None."""
    pol = policy or security.DEFAULTS
    tiers = (pol.get('confirm') or {}).get('classes') or {}
    live = [c for c in found if RANK.get(tiers.get(c, 'off'))]
    if not live:
        return None
    best = max(RANK[tiers[c]] for c in live)
    cls = next((c for c in ORDER if c in live and RANK[tiers[c]] == best), None) \
        or sorted(c for c in live if RANK[tiers[c]] == best)[0]
    return cls, tiers[cls]


def hits(tool_name, tool_input, policy, cfg):
    """Every class one tool call matches, before the tier choice (also used by watchdog.classify_dialog, P8j)."""
    pol = policy or security.DEFAULTS
    confirm = pol.get('confirm') or {}
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    found = set()
    if tool_name == 'Bash':
        cmd = tool_input.get('command')
        cmd = cmd if isinstance(cmd, str) else _canon(tool_input)
        found |= _bash_classes(cmd, cfg)
        text = cmd
    else:
        text = _canon(_params(tool_input))
        paths = _paths_of(tool_input)
        if tool_name in ('Edit', 'Write', 'NotebookEdit', 'MultiEdit'):
            if any(_is_config_path(p, cfg) for p in paths):
                found.add('config')
        if tool_name in ('Read', 'NotebookRead', 'Grep', 'Glob'):
            if any(_is_secret_path(p) for p in paths):
                found.add('secret_read')
        if tool_name == 'WebFetch':
            url = tool_input.get('url') or ''
            host = _host_of(url) if isinstance(url, str) else ''
            allow = (pol.get('sandbox') or {}).get('allow_domains') or []
            if not _domain_allowed(host, allow):
                found.add('send_out')
    for cls, pats in (confirm.get('patterns') or {}).items():  # real_money only from here; others appended
        for pat in pats or ():
            try:
                matched = re.search(pat, text)
            except re.error:
                matched = str(pat) in text
            if matched:
                found.add(cls)
                break
    return found


# ------------------------------------------------------------------ hash and text
def _norm_input(tool_name, tool_input):
    data = dict(_params(tool_input)) if isinstance(tool_input, dict) else {'_': tool_input}
    if tool_name == 'Bash' and isinstance(data.get('command'), str):
        try:
            data['command'] = shlex.split(data['command'])
        except ValueError:
            data['command'] = ' '.join(data['command'].split())
    return data


def action_hash(slot, generation, tool_name, tool_input):
    """sha256(slot, generation, tool_name, normalised parameters)[:32]; the model's ``description`` is not a
    parameter. Computed over the real (unredacted) text (C13)."""
    raw = '\0'.join((str(slot), str(generation), str(tool_name), _canon(_norm_input(tool_name, tool_input))))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def action_text(tool_name, tool_input):
    """What the card and the audit show: built from the real parameters (never the model's description),
    redacted (C13), at most 300 characters plus '...'."""
    from fleetmux import channels
    data = _params(tool_input) if isinstance(tool_input, dict) else {}
    if tool_name == 'Bash' and isinstance(data.get('command'), str):
        text = data['command']
    elif tool_name == 'WebFetch' and isinstance(data.get('url'), str):
        text = 'WebFetch ' + data['url']
    elif _paths_of(data) and tool_name in ('Edit', 'Write', 'NotebookEdit', 'MultiEdit', 'Read', 'NotebookRead'):
        text = tool_name + ' ' + ', '.join(_paths_of(data))
    else:
        text = str(tool_name) + ' ' + _canon(data)
    text = channels.redact(text)
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX] + '...'


# ------------------------------------------------------------------ hook registration (claude_argv)
def has_pretool_gate(cfg, slot):
    """X1 / U8: True only for a slot whose CLI is Claude (the only CLI with a verified pre-execution gate)."""
    if not isinstance(cfg, dict):
        return False
    if slot == 'advisor':
        return True
    profile = (cfg.get('worker_profiles') or {}).get(slot) if isinstance(cfg.get('worker_profiles'), dict) else None
    if isinstance(profile, dict) and profile.get('cli'):
        return profile['cli'] == 'claude'
    key = 'supervisor_cli' if slot == 'supervisor' else 'worker_cli'
    return (cfg.get(key) or 'codex') == 'claude'


def gated(cfg):
    """The pinned config asks for the PreToolUse gate: a taint (even without a policy), or a policy with at
    least one confirm class not 'off'."""
    if not isinstance(cfg, dict):
        return False
    if 'security_taint' in cfg:
        return True
    pol = security.policy(cfg)
    if pol is None:
        return False
    classes = (pol.get('confirm') or {}).get('classes') or {}
    return any(v != 'off' for v in classes.values())


def pretool_hooks(cfg, cfg_path, name, generation, python, fleet_py):
    """``hooks['PreToolUse']`` for claude_argv, or None (no gate: settings unchanged)."""
    if not gated(cfg):
        return None
    pol = security.policy(cfg) or security.DEFAULTS
    wait_s = (pol.get('confirm') or {}).get('wait_s', security.DEFAULTS['confirm']['wait_s'])
    command = shlex.join([python, str(fleet_py), 'claude-pretool', str(cfg_path), name, str(generation)])
    return [{'matcher': MATCHER, 'hooks': [{'type': 'command', 'timeout': int(wait_s) + 30, 'onFailure': 'block',
                                            'command': command}]}]


UNKNOWN_TAINT = {'kinds': ['unknown'], 'root_request': '?'}


def normalize_taint(value):
    """Rule T: a mapping whose ``kinds`` is a non-empty list of strings, else the unknown taint."""
    kinds = value.get('kinds') if isinstance(value, dict) else None
    if not (isinstance(kinds, list) and kinds and all(isinstance(k, str) for k in kinds)):
        return dict(UNKNOWN_TAINT, kinds=['unknown'])
    root_request = value.get('root_request')
    return {'kinds': list(kinds), 'root_request': root_request if isinstance(root_request, str) else '?'}


def _rule_t(cfg):
    return normalize_taint(cfg['security_taint']) if 'security_taint' in cfg else None


class _Refused(Exception):
    def __init__(self, reason, detail=''):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def _refused(reason, detail=''):
    return Ancestry('refused', reason, detail or reason, (), (), '', (), None)


def _own_path(cfg):
    return os.path.realpath(os.path.join(str(cfg['run']), 'config.json'))


def codex_hook_argv(cfg, cfg_path, name, generation, python, fleet_py):
    """P8j / U8: the Codex ``-c`` flags that run the same ``claude-pretool`` entry as a PreToolUse hook, or [].
    Only with a policy whose ``sandbox.codex_hooks`` is 'on' (default 'off') and a Codex binary that the probe says
    understands ``features.hooks``; a Codex slot still never counts as gated (has_pretool_gate)."""
    pol = security.policy(cfg)
    if pol is None or (pol.get('sandbox') or {}).get('codex_hooks') != 'on':
        return []
    from fleetmux import sandbox
    try:
        if sandbox.codex_capability(cfg, 'hooks') is not True:
            return []
    except Exception:  # noqa: BLE001 — an unknown capability is unsupported
        return []
    wait_s = (pol.get('confirm') or {}).get('wait_s', security.DEFAULTS['confirm']['wait_s'])
    command = shlex.join([python, str(fleet_py), 'claude-pretool', str(cfg_path), name, str(generation)])
    hook = ('[{matcher = ' + json.dumps(MATCHER) + ', hooks = [{type = "command", command = ' + json.dumps(command)
            + ', timeout = ' + str(int(wait_s) + 30) + '}]}]')
    return ['-c', 'features.hooks=true', '-c', 'hooks.PreToolUse=' + hook]


def verify_ancestry(cfg, *, cfg_path=None, connect=None, load=None):
    """O8: the single ancestor-chain check (contracts §8 「子 fleet 根确认与祖先链」). Never writes, never raises."""
    try:
        return _verify_ancestry(cfg, cfg_path, connect, load)
    except _Refused as exc:
        return _refused(exc.reason, exc.detail)
    except Exception as exc:  # noqa: BLE001 — every other failure is 'internal', never propagated
        return _refused('internal', type(exc).__name__)


def _verify_ancestry(cfg, cfg_path, connect, load):
    from fleetmux import subfleet
    if connect is None or load is None:
        from fleetmux import fleet as fleet_mod
        connect = connect or fleet_mod.connect
        load = load or fleet_mod.load
    if not isinstance(cfg, dict) or not cfg.get('run'):
        raise _Refused('cfg_invalid', 'config has no run')
    if bool(cfg.get('parent_config')) != bool(cfg.get('root_config')):
        raise _Refused('cfg_invalid', 'only one of parent_config / root_config')
    own = _own_path(cfg)
    if cfg_path is not None and os.path.realpath(str(cfg_path)) != own:
        raise _Refused('path_mismatch', 'config path is not <run>/config.json')
    if not cfg.get('parent_config'):
        return Ancestry('root', '', '', (), (own,), own, (security.policy(cfg),), _rule_t(cfg))
    if not os.path.exists(own):
        raise _Refused('path_mismatch', 'child <run>/config.json does not exist')
    start_root = os.path.realpath(str(cfg['root_config']))
    hops, paths, policies, seen = [], [own], [security.policy(cfg)], {own}
    child, child_path = cfg, own
    max_hops = subfleet.LIMITS['max_depth'] + 1
    while child.get('parent_config'):
        adopt = child.get('adopt')
        if not (isinstance(adopt, dict) and adopt.get('slot') and 'generation' in adopt
                and isinstance(child.get('task_id'), str) and child.get('root_config')):
            raise _Refused('cfg_invalid', 'adopt / task_id / root_config missing')
        if len(hops) >= max_hops:
            raise _Refused('depth_exceeded', 'too many hops')
        parent_path = os.path.realpath(str(child['parent_config']))
        if parent_path in seen:
            raise _Refused('depth_exceeded', 'cycle')
        try:
            parent = load(parent_path)
        except Exception as exc:  # noqa: BLE001
            raise _Refused('hop_load_failed', type(exc).__name__)
        if not isinstance(parent, dict) or not parent.get('run'):
            raise _Refused('hop_load_failed', 'parent config invalid')
        if _own_path(parent) != parent_path:                                      # L3 back-link
            raise _Refused('parent_mismatch', 'parent_config is not the parent run config')
        info = _parent_agent(connect, parent, str(adopt['slot']))                  # L1
        if info.get('state') != 'delegated':
            raise _Refused('not_delegated', 'parent slot is not delegated')
        try:
            if int(info.get('generation')) != int(adopt['generation']):
                raise _Refused('generation_mismatch', 'parent generation differs')
        except (TypeError, ValueError):
            raise _Refused('generation_mismatch', 'generation is not an integer')
        if info.get('delegated_task') != child['task_id']:
            raise _Refused('task_mismatch', 'delegated_task differs')
        expected = os.path.realpath(os.path.join(str(parent['run']), 'sub', child['task_id'], 'config.json'))
        if (not info.get('delegate_cfg') or os.path.realpath(str(info['delegate_cfg'])) != expected
                or os.path.realpath(str(child['run'])) != os.path.dirname(expected) or child_path != expected):
            raise _Refused('link_mismatch', 'delegate_cfg / run does not resolve to <parent run>/sub/<task>/')
        want_root = os.path.realpath(str(parent['root_config'])) if parent.get('root_config') else parent_path
        if os.path.realpath(str(child['root_config'])) != want_root:
            raise _Refused('parent_mismatch', 'root_config does not match the parent')
        if not subfleet.valid_child_id(parent.get('task_id'), child['task_id']):
            raise _Refused('task_mismatch', 'task id is not a child of the parent task')
        if 'security_taint' in parent and (                                         # L5-taint (D2: normalised)
                'security_taint' not in child
                or normalize_taint(child['security_taint']) != normalize_taint(parent['security_taint'])):
            raise _Refused('taint_not_inherited', 'parent taint not inherited')
        hops.append((parent_path, str(adopt['slot']), int(adopt['generation']), child['task_id']))
        paths.append(parent_path)
        policies.append(security.policy(parent))
        seen.add(parent_path)
        child, child_path = parent, parent_path
    if child_path != start_root:
        raise _Refused('root_mismatch', 'chain does not end at root_config')
    return Ancestry('verified', '', '', tuple(hops), tuple(paths), child_path, tuple(policies), _rule_t(cfg))


def _parent_agent(connect, parent, slot):
    try:
        r = connect(parent)
    except Exception as exc:  # noqa: BLE001
        raise _Refused('parent_unreachable', type(exc).__name__)
    try:
        return r.hgetall('fx:agent:' + slot) or {}
    except Exception as exc:  # noqa: BLE001
        raise _Refused('parent_unreachable', type(exc).__name__)
    finally:
        _close(r)


# ------------------------------------------------------------------ the hook entry
class _Deny(Exception):
    def __init__(self, reason, p_id=''):
        super().__init__(reason)
        self.reason, self.p_id = reason, p_id


def _deny_json(reason, p_id=''):
    head = (p_id + ': ') if p_id else ''
    text = (head + 'action needs user confirmation (' + reason + '); '
            'after approval retry the exact same command (ticket 30 min)')
    return json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                              'permissionDecisionReason': text}}, ensure_ascii=False)


def _unpoliced(cfg):
    return isinstance(cfg, dict) and security.policy(cfg) is None and 'security_taint' not in cfg


def _connect(cfg):
    import redis
    from fleetmux import redis_runtime
    sock = redis_runtime.validate_socket(cfg)
    client = redis.Redis(unix_socket_path=str(sock), db=0, decode_responses=True,
                         socket_connect_timeout=3, socket_timeout=5)
    client.ping()
    return client


def _close(r):
    try:
        r.close()
        r.connection_pool.disconnect()
    except Exception:
        pass


_COUNT_LUA = "local n = redis.call('INCR', KEYS[1]) if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end return n"
COUNT_TTL_S = 7 * 24 * 3600


class _Run:
    """The acting run: its Redis client, bus and audit writer."""

    def __init__(self, cfg, cfg_path, r):
        from fleetmux import runtime_v2
        self.cfg, self.cfg_path, self.r = cfg, cfg_path, r
        self.bus = runtime_v2.bus_for(cfg, r)

    def audit(self, event, **fields):
        from fleetmux.presentation import mask_secrets, sanitize_text
        data = mask_secrets(fields)
        row = {k: (sanitize_text(v) if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str))
               for k, v in data.items()}
        row.update(event=event, run_id=self.bus.run_id, at=str(int(time.time() * 1000)))
        self.r.xadd(self.bus.audit_key, row)


def _count(run, slot, generation, cap):
    from fleetmux import channels
    scope = run.r.hget(run.bus.lease_key(slot), 'current_delivery') or 'startup'
    key = channels.GUARD_CALLS + run.bus.run_id + ':' + slot + ':' + str(generation) + ':' + scope
    n = int(run.r.eval(_COUNT_LUA, 1, key, COUNT_TTL_S))
    if cap and n > int(cap):
        run.audit('security.limit_tripped', kind='tool_calls', slot=slot, generation=str(generation), count=n,
                  limit=int(cap))
        raise _Deny('tool_calls limit ' + str(cap) + ' per delivery reached')
    return n


def _taint_kinds(cfg):
    taint = _rule_t(cfg)
    return taint['kinds'] if taint else None


def _request_has_record(r, request):
    """P1-01 (M197, reviewer option b): did this request come in through a channel / the request intake? Evidence,
    all keyed on envelope.origin_id: the actor's fx:channel:origin:<id> (written on acceptance), a P8d
    fx:channel:untrusted:<id> mark, or the request's own fx:req:<id> (service intake; keeps a channel request
    guarded after its 7-day origin record has expired, since its spec still carries the outside-content wrap).
    Task-mode deliveries (dispatch id / task id / 'notice') have none of these."""
    from fleetmux import channels
    from fleetmux.channels import envelope
    oid = envelope.origin_id(request)
    return bool(r.exists(channels.ORIGIN + oid, channels.UNTRUSTED + oid, 'fx:req:' + oid))


def _untrusted(run, slot, pol):
    """Outside kinds of this call, or None. The fleet's taint (rule T) first; otherwise the slot's current request
    through P8d ``strangers.untrusted_kinds`` when that request has a channel / intake record (P1-01): only 'clean' /
    'off' are trusted there ('unknown' counts as marked). A request with no such record (task-mode dispatch / task /
    notice ids) is not outside content: clean. Sub-fleets stay guarded through their taint (rule T) and, for an
    adopted pane, through the pinned slot's request in the parent run (_decide_pinned)."""
    from fleetmux.channels import approve, strangers
    kinds = _taint_kinds(run.cfg)
    if kinds:
        return kinds
    binding = approve.agent_binding(run.r, slot)
    if not binding.request:
        return None                                      # startup turn: no request in flight
    if not _request_has_record(run.r, binding.request):
        return None                                      # P1-01: no channel / intake record -> not outside content
    got = strangers.untrusted_kinds(run.r, binding.request, policy=pol)
    outcome = got.get('outcome') if isinstance(got, dict) else None
    if outcome in ('clean', 'off'):
        return None
    return list(got.get('kinds') or ['unknown']) if isinstance(got, dict) else ['unknown']


def _decide(cfg, cfg_path, name, generation, raw):
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get('tool_name'), str):
        raise _Deny('malformed hook input')
    tool = data['tool_name']
    tool_input = data.get('tool_input') if isinstance(data.get('tool_input'), dict) else {}
    gen = int(generation)
    pinned = _connect(cfg)
    try:
        return _decide_pinned(cfg, cfg_path, name, gen, tool, tool_input, pinned)
    finally:
        _close(pinned)


def _decide_pinned(cfg, cfg_path, name, gen, tool, tool_input, pinned):
    from fleetmux.channels import approve, canary
    from fleetmux import fleet as fleet_mod, subfleet
    info = pinned.hgetall(fleet_mod.key('agent', name))
    acting_cfg, acting_path, slot, slot_gen, moved = cfg, str(cfg_path), name, gen, False
    if info.get('state') == 'delegated':
        route = subfleet.delegated_route(cfg, name, gen, strict=True)
        if route is None:
            raise _Deny('route unreadable')
        if route[0] is cfg:
            raise _Deny('stale generation on a delegated slot')       # X4
        acting_cfg, slot, slot_gen = route
        acting_path, moved = _own_path(acting_cfg), True
        adopt = acting_cfg.get('adopt') if isinstance(acting_cfg, dict) else None
        if not (isinstance(adopt, dict) and str(adopt.get('slot')) == name and str(adopt.get('generation')) == str(gen)):
            raise _Deny('link_mismatch')                               # L4: the adopted pane is this hook's
    pol = security.policy(acting_cfg) or security.DEFAULTS
    r = _connect(acting_cfg)
    try:
        run = _Run(acting_cfg, acting_path, r)
        _count(run, slot, slot_gen, (pol.get('limits') or {}).get('tool_calls_per_delivery'))
        if canary.check_read(tool, tool_input, acting_cfg):
            run.audit('security.canary_tripped', kind='read', level='error', slot=slot, tool=tool)
        got = classify(tool, tool_input, pol, acting_cfg)
        kinds = None
        if tool not in READ_ONLY:
            kinds = _untrusted(run, slot, pol)
            if moved and not kinds:   # the adopted pane still serves the pinned slot's request (P8d mark there)
                kinds = _untrusted(_Run(cfg, str(cfg_path), pinned), name, security.policy(cfg) or pol)
        if got is None and not kinds:
            return None
        text = action_text(tool, tool_input)
        if kinds and tool not in READ_ONLY and pol.get('untrusted_actions') == 'deny':
            run.audit('security.action_denied', slot=slot, reason='untrusted_actions_deny', action_text=text)
            raise _Deny('untrusted request: write / exec actions are denied')
        cls, tier = got if got else ('untrusted', 'normal')            # M126 (b)
        return _confirm(run, approve, slot, slot_gen, tool, tool_input, cls, tier, text,
                        pinned_path=str(cfg_path) if moved else None)
    finally:
        _close(r)


class _Ctx:
    """One confirmation: where the ticket lives (local run or the root) and the checks around it."""


def _confirm(run, approve, slot, gen, tool, tool_input, cls, tier, text, pinned_path=None):
    rr = approve.route_root(run.cfg, run.cfg_path, verify=verify_ancestry)
    if rr.outcome == 'refused':
        run.audit('security.action_denied', slot=slot, reason=rr.reason, action_text=text)
        raise _Deny(rr.reason or 'refused')
    if rr.outcome not in ('local', 'routed'):
        raise _Deny('internal')
    routed = rr.outcome == 'routed'
    route = rr.route if routed else None
    if routed:
        pol = route.policy or security.DEFAULTS
        oob = route.oob_modes or ''
        if pinned_path and (not route.hops or os.path.realpath(str(route.hops[0][0])) != os.path.realpath(pinned_path)):
            run.audit('security.action_denied', slot=slot, reason='link_mismatch', action_text=text)
            raise _Deny('link_mismatch')                               # L4: first hop is the pinned cfg
    else:
        pol = security.policy(run.cfg) or security.DEFAULTS
        high = (pol.get('confirm') or {}).get('high')
        oob = high if high in HIGH_MODES else ''
    if tier == 'high' and oob not in HIGH_MODES:                        # D1 / C8.4
        run.audit('security.action_denied', slot=slot, reason='policy_refused', action_text=text)
        raise _Deny('policy_refused')
    ctx = _Ctx()
    ctx.run, ctx.slot, ctx.gen, ctx.text, ctx.routed, ctx.route = run, slot, int(gen), text, routed, route
    ctx.target = route.r if routed else run.r
    ctx.confirm = pol.get('confirm') or {}
    try:
        b0 = approve.agent_binding(run.r, slot)
    except approve.BindingError:
        raise _Deny('no_agent')
    if int(b0.generation) != ctx.gen:
        raise _Deny('old_generation')
    ctx.b0 = b0
    try:
        if routed:
            if approve.stop_pending(run.r):
                raise _Deny('stopped')
            ctx.route_epoch = approve.current_epoch(run.r)
        ctx.call_epoch = approve.current_epoch(ctx.target)
    except approve.EpochError:
        raise _Deny('epoch_malformed')
    ahash = action_hash(slot, gen, tool, tool_input)
    run_id = route.run_id if routed else run.bus.run_id
    ctx.k = approve.route_hash(run_id, ahash) if routed else ahash
    ctx.expect = dict(scope='routed' if routed else 'local', run_id=run_id, slot=slot, generation=ctx.gen,
                      request_id=b0.request, action_hash=ahash)
    if routed:
        ctx.expect.update(route_epoch=ctx.route_epoch, root_request=route.root_request)
    res = approve.consume_ticket(ctx.target, ctx.k, expect=ctx.expect, call_epoch=ctx.call_epoch, audit=run.audit)
    if res.outcome == 'consumed':
        return _allow(ctx, approve, res.p_id)
    if res.outcome == 'absent':                       # O1: the only place an action is registered
        reg = approve.register_action(ctx.target, slot=slot, generation=ctx.gen, request_id=b0.request,
                                      run_id=run_id, cls=cls, tier=tier, action_hash=ahash, action_text=text,
                                      call_epoch=ctx.call_epoch, oob_modes=oob, route=route,
                                      route_epoch=ctx.route_epoch if routed else None)
        if reg.outcome == 'registered' or reg.outcome == 'reused':   # M126 (a): register_action pushes the card
            return _wait(ctx, approve, reg.p_id, cls, tier)
        run.audit('security.action_denied', slot=slot, reason=reg.reason, action_text=text)
        raise _Deny(reg.reason or 'refused', reg.p_id)
    run.audit('security.action_denied', slot=slot, reason=res.reason, action_text=text)
    raise _Deny(res.reason or 'refused', res.p_id)


def _still_valid(ctx, approve):
    """Between wait slices: the root / local epoch unchanged; routed: child epoch, STOP and binding unchanged."""
    try:
        if approve.current_epoch(ctx.target) != ctx.call_epoch:
            return 'stopped'
        if ctx.routed:
            if approve.current_epoch(ctx.run.r) != ctx.route_epoch or approve.stop_pending(ctx.run.r):
                return 'stopped'
    except approve.EpochError:
        return 'epoch_malformed'
    return ''


def _allow(ctx, approve, p_id):
    if ctx.routed:                                    # §12.8 post-check, child side (not atomic with the root)
        try:
            b1 = approve.agent_binding(ctx.run.r, ctx.slot)
            same = (b1 == ctx.b0 and approve.current_epoch(ctx.run.r) == ctx.route_epoch
                    and not approve.stop_pending(ctx.run.r))
            rr = approve.route_root(ctx.run.cfg, ctx.run.cfg_path, verify=verify_ancestry) if same else None
            same = same and rr.outcome == 'routed' and rr.route.run_id == ctx.route.run_id \
                and rr.route.root_cfg_path == ctx.route.root_cfg_path
        except Exception:  # noqa: BLE001
            same = False
        if not same:
            ctx.run.audit('security.action_denied', p_id=p_id, slot=ctx.slot, reason='post_check', action_text=ctx.text)
            raise _Deny('post_check', p_id)
    ctx.run.audit('security.action_approved', stage='allowed', p_id=p_id, slot=ctx.slot, action_text=ctx.text)
    return None


def _wait(ctx, approve, p_id, cls, tier):
    from fleetmux import channels
    run = ctx.run
    run.audit('security.action_pending', p_id=p_id, slot=ctx.slot, cls=cls, tier=tier, action_text=ctx.text)
    wait_key = channels.GUARD_WAIT + ctx.k
    end = time.monotonic() + float(ctx.confirm.get('wait_s', security.DEFAULTS['confirm']['wait_s']))
    while True:
        left = end - time.monotonic()
        if left <= 0:
            run.audit('security.action_timeout', p_id=p_id, slot=ctx.slot, action_text=ctx.text)
            raise _Deny('timed out waiting for the user', p_id)
        popped = ctx.target.blpop([wait_key], timeout=max(1, int(min(WAIT_SLICE_S, left) + .999)))
        if popped is None:
            why = _still_valid(ctx, approve)
            if why:
                run.audit('security.action_denied', p_id=p_id, slot=ctx.slot, reason=why, action_text=ctx.text)
                raise _Deny(why, p_id)
            continue
        if popped[1] != 'approve':
            run.audit('security.action_denied', p_id=p_id, slot=ctx.slot, reason='rejected', action_text=ctx.text)
            raise _Deny('rejected by the user', p_id)
        res2 = approve.consume_ticket(ctx.target, ctx.k, expect=ctx.expect, call_epoch=ctx.call_epoch, audit=run.audit)
        if res2.outcome == 'consumed':
            return _allow(ctx, approve, p_id)
        run.audit('security.action_denied', p_id=p_id, slot=ctx.slot, reason=res2.reason or res2.outcome,
                  action_text=ctx.text)
        raise _Deny(res2.reason or 'no ticket', p_id)


def pretool(cfg_path, name, generation, raw):
    """``fleet.py claude-pretool``: the PreToolUse decision JSON to print, or None (allow, no output).

    No policy and no taint in the pinned config: None, without touching Redis (behaviour unchanged).
    Otherwise every failure is a deny JSON (fail closed), never an exception."""
    try:
        cfg = json.loads(Path(cfg_path).read_text())
    except Exception:
        return _deny_json('config unreadable')
    if _unpoliced(cfg):
        return None
    if not isinstance(cfg, dict):
        return _deny_json('config invalid')
    try:
        return _decide(cfg, cfg_path, name, generation, raw)
    except _Deny as exc:
        return _deny_json(exc.reason, exc.p_id)
    except Exception as exc:
        return _deny_json('internal: ' + type(exc).__name__)
