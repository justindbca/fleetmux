"""No new wall-clock deadline (docs/design.md section 35): a deadline or age measured on ``time.time()`` counts the
time the machine slept. New code measures waits with ``fleetmux.awake`` (``awake.now`` / ``since`` / ``deadline``
/ ``passed`` / ``from_wall``, or ``awake.mono`` inside one process).

The scan parses every module under ``fleetmux/`` and reports, per function, any use of the wall clock in a deadline
shape:
  * a comparison with ``time.time()`` or with a name bound to it in that function (``now = time.time()``,
    ``now = time.time() if now is None else now``);
  * ``+`` / ``-`` on such a value (a deadline being made, an age being taken);
  * ``time.time`` passed as a default clock (``clock=time.time``).
What is left is listed in ALLOWED by ``path::function`` with the reason it may stay on wall time.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = {'fleetmux/awake.py'}  # defines the clock

# Reasons a function may stay on wall time. "open" = a known remaining instance (docs/design.md section 35).
BUS = ('bus lease / message deadline (ms), shared with the bridges (Python, TypeScript) and Lua; '
       'held across a sleep by the section 35 wake grace; open')
DISPLAY = 'an elapsed or age figure for display or a log record; no decision'
POLL = 'a bounded poll or grace of a short-lived CLI or helper process, seconds long'
TTL = 'a cadence, cache or rate window; a sleep only makes it due early, which is harmless'
LIVENESS = "another process's liveness from its wall-clock beat; the beat is rewritten within one loop after a wake"
MTIME = 'compares with file mtimes (wall); open'
CHANNEL = 'channel adapters and the actor run on wall time (remote services, injectable clocks for tests)'
SECURITY = 'confirmation nonces and tickets expire in real time on purpose: a sleep only voids them (fail closed)'
WATCHDOG = 'watchdog-style stall window on busy_since; the daemon gap floors it (act_floor, section 24); open'

DEMO = ('section 61 `fleetmux demo`: a one-off foreground run of a scripted fleet in a temp home; its waits are '
          'seconds long and end with the command (no daemon deadline across a sleep)')

ALLOWED = {
    'fleetmux/demo.py::cleanup': DEMO,
    'fleetmux/demo.py::run': DEMO,
    'fleetmux/demo.py::settle_audit': DEMO,
    'fleetmux/demo.py::stop_terminal_server': DEMO,
    'fleetmux/demo.py::wait_closed': DEMO,
    'fleetmux/demo.py::wait_for_end': DEMO,
    'fleetmux/restart_state.py::deliver': 'section 60 ruling reminder: delivered_at is kept in the ledger file across '
                                         'daemon lives; a reminder right after a wake is harmless (it fails nothing)',
    'fleetmux/restart_state.py::prune': 'section 60: a temp file is judged by its mtime, which is wall time',
    'fleetmux/restart_state.py::tick': 'section 60 timer snapshot: last_timer_at is kept in state.json across daemon '
                                      'lives; an extra snapshot right after a wake is harmless (it fails nothing)',
    'fleetmux/subfleet.py::readopt_child': 'section 58: the job deadline moves by the wall time the root was down '
                                           '(suspended_at is written by an earlier process; sleep included on purpose)',
    'fleetmux/agent_bridge.py::daemon_beat': LIVENESS,
    'fleetmux/applet_view.py::tick': TTL + ' (tab check and retry cadence)',
    'fleetmux/brake.py::pool_reading': TTL,
    'fleetmux/channels/actor.py::drain': CHANNEL,
    'fleetmux/channels/actor.py::inbound_loop': CHANNEL,
    'fleetmux/channels/actor.py::outbound_loop': CHANNEL,
    'fleetmux/channels/actor.py::outbound_scan': CHANNEL,
    'fleetmux/channels/approve.py::answer_action': SECURITY,
    'fleetmux/channels/approve.py::answer_dialog': SECURITY,
    'fleetmux/channels/approve.py::handle_command': SECURITY,
    'fleetmux/channels/approve.py::register': SECURITY,
    'fleetmux/channels/approve.py::register_action': SECURITY,
    'fleetmux/channels/auditlog.py::__init__': CHANNEL,
    'fleetmux/channels/base.py::__init__': CHANNEL,
    'fleetmux/channels/base.py::_live': CHANNEL,
    'fleetmux/channels/base.py::login': CHANNEL,
    'fleetmux/channels/base.py::set': CHANNEL,
    'fleetmux/channels/cli.py::_beat_fresh': LIVENESS,
    'fleetmux/channels/cli.py::actor_main.on_term': POLL,
    'fleetmux/channels/cli.py::actor_main.watch_drain': POLL,
    'fleetmux/channels/email.py::__init__': CHANNEL,
    'fleetmux/channels/limits.py::__init__': CHANNEL,
    'fleetmux/channels/stop.py::handle': CHANNEL,
    'fleetmux/channels/strangers.py::taint_snapshot': CHANNEL,
    'fleetmux/channels/strangers.py::watch': CHANNEL,
    'fleetmux/channels/supervise.py::__init__': CHANNEL,
    'fleetmux/control_view.py::_snapshot': DISPLAY,
    'fleetmux/fleet_superpowers_hot.py::flush': TTL,
    'fleetmux/fleet_superpowers_hot.py::ingest_quota': MTIME,
    'fleetmux/fleet_superpowers_refresh.py::daemon_tick': TTL,
    'fleetmux/fleet.py::advice_finish': DISPLAY,
    'fleetmux/fleet.py::bring_window_to_front': POLL,
    'fleetmux/fleet.py::chrome': TTL,
    'fleetmux/fleet.py::midturn_order': BUS,
    'fleetmux/fleet.py::midturn_timeouts': BUS,
    'fleetmux/fleet.py::network_up': TTL,
    'fleetmux/fleet.py::reap_orphan_redis': MTIME,
    'fleetmux/fleet.py::rescue': DISPLAY,
    'fleetmux/fleet.py::split_apply': WATCHDOG,
    'fleetmux/fleet.py::split_due': WATCHDOG,
    'fleetmux/fleet.py::task_waiting_line': DISPLAY + ' (an awake deadline shown as a clock time)',
    'fleetmux/fleet.py::start_rotation': BUS + ' (handoff_deadline becomes the follow-up message deadline_at)',
    'fleetmux/fleet.py::tick': 'v1 loop: ' + BUS + '; liveness cadence (TTL)',
    'fleetmux/named_subfleet.py::revive': TTL + ' (crash window)',
    'fleetmux/named_subfleet.py::tick': TTL + ' (usage refresh)',
    'fleetmux/peer.py::broker': TTL,
    'fleetmux/relay/cli.py::cmd_status': DISPLAY + ' (the SEEN column of relay status)',
    'fleetmux/runtime_v2.py::daemon_outage': DISPLAY,
    'fleetmux/runtime_v2.py::heartbeat_check': BUS + ' (bridge heartbeat key TTL, hb_grace_until)',
    'fleetmux/runtime_v2.py::midturn_split': BUS + ' (handoff_deadline becomes the follow-up message deadline_at)',
    'fleetmux/runtime_v2.py::renew_tui': BUS,
    'fleetmux/runtime_v2.py::settle': BUS,
    'fleetmux/runtime_v2.py::tick': BUS + '; liveness cadence (TTL)',
    'fleetmux/session_board.py::board_state': MTIME,
    'fleetmux/session_env.py::route_closed': TTL + ' (port probe cadence)',
    'fleetmux/subfleet.py::follow_jump': TTL,
    'fleetmux/subfleet.py::tick': POLL + ' (launch grace of a child daemon)',
    'fleetmux/tabs.py::open_all': TTL,
    'fleetmux/tabs.py::reopen': TTL,
    'fleetmux/tabs.py::request_restart': POLL,
    'fleetmux/tabs.py::watch': TTL,
    'fleetmux/watchdog.py::answer_user': SECURITY,
    'fleetmux/watchdog.py::revive_summary': DISPLAY,
    'fleetmux/watchdog.py::tick': TTL + ' (check cadence)',
}


def _wall_call(node):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'time'
            and isinstance(node.func.value, ast.Name) and node.func.value.id == 'time' and not node.args)


def _wall_value(node):
    """``time.time()``, or an expression that is it: ``a if c else time.time()``, ``x or time.time()``,
    ``time.time() + n``, ``int(time.time() * 1000)``."""
    if _wall_call(node):
        return True
    if isinstance(node, ast.IfExp):
        return _wall_value(node.body) or _wall_value(node.orelse)
    if isinstance(node, ast.BoolOp):
        return any(_wall_value(v) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        return _wall_value(node.left) or _wall_value(node.right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('float', 'int', 'round',
                                                                                           'max', 'min'):
        return any(_wall_value(a) for a in node.args)
    return False


class _Scan(ast.NodeVisitor):
    def __init__(self):
        self.stack, self.found = [], []

    def _func(self, node):
        tainted = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Assign):
                for target in n.targets:
                    if isinstance(target, ast.Name) and _wall_value(n.value):
                        tainted.add(target.id)
                    elif isinstance(target, ast.Tuple) and isinstance(n.value, ast.Tuple):
                        for t, v in zip(target.elts, n.value.elts):
                            if isinstance(t, ast.Name) and _wall_value(v):
                                tainted.add(t.id)
        self.stack.append((node.name, tainted))
        for default in node.args.defaults + node.args.kw_defaults:
            if (isinstance(default, ast.Attribute) and default.attr == 'time' and isinstance(default.value, ast.Name)
                    and default.value.id == 'time'):
                self.hit(node)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _func

    def _wallish(self, node):
        tainted = self.stack[-1][1] if self.stack else set()
        for n in ast.walk(node):
            if _wall_call(n) or (isinstance(n, ast.Name) and n.id in tainted):
                return True
        return False

    def visit_Compare(self, node):
        if (not all(isinstance(op, (ast.Is, ast.IsNot)) for op in node.ops)
                and any(self._wallish(x) for x in [node.left] + node.comparators)):
            self.hit(node)
        self.generic_visit(node)

    def visit_BinOp(self, node):
        if isinstance(node.op, (ast.Add, ast.Sub)) and (self._operand(node.left) or self._operand(node.right)):
            self.hit(node)
        self.generic_visit(node)

    def _operand(self, node):
        tainted = self.stack[-1][1] if self.stack else set()
        return _wall_call(node) or (isinstance(node, ast.Name) and node.id in tainted)

    def hit(self, node):
        self.found.append('.'.join(name for name, _ in self.stack) or '<module>')


def scan_source(text):
    """{function name: number of wall-clock deadline shapes} of one module's source."""
    s = _Scan()
    s.visit(ast.parse(text))
    out = {}
    for name in s.found:
        out[name] = out.get(name, 0) + 1
    return out


