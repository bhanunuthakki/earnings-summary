"""Typed family-editor contracts: field specs, proposals, and the active pointer.

Field tables are hard-coded per model family so request validation stays
owner-true (real domains, real int/float types) instead of generic clamps.
Proposal artifacts are immutable and content-addressed; the active pointer is
derived selection metadata promoted inside the owning persistence
transaction, never an input to any hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator

from dcf.artifact_promotion import ArtifactPromotion, StagedFilePromotion, unique_staged_path
from dcf.input_evidence import FrozenModel, canonical_digest
from dcf.meli_model import validate_credit_terminal
from dcf.persist import DcfRunRevision

NU_FAMILY = "nu_platform_dcf"
MELI_FAMILY = "meli_platform_sotp"

FAMILY_BY_TICKER: dict[str, str] = {"NU": NU_FAMILY, "MELI": MELI_FAMILY}

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FamilyFieldError(ValueError):
    """A typed family-edit rejection carrying its route status code."""

    def __init__(self, reason: str, *, field: str | None = None, http_status: int = 400) -> None:
        self.reason = reason
        self.field = field
        self.http_status = http_status
        super().__init__(f"{reason}:{field}" if field is not None else reason)


class FamilyContractUnavailableError(FamilyFieldError):
    """A required family contract (pointer, proposal, workbook, or source) is unavailable."""

    def __init__(self, reason: str, *, field: str | None = None, http_status: int = 409) -> None:
        super().__init__(reason, field=field, http_status=http_status)


class FamilyPointerInvalidError(FamilyContractUnavailableError):
    """The active pointer file exists but is unreadable; callers fail closed."""


class FamilyProposalConflictError(ValueError):
    """A content-addressed proposal target already holds different bytes."""


class FamilyVectorState(dict[str, float | int]):
    """Typed vector state carrying source attribution ('baseline' | 'active_pointer' | 'committed')."""

    def __init__(
        self,
        values: Mapping[str, float | int] | None = None,
        *,
        source: str = "baseline",
        **kwargs: float | int,
    ) -> None:
        initial = dict(values) if values is not None else kwargs
        super().__init__(initial)
        self.source = source


class FamilyFieldSpec:
    __slots__ = ("editable", "kind", "name", "read_only_reason")

    def __init__(
        self, name: str, kind: Literal["float", "int"], editable: bool, reason: str = ""
    ) -> None:
        self.name = name
        self.kind = kind
        self.editable = editable
        self.read_only_reason = reason


def _float(name: str) -> FamilyFieldSpec:
    return FamilyFieldSpec(name, "float", True)


def _int(name: str) -> FamilyFieldSpec:
    return FamilyFieldSpec(name, "int", True)


def _locked(name: str, reason: str) -> FamilyFieldSpec:
    return FamilyFieldSpec(name, "float", False, reason)


NU_BASE_FACT = "base balance-sheet fact; editable only through reviewed evidence"
NU_MARKET_FACT = "market/marketplace fact; editable only through reviewed evidence"

NU_FIELDS: dict[str, FamilyFieldSpec] = {
    spec.name: spec
    for spec in (
        _locked("cust0", NU_BASE_FACT),
        _locked("arpac0", NU_MARKET_FACT),
        _locked("cb0", NU_BASE_FACT),
        _locked("eq0", NU_BASE_FACT),
        _locked("shares", "share count; editable only through reviewed evidence"),
        _locked("price", "live market price; owned by the price seed, not the editor"),
        _float("custg_near"),
        _float("custg_term"),
        _float("arpacg_near"),
        _float("arpacg_term"),
        _float("activity"),
        _float("gpm_near"),
        _float("gpm_term"),
        _float("opex_near"),
        _float("opex_term"),
        _float("tax"),
        _float("cr_near"),
        _float("cr_term"),
        _float("fl_near"),
        _float("fl_term"),
        _float("cbg_near"),
        _float("cbg_term"),
        _float("cap_ratio"),
        _float("ke"),
        _float("beta"),
        _float("country_risk_premium"),
        _int("derive_ke_capm"),
        _float("g_term"),
        _float("terminal_roe"),
        _float("exit_pe"),
        _int("years"),
    )
}

MELI_REPORTED_FACT = "issuer-reported FY fact; editable only through reviewed evidence"
MELI_REVIEWED_ALLOCATION = "reviewed liquidity/debt allocation; owned by the acceptance artifact"

MELI_FIELDS: dict[str, FamilyFieldSpec] = {
    spec.name: spec
    for spec in (
        _locked("comm_rev0", MELI_REPORTED_FACT),
        _locked("fpay_rev0", MELI_REPORTED_FACT),
        _locked("cb0", MELI_REPORTED_FACT),
        _locked("shares", "share count; editable only through reviewed evidence"),
        _locked("price", "live market price; owned by the price seed, not the editor"),
        _locked("net_cash", MELI_REVIEWED_ALLOCATION),
        _locked("credit_cash_allocation", MELI_REVIEWED_ALLOCATION),
        _locked("operating_cash_reserve", MELI_REVIEWED_ALLOCATION),
        _locked("credit_funding_debt_allocation", MELI_REVIEWED_ALLOCATION),
        _locked("financing_other_expenses_reserve", MELI_REVIEWED_ALLOCATION),
        _float("comm_g_near"),
        _float("comm_g_term"),
        _float("comm_margin_near"),
        _float("comm_margin_term"),
        _float("fpay_g_near"),
        _float("fpay_g_term"),
        _float("fpay_margin_near"),
        _float("fpay_margin_term"),
        _float("da_pct"),
        _float("capex_pct_near"),
        _float("capex_pct_term"),
        _float("nwc_pct"),
        _float("tax"),
        _float("op_exit_ebitda_mult"),
        _float("wacc"),
        _float("cbg_near"),
        _float("cbg_term"),
        _float("nimal_near"),
        _float("nimal_term"),
        _float("credit_opex_ratio"),
        _float("cap_ratio"),
        _float("credit_ke"),
        _float("credit_g_term"),
        _float("credit_terminal_roe"),
        _float("g_term"),
        _float("beta_op"),
        _float("beta_credit"),
        _float("country_risk_premium"),
        _int("derive_capm"),
        _int("years"),
    )
}


def family_for_ticker(ticker: str) -> str:
    """Typed family dispatch; unsupported tickers fail before generic parsing."""
    family = FAMILY_BY_TICKER.get(ticker.upper())
    if family is None:
        raise FamilyFieldError("family_unsupported", field=ticker, http_status=409)
    return family


def _fields(family: str) -> dict[str, FamilyFieldSpec]:
    if family == NU_FAMILY:
        return NU_FIELDS
    if family == MELI_FAMILY:
        return MELI_FIELDS
    raise FamilyFieldError("family_unsupported", field=family, http_status=409)


def normalize_family_vector(family: str, raw: Mapping[str, object]) -> dict[str, float | int]:
    """Project a resolved owner vector onto the family's typed spec table."""
    specs = _fields(family)
    resolved: dict[str, float | int] = {}
    for name, spec in specs.items():
        if name not in raw:
            continue
        value = raw[name]
        if spec.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise FamilyFieldError("invalid_int", field=name)
            resolved[name] = int(value)
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FamilyFieldError("non_finite", field=name)
            number = float(value)
            if not _is_finite(number):
                raise FamilyFieldError("non_finite", field=name)
            resolved[name] = number
    return resolved


