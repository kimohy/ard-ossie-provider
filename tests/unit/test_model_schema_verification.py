from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import ard_ossie.application.model_schema_verification as verification
from ard_ossie.application.model_schema_verification import (
    MODEL_SCHEMA_CATALOG,
    ModelSchemaReference,
    ModelSchemaVerificationError,
    active_model_schema_catalog,
    main,
    verify_model_schemas,
)


def test_active_model_schema_catalog_skips_absent_optional_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required = ModelSchemaReference(
        Path("required.schema.json"), "json", "JSONDecoder"
    )
    optional_group = (
        ModelSchemaReference(
            Path("reports/first.schema.json"), "json", "JSONDecoder"
        ),
        ModelSchemaReference(
            Path("reports/second.schema.json"), "json", "JSONDecoder"
        ),
    )
    (tmp_path / "schemas").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(verification, "MODEL_SCHEMA_CATALOG", (required,))
    monkeypatch.setattr(verification, "OPTIONAL_MODEL_SCHEMA_GROUPS", (optional_group,))

    assert active_model_schema_catalog(tmp_path) == (required,)


def test_active_model_schema_catalog_includes_complete_optional_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required = ModelSchemaReference(
        Path("required.schema.json"), "json", "JSONDecoder"
    )
    optional_group = (
        ModelSchemaReference(
            Path("reports/first.schema.json"), "json", "JSONDecoder"
        ),
        ModelSchemaReference(
            Path("reports/second.schema.json"), "json", "JSONDecoder"
        ),
    )
    (tmp_path / "schemas" / "reports").mkdir(parents=True)
    for reference in optional_group:
        (tmp_path / "schemas" / reference.schema_path).write_text(
            "{}", encoding="utf-8"
        )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(verification, "MODEL_SCHEMA_CATALOG", (required,))
    monkeypatch.setattr(verification, "OPTIONAL_MODEL_SCHEMA_GROUPS", (optional_group,))

    assert active_model_schema_catalog(tmp_path) == (required, *optional_group)


@pytest.mark.parametrize(
    "present_path",
    ("reports/first.schema.json", "reports/second.schema.json"),
)
def test_active_model_schema_catalog_rejects_partial_optional_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    present_path: str,
) -> None:
    required = ModelSchemaReference(
        Path("required.schema.json"), "json", "JSONDecoder"
    )
    optional_group = (
        ModelSchemaReference(
            Path("reports/first.schema.json"), "json", "JSONDecoder"
        ),
        ModelSchemaReference(
            Path("reports/second.schema.json"), "json", "JSONDecoder"
        ),
    )
    schema = tmp_path / "schemas" / present_path
    schema.parent.mkdir(parents=True)
    schema.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(verification, "MODEL_SCHEMA_CATALOG", (required,))
    monkeypatch.setattr(verification, "OPTIONAL_MODEL_SCHEMA_GROUPS", (optional_group,))

    with pytest.raises(ModelSchemaVerificationError, match="SCHEMA_CATALOG_MISMATCH"):
        active_model_schema_catalog(tmp_path)


def test_active_model_schema_catalog_rejects_dangling_optional_schema_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required = ModelSchemaReference(
        Path("required.schema.json"), "json", "JSONDecoder"
    )
    optional_group = (
        ModelSchemaReference(
            Path("reports/first.schema.json"), "json", "JSONDecoder"
        ),
        ModelSchemaReference(
            Path("reports/second.schema.json"), "json", "JSONDecoder"
        ),
    )
    dangling = tmp_path / "schemas" / optional_group[0].schema_path
    dangling.parent.mkdir(parents=True)
    dangling.symlink_to(tmp_path / "missing.schema.json")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(verification, "MODEL_SCHEMA_CATALOG", (required,))
    monkeypatch.setattr(verification, "OPTIONAL_MODEL_SCHEMA_GROUPS", (optional_group,))

    with pytest.raises(ModelSchemaVerificationError, match="SCHEMA_CATALOG_MISMATCH"):
        active_model_schema_catalog(tmp_path)


def test_active_model_schema_catalog_rejects_optional_schema_resolution_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required = ModelSchemaReference(
        Path("required.schema.json"), "json", "JSONDecoder"
    )
    optional_group = (
        ModelSchemaReference(
            Path("reports/first.schema.json"), "json", "JSONDecoder"
        ),
        ModelSchemaReference(
            Path("reports/second.schema.json"), "json", "JSONDecoder"
        ),
    )
    target = tmp_path / "schemas" / optional_group[0].schema_path
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    original_resolve = Path.resolve

    def fail_optional_resolution(path: Path, *args: object, **kwargs: object) -> Path:
        if path == target:
            raise PermissionError("injected optional schema access failure")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "resolve", fail_optional_resolution)
    monkeypatch.setattr(verification, "MODEL_SCHEMA_CATALOG", (required,))
    monkeypatch.setattr(verification, "OPTIONAL_MODEL_SCHEMA_GROUPS", (optional_group,))

    with pytest.raises(ModelSchemaVerificationError, match="SCHEMA_CATALOG_MISMATCH"):
        active_model_schema_catalog(tmp_path)


