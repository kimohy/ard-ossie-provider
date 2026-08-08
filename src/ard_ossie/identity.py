from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Literal

from pydantic import Field

from ard_ossie.models import EntityStatus, Operation, ProductRecord, StrictModel, TableRecord


class DuplicateDecision(StrEnum):
    BLOCK = "BLOCK"
    CREATE = "CREATE"
    NO_CHANGE = "NO_CHANGE"
    REUSE = "REUSE"
    UPDATE = "UPDATE"
    WARN = "WARN"


class DuplicateReport(StrictModel):
    entity_type: Literal["product", "table"]
    decision: DuplicateDecision
    code: str
    target_id: str | None = None
    candidate_ids: list[str] = Field(default_factory=list)
    evidence: dict[str, str] = Field(default_factory=dict)


def classify_product(
    candidate: ProductRecord,
    existing_products: Iterable[ProductRecord],
    *,
    operation: Operation,
    semantic_candidate_ids: Iterable[str] = (),
) -> DuplicateReport:
    existing = list(existing_products)
    id_match = next(
        (item for item in existing if item.product_id == candidate.product_id),
        None,
    )

    if operation is not Operation.CREATE:
        if id_match is None:
            return _product_report(DuplicateDecision.BLOCK, "PRODUCT_ID_NOT_FOUND")
        if id_match.status is EntityStatus.RETIRED and candidate.status is EntityStatus.ACTIVE:
            return _product_report(
                DuplicateDecision.BLOCK,
                "RETIRED_ID_REUSE",
                target_id=id_match.product_id,
            )
        if candidate.product_key != id_match.product_key:
            return _product_report(
                DuplicateDecision.BLOCK,
                "PRODUCT_ID_KEY_CONFLICT",
                target_id=id_match.product_id,
            )
        if candidate.canonical_hash == id_match.canonical_hash:
            return _product_report(
                DuplicateDecision.NO_CHANGE,
                "PRODUCT_NO_CHANGE",
                target_id=id_match.product_id,
            )
        return _product_report(
            DuplicateDecision.UPDATE,
            "PRODUCT_UPDATE",
            target_id=id_match.product_id,
        )

    if id_match is not None:
        return _product_report(
            DuplicateDecision.BLOCK,
            "PRODUCT_ID_CONFLICT",
            target_id=id_match.product_id,
        )

    key_match = next(
        (item for item in existing if item.product_key == candidate.product_key),
        None,
    )
    if key_match is not None:
        return _product_report(
            DuplicateDecision.BLOCK,
            "PRODUCT_KEY_CONFLICT",
            target_id=key_match.product_id,
        )

    alias_match = next(
        (item for item in existing if candidate.product_key in item.aliases),
        None,
    )
    if alias_match is not None:
        return _product_report(
            DuplicateDecision.BLOCK,
            "PRODUCT_ALIAS_CONFLICT",
            target_id=alias_match.product_id,
        )

    content_match = next(
        (
            item
            for item in existing
            if candidate.canonical_hash is not None
            and candidate.canonical_hash == item.canonical_hash
        ),
        None,
    )
    if content_match is not None:
        return _product_report(
            DuplicateDecision.BLOCK,
            "PRODUCT_CONTENT_DUPLICATE",
            target_id=content_match.product_id,
        )

    semantic_ids = sorted(set(semantic_candidate_ids))
    if semantic_ids:
        return _product_report(
            DuplicateDecision.WARN,
            "POSSIBLE_DUPLICATE",
            candidate_ids=semantic_ids,
        )

    return _product_report(DuplicateDecision.CREATE, "PRODUCT_CREATE")


def classify_table(
    candidate: TableRecord,
    existing_tables: Iterable[TableRecord],
) -> DuplicateReport:
    existing = list(existing_tables)
    id_match = next((item for item in existing if item.table_id == candidate.table_id), None)
    if id_match is not None:
        if id_match.status is EntityStatus.RETIRED and candidate.status is EntityStatus.ACTIVE:
            return _table_report(
                DuplicateDecision.BLOCK,
                "RETIRED_ID_REUSE",
                target_id=id_match.table_id,
            )
        if id_match.locator.key != candidate.locator.key:
            return _table_report(
                DuplicateDecision.BLOCK,
                "TABLE_ID_LOCATOR_CONFLICT",
                target_id=id_match.table_id,
            )
        if id_match.canonical_hash == candidate.canonical_hash:
            return _table_report(
                DuplicateDecision.NO_CHANGE,
                "TABLE_NO_CHANGE",
                target_id=id_match.table_id,
            )
        return _table_report(
            DuplicateDecision.UPDATE,
            "TABLE_UPDATE",
            target_id=id_match.table_id,
        )

    locator_match = next(
        (item for item in existing if item.locator.key == candidate.locator.key),
        None,
    )
    if locator_match is not None:
        if locator_match.status is EntityStatus.RETIRED:
            return _table_report(
                DuplicateDecision.BLOCK,
                "RETIRED_ID_REUSE",
                target_id=locator_match.table_id,
            )
        return _table_report(
            DuplicateDecision.REUSE,
            "TABLE_LOCATOR_REUSE",
            target_id=locator_match.table_id,
        )

    schema_matches = sorted(
        item.table_id
        for item in existing
        if candidate.schema_hash is not None and candidate.schema_hash == item.schema_hash
    )
    if schema_matches:
        return _table_report(
            DuplicateDecision.WARN,
            "POSSIBLE_CLONE",
            candidate_ids=schema_matches,
        )

    return _table_report(DuplicateDecision.CREATE, "TABLE_CREATE")


def _product_report(
    decision: DuplicateDecision,
    code: str,
    *,
    target_id: str | None = None,
    candidate_ids: list[str] | None = None,
) -> DuplicateReport:
    return DuplicateReport(
        entity_type="product",
        decision=decision,
        code=code,
        target_id=target_id,
        candidate_ids=candidate_ids or [],
    )


def _table_report(
    decision: DuplicateDecision,
    code: str,
    *,
    target_id: str | None = None,
    candidate_ids: list[str] | None = None,
) -> DuplicateReport:
    return DuplicateReport(
        entity_type="table",
        decision=decision,
        code=code,
        target_id=target_id,
        candidate_ids=candidate_ids or [],
    )
