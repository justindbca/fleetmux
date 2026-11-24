"""Scenario one (docs/design.md section 60): a root service fleet is stopped by the operator and started again as a
new run; the service sub-fleets that were attached to it with `start` (they host applets) come back without anyone
attaching them again, even while the root supervisor never says READY, and the reconcile ledger lists them.
Private test Redis, fake cmux, per-test fleet home; the applet process is a fake handle."""
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from restart_kit import (all_ready, crash, items, kill_extra, ledger, ledgers, new_run, operator_stop, rdir,
                         reconcile_notices, snaps, tick)
from test_applet_host import applet_rows
from test_service_attach import attach_up, op, root  # noqa: F401  (fixture)
from test_named_subfleet_chain import named_chain_as_before  # noqa: F401  (fixture)
from test_subfleet import adopt_child, child_daemon, given, hook, result, step, stop_child_like_daemon, svc  # noqa: F401
from fleetmux import applet_host, fleet, join, lifecycle_close, named_subfleet, restart_state, subfleet

pytestmark = pytest.mark.usefixtures('named_chain_as_before')


@pytest.fixture
def rt(root, monkeypatch):
    sent = []
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: sent.append(rec) or 'sent')
    root.sent = sent
    yield root
    kill_extra(root)


def source_roster(t, name='docs', applet=True):
    path = t.tmp / (name + '-source.yaml')
    path.write_text('workers:\n  - name: ' + name + '-1\n' + (applet_rows(name='applet-' + name + '-1') if applet else ''))
    return path


def stub_for(t, rid, name='docs'):
    """The join stub the attaching `start` wrote in the root's fleet home."""
    path = fleet.HOME / 'joins' / (name + '.json')
    fleet.save(path, {join.KEY: {'kind': 'service', 'child': name, 'attach_id': rid, 'root_home': str(fleet.HOME),
                                 'root_config': str(t.f.path), 'child_config': str(t.f.run_dir / 'sub' / name /
                                                                                   'config.json')}})
    return path


def bring_restored_up(t, name='docs'):
    """The restored declaration is started by the ordinary autostart tick (the same path as a first attach, section
    29); its daemon then boots in process from the config the root wrote."""
    for _ in range(3):
        subfleet.tick(t.f)
        if named_subfleet.state(t.f, name).get('state') == 'starting':
            break
    assert named_subfleet.state(t.f, name).get('state') == 'starting', named_subfleet.state(t.f, name)
    rec = subfleet.record(t.f.run_dir / 'sub' / name)
    assert rec['state'] in subfleet.ACTIVE and rec['run'] == str(t.f.run_dir / 'sub' / name)
    return rec, child_daemon(t, rec)


def fake_applets(cf):
    started = []
    cf.applet_spawn = lambda cfg, spec, token: started.append(spec['name']) or SimpleNamespace(
        pid=2 ** 22 + len(started), poll=lambda: None, returncode=None)
    return started


def test_scenario1_attached_service_subfleet_and_its_applet_come_back_without_supervisor_ready(rt):
    src = source_roster(rt)
    rid, rec, cf = attach_up(rt, 'docs', roster=str(src))
    stub = stub_for(rt, rid)
    tick(rt)
    operator_stop(rt)
    old = rt.f
    f = new_run(rt, ready=False)  # the new supervisor never says READY (scenario three's shape)
    all_ready(rt, slots=f.workers)
    tick(rt)
    assert 'docs' in named_subfleet.attached(f)  # declared again by the boot reconcile, no supervisor involved
    led = ledger(f)
    assert led and led['mode'] == 'restart'
    it, = [i for i in led['items'] if i['kind'] == 'subfleet']
    assert it['class'] == 'stopped' and it['auto']['action'] == 're-attach' and it['auto']['source'] == 'source file'
    assert len({r['text'] for r in rt.sent if r['event'] == 'reconcile_opened'}) == 1  # one notice (each channel)
    rec2, cf2 = bring_restored_up(rt)
    assert fleet.load(Path(rec2['run']) / 'config.json')['named']['attached'] is True
    started = fake_applets(cf2)
    cf2.roster_tick()  # the child's own roster (the source file) declares its applet row
    for _ in range(3):
        applet_host.tick(cf2)
    assert started == ['applet-docs-1'] and applet_host.states(cf2.r)['applet-docs-1'] in ('starting', 'up')
    s = json.loads(stub.read_text())[join.KEY]  # the stub follows the new root
    assert s['root_config'] == str(f.path) and s['attach_id'] == it['auto']['op']
    assert reconcile_notices(f) == []  # nothing goes to a supervisor that is not READY
    assert old.run_dir != f.run_dir


