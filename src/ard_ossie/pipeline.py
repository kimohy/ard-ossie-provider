from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, model_validator
from sqlglot import Dialect, Parser, exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.annotate_types import annotate_types

from ard_ossie.canonical import canonical_hash, schema_hash
from ard_ossie.docling_parser import DoclingParser, Evidence, ParsedDocument
from ard_ossie.excel_adapter import DictionaryTable, ParsedDictionary, parse_dictionary
from ard_ossie.identity import DuplicateDecision, DuplicateReport, classify_product, classify_table
from ard_ossie.ids import new_id
from ard_ossie.impact import analyze_table_change
from ard_ossie.ingestion import SourceManifest, SourceRole, scan_sources
from ard_ossie.ir import (
    ColumnIR,
    MetricIR,
    ProductFactIR,
    ProductIR,
    RelationshipIR,
    TableIR,
)
from ard_ossie.llm import (
    AISuggestion,
    LLMProvider,
    MetricSuggestion,
    ProductFactSuggestion,
    ProviderExecutionError,
    ProviderFailureKind,
    semantic_extraction_schema,
    validate_semantic_suggestions,
)
from ard_ossie.models import (
    ColumnRecord,
    EntityStatus,
    MetricRecord,
    Operation,
    ProductId,
    ProductRecord,
    ProductTableRef,
    RelationshipRecord,
    Sha256,
    StrictModel,
    TableLocator,
    TableRecord,
    Version,
)
from ard_ossie.ossie_compiler import compile_ossie
from ard_ossie.registry import Registry
from ard_ossie.renderers import (
    render_dictionary_json,
    render_product_markdown,
    render_semantic_markdown,
)
from ard_ossie.versioning import VersionDecision, VersionOutcome, plan_version


class PipelineValidationError(ValueError):
    def __init__(self, message: str, *, report: QualityReport | None = None) -> None:
        super().__init__(message)
        self.report = report


class PipelineSecurityError(PipelineValidationError):
    pass


class QualityStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class QualityFinding(StrictModel):
    code: str
    message: str
    path: str | None = None


class QualityReport(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        title="ARD quality report",
        json_schema_extra={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": (
                "https://github.com/kimohy/ard-ossie-provider/"
                "schemas/reports/quality-report.schema.json"
            ),
        },
    )

    status: QualityStatus
    product_id: ProductId | None
    product_version: Version | None
    completeness: float = Field(ge=0, le=1)
    hard_errors: list[QualityFinding]
    warnings: list[QualityFinding]
    artifact_hashes: dict[str, Sha256]


class TableConfig(StrictModel):
    locator: str
    table_id: str | None = None
    version: Version | None = None
    base_version: Version | None = None
    usage: Literal["SOURCE", "OUTPUT", "REFERENCE"] = "SOURCE"
    required: bool = True

    @model_validator(mode="after")
    def validate_locator(self) -> TableConfig:
        _parse_locator(self.locator)
        return self


class ProductConfig(StrictModel):
    operation: Operation
    product_id: ProductId
    product_key: str
    version: Version
    base_version: Version | None = None
    display_name: str
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    changeset_id: str | None = None
    tables: list[TableConfig] = Field(default_factory=list)


class SuggestionBatch(StrictModel):
    suggestions: list[AISuggestion]
    metrics: list[MetricSuggestion]
    product_facts: list[ProductFactSuggestion]


class _PreparedMetrics(StrictModel):
    suggestions: list[MetricSuggestion]
    findings: list[QualityFinding]
    excluded_names: list[str]


class _MetricSqlParser(Parser):
    def _warn_unsupported(self) -> None:
        """Keep rejected provider SQL out of application logs."""


class ProcessResult(StrictModel):
    product_id: str
    product_version: Version
    generated_dir: Path
    quality_report: QualityReport


