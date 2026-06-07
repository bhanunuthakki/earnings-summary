# Browser-assisted IR fetch (the fallback for names the headless crawler can't crack)

The automated pipeline (`discover_ir_documents_all.py`) uses a **headless** browser.
A handful of issuer IR sites defeat headless automation. This is the on-demand
fallback for those names: a **real browser** (the Claude-in-Chrome bridge, i.e. an
agent driving your actual logged-in Chrome) sources the document URLs, and
`execution/fetch_ir_from_urls.py` bridges them into the same
download → content-classify → register → anchor pipeline. A browser-recovered name
is then first-class downstream (documents table + `ir_narrative` + `--enable-llm`),
identical to an auto-crawled one.

It is **not a cron** — it needs a real browser session, so it's a deliberate,
you're-present action for the few names that need it.

## When a name needs this

The IR Docs dashboard tab shows a name as a gap with a "last crawl" reason. The
headless crawler fails in one of three ways — and only one is recoverable this way:

| Failure mode | What you see | Browser fallback? |
|---|---|---|
| **Reachable + direct doc links** | real browser renders the IR page with `<a href=…pdf>` links (often a Q4 CDN, `s##.q4cdn.com/…/files/…`) | ✅ **Yes** — extract the URLs, run the CLI |
| **Reachable + JS-widget docs** | real browser loads the page, but the docs are delivered by a JS widget with no extractable href (download buttons, viewers) | ⚠️ **Partial** — needs per-site click-through to surface each doc's real URL (intercept the network request), then run the CLI |
| **WAF / "Access denied"** | even your real, logged-in Chrome gets *"You are not authorized to access this page"* / a Cloudflare challenge | ❌ **No** — a server-side block a browser can't route around; this stays a true manual pull |

**Key distinction proved empirically:** the *download* of the file itself almost
always succeeds once you have the URL — issuer file CDNs serve the bytes to the
pipeline's browser-UA downloader even when the IR *page* tarpits automation. The
hard part is *discovering* the URL, which is what the real browser is for.

## Procedure

1. **Drive your real Chrome to the IR page** (Claude-in-Chrome): `navigate` to the
   issuer's IR / quarterly-results / presentations page. If it returns "Access
   denied", it's WAF-walled — stop, it's a manual pull.
2. **Extract the document URLs** with `javascript_tool`:
   ```js
   JSON.stringify([...new Set([...document.querySelectorAll('a')].map(a=>a.href)
     .filter(h=>/\.pdf($|\?)|\.xlsx?($|\?)|q4cdn|mzfilemanager|\/files\//i.test(h)))])
   ```
   If it returns `[]`, the docs are widget-delivered — navigate the sub-pages
   (Financials / Quarterly Results / Presentations) and/or click each quarter and
   read the resolved download URL from the network panel.
3. **Register them** through the pipeline:
   ```
   python execution/fetch_ir_from_urls.py --ticker <T> \
       --url <DOC_URL_1> --url <DOC_URL_2> ...
   # or: --urls-file urls.txt   (one URL per line, # comments ok)
   ```
   This writes the canonical manifest, downloads + content-classifies + registers
   each doc (`documents.source_type='ir_doc'`), refreshes the `ir_narrative` anchor,
   flips `brief_dirty` (so the docs feed the next `--enable-llm` brief), and records
   the outcome in `ir_fetch_status` (so the dashboard reflects the recovery).
   `--no-process` registers only (skips the anchor/brief_dirty).

## Status of the 7 headless-gap names (validated 2026-06-05)

- **NOW** — reachable + direct q4cdn links → **recovered (12 docs)** with this tool.
- **TEM, WGS, FIGR, FRVO, BHP** — reachable in a real browser, but docs are behind
  JS widgets (no extractable hrefs on the landing/financials pages). Recoverable
  only with per-site click-through; not a quick href scrape.
- **LLY** — "Access denied" even in a real logged-in Chrome (WAF/IP block). True
  manual pull; the browser fallback does not help.

Net: the browser fallback cleanly recovers the *direct-href* class, can be pushed
(with effort) through the *widget* class, and cannot touch the *WAF* class. The
weekly + twice-weekly headless crons keep retrying all of them in case a site
changes; this tool is the on-demand assist when you want a specific name now.
