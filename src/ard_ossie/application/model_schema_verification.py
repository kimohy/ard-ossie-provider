from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModelSchemaReference:
    schema_path: Path
    module_name: str
    class_name: str


MODEL_SCHEMA_CATALOG = (
    ModelSchemaReference(
        Path("candidate-change.schema.json"),
        "ard_ossie.models",
        "CandidateChange",
    ),
    ModelSchemaReference(
        Path("changeset.schema.json"),
        "ard_ossie.impact",
        "ChangeSetRecord",
    ),
    ModelSchemaReference(
        Path("ir/product-ir.schema.json"),
        "ard_ossie.ir",
        "ProductIR",
    ),
    ModelSchemaReference(
        Path("reports/duplicate-report.schema.json"),
        "ard_ossie.identity",
        "DuplicateReport",
    ),
    ModelSchemaReference(
        Path("reports/impact-report.schema.json"),
        "ard_ossie.impact",
        "ImpactReport",
    ),
    ModelSchemaReference(
        Path("reports/quality-report.schema.json"),
        "ard_ossie.pipeline",
        "QualityReport",
    ),
    ModelSchemaReference(
        Path("reports/version-report.schema.json"),
        "ard_ossie.versioning",
        "VersionDecision",
    ),
    ModelSchemaReference(
        Path("source-manifest.schema.json"),
        "ard_ossie.ingestion",
        "SourceManifest",
    ),
)


class ModelSchemaVerificationError(RuntimeError):
    def __init__(self, code: str, schema_path: Path = Path(".")) -> None:
        self.code = code
        self.schema_path = schema_path
        super().__init__(f"{code}:{schema_path.as_posix()}")


def verify_model_schemas(repository: Path) -> None:
    root = _candidate_root(repository)
    with (
        open(os.devnull, "w", encoding="utf-8") as sink,  # noqa: PTH123
        redirect_stdout(sink),
        redirect_stderr(sink),
    ):
        strict_model = _strict_model_class()
        for reference in MODEL_SCHEMA_CATALOG:
            schema = _load_schema(root, reference.schema_path)
            model = _load_model(reference, strict_model)
            try:
                generated = model.model_json_schema()
                synchronized = isinstance(generated, dict) and schema == generated
            except BaseException as error:
                raise ModelSchemaVerificationError(
                    "SCHEMA_SYNCHRONIZATION_FAILED",
                    reference.schema_path,
                ) from error
            if not synchronized:
                raise ModelSchemaVerificationError(
                    "SCHEMA_SYNCHRONIZATION_FAILED",
                    reference.schema_path,
                )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify candidate model JSON schemas")
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--nonce")
    arguments = parser.parse_args(argv)
    try:
        if (arguments.result is None) != (arguments.nonce is None):
            raise ModelSchemaVerificationError("MODEL_SCHEMA_RECEIPT_INVALID")
        verify_model_schemas(arguments.repository)
        if arguments.result is not None:
            _write_receipt(arguments.result, arguments.nonce)
    except ModelSchemaVerificationError as error:
        print(str(error), file=sys.stderr)
        return 10
    return 0


def _candidate_root(repository: Path) -> Path:
    try:
        root = repository.expanduser().resolve(strict=True)
        schemas = (root / "schemas").resolve(strict=True)
    except OSError as error:
        raise ModelSchemaVerificationError("MODEL_SCHEMA_REPOSITORY_INVALID") from error
    if root != Path.cwd().resolve() or not schemas.is_dir() or not schemas.is_relative_to(root):
        raise ModelSchemaVerificationError("MODEL_SCHEMA_REPOSITORY_INVALID")
    return root


def _strict_model_class() -> type[Any]:
    try:
        module = importlib.import_module("ard_ossie.models")
        model = module.StrictModel
    except BaseException as error:
        raise ModelSchemaVerificationError("MODEL_SCHEMA_IMPORT_FAILED") from error
    if not isinstance(model, type):
        raise ModelSchemaVerificationError("MODEL_SCHEMA_TYPE_INVALID")
    return model


def _load_schema(root: Path, schema_path: Path) -> object:
    try:
        schemas = (root / "schemas").resolve(strict=True)
        resolved = (schemas / schema_path).resolve(strict=True)
        if not resolved.is_relative_to(schemas):
            raise OSError("schema path escaped root")
        schema = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TypeError, ValueError) as error:
        raise ModelSchemaVerificationError(
            "MODEL_SCHEMA_SCHEMA_INVALID",
            schema_path,
        ) from error
    if not isinstance(schema, dict):
        raise ModelSchemaVerificationError(
            "MODEL_SCHEMA_SCHEMA_INVALID",
            schema_path,
        )
    return schema


def _load_model(
    reference: ModelSchemaReference,
    strict_model: type[Any],
) -> type[Any]:
    try:
        module = importlib.import_module(reference.module_name)
        model = getattr(module, reference.class_name)
    except BaseException as error:
        raise ModelSchemaVerificationError(
            "MODEL_SCHEMA_IMPORT_FAILED",
            reference.schema_path,
        ) from error
    try:
        valid = isinstance(model, type) and issubclass(model, strict_model)
    except BaseException as error:
        raise ModelSchemaVerificationError(
            "MODEL_SCHEMA_TYPE_INVALID",
            reference.schema_path,
        ) from error
    if not valid:
        raise ModelSchemaVerificationError(
            "MODEL_SCHEMA_TYPE_INVALID",
            reference.schema_path,
        )
    return model


def _write_receipt(result: Path, nonce: str) -> None:
    receipt = {
        "nonce": nonce,
        "schemas": [
            reference.schema_path.as_posix() for reference in MODEL_SCHEMA_CATALOG
        ],
        "status": "success",
    }
    try:
        result.write_text(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise ModelSchemaVerificationError("MODEL_SCHEMA_RECEIPT_WRITE_FAILED") from error


if __name__ == "__main__":
    raise SystemExit(main())