def test_model_schema_helper_rejects_partial_optional_group_before_model_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schemas = tmp_path / "schemas"
    shutil.copytree(Path(__file__).parents[2] / "schemas", schemas)
    partial = schemas / "reports" / "semantic-fidelity.schema.json"
    partial.write_text("{}", encoding="utf-8")
    imported = tmp_path / "candidate-imported"

    def import_candidate_model() -> type[Any]:
        imported.write_text("unexpected import", encoding="utf-8")
        return object

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(verification, "_strict_model_class", import_candidate_model)

    with pytest.raises(ModelSchemaVerificationError, match="SCHEMA_CATALOG_MISMATCH"):
        verify_model_schemas(tmp_path)

    assert not imported.exists()


def test_model_schema_helper_receipt_retains_catalog_verified_before_model_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required = ModelSchemaReference(Path("required.schema.json"), "json", "Required")
    optional_group = (
        ModelSchemaReference(Path("reports/first.schema.json"), "json", "First"),
        ModelSchemaReference(Path("reports/second.schema.json"), "json", "Second"),
    )
    schemas = tmp_path / "schemas"
    for reference in optional_group:
        path = schemas / reference.schema_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    class Model:
        @classmethod
        def model_json_schema(cls) -> dict[str, object]:
            return {}

    def load_model(*_: object) -> type[Model]:
        for reference in optional_group:
            (schemas / reference.schema_path).unlink(missing_ok=True)
        return Model

    result = tmp_path / "receipt.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(verification, "MODEL_SCHEMA_CATALOG", (required,))
    monkeypatch.setattr(verification, "OPTIONAL_MODEL_SCHEMA_GROUPS", (optional_group,))
    monkeypatch.setattr(verification, "_strict_model_class", lambda: Model)
    monkeypatch.setattr(verification, "_load_schema", lambda *_: {})
    monkeypatch.setattr(verification, "_load_model", load_model)

    assert main(["--repository", str(tmp_path), "--result", str(result), "--nonce", "n"]) == 0

    assert json.loads(result.read_text(encoding="utf-8"))["schemas"] == [
        "required.schema.json",
        "reports/first.schema.json",
        "reports/second.schema.json",
    ]


def test_model_schema_helper_rejects_stale_preimport_schema_snapshot_after_transient_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required = ModelSchemaReference(Path("required.schema.json"), "json", "Required")
    schemas = tmp_path / "schemas"
    contents = {required.schema_path: '{"stale":true}'}
    for schema_path, content in contents.items():
        path = schemas / schema_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    class Model:
        @classmethod
        def model_json_schema(cls) -> dict[str, object]:
            for schema_path, content in contents.items():
                (schemas / schema_path).write_text(content, encoding="utf-8")
            return {}

    def transient_candidate_import() -> type[Model]:
        for schema_path in contents:
            (schemas / schema_path).write_text("{}", encoding="utf-8")
        return Model

    def load_candidate_model(*_: object) -> type[Model]:
        return Model

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(verification, "MODEL_SCHEMA_CATALOG", (required,))
    monkeypatch.setattr(verification, "OPTIONAL_MODEL_SCHEMA_GROUPS", ())
    monkeypatch.setattr(verification, "_strict_model_class", transient_candidate_import)
    monkeypatch.setattr(verification, "_load_model", load_candidate_model)

    with pytest.raises(
        ModelSchemaVerificationError,
        match=r"SCHEMA_SYNCHRONIZATION_FAILED:required\.schema\.json",
    ):
        verify_model_schemas(tmp_path)

    assert {
        schema_path: (schemas / schema_path).read_text(encoding="utf-8")
        for schema_path in contents
    } == contents


