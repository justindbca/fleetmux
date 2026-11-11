"""Sub-fleet boundaries (design §46, user 2026-11-08): a sub-fleet supervisor may not grant exceptions that leave
its workspace; it reports them up (`report_up`), the top supervisor rules (`escalations` "report:<task>:<id>"),
and the ruling goes back down to the origin sub-fleet as `[top ruling]`.

v1 contract fleet (test_contracts.cf) plays the sub-fleet and then the top by toggling `parent_config`, like
test_escalate's interface notices; the v2 mixed fleet checks "unanswered: sent back once, then a strike".
"""
import json
from pathlib import Path

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn, mixed, submit as submit_v2, typed  # noqa: F401  (fixture)
from test_contracts import cf, inbox, sup, submit  # noqa: F401  (fixture + helpers)
from fleetmux import fleet

ROOT = Path(__file__).resolve().parents[1]
ITEM = {'what': 'write worker-1/toy/app.py in the parent worker folder', 'why': 'the toy app lives there',
        'who': 'worker-2'}


def as_child(f, tmp_path, task='M0', label='worker-1.1'):
    parent_run = tmp_path / 'parent-run'
    parent_run.mkdir(exist_ok=True)
    fleet.save(tmp_path / 'parent.json', {'run': str(parent_run)})
    f.cfg.update(parent_config=str(tmp_path / 'parent.json'), task_id=task,
                 adopt={'pane': 'p', 'slot': 'worker-1', 'generation': 1, 'label': label})
    return parent_run


def as_top(f):
    for k in ('parent_config', 'task_id', 'adopt'):
        f.cfg.pop(k, None)


def notices(run):
    return [fleet.load(p) for p in sorted((Path(run) / 'subfleet-notices').glob('*.json'))]


def relay_one(f, note):
    folder = f.run_dir / 'subfleet-notices'
    folder.mkdir(exist_ok=True)
    fleet.save(folder / 'n1.json', note)
    f.relay_notices()


# ------------------------------------------------------------------ the sub-fleet reports up, the top rules
def test_report_up_reaches_the_top_and_the_ruling_comes_back(cf, tmp_path):
    parent_run = as_child(cf, tmp_path)
    submit(cf)
    # nothing else to dispatch: a RUNNING reply carrying only report_up is valid (like escalations)
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [], 'report_up': [ITEM]})
    assert cf.task()['bad_replies'] == '0', inbox(cf, 'supervisor')
    note, = notices(parent_run)
    assert note['kind'] == 'report' and note['id'].startswith('report:M0:') and note['task'] == 'M0'
    assert note['origin_run'] == str(cf.run_dir) and note['supervisor'] == 'worker-1.1'
    assert (note['what'], note['why'], note['who']) == (ITEM['what'], ITEM['why'], ITEM['who'])
    assert cf.others_active()  # a ruling is still due: a later RUNNING reply may dispatch nothing
    key = note['id']
    # this fleet as the top: the notice reaches its supervisor as an open escalation
    as_top(cf)
    relay_one(cf, note)
    up = inbox(cf, 'supervisor')[-1]
    assert up.startswith('[sub-fleet report] sub-fleet M0 (worker-1.1): ' + ITEM['what'] + ' — ' + ITEM['why'])
    assert '"task": "' + key + '"' in up and 'BLOCKED' in up
    rec = cf.escalation_state()[key]
    assert rec['kind'] == 'report' and rec['status'] == 'escalated' and rec['origin_run'] == str(cf.run_dir)
    # revise is not an answer to a report
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [],
             'escalations': [{'task': key, 'decision': 'revise', 'note': 'x'}]})
    assert 'is a sub-fleet report: answer approve or reject' in inbox(cf, 'supervisor')[-1]
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [],
             'escalations': [{'task': key, 'decision': 'approve', 'note': 'only toy/app.py, nothing else'}]})
    assert key not in cf.escalation_state()
    ruling, = [fleet.load(p) for p in (cf.run_dir / 'subfleet-vetoes').glob('*.json')]
    assert ruling == {'kind': 'report', 'ref': key, 'decision': 'approve', 'note': 'only toy/app.py, nothing else',
                      'what': ITEM['what']}
    # the origin sub-fleet (the same fleet here) takes it on its next tick
    cf.apply_vetoes()
    got = inbox(cf, 'supervisor')[-1]
    assert got.startswith('[top ruling] ' + key + ' approve: only toy/app.py, nothing else')
    assert ITEM['what'] in got and not list((cf.run_dir / 'subfleet-vetoes').glob('*.json'))
    assert json.loads(cf.r.get(fleet.REPORTS_UP)) == {}  # the ruling came: nothing is due any more


