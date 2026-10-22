"""Persisted reader projections must agree with the reviewed scenario replay."""

from dataclasses import asdict, replace

import pytest

from dcf.input_evidence import InputEvidenceError, canonical_digest
from dcf.meli_inputs import effective_numeric_inputs, model_output
from dcf.meli_model import Assum
from dcf.readiness import replay_model_scenarios


@pytest.mark.parametrize("damage", [None, "fair_value", "input_hash", "provenance", "missing_case"])
def test_compact_scenario_reader_cannot_disagree_with_reviewed_vectors(damage: str | None) -> None:
    base = Assum()
    numeric = effective_numeric_inputs(asdict(base))
    vectors = {
        name: effective_numeric_inputs(asdict(replace(base, comm_g_near=growth)))
        for name, growth in (
            ("bear", base.comm_g_near - 0.02),
            ("base", base.comm_g_near),
            ("bull", base.comm_g_near + 0.02),
        )
    }
    projected = {
        name: {
            "fair_value_per_share_usd": model_output(vector)["vps"],
            "provenance": "explicit_vector_acceptance_separate",
            "effective_inputs_sha256": canonical_digest(vector),
        }
        for name, vector in vectors.items()
    }
    if damage == "fair_value":
        projected["bear"]["fair_value_per_share_usd"] = 1
    elif damage == "input_hash":
        projected["bear"]["effective_inputs_sha256"] = "0" * 64
    elif damage == "provenance":
        projected["bear"]["provenance"] = "seeded_unreviewed"
    elif damage == "missing_case":
        projected.pop("bear")
    snapshot: dict[str, object] = {"reviewed_scenario_inputs": vectors, "scenarios": projected}
    if damage:
        with pytest.raises(InputEvidenceError, match="persisted_scenario_output_replay_mismatch"):
            replay_model_scenarios("MELI", numeric, model_output(numeric), snapshot)
    else:
        assert set(replay_model_scenarios("MELI", numeric, model_output(numeric), snapshot)) == {
            "base_model_output",
            "scenarios",
        }
