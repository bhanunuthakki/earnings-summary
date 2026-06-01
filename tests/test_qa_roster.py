"""Tests for the §6 Q&A roster parser (``report.sections.qa_roster``).

Covers the two aggregator transcript shapes the boundary parser must handle —
both observed in real roic.ai output that the original regex silently failed on:

  * US operator style:  "...question comes from the line of <A> with <Firm>."
  * webcast / IR-host style (NU et al.):  "...open the line for [Mr.] <A> from <Firm>."

plus the Latin particle-surname speaker split ("Guilherme Marques do Lago" — a
real NU CFO whose answers otherwise merge into the analyst's question) and the
operator hand-off stub filter.
"""

from __future__ import annotations

from report.models import AppendixSection, SectionStatus, TranscriptEntry
from report.sections import qa_roster


def _appendix(text: str, quarter: str = "Q1", year: int = 2025) -> AppendixSection:
    return AppendixSection(
        status=SectionStatus.OK,
        transcripts=[TranscriptEntry(quarter=quarter, year=year, source_path="x.txt", text=text)],
    )


# roic.ai US shape — note the "the line of" insert the original regex couldn't
# handle, and the "O Operator" single-initial marker on the boundary line.
US_ROIC = """=== Q&A SEGMENT ===
O Operator And your first question comes from the line of Brian Nowak with Morgan Stanley.

B Brian Nowak I have two questions. First, can you talk about the CapEx outlook for next year?

S Susan Li Thanks, Brian, for the question. The growth in CapEx comes from each of the core areas.

O Operator Your next question comes from the line of Doug Anmuth with JPMorgan.

D Doug Anmuth I appreciate the color. How are you thinking about capacity for next year?

S Susan Li Thanks, Doug. We are relatively early in the build-out and adding capacity steadily.
"""

# NU webcast shape — the IR officer queues each analyst ("open the line for
# Mr. X from FIRM"), and the CFO has a Latin particle surname.
NU_WEBCAST = """=== Q&A SEGMENT ===
G Guilherme Souto Thank you, operator. Could you please open the line for Mr. Yuri Fernandes from JPMorgan.

Y Yuri Fernandes Thanks for taking my question. Can you walk through the asset quality trends this quarter?

G Guilherme Marques do Lago No, thanks, Yuri, for the question. Cost of risk improved on the new underwriting model.

G Guilherme Souto Operator, could you please open the line for Mr. Jorge Kuri from Morgan Stanley.

J Jorge Kuri Congrats on the numbers. My question is around your net interest margin this quarter.

G Guilherme Marques do Lago Sure, Jorge. The mix shifted toward less risky assets, compressing the margin.
"""

# Older roic output (and the shape the original docstring documented): no
# "the line of" between "from" and the analyst.
LEGACY_NO_LINE_OF = """=== Q&A SEGMENT ===
O Operator Your next question comes from David Jones with Goldman Sachs.

D David Jones Thanks. What is driving the margin expansion this quarter?

A Andrew Smith Great question, David. Operating leverage is the main driver here.
"""


def test_roic_us_format_the_line_of() -> None:
    section = qa_roster.build(_appendix(US_ROIC))
    assert section.status == SectionStatus.OK
    assert len(section.quarters) == 1
    entries = section.quarters[0].entries
    assert len(entries) == 2
    assert entries[0].analysts == "Brian Nowak (Morgan Stanley)"
    assert entries[0].answers[0][0] == "Susan Li"
    assert entries[1].analysts == "Doug Anmuth (JPMorgan)"


def test_nu_webcast_open_the_line_for() -> None:
    section = qa_roster.build(_appendix(NU_WEBCAST))
    assert section.status == SectionStatus.OK
    entries = section.quarters[0].entries
    assert len(entries) == 2
    assert entries[0].analysts == "Yuri Fernandes (JPMorgan)"
    assert entries[1].analysts == "Jorge Kuri (Morgan Stanley)"
    # The CFO's Latin particle surname must be recognized as its own speaker so
    # the answer splits out of the analyst's question paragraph.
    assert entries[0].answers, "expected the CFO answer to be captured"
    assert entries[0].answers[0][0] == "Guilherme Marques do Lago"
    # The analyst question must NOT have absorbed the CFO's answer text.
    assert "cost of risk improved" not in entries[0].question.lower()


def test_legacy_format_without_the_line_of() -> None:
    section = qa_roster.build(_appendix(LEGACY_NO_LINE_OF))
    assert section.status == SectionStatus.OK
    entries = section.quarters[0].entries
    assert len(entries) == 1
    assert entries[0].analysts == "David Jones (Goldman Sachs)"
    assert entries[0].answers[0][0] == "Andrew Smith"


def test_operator_handoff_stub_filtered() -> None:
    section = qa_roster.build(_appendix(NU_WEBCAST))
    for q in section.quarters:
        for entry in q.entries:
            for _speaker, atext in entry.answers:
                assert not atext.lower().startswith("operator,"), (
                    f"hand-off stub leaked as an answer: {atext!r}"
                )
