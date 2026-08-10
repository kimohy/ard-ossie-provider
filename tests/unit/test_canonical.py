from __future__ import annotations

from ard_ossie.canonical import canonical_hash, schema_hash


def test_volatile_metadata_does_not_change_canonical_hash() -> None:
    left = {
        "name": "Orders",
        "generated_at": "2026-08-08T01:00:00Z",
        "fields": ["id"],
        "nested": {"actions_run_id": "100"},
    }
    right = {
        "fields": ["id"],
        "nested": {"actions_run_id": "200"},
        "name": "Orders",
        "generated_at": "2026-08-09T01:00:00Z",
    }

    assert canonical_hash(left) == canonical_hash(right)


def test_changed_semantic_description_changes_hash() -> None:
    assert canonical_hash({"description": "ordered"}) != canonical_hash({"description": "shipped"})


def test_unicode_composed_and_decomposed_text_have_same_hash() -> None:
    assert canonical_hash({"name": "Café"}) == canonical_hash({"name": "Cafe\u0301"})


def test_schema_hash_ignores_descriptions_but_detects_physical_change() -> None:
    original = {
        "columns": [{"ordinal": 1, "name": "order_id", "type": "INT64", "description": "old"}],
        "generated_at": "now",
    }
    description_only = {
        "columns": [{"ordinal": 1, "name": "order_id", "type": "INT64", "description": "new"}]
    }
    changed_type = {
        "columns": [{"ordinal": 1, "name": "order_id", "type": "STRING", "description": "old"}]
    }

    assert schema_hash(original) == schema_hash(description_only)
    assert schema_hash(original) != schema_hash(changed_type)