def process_product(
    product_path: str | Path,
    *,
    registry_root: str | Path,
    provider: LLMProvider | None = None,
    parser: DoclingParser | None = None,
    pr_number: int | None = None,
    warnings_as_errors: bool = False,
) -> ProcessResult:
    root = Path(product_path).resolve()
    registry_path = _validated_registry_path(registry_root)
    registry_initially_exists, registry_snapshot = _snapshot_registry(registry_path)
    registry = _load_registry_snapshot(registry_snapshot)
    config = _load_config(root / "product.yaml")
    manifest = scan_sources(root / "sources")
    active_parser = parser or DoclingParser()
    product_document = active_parser.parse(manifest.by_role(SourceRole.PRODUCT_HTML))
    semantic_document = active_parser.parse(manifest.by_role(SourceRole.SEMANTIC_DOCUMENT))
    dictionary_source = manifest.by_role(SourceRole.DICTIONARY_EXCEL)
    dictionary = parse_dictionary(dictionary_source.path, source_hash=dictionary_source.sha256)

    existing_product = _resolve_existing_product(config, registry)
    product_id = config.product_id
    table_drafts = _resolve_tables(config, dictionary, registry)
    configured_description = config.description
    suggestion_batch = SuggestionBatch(suggestions=[], metrics=[], product_facts=[])
    prepared_metrics = _PreparedMetrics(
        suggestions=[],
        findings=[],
        excluded_names=[],
    )
    product_facts = _validate_product_facts(
        [],
        product_document,
        configured_description=configured_description,
    )
    if provider is not None:
        _metric_dataset_catalog(table_drafts)
        try:
            suggestion_batch = _extract_suggestions(
                provider, product_document, semantic_document, table_drafts
            )
            product_facts = _validate_product_facts(
                suggestion_batch.product_facts,
                product_document,
                configured_description=configured_description,
            )
            prepared_metrics = _prepare_metrics(
                suggestion_batch.metrics,
                table_drafts,
            )
            _validate_metric_name_collisions(
                prepared_metrics.suggestions,
                existing_product,
                excluded_names=prepared_metrics.excluded_names,
            )
        except ProviderExecutionError as error:
            raise ProviderExecutionError(error.code, kind=error.kind) from None
        except ValidationError:
            raise ProviderExecutionError(
                "LLM_OUTPUT_VALIDATION_FAILED",
                kind=ProviderFailureKind.OUTPUT,
            ) from None
        except ValueError as error:
            code = str(error).partition(":")[0]
            if re.fullmatch(r"LLM_[A-Z0-9_]{1,123}", code) is None:
                code = "LLM_OUTPUT_VALIDATION_FAILED"
            raise ProviderExecutionError(
                code,
                kind=ProviderFailureKind.OUTPUT,
            ) from None
        except Exception:
            raise ProviderExecutionError(
                "LLM_PROVIDER_FAILURE",
                kind=ProviderFailureKind.TRANSIENT,
            ) from None
        config, table_drafts = _apply_suggestions(
            config, table_drafts, suggestion_batch.suggestions
        )

    source_hashes = {item.role.value: item.sha256 for item in manifest.files}
    table_records, table_irs, table_versions = _build_table_records(table_drafts, registry)
    relationships, relationship_records, relationship_findings = _build_relationships(
        table_drafts,
        existing_product,
    )
    metrics, metric_records = _build_metrics(
        prepared_metrics.suggestions,
        existing_product,
        excluded_names=prepared_metrics.excluded_names,
    )
    product_hash = canonical_hash(
        {
            "product_key": config.product_key,
            "display_name": config.display_name,
            "description": config.description,
            "synonyms": config.synonyms,
            "semantic_markdown": semantic_document.markdown,
            "source_hashes": source_hashes,
            "tables": [
                {"table_id": table.table_id, "table_version": table.table_version}
                for table in table_irs
            ],
            "relationships": relationships,
            "metrics": metrics,
        }
    )
    product_record = ProductRecord(
        product_id=product_id,
        product_key=config.product_key,
        version=config.version,
        display_name=config.display_name,
        aliases=existing_product.aliases if existing_product else [],
        canonical_hash=product_hash,
        metrics=metric_records,
        relationships=relationship_records,
    )
    duplicate_reports = _duplicate_reports(config, product_record, table_records, registry)
    product_changed = existing_product is None or existing_product.canonical_hash != product_hash
    product_version = plan_version(
        current_version=existing_product.version if existing_product else None,
        changed=product_changed,
        base_version=(
            config.base_version
            if config.base_version is not None
            else (existing_product.version if existing_product else None)
        ),
        proposed_version=config.version,
    )
    version_reports = [product_version, *table_versions]
    hard_errors = _hard_findings(duplicate_reports, version_reports)
    hard_errors.extend(
        _shared_table_findings(
            config,
            product_id,
            table_records,
            table_versions,
            registry,
            pr_number=pr_number,
        )
    )
    hard_errors.extend(relationship_findings)
    warnings = _completeness_findings(config, table_irs, semantic_document)
    warnings.extend(prepared_metrics.findings)
    if warnings_as_errors and warnings:
        hard_errors.append(
            QualityFinding(
                code="WARNINGS_AS_ERRORS",
                message="Quality warnings are configured as hard failures",
            )
        )

    quality = QualityReport(
        status=QualityStatus.FAIL
        if hard_errors
        else (QualityStatus.WARN if warnings else QualityStatus.PASS),
        product_id=product_id,
        product_version=config.version,
        completeness=_completeness_score(config, table_irs),
        hard_errors=hard_errors,
        warnings=warnings,
        artifact_hashes={},
    )
    if hard_errors:
        _write_quality(
            root,
            quality,
            duplicate_reports,
            version_reports,
            product_id,
            table_records,
            suggestion_batch,
            product_document,
        )
        raise PipelineValidationError(
            "; ".join(item.code for item in hard_errors),
            report=quality,
        )

    product_ir = ProductIR(
        product_id=product_id,
        product_key=config.product_key,
        version=config.version,
        display_name=config.display_name,
        description=config.description,
        product_facts=product_facts,
        synonyms=config.synonyms,
        instructions=semantic_document.markdown.strip(),
        source_hashes=source_hashes,
        tables=table_irs,
        relationships=relationships,
        metrics=metrics,
    )
    compiled = compile_ossie(product_ir)
    artifacts = {
        "data-product.md": render_product_markdown(product_ir),
        "data-semantic.md": render_semantic_markdown(product_ir),
        "data-dictionary.json": render_dictionary_json(product_ir),
        "ossie-model.json": _json_text(compiled),
        "source-manifest.json": _json_text(_manifest_payload(manifest)),
    }
    quality.artifact_hashes = {
        name: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for name, content in sorted(artifacts.items())
    }

    candidate_root = root / ".build" / canonical_hash(_manifest_payload(manifest)) / "candidate"
    generated_candidate = candidate_root / "generated"
    if candidate_root.exists():
        shutil.rmtree(candidate_root)
    generated_candidate.mkdir(parents=True)
    for name, content in artifacts.items():
        (generated_candidate / name).write_text(content, encoding="utf-8")

    mappings = _build_mappings(product_id, table_records, table_drafts, registry)
    _require_registry_state(
        registry_path,
        expected_exists=registry_initially_exists,
    )
    registry_candidate = registry_path.with_name(
        f".{registry_path.name}.candidate-{candidate_root.parent.name[:12]}"
    )
    if registry_candidate.exists():
        shutil.rmtree(registry_candidate)
    registry_candidate.mkdir(parents=True)
    _write_registry_snapshot(registry_candidate, registry_snapshot)
    try:
        staged_registry = Registry.load(registry_candidate)
        staged_registry.write_product(product_record)
        for table in table_records:
            staged_registry.write_table(table)
        staged_registry.write_mappings(product_id, mappings)
        Registry.load(registry_candidate)
        _write_quality(
            candidate_root,
            quality,
            duplicate_reports,
            version_reports,
            product_id,
            table_records,
            suggestion_batch,
            product_document,
        )
        _require_registry_state(
            registry_path,
            expected_exists=registry_initially_exists,
        )
        _promote_directories(
            [
                (registry_candidate, registry_path),
                (generated_candidate, root / "generated"),
                (candidate_root / "quality", root / "quality"),
            ],
            token=candidate_root.parent.name[:12],
        )
    finally:
        if registry_candidate.exists():
            shutil.rmtree(registry_candidate)
    return ProcessResult(
        product_id=product_id,
        product_version=config.version,
        generated_dir=root / "generated",
        quality_report=quality,
    )


def _validated_registry_path(value: str | Path) -> Path:
    path = Path(os.path.abspath(Path(value).expanduser()))
    _require_registry_state(path, expected_exists=None)
    return path


def _require_registry_state(path: Path, *, expected_exists: bool | None) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        exists = False
    else:
        exists = True
        if _is_link_or_reparse_point(path_stat):
            raise PipelineSecurityError("SYMLINK_NOT_ALLOWED")
        if not stat.S_ISDIR(path_stat.st_mode):
            raise PipelineSecurityError("READ_PATH_TYPE_NOT_ALLOWED")
    if expected_exists is not None and exists is not expected_exists:
        raise PipelineSecurityError("REGISTRY_PATH_CHANGED")


def _snapshot_registry(path: Path) -> tuple[bool, dict[Path, bytes]]:
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    if not directory_flag or not nofollow_flag:
        return _snapshot_registry_portable(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | directory_flag | nofollow_flag)
    except FileNotFoundError:
        return False, {}
    except OSError as error:
        _raise_registry_snapshot_error(path, error)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise PipelineSecurityError("READ_PATH_TYPE_NOT_ALLOWED")
        return True, _read_registry_directory(descriptor, Path())
    finally:
        os.close(descriptor)