def validate_family_edits(family: str, edits: Mapping[str, object]) -> dict[str, float | int]:
    """Validate a partial edit payload against the family spec table.

    Rejects unsupported keys, read-only fields, booleans posed as numbers,
    non-finite floats, and non-int integers, each with a typed reason.
    """
    specs = _fields(family)
    resolved: dict[str, float | int] = {}
    for key, value in edits.items():
        spec = specs.get(key)
        if spec is None:
            raise FamilyFieldError("unsupported_field", field=key)
        if not spec.editable:
            raise FamilyFieldError("read_only_field", field=key)
        if isinstance(value, bool):
            raise FamilyFieldError("bool_as_number_rejected", field=key)
        if spec.kind == "int":
            if not isinstance(value, int):
                raise FamilyFieldError("invalid_int", field=key)
            resolved[key] = value
        else:
            if not isinstance(value, (int, float)):
                raise FamilyFieldError("non_finite", field=key)
            number = float(value)
            if not _is_finite(number):
                raise FamilyFieldError("non_finite", field=key)
            resolved[key] = number
    return resolved


def _is_finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def validate_family_vector(family: str, resolved: Mapping[str, float | int]) -> None:
    """Owner-true domain gates on a complete merged vector (HTTP 422 class)."""
    specs = _fields(family)
    missing = [name for name in specs if name not in resolved]
    if missing:
        raise FamilyFieldError("vector_incomplete", field=missing[0], http_status=422)
    years = resolved["years"]
    if not isinstance(years, int) or years < 1:
        raise FamilyFieldError("invalid_int", field="years", http_status=422)
    if float(resolved["cap_ratio"]) <= 0:
        raise FamilyFieldError("degenerate_capital_inputs", field="cap_ratio", http_status=422)
    if family == NU_FAMILY:
        if not 0 <= float(resolved["tax"]) < 1:
            raise FamilyFieldError("degenerate_terminal_inputs", field="tax", http_status=422)
        ke = float(resolved["ke"])
        g_term = float(resolved["g_term"])
        terminal_roe = float(resolved["terminal_roe"])
        if ke <= g_term or terminal_roe <= g_term:
            raise FamilyFieldError("degenerate_terminal_inputs", field="ke", http_status=422)
        return
    if not 0 <= float(resolved["tax"]) < 1:
        raise FamilyFieldError("degenerate_terminal_inputs", field="tax", http_status=422)
    try:
        validate_credit_terminal({k: float(v) for k, v in resolved.items()})
    except ValueError as exc:
        raise FamilyFieldError(
            "degenerate_terminal_inputs", field="credit_terminal_roe", http_status=422
        ) from exc


