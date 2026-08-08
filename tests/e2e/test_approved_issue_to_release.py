from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import httpx
from docx import Document
from openpyxl import Workbook

from ard_ossie.github_event import authorize_label, prepare_issue_event
from ard_ossie.pipeline import process_product
from ard_ossie.registry import Registry
from ard_ossie.release import build_release_bundle, resolve_release_plan

FIXTURES = Path("tests/fixtures/github")


def document_bytes() -> bytes:
    buffer = io.BytesIO()
    document = Document()
    document.add_heading("Order semantics", level=1)
    document.add_paragraph("An order is a confirmed customer purchase.")
    document.save(buffer)
    return buffer.getvalue()


def dictionary_bytes() -> bytes:
    buffer = io.BytesIO()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Dictionary"
    sheet.append(
        [
            "platform",
            "catalog",
            "schema",
            "table",
            "column",
            "data_type",
            "nullable",
            "pk",
            "description",
        ]
    )
    sheet.append(
        [
            "erp",
            "analytics",
            "sales",
            "orders",
            "order_id",
            "INT64",
            "false",
            "true",
            "Unique order identifier",
        ]
    )
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def attachment_transport() -> httpx.MockTransport:
    payloads = {
        "11111111-1111-1111-1111-111111111111": (
            b"<html><body><h1>Sales Order</h1><p>Order analytics.</p></body></html>",
            "text/html",
        ),
        "22222222-2222-2222-2222-222222222222": (
            document_bytes(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        "33333333-3333-3333-3333-333333333333": (
            dictionary_bytes(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.rsplit("/", 1)[-1]
        content, content_type = payloads[key]
        return httpx.Response(200, headers={"content-type": content_type}, content=content)

    return httpx.MockTransport(handler)


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_approved_issue_to_numeric_release_is_public_reproducible_and_traceable(
    tmp_path: Path,
) -> None:
    permission = json.loads((FIXTURES / "collaborator-permission.json").read_text(encoding="utf-8"))
    assert authorize_label(permission["permission"], "ard:approved").allowed

    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "ARD Test")
    git(repository, "config", "user.email", "ard-test@example.invalid")
    with httpx.Client(transport=attachment_transport()) as client:
        intake = prepare_issue_event(
            FIXTURES / "approved-issue.json",
            repository,
            client=client,
        )
    product_root = repository / "products" / intake.product_key
    git(repository, "add", "products")
    git(repository, "commit", "-m", "data: ingest approved issue #42")

    first = process_product(product_root, registry_root=repository / "registry")
    first_dictionary = json.loads(
        (product_root / "generated" / "data-dictionary.json").read_text(encoding="utf-8")
    )
    first_column_id = first_dictionary["tables"][0]["columns"][0]["column_id"]
    first_generated = {
        path.name: path.read_bytes() for path in (product_root / "generated").iterdir()
    }
    git(repository, "add", f"products/{intake.product_key}/generated")
    git(repository, "add", f"products/{intake.product_key}/quality")
    git(repository, "add", "registry")
    git(repository, "commit", "-m", "data: compile validated Ossie artifacts")

    second = process_product(product_root, registry_root=repository / "registry")
    second_dictionary = json.loads(
        (product_root / "generated" / "data-dictionary.json").read_text(encoding="utf-8")
    )
    assert second_dictionary["tables"][0]["columns"][0]["column_id"] == first_column_id
    assert {
        path.name: path.read_bytes() for path in (product_root / "generated").iterdir()
    } == first_generated

    registry = Registry.load(repository / "registry")
    product = registry.get_product(intake.product_id)
    assert product is not None and product.version == 1
    tables = registry.tables()
    assert len(tables) == 1 and tables[0].version == 1
    assert registry.mappings()[0].product_id == intake.product_id
    assert registry.mappings()[0].table_id == tables[0].table_id
    assert first.product_id == second.product_id == intake.product_id

    manifest = json.loads(
        (product_root / "generated" / "source-manifest.json").read_text(encoding="utf-8")
    )
    assert len(manifest["files"]) == 3
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])
    assert all(not item["relative_path"].startswith("/") for item in manifest["files"])

    plan = resolve_release_plan(
        intake.product_id,
        registry_root=repository / "registry",
        repository_root=repository,
    )
    assert plan.product_tag == f"product/{intake.product_id}/v1"
    assert plan.table_tags == [f"table/{tables[0].table_id}/v1"]
    bundle = build_release_bundle(product_root, repository / "dist" / "release.zip")
    assert bundle.is_file()
    git(repository, "tag", plan.product_tag)
    git(repository, "tag", plan.table_tags[0])
    assert git(repository, "rev-list", "--count", "HEAD") == "2"
    assert git(repository, "tag", "--list", "*/v1").splitlines() == [
        plan.product_tag,
        plan.table_tags[0],
    ]
    for relative_path in git(repository, "ls-files").splitlines():
        secret_assignment = b"ARD_LLM_API_KEY" + b"="
        assert secret_assignment not in (repository / relative_path).read_bytes()
