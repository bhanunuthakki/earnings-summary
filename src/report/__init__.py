"""Research-report package.

Builds three artifacts per ticker from the unified data layer:
  - long-form Markdown / PDF research doc  (renderers.markdown / .pdf)
  - frontend section payloads (sections.json) (renderers.sections_json)
  - DCF + supporting workbook (.xlsx)        (renderers.workbook)

Entry point: report.builder.build_report(ticker, repo_root) → ReportSpec.
"""
