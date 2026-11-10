// Service-mode Pi supervisor: the actual fleet_decision TypeBox schema accepts escalations[] (plan F', 2026-10-14).
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { Value } from 'typebox/value';
const {default: register}=await import(pathToFileURL(process.argv[2]).href);
const tools=new Map();
register({on(){},registerTool:(t)=>tools.set(t.name,t),registerEntryRenderer(){},registerMessageRenderer(){},appendEntry(){},sendMessage(){}});
const schema=tools.get('fleet_decision').parameters;
const base={status:'RUNNING',reason:'r',dispatch:[],skip:[]};
assert.ok(Value.Check(schema,base),'plain service decision');
assert.ok(Value.Check(schema,{...base,escalations:[{task:'dialog:worker-1:g0',decision:'reject'}]}),'dialog answer');
assert.ok(Value.Check(schema,{...base,escalations:[{task:'blocked:worker-1',decision:'revise',note:'other way'}]}),'blocked answer');
assert.ok(!Value.Check(schema,{...base,escalations:[{task:'blocked:worker-1',decision:'maybe'}]}),'decision is an enum');
assert.ok(!Value.Check(schema,{...base,escalations:{task:'blocked:worker-1',decision:'approve'}}),'escalations is a list');
assert.ok(Value.Check(schema,{...base,dispatch:[{to:'worker-1',instruction:'Judge this fill again',fresh:true}]}),'fresh (design §37)');
assert.ok(!Value.Check(schema,{...base,dispatch:[{to:'worker-1',instruction:'Judge this fill again',fresh:1}]}),'fresh is a boolean');
console.log('service decision schema contract passed');
