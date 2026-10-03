"""Exercise inline filter reads with deliberately reversed response order."""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline import decision_journal_panel, journal_panel, ledger_panel, worldview_panel


def _old_rows(*args: object, **kwargs: object) -> str:
    return "old"


@pytest.mark.parametrize("surface", ["decisions", "research_items"])
def test_latest_filter_wins_and_failed_read_preserves_entries(
    surface: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for inline request lifecycle evidence")
    if surface == "decisions":
        monkeypatch.setattr(decision_journal_panel, "render_decision_journal_list", _old_rows)
        html = decision_journal_panel.render_decision_journal_panel(Path("unused"))
        settings = {
            "filter": "data-dj-filter",
            "list": "#dj-list",
            "feedback": "#dj-feedback",
            "retry": "[data-dj-retry]",
        }
    else:
        monkeypatch.setattr(journal_panel, "render_journal_list", _old_rows)
        html = journal_panel.render_research_items_band(Path("unused"), ticker="TEST")
        settings = {
            "filter": "data-rib-status",
            "list": "[data-rib-list]",
            "feedback": "[data-rib-feedback]",
            "retry": "[data-rib-retry]",
        }
    scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    assert len(scripts) == 1
    harness = r"""
const assert = require('node:assert/strict');
const settings = SETTINGS;
const pending = [];
const nodes = new Map();
function element() {
  return {hidden: true, textContent: '', innerHTML: 'old',
    classList: {toggle() {}}, setAttribute() {}};
}
for (const key of ['list', 'feedback', 'retry']) nodes.set(settings[key], element());
let click;
const root = {dataset: {}, isConnected: true,
  getAttribute(name) {return name === 'data-ticker' ? 'TEST' : 'owner';},
  setAttribute() {}, querySelector(selector) {return nodes.get(selector);},
  querySelectorAll() {return [];}, addEventListener(name, callback) {click = callback;}};
global.document = {getElementById(id) {return id.endsWith('root') || id === 'workOsBriefResearchItems' ? root : nodes.get('#' + id);}};
function read(url, options = {}) {
  return new Promise((resolve, reject) => pending.push({url, options, resolve, reject}));
}
global.window = {uiFetch: read};
global.fetch = read;
eval(SCRIPT);
function choose(value) {
  const button = {hasAttribute() {return false;}, getAttribute(name) {return name === settings.filter ? value : null;}};
  click({target: {closest(selector) {return selector === 'button' || selector === '[' + settings.filter + ']' ? button : null;}}});
}
const settle = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  choose('open'); choose('archived');
  assert.equal(pending.length, 2);
  pending[1].resolve({ok: true, text: () => Promise.resolve('newest')});
  await settle();
  pending[0].resolve({ok: true, text: () => Promise.resolve('obsolete')});
  await settle();
  assert.equal(nodes.get(settings.list).innerHTML, 'newest');
  assert.equal(pending[0].options.signal.aborted, true);
  choose('open');
  pending[2].reject(new Error('deadline'));
  await settle();
  assert.equal(nodes.get(settings.list).innerHTML, 'newest');
  assert.equal(nodes.get(settings.retry).hidden, false);
  assert.equal(nodes.get(settings.feedback).hidden, false);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    harness = harness.replace("SETTINGS", json.dumps(settings)).replace(
        "SCRIPT", json.dumps(scripts[0])
    )
    result = subprocess.run(
        [node, "-"], input=harness, text=True, capture_output=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("surface", ["research", "reconcile", "worldview"])
def test_fragment_refresh_failure_preserves_rows_and_has_read_only_retry(surface: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for inline request lifecycle evidence")
    module, constant = {
        "research": (ledger_panel, "_RESEARCH_JS"),
        "reconcile": (ledger_panel, "_RECONCILE_JS"),
        "worldview": (worldview_panel, "_WORLDVIEW_JS"),
    }[surface]
    source = ""
    module_path = module.__file__
    assert module_path is not None
    for statement in ast.parse(Path(module_path).read_text()).body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == constant for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            assert isinstance(value, str)
            source = value
            break
    assert source
    script = re.findall(r"<script>(.*?)</script>", source, re.DOTALL)[0]
    script = script.replace("})();", "globalThis.testReload=reload;})();")
    harness = r"""
const assert = require('node:assert/strict');
const pending = [];
const feedback = [];
const el = {outerHTML:'prior rows', querySelector(){return null;}, prepend(node){feedback.push(node);}};
global.window = {uiFetch(url, options){return new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}));}};
global.document = {addEventListener(){}, getElementById(){return el;},
  createElement(){return {children:[], setAttribute(){}, addEventListener(name,handler){this.click=handler;}, appendChild(child){this.children.push(child);}};}};
eval(SCRIPT);
(async () => {
  testReload();
  pending[0].reject(new Error('deadline'));
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(el.outerHTML,'prior rows');
  assert.equal(feedback.length,1);
  assert.match(feedback[0].textContent,/Previous entries remain visible/);
  const retry=feedback[0].children[0];
  assert.equal(retry.textContent,'Retry refresh');
  retry.click();
  assert.equal(pending.length,2);
  assert.match(pending[1].url,/fragment=/);
  assert.equal(pending[1].options.method,undefined);
  pending[1].resolve({ok:true,text:()=>Promise.resolve('refreshed rows')});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(el.outerHTML,'refreshed rows');
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("SCRIPT", json.dumps(script))
    result = subprocess.run(
        [node, "-"], input=harness, text=True, capture_output=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr
