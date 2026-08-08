from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from pydantic import BaseModel

VOLATILE_FIELDS = frozenset(
    {
        "actions_run_id",
        "commit_sha",
        "generated_at",
        "llm_response_id",
        "provenance_collected_at",
    }
)
SEMANTIC_ONLY_FIELDS = frozenset(
    {
        "description",
        "display_name",
        "examples",
        "logical_name",
        "synonyms",
    }
)


def canonical_hash(value: object) -> str:
    """Hash normalized semantic content while excluding execution metadata."""

    return _hash_normalized(value, excluded=VOLATILE_FIELDS)


def schema_hash(value: object) -> str:
    """Hash only physical schema content, excluding semantic labels and metadata."""

    return _hash_normalized(value, excluded=VOLATILE_FIELDS | SEMANTIC_ONLY_FIELDS)


def _hash_normalized(value: object, *, excluded: frozenset[str]) -> str:
    normalized = _normalize(value, excluded=excluded)
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalize(value: object, *, excluded: frozenset[str]) -> Any:
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="json"), excluded=excluded)
    if isinstance(value, Enum):
        return _normalize(value.value, excluded=excluded)
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {
            unicodedata.normalize("NFC", str(key)): _normalize(item, excluded=excluded)
            for key, item in value.items()
            if str(key) not in excluded
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_normalize(item, excluded=excluded) for item in value]
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")
