"""Consolidated Provenance console assembler + route (S10 PR2).

The 8-tab System diagnostics strip collapsed into one page composing the 8
builders, Coverage prominent. These pin: the composition (all 8 sections,
Coverage first, anchor nav), the degrade-don't-crash contract, and the
/api/panel/provenance route + the old-id deep-link aliases.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from pipeline.provenance_panel import render_provenance_panel  # noqa: E402

_SECTIONS = (
    "coverage",
    "validation",
    "evals",
    "ir_coverage",
    "source_calls",
    "cron_health",
    "dcf_coverage",
    "restatements",
)


def _empty_db(tmp_path: Path) -> Path:
    db = tmp_path / "portfolio.db"
    sqlite3.connect(str(db)).close()
    return db


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------


def test_assembler_composes_all_eight_sections(tmp_path: Path) -> None:
    html = render_provenance_panel(_empty_db(tmp_path), tmp_path)
    assert 'class="prov-console"' in html
    for anchor in _SECTIONS:
        assert f'id="prov-{anchor}"' in html  # the section wrapper
        assert f'data-prov-jump="prov-{anchor}"' in html  # the anchor-nav chip (JS-scroll)
    # The chips must NOT be href anchors — that would trip the shell hash router.
    assert 'href="#prov-' not in html


def test_nav_band_is_sticky_with_underline_active_chips(tmp_path: Path) -> None:
    """Owner directive 2026-08-02: across eleven sections the anchor nav must
    stay pinned below the shell topbar (``.k-toolbar-sticky``) rather than
    scrolling away, and its chips use the shared underline-active modifier
    (``.k-chip-tab``) instead of a per-console reinvention."""
    html = render_provenance_panel(_empty_db(tmp_path), tmp_path)
    assert 'class="k-toolbar k-toolbar-sticky"' in html
    for anchor in _SECTIONS:
        assert f'class="k-chip k-chip-btn k-chip-tab" data-prov-jump="prov-{anchor}"' in html


def test_coverage_leads_prominently(tmp_path: Path) -> None:
    """The owner: 'Coverage already makes sense, keep it prominent.' It is the
    first section, not buried mid-page."""
    html = render_provenance_panel(_empty_db(tmp_path), tmp_path)
    assert html.index('id="prov-coverage"') < html.index('id="prov-validation"')
    assert html.index('id="prov-validation"') < html.index('id="prov-evals"')


def test_assembler_initializes_every_diagnostic_with_native_loading_states(
    tmp_path: Path,
) -> None:
    """Every builder degrades to a stub on missing tables — the console renders
    rather than crashing on a bare DB."""
    html = render_provenance_panel(_empty_db(tmp_path), tmp_path)
    assert html.count('data-prov-endpoint="/api/panel/') == 11
    assert 'data-prov-endpoint="/api/panel/section_coverage"' in html
    assert 'data-prov-endpoint="/api/panel/validation"' in html
    assert 'data-prov-endpoint="/api/panel/credibility"' in html
    assert 'data-prov-state="loading"' in html
    assert "data-prov-retry" in html
    assert "window.htmx" not in html
    assert "fetch(endpoint" in html
    assert "MAX_CONCURRENT = 3" in html
    assert "visibilityObserver.observe" in html
    assert "unmountObserver.observe" in html
    assert "childList: true,\n    subtree: true" not in html
    assert "requestState.reason = 'timeout'" in html
    assert "temporarily unavailable" in html
    assert "'empty'" in html
    assert "'unavailable'" in html
    assert "'error'" in html
    assert "Fetched" in html
    # A bare DB still produces each builder's missing/empty state, not a 500.
    assert "Validation" in html


def test_provenance_runtime_bounds_concurrency_and_aborts_when_hidden() -> None:
    node = shutil.which("node")
    if node is None:
        return
    import pipeline.provenance_panel as provenance_panel

    runtime = json.dumps(getattr(provenance_panel, "_PROV_NAV_JS"))
    harness = f"""
const observerCallbacks = [];
let visibilityCallback = null;
let fetchCount = 0;
let abortCount = 0;
const status = {{textContent:''}};
function section(index) {{
  return {{
    dataset: {{provEndpoint:'/api/panel/' + index, provLabel:'S' + index, provState:'loading'}},
    setAttribute() {{}}, removeAttribute() {{}}, querySelector() {{ return null; }},
    querySelectorAll() {{ return []; }}, append() {{}}, focus() {{}}, innerHTML:''
  }};
}}
const sections = Array.from({{length:11}}, (_value, index) => section(index));
const root = {{
  isConnected: true, dataset: {{}},
  parentElement: {{}},
  classList: {{contains: value => value === 'prov-console'}},
  closest: () => null,
  querySelector: selector => selector.includes('live-status') ? status : null,
  querySelectorAll: selector => selector === '[data-prov-section]' ? sections : [],
  addEventListener() {{}}
}};
global.window = {{
  location: new URL('http://127.0.0.1:7421/'), setTimeout, clearTimeout
}};
global.document = {{
  hidden: true,
  currentScript: {{previousElementSibling: root}},
  documentElement: {{}},
  addEventListener: (name, fn) => {{ if (name === 'visibilitychange') visibilityCallback = fn; }},
  removeEventListener() {{}},
  createElement: () => ({{className:'', dataset:{{}}, textContent:''}}),
  getElementById: () => null
}};
global.MutationObserver = class {{
  constructor(fn) {{ observerCallbacks.push(fn); }}
  observe() {{}}
  disconnect() {{}}
}};
global.fetch = (_url, options) => {{
  fetchCount += 1;
  return new Promise((_resolve, reject) => {{
    options.signal.addEventListener('abort', () => {{
      abortCount += 1;
      const error = new Error('aborted'); error.name = 'AbortError'; reject(error);
    }});
  }});
}};
eval({runtime});
if (fetchCount !== 0) throw new Error('hidden provenance root fetched');
document.hidden = false;
visibilityCallback();
if (fetchCount !== 3) throw new Error('provenance concurrency was not bounded to three');
document.hidden = true;
observerCallbacks[0]();
if (abortCount !== 3) throw new Error('hidden provenance root did not abort active requests');
"""
    result = subprocess.run(
        [node, "-"], input=harness, text=True, capture_output=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_provenance_retries_use_the_same_concurrency_queue() -> None:
    node = shutil.which("node")
    if node is None:
        return
    import pipeline.provenance_panel as provenance_panel

    runtime = json.dumps(getattr(provenance_panel, "_PROV_NAV_JS"))
    harness = f"""