def test_active_model_schema_catalog_resolves_schema_root_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    original_resolve = Path.resolve
    resolutions = 0

    def reject_second_schema_root_resolution(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> Path:
        nonlocal resolutions
        if path == schemas:
            resolutions += 1
            if resolutions == 2:
                raise PermissionError("injected second schemas resolution failure")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", reject_second_schema_root_resolution)

    assert active_model_schema_catalog(tmp_path) == MODEL_SCHEMA_CATALOG
    assert resolutions == 1


def test_model_schema_helper_bounds_schema_root_access_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    original_resolve = Path.resolve

    def reject_schema_root_resolution(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> Path:
        if path == schemas:
            raise PermissionError("injected schemas resolution failure")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "resolve", reject_schema_root_resolution)

    assert main(["--repository", str(tmp_path)]) == 10

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "MODEL_SCHEMA_REPOSITORY_INVALID:.\n"


def test_model_schema_helper_accepts_current_candidate_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(__file__).parents[2]
    monkeypatch.chdir(repository)

    verify_model_schemas(repository)


def test_model_schema_helper_rejects_stale_candidate_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutil.copytree(Path(__file__).parents[2] / "schemas", tmp_path / "schemas")
    stale = tmp_path / "schemas" / "candidate-change.schema.json"
    schema = json.loads(stale.read_text(encoding="utf-8"))
    schema["required"].remove("operation")
    stale.write_text(json.dumps(schema), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(
        ModelSchemaVerificationError,
        match=r"SCHEMA_SYNCHRONIZATION_FAILED:candidate-change\.schema\.json",
    ):
        verify_model_schemas(tmp_path)


def test_model_schema_helper_rejects_repository_other_than_working_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ModelSchemaVerificationError,
        match="MODEL_SCHEMA_REPOSITORY_INVALID",
    ):
        verify_model_schemas(tmp_path)


def test_model_schema_helper_maps_import_failure_to_bounded_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutil.copytree(Path(__file__).parents[2] / "schemas", tmp_path / "schemas")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "ard_ossie.application.model_schema_verification.MODEL_SCHEMA_CATALOG",
        (
            ModelSchemaReference(
                MODEL_SCHEMA_CATALOG[0].schema_path,
                "candidate_module_that_does_not_exist",
                "CandidateChange",
            ),
        ),
    )

    with pytest.raises(
        ModelSchemaVerificationError,
        match=r"MODEL_SCHEMA_IMPORT_FAILED:candidate-change\.schema\.json",
    ):
        verify_model_schemas(tmp_path)


def test_model_schema_helper_rejects_non_strict_model_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutil.copytree(Path(__file__).parents[2] / "schemas", tmp_path / "schemas")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "ard_ossie.application.model_schema_verification.MODEL_SCHEMA_CATALOG",
        (
            ModelSchemaReference(
                MODEL_SCHEMA_CATALOG[0].schema_path,
                "json",
                "JSONDecoder",
            ),
        ),
    )

    with pytest.raises(
        ModelSchemaVerificationError,
        match=r"MODEL_SCHEMA_TYPE_INVALID:candidate-change\.schema\.json",
    ):
        verify_model_schemas(tmp_path)


def test_model_schema_helper_cli_emits_only_bounded_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--repository", str(tmp_path)]) == 10

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "MODEL_SCHEMA_REPOSITORY_INVALID:.\n"


@pytest.mark.parametrize(
    ("module_name", "failure"),
    [
        ("candidate_runtime_failure", "raise RuntimeError('candidate detail')"),
        ("candidate_early_success", "raise SystemExit(0)"),
    ],
)
def test_model_schema_helper_maps_unexpected_candidate_termination_to_bounded_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    module_name: str,
    failure: str,
) -> None:
    shutil.copytree(Path(__file__).parents[2] / "schemas", tmp_path / "schemas")
    (tmp_path / f"{module_name}.py").write_text(
        f"print('candidate output must be discarded')\n{failure}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(
        "ard_ossie.application.model_schema_verification.MODEL_SCHEMA_CATALOG",
        (
            ModelSchemaReference(
                MODEL_SCHEMA_CATALOG[0].schema_path,
                module_name,
                "CandidateChange",
            ),
        ),
    )

    assert main(["--repository", str(tmp_path)]) == 10

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "MODEL_SCHEMA_IMPORT_FAILED:candidate-change.schema.json\n"


def test_model_schema_helper_writes_full_catalog_receipt_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(__file__).parents[2]
    result = tmp_path / "receipt.json"
    nonce = "trusted-parent-nonce"
    monkeypatch.chdir(repository)

    assert (
        main(
            [
                "--repository",
                str(repository),
                "--result",
                str(result),
                "--nonce",
                nonce,
            ]
        )
        == 0
    )

    assert json.loads(result.read_text(encoding="utf-8")) == {
        "nonce": nonce,
        "schemas": [
            reference.schema_path.as_posix() for reference in MODEL_SCHEMA_CATALOG
        ],
        "status": "success",
    }
