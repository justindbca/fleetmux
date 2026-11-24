"""The review of a decomposition (docs/design.md §62, plan 2.9), switch on: tree.json's ``review`` is recomputed on every
save (returns by cause, parallel fill = wall minutes / actual longest chain, each split's extra session minutes
against the time it saved), the final table goes into result.json; only a table, never a verdict. The closing
COMPLETED may carry ``decompose_tune``: applied to the Redis parameters (and marked for write-back) only when the
COMPLETED passes verification; a sub-fleet's tune is forwarded to its parent at collect, up to the root, and only the
root writes back (a service root with a job sub-fleet too)."""
import json
import os
from pathlib import Path

import pytest
import yaml

from fleetmux import decompose, fleet_config, subfleet
from test_decompose_fleet import answer_turn, hand_in_tree, reply, tree
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_service_job import start_job
from test_service_split import ready_again, split_tree  # noqa: F401
from test_subfleet import adopt_by_hook, step, stop_child_like_daemon
from test_v2_codex_claude import mixed  # noqa: F401

T0 = 1_000_000.0


def block(nid, est, start, end, session, after=(), returns=0, causes=None, parent='root', state='reported'):
    return {'id': nid, 'parent': parent, 'goal': nid, 'cut': 'function', 'owns': [], 'decides': [], 'consumes': [],
            'after': list(after), 'depends_on': [], 'merge_of': [], 'est': {'minutes': est, 'sessions': 1},
            'accept': 'ok', 'state': state,
            'actual': dict(decompose.blank_actual(), started=T0 + start * 60, ended=T0 + end * 60 if end else 0,
                           minutes=end - start if end else 0, session_minutes=session, returns=returns,
                           causes=causes or {})}


def finished_tree():
    t = decompose.new_tree('root', 'goal')
    t['nodes'] += [block('M0', 10, 0, 10, 10, returns=2, causes={'shared_decision': 1, 'scope': 1}),
                   block('M1', 20, 10, 35, 25, after=['M0'], returns=1, causes={'file_overlap': 1}),
                   block('M2', 5, 0, 5, 9),
                   block('M3', 30, 0, 20, 45),
                   block('M3.0', 15, 0, 18, 20, parent='M3'), block('M3.1', 15, 0, 20, 21, parent='M3'),
                   block('M4', 6, 2, 6, 4), block('M5', 6, 2, 8, 6)]
    t['splits'] = [{'of': 'M3', 'pieces': ['M3.0', 'M3.1'], 'dT': 6, 'dC': 12, 'claimed': {'dT': 10, 'dC': 8},
                    'by': 'worker', 'session_minutes': 41},
                   {'of': 'M2', 'pieces': ['M2.0', 'M2.1'], 'dT': 3, 'dC': 2, 'claimed': {'dT': 3, 'dC': 2},
                    'by': 'worker', 'rolled_back': 'sub-fleet failed', 'session_minutes': 4},
                   {'of': 'M9', 'pieces': ['M4', 'M5'], 'dT': 5, 'dC': 1, 'claimed': {'dT': 6, 'dC': 1},
                    'by': 'supervisor', 'est': {'minutes': 12, 'sessions': 1}}]
    return t


def test_the_review_table_has_the_three_rulers_and_no_verdict():
    r = decompose.review(finished_tree(), now=T0 + 3600)
    assert r['returns'] == {'total': 3, 'by_cause': {'file_overlap': 1, 'scope': 1, 'shared_decision': 1},
                            'by_block': {'M0': {'returns': 2, 'causes': {'scope': 1, 'shared_decision': 1}},
                                         'M1': {'returns': 1, 'causes': {'file_overlap': 1}}}}
    assert r['parallel'] == {'wall_minutes': 35, 'chain_minutes': 35, 'chain': ['M0', 'M1'], 'fill': 1,
                             'work_minutes': 70}
    worker, rolled, sup = r['splits']
    assert worker == {'of': 'M3', 'by': 'worker', 'pieces': ['M3.0', 'M3.1'], 'claimed': {'dT': 10, 'dC': 8},
                      'program': {'dT': 6, 'dC': 12}, 'est_minutes': 30, 'actual_minutes': 20, 'saved_minutes': 10,
                      'session_minutes': 45, 'extra_session_minutes': 15, 'rolled_back': None, 'open': False}
    assert rolled['rolled_back'] == 'sub-fleet failed' and rolled['saved_minutes'] == 0
    assert rolled['extra_session_minutes'] == 4
    assert sup['est_minutes'] == 12 and sup['actual_minutes'] == 6 and sup['saved_minutes'] == 6
    assert sup['session_minutes'] == 10 and sup['extra_session_minutes'] == -2
    assert not {'pass', 'verdict', 'ok'} & set(r)


