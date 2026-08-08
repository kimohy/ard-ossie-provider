from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, TypeAdapter, model_validator

from ard_ossie.canonical import canonical_hash, schema_hash
from ard_ossie.docling_parser import DoclingParser, ParsedDocument
from ard_ossie.excel_adapter import DictionaryTable, ParsedDictionary, parse_dictionary
from ard_ossie.identity import DuplicateDecision, DuplicateReport, classify_product, classify_table
from ard_ossie.ids import new_id
from ard_ossie.impact import analyze_table_change
from ard_ossie.ingestion import SourceManifest, SourceRole, scan_sources
from ard_ossie.ir import ColumnIR, MetricIR, ProductIR, RelationshipIR, TableIR
from ard_ossie.llm import (
    AISuggestion,
    LLMProvider,
    MetricSuggestion,
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


class ProviderExecutionError(RuntimeError):
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
    status: QualityStatus
    product_id: str | None = None
    product_version: int | None = None
    completeness: float = Field(ge=0, le=1)
    hard_errors: list[QualityFinding] = Field(default_factory=list)
    warnings: list[QualityFinding] = Field(default_factory=list)
    artifact_hashes: dict[str, str] = Field(default_factory=dict)


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
    suggestions: list[AISuggestion] = Field(default_factory=list)
    metrics: list[MetricSuggestion] = Field(default_factory=list)


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
) -> ProcessResult:
    root = Path(product_path).resolve()
    registry_path = Path(registry_root).resolve()
    registry = Registry.load(registry_path)
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
    suggestion_batch = SuggestionBatch()
    if provider is not None:
        try:
            suggestion_batch = _extract_suggestions(
                provider, product_document, semantic_document, table_drafts
            )
        except Exception as error:
            raise ProviderExecutionError(f"LLM_PROVIDER_FAILURE: {error}") from error
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
        suggestion_batch.metrics,
        existing_product,
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

    quality = QualityReport(
        status=QualityStatus.FAIL
        if hard_errors
        else (QualityStatus.WARN if warnings else QualityStatus.PASS),
        product_id=product_id,
        product_version=config.version,
        completeness=_completeness_score(config, table_irs),
        hard_errors=hard_errors,
        warnings=warnings,
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
        description=config.description or product_document.markdown.strip(),
        product_document_markdown=product_document.markdown.strip(),
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
    registry_candidate = registry_path.with_name(
        f".{registry_path.name}.candidate-{candidate_root.parent.name[:12]}"
    )
    if registry_candidate.exists():
        shutil.rmtree(registry_candidate)
    if registry_path.exists():
        shutil.copytree(registry_path, registry_candidate)
    else:
        registry_candidate.mkdir(parents=True)
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


def _extract_suggestions(
    provider: LLMProvider,
    product_document: ParsedDocument,
    semantic_document: ParsedDocument,
    drafts: list[_TableDraft],
) -> SuggestionBatch:
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
                    "Extract semantic suggestions only. "
                    "Extract business metrics as ANSI SQL expressions when explicitly supported. "
                    "Every suggestion and metric must cite supplied evidence. "
                    "Return every required JSON property; use null for unavailable locator values. "
                    f"Allowed field_path values: {json.dumps(allowed_paths)}"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "product": product_document.model_dump(mode="json"),
                        "semantic": semantic_document.model_dump(mode="json"),
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
        _validate_metric_expression(metric, drafts)
    return batch.model_copy(update={"suggestions": suggestions})


def _apply_suggestions(
    config: ProductConfig,
    drafts: list[_TableDraft],
    suggestions: list[AISuggestion],
) -> tuple[ProductConfig, list[_TableDraft]]:
    updated_config = config.model_copy(deep=True)
    updated_drafts = [draft.model_copy(deep=True) for draft in drafts]
    by_table = {draft.table_id: draft for draft in updated_drafts}
    for suggestion in suggestions:
        if suggestion.confidence < 0.7:
            continue
        if suggestion.field_path == "product.description" and not updated_config.description:
            updated_config.description = str(suggestion.value)
        elif suggestion.field_path == "product.synonyms":
            if isinstance(suggestion.value, list):
                updated_config.synonyms = [str(item) for item in suggestion.value]
        else:
            parts = suggestion.field_path.split(".")
            if len(parts) == 3 and parts[0] == "tables" and parts[2] == "description":
                if not by_table[parts[1]].description:
                    by_table[parts[1]].description = str(suggestion.value)
            elif len(parts) == 5 and parts[0] == "tables" and parts[2] == "columns":
                column = next(
                    item for item in by_table[parts[1]].columns if item.column_id == parts[3]
                )
                if parts[4] == "description" and not column.description:
                    column.description = str(suggestion.value)
    return updated_config, updated_drafts


_SQL_MUTATION = re.compile(
    r"\b(?:ALTER|CREATE|DELETE|DROP|INSERT|MERGE|TRUNCATE|UPDATE)\b",
    re.IGNORECASE,
)
_SQL_COLUMN_REFERENCE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b"
)


def _validate_metric_expression(
    metric: MetricSuggestion,
    drafts: list[_TableDraft],
) -> None:
    expression = metric.expression.strip()
    if not metric.name.strip() or not expression:
        raise ValueError("LLM_METRIC_NAME_OR_EXPRESSION_EMPTY")
    if ";" in expression or _SQL_MUTATION.search(expression):
        raise ValueError(f"LLM_METRIC_SQL_UNSAFE: {metric.name}")
    columns = {
        draft.locator.table_name.casefold(): {
            column.name.casefold() for column in draft.columns
        }
        for draft in drafts
    }
    for table_name, column_name in _SQL_COLUMN_REFERENCE.findall(expression):
        available = columns.get(table_name.casefold())
        if available is None or column_name.casefold() not in available:
            raise ValueError(
                f"LLM_METRIC_REFERENCE_UNKNOWN: {metric.name}:{table_name}.{column_name}"
            )


def _build_metrics(
    suggestions: list[MetricSuggestion],
    existing_product: ProductRecord | None,
) -> tuple[list[MetricIR], list[MetricRecord]]:
    existing = existing_product.metrics if existing_product else []
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
    (quality / "llm-suggestions.json").write_text(
        _json_text(suggestions.model_dump(mode="json")),
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
