from __future__ import annotations

import json

from ard_ossie.semantic.adjudication import candidate_choice_schema


def test_embedded_table_heavy_candidate_budget(issue_3_replay) -> None:
    result, provider, _repeated, repeated_provider = issue_3_replay
    ambiguous_decisions = sum(
        decision.source == "model" for decision in result.decisions.decisions
    )

    assert len(result.evidence.atoms) >= 700
    assert sum(block.kind == "table" for block in result.canonical.blocks) >= 8
    assert provider.calls <= ambiguous_decisions
    assert provider.max_candidate_count <= 5
    assert len(json.dumps(candidate_choice_schema(), sort_keys=True).encode()) <= 2_048
    assert repeated_provider.calls == 0
