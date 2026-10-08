"""Standalone DCF input reads retain their configured authority and stop waiting."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from report.renderers.workspace_dcf import JS


def test_shared_reader_scopes_dcf_controls_and_cleans_up_without_canceling_save() -> None:
    node = shutil.which("node")
    assert node is not None, "Node is required for shared-reader DCF behavior evidence"
    harness = r"""
const assert=require('node:assert/strict');
require('node:events').setMaxListeners(0);
const requests=[];const timers=new Map();let timerId=0;
function element(){
  const e={hidden:false,isConnected:true,value:'',dataset:{},children:[],events:new Map(),attributes:{},
    classList:{add(){},remove(){}},appendChild(child){this.children.push(child);},
    addEventListener(name,callback,options){
      if(!this.events.has(name))this.events.set(name,new Set());this.events.get(name).add(callback);
      if(options && options.signal)options.signal.addEventListener('abort',()=>this.events.get(name).delete(callback));
    },
    fire(name,event={}){for(const callback of this.events.get(name)||[])callback(event);},
    setAttribute(key,value){this.attributes[key]=value;},getAttribute(key){return this.attributes[key]||null;},
    scrollIntoView(){}};
  let text='';Object.defineProperty(e,'textContent',{get(){return text;},set(v){text=v;this.children=[];}});
  return e;
}
const names=['toggle','body','status','controls','scenarios','heatmap','reset','save','retry'];
function editor(prefix){
  const root=element();root.attributes['data-dcf-ticker']='WRONG';
  const controls=new Map(names.map(name=>['dcf-edit-'+name,element()]));
  for(const [id,control] of controls)control.attributes.id=prefix+id;
  controls.get('dcf-edit-body').hidden=true;
  root.querySelector=selector=>{
    const exact=selector.match(/id="([^"]+)"/)[1];
    const suffix=selector.match(/id\$="([^"]+)"/)[1];
    return [...controls.values()].find(control=>control.attributes.id===exact || control.attributes.id.endsWith(suffix))||null;
  };
  root.querySelectorAll=()=>[];
  root.attributes.id=prefix+'dcf-edit';
  return {root,controls,scope:element()};
}
global.document={getElementById(){return null;},createElement:element};
global.window={location:{protocol:'https:',origin:'https://fixture.ts.net'},addEventListener(){}};
global.CCAction={busy(){},release(){},receipt(){}};
global.setTimeout=(callback,ms)=>{const id=++timerId;timers.set(id,{callback,ms});return id;};
global.clearTimeout=id=>timers.delete(id);
global.fetch=(url,options)=>new Promise((resolve,reject)=>{
  requests.push({url,options,resolve,reject});
  if(options.signal)options.signal.addEventListener('abort',()=>reject(new Error('aborted')));
});
const base={segments:['Synthetic'],base_revenue_by_segment:{Synthetic:1000},
  near_growth_by_segment:{Synthetic:0.1},terminal_growth_by_segment:{Synthetic:0.03},
  wacc:0.11,beta:1.2,risk_free_rate:0.04,equity_risk_premium:0.05,country_risk_premium:0.02,
  cost_of_debt:0.05,tax_rate:0.24,near_op_margin:0.2,terminal_op_margin:0.25,
  terminal_method:'Exit multiple',exit_multiple:12,terminal_growth_g:0.03};
