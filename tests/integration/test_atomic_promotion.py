from __future__ import annotations

import errno
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import ard_ossie.pipeline as pipeline_module
from ard_ossie.cli import app
from ard_ossie.docling_parser import ParsedDocument
from ard_ossie.ingestion import SourceFile, SourceRole
from ard_ossie.pipeline import PipelineSecurityError, PipelineValidationError, process_product
from ard_ossie.semantic.adjudication import DecisionReport
from ard_ossie.semantic.pipeline_v2 import (
    SemanticPipelineResult,
    canonical_fidelity_report,
)
from ard_ossie.semantic.replay import (
    SemanticReplayBaseline,
    SemanticReplayCatalog,
    semantic_replay_identity,
)
from scripts.verify_issue_3_semantic import ReplayCandidateProvider, run_evidence_replay
from tests.integration.test_cli_process import (
    DatasetSafetyProvider,
    FidelityParser,
    add_complete_dictionary_descriptions,
    configure_metric_safety_fixture,
    create_product_fixture,
    degraded_fidelity_report,
)


class ReplayMismatchParser(FidelityParser):
    def __init__(self, result: SemanticPipelineResult) -> None:
        super().__init__(
            canonical_fidelity_report(result.evidence, result.canonical, result.validation)
        )
        self.result = result

    def parse(self, source: SourceFile) -> ParsedDocument:
        if source.role is SourceRole.PRODUCT_HTML:
            return super().parse(source)
        return ParsedDocument(
            role=source.role,
            source_hash=source.sha256,
            markdown=self.result.markdown,
            semantic_fidelity=self.fidelity,
            semantic_validation=self.result.validation,
            semantic_pipeline_result=self.result,
        )


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


