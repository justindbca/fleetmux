"""First-run discovery (docs/design.md §52): which agent CLIs this machine has, whether they are logged in, which
models each can run, and the default supervisor x worker pair ranked by fleet.yaml#discovery.

The result is a cache, not operator config: <fleet home>/discovered.yaml (mode 0600; $FLEETMUX_DISCOVERED moves
it). `fleetmux setup` rewrites it; `start` / `gui` write it once when it is missing. Privacy (§52): login probes
keep only logged_in / auth_method (never email / org), pi's models.json is read for provider names, base URLs and
model ids only (never apiKey, never auth.json), and the only network calls are one GET <baseUrl>/models per pi
custom provider, without a key and without a proxy. pi's logins are checked per provider by pi itself (`pi auth
check`, which may refresh an expired OAuth login); only its status and reason are kept.
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
# Where this machine recorded the programs it found (docs/design.md §50.3): the dependency rows of the installed
# release (~/fleetmux-current, the link install_release.sh --activate moves) and of this code's own release.
CURRENT = Path('~/fleetmux-current')
OWN_INSTALLED = ROOT / 'bin' / 'installed.json'
_ENV_SHEBANG = re.compile(rb'#![ \t]*/usr/bin/env[ \t]+(?:-S[ \t]+)?([^\s/]+)')



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


def search_path(warn=None):
    """The login shell's PATH first, then this process's, then the directories this machine recorded where it found
    a program before (``recorded_dirs``): a GUI-started process lacks the login PATH (T3 Code issue 1787), a cmux
    pane's PATH starts with session-private shims that die with the session, and a program in a private directory
    (pi under its own npm prefix) is on neither when the caller is ssh. This is the one resolver for the user's
    programs (docs/design.md §50.3): discovery, `start`, scripts/check_deps.py and applets use it. The order is fixed:
    what the user's environment says now wins, a record is only the fallback for what it misses. When the shell
    cannot be read, the login part is left out and ``warn`` gets one line."""
    parts, why = [], None
    shell = os.environ.get('SHELL') or '/bin/zsh'
    try:
        out = subprocess.run([shell, '-lic', 'printf "__FLEET_PATH__%s__END__" "$PATH"'], capture_output=True,
                             text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL).stdout
        m = _PATH_MARK.search(out)
        if m:
            parts += m.group(1).split(os.pathsep)
        else:
            why = 'no PATH in its output'
    except (OSError, subprocess.SubprocessError) as exc:
        why = type(exc).__name__ + ': ' + str(exc)[:120]
    if why and warn:
        warn('login shell PATH unavailable (' + shell + ' -lic: ' + why + '); using the process PATH only')
    parts += os.environ.get('PATH', '').split(os.pathsep)
    try:
        parts += recorded_dirs()
    except Exception:  # a record never breaks the lookup
        pass
    seen = []
    for p in parts:
        if p and p not in seen:
            seen.append(p)
    return os.pathsep.join(seen)


def interpreter_of(path):
    """The program an ``#!/usr/bin/env <name>`` script runs under (pi's cli.js: node), else None."""
    try:
        with open(path, 'rb') as fh:
            first = fh.read(256).split(b'\n', 1)[0]
    except OSError:
        return None
    m = _ENV_SHEBANG.match(first)
    return m.group(1).decode('utf-8', 'replace') if m else None


def locate(name, search):
    """Where ``name`` is on ``search``, as recorded (docs/design.md §50.3): ``path`` (links followed, the install
    itself), ``dir`` (the PATH entry it was found in, reusable as a PATH entry) and, for an ``#!/usr/bin/env``
    script, ``interpreter`` / ``interpreter_path`` / ``interpreter_dir`` of that interpreter on the same search.
    None when it is not there."""
    found = shutil.which(name, path=search)
    if not found:
        return None
    path = os.path.realpath(found)
    row = {'path': path, 'dir': os.path.dirname(os.path.abspath(found))}
    interp = interpreter_of(path)
    if interp:
        hit = shutil.which(interp, path=search)
        row.update(interpreter=interp, interpreter_path=os.path.realpath(hit) if hit else None,
                   interpreter_dir=os.path.dirname(os.path.abspath(hit)) if hit else None)
    return row


def _installed_rows(path):
    try:
        path = Path(os.path.realpath(Path(path).expanduser()))
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    rows = data.get('dependencies') if isinstance(data, dict) else None
    return [(r['name'], r, str(path)) for r in rows if isinstance(r, dict) and isinstance(r.get('name'), str)] \
        if isinstance(rows, list) else []


