"""Compact brief navigation preserves report identity, access, and selection."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup

from pipeline.work_os_briefs import ReportReaderPayload
from pipeline.work_os_research import render_brief_reader_shell
from report.renderers.workspace_reader_assets import READER_CSS

RUNTIME_PATH = Path(__file__).parents[1] / "src/pipeline/work_os_runtime.js"


def test_reader_keeps_one_header_and_discloses_secondary_context() -> None:
    soup = BeautifulSoup(render_brief_reader_shell(), "html.parser")
    reader = soup.select_one("#workOsBriefReader")
    assert reader is not None
    assert reader.get("role") == "region"
    assert reader.get("aria-modal") != "true"
    headers = reader.select("header.work-os-reader-header")
    assert len(headers) == 1
    assert headers[0].select_one("#workOsBriefReaderTitle") is not None
    toggle = headers[0].select_one("#workOsBriefSectionsToggle")
    assert toggle is not None
    assert toggle.get("aria-controls") == "workOsBriefReaderSections"
    assert toggle.get("aria-expanded") in {"true", "false"}
    assert toggle.get("aria-label")
    assert {"k-btn", "k-btn-quiet"} <= set(toggle.get_attribute_list("class"))
    assert toggle.select_one("svg") is not None
    edition = reader.select_one("details#workOsBriefEditionDetails")
    assert edition is not None and edition.find("summary", recursive=False) is not None
    assert not edition.has_attr("open")
    for node_id in (
        "workOsBriefOwnerState",
        "workOsBriefModelState",
        "workOsBriefDecisionRelationship",
    ):
        assert edition.select_one(f"#{node_id}") is not None
    live_context = edition.select_one("details#workOsBriefLiveContext")
    assert live_context is not None and not live_context.has_attr("open")
    assert live_context.select_one("#workOsBriefResearchItemsMount") is not None


def _runtime_function(name: str) -> str:
    runtime = RUNTIME_PATH.read_text(encoding="utf-8")
    marker = f"  function {name}("
    assert marker in runtime, f"Missing production helper: {name}"
    start = runtime.index(marker)
    end = runtime.index("\n  }", start) + len("\n  }")
    return runtime[start:end]


def test_rail_preferences_preserve_content_and_recover_from_unavailable_storage() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for runtime rail checks")
    helpers = "\n".join(
        _runtime_function(name)
        for name in (
            "workOsRestoreRailPreference",
            "workOsApplyAppSidebar",
            "workOsApplyBriefSections",
        )
    )
    harness = (
        r"""
const assert = require('node:assert/strict');
const values = new Map();
let rejectStorage = false;
global.sessionStorage = {
  getItem(key) { if(rejectStorage) throw Error('blocked'); return values.get(key) ?? null; },
  setItem(key,value) { if(rejectStorage) throw Error('blocked'); values.set(key,String(value)); }
};
function element() {
  const classes = new Set(), attrs = new Map();
  return {
    dataset: {}, hidden: false, children: [],
    classList: {
      toggle(name,on) { if(on) classes.add(name); else classes.delete(name); },
      contains(name) { return classes.has(name); }
    },
    setAttribute(name,value) { attrs.set(name,String(value)); },
    getAttribute(name) { return attrs.get(name) ?? null; },
    contains(node) { return node === this || this.children.includes(node); },
    focus() { document.activeElement = this; }
  };
}
const ids = Object.fromEntries([
  'appSidebar','workOsAppSidebarToggle','workOsBriefReader',
  'workOsBriefReaderSections','workOsBriefSectionsToggle'
].map(id => [id,element()]));
global.document = {
  body: element(), activeElement: null,
  getElementById(id) { return ids[id] ?? null; }
};
global.window = { sessionStorage };
const appKey = 'work-os:app-sidebar:collapsed';
const briefKey = 'work-os:brief-sections:collapsed';
const artifact = Object.freeze({ artifact_id:'report_NU_fixed', ticker:'NU', body_sha256:'fixed' });
global.workOsReaderContext = artifact;
const selected = element();
selected.setAttribute('aria-current','location');
selected.dataset.sectionId = 'financials';
ids.workOsBriefReaderSections.children.push(selected);
const navLink = element();
ids.appSidebar.children.push(navLink);
"""
        + helpers
        + r"""
