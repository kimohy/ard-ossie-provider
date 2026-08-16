from __future__ import annotations

import io
import json
import zipfile
from collections import UserDict
from pathlib import Path

import httpx
import pytest
import yaml

import ard_ossie.github_event as github_event_module
from ard_ossie.github_event import (
    AttachmentSecurityError,
    DownloadedAttachment,
    IntakeAttachment,
    IssueIntake,
    authorize_label,
    download_attachment,
    parse_issue_body,
    prepare_issue_event,
    validate_attachment_url,
)

ATTACHMENT_URL = "https://github.com/user-attachments/assets/11111111-1111-1111-1111-111111111111"
FILE_ATTACHMENT_URL = (
    "https://github.com/user-attachments/files/30932953/Marketing.Insight.Data.Dictionary.xlsx"
)


@pytest.fixture(autouse=True)
def private_attachment_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARD_ATTACHMENT_TOKEN", "fixture-attachment-token")


def issue_body() -> str:
    return """### Operation
create

### Product key
sales-order

### Existing product ID
_No response_

### Requested version
1

### Display name
Sales Order

### Description
Order analytics product

### Changeset ID
_No response_

### Product HTML
[product.html](https://github.com/user-attachments/assets/11111111-1111-1111-1111-111111111111)

### Semantic document
[semantic.docx](https://github.com/user-attachments/assets/22222222-2222-2222-2222-222222222222)

### Data dictionary
[dictionary.xlsx](https://github.com/user-attachments/assets/33333333-3333-3333-3333-333333333333)

### Change reason
Initial publication
"""


@pytest.mark.parametrize("permission", ["admin", "maintain", "write"])
def test_approved_label_requires_write_permission(permission: str) -> None:
    assert authorize_label(permission, "ard:approved").allowed


@pytest.mark.parametrize("permission", ["triage", "read", "none"])
def test_approved_label_rejects_lower_permissions(permission: str) -> None:
    assert not authorize_label(permission, "ard:approved").allowed


def test_attachment_rejects_non_github_host() -> None:
    with pytest.raises(AttachmentSecurityError, match="UNTRUSTED_ATTACHMENT_HOST"):
        validate_attachment_url("https://example.org/dictionary.xlsx")


@pytest.mark.parametrize(
    "url",
    [
        "https://raw.githubusercontent.com/acme/repo/main/product.html",
        "https://github.com/acme/repo/raw/main/dictionary.xlsx",
        "https://avatars.githubusercontent.com/u/1",
        "https://objects.githubusercontent.com/download/1",
        "https://objects.githubusercontent.com/download/1?signature=value",
        "https://github.com/user-attachments/assets/not-a-uuid",
    ],
)
def test_initial_attachment_requires_canonical_immutable_github_upload(url: str) -> None:
    with pytest.raises(AttachmentSecurityError):
        validate_attachment_url(url)


def test_initial_attachment_accepts_canonical_github_file_upload() -> None:
    assert validate_attachment_url(FILE_ATTACHMENT_URL) == FILE_ATTACHMENT_URL


@pytest.mark.parametrize(
    "path",
    [
        "/user-attachments/files/030932953/dictionary.xlsx",
        "/user-attachments/files/0/dictionary.xlsx",
        "/user-attachments/files/30932953",
        "/user-attachments/files/30932953/directory/dictionary.xlsx",
        "/user-attachments/files/30932953/directory%2Fdictionary.xlsx",
        "/user-attachments/files/30932953/directory%5Cdictionary.xlsx",
        "/user-attachments/files/30932953/%2E%2E",
        "/user-attachments/files/30932953/.dictionary.xlsx",
        "/user-attachments/files/30932953/dictionary%.xlsx",
        "/user-attachments/files/30932953/dictionary%FF.xlsx",
        "/user-attachments/files/30932953/%C2%85dictionary.xlsx",
        "/user-attachments/files/30932953/dictionary%E2%80%8E.xlsx",
    ],
)
def test_initial_attachment_rejects_noncanonical_github_file_path(path: str) -> None:
    with pytest.raises(AttachmentSecurityError, match="UNTRUSTED_ATTACHMENT_PATH"):
        validate_attachment_url(f"https://github.com{path}")