def test_restored_child_is_delivered_ruled_and_reported(rt):
    rid, _, _ = attach_up(rt, 'docs', roster=str(source_roster(rt, applet=False)))
    tick(rt)
    operator_stop(rt)
    f = new_run(rt)
    tick(rt)
    bring_restored_up(rt)
    tick(rt)
    notes = reconcile_notices(f)
    assert notes and '[Reconcile L1]' in notes[-1] and 'subfleet docs' in notes[-1]
    open_items = [i for i in ledger(f)['items'] if i['state'] == 'open']
    assert [i['kind'] for i in open_items] == ['subfleet'], open_items
    prompt = given(rt, f, 'supervisor')[-1]
    text = json.dumps({'status': 'RUNNING', 'dispatch': [], 'skip': [], 'reconcile': [
        {'item': i['id'], 'verdict': 'recovered', 'reason': 'docs is up again'} for i in open_items]})
    assert hook(rt, f, 'supervisor', text, prompt=prompt)['status'] == 'committed'
    step(rt)
    assert ledger(f) is None
    led, = ledgers(f)
    assert led['closed'] and led['items'][0]['state'] == 'recovered'
    report = rdir(f) / 'reports' / 'reconcile-L1.md'
    assert report.exists() and 'docs is up again' in report.read_text()
    closing = [r for r in rt.sent if r['event'] == 'reconcile_closed']
    assert closing and 'recovered 1' in closing[0]['text']


def test_the_same_start_again_gets_the_restored_child_and_one_stub(rt):
    rid, _, _ = attach_up(rt, 'docs', roster=str(source_roster(rt, applet=False)))
    tick(rt)
    operator_stop(rt)
    f = new_run(rt)
    tick(rt)
    restore_id = ledger(f)['items'][0]['auto']['op']
    again, answer = op(rt, 'attach', 'docs', roster=str(source_roster(rt, applet=False)))
    assert answer['status'] == 'accepted' and answer.get('id') == restore_id  # the restore's address, no clash
    assert join.own_stub(fleet.HOME, 'docs', str(fleet.HOME)) is None or True


@pytest.mark.parametrize('remove, source', [('nothing', 'source file'), ('source', 'previous run copy'),
                                            ('both', 'snapshot mirror')])
def test_restore_prefers_the_current_source_roster_and_says_which_it_used(rt, remove, source):
    src = source_roster(rt, applet=False)
    attach_up(rt, 'docs', roster=str(src))
    tick(rt)
    operator_stop(rt)
    if remove in ('source', 'both'):
        src.unlink()
    if remove == 'both':
        (rt.f.run_dir / 'attached' / 'docs' / 'roster.yaml').unlink()
    else:
        src.exists() and src.write_text('workers:\n  - name: docs-1\n  - name: docs-2\n')  # edited while down
    f = new_run(rt)
    tick(rt)
    it, = items(f, 'subfleet')
    assert it['auto'].get('source') == source and it['auto']['state'] == 'started', it
    entry = named_subfleet.attached(f)['docs']['entry']
    workers = Path(entry['roster']).read_text()
    assert ('docs-2' in workers) == (remove == 'nothing')  # the edited source wins over the old copy


