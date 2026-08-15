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
    result, _provider, repeated, repeated_provider = issue_3_replay
    golden = json.loads(
        Path("tests/fixtures/semantic/issue-3-golden.json").read_text(encoding="utf-8")
    )
    headings = [block.text for block in result.canonical.blocks if block.kind == "heading"]
    heading_levels = [
        block.heading_level
        for block in result.canonical.blocks
        if block.kind == "heading"
    ]
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
    assert heading_levels == golden["heading_levels"]
    assert table_dimensions == golden["table_dimensions"]
    assert all(phrase in plain_text for phrase in golden["required_phrases"])
    assert all(value not in result.markdown for value in golden["forbidden_strings"])
    assert "<pre" not in result.markdown
    table_region_ids = {
        block.region_id for block in result.canonical.blocks if block.kind == "table"
    }
    assert not any(
        candidate_set.decision_type == "spacing"
        and candidate_set.region_id in table_region_ids
        for candidate_set in result.candidate_sets
    )
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
