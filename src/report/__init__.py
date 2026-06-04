"""Research-report package.

Builds the per-ticker artifacts from the unified data layer:
  - long-form Markdown / PDF research doc  (renderers.markdown / .pdf)
  - HTML / workspace brief                  (renderers.html / .workspace_html)
  - frontend section payloads (sections.json) (renderers.sections_json)

The DCF model is the redesigned workbook at `dcf/<TICKER>.xlsx` (built by
`execution/build_redesigned_dcf.py`, refreshed by `execution/refresh_dcf.py`);
the brief references it rather than rendering its own copy.

Entry point: report.builder.build_report(ticker, repo_root) → ReportSpec.
"""
