"""Typed, declarative manifest for the resumable morning pipeline."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum


class ArgumentProfile(StrEnum):
    """Dynamic argument forwarding applied after a stage's fixed argv."""

    NONE = "none"
    DB_PATH = "db_path"
    DB = "db"
    REPO_ROOT_FROM_DB = "repo_root_from_db"


@dataclass(frozen=True, slots=True)
class StageSpec:
    """Static stage contract; runtime values are expanded by the orchestrator."""

    key: str
    label: str
    script: str
    base_argv: tuple[str, ...]
    timeout_s: int
    selection_dependencies: tuple[str, ...]
    skip_flags: tuple[str, ...] = ()
    argument_profile: ArgumentProfile = ArgumentProfile.NONE


STAGE_PREFLIGHT = "stage_preflight"
STAGE_NEWS = "stage_0_news"
STAGE_LIST_TYPE = "stage_0a_list_type"
STAGE_DECISIONS = "stage_0b_decisions"
STAGE_LIFECYCLE = "stage_0c_lifecycle"
STAGE_DECISION_ACTIONS = "stage_0c2_decision_actions"
STAGE_FUNDAMENTALS = "stage_0d_fundamentals"
STAGE_DERIVED_METRICS = "stage_0d2_derived_metrics"
STAGE_REPRICE = "stage_0e_reprice"
STAGE_CANDIDATE_FIT = "stage_0f_candidate_fit"
STAGE_FACTOR_PROXIES = "stage_0g_factor_proxies"
STAGE_POSITION_GUARD = "stage_0h_position_guard"
STAGE_RISK_SNAPSHOT = "stage_0i_risk_snapshot"
STAGE_WEALTH_CONTEXT = "stage_0j_wealth_context"
STAGE_TRIGGERS = "stage_1_triggers"
STAGE_STANDUP = "stage_1b_standup"
STAGE_PRE_ER_BRIEF = "stage_1c_pre_earnings_briefs"
STAGE_POST_ER_READOUT = "stage_1d_post_earnings_readouts"
STAGE_FEED = "stage_2_feed"
STAGE_VALIDATE = "stage_3_validate"


