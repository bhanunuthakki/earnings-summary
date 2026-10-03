"""Keep company identity truthful while the visible desk loads and recovers."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_company_desk_pending_failure_retry_identity_supersession_and_close() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for runtime lifecycle checks")
    runtime = (Path(__file__).parents[1] / "src/pipeline/work_os_runtime.js").read_text()
    render = runtime.split("  async function workOsRenderCompanyDesk(ticker)", 1)[1].split(
        "  function workOsBriefFacetCounts", 1
    )[0]
    switch = runtime.split("  window.switchCompanyWorkspace = async function", 1)[1].split(
        "  function workOsActionEvidence", 1
    )[0]
    navigate = runtime.split("  window.navigateTo = function", 1)[1].split(
        "  window.goCounterreadHome", 1
    )[0]
    harness = (
        r"""
global.window={location:{pathname:'/',search:'',hash:''},history:{pushState(){throw Error('premature URL write');}}};
let context='A',visible='screen-cockpit',retry=null,workOsCompanyRequestSequence=0,workOsCompanyRequestController=null;
const nodes=new Map();
function element(id){if(!nodes.has(id))nodes.set(id,{id,textContent:'',innerHTML:'',dataset:{},classList:{toggle(){}},attributes:{},hidden:false,setAttribute(k,v){this.attributes[k]=v;},removeAttribute(k){delete this.attributes[k];},prepend(child){this.firstChild=child;},querySelector(){return {addEventListener(_name,callback){retry=callback;}};},querySelectorAll(){return [];}});return nodes.get(id);}
global.document={getElementById:element,querySelectorAll:()=>[element('screen-workspace'),element('screen-cockpit')]};
const companyPickerStatus=element('pickerStatus'),workOsPortfolioHydration={companies:[]},WORK_OS_ENDPOINTS={'screen-workspace':true,'screen-cockpit':true};
const workOsPersistentMountIds={};
let workOsFactPlaygroundRequestController=null,workOsFactPlaygroundRequestSequence=0,workOsFactPlaygroundLoading=null;
function workOsNormalizeTicker(value){return value;}
function workOsCurrentCompanyTicker(){return context;}
function workOsWriteCompanyContext(value){context=value;}
function workOsRenderCompanyBreadcrumb(){}
function workOsAbortTarget(){}
function originalNavigateTo(value){visible=value;}
async function workOsEnsureResearchCompanies(){}
function workOsCompanyByTicker(value){return ['A','B','C'].includes(value)?{ticker:value,name:value}:null;}
function escapeWorkOsHtml(value){return String(value);}
function workOsDecisionMeta(){return '';}
function workOsPercent(){return '';}
function workOsMoney(){return '';}
function workOsRenderEarningsDoorway(){}
function workOsPillClass(){return 'k-pill';}
function workOsThesisStatus(){return 'unavailable';}
function workOsSplitThesisSentences(){return [];}
function workOsFormatThesisNumber(){return '';}
const pending=[];
async function workOsFetch(url,options){return new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}));}
async function settle(){await Promise.resolve();await Promise.resolve();}
function response(ticker){return new Response(JSON.stringify({company:{ticker,name:ticker},conditions:[],open_questions:[]}));}
element('deskTicker').textContent='A';element('deskProvenanceLinks').innerHTML='A evidence';
async function workOsRenderCompanyDesk(ticker)
"""
        + render
        + "\nwindow.switchCompanyWorkspace = async function"
        + switch
    )
    harness += (
        "\nwindow.navigateTo = function"
        + navigate
        + r"""
(async()=>{
  const slow=window.switchCompanyWorkspace('B');
  if(visible!=='screen-workspace'||element('screen-workspace').attributes['aria-busy']!=='true')throw Error('desk navigation waits for network');
  if(!element('deskWarnings').textContent.includes('Loading B')||element('deskWarnings').hidden)throw Error('requested company loading not visible');
  if(context!=='A'||element('deskTicker').textContent!=='A'||element('deskProvenanceLinks').innerHTML!=='A evidence')throw Error('pending read changed identity or evidence');
  await settle();pending[0].reject(Object.assign(new Error('deadline'),{name:'TimeoutError'}));
  if(await slow)throw Error('failed request committed');
  if(visible!=='screen-workspace'||!element('deskWarnings').innerHTML.includes('Retry B')||!retry)throw Error('failed desk lacks visible retry');
  const recovered=retry();await settle();
  if(!pending[1].url.endsWith('/B/desk'))throw Error('retry changed requested company');
  pending[1].resolve(response('B'));if(!await recovered)throw Error('retry failed');
  if(context!=='B'||element('deskTicker').textContent!=='B')throw Error('validated company not committed');
  const evidence=element('deskProvenanceLinks').innerHTML;
  const mismatch=window.switchCompanyWorkspace('C');await settle();pending[2].resolve(response('A'));
  if(await mismatch||context!=='B'||element('deskTicker').textContent!=='B'||element('deskProvenanceLinks').innerHTML!==evidence)throw Error('mismatched identity changed desk');
  const missing=window.switchCompanyWorkspace('C');await settle();pending[3].resolve(new Response('{}'));
  if(await missing||context!=='B')throw Error('missing identity was inferred');
  const unknown=window.switchCompanyWorkspace('UNKNOWN');if(await unknown||pending.length!==4||context!=='B')throw Error('unknown company changed context or fetched');
  const earlier=window.switchCompanyWorkspace('A');await settle();const latest=window.switchCompanyWorkspace('C');await settle();
  if(!pending[4].options.signal.aborted)throw Error('superseded read not aborted');
  pending[5].resolve(response('C'));await latest;pending[4].resolve(response('A'));await earlier;
  if(context!=='C'||element('deskTicker').textContent!=='C')throw Error('late read replaced current identity');
  const closing=window.switchCompanyWorkspace('B');await settle();window.navigateTo('screen-cockpit',{fromHistory:true});
  if(!pending[6].options.signal.aborted)throw Error('navigation did not cancel desk');
  pending[6].resolve(response('B'));await closing;
  if(context!=='C'||visible!=='screen-cockpit'||element('deskTicker').textContent!=='C')throw Error('closed desk committed or reopened');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    )
    result = subprocess.run([node, "-"], input=harness, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
