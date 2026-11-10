"""P8j Codex / pi path (brief v2.2 §8.4 test_security_guard_screen.py; sec-3 survey §5.4 U7, U6, U8, C10, S6).

Red in A2' (stubs raise / today's code has no multi-class) except the two [baseline] tests. Real private Redis
and the real watchdog (``wd`` from test_watchdog), real AgentBridge for the pi audit row, and the real
``pi_extension_v2.ts`` run under the local Node with two fake modules and a fake extension API (S6: no Pi
install, never SKIP; this checks the extension's contract with the bridge, not the real Pi SDK / TUI).
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import dispatch_one, mixed  # noqa: F401  (fixture)
from test_watchdog import READ_FAR, audits, escalated, escalations, keys, wd  # noqa: F401  (fixture)
from test_runtime_v2 import fx  # noqa: F401  (fixture)
from test_bridge_late_settle import rig  # noqa: F401  (fixture)
from cmux_fleet import fleet, fleet_config, sandbox, watchdog
from cmux_fleet import channels
from cmux_fleet.channels import security

ROOT = Path(__file__).resolve().parents[1]
DIALOG_INSTALL = ('Bash command\n  pip install requests\n\nDo you want to proceed?\n❯ 1. Yes\n  2. No')
DIALOG_BOTH = ('Bash command\n  rm x && pip install y\n\nDo you want to proceed?\n❯ 1. Yes\n  2. No')


def policy(**classes):
    pol, errors = security.parse_security('')
    assert not errors
    pol = copy.deepcopy(pol)
    pol['confirm']['classes'].update(classes)
    return pol


def table():
    return watchdog.parse_table(fleet_config.block('watchdog_prompts'))


def test_classify_dialog_multiclass_returns_delete_first_then_others():
    classify_dialog = getattr(watchdog, 'classify_dialog')
    pol = policy()
    assert classify_dialog(table(), None, DIALOG_BOTH, pol) == 'delete'
    assert classify_dialog(table(), None, DIALOG_INSTALL, pol) == 'install'
    assert classify_dialog(table(), None, 'Do you want to make this edit to report.md?\n❯ 1. Yes\n  2. No', pol) == 'other'
    assert classify_dialog(table(), None, '', pol) is None
    # U3: a class set to off never hides another; off classes read as 'other'
    assert classify_dialog(table(), None, DIALOG_BOTH, policy(delete='off')) == 'install'
    assert classify_dialog(table(), None, DIALOG_INSTALL, policy(install='off')) == 'other'


def test_install_dialog_routes_to_user_card_when_class_not_off(wd, tmp_path):
    f = wd
    f.cfg['security'] = policy()
    f.cfg['channels'] = [{'type': 'telegram', 'account': 'fleet-bot-1'}]
    prompt_table = tmp_path / 'prompts.yaml'               # the test's own --prompt-table, never fleet.yaml
    prompt_table.write_text('watchdog_prompts:\n  user_approve: []\n')
    f.cfg['prompt_table'] = str(prompt_table)
    dispatch_one(f)
    pane = f.agent('worker-1')['pane']
    f.mgr.screens[pane] = DIALOG_INSTALL
    f.v2.watchdog.tick(time.time())
    info = f.agent('worker-1')
    assert info['prompt_route'] == 'user' and f.paused()
    waits = audits(f, 'execution.prompt_wait')
    assert waits and json.loads(waits[-1]['detail'])['dialog_class'] == 'install'
    pending = [k for k in f.r.scan_iter(channels.PENDING + '*')]
    assert len(pending) == 1 and f.r.hget(pending[0], 'kind') == 'dialog'
    assert '安装' in json.dumps(f.r.hgetall(pending[0]), ensure_ascii=False)
    assert '确认删除' not in json.dumps(f.r.hgetall(pending[0]), ensure_ascii=False)   # U13: confirm_action
    assert keys(f, 'worker-1') == [] and 'dialog:worker-1:g0' not in escalations(f)
    # P6 baseline: no registered key -> no approve, never inferred from the cursor; never auto-resumed
    assert f.r.hget(pending[0], 'keys_approve') in (None, '', '[]')
    for _ in range(3):
        f.v2.watchdog.tick(time.time())
    assert f.paused() and keys(f, 'worker-1') == []
    # U7: an unreadable screen is not promoted to the user route by the policy
    assert getattr(watchdog, 'classify_dialog')(table(), None, '', f.cfg['security']) is None
    yaml_text = (ROOT / 'config' / 'fleet.yaml').read_text() if (ROOT / 'config' / 'fleet.yaml').exists() else ''
    assert 'user_approve' not in yaml_text


def test_class_off_keeps_today_behaviour(wd):  # [baseline]
    f = wd
    f.cfg['security'] = policy(install='off')
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = DIALOG_INSTALL
    f.v2.watchdog.tick(time.time())
    assert 'dialog:worker-1:g0' in escalations(f)          # the supervisor route of today (prompt_escalate)
    assert not list(f.r.scan_iter(channels.PENDING + '*'))
    assert keys(f, 'worker-1') == []


BINARY_CASES = [
    ('Bash command\n  rm -rf build/tmp\n\nDo you want to proceed?\n❯ 1. Yes\n  2. No', 'delete'),
    ('Bash command\n  git push --force origin main\n\nDo you want to proceed?\n❯ 1. Yes\n  2. No', 'delete'),
    (READ_FAR, 'other'),
    ('Do you want to make this edit to report.md?\n❯ 1. Yes\n  2. No', 'other'),
    (DIALOG_INSTALL, 'other'),
    ('', None),
]


def test_no_policy_dialog_class_is_binary_as_before(wd):  # [baseline]
    for screen, expected in BINARY_CASES:
        assert watchdog.dialog_class(table(), None, screen) == expected, screen
    f = wd                                                  # driven: no security -> today's supervisor route
    assert 'security' not in f.cfg
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = READ_FAR
    f.v2.watchdog.tick(time.time())
    escalated(f, 'worker-1', 'read_outside', ['Enter'])
    assert not list(f.r.scan_iter(channels.PENDING + '*'))


def test_codex_argv_adds_hooks_only_when_probe_says_supported(wd, monkeypatch):
    f = wd
    cfg = dict(f.cfg, worker_cli='codex', supervisor_cli='codex')
    before = fleet.actor_argv(f.path, cfg, 'worker-1', 0, 'p')
    monkeypatch.setattr(sandbox, 'codex_capability', lambda c, what: True)
    on = dict(cfg, security=dict(policy(), sandbox=dict(policy()['sandbox'], codex_hooks='on')))
    argv = fleet.actor_argv(f.path, on, 'worker-1', 0, 'p')
    joined = ' '.join(argv)
    assert 'features.hooks=true' in joined and 'hooks.PreToolUse' in joined and 'claude-pretool' in joined
    monkeypatch.setattr(sandbox, 'codex_capability', lambda c, what: False)
    assert fleet.actor_argv(f.path, on, 'worker-1', 0, 'p') == before
    off = dict(cfg, security=policy())                     # codex_hooks off (default)
    assert fleet.actor_argv(f.path, off, 'worker-1', 0, 'p') == before
    assert fleet.actor_argv(f.path, cfg, 'worker-1', 0, 'p') == before        # no policy: unchanged
    # U8: off -> one degradation audit per Codex slot; a Codex slot never counts as gated
    f.cfg['security'] = policy()
    getattr(sandbox, 'startup_audits')(f)
    rows = [x for x in audits(f, 'security.sandbox_off') if x.get('slot') == 'supervisor']
    assert len(rows) == 1 and rows[0].get('reason') == 'codex_hooks_unverified'
    from cmux_fleet import guard
    assert getattr(guard, 'has_pretool_gate')(dict(cfg, security=on['security']), 'worker-1') is False


def test_pi_worker_triggers_sandbox_off_audit_once(fx):
    f = fx
    f.cfg['security'] = policy()
    startup_audits = getattr(sandbox, 'startup_audits')
    startup_audits(f)
    rows = [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == 'security.sandbox_off']
    assert sorted(x['slot'] for x in rows) == sorted(f.names)
    for _ in range(3):
        f.tick()
    startup_audits(f)                                      # idempotent per run start
    rows2 = [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == 'security.sandbox_off']
    assert len(rows2) == len(rows)
    assert not [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == 'security.tool_call']


FAKE_PI_TUI = 'export class Text { constructor(text) { this.text = text; } render() { return [String(this.text)]; } }\n'
FAKE_TYPEBOX = 'export const Type = new Proxy({}, { get: () => (...args) => ({ schema: args }) });\n'
FIXTURE_CHILD = r'''import json,sys,pathlib
records=pathlib.Path(sys.argv[1])
for line in sys.stdin:
    d=json.loads(line)
    records.open('a').write(json.dumps(d)+'\n')
    if d['kind']=='fixture':
        frame={'kind':'delivery','delivery':{'run_id':'test-run','slot':'worker-1','generation':0,'session_id':'session-test',
               'message_id':d['message_id'],'delivery_id':'d-'+d['message_id'],'stream_id':'1-0','fence':1,
               'body':{'text':'task '+d['message_id'],'title':'t','mode':'service','purpose':'worker',
                       'reply':{'required':['reply'],'allowed':['reply']}}}}
        print(json.dumps(frame),flush=True)
        continue
    if d['kind']=='tool_call':   # the audit write failed on the bridge side
        print(json.dumps({'kind':'reply','call_id':d.get('call_id'),'answer':{'status':'rejected','reason':'audit write failed'}}),flush=True)
        continue
    status='registered' if d['kind']=='init' else 'ready' if d['kind']=='startup' else 'recorded'
    print(json.dumps({'kind':'reply','call_id':d.get('call_id'),'answer':{'status':status}}),flush=True)
    if d['kind']=='shutdown': break
'''
HARNESS = r'''import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const {default: register}=await import(pathToFileURL(process.argv[2]).href);
const handlers=new Map(), tools=new Map(), entries=[], messages=[], statuses=[];
const ctx={ mode:'tui', hasUI:true, isIdle:()=>true, hasPendingMessages:()=>false, cwd:'/tmp',
 ui:{setTitle(){},setStatus:(key,text)=>statuses.push(text),notify(){}},
 getContextUsage:()=>({tokens:100,contextWindow:10000}),
 sessionManager:{getSessionId:()=> 'session-test',getSessionFile:()=>'/tmp/test-session.jsonl',getSessionName:()=> 'worker-1',
 getBranch:()=>[]}};
register({on:(name,fn)=>handlers.set(name,fn),registerTool:(tool)=>tools.set(tool.name,tool),
 registerEntryRenderer(){},registerMessageRenderer(){},
 appendEntry:(type,data)=>entries.push({type,data}),sendMessage:(msg,opts)=>messages.push({msg,opts})});
const emit=async(name,event={})=>handlers.get(name)?.(event,ctx);
const records=()=>readFileSync(process.argv[3],'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
const wait=async(fn)=>{ const end=Date.now()+3000; while(!fn()){ assert.ok(Date.now()<end,'no tool_call frame from the extension'); await new Promise(r=>setTimeout(r,5)); } };
await emit('session_start');
await emit('before_agent_start',{prompt:'startup'});
await emit('message_end',{message:{role:'assistant',stopReason:'stop',content:[{type:'text',text:'READY'}]}});
await emit('agent_settled',{aborted:false});
'''


def test_pi_tool_call_audit_event_shape(rig, tmp_path):
    # (a) the real extension under the local Node: one tool_call frame, args summarised, no fault on error
    node = shutil.which('node')
    assert node, 'S6: the local Node is required (never SKIP); install node to run this contract'
    mods = tmp_path / 'node_modules'
    for name, body in (('@earendil-works/pi-tui', FAKE_PI_TUI), ('typebox', FAKE_TYPEBOX)):
        (mods / name).mkdir(parents=True)
        (mods / name / 'package.json').write_text(json.dumps({'name': name, 'type': 'module', 'exports': './index.js'}))
        (mods / name / 'index.js').write_text(body)
    extension = tmp_path / 'extension.ts'
    shutil.copy(ROOT / 'cmux_fleet' / 'pi_extension_v2.ts', extension)
    records = tmp_path / 'records.jsonl'
    records.write_text('')
    child = tmp_path / 'bridge-fixture.py'
    child.write_text(FIXTURE_CHILD)
    long_args = 'echo ' + 'x' * 300 + ' -H "Authorization: Bearer abcdef0123456789abcdef"'
    harness = tmp_path / 'harness.mjs'
    harness.write_text(HARNESS + "await emit('tool_execution_start',{toolCallId:'t1',toolName:'bash',args:{command:"
                       + json.dumps(long_args) + "}});\n"
                       "await wait(()=>records().some(r=>r.kind==='tool_call'));\n"
                       "await new Promise(r=>setTimeout(r,200));\n"
                       "const calls=records().filter(r=>r.kind==='tool_call');\n"
                       "assert.equal(calls.length,1);\n"
                       "assert.equal(calls[0].tool,'bash');\n"
                       "for (const v of Object.values(calls[0])) if (typeof v==='string') assert.ok(v.length<=200);\n"
                       "assert.equal(records().filter(r=>r.kind==='fault').length,0,'a failed audit must not fault');\n"
                       "console.log('tool_call contract passed');\n"
                       "process.exit(0);\n")
    env = dict(os.environ, CMUX_FLEET_PI_BRIDGE=json.dumps([sys.executable, '-u', str(child), str(records), 'worker-1', '0']),
               CMUX_FLEET_PI_SLOT='worker-1', CMUX_FLEET_PI_MODE='service')
    result = subprocess.run([node, '--experimental-strip-types', str(harness), str(extension), str(records)],
                            env=env, capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
    assert result.returncode == 0 and 'tool_call contract passed' in result.stdout, result.stdout + result.stderr

    # (b) the real AgentBridge: the audit row is written with the bridge's own binding, redacted and capped
    view, bridges, outputs, metadata, daemon, _ = rig
    bridge = bridges['worker-1']
    bridge.cfg['security'] = policy()
    bridge.command(dict(metadata['worker-1'], kind='tool_call', tool='bash', args=long_args))
    bridge.command(dict(metadata['worker-1'], kind='tool_call', tool='bash', args='ls', slot='worker-9'))
    rows = [x for _, x in view.r.xrange(view.bus.audit_key) if x.get('event') == 'security.tool_call']
    assert len(rows) == 1
    row = rows[0]
    assert row['slot'] == 'worker-1' and row.get('generation') == '0' and row.get('tool') == 'bash'
    assert len(row.get('args_summary', '')) <= 200 and 'abcdef0123456789abcdef' not in json.dumps(row)
    assert not [x for _, x in view.r.xrange(view.bus.audit_key) if x.get('event') == 'tool_call']   # not via handle
    bridge.cfg.pop('security')
    bridge.command(dict(metadata['worker-1'], kind='tool_call', tool='bash', args='ls'))
    assert len([x for _, x in view.r.xrange(view.bus.audit_key) if x.get('event') == 'security.tool_call']) == 1
