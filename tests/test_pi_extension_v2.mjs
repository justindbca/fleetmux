// Actual extension + installed TypeBox/Text + real persistent JSONL child.
// Pi event API is mocked: this is a contract harness, not SDK/TUI validation.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { createRequire, syncBuiltinESMExports } from 'node:module';
const require=createRequire(import.meta.url);
const cp=require('node:child_process');
const realSpawn=cp.spawn;
let bridge;
cp.spawn=(...args)=>{ bridge=realSpawn(...args); return bridge; };
syncBuiltinESMExports();
const {default: register}=await import(pathToFileURL(process.argv[2]).href);
const handlers=new Map(), tools=new Map(), renderers=new Map(), entries=[], messages=[], statuses=[];
let idle=true;
const ctx={ mode:'tui', hasUI:true, isIdle:()=>idle, hasPendingMessages:()=>false, cwd:'/tmp',
 ui:{setTitle(){},setStatus:(key,text)=>statuses.push(text),notify(){}},
 getContextUsage:()=>({tokens:100,contextWindow:10000}),
 sessionManager:{getSessionId:()=> 'session-test',getSessionFile:()=>'/tmp/test-session.jsonl',getSessionName:()=> 'worker-1',
 getBranch:()=>[{type:'custom',customType:'fleet-ui',data:{event_id:'saved'}}]}};
register({on:(name,fn)=>handlers.set(name,fn),registerTool:(tool)=>tools.set(tool.name,tool),
 registerEntryRenderer:(name,fn)=>renderers.set(name,fn),registerMessageRenderer:(name,fn)=>renderers.set(name,fn),
 appendEntry:(type,data)=>entries.push({type,data}),sendMessage:(msg,opts)=>messages.push({msg,opts})});
