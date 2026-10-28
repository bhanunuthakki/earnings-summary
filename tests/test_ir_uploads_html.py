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


@pytest.mark.parametrize("extension", [".html", ".htm"])
def test_release_archive_cannot_use_linked_headlines_as_release_content(
    tmp_path: Path, extension: str
) -> None:
    path = tmp_path / f"archive{extension}"
    payload = b"""<html><body><main><h1>Meta quarterly results archive</h1>
    <ul><li><a href="/q3-release">Meta Reports Third Quarter 2025 Results</a></li>
    <li><a href="/q2-release">Meta Reports Second Quarter 2025 Results</a></li></ul>
    </main></body></html>"""
    path.write_bytes(payload)
    result = classify_ir_file(path)
    assert isinstance(result, CategorizationFailure)
    assert result.reason == "html_earnings_release_unidentified"
    assert path.read_bytes() == payload


def test_short_release_keeps_linked_issuer_and_local_heading_anchor(tmp_path: Path) -> None:
    path = tmp_path / "release.html"
    path.write_text(
        '<main><h1><a href="#results">Meta Reports Third Quarter 2025 Results</a></h1>'
        '<p><a href="https://about.meta.com">Meta Platforms</a> today reported financial '
        "results for the third quarter 2025.</p><p>Revenue was $40 billion.</p></main>"
    )
    result = classify_ir_file(path)
    assert isinstance(result, CategorizationResult)
    assert result.ticker == "META"
    assert result.doc_type == DocType.IR_PRESS_RELEASE
    assert result.period_end == date(2025, 9, 30)


@pytest.mark.parametrize("prefix", [b"%PDF-1.7\n", b"PK\x03\x04"])
def test_html_release_rejects_mislabeled_binary_bytes(tmp_path: Path, prefix: bytes) -> None:
    path = tmp_path / "release.html"
    payload = prefix + (
        b"Meta Reports Third Quarter 2025 Results. "
        b"Meta Platforms today reported financial results for the third quarter 2025."
    )
    path.write_bytes(payload)
    result = classify_ir_file(path)
    assert isinstance(result, CategorizationFailure)
    assert path.read_bytes() == payload