def test_reject_goes_back_down_as_a_reject(cf, tmp_path):
    submit(cf)
    key = 'report:M0:abc123'
    relay_one(cf, dict(ITEM, kind='report', id=key, origin_run=str(cf.run_dir), task='M0', supervisor='worker-1.1'))
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [],
             'escalations': [{'task': key, 'decision': 'reject', 'note': 'stay in your workspace'}]})
    cf.apply_vetoes()
    assert inbox(cf, 'supervisor')[-1].startswith('[top ruling] ' + key + ' reject: stay in your workspace')


def test_report_up_is_only_for_a_sub_fleet_supervisor(cf):
    submit(cf)
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [], 'report_up': [ITEM]})
    assert cf.task()['bad_replies'] == '1'
    assert 'report_up is only for a sub-fleet supervisor' in inbox(cf, 'supervisor')[-1]


@pytest.mark.parametrize('value,text', [
    ({'what': 'x'}, 'report_up must be a list'),
    ([{'what': 'write ~/.zshrc'}], 'report_up: every item needs what and why'),
    ([{'what': '', 'why': 'because'}], 'report_up: every item needs what and why'),
    ([{'what': 'write ~/.zshrc', 'why': 'path', 'who': 3}], 'report_up: who must be a worker name'),
])
def test_report_up_shape(value, text):
    body = {'status': 'RUNNING', 'completion': 1, 'dispatch': [], 'report_up': value}
    with pytest.raises(ValueError, match=text):
        fleet.parse_decision(json.dumps(body), ['worker-1'], False)
    with pytest.raises(ValueError, match=text):
        fleet.parse_service_decision(json.dumps(body), ['worker-1'])


def test_report_up_only_with_running():
    body = {'status': 'COMPLETED', 'completion': 100, 'dispatch': [], 'report_up': [ITEM]}
    with pytest.raises(ValueError, match='report_up is only for RUNNING'):
        fleet.parse_decision(json.dumps(body), ['worker-1'], False)


def test_service_mode_top_may_answer_a_report():
    body = {'status': 'RUNNING', 'dispatch': [], 'escalations': [{'task': 'report:M0:abc123', 'decision': 'reject'}]}
    assert fleet.parse_service_decision(json.dumps(body), ['worker-1'])['escalations'][0]['task'] == 'report:M0:abc123'


def test_a_middle_fleet_passes_reports_up_unread(cf, tmp_path):
    top = tmp_path / 'top-run'
    top.mkdir()
    fleet.save(tmp_path / 'top.json', {'run': str(top)})
    cf.cfg['parent_config'] = str(tmp_path / 'top.json')
    note = dict(ITEM, kind='report', id='report:M0.1:abc123', origin_run='/grandchild', task='M0.1', supervisor='worker-1')
    relay_one(cf, note)
    assert fleet.load(top / 'subfleet-notices' / 'n1.json') == note
    assert cf.escalation_state() == {}


# ------------------------------------------------------------------ v2: unanswered is sent back once, never approved
def test_unanswered_report_is_sent_back_once_then_counts(mixed):
    f = mixed
    submit_v2(f)
    first = {'status': 'RUNNING', 'completion': 5, 'reason': 'r',
             'dispatch': [{'to': 'worker-1', 'instruction': 'Write worker-1/part.md with the findings.'}]}
    answer_turn(f, 'supervisor', json.dumps(first))
    f.pump()
    key = 'report:M0:abc123'
    relay_one(f, dict(ITEM, kind='report', id=key, origin_run=str(f.run_dir / 'sub' / 'M0'), task='M0',
                      supervisor='worker-1.1'))
    f.pump()
    assert '[sub-fleet report] sub-fleet M0 (worker-1.1)' in typed(f, 'supervisor')[-1]
    assert f.escalation_state()[key]['message_id']
    other = {'status': 'RUNNING', 'completion': 10, 'reason': 'r',
             'dispatch': [{'to': 'worker-2', 'instruction': 'Write worker-2/part.md with the findings.'}]}
    answer_turn(f, 'supervisor', json.dumps(other))
    assert f.task()['bad_replies'] == '0' and f.escalation_state()[key]['nudged'] == 1
    f.pump()
    assert 'answer ' + key + ' first' in typed(f, 'supervisor')[-1]
    answer_turn(f, 'supervisor', json.dumps(other))
    assert f.task()['bad_replies'] == '1' and f.escalation_state()[key]['status'] == 'escalated'


