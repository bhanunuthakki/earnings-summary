"""Saved analysis restoration is a bounded, cancellable read computation."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from pipeline.work_os_copilot import WORK_OS_COPILOT_JS


def test_saved_view_deadline_covers_body_and_retry_does_not_replay_writes() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for saved-view request lifecycle evidence")
    start = WORK_OS_COPILOT_JS.index("  function cancelSavedViewRestores()")
    end = WORK_OS_COPILOT_JS.index("  function hydrateStoredExchangeArtifact(", start)
    source = WORK_OS_COPILOT_JS[start:end]
    harness = r"""
const assert=require('node:assert/strict');
var sessionLoadToken=3; var savedViewRestores=new Set();
const reads=[]; const timers=new Map(); let timerId=0;
global.window={setTimeout(callback,ms){const id=++timerId; timers.set(id,{callback,ms});return id;},
  clearTimeout(id){timers.delete(id);},addEventListener(){}};
global.document={createElement(){return {addEventListener(name,handler){this[name]=handler;}};}};
global.fetch=(url,options)=>new Promise(resolve=>reads.push({url,options,resolve}));
const fragment={isConnected:true,innerHTML:'previous content',textContent:'',children:[],
  setAttribute(){},removeAttribute(){},appendChild(child){this.children.push(child);}};
eval(SOURCE);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  const spec={kind:'table',metric:'revenue'};
  const first=restoreSavedView(fragment,spec,3);
  reads[0].resolve({ok:true,text:()=>new Promise((resolve,reject)=>{
    reads[0].options.signal.addEventListener('abort',()=>reject(new Error('body aborted')));
  })});
  await settle();
  assert.equal(timers.size,1,'deadline must cover body after headers');
  assert.equal([...timers.values()][0].ms,30000);
  [...timers.values()][0].callback(); await first;
  assert.match(fragment.textContent,/timed out/);
  assert.equal(savedViewRestores.size,0); assert.equal(timers.size,0);
  const retry=fragment.children[0]; assert.equal(retry.textContent,'Retry restoring view');
  retry.click();
  assert.equal(reads[1].url,'/api/viewspec/run');
  assert.deepEqual(JSON.parse(reads[1].options.body),{spec});
  cancelSavedViewRestores();
  assert.equal(reads[1].options.signal.aborted,true);
  sessionLoadToken=4;
  reads[1].resolve({ok:true,text:()=>Promise.resolve('obsolete view')}); await settle();
  assert.equal(fragment.innerHTML,'previous content','obsolete computation cannot overwrite current conversation');
  assert.equal(timers.size,0);
  const next=restoreSavedView(fragment,spec,4);
  reads[2].resolve({ok:true,text:()=>Promise.resolve('current view')}); await next;
  assert.equal(fragment.innerHTML,'current view');
  assert.equal(savedViewRestores.size,0); assert.equal(timers.size,0);
  assert.ok(reads.every(read=>read.url==='/api/viewspec/run'),'retry never calls Ask stream, save, or approval endpoints');
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("SOURCE", json.dumps(source))
    result = subprocess.run(
        [node, "-"], input=harness, text=True, capture_output=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_history_refresh_keeps_valid_rows_and_exposes_read_only_retry() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for conversation history lifecycle evidence")
    start = WORK_OS_COPILOT_JS.index("  var historyLoadToken = 0;")
    end = WORK_OS_COPILOT_JS.index("  function appendTurn(", start)
    source = WORK_OS_COPILOT_JS[start:end]
    harness = r"""
const assert=require('node:assert/strict');
const pending=[]; const feedbacks=[]; var sessions=[];
const historyNode={innerHTML:'',before(node){feedbacks.push(node);}};
const companySelect={value:'NU'}; const categorySelect={value:'research'}; const historySearch={value:'margin'};
function populateCopilotCompanies(){}
function filterCopilotSessions(){historyNode.innerHTML='valid conversation rows';}
global.window={uiFetch(url,options){return new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}));}};
global.document={createElement(){return {children:[],setAttribute(name,value){this[name]=value;},
  addEventListener(name,handler){this[name]=handler;},appendChild(node){this.children.push(node);}};}};
eval(SOURCE);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  loadCopilotSessions(); pending[0].reject(new Error('offline')); await settle();
  assert.equal(historyNode.innerHTML,'');
  assert.equal(feedbacks[0].role,'alert');
  const retry=feedbacks[0].children[0]; assert.equal(retry.textContent,'Retry loading history');
  retry.click();
  assert.equal(pending[1].url,'/api/ask/sessions?limit=200');
  assert.equal(pending[1].options.method,undefined);
  pending[1].resolve({ok:true,json:()=>Promise.resolve({sessions:[{id:'saved'}]})}); await settle();
  assert.equal(historyNode.innerHTML,'valid conversation rows');
  assert.equal(feedbacks[0].hidden,true);
  loadCopilotSessions();
  assert.equal(historyNode.innerHTML,'valid conversation rows','refresh must not clear previous results');
  pending[2].reject(new Error('deadline')); await settle();
  assert.equal(historyNode.innerHTML,'valid conversation rows');
  assert.match(feedbacks[0].textContent,/may be stale/);
  assert.equal(companySelect.value,'NU'); assert.equal(categorySelect.value,'research');
  assert.equal(historySearch.value,'margin');
  loadCopilotSessions(); loadCopilotSessions();
  assert.equal(pending[3].options.signal.aborted,true);
  pending[4].resolve({ok:true,json:()=>Promise.resolve({sessions:[{id:'newest'}]})}); await settle();
  pending[3].resolve({ok:true,json:()=>Promise.resolve({sessions:[{id:'obsolete'}]})}); await settle();
  assert.equal(sessions[0].id,'newest');
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("SOURCE", json.dumps(source))
    result = subprocess.run(
        [node, "-"], input=harness, text=True, capture_output=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr
