"""Advisor x the no-typing channels (Claude Stop-hook mailbox, Codex queue), merged 2026-10-14.

* Advisor notes to a Codex supervisor ride on a queued delivery: the queued text carries the opaque
  delivery-token line, never a paste of their own.
* A Claude worker held for its first sheet has no CLI yet (the actor waits before exec), so no Stop hook
  waits and the watchdog leaves it alone.
* An advisor respawn bumps the generation before the old tab is killed: a Stop hook of the old session that
  is still waiting lets go (``alive()`` false), never takes the new generation's mailbox, and a late Stop of
  the old generation never settles.

The advisor process is always a fake runner: no test starts `claude` or `codex`."""
import json
import os
import re
import time

from test_advisor import Runner, afx, item, report, send_decision, v2adv  # noqa: F401
from test_fleet import decision, turn
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from cmux_fleet import claude_hook, codex_queue, fleet, watchdog

MARK = re.compile(r'\n\n\(fleet delivery fx-[0-9a-f]{16}\)$')


def supervisor_turn(f, decision_json):
    """The supervisor answers its queued delivery (the conftest fake queue binds the turn)."""
    f.pump()  # deliver whatever waits for the supervisor (the task message first)
    assert codex_queue.record(f.agent('supervisor'))['cq_state'] == 'queued'
    f.handle(turn('supervisor', decision_json))


def test_advisor_notes_reach_a_codex_supervisor_only_inside_a_marked_queue_delivery(afx):
    f = afx
    assert codex_queue.is_codex(f.runtime_cfg('supervisor'), 'supervisor')
    f.advisor_runner = Runner(TimeoutError('advisor timed out after 90 s'))  # -> [advisor unavailable] note
    supervisor_turn(f, decision(dispatch=[item()]))
    f.pump()
    assert any(n.startswith('[advisor unavailable]') for n in f.r.lrange(fleet.ADVICE_NOTES, 0, -1))
    report(f, 'worker-1')
    f.pump()
    rec = codex_queue.record(f.agent('supervisor'))
    assert '[advisor unavailable] 按缺省派出 worker-1' in rec['cq_text'] and MARK.search(rec['cq_text'])
    assert rec['cq_text'].endswith('(fleet delivery ' + rec['cq_token'] + ')')
    assert f.r.llen(fleet.ADVICE_NOTES) == 0
    pane = f.agent('supervisor')['pane']
    assert not [t for p, t in f.mgr.sent if p == pane and '[advisor' in t]  # never typed


def test_consult_only_off_note_is_a_marked_queue_delivery_too(afx):
    f = afx
    supervisor_turn(f, decision(dispatch=[dict(item(), id='M0')]))
    f.pump()
    report(f, 'worker-1')
    f.pump()  # the report is queued to the supervisor
    assert codex_queue.record(f.agent('supervisor'))['cq_text'].startswith('[worker-1 report]')
    assert f.set_advisor(False, 'user', write=True) is None
    supervisor_turn(f, decision(dispatch=[], consult=[{'worker': 'worker-1', 'task': 'M0', 'finding': '两轮 403'}]))
    f.pump()
    rec = codex_queue.record(f.agent('supervisor'))
    assert rec['cq_text'].startswith('[advisor off] consult M0') and MARK.search(rec['cq_text'])


# ------------------------------------------------------------------ held first generation (v2)
def test_held_first_generation_has_no_hook_and_the_watchdog_leaves_it_alone(v2adv):
    f = v2adv
    info = f.agent('worker-1')
    assert info['state'] == 'starting' and info['launch_ready'] == '0' and info['advice_hold'] == '1'
    k = fleet.hook_keys(f.cfg, f.r, 'worker-1', int(info['generation']))
    assert not claude_hook.live(f.r, k) and not f.r.llen(k['mailbox'])  # no CLI yet: no Stop hook waits
    f.mgr.screen = lambda pane: ''  # whatever the held tab shows
    pressed = []
    f.mgr.send_key = lambda pane, key: pressed.append((pane, key)) or True
    f.cfg['stall_timeout'] = 1
    f.setf('worker-1', busy_since=time.time() - 3600, startup_deadline=time.time() - 3600)
    dog = watchdog.Watchdog(f.v2)
    for at in (time.time(), time.time() + 3600, time.time() + 7200):
        dog.next_check.clear()
        dog.tick(at)
        f.v2.tick()
    info = f.agent('worker-1')
    assert pressed == [] and info['state'] == 'starting' and info['advice_hold'] == '1'
    assert info.get('watch_state') == 'working'  # looked at, classified as starting without a delivery
    assert f.task().get('status') != 'blocked'


# ------------------------------------------------------------------ respawn while the old Stop hook waits (v1)
def test_respawn_lets_a_waiting_old_hook_go_and_its_late_stop_never_settles(afx):
    f = afx
    old_gen = int(f.agent('worker-1')['generation'])
    k_old = claude_hook.keys('fx', 'worker-1', old_gen)
    thread = 'old-session'
    k, alive, accept = fleet.hook_session(f.cfg, f.r, 'worker-1', old_gen, thread, os.getppid())
    assert alive()  # the old session's Stop hook would be waiting on its mailbox now
    f.advisor_runner = Runner({'model': 'opus', 'effort': 'high', 'domains': ['research_web']})
    send_decision(f, [item()])
    f.pump()  # fingerprint change: switch_profile bumps the generation, then retires the old tab
    info = f.agent('worker-1')
    assert int(info['generation']) == old_gen + 1 and info['state'] == 'starting'
    assert not alive()  # the waiting hook lets go on its next poll: nothing is handed to the old session
    text, entry = claude_hook.wait(f.r, k, old_gen, thread, time.monotonic() + 5, alive, accept, poll=1)
    assert (text, entry) == (None, None)
    # The successor's mailbox is keyed by its own generation: the old hook can never read it.
    assert claude_hook.keys('fx', 'worker-1', old_gen + 1)['mailbox'] != k_old['mailbox']
    # A late Stop of the old generation is stale: it neither settles nor reports.
    before = f.r.llen('fx:inbox:supervisor')
    f.handle(dict(turn('worker-1', 'Done:\n- late\nEvidence:\n- x\nNot done: none\nBlocked: none', old_gen),
                  msg_id=info.get('msg_id', '')))
    assert f.agent('worker-1')['state'] == 'starting' and f.r.llen('fx:inbox:supervisor') == before
    assert any(json.loads(x).get('kind') == 'turn.stale' for x in f.r.lrange(fleet.LOG, -20, -1))


# ------------------------------------------------------------------ batch review rules x advised layout
def test_advised_respawn_keeps_the_fleet_review_rules(afx):
    """REVIEWER.md (batch 816c28a1) is written before the advised kit; Kit.clear only touches .claude/."""
    f = afx
    f.advisor_runner = Runner({'model': 'opus', 'effort': 'high', 'domains': ['research_web']})
    send_decision(f, [item()])
    f.pump()  # advised respawn: worker-1 -> worker-1.1 with the research_web kit
    cwd = fleet.agent_cwd(f.cfg, 'worker-1')
    assert f.agent('worker-1')['generation'] == '1' and 'RULES OF research_web' in (cwd / 'CLAUDE.md').read_text()
    assert (cwd / 'REVIEWER.md').is_file() and 'REVIEWER.md' in (cwd / 'CLAUDE.md').read_text()

