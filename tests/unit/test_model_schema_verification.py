from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ard_ossie.application.model_schema_verification import (
    MODEL_SCHEMA_CATALOG,
    ModelSchemaReference,
    ModelSchemaVerificationError,
    main,
    verify_model_schemas,
)


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
