"""Closing a sub-fleet from the fleetmux sidebar (docs/design.md section 19, "Closing a sub-fleet from the sidebar").

The server writes the root-only kv ``child_stop`` ``{"v":1,"state":"requested","id","page","at"}`` only after the user
confirmed the box of a child card's close button. The root daemon (``fleetmux_sidebar._child_stop_request``) handles
each request once, after checking the card by the same rule that gave it the button (a direct child of the root,
still running, with a stop path): a job sub-fleet through ``Fleet.job_cancel`` (its result is published failed with
outcome ``cancelled``), a named sub-fleet through an approved ``subfleet.down`` (the confirmation is the approval; one
audit record), an attached one through ``subfleet.detach``. It answers ``accepted`` / ``refused`` with a reason.
Private test Redis, fake cmux, fake CLIs; the kv is a dict on the fake manager.
"""
import json
from pathlib import Path

import pytest

from test_escalate import ok_check  # noqa: F401  (fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_named_subfleet_chain import named_chain_as_before  # noqa: F401  (fixture)
from test_service_attach import attach_up, root  # noqa: F401  (fixture)
from test_service_escalate import audits, result
from test_service_job import jobs, start_job
from test_service_split import finish_child, queued_to, split_tree  # noqa: F401  (fixture)
from test_subfleet import bring_up, stop_child_like_daemon, svc  # noqa: F401  (fixture)
from fleetmux import fleetmux_sidebar, named_subfleet, subfleet


def kv_on(f, monkeypatch):
    kv = {}
    monkeypatch.setattr(f.mgr, 'kv_get', lambda key: kv.get(key), raising=False)
    monkeypatch.setattr(f.mgr, '_kv_set', lambda key, value: kv.__setitem__(key, value) or True, raising=False)
    return kv


def confirmed(f, kv, card, at=1):
    """What the server writes after `y` in a child card's box, then the root daemon's tick part."""
    kv['child_stop'] = json.dumps({'v': 1, 'state': 'requested', 'id': card, 'page': card, 'at': at})
    fleetmux_sidebar._child_stop_request(f)
    return json.loads(kv['child_stop'])


@pytest.fixture
def jt(split_tree, monkeypatch):  # noqa: F811
    yield split_tree


# ------------------------------------------------------------------ job sub-fleet
@pytest.mark.usefixtures('job_chains_as_before')
def test_after_the_confirmation_a_job_subfleet_is_recorded_as_cancelled(jt, monkeypatch):
    t = jt
    f = t.f
    jid, key = start_job(t)
    d = f.run_dir / 'sub' / key
    assert subfleet.record(d)['state'] == 'running'
    kv = kv_on(f, monkeypatch)
    answer = confirmed(f, kv, d.name)
    assert answer['state'] == 'accepted' and answer['id'] == d.name, answer
    assert jobs(f)[jid]['cancel']['by'] == 'fleetmux-sidebar'
    rec = subfleet.record(d)
    assert rec['state'] == 'stopping' and rec['outcome'] == 'cancelled'
    finish_child(t, key, outcome='cancelled', monkeypatch=monkeypatch)
    published = result(f, jid)
    assert published['status'] == 'failed' and published['result']['outcome'] == 'cancelled', published
    # the card is gone from the running ones: no stop button any more, only the finished card's dismiss
    cards = {c['id']: c for c in fleetmux_sidebar.payload(f)['fleets']}
    assert cards[d.name]['close']['action'] == 'dismiss'


@pytest.mark.usefixtures('job_chains_as_before')
def test_nothing_happens_without_the_confirmation(jt, monkeypatch):
    t = jt
    f = t.f
    jid, key = start_job(t)
    d = f.run_dir / 'sub' / key
    kv = kv_on(f, monkeypatch)
    fleetmux_sidebar._child_stop_request(f)  # no kv child_stop: the box was never confirmed
    for state in ('accepted', 'refused', 'nonsense'):
        kv['child_stop'] = json.dumps({'v': 1, 'state': state, 'id': d.name, 'at': 1})
        fleetmux_sidebar._child_stop_request(f)
        assert json.loads(kv['child_stop'])['state'] == state  # never answered, never acted on
    kv['child_stop'] = 'not json'
    fleetmux_sidebar._child_stop_request(f)
    assert 'cancel' not in jobs(f)[jid] and subfleet.record(d)['state'] == 'running'
    # the same confirmed request is handled once: a second tick that sees it again does nothing more
    first = confirmed(f, kv, d.name, at=7)
    assert first['state'] == 'accepted'
    kv['child_stop'] = json.dumps({'v': 1, 'state': 'requested', 'id': d.name, 'page': d.name, 'at': 7})
    fleetmux_sidebar._child_stop_request(f)
    assert json.loads(kv['child_stop'])['state'] == 'requested'  # the repeated (id, at) is left alone


