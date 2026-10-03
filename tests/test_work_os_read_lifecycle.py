"""Exercise brief inventory lookup races and recovery with synthetic responses."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_brief_lookup_supersession_error_retry_and_reader_close() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for runtime lifecycle checks")
    runtime = (Path(__file__).parents[1] / "src/pipeline/work_os_runtime.js").read_text()
    lookup = runtime.split("  window.openWorkOsBriefReader = async function", 1)[1].split(
        "  window.openFullBriefCanvas", 1
    )[0]
    harness = (
        r"""
global.window={};
let workOsBriefLookupSequence=0,workOsBriefLookupController=null;
const workOsRequests=new WeakMap();
const body={innerHTML:'',setAttribute(){},removeAttribute(){},querySelector(){return {addEventListener(_name, callback){retry=callback;}}}};
const title={textContent:''};
let retry=null,opens=0;
global.document={getElementById:id=>id==='workOsBriefReaderBody'?body:title};
const briefReaderOverlay={open(){opens++;}};
function workOsNormalizeTicker(value){return value;}
function workOsCurrentCompanyTicker(){return 'A';}
function workOsFinishRead(target,state){if(workOsRequests.get(target)===state)workOsRequests.delete(target);}
function workOsBeginRead(target){const previous=workOsRequests.get(target);if(previous)previous.controller.abort();const state={controller:new AbortController()};workOsRequests.set(target,state);return state;}
const accepted=[];
async function workOsLoadBriefArtifact(artifact){accepted.push(artifact.ticker);}
const pending=[];
let workOsFetch=async(url,options)=>new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}));
window.openWorkOsBriefReader = async function
"""
        + lookup
        + r"""
(async()=>{
  const a=window.openWorkOsBriefReader('A',{fromHistory:true});
  const b=window.openWorkOsBriefReader('B',{fromHistory:true});
  if(!pending[0].options.signal.aborted)throw Error('old lookup not aborted');
  pending[1].resolve(new Response(JSON.stringify({items:[{ticker:'B'}]})));await b;
  pending[0].resolve(new Response(JSON.stringify({items:[{ticker:'A'}]})));await a;
  if(accepted.join(',')!=='B')throw Error('old company replaced current brief');
  const failed=window.openWorkOsBriefReader('C',{fromHistory:true});
  pending[2].reject(Object.assign(new Error('unavailable'),{status:503}));await failed;
  if(!body.innerHTML.includes('data-work-os-brief-lookup-retry')||!retry)throw Error('no recovery UI');
  const retried=retry();
  await Promise.resolve();
  if(!pending[3].url.includes('ticker=C'))throw Error('retry lost ticker');
  pending[3].resolve(new Response(JSON.stringify({items:[]})));await Promise.resolve();await Promise.resolve();
  const closing=window.openWorkOsBriefReader('D',{fromHistory:true});
  workOsBriefLookupSequence++;workOsBriefLookupController.abort();
  pending[4].resolve(new Response(JSON.stringify({items:[{ticker:'D'}]})));await closing;
  if(accepted.includes('D'))throw Error('closed lookup reopened reader');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    )
    result = subprocess.run([node, "-"], input=harness, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_company_picker_unavailable_partial_roster_and_recovery() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for runtime lifecycle checks")
    runtime = (Path(__file__).parents[1] / "src/pipeline/work_os_runtime.js").read_text()
    picker = runtime.split("  async function workOsOpenCompanyPicker()", 1)[1].split(
        "  function workOsChooseCompany", 1
    )[0]
    harness = (
        r"""
let open=false,companyPickerActiveIndex=-1,companyPickerMatches=[];
const companyPickerStatus={textContent:''};
const companyPickerOverlay={isOpen:()=>open,open(){open=true;}};
let roster=[],portfolio=[],attempts=0;
async function workOsEnsureResearchCompanies(){attempts++;if(attempts<3)throw Error('unavailable');roster=[{ticker:'B'}];}
function workOsRenderCompanyPickerOptions(){companyPickerMatches=portfolio.concat(roster);}
async function workOsOpenCompanyPicker()
"""
        + picker
        + r"""
(async()=>{
  await workOsOpenCompanyPicker();
  if(!companyPickerStatus.textContent.includes('unavailable')||companyPickerStatus.textContent.includes('0 companies available'))throw Error('failure became empty roster');
  if(!companyPickerStatus.textContent.includes('reopen'))throw Error('recovery instruction absent');
  open=false;portfolio=[{ticker:'A'}];await workOsOpenCompanyPicker();
  if(!companyPickerStatus.textContent.includes('Partial')||companyPickerMatches.length!==1)throw Error('partial roster not identified');
  open=false;await workOsOpenCompanyPicker();
  if(companyPickerStatus.textContent!=='2 companies available'||attempts!==3)throw Error('recovery did not restore roster');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    )
    result = subprocess.run([node, "-"], input=harness, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
