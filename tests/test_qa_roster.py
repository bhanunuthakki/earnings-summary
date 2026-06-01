"""Tests for the §6 Q&A roster parser (``report.sections.qa_roster``).

The free aggregators (roic.ai, stockanalysis.com) emit the analyst-Q&A segment
in several shapes; the boundary + speaker-block parser must handle all of them.
A universe probe over ``transcripts/processed/*.txt`` rose from ~0% → ~73% once
the roic US / NU-webcast boundary shapes + Latin particle surnames were handled,
then to ~98% once these remaining families were covered:

  * roic boundary verb/connector drift — "will come from" / "is coming from" /
    "today comes from" / bare "question from", and the "<A> of <Firm>" connector,
  * roic host-queue phrasing — "we'll go first today to <A> with <Firm>",
  * the stockanalysis.com full-name speaker format — "<Name> <Role…>, <Company>
    <text>" with no single-initial marker (every NU quarter uses it),
  * '?'-terminated webcast hand-offs and abbreviation firm names ("J.P. Morgan").

These tests pin one representative transcript per shape. ``_apply_llm_topics``
(the LLM topic-labeling layer) is covered separately in
``tests/test_qa_roster_resilience.py``; here ``build`` runs offline (enable_llm
defaults to False) so the regex-derived rosters are exercised directly.
"""

from __future__ import annotations

from report.models import AppendixSection, SectionStatus, TranscriptEntry
from report.sections import qa_roster


def _appendix(text: str, quarter: str = "Q1", year: int = 2025) -> AppendixSection:
    return AppendixSection(
        status=SectionStatus.OK,
        transcripts=[TranscriptEntry(quarter=quarter, year=year, source_path="x.txt", text=text)],
    )


def _entries(text: str) -> list:
    section = qa_roster.build(_appendix(text))
    assert section.status == SectionStatus.OK, section.missing
    assert len(section.quarters) == 1
    return section.quarters[0].entries


# ---------------------------------------------------------------------------
# roic.ai US operator format
# ---------------------------------------------------------------------------

# "the line of" insert + "O Operator" single-initial marker on the boundary line.
US_ROIC = """=== Q&A SEGMENT ===
O Operator And your first question comes from the line of Brian Nowak with Morgan Stanley.

B Brian Nowak I have two questions. First, can you talk about the CapEx outlook for next year?

S Susan Li Thanks, Brian, for the question. The growth in CapEx comes from each of the core areas.

O Operator Your next question comes from the line of Doug Anmuth with JPMorgan.

D Doug Anmuth I appreciate the color. How are you thinking about capacity for next year?

S Susan Li Thanks, Doug. We are relatively early in the build-out and adding capacity steadily.
"""

# Older roic output: no "the line of" between "from" and the analyst.
LEGACY_NO_LINE_OF = """=== Q&A SEGMENT ===
O Operator Your next question comes from David Jones with Goldman Sachs.

D David Jones Thanks. What is driving the margin expansion this quarter?

A Andrew Smith Great question, David. Operating leverage is the main driver here.
"""


def test_roic_us_format_the_line_of() -> None:
    entries = _entries(US_ROIC)
    assert len(entries) == 2
    assert entries[0].analysts == "Brian Nowak (Morgan Stanley)"
    assert entries[0].answers[0][0] == "Susan Li"
    assert entries[1].analysts == "Doug Anmuth (JPMorgan)"


def test_roic_legacy_format_without_the_line_of() -> None:
    entries = _entries(LEGACY_NO_LINE_OF)
    assert len(entries) == 1
    assert entries[0].analysts == "David Jones (Goldman Sachs)"
    assert entries[0].answers[0][0] == "Andrew Smith"


# ---------------------------------------------------------------------------
# roic.ai boundary verb / connector drift (the bulk of the recovered universe)
# ---------------------------------------------------------------------------

