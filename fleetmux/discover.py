"""First-run discovery (docs/design.md §52): which agent CLIs this machine has, whether they are logged in, which
models each can run, and the default supervisor x worker pair ranked by fleet.yaml#discovery.

The result is a cache, not operator config: <fleet home>/discovered.yaml (mode 0600; $FLEETMUX_DISCOVERED moves
it). `fleetmux setup` rewrites it; `start` / `gui` write it once when it is missing. Privacy (§52): login probes
keep only logged_in / auth_method (never email / org), pi's models.json is read for provider names, base URLs and
model ids only (never apiKey, never auth.json), and the only network calls are one GET <baseUrl>/models per pi
custom provider, without a key and without a proxy.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from . import cli_pairs, fleet_config, paths

ROOT = Path(__file__).resolve().parents[1]
CACHE_ENV = 'FLEETMUX_DISCOVERED'
OFF_ENV = 'FLEETMUX_DISCOVERY'  # "off": start / gui never probe (tests, scripted starts)
TIMEOUT = 10  # per probe, seconds (T3 Code raised its login probe from 4 s to 10 s)
GATEWAY_TIMEOUT = 3
MODEL_KEYS = {'claude': 'worker_model', 'pi': 'pi_model', 'opencode': 'opencode_model'}
_PATH_MARK = re.compile(r'__FLEET_PATH__(.*?)__END__', re.S)



def version(text):
    """First version-looking token as a tuple: the rule of scripts/check_deps.py, repeated here because an
    installed package may not ship scripts/ (nothing in this module reads repository files at import time)."""
    m = re.search(r'\d+(?:\.\d+)*', text or '')
    return tuple(int(x) for x in m.group(0).split('.')) if m else None


def at_least(have, need):
    need = version(str(need))
    return have is not None and need is not None and have[:len(need)] >= need


def cache_path():
    env = os.environ.get(CACHE_ENV)
    if env:
        return Path(env).expanduser()
    return paths.home() / 'discovered.yaml'


def search_path():
    """The login shell's PATH first, then this process's: a GUI-started process lacks the login PATH (T3 Code
    issue 1787), and a cmux pane's PATH starts with session-private shims that die with the session."""
    parts = []
    shell = os.environ.get('SHELL') or '/bin/zsh'
    try:
        out = subprocess.run([shell, '-lic', 'printf "__FLEET_PATH__%s__END__" "$PATH"'], capture_output=True,
                             text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL).stdout
        m = _PATH_MARK.search(out)
        if m:
            parts += m.group(1).split(os.pathsep)
    except (OSError, subprocess.SubprocessError):
        pass
    parts += os.environ.get('PATH', '').split(os.pathsep)
    seen = []
    for p in parts:
        if p and p not in seen:
            seen.append(p)
    return os.pathsep.join(seen)


def _run(cmd, timeout=TIMEOUT):
    """(returncode, stdout + stderr) or (None, '') on a missing binary / timeout."""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return done.returncode, (done.stdout or '') + (done.stderr or '')
    except (OSError, subprocess.SubprocessError):
        return None, ''


def _login(name, path):
    """{logged_in: True / False / None, auth_method?} -- whitelisted fields only."""
    if name == 'claude':
        code, out = _run([path, 'auth', 'status'])
        try:
            data = json.loads(out[out.index('{'):])
        except ValueError:
            return {'logged_in': None}
        row = {'logged_in': bool(data.get('loggedIn'))}
        if isinstance(data.get('authMethod'), str):
            row['auth_method'] = data['authMethod']
        return row
    if name == 'codex':
        code, out = _run([path, 'login', 'status'])
        low = out.lower()
        if 'not logged in' in low:
            return {'logged_in': False}
        return {'logged_in': True} if code == 0 and 'logged in' in low else {'logged_in': None}
    return {'logged_in': None}


def _models(name, path, claude_models):
    """Model ids a CLI can run now, or None when it has no listing we use."""
    if name == 'claude':
        return list(claude_models)
    if name == 'pi':
        code, out = _run([path, '--list-models'])
        if code != 0:
            return []
        rows = []
        for line in out.splitlines():
            cols = line.split()
            if len(cols) >= 2 and cols[0] != 'provider':
                rows.append(cols[0] + '/' + cols[1])
        return rows
    if name == 'opencode':
        code, out = _run([path, 'models'])
        return [ln.strip() for ln in out.splitlines() if code == 0 and re.fullmatch(r'[\w.\-]+/\S+', ln.strip())]
    if name == 'ollama':
        code, out = _run([path, 'list'])
        if code != 0:
            return None  # installed, server not running (or not answering)
        return [ln.split()[0] for ln in out.splitlines()[1:] if ln.split()]
    return None