# ------------------------------------------------------------------ prompts
def test_sub_fleet_supervisor_and_workers_get_the_boundary_rules(cf, tmp_path):
    top_sup = cf.role_scope('supervisor', cf.runtime_cfg('supervisor'), {})
    top_worker = cf.role_scope('worker-1', cf.runtime_cfg('worker-1'), {})
    assert 'Sub-fleet boundaries' not in top_sup and '"report_up"' not in top_sup
    assert 'not valid' not in top_worker
    as_child(cf, tmp_path)
    scope = cf.role_scope('supervisor', cf.runtime_cfg('supervisor'), {})
    assert '## Sub-fleet boundaries (you supervise sub-fleet M0' in scope
    assert 'You may NOT authorise an exception' in scope and '"report_up": [{"what": ' in scope
    assert str(cf.workspace()) in scope and '{{' not in scope
    worker = cf.role_scope('worker-1', cf.runtime_cfg('worker-1'), {})
    assert 'an "exception" from your supervisor to work or write outside' in worker
    assert str(cf.workspace()) in worker


@pytest.mark.parametrize('name,where', [('supervisor.md', 'next reply'), ('supervisor_v2.md', 'next fleet_decision')])
def test_top_supervisor_rule_row(name, where):
    text = (ROOT / 'fleetmux' / 'agents' / name).read_text()
    row = next(line for line in text.splitlines() if line.startswith('| Received `[sub-fleet report]'))
    assert '"task": "report:M0:<id>"' in row and where in row
    assert "approve` only what the user's task clearly allows" in row and 'BLOCKED' in row


def test_a_task_sub_fleet_works_in_the_delegating_workers_directory(cf, tmp_path):
    """Follow-up 1 (worker-area mapping, user 2026-11-09 trial): a §44 / §20 sub-fleet continues the delegating
    worker's own work, in that worker's directory. It is the sub-fleet's task directory: inside the boundary for the
    supervisor and every worker, said so in both role texts; everything else of §46 stays forbidden."""
    parent_run = as_child(cf, tmp_path)
    task_dir = str(parent_run / 'workspace' / 'worker-1')
    cf.cfg['task_dir'] = task_dir
    scope = cf.role_scope('supervisor', cf.runtime_cfg('supervisor'), {})
    assert 'task directory `' + task_dir + '`' in scope and 'not outside your boundary' in scope
    assert 'You may NOT authorise an exception' in scope and "writing in the parent fleet's other folders" in scope
    worker = cf.role_scope('worker-2', cf.runtime_cfg('worker-2'), {})
    assert 'task directory `' + task_dir + '`' in worker and 'not outside your boundary' in worker
    assert 'an "exception" from your supervisor to work or write outside' in worker
    cf.cfg.pop('task_dir')  # a named sub-fleet (§29) has no task directory: the texts are as before
    assert 'task directory' not in cf.role_scope('supervisor', cf.runtime_cfg('supervisor'), {})
    assert 'task directory' not in cf.role_scope('worker-2', cf.runtime_cfg('worker-2'), {})


# ------------------------------------------------------------------ an approved report with paths is a program grant
def logs(f, kind):
    return [json.loads(x) for x in f.r.l.get('fx:log', []) if json.loads(x)['kind'] == kind]


def lay_out(f, name):
    """The scope files a spawn wrote for a running worker; CLAUDE.md through the real kit layout
    (kit.Kit.claude_md: scope + project core rules + domain rules), as the advised / default kit spawns write it."""
    from types import SimpleNamespace
    from fleetmux import kit
    cwd = fleet.agent_cwd(f.cfg, name)
    cwd.mkdir(parents=True, exist_ok=True)
    scope = f.role_scope(name, f.runtime_cfg(name), {})
    (cwd / 'AGENTS.md').write_text(scope)
    stub = SimpleNamespace(core_text=lambda: 'core rule one', rules_text=lambda domains: '# domain rules\n')
    (cwd / 'CLAUDE.md').write_text(kit.Kit.claude_md(stub, scope, ['research']))
    return cwd