class FamilyProposalSource(FrozenModel):
    path: str
    role: str
    sha256: Sha256


class FamilyBaseRevision(FrozenModel):
    row_id: int
    input_sha256: str | None = None
    workbook_sha256: str | None = None
    assumption_snapshot_sha256: str | None = None


class FamilyProposal(FrozenModel):
    """A pending immutable family-edit proposal; content-addressed by payload.

    ``created_at``, ``base``, ``sources``, and ``workbook_sha256`` are excluded
    from the semantic digest: semantic identity is the ticker/family/author/
    rationale/edits/resolved_inputs alone, so a retry or restart of the same
    payload — even after the committed base moved — returns the original
    immutable bytes and metadata.
    """

    schema_version: Literal["dcf_family_proposal.v1"] = "dcf_family_proposal.v1"
    ticker: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z][A-Z0-9.-]*$")
    family: str = Field(min_length=1)
    base: FamilyBaseRevision
    sources: list[dict[str, str]] = Field(default_factory=lambda: list[dict[str, str]]())
    workbook_sha256: str | None = None
    author: str = Field(min_length=1)
    rationale: str = Field(min_length=20)
    created_at: AwareDatetime
    edits: dict[str, float | int]
    resolved_inputs: dict[str, float | int]


def canonical_family_digest(payload: Mapping[str, object]) -> str:
    """Semantic id: canonical digest over everything but creation time."""
    return canonical_digest(dict(payload))


def _proposals_root(state_root: str | os.PathLike[str], ticker: str) -> Path:
    return Path(state_root) / "data" / "dcf_proposals" / ticker.upper()


def proposal_path(state_root: str | os.PathLike[str], ticker: str, proposal_id: str) -> Path:
    return _proposals_root(state_root, ticker) / f"{proposal_id}.json"


