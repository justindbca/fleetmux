"""The security pack's machine-local policy (brief v2.2 §8.1; package P8a; contracts §8 「安全包」).

``security.yaml`` lives in ``channels.config_dir()`` (``start --security <path>`` elsewhere, ``--no-security`` off),
never in a roster: a supervisor may edit its roster, so a policy there would let an injected supervisor widen its
own powers. ``start`` parses it once with ``parse_security`` and pins the result into the run ``config.json`` under
``security`` (no hot reload). Every reader asks ``policy(cfg)``: ``None`` means no ``security.yaml`` and is the
"behaviour unchanged" branch of every P8 package.

Every default below is a parameter (the six points the notes put to the user use their recommended values:
confirm.high desk, sandbox.mode off, delete / send_out / config / install / secret_read normal and real_money high,
stop.level interrupt, links.allow [] , the limits 60 / 30 / 300 / 400 / pause). Error texts name the key and the
allowed values, never the value written.
"""
import copy
import re

ON_OFF = ('on', 'off')
CLASSES = ('delete', 'send_out', 'config', 'install', 'secret_read', 'real_money')
TIERS = ('off', 'normal', 'high')
STALL_WINDOW_S = 300  # the default stall_window; confirm.wait_s must stay below the run's

DEFAULTS = {
    'provenance': {'spotlight': 'on'},
    'reader': {'mode': 'on', 'model': None, 'timeout_s': 120, 'max_chars': 32000, 'raw_to_supervisor': 'off'},
    'untrusted_actions': 'confirm',
    'strangers': {'alert_after': 5, 'window_s': 600, 'cooldown_s': 3600},
    'secrets': {'rules': 'builtin', 'values_from': ['accounts'], 'variants': ['base64', 'hex', 'url'],
                'on_hit': 'block'},
    'links': {'allow': [], 'mode': 'defang', 'preview': 'off'},
    'canary': {'enabled': 'off', 'path': 'workspace/.secrets/canary.env'},
    'confirm': {'wait_s': 240, 'ticket_ttl_s': 1800,
                'classes': {'delete': 'normal', 'send_out': 'normal', 'config': 'normal', 'install': 'normal',
                            'secret_read': 'normal', 'real_money': 'high'},
                'patterns': {}, 'high': 'desk'},
    'stop': {'level': 'interrupt'},
    'limits': {'inbound_per_sender_10m': 60, 'requests_per_h': 30, 'outbound_per_h': 300,
               'tool_calls_per_delivery': 400, 'on_trip': 'pause'},
    'audit_log': {'enabled': 'on', 'dir': 'audit', 'uappend': 'on',
                  'events': ['channel.*', 'security.*', 'dialog.*', 'execution.*', 'hook.*', 'service.*']},
    'sandbox': {'mode': 'off', 'allow_domains': [], 'deny_read': [], 'memory': 'off', 'codex_hooks': 'off'},
}
# reader.model None = the run config's worker_model (resolved by the reader, P8c)
POLICY_KEYS = {k: (tuple(v) if isinstance(v, dict) else ()) for k, v in DEFAULTS.items()}

_CHOICES = {
    ('provenance', 'spotlight'): ON_OFF, ('reader', 'mode'): ON_OFF, ('reader', 'raw_to_supervisor'): ON_OFF,
    ('untrusted_actions',): ('confirm', 'deny'), ('secrets', 'rules'): ('builtin',), ('secrets', 'on_hit'): ('block',),
    ('links', 'mode'): ('defang', 'strip'), ('links', 'preview'): ON_OFF, ('canary', 'enabled'): ON_OFF,
    ('confirm', 'high'): ('desk', 'second_channel', 'totp'), ('stop', 'level'): ('pause', 'interrupt', 'kill'),
    ('limits', 'on_trip'): ('pause', 'alert'), ('audit_log', 'enabled'): ON_OFF,
    ('audit_log', 'uappend'): ON_OFF, ('sandbox', 'mode'): ('off', 'claude_native', 'os'),
    ('sandbox', 'memory'): ON_OFF, ('sandbox', 'codex_hooks'): ON_OFF,
}
_POSITIVE = {('reader', 'timeout_s'), ('reader', 'max_chars'), ('strangers', 'alert_after'), ('strangers', 'window_s'),
             ('strangers', 'cooldown_s'), ('confirm', 'wait_s'), ('confirm', 'ticket_ttl_s'),
             ('limits', 'inbound_per_sender_10m'), ('limits', 'requests_per_h'), ('limits', 'outbound_per_h'),
             ('limits', 'tool_calls_per_delivery')}
_STR_LISTS = {('links', 'allow'), ('audit_log', 'events'), ('sandbox', 'allow_domains'), ('sandbox', 'deny_read')}
_BUDGET_WORDS = ('usd', 'cost', 'budget', 'calls', 'rounds', 'hours', 'tokens')


def _name(path):
    return '.'.join(path)


def _norm(value):
    """YAML 1.1 reads on / off as booleans: give them back as 'on' / 'off'."""
    if value is True:
        return 'on'
    if value is False:
        return 'off'
    return value


