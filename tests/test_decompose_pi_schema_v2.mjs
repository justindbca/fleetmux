// Task-mode Pi supervisor: the fleet_decision schema carries the decomposition tree keys (design §62).
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { Value } from 'typebox/value';
const {default: register}=await import(pathToFileURL(process.argv[2]).href);
const tools=new Map();
register({on(){},registerTool:(t)=>tools.set(t.name,t),registerEntryRenderer(){},registerMessageRenderer(){},appendEntry(){},sendMessage(){}});
const schema=tools.get('fleet_decision').parameters;
const base={status:'RUNNING',completion:10,reason:'r',dispatch:[]};
const block={id:'M0',goal:'skeleton',cut:'decision',owns:['a.py'],decides:['D1'],est:{minutes:10,sessions:1},
  accept:'tests red',claim:{leaf:true,reason:'one file'}};
assert.ok(Value.Check(schema,{...base,tree:[block,{decision:{id:'D1',q:'shape',owner:'M0',status:'open'}}]}),'tree');
assert.ok(Value.Check(schema,{...base,tree_drop:['M3']}),'tree_drop');
assert.ok(!Value.Check(schema,{...base,tree:[{...block,cut:'random'}]}),'cut is one of three');
assert.ok(!Value.Check(schema,{...base,tree:[{...block,est:{minutes:'ten'}}]}),'est.minutes is a number');
const esc={task:'blocked:worker-1',decision:'approve'};
assert.ok(Value.Check(schema,{...base,escalations:[{...esc,cause:'file_overlap'}]}),'cause');
assert.ok(!Value.Check(schema,{...base,escalations:[{...esc,cause:'bad luck'}]}),'cause is one of five');
assert.ok(Value.Check(schema,{status:'COMPLETED',completion:100,dispatch:[],
  decompose_tune:[{param:'split.k',value:3,reason:'splits cost more than they saved'}]}),'decompose_tune');
assert.ok(!Value.Check(schema,{status:'COMPLETED',completion:100,dispatch:[],decompose_tune:[{param:'split.k',value:'3',reason:'x'}]}),'value is a number');
console.log('decompose schema contract passed');