def probe_cli(name, need, search, claude_models):
    found = shutil.which(name, path=search)
    if not found:
        return {'installed': False}
    path = os.path.realpath(found)
    code, out = _run([path, '--version'])
    have = version(out) if code == 0 else None
    row = {'installed': True, 'path': path, 'version': '.'.join(map(str, have)) if have else None,
           'min': need, 'version_ok': (at_least(have, need) if need else have is not None)}
    row.update(_login(name, path))
    models = _models(name, path, claude_models)
    if models is not None:
        row['models'] = models
        if name in ('pi', 'opencode') and row['logged_in'] is None:
            row['logged_in'] = bool(models)
    elif name == 'ollama':
        row['serving'] = False
    if name == 'ollama' and models is not None:
        row['serving'] = True
    return row


def pi_gateways(pi_dir=None):
    """{provider: {online}} for pi's custom providers that have a baseUrl; nothing else is read or kept."""
    path = Path(pi_dir or Path.home() / '.pi' / 'agent') / 'models.json'
    try:
        providers = json.loads(path.read_text()).get('providers') or {}
    except (OSError, ValueError):
        return {}
    urls = {p: v['baseUrl'] for p, v in providers.items() if isinstance(v, dict) and isinstance(v.get('baseUrl'), str)}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def online(url):
        try:
            opener.open(url.rstrip('/') + '/models', timeout=GATEWAY_TIMEOUT).close()
            return True
        except urllib.error.HTTPError:
            return True  # it answered (401 without a key still means online)
        except (OSError, ValueError):
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        states = dict(zip(urls, pool.map(online, urls.values())))
    return {p: {'online': up} for p, up in states.items()}


def tokens(text):
    return [t for t in re.split(r'[-/_.\s]+', text.lower()) if t]


def matches(pattern, model):
    """Whole-token match. A space separates parts that must appear in that order, not necessarily adjacent
    ("deepseek flash" matches deepseek-v4.1-flash-sg, not deepseek-v4-pro); each part matches contiguously."""
    have, start = tokens(model), 0
    for part in pattern.split():
        want = tokens(part)
        hit = next((i for i in range(start, len(have) - len(want) + 1) if have[i:i + len(want)] == want), None)
        if hit is None:
            return False
        start = hit + len(want)
    return start > 0


def _newest(model):
    return tuple(int(n) for n in re.findall(r'\d+', model.rsplit('/', 1)[-1]))


def candidates(clis, gateways, cfg):
    """[(cli, model)] usable now: installed, version ok, not logged out, pi gateway online, only_via respected."""
    out, skipped = [], []
    for cli in cfg.get('cli_order') or []:
        row = clis.get(cli) or {}
        if not row.get('installed') or not row.get('version_ok') or row.get('logged_in') is False:
            continue
        for model in row.get('models') or []:
            via = [v for p, v in (cfg.get('only_via') or {}).items() if matches(p, model)]
            if via and cli not in via:
                continue
            if cli == 'pi' and gateways.get(model.split('/', 1)[0], {}).get('online') is False:
                skipped.append({'cli': cli, 'model': model, 'why': 'gateway offline'})
                continue
            out.append((cli, model))
    return out, skipped


def rank(pool, patterns, order, rest=False):
    """Pool entries in pattern order (newest version first, then cli order); `rest` appends the unmatched ones."""
    def by_version_then_cli(rows):  # two stable sorts: cli order first, newest version within one cli
        rows = sorted(rows, key=lambda c: _newest(c[1]), reverse=True)
        return sorted(rows, key=lambda c: order.index(c[0]))
    ranked = []
    for pattern in patterns:
        ranked += by_version_then_cli([c for c in pool if matches(pattern, c[1]) and c not in ranked])
    if rest:
        ranked += sorted((c for c in pool if c not in ranked), key=lambda c: (order.index(c[0]), c[1]))
    return ranked


