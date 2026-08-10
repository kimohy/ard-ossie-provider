from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.application.contracts import WorkflowSecurityError
from ard_ossie.application.modeling import ModelingService
from ard_ossie.cli import app
from ard_ossie.pipeline import process_product
from tests.integration.test_cli_process import create_product_fixture


def snapshot_tree(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_parse_dictionary_command_writes_structured_json(tmp_path: Path) -> None:
    """The CLI must expose the cell-preserving parser without running the full pipeline."""
    product = create_product_fixture(tmp_path)
    dictionary = product / "sources" / "dictionary" / "dictionary.xlsx"
    output = tmp_path / "parsed" / "dictionary.json"

    result = CliRunner().invoke(
        app,
        [
            "parse",
            "dictionary",
            str(dictionary.relative_to(tmp_path)),
            "--output",
            str(output.relative_to(tmp_path)),
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["parser_kind"] == "cell-preserving-excel"
    assert payload["tables"][0]["columns"][0]["name"] == "order_id"


def test_model_build_is_reproducible_and_does_not_mutate_source_or_registry(
    tmp_path: Path,
) -> None:
    """Preview builds must be byte-stable and isolated from authoritative state."""
    product = create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
    process_product(product, registry_root=registry)
    product_before = snapshot_tree(product)
    registry_before = snapshot_tree(registry)
    output = tmp_path / ".ard" / "previews" / "model-build"
    arguments = [
        "model",
        "build",
        str(product.relative_to(tmp_path)),
        "--registry",
        str(registry.relative_to(tmp_path)),
        "--staging-output",
        str(output.relative_to(tmp_path)),
        "--repository",
        str(tmp_path),
        "--no-llm",
    ]

    first = CliRunner().invoke(app, arguments)
    first_output = snapshot_tree(output)
    second = CliRunner().invoke(app, arguments)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert snapshot_tree(output) == first_output
    assert set(first_output) == {
        ".ard-preview-owned.json",
        "data-dictionary.json",
        "data-product.md",
        "data-semantic.md",
        "ossie-model.json",
        "source-manifest.json",
    }
    assert snapshot_tree(product) == product_before
    assert snapshot_tree(registry) == registry_before


@pytest.mark.parametrize("staging_output", [".", "products", ".ard", ".ard/staging"])
def test_model_build_rejects_output_that_could_replace_repository_state(
    tmp_path: Path,
    staging_output: str,
) -> None:
    """A preview destination must be an isolated leaf outside source and runtime state."""
    product = create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
    process_product(product, registry_root=registry)
    before = snapshot_tree(tmp_path)

    with pytest.raises(WorkflowSecurityError) as raised:
        ModelingService(RepositoryPaths(tmp_path)).build(
            product.relative_to(tmp_path),
            registry.relative_to(tmp_path),
            staging_output,
        )

    assert raised.value.code == "STAGING_OUTPUT_OVERLAPS_AUTHORITATIVE_STATE"
    assert snapshot_tree(tmp_path) == before


@pytest.mark.parametrize(
    "staging_output",
    ["products/other-product", "src", "existing-directory"],
)
def test_model_build_never_replaces_an_existing_unowned_directory(
    tmp_path: Path,
    staging_output: str,
) -> None:
    """Only a command-owned preview leaf may be atomically replaced on retry."""
    product = create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
    process_product(product, registry_root=registry)
    unowned = tmp_path / staging_output
    unowned.mkdir(parents=True)
    (unowned / "sentinel.txt").write_text("authoritative\n", encoding="utf-8")
    before = snapshot_tree(tmp_path)

    with pytest.raises(WorkflowSecurityError):
        ModelingService(RepositoryPaths(tmp_path)).build(
            product.relative_to(tmp_path),
            registry.relative_to(tmp_path),
            staging_output,
        )

    assert snapshot_tree(tmp_path) == before


def test_model_build_requires_ownership_marker_for_existing_preview_leaf(
    tmp_path: Path,
) -> None:
    """Even inside the preview runtime root, an unowned directory is authoritative."""
    product = create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
    process_product(product, registry_root=registry)
    unowned = tmp_path / ".ard" / "previews" / "unowned"
    unowned.mkdir(parents=True)
    (unowned / "sentinel.txt").write_text("preserve\n", encoding="utf-8")
    before = snapshot_tree(tmp_path)

    with pytest.raises(WorkflowSecurityError) as raised:
        ModelingService(RepositoryPaths(tmp_path)).build(
            product.relative_to(tmp_path),
            registry.relative_to(tmp_path),
            unowned.relative_to(tmp_path),
        )

    assert raised.value.code == "STAGING_OUTPUT_NOT_OWNED"
    assert snapshot_tree(tmp_path) == before


def test_validate_product_is_read_only(tmp_path: Path) -> None:
    """Validation findings must never promote temporary artifacts or Registry records."""
    product = create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
    process_product(product, registry_root=registry)
    product_before = snapshot_tree(product)
    registry_before = snapshot_tree(registry)

    result = CliRunner().invoke(
        app,
        [
            "validate",
            "product",
            str(product.relative_to(tmp_path)),
            "--registry",
            str(registry.relative_to(tmp_path)),
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert snapshot_tree(product) == product_before
    assert snapshot_tree(registry) == registry_before


def test_parse_command_rejects_traversal_with_security_exit_and_redacted_result(
    tmp_path: Path,
) -> None:
    """A path outside the explicit repository root must fail before parsing."""
    repository = tmp_path / "repo"
    repository.mkdir()
    outside = tmp_path / "outside.xlsx"
    outside.write_bytes(b"not read")

    result = CliRunner().invoke(
        app,
        [
            "parse",
            "dictionary",
            "../outside.xlsx",
            "--output",
            "parsed.json",
            "--repository",
            str(repository),
        ],
    )

    assert result.exit_code == 50
    envelope = json.loads(
        (repository / ".ard" / "run" / "parse.dictionary-result.json").read_text()
    )
    assert envelope["findings"][0]["code"] == "PATH_OUTSIDE_REPOSITORY"
    assert str(tmp_path) not in json.dumps(envelope)


def test_parse_command_rejects_symlink_input_with_security_exit(tmp_path: Path) -> None:
    """A source symlink inside the root must not redirect the parser outside it."""
    repository = tmp_path / "repo"
    repository.mkdir()
    outside = tmp_path / "outside.xlsx"
    outside.write_bytes(b"not read")
    (repository / "dictionary.xlsx").symlink_to(outside)

    result = CliRunner().invoke(
        app,
        [
            "parse",
            "dictionary",
            "dictionary.xlsx",
            "--output",
            "parsed.json",
            "--repository",
            str(repository),
        ],
    )

    assert result.exit_code == 50
    envelope = json.loads(
        (repository / ".ard" / "run" / "parse.dictionary-result.json").read_text()
    )
    assert envelope["findings"][0]["code"] == "SYMLINK_NOT_ALLOWED"


def test_model_build_validation_failure_writes_versioned_envelope(tmp_path: Path) -> None:
    """A malformed preview input must fail with the stable validation code and no output."""
    product = create_product_fixture(tmp_path, valid_dictionary=False)
    registry = tmp_path / "registry"
    registry.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "model",
            "build",
            str(product.relative_to(tmp_path)),
            "--registry",
            str(registry.relative_to(tmp_path)),
            "--staging-output",
            ".ard/previews/model-build",
            "--repository",
            str(tmp_path),
            "--no-llm",
        ],
    )

    assert result.exit_code == 10
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "model.build-result.json").read_text()
    )
    assert envelope["schema_version"] == 1
    assert envelope["findings"][0]["code"] == "MISSING_DICTIONARY_HEADERS"
    assert not (tmp_path / ".ard" / "previews" / "model-build").exists()