const emit=async(name,event={})=>handlers.get(name)?.(event,ctx);
const records=()=>readFileSync(process.argv[3],'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
const fixture=(data)=>bridge.stdin.write(JSON.stringify({kind:'fixture',...data})+'\n');
const wait=async(fn)=> { const end=Date.now()+2000; while(!fn()){assert.ok(Date.now()<end,'contract frame timed out'); await new Promise(r=>setTimeout(r,5));} };
assert.equal(bridge,undefined,'factory must not spawn a process');
await emit('session_start');
assert.ok(bridge && renderers.has('fleet-ui'));
await emit('before_agent_start',{prompt:'startup'});
assert.equal((await emit('input',{source:'interactive',text:'too early operator text'})).action,'handled');
assert.equal(messages.length,0,'unready manual input must be visibly rejected');
await emit('message_end',{message:{role:'assistant',stopReason:'stop',content:[{type:'text',text:'READY'}]}});
await emit('agent_settled',{aborted:false});
assert.equal(records().filter(r=>r.kind==='startup').length,1);
fixture({event:'ui',event_id:'ui-1',render:'超时：原执行尚未确认停止。'});
fixture({event:'ui',event_id:'ui-1',render:'duplicate'});
fixture({event:'ui',event_id:'saved',render:'replayed old card'});
await wait(()=>entries.some(e=>e.data.event_id==='ui-1'));
assert.equal(entries.filter(e=>e.data.event_id==='ui-1').length,1);
assert.equal(entries.filter(e=>e.data.event_id==='saved').length,0);
assert.equal(messages.length,0,'UI-only must never call sendMessage');
fixture({event:'delivery',message_id:'m-first'});
await wait(()=>messages.length===1);
assert.equal(messages[0].msg.content,'业务 JSON {"amount":2} /tmp/report.md https://example.org');
assert.ok(!messages[0].msg.content.includes('m-first'));
assert.equal(messages[0].msg.details.message_id,'m-first');
assert.deepEqual(messages[0].opts,{triggerTurn:true}); // no hidden followUp/steer queue
await emit('agent_start');
const midturnText='[FLEET MID-TURN HANDOFF fx-0123456789abcdef] Context is 8000/10000. HANDOFF.';
assert.equal((await emit('input',{source:'interactive',text:midturnText})).action,'continue');
assert.equal((await emit('input',{source:'interactive',text:'ordinary manual worker text'})).action,'handled');
assert.equal(records().filter(r=>r.kind==='operator_input').length,0);
const result=tools.get('fleet_result');
assert.ok(result.renderResult({content:[{type:'text',text:'validation error'}]}).render(80).join('\n').includes('候选未记录'));
await emit('tool_execution_end',{toolCallId:'rejected-tool',toolName:'fleet_result',isError:true,result:{content:[{type:'text',text:'invalid required key'}]}});
await emit('message_end',{message:{role:'assistant',stopReason:'error',errorMessage:'full provider error',content:[]}});
assert.ok(records().some(record=>record.kind==='candidate' && record.candidate?.error==='full provider error'));
await emit('message_end',{message:{role:'assistant',stopReason:'toolUse',content:[]}}); // recovered provider run
const candidate=await result.execute('tool-1',{result:{reply:'{"business":true} https://example.org'}},undefined,undefined,ctx);
assert.equal(candidate.terminate,true);
assert.equal(records().filter(r=>r.kind==='settled').length,0);
await emit('agent_end'); await emit('turn_end');
assert.equal(records().filter(r=>r.kind==='settled').length,0);
await emit('agent_settled',{aborted:false});
const settled=records().filter(r=>r.kind==='settled');
assert.equal(settled.length,1);
assert.deepEqual(settled[0].candidates,[{tool:'fleet_result',value:{reply:'{"business":true} https://example.org'}}]);
assert.equal(messages.length,1,'settlement must not create a protocol-text follow-up turn');
await emit('agent_settled',{aborted:false});
assert.equal(records().filter(r=>r.kind==='settled').length,1);
fixture({event:'completed',message_id:'m-first'});
await wait(()=>statuses.at(-1)==='等待派工');
// A manual run cannot collect a result and cannot receive a hidden delivery.
assert.equal((await emit('input',{source:'interactive',text:'manual worker question'})).action,'continue');
await emit('before_agent_start',{prompt:'manual worker question'}); idle=false;
await assert.rejects(()=>result.execute('manual',{result:{reply:'not fleet'}},undefined,undefined,ctx),/No active/);
fixture({event:'delivery',message_id:'m-collision'});
await wait(()=>records().some(r=>r.kind==='reject_delivery'));
assert.equal(messages.length,1);
await emit('agent_settled',{aborted:false}); idle=true;
assert.equal(records().filter(r=>r.kind==='settled').length,1,'manual run never publishes a fleet result');
fixture({event:'delivery',message_id:'m-second'});
await wait(()=>messages.length===2);
await emit('agent_start');
await result.execute('one',{result:{reply:'a'}},undefined,undefined,ctx);
await result.execute('two',{result:{reply:'b'}},undefined,undefined,ctx);
await emit('agent_settled',{aborted:true});
assert.equal(records().filter(r=>r.kind==='settled').at(-1).candidates.length,2);
assert.equal(records().filter(r=>r.kind==='settled').at(-1).aborted,true);
fixture({event:'completed',message_id:'m-second'});
await wait(()=>statuses.at(-1)==='等待派工');
// Replay the hook order real Pi emitted for a sendMessage(triggerTurn) delivery run
// (tests/record_pi_event_order.py). 1.2.0 adds before_agent_start{messageType:'custom'}.
for (const version of ['1.1.0','1.2.0']) {
  const recorded=JSON.parse(readFileSync(new URL(`./fixtures/pi_events_${version}.json`,import.meta.url),'utf8'));
  assert.equal(recorded.pi_version,version);
  const run=recorded.events.slice(recorded.events.findIndex(e=>e.hook==='delivery_frame'));
  assert.equal(run.some(e=>e.hook==='before_agent_start'),version!=='1.1.0','fixture shows the version difference');
  const id=`m-replay-${version}`, before=records().filter(r=>r.kind==='settled').length, startedBefore=records().filter(r=>r.kind==='started').length;
  fixture({event:'delivery',message_id:id});
  await wait(()=>messages.at(-1)?.msg.details.message_id===id);
  // The fix relies on this: while a delivery is active no human run can start on the slot.
  const sent=messages.length;
  assert.equal((await emit('input',{source:'interactive',text:'typed during delivery'})).action,'handled');
  assert.equal(messages.length,sent);
  for (const e of run) {
    idle=e.idle ?? idle;
    if (e.hook==='before_agent_start') await emit('before_agent_start',{messageType:e.messageType,
      message:{role:'custom',customType:e.customType,content:messages.at(-1).msg.content,display:true,details:messages.at(-1).msg.details}});
    else if (e.hook==='agent_start') await emit('agent_start');
    else if (e.hook==='tool_execution_start') await emit('tool_execution_start',{toolCallId:'replay-'+version,toolName:'fleet_result'});
    else if (e.hook==='tool_execute') assert.equal((await result.execute('replay-'+version,{result:{reply:'replayed'}},undefined,undefined,ctx)).terminate,true);
    else if (e.hook==='tool_execution_end') await emit('tool_execution_end',{toolCallId:'replay-'+version,toolName:'fleet_result',isError:e.isError});
    else if (e.hook==='agent_settled') { idle=true; await emit('agent_settled',{aborted:e.aborted}); }
  }
  assert.equal(records().filter(r=>r.kind==='started').length,startedBefore+1,`${version}: delivery run reports started`);
  const settledNow=records().filter(r=>r.kind==='settled');
  assert.equal(settledNow.length,before+1,`${version}: delivery run must settle`);
  assert.deepEqual(settledNow.at(-1).candidates,[{tool:'fleet_result',value:{reply:'replayed'}}]);
  fixture({event:'completed',message_id:id});
  await wait(()=>statuses.at(-1)==='等待派工');
}
// §47 a busy daemon answers settled late: no fault, no idle report while the delivery is still
// active; the late completed frame reports availability so the next delivery can come.
fixture({event:'settle_pending'});
fixture({event:'delivery',message_id:'m-late'});
await wait(()=>messages.at(-1)?.msg.details.message_id==='m-late');
await emit('agent_start'); idle=false;
await result.execute('late',{result:{reply:'late'}},undefined,undefined,ctx);
const idleBefore=records().filter(r=>r.kind==='availability'&&r.idle===true).length;
idle=true; await emit('agent_settled',{aborted:false});
await wait(()=>statuses.at(-1)==='已结算，等 fleet 确认');
await new Promise(r=>setTimeout(r,50));
assert.equal(records().filter(r=>r.kind==='availability'&&r.idle===true).length,idleBefore,'no idle report while the late settle is open');
assert.ok(!statuses.includes('通信中断；fleet 将自动重启本会话'));
fixture({event:'completed',message_id:'m-late'});
await wait(()=>records().filter(r=>r.kind==='availability'&&r.idle===true).length===idleBefore+1);
assert.equal(statuses.at(-1),'等待派工');
fixture({event:'delivery',message_id:'m-after-late'});
await wait(()=>messages.at(-1)?.msg.details.message_id==='m-after-late');
await emit('agent_start');
await result.execute('after',{result:{reply:'after'}},undefined,undefined,ctx);
await emit('agent_settled',{aborted:false});
fixture({event:'completed',message_id:'m-after-late'});
await wait(()=>statuses.at(-1)==='等待派工');
// §47 the settled send itself times out (bridge busy with an earlier command): same as a late answer.
const realTimeout=globalThis.setTimeout;
globalThis.setTimeout=(fn,ms,...rest)=>realTimeout(fn,ms===35000?30:ms,...rest);
fixture({event:'settle_silent'});
fixture({event:'delivery',message_id:'m-silent'});
await wait(()=>messages.at(-1)?.msg.details.message_id==='m-silent');
await emit('agent_start');
await result.execute('silent',{result:{reply:'silent'}},undefined,undefined,ctx);
const idleSilent=records().filter(r=>r.kind==='availability'&&r.idle===true).length;
await emit('agent_settled',{aborted:false});
globalThis.setTimeout=realTimeout;
assert.equal(statuses.at(-1),'已结算，等 fleet 确认');
fixture({event:'completed',message_id:'m-silent'});
await wait(()=>records().filter(r=>r.kind==='availability'&&r.idle===true).length===idleSilent+1);
const count=messages.length;
fixture({event:'ui',event_id:'late-ui',render:'失败，不代表已停止。'});
await wait(()=>entries.some(e=>e.data.event_id==='late-ui'));
assert.equal(messages.length,count);
// A split-cap hold arrives as a fault frame with its own text; it must not read as "connection lost".
fixture({event:'fault',reason:'已达续做上限，等监工拆小。'});
await wait(()=>statuses.at(-1)==='已达续做上限，等监工拆小。');
await emit('session_shutdown'); await emit('session_shutdown');
assert.equal(bridge.exitCode,0);
// A second actual extension instance proves the prefix check precedes the supervisor input bridge.
process.env.FLEETMUX_PI_SLOT='supervisor';
const supervisorHandlers=new Map(), supervisorMessages=[];
register({on:(name,fn)=>supervisorHandlers.set(name,fn),registerTool(){},
 registerEntryRenderer(){},registerMessageRenderer(){},appendEntry(){},
 sendMessage:(msg)=>supervisorMessages.push(msg)});
const sup=async(name,event={})=>supervisorHandlers.get(name)?.(event,ctx);
await sup('session_start');
await sup('before_agent_start',{prompt:'startup'});
await sup('message_end',{message:{role:'assistant',stopReason:'stop',content:[{type:'text',text:'READY'}]}});
await sup('agent_settled',{aborted:false});
fixture({event:'delivery',message_id:'m-supervisor',slot:'supervisor'});
await wait(()=>supervisorMessages.length===1);
await sup('agent_start');
const operatorCount=records().filter(r=>r.kind==='operator_input').length;
assert.equal((await sup('input',{source:'interactive',text:midturnText})).action,'continue');
assert.equal(records().filter(r=>r.kind==='operator_input').length,operatorCount);
assert.equal((await sup('input',{source:'interactive',text:'ordinary supervisor instruction'})).action,'handled');
assert.equal(records().filter(r=>r.kind==='operator_input').length,operatorCount+1);
await sup('session_shutdown');
console.log('Pi v2 persistent bridge contract passed (mock Pi API; no real TUI)');