def _discovered_rows():
    """The discovery rows (``clis``), read only: the models block of fleet-superpowers.yaml, then discovered.yaml.
    Never ``load()``: its one-time migration writes."""
    out = []
    try:
        from . import fleet_superpowers
        blocks = [(fleet_superpowers.read_block('models'), str(fleet_superpowers.file_path()))]
    except Exception:
        blocks = []
    try:
        blocks.append((yaml.safe_load(cache_path().read_text()), str(cache_path())))
    except (OSError, yaml.YAMLError):
        pass
    for data, source in blocks:
        clis = data.get('clis') if isinstance(data, dict) else None
        for name, row in (clis.items() if isinstance(clis, dict) else ()):
            if isinstance(name, str) and isinstance(row, dict):
                out.append((name, row, source))
    return out


def recorded():
    """[(name, row, source file)] of every program this machine recorded, read only, never raising: the installed
    release's installed.json ``dependencies`` (~/fleetmux-current), this code's own release's, then discovery."""
    rows = _installed_rows(CURRENT / 'bin' / 'installed.json')
    if os.path.realpath(Path(CURRENT).expanduser() / 'bin' / 'installed.json') != os.path.realpath(OWN_INSTALLED):
        rows += _installed_rows(OWN_INSTALLED)
    return rows + _discovered_rows()


def _usable(d, name, path):
    """A recorded directory counts only while it still holds the recorded program, and never when it is a release's
    own bin/ (an older release's fleetmux binaries must not shadow the ones being checked or linked)."""
    if not all(isinstance(v, str) and v for v in (d, name, path)) or not os.path.isabs(d) or not os.path.isdir(d):
        return False
    if os.path.basename(d) == 'bin' and os.path.isfile(os.path.join(d, 'installed.json')):
        return False
    hit = os.path.join(d, name)
    return os.access(hit, os.X_OK) and os.path.realpath(hit) == path


def _derived(path):
    """Directories an older row (``path`` only, no ``dir``) may have come from: the file's own directory, and for an
    npm package (``<prefix>/lib/node_modules/...``, pi's cli.js) the prefix's bin/."""
    if not isinstance(path, str):
        return []
    out = [os.path.dirname(path)]
    if '/lib/node_modules/' in path:
        out.append(path.split('/lib/node_modules/', 1)[0] + '/bin')
    return out


def recorded_dirs():
    """The directories of ``recorded()`` that still hold the recorded program (see ``_usable``), in record order."""
    out = []
    for name, row, _ in recorded():
        path = row.get('path')
        for d in ([row.get('dir')] if 'dir' in row else _derived(path)):
            if _usable(d, name, path) and d not in out:
                out.append(d)
        d = row.get('interpreter_dir')
        if _usable(d, row.get('interpreter'), row.get('interpreter_path')) and d not in out:
            out.append(d)
    return out


def last_found_at(name):
    """(path, source file) of the first record of ``name``, or None."""
    for n, row, source in recorded():
        if n == name and isinstance(row.get('path'), str) and row['path']:
            return row['path'], source
    return None


def last_found(name):
    """The hint for a missing program: 'last found at <path> (recorded in <file>)', or None without a record."""
    hit = last_found_at(name)
    return 'last found at ' + hit[0] + ' (recorded in ' + hit[1] + ')' if hit else None


def run_dirs(program):
    """Directories to append to the process PATH so ``program`` runs: the directory of its ``#!/usr/bin/env``
    interpreter found on search_path() (pi -> node) when the process PATH lacks it; [] otherwise and when discovery
    is off (tests, scripted starts)."""
    if not program or os.environ.get(OFF_ENV, '').strip().lower() == 'off':
        return []
    interp = interpreter_of(os.path.realpath(program))
    if not interp or shutil.which(interp):
        return []
    hit = which(interp)
    return [os.path.dirname(os.path.abspath(hit))] if hit else []


_SEARCH = []


def which(name):
    """Absolute path of ``name`` on search_path() (probed once per process), or None. None as well when discovery
    is off (tests, scripted starts): those callers see only the process PATH."""
    if os.environ.get(OFF_ENV, '').strip().lower() == 'off':
        return None
    if not _SEARCH:
        _SEARCH.append(search_path(warn=lambda line: print('fleetmux: ' + line, file=sys.stderr)))
    return shutil.which(name, path=_SEARCH[0])


def deps_programs():
    """deps.yaml `programs` (the packaged copy, kept equal to the checkout's by tests/test_setup_install.py), else {}
    (versions are then shown, not judged)."""
    try:
        return (yaml.safe_load(Path(__file__).resolve().with_name('deps.yaml').read_text()) or {}).get('programs') or {}
    except (OSError, yaml.YAMLError):
        return {}