def _read_registry_directory(
    descriptor: int,
    prefix: Path,
) -> dict[Path, bytes]:
    snapshot: dict[Path, bytes] = {}
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        with os.scandir(descriptor) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        for entry in entries:
            relative = prefix / entry.name
            if entry.is_symlink():
                raise PipelineSecurityError("SYMLINK_NOT_ALLOWED")
            if entry.is_dir(follow_symlinks=False):
                child = os.open(entry.name, directory_flags, dir_fd=descriptor)
                try:
                    snapshot.update(_read_registry_directory(child, relative))
                finally:
                    os.close(child)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise PipelineSecurityError("READ_PATH_TYPE_NOT_ALLOWED")
            file_descriptor = os.open(entry.name, file_flags, dir_fd=descriptor)
            try:
                if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                    raise PipelineSecurityError("READ_PATH_TYPE_NOT_ALLOWED")
                with os.fdopen(file_descriptor, "rb", closefd=False) as handle:
                    snapshot[relative] = handle.read()
            finally:
                os.close(file_descriptor)
    except PipelineSecurityError:
        raise
    except OSError as error:
        raise PipelineSecurityError("REGISTRY_PATH_CHANGED") from error
    return snapshot


def _raise_registry_snapshot_error(path: Path, error: OSError) -> None:
    try:
        path_stat = path.lstat()
    except OSError:
        raise PipelineSecurityError("REGISTRY_PATH_CHANGED") from error
    if _is_link_or_reparse_point(path_stat):
        raise PipelineSecurityError("SYMLINK_NOT_ALLOWED")
    if not stat.S_ISDIR(path_stat.st_mode):
        raise PipelineSecurityError("READ_PATH_TYPE_NOT_ALLOWED")
    raise PipelineSecurityError("REGISTRY_PATH_CHANGED") from error


def _snapshot_registry_portable(path: Path) -> tuple[bool, dict[Path, bytes]]:
    """Snapshot safely on platforms without directory-relative no-follow opens."""
    try:
        root_stat = path.lstat()
    except FileNotFoundError:
        return False, {}
    except OSError as error:
        raise PipelineSecurityError("REGISTRY_PATH_CHANGED") from error
    _require_portable_path_type(root_stat, directory=True)
    try:
        snapshot = _read_registry_directory_portable(path, Path(), root_stat)
        _require_same_path_identity(path, root_stat)
    except PipelineSecurityError:
        raise
    except OSError as error:
        raise PipelineSecurityError("REGISTRY_PATH_CHANGED") from error
    return True, snapshot


def _read_registry_directory_portable(
    path: Path,
    prefix: Path,
    expected_stat: os.stat_result,
) -> dict[Path, bytes]:
    _require_same_path_identity(path, expected_stat)
    with os.scandir(path) as iterator:
        entries = sorted(iterator, key=lambda item: item.name)
    _require_same_path_identity(path, expected_stat)
    snapshot: dict[Path, bytes] = {}
    for entry in entries:
        _require_same_path_identity(path, expected_stat)
        child_path = path / entry.name
        child_stat = child_path.lstat()
        relative = prefix / entry.name
        if _is_link_or_reparse_point(child_stat):
            raise PipelineSecurityError("SYMLINK_NOT_ALLOWED")
        if stat.S_ISDIR(child_stat.st_mode):
            snapshot.update(
                _read_registry_directory_portable(child_path, relative, child_stat)
            )
            continue
        _require_portable_path_type(child_stat, directory=False)
        snapshot[relative] = _read_registry_file_portable(child_path, child_stat)
    _require_same_path_identity(path, expected_stat)
    return snapshot


def _read_registry_file_portable(path: Path, expected_stat: os.stat_result) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        opened_stat = os.fstat(descriptor)
        _require_same_identity(opened_stat, expected_stat)
        _require_portable_path_type(opened_stat, directory=False)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read()
        _require_same_identity(os.fstat(descriptor), expected_stat)
    finally:
        os.close(descriptor)
    _require_same_path_identity(path, expected_stat)
    return payload


def _require_portable_path_type(
    path_stat: os.stat_result,
    *,
    directory: bool,
) -> None:
    if _is_link_or_reparse_point(path_stat):
        raise PipelineSecurityError("SYMLINK_NOT_ALLOWED")
    predicate = stat.S_ISDIR if directory else stat.S_ISREG
    if not predicate(path_stat.st_mode):
        raise PipelineSecurityError("READ_PATH_TYPE_NOT_ALLOWED")


def _require_same_path_identity(path: Path, expected_stat: os.stat_result) -> None:
    current_stat = path.lstat()
    if _is_link_or_reparse_point(current_stat):
        raise PipelineSecurityError("SYMLINK_NOT_ALLOWED")
    _require_same_identity(current_stat, expected_stat)


def _require_same_identity(
    current_stat: os.stat_result,
    expected_stat: os.stat_result,
) -> None:
    if _path_identity(current_stat) != _path_identity(expected_stat):
        raise PipelineSecurityError("REGISTRY_PATH_CHANGED")


def _path_identity(path_stat: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        stat.S_IFMT(path_stat.st_mode),
        path_stat.st_dev,
        path_stat.st_ino,
        path_stat.st_size,
        path_stat.st_mtime_ns,
        path_stat.st_ctime_ns,
    )