# Ordering, activation, dependencies, command shape, and timeout live together.
# Selection dependencies control focused ``--only`` expansion only; they never
# short-circuit a normal run because the pipeline's best-effort contract always
# attempts every active stage even after an upstream failure.
STAGE_MANIFEST: tuple[StageSpec, ...] = (
    StageSpec(
        STAGE_PREFLIGHT,
        "Stage preflight - environment check (validate_environment.py)",
        "validate_environment.py",
        (),
        30,
        (),
    ),
    StageSpec(
        STAGE_NEWS,
        "Stage 0 - news fetch (fetch_news.py)",
        "fetch_news.py",
        ("--source", "{news_source}"),
        1200,
        (STAGE_PREFLIGHT,),
        ("skip_triggers", "skip_news"),
        ArgumentProfile.DB_PATH,
    ),
    StageSpec(
        STAGE_LIST_TYPE,
        "Stage 0a - list_type reconcile (sync_list_type_from_holdings.py)",
        "sync_list_type_from_holdings.py",
        ("--apply", "--user-id", "{user_id}"),
        120,
        (STAGE_PREFLIGHT,),
        ("skip_triggers",),
        ArgumentProfile.DB_PATH,
    ),
    StageSpec(
        STAGE_DECISIONS,
        "Stage 0b - decision conditions (record_decisions.py)",
        "record_decisions.py",
        (),
        900,
        (STAGE_PREFLIGHT,),
        ("skip_triggers",),
        ArgumentProfile.DB_PATH,
    ),
    StageSpec(
        STAGE_LIFECYCLE,
        "Stage 0c - position lifecycle (sync_position_lifecycle.py)",
        "sync_position_lifecycle.py",
        ("--user-id", "{user_id}"),
        120,
        (STAGE_LIST_TYPE, STAGE_DECISIONS),
        ("skip_triggers",),
        ArgumentProfile.DB_PATH,
    ),
    StageSpec(
        STAGE_DECISION_ACTIONS,
        "Stage 0c2 - decision actions (reconcile_decision_actions.py)",
        "reconcile_decision_actions.py",
        (),
        120,
        (STAGE_LIFECYCLE,),
        ("skip_triggers",),
        ArgumentProfile.DB_PATH,
    ),
    StageSpec(
        STAGE_FUNDAMENTALS,
        "Stage 0d - cockpit fundamentals (refresh_cockpit_fundamentals.py)",
        "refresh_cockpit_fundamentals.py",
        (),
        60,
        (STAGE_PREFLIGHT,),
        ("skip_triggers",),
        ArgumentProfile.DB_PATH,
    ),
    StageSpec(
        STAGE_DERIVED_METRICS,
        "Stage 0d2 - derived metrics (compute_derived_metrics.py)",
        "compute_derived_metrics.py",
        ("--all",),
        600,
        (STAGE_FUNDAMENTALS,),
        ("skip_triggers",),
        ArgumentProfile.DB,
    ),
    StageSpec(
        STAGE_REPRICE,
        "Stage 0e - DCF re-price (reprice_dcf.py)",
        "reprice_dcf.py",
        (),
        300,
        (STAGE_FUNDAMENTALS,),
        ("skip_triggers",),
        ArgumentProfile.DB_PATH,
    ),
    StageSpec(
        STAGE_CANDIDATE_FIT,
        "Stage 0f - candidate fit (refresh_candidate_fit.py)",
        "refresh_candidate_fit.py",
        (),
        180,
        (STAGE_LIFECYCLE, STAGE_REPRICE),
        ("skip_triggers",),
        ArgumentProfile.DB_PATH,
    ),
    StageSpec(
        STAGE_FACTOR_PROXIES,
        "Stage 0g - factor proxies (fetch_factor_proxies.py)",
        "fetch_factor_proxies.py",
        (),
        300,
        (STAGE_PREFLIGHT,),
        ("skip_triggers",),
        ArgumentProfile.REPO_ROOT_FROM_DB,
    ),
    StageSpec(
        STAGE_POSITION_GUARD,
        "Stage 0h - naked-position gate (refresh_position_guard.py)",
        "refresh_position_guard.py",
        (),
        120,
        (STAGE_LIFECYCLE,),
        ("skip_triggers",),
        ArgumentProfile.DB_PATH,
    ),
    StageSpec(
        STAGE_RISK_SNAPSHOT,
        "Stage 0i - risk snapshot (refresh_portfolio_risk_snapshot.py)",
        "refresh_portfolio_risk_snapshot.py",
        (),
        300,
        (STAGE_FACTOR_PROXIES,),
        ("skip_triggers",),
        ArgumentProfile.DB_PATH,
    ),
    StageSpec(
        STAGE_WEALTH_CONTEXT,
        "Stage 0j - wealth context (refresh_wealth_context_snapshot.py)",
        "refresh_wealth_context_snapshot.py",
        (),
        120,
        (STAGE_PREFLIGHT,),
        ("skip_triggers",),
        ArgumentProfile.DB_PATH,
    ),
    StageSpec(
        STAGE_TRIGGERS,
        "Stage 1 - triggers (run_triggers.py)",
        "run_triggers.py",
        ("--user-id", "{user_id}", "--max-cost-usd", "{max_cost_usd}"),
        1800,
        (
            STAGE_NEWS,
            STAGE_LIFECYCLE,
            STAGE_DERIVED_METRICS,
            STAGE_REPRICE,
            STAGE_CANDIDATE_FIT,
            STAGE_POSITION_GUARD,
            STAGE_RISK_SNAPSHOT,
            STAGE_WEALTH_CONTEXT,
        ),
        ("skip_triggers",),
        ArgumentProfile.DB_PATH,
    ),
    StageSpec(
        STAGE_STANDUP,
        "Stage 1b - proactive standup (run_standup.py)",
        "run_standup.py",
        ("--user-id", "{user_id}"),
        900,
        (STAGE_TRIGGERS,),
        ("skip_triggers", "skip_standup"),
        ArgumentProfile.DB_PATH,
    ),
    StageSpec(
        STAGE_PRE_ER_BRIEF,
        "Stage 1c - pre-earnings briefs (generate_pre_earnings_briefs.py)",
        "generate_pre_earnings_briefs.py",
        (),
        600,
        (STAGE_TRIGGERS,),
        ("skip_triggers", "skip_pre_earnings_briefs"),
        ArgumentProfile.DB_PATH,
    ),
    StageSpec(
        STAGE_POST_ER_READOUT,
        "Stage 1d - post-earnings readouts (generate_post_earnings_readouts.py)",
        "generate_post_earnings_readouts.py",
        (),
        900,
        (STAGE_TRIGGERS,),
        ("skip_triggers", "skip_post_earnings_readouts"),
        ArgumentProfile.DB_PATH,
    ),
    StageSpec(
        STAGE_FEED,
        "Stage 2 - alert feed (build_alert_feed.py)",
        "build_alert_feed.py",
        ("--user-id", "{user_id}"),
        300,
        (STAGE_PREFLIGHT,),
        (),
        ArgumentProfile.DB_PATH,
    ),
    StageSpec(
        STAGE_VALIDATE,
        "Stage 3 - data validation gate (run_validation_engine.py --gate)",
        "run_validation_engine.py",
        ("--gate",),
        600,
        (STAGE_FEED,),
        ("skip_triggers", "skip_validation"),
        ArgumentProfile.DB_PATH,
    ),
)


def validate_manifest(specs: tuple[StageSpec, ...] = STAGE_MANIFEST) -> None:
    """Reject duplicate, missing, cyclic, or forward selection dependencies."""
    keys = [spec.key for spec in specs]
    if len(keys) != len(set(keys)):
        raise ValueError("morning pipeline manifest contains duplicate stage keys")
    positions = {key: index for index, key in enumerate(keys)}
    for spec in specs:
        if spec.timeout_s <= 0:
            raise ValueError(f"{spec.key}: timeout must be positive")
        for dependency in spec.selection_dependencies:
            if dependency not in positions:
                raise ValueError(f"{spec.key}: unknown selection dependency {dependency}")
            if positions[dependency] >= positions[spec.key]:
                raise ValueError(
                    f"{spec.key}: selection dependency {dependency} must precede it"
                )


def manifest_digest(specs: tuple[StageSpec, ...] = STAGE_MANIFEST) -> str:
    """Return a deterministic digest of every dispatch-relevant stage field."""
    payload = [
        {
            "argument_profile": spec.argument_profile.value,
            "base_argv": spec.base_argv,
            "selection_dependencies": spec.selection_dependencies,
            "key": spec.key,
            "label": spec.label,
            "script": spec.script,
            "skip_flags": spec.skip_flags,
            "timeout_s": spec.timeout_s,
        }
        for spec in specs
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


validate_manifest()
