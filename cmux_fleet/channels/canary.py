"""Decoy credential (package P8h; brief v2.2 §8.2 / §8.4; contracts §8 key table ``fx:channel:canary``; sec-2 survey §8.2).

P8h (supervisor M101 / M134 / M174): ``plant``, ``on_resume``, ``read_valid``, ``value``, ``trip`` and
``check_read`` (used by P8i ``guard``: audited, never blocked) are implemented. Off (no policy or ``canary.enabled: off``): ``plant`` / ``on_resume`` write nothing, delete nothing and
return None, ``value`` returns None (never reads an old Redis value or file), ``check_read`` returns False.

Interfaces (sec-2 test-plan §0):
- ``plant(run, policy, *, r=None) -> Path | None``: ``<run>/<canary.path>`` = ``CANARY_TOKEN=fmx-canary-<24 hex>``,
  0644, ``O_CREAT|O_EXCL|O_NOFOLLOW`` (never overwrites); ``SET fx:channel:canary`` (string).
- ``on_resume(run, old_policy, new_policy, *, r) -> Path | None``: keep a valid original value and SET it again;
  plant only when newly enabled; missing / bad / unreadable / moved file -> ValueError (resume refused); child runs
  (``parent_config``) skipped.
- ``read_valid(path) -> str | None``; ``value(r=None, run=None, policy=None) -> str | None``.
- ``check_read(tool_name, tool_input, cfg) -> bool``: a pure read of this run's canary file only.
- ``trip(r, item_id, policy, *, audit, notify)``: ``security.canary_tripped level=error``; ``limits.on_trip: pause``
  -> ``channels.stop.pause_service`` (P8m).
"""
import json
import os
import re
import sys
from pathlib import Path

from cmux_fleet import channels
from cmux_fleet.channels import security

_STUB = 'P8h: not implemented (A2\' stub)'
KEY = 'CANARY_TOKEN'
PREFIX = 'fmx-canary-'
_LINE = re.compile(KEY + r'=(' + PREFIX + r'[0-9a-f]{24})\n?')


def _off(policy):
    return policy is None or (policy.get('canary') or {}).get('enabled') != 'on'


def _rel(policy):
    """The configured relative path, checked again here (the parser is the first line): relative, no ``..``."""
    rel = (policy.get('canary') or {}).get('path') or security.DEFAULTS['canary']['path']
    if (not isinstance(rel, str) or not rel.strip() or rel.startswith(('/', '~'))
            or '..' in rel.replace('\\', '/').split('/')):
        raise ValueError('canary.path must be a relative path inside the run directory')
    return rel


