"""Policy receipt refresh preserves confirmed charts on cancellation/failure."""

from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline import performance_risk_panel


def test_policy_refresh_is_bounded_preserves_window_and_retires_hidden_reads() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for policy lifecycle evidence")
    module_path = performance_risk_panel.__file__
    assert module_path is not None
    source = ""
    for statement in ast.parse(Path(module_path).read_text()).body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_POLICY_EDITOR_JS"
            for target in statement.targets
        ):
            expression = statement.value
            assert isinstance(expression, ast.Call)
            assert isinstance(expression.func, ast.Attribute)
            assert expression.func.attr == "strip"
            value = ast.literal_eval(expression.func.value)
            assert isinstance(value, str)
            source = value.strip()
            break
    assert source
    runtime = source.removeprefix("<script>").removesuffix("</script>")
    harness = (
        r"""
const timers=[]; const pending=[];
let hidden=false; let observerCallback=null;
const form={dataset:{writeReady:'0'}};
const status={textContent:'',dataset:{}};
const target={dataset:{consoleEndpoint:'/api/panel/performance_risk?fragment=performance&start_date=2026-01-01'},innerHTML:'CONFIRMED CHART',querySelectorAll:()=>[]};
const root={isConnected:true,dataset:{policyRevision:'1',recomputationStatus:'pending'},
 querySelector:key=>key==='[data-policy-form]'?form:status,
 closest:key=>key==='[data-console-endpoint]'?target:hidden&&key.includes('[hidden]')?{}:null};
let receipt=JSON.stringify({revision:2,attempts:0});
global.window={setTimeout:(fn,ms)=>{timers.push({fn,ms,cleared:false});return timers.length;},clearTimeout:id=>{if(timers[id-1])timers[id-1].cleared=true;},
 sessionStorage:{getItem:()=>receipt,setItem:(_key,value)=>receipt=value,removeItem:()=>receipt=null},
 uiFetch:(url,options)=>new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}))};
global.document={hidden:false,body:{},querySelectorAll:()=>[root],addEventListener(){},removeEventListener(){}};
global.MutationObserver=class{constructor(fn){observerCallback=fn;}observe(){}disconnect(){}};
"""
        + runtime
        + r"""
(async()=>{
  timers.find(item=>item.ms===1250).fn();
  if(pending[0].url!==target.dataset.consoleEndpoint||pending[0].options.timeoutMs!==45000)throw Error('window/deadline lost');
  hidden=true;observerCallback();
  if(!pending[0].options.signal.aborted)throw Error('hidden read not cancelled');
  pending[0].resolve(new Response('WRONG OLD CHART'));
  await new Promise(resolve=>setImmediate(resolve));
  if(target.innerHTML!=='CONFIRMED CHART')throw Error('hidden result replaced confirmed chart');
  hidden=false;observerCallback();
  timers.filter(item=>item.ms===1250).at(-1).fn();
  pending[1].reject(new Error('offline'));
  await new Promise(resolve=>setImmediate(resolve));
  if(target.innerHTML!=='CONFIRMED CHART'||!status.textContent.includes('unchanged'))throw Error('failure discarded chart');
  if(!receipt||JSON.parse(receipt).revision!==2)throw Error('pending receipt discarded');
  root.isConnected=false;observerCallback();
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    )
    result = subprocess.run([node, "-"], input=harness, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
