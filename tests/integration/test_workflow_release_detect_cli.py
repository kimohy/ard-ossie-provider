from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import ard_ossie.cli.workflow as workflow_cli
from ard_ossie.application.contracts import WorkflowResult, WorkflowStatus
from ard_ossie.cli import app


class StubReleaseDetectionService:
    def __init__(self) -> None:
        self.request = None

    def run(self, request):
        self.request = request
        return WorkflowResult(
            command="workflow.release-detect",
            status=WorkflowStatus.SUCCESS,
            outputs={
                "products": ["finance-order", "sales-order"],
                "tables": ["tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"],
            },
        )


class StubNoopReleaseDetectionService:
    def __init__(self) -> None:
        self.request = None

    def run(self, request):
        self.request = request
        return WorkflowResult(
            command="workflow.release-detect",
            status=WorkflowStatus.NOOP,
            outputs={"products": [], "tables": []},
        )


def test_workflow_release_detect_writes_json_matrix_outputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = StubReleaseDetectionService()
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setattr(
        workflow_cli,
        "_release_detection_service",
        lambda paths: service,
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "release-detect",
            "--before",
            "a" * 40,
            "--current",
            "b" * 40,
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert service.request.before == "a" * 40
    assert service.request.current == "b" * 40
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.release-detect-result.json").read_text()
    )
    assert envelope["outputs"]["products"] == ["finance-order", "sales-order"]
    output = github_output.read_text(encoding="utf-8")
    assert 'products=["finance-order","sales-order"]\n' in output
    assert 'tables=["tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"]\n' in output


def test_workflow_release_detect_writes_empty_json_matrix_outputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = StubNoopReleaseDetectionService()
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setattr(
        workflow_cli,
        "_release_detection_service",
        lambda paths: service,
        raising=False,
    )

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "release-detect",
            "--before",
            "a" * 40,
            "--current",
            "b" * 40,
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert service.request.before == "a" * 40
    assert service.request.current == "b" * 40
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.release-detect-result.json").read_text()
    )
    assert envelope["status"] == "noop"
    assert envelope["outputs"] == {"products": [], "tables": []}
    output = github_output.read_text(encoding="utf-8")
    assert "products=[]\n" in output
    assert "tables=[]\n" in output
