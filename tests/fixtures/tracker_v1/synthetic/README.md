# Portfolio Data Service `/api/v1` fixtures — synthetic fallback (currently empty)

**Status as of 2026-07-24: empty.** The provider (`portfolio-tracker`) closed
the fixture gap in PR #52 ("feat(api): complete the v1 consumer fixture
suite", merged to `main` @ `451141c`) — every v1 endpoint now has an official
fixture vendored one directory up at `tests/fixtures/tracker_v1/`. The seven
files that used to live here (`positions.json`, `position-snapshots.json`,
`data-quality.json`, `analytics-performance.json`,
`analytics-position-performance.json`, `analytics-risk.json`,
`analytics-exit-quality.json`) were deleted once their official
provider-generated equivalents were vendored.

**This directory stays as the fallback location.** If a future v1 endpoint
ships with no official fixture yet, or a provider fetch fails when one is
next needed, a consumer-hand-derived payload goes here — same rules as
before:

- Derived directly from `docs/api/openapi.v1.json` in the provider repo.
- Every field synthetic, chosen only to be schema-valid (required fields
  present, decimal-string money/quantity, ISO dates, correct envelope shape).
- A stopgap: once the provider ships a real fixture for that endpoint,
  replace the file here with the vendored official one and delete it from
  this directory (matching what just happened for these seven).
- Never hand-edit an existing synthetic fixture in place — re-derive it (or,
  preferably, adopt the provider's own fixture once it exists).