def test_semantic_replay_mismatch_writes_diagnostics_without_promotion(
    tmp_path: Path,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    registry = tmp_path / "registry"
    process_product(product, registry_root=registry)
    before = {
        "generated": tree_hash(product / "generated"),
        "registry": tree_hash(registry),
    }
    config_path = product / "product.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["operation"] = "update"
    config["base_version"] = 1
    config["version"] = 2
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    product_html = product / "sources" / "product-info" / "product.html"
    product_html.write_text(
        product_html.read_text(encoding="utf-8").replace(
            "Order analytics",
            "Order replay analytics",
        ),
        encoding="utf-8",
    )
    decisions = DecisionReport.model_validate_json(
        Path("products/500138301/quality/decision-report.json").read_text(encoding="utf-8")
    )
    catalog = SemanticReplayCatalog.build(
        (
            SemanticReplayBaseline(
                product_key="500138301",
                identity=semantic_replay_identity(decisions),
                canonical_markdown=b"different-but-hash-verified-base\n",
                decisions=decisions,
            ),
        )
    )
    failed, provider = run_evidence_replay(
        Path("tests/fixtures/semantic/issue-3-evidence.json"),
        trusted_semantic_replay_catalog=catalog,
        provider=_StoredDecisionIdentityProvider(),
    )

    assert provider.calls == 0
    assert failed.validation.status == "failed"
    with pytest.raises(
        PipelineValidationError,
        match="SEMANTIC_SOURCE_REPLAY_MISMATCH",
    ):
        process_product(
            product,
            registry_root=registry,
            parser=ReplayMismatchParser(failed),
        )

    assert tree_hash(product / "generated") == before["generated"]
    assert tree_hash(registry) == before["registry"]
    quality = json.loads((product / "quality/quality-report.json").read_text())
    validation = json.loads((product / "quality/validation-report.json").read_text())
    application = json.loads((product / "quality/application-report.json").read_text())
    assert [item["code"] for item in quality["hard_errors"]] == ["SEMANTIC_SOURCE_REPLAY_MISMATCH"]
    assert validation["publishable"] is False
    assert "SEMANTIC_SOURCE_REPLAY_MISMATCH" in application["applications"][0]["invariant_codes"]


class _StoredDecisionIdentityProvider(ReplayCandidateProvider):
    def capabilities(self) -> dict[str, object]:
        return {
            "provider": "openai_compatible",
            "model": "gpt-5.6-terra",
            "structured_output": "json_schema",
        }

    def generate_structured(self, **_kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("trusted same-source decisions must be reused")


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


def test_first_registry_promotion_failure_restores_absent_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later promotion failure must remove a newly installed first registry."""
    product = create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
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
            raise OSError("simulated first promotion failure")
        return real_replace(source, destination)

    monkeypatch.setattr(pipeline_module.os, "replace", fail_generated_install)

    with pytest.raises(OSError, match="simulated first promotion failure"):
        process_product(product, registry_root=registry)

    assert not registry.exists()
    assert not (product / "generated").exists()
    assert not (product / "quality").exists()


def test_existing_registry_is_not_dereferenced_again_after_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidate construction must use a stable snapshot, not the authoritative path."""
    product = create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
    process_product(product, registry_root=registry)
    config_path = product / "product.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["operation"] = "update"
    config["base_version"] = 1
    config["version"] = 2
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    product_html = product / "sources" / "product-info" / "product.html"
    product_html.write_text(
        product_html.read_text(encoding="utf-8").replace(
            "Order analytics",
            "Order snapshot analytics",
        ),
        encoding="utf-8",
    )
    real_copytree = pipeline_module.shutil.copytree

    def reject_authoritative_registry_copy(source, destination, *args, **kwargs):
        if Path(source) == registry:
            raise AssertionError("authoritative registry was dereferenced after validation")
        return real_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(
        pipeline_module.shutil,
        "copytree",
        reject_authoritative_registry_copy,
    )

    result = process_product(product, registry_root=registry)

    assert result.product_version == 2


def test_registry_snapshot_uses_portable_path_when_nofollow_flags_are_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows-compatible processing must support both absent and existing registries."""
    product = create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
    monkeypatch.setattr(
        pipeline_module,
        "_secure_registry_directory_fd_supported",
        lambda: False,
    )

    first = process_product(product, registry_root=registry)
    second = process_product(product, registry_root=registry)

    assert first.product_version == 1
    assert second.product_version == 1
    assert (registry / "products" / f"{first.product_id}.json").is_file()


def test_portable_registry_snapshot_rejects_nested_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows-compatible fallback must retain the registry symlink boundary."""
    product = create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
    process_product(product, registry_root=registry)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (registry / "linked.json").symlink_to(outside)
    monkeypatch.setattr(
        pipeline_module,
        "_secure_registry_directory_fd_supported",
        lambda: False,
    )

    with pytest.raises(PipelineSecurityError, match="SYMLINK_NOT_ALLOWED"):
        process_product(product, registry_root=registry)


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


def test_warnings_as_errors_blocks_promotion_but_preserves_quality_evidence(
    tmp_path: Path,
) -> None:
    """Strict processing must decide warning policy before Registry/generated promotion."""
    product = create_product_fixture(tmp_path)
    registry = tmp_path / "registry"

    with pytest.raises(PipelineValidationError, match="WARNINGS_AS_ERRORS"):
        process_product(
            product,
            registry_root=registry,
            warnings_as_errors=True,
        )

    assert not (product / "generated").exists()
    assert not (registry / "products").exists()
    report = json.loads((product / "quality" / "quality-report.json").read_text())
    assert report["hard_errors"][0]["code"] == "WARNINGS_AS_ERRORS"


def test_semantic_structure_warning_blocks_strict_promotion(tmp_path: Path) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    registry = tmp_path / "registry"

    with pytest.raises(PipelineValidationError, match="WARNINGS_AS_ERRORS"):
        process_product(
            product,
            registry_root=registry,
            parser=FidelityParser(degraded_fidelity_report()),
            warnings_as_errors=True,
        )

    assert not (product / "generated").exists()
    assert not registry.exists()
    fidelity_path = product / "quality" / "semantic-fidelity.json"
    report = json.loads((product / "quality" / "quality-report.json").read_text())
    assert [finding["code"] for finding in report["warnings"]] == ["SEMANTIC_STRUCTURE_DEGRADED"]
    assert report["status"] == "FAIL"
    assert (
        report["quality_artifact_hashes"][fidelity_path.name]
        == hashlib.sha256(fidelity_path.read_bytes()).hexdigest()
    )


def _process_semantic_strict_failure(product: Path, registry: Path) -> None:
    process_product(
        product,
        registry_root=registry,
        parser=FidelityParser(degraded_fidelity_report()),
        warnings_as_errors=True,
    )


def test_validation_failure_rejects_symlinked_quality_directory_before_mutation(
    tmp_path: Path,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("trusted", encoding="utf-8")
    (product / "quality").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PipelineSecurityError, match="SYMLINK_NOT_ALLOWED"):
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert sentinel.read_text(encoding="utf-8") == "trusted"
    assert {path.name for path in outside.iterdir()} == {"sentinel.txt"}


def test_validation_failure_rejects_non_directory_quality_path(tmp_path: Path) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    quality = product / "quality"
    quality.write_text("trusted", encoding="utf-8")

    with pytest.raises(PipelineSecurityError, match="READ_PATH_TYPE_NOT_ALLOWED"):
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert quality.read_text(encoding="utf-8") == "trusted"


@pytest.mark.parametrize(
    "name",
    [
        "quality-report.json",
        "duplicate-report.json",
        "version-report.json",
        "impact-report.json",
        "llm-suggestions.json",
        "semantic-fidelity.json",
        "semantic-structure-repair.json",
    ],
)
def test_validation_failure_rejects_symlinked_quality_child_before_mutation(
    tmp_path: Path,
    name: str,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    quality = product / "quality"
    quality.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("trusted", encoding="utf-8")
    (quality / name).symlink_to(outside)

    with pytest.raises(PipelineSecurityError, match="SYMLINK_NOT_ALLOWED"):
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert outside.read_text(encoding="utf-8") == "trusted"
    assert (quality / name).is_symlink()


def test_validation_failure_rejects_dangling_optional_repair_symlink(
    tmp_path: Path,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    quality = product / "quality"
    quality.mkdir()
    repair = quality / "semantic-structure-repair.json"
    repair.symlink_to(tmp_path / "missing-repair.json")

    with pytest.raises(PipelineSecurityError, match="SYMLINK_NOT_ALLOWED"):
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert repair.is_symlink()


@pytest.mark.parametrize(
    "name",
    ["semantic-fidelity.json", "semantic-structure-repair.json"],
)
def test_validation_failure_rejects_non_regular_quality_child(
    tmp_path: Path,
    name: str,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    destination = product / "quality" / name
    destination.mkdir(parents=True)

    with pytest.raises(PipelineSecurityError, match="READ_PATH_TYPE_NOT_ALLOWED"):
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert destination.is_dir()


def test_validation_failure_portable_write_rejects_dangling_quality_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    quality = product / "quality"
    quality.mkdir()
    fidelity = quality / "semantic-fidelity.json"
    fidelity.symlink_to(tmp_path / "missing-fidelity.json")
    monkeypatch.setattr(
        pipeline_module,
        "_secure_quality_directory_fd_supported",
        lambda: False,
    )

    with pytest.raises(PipelineSecurityError, match="SYMLINK_NOT_ALLOWED"):
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert fidelity.is_symlink()


def test_validation_failure_portable_write_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    monkeypatch.setattr(
        pipeline_module,
        "_secure_quality_directory_fd_supported",
        lambda: False,
    )

    with pytest.raises(PipelineSecurityError, match="SECURE_QUALITY_WRITE_UNAVAILABLE"):
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert not (product / "quality").exists()


def test_validation_failure_fails_before_staging_when_replace_dir_fd_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    monkeypatch.setattr(
        pipeline_module,
        "_secure_quality_replace_dir_fd_supported",
        lambda: False,
        raising=False,
    )

    with pytest.raises(PipelineSecurityError, match="SECURE_QUALITY_WRITE_UNAVAILABLE"):
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert not (product / "quality").exists()


def test_validation_failure_rejects_quality_directory_replacement_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    quality = product / "quality"
    quality.mkdir()
    moved = tmp_path / "moved-quality"
    real_open = pipeline_module.os.open
    replaced = False

    def replace_quality_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if not replaced and path == "quality" and kwargs.get("dir_fd") is not None:
            replaced = True
            pipeline_module.os.replace(quality, moved)
            quality.mkdir()
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(pipeline_module.os, "open", replace_quality_before_open)

    with pytest.raises(PipelineSecurityError, match="QUALITY_PATH_CHANGED"):
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert replaced
    assert list(quality.iterdir()) == []
    assert list(moved.iterdir()) == []


def test_validation_failure_classifies_quality_symlink_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    quality = product / "quality"
    quality.mkdir()
    moved = tmp_path / "moved-quality"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("trusted", encoding="utf-8")
    real_open = pipeline_module.os.open
    replaced = False

    def replace_quality_with_symlink_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if not replaced and path == "quality" and kwargs.get("dir_fd") is not None:
            replaced = True
            pipeline_module.os.replace(quality, moved)
            quality.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(
        pipeline_module.os,
        "open",
        replace_quality_with_symlink_before_open,
    )

    with pytest.raises(PipelineSecurityError, match="SYMLINK_NOT_ALLOWED"):
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert replaced
    assert sentinel.read_text(encoding="utf-8") == "trusted"
    assert {path.name for path in outside.iterdir()} == {"sentinel.txt"}


def test_validation_failure_anchors_product_ancestor_against_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    moved = tmp_path / "moved-product"
    outside = tmp_path / "outside-product"
    (outside / "quality").mkdir(parents=True)
    sentinel = outside / "quality" / "sentinel.txt"
    sentinel.write_text("trusted", encoding="utf-8")
    quality = product / "quality"
    real_open = pipeline_module.os.open
    swapped = False
    product_open_count = 0

    def swap_product_before_quality_open(path, flags, *args, **kwargs):
        nonlocal product_open_count, swapped
        candidate = Path(path)
        if candidate == Path(product.name) and kwargs.get("dir_fd") is not None:
            product_open_count += 1
        if not swapped and (candidate == quality or product_open_count >= 2):
            swapped = True
            pipeline_module.os.replace(product, moved)
            product.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(pipeline_module.os, "open", swap_product_before_quality_open)

    with pytest.raises(PipelineSecurityError, match="SYMLINK_NOT_ALLOWED"):
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert swapped
    assert sentinel.read_text(encoding="utf-8") == "trusted"
    assert {path.name for path in (outside / "quality").iterdir()} == {"sentinel.txt"}


@pytest.mark.parametrize("error_number", [errno.EIO, errno.EACCES])
def test_validation_failure_preserves_product_ancestor_open_io_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    real_open = pipeline_module.os.open
    product_open_count = 0

    def fail_product_ancestor_open(path, flags, *args, **kwargs):
        nonlocal product_open_count
        if path == product.name and kwargs.get("dir_fd") is not None:
            product_open_count += 1
        if product_open_count >= 2 and path == product.name:
            raise OSError(error_number, "simulated product ancestor open failure")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(pipeline_module.os, "open", fail_product_ancestor_open)

    with pytest.raises(OSError) as captured:
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert captured.value.errno == error_number
    assert not (product / "quality").exists()


def test_validation_failure_cleans_staged_files_after_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    quality = product / "quality"
    quality.mkdir()
    moved = tmp_path / "moved-quality"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("trusted", encoding="utf-8")
    real_fsync = pipeline_module.os.fsync
    swapped = False

    def swap_quality_after_first_staged_write(descriptor):
        nonlocal swapped
        if not swapped:
            opened = Path(f"/proc/self/fd/{descriptor}")
            if opened.exists() and opened.resolve().parent == quality:
                swapped = True
                pipeline_module.os.replace(quality, moved)
                quality.symlink_to(outside, target_is_directory=True)
        return real_fsync(descriptor)

    monkeypatch.setattr(
        pipeline_module.os,
        "fsync",
        swap_quality_after_first_staged_write,
    )

    with pytest.raises(PipelineSecurityError, match="SYMLINK_NOT_ALLOWED"):
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert swapped
    assert list(moved.iterdir()) == []
    assert sentinel.read_text(encoding="utf-8") == "trusted"
    assert {path.name for path in outside.iterdir()} == {"sentinel.txt"}


def test_validation_failure_stage_io_error_cleans_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    quality = product / "quality"
    quality.mkdir()
    real_write = pipeline_module.os.write
    failed = False

    def fail_first_quality_write(descriptor, payload):
        nonlocal failed
        opened = Path(f"/proc/self/fd/{descriptor}")
        if not failed and opened.exists() and opened.resolve().parent == quality:
            failed = True
            raise OSError("simulated quality write failure")
        return real_write(descriptor, payload)

    monkeypatch.setattr(pipeline_module.os, "write", fail_first_quality_write)

    with pytest.raises(OSError, match="simulated quality write failure"):
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert failed
    assert list(quality.iterdir()) == []


@pytest.mark.parametrize("error_number", [errno.EIO, errno.EACCES])
def test_validation_failure_preserves_initial_quality_stat_io_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    quality = product / "quality"
    quality.mkdir()
    real_stat = pipeline_module.os.stat

    def fail_quality_stat(path, *args, **kwargs):
        if path == "quality" and kwargs.get("dir_fd") is not None:
            raise OSError(error_number, "simulated initial quality stat failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(pipeline_module.os, "stat", fail_quality_stat)

    with pytest.raises(OSError) as captured:
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert captured.value.errno == error_number


@pytest.mark.parametrize("error_number", [errno.EIO, errno.EACCES])
def test_validation_failure_preserves_child_lstat_io_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    quality = product / "quality"
    quality.mkdir()
    real_stat = pipeline_module.os.stat

    def fail_fidelity_stat(path, *args, **kwargs):
        if path == "semantic-fidelity.json" and kwargs.get("dir_fd") is not None:
            raise OSError(error_number, "simulated child lstat failure")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(pipeline_module.os, "stat", fail_fidelity_stat)

    with pytest.raises(OSError) as captured:
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert captured.value.errno == error_number
    assert list(quality.iterdir()) == []


@pytest.mark.parametrize("error_number", [errno.ELOOP, errno.ENOTDIR])
def test_validation_failure_classifies_open_path_race_after_aba_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    quality = product / "quality"
    quality.mkdir()
    real_open = pipeline_module.os.open

    def fail_quality_open(path, flags, *args, **kwargs):
        if path == "quality" and kwargs.get("dir_fd") is not None:
            raise OSError(error_number, "simulated no-follow open race")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(pipeline_module.os, "open", fail_quality_open)

    with pytest.raises(PipelineSecurityError, match="QUALITY_PATH_CHANGED"):
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert list(quality.iterdir()) == []


@pytest.mark.parametrize("replacement_kind", ["directory", "symlink"])
def test_validation_failure_classifies_child_change_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    quality = product / "quality"
    quality.mkdir()
    destination_name = "duplicate-report.json"
    destination = quality / destination_name
    destination.write_text("trusted", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("trusted outside", encoding="utf-8")
    real_replace = pipeline_module.os.replace
    real_unlink = pipeline_module.os.unlink
    changed = False

    def change_destination_before_replace(source, target, *args, **kwargs):
        nonlocal changed
        if not changed and target == destination_name and kwargs.get("dst_dir_fd") is not None:
            changed = True
            descriptor = kwargs["dst_dir_fd"]
            real_unlink(destination_name, dir_fd=descriptor)
            if replacement_kind == "directory":
                pipeline_module.os.mkdir(destination_name, dir_fd=descriptor)
            else:
                pipeline_module.os.symlink(
                    outside,
                    destination_name,
                    dir_fd=descriptor,
                )
            raise OSError(errno.EIO, "simulated replace race")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(pipeline_module.os, "replace", change_destination_before_replace)

    expected_code = (
        "READ_PATH_TYPE_NOT_ALLOWED" if replacement_kind == "directory" else "SYMLINK_NOT_ALLOWED"
    )
    with pytest.raises(PipelineSecurityError, match=expected_code):
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert changed
    assert outside.read_text(encoding="utf-8") == "trusted outside"
    assert not any(path.name.endswith(".tmp") for path in quality.iterdir())


def test_validation_failure_preserves_unrelated_replace_io_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    quality = product / "quality"
    quality.mkdir()
    destination_name = "duplicate-report.json"
    (quality / destination_name).write_text("trusted", encoding="utf-8")
    real_replace = pipeline_module.os.replace
    failed = False

    def fail_quality_replace(source, target, *args, **kwargs):
        nonlocal failed
        if not failed and target == destination_name and kwargs.get("dst_dir_fd") is not None:
            failed = True
            raise OSError(errno.EIO, "simulated unrelated replace failure")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(pipeline_module.os, "replace", fail_quality_replace)

    with pytest.raises(OSError) as captured:
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert captured.value.errno == errno.EIO
    assert (quality / destination_name).read_text(encoding="utf-8") == "trusted"
    assert not any(path.name.endswith(".tmp") for path in quality.iterdir())


def test_validation_failure_classifies_optional_repair_change_before_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    quality = product / "quality"
    quality.mkdir()
    repair_name = "semantic-structure-repair.json"
    (quality / repair_name).write_text("trusted repair", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("trusted outside", encoding="utf-8")
    real_unlink = pipeline_module.os.unlink
    changed = False

    def change_repair_before_unlink(path, *args, **kwargs):
        nonlocal changed
        if not changed and path == repair_name and kwargs.get("dir_fd") is not None:
            changed = True
            descriptor = kwargs["dir_fd"]
            real_unlink(repair_name, dir_fd=descriptor)
            pipeline_module.os.symlink(outside, repair_name, dir_fd=descriptor)
            raise OSError(errno.EIO, "simulated unlink race")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(pipeline_module.os, "unlink", change_repair_before_unlink)

    with pytest.raises(PipelineSecurityError, match="SYMLINK_NOT_ALLOWED"):
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert changed
    assert outside.read_text(encoding="utf-8") == "trusted outside"
    assert not any(path.name.endswith(".tmp") for path in quality.iterdir())


def test_validation_failure_preserves_unrelated_optional_repair_unlink_io_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = create_product_fixture(tmp_path)
    add_complete_dictionary_descriptions(product)
    quality = product / "quality"
    quality.mkdir()
    repair_name = "semantic-structure-repair.json"
    repair = quality / repair_name
    repair.write_text("trusted repair", encoding="utf-8")
    real_unlink = pipeline_module.os.unlink
    failed = False

    def fail_repair_unlink(path, *args, **kwargs):
        nonlocal failed
        if not failed and path == repair_name and kwargs.get("dir_fd") is not None:
            failed = True
            raise OSError(errno.EIO, "simulated unrelated unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(pipeline_module.os, "unlink", fail_repair_unlink)

    with pytest.raises(OSError) as captured:
        _process_semantic_strict_failure(product, tmp_path / "registry")

    assert captured.value.errno == errno.EIO
    assert repair.read_text(encoding="utf-8") == "trusted repair"
    assert not any(path.name.endswith(".tmp") for path in quality.iterdir())


def test_metric_exclusion_warning_blocks_update_promotion_in_strict_mode(
    tmp_path: Path,
) -> None:
    product = create_product_fixture(tmp_path)
    configure_metric_safety_fixture(product)
    registry = tmp_path / "registry"
    process_product(
        product,
        registry_root=registry,
        provider=DatasetSafetyProvider(),
    )
    before = {
        "registry": tree_hash(registry),
        "generated": tree_hash(product / "generated"),
    }
    config_path = product / "product.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["operation"] = "update"
    config["base_version"] = 1
    config["version"] = 2
    config["description"] = "Updated campaign and sales performance product."
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(PipelineValidationError, match="WARNINGS_AS_ERRORS"):
        process_product(
            product,
            registry_root=registry,
            provider=DatasetSafetyProvider(),
            warnings_as_errors=True,
        )

    assert tree_hash(registry) == before["registry"]
    assert tree_hash(product / "generated") == before["generated"]
    report = json.loads((product / "quality" / "quality-report.json").read_text())
    assert [finding["code"] for finding in report["hard_errors"]] == ["WARNINGS_AS_ERRORS"]
    assert "METRIC_MULTI_DATASET_UNSUPPORTED" in {finding["code"] for finding in report["warnings"]}