let clickHandler = null;
let activeFetches = 0;
let maxFetches = 0;
const pending = [];
const status = {{textContent:''}};
function section(index) {{
  return {{
    dataset: {{provEndpoint:'/api/panel/' + index, provLabel:'S' + index, provState:'unavailable'}},
    setAttribute() {{}}, removeAttribute() {{}}, querySelector() {{ return null; }},
    querySelectorAll() {{ return []; }}, append() {{}}, focus() {{}}, innerHTML:'',
    closest: () => null
  }};
}}
const sections = Array.from({{length:11}}, (_value, index) => section(index));
const root = {{
  isConnected:true, dataset:{{}}, parentElement:{{}},
  classList:{{contains:value => value === 'prov-console'}}, closest:()=>null,
  querySelector:selector => selector.includes('live-status') ? status : null,
  querySelectorAll:selector => selector === '[data-prov-section]' ? sections : [],
  addEventListener:(name, fn) => {{ if (name === 'click') clickHandler = fn; }}
}};
global.window = {{location:new URL('http://127.0.0.1:7421/'), setTimeout, clearTimeout}};
global.document = {{
  hidden:false, currentScript:{{previousElementSibling:root}}, documentElement:{{}},
  addEventListener() {{}}, removeEventListener() {{}},
  createElement:() => ({{className:'', dataset:{{}}, textContent:''}}), getElementById:()=>null
}};
global.MutationObserver = class {{ constructor(_fn) {{}} observe() {{}} disconnect() {{}} }};
global.fetch = (_url, options) => {{
  activeFetches += 1; maxFetches = Math.max(maxFetches, activeFetches);
  return new Promise((resolve, reject) => {{
    options.signal.addEventListener('abort', () => reject(new Error('aborted')));
    pending.push(() => {{ activeFetches -= 1; resolve({{ok:false, text:async()=>''}}); }});
  }});
}};
eval({runtime});
sections.forEach(section => {{
  const retry = {{closest:selector => selector === '[data-prov-section]' ? section : null}};
  clickHandler({{target:{{closest:selector => selector === '[data-prov-retry]' ? retry : null}}}});
}});
if (maxFetches !== 3) throw new Error('retry concurrency exceeded shared cap: ' + maxFetches);
if (pending.length !== 3) throw new Error('retry queue did not stop at three active requests');
process.exit(0);
"""
    result = subprocess.run(
        [node, "-"], input=harness, text=True, capture_output=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_assembler_does_not_call_diagnostic_builders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_safe isolates a raised builder into an error card; the rest still render.
    The assembler re-imports each builder per call, so patching the source name
    takes effect."""

    def _boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("synthetic builder failure")

    monkeypatch.setattr("pipeline.validation_issues_panel.render_validation_panel", _boom)
    html = render_provenance_panel(_empty_db(tmp_path), tmp_path)
    assert "synthetic builder failure" not in html
    assert 'data-prov-endpoint="/api/panel/validation"' in html
    assert 'id="prov-coverage"' in html  # the other sections survived


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def test_provenance_route_renders(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    _empty_db(tmp_path / "data")
    client = comments_server.create_app(tmp_path).test_client()
    resp = client.get("/api/panel/provenance")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'class="prov-console"' in body
    assert 'id="prov-coverage"' in body


def test_cron_health_route_serves_a_pollable_live_fragment(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    _empty_db(tmp_path / "data")
    client = comments_server.create_app(tmp_path).test_client()

    panel = client.get("/api/panel/cron_health").get_data(as_text=True)
    fragment = client.get("/api/panel/cron_health?fragment=live")

    assert 'data-cron-fragment-url="/api/panel/cron_health?fragment=live"' in panel
    assert 'data-refresh-ms="60000"' in panel
    assert fragment.status_code == 200
    assert fragment.headers["Cache-Control"] == "no-store"
    fragment_body = fragment.get_data(as_text=True)
    assert "No pipeline run rows yet" in fragment_body
    assert "Observed by server" in fragment_body
    assert "<section" not in fragment_body


def test_old_system_panel_routes_still_serve(tmp_path: Path) -> None:
    """The /api/panel/<id> fetch routes for each builder stay live (the console
    composes them; a direct fetch / cached deep-link still resolves)."""
    (tmp_path / "data").mkdir()
    _empty_db(tmp_path / "data")
    client = comments_server.create_app(tmp_path).test_client()
    for panel_id in (
        "section_coverage",
        "validation",
        "evals",
        "restatements",
        "overrides",
        "credibility",
    ):
        assert client.get(f"/api/panel/{panel_id}").status_code == 200
