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
    DB_PATH_AND_REPO_ROOT = "db_path_and_repo_root"
    DB = "db"
    DB_AND_STATE_ROOT = "db_and_state_root"
    REPO_ROOT_FROM_DB = "repo_root_from_db"


@dataclass(frozen=True, slots=True)
class StageSpec:
    """Static stage contract; runtime values are expanded by the orchestrator.

    ``db_writer`` declares whether the stage's subprocess can hold a write
    transaction on the pipeline's SQLite database file. The lane scheduler
    never runs two ``db_writer`` stages concurrently (single-writer discipline);
    read/compute stages — verified to touch the DB through read-only
    connections only, persisting to file caches instead — may overlap a writer.

    ``lane_dependencies`` adds full-run scheduling dependencies beyond
    ``selection_dependencies``. They gate lane dispatch in a normal run but are
    deliberately excluded from ``--only``/focused-selection closure expansion:
    a stage that is safely runnable standalone (e.g. the feed render for a
    re-render-only pass) may still have to follow an earlier stage when the
    whole pipeline runs (the feed renders the day's alerts, so a full run must
    not dispatch it before the trigger sweep settles).
    """

    key: str
    label: str
    script: str
    base_argv: tuple[str, ...]
    timeout_s: int
    selection_dependencies: tuple[str, ...]
    skip_flags: tuple[str, ...] = ()
    argument_profile: ArgumentProfile = ArgumentProfile.NONE
    db_writer: bool = True
    lane_dependencies: tuple[str, ...] = ()


STAGE_PREFLIGHT = "stage_preflight"
STAGE_NEWS = "stage_0_news"
STAGE_LIST_TYPE = "stage_0a_list_type"
STAGE_DECISIONS = "stage_0b_decisions"
STAGE_LIFECYCLE = "stage_0c_lifecycle"
STAGE_DECISION_ACTIONS = "stage_0c2_decision_actions"
STAGE_FUNDAMENTALS = "stage_0d_fundamentals"
STAGE_DOCUMENT_EVIDENCE = "stage_0d1_document_evidence"
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
# Dependencies control two distinct things. Selection dependencies (plus the
# optional lane supplements) drive focused ``--only`` expansion and full-run
# lane scheduling respectively; they never short-circuit a normal run because
# the pipeline's best-effort contract always attempts every active stage even
# after an upstream failure — a settled failure stops delaying a dependent,
# it never cancels it.
STAGE_MANIFEST: tuple[StageSpec, ...] = (
    StageSpec(
        STAGE_PREFLIGHT,
        "Stage preflight - environment check (validate_environment.py)",
        "validate_environment.py",
        (),
        30,
        (),
        (),
        ArgumentProfile.NONE,
        # Environment checks only: reads config/paths, never the pipeline DB.
        False,
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
        ArgumentProfile.DB_PATH_AND_REPO_ROOT,
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
        # Read-only DB pass materializing data/cockpit_fundamentals.json and
        # the cockpit file-input caches; never opens a writer connection.
        False,
    ),
    StageSpec(
        STAGE_DOCUMENT_EVIDENCE,
        "Stage 0d1 - deterministic source evidence (process_document_evidence.py)",
        "process_document_evidence.py",
        ("--apply", "--resume", "--batch-size", "100"),
        600,
        (STAGE_PREFLIGHT, STAGE_LIST_TYPE),
        ("skip_triggers",),
        ArgumentProfile.DB_AND_STATE_ROOT,
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
        # Read-only DB pass materializing data/candidate_fit.json (plus the
        # ETF score cache); never opens a writer connection.
        False,
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
        # No DB at all: fetches ETF price series into data/factor_proxies/.
        False,
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
        # Read-only DB pass materializing the position-guard cache file.
        False,
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
            STAGE_DOCUMENT_EVIDENCE,
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
        # Read-only render of the persisted alert/inbox stream into
        # data/dashboard/feed.html; never opens a writer connection.
        False,
        # Selection-wise the feed is standalone (a re-render-only pass runs it
        # alone), but in a FULL run it renders the day's alerts, decisions, and
        # queued-action drafts — all of which are transitive inputs of the
        # trigger sweep — so lanes must not dispatch it before stage 1 settles.
        (STAGE_TRIGGERS,),
    ),
    StageSpec(
        STAGE_VALIDATE,
        "Stage 3 - data validation gate (run_validation_engine.py --gate)",
        "run_validation_engine.py",
        ("--gate", "--user-id", "{user_id}"),
        600,
        (STAGE_FEED,),
        ("skip_triggers", "skip_validation"),
        ArgumentProfile.DB_PATH,
    ),
)


def validate_manifest(specs: tuple[StageSpec, ...] = STAGE_MANIFEST) -> None:
    """Reject duplicate, missing, cyclic, or forward dependencies."""

    keys = [spec.key for spec in specs]
    if len(keys) != len(set(keys)):
        raise ValueError("morning pipeline manifest contains duplicate stage keys")
    positions = {key: index for index, key in enumerate(keys)}
    for spec in specs:
        if spec.timeout_s <= 0:
            raise ValueError(f"{spec.key}: timeout must be positive")
        for field_name, dependencies in (
            ("selection_dependencies", spec.selection_dependencies),
            ("lane_dependencies", spec.lane_dependencies),
        ):
            for dependency in dependencies:
                if dependency not in positions:
                    raise ValueError(f"{spec.key}: unknown {field_name} dependency {dependency}")
                if positions[dependency] >= positions[spec.key]:
                    raise ValueError(
                        f"{spec.key}: {field_name} dependency {dependency} must precede it"
                    )


def selection_closure(target: str, specs: tuple[StageSpec, ...] = STAGE_MANIFEST) -> frozenset[str]:
    """Return ``target`` plus every transitive selection dependency.

    Deliberately excludes ``lane_dependencies``: focused selection answers
    "what is safe to run standalone", which the lane supplements never gate.
    """

    by_key = {spec.key: spec for spec in specs}
    if target not in by_key:
        raise ValueError(f"unknown stage key: {target}")
    selected: set[str] = set()

    def add(key: str) -> None:
        if key in selected:
            return
        selected.add(key)
        for dependency in by_key[key].selection_dependencies:
            add(dependency)

    add(target)
    return frozenset(selected)


def lane_dependencies(spec: StageSpec) -> tuple[str, ...]:
    """The full-run scheduling dependency set: selection deps plus lane deps.

    Callers intersect this with the actually-selected stage keys so a skipped
    or unselected dependency never deadlocks its dependent (the pipeline's
    always-attempt contract: a missing upstream degrades, it never blocks).
    """

    return (*spec.selection_dependencies, *spec.lane_dependencies)


def manifest_digest(specs: tuple[StageSpec, ...] = STAGE_MANIFEST) -> str:
    """Return a deterministic digest of every dispatch-relevant stage field."""
    payload = [
        {
            "argument_profile": spec.argument_profile.value,
            "base_argv": spec.base_argv,
            "db_writer": spec.db_writer,
            "lane_dependencies": spec.lane_dependencies,
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
