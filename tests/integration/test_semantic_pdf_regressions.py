from __future__ import annotations

import json
from pathlib import Path


def _table_rows(result, region_id: str) -> list[list[str]]:
    block = next(item for item in result.canonical.blocks if item.region_id == region_id)
    rows = [["" for _ in range(block.column_count or 0)] for _ in range(block.row_count or 0)]
    for cell in block.cells:
        rows[cell.start_row][cell.start_column] = cell.text
    return rows


def test_issue_3_replay_is_verified_without_korean_corruption(issue_3_replay) -> None:
    result, provider, repeated, repeated_provider = issue_3_replay
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
    cell_texts = [cell.text for block in result.canonical.blocks for cell in block.cells]
    assert all(value in cell_texts for value in golden["required_repaired_table_cells"])
    assert all(value in cell_texts for value in golden["required_unchanged_table_cells"])
    assert all(
        fragment not in plain_text for fragment in golden["forbidden_table_cell_fragments"]
    )
    assert all(value not in result.markdown for value in golden["forbidden_strings"])
    assert "<pre" not in result.markdown
    table_decisions = [
        decision for decision in result.decisions.decisions if decision.decision_type == "table"
    ]
    assert table_decisions
    assert all(decision.source == "deterministic" for decision in table_decisions)
    table_region_ids = {
        block.region_id for block in result.canonical.blocks if block.kind == "table"
    }
    table_spacing_sets = [
        candidate_set
        for candidate_set in result.candidate_sets
        if candidate_set.decision_type == "spacing"
        and candidate_set.region_id in table_region_ids
    ]
    assert table_spacing_sets
    assert all(
        all("table_cell_composite" in candidate.features for candidate in candidate_set.candidates)
        for candidate_set in table_spacing_sets
    )
    assert provider.generation_calls > 0
    assert provider.verification_calls == provider.generation_calls
    for region_id, expected_rows in golden["exact_tables"].items():
        assert _table_rows(result, region_id) == expected_rows
        block = next(item for item in result.canonical.blocks if item.region_id == region_id)
        assert block.text == "\n".join("\t".join(row) for row in expected_rows)
    assert result.validation.canonical_hash == repeated.validation.canonical_hash
    assert repeated_provider.calls == 0


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
