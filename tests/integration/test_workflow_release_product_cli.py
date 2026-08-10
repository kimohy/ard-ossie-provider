from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import ard_ossie.cli.workflow as workflow_cli
from ard_ossie.application.contracts import WorkflowResult, WorkflowStatus
from ard_ossie.cli import app


class StubReleasePublicationService:
    def __init__(self) -> None:
        self.request = None

    def run(self, request):
        self.request = request
        return WorkflowResult(
            command="workflow.release-product",
            status=WorkflowStatus.SUCCESS,
            outputs={
                "product_id": "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631",
                "product_key": "sales-order",
                "version": 12,
                "product_tag": (
                    "product/prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631/v12"
                ),
                "commit": "a" * 40,
                "artifact_sha256": "b" * 64,
                "artifact_hashes": {"generated/ossie-model.json": "c" * 64},
            },
            artifacts=["dist/product.zip"],
        )


def test_workflow_release_product_maps_verified_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = StubReleasePublicationService()
    monkeypatch.setattr(
        workflow_cli,
        "_release_publication_service",
        lambda repository_name, paths: service,
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "release-product",
            "--product-key",
            "sales-order",
            "--current",
            "a" * 40,
            "--table-ids",
            '["tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"]',
            "--output",
            str(tmp_path / "dist"),
            "--repository-name",
            "owner/repository",
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert service.request.product_key == "sales-order"
    assert service.request.current == "a" * 40
    assert service.request.table_ids == [
        "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"
    ]
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.release-product-result.json").read_text()
    )
    assert envelope["outputs"]["version"] == 12
