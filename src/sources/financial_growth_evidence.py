"""Bounded selection references for reported-financial growth calculations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sources.report_financials import FinancialEvidenceReference

GrowthFormula = Literal[
    "qoq",
    "yoy",
    "cagr_1y_ttm",
    "cagr_3y_ttm",
    "cagr_1y_level",
    "cagr_2y_level",
    "cagr_3y_level",
]
GROWTH_WINDOWS: dict[GrowthFormula, int] = {
    "qoq": 2,
    "yoy": 5,
    "cagr_1y_ttm": 8,
    "cagr_3y_ttm": 16,
    "cagr_1y_level": 5,
    "cagr_2y_level": 9,
    "cagr_3y_level": 13,
}
GROWTH_FORMULAS: dict[GrowthFormula, str] = {
    "qoq": "Latest quarter / prior quarter - 1",
    "yoy": "Quarter / same quarter a year earlier - 1",
    "cagr_1y_ttm": "Latest four-quarter total / prior four-quarter total - 1",
    "cagr_3y_ttm": "(Latest four-quarter total / four-quarter total three years earlier)^(1/3) - 1",
    "cagr_1y_level": "Latest quarter / quarter one year earlier - 1",
    "cagr_2y_level": "(Latest quarter / quarter two years earlier)^(1/2) - 1",
    "cagr_3y_level": "(Latest quarter / quarter three years earlier)^(1/3) - 1",
}


class FinancialGrowthReference(BaseModel):
    """All selected inputs, including witnesses for comparison continuity."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    calculation_version: Literal["canonical-growth/v1"] = "canonical-growth/v1"
    formula: GrowthFormula
    inputs: tuple[FinancialEvidenceReference, ...] = Field(min_length=2, max_length=16)

    @model_validator(mode="after")
    def _one_report_selection(self) -> FinancialGrowthReference:
        if len(self.inputs) != GROWTH_WINDOWS[self.formula]:
            raise ValueError("incomplete growth calculation window")
        first = self.inputs[0]
        if any(
            item.reader_kind != "report_table"
            or (item.ticker, item.concept, item.as_of) != (first.ticker, first.concept, first.as_of)
            for item in self.inputs
        ):
            raise ValueError("growth inputs require one report concept and cutoff")
        if len({item.observation_id for item in self.inputs}) != len(self.inputs):
            raise ValueError("growth inputs must have distinct observations")
        return self
