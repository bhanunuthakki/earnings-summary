"""Exercise exact-edition routing and safe returns without persisted user data."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

RUNTIME_PATH = Path(__file__).parents[1] / "src/pipeline/work_os_runtime.js"


def _runtime_block(start: str, end: str) -> str:
    runtime = RUNTIME_PATH.read_text(encoding="utf-8")
    return runtime.split(start, 1)[1].split(end, 1)[0]


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for exact brief routing checks")
    result = subprocess.run(
        [node, "-"], input=script, text=True, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr


def test_section_selection_handles_direct_links_and_preserves_pushed_origin() -> None:
    helper = "  function workOsRememberBriefSection(" + _runtime_block(
        "  function workOsRememberBriefSection(", "\n  async function workOsLoadBriefResearchItems"
    )
    _run_node(
        r"""
const assert = require('node:assert/strict');
const initial = 'https://research.example/?ticker=NU&work_os_brief=NU#screen-workspace';
global.window = {
  location: new URL(initial),
  history: {
    state: null,
    replaceState(state, _unused, url) {
      this.state = state;
      window.location = new URL(url, window.location);
    }
  }
};
const artifact = Object.freeze({ticker:'NU', artifact_id:'report_NU_fixed'});
"""
        + helper
        + r"""
workOsRememberBriefSection(artifact, 'saydo');
assert.deepEqual(window.history.state.workOsBriefReader, {
  ticker:'NU', artifactId:'report_NU_fixed', sectionId:'saydo'
});
assert.notEqual(window.history.state.workOsBriefReader.pushed, true);
assert.equal(window.location.searchParams.get('work_os_brief_artifact'), artifact.artifact_id);
assert.equal(window.location.searchParams.get('work_os_brief_section'), 'saydo');
assert.equal(window.location.searchParams.get('work_os_brief'), 'NU');
const previous = Object.freeze({
  screenId:'screen-brief-library', untouched:'retained',
  workOsBriefReader:Object.freeze({
    pushed:true, ticker:'NU', artifactId:'report_NU_fixed',
    origin:'encoded-library-origin', focusId:'brief-edition-control'
  })
});
window.history.state = previous;
workOsRememberBriefSection(artifact, 'sources');
const selected = window.history.state;
assert.equal(selected.untouched, 'retained');
assert.equal(selected.screenId, 'screen-brief-library');
assert.equal(selected.workOsBriefReader.pushed, true);
assert.equal(selected.workOsBriefReader.origin, 'encoded-library-origin');
assert.equal(selected.workOsBriefReader.focusId, 'brief-edition-control');
assert.equal(selected.workOsBriefReader.sectionId, 'sources');
assert.equal(previous.workOsBriefReader.sectionId, undefined, 'Mutated existing history entry');
"""
    )


@pytest.mark.parametrize(
    "wrong_identity",
    ["{ticker:'GOOG',artifact_id:'report_NU_fixed'}", "{ticker:'NU',artifact_id:'other-edition'}"],
    ids=["wrong-company", "wrong-edition"],
)
def test_exact_lookup_rejects_wrong_identity_and_retry_retains_edition_and_section(
    wrong_identity: str,
) -> None:
    lookup = "  window.openWorkOsBriefReader = async function" + _runtime_block(
        "  window.openWorkOsBriefReader = async function", "\n  window.openFullBriefCanvas"
    )
    _run_node(
        r"""
const assert = require('node:assert/strict');
global.window = {};
let workOsBriefLookupSequence = 0, workOsBriefLookupController = null, retry = null;
const workOsRequests = new WeakMap();
const body = {
  innerHTML:'', setAttribute(){}, removeAttribute(){},
  querySelector(){return {addEventListener(_event, callback){retry = callback;}};}
};
const title = {textContent:''};
global.document = {getElementById:id => id === 'workOsBriefReaderBody' ? body : title};
const briefReaderOverlay = {open(){}};
function workOsNormalizeTicker(value){return String(value || '').toUpperCase();}
function workOsCurrentCompanyTicker(){return 'NU';}
function workOsFinishRead(target,state){if(workOsRequests.get(target) === state)workOsRequests.delete(target);}
function workOsBeginRead(target){
  const previous = workOsRequests.get(target);
  if(previous)previous.controller.abort();
  const state = {controller:new AbortController()};
  workOsRequests.set(target,state);
  return state;
}
const accepted = [], pending = [];
async function workOsLoadBriefArtifact(artifact,options){accepted.push({artifact,options});}
async function workOsFetch(url,options){
  return new Promise((resolve,reject) => pending.push({url,options,resolve,reject}));
}
"""
        + lookup
        + r"""
