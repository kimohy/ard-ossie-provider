from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import ard_ossie.pipeline as pipeline_module
from ard_ossie.cli import app
from ard_ossie.pipeline import process_product
from tests.integration.test_cli_process import create_product_fixture


def tree_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(directory).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_hard_quality_error_keeps_previous_generated_directory(tmp_path: Path) -> None:
    product = create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
    runner = CliRunner()
    first = runner.invoke(app, ["process", str(product), "--registry", str(registry)])
    assert first.exit_code == 0, first.output
    before = tree_hash(product / "generated")

    dictionary = product / "sources" / "dictionary" / "dictionary.xlsx"
    dictionary.write_bytes(b"not an xlsx")
    result = runner.invoke(app, ["process", str(product), "--registry", str(registry)])

    assert result.exit_code == 2
    assert tree_hash(product / "generated") == before


def test_promotion_failure_rolls_back_registry_generated_and_quality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
    process_product(product, registry_root=registry)
    before = {
        "registry": tree_hash(registry),
        "generated": tree_hash(product / "generated"),
        "quality": tree_hash(product / "quality"),
    }
    config_path = product / "product.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["operation"] = "update"
    config["base_version"] = 1
    config["version"] = 2
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    product_html = product / "sources" / "product-info" / "product.html"
    product_html.write_text(
        product_html.read_text(encoding="utf-8").replace("Order analytics", "Order insights"),
        encoding="utf-8",
    )
    real_replace = pipeline_module.os.replace
    failed = False

    def fail_generated_install(source, destination):
        nonlocal failed
        source_path = Path(source)
        if (
            not failed
            and source_path.name == "generated"
            and source_path.parent.name == "candidate"
        ):
            failed = True
            raise OSError("simulated promotion failure")
        return real_replace(source, destination)

    monkeypatch.setattr(pipeline_module.os, "replace", fail_generated_install)

    with pytest.raises(OSError, match="simulated promotion failure"):
        process_product(product, registry_root=registry)

    assert tree_hash(registry) == before["registry"]
    assert tree_hash(product / "generated") == before["generated"]
    assert tree_hash(product / "quality") == before["quality"]


def test_cli_preserves_detailed_hard_failure_report(tmp_path: Path) -> None:
    product = create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
    runner = CliRunner()
    assert runner.invoke(app, ["process", str(product), "--registry", str(registry)]).exit_code == 0
    config_path = product / "product.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["operation"] = "update"
    config["base_version"] = 1
    config["version"] = 3
    config["tables"][0]["base_version"] = 1
    config["tables"][0]["version"] = 2
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    result = runner.invoke(app, ["process", str(product), "--registry", str(registry)])
    report = json.loads((product / "quality" / "quality-report.json").read_text())

    assert result.exit_code == 2
    assert [item["code"] for item in report["hard_errors"]] == [
        "VERSION_GAP",
        "VERSION_NO_CHANGE",
    ]
