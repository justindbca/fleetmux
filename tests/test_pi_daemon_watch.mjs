// §36 the supervisor's daemon watchdog in the actual extension: the pure decision, then the loop against a
// fixture bridge that reports a dead daemon. Pi event API is mocked (contract harness, not a TUI run).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const mod=await import(pathToFileURL(process.argv[2]).href);
const knobs={check_interval_s:120, stale_after_s:120, realert_after_s:600};
const dead={status:'ok', now:10000, beat:9500, daemon_running:false, unfinished:true, last_event:'deliver',
 last_event_at:9499, pending_events:3, held:['worker-1']};
const watch=mod.daemonWatchdog;
assert.equal(typeof watch,'function');
assert.equal(watch({...dead, beat:9950}, true, 0, 10000, knobs), null, 'fresh beat: no turn');
const text=watch(dead, true, 0, 10000, knobs);
assert.ok(text.startsWith('[Daemon watchdog]'), text);
for (const fact of ['500 s', 'deliver', 'Unprocessed events: 3', 'worker-1', 'AGENTS.md']) assert.ok(text.includes(fact), fact);
assert.ok(!text.includes('\n'), 'one line');
assert.equal(watch({...dead, daemon_running:true}, true, 0, 10000, knobs), null, 'a wedged daemon is not restarted');
assert.equal(watch({...dead, unfinished:false}, true, 0, 10000, knobs), null, 'finished run');
assert.equal(watch(dead, false, 0, 10000, knobs), null, 'session busy: no second turn');
assert.equal(watch(dead, true, 9600, 10000, knobs), null, 'within the re-alert window');
assert.ok(watch(dead, true, 9300, 10000, knobs), 'after the re-alert window');
assert.equal(watch({status:'recorded'}, true, 0, 10000, knobs), null, 'an old bridge answer');
assert.equal(watch(undefined, true, 0, 10000, knobs), null);

const handlers=new Map(), messages=[];
let idle=true;
const ctx={ mode:'tui', hasUI:true, isIdle:()=>idle, hasPendingMessages:()=>false, cwd:'/tmp',
 ui:{setTitle(){},setStatus(){},notify(){}}, getContextUsage:()=>({tokens:100,contextWindow:10000}),
 sessionManager:{getSessionId:()=> 'session-test',getSessionFile:()=>'/tmp/test-session.jsonl',getSessionName:()=> 'supervisor-1',
 getBranch:()=>[]}};
mod.default({on:(name,fn)=>handlers.set(name,fn),registerTool(){},registerEntryRenderer(){},registerMessageRenderer(){},
 appendEntry(){},sendMessage:(msg,opts)=>messages.push({msg,opts})});
const emit=async(name,event={})=>handlers.get(name)?.(event,ctx);
const records=()=>readFileSync(process.argv[3],'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
const sleep=(ms)=>new Promise(r=>setTimeout(r,ms));
await emit('session_start');
await sleep(300);
assert.equal(records().filter(r=>r.kind==='daemon_beat').length,0,'no check before READY');
await emit('before_agent_start',{prompt:'startup'});
await emit('message_end',{message:{role:'assistant',stopReason:'stop',content:[{type:'text',text:'READY'}]}});
await emit('agent_settled',{aborted:false});
const end=Date.now()+3000;
while (!messages.length) { assert.ok(Date.now()<end,'no watchdog turn'); await sleep(10); }
assert.equal(messages[0].msg.customType,'fleet-daemon-watchdog');
assert.ok(messages[0].msg.content.startsWith('[Daemon watchdog]'));
assert.deepEqual(messages[0].opts,{triggerTurn:true});
assert.equal(records().find(r=>r.kind==='daemon_beat').stale_after,120);
await sleep(300);
assert.equal(messages.length,1,'one turn per re-alert window');
assert.ok(records().filter(r=>r.kind==='daemon_beat').length>2,'the loop keeps checking');
await emit('session_shutdown');
const checks=records().filter(r=>r.kind==='daemon_beat').length;
await sleep(200);
assert.equal(records().filter(r=>r.kind==='daemon_beat').length,checks,'the loop stops with the session');
console.log('Pi v2 daemon watchdog contract passed (mock Pi API; no real TUI)');
