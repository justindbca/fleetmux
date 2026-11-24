"""The reconcile ledger (docs/design.md section 60): the `reconcile` decision key in both decision parsers, the
operator's `fleetmux reconcile rule`, a long list spilled to the ledger file, the reminder, the closing report and
the retention of ended child fleets' directories. Private test Redis, fake cmux, per-test fleet home."""
import json
import time

import pytest

from restart_kit import kill_extra, ledger, ledgers, rdir, reconcile_notices, tick
from test_named_subfleet_chain import named_chain_as_before  # noqa: F401  (fixture)
from test_service_attach import root  # noqa: F401  (fixture)
from test_subfleet import drive, svc  # noqa: F401
from fleetmux import fleet, lifecycle_close, restart_state, runtime_v2

pytestmark = pytest.mark.usefixtures('named_chain_as_before')


@pytest.fixture
def rt(root, monkeypatch):
    sent = []
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: sent.append(rec) or 'sent')
    root.sent = sent
    yield root
    kill_extra(root)


def open_items(f, n):
    restart_state.open_ledger(f, [restart_state.item('errored', 'audit', 'e' + str(i), 'error number ' + str(i))
                                  for i in range(n)], 'restart')
    return ledger(f)


def test_reconcile_only_decisions_are_valid_in_both_parsers():
    rec = [{'item': 'R1', 'verdict': 'drop', 'reason': 'not needed any more'}]
    d = fleet.parse_service_decision(json.dumps({'status': 'RUNNING', 'dispatch': [], 'reconcile': rec}), ['worker-1'])
    assert d['reconcile'] == rec
    d = fleet.parse_decision(json.dumps({'status': 'RUNNING', 'completion': 10, 'dispatch': [], 'reconcile': rec}),
                             ['worker-1'], others_active=False)
    assert d['reconcile'] == rec
    for bad in ([{'item': 'R1', 'verdict': 'drop'}], [{'item': 'R1', 'verdict': 'maybe'}], {'R1': 'drop'}):
        with pytest.raises(ValueError):
            fleet.parse_service_decision(json.dumps({'status': 'RUNNING', 'reconcile': bad}), ['worker-1'])
    for name in ('service_supervisor_v2.md', 'service_supervisor_tui_v2.md', 'supervisor_v2.md'):
        text = (fleet.Path(fleet.__file__).parent / 'agents' / name).read_text(encoding='utf-8')
        assert '`reconcile`' in text and '"verdict"' in text, name


def test_operator_rules_through_the_daemon_and_unknown_items_are_refused(rt):
    f = rt.f
    led = open_items(f, 2)
    ev = {'kind': 'reconcile', 'by': 'operator',  # what `fleetmux reconcile rule R1 --verdict recovered` sends
          'rulings': [{'item': 'R1', 'verdict': 'recovered', 'reason': ''}, {'item': 'R9', 'verdict': 'recovered'}]}
    ans = drive(rt, lambda: fleet.request(f.cfg, ev))
    assert ans['status'] == 'accepted' and ans['applied'] == ['R1'] and 'R9' in ans['refused'][0]
    assert ledger(f)['items'][0]['state'] == 'recovered' and ledger(f)['items'][1]['state'] == 'open'
    md = (rdir(f) / 'ledgers' / (led['id'] + '.md')).read_text()
    assert '| R1 | errored |' in md and 'recovered' in md  # marked item by item


def test_long_lists_spill_to_the_ledger_file_and_open_items_are_reminded(rt, monkeypatch):
    f = rt.f
    tick(rt)
    open_items(f, 35)
    tick(rt)
    note, = reconcile_notices(f)
    assert 'R30 ' in note and 'R31 ' not in note and '5 more item(s)' in note and 'L1.md' in note
    real = restart_state.knobs
    monkeypatch.setattr(restart_state, 'knobs', lambda: dict(real(), ruling_remind_s=1))
    led = ledger(f)
    led['delivered_at'] = time.time() - 5
    restart_state.save_ledger(f, led)
    restart_state.deliver(f)
    assert ledger(f)['delivered_at'] > time.time() - 2  # sent again


def test_closing_writes_the_report_and_pushes_a_summary_with_drop_reasons(rt):
    f = rt.f
    open_items(f, 2)
    assert [r['event'] for r in rt.sent][:1] == ['reconcile_opened']
    restart_state.decide(f, [{'item': 'R1', 'verdict': 'recovered'},
                             {'item': 'R2', 'verdict': 'drop', 'reason': 'the error is from a test'}], by='operator')
    led, = ledgers(f)
    assert led['closed'] and (rdir(f) / 'reports' / 'reconcile-L1.json').exists()
    closing = [r['text'] for r in rt.sent if r['event'] == 'reconcile_closed']
    assert closing and 'recovered 1' in closing[0] and 'dropped 1' in closing[0] and 'from a test' in closing[0]


def test_ended_child_fleet_directories_are_kept_to_the_newest_n(rt, monkeypatch):
    f = rt.f
    real = restart_state.knobs
    monkeypatch.setattr(restart_state, 'knobs', lambda: dict(real(), keep_child_dirs=2))
    base = restart_state.home(f.cfg) / 'restart'
    for i in range(4):
        d = base / ('job-job-%02d' % i)
        d.mkdir(parents=True)
        restart_state.save_state(d, {'seq': 1})
        time.sleep(0.01)
    open_one = base / 'split-x-M9'
    open_one.mkdir()
    restart_state.save_state(open_one, {'ledger': 'L1'})
    restart_state.prune_children(f)
    left = sorted(p.name for p in base.iterdir() if p.name.startswith(('job-', 'split-')))
    assert left == ['job-job-02', 'job-job-03', 'split-x-M9']  # an open ledger is never pruned
