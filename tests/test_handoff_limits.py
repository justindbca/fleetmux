"""Per-model handoff window / ratio (docs/design.md §22)."""
import json
import os

from test_fleet import FakeMgr, FakeRedis, THREADS, cfx, fx, seasoned, write_rollout  # noqa: F401
from cmux_fleet import control_view, fleet

CFG = {'claude_window': 200000, 'handoff_ratio': .6}


def transcript(f, name, used):
    path = f.run_dir / (name + '.jsonl')
    path.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': used}}}) + '\n')
    f.setf(name, transcript=str(path))


def roster(f, text, n=[0]):
    path = f.run_dir / 'roster.yaml'
    path.write_text(text)
    n[0] += 1
    os.utime(path, ns=(1_700_000_000_000_000_000 + n[0] * 10**9,) * 2)
    f.cfg['roster'] = str(path)
    f.roster_tick()


def deliver(f, name):
    """Queue one message and pump; return the agent state ('busy' = delivered, 'handoff' = rotating)."""
    seasoned(f, name)
    f.queue(name, 'next piece of work')
    f.pump()
    return f.agent(name)['state']


def test_builtin_table_and_flags():
    lim = fleet.handoff_limits
    assert lim('claude', 'opus', CFG)[:2] == (1000000, .6)
    assert lim('claude', 'claude-opus-4-1', CFG)[0] == 1000000
    assert lim('claude', 'fable', CFG)[0] == 1000000
    assert lim('claude', 'sonnet[1m]', CFG)[0] == 1000000
    assert lim('claude', 'sonnet', CFG)[0] == 1000000  # native 1M (code.claude.com/docs/en/model-config)
    assert lim('claude', 'haiku', CFG) == (200000, .6, 'window: --claude-window; ratio: --handoff-ratio')
    assert lim('claude', 'haiku', dict(CFG, claude_window=150000, handoff_ratio=.5))[:2] == (150000, .5)
    assert lim('codex', 'gpt-6', CFG)[:2] == (None, .6)  # measured from the rollout
    assert lim('pi', 'local/flash', CFG)[0] is None


def test_resolution_order_worker_roster_config_builtin():
    lim = fleet.handoff_limits
    cfg = dict(CFG, handoff={'claude:opus': {'window': 900000}, 'claude': {'ratio': .5}, 'codex': {'window': 50000}})
    table = {'claude:opus': {'ratio': .7}, 'claude': {'window': 300000, 'ratio': .4}}
    assert lim('claude', 'opus', cfg) == (900000, .5, 'window: config claude:opus; ratio: config claude')
    assert lim('claude', 'opus', cfg, table) == (300000, .7, 'window: roster claude; ratio: roster claude:opus')
    assert lim('claude', 'opus', cfg, table, {'window': 1000000}) == (
        1000000, .7, 'window: worker; ratio: roster claude:opus')
    assert lim('claude', 'opus', cfg, table, {'ratio': .8})[1] == .8
    assert lim('codex', 'gpt-6', cfg)[0] == 50000  # explicit window overrides the measured one
    assert lim('claude', 'haiku', CFG, {'claude:opus': {'window': 1}})[0] == 200000  # exact model key only


def test_opus_1m_does_not_rotate_at_300k_but_haiku_rotates_at_120k(cfx):
    cfx.cfg['worker_model'] = 'opus'
    transcript(cfx, 'worker-1', 300000)
    assert deliver(cfx, 'worker-1') == 'busy'
    assert cfx.agent('worker-1')['ctx_window'] == '1000000'
    cfx.setf('worker-1', state='idle')  # turn finished
    cfx.cfg['worker_model'] = 'haiku'
    transcript(cfx, 'worker-1', 120000)
    assert deliver(cfx, 'worker-1') == 'handoff'


def test_runtime_profile_model_is_used(cfx):
    cfx.setf('worker-1', runtime_launch=json.dumps({'overrides': {'worker_cli': 'claude', 'worker_model': 'opus'}}))
    assert cfx.handoff_limits('worker-1')[0] == 1000000
    assert cfx.handoff_limits('supervisor')[0] is None  # codex supervisor keeps its measured window


