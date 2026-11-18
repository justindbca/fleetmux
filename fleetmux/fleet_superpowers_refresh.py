"""``fleetmux superpowers refresh``: rewrite this machine's own ``machines`` entry from local facts.

Sources (docs/distributed-development.md, "machines entry schema"), no new prober:

* ``clis``, ``models``: the ``fleetmux setup`` cache (fleetmux/discover.py ``load``).
* ``fleets``: ``transport_inventory.inventory()`` and each run's ``config.json``.
* ``models[].reachable``: for ``claude`` and ``codex``, one HTTPS request to the CLI's API host per refresh
  (``reach_api``; any HTTP answer counts as reachable). When a running root fleet of this machine routes the CLI
  through an applet (its run config's ``session_env``, fleetmux/session_env.py), the request goes through that
  route's ``HTTPS_PROXY``, the way the sessions go, and the row records ``via: <applet>``; otherwise it goes
  direct (no proxy from the environment either) with ``via: direct``. ``answered`` records the HTTP class seen
  (``2xx`` / ``3xx`` / ``4xx`` / ``5xx``) or ``none``, so a 401 or 429 is visible. For other CLIs, the cached
  login state and, for pi, the cached gateway state.

``reach`` is written by the owner (``superpowers add <self>`` or by hand): refresh keeps the value stored when it
writes, read under the file lock. Every other entry of the file is left byte for byte
(fleetmux/fleet_superpowers.py ``write_self``). Every source is a parameter, so tests never probe, list processes
or open a connection.

Each root fleet daemon calls ``daemon_tick`` on every tick: it refreshes the entry once shortly after the daemon
starts (``START_DELAY``) and then whenever the entry is older than ``hot.refresh_seconds`` (same YAML file, default
600; 0 = off), in a background thread, reusing probe answers younger than that interval (never a ``none``). A non-blocking lock on
``<file>.lock.refresh`` lets one daemon per machine refresh at a time; the entry's ``updated_at`` tells the others
it is fresh. A machine without a self entry is skipped (the first refresh needs ``--name``).
"""
import datetime
import fcntl
import ipaddress
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import fleet_superpowers as fs, paths, releases

ROOT = Path(__file__).resolve().parents[1]
API_HOSTS = {'claude': 'https://api.anthropic.com', 'codex': 'https://api.openai.com'}
# Where a CLI talks to with a given login (setup cache ``auth_method``), when not its API host. A codex logged in
# with ChatGPT talks to the ChatGPT backend: the codex-cli 0.164.0 binary carries
# https://chatgpt.com/backend-api/codex as its ChatGPT base URL and https://api.openai.com/v1 as the API one.
LOGIN_HOSTS = {('codex', 'chatgpt'): 'https://chatgpt.com/backend-api/codex'}


def probe_url(cli, auth_method):
    """The URL the reach probe of ``cli`` asks: the host of its login method, else its API host."""
    return LOGIN_HOSTS.get((cli, auth_method), API_HOSTS[cli])
PROBE_TIMEOUT = 5.0  # seconds for the one HTTPS request per CLI


