from __future__ import annotations

import uuid

import pytest

from ard_ossie.ids import new_id


def test_new_id_contains_requested_prefix_and_uuidv7_payload() -> None:
    value = new_id("prd", timestamp_ms=1_723_078_800_123, random_bits=1)

    prefix, payload = value.split("_", maxsplit=1)
    parsed = uuid.UUID(payload)
    assert prefix == "prd"
    assert parsed.version == 7
    assert parsed.variant == uuid.RFC_4122


def test_uuidv7_payloads_sort_by_increasing_millisecond_timestamp() -> None:
    earlier = new_id("tbl", timestamp_ms=1_723_078_800_123, random_bits=0)
    later = new_id("tbl", timestamp_ms=1_723_078_800_124, random_bits=0)

    assert earlier < later


def test_new_id_rejects_unknown_prefix() -> None:
    with pytest.raises(ValueError, match="unsupported ID prefix"):
        new_id("unknown")


def test_new_id_supports_changeset_identity() -> None:
    assert new_id("cst", timestamp_ms=1_723_078_800_123, random_bits=1).startswith("cst_")


def test_new_id_rejects_random_value_outside_74_bits() -> None:
    with pytest.raises(ValueError, match="74-bit"):
        new_id("prd", random_bits=1 << 74)