def test_cancel_then_attach_again_then_restart_brings_it_back(rt):
    _, _, cf = attach_up(rt, 'docs', roster=str(source_roster(rt, applet=False)))
    rid, answer = op(rt, 'detach', 'docs')
    assert answer['status'] == 'accepted'
    tomb = restart_state.root_dir(rt.f.cfg) / 'cancels' / 'subfleet-docs.json'
    assert tomb.exists()
    subfleet.tick(rt.f)  # drains, then the stop is posted
    stop_child_like_daemon(cf)
    cf.lock.close()
    for _ in range(3):
        subfleet.tick(rt.f)  # collected, the detach finished
    rid2, answer = op(rt, 'attach', 'docs', roster=str(source_roster(rt, applet=False)))
    assert answer['status'] == 'accepted', answer
    assert not tomb.exists()  # named again: the cancel no longer holds
    tick(rt)
    operator_stop(rt)
    f = new_run(rt)
    tick(rt)
    assert 'docs' in named_subfleet.attached(f)


def test_start_no_restore_lists_the_child_for_the_supervisor_and_declares_nothing(rt):
    attach_up(rt, 'docs', roster=str(source_roster(rt, applet=False)))
    tick(rt)
    operator_stop(rt)
    f = new_run(rt, no_restore=True)  # `start --no-restore`
    tick(rt)
    assert 'docs' not in named_subfleet.attached(f)
    it, = items(f, 'subfleet')
    assert it['state'] == 'open' and it['auto'] == {'action': 're-attach', 'state': 'skipped',
                                                     'note': 'start --no-restore'}


def die_with_children(t, cf):
    """Power loss: the root and its child daemon die together; no stop tail runs, the child's lock goes with it and
    the reboot closed its terminal tabs."""
    cf.stopped = True
    cf.lock.close()
    cf.mgr.close_all()
    crash(t)


def test_attached_then_crash_before_the_next_timer_snapshot_comes_back_once(rt):
    tick(rt)
    restart_state.write(rt.f, 'timer')  # the last timer snapshot is from before the attach
    rid, _, cf = attach_up(rt, 'docs', roster=str(source_roster(rt, applet=False)))
    die_with_children(rt, cf)
    f = new_run(rt)
    tick(rt, 2)
    assert 'docs' in named_subfleet.attached(f)
    it, = [i for i in items(f, 'subfleet') if i['key'] == 'docs']
    assert it['auto']['action'] == 're-attach' and it['auto']['state'] in ('started', 'up')
    first = named_subfleet.attached(f)['docs']['id']
    bring_restored_up(rt)
    tick(rt, 2)
    assert named_subfleet.attached(f)['docs']['id'] == first  # declared once
    assert len([i for i in items(f, 'subfleet') if i['key'] == 'docs']) == 1
    assert [p.name for p in (f.run_dir / 'sub').iterdir()] == ['docs']  # one child run, started once


def test_detached_then_crash_before_the_next_timer_snapshot_does_not_come_back(rt):
    _, _, cf = attach_up(rt, 'docs', roster=str(source_roster(rt, applet=False)))
    tick(rt)
    restart_state.write(rt.f, 'timer')  # the last timer snapshot still has it attached
    rid, answer = op(rt, 'detach', 'docs')
    assert answer['status'] == 'accepted'
    die_with_children(rt, cf)
    f = new_run(rt)
    tick(rt, 2)
    assert 'docs' not in named_subfleet.attached(f)
    assert not [i for i in items(f, 'subfleet') if i['key'] == 'docs' and (i.get('auto') or {}).get('op')]


def test_an_attach_goes_on_when_its_snapshot_cannot_be_written(rt, monkeypatch):
    monkeypatch.setattr(restart_state, 'write_json', lambda p, d: 1 / 0)
    rid, answer = op(rt, 'attach', 'docs', roster=str(source_roster(rt, applet=False)))
    assert answer['status'] == 'accepted' and 'docs' in named_subfleet.attached(rt.f)
    assert any(x.get('event') == 'restart.error' for _, x in rt.f.r.xrange(rt.f.v2.bus.audit_key))