# "will come from", "is coming from", "today comes from", bare "question from",
# and the "<A> of <Firm>" / "<A> at <Firm>" connectors — none matched by the
# original "(comes|is) from … with" boundary regex.
ROIC_VERB_VARIANTS = """=== Q&A SEGMENT ===
O Operator Our first question will come from David Roman from Goldman Sachs.

D David Roman Thanks for taking the question. How should we think about gross margin?

S Stefan Murry We expect gradual improvement as yields ramp.

O Operator Our next question is coming from Maury Raycroft of Jefferies.

M Maury Raycroft Appreciate it. What is the cadence of new product launches?

S Stefan Murry We have several launches planned through the back half.

O Operator Our next question today comes from Joseph Spak at UBS.

J Joseph Spak Thank you. Can you size the backlog exiting the quarter?

S Stefan Murry The backlog is up meaningfully year over year.

O Operator And our first question from Amit Daryanani of Evercore ISI.

A Amit Daryanani Two for me. What drove the operating leverage this period?

S Stefan Murry Operating leverage came from disciplined cost control.
"""


def test_roic_boundary_verb_and_connector_variants() -> None:
    entries = _entries(ROIC_VERB_VARIANTS)
    assert len(entries) == 4
    assert entries[0].analysts == "David Roman (Goldman Sachs)"  # "will come from … from"
    assert entries[1].analysts == "Maury Raycroft (Jefferies)"  # "is coming from … of"
    assert entries[2].analysts == "Joseph Spak (UBS)"  # "today comes from … at"
    assert entries[3].analysts == "Amit Daryanani (Evercore ISI)"  # bare "question from … of"
    # All four bodies split correctly (the analyst's question, not the answer).
    assert entries[0].answers[0][0] == "Stefan Murry"


def test_roic_abbreviation_firm_not_truncated() -> None:
    # The firm capture must not stop at the dot inside an abbreviation; "J.P.
    # Morgan" used to truncate to "J" because the terminator was a bare period.
    text = (
        "=== Q&A SEGMENT ===\n"
        "O Operator Our first question will come from Param Singh from J.P. Morgan.\n\n"
        "P Param Singh Thanks. What is the outlook for fiscal 2026?\n\n"
        "R Ravi Kumar We expect continued double-digit growth.\n"
    )
    entries = _entries(text)
    assert len(entries) == 1
    assert entries[0].analysts == "Param Singh (J.P. Morgan)"


# "we'll go (first|next) [today|this morning] to <A> with <Firm>" host-queue style.
ROIC_GO_TO = """=== Q&A SEGMENT ===
O Operator We will go first today to Craig Siegenthaler with Bank of America.

C Craig Siegenthaler Good morning. How is fundraising trending across the credit platform?

M Michael Arougheti Thanks, Craig. Fundraising had a strong quarter despite some retail deceleration.

O Operator We'll go next to Bose George with KBW.

B Bose George Thanks. What are you seeing on net interest margins?

M Michael Arougheti Margins held up well, supported by the floating-rate book.
"""


def test_roic_go_to_host_queue() -> None:
    entries = _entries(ROIC_GO_TO)
    assert len(entries) == 2
    assert entries[0].analysts == "Craig Siegenthaler (Bank of America)"
    assert entries[1].analysts == "Bose George (KBW)"
    assert entries[0].answers[0][0] == "Michael Arougheti"


# ---------------------------------------------------------------------------
# NU webcast format ("open the line for [Mr.] <A> from <Firm>") + particle surname
# ---------------------------------------------------------------------------

# Period-terminated hand-offs; the CFO carries a Latin particle surname that the
# speaker regex must keep whole, or his answer merges into the question.
NU_WEBCAST = """=== Q&A SEGMENT ===
G Guilherme Souto Thank you, operator. Could you please open the line for Mr. Yuri Fernandes from JPMorgan.

Y Yuri Fernandes Thanks for taking my question. Can you walk through the asset quality trends this quarter?

G Guilherme Marques do Lago No, thanks, Yuri, for the question. Cost of risk improved on the new underwriting model.

G Guilherme Souto Operator, could you please open the line for Mr. Jorge Kuri from Morgan Stanley.

J Jorge Kuri Congrats on the numbers. My question is around your net interest margin this quarter.

G Guilherme Marques do Lago Sure, Jorge. The mix shifted toward less risky assets, compressing the margin.
"""


def test_nu_webcast_open_the_line_for() -> None:
    entries = _entries(NU_WEBCAST)
    assert len(entries) == 2
    assert entries[0].analysts == "Yuri Fernandes (JPMorgan)"
    assert entries[1].analysts == "Jorge Kuri (Morgan Stanley)"
    # The CFO's Latin particle surname must be its own speaker so the answer
    # splits out of the analyst's question paragraph.
    assert entries[0].answers, "expected the CFO answer to be captured"
    assert entries[0].answers[0][0] == "Guilherme Marques do Lago"
    assert "cost of risk improved" not in entries[0].question.lower()


