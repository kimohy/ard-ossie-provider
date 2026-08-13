from __future__ import annotations

from typer.testing import CliRunner

from ard_ossie.cli import app


def test_console_entrypoint_exports_all_command_groups() -> None:
    """Splitting the module must not hide old groups or delay later lifecycle groups."""
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    for group in (
        "registry",
        "impact",
        "changeset",
        "release",
        "parse",
        "model",
        "validate",
        "github",
        "llm",
        "workflow",
    ):
        assert group in result.stdout


def test_existing_process_command_remains_compatible() -> None:
    """The package refactor must preserve public process options for one release."""
    result = CliRunner().invoke(app, ["process", "--help"])

    assert result.exit_code == 0, result.output
    assert "--warnings-as-errors" in result.stdout
    assert "--pr-number" in result.stdout


def test_existing_root_history_commands_remain_registered() -> None:
    """History, show, and diff are root commands rather than accidental module globals."""
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("history", "show", "diff"):
        assert command in result.stdout


def test_parse_product_is_the_canonical_product_document_command() -> None:
    """Workflow callers must be able to use the documented `ard parse product` spelling."""
    result = CliRunner().invoke(app, ["parse", "product", "--help"])

    assert result.exit_code == 0, result.output
    assert "--output" in result.stdout
    assert "--evidence" in result.stdout