def test_initial_file_attachment_rejects_query_and_fragment() -> None:
    with pytest.raises(AttachmentSecurityError, match="ATTACHMENT_QUERY_FORBIDDEN"):
        validate_attachment_url(f"{FILE_ATTACHMENT_URL}?token=secret")
    with pytest.raises(AttachmentSecurityError, match="ATTACHMENT_QUERY_FORBIDDEN"):
        validate_attachment_url(f"{FILE_ATTACHMENT_URL}#fragment")


def test_initial_file_attachment_enforces_decoded_filename_length_boundary() -> None:
    prefix = "https://github.com/user-attachments/files/30932953/"
    valid_url = f"{prefix}{'a' * 250}.xlsx"
    invalid_url = f"{prefix}{'a' * 251}.xlsx"

    assert validate_attachment_url(valid_url) == valid_url
    with pytest.raises(AttachmentSecurityError, match="UNTRUSTED_ATTACHMENT_PATH"):
        validate_attachment_url(invalid_url)


def test_attachment_rejects_http_and_credentialed_url() -> None:
    with pytest.raises(AttachmentSecurityError, match="ATTACHMENT_HTTPS_REQUIRED"):
        validate_attachment_url(ATTACHMENT_URL.replace("https://", "http://"))
    with pytest.raises(AttachmentSecurityError, match="ATTACHMENT_CREDENTIALS_FORBIDDEN"):
        validate_attachment_url(ATTACHMENT_URL.replace("https://", "https://user:pass@"))
    with pytest.raises(AttachmentSecurityError, match="ATTACHMENT_QUERY_FORBIDDEN"):
        validate_attachment_url(f"{ATTACHMENT_URL}?token=secret")


def test_issue_form_requires_one_product_and_three_source_roles() -> None:
    intake = parse_issue_body(issue_body())

    assert intake.product_key == "sales-order"
    assert intake.version == 1
    assert set(intake.attachments) == {
        "product_html",
        "semantic_document",
        "dictionary_excel",
    }
    assert intake.attachments["dictionary_excel"].filename == "dictionary.xlsx"


def test_update_requires_existing_product_id() -> None:
    body = issue_body().replace("### Operation\ncreate", "### Operation\nupdate")

    with pytest.raises(ValueError, match="PRODUCT_ID_REQUIRED_FOR_UPDATE"):
        parse_issue_body(body)


@pytest.mark.parametrize(
    "product_id",
    [
        "Marketing Insight",
        "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631",
    ],
)
def test_create_forbids_existing_product_id_before_id_validation(product_id: str) -> None:
    body = issue_body().replace(
        "### Existing product ID\n_No response_",
        f"### Existing product ID\n{product_id}",
    )

    with pytest.raises(ValueError, match="PRODUCT_ID_FORBIDDEN_FOR_CREATE"):
        parse_issue_body(body)


def test_create_forbids_existing_product_id_from_generic_mapping() -> None:
    payload = parse_issue_body(issue_body()).model_dump()
    payload["product_id"] = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"

    with pytest.raises(ValueError, match="PRODUCT_ID_FORBIDDEN_FOR_CREATE"):
        IssueIntake.model_validate(UserDict(payload))


def test_retire_is_rejected_until_tombstone_pipeline_is_implemented() -> None:
    body = issue_body().replace("### Operation\ncreate", "### Operation\nretire")
    body = body.replace(
        "### Existing product ID\n_No response_",
        "### Existing product ID\nprd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631",
    )

    with pytest.raises(ValueError, match="RETIRE_NOT_SUPPORTED"):
        parse_issue_body(body)


def test_duplicate_issue_form_heading_is_rejected() -> None:
    body = issue_body() + "\n### Product key\nother-product\n"

    with pytest.raises(ValueError, match="DUPLICATE_ISSUE_FIELD"):
        parse_issue_body(body)


