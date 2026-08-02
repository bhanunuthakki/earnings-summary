"""Operator action blocks for the command center (IR-KPI refresh + maintenance).

The ops-status tables that used to live here were superseded by the Research
cockpit (``pipeline.research_cockpit``, master build P1.2); what remains is the
two streamed action blocks, which moved out of Overview and now ship as the
Governance theme's Actions fragment (``GET /api/panel/actions``) — the proper
settings drawer is P3.4. Each block POSTs to its ``/actions/<name>`` endpoint
and streams the job's output back via an EventSource on
``/actions/stream/<job_id>``; both carry their own ``<style>``/``<script>`` so
they work inlined or injected (the shell re-executes scripts on injection).
"""

from __future__ import annotations


def render_actions_panel() -> str:
    """The Governance → Actions fragment: IR-KPI refresh + repo maintenance."""
    return _ACTIONS_BLOCK + _MAINTENANCE_BLOCK


# The "Refresh IR KPIs" control. Rendered into the page via a `{actions_block}`
# placeholder, so it is a `.format()` *argument* — its literal `{`/`}` (CSS rules,
# JS object literals) pass through untouched and need no double-brace escaping.
# In the JS, newlines appended to the log are written `\\n` in this Python source
# so they reach the browser as a JS `\n` escape, not a raw line break (which would
# be a syntax error inside a single-quoted JS string).
_ACTIONS_BLOCK = """
<section class="actions-section" aria-labelledby="actions-h2">
  <h2 id="actions-h2">Refresh IR KPIs</h2>
  <p class="actions-help">
    Pull the issuer's official historical-data spreadsheet (a headless browser
    discovers the current URL), parse it, and ingest KPI facts at the IR-doc
    tier &mdash; superseding LLM-brief values. The ticker needs a parser config
    in <code>micro_thesis/ir_config/</code> (e.g. NU).
  </p>
  <form id="refresh-ir-form" class="actions-form" autocomplete="off">
    <input type="text" id="ir-ticker" name="ticker" class="ir-ticker"
           placeholder="Ticker (e.g. NU)" required aria-label="Ticker">
    <label class="ir-quarters-label">quarters
      <input type="number" id="ir-quarters" name="quarters" class="ir-quarters"
             value="8" min="1" max="40">
    </label>
    <button type="submit" id="ir-submit" class="k-btn k-btn-primary">Refresh IR KPIs</button>
    <span id="ir-status" class="actions-status" role="status" aria-live="polite"></span>
  </form>
  <pre id="ir-output" class="actions-output" hidden></pre>
</section>
<style>
.actions-section { margin: 0 0 var(--sp-4); padding: 14px 16px; background: var(--surface);
  border: 1px solid var(--border); border-radius: var(--radius); }
.actions-section h2 { margin: 0 0 6px; }
.actions-help { font-size: var(--fs-caption); color: var(--muted); margin: 0 0 12px;
  max-width: 760px; }
.actions-help code { font-family: var(--mono); font-size: 0.93em; }
.actions-form { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
/* Inputs: skinned by the shared control kit (ui/controls.py). input.ir-ticker
   outranks the kit's input[type] baseline so the mono face survives. */
input.ir-ticker { width: 170px; text-transform: uppercase; font-family: var(--mono); }
.ir-quarters-label { font-size: var(--fs-caption); color: var(--muted); display: inline-flex;
  align-items: center; gap: 6px; }
.ir-quarters { width: 60px; }
/* #ir-submit (primary) + the maintenance buttons (quiet) ride the shared kit
   (.k-btn, controls.py); the shell composes controls_css for this fragment. */
.actions-status { font-size: var(--fs-caption); font-weight: 500; }
.actions-status.running { color: var(--warn); }
.actions-status.ok { color: var(--ok); }
.actions-status.error { color: var(--bad); }
.actions-output { margin: 12px 0 0; padding: 10px 12px; max-height: 320px; overflow-y: auto;
  background: var(--bg); color: var(--fg-soft); border-radius: var(--radius);
  font-family: var(--mono);
  font-size: var(--fs-caption); line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
</style>
<script>
(function () {
  var form = document.getElementById('refresh-ir-form');
  if (!form) return;
  var tickerEl = document.getElementById('ir-ticker');
  var quartersEl = document.getElementById('ir-quarters');
  var submitEl = document.getElementById('ir-submit');
  var statusEl = document.getElementById('ir-status');
  var outputEl = document.getElementById('ir-output');
  var es = null;
  var finished = false;
  var t0 = 0;

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = 'actions-status' + (cls ? ' ' + cls : '');
  }
  function elapsed() {
    var s = Math.floor((Date.now() - t0) / 1000);
    return '[' + Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2) + '] ';
  }
  function appendLine(line) {
    outputEl.hidden = false;
    outputEl.textContent += elapsed() + line + '\\n';
    outputEl.scrollTop = outputEl.scrollHeight;
  }
  function closeStream() {
    if (es) { es.close(); es = null; }
  }

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var ticker = (tickerEl.value || '').trim().toUpperCase();
    if (!ticker) { setStatus('Enter a ticker.', 'error'); return; }
    var quarters = parseInt(quartersEl.value, 10);
    if (!(quarters > 0)) quarters = 8;
    closeStream();
    finished = false;
    t0 = Date.now();
    outputEl.textContent = '';
    outputEl.hidden = true;
    submitEl.disabled = true;
    setStatus('Starting...', 'running');

    fetch('/actions/refresh-ir', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: ticker, quarters: quarters })
    }).then(function (resp) {
      return resp.json().then(function (body) {
        return { ok: resp.ok, status: resp.status, body: body };
      });
    }).then(function (r) {
      if (!r.ok) {
        var msg = (r.body && r.body.error) ? r.body.error : ('HTTP ' + r.status);
        setStatus('Error: ' + msg, 'error');
        submitEl.disabled = false;
        return;
      }
      setStatus('Running ' + r.body.ticker + ' (' + r.body.kind + ')...', 'running');
      es = new EventSource(r.body.stream_url);
      es.onmessage = function (e) {
        var m;
        try { m = JSON.parse(e.data); } catch (_) { return; }
        if (m.event === 'start') {
          appendLine('> job ' + m.job_id + ' started for ' + m.ticker + ' (' + m.kind + ')');
        } else if (m.event === 'log') {
          appendLine(m.line);
        } else if (m.event === 'done') {
          finished = true;
          appendLine('# exit code ' + m.exit_code);
          var took = Math.round((Date.now() - t0) / 1000);
          if (m.exit_code === 0) {
            setStatus('Done in ' + took + 's. KPIs ingested at IR-doc tier.', 'ok');
          } else {
            setStatus('Failed (exit ' + m.exit_code + ') after ' + took + 's. See log above.', 'error');
          }
          submitEl.disabled = false;
          closeStream();
        }
      };
      es.onerror = function () {
        if (finished) return;
        setStatus('Stream interrupted - is the server still running?', 'error');
        submitEl.disabled = false;
        closeStream();
      };
    }).catch(function (err) {
      setStatus('Request failed: ' + err.message, 'error');
      submitEl.disabled = false;
    });
  });
})();
</script>
""".strip()