def below_min(cli, text):
    """One line when ``text`` (the output of `<cli> --version`) is a version below the deps.yaml minimum of that
    agent CLI, else None (also when the version is unreadable or there is no minimum)."""
    need = (deps_programs().get(cli) or {}).get('min')
    have = version(text)
    if not need or have is None or at_least(have, need):
        return None
    return (cli + ' ' + '.'.join(map(str, have)) + ' is below ' + str(need)
            + ' (deps.yaml): not used by fleets on this machine; choose another CLI (`fleetmux setup` lists the usable ones)')


def _run(cmd, timeout=TIMEOUT, search=None):
    """(returncode, stdout + stderr) or (None, '') on a missing binary / timeout. ``search`` goes first on the child's
    PATH (the process PATH after it), so an ``#!/usr/bin/env node`` CLI finds the node of the same lookup."""
    env = None
    if search:
        dirs = search.split(os.pathsep) + os.environ.get('PATH', '').split(os.pathsep)
        env = dict(os.environ, PATH=os.pathsep.join(dict.fromkeys(d for d in dirs if d)))
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
        return done.returncode, (done.stdout or '') + (done.stderr or '')
    except (OSError, subprocess.SubprocessError):
        return None, ''


def _login(name, path, search=None):
    """{logged_in: True / False / None, auth_method?} -- whitelisted fields only."""
    if name == 'claude':
        code, out = _run([path, 'auth', 'status'], search=search)
        try:
            data = json.loads(out[out.index('{'):])
        except ValueError:
            return {'logged_in': None}
        row = {'logged_in': bool(data.get('loggedIn'))}
        if isinstance(data.get('authMethod'), str):
            row['auth_method'] = data['authMethod']
        return row
    if name == 'codex':
        code, out = _run([path, 'login', 'status'], search=search)
        low = out.lower()
        if 'not logged in' in low:
            return {'logged_in': False}
        if not (code == 0 and 'logged in' in low):
            return {'logged_in': None}
        # "Logged in using ChatGPT" (codex-cli 0.164.0): the only method kept; any other text, an API key line
        # with its masked key included, is dropped and the reach probe falls back to the API host.
        return dict({'logged_in': True}, **({'auth_method': 'chatgpt'} if 'logged in using chatgpt' in low else {}))
    return {'logged_in': None}


def _models(name, path, claude_models, search=None):
    """Model ids a CLI can run now, or None when it has no listing we use."""
    if name == 'claude':
        return list(claude_models)
    if name == 'pi':
        code, out = _run([path, '--list-models'], search=search)
        if code != 0:
            return []
        rows, table = [], False
        for line in out.splitlines():  # rows follow the `provider model ...` header; without a login pi prints
            cols = line.split()        # "No models available. Use /login ..." and no table
            if cols[:1] == ['provider']:
                table = True
            elif table and len(cols) >= 2:
                rows.append(cols[0] + '/' + cols[1])
        return rows
    if name == 'opencode':
        code, out = _run([path, 'models'], search=search)
        return [ln.strip() for ln in out.splitlines() if code == 0 and re.fullmatch(r'[\w.\-]+/\S+', ln.strip())]
    if name == 'ollama':
        code, out = _run([path, 'list'], search=search)
        if code != 0:
            return None  # installed, server not running (or not answering)
        return [ln.split()[0] for ln in out.splitlines()[1:] if ln.split()]
    return None


def _pi_providers(path, models, search=None):
    """{provider: {ok, status?, reason?}} per provider of pi's listed models, from `pi auth check --provider <p>
    --json` (pi refreshes an expired OAuth login first; a refresh that fails is ``invalid``). Only status and reason
    are kept, never a credential. ok None: no answer we read (a pi without `auth check`)."""
    providers = list(dict.fromkeys(m.split('/', 1)[0] for m in models if '/' in m))

    def check(provider):
        code, out = _run([path, 'auth', 'check', '--provider', provider, '--json'], search=search)
        try:
            data = json.loads(out[out.index('{'):out.rindex('}') + 1])
        except ValueError:
            return {'ok': None}
        status = data.get('status') if isinstance(data, dict) else None
        if status not in ('ready', 'not_ready', 'invalid'):
            return {'ok': None}
        row = {'ok': status == 'ready'}
        if status != 'ready':
            row.update(status=status, reason=str(data.get('reason') or 'unknown'))
        return row
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(providers)))) as pool:
        return dict(zip(providers, pool.map(check, providers)))


