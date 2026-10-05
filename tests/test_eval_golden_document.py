"""Common golden-document validation retains each evaluator's contract."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from evals import golden_classifiers, injection_canaries, provenance_caution


@pytest.fixture(params=["transcript_metadata", "injection_canaries", "provenance_caution"])
def loader(request: pytest.FixtureRequest) -> tuple[str, Callable[[Path], list[dict[str, object]]]]:
    purpose = str(request.param)
    if purpose == "transcript_metadata":
        load = cast(
            Callable[[Path, str], list[dict[str, object]]], getattr(golden_classifiers, "_load_doc")
        )
        return purpose, lambda path: load(path, purpose)
    module = injection_canaries if purpose == "injection_canaries" else provenance_caution
    return purpose, cast(Callable[[Path], list[dict[str, object]]], getattr(module, "_load_doc"))


def test_preserves_case_order_and_unicode(
    tmp_path: Path, loader: tuple[str, Callable[[Path], list[dict[str, object]]]]
) -> None:
    purpose, load = loader
    cases = [{"id": "second", "text": "⚠ issuer wording"}, {"id": "first"}]
    path = tmp_path / "golden.json"
    path.write_text(json.dumps({"purpose": purpose, "cases": cases}), encoding="utf-8")
    assert load(path) == cases


_INVALID_DOCUMENTS: list[tuple[object, str]] = [
    ([], "golden file must be a JSON object"),
    ({"purpose": "wrong"}, "golden file purpose must be"),
    ({"cases": []}, "golden file needs a non-empty `cases` list"),
    ({"cases": [{}, 7]}, "cases[1]: must be an object"),
]


@pytest.mark.parametrize(("payload", "message"), _INVALID_DOCUMENTS)
def test_rejects_invalid_document(
    tmp_path: Path,
    loader: tuple[str, Callable[[Path], list[dict[str, object]]]],
    payload: object,
    message: str,
) -> None:
    purpose, load = loader
    if isinstance(payload, dict) and "purpose" not in payload:
        payload = {**cast(dict[str, object], payload), "purpose": purpose}
    path = tmp_path / "golden.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError) as error:
        load(path)
    assert message in str(error.value)


@pytest.mark.parametrize("contents", [None, "{invalid", "\xff"])
def test_wraps_unreadable_document(
    tmp_path: Path,
    loader: tuple[str, Callable[[Path], list[dict[str, object]]]],
    contents: str | None,
) -> None:
    _, load = loader
    path = tmp_path / "golden.json"
    if contents is not None:
        path.write_bytes(contents.encode("latin-1"))
    with pytest.raises(ValueError) as error:
        load(path)
    assert str(error.value).startswith(f"golden file unreadable at {path}:")