def test_per_worker_override_and_hot_reload_without_restart(cfx):
    cfx.cfg['worker_model'] = 'opus'
    transcript(cfx, 'worker-1', 300000)
    roster(cfx, 'workers:\n  - name: worker-1\nhandoff:\n  claude:opus: {window: 400000}\n')  # 300k >= 0.6 * 400k
    changed = [json.loads(x) for x in cfx.r.l['fx:log'] if 'roster.changed' in x][-1]
    assert changed['handoff']['table'] == {'before': {}, 'after': {'claude:opus': {'window': 400000}}}
    assert cfx.agent('worker-1')['handoff_window'] == '400000'  # published before any delivery
    assert cfx.agent('worker-1')['handoff_source'] == 'window: roster claude:opus; ratio: --handoff-ratio'
    roster(cfx, 'workers:\n  - {name: worker-1, handoff_ratio: 0.9}\nhandoff:\n  claude:opus: {window: 400000}\n')
    changed = [json.loads(x) for x in cfx.r.l['fx:log'] if 'roster.changed' in x][-1]
    assert changed['handoff'] == {'worker-1': {'before': {}, 'after': {'ratio': 0.9}}}
    assert deliver(cfx, 'worker-1') == 'busy'  # 300k < 0.9 * 400k
    cfx.setf('worker-1', state='idle')  # turn finished
    roster(cfx, 'workers:\n  - {name: worker-1, window: 450000}\n')  # same daemon, no restart
    assert deliver(cfx, 'worker-1') == 'handoff'  # 300k >= 0.6 * 450k


def test_invalid_handoff_rejects_whole_roster_and_keeps_thresholds(cfx):
    roster(cfx, 'workers:\n  - name: worker-1\nhandoff:\n  claude: {window: 500000}\n')
    roster(cfx, 'workers:\n  - name: worker-1\nhandoff:\n  claude: {window: 600000, ratio: 1.5}\n')
    rejected = [json.loads(x) for x in cfx.r.l['fx:log'] if 'roster.rejected' in x][-1]
    assert any('ratio must be a number between 0 and 1' in e for e in rejected['errors'])
    assert cfx.handoff_table == {'claude': {'window': 500000}} and cfx.handoff_limits('worker-1')[0] == 500000


def test_config_handoff_table_is_validated(cfx):
    fleet.save(cfx.path, dict(fleet.load(cfx.path), handoff={'claude': {'ratio': 0}}))
    try:
        fleet.Fleet(cfx.path, FakeRedis(), FakeMgr(), sessions=cfx.sessions)
    except ValueError as exc:
        assert 'handoff[claude]: ratio' in str(exc)
    else:
        raise AssertionError('invalid config handoff table accepted')


def test_codex_measured_window_untouched_unless_overridden(fx):
    cwd = fleet.agent_cwd(fx.cfg, 'supervisor')
    write_rollout(fx.sessions, THREADS['supervisor'], cwd, used=70000, window=258400)
    assert fx.usage('supervisor') == (70000, 258400)
    assert fx.agent('supervisor')['handoff_window'] == 'measured'
    fx.handoff_table = {'codex': {'window': 100000}}
    assert fx.usage('supervisor') == (70000, 100000)


def test_control_view_shows_effective_handoff(cfx):
    cfx.cfg['worker_model'] = 'opus'
    cfx.handoff_limits('worker-1')
    agent = cfx.agent('worker-1')
    assert agent['handoff_window'] == '1000000' and agent['handoff_ratio'] == '0.6'
    assert agent['handoff_source'] == 'window: built-in opus; ratio: --handoff-ratio'
    assert not control_view._secret_key('handoff_window') and not control_view._secret_key('handoff_source')


def test_profile_card_ratio_is_one_layer_below_the_tables():
    """Integration of §12 model cards into §22: the running profile card gives a ratio only, below
    the roster / config tables and above --handoff-ratio; never a window."""
    lim = fleet.handoff_limits
    card = ('flashnext-coder', .5)
    assert lim('pi', 'local/flash', CFG, card=card) == (None, .5, 'window: measured; ratio: card flashnext-coder')
    assert lim('claude', 'haiku', CFG, card=card)[:2] == (200000, .5)  # window untouched by the card
    assert lim('pi', 'local/flash', CFG, worker={'ratio': .8}, card=card)[1] == .8
    assert lim('pi', 'local/flash', CFG, table={'pi': {'ratio': .7}}, card=card)[1] == .7
    assert lim('pi', 'local/flash', dict(CFG, handoff={'pi': {'ratio': .65}}), card=card)[1] == .65
    assert lim('pi', 'local/flash', CFG, card=('researcher', None))[1] == .6  # card without a ratio
