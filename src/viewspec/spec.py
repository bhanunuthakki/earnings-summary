"""ViewSpec — the deterministic, saveable pivot spec (master build P5.1).

The owner's slice-and-dice substrate: metrics x tickers x period x
transform over financial_facts / kpi_facts / the segment junction. A spec
is plain data — JSON in, JSON out — so it can be saved (saved_views,
alembic 0079), embedded in the cockpit/reports, built by the structured
panel UI, or (P5.2) compiled from a natural-language query by a fast
model. Execution is `viewspec.engine.execute_view`: instant, LLM-free,
never raw SQL from a caller.

Serialized shape (the contract the P5.2 compiler must emit)::

    {
      "tickers": ["NU", "MELI"],                 # 1..MAX_TICKERS
      "metrics": [                               # 1..MAX_METRICS
        {"domain": "fin", "key": "revenue"},
        {"domain": "kpi", "key": "ROE"},
        {"domain": "seg", "key": "revenue",
         "dim_type": "product", "dim_name": "AWS"}
      ],
      # each metric may equivalently be its compact token string:
      # "fin:revenue" | "kpi:ROE" | "seg:product:AWS:revenue"
      "transform": "yoy",                        # level|yoy|cagr|margin
      "cadence": "quarterly",                    # quarterly|annual
      "periods": 12,                             # display window, 1..MAX_PERIODS
      "cagr_years": 3                            # lookback for transform="cagr"
    }

`from_dict` validates hard and raises ViewSpecError listing every problem
(the NL-compile path surfaces these to degrade onto the builder UI);
unknown top-level keys are ignored for forward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

DOMAINS: tuple[str, ...] = ("fin", "kpi", "seg")
TRANSFORMS: tuple[str, ...] = ("level", "yoy", "cagr", "margin")
CADENCES: tuple[str, ...] = ("quarterly", "annual")

# Whole-portfolio pivots are the primary flow (the Explore panel prefills
# every portfolio name — 11 today), so the ticker cap leaves headroom over
# that while still bounding a run to tickers x metrics loader queries.
MAX_TICKERS = 16
MAX_METRICS = 10
MAX_PERIODS = 40
MAX_CAGR_YEARS = 10


class ViewSpecError(ValueError):
    """A spec dict failed validation. ``str(exc)`` lists every problem."""


@dataclass(frozen=True, slots=True)
class MetricRef:
    """One metric axis entry.

    ``domain`` picks the substrate: ``fin`` = financial_facts.line_item,
    ``kpi`` = kpi_definitions.name, ``seg`` = a segment slice where ``key``
    is the junction metric (revenue / operating_income / ...) and
    dim_type/dim_name name the slice (product:AWS).
    """

    domain: str
    key: str
    dim_type: str | None = None
    dim_name: str | None = None

    @property
    def label(self) -> str:
        """Human-readable row label fragment."""
        if self.domain == "seg":
            return f"{self.dim_name} {self.key}"
        return self.key

    def token(self) -> str:
        """Compact form for picker option values: ``fin:revenue``,
        ``kpi:ROE``, ``seg:product:AWS:revenue``."""
        if self.domain == "seg":
            return f"seg:{self.dim_type}:{self.dim_name}:{self.key}"
        return f"{self.domain}:{self.key}"

    @classmethod
    def parse_token(cls, token: str) -> MetricRef:
        """Inverse of :meth:`token`. Raises ViewSpecError on a bad token."""
        parts = token.split(":")
        if len(parts) == 2 and parts[0] in ("fin", "kpi") and parts[1]:
            return cls(domain=parts[0], key=parts[1])
        if len(parts) == 4 and parts[0] == "seg" and all(parts[1:]):
            return cls(domain="seg", key=parts[3], dim_type=parts[1], dim_name=parts[2])
        raise ViewSpecError(f"unparseable metric token {token!r}")

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {"domain": self.domain, "key": self.key}
        if self.domain == "seg":
            d["dim_type"] = self.dim_type
            d["dim_name"] = self.dim_name
        return d

    @classmethod
    def from_dict(cls, raw: object) -> MetricRef:
        if isinstance(raw, str):  # token form, as the picker UI submits it
            return cls.parse_token(raw)
        if not isinstance(raw, dict):
            raise ViewSpecError(f"metric entry must be an object, got {type(raw).__name__}")
        m = cast("dict[str, object]", raw)
        domain = str(m.get("domain") or "")
        key = str(m.get("key") or "").strip()
        if domain not in DOMAINS:
            raise ViewSpecError(f"metric domain must be one of {DOMAINS}, got {domain!r}")
        if not key:
            raise ViewSpecError("metric key must be non-empty")
        if domain == "seg":
            dim_type = str(m.get("dim_type") or "").strip()
            dim_name = str(m.get("dim_name") or "").strip()
            if not dim_type or not dim_name:
                raise ViewSpecError("seg metrics need dim_type and dim_name")
            return cls(domain=domain, key=key, dim_type=dim_type, dim_name=dim_name)
        return cls(domain=domain, key=key)


@dataclass(frozen=True, slots=True)
class ViewSpec:
    """The full pivot spec. Frozen — derive a new spec rather than mutate."""

    tickers: tuple[str, ...]
    metrics: tuple[MetricRef, ...]
    transform: str = "level"
    cadence: str = "quarterly"
    periods: int = 12
    cagr_years: int = 3

    def to_dict(self) -> dict[str, object]:
        return {
            "tickers": list(self.tickers),
            "metrics": [m.to_dict() for m in self.metrics],
            "transform": self.transform,
            "cadence": self.cadence,
            "periods": self.periods,
            "cagr_years": self.cagr_years,
        }

    @classmethod
    def from_dict(cls, raw: object) -> ViewSpec:
        """Validate + normalize a spec dict. Raises ViewSpecError listing
        every problem found (not just the first)."""
        if not isinstance(raw, dict):
            raise ViewSpecError(f"spec must be an object, got {type(raw).__name__}")
        spec = cast("dict[str, object]", raw)
        errors: list[str] = []

        tickers_raw = spec.get("tickers")
        tickers: tuple[str, ...] = ()
        if not isinstance(tickers_raw, list) or not tickers_raw:
            errors.append("tickers must be a non-empty list")
        else:
            seen: dict[str, None] = {}
            for t in cast("list[object]", tickers_raw):
                s = str(t).strip().upper()
                if s:
                    seen.setdefault(s, None)
            tickers = tuple(seen)
            if not tickers:
                errors.append("tickers must contain at least one non-empty symbol")
            elif len(tickers) > MAX_TICKERS:
                errors.append(f"at most {MAX_TICKERS} tickers per view, got {len(tickers)}")

        metrics_raw = spec.get("metrics")
        metrics: tuple[MetricRef, ...] = ()
        if not isinstance(metrics_raw, list) or not metrics_raw:
            errors.append("metrics must be a non-empty list")
        else:
            parsed: list[MetricRef] = []
            for entry in cast("list[object]", metrics_raw):
                try:
                    parsed.append(MetricRef.from_dict(entry))
                except ViewSpecError as exc:
                    errors.append(str(exc))
            metrics = tuple(dict.fromkeys(parsed))
            if len(metrics) > MAX_METRICS:
                errors.append(f"at most {MAX_METRICS} metrics per view, got {len(metrics)}")

        transform = str(spec.get("transform") or "level")
        if transform not in TRANSFORMS:
            errors.append(f"transform must be one of {TRANSFORMS}, got {transform!r}")

        cadence = str(spec.get("cadence") or "quarterly")
        if cadence not in CADENCES:
            errors.append(f"cadence must be one of {CADENCES}, got {cadence!r}")

        periods = _coerce_int(spec.get("periods"), default=12)
        if periods is None or not 1 <= periods <= MAX_PERIODS:
            errors.append(f"periods must be an integer in 1..{MAX_PERIODS}")
            periods = 12

        cagr_years = _coerce_int(spec.get("cagr_years"), default=3)
        if cagr_years is None or not 1 <= cagr_years <= MAX_CAGR_YEARS:
            errors.append(f"cagr_years must be an integer in 1..{MAX_CAGR_YEARS}")
            cagr_years = 3

        if errors:
            raise ViewSpecError("; ".join(errors))
        return cls(
            tickers=tickers,
            metrics=metrics,
            transform=transform,
            cadence=cadence,
            periods=periods,
            cagr_years=cagr_years,
        )


def _coerce_int(raw: object, *, default: int) -> int | None:
    """int from a JSON value (int, float, numeric string); ``default`` when
    absent; None when present-but-unusable (which is an error upstream)."""
    if raw is None:
        return default
    if isinstance(raw, bool):  # bool is an int subclass; reject explicitly
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None