def test_download_validates_every_redirect_host(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/file.xlsx"})

    attachment = IntakeAttachment(
        role="dictionary_excel",
        filename="dictionary.xlsx",
        url=ATTACHMENT_URL,
    )
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(AttachmentSecurityError, match="UNTRUSTED_ATTACHMENT_HOST"),
    ):
        download_attachment(attachment, tmp_path / "dictionary.xlsx", client=client)


def test_download_accepts_canonical_github_file_upload_redirect(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "types")
        archive.writestr("xl/workbook.xml", "workbook")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/user-attachments/assets/"):
            return httpx.Response(302, headers={"location": FILE_ATTACHMENT_URL})
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=buffer.getvalue(),
        )

    attachment = IntakeAttachment(
        role="dictionary_excel",
        filename="dictionary.xlsx",
        url=ATTACHMENT_URL,
    )
    target = tmp_path / "dictionary.xlsx"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = download_attachment(attachment, target, client=client)

    assert result.size_bytes == len(buffer.getvalue())
    assert target.is_file()


def test_download_requires_attachment_token_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(404)

    monkeypatch.delenv("ARD_ATTACHMENT_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "must-not-be-used")
    attachment = IntakeAttachment(
        role="dictionary_excel",
        filename="dictionary.xlsx",
        url=FILE_ATTACHMENT_URL,
    )
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(AttachmentSecurityError, match="ATTACHMENT_TOKEN_REQUIRED"),
    ):
        download_attachment(attachment, tmp_path / "dictionary.xlsx", client=client)

    assert called is False


def test_download_explicit_attachment_token_overrides_environment_and_client_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<!doctype html><html><body>private product</body></html>",
        )

    monkeypatch.setenv("ARD_ATTACHMENT_TOKEN", "environment-attachment-token")
    monkeypatch.setenv("GH_TOKEN", "must-not-be-used")
    attachment = IntakeAttachment(
        role="product_html",
        filename="product.html",
        url=ATTACHMENT_URL,
    )
    target = tmp_path / "product.html"
    with httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer client-default"},
        auth=httpx.BasicAuth("client-user", "client-password"),
    ) as client:
        result = download_attachment(
            attachment,
            target,
            client=client,
            attachment_token="explicit-attachment-token",
        )

    assert result.size_bytes == target.stat().st_size
    assert requests[0].headers["authorization"] == "Bearer explicit-attachment-token"


@pytest.mark.parametrize(
    "storage_url",
    [
        "https://objects.githubusercontent.com/download/1?signature=value",
        (
            "https://github-production-user-asset-6210df.s3.amazonaws.com/"
            "asset.xlsx?X-Amz-Signature=value"
        ),
    ],
)
def test_download_authenticates_github_without_leaking_credentials_to_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    storage_url: str,
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "types")
        archive.writestr("xl/workbook.xml", "workbook")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "github.com":
            return httpx.Response(302, headers={"location": storage_url})
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=buffer.getvalue(),
        )

    monkeypatch.setenv("ARD_ATTACHMENT_TOKEN", "environment-attachment-token")
    monkeypatch.setenv("GH_TOKEN", "must-not-be-used")
    attachment = IntakeAttachment(
        role="dictionary_excel",
        filename="dictionary.xlsx",
        url=FILE_ATTACHMENT_URL,
    )
    target = tmp_path / "dictionary.xlsx"
    with httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer client-default"},
        auth=httpx.BasicAuth("client-user", "client-password"),
    ) as client:
        result = download_attachment(attachment, target, client=client)

    assert result.size_bytes == len(buffer.getvalue())
    assert requests[0].headers["authorization"] == "Bearer environment-attachment-token"
    assert "authorization" not in requests[1].headers