def chain(role, extra=(), cfg=None, cached=None):
    """([(cli, model)], patterns): the role's wall chain on this machine (user 2026-11-11: the discovery order is also
    the order to walk down after a usage wall; docs/design.md §23). Only models a fleet.yaml#discovery.<role> pattern
    names: an unlisted model is never a fallback. The pool is the cached probe's clis / gateways re-ranked with the
    patterns in force now (no new probe); without a cache, the model cards plus ``extra`` (the run's own models)."""
    cfg = fleet_config.block('discovery') if cfg is None else cfg
    order, patterns = list(cfg.get('cli_order') or []), list(cfg.get(role) or [])
    data = load() if cached is None else cached
    if data and isinstance(data.get('clis'), dict):
        pool = candidates(data['clis'], data.get('gateways') or {}, cfg)[0]
    else:
        pool = []
        for cli, model in [(m.get('cli'), m.get('model')) for m in (fleet_config.block('models') or {}).values()]:
            if model and (cli, model) not in pool and _via_ok(cli, model, order, cfg):
                pool.append((cli, model))
        for cli, model in extra:  # a run's own model, unless a card of that CLI already sits at its place
            at = first_match(patterns, model)
            if (model and _via_ok(cli, model, order, cfg)
                    and not any(c == cli and first_match(patterns, m) == at for c, m in pool)):
                pool.append((cli, model))
    if role == 'supervisor':
        pool = [c for c in pool if c[0] in cli_pairs.CLIS]
    return rank(pool, patterns, order), patterns


def _via_ok(cli, model, order, cfg):
    via = [v for p, v in (cfg.get('only_via') or {}).items() if matches(p, model)]
    return cli in order and not (via and cli not in via)


def first_match(patterns, model):
    return next((i for i, p in enumerate(patterns) if matches(p, model or '')), None)


def place(entries, patterns, cli, model):
    """Index of the running (cli, model) on a chain: that entry, else the entry of the same CLI under the first
    pattern the model matches (a run may say claude-opus-5-5 where the chain says opus); None: not on the chain."""
    if (cli, model) in entries:
        return entries.index((cli, model))
    at = first_match(patterns, model)
    if at is None:
        return None
    return next((i for i, (c, m) in enumerate(entries) if c == cli and first_match(patterns, m) == at), None)


def pick(sups, workers, pairs, mode):
    """(default pair, notes): the first verified (supervisor, worker) in rank order; notes say why better-ranked
    workers were passed over for the chosen supervisor."""
    for s in sups:
        notes = []
        for w in workers:
            row = pairs.lookup(s[0], w[0], mode) or {}
            if row.get('status') == 'verified' and not row.get('adapter_missing'):
                return (s, w), notes
            notes.append(w[1] + ' via ' + w[0] + ': ' + s[0] + ' supervisor + ' + w[0] + ' workers (' + mode + ') is '
                         + (row.get('status') or 'not in the pair table'))
    return None, []


def default_of(choice, cfg, mode):
    (scli, smodel), (wcli, wmodel) = choice
    out = {'supervisor_cli': scli, 'worker_cli': wcli, 'mode': mode,
           'supervisor': scli + ':' + smodel, 'worker': wcli + ':' + wmodel, MODEL_KEYS[wcli]: wmodel}
    if scli == 'pi':
        out['supervisor_pi_model'] = smodel
        out['supervisor_pi_thinking'] = cfg.get('supervisor_pi_thinking') or 'high'
    else:
        out['supervisor_model'] = smodel
    return out


def run(cfg=None, search=None, pi_dir=None, pairs=None, claude_models=None):
    cfg = fleet_config.block('discovery') if cfg is None else cfg
    pairs = pairs or cli_pairs.load_pairs()
    search = search_path() if search is None else search
    if claude_models is None:
        claude_models = [m['model'] for m in (fleet_config.block('models') or {}).values() if m.get('cli') == 'claude']
    try:  # minimums from deps.yaml when the checkout ships it; without it versions are shown, not judged
        deps = (yaml.safe_load((ROOT / 'deps.yaml').read_text()) or {}).get('programs') or {}
    except (OSError, yaml.YAMLError):
        deps = {}
    names = list(dict.fromkeys(list(cfg.get('cli_order') or []) + list(cfg.get('report_only') or [])))
    with ThreadPoolExecutor(max_workers=len(names) + 1) as pool:
        jobs = {n: pool.submit(probe_cli, n, (deps.get(n) or {}).get('min'), search, claude_models) for n in names}
        gw = pool.submit(pi_gateways, pi_dir)
        clis = {n: j.result() for n, j in jobs.items()}
        gateways = gw.result()
    pool_, skipped = candidates(clis, gateways, cfg)
    order = list(cfg.get('cli_order') or [])
    sup_pool = [c for c in pool_ if c[0] in cli_pairs.CLIS]
    sups = rank(sup_pool, cfg.get('supervisor') or [], order)
    sup_only = [c for c in sups if not any(matches(p, c[1]) for p in cfg.get('worker') or [])]
    workers = rank([c for c in pool_ if c not in sup_only], cfg.get('worker') or [], order, rest=True)
    mode = cfg.get('mode') or 'task'
    choice, notes = pick(sups, workers, pairs, mode)
    result = {'version': 1, 'probed_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
              'clis': clis, 'gateways': gateways,
              'supervisors': [c[0] + ':' + c[1] for c in sups], 'workers': [c[0] + ':' + c[1] for c in workers],
              'skipped': skipped, 'passed_over': notes, 'default': default_of(choice, cfg, mode) if choice else None,
              'fixes': fixes(clis, choice)}
    private = fleet_config.user_path()
    if private and private.exists() and private.stat().st_mode & 0o077:
        result['fixes'].append('private config ' + str(private) + ' is readable by other users: chmod 600 it')
    return result


