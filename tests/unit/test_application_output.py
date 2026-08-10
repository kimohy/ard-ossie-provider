from __future__ import annotations

import json
from pathlib import Path

import pytest

from ard_ossie.application.contracts import WorkflowResult
from ard_ossie.application.output import ResultWriter


def test_result_writer_atomically_writes_result_outputs_and_summary(tmp_path: Path) -> None:
    """Missing any output surface would make local and Actions execution diverge."""
    result_path = tmp_path / ".ard" / "run" / "result.json"
    github_output = tmp_path / "github-output"
    github_summary = tmp_path / "summary.md"
    writer = ResultWriter(
        result_path=result_path,
        github_output=github_output,
        github_summary=github_summary,
    )

    writer.write(
        WorkflowResult(
            command="workflow.release-detect",
            status="success",
            outputs={
                "key": "sales-order",
                "products": ["finance-order", "sales-order"],
            },
            artifacts=["products/sales-order/generated/data-product.md"],
            findings=[{"code": "WARNING", "message": "review | details"}],
        )
    )

    assert json.loads(result_path.read_text(encoding="utf-8"))["schema_version"] == 1
    output_text = github_output.read_text(encoding="utf-8")
    assert "key=sales-order\n" in output_text
    assert 'products=["finance-order","sales-order"]\n' in output_text
    summary = github_summary.read_text(encoding="utf-8")
    assert "workflow.release-detect" in summary
    assert "| Findings | 1 |" in summary
    assert list(result_path.parent.glob("*.tmp")) == []


def test_result_writer_uses_collision_safe_multiline_github_output(tmp_path: Path) -> None:
    """A newline in a value must not create an attacker-controlled GitHub output key."""
    github_output = tmp_path / "github-output"
    writer = ResultWriter(
        result_path=tmp_path / "result.json",
        github_output=github_output,
    )

    writer.write(
        WorkflowResult(
            command="workflow.test",
            status="success",
            outputs={"message": "first\nARD_EOF\nsecond"},
        )
    )

    lines = github_output.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("message<<ARD_OUTPUT_")
    delimiter = lines[0].removeprefix("message<<")
    assert delimiter not in {"ARD_EOF", "first", "second"}
    assert lines[-1] == delimiter


def test_result_writer_skips_absent_github_files(tmp_path: Path) -> None:
    """Local CLI use must not require GitHub-specific output paths."""
    result_path = tmp_path / "result.json"

    ResultWriter(result_path=result_path).write(
        WorkflowResult(command="parse.dictionary", status="success")
    )

    assert json.loads(result_path.read_text(encoding="utf-8"))["status"] == "success"


def test_result_writer_validates_all_outputs_before_publishing_success(tmp_path: Path) -> None:
    """An invalid Actions output must not replace the last trustworthy result envelope."""
    result_path = tmp_path / "result.json"
    result_path.write_text("previous-result\n", encoding="utf-8")
    github_output = tmp_path / "github-output"
    github_output.write_text("previous-output\n", encoding="utf-8")

    with pytest.raises(ValueError, match="INVALID_GITHUB_OUTPUT_KEY"):
        ResultWriter(result_path=result_path, github_output=github_output).write(
            WorkflowResult(
                command="workflow.test",
                status="success",
                outputs={"valid": "value", "invalid key": "value"},
            )
        )

    assert result_path.read_text(encoding="utf-8") == "previous-result\n"
    assert github_output.read_text(encoding="utf-8") == "previous-output\n"


def test_result_writer_publishes_result_envelope_after_external_channels(
    tmp_path: Path,
) -> None:
    """An external channel failure must not leave a contradictory success envelope."""
    result_path = tmp_path / "result.json"
    github_output = tmp_path / "github-output"
    github_output.mkdir()

    with pytest.raises(IsADirectoryError):
        ResultWriter(result_path=result_path, github_output=github_output).write(
            WorkflowResult(
                command="workflow.test",
                status="success",
                outputs={"valid": "value"},
            )
        )

    assert not result_path.exists()
