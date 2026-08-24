"""Build an inert issuer_fact_manifest.v1 from reviewed offline inputs.

The command never opens a database.  It creates the requested output once with
an exclusive create, so an existing reviewed artifact is never replaced.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pipeline.issuer_document_coverage import ExtractorFactPopulationFrame  # noqa: E402
from pipeline.issuer_fact_manifest_producer import (  # noqa: E402
    ReviewedSegmentValues,
    produce_issuer_fact_manifest,
)
from pipeline.kpi_persistence import KpiExtractionManifest  # noqa: E402
from provenance.secure_file_install import install_bytes_no_clobber  # noqa: E402


class LegacyKpiManifestFile(BaseModel):
    """The one-document legacy wrapper emitted by extract_kpis_from_ir."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifests: tuple[KpiExtractionManifest, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_document_only(self) -> LegacyKpiManifestFile:
        if len(self.manifests) != 1:
            raise ValueError("issuer manifest producer requires exactly one legacy KPI manifest")
        return self


def _read_model(path: Path, model: type[BaseModel]) -> BaseModel:
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def _publish_no_clobber(path: Path, contents: str) -> None:
    """Stage exact bytes, then atomically publish a no-clobber target.

    ``install_bytes_no_clobber`` keeps the final name absent until the staged
    bytes are complete and verified, and treats a pre-existing different target
    as a conflict rather than overwriting it.
    """
    payload = contents.encode("utf-8")
    install_bytes_no_clobber(
        path.parent,
        path.name,
        payload,
        expected_size=len(payload),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-kpi-manifest", type=Path, required=True)
    parser.add_argument("--population-frame", type=Path, required=True)
    parser.add_argument("--segment-values", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    legacy_file = _read_model(args.legacy_kpi_manifest, LegacyKpiManifestFile)
    frame = _read_model(args.population_frame, ExtractorFactPopulationFrame)
    segments = _read_model(args.segment_values, ReviewedSegmentValues)
    assert isinstance(legacy_file, LegacyKpiManifestFile)
    assert isinstance(frame, ExtractorFactPopulationFrame)
    assert isinstance(segments, ReviewedSegmentValues)
    manifest = produce_issuer_fact_manifest(legacy_file.manifests[0], frame, segments)
    _publish_no_clobber(args.output, manifest.canonical_json)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "manifest_sha256": manifest.manifest_sha256,
                "expected_count": len(manifest.expected),
                "captured_count": len(manifest.values),
                "rejected_count": len(manifest.rejected),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