def test_a_supervisor_split_records_the_estimate_it_replaced():
    t = decompose.new_tree('root', 'goal')
    t['nodes'].append(block('M0', 40, 0, 0, 0, state='planned'))
    ctx = {'tree': t, 'root': 'root', 'prefix': 'M', 'params': decompose.merged({'split': {'k': 1, 'margin': 0},
                                                                                  'cost': {}})}
    pieces = [dict(block('M1', 20, 0, 0, 0, state='planned'), split_from={'id': 'M0', 'dT': 20, 'dC': 1}),
              dict(block('M2', 20, 0, 0, 0, state='planned'), split_from={'id': 'M0', 'dT': 20, 'dC': 1})]
    for p in pieces:
        for k in ('state', 'actual', 'parent'):
            p.pop(k)
    new = decompose.apply(t, pieces, ['M0'], ctx)
    assert new['splits'][-1]['est'] == {'minutes': 40, 'sessions': 1}


def test_on_every_save_keeps_the_review_in_tree_json(mixed, decompose_on):
    f = mixed
    hand_in_tree(f)
    t = tree(f)
    assert set(t['review']) >= {'returns', 'parallel', 'splits'}
    assert json.loads((f.run_dir / 'tree.json').read_text())['review'] == t['review']


TUNE = [{'param': 'split.k', 'value': 3, 'reason': 'two splits cost more than they saved'},
        {'param': 'cost.merge', 'value': 2.5, 'reason': 'merges took longer'}]


def complete(f, tune, written=True):
    if written:
        (f.workspace() / 'worker-1').mkdir(parents=True, exist_ok=True)
        (f.workspace() / 'worker-1' / 'a.md').write_text('the result')
    return answer_turn(f, 'supervisor', reply(status='COMPLETED', completion=100, reason='done', decompose_tune=tune,
                                              artifacts=[{'path': 'worker-1/a.md'}]))


def test_on_a_verified_completed_applies_the_tune_and_result_json_has_the_table(mixed, decompose_on):
    f = mixed
    f.decompose_load()
    hand_in_tree(f)
    assert complete(f, TUNE)['status'] == 'committed'
    assert f.task()['status'] == 'completed'
    p = decompose.params_get(f.r)
    assert p['split']['k'] == 3 and p['cost']['merge'] == 2.5 and f.r.hget(decompose.META_KEY, 'dirty') == '1'
    f.tick()
    out = json.loads((f.run_dir / 'result.json').read_text())
    assert set(out['decompose']['review']) >= {'returns', 'parallel', 'splits'}
    assert [(x['param'], x['value'], x['origin']) for x in out['decompose']['tune']] == [('split.k', 3, 'root'),
                                                                                       ('cost.merge', 2.5, 'root')]
    assert out['decompose']['chain'] == tree(f)['chain']


def test_on_a_bounced_completed_the_tune_does_not_apply(mixed, decompose_on):
    f = mixed
    f.decompose_load()
    hand_in_tree(f)
    before = decompose.params_get(f.r)
    assert complete(f, TUNE, written=False)['status'] == 'committed'
    assert f.task()['status'] == 'running'
    assert decompose.params_get(f.r) == before and f.r.hget(decompose.META_KEY, 'dirty') == '0'
    assert not f.r.lrange(decompose.TUNED_KEY, 0, -1)


@pytest.mark.usefixtures('job_chains_as_before')  # §58 model chains as in tests/test_service_job.py
def test_on_a_job_sub_fleets_tune_goes_to_the_service_root_which_alone_writes_back(split_tree, decompose_on,
                                                                                   monkeypatch):
    private = Path(os.environ[fleet_config.USER_ENV])  # job_chains_as_before's private file: discovery only
    t = split_tree
    f = t.f
    f.decompose_load()
    jid, key = start_job(t)
    rec = subfleet.record(f.run_dir / 'sub' / key)
    cf, _ = adopt_by_hook(t, monkeypatch, rec)
    step(t)
    cf.decompose_load()
    decompose.tune_apply(cf.r, [TUNE[0]], key)  # the child's closing COMPLETED passed verification
    assert cf.decompose_flush(force=True) is False and 'decompose' not in private.read_text()  # a sub-fleet never writes
    cf.v2.finish('completed', 'test end: completed')
    cf.write_result()
    assert json.loads((cf.run_dir / 'result.json').read_text())['decompose']['tune'][0]['origin'] == key
    f.tick()
    stop_child_like_daemon(cf)
    f.tick()
    step(t)
    ready_again(t)
    assert decompose.params_get(f.r)['split']['k'] == 3 and f.r.hget(decompose.META_KEY, 'dirty') == '1'
    assert [x['origin'] for x in decompose.tuned(f.r)] == [key]
    assert f.decompose_flush(force=True) is True
    assert yaml.safe_load(private.read_text())['decompose']['split']['k'] == 3
    assert yaml.safe_load(private.read_text())['discovery']['supervisor']  # the other blocks stay
