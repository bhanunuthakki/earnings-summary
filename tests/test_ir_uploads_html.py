"""HTML release classification preserves publisher bytes and rejects unrelated pages."""

from datetime import date
from pathlib import Path

import pytest

from ir_uploads import (
    CategorizationFailure,
    CategorizationResult,
    canonical_path,
    classify_ir_file,
    fingerprint,
    iter_uncategorized_files,
    parse_canonical_path,
    sha256_of,
)
from models.documents import DocType


@pytest.mark.parametrize("extension", [".html", ".htm"])
def test_html_release_classification_and_canonical_retention(
    tmp_path: Path, extension: str
) -> None:
    path = tmp_path / f"release{extension}"
    payload = b"""<!doctype html><html><head><script>Rubrik Reports First Quarter 2024</script>
    <style>Veeva Systems</style></head><body><nav>Rubrik annual report 2024</nav>
    <main><h1>Meta Reports Third Quarter 2025 Results</h1>
    <p>Meta Platforms today reported financial results for the third quarter 2025.</p>
    <table><tr><td>Revenue</td><td>$40 &amp; more</td></tr></table></main></body></html>"""
    path.write_bytes(payload)
    result = classify_ir_file(path)
    assert isinstance(result, CategorizationResult)
    assert result.ticker == "META"
    assert result.doc_type == DocType.IR_PRESS_RELEASE
    assert result.period_end == date(2025, 9, 30)
    assert "Rubrik" not in fingerprint(path)
    assert "$40 & more" in fingerprint(path)
    destination = canonical_path(tmp_path, result, sha256_of(path), extension)
    destination.parent.mkdir(parents=True)
    path.rename(destination)
    restored = parse_canonical_path(destination, tmp_path)
    assert restored is not None
    assert restored.doc_type == result.doc_type
    assert iter_uncategorized_files(tmp_path) == [destination]
    assert destination.read_bytes() == payload


def test_html_filename_cannot_turn_error_page_into_release(tmp_path: Path) -> None:
    path = tmp_path / "META_press_release_2025Q3.html"
    path.write_text("<html><body><h1>Access denied</h1></body></html>")
    result = classify_ir_file(path, ticker_hint="META", calendar_override="calendar")
    assert isinstance(result, CategorizationFailure)
    assert result.reason == "html_earnings_release_unidentified"


def test_html_dateline_alone_is_not_an_earnings_release(tmp_path: Path) -> None:
    path = tmp_path / "META_press_release_2025Q3.html"
    path.write_text(
        "<html><body><main>Meta Platforms\n"
        "MENLO PARK, California; October 30, 2025\n"
        "New office opening.</main></body></html>"
    )
    result = classify_ir_file(path)
    assert isinstance(result, CategorizationFailure)
    assert result.reason == "html_earnings_release_unidentified"
