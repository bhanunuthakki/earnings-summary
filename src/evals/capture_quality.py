"""Replay-based quality audits over opt-in production LLM captures.

The live call path remains untouched. This module reads already-produced
prompt/response pairs from the private capture archive and asks the governed
eval judge to score a bounded, newest-first sample against a versioned,
purpose-specific quality bar. Missing production captures fail loudly at the
runner boundary instead of becoming a synthetic passing case.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from evals.capture_quality_specs import CAPTURE_QUALITY_SPECS, CaptureQualitySpec
from evals.corpora import MAX_CONTENT_CHARS, AuditItem
from evals.harness import EvalAbortError, EvalRunSummary, now_naive_utc, resolve_git_sha
from evals.judge import JUDGE_PURPOSE
from evals.rubric_judge import Rubric, judge_item
from llm.capture import capture_purpose_suffix, default_capture_archive_dir
from llm.cli import DEFAULT_MODEL, LLM_MODELS, call_llm
from llm.prompt_versions import prompt_version_for

log = logging.getLogger(__name__)

LlmCaller = Callable[..., str]
_PURPOSE_SHARD_RX = re.compile(r"_p([0-9a-f]{12})\.jsonl$")


@dataclass(frozen=True, slots=True)
class CaptureAuditItem(AuditItem):
    """One capture plus the exact production cohort that produced it."""

    prompt_version: str = ""
    model: str = ""
    backend: str = ""


_FACETS: dict[str, tuple[tuple[str, str], ...]] = {
    "classification": (
        (
            "label_correctness",
            "The label or route matches the supplied evidence and task boundary.",
        ),
        ("boundary_calibration", "Ambiguity and borderline cases are handled conservatively."),
        ("evidence_use", "The classification follows from relevant evidence rather than keywords."),
        ("no_fabrication", "No unsupported fact, label premise, or certainty is introduced."),
    ),
    "coaching": (
        ("context_fidelity", "The output accurately reflects the supplied history and objective."),
        ("specificity", "Advice names the concrete behavior, tradeoff, or decision at issue."),
        ("actionability", "The next step is bounded, reversible, and useful."),
        ("calibration", "The tone and confidence match the strength of the evidence."),
    ),
    "extraction": (
        ("source_fidelity", "Every extracted claim is traceable to the supplied source."),
        ("completeness", "All material in-scope fields are captured without irrelevant additions."),
        ("field_precision", "Entities, periods, units, labels, and relationships are correct."),
        ("no_fabrication", "Missing or ambiguous source data is not guessed."),
    ),
    "judgment": (
        ("source_fidelity", "The conclusion is supported by the supplied evidence."),
        ("reasoning_quality", "The causal chain and decision criteria are explicit and coherent."),
        ("countercase", "Material contrary evidence or alternative explanations are addressed."),
        ("calibration", "Confidence and recommended action match uncertainty and stakes."),
    ),
    "research": (
        (
            "source_quality",
            "Sources are current, credible, primary where possible, and directly relevant.",
        ),
        (
            "evidence_coverage",
            "The output covers the material evidence and meaningful counterevidence.",
        ),
        ("provenance", "Claims remain attributable and fact is separated from interpretation."),
        ("synthesis", "The evidence is converted into a concise, decision-relevant conclusion."),
    ),
    "synthesis": (
        (
            "source_fidelity",
            "The synthesis preserves the facts and qualifiers in the supplied material.",
        ),
        ("prioritization", "Material information leads and noise is omitted."),
        ("balance", "Risks, counterevidence, and uncertainty are represented fairly."),
        ("decision_usefulness", "The output clarifies implications, gaps, or next actions."),
    ),
    "visualization": (
        ("information_accuracy", "Nodes, labels, and relationships match the supplied material."),
        ("hierarchy", "The visual hierarchy makes the important structure immediately legible."),
        ("completeness", "Material components and dependencies are present without clutter."),
        ("usability", "The output can be rendered or implemented without ambiguous instructions."),
    ),
}


def rubric_for_capture(spec: CaptureQualitySpec) -> Rubric:
    facets = _FACETS[spec.family]
    facet_text = "\n".join(
        f"## Facet: {facet_id} — {description}\n"
        "Score 1.0 when fully met, 0.5 when partially met, and 0.0 when materially missed."
        for facet_id, description in facets
    )
    text = (
        f"# Rubric: {spec.purpose}\n\n"
        f"Priority: {spec.priority}\n"
        f"Traffic tier: {spec.traffic_tier}\n"
        f"Pass threshold: {spec.pass_threshold:.2f}\n\n"
        f"Purpose objective: {spec.objective}\n\n"
        f"Critical failure: {spec.critical_failure}\n\n"
        "Judge the RESPONSE against the PROMPT and embedded source material. "
        "Do not reward verbosity. A critical failure caps every affected facet at 0.0.\n\n"
        f"{facet_text}\n"
    )
    return Rubric(
        purpose=spec.purpose,
        pass_threshold=spec.pass_threshold,
        facet_ids=tuple(facet_id for facet_id, _ in facets),
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _parse_captured_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _matches_purpose(captured: str, requested: str) -> bool:
    return captured.startswith("lens:") if requested == "lens:*" else captured == requested


def _clip_exchange(prompt: str, response: str) -> str:
    header = "PROMPT AND SOURCE MATERIAL:\n"
    divider = "\n\nRESPONSE UNDER AUDIT:\n"
    response_budget = min(len(response), MAX_CONTENT_CHARS // 2)
    prompt_budget = MAX_CONTENT_CHARS - len(header) - len(divider) - response_budget
    clipped_prompt = prompt[: max(0, prompt_budget)]
    clipped_response = response[:response_budget]
    suffix = ""
    if len(prompt) > len(clipped_prompt) or len(response) > len(clipped_response):
        suffix = "\n...[capture clipped for bounded evaluation]"
    return f"{header}{clipped_prompt}{divider}{clipped_response}{suffix}"


def _capture_dirs(repo_root: Path) -> tuple[Path, ...]:
    primary = default_capture_archive_dir(repo_root)
    legacy = repo_root / "data" / "llm_capture"
    if primary == legacy or not (repo_root / "pyproject.toml").is_file():
        return (legacy,)
    return (primary, legacy)


def _capture_paths(capture_dir: Path, purpose: str) -> Iterator[Path]:
    """Yield legacy mixed shards plus only the requested purpose's new shards."""
    target_suffix = capture_purpose_suffix(purpose)
    for path in capture_dir.glob("capture_*.jsonl"):
        match = _PURPOSE_SHARD_RX.search(path.name)
        if match is None or match.group(1) == target_suffix:
            yield path