def _is_link_or_reparse_point(path_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(path_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(path_stat.st_mode) or bool(
        reparse_flag and file_attributes & reparse_flag
    )


def _load_registry_snapshot(snapshot: dict[Path, bytes]) -> Registry:
    with tempfile.TemporaryDirectory(prefix="ard-registry-snapshot-") as value:
        root = Path(value)
        _write_registry_snapshot(root, snapshot)
        return Registry.load(root)


def _write_registry_snapshot(root: Path, snapshot: dict[Path, bytes]) -> None:
    for relative, payload in sorted(snapshot.items(), key=lambda item: item[0].as_posix()):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


class _TableDraft(StrictModel):
    config: TableConfig
    dictionary: DictionaryTable
    table_id: str
    locator: TableLocator
    description: str | None = None
    columns: list[ColumnIR]
    column_records: list[ColumnRecord]


def _load_config(path: Path) -> ProductConfig:
    if not path.is_file():
        raise PipelineValidationError("MISSING_PRODUCT_CONFIG")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = ProductConfig.model_validate(payload)
    if config.operation is Operation.RETIRE:
        raise PipelineValidationError("RETIRE_NOT_SUPPORTED")
    return config


def _resolve_existing_product(config: ProductConfig, registry: Registry) -> ProductRecord | None:
    return registry.get_product(config.product_id)


def _resolve_tables(
    config: ProductConfig,
    dictionary: ParsedDictionary,
    registry: Registry,
) -> list[_TableDraft]:
    config_by_locator = {item.locator.lower(): item for item in config.tables}
    if len(config_by_locator) != len(config.tables):
        raise PipelineValidationError("DUPLICATE_TABLE_CONFIG")
    dictionary_locators = {item.locator for item in dictionary.tables}
    unknown = sorted(set(config_by_locator) - dictionary_locators)
    if unknown:
        raise PipelineValidationError(f"CONFIG_TABLE_NOT_IN_DICTIONARY: {unknown[0]}")

    drafts: list[_TableDraft] = []
    for table in dictionary.tables:
        table_config = config_by_locator.get(table.locator, TableConfig(locator=table.locator))
        locator = _parse_locator(table.locator)
        existing = next(
            (item for item in registry.tables() if item.locator.key == locator.key),
            None,
        )
        if existing and table_config.table_id and existing.table_id != table_config.table_id:
            raise PipelineValidationError(
                f"TABLE_LOCATOR_CONFLICT: {locator.key} -> {existing.table_id}"
            )
        table_id = table_config.table_id or (existing.table_id if existing else new_id("tbl"))
        existing_columns = existing.columns if existing else []
        column_records: list[ColumnRecord] = []
        columns: list[ColumnIR] = []
        matched_ids: set[str] = set()
        for column in table.columns:
            matched = next(
                (
                    item
                    for item in existing_columns
                    if column.name.casefold()
                    in {item.name.casefold(), *(alias.casefold() for alias in item.aliases)}
                ),
                None,
            )
            if matched is not None and matched.status is EntityStatus.RETIRED:
                raise PipelineValidationError(f"RETIRED_COLUMN_REUSE: {matched.column_id}")
            column_id = matched.column_id if matched else new_id("col")
            matched_ids.add(column_id)
            column_records.append(
                ColumnRecord(
                    column_id=column_id,
                    name=column.name,
                    aliases=matched.aliases if matched else [],
                )
            )
            columns.append(
                ColumnIR(
                    column_id=column_id,
                    ordinal=column.ordinal,
                    name=column.name,
                    logical_name=column.logical_name,
                    data_type=column.data_type,
                    nullable=column.nullable,
                    primary_key=column.primary_key,
                    description=column.description,
                    foreign_key=column.foreign_key,
                    formula=column.formula,
                    comment=column.comment,
                    evidence=[column.evidence],
                )
            )
        column_records.extend(
            previous.model_copy(update={"status": EntityStatus.RETIRED})
            for previous in existing_columns
            if previous.column_id not in matched_ids
        )
        drafts.append(
            _TableDraft(
                config=table_config,
                dictionary=table,
                table_id=table_id,
                locator=locator,
                description=table.description,
                columns=columns,
                column_records=column_records,
            )
        )
    return drafts


def _build_table_records(
    drafts: list[_TableDraft], registry: Registry
) -> tuple[list[TableRecord], list[TableIR], list[VersionDecision]]:
    records: list[TableRecord] = []
    irs: list[TableIR] = []
    decisions: list[VersionDecision] = []
    for draft in drafts:
        existing = registry.get_table(draft.table_id)
        physical = [
            {
                "ordinal": item.ordinal,
                "name": item.name,
                "data_type": item.data_type,
                "nullable": item.nullable,
                "primary_key": item.primary_key,
            }
            for item in draft.columns
        ]
        current_schema_hash = schema_hash(physical)
        current_canonical_hash = canonical_hash(
            {"locator": draft.locator, "columns": draft.columns, "description": draft.description}
        )
        changed = existing is None or existing.canonical_hash != current_canonical_hash
        proposed = draft.config.version
        if proposed is None:
            proposed = 1 if existing is None else existing.version + (1 if changed else 0)
        decision = plan_version(
            current_version=existing.version if existing else None,
            changed=changed,
            base_version=(
                draft.config.base_version
                if draft.config.base_version is not None
                else (existing.version if existing else None)
            ),
            proposed_version=proposed,
        )
        decisions.append(decision)
        record = TableRecord(
            table_id=draft.table_id,
            locator=draft.locator,
            version=proposed,
            aliases=existing.aliases if existing else [],
            columns=draft.column_records,
            schema_hash=current_schema_hash,
            canonical_hash=current_canonical_hash,
        )
        records.append(record)
        irs.append(
            TableIR(
                table_id=draft.table_id,
                table_version=proposed,
                dataset_name=draft.locator.table_name,
                source=".".join(
                    (draft.locator.catalog, draft.locator.schema_name, draft.locator.table_name)
                ),
                description=draft.description,
                columns=draft.columns,
            )
        )
    return records, irs, decisions


def _duplicate_reports(
    config: ProductConfig,
    product: ProductRecord,
    tables: list[TableRecord],
    registry: Registry,
) -> list[DuplicateReport]:
    existing = registry.get_product(product.product_id)
    if (
        config.operation is Operation.CREATE
        and existing is not None
        and existing.canonical_hash == product.canonical_hash
    ):
        product_report = DuplicateReport(
            entity_type="product",
            decision=DuplicateDecision.NO_CHANGE,
            code="PRODUCT_NO_CHANGE",
            target_id=existing.product_id,
        )
    else:
        product_report = classify_product(
            product,
            registry.products(),
            operation=config.operation,
        )
    return [
        product_report,
        *(classify_table(table, registry.tables()) for table in tables),
    ]


def _hard_findings(
    duplicates: list[DuplicateReport], versions: list[VersionDecision]
) -> list[QualityFinding]:
    findings = [
        QualityFinding(code=item.code, message=f"Duplicate check blocked {item.entity_type}")
        for item in duplicates
        if item.decision is DuplicateDecision.BLOCK
    ]
    findings.extend(
        QualityFinding(code=item.code, message="Numeric version transition is invalid")
        for item in versions
        if item.outcome is VersionOutcome.BLOCK
    )
    return findings


def _shared_table_findings(
    config: ProductConfig,
    product_id: str,
    tables: list[TableRecord],
    versions: list[VersionDecision],
    registry: Registry,
    *,
    pr_number: int | None,
) -> list[QualityFinding]:
    changed_existing = [
        table
        for table, decision in zip(tables, versions, strict=True)
        if decision.changed and registry.get_table(table.table_id) is not None
    ]
    shared = [
        table
        for table in changed_existing
        if analyze_table_change(table.table_id, registry.mappings()).shared
    ]
    if not shared:
        return []
    if not config.changeset_id:
        return [
            QualityFinding(
                code="SHARED_TABLE_CHANGESET_REQUIRED",
                message=f"Shared table {table.table_id} must use a changeset",
            )
            for table in shared
        ]
    changeset = registry.get_changeset(config.changeset_id)
    if changeset is None:
        return [
            QualityFinding(
                code="CHANGESET_NOT_FOUND",
                message=f"Changeset {config.changeset_id} does not exist",
            )
        ]
    findings: list[QualityFinding] = []
    if product_id not in changeset.required_product_ids:
        findings.append(
            QualityFinding(
                code="CHANGESET_PRODUCT_NOT_REQUIRED",
                message=f"Product {product_id} is not required by {config.changeset_id}",
            )
        )
    for table in shared:
        if table.table_id not in changeset.table_ids:
            findings.append(
                QualityFinding(
                    code="CHANGESET_TABLE_NOT_INCLUDED",
                    message=f"Table {table.table_id} is not included by {config.changeset_id}",
                )
            )
    if pr_number is None or pr_number < 1:
        findings.append(
            QualityFinding(
                code="CHANGESET_PR_NUMBER_REQUIRED",
                message="A positive PR number is required to mark changeset readiness",
            )
        )
    return findings


def _completeness_findings(
    config: ProductConfig,
    tables: list[TableIR],
    semantic_document: ParsedDocument,
) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    if not config.description:
        findings.append(
            QualityFinding(code="MISSING_PRODUCT_DESCRIPTION", message="Description is missing")
        )
    if not semantic_document.markdown.strip():
        findings.append(
            QualityFinding(code="EMPTY_SEMANTIC_DOCUMENT", message="Semantic document is empty")
        )
    for table in tables:
        if not table.description:
            findings.append(
                QualityFinding(
                    code="MISSING_TABLE_DESCRIPTION",
                    message=f"Description is missing for {table.dataset_name}",
                    path=f"tables.{table.table_id}.description",
                )
            )
        for column in table.columns:
            if not column.description:
                findings.append(
                    QualityFinding(
                        code="MISSING_COLUMN_DESCRIPTION",
                        message=f"Description is missing for {table.dataset_name}.{column.name}",
                        path=f"tables.{table.table_id}.columns.{column.column_id}.description",
                    )
                )
    return findings


def _completeness_score(config: ProductConfig, tables: list[TableIR]) -> float:
    values = [bool(config.description)]
    values.extend(bool(table.description) for table in tables)
    values.extend(bool(column.description) for table in tables for column in table.columns)
    return round(sum(values) / len(values), 4) if values else 1.0


def _build_mappings(
    product_id: str,
    tables: list[TableRecord],
    drafts: list[_TableDraft],
    registry: Registry,
) -> list[ProductTableRef]:
    existing = {
        (item.table_id, item.usage): item
        for item in registry.mappings()
        if item.product_id == product_id
    }
    return [
        ProductTableRef(
            link_id=(
                existing[(table.table_id, draft.config.usage)].link_id
                if (table.table_id, draft.config.usage) in existing
                else new_id("lnk")
            ),
            product_id=product_id,
            table_id=table.table_id,
            table_version=table.version,
            usage=draft.config.usage,
            required=draft.config.required,
        )
        for table, draft in zip(tables, drafts, strict=True)
    ]


def _product_evidence_catalog(document: ParsedDocument) -> dict[str, Evidence]:
    return {
        f"product-evidence-{position:06d}": evidence
        for position, evidence in enumerate(document.evidence, start=1)
    }


def _product_prompt_payload(document: ParsedDocument) -> dict[str, object]:
    payload = document.model_dump(mode="json")
    payload["evidence"] = [
        {"evidence_id": evidence_id, **evidence.model_dump(mode="json")}
        for evidence_id, evidence in _product_evidence_catalog(document).items()
    ]
    return payload


def _metric_dataset_catalog(
    drafts: list[_TableDraft],
) -> dict[str, tuple[str, dict[str, str]]]:
    catalog: dict[str, tuple[str, dict[str, str]]] = {}
    for draft in drafts:
        dataset_name = draft.locator.table_name
        dataset_key = dataset_name.casefold()
        if dataset_key in catalog:
            raise PipelineValidationError("METRIC_DATASET_NAME_AMBIGUOUS")
        catalog[dataset_key] = (
            dataset_name,
            {column.name.casefold(): column.name for column in draft.columns},
        )
    return catalog


def _extract_suggestions(
    provider: LLMProvider,
    product_document: ParsedDocument,
    semantic_document: ParsedDocument,
    drafts: list[_TableDraft],
) -> SuggestionBatch:
    dataset_catalog = _metric_dataset_catalog(drafts)
    allowed_paths = ["product.description", "product.synonyms"]
    for draft in drafts:
        allowed_paths.append(f"tables.{draft.table_id}.description")
        allowed_paths.extend(
            f"tables.{draft.table_id}.columns.{column.column_id}.description"
            for column in draft.columns
        )
    response = provider.generate_structured(
        schema=semantic_extraction_schema(),
        messages=[
            {
                "role": "system",
                "content": (
                    "Extract semantic suggestions and evidence-backed product facts. "
                    "Extract business metrics as ANSI SQL expressions when explicitly supported. "
                    "For every metric, set dataset_names to every referenced dataset using the "
                    "exact dataset names supplied in datasets. "
                    "Suggestions and metrics must cite supplied evidence objects. "
                    "Product facts must cite supplied product evidence_id values in evidence_ids; "
                    "do not reproduce product evidence objects. "
                    "Product facts must use only explicit values submitted in the product HTML. "
                    "Ignore navigation, search, menus, buttons, attachment actions and sizes, "
                    "privacy notices, authoring hints, review-only empty fields, fields labeled "
                    "as AI-generated summaries, next or previous links, footer text, and chatbot "
                    "content. Return no product fact when product HTML evidence is absent. "
                    "Return every required JSON property; use null for unavailable locator values. "
                    f"Allowed field_path values: {json.dumps(allowed_paths)}"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "product": _product_prompt_payload(product_document),
                        "semantic": semantic_document.model_dump(mode="json"),
                        "datasets": [
                            {
                                "dataset_name": dataset_name,
                                "columns": list(columns.values()),
                            }
                            for dataset_name, columns in sorted(
                                dataset_catalog.values(),
                                key=lambda item: item[0].casefold(),
                            )
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    )
    batch = SuggestionBatch.model_validate(response)
    suggestions = validate_semantic_suggestions(batch.suggestions)
    unknown = sorted({item.field_path for item in suggestions} - set(allowed_paths))
    if unknown:
        raise ValueError(f"LLM_FIELD_PATH_NOT_ALLOWED: {unknown[0]}")
    source_hashes = {product_document.source_hash, semantic_document.source_hash}
    for suggestion in suggestions:
        if any(evidence.source_hash not in source_hashes for evidence in suggestion.evidence):
            raise ValueError(f"LLM_EVIDENCE_SOURCE_UNKNOWN: {suggestion.field_path}")
    for metric in batch.metrics:
        if any(evidence.source_hash not in source_hashes for evidence in metric.evidence):
            raise ValueError(f"LLM_EVIDENCE_SOURCE_UNKNOWN: metric.{metric.name}")
    return batch.model_copy(update={"suggestions": suggestions})


_PRODUCT_FACT_KIND_ORDER = (
    "description",
    "purpose",
    "domain",
    "data_type",
    "storage_location",
    "source_system",
    "source_name",
    "tag",
    "access",
    "security_classification",
    "owner",
    "contact",
    "consumer",
    "refresh_schedule",
    "freshness",
    "sla",
    "ai_readiness",
    "quality",
    "constraint",
    "related_link",
)
_PRODUCT_FACT_SINGLETONS = frozenset(
    {
        "description",
        "purpose",
        "domain",
        "data_type",
        "storage_location",
        "access",
        "security_classification",
        "refresh_schedule",
        "freshness",
        "sla",
        "ai_readiness",
    }
)
_PRODUCT_FACT_POSITION = {
    kind: position for position, kind in enumerate(_PRODUCT_FACT_KIND_ORDER)
}
_AI_GENERATED_EVIDENCE = re.compile(
    r"(?:\(\s*)?AI\s*(?:자동\s*생성|generated)(?:\s*\))?",
    re.IGNORECASE,
)


def _product_evidence_key(evidence: Evidence) -> tuple[str, str, str, str | None]:
    locator = {key: value for key, value in evidence.locator.items() if value is not None}
    return (
        evidence.source_hash,
        evidence.role.value,
        json.dumps(locator, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        evidence.excerpt,
    )


def _product_fact_input_key(fact: ProductFactSuggestion) -> tuple[int, str, str, str]:
    return (
        _PRODUCT_FACT_POSITION[fact.kind],
        fact.value.casefold(),
        fact.value,
        json.dumps(
            fact.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _resolve_product_fact_evidence(
    fact: ProductFactSuggestion,
    evidence_catalog: dict[str, Evidence],
) -> list[Evidence]:
    if len(fact.evidence_ids) != len(set(fact.evidence_ids)):
        raise ValueError("LLM_PRODUCT_FACT_EVIDENCE_ID_DUPLICATE")
    resolved: list[Evidence] = []
    for evidence_id in fact.evidence_ids:
        evidence = evidence_catalog.get(evidence_id)
        if evidence is None:
            raise ValueError("LLM_PRODUCT_FACT_EVIDENCE_UNKNOWN")
        resolved.append(evidence)
    return resolved


def _validate_product_facts(
    facts: list[ProductFactSuggestion],
    product_document: ParsedDocument,
    *,
    configured_description: str | None,
) -> list[ProductFactIR]:
    accepted: dict[tuple[str, str], ProductFactIR] = {}
    singleton_values: dict[str, str] = {}
    evidence_catalog = _product_evidence_catalog(product_document)
    known_evidence = {_product_evidence_key(item) for item in product_document.evidence}
    excluded_evidence = {
        _product_evidence_key(item)
        for item in product_document.excluded_product_fact_evidence
    }
    for fact in sorted(facts, key=_product_fact_input_key):
        resolved_evidence = _resolve_product_fact_evidence(fact, evidence_catalog)
        for evidence in resolved_evidence:
            if evidence.role is not SourceRole.PRODUCT_HTML:
                raise ValueError("LLM_PRODUCT_FACT_EVIDENCE_ROLE_INVALID")
            if evidence.source_hash != product_document.source_hash:
                raise ValueError("LLM_PRODUCT_FACT_EVIDENCE_SOURCE_UNKNOWN")
            if not evidence.excerpt or not evidence.excerpt.strip():
                raise ValueError("LLM_PRODUCT_FACT_EVIDENCE_EXCERPT_REQUIRED")
            evidence_key = _product_evidence_key(evidence)
            if evidence_key in excluded_evidence:
                raise ValueError("LLM_PRODUCT_FACT_EVIDENCE_AI_GENERATED")
            if evidence_key not in known_evidence:
                raise ValueError("LLM_PRODUCT_FACT_EVIDENCE_UNKNOWN")
            if _AI_GENERATED_EVIDENCE.search(evidence.excerpt):
                raise ValueError("LLM_PRODUCT_FACT_EVIDENCE_AI_GENERATED")
        if fact.confidence < 0.7:
            continue
        normalized_key = fact.value.casefold()
        key = (fact.kind, normalized_key)
        if key in accepted:
            continue
        existing = singleton_values.get(fact.kind)
        if fact.kind in _PRODUCT_FACT_SINGLETONS and existing not in {None, normalized_key}:
            raise ValueError("LLM_PRODUCT_FACT_SINGLETON_CONFLICT")
        singleton_values[fact.kind] = normalized_key
        accepted[key] = ProductFactIR(
            kind=fact.kind,
            value=fact.value,
            evidence=resolved_evidence,
        )

    normalized_description = (
        " ".join(configured_description.split()) if configured_description else ""
    )
    if normalized_description:
        accepted = {
            key: fact for key, fact in accepted.items() if fact.kind != "description"
        }
        accepted[("description", normalized_description.casefold())] = ProductFactIR(
            kind="description",
            value=normalized_description,
        )

    return sorted(
        accepted.values(),
        key=lambda fact: (
            _PRODUCT_FACT_POSITION[fact.kind],
            fact.value.casefold(),
            fact.value,
        ),
    )


def _apply_suggestions(
    config: ProductConfig,
    drafts: list[_TableDraft],
    suggestions: list[AISuggestion],
) -> tuple[ProductConfig, list[_TableDraft]]:
    updated_config = config.model_copy(deep=True)
    updated_drafts = [draft.model_copy(deep=True) for draft in drafts]
    for suggestion in suggestions:
        if suggestion.confidence < 0.7:
            continue
        if suggestion.field_path == "product.description" and not updated_config.description:
            updated_config.description = str(suggestion.value)
        elif suggestion.field_path == "product.synonyms" and isinstance(
            suggestion.value, list
        ):
            updated_config.synonyms = [str(item) for item in suggestion.value]
    return updated_config, updated_drafts


_METRIC_SCALAR_ROOTS = (
    exp.Binary,
    exp.Boolean,
    exp.Case,
    exp.Cast,
    exp.Column,
    exp.Func,
    exp.Literal,
    exp.Null,
    exp.Paren,
    exp.Predicate,
    exp.Unary,
)

_METRIC_RELATIONAL_NODES = (
    exp.Command,
    exp.DDL,
    exp.DerivedTable,
    exp.DML,
    exp.JSONTable,
    exp.Query,
    exp.Selectable,
    exp.Subquery,
    exp.Table,
    exp.UDTF,
)


def _parse_metric_scalar(expression: str) -> exp.Expr:
    dialect = Dialect.get_or_raise(None)
    try:
        expressions = _MetricSqlParser(
            dialect=dialect,
            error_message_context=0,
        ).parse(dialect.tokenize(expression), expression)
    except SqlglotError:
        raise ValueError("LLM_METRIC_SQL_INVALID") from None
    if len(expressions) != 1 or expressions[0] is None:
        raise ValueError("LLM_METRIC_SQL_UNSAFE")
    normalized = expressions[0]
    if not isinstance(normalized, _METRIC_SCALAR_ROOTS) or any(
        isinstance(node, _METRIC_RELATIONAL_NODES) for node in normalized.walk()
    ):
        raise ValueError("LLM_METRIC_SQL_UNSAFE")
    return normalized


def _canonical_metric_identifier(
    value: str,
    *,
    source: exp.Expr | None,
) -> exp.Identifier:
    quoted = (
        isinstance(source, exp.Identifier) and source.args.get("quoted") is True
    ) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None
    return exp.to_identifier(value, quoted=quoted)


_METRIC_DIVISION_TYPE = "DECIMAL(38, 12)"


def _normalize_metric_divisions(
    expression: exp.Expr,
    *,
    dataset_name: str,
    columns: list[ColumnIR],
) -> exp.Expr:
    if expression.find(exp.Div) is None:
        return expression
    query = exp.select(expression.copy()).from_(
        exp.Table(this=_canonical_metric_identifier(dataset_name, source=None))
    )
    annotate_types(
        query,
        schema={dataset_name: {column.name: column.data_type for column in columns}},
    )
    normalized = query.expressions[0]
    for division in reversed(list(normalized.find_all(exp.Div))):
        numerator = division.this
        if numerator.type.is_type(*exp.DataType.INTEGER_TYPES):
            division.set(
                "this",
                exp.cast(numerator.copy(), _METRIC_DIVISION_TYPE),
            )
    return normalized


def _prepare_metrics(
    suggestions: list[MetricSuggestion],
    drafts: list[_TableDraft],
) -> _PreparedMetrics:
    catalog = _metric_dataset_catalog(drafts)
    prepared: list[MetricSuggestion] = []
    findings: list[QualityFinding] = []
    excluded_names: list[str] = []
    for suggestion in suggestions:
        expression = suggestion.expression.strip()
        if not suggestion.name.strip() or not expression:
            raise ValueError("LLM_METRIC_NAME_OR_EXPRESSION_EMPTY")
        dataset_keys = [name.strip().casefold() for name in suggestion.dataset_names]
        if any(not name for name in dataset_keys):
            raise ValueError("LLM_METRIC_DATASET_EMPTY")
        if len(set(dataset_keys)) != len(dataset_keys):
            raise ValueError("LLM_METRIC_DATASET_DUPLICATE")
        if any(name not in catalog for name in dataset_keys):
            raise ValueError("LLM_METRIC_DATASET_UNKNOWN")
        normalized = _parse_metric_scalar(expression)
        if any(column.db or column.catalog for column in normalized.find_all(exp.Column)):
            raise ValueError("LLM_METRIC_REFERENCE_UNKNOWN")
        if len(dataset_keys) > 1:
            for column in normalized.find_all(exp.Column):
                qualifier = column.table.strip().casefold()
                column_key = column.name.casefold()
                if qualifier:
                    if qualifier not in dataset_keys:
                        raise ValueError("LLM_METRIC_REFERENCE_UNKNOWN")
                    if column_key not in catalog[qualifier][1]:
                        raise ValueError("LLM_METRIC_REFERENCE_UNKNOWN")
                elif not any(column_key in catalog[name][1] for name in dataset_keys):
                    raise ValueError("LLM_METRIC_REFERENCE_UNKNOWN")
            findings.append(
                QualityFinding(
                    code="METRIC_MULTI_DATASET_UNSUPPORTED",
                    path=f"metrics.{suggestion.name.strip()}",
                    message=(
                        "Metric uses multiple datasets and was excluded because join path, "
                        "cardinality, and grain are not declared"
                    ),
                )
            )
            excluded_names.append(suggestion.name.strip())
            continue
        dataset_key = dataset_keys[0]
        canonical_dataset, columns = catalog[dataset_key]
        for column in normalized.find_all(exp.Column):
            qualifier = column.table
            if qualifier and qualifier.casefold() != dataset_key:
                raise ValueError("LLM_METRIC_REFERENCE_UNKNOWN")
            canonical_column = columns.get(column.name.casefold())
            if canonical_column is None:
                raise ValueError("LLM_METRIC_REFERENCE_UNKNOWN")
            source_column = column.args.get("this")
            source_table = column.args.get("table")
            column.set(
                "this",
                _canonical_metric_identifier(
                    canonical_column,
                    source=source_column,
                ),
            )
            column.set(
                "table",
                _canonical_metric_identifier(
                    canonical_dataset,
                    source=source_table,
                ),
            )
        dataset_draft = next(
            draft
            for draft in drafts
            if draft.locator.table_name.casefold() == dataset_key
        )
        normalized = _normalize_metric_divisions(
            normalized,
            dataset_name=canonical_dataset,
            columns=dataset_draft.columns,
        )
        normalized_expression = normalized.sql()
        _parse_metric_scalar(normalized_expression)
        prepared.append(
            suggestion.model_copy(update={"expression": normalized_expression})
        )
    _validate_metric_name_collisions(
        prepared,
        None,
        excluded_names=excluded_names,
    )
    return _PreparedMetrics(
        suggestions=prepared,
        findings=findings,
        excluded_names=excluded_names,
    )


def _validate_metric_name_collisions(
    suggestions: list[MetricSuggestion],
    existing_product: ProductRecord | None,
    *,
    excluded_names: list[str],
) -> None:
    accepted_names = [suggestion.name.strip().casefold() for suggestion in suggestions]
    accepted_keys = set(accepted_names)
    excluded_keys = {name.casefold() for name in excluded_names}
    if len(accepted_names) != len(accepted_keys) or accepted_keys & excluded_keys:
        raise ValueError("LLM_METRIC_NAME_DUPLICATE")
    for record in existing_product.metrics if existing_product else []:
        identity_keys = {
            record.name.casefold(),
            *(alias.casefold() for alias in record.aliases),
        }
        if identity_keys & accepted_keys and identity_keys & excluded_keys:
            raise ValueError("LLM_METRIC_NAME_DUPLICATE")


def _build_metrics(
    suggestions: list[MetricSuggestion],
    existing_product: ProductRecord | None,
    *,
    excluded_names: list[str],
) -> tuple[list[MetricIR], list[MetricRecord]]:
    excluded_keys = {name.casefold() for name in excluded_names}
    _validate_metric_name_collisions(
        suggestions,
        existing_product,
        excluded_names=excluded_names,
    )
    existing = [
        record
        for record in (existing_product.metrics if existing_product else [])
        if excluded_keys.isdisjoint(
            {record.name.casefold(), *(alias.casefold() for alias in record.aliases)}
        )
    ]
    records_by_id = {record.metric_id: record for record in existing}
    metrics: list[MetricIR] = []
    seen_names: set[str] = set()
    for suggestion in suggestions:
        if suggestion.confidence < 0.7:
            continue
        name_key = suggestion.name.strip().casefold()
        if name_key in seen_names:
            raise ValueError(f"LLM_METRIC_NAME_DUPLICATE: {suggestion.name}")
        seen_names.add(name_key)
        matched = next(
            (
                record
                for record in existing
                if name_key
                in {record.name.casefold(), *(alias.casefold() for alias in record.aliases)}
            ),
            None,
        )
        if matched is not None and matched.status is EntityStatus.RETIRED:
            raise ValueError(f"RETIRED_METRIC_REUSE: {matched.metric_id}")
        metric_id = matched.metric_id if matched else new_id("met")
        records_by_id[metric_id] = MetricRecord(
            metric_id=metric_id,
            name=suggestion.name.strip(),
            aliases=matched.aliases if matched else [],
        )
        metrics.append(
            MetricIR(
                metric_id=metric_id,
                name=suggestion.name.strip(),
                expression=suggestion.expression.strip(),
                description=suggestion.description,
                synonyms=suggestion.synonyms,
                evidence=suggestion.evidence,
            )
        )
    return (
        sorted(metrics, key=lambda item: item.metric_id),
        sorted(records_by_id.values(), key=lambda item: item.metric_id),
    )


def _build_relationships(
    drafts: list[_TableDraft],
    existing_product: ProductRecord | None,
) -> tuple[list[RelationshipIR], list[RelationshipRecord], list[QualityFinding]]:
    target_names: dict[str, list[_TableDraft]] = {}
    for draft in drafts:
        names = {
            draft.locator.table_name,
            ".".join(
                (
                    draft.locator.catalog,
                    draft.locator.schema_name,
                    draft.locator.table_name,
                )
            ),
            draft.locator.key,
        }
        for name in names:
            target_names.setdefault(name.casefold(), []).append(draft)

    existing = existing_product.relationships if existing_product else []
    records_by_id: dict[str, RelationshipRecord] = {}
    relationships: list[RelationshipIR] = []
    findings: list[QualityFinding] = []
    for source in drafts:
        for column in source.columns:
            if not column.foreign_key:
                continue
            try:
                target_name, target_column_name = column.foreign_key.rsplit(".", 1)
            except ValueError:
                findings.append(
                    QualityFinding(
                        code="FOREIGN_KEY_FORMAT_INVALID",
                        message=f"Foreign key must be table.column: {column.foreign_key}",
                        path=f"tables.{source.table_id}.columns.{column.column_id}.foreign_key",
                    )
                )
                continue
            targets = target_names.get(target_name.casefold(), [])
            if len(targets) > 1:
                findings.append(
                    QualityFinding(
                        code="FOREIGN_KEY_TARGET_AMBIGUOUS",
                        message=(
                            "Foreign key target must use a qualified table name: "
                            f"{column.foreign_key}"
                        ),
                        path=f"tables.{source.table_id}.columns.{column.column_id}.foreign_key",
                    )
                )
                continue
            target = targets[0] if targets else None
            target_column = (
                next(
                    (
                        item
                        for item in target.columns
                        if item.name.casefold() == target_column_name.casefold()
                    ),
                    None,
                )
                if target is not None
                else None
            )
            if target is None or target_column is None:
                findings.append(
                    QualityFinding(
                        code="FOREIGN_KEY_TARGET_NOT_FOUND",
                        message=f"Foreign key target is not in this product: {column.foreign_key}",
                        path=f"tables.{source.table_id}.columns.{column.column_id}.foreign_key",
                    )
                )
                continue
            name = (
                f"{source.locator.table_name}_{column.name}_to_"
                f"{target.locator.table_name}_{target_column.name}"
            )
            matched = next(
                (
                    record
                    for record in existing
                    if name.casefold()
                    in {record.name.casefold(), *(alias.casefold() for alias in record.aliases)}
                ),
                None,
            )
            if matched is not None and matched.status is EntityStatus.RETIRED:
                findings.append(
                    QualityFinding(
                        code="RETIRED_RELATIONSHIP_REUSE",
                        message=(
                            "Retired relationship ID cannot be reused: "
                            f"{matched.relationship_id}"
                        ),
                    )
                )
                continue
            relationship_id = matched.relationship_id if matched else new_id("rel")
            records_by_id[relationship_id] = RelationshipRecord(
                relationship_id=relationship_id,
                name=name,
                aliases=matched.aliases if matched else [],
            )
            relationships.append(
                RelationshipIR(
                    relationship_id=relationship_id,
                    name=name,
                    from_table_id=source.table_id,
                    to_table_id=target.table_id,
                    from_columns=[column.name],
                    to_columns=[target_column.name],
                    evidence=column.evidence,
                )
            )
    matched_ids = set(records_by_id)
    for record in existing:
        if record.relationship_id not in matched_ids:
            records_by_id[record.relationship_id] = record.model_copy(
                update={"status": EntityStatus.RETIRED}
            )
    return (
        sorted(relationships, key=lambda item: item.relationship_id),
        sorted(records_by_id.values(), key=lambda item: item.relationship_id),
        findings,
    )


def _parse_locator(value: str) -> TableLocator:
    parts = value.split("|")
    if len(parts) != 4:
        raise ValueError("table locator must be platform|catalog|schema|table")
    return TableLocator(
        source_system_id=parts[0],
        catalog=parts[1],
        schema_name=parts[2],
        table_name=parts[3],
    )


def _manifest_payload(manifest: SourceManifest) -> dict[str, object]:
    return {
        "files": [
            {
                "role": item.role.value,
                "relative_path": item.relative_path,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for item in manifest.files
        ]
    }


def _write_quality(
    root: Path,
    report: QualityReport,
    duplicates: list[DuplicateReport],
    versions: list[VersionDecision],
    product_id: str,
    tables: list[TableRecord],
    suggestions: SuggestionBatch,
    product_document: ParsedDocument,
) -> None:
    quality = root / "quality"
    quality.mkdir(parents=True, exist_ok=True)
    (quality / "quality-report.json").write_text(
        _json_text(report.model_dump(mode="json")), encoding="utf-8"
    )
    (quality / "duplicate-report.json").write_text(
        _json_text(TypeAdapter(list[DuplicateReport]).dump_python(duplicates, mode="json")),
        encoding="utf-8",
    )
    (quality / "version-report.json").write_text(
        _json_text(TypeAdapter(list[VersionDecision]).dump_python(versions, mode="json")),
        encoding="utf-8",
    )
    (quality / "impact-report.json").write_text(
        _json_text(
            {
                "product_ids": [product_id],
                "table_ids": sorted(table.table_id for table in tables),
            }
        ),
        encoding="utf-8",
    )
    suggestion_payload = suggestions.model_dump(mode="json")
    evidence_catalog = _product_evidence_catalog(product_document)
    suggestion_payload["product_facts"] = [
        {
            "kind": fact.kind,
            "value": fact.value,
            "confidence": fact.confidence,
            "evidence": [
                evidence.model_dump(mode="json")
                for evidence in _resolve_product_fact_evidence(fact, evidence_catalog)
            ],
        }
        for fact in suggestions.product_facts
    ]
    (quality / "llm-suggestions.json").write_text(
        _json_text(suggestion_payload),
        encoding="utf-8",
    )


def _promote_directories(
    directories: list[tuple[Path, Path]],
    *,
    token: str,
) -> None:
    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    try:
        for candidate, target in directories:
            target.parent.mkdir(parents=True, exist_ok=True)
            backup = target.with_name(f".{target.name}.backup-{token}")
            if target.is_symlink() or backup.is_symlink():
                raise PipelineSecurityError("SYMLINK_NOT_ALLOWED")
            if backup.exists():
                shutil.rmtree(backup)
            if target.exists():
                os.replace(target, backup)
                backups.append((target, backup))
            os.replace(candidate, target)
            installed.append(target)
    except Exception:
        for target in reversed(installed):
            if target.exists():
                shutil.rmtree(target)
        for target, backup in reversed(backups):
            if backup.exists():
                os.replace(backup, target)
        raise
    else:
        for _, backup in backups:
            if backup.exists():
                shutil.rmtree(backup)


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
