# explore-sandbox

The Streamlit analytics deep-dive sandbox: a read-only, deterministic
catalog → ViewSpec surface beside the Flask cockpit.

## What it is

- Interactive composition of a `ViewSpec` (metrics × tickers × cadence ×
  periods × transform) over the SAME engine the cockpit and reports use
  (`viewspec.engine.execute_view`) — every number comes from the shared
  provenance-aware resolver.
- Token parity with the cockpit by construction: the theme
  (`.streamlit/config.toml` + `theme/tokens.css`) is generated from
  `src/ui/tokens.py`, the control layer (`theme/controls.css`) from
  `src/ui/controls.py`, and `sandbox_kit` re-exports the control functions
  themselves. `scripts/gen_streamlit_theme.py` regenerates all three;
  the design-sync gate and `tests/test_explore_sandbox_kit.py` fail on
  drift.

## What it is not

- No LLM anywhere, no writes, no production authority.
- The database is always explicit: the synthetic benchmark clone
  (`.tmp/vs_opt.db`) or a restored snapshot path you name. A checkout-local
  `data/portfolio.db` is refused outright.

## Run it

```sh
pip install -e ".[sandbox]"
streamlit run explore-sandbox/analytics_deep_dive.py
```

Nothing in `src/` or `execution/` imports streamlit; this directory and its
optional dependency group are the only consumers, and `sandbox_kit.py`
itself stays streamlit-free so the drift test runs in every checkout.
