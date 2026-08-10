from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from pydantic import Field

from ard_ossie.application.contracts import WorkflowSecurityError, WorkflowValidationError
from ard_ossie.ingestion import SourceValidationError
from ard_ossie.models import StrictModel
from ard_ossie.pipeline import (
    PipelineValidationError,
    QualityFinding,
    QualityReport,
    process_product,
)
from ard_ossie.ports.filesystem import FileSystemPort

_PREVIEW_MARKER = ".ard-preview-owned.json"
_PREVIEW_MARKER_CONTENT = '{"owner":"ard-ossie:model-build","schema_version":1}\n'


class ModelingResult(StrictModel):
    generated_files: list[str]
    quality_report: QualityReport


class ValidationResult(StrictModel):
    passed: bool
    findings: list[QualityFinding] = Field(default_factory=list)
    quality_report: QualityReport | None = None


class ModelingService:
    def __init__(self, paths: FileSystemPort) -> None:
        self.paths = paths

    def build(
        self,
        product_path: str | Path,
        registry_path: str | Path,
        staging_output: str | Path,
    ) -> ModelingResult:
        product = self.paths.resolve_read(product_path)
        registry = self.paths.resolve_read(registry_path)
        output = self.paths.resolve_write(staging_output)
        self._validate_roots(product, registry, output)
        try:
            with self._staged_state(product, registry) as (staged_product, staged_registry):
                processed = process_product(
                    staged_product,
                    registry_root=staged_registry,
                    provider=None,
                )
                _replace_directory(processed.generated_dir, output)
                return ModelingResult(
                    generated_files=sorted(
                        path.name
                        for path in processed.generated_dir.iterdir()
                        if path.is_file()
                    ),
                    quality_report=processed.quality_report,
                )
        except (PipelineValidationError, SourceValidationError, ValueError) as error:
            code = str(error).partition(":")[0] or type(error).__name__
            raise WorkflowValidationError(code, "model build validation failed") from error

    def validate(
        self,
        product_path: str | Path,
        registry_path: str | Path,
    ) -> ValidationResult:
        product = self.paths.resolve_read(product_path)
        registry = self.paths.resolve_read(registry_path)
        self._validate_roots(product, registry)
        try:
            with self._staged_state(product, registry) as (staged_product, staged_registry):
                processed = process_product(
                    staged_product,
                    registry_root=staged_registry,
                    provider=None,
                )
        except PipelineValidationError as error:
            report = error.report
            findings = report.hard_errors if report is not None else [_finding(error)]
            return ValidationResult(passed=False, findings=findings, quality_report=report)
        except (SourceValidationError, ValueError) as error:
            return ValidationResult(passed=False, findings=[_finding(error)])
        return ValidationResult(
            passed=True,
            findings=[],
            quality_report=processed.quality_report,
        )

    def _validate_roots(
        self,
        product: Path,
        registry: Path,
        output: Path | None = None,
    ) -> None:
        if not product.is_dir() or not registry.is_dir():
            raise WorkflowSecurityError(
                "MODEL_INPUT_NOT_DIRECTORY",
                "product and registry must be directories",
            )
        _reject_tree_symlinks(product)
        _reject_tree_symlinks(registry)
        if output is not None:
            preview_root = self.paths.root / ".ard" / "previews"
            if output.parent != preview_root:
                raise WorkflowSecurityError(
                    "STAGING_OUTPUT_OVERLAPS_AUTHORITATIVE_STATE",
                    "staging output must be a direct child of .ard/previews",
                )
            if output.exists() and not _is_owned_preview(output):
                raise WorkflowSecurityError(
                    "STAGING_OUTPUT_NOT_OWNED",
                    "existing staging output was not created by model build",
                )

    def _staged_state(self, product: Path, registry: Path):
        staging_parent = self.paths.resolve_write(Path(".ard/staging"))
        staging_parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="ard-model-", dir=staging_parent)
        root = Path(temporary.name)
        staged_product = root / "product"
        staged_registry = root / "registry"
        shutil.copytree(product, staged_product)
        shutil.copytree(registry, staged_registry)

        class StagedState:
            def __enter__(self):
                return staged_product, staged_registry

            def __exit__(self, exc_type, exc_value, traceback):
                temporary.cleanup()

        return StagedState()


def _reject_tree_symlinks(root: Path) -> None:
    if any(path.is_symlink() for path in root.rglob("*")):
        raise WorkflowSecurityError("SYMLINK_NOT_ALLOWED", "input tree contains a symlink")


def _is_owned_preview(path: Path) -> bool:
    marker = path / _PREVIEW_MARKER
    if not path.is_dir() or marker.is_symlink() or not marker.is_file():
        return False
    try:
        return marker.read_text(encoding="utf-8") == _PREVIEW_MARKER_CONTENT
    except OSError:
        return False


def _replace_directory(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    candidate = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.candidate-",
            dir=destination.parent,
        )
    )
    backup: Path | None = None
    try:
        shutil.rmtree(candidate)
        shutil.copytree(source, candidate)
        (candidate / _PREVIEW_MARKER).write_text(
            _PREVIEW_MARKER_CONTENT,
            encoding="utf-8",
        )
        if destination.exists():
            backup = Path(
                tempfile.mkdtemp(prefix=f".{destination.name}.backup-", dir=destination.parent)
            )
            shutil.rmtree(backup)
            destination.replace(backup)
        candidate.replace(destination)
        if backup is not None:
            shutil.rmtree(backup)
    except Exception:
        if destination.exists() and backup is not None and backup.exists():
            shutil.rmtree(destination)
        if backup is not None and backup.exists():
            backup.replace(destination)
        raise
    finally:
        if candidate.exists():
            shutil.rmtree(candidate)


def _finding(error: Exception) -> QualityFinding:
    code = str(error).partition(":")[0] or type(error).__name__
    return QualityFinding(code=code, message=code)