# Repo-wide maintenance controls. Like _ACTIONS_BLOCK this is a `.format()`
# argument, so its literal {/} (JS object literals, CSS) pass through untouched.
# Reuses the .actions-section / .actions-output / .actions-status styles above.
_MAINTENANCE_BLOCK = """
<section class="actions-section" aria-labelledby="maint-h2">
  <h2 id="maint-h2">Maintenance</h2>
  <p class="actions-help">
    Repo-wide chores, streamed live — the same CLIs the crons run.
  </p>
  <div class="actions-form">
    <button type="button" class="maint-btn k-btn k-btn-quiet" data-action="seed_kpis"
      title="Re-seed kpi_definitions from the per-ticker holdings JSONs. Idempotent - safe to re-run.">Seed KPI defs</button>
    <button type="button" class="maint-btn k-btn k-btn-quiet" data-action="process_inbox"
      title="Register documents dropped into the inbox folder (categorize + attach to their tickers).">Process dropped docs</button>
    <button type="button" class="maint-btn k-btn k-btn-quiet" data-action="sweep_history"
      title="Archive superseded report builds out of output/ - keeps the newest per ticker.">Sweep output history</button>
    <button type="button" class="maint-btn k-btn k-btn-quiet" data-action="onboard_pending"
      title="Run the full onboarding pipeline for every ticker queued as pending.">Onboard pending</button>
    <span class="maint-sep">|</span>
    <input type="text" id="maint-onboard-ticker" class="ir-ticker" placeholder="Ticker" aria-label="Ticker to onboard">
    <button type="button" class="maint-btn k-btn k-btn-quiet" data-action="onboard" data-needs-ticker="1"
      title="FMP onboard + first report build for ONE new ticker. Takes minutes and spends FMP quota.">Onboard ticker</button>
    <span id="maint-status" class="actions-status" role="status" aria-live="polite"></span>
  </div>
  <pre id="maint-output" class="actions-output" hidden></pre>
  <p class="actions-help maint-export">
    <a class="k-btn k-btn-quiet k-btn-sm" href="/export/cio">Export CIO workbook (.xlsx)</a>
    &mdash; decisions + triggers + thesis ledger, one file. Previously reachable only from the
    command palette.
  </p>
</section>
<style>
.maint-sep { color: var(--muted); margin: 0 4px; }
.maint-export { margin: 12px 0 0; }
</style>
<script>
(function () {
  var btns = document.querySelectorAll('.maint-btn');
  if (!btns.length) return;
  var statusEl = document.getElementById('maint-status');
  var outputEl = document.getElementById('maint-output');
  var tickerEl = document.getElementById('maint-onboard-ticker');
  var es = null, finished = false, t0 = 0;
  function elapsed() {
    var s = Math.floor((Date.now() - t0) / 1000);
    return '[' + Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2) + '] ';
  }
  function setStatus(t, c) { statusEl.textContent = t; statusEl.className = 'actions-status' + (c ? ' ' + c : ''); }
  function appendLine(l) { outputEl.hidden = false; outputEl.textContent += elapsed() + l + '\\n'; outputEl.scrollTop = outputEl.scrollHeight; }
  function enable() { btns.forEach(function (b) { b.disabled = false; }); }
  function run(action, ticker) {
    if (es) { es.close(); es = null; }
    finished = false; outputEl.textContent = ''; outputEl.hidden = true; t0 = Date.now();
    btns.forEach(function (b) { b.disabled = true; });
    setStatus('Starting ' + action + '...', 'running');
    var payload = { action: action };
    if (ticker) payload.ticker = ticker;
    fetch('/actions/maintenance', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    }).then(function (resp) {
      return resp.json().then(function (body) { return { ok: resp.ok, status: resp.status, body: body }; });
    }).then(function (r) {
      if (!r.ok) { setStatus('Error: ' + ((r.body && r.body.error) || ('HTTP ' + r.status)), 'error'); enable(); return; }
      setStatus('Running ' + r.body.kind + '...', 'running');
      es = new EventSource(r.body.stream_url);
      es.onmessage = function (e) {
        var m; try { m = JSON.parse(e.data); } catch (_) { return; }
        if (m.event === 'start') { appendLine('> ' + m.kind + ' started (job ' + m.job_id + ')'); }
        else if (m.event === 'log') { appendLine(m.line); }
        else if (m.event === 'done') {
          finished = true; appendLine('# exit code ' + m.exit_code);
          var took = Math.round((Date.now() - t0) / 1000);
          setStatus(m.exit_code === 0 ? ('Done in ' + took + 's.') : ('Failed (exit ' + m.exit_code + ') after ' + took + 's.'), m.exit_code === 0 ? 'ok' : 'error');
          enable(); if (es) { es.close(); es = null; }
        }
      };
      es.onerror = function () { if (finished) return; setStatus('Stream interrupted.', 'error'); enable(); if (es) { es.close(); es = null; } };
    }).catch(function (err) { setStatus('Request failed: ' + err.message, 'error'); enable(); });
  }
  btns.forEach(function (b) {
    b.addEventListener('click', function () {
      var needsTicker = b.getAttribute('data-needs-ticker');
      var ticker = needsTicker ? (tickerEl.value || '').trim().toUpperCase() : '';
      if (needsTicker && !ticker) { setStatus('Enter a ticker to onboard.', 'error'); return; }
      run(b.getAttribute('data-action'), ticker);
    });
  });
})();
</script>
""".strip()