(async()=>{
  const options = Object.freeze({fromHistory:true,artifactId:'report_NU_fixed',sectionId:'sources',factRef:'fact-exact'});
  const initial = window.openWorkOsBriefReader('NU', options);
  assert.equal(pending[0].url, '/api/work-os/briefs/report_NU_fixed');
  pending[0].resolve(new Response(JSON.stringify("""
        + wrong_identity
        + r""")));
  await initial;
  assert.equal(accepted.length, 0, 'Admitted an artifact from another company or edition');
  assert.match(body.innerHTML, /data-work-os-brief-lookup-retry/);
  assert.equal(typeof retry, 'function');
  retry();
  await new Promise(setImmediate);
  assert.equal(pending[1].url, '/api/work-os/briefs/report_NU_fixed', 'Retry fell back to latest edition');
  assert.equal(pending[1].options.signal.aborted, false);
  pending[1].resolve(new Response(JSON.stringify({ticker:'NU',artifact_id:'report_NU_fixed'})));
  await new Promise(setImmediate);
  assert.equal(accepted.length, 1);
  assert.equal(accepted[0].artifact.artifact_id, options.artifactId);
  assert.equal(accepted[0].options.artifactId, options.artifactId);
  assert.equal(accepted[0].options.sectionId, options.sectionId);
  assert.equal(accepted[0].options.factRef, options.factRef);
  assert.equal(accepted[0].options.fromHistory, true);
})().catch(error => {console.error(error);process.exitCode = 1;});
"""
    )


def test_direct_reader_close_returns_to_company_without_leaving_application() -> None:
    close = "  window.closeWorkOsBriefReader = function" + _runtime_block(
        "  window.closeWorkOsBriefReader = function", "\n  const briefReaderBack"
    )
    _run_node(
        r"""
const assert = require('node:assert/strict');
let backs = 0, closes = 0, restores = 0, workOsReaderContext = null;
global.window = {
  location:new URL('https://research.example/?screen=analytics-playground&ticker=GOOG&work_os_brief=NU&work_os_brief_artifact=report_NU_fixed&work_os_brief_section=sources&work_os_focus=old-control&work_os_detail_origin=old-origin#screen-workspace'),
  history:{
    state:null,
    back(){backs++;},
    replaceState(state,_unused,url){this.state=state;window.location=new URL(url,window.location);}
  }
};
const briefReaderOverlay = {close(){closes++;workOsReaderContext=null;}};
function workOsNormalizeTicker(value){return String(value || '').toUpperCase();}
function workOsRestoreCompanyContextFromHistory(){restores++;}
"""
        + close
        + r"""
window.closeWorkOsBriefReader();
assert.equal(backs, 0, 'Direct URL sent the user to an unrelated history entry');
assert.equal(closes, 1);
assert.equal(restores, 1);
assert.equal(window.location.searchParams.get('ticker'), 'NU');
assert.equal(window.location.searchParams.get('screen'), 'company-desk');
assert.equal(window.location.hash, '#screen-workspace');
for(const key of ['work_os_brief','work_os_brief_artifact','work_os_brief_section','work_os_focus','work_os_detail_origin']){
  assert.equal(window.location.searchParams.has(key), false, 'Kept transient parameter ' + key);
}
assert.equal(window.history.state.screenId, 'screen-workspace');
window.history.state = {workOsBriefReader:{pushed:true,ticker:'NU',origin:'library-origin',focusId:'edition-button'}};
window.closeWorkOsBriefReader();
assert.equal(backs, 1, 'Pushed reader did not return to its exact origin');
assert.equal(closes, 1, 'Pushed reader bypassed the history restoration lifecycle');
assert.equal(restores, 1);
"""
    )