def test_operator_handoff_stub_filtered() -> None:
    # "Operator, could you please [open the line for …]" stubs must not surface
    # as one-line answers.
    for entry in _entries(NU_WEBCAST):
        for _speaker, atext in entry.answers:
            assert not atext.lower().startswith("operator,"), (
                f"hand-off stub leaked as an answer: {atext!r}"
            )


# ---------------------------------------------------------------------------
# stockanalysis.com full-name speaker format (every NU quarter)
# ---------------------------------------------------------------------------

# "<Name> <Role…>, <Company> <text>" with NO initial marker; '?'-terminated
# webcast hand-offs; an abbreviation firm ("J.P. Morgan"); an accented surname
# ("Vélez"); a multi-comma role ("Founder, Chief Executive Officer, and
# Chairman"); and a trailing IR-host hand-off stub to drop.
SA_FULLNAME = (
    "=== Q&A SEGMENT ===\n"
    "Guilherme Souto Investor Relations Officer and Director of Market Intelligence, "
    "Nu Holdings Thank you, operator. Could you please open the line for Mr. Eduardo "
    "Rosman from BTG Pactual? "
    "Eduardo Rosman Analyst, BTG Pactual Hi, everyone. Good evening. How do you think "
    "about the risk that AI disrupts the business? "
    "David Vélez Founder, Chief Executive Officer, and Chairman, Nu Holdings Sure. The "
    "answer is both a challenge and an opportunity for us. "
    "Eduardo Rosman Analyst, BTG Pactual Perfect. Thanks a lot for the answer. "
    "Guilherme Souto Investor Relations Officer and Director of Market Intelligence, Nu "
    "Holdings Operator, could you please open the line for Mr. Jorge Kuri from J.P. Morgan? "
    "Jorge Kuri Equity Research Analyst, J.P. Morgan Hi, good afternoon. Can you walk "
    "through your loan growth for the quarter? "
    "Guilherme Lago Chief Financial Officer, Nu Holdings Hi, Jorge. Thanks for the "
    "question. Loan growth was strong this quarter.\n"
)


def test_stockanalysis_fullname_format() -> None:
    entries = _entries(SA_FULLNAME)
    assert len(entries) == 2

    # Analyst metadata comes from the hand-off; the '?' terminator and the
    # abbreviation firm must both parse cleanly (not "J").
    assert entries[0].analysts == "Eduardo Rosman (BTG Pactual)"
    assert entries[1].analysts == "Jorge Kuri (J.P. Morgan)"

    q0 = entries[0]
    # The spoken text must start at the speech, not the "<Role…>, <Company>" title.
    assert q0.question.startswith("Hi, everyone."), q0.question
    assert "Analyst, BTG Pactual" not in q0.question
    # The answer is attributed to the full-name management speaker (accented
    # surname preserved), with the title stripped from the answer text.
    assert q0.answers[0][0] == "David Vélez"
    assert q0.answers[0][1].startswith("Sure.")
    assert "Chief Executive Officer" not in q0.answers[0][1]
    # Same analyst's later turn is the follow-up, not a second answer.
    assert q0.follow_up is not None
    assert q0.follow_up.startswith("Perfect.")
    # The trailing IR-host hand-off stub ("Operator, could you please …") is dropped.
    for _speaker, atext in q0.answers:
        assert "could you please" not in atext.lower()

    assert entries[1].answers[0][0] == "Guilherme Lago"


def test_stockanalysis_title_prefix_fully_stripped() -> None:
    # No role/company tokens should leak into any spoken text across the roster.
    leaky = ("Officer", "Founder", "Chairman", "Holdings", "Pactual", "Analyst, ")
    for entry in _entries(SA_FULLNAME):
        for fragment in (entry.question, *(a[1] for a in entry.answers)):
            for tok in leaky:
                assert tok not in fragment, f"title token {tok!r} leaked into: {fragment!r}"


# ---------------------------------------------------------------------------
# Degradation: a transcript with no recognizable Q&A turns
# ---------------------------------------------------------------------------


def test_unparseable_transcript_reports_partial() -> None:
    section = qa_roster.build(_appendix("=== Q&A SEGMENT ===\nNo structured turns here at all.\n"))
    assert section.status == SectionStatus.PARTIAL
    assert section.missing is not None