def fixes(clis, choice):
    out = []
    hints = {'claude': 'claude auth login', 'codex': 'codex login', 'pi': 'pi, then /login',
             'opencode': 'opencode auth login'}
    for name, row in clis.items():
        if row.get('installed') and not row.get('version_ok'):
            out.append(name + ' ' + str(row.get('version')) + ' is older than ' + str(row.get('min')) + ': upgrade it')
        elif row.get('logged_in') is False and name in hints:
            out.append(name + ' is not logged in: run `' + hints[name] + '`')
        elif name == 'ollama' and row.get('installed') and not row.get('serving'):
            out.append('ollama is installed but not serving: `ollama serve` to use its models')
    if choice is None:
        out.append('no default pair: needs one supervisor model (fleet.yaml#discovery.supervisor) and one worker '
                   'model on a verified cli_pairs row; install / log in one of claude, pi')
    return out


def save(result, path=None):
    path = path or cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh:
        yaml.safe_dump(result, fh, allow_unicode=True, sort_keys=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def load(path=None):
    try:
        data = yaml.safe_load((path or cache_path()).read_text())
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def render(result):
    lines = ['fleet setup -- agent CLIs on this machine (' + str(result.get('probed_at')) + ')']
    for name, row in (result.get('clis') or {}).items():
        if not row.get('installed'):
            lines.append('  - ' + name + ': not installed')
            continue
        state = {True: 'logged in', False: 'NOT logged in', None: 'login unknown'}[row.get('logged_in')]
        bits = [str(row.get('version') or '?') + ('' if row.get('version_ok') else ' (too old, need ' + str(row.get('min')) + ')'),
                state]
        if 'models' in row:
            bits.append(str(len(row['models'])) + ' models')
        if name == 'ollama':
            bits.append('serving' if row.get('serving') else 'not serving')
        lines.append(('  ok ' if row.get('version_ok') and row.get('logged_in') is not False else '  !! ') + name
                     + ': ' + ', '.join(bits) + '  [' + row['path'] + ']')
    for p, g in (result.get('gateways') or {}).items():
        lines.append('  gateway ' + p + ': ' + ('online' if g.get('online') else 'OFFLINE'))
    for label, key in (('supervisor', 'supervisors'), ('worker', 'workers')):
        ranked = result.get(key) or []
        lines.append(label + ' ranking: ' + (' > '.join(ranked[:8]) or '(none)')
                     + (' > ... (' + str(len(ranked)) + ' in all)' if len(ranked) > 8 else ''))
    d = result.get('default')
    lines.append('default pair: ' + (d['supervisor'] + ' supervisor x ' + d['worker'] + ' workers (' + d['mode'] + ')'
                                     if d else '(none)'))
    for note in result.get('passed_over') or []:
        lines.append('  passed over: ' + note)
    for fix in result.get('fixes') or []:
        lines.append('  to do: ' + fix)
    lines.append('cached in ' + str(cache_path()) + '; `fleetmux setup` probes again')
    return '\n'.join(lines)


def ensure(out=None):
    """The cached result; probe and print once when there is none. Never raises: a failed probe only warns."""
    if os.environ.get(OFF_ENV, '').strip().lower() == 'off':
        return None
    cached = load()
    if cached is not None:
        return cached
    try:
        result = run()
        save(result)
    except Exception as exc:  # discovery must never stop a start: report it and go on with the table default
        print('fleet setup: discovery failed (' + type(exc).__name__ + ': ' + str(exc) + '); using defaults',
              file=out or sys.stderr)
        return None
    print(render(result), file=out or sys.stderr)
    return result


def apply_models(args, found):
    """Fill model flags the operator did not type from the discovered default (after resolve_supervisor)."""
    if args.supervisor_cli == 'claude' and getattr(args, 'supervisor_model', None) is None:
        args.supervisor_model = found.get('supervisor_model')
    key = MODEL_KEYS.get(args.worker_cli)
    if key and getattr(args, key, None) is None and found.get(key):
        setattr(args, key, found[key])
