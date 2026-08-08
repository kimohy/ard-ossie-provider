from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from ard_ossie.ids import new_id
from ard_ossie.models import ProductId, ProductTableRef, StrictModel, TableId, Version

ChangeSetId = Annotated[
    str,
    StringConstraints(
        pattern=r"^cst_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
GitCommitSha = Annotated[
    str,
    StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"),
]


class ChangeSetStatus(StrEnum):
    BLOCKED = "blocked"
    READY = "ready"


class ImpactReport(StrictModel):
    table_id: TableId
    required_product_ids: list[ProductId]
    shared: bool


class ProductReadiness(StrictModel):
    version: Version
    pr_number: int = Field(gt=0)
    head_sha: GitCommitSha


class ChangeSetRecord(StrictModel):
    changeset_id: ChangeSetId
    table_ids: list[TableId]
    required_product_ids: list[ProductId]
    ready_products: dict[ProductId, ProductReadiness] = Field(default_factory=dict)

    @property
    def status(self) -> ChangeSetStatus:
        ready = set(self.ready_products)
        required = set(self.required_product_ids)
        return ChangeSetStatus.READY if ready == required else ChangeSetStatus.BLOCKED

    def mark_ready(
        self, product_id: str, *, version: int, pr_number: int, head_sha: str
    ) -> None:
        if product_id not in self.required_product_ids:
            raise ValueError(f"product is not required by changeset: {product_id}")
        self.ready_products[product_id] = ProductReadiness(
            version=version,
            pr_number=pr_number,
            head_sha=head_sha,
        )


def analyze_table_change(
    table_id: str,
    mappings: Iterable[ProductTableRef],
) -> ImpactReport:
    products = sorted({item.product_id for item in mappings if item.table_id == table_id})
    return ImpactReport(
        table_id=table_id,
        required_product_ids=products,
        shared=len(products) > 1,
    )


def build_changeset(
    table_ids: Iterable[str],
    required_product_ids: Iterable[str],
    *,
    changeset_id: str | None = None,
) -> ChangeSetRecord:
    return ChangeSetRecord(
        changeset_id=changeset_id or new_id("cst"),
        table_ids=sorted(set(table_ids)),
        required_product_ids=sorted(set(required_product_ids)),
    )