eval(SCRIPT);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const latest=()=>requests[requests.length-1];
async function respond(request,payload){request.resolve({ok:true,status:200,json:()=>Promise.resolve(payload)});await tick();}
const preview={wacc:0.11,current_price:50,fair_value_per_share_usd:70,scenarios:{base:70,bull:80,bear:60},sensitivity:null};
(async()=>{
  assert.equal(typeof window.initDcfEditor,'function','trusted current renderer exposes its initializer');
  const first=editor('reader-0123456789ab-');
  const options={scope:first.scope,ticker:'TEST',mutationHeaders:()=>({'Content-Type':'application/json','X-Fixture':'present'})};
  const mounted=window.initDcfEditor(first.root,options);
  assert.equal(window.initDcfEditor(first.root,options),mounted,'one mount owns each editor');
  first.controls.get('dcf-edit-toggle').fire('click');
  assert.equal(first.controls.get('dcf-edit-body').hidden,false,'namespaced toggle opens');
  const load=latest();assert.equal(load.url,'https://fixture.ts.net/api/dcf/inputs/TEST');
  assert.equal(load.options.headers['X-Fixture'],'present');
  mounted.destroy();assert.equal(load.options.signal.aborted,true,'reader close aborts its input read');
  await respond(load,{inputs:base});
  assert.equal(requests.length,1,'late load cannot start preview');
  first.controls.get('dcf-edit-toggle').fire('click');assert.equal(requests.length,1,'disposed listener removed');
  const second=editor('reader-abcdef012345-');
  const secondMount=window.initDcfEditor(second.root,{scope:second.scope,ticker:'NEXT'});
  second.controls.get('dcf-edit-toggle').fire('click');await respond(latest(),{inputs:base});
  const pendingPreview=latest();assert.match(pendingPreview.url,/\/recompute$/);
  second.controls.get('dcf-edit-save').fire('click');const save=latest();
  assert.equal(pendingPreview.options.signal.aborted,true,'save cancels only pure preview');
  assert.equal(save.options.signal,undefined,'durable save is never canceled');
  assert.equal(JSON.parse(save.options.body).ticker,'NEXT','trusted artifact owns the ticker');
  secondMount.destroy();assert.equal(save.options.signal,undefined);
  await respond(save,{saved:true,inputs:base,...preview});
  assert.doesNotMatch(second.controls.get('dcf-edit-status').textContent,/Saved to model/,'disposed editor ignores late save presentation');
  const third=editor('reader-111111111111-');
  const thirdMount=window.initDcfEditor(third.root,{scope:third.scope,ticker:'THIRD'});
  third.controls.get('dcf-edit-toggle').fire('click');await respond(latest(),{inputs:base});await respond(latest(),preview);
  const inject=element();inject.attributes={'data-dcf-inject':'beta','data-dcf-value':'1.8','data-dcf-label':'Synthetic beta'};
  third.scope.fire('click',{target:{closest(){return inject;}},preventDefault(){}});
  assert.ok([...timers.values()].some(timer=>timer.ms===280),'scoped injection queues preview');
  thirdMount.destroy();
  assert.equal([...timers.values()].some(timer=>timer.ms===280),false,'reader close clears debounce');
  assert.equal(timers.size,0,'reader close also clears presentation timers');
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("SCRIPT", json.dumps(JS))
    result = subprocess.run(
        [node, "-"], input=harness, text=True, capture_output=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_dcf_read_deadline_covers_body_and_cancellation_preserves_retry() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for standalone DCF request lifecycle evidence")
    script = JS.replace(
        "var handle = {destroy: destroy, setDriver: setDriver};",
        "globalThis.loadDcf=load; globalThis.cancelDcf=cancelLoad; var handle = {destroy: destroy, setDriver: setDriver};",
    )
    harness = r"""
const assert=require('node:assert/strict');
const timers=new Map(); let nextTimer=0; const reads=[]; const nodes=new Map();
function node(){return {hidden:true,isConnected:true,textContent:'',dataset:{},getAttribute(){return 'TEST';},
  addEventListener(name,callback){this[name]=callback;},setAttribute(){}};}
for (const id of ['dcf-edit','dcf-edit-toggle','dcf-edit-body','dcf-edit-status','dcf-edit-controls',
  'dcf-edit-scenarios','dcf-edit-heatmap','dcf-edit-reset','dcf-edit-save','dcf-edit-retry']) nodes.set(id,node());
nodes.set('workspace-boot',{textContent:JSON.stringify({server_url:'http://fixture-authority.invalid:7421',ticker:'TEST'})});
global.document={getElementById(id){return nodes.get(id);},addEventListener(){}};
nodes.get('dcf-edit').querySelector=selector=>nodes.get(selector.match(/id="([^"]+)"/)[1]);
global.window={location:{protocol:'file:'},addEventListener(){}};
global.setTimeout=(callback,ms)=>{const id=++nextTimer;timers.set(id,{callback,ms});return id;};
global.clearTimeout=id=>timers.delete(id);
global.fetch=(url,options)=>new Promise(resolve=>reads.push({url,options,resolve}));
eval(SCRIPT);
(async()=>{
  const first=loadDcf();
  assert.equal(reads[0].url,'http://fixture-authority.invalid:7421/api/dcf/inputs/TEST');
  assert.equal(reads[0].options.method,undefined);
  reads[0].resolve({status:200,ok:true,json:()=>new Promise((resolve,reject)=>{
    reads[0].options.signal.addEventListener('abort',()=>reject(new Error('body aborted')));
  })});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(timers.size,1,'deadline stays active after headers');
  [...timers.values()][0].callback();
  await first;
  assert.equal(nodes.get('dcf-edit-retry').hidden,false);
  assert.match(nodes.get('dcf-edit-status').textContent,/timed out/);
  assert.equal(timers.size,0);
  const second=loadDcf();
  cancelDcf();
  assert.equal(reads[1].options.signal.aborted,true);
  reads[1].resolve({status:200,ok:true,json:()=>Promise.resolve({inputs:{}})});
  await second;
  assert.equal(reads.length,2,'obsolete read must not start a recompute mutation');
  assert.equal(timers.size,0);
  const absent=loadDcf();
  reads[2].resolve({status:404,ok:false}); await absent;
  assert.match(nodes.get('dcf-edit-status').textContent,/No editable DCF/);
  assert.equal(nodes.get('dcf-edit-retry').hidden,true);
  const failed=loadDcf(); reads[3].resolve({status:503,ok:false,json:()=>Promise.resolve({error:'Unavailable'})});
  await failed;
  assert.equal(nodes.get('dcf-edit-retry').hidden,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("SCRIPT", json.dumps(script))
    result = subprocess.run(
        [node, "-"], input=harness, text=True, capture_output=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_dcf_edit_intent_and_latest_preview_save_lifecycle() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for DCF editor interaction evidence")
    script = JS.replace(
        "var handle = {destroy: destroy, setDriver: setDriver};",
        "globalThis.loadDcf=load; var handle = {destroy: destroy, setDriver: setDriver};",
    )
    harness = r"""
const assert=require('node:assert/strict');
const timers=new Map(); let nextTimer=0; const requests=[]; const nodes=new Map();
function element(){
  const e={hidden:false,isConnected:true,value:'',dataset:{},children:[],events:{},attributes:{},
    classList:{add(){},remove(){}},appendChild(child){this.children.push(child);},
    addEventListener(name,callback){this.events[name]=callback;},
    setAttribute(key,value){this.attributes[key]=value;},getAttribute(){return 'TEST';},scrollIntoView(){}};
  let text='';Object.defineProperty(e,'textContent',{get(){return text;},set(v){text=v;this.children=[];}});
  return e;
}
for(const id of ['dcf-edit','dcf-edit-toggle','dcf-edit-body','dcf-edit-status','dcf-edit-controls',
  'dcf-edit-scenarios','dcf-edit-heatmap','dcf-edit-reset','dcf-edit-save','dcf-edit-retry']) nodes.set(id,element());
nodes.get('dcf-edit-body').hidden=true;
nodes.set('workspace-boot',{textContent:JSON.stringify({server_url:'http://fixture-authority.invalid:7421',ticker:'TEST'})});
global.document={getElementById(id){return nodes.get(id);},createElement:element,addEventListener(){}};
nodes.get('dcf-edit').querySelector=selector=>nodes.get(selector.match(/id="([^"]+)"/)[1]);
global.window={location:{protocol:'file:'},addEventListener(){},__workspaceMutationHeaders:{'Content-Type':'application/json','X-Fixture':'present'}};
global.CCAction={busy(){},release(){},receipt(){}};
global.setTimeout=(callback,ms)=>{const id=++nextTimer;timers.set(id,{callback,ms});return id;};
global.clearTimeout=id=>timers.delete(id);
global.fetch=(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));
const base={segments:['Total company'],base_revenue_by_segment:{'Total company':1000},
  near_growth_by_segment:{'Total company':0.1},terminal_growth_by_segment:{'Total company':0.03},
  wacc:0.11,beta:1.2,risk_free_rate:0.04,equity_risk_premium:0.05,country_risk_premium:0.02,
  cost_of_debt:0.05,tax_rate:0.24,near_op_margin:0.2,terminal_op_margin:0.25,
  terminal_method:'Exit multiple',exit_multiple:12,terminal_growth_g:0.03};
eval(SCRIPT);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function fireTimer(ms){const found=[...timers].find(([,entry])=>entry.ms===ms);assert.ok(found,`timer ${ms}`);
  timers.delete(found[0]);found[1].callback();}
function field(key){
  const label={'wacc':'WACC (preview only)','beta':'Beta','tax_rate':'Tax rate','near_op_margin':'Near op margin','risk_free_rate':'Risk-free'}[key];
  const walk=e=>e.children.flatMap(child=>[child,...walk(child)]);
  const wrap=walk(nodes.get('dcf-edit-controls')).find(e=>e.children.some(c=>c.textContent===label || c.textContent===label+' (%)'));
  assert.ok(wrap,`field ${key}`);return wrap.children.find(c=>c.type==='number');
}
function edit(key,value){const input=field(key);input.value=String(value);input.events.input();}
function latest(){return requests[requests.length-1];}
function body(request){return JSON.parse(request.options.body);}
function preview(wacc,value=70){return {wacc,current_price:50,fair_value_per_share_usd:value,
  scenarios:{base:value,bull:value+10,bear:value-10},sensitivity:null};}
async function respond(request,payload,ok=true){request.resolve({ok,status:ok?200:422,json:()=>Promise.resolve(payload)});await tick();}
async function begin(){const promise=loadDcf();await respond(latest(),{inputs:base});await promise;return latest();}
(async()=>{
  const initial=await begin();
  assert.equal(initial.url,'http://fixture-authority.invalid:7421/api/dcf/recompute');
  assert.equal(initial.options.headers['X-Fixture'],'present');
  assert.equal(body(initial).wacc_mode,'override','initial workbook WACC is honored');
  await respond(initial,preview(0.11));
  edit('beta',1.4);fireTimer(280);
  const driver=latest();assert.equal(body(driver).wacc_mode,'drivers');
  assert.equal(body(driver).inputs.country_risk_premium,0.02);
  assert.equal(field('wacc').value,'11.00','no local CAPM formula');
  await respond(driver,preview(0.135));assert.equal(field('wacc').value,'13.50');
  edit('wacc',15);fireTimer(280);const override=latest();
  assert.equal(body(override).wacc_mode,'override');assert.equal(body(override).inputs.wacc,0.15);
  edit('near_op_margin',23); // Invalidate before the next debounce fires.
  assert.equal(override.options.signal.aborted,true);
  await respond(override,preview(0.01,999));
  assert.equal(field('wacc').value,'15','old response cannot overwrite input');
  fireTimer(280);const unrelated=latest();assert.equal(body(unrelated).wacc_mode,'override');
  await respond(unrelated,preview(0.15));assert.match(nodes.get('dcf-edit-status').textContent,/preview.only/i);
  edit('tax_rate',30);fireTimer(280);const tax=latest();assert.equal(body(tax).wacc_mode,'drivers');
  await respond(tax,preview(0.133));
  window.dcfSetDriver('wacc',0.18,'Synthetic WACC');fireTimer(280);
  assert.equal(body(latest()).wacc_mode,'override');await respond(latest(),preview(0.18));
  window.dcfSetDriver('tax_rate',0.32,'Synthetic tax');fireTimer(280);
  const injectedTax=latest();assert.equal(body(injectedTax).wacc_mode,'drivers');
  window.dcfSetDriver('beta',1.8,'Synthetic beta');
  await respond(injectedTax,{error:'old error'},false);
  assert.doesNotMatch(nodes.get('dcf-edit-status').textContent,/old error/);
  fireTimer(280);const injectedBeta=latest();assert.equal(body(injectedBeta).wacc_mode,'drivers');
  nodes.get('dcf-edit-reset').events.click();const reset=latest();
  assert.equal(injectedBeta.options.signal.aborted,true);
  await respond(injectedBeta,preview(0.50));
  assert.equal(field('wacc').value,'11.00');assert.equal(body(reset).wacc_mode,'override');
  await respond(reset,preview(0.11));
  edit('beta',1.5);fireTimer(280);const oldNetwork=latest();
  edit('beta',1.6);oldNetwork.reject(new Error('obsolete network error'));await tick();
  assert.doesNotMatch(nodes.get('dcf-edit-status').textContent,/unavailable/);
  fireTimer(280);const oldReload=latest();
  const reloading=loadDcf();const read=latest();
  assert.equal(oldReload.options.signal.aborted,true);
  await respond(oldReload,preview(0.6));
  await respond(read,{inputs:base});await reloading;
  assert.equal(field('beta').value,'1.2','reload cannot accept an old preview');
  await respond(latest(),preview(0.11));
  edit('beta',1.45);const beforeClose=requests.length;
  nodes.get('dcf-edit-toggle').events.click();
  assert.equal(nodes.get('dcf-edit-body').hidden,true);
  assert.equal([...timers.values()].some(entry=>entry.ms===280),false,'close clears debounce');
  nodes.get('dcf-edit-toggle').events.click();
  assert.equal(requests.length,beforeClose+1,'reopen resumes an undispatched preview');
  assert.equal(body(latest()).wacc_mode,'drivers');assert.equal(body(latest()).inputs.beta,1.45);
  await respond(latest(),preview(0.14));assert.equal(field('wacc').value,'14.00');
  edit('tax_rate',28);fireTimer(280);const closedFlight=latest();
  nodes.get('dcf-edit-toggle').events.click();assert.equal(closedFlight.options.signal.aborted,true);
  nodes.get('dcf-edit-toggle').events.click();const reopenedFlight=latest();
  assert.notEqual(reopenedFlight,closedFlight,'reopen resumes an aborted preview');
  await respond(closedFlight,preview(0.9));assert.equal(field('wacc').value,'14.00');
  assert.equal(body(reopenedFlight).inputs.tax_rate,0.28);
  await respond(reopenedFlight,preview(0.139));assert.equal(field('wacc').value,'13.90');
  assert.doesNotMatch(nodes.get('dcf-edit-status').textContent,/Recomputing/);
  edit('beta',1.7);fireTimer(280);const timed=latest();
  timed.resolve({ok:true,status:200,json:()=>new Promise((resolve,reject)=>{
    timed.options.signal.addEventListener('abort',()=>reject(new Error('body aborted')));
  })});await tick();fireTimer(15000);await tick();
  assert.equal(timed.options.signal.aborted,true);
  assert.match(nodes.get('dcf-edit-status').textContent,/timed out/);
  window.dcfSetDriver('wacc',0.02,'Synthetic invalid preview');fireTimer(280);
  await respond(latest(),{error:'perpetuity preview rejected'},false);
  edit('beta',1.9);const beforeImmediateSave=requests.length;
  nodes.get('dcf-edit-save').events.click();const save=latest();
  assert.equal(requests.length,beforeImmediateSave+1,'save dispatches before the pending derivation');
  assert.equal([...timers.values()].some(entry=>entry.ms===280),false,'save clears pending preview');
  assert.equal(save.url,'http://fixture-authority.invalid:7421/api/dcf/save');
  assert.equal(save.options.signal,undefined,'save has no cancellation');
  assert.equal(body(save).inputs.beta,1.9);
  assert.equal(body(save).inputs.wacc,0.02,'backend save owns derivation even with stale preview');
  nodes.get('dcf-edit-save').events.click();assert.equal(latest(),save,'no concurrent save');
  edit('beta',2.1);
  await respond(save,{saved:true,recovery_required:true,cleanup_warning:'retained backup',inputs:{...base,beta:1.9,wacc:0.17},...preview(0.17)});
  assert.equal(field('beta').value,'2.1','later unsaved input survives save completion');
  assert.match(nodes.get('dcf-edit-status').textContent,/unsaved/i);
  assert.match(nodes.get('dcf-edit-status').textContent,/Backup cleanup failed/);
  fireTimer(280);await respond(latest(),preview(0.19));
  assert.match(nodes.get('dcf-edit-status').textContent,/unsaved/i);
  nodes.get('dcf-edit-reset').events.click();assert.equal(field('beta').value,'1.9','reset uses saved baseline');
  await respond(latest(),preview(0.17));
  while([...timers.values()].some(entry=>entry.ms===1500)) fireTimer(1500);
  edit('near_op_margin',27);fireTimer(280);const preHiddenSave=latest();
  nodes.get('dcf-edit-save').events.click();const hiddenSave=latest();
  assert.equal(preHiddenSave.options.signal.aborted,true,'save cancels the stateless preview');
  nodes.get('dcf-edit-toggle').events.click();
  await respond(hiddenSave,{saved:true,recovery_required:true,cleanup_warning:'retained backup',inputs:{...base,beta:1.9,wacc:0.17,near_op_margin:0.27},...preview(0.17)});
  assert.doesNotMatch(nodes.get('dcf-edit-status').textContent,/unsaved/,'closing does not invalidate saved inputs');
  assert.match(nodes.get('dcf-edit-status').textContent,/Saved to model.*Backup cleanup failed/);
  nodes.get('dcf-edit-toggle').events.click();
  while([...timers.values()].some(entry=>entry.ms===1500)) fireTimer(1500);
  edit('near_op_margin',28);fireTimer(280);await respond(latest(),preview(0.17));
  nodes.get('dcf-edit-save').events.click();const failure=latest();
  failure.reject(new Error('lost response'));await tick();
  assert.match(nodes.get('dcf-edit-status').textContent,/not confirmed/i);
  assert.equal(requests.filter(r=>r.url.endsWith('/save')).length,3,'never retry a save automatically');
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("SCRIPT", json.dumps(script))
    result = subprocess.run(
        [node, "-"], input=harness, text=True, capture_output=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr
