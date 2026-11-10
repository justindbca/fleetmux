// Service-mode Pi supervisor: the actual fleet_decision TypeBox schema takes the optional subfleet answer (design §29).
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { Value } from 'typebox/value';
const {default: register}=await import(pathToFileURL(process.argv[2]).href);
const tools=new Map();
register({on(){},registerTool:(t)=>tools.set(t.name,t),registerEntryRenderer(){},registerMessageRenderer(){},appendEntry(){},sendMessage(){}});
const schema=tools.get('fleet_decision').parameters;
const base={status:'RUNNING',dispatch:[],skip:[]};
assert.ok(Value.Check(schema,base),'a plain service decision still passes');
assert.ok(Value.Check(schema,{...base,subfleet:{id:'op-1',decision:'approve',note:'fine'}}),'subfleet approve must be accepted');
assert.ok(Value.Check(schema,{...base,subfleet:{id:'op-1',decision:'reject',note:'not now'}}),'subfleet reject must be accepted');
assert.ok(Value.Check(schema,{...base,subfleet:{decision:'approve'}}),'id (bound by code) and note are optional');
assert.ok(!Value.Check(schema,{...base,subfleet:{id:'op-1',decision:'maybe'}}),'decision is approve or reject only');
assert.ok(!Value.Check(schema,{...base,subfleet:{id:'op-1'}}),'decision is required');
assert.ok(!Value.Check(schema,{...base,subfleet:{id:5,decision:'approve'}}),'id is text');
assert.ok(!Value.Check(schema,{...base,subfleet:{decision:'approve',note:3}}),'note is text');
assert.ok(!Value.Check(schema,{...base,subfleet:'approve'}),'subfleet is an object');
console.log('service decision schema contract passed');