def reach_api(url, proxy=None, timeout=PROBE_TIMEOUT):
    """The HTTP class the host answered with (``'2xx'`` ... ``'5xx'``; an HTTP error status is an answer), or
    None on no connection. ``proxy``: the HTTPS proxy to go through; without it the request goes direct and
    ignores proxy variables in the environment."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': proxy, 'http': proxy} if proxy else {}))
    try:
        answer = opener.open(urllib.request.Request(url, method='HEAD'), timeout=timeout)
    except urllib.error.HTTPError as exc:
        return '%dxx' % (exc.code // 100)
    except (OSError, ValueError):
        return None
    try:
        return '%dxx' % (int(getattr(answer, 'status', 200)) // 100)
    finally:
        answer.close()


def short_name(hostname=None):
    """The default machine name of a first refresh: the host name up to the first dot, lower case."""
    return (hostname or socket.gethostname()).split('.')[0].lower()


def local_addresses(hostname=None):
    """[{kind, address}] of the host name's IPv4 addresses (informational; [] when it does not resolve)."""
    try:
        infos = socket.getaddrinfo(hostname or socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    out = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback or any(r['address'] == str(ip) for r in out):
            continue
        kind = ('overlay' if ip in ipaddress.ip_network('100.64.0.0/10')
                else 'lan' if ip.is_private else 'public')
        out.append({'kind': kind, 'address': str(ip)})
    return out


def _tilde(path):
    path, home = Path(path), Path.home()
    try:
        return '~/' + str(path.relative_to(home))
    except ValueError:
        return str(path)


def local_bin():
    """The ``fleetmux`` command of this machine: the default link when it exists, else this release's binary."""
    if Path(fs.DEFAULT_BIN).expanduser().exists():
        return fs.DEFAULT_BIN
    own = ROOT / 'bin' / 'fleetmux'
    return _tilde(own) if own.exists() else fs.DEFAULT_BIN


def clis_of(discovered):
    """{cli: {version, logged_in}} of the installed CLIs in the setup cache."""
    rows = (discovered or {}).get('clis') or {}
    return {name: {'version': row.get('version'), 'logged_in': row.get('logged_in')}
            for name, row in rows.items() if isinstance(row, dict) and row.get('installed')}


def routes_of(found):
    """{cli: {applet, proxy}}: the session routes of this machine's running root fleets (their run configs'
    ``session_env``, only CLIs ``session_env.routes`` accepts). ``proxy`` is the route's ``HTTPS_PROXY`` (or
    ``https_proxy``), None when it sets none: the probe then goes direct. The first fleet wins per CLI."""
    from . import session_env
    out = {}
    for item in (found or {}).get('instances') or []:
        configs = item.get('configs') or []
        if not item.get('observed_running') or not configs:
            continue
        try:
            cfg = json.loads(Path(configs[0]).read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(cfg, dict) or cfg.get('parent_config') or not cfg.get('session_env'):
            continue
        for cli in API_HOSTS:
            try:
                found_routes = session_env.routes(cfg, cli)
            except (ValueError, TypeError, AttributeError):
                continue
            if cli in out or not found_routes:
                continue
            names = found_routes[0].get('vars') or {}
            proxy = names.get('HTTPS_PROXY') or names.get('https_proxy') or None
            out[cli] = {'applet': found_routes[0].get('applet'), 'proxy': proxy}
    return out


def _age(now, stamp):
    """Seconds from ISO ``stamp`` to ISO ``now``; None when either does not parse."""
    try:
        return (datetime.datetime.fromisoformat(now) - datetime.datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return None


def _reusable(previous, cli, url, via, now, max_age):
    """A previous probed row of ``cli`` for the same URL and route that got an answer, younger than ``max_age``
    seconds, or None. A ``none`` answer is never reused: the next refresh probes again."""
    for row in previous or [] if max_age else []:
        if (isinstance(row, dict) and row.get('cli') == cli and row.get('probe_host') == url
                and row.get('via') == via and row.get('answered') not in (None, 'none')):
            age = _age(now, row.get('checked_at'))
            return row if age is not None and 0 <= age < max_age else None
    return None


def models_of(discovered, probe, now, routes=None, previous=None, max_age=None):
    """[{id, cli, source, reachable, checked_at}] from the setup cache. ``source``: ``card`` for claude (the setup
    cache copies the model cards with cli=claude; nothing asks the account) or ``listed`` (the CLI listed it). No
    model is probed on its own, so nothing spends quota. One ``probe(url, proxy)`` call per CLI in
    API_HOSTS, at ``probe_url`` (the host of the CLI's login method), through the CLI's route in ``routes``
    (``routes_of``) when it has one with a proxy. Probed rows also carry ``probe_host`` (the URL asked), ``via``
    (the applet, or ``direct``) and ``answered`` (the HTTP class, or ``none``). With ``max_age``, a row of
    ``previous`` (the stored models) for the same CLI, URL and route younger than that is reused, not probed."""
    discovered, routes = discovered or {}, routes or {}
    rows, gateways = discovered.get('clis') or {}, discovered.get('gateways') or {}
    out, probed = [], {}
    for cli, row in rows.items():
        if not isinstance(row, dict) or not row.get('installed'):
            continue
        for model in row.get('models') or []:
            logged = row.get('logged_in') is not False
            extra = {}
            if cli in API_HOSTS:
                if cli not in probed:
                    route = routes.get(cli) or {}
                    proxy = route.get('proxy')
                    url = probe_url(cli, row.get('auth_method'))
                    via = route.get('applet') if proxy else 'direct'
                    old = _reusable(previous, cli, url, via, now, max_age)
                    seen = old['answered'] if old else (probe(url, proxy) or 'none')
                    probed[cli] = ({'probe_host': url, 'via': via, 'answered': seen},
                                   old['checked_at'] if old else now)
                extra, at = probed[cli]
                reachable = logged and extra['answered'] != 'none'
            else:
                gateway = gateways.get(str(model).split('/', 1)[0], {}) if cli == 'pi' else {}
                reachable, at = logged and gateway.get('online') is not False, discovered.get('probed_at') or now
            out.append(dict({'id': str(model), 'cli': cli, 'source': 'card' if cli == 'claude' else 'listed',
                             'reachable': bool(reachable), 'checked_at': str(at)}, **extra))
    return out


def _fleet_name(home):
    home = Path(home)
    if home == paths.default_home().resolve() or home == paths.default_home():
        return 'main'
    return home.name


def _root_name(cfg, fallback):
    """The fleet name of a sub-fleet's root (its ``root_config``, else ``parent_config``), else ``fallback``."""
    for key in ('root_config', 'parent_config'):
        try:
            home = json.loads(Path(cfg[key]).read_text()).get('fleet_home') if cfg.get(key) else None
        except (OSError, ValueError, AttributeError):
            home = None
        if home:
            return _fleet_name(home)
    return fallback


def fleets_of(found):
    """[{name, home, config, mode, running, release, root}] from a transport inventory: every running run, plus
    the run each fleet home's latest.json points at. A sub-fleet inherits its root's fleet home and so its name:
    its row has ``root: false``, ``parent`` (the root's name) and ``sub`` (its id, e.g. ``M0``); a root fleet's
    row has ``root: true``."""
    out = []
    for item in (found or {}).get('instances') or []:
        configs = item.get('configs') or []
        if not configs:
            continue
        try:
            cfg = json.loads(Path(configs[0]).read_text())
        except (OSError, ValueError):
            continue
        home = cfg.get('fleet_home')
        running = bool(item.get('observed_running'))
        latest = None
        if home:
            try:
                data = json.loads((Path(home) / 'latest.json').read_text())
                latest = data.get('config') or (data.get('run') and str(Path(data['run']) / 'config.json'))
            except (OSError, ValueError, AttributeError):
                latest = None
        if not running and (not latest or Path(latest) != Path(configs[0])):
            continue
        release = releases.running(cfg)  # what the run's daemon runs, never ~/fleetmux-current
        name = _fleet_name(home) if home else Path(item['run']).name
        row = {'name': name, 'home': home, 'config': str(configs[0]),
               'mode': item.get('mode') or cfg.get('mode') or 'task', 'running': running, 'release': release,
               'root': not cfg.get('parent_config')}
        if cfg.get('parent_config'):
            row.update(parent=_root_name(cfg, name), sub=str(cfg.get('task_id') or Path(item['run']).name))
        out.append(row)
    return out


def entry(previous, discovered, found, probe, now, hostname, addresses, bin_, release, max_probe_age=None):
    """This machine's entry, built from the given facts (pure). ``reach`` comes from ``previous``, and so do probe
    answers younger than ``max_probe_age`` seconds."""
    clis = clis_of(discovered)
    models = models_of(discovered, probe, now, routes_of(found), (previous or {}).get('models'), max_probe_age)
    out = {'source': 'self', 'updated_at': now, 'hostname': hostname, 'addresses': addresses,
           'reach': list((previous or {}).get('reach') or []),
           'fleetmux': {'bin': bin_, **({'release': release} if release else {})},
           'clis': clis, 'models': models,
           'can_start_claude': bool((clis.get('claude') or {}).get('logged_in') is True
                                    and any(m['cli'] == 'claude' and m['reachable'] for m in models)),
           'fleets': fleets_of(found)}
    return out


def refresh(name=None, path=None, probe=reach_api, discovered=None, found=None, now=None, hostname=None,
            addresses=None, bin_=None, release=None, max_probe_age=None):
    """Rewrite ``machines.<self>`` and return ``(name, entry)``. The first refresh needs ``name``: workflows and
    other machines name this machine verbatim, so a guessed host name would not match them. Later refreshes use
    the stored self name. Every fact not passed in is read from this machine."""
    from . import discover, transport_inventory
    current = fs.self_name(path)
    if not name and not current:
        suggested = short_name(hostname)
        raise fs.InventoryError('the first refresh needs the machine name that workflows and other machines use '
                                'for this one (suggested: %s): fleetmux superpowers refresh --name %s'
                                % (suggested, suggested))
    name = name or current
    previous = fs.machine(name, path) if current == name else None
    hostname = hostname or socket.gethostname()
    built = entry(previous,
                  discover.load() if discovered is None else discovered,
                  transport_inventory.inventory() if found is None else found,
                  probe, now or fs.now_iso(), hostname,
                  local_addresses(hostname) if addresses is None else addresses,
                  bin_ or local_bin(), release if release is not None else releases.installed() or releases.own(),
                  max_probe_age)
    fs.write_self(built, name=name, path=path, keep_reach=True)
    from . import fleet_superpowers_capability as capability  # hardware, always_on, toolchains: their own keys
    built.update(capability.write(name, path))
    return name, built


# -- refresh from the root fleet daemon ------------------------------------------------------------------

NOT_SET_UP_RETRY = 60.0  # seconds before a daemon looks again: no self entry, refresh off, or a broken file
START_DELAY = 30.0  # seconds after daemon start before its first refresh: the applets it starts come up first
BUSY_RETRY = 30.0  # seconds before a daemon whose start refresh found the lock held tries again


def _try_lock():
    """An fd holding ``<file>.lock.refresh`` exclusively, or None when another process holds it."""
    target = Path(str(fs.lock_path()) + '.refresh')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _spawn(work):
    thread = threading.Thread(target=work, name='superpowers-refresh', daemon=True)
    thread.start()
    return thread


def daemon_tick(state, now=None, run=None, spawn=_spawn):
    """One root fleet daemon's share of keeping ``machines.<self>`` fresh; ``state`` is that daemon's own dict.
    Returns what it did: ``started``, ``running``, ``waiting``, ``fresh``, ``busy``, ``off``, ``no-self`` or
    ``failed: <error>`` (a failed background refresh, or an inventory file that cannot be read; each distinct
    error once). Never probes in the caller's thread: the refresh runs in ``spawn(work)``. The first refresh
    comes ``START_DELAY`` after the first call; a start refresh that finds another daemon refreshing retries
    after ``BUSY_RETRY``."""
    now = time.time() if now is None else now
    if state.get('error'):
        return 'failed: ' + state.pop('error')
    thread = state.get('thread')
    if thread is not None and thread.is_alive():
        return 'running'
    state.setdefault('next', now + START_DELAY)
    if now < state['next']:
        return 'waiting'
    try:
        interval = fs.refresh_seconds()
        name = fs.self_name() if interval else None
        updated = (fs.machine(name) or {}).get('updated_at') if name else None
    except fs.InventoryError as exc:
        state['next'] = now + NOT_SET_UP_RETRY
        if state.get('reported') == str(exc):
            return 'waiting'
        state['reported'] = str(exc)
        return 'failed: ' + str(exc)
    state.pop('reported', None)
    if not interval or name is None:
        state['next'] = now + NOT_SET_UP_RETRY
        return 'off' if not interval else 'no-self'
    if state.get('started'):
        try:
            age = now - datetime.datetime.fromisoformat(updated).timestamp()
        except (TypeError, ValueError):
            age = interval
        if age < interval:
            state['next'] = now + interval - age
            return 'fresh'
    fd = _try_lock()
    if fd is None:
        state['next'] = now + (interval if state.get('started') else BUSY_RETRY)
        return 'busy'
    state['started'], state['next'] = True, now + interval

    def work():
        try:
            (run or refresh)(max_probe_age=interval)
        except Exception as exc:  # reported by the next tick, never raised into the daemon
            state['error'] = repr(exc)[:300]
        finally:
            os.close(fd)
    state['thread'] = spawn(work)
    return 'started'
