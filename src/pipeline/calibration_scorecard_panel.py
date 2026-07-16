"""Calibration scorecard panel (close_the_loops L8) — the coach's monthly read.

Renders the latest persisted ``CalibrationScorecard`` (the named recurring
biases + the period's behavioural experiment) as a section on the Portfolio →
Decisions page, BELOW the live cohort-trend + skill-decomposition sections
(PR1). Those show the live numbers; this shows the eval-gated LLM coaching the
numbers can't produce on their own.

This module imports ``calibration_coach`` freely; the decisions panel mounts the
rendered HTML as a string (``compose_decisions_page(scorecard_html=...)``) so the
coach module — which itself imports the decisions panel for the sizing-audit
conviction map — is never imported at the panel's module load (no cycle).

Honesty is the whole point: when the ledger is too thin to coach, or the
synthesised prose failed the eval gate, the section says so plainly rather than
showing fabricated or unvetted coaching. When no scorecard has been generated
yet, the section renders a one-line starvation stub (REQ-6: coach_pings /
coach_mutes / an ungenerated scorecard must never render as a silent absence)
instead of "" — the caller threads in how many decisions have graded so far so
the stub reads as progress, not a dead end.
"""

from __future__ import annotations

from html import escape

from calibration_coach import CalibrationScorecard, NamedBias


def render_scorecard_section(card: CalibrationScorecard | None, *, n_graded: int = 0) -> str:
    """The coach's-read section.

    ``card`` None (no scorecard generated yet) renders a one-line honest
    caption naming how many graded decisions have accrued so far
    (``n_graded``, threaded in by the caller from the calibration stats the
    page already computes) rather than vanishing — REQ-6's starvation stub."""
    # "Calibration coach" (wave B B6): the section header names WHICH coach —
    # Allocation carries the Positioning coach, Record this one. Inner labels
    # ("Coach's read: …") keep their wording.
    if card is None:
        return (
            '<section class="panel cs-stub"><h2>Calibration coach</h2>'
            '<p class="muted cs-caption">Coach&rsquo;s read: no scorecard yet — generated '
            "monthly once graded decisions accrue (currently "
            f"{int(n_graded)} graded).</p></section>"
        )
    head = (
        '<section class="panel"><h2>Calibration coach</h2>'
        '<p class="sub">The monthly calibration scorecard — named recurring biases drawn from '
        "your own graded history, and one falsifiable behavioural experiment for the period. "
        "LLM-composed and eval-gated against the <code>calibration_coach</code> rubric before "
        f"it shows. Generated {escape(card.generated_at[:10]) if card.generated_at else '—'} "
        f"for <b>{escape(card.period)}</b>.</p>"
    )
    body = _coach_body(card)
    notes = "".join(f'<p class="muted cs-note">{escape(n)}</p>' for n in card.notes)
    return f"{head}{body}{notes}</section>"


def _coach_body(card: CalibrationScorecard) -> str:
    if not card.biases and card.experiment is None:
        # Either too thin to coach or the gate suppressed the prose — the notes
        # (rendered by the caller) carry the honest reason; no fabricated body.
        if not card.can_coach:
            return (
                '<p class="muted">Not enough graded history to coach yet — the live cohort '
                "trend and skill decomposition above still render.</p>"
            )
        return ""
    badge = _quality_badge(card)
    bias_html = "".join(_bias_card(b) for b in card.biases)
    biases_block = f'{badge}<div class="cs-biases">{bias_html}</div>' if card.biases else badge
    return f"{biases_block}{_experiment_block(card)}"


def _quality_badge(card: CalibrationScorecard) -> str:
    if card.coach_quality_ok is None:
        return ""
    score = f" ({card.coach_quality_score:.2f})" if card.coach_quality_score is not None else ""
    if card.coach_quality_ok:
        return f'<p class="cs-gate cs-gate-ok">Passed the calibration_coach eval gate{score}.</p>'
    return (
        f'<p class="cs-gate cs-gate-bad">Coach prose suppressed — failed the eval gate{score}.</p>'
    )


def _bias_card(b: NamedBias) -> str:
    evidence = "".join(f"<li>{escape(e)}</li>" for e in b.evidence)
    tell = (
        f'<p class="cs-tell"><span class="cs-tell-k">tell</span> {escape(b.tell)}</p>'
        if b.tell
        else ""
    )
    return (
        '<div class="cs-bias">'
        f'<h3 class="cs-bias-name">{escape(b.name)}</h3>'
        f'<p class="cs-bias-pattern">{escape(b.pattern)}</p>'
        f'<ul class="cs-evidence">{evidence}</ul>'
        f"{tell}</div>"
    )


def _experiment_block(card: CalibrationScorecard) -> str:
    e = card.experiment
    if e is None:
        return ""
    test = ""
    if e.condition is not None:
        note = e.condition.note or e.condition.metric
        test = f'<p class="cs-exp-test"><span class="cs-tell-k">falsifiable test</span> {escape(note)}</p>'
    why = f'<p class="cs-exp-why">{escape(e.rationale)}</p>' if e.rationale else ""
    return (
        '<div class="cs-experiment"><h3 class="adc-sub">This period&rsquo;s experiment</h3>'
        f'<p class="cs-exp-hyp">{escape(e.hypothesis)}</p>'
        f"{why}{test}</div>"
    )


SCORECARD_CSS = """
/* Calibration scorecard (L8): coach's-read cards. */
.cs-gate { font-size: var(--fs-caption); margin: 2px 0 8px; }
.cs-gate-ok { color: var(--ok); }
.cs-gate-bad { color: var(--bad); }
.cs-biases { display: flex; flex-direction: column; gap: 10px; }
.cs-bias { border-left: 3px solid var(--border); padding: 2px 0 2px 12px; }
.cs-bias-name { font-size: var(--fs-body); font-weight: 600; margin: 0 0 2px;
  text-transform: none; letter-spacing: 0; }
.cs-bias-pattern { font-size: var(--fs-body); margin: 0 0 4px; }
.cs-evidence { margin: 0 0 4px; padding-left: 18px; font-size: var(--fs-caption);
  color: var(--muted); }
.cs-tell, .cs-exp-test { font-size: var(--fs-caption); margin: 2px 0; }
.cs-tell-k { font-family: var(--sans); color: var(--muted); text-transform: uppercase;
  font-size: var(--fs-micro); letter-spacing: 0.04em; margin-right: 6px; }
.cs-experiment { margin-top: 12px; }
.cs-exp-hyp { font-size: var(--fs-body); font-weight: 600; margin: 0 0 2px; }
.cs-exp-why { font-size: var(--fs-caption); color: var(--muted); margin: 0 0 4px; }
.cs-note { font-size: var(--fs-caption); margin: 6px 0 0; }
.cs-caption { font-size: var(--fs-caption); margin: 2px 0 0; }
"""


__all__ = ["SCORECARD_CSS", "render_scorecard_section"]