def _reverse_lines(path: Path, *, chunk_size: int = 64 * 1024) -> Iterator[str]:
    """Yield UTF-8 JSONL rows newest-first without loading a whole shard."""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        remainder = b""
        while position:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            block = handle.read(read_size) + remainder
            parts = block.split(b"\n")
            remainder = parts[0]
            for raw_line in reversed(parts[1:]):
                if raw_line:
                    yield raw_line.decode("utf-8")
        if remainder:
            yield remainder.decode("utf-8")


def _time_rank(value: datetime | None) -> int:
    """Comparable newest-first rank without platform-sensitive timestamps."""
    if value is None:
        return 0
    return (
        value.toordinal() * 86_400_000_000
        + value.hour * 3_600_000_000
        + value.minute * 60_000_000
        + value.second * 1_000_000
        + value.microsecond
    )


def _iter_capture_items(
    path: Path,
    purpose: str,
    *,
    cutoff: datetime | None,
) -> Iterator[tuple[CaptureAuditItem, str]]:
    """Yield one shard newest-first, filtering age before deduplication."""
    try:
        lines = _reverse_lines(path)
        for line_number, line in enumerate(lines, start=1):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                log.warning(
                    {
                        "event": "capture_quality_row_invalid",
                        "file": path.name,
                        "reverse_line": line_number,
                    }
                )
                continue
            if not isinstance(raw, dict):
                continue
            record = cast("dict[str, object]", raw)
            captured_purpose = record.get("purpose")
            prompt = record.get("prompt")
            response = record.get("response")
            prompt_version = record.get("prompt_version")
            model = record.get("model")
            backend = record.get("backend")
            if (
                not isinstance(captured_purpose, str)
                or not _matches_purpose(captured_purpose, purpose)
                or not isinstance(prompt, str)
                or not isinstance(response, str)
                or not isinstance(prompt_version, str)
                or not prompt_version
                or not isinstance(model, str)
                or not model
                or not isinstance(backend, str)
                or not backend
            ):
                continue
            captured_at = _parse_captured_at(record.get("captured_at"))
            if cutoff is not None and (captured_at is None or captured_at < cutoff):
                continue
            prompt_sha = record.get("prompt_sha256")
            identity = (
                prompt_sha
                if isinstance(prompt_sha, str) and prompt_sha
                else hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            )
            ticker_value = record.get("ticker")
            ticker = ticker_value if isinstance(ticker_value, str) and ticker_value else None
            stamp = captured_at.isoformat() if captured_at is not None else path.stem
            item_id = f"capture:{captured_purpose}:{identity[:16]}"
            label = f"{captured_purpose} capture {stamp}" + (f" ({ticker})" if ticker else "")
            yield (
                CaptureAuditItem(
                    item_id=item_id,
                    label=label,
                    ticker=ticker,
                    content=_clip_exchange(prompt, response),
                    produced_at=captured_at,
                    prompt_version=prompt_version,
                    model=model,
                    backend=backend,
                ),
                identity,
            )
    except (OSError, UnicodeDecodeError) as exc:
        log.warning(
            {
                "event": "capture_quality_file_unreadable",
                "file": path.name,
                "error": f"{type(exc).__name__}",
            }
        )


