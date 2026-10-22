"""Regression: the evaluation-roster script writes only readable enum values.

`tracked_companies_for_user` parses `instrument_type` with `InstrumentType`, so
one invalid value (ONON was written as ``foreign_private_issuer``) crashes every
reader of the roster, including `fetch_sec_xbrl.py`.
"""

from __future__ import annotations

from execution.track_evaluation_names import EVALUATION_NAMES
from models.companies import FilingRegime, InstrumentType


def test_every_roster_row_uses_enum_values() -> None:
    for _ticker, _name, _list_type, inst_type, filing_regime, _sector in EVALUATION_NAMES:
        assert InstrumentType(inst_type) is inst_type
        assert FilingRegime(filing_regime) is filing_regime


def test_onon_is_an_ordinary_share_listing_under_the_20f_regime() -> None:
    rows = {row[0]: row for row in EVALUATION_NAMES}
    assert rows["ONON"][3] is InstrumentType.EQUITY
    assert rows["ONON"][4] is FilingRegime.FORM_20F
