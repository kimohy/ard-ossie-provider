from __future__ import annotations

import json
from pathlib import Path


def test_issue_3_replay_is_verified_without_korean_corruption(issue_3_replay) -> None:
    result, _provider, repeated, repeated_provider = issue_3_replay
    golden = json.loads(
        Path("tests/fixtures/semantic/issue-3-golden.json").read_text(encoding="utf-8")
    )
    headings = [block.text for block in result.canonical.blocks if block.kind == "heading"]
    table_dimensions = [
        [block.row_count, block.column_count]
        for block in result.canonical.blocks
        if block.kind == "table"
    ]
    plain_text = "\n".join(
        cell.text
        for block in result.canonical.blocks
        for cell in block.cells
    )

    assert result.validation.status == "verified"
    assert result.validation.character_coverage == 1.0
    assert result.validation.missing_atom_count == 0
    assert result.validation.duplicate_atom_count == 0
    assert result.validation.degraded_block_count == 0
    assert headings == golden["headings"]
    assert table_dimensions == golden["table_dimensions"]
    assert all(phrase in plain_text for phrase in golden["required_phrases"])
    assert all(value not in result.markdown for value in golden["forbidden_strings"])
    assert "<pre" not in result.markdown
    recovered = [
        decision
        for decision in result.decisions.decisions
        if decision.recovery_status == "recovered"
    ]
    assert len(recovered) == 2
    assert sorted(item.attempts[0].confidence for item in recovered) == [0.70, 0.74]
    assert all(item.consensus_method == "same_candidate" for item in recovered)
    assert all(item.validation_codes == ("LLM_LOW_CONFIDENCE_RECOVERED",) for item in recovered)
    candidate_sets = {item.candidate_set_id: item for item in result.candidate_sets}
    for decision in recovered:
        expected = golden["recovered_candidates"][decision.candidate_set_id]
        selected = next(
            candidate
            for candidate in candidate_sets[decision.candidate_set_id].candidates
            if candidate.candidate_id == decision.selected_candidate_id
        )
        assert decision.selected_candidate_id == expected["candidate_id"]
        assert selected.rendered_text == expected["rendering"]
    assert result.validation.canonical_hash == repeated.validation.canonical_hash
    assert repeated_provider.calls == 0
    recovered_candidate_sets = {item.candidate_set_id for item in recovered}
    assert all(
        repeated_decision.source == "cache"
        for repeated_decision in repeated.decisions.decisions
        if repeated_decision.candidate_set_id in recovered_candidate_sets
    )


def test_issue_3_capture_is_public_metadata_only() -> None:
    payload = json.loads(
        Path("tests/fixtures/semantic/issue-3-evidence.json").read_text(encoding="utf-8")
    )

    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload["source_hash"] == (
        "ca630eac7231e454a2398e2f1e25328490966ab1e110230f1c5eaba6ab367cf6"
    )
    assert payload["page_count"] == 5
    assert "page_images" not in serialized
    assert "image_bytes" not in serialized
    assert "api_key" not in serialized