def family_proposal_digest(proposal: FamilyProposal) -> str:
    """Semantic id: canonical digest over the semantic identity only."""
    return canonical_family_digest(
        proposal.model_dump(
            mode="json", exclude={"created_at", "base", "sources", "workbook_sha256"}
        )
    )


def validate_proposal_binding(
    proposal: FamilyProposal,
    *,
    expected_workbook_sha256: str | None = None,
    expected_sources: list[dict[str, str]] | None = None,
) -> None:
    """Validate proposal binding integrity.

    Enforces that proposal.sources is non-empty and matches expected_sources
    when expected_sources is provided. Also verifies expected_workbook_sha256
    when provided.
    """
    if not proposal.sources:
        raise FamilyPointerInvalidError(f"family_proposal_sources_empty:{proposal.ticker}")
    if expected_sources is not None:

        def _norm_source(s: dict[str, str]) -> dict[str, str]:
            return {
                "path": str(s.get("path", "")),
                "role": str(s.get("role", "")),
                "sha256": str(s.get("sha256", "")),
            }

        def _sort_key(s: dict[str, str]) -> tuple[str, str]:
            return (s.get("role", ""), s.get("path", ""))

        norm_prop = sorted([_norm_source(item) for item in proposal.sources], key=_sort_key)
        norm_exp = sorted([_norm_source(item) for item in expected_sources], key=_sort_key)
        if norm_prop != norm_exp:
            raise FamilyPointerInvalidError(f"family_proposal_sources_mismatch:{proposal.ticker}")
    if (
        expected_workbook_sha256 is not None
        and proposal.workbook_sha256 is not None
        and proposal.workbook_sha256 != expected_workbook_sha256
    ):
        raise FamilyPointerInvalidError(f"family_proposal_workbook_mismatch:{proposal.ticker}")