def load_capture_quality_corpus(
    repo_root: Path,
    purpose: str,
    *,
    limit: int | None = None,
    since_days: int | None = None,
    required_prompt_version: str | None = None,
    required_backend: str | None = None,
) -> list[CaptureAuditItem]:
    """Load newest unique captured exchanges for ``purpose``.

    Invalid JSONL rows are skipped with a bounded metadata-only warning; prompt
    and response text are never written to logs. When ``limit`` is provided,
    scanning stops as soon as the newest bounded sample is full instead of
    reading the entire historical corpus.
    """
    if limit == 0:
        return []

    items: list[CaptureAuditItem] = []
    seen: set[str] = set()
    selected_cohort: tuple[str, str, str] | None = None
    cutoff = (
        datetime.now(UTC).replace(tzinfo=None) - timedelta(days=since_days)
        if since_days is not None
        else None
    )
    paths = sorted(
        {
            path
            for capture_dir in _capture_dirs(repo_root)
            if capture_dir.is_dir()
            for path in _capture_paths(capture_dir, purpose)
        },
        key=lambda path: path.name,
    )
    iterators: list[Iterator[tuple[CaptureAuditItem, str]]] = []
    heap: list[tuple[int, int, CaptureAuditItem, str]] = []
    for path in paths:
        iterator = _iter_capture_items(path, purpose, cutoff=cutoff)
        iterator_index = len(iterators)
        iterators.append(iterator)
        try:
            item, identity = next(iterator)
        except StopIteration:
            continue
        heapq.heappush(
            heap,
            (-_time_rank(item.produced_at), iterator_index, item, identity),
        )

    while heap:
        _, iterator_index, item, identity = heapq.heappop(heap)
        cohort = (item.prompt_version, item.model, item.backend)
        if (
            required_prompt_version is not None and item.prompt_version != required_prompt_version
        ) or (required_backend is not None and item.backend != required_backend):
            pass
        elif selected_cohort is None:
            selected_cohort = cohort
        if cohort == selected_cohort and identity not in seen:
            seen.add(identity)
            items.append(item)
        if limit is not None and len(items) >= limit:
            break

        iterator = iterators[iterator_index]
        try:
            next_item, next_identity = next(iterator)
        except StopIteration:
            continue
        heapq.heappush(
            heap,
            (
                -_time_rank(next_item.produced_at),
                iterator_index,
                next_item,
                next_identity,
            ),
        )
    return items


def run_capture_quality_eval(
    purpose: str,
    *,
    repo_root: Path,
    code_root: Path,
    limit: int | None = None,
    since_days: int | None = None,
    required_backend: str | None = None,
    caller: LlmCaller = call_llm,
) -> EvalRunSummary:
    """Judge a bounded replay sample without invoking the production purpose."""
    spec = CAPTURE_QUALITY_SPECS.get(purpose)
    if spec is None:
        raise ValueError(
            f"no capture-quality spec for purpose {purpose!r}; "
            f"known: {sorted(CAPTURE_QUALITY_SPECS)}"
        )
    rubric = rubric_for_capture(spec)
    bounded_limit = spec.default_limit if limit is None else max(0, limit)
    current_prompt_version = prompt_version_for(purpose)
    items = load_capture_quality_corpus(
        repo_root,
        purpose,
        limit=bounded_limit,
        since_days=since_days,
        required_prompt_version=current_prompt_version,
        required_backend=required_backend,
    )

    run_id = uuid4().hex
    judge_model = LLM_MODELS.get(JUDGE_PURPOSE, DEFAULT_MODEL)
    cohort_model = items[0].model if items else LLM_MODELS.get(purpose, DEFAULT_MODEL)
    cohort_backend = items[0].backend if items else "none"
    summary = EvalRunSummary(
        run_id=run_id,
        purpose=purpose,
        mode="capture_audit",
        prompt_version=current_prompt_version,
        model=cohort_model,
        judge_model=judge_model,
        golden_set_sha=rubric.sha256,
        started_at=now_naive_utc(),
        git_sha=resolve_git_sha(code_root),
        notes=(
            f"capture audit priority={spec.priority} traffic={spec.traffic_tier} "
            f"n={len(items)} limit={bounded_limit} backend={cohort_backend} "
            f"prompt_version={current_prompt_version}"
            + (f" required_backend={required_backend}" if required_backend is not None else "")
            + (f" since_days={since_days}" if since_days is not None else "")
        ),
    )
    consecutive_infra = 0
    for item in items:
        result = judge_item(
            rubric,
            item,
            run_id=run_id,
            caller=caller,
            sensitive=True,
        )
        # The generic rubric judge includes its full prompt and raw response for
        # normal low-volume audits. Capture audits must not persist that prompt:
        # it embeds the private production exchange and would otherwise escape
        # the archive's retention policy into portfolio.db and its backups.
        result.prompt_text = None
        result.response_text = None
        result.judge_verdict = None
        result.judge_rationale = None
        summary.cases.append(result)
        consecutive_infra = consecutive_infra + 1 if result.is_infra_failure else 0
        if consecutive_infra >= 3:
            raise EvalAbortError(
                f"[{purpose}] three consecutive capture-audit judge failures; "
                "aborting instead of measuring transport health."
            )
    summary.finished_at = now_naive_utc()
    return summary
