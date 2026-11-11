"""Checked terminal failures preserve the existing Redis delivery/held protocol.

Only fixture-owned private Redis and in-memory terminals; no fleet processes.
"""
import pytest

from fleetmux import fleet

from test_claude_editor_send import editor_backend  # noqa: F401
from test_claude_supervisor import service_fleet, no_trust, event  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401

# §48 (user 2026-11-09:「任何场景都不用 claude 输入框打字」): these tests exercised a channel that typed into a
# Claude input box; it is refused at start now (tests/test_no_claude_typing.py covers the refusal).
RETIRED = pytest.mark.skip(reason='docs/design.md §48: fleet never types into a Claude input box (2026-11-09)')


def _legacy_typed_run(f):
    """Since the hook line, new v2 runs deliver to a Claude supervisor by Stop hook; the typed channel these tests
    check is the one old/resumed v2 runs (no launch marker) keep."""
    cfg = fleet.load(f.path)
    cfg.pop('claude_supervisor_hook_delivery', None)
    fleet.save(f.path, cfg)
    f.cfg.pop('claude_supervisor_hook_delivery', None)
    if getattr(f, 'ready_hook', None):
        f.ready_hook.kill()



@RETIRED
@pytest.mark.parametrize('failure', ['corrupt', 'busy', 'unreadable', 'restore'])
def test_checked_send_failure_holds_delivery_and_records_reason(service_fleet, editor_backend, failure):
    f = service_fleet
    _legacy_typed_run(f)
    mgr, editor = editor_backend
    f.mgr = mgr
    f.setf('supervisor', pane='p')
    editor.text = ''
    if failure == 'corrupt':
        editor.corrupt_paste = True
    elif failure == 'busy':
        editor.busy = True
    elif failure == 'unreadable':
        editor.readable = False
    else:
        editor.restore = True
    mid = f.v2.queue('supervisor', 'validated request', kind='followup')
    f.v2.deliver('supervisor')
    assert f.agent('supervisor')['state'] == 'held' and f.paused()
    lease = f.v2.bus.slot_state('supervisor')
    assert lease['status'] == 'uncertain' and lease['message_id'] == mid
    msg = f.v2.bus.message(mid)
    assert msg['status'] == 'uncertain'
    reason = f.r.hget(f.v2.bus.msg_key(mid), 'uncertain_reason')
    assert 'Claude input held:' in reason
    events = f.v2.bus.audit_tail(100)
    assert any(row['event'] == 'execution.held' and 'Claude input held:' in row['error'] for _, row in events)
    assert not any(row['event'] == 'tui.typed' for _, row in events)
    before = list(editor.pastes)
    f.v2.deliver('supervisor')
    assert editor.pastes == before  # a held delivery is not retried
    assert editor.submitted == (['validated request'] if failure == 'restore' else [])


@RETIRED
def test_failed_startup_nudge_is_held_instead_of_silently_ignored(service_fleet, editor_backend):
    f = service_fleet
    _legacy_typed_run(f)
    mgr, editor = editor_backend
    f.mgr = mgr
    editor.busy = True
    f.setf('supervisor', pane='p', state='starting', runtime_ready=0)
    event(f, dict(kind='startup', agent='supervisor', generation=0, run_id=f.v2.bus.run_id,
                  thread=f.test_threads['supervisor'], text='not ready yet', failed=False, aborted=False))
    assert f.agent('supervisor')['state'] == 'held' and f.paused()
    assert not editor.pastes and not editor.keys
    assert any(row['event'] == 'execution.held' and 'startup input unconfirmed:' in row['error']
               for _, row in f.v2.bus.audit_tail(100))
