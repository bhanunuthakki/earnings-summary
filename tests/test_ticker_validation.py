"""Guards for ``ticker_validation.safe_ticker`` — the path-traversal allowlist
that gates external tickers before they reach a filesystem path."""

from __future__ import annotations

import pytest

from ticker_validation import safe_ticker


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NU", "NU"),
        ("nu", "NU"),  # uppercased
        ("  meli  ", "MELI"),  # trimmed
        ("BRK-B", "BRK-B"),  # hyphen
        ("BRK.A", "BRK.A"),  # dot inside
        ("GOOGL", "GOOGL"),
        ("FLKR", "FLKR"),
        ("0700", "0700"),  # leading digit allowed
    ],
)
def test_accepts_real_universe_shapes(raw: str, expected: str) -> None:
    assert safe_ticker(raw) == expected


@pytest.mark.parametrize(
    "evil",
    [
        "",
        "   ",
        "..",  # parent-dir token — the hole a bare [A-Z0-9.-]+ would allow
        ".",
        "../../etc",
        "../etc/passwd",
        "..\\..\\windows",
        "a/b",
        "NU/../X",
        "-X",  # must start alphanumeric
        ".NU",
        "TICKERTOOLONG",  # >12 chars
        "NU ME",  # whitespace inside
        "NU;DROP",  # stray punctuation
        "%2e%2e",
    ],
)
def test_rejects_traversal_and_malformed(evil: str) -> None:
    with pytest.raises(ValueError):
        safe_ticker(evil)


def test_rejects_non_string() -> None:
    for bad in (None, 123, ["NU"], {"ticker": "NU"}):
        with pytest.raises(ValueError):
            safe_ticker(bad)


def test_max_length_boundary() -> None:
    assert safe_ticker("A" * 12) == "A" * 12  # 12 ok
    with pytest.raises(ValueError):
        safe_ticker("A" * 13)  # 13 rejected
