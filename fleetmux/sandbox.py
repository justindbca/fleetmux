"""P8o worker sandbox and memory switch (brief v2.2 §8.2 P8o; C5 / C15 / C16 / C18 / U8 / U9 rulings).

Without a policy every function is a no-op: ``claude_settings`` -> {}, ``codex_flags`` -> [], ``os_wrap`` -> the argv
unchanged, ``startup_audits`` writes nothing. ``failIfUnavailable`` and the macOS / Linux wrappers are written as
configuration; whether this machine's CLIs honour them is a real-machine item, not claimed here.
"""
import json
import os
from pathlib import Path
import shutil
import sys

from fleetmux.channels import security


def platform_name():
    """Injectable: 'darwin' / 'linux' / ... (tests replace it)."""
    return sys.platform


def which(name):
    """Injectable shutil.which (tests replace it)."""
    return shutil.which(name)


def codex_capability(cfg, what):
    """Injectable: whether this machine's Codex accepts ``what`` (never inferred from --help or a version).
    Nothing is verified on this machine yet (U8), so the answer is False until a real-machine check sets it."""
    caps = (cfg or {}).get('codex_capabilities') if isinstance(cfg, dict) else None
    return isinstance(caps, dict) and caps.get(what) is True


def _sandbox(policy):
    return (policy or {}).get('sandbox') or {}


def BUILTIN_DENY_READ():
    """Generic credential locations (no business words), resolved against the current HOME and config_dir()."""
    from fleetmux import channels
    home = Path.home()
    return [str(home / p) for p in ('.ssh', '.gnupg', '.aws', '.config/gcloud', '.claude.json',
                                    '.claude/settings.json', '.codex/auth.json', '.netrc')] + [str(channels.config_dir())]


def deny_read(policy):
    extra = [str(Path(os.path.expanduser(str(p)))) for p in _sandbox(policy).get('deny_read') or []]
    return list(dict.fromkeys(BUILTIN_DENY_READ() + extra))


def _writable_roots(cfg, name):
    from fleetmux import roster
    roots = []
    for key in ('roster', 'roster_night'):
        path = cfg.get(key) if isinstance(cfg, dict) else None
        if not path:
            continue
        try:
            roots += roster.writable_roots(Path(path).read_bytes()).get(name) or []
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return [str(Path(os.path.expanduser(str(p)))) for p in roots]


def claude_settings(policy, cfg, name):
    """The settings keys P8o merges into a Claude slot's --settings; {} without a policy."""
    if policy is None:
        return {}
    from fleetmux import fleet
    sb, out = _sandbox(policy), {}
    if sb.get('mode') == 'claude_native':
        domains = [str(d) for d in sb.get('allow_domains') or []]
        deny = deny_read(policy)
        allow = [str(fleet.agent_cwd(cfg, name)), str(Path(cfg['run']) / 'workspace')] + _writable_roots(cfg, name)
        out['sandbox'] = {'enabled': True, 'allowUnsandboxedCommands': False, 'autoAllowBashIfSandboxed': True,
                          'failIfUnavailable': True,
                          'network': {'allowedDomains': domains,
                                      'allowUnixSockets': [str(cfg['redis_socket'])] if cfg.get('redis_socket') else []},
                          'filesystem': {'denyRead': deny, 'allowWrite': list(dict.fromkeys(allow))}}
        perms = ['Read(' + p + ')' for p in deny]
        if not domains:
            perms.append('WebFetch(domain:*)')
        out['permissions'] = {'deny': perms}
    if sb.get('memory', 'off') == 'off':
        out['autoMemoryEnabled'] = False
        out['env'] = {'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1'}
    return out


def settings_for(cfg, name):
    """claude_argv: claude_settings for the run's pinned policy ({} without one)."""
    return claude_settings(security.policy(cfg), cfg, name)


def merge_settings(data, extra):
    """claude_argv: merge ``claude_settings`` into the settings dict (env merged, other keys set). {} -> unchanged."""
    for key, value in extra.items():
        if key == 'env':
            data['env'] = dict(data.get('env') or {}, **value)
        else:
            data[key] = value
    return data


def codex_flags(policy, cfg=None):
    """``-c`` fragment that turns a Codex slot's network off (no allow_domains) or limits it to the domains when
    this machine's Codex accepts features.network_proxy; otherwise off. [] without a policy."""
    if policy is None:
        return []
    domains = [str(d) for d in _sandbox(policy).get('allow_domains') or []]
    if domains and codex_capability(cfg, 'network_proxy') is True:
        return ['-c', 'features.network_proxy.domains.allow=' + json.dumps(domains)]
    return ['-c', 'sandbox_workspace_write.network_access=false']


def actor_codex_flags(policy, cfg):
    """actor_argv: the codex_flags fragment only when sandbox.mode is not 'off'."""
    return codex_flags(policy, cfg) if policy is not None and _sandbox(policy).get('mode', 'off') != 'off' else []


def _q(path):
    return '"' + str(path).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _read_roots(cwd, cfg):
    roots = [cwd, str(Path(__file__).resolve().parents[1]), sys.prefix, sys.base_prefix]
    if isinstance(cfg, dict) and cfg.get('run'):
        roots.append(str(cfg['run']))
    return list(dict.fromkeys(str(r) for r in roots))


