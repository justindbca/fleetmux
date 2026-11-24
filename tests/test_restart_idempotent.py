"""Two restarts in a row give the same result (docs/design.md section 60): stop, start, stop, start - with and
without a ruling in between - leaves the attached sub-fleet declared once, one open ledger item for it (never two),
and the same set of declarations as after the first restart. Private test Redis, fake cmux, per-test fleet home."""
import pytest

from restart_kit import items, kill_extra, ledger, ledgers, new_run, operator_stop, tick
from test_named_subfleet_chain import named_chain_as_before  # noqa: F401  (fixture)
from test_service_attach import attach_up, root  # noqa: F401  (fixture)
from test_subfleet import svc  # noqa: F401
from fleetmux import lifecycle_close, named_subfleet, restart_state

pytestmark = pytest.mark.usefixtures('named_chain_as_before')


@pytest.fixture
def rt(root, monkeypatch):
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: 'sent')
    yield root
    kill_extra(root)


def restart(t):
    operator_stop(t)
    f = new_run(t)
    tick(t, 2)
    return f


@pytest.mark.parametrize('rule_between', [False, True])
def test_two_restarts_in_a_row_end_the_same(rt, rule_between):
    attach_up(rt, 'docs')
    tick(rt)
    f1 = restart(rt)
    after_first = sorted(named_subfleet.attached(f1))
    if rule_between:
        restart_state.decide(f1, [{'item': i['id'], 'verdict': 'recovered', 'reason': 'ok'}
                                  for i in ledger(f1)['items'] if i['state'] == 'open'], by='operator')
    f2 = restart(rt)
    assert sorted(named_subfleet.attached(f2)) == after_first == ['docs']
    subs = [i for i in items(f2, 'subfleet') if i['key'] == 'docs']
    assert len(subs) == 1 and subs[0]['auto']['state'] in ('started', 'up')
    open_ledgers = [x for x in ledgers(f2) if not x['closed']]
    assert len(open_ledgers) == 1  # the earlier open one is reused, or a new one after the ruling closed it
    assert len(ledgers(f2)) == (2 if rule_between else 1)
