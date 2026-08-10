from __future__ import annotations

from ard_ossie.impact import ChangeSetStatus, analyze_table_change, build_changeset
from ard_ossie.models import ProductTableRef

PRODUCT_A = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
PRODUCT_B = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632"
TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"


def mapping(product_id: str, suffix: str) -> ProductTableRef:
    return ProductTableRef(
        link_id=f"lnk_0198f6ce-c3d5-7fc8-9401-22fa7b330e{suffix}",
        product_id=product_id,
        table_id=TABLE_ID,
        table_version=7,
        usage="SOURCE",
    )


def test_changed_shared_table_requires_every_linked_product() -> None:
    impact = analyze_table_change(
        TABLE_ID,
        [mapping(PRODUCT_B, "c3"), mapping(PRODUCT_A, "c2")],
    )

    assert impact.required_product_ids == [PRODUCT_A, PRODUCT_B]
    assert impact.shared is True


def test_changeset_is_incomplete_until_every_product_version_is_ready() -> None:
    change = build_changeset(
        [TABLE_ID],
        [PRODUCT_A, PRODUCT_B],
        changeset_id="cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2",
    )

    change.mark_ready(PRODUCT_A, version=4, pr_number=21, head_sha="a" * 40)
    assert change.status is ChangeSetStatus.BLOCKED
    change.mark_ready(PRODUCT_B, version=9, pr_number=22, head_sha="b" * 40)
    assert change.status is ChangeSetStatus.READY


def test_changeset_rejects_product_outside_impact_set() -> None:
    change = build_changeset(
        [TABLE_ID],
        [PRODUCT_A],
        changeset_id="cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2",
    )

    try:
        change.mark_ready(PRODUCT_B, version=1, pr_number=22, head_sha="b" * 40)
    except ValueError as error:
        assert str(error) == f"product is not required by changeset: {PRODUCT_B}"
    else:
        raise AssertionError("unknown product must be rejected")
