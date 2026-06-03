# DCF ⇄ Google Sheets round-trip

Push a ticker's DCF workbook (`dcf/<TICKER>.xlsx`) to a Google Sheet so you can
edit the Forecast INPUTS in the browser, then pull the edited Sheet back to
recompute the fair value into `dcf_runs` (the table briefs read from).

The Sheet is **purely an input editor**. Just like the Excel loop, the recompute
always happens in Python — `import` pulls the Sheet down, places it at
`dcf/<TICKER>.xlsx`, and runs `refresh_dcf.refresh_one` (read edited INPUTS →
recompute Forecast PROJECTED + Valuation → upsert `dcf_runs`). Pushing/pulling
only moves the workbook to/from the browser.

```
 export ─►  Google Sheet  ─► (you edit Forecast INPUTS in the browser) ─►  import
   ▲              │                                                          │
 dcf/<T>.xlsx ────┘                                          dcf/<T>.xlsx ◄──┘  → refresh_dcf → dcf_runs
```

- Code: `src/integrations/gsheets.py` (the Google Drive seam) + `execution/dcf_sheets.py` (CLI).
- Dashboard: the Holding tab's **DCF ⇄ Google Sheets** panel ("Push to Sheets" /
  "Re-ingest from Sheets"), backed by `POST /actions/dcf-export` and
  `POST /actions/dcf-import`.

The credential-free `import --file <path>` mode recomputes from an
already-downloaded `.xlsx` and needs **none** of the setup below.

---

## 1. Install the optional dependency

The Google client libraries are an optional extra (like `[ir]`/playwright), not a
base requirement:

```bash
pip install -e .[gsheets]
```

This pulls `google-api-python-client`, `google-auth`, and `google-auth-oauthlib`.
They are imported lazily, so the rest of the pipeline runs without them.

## 2. Provide credentials

Credentials are **not** in the repo. Drop a credential JSON at
`data/secrets/gsheets_credentials.json` (under the git-ignored `data/` tree), or
point `$DCF_GSHEETS_CREDENTIALS` at it elsewhere. Two shapes are auto-detected:

### Option A — User OAuth (recommended)

The Sheet is created in **your** Google Drive (owned by you, appears natively).
Costs a one-time browser consent.

1. In the [Google Cloud console](https://console.cloud.google.com/): create a
   project, **enable the Google Drive API**, and configure the OAuth consent
   screen (External; add your own Google account as a test user).
2. Create an **OAuth client ID** of type **Desktop app**. Download the JSON
   (it looks like `{"installed": {...}}`) to
   `data/secrets/gsheets_credentials.json`.
3. Authorize once — this opens a browser, and caches the token at
   `data/secrets/gsheets_token.json` (override with `$DCF_GSHEETS_TOKEN`):

   ```bash
   python execution/dcf_sheets.py auth
   ```

   After this, `export`/`import` (and the dashboard buttons, which run as a
   headless server subprocess) work without further prompts; the token
   auto-refreshes.

### Option B — Service account (headless, no consent flow)

The Sheet is owned by the service account, so you must share it to your email to
open it. Best when you never want an interactive step.

1. Create a **service account** in the Cloud console, **enable the Drive API**,
   and download its **JSON key** (`{"type": "service_account", ...}`) to
   `data/secrets/gsheets_credentials.json`. No `auth` step is needed.
2. On `export`, pass the email to share with:

   ```bash
   python execution/dcf_sheets.py export --ticker NU --share-with you@example.com
   ```

   The dashboard's "Push to Sheets" button sends no `share_with`, so for a
   service account run the first `export` from the CLI to create + share the
   Sheet; later re-exports/imports update it in place.

### Scope

Only `https://www.googleapis.com/auth/drive.file` is requested — per-file access
to files this tool creates. It cannot see Sheets you made by hand; always
`export` first so the app owns the file it later imports.

## 3. Use it

```bash
# Push the workbook to a Sheet (creates + links one on first run, updates it after).
python execution/dcf_sheets.py export --ticker NU

# ...edit the Forecast INPUTS in the browser...

# Pull the edited Sheet back and recompute dcf_runs.
python execution/dcf_sheets.py import --ticker NU

# Or recompute from a local .xlsx with no Google call at all:
python execution/dcf_sheets.py import --ticker NU --file dcf/NU.xlsx
```

The per-ticker Sheet id is stored at `dcf_defaults.gsheet_id` in
`micro_thesis/holdings/<TICKER>.json` (written by `export`, read by `import` and
by `GET /api/dcf-sheet/<ticker>`, which the dashboard uses to surface the
"Open in Google Sheets" link). Pass `--sheet-id <id|url>` to override.

## Caveats

- **`export` overwrites the Sheet's contents** with the local workbook — any
  edits made in the browser since the last export are lost. Pull edits back with
  `import` before re-exporting. Conversely, `import` overwrites
  `dcf/<TICKER>.xlsx` with the pulled Sheet.
- **Edit the Forecast INPUTS, not the Valuation sheet.** The Valuation sheet
  holds literal values that the Python refresher rewrites on every `import`; only
  Forecast INPUT edits propagate into the recomputed fair value.
- The xlsx↔Sheets conversion preserves sheet names and cell values (what
  `workbook_reader` reads), so the round-trip is lossless for the DCF math.
