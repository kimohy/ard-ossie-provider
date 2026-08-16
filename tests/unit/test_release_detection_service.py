from __future__ import annotations

from pathlib import Path

import pytest

from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.application.contracts import WorkflowConflict, WorkflowStatus
from ard_ossie.application.release_detection import (
    ReleaseDetectionRequest,
    ReleaseDetectionService,
)
from ard_ossie.impact import build_changeset
from ard_ossie.models import ProductRecord, TableLocator, TableRecord
from ard_ossie.ports.git import ChangedPaths
from ard_ossie.registry import Registry

BEFORE = "a" * 40
CURRENT = "b" * 40
SALES_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
FINANCE_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632"
TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"
CHANGESET_ID = "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2"


class FakeGit:
    def __init__(self, paths: tuple[Path, ...]) -> None:
        self.paths = paths

    def current_sha(self) -> str:
        return CURRENT

    def changed_paths(self, base_ref: str, head_ref: str = "HEAD") -> ChangedPaths:
        assert base_ref == BEFORE
        assert head_ref == CURRENT
        return ChangedPaths(merge_base=BEFORE, paths=self.paths)


def request(tmp_path: Path) -> ReleaseDetectionRequest:
    return ReleaseDetectionRequest(
        repository=tmp_path,
        before=BEFORE,
        current=CURRENT,
    )


def build_repository(
    tmp_path: Path,
    *,
    current_versions: tuple[int, int] = (1, 1),
    readiness_versions: tuple[int, int] = (1, 1),
) -> None:
    registry = Registry.load(tmp_path / "registry")
    registry.write_product(
        ProductRecord(
            product_id=SALES_ID,
            product_key="sales-order",
            version=current_versions[0],
        )
    )
    registry.write_product(
        ProductRecord(
            product_id=FINANCE_ID,
            product_key="finance-order",
            version=current_versions[1],
        )
    )
    registry.write_table(
        TableRecord(
            table_id=TABLE_ID,
            locator=TableLocator(
                source_system_id="erp",
                catalog="analytics",
                schema_name="sales",
                table_name="orders",
            ),
            version=1,
        )
    )
    changeset = build_changeset(
        [TABLE_ID],
        [SALES_ID, FINANCE_ID],
        changeset_id=CHANGESET_ID,
    )
    changeset.mark_ready(
        SALES_ID,
        version=readiness_versions[0],
        pr_number=7,
        head_sha="c" * 40,
    )
    changeset.mark_ready(
        FINANCE_ID,
        version=readiness_versions[1],
        pr_number=8,
        head_sha="d" * 40,
    )
    registry.write_changeset(changeset)
    for product_key, product_id, version in (
        ("sales-order", SALES_ID, current_versions[0]),
        ("finance-order", FINANCE_ID, current_versions[1]),
    ):
        product = tmp_path / "products" / product_key
        (product / "generated").mkdir(parents=True)
        (product / "generated" / "ossie-model.json").write_text("{}", encoding="utf-8")
        (product / "product.yaml").write_text(
            "\n".join(
                (
                    f"product_id: {product_id}",
                    f"product_key: {product_key}",
                    f"version: {version}",
                    f"changeset_id: {CHANGESET_ID}",
                    "",
                )
            ),
            encoding="utf-8",
        )


def test_detect_expands_completed_changeset_products(tmp_path: Path) -> None:
    build_repository(tmp_path)
    service = ReleaseDetectionService(
        RepositoryPaths(tmp_path),
        FakeGit((Path(f"registry/changesets/{CHANGESET_ID}.json"),)),
    )

    result = service.run(request(tmp_path))

    assert result.outputs["products"] == ["finance-order", "sales-order"]
    assert result.outputs["tables"] == [TABLE_ID]


def test_detect_rejects_stale_readiness_version(tmp_path: Path) -> None:
    build_repository(
        tmp_path,
        current_versions=(2, 1),
        readiness_versions=(1, 1),
    )
    service = ReleaseDetectionService(
        RepositoryPaths(tmp_path),
        FakeGit((Path(f"registry/changesets/{CHANGESET_ID}.json"),)),
    )

    with pytest.raises(WorkflowConflict, match="CHANGESET_VERSION_NOT_CURRENT"):
        service.run(request(tmp_path))


def test_detect_defers_changeset_when_all_readiness_versions_are_future(
    tmp_path: Path,
) -> None:
    build_repository(
        tmp_path,
        current_versions=(1, 1),
        readiness_versions=(2, 2),
    )
    service = ReleaseDetectionService(
        RepositoryPaths(tmp_path),
        FakeGit((Path(f"registry/changesets/{CHANGESET_ID}.json"),)),
    )

    result = service.run(request(tmp_path))

    assert result.status is WorkflowStatus.NOOP
    assert result.outputs["products"] == []
    assert result.outputs["tables"] == []


def test_detect_defers_whole_changeset_when_one_product_is_future(
    tmp_path: Path,
) -> None:
    build_repository(
        tmp_path,
        current_versions=(2, 1),
        readiness_versions=(2, 2),
    )
    service = ReleaseDetectionService(
        RepositoryPaths(tmp_path),
        FakeGit(
            (
                Path("products/sales-order/generated/ossie-model.json"),
                Path(f"registry/tables/{TABLE_ID}.json"),
            )
        ),
    )

    result = service.run(request(tmp_path))

    assert result.status is WorkflowStatus.NOOP
    assert result.outputs["products"] == []
    assert result.outputs["tables"] == []


def test_detect_direct_product_from_current_generated_artifact(tmp_path: Path) -> None:
    build_repository(tmp_path)
    product = tmp_path / "products" / "sales-order"
    (product / "product.yaml").write_text(
        "\n".join(
            (
                f"product_id: {SALES_ID}",
                "product_key: sales-order",
                "version: 1",
                "changeset_id:",
                "",
            )
        ),
        encoding="utf-8",
    )
    service = ReleaseDetectionService(
        RepositoryPaths(tmp_path),
        FakeGit((Path("products/sales-order/generated/ossie-model.json"),)),
    )

    result = service.run(request(tmp_path))

    assert result.outputs["products"] == ["sales-order"]
    assert result.outputs["tables"] == []


def test_detect_rejects_deleted_current_artifact(tmp_path: Path) -> None:
    build_repository(tmp_path)
    deleted = tmp_path / "products" / "sales-order" / "generated" / "missing.json"
    service = ReleaseDetectionService(
        RepositoryPaths(tmp_path),
        FakeGit((deleted.relative_to(tmp_path),)),
    )

    with pytest.raises(WorkflowConflict, match="RELEASE_ARTIFACT_DELETED"):
        service.run(request(tmp_path))


def test_detect_empty_range_is_successful_noop(tmp_path: Path) -> None:
    build_repository(tmp_path)

    result = ReleaseDetectionService(RepositoryPaths(tmp_path), FakeGit(())).run(
        request(tmp_path)
    )

    assert result.status is WorkflowStatus.NOOP
    assert result.outputs["products"] == []
    assert result.outputs["tables"] == []
