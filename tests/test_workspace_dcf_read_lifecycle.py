"""Standalone DCF input reads retain their configured authority and stop waiting."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from report.renderers.workspace_dcf import JS


def test_dcf_read_deadline_covers_body_and_cancellation_preserves_retry() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for standalone DCF request lifecycle evidence")
    script = JS.replace("})();", "globalThis.loadDcf=load; globalThis.cancelDcf=cancelLoad;})();")
    harness = r"""
const assert=require('node:assert/strict');
const timers=new Map(); let nextTimer=0; const reads=[]; const nodes=new Map();
function node(){return {hidden:true,isConnected:true,textContent:'',dataset:{},getAttribute(){return 'TEST';},
  addEventListener(name,callback){this[name]=callback;},setAttribute(){}};}
for (const id of ['dcf-edit','dcf-edit-toggle','dcf-edit-body','dcf-edit-status','dcf-edit-controls',
  'dcf-edit-scenarios','dcf-edit-heatmap','dcf-edit-reset','dcf-edit-save','dcf-edit-retry']) nodes.set(id,node());
nodes.set('workspace-boot',{textContent:JSON.stringify({server_url:'http://fixture-authority.invalid:7421',ticker:'TEST'})});
global.document={getElementById(id){return nodes.get(id);},addEventListener(){}};
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