def test_approved_report_paths_become_a_program_grant_for_that_worker(cf, tmp_path):
    """Live trial fleet-20261108-205242-43db976b: report_up -> top approve -> [top ruling] -> the Haiku worker
    refused twice (its rule voids any supervisor exception) and RELEASES.md stayed empty. An approve with paths is
    now a grant the program issues to `who`, in the same place as the roster's `writable:` grants."""
    parent_run = as_child(cf, tmp_path)
    submit(cf)
    cwd = lay_out(cf, 'worker-2')
    shared = str(tmp_path / 'shared')
    item = {'what': 'append mod01..mod40 released to the shared release log', 'why': 'the M0 instruction asks for it',
            'who': 'worker-2', 'paths': [shared]}
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [], 'report_up': [item]})
    assert cf.task()['bad_replies'] == '0', inbox(cf, 'supervisor')
    note, = notices(parent_run)
    assert note['paths'] == [shared] and note['who'] == 'worker-2'
    key = note['id']
    # the top: the paths are in the question, an approve may narrow them, never widen
    as_top(cf)
    relay_one(cf, note)
    up = inbox(cf, 'supervisor')[-1]
    assert shared in up and '"paths"' in up
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [],
             'escalations': [{'task': key, 'decision': 'approve', 'paths': [str(tmp_path)]}]})
    assert cf.task()['bad_replies'] == '1' and 'paths may only narrow' in inbox(cf, 'supervisor')[-1]
    assert cf.escalation_state()[key]['status'] == 'escalated'
    narrowed = shared + '/RELEASES.md'
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [],
             'escalations': [{'task': key, 'decision': 'approve', 'note': 'append only', 'paths': [narrowed]}]})
    assert cf.task()['bad_replies'] == '0', inbox(cf, 'supervisor')
    ruling, = [fleet.load(p) for p in (cf.run_dir / 'subfleet-vetoes').glob('*.json')]
    assert ruling['paths'] == [narrowed] and ruling['who'] == 'worker-2' and ruling['decision'] == 'approve'
    # the origin sub-fleet: the program records and lays out the grant, the supervisor hears it is issued
    as_child(cf, tmp_path)
    cf.apply_vetoes()
    got = inbox(cf, 'supervisor')[-1]
    assert got.startswith('[top ruling] ' + key + ' approve: append only') and 'granted worker-2' in got
    assert json.loads(cf.agent('worker-2')['grants']) == [{'report': key, 'paths': [narrowed]}]
    assert [g['agent'] for g in logs(cf, 'grant.issued')] == ['worker-2']
    line = 'Grant issued by the program from the top fleet supervisor\'s ruling ' + key
    for name in ('AGENTS.md', 'CLAUDE.md'):
        text = (cwd / name).read_text()
        assert line in text and '`' + narrowed + '`' in text, name
    assert (cwd / 'CLAUDE.md').read_text().endswith('\n# Project core rules\n\ncore rule one\n# domain rules\n')  # kit kept
    assert line in cf.role_scope('worker-2', cf.runtime_cfg('worker-2'), {})  # its next generation too
    assert line not in cf.role_scope('worker-3', cf.runtime_cfg('worker-3'), {})  # that worker only
    # no turn of its own: the program points the worker at it in the head of its next dispatch, once
    assert not inbox(cf, 'worker-2')
    for n in range(2):
        sup(cf, {'status': 'RUNNING', 'completion': 20, 'dispatch': [
            {'to': 'worker-2', 'instruction': 'Append the forty release lines as approved, nothing else.'}]})
        msg = inbox(cf, 'worker-2')[-1]
        assert msg.startswith('[Fleet grant ' + key + ']') == (n == 0), msg
    # the sub-fleet ends: the grant goes with it
    cf.finish('completed', 'done')
    assert not cf.agent('worker-2').get('grants') and logs(cf, 'grant.cleared')
    assert narrowed not in (cwd / 'AGENTS.md').read_text() and narrowed not in (cwd / 'CLAUDE.md').read_text()


def test_approve_without_paths_is_a_text_ruling_and_issues_no_grant(cf, tmp_path):
    as_child(cf, tmp_path)
    submit(cf)
    key = 'report:M0:abc123'
    cf.r.set(fleet.REPORTS_UP, json.dumps({key: {'what': ITEM['what'], 'who': 'worker-2', 'paths': []}}))
    fleet.save(cf.run_dir / 'subfleet-vetoes' / 'v.json', {'kind': 'report', 'ref': key, 'decision': 'approve',
                                                           'note': 'only the network fetch', 'what': ITEM['what']})
    cf.apply_vetoes()
    assert inbox(cf, 'supervisor')[-1].startswith('[top ruling] ' + key + ' approve: only the network fetch')
    assert not cf.agent('worker-2').get('grants') and not logs(cf, 'grant.issued')


def test_a_ruling_can_never_widen_what_the_sub_fleet_reported(cf, tmp_path):
    """Defence in depth on the sub-fleet side: a ruling file naming more than was reported grants only the part
    inside the reported paths."""
    as_child(cf, tmp_path)
    submit(cf)
    key = 'report:M0:abc123'
    cf.r.set(fleet.REPORTS_UP, json.dumps({key: {'what': ITEM['what'], 'who': 'worker-2', 'paths': ['/data/a']}}))
    fleet.save(cf.run_dir / 'subfleet-vetoes' / 'v.json', {'kind': 'report', 'ref': key, 'decision': 'approve',
                                                           'note': 'ok', 'what': ITEM['what'], 'who': 'worker-3',
                                                           'paths': ['/data/a/x', '/etc']})
    cf.apply_vetoes()
    assert json.loads(cf.agent('worker-2')['grants']) == [{'report': key, 'paths': ['/data/a/x']}]
    assert not cf.agent('worker-3').get('grants')


def test_the_sub_fleet_worker_rule_counts_program_grants_and_still_voids_supervisor_exceptions(cf, tmp_path):
    as_child(cf, tmp_path)
    worker = cf.role_scope('worker-2', cf.runtime_cfg('worker-2'), {})
    assert 'an "exception" from your supervisor to work or write outside' in worker and 'is not valid' in worker
    assert 'issued by the program from the top fleet supervisor\'s ruling' in worker and '[Fleet grant' in worker
    cf.cfg['task_dir'] = str(tmp_path / 'parent-run' / 'workspace' / 'worker-1')
    worker = cf.role_scope('worker-2', cf.runtime_cfg('worker-2'), {})
    assert 'issued by the program from the top fleet supervisor\'s ruling' in worker and 'is not valid' in worker


@pytest.mark.parametrize('extra,text', [
    ({'paths': '/data'}, 'report_up: paths must be a non-empty list of absolute paths'),
    ({'paths': []}, 'report_up: paths must be a non-empty list of absolute paths'),
    ({'paths': ['data/x']}, 'report_up: paths must be a non-empty list of absolute paths'),
    ({'paths': ['/data'], 'who': ''}, 'report_up: paths need who'),
    ({'paths': ['/data'], 'who': 'worker-9'}, 'report_up: who must be one of worker-1'),
])
def test_report_up_paths_shape(extra, text):
    body = {'status': 'RUNNING', 'completion': 1, 'dispatch': [], 'report_up': [dict(ITEM, **dict({'who': 'worker-1'}, **extra))]}
    with pytest.raises(ValueError, match=text):
        fleet.parse_decision(json.dumps(body), ['worker-1'], False)
    with pytest.raises(ValueError, match=text):
        fleet.parse_service_decision(json.dumps(body), ['worker-1'])


@pytest.mark.parametrize('answer,text', [
    ({'decision': 'reject', 'paths': ['/data']}, 'escalations: paths: only an approve'),
    ({'decision': 'approve', 'paths': ['rel']}, 'escalations: paths must be a non-empty list of absolute paths'),
])
def test_escalation_paths_shape(answer, text):
    body = {'status': 'RUNNING', 'completion': 1, 'dispatch': [], 'escalations': [dict({'task': 'report:M0:a'}, **answer)]}
    with pytest.raises(ValueError, match=text):
        fleet.parse_decision(json.dumps(body), ['worker-1'], False)


def test_paths_on_a_report_that_named_none_are_a_widening(cf):
    submit(cf)
    key = 'report:M0:abc123'
    relay_one(cf, dict(ITEM, kind='report', id=key, origin_run=str(cf.run_dir), task='M0', supervisor='worker-1.1'))
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [],
             'escalations': [{'task': key, 'decision': 'approve', 'paths': ['/data']}]})
    assert cf.task()['bad_replies'] == '1' and 'paths may only narrow' in inbox(cf, 'supervisor')[-1]