def _is_child(run):
    try:
        cfg = json.loads((Path(run) / 'config.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return isinstance(cfg, dict) and bool(cfg.get('parent_config'))


def plant(run, policy, *, r=None):
    """Write ``<run>/<canary.path>`` = ``CANARY_TOKEN=fmx-canary-<24 hex>`` (0644, a new value each call; never
    overwrites: an existing file -> FileExistsError) and SET ``fx:channel:canary``. Returns the path."""
    if _off(policy):
        return None
    path = Path(run) / _rel(policy)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = PREFIX + os.urandom(12).hex()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o644)
    try:
        os.fchmod(fd, 0o644)
        os.write(fd, (KEY + '=' + value + '\n').encode('ascii'))
    finally:
        os.close(fd)
    if r is not None:
        r.set(channels.CANARY, value)
    return path


def read_valid(path):
    """The canary value when ``path`` holds exactly one valid canary line, else None (missing, unreadable, bad)."""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None
    m = _LINE.fullmatch(text)
    return m.group(1) if m else None


def _refuse(run, why):
    return ValueError(str(run) + ': canary ' + why + '; resume refused (restore the file, or set canary.enabled: off)')


def on_resume(run, old_policy, new_policy, *, r):
    """C1 / C1-R. Off -> nothing written or deleted, None. A child run (``parent_config``) -> skipped, None.
    Enabled before and now: the path must be unchanged and the file valid -> its original value is SET again;
    otherwise ValueError (names the run, never a value), nothing written. Newly enabled: a valid file already at
    the path is kept and SET, else a first ``plant`` (an invalid file there is refused, never overwritten)."""
    if _off(new_policy):  # off: nothing written, nothing deleted
        return None
    if _is_child(run):
        return None
    rel = _rel(new_policy)
    path = Path(run) / rel
    if not _off(old_policy):
        if _rel(old_policy) != rel:
            raise _refuse(run, 'path changed')
        value = read_valid(path)
        if value is None:
            raise _refuse(run, 'file missing, unreadable or invalid at ' + rel)
        r.set(channels.CANARY, value)
        return path
    if os.path.lexists(path):
        value = read_valid(path)
        if value is None:
            raise _refuse(run, 'file at ' + rel + ' is not a canary line')
        r.set(channels.CANARY, value)
        return path
    return plant(run, new_policy, r=r)


def value(r=None, run=None, policy=None):
    """This run's canary value (Redis first, then the file), or None; off -> None without reading anything."""
    if _off(policy):
        return None
    got = r.get(channels.CANARY) if r is not None else None
    if isinstance(got, bytes):
        got = got.decode('ascii', 'replace')
    if got:
        return got
    return read_valid(Path(run) / _rel(policy)) if run is not None else None


PURE_READERS = frozenset({'cat', 'head', 'tail', 'less', 'more', 'nl', 'tac', 'wc', 'strings', 'od', 'xxd',
                           'hexdump'})
_SHELL_META = re.compile(r'[;&|<>`$(){}\n\\]')


def _same_file(arg, target):
    try:
        return os.path.realpath(os.path.expanduser(str(arg))) == target
    except (TypeError, ValueError, OSError):
        return False


def check_read(tool_name, tool_input, cfg):
    """True only for a pure read of this run's canary file: ``Read`` / ``NotebookRead`` of exactly that path, or a
    Bash command that is one plain reader (``PURE_READERS``) whose every operand is that file (options and numbers
    allowed) -- no pipe, list, redirection, substitution or second path. Then the P8i gate lets it pass (no
    confirmation: blocking the read would spoil the probe) and audits ``security.canary_tripped kind=read``.
    Anything else (``| curl``, ``; rm``, ``cp``, ``> out``, a second file) is False and is classified as usual.
    Off / no policy / unreadable config -> False. Never raises."""
    try:
        policy = security.policy(cfg)
        if _off(policy) or not isinstance(tool_input, dict):
            return False
        target = os.path.realpath(str(Path(cfg['run']) / _rel(policy)))
        if tool_name in ('Read', 'NotebookRead'):
            paths = [tool_input.get(k) for k in ('file_path', 'notebook_path') if tool_input.get(k)]
            return bool(paths) and all(_same_file(p, target) for p in paths)
        if tool_name != 'Bash':
            return False
        cmd = tool_input.get('command')
        if not isinstance(cmd, str) or _SHELL_META.search(cmd):
            return False
        import shlex
        argv = shlex.split(cmd)
        if not argv or os.path.basename(argv[0]) not in PURE_READERS:
            return False
        operands = [a for a in argv[1:] if not a.startswith('-') and not a.isdigit()]
        return bool(operands) and all(_same_file(a, target) for a in operands)
    except Exception:  # noqa: BLE001 - a broken input is simply not a pure canary read
        return False


ALERT_TEXT = 'Fleet 安全告警：诱饵密钥出现在一条出站消息里（{id}），已整条拦下。{action}'
ACTION_TEXT = {'pause': '已暂停派工，查明后用 cmux-fleet resume 恢复。', 'alert': '按配置只告警、不暂停。'}
PAUSE_FAILED_TEXT = '暂停没有生效，请在电脑上检查。'


def trip(r, item_id, policy, *, audit, notify=None):
    """An outbound item carried the canary (secrets.guard already blocked it). ``limits.on_trip``: ``pause`` ->
    ``stop.pause_service`` (reason names the canary; never resumes on its own), ``alert`` -> no pause. Always one
    ``security.canary_tripped`` audit row (``level=error``, ``rule``, ``item_id``, ``action``, ``paused`` 1 / 0) and one
    ``security`` alert onto ``fx:channel:outq`` (``user_only``; no value, no matched text). A failing pause is recorded
    as ``paused=0`` and never undoes the block. Returns ``{'paused': bool}``. ``notify``: the alert's sink when there
    is no Redis client."""
    from cmux_fleet.channels import envelope, stop
    action = ((policy or {}).get('limits') or {}).get('on_trip') or security.DEFAULTS['limits']['on_trip']
    paused = False
    if action == 'pause' and r is not None:
        try:
            stop.pause_service(r, 'canary tripped: ' + str(item_id or ''))
            paused = True
        except Exception as exc:  # noqa: BLE001 - the item stays blocked; the audit says paused=0
            print('canary: pause failed: ' + type(exc).__name__, file=sys.stderr)
    if audit is not None:
        try:
            audit('security.canary_tripped', level='error', rule='canary', item_id=str(item_id or ''), action=action,
                  paused='1' if paused else '0')
        except Exception:  # noqa: BLE001
            pass
    text = ALERT_TEXT.format(id=item_id or '', action=ACTION_TEXT.get(action, '') if (paused or action != 'pause')
                             else PAUSE_FAILED_TEXT)
    alert = envelope.outbound('security', target='last', text=text, user_only=True)
    try:
        if r is not None:
            r.lpush(channels.OUTQ, envelope.dumps(alert))
        elif notify is not None:
            notify(alert)
    except Exception as exc:  # noqa: BLE001
        print('canary: alert not queued: ' + type(exc).__name__, file=sys.stderr)
    return {'paused': paused}