def test_download_rejects_declared_size_before_writing(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-length": "5000",
                "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            },
            content=b"PK\x03\x04",
        )

    attachment = IntakeAttachment(
        role="dictionary_excel",
        filename="dictionary.xlsx",
        url=ATTACHMENT_URL,
    )
    target = tmp_path / "dictionary.xlsx"
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(AttachmentSecurityError, match="ATTACHMENT_TOO_LARGE"),
    ):
        download_attachment(attachment, target, client=client, max_bytes=100)
    assert not target.exists()


@pytest.mark.parametrize(
    "location",
    [
        "https://objects.githubusercontent.com/download/1?signature=value",
        (
            "https://github-production-user-asset-6210df.s3.amazonaws.com/"
            "asset.xlsx?X-Amz-Signature=value"
        ),
    ],
)
def test_download_allows_signed_query_only_after_trusted_redirect(
    tmp_path,
    location: str,
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "types")
        archive.writestr("xl/workbook.xml", "workbook")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com":
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=buffer.getvalue(),
        )

    attachment = IntakeAttachment(
        role="dictionary_excel",
        filename="dictionary.xlsx",
        url=ATTACHMENT_URL,
    )
    target = tmp_path / "dictionary.xlsx"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = download_attachment(attachment, target, client=client)

    assert result.size_bytes == len(buffer.getvalue())
    assert target.is_file()


def test_download_rejects_mutable_github_redirect(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com":
            return httpx.Response(
                302,
                headers={
                    "location": ("https://raw.githubusercontent.com/acme/repo/main/dictionary.xlsx")
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=b"PK\x03\x04",
        )

    attachment = IntakeAttachment(
        role="dictionary_excel",
        filename="dictionary.xlsx",
        url=ATTACHMENT_URL,
    )
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(AttachmentSecurityError, match="UNTRUSTED_ATTACHMENT_HOST"),
    ):
        download_attachment(attachment, tmp_path / "dictionary.xlsx", client=client)


def test_update_preserves_table_config_and_replaces_old_role_extensions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product_id = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
    product_root = tmp_path / "products" / "sales-order"
    for directory in ("product-info", "semantic", "dictionary"):
        (product_root / "sources" / directory).mkdir(parents=True, exist_ok=True)
    (product_root / "sources" / "product-info" / "product.htm").write_text("old")
    (product_root / "sources" / "semantic" / "semantic.pdf").write_bytes(b"%PDF-old")
    (product_root / "product.yaml").write_text(
        yaml.safe_dump(
            {
                "operation": "create",
                "product_id": product_id,
                "product_key": "sales-order",
                "version": 4,
                "display_name": "Old name",
                "synonyms": ["orders"],
                "tables": [{"locator": "erp|analytics|sales|orders", "version": 2}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    body = issue_body().replace("### Operation\ncreate", "### Operation\nupdate")
    body = body.replace(
        "### Existing product ID\n_No response_",
        f"### Existing product ID\n{product_id}",
    ).replace("### Requested version\n1", "### Requested version\n5")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"issue": {"number": 17, "body": body}}), encoding="utf-8")

    def fake_download(attachment, destination, **kwargs):
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"new-content")
        return DownloadedAttachment(
            role=attachment.role,
            filename=attachment.filename,
            relative_path=target.as_posix(),
            sha256="a" * 64,
            size_bytes=11,
            source_url=attachment.url,
        )

    monkeypatch.setattr(github_event_module, "download_attachment", fake_download)

    prepare_issue_event(event, tmp_path)
    updated = yaml.safe_load((product_root / "product.yaml").read_text(encoding="utf-8"))

    assert updated["base_version"] == 4
    assert updated["version"] == 5
    assert updated["synonyms"] == ["orders"]
    assert updated["tables"] == [{"locator": "erp|analytics|sales|orders", "version": 2}]
    assert not (product_root / "sources" / "product-info" / "product.htm").exists()
    assert not (product_root / "sources" / "semantic" / "semantic.pdf").exists()
    assert (product_root / "sources" / "product-info" / "product.html").is_file()
    assert (product_root / "sources" / "semantic" / "semantic.docx").is_file()