def scan(root=ROOT):
    out = {}
    for path in sorted((root / 'fleetmux').rglob('*.py')):
        rel = path.relative_to(root).as_posix()
        if rel in SKIP:
            continue
        for name in scan_source(path.read_text()):
            out[rel + '::' + name] = True
    return out


def test_the_scanner_finds_each_deadline_shape():
    planted = '''
import time

def wait_for(proc, wait_s):
    deadline = time.time() + wait_s
    while time.time() < deadline:
        pass

def stale(rec, now=None):
    now = time.time() if now is None else now
    return now - float(rec['at']) > 60

def poll(clock=time.time):
    return clock()

def fine(r):
    return int(time.time() * 1000), time.monotonic() + 5
'''
    assert set(scan_source(planted)) == {'wait_for', 'stale', 'poll'}


def test_no_wall_clock_deadline_outside_the_allowlist():
    found = scan()
    new = sorted(k for k in found if k not in ALLOWED)
    assert not new, ('wall-clock deadline or age (counts the time the machine slept); use fleetmux.awake, or add '
                     'the function to ALLOWED with the reason it may stay on wall time:\n  ' + '\n  '.join(new))
    stale = sorted(k for k in ALLOWED if k not in found)
    assert not stale, 'ALLOWED entries that no longer match anything (remove them):\n  ' + '\n  '.join(stale)


def test_a_planted_wall_clock_deadline_fails_the_scan(tmp_path):
    (tmp_path / 'fleetmux').mkdir()
    (tmp_path / 'fleetmux' / 'new_part.py').write_text(
        'import time\n\n\ndef lease_expired(rec):\n    return time.time() >= float(rec["expires_at"])\n')
    found = scan(tmp_path)
    assert [k for k in found if k not in ALLOWED] == ['fleetmux/new_part.py::lease_expired']