def probe_cli(name, need, search, claude_models):
    where = locate(name, search)
    if not where:
        return {'installed': False}
    path = where['path']
    code, out = _run([path, '--version'], search=search)
    have = version(out) if code == 0 else None
    row = {'installed': True, 'path': path, 'version': '.'.join(map(str, have)) if have else None,
           'min': need, 'version_ok': (at_least(have, need) if need else have is not None)}
    row.update((k, v) for k, v in where.items() if k != 'path')  # dir / interpreter*: reusable by the next lookup
    row.update(_login(name, path, search=search))
    models = _models(name, path, claude_models, search=search)
    if models is not None:
        row['models'] = models
        if name == 'pi':  # login per provider (user 2026-11-27): one dead login is not a logged-in pi
            row['providers'] = _pi_providers(path, models, search=search)
            models = [m for m in models if row['providers'].get(m.split('/', 1)[0], {}).get('ok') is not False]
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
            login = ((row.get('providers') or {}).get(model.split('/', 1)[0]) or {}) if cli == 'pi' else {}
            if login.get('ok') is False:
                skipped.append({'cli': cli, 'model': model,
                                'why': 'login: ' + str(login.get('status')) + ' (' + str(login.get('reason')) + ')'})
                continue
            out.append((cli, model))
    return pi_one_account(out, skipped, cfg)


PI_PROVIDER_PREFER, PI_PROVIDER_GROUPS = ['openai-codex'], {'openai': ['openai', 'openai-codex']}  # = fleet.yaml


def pi_one_account(pool, skipped, cfg):
    """(pool, skipped) with a pi model id listed by several providers of one fleet.yaml#discovery.pi_provider_groups
    group (logins of one account) kept once: the provider first in pi_provider_prefer, else the first listed.
    A private discovery block without the keys gets the shipped values (the block is overridden whole)."""
    prefer = cfg.get('pi_provider_prefer', PI_PROVIDER_PREFER) or []
    groups = cfg.get('pi_provider_groups', PI_PROVIDER_GROUPS) or {}
    group_of = {p: g for g, members in groups.items() for p in members or []}
    best = {}
    for cli, model in pool:
        if cli == 'pi' and '/' in model:
            provider, rest = model.split('/', 1)
            if provider in group_of:
                rank_ = prefer.index(provider) if provider in prefer else len(prefer)
                k = (group_of[provider], rest)
                best[k] = min(best.get(k, (rank_, provider)), (rank_, provider), key=lambda x: x[0])
    kept = []
    for cli, model in pool:
        provider, _, rest = model.partition('/')
        keep = best.get((group_of.get(provider), rest)) if cli == 'pi' else None
        if keep and keep[1] != provider:
            skipped.append({'cli': cli, 'model': model, 'why': 'same account as ' + keep[1]})
            continue
        kept.append((cli, model))
    return kept, skipped


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
    deps = deps_programs()
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
            out.append(name + ' ' + str(row.get('version')) + ' is below ' + str(row.get('min'))
                       + ': not used by fleets on this machine')
        elif row.get('logged_in') is False and name in hints:
            out.append(name + ' is not logged in: run `' + hints[name] + '`')
        elif name == 'ollama' and row.get('installed') and not row.get('serving'):
            out.append('ollama is installed but not serving: `ollama serve` to use its models')
        for provider, login in (row.get('providers') or {}).items():
            if login.get('ok') is False:
                out.append('pi provider ' + provider + ': login not usable (' + str(login.get('status')) + ': '
                           + str(login.get('reason')) + '); its models are in no chain: run `pi`, then /login '
                           + provider + ' (or remove that login)')
    if choice is None:
        out.append('no default pair: needs one supervisor model (fleet.yaml#discovery.supervisor) and one worker '
                   'model on a verified cli_pairs row; install / log in one of claude, pi')
    return out


def save(result, path=None):
    if path is None and not os.environ.get(CACHE_ENV):  # the models block of fleet-superpowers is the cache
        from . import fleet_superpowers
        return fleet_superpowers.write_block('models', result)
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
    if path is None and not os.environ.get(CACHE_ENV):  # fleet-superpowers models (once from discovered.yaml)
        from . import fleet_superpowers
        try:
            return fleet_superpowers.migrate('models') or None
        except Exception:  # never stop a start: an unusable inventory reads the old cache as before
            pass
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
        for provider, login in (row.get('providers') or {}).items():
            lines.append('      provider ' + provider + ': ' + {True: 'ready', None: 'login unknown'}.get(
                login.get('ok'), 'NOT usable (' + str(login.get('status')) + ': ' + str(login.get('reason')) + ')'))
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
    from . import fleet_superpowers
    where = cache_path() if os.environ.get(CACHE_ENV) else fleet_superpowers.file_path()
    lines.append('cached in ' + str(where) + '; `fleetmux setup` probes again')
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