def write_family_proposal(
    state_root: str | os.PathLike[str], proposal: FamilyProposal
) -> tuple[Path, str]:
    """Write the proposal once; retries return the original immutable bytes."""
    digest = family_proposal_digest(proposal)
    root = _proposals_root(state_root, proposal.ticker)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{digest}.json"
    if target.exists():
        try:
            existing = FamilyProposal.model_validate_json(target.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise FamilyProposalConflictError(f"family_proposal_conflict:{digest}") from exc
        if family_proposal_digest(existing) != digest:
            raise FamilyProposalConflictError(f"family_proposal_conflict:{digest}")
        return target, digest
    payload = json.dumps(json.loads(proposal.model_dump_json()), indent=2) + "\n"
    staged = target.with_name(f"{target.stem}.writing.{uuid4().hex}{target.suffix}")
    staged.write_bytes(payload.encode("utf-8"))
    os.replace(staged, target)
    return target, digest


def read_family_proposal(
    state_root: str | os.PathLike[str], ticker: str, digest: str
) -> FamilyProposal:
    target = proposal_path(state_root, ticker, digest)
    if not target.exists():
        raise FileNotFoundError(f"family_proposal_missing:{digest}")
    return FamilyProposal.model_validate_json(target.read_text(encoding="utf-8"))


def read_bound_family_proposal(
    state_root: str | os.PathLike[str],
    ticker: str,
    pointer: ActiveFamilyPointer,
    *,
    expected_family: str | None = None,
    expected_run_row_id: int | None = None,
    expected_engine_version: str | None = None,
    expected_input_sha256: str | None = None,
    expected_assumption_snapshot_sha256: str | None = None,
    expected_workbook_sha256: str | None = None,
    expected_sources: list[dict[str, str]] | None = None,
) -> FamilyProposal:
    """Read the proposal an active pointer binds, verifying the FULL binding.

    Validates both pointer fields and proposal bindings against expected current state.
    """
    t = ticker.upper()
    if pointer.ticker.upper() != t:
        raise FamilyPointerInvalidError(f"family_pointer_mismatch:{t}")
    fam = expected_family or pointer.family
    if pointer.family != fam:
        raise FamilyPointerInvalidError(f"family_pointer_mismatch:{t}")
    if expected_run_row_id is not None and pointer.run_row_id != expected_run_row_id:
        raise FamilyPointerInvalidError(f"family_pointer_run_row_mismatch:{t}")
    if expected_engine_version is not None and pointer.engine_version != expected_engine_version:
        raise FamilyPointerInvalidError(f"family_pointer_engine_version_mismatch:{t}")
    if expected_input_sha256 is not None and pointer.input_sha256 != expected_input_sha256:
        raise FamilyPointerInvalidError(f"family_pointer_input_sha256_mismatch:{t}")
    if (
        expected_assumption_snapshot_sha256 is not None
        and pointer.assumption_snapshot_sha256 != expected_assumption_snapshot_sha256
    ):
        raise FamilyPointerInvalidError(f"family_pointer_snapshot_sha256_mismatch:{t}")
    if expected_workbook_sha256 is not None and pointer.workbook_sha256 != expected_workbook_sha256:
        raise FamilyPointerInvalidError(f"family_pointer_workbook_sha256_mismatch:{t}")

    target = proposal_path(state_root, t, pointer.proposal_id)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise FamilyPointerInvalidError(f"family_proposal_unreadable:{t}") from exc
    if hashlib.sha256(raw).hexdigest() != pointer.proposal_sha256:
        raise FamilyPointerInvalidError(f"family_pointer_proposal_bytes_mismatch:{t}")
    try:
        proposal = FamilyProposal.model_validate_json(raw)
    except ValueError as exc:
        raise FamilyPointerInvalidError(f"family_pointer_proposal_invalid:{t}") from exc
    if proposal.ticker.upper() != t or proposal.family != pointer.family:
        raise FamilyPointerInvalidError(f"family_pointer_mismatch:{t}")
    if family_proposal_digest(proposal) != pointer.proposal_id:
        raise FamilyPointerInvalidError(f"family_pointer_proposal_id_mismatch:{t}")

    validate_proposal_binding(
        proposal,
        expected_workbook_sha256=expected_workbook_sha256,
        expected_sources=expected_sources,
    )
    return proposal


class ActiveFamilyPointer(FrozenModel):
    """Derived selection metadata binding the live family row to its proposal."""

    schema_version: Literal["dcf_family_active.v1"] = "dcf_family_active.v1"
    ticker: str
    family: str
    proposal_id: str = Field(min_length=1)
    proposal_sha256: Sha256
    run_row_id: int
    input_sha256: str
    workbook_sha256: str
    engine_version: str
    assumption_snapshot_sha256: str
    updated_at: AwareDatetime

    @property
    def family_profile(self) -> str:
        return self.family

    @model_validator(mode="after")
    def _validate_pointer_digests(self) -> Self:
        for val, field_name in (
            (self.proposal_id, "proposal_id"),
            (self.proposal_sha256, "proposal_sha256"),
            (self.input_sha256, "input_sha256"),
            (self.workbook_sha256, "workbook_sha256"),
            (self.assumption_snapshot_sha256, "assumption_snapshot_sha256"),
        ):
            if not _SHA256_RE.match(val):
                raise ValueError(f"invalid_sha256_digest:{field_name}")
        return self


def active_family_pointer_path(state_root: str | os.PathLike[str], ticker: str) -> Path:
    return _proposals_root(state_root, ticker) / "active.json"


def read_active_family_pointer(
    state_root: str | os.PathLike[str], ticker: str
) -> ActiveFamilyPointer | None:
    """None when absent; a typed error when present-but-invalid (fail closed)."""
    path = active_family_pointer_path(state_root, ticker)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        pointer = ActiveFamilyPointer.model_validate(raw)
    except Exception as exc:
        raise FamilyPointerInvalidError(f"family_pointer_invalid:{ticker.upper()}") from exc

    if pointer.ticker.upper() != ticker.upper() or pointer.family not in FAMILY_BY_TICKER.values():
        raise FamilyPointerInvalidError(f"family_pointer_mismatch:{ticker.upper()}")
    return pointer


def active_pointer_promotion_factory(
    state_root: str | os.PathLike[str],
    ticker: str,
    *,
    family: str,
    proposal_id: str,
    proposal_sha256: str,
) -> Callable[[DcfRunRevision], ArtifactPromotion]:
    """Bind the actual committed receipt into the pointer inside the savepoint.

    The factory runs after insert+ledgers and before artifact promotion apply,
    so the pointer can only ever reference a row that truly committed, and its
    staged file promotion rides the same rollback/finalize contract.
    """
    root = _proposals_root(state_root, ticker)
    active_path = root / "active.json"

    def factory(revision: DcfRunRevision) -> ArtifactPromotion:
        pointer = ActiveFamilyPointer(
            ticker=ticker.upper(),
            family=family,
            proposal_id=proposal_id,
            proposal_sha256=proposal_sha256,
            run_row_id=revision.row_id,
            input_sha256=revision.input_sha256 or ("0" * 64),
            workbook_sha256=revision.workbook_sha256 or ("0" * 64),
            engine_version=revision.engine_version or f"{family}.v1",
            assumption_snapshot_sha256=revision.assumption_snapshot_sha256,
            updated_at=datetime.now(UTC),
        )
        root.mkdir(parents=True, exist_ok=True)
        staged = unique_staged_path(active_path, "pointer")
        staged.write_bytes((pointer.model_dump_json(indent=2) + "\n").encode("utf-8"))
        return StagedFilePromotion(staged, active_path)

    return factory


class MeliPendingHandoff(FrozenModel):
    """The immutable pending state returned when a MELI Save cannot commit."""

    schema_version: Literal["dcf_meli_pending_handoff.v1"] = "dcf_meli_pending_handoff.v1"
    ticker: Literal["MELI"] = "MELI"
    proposal_id: str = Field(min_length=1)
    proposal_sha256: Sha256
    saved: Literal[False] = False
    missing_bindings: tuple[str, ...] = ()
    required_acceptance: tuple[str, ...] = (
        "model_input_receipt_sha256",
        "effective_inputs_sha256",
        "actuals_sha256",
        "snapshot_member_sha256",
        "inventory_snapshot_ids",
        "model_output_sha256",
        "scenarios: bear/base/bull accepted",
    )


def now_utc() -> datetime:
    return datetime.now(UTC)


def require_workbook_authority(
    workbook: Path, live_workbook: Path, artifact_promotion: ArtifactPromotion | None
) -> None:
    """Prove a claimed workbook locator is backed by the bytes being hashed.

    An explicit ``workbook_locator_path`` is a durable authority. It is truthful
    only when the locator will hold the same bytes as ``workbook`` at commit. A
    matching ``artifact_promotion`` (the real staged-to-live swap) is that proof;
    without one, a distinct locator is accepted only when the live file already
    carries identical bytes. Otherwise the claim is rejected before persistence.
    """
    if artifact_promotion is not None:
        staged = getattr(artifact_promotion, "staged_path", None)
        live = getattr(artifact_promotion, "live_path", None)
        if staged is None or live is None:
            raise ValueError(
                "dcf_artifact_promotion_unverified: promotion must expose staged_path and live_path"
            )
        if Path(staged) != Path(workbook) or Path(live) != Path(live_workbook):
            raise ValueError(
                "dcf_artifact_promotion_mismatch: staged/live pair "
                f"{staged!s}->{live!s} does not match workbook/locator "
                f"{workbook!s}->{live_workbook!s}"
            )
        return
    if Path(workbook) == Path(live_workbook):
        return
    try:
        same_bytes = (
            workbook.is_file()
            and live_workbook.is_file()
            and workbook.read_bytes() == live_workbook.read_bytes()
        )
    except OSError:
        same_bytes = False
    if not same_bytes:
        raise ValueError(
            f"dcf_workbook_locator_unverified: locator {live_workbook!s} is not "
            f"backed by a promotion or identical live bytes for {workbook!s}"
        )
