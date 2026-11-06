"""Why the tracker cannot state a return, in the owner's words.

A blocked benchmark calculation can carry many reason codes. The notice merges
them into a few facts. The exact reasons stay available as tracker detail.
"""

from __future__ import annotations

from collections.abc import Iterable
from html import escape

from integrations.portfolio_tracker_client import PerformanceSeries
from ui.controls import k_empty

_PERFORMANCE_REASON_LABELS = {
    "no_portfolio_values": "no portfolio values",
    "external_share_movement_missing_security": "external share movement missing security identity",
    "external_share_movement_missing_ticker": "external share movement missing ticker identity",
    "external_share_movement_price_unavailable": "external share-movement price unavailable",
    "nonpositive_dietz_denominator": "nonpositive Modified Dietz denominator",
    "portfolio_start_value_unavailable": "requested start-date valuation unavailable",
    "portfolio_end_value_unavailable": "requested end-date valuation unavailable",
    "partial_snapshot_start_date": "start-date account snapshot is incomplete",
    "partial_snapshot_end_date": "end-date account snapshot is incomplete",
    "partial_account_valuation_start_date": "start-date account valuation coverage is incomplete",
    "partial_account_valuation_end_date": "end-date account valuation coverage is incomplete",
    "account_valuation_integrity_invalid": "account valuation evidence failed integrity checks",
    "modeled_opening_account_coverage_incomplete": "modeled opening account coverage incomplete",
    "modeled_opening_valuation_coverage_incomplete": "modeled opening valuation incomplete",
    "portfolio_account_universe_coverage_incomplete": "portfolio account-universe valuation coverage incomplete",
    "account_valuation_index_exclusion_unsupported": "account valuation cannot support the requested index-fund exclusion",
    "provider_valuation_as_of_unasserted": "provider valuation date is not asserted",
    "performance_window_too_short": "performance window is too short",
    "spy_benchmark_price_unavailable": "SPY benchmark history incomplete",
    "qqq_benchmark_price_unavailable": "QQQ benchmark history incomplete",
    "policy_benchmark_price_unavailable": "policy benchmark history incomplete",
    "external_flow_source_coverage_incomplete": "broker-statement cash-flow coverage incomplete",
    "broker_archive_coverage_not_complete": "broker archive coverage incomplete",
    "external_flow_provenance_current_decision_missing": "cash-flow provenance decision missing",
    "external_flow_provenance_current_decision_conflict": "cash-flow provenance decisions conflict",
    "external_flow_provenance_current_decision_unapproved": "cash-flow provenance decision unapproved",
    "external_flow_provenance_current_decision_unresolved": "cash-flow provenance decision unresolved",
    "external_flow_provenance_current_decision_provisional": "cash-flow provenance decision provisional",
    "external_flow_provenance_target_transaction_missing": "cash-flow provenance target transaction missing",
    "external_flow_provenance_target_transaction_account_mismatch": "cash-flow provenance target account mismatch",
    "external_flow_provenance_target_transaction_economics_mismatch": "cash-flow provenance target economics mismatch",
    "external_flow_provenance_transaction_target_conflict": "cash-flow provenance transaction target conflict",
    "external_flow_provenance_decision_payload_digest_mismatch": "cash-flow provenance decision integrity mismatch",
    "external_flow_provenance_source_event_set_digest_mismatch": "cash-flow provenance source-event integrity mismatch",
    "external_flow_provenance_effective_date_basis_mismatch": "cash-flow provenance effective-date basis mismatch",
    "unpriceable_holding_snapshot": "one or more held positions could not be valued",
    "option_history_reconstruction_unsupported": "option position history cannot be reconstructed",
}


_PERFORMANCE_REASON_GROUPS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("external_", "share_movement", "trade_"),
        "Deposits and withdrawals are not confirmed for every account, so a gain cannot be "
        "separated from money moved in or out.",
    ),
    (("option_",), "Option positions cannot be rebuilt for past dates."),
    (("benchmark", "spy_", "qqq_", "policy_"), "Index price history is incomplete."),
)
_PERFORMANCE_VALUATION_BLOCKER = "Some accounts have no verified value on the start or end date."


def _performance_blockers(codes: Iterable[str]) -> list[str]:
    """Merge tracker reason codes into the few facts the owner can act on."""
    blockers: list[str] = []
    for code in codes:
        blocker = next(
            (
                text
                for tokens, text in _PERFORMANCE_REASON_GROUPS
                if any(token in code for token in tokens)
            ),
            _PERFORMANCE_VALUATION_BLOCKER,
        )
        if blocker not in blockers:
            blockers.append(blocker)
    return blockers


def performance_reason_list(perf: PerformanceSeries) -> str:
    reasons = "".join(
        f"<li>{escape(_PERFORMANCE_REASON_LABELS.get(code, code.replace('_', ' ')))}</li>"
        for code in perf.calculation_reason_codes
    )
    return f'<ul class="pf-perf-blockers">{reasons}</ul>' if reasons else ""


def performance_calculation_notice(perf: PerformanceSeries) -> str:
    """Say what is missing in plain words."""
    if perf.calculation_status == "available":
        return ""
    headline = k_empty("Your return against the benchmarks cannot be calculated yet.")
    if perf.compatibility_issue is not None:
        return headline + (
            '<p class="muted">The provider response did not satisfy the supported calculation '
            "contract.</p>"
        )
    codes = perf.calculation_reason_codes
    if not codes:
        return headline + '<p class="muted">The tracker could not validate the calculation.</p>'
    causes = "".join(f"<li>{escape(text)}</li>" for text in _performance_blockers(codes))
    return f'{headline}<ul class="pf-perf-blockers">{causes}</ul>'