@pytest.mark.usefixtures('job_chains_as_before')
def test_a_request_is_checked_by_the_card_rule_never_by_the_id_alone(jt, monkeypatch):
    t = jt
    f = t.f
    jid, key = start_job(t)
    d = f.run_dir / 'sub' / key
    kv = kv_on(f, monkeypatch)
    assert confirmed(f, kv, 'no-such-run')['state'] == 'refused'
    grand = d / 'sub' / 'M0.1'  # a grandchild never has a button: not a direct child of this fleet
    grand.mkdir(parents=True)
    (grand / 'subfleet.json').write_text(json.dumps({'task_id': 'M0.1', 'state': 'running', 'job': jid}))
    assert confirmed(f, kv, 'M0.1', at=2)['state'] == 'refused'
    split = f.run_dir / 'sub' / 'M77'  # a split / escalate child (neither job nor named): no stop path here
    split.mkdir(parents=True)
    (split / 'subfleet.json').write_text(json.dumps({'task_id': 'M77', 'state': 'running', 'created': 1.0}))
    refused = confirmed(f, kv, 'M77', at=3)
    assert refused['state'] == 'refused' and refused['reason'], refused
    assert 'cancel' not in jobs(f)[jid]
    rec = subfleet.record(d)
    rec['state'] = 'returned'
    subfleet.save_record(d, rec)
    gone = confirmed(f, kv, d.name, at=4)
    assert gone['state'] == 'refused' and 'not running' in gone['reason'], gone
    assert 'cancel' not in jobs(f)[jid]


# ------------------------------------------------------------------ named / attached sub-fleets
@pytest.mark.usefixtures('named_chain_as_before')
def test_a_named_subfleet_goes_through_down(svc, monkeypatch):  # noqa: F811
    t, f = svc, svc.f
    _, rec, cf = bring_up(t, 'ops')
    card = Path(rec['run']).name
    asked = len(queued_to(f, 'supervisor', 'wants sub-fleet ops down'))
    kv = kv_on(f, monkeypatch)
    answer = confirmed(f, kv, card)
    assert answer['state'] == 'accepted', answer
    downs = named_subfleet.ops(f, 'ops', 'down', approved=True)
    assert len(downs) == 1  # approved by the confirmation, never put to the root supervisor
    assert len(queued_to(f, 'supervisor', 'wants sub-fleet ops down')) == asked
    assert len(audits(f, 'subfleet.sidebar_down')) == 1
    subfleet.tick(f)
    assert named_subfleet.state(f, 'ops')['state'] == 'draining'  # the existing down path from here on
    again = confirmed(f, kv, card, at=2)  # clicked twice: already going down, nothing new
    assert again['state'] == 'refused' and len(named_subfleet.ops(f, 'ops', 'down')) == 1
    subfleet.tick(f)  # draining -> quiet -> stop posted (as test_subfleet.take_down)
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    assert subfleet.record(Path(rec['run']))['state'] not in subfleet.ACTIVE


@pytest.mark.usefixtures('named_chain_as_before')
def test_an_autostart_named_subfleet_stays_refused_by_the_gate(svc, monkeypatch):  # noqa: F811
    t, f = svc, svc.f
    _, rec, _ = bring_up(t, 'ops')
    monkeypatch.setattr(named_subfleet, 'gate', lambda f, op, name: (
        'rejected', {'reason': 'program: ' + name + ' is autostart; it cannot be taken down from outside'}))
    kv = kv_on(f, monkeypatch)
    answer = confirmed(f, kv, Path(rec['run']).name)
    assert answer['state'] == 'refused' and 'autostart' in answer['reason'], answer
    assert not named_subfleet.ops(f, 'ops', 'down') and not audits(f, 'subfleet.sidebar_down')


@pytest.mark.usefixtures('named_chain_as_before')
def test_an_attached_subfleet_goes_through_detach(root, monkeypatch):  # noqa: F811
    t, f = root, root.f
    _, rec, _ = attach_up(t, 'docs')
    kv = kv_on(f, monkeypatch)
    answer = confirmed(f, kv, Path(rec['run']).name)
    assert answer['state'] == 'accepted', answer
    assert 'docs' not in named_subfleet.attached(f)
    assert named_subfleet.ops(f, 'docs', 'down', approved=True)