assert.equal(workOsRestoreRailPreference(appKey,false),false);
assert.equal(workOsRestoreRailPreference(appKey,true),true);
values.set(appKey,'not-a-preference');
assert.equal(workOsRestoreRailPreference(appKey,true),true);
values.delete(appKey);
workOsApplyAppSidebar(true,true);
assert.equal(ids.appSidebar.classList.contains('is-collapsed'),true);
assert.equal(document.body.dataset.workOsSidebarCollapsed,'true');
assert.equal(ids.workOsAppSidebarToggle.getAttribute('aria-expanded'),'false');
assert.match(ids.workOsAppSidebarToggle.getAttribute('aria-label'),/expand/i);
assert.equal(workOsRestoreRailPreference(appKey,false),true);
assert.equal(navLink.hidden,false,'Collapsed app rail must retain its icon links');
assert.equal(ids.workOsBriefReaderSections.hidden,false,'App toggle changed report rail');
document.activeElement = selected;
workOsApplyBriefSections(true,true);
assert.equal(ids.workOsBriefReader.classList.contains('is-sections-collapsed'),true);
assert.equal(ids.workOsBriefReaderSections.hidden,true);
assert.equal(ids.workOsBriefSectionsToggle.getAttribute('aria-expanded'),'false');
assert.match(ids.workOsBriefSectionsToggle.getAttribute('aria-label'),/expand/i);
assert.equal(document.activeElement,ids.workOsBriefSectionsToggle,'Focus left in hidden rail');
assert.equal(workOsRestoreRailPreference(briefKey,false),true);
assert.equal(selected.getAttribute('aria-current'),'location');
assert.equal(selected.dataset.sectionId,'financials');
assert.equal(workOsReaderContext,artifact);
workOsApplyBriefSections(false,false);
assert.equal(ids.workOsBriefReaderSections.hidden,false);
assert.equal(ids.workOsBriefSectionsToggle.getAttribute('aria-expanded'),'true');
assert.match(ids.workOsBriefSectionsToggle.getAttribute('aria-label'),/collapse/i);
assert.equal(workOsRestoreRailPreference(briefKey,false),true,'Nonpersistent render wrote preference');
assert.equal(ids.appSidebar.classList.contains('is-collapsed'),true);
workOsApplyAppSidebar(false,true);
assert.equal(document.body.dataset.workOsSidebarCollapsed,'false');
assert.equal(ids.workOsAppSidebarToggle.getAttribute('aria-expanded'),'true');
assert.equal(workOsRestoreRailPreference(appKey,true),false);
rejectStorage = true;
assert.equal(workOsRestoreRailPreference(briefKey,true),true);
assert.equal(workOsRestoreRailPreference(briefKey,false),false);
workOsApplyBriefSections(true,true);
workOsApplyAppSidebar(true,true);
assert.equal(ids.workOsBriefReaderSections.hidden,true);
assert.equal(document.body.dataset.workOsSidebarCollapsed,'true');
assert.equal(workOsReaderContext,artifact);
"""
    )
    result = subprocess.run(
        [node, "-"], input=harness, text=True, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr


def test_reader_stylesheet_url_tracks_current_asset_content() -> None:
    field = ReportReaderPayload.model_fields["style_url"]
    value = field.get_default(call_default_factory=True)
    assert isinstance(value, str)
    url = urlsplit(value)
    assert url.path == "/api/work-os/report-reader.css"
    versions = [item for items in parse_qs(url.query).values() for item in items]
    digest = hashlib.sha256(READER_CSS.encode("utf-8")).hexdigest()
    assert any(
        re.fullmatch(r"[0-9a-f]{8,64}", item) and digest.startswith(item) for item in versions
    )