def _check(path, value, errors):
    where = _name(path)
    if path in _CHOICES:
        if _norm(value) not in _CHOICES[path]:
            errors.append(where + ': must be one of ' + ' / '.join(_CHOICES[path]))
            return None
        return _norm(value)
    if path in _POSITIVE:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            errors.append(where + ': must be a positive integer')
            return None
        return value
    if path in _STR_LISTS:
        if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
            errors.append(where + ': must be a list of non-empty strings')
            return None
        return list(value)
    if path == ('reader', 'model'):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            errors.append(where + ': must be a model id (or absent: the run worker_model)')
            return None
        return value
    if path == ('secrets', 'values_from'):
        ok = isinstance(value, list) and all(isinstance(x, str) and (
            x == 'accounts' or re.fullmatch(r'env:[A-Za-z_][A-Za-z0-9_]*', x) or re.fullmatch(r'(file|glob):.+', x))
            for x in value)
        if not ok:
            errors.append(where + ': must be a list of accounts / env:NAME / file:<path> / glob:<pattern>')
            return None
        return list(value)
    if path == ('secrets', 'variants'):
        if not isinstance(value, list) or not set(value) <= {'base64', 'hex', 'url'}:
            errors.append(where + ': must be a list from base64 / hex / url')
            return None
        return list(value)
    if path in (('canary', 'path'), ('audit_log', 'dir')):
        if (not isinstance(value, str) or not value.strip() or value.startswith(('/', '~'))
                or '..' in value.replace('\\', '/').split('/')):
            errors.append(where + ': must be a relative path inside the run directory')
            return None
        return value
    if path == ('confirm', 'classes'):
        if not isinstance(value, dict):
            errors.append(where + ': must map ' + ' / '.join(CLASSES) + ' to ' + ' / '.join(TIERS))
            return None
        out = dict(DEFAULTS['confirm']['classes'])
        for k, v in value.items():
            if k not in CLASSES:
                errors.append(where + ': unknown class (known: ' + ', '.join(CLASSES) + ')')
            elif _norm(v) not in TIERS:
                errors.append(where + '.' + str(k) + ': must be one of ' + ' / '.join(TIERS))
            else:
                out[k] = _norm(v)
        return out
    if path == ('confirm', 'patterns'):
        if not isinstance(value, dict):
            errors.append(where + ': must map a class to a list of regular expressions')
            return None
        out = {}
        for k, v in value.items():
            if k not in CLASSES:
                errors.append(where + ': unknown class (known: ' + ', '.join(CLASSES) + ')')
                continue
            if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
                errors.append(where + '.' + str(k) + ': must be a list of regular expressions')
                continue
            for x in v:
                try:
                    re.compile(x)
                except re.error:
                    errors.append(where + '.' + str(k) + ': a pattern is not a valid regular expression')
                    break
            else:
                out[k] = list(v)
        return out
    errors.append(where + ': unknown key')
    return None


def parse_security(text, *, stall_window=STALL_WINDOW_S):
    """``security.yaml`` text -> (policy, errors). An empty file is every default. Any error rejects the whole file
    (policy None). The policy is a full copy of DEFAULTS with the file's values in it."""
    import yaml
    try:
        data = yaml.safe_load(text or '')
    except yaml.YAMLError:
        return None, ['security.yaml: not valid YAML']
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, ['security.yaml: the top level must be a mapping of ' + ', '.join(DEFAULTS)]
    policy, errors = copy.deepcopy(DEFAULTS), []
    for key, value in data.items():
        if key not in DEFAULTS:
            errors.append(str(key) + ': unknown key (known: ' + ', '.join(DEFAULTS) + ')')
            continue
        if not POLICY_KEYS[key]:
            got = _check((key,), value, errors)
            if got is not None:
                policy[key] = got
            continue
        if not isinstance(value, dict):
            errors.append(key + ': must be a mapping of ' + ', '.join(POLICY_KEYS[key]))
            continue
        for sub, v in value.items():
            if sub not in POLICY_KEYS[key]:
                hint = ''
                if key == 'limits' and any(w in str(sub).lower() for w in _BUDGET_WORDS):
                    hint = ' — money / rounds / hours limits are the budget card (start --budget, §23 brake), not here'
                errors.append(key + '.' + str(sub) + ': unknown key (known: ' + ', '.join(POLICY_KEYS[key]) + ')'
                              + hint)
                continue
            got = _check((key, sub), v, errors)
            if got is not None:
                policy[key][sub] = got
    if policy['confirm']['wait_s'] >= stall_window:
        errors.append('confirm.wait_s: must be below the run stall_window, or a waiting worker is taken for stalled')
    return (None, errors) if errors else (policy, [])


def policy(cfg):
    """The run's pinned policy (``cfg['security']``), or None: no security.yaml, every reader behaves as before."""
    value = cfg.get('security') if isinstance(cfg, dict) else None
    return value if isinstance(value, dict) and value else None
