"""Source-bound IR event parsing and retained event lifecycle."""

from datetime import date

import pytest

from signals.ir_event_discovery import parse_event_feed


def test_calendar_preserves_date_only_and_converts_timestamp() -> None:
    feed = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:first\r
SUMMARY:Investor Day\r
CATEGORIES:Investor Day\r
DTSTART;VALUE=DATE:20261102\r
URL:https://ir.example.com/first\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:second\r
SUMMARY:Financial Analyst Day\r
CATEGORIES:Analyst Day\r
DTSTART:20261103T020000Z\r
URL:https://ir.example.com/second\r
END:VEVENT\r
END:VCALENDAR\r
"""
    events = parse_event_feed(feed, "text/calendar")
    assert [event.event_date for event in events] == [date(2026, 11, 2)] * 2
    assert events[0].starts_at is None
    assert events[1].starts_at is not None
    assert events[0].source_event_id != events[1].source_event_id


def test_empty_feed_requires_structural_completion() -> None:
    assert parse_event_feed(b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR", "text/calendar") == ()
    with pytest.raises(ValueError):
        parse_event_feed(b"<html>No events found</html>", "text/html")


def test_clock_without_zone_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_event_feed(
            b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\nSUMMARY:Investor Day\nCATEGORIES:Investor Day\nDTSTART:20261103T020000\nURL:https://ir.example.com/a\nEND:VEVENT\nEND:VCALENDAR",
            "text/calendar",
        )


def test_structured_html_feed_reconciles_count_and_supported_categories() -> None:
    import json

    payload = {
        "@type": "ItemList",
        "numberOfItems": 1,
        "itemListElement": [
            {
                "@type": "BusinessEvent",
                "identifier": "official-id",
                "name": "Acme Capital Markets Day",
                "category": "Capital Markets Day",
                "startDate": "2026-11-03T02:00:00+00:00",
                "url": "https://ir.example.com/cmd",
            }
        ],
    }
    raw = (
        '<html><script type="application/ld+json">' + json.dumps(payload) + "</script></html>"
    ).encode()
    events = parse_event_feed(raw, "text/html")
    assert events[0].event_kind == "capital_markets_day"
    assert events[0].event_date == date(2026, 11, 2)
    with pytest.raises(ValueError, match="count"):
        parse_event_feed(raw.replace(b'"numberOfItems": 1', b'"numberOfItems": 2'), "text/html")
    with pytest.raises(ValueError, match="category"):
        parse_event_feed(raw.replace(b"Capital Markets Day", b"Unknown Category"), "text/html")


@pytest.mark.parametrize("value", ["20261101T013000", "20260308T023000"])
def test_ambiguous_and_nonexistent_pacific_clock_rejected(value: str) -> None:
    raw = f"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\nSUMMARY:Investor Day\nCATEGORIES:Investor Day\nDTSTART;TZID=America/Los_Angeles:{value}\nURL:https://ir.example.com/a\nEND:VEVENT\nEND:VCALENDAR".encode()
    with pytest.raises(ValueError, match="ambiguous or nonexistent"):
        parse_event_feed(raw, "text/calendar")


def test_irrelevant_structured_event_can_be_excluded() -> None:
    raw = b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\nSUMMARY:Earnings\nCATEGORIES:Earnings Call\nDTSTART:20261103T020000Z\nURL:https://ir.example.com/a\nEND:VEVENT\nEND:VCALENDAR"
    assert parse_event_feed(raw, "text/calendar") == ()