def os_wrap(argv, policy, cwd, cfg=None):
    """``mode: os``: wrap the CLI in sandbox-exec (macOS) or bwrap (Linux). No wrapper available -> raise (C5:
    never exec unsandboxed). Any other mode / no policy -> argv unchanged."""
    if policy is None or _sandbox(policy).get('mode') != 'os':
        return list(argv)
    cfg = cfg if isinstance(cfg, dict) else {}
    plat = platform_name()
    home = str(Path.home())
    workspace = str(Path(cfg['run']) / 'workspace') if cfg.get('run') else None
    sock = cfg.get('redis_socket')
    if plat == 'darwin':
        exe = which('sandbox-exec')
        if not exe:
            raise RuntimeError('sandbox.mode os: sandbox-exec not found; refusing to start unsandboxed')
        rules = ['(version 1)', '(allow default)', '(deny file-read* (subpath ' + _q(home) + '))']
        rules += ['(allow file-read* (subpath ' + _q(p) + '))' for p in _read_roots(str(cwd), cfg)]
        rules += ['(deny file-read* (subpath ' + _q(p) + '))' for p in deny_read(policy)]   # C16: no exemption
        writes = ' '.join('(subpath ' + _q(p) + ')' for p in [str(cwd)] + ([workspace] if workspace else []))
        rules += ['(deny file-write*)', '(allow file-write* ' + writes + ' (literal "/dev/null"))',
                  '(deny network*)', '(allow network-outbound (remote ip "*:443"))',
                  '(deny network-outbound (remote ip "localhost:*"))']
        if sock:
            rules.append('(allow network-outbound (literal ' + _q(sock) + '))')
        return [exe, '-p', '\n'.join(rules)] + list(argv)
    if plat.startswith('linux'):
        exe = which('bwrap')
        if not exe:
            raise RuntimeError('sandbox.mode os: bwrap not found; refusing to start unsandboxed')
        out = [exe, '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc', '--tmpfs', home]
        for p in [str(cwd)] + ([workspace] if workspace else []):
            out += ['--bind', p, p]
        if sock:
            out += ['--bind', os.path.dirname(str(sock)), os.path.dirname(str(sock))]
        out.append('--share-net' if _sandbox(policy).get('allow_domains') else '--unshare-net')
        return out + ['--'] + list(argv)
    raise RuntimeError('sandbox.mode os: no OS sandbox for platform ' + str(plat))


def _slot_cli(cfg, slot):
    profile = (cfg.get('worker_profiles') or {}).get(slot) if isinstance(cfg.get('worker_profiles'), dict) else None
    if isinstance(profile, dict) and profile.get('cli'):
        return profile['cli']
    if slot == 'advisor':
        return 'claude'
    return cfg.get('supervisor_cli' if slot == 'supervisor' else 'worker_cli') or 'codex'


def degraded(cfg, slot, policy):
    """Why this slot runs below the policy's protection ('' = not degraded). One reason string per slot."""
    cli, sb, why = _slot_cli(cfg, slot), _sandbox(policy), []
    if cli == 'pi':
        why.append('pi_no_gate')
    elif cli == 'codex':
        why.append('codex_hooks_unverified')                # U8: never counted as gated
    elif cli != 'claude':
        why.append(cli + '_no_gate')
    if sb.get('mode') == 'claude_native' and cli != 'claude' and cli != 'codex':
        why.append('no_native_sandbox')
    if sb.get('mode') == 'os' and sb.get('allow_domains') and platform_name().startswith('linux'):
        why.append('os_domains_not_enforced')
    return ','.join(why)


def _audit(f):
    """The daemon's audit writer: the v2 audit stream, else the legacy fleet log."""
    if getattr(f, 'v2', None) is not None:
        return f.v2.audit
    return lambda event, **fields: f.log(event, **fields)


def startup_audits(f):
    """C18 / U6 / U8: one security.sandbox_off per degraded slot, once per run start (per Fleet object).
    Called by Fleet.run before the loop starts (M172); no policy: nothing."""
    pol = security.policy(f.cfg)
    if pol is None:
        return []
    audit = _audit(f)
    done = f.__dict__.setdefault('_sandbox_off_audited', set())
    rows = []
    for slot in list(getattr(f, 'names', None) or []):
        if slot in done:
            continue
        why = degraded(f.cfg, slot, pol)
        if why:
            audit('security.sandbox_off', slot=slot, reason=why, mode=_sandbox(pol).get('mode', 'off'))
            rows.append(slot)
        done.add(slot)
    return rows


class SandboxUnavailable(RuntimeError):
    """sandbox.mode os and no OS sandbox on this machine: the slot is not started (C5)."""


def os_unavailable(policy):
    """'' when mode is not os or the platform's wrapper exists; else why the os sandbox cannot be applied."""
    if policy is None or _sandbox(policy).get('mode') != 'os':
        return ''
    plat = platform_name()
    tool = 'sandbox-exec' if plat == 'darwin' else 'bwrap' if plat.startswith('linux') else None
    if tool is None:
        return 'no OS sandbox for platform ' + str(plat)
    return '' if which(tool) else tool + ' not found'


def preflight(f, name):
    """Fleet.spawn, before any side effect (M172): under sandbox.mode os with no wrapper, write
    security.sandbox_off reason=unavailable through the daemon's audit writer and refuse the start.
    actor() would also refuse at exec (os_wrap raises) but has no audit writer. No policy / other mode: nothing."""
    pol = security.policy(f.cfg)
    why = os_unavailable(pol)
    if not why:
        return
    _audit(f)('security.sandbox_off', slot=name, reason='unavailable', detail=why, mode='os')
    raise SandboxUnavailable('spawn failed: ' + str(name) + ': sandbox.mode os: ' + why
                             + '; refusing to start the slot unsandboxed')
