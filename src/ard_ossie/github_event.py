from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import unicodedata
import zipfile
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Literal
from urllib.parse import unquote_to_bytes, urljoin, urlsplit
from uuid import UUID

import httpx
import yaml
from pydantic import Field, model_validator

from ard_ossie.ids import new_id
from ard_ossie.models import Operation, ProductId, ProductKey, StrictModel, Version


class AttachmentSecurityError(ValueError):
    pass


class AuthorizationDecision(StrictModel):
    allowed: bool
    code: str


class IntakeAttachment(StrictModel):
    role: Literal["product_html", "semantic_document", "dictionary_excel"]
    filename: str
    url: str


class IssueIntake(StrictModel):
    operation: Operation
    product_key: ProductKey
    product_id: ProductId | None = None
    version: Version
    display_name: str
    description: str | None = None
    changeset_id: str | None = None
    change_reason: str
    attachments: dict[str, IntakeAttachment]

    @model_validator(mode="before")
    @classmethod
    def validate_raw_operation(cls, value: object) -> object:
        if isinstance(value, Mapping):
            operation = value.get("operation")
            normalized_operation = (
                operation.value if isinstance(operation, Operation) else str(operation).lower()
            )
            product_id = value.get("product_id")
            if (
                normalized_operation == Operation.CREATE.value
                and product_id is not None
                and str(product_id).strip()
            ):
                raise ValueError("PRODUCT_ID_FORBIDDEN_FOR_CREATE")
        return value

    @model_validator(mode="after")
    def validate_operation(self) -> IssueIntake:
        if self.operation is Operation.RETIRE:
            raise ValueError("RETIRE_NOT_SUPPORTED")
        if self.operation is Operation.UPDATE and self.product_id is None:
            raise ValueError(f"PRODUCT_ID_REQUIRED_FOR_{self.operation.value.upper()}")
        expected = {"product_html", "semantic_document", "dictionary_excel"}
        if set(self.attachments) != expected:
            raise ValueError("ISSUE_ATTACHMENTS_INCOMPLETE")
        return self


class DownloadedAttachment(StrictModel):
    role: str
    filename: str
    relative_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    source_url: str


class IntakeManifest(StrictModel):
    issue_number: int = Field(gt=0)
    product_key: ProductKey
    product_id: ProductId
    version: Version
    files: list[DownloadedAttachment]


_APPROVER_PERMISSIONS = frozenset({"admin", "maintain", "write"})
_HEADING_PATTERN = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_MARKDOWN_LINK_PATTERN = re.compile(r"^\s*\[([^\]]+)\]\((https://[^\s)]+)\)\s*$")
_USER_ASSET_PATH = re.compile(
    r"^/user-attachments/assets/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
_USER_FILE_PATH = re.compile(r"^/user-attachments/files/([1-9][0-9]*)/([^/]+)$")
_MALFORMED_PERCENT_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_ASSET_STORAGE_HOST = re.compile(
    r"^github-production-user-asset-[a-z0-9-]+\.s3\.amazonaws\.com$"
)
_FIELD_NAMES = {
    "Operation": "operation",
    "Product key": "product_key",
    "Existing product ID": "product_id",
    "Requested version": "version",
    "Display name": "display_name",
    "Description": "description",
    "Changeset ID": "changeset_id",
    "Product HTML": "product_html",
    "Semantic document": "semantic_document",
    "Data dictionary": "dictionary_excel",
    "Change reason": "change_reason",
}


def authorize_label(permission: str, label: str) -> AuthorizationDecision:
    if label != "ard:approved":
        return AuthorizationDecision(allowed=False, code="LABEL_NOT_APPROVAL")
    if permission.lower() not in _APPROVER_PERMISSIONS:
        return AuthorizationDecision(allowed=False, code="INSUFFICIENT_APPROVER_PERMISSION")
    return AuthorizationDecision(allowed=True, code="AUTHORIZED")


def validate_attachment_url(url: str) -> str:
    parsed = _validate_attachment_transport(url, allow_query=False)
    host = (parsed.hostname or "").lower()
    if host != "github.com":
        raise AttachmentSecurityError(f"UNTRUSTED_ATTACHMENT_HOST: {host}")
    _validate_user_attachment_path(parsed.path)
    return url


def _validate_attachment_redirect_url(url: str) -> str:
    parsed = _validate_attachment_transport(url, allow_query=True)
    host = (parsed.hostname or "").lower()
    if host == "github.com":
        if parsed.query:
            raise AttachmentSecurityError("ATTACHMENT_QUERY_FORBIDDEN")
        _validate_user_attachment_path(parsed.path)
        return url
    if host == "objects.githubusercontent.com" or _ASSET_STORAGE_HOST.fullmatch(host):
        if not parsed.path or parsed.path == "/":
            raise AttachmentSecurityError("ATTACHMENT_PATH_FORBIDDEN")
        return url
    raise AttachmentSecurityError(f"UNTRUSTED_ATTACHMENT_HOST: {host}")


def _attachment_request_headers(url: str) -> dict[str, str]:
    if (urlsplit(url).hostname or "").lower() != "github.com":
        return {}
    token = os.environ.get("GH_TOKEN")
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _validate_attachment_transport(url: str, *, allow_query: bool):
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise AttachmentSecurityError("ATTACHMENT_HTTPS_REQUIRED")
    if parsed.username or parsed.password:
        raise AttachmentSecurityError("ATTACHMENT_CREDENTIALS_FORBIDDEN")
    if (parsed.query and not allow_query) or parsed.fragment:
        raise AttachmentSecurityError("ATTACHMENT_QUERY_FORBIDDEN")
    if parsed.port not in (None, 443):
        raise AttachmentSecurityError("ATTACHMENT_PORT_FORBIDDEN")
    return parsed


def _validate_user_attachment_path(path: str) -> None:
    asset_match = _USER_ASSET_PATH.fullmatch(path)
    if asset_match is not None:
        value = asset_match.group(1)
        try:
            parsed = UUID(value)
        except ValueError as error:
            raise AttachmentSecurityError("UNTRUSTED_ATTACHMENT_PATH") from error
        if str(parsed) != value:
            raise AttachmentSecurityError("UNTRUSTED_ATTACHMENT_PATH")
        return

    file_match = _USER_FILE_PATH.fullmatch(path)
    if file_match is None:
        raise AttachmentSecurityError("UNTRUSTED_ATTACHMENT_PATH")
    encoded_filename = file_match.group(2)
    if _MALFORMED_PERCENT_ESCAPE.search(encoded_filename):
        raise AttachmentSecurityError("UNTRUSTED_ATTACHMENT_PATH")
    try:
        filename = unquote_to_bytes(encoded_filename).decode("utf-8")
    except UnicodeDecodeError as error:
        raise AttachmentSecurityError("UNTRUSTED_ATTACHMENT_PATH") from error
    if (
        not filename
        or len(filename) > 255
        or filename in {".", ".."}
        or filename.startswith(".")
        or "/" in filename
        or "\\" in filename
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
            for character in filename
        )
    ):
        raise AttachmentSecurityError("UNTRUSTED_ATTACHMENT_PATH")


def parse_issue_body(body: str) -> IssueIntake:
    matches = list(_HEADING_PATTERN.finditer(body))
    fields: dict[str, str] = {}
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        name = _FIELD_NAMES.get(heading)
        if name is None:
            continue
        if name in fields:
            raise ValueError(f"DUPLICATE_ISSUE_FIELD: {heading}")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        value = body[match.end() : end].strip()
        fields[name] = "" if value == "_No response_" else value

    required = {
        "operation",
        "product_key",
        "version",
        "display_name",
        "product_html",
        "semantic_document",
        "dictionary_excel",
        "change_reason",
    }
    missing = sorted(name for name in required if not fields.get(name))
    if missing:
        raise ValueError(f"MISSING_ISSUE_FIELD: {missing[0]}")

    attachments = {
        role: _parse_attachment(role, fields[role])
        for role in ("product_html", "semantic_document", "dictionary_excel")
    }
    try:
        version = int(fields["version"].removeprefix("v"))
    except ValueError as error:
        raise ValueError("INVALID_REQUESTED_VERSION") from error
    return IssueIntake(
        operation=fields["operation"].lower(),
        product_key=fields["product_key"],
        product_id=fields.get("product_id") or None,
        version=version,
        display_name=fields["display_name"],
        description=fields.get("description") or None,
        changeset_id=fields.get("changeset_id") or None,
        change_reason=fields["change_reason"],
        attachments=attachments,
    )


def download_attachment(
    attachment: IntakeAttachment,
    destination: str | Path,
    *,
    max_bytes: int = 50 * 1024 * 1024,
    client: httpx.Client | None = None,
    max_redirects: int = 5,
) -> DownloadedAttachment:
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    active_client = client or httpx.Client(timeout=120)
    owns_client = client is None
    current_url = validate_attachment_url(attachment.url)
    try:
        for redirect_count in range(max_redirects + 1):
            request = active_client.build_request("GET", current_url)
            request.headers.pop("authorization", None)
            request.headers.update(_attachment_request_headers(current_url))
            with closing(
                active_client.send(
                    request,
                    stream=True,
                    follow_redirects=False,
                )
            ) as response:
                if response.is_redirect:
                    if redirect_count == max_redirects:
                        raise AttachmentSecurityError("ATTACHMENT_TOO_MANY_REDIRECTS")
                    location = response.headers.get("location")
                    if not location:
                        raise AttachmentSecurityError("ATTACHMENT_REDIRECT_WITHOUT_LOCATION")
                    current_url = _validate_attachment_redirect_url(
                        urljoin(current_url, location)
                    )
                    continue
                response.raise_for_status()
                _validate_content_headers(attachment, response.headers, max_bytes=max_bytes)
                digest = hashlib.sha256()
                size = 0
                temp_path: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb", dir=target.parent, prefix=f".{target.name}.", delete=False
                    ) as handle:
                        temp_path = Path(handle.name)
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise AttachmentSecurityError("ATTACHMENT_TOO_LARGE")
                            digest.update(chunk)
                            handle.write(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                    _validate_downloaded_file(attachment, temp_path)
                    os.replace(temp_path, target)
                    temp_path = None
                finally:
                    if temp_path is not None:
                        temp_path.unlink(missing_ok=True)
                return DownloadedAttachment(
                    role=attachment.role,
                    filename=attachment.filename,
                    relative_path=target.as_posix(),
                    sha256=digest.hexdigest(),
                    size_bytes=size,
                    source_url=attachment.url,
                )
        raise AssertionError("redirect loop terminated unexpectedly")
    finally:
        if owns_client:
            active_client.close()


def prepare_issue_event(
    event_path: str | Path,
    workspace: str | Path,
    *,
    max_bytes: int = 50 * 1024 * 1024,
    client: httpx.Client | None = None,
) -> IntakeManifest:
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    issue = event["issue"]
    intake = parse_issue_body(issue["body"])
    product_id = intake.product_id or new_id("prd")
    product_root = Path(workspace) / "products" / intake.product_key
    existing_config: dict = {}
    config_path = product_root / "product.yaml"
    if intake.operation is Operation.UPDATE:
        if not config_path.is_file():
            raise ValueError("UPDATE_PRODUCT_CONFIG_NOT_FOUND")
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("UPDATE_PRODUCT_CONFIG_INVALID")
        existing_config = loaded
        if loaded.get("product_id") != product_id:
            raise ValueError("UPDATE_PRODUCT_ID_MISMATCH")
        if loaded.get("product_key") != intake.product_key:
            raise ValueError("UPDATE_PRODUCT_KEY_MISMATCH")
    sources = product_root / "sources"
    destinations = {
        "product_html": sources
        / "product-info"
        / _canonical_filename(intake.attachments["product_html"]),
        "semantic_document": sources
        / "semantic"
        / _canonical_filename(intake.attachments["semantic_document"]),
        "dictionary_excel": sources / "dictionary" / "dictionary.xlsx",
    }
    downloaded = []
    for role, attachment in intake.attachments.items():
        destination = destinations[role]
        record = download_attachment(
            attachment,
            destination,
            max_bytes=max_bytes,
            client=client,
        )
        downloaded.append(
            record.model_copy(
                update={"relative_path": destination.relative_to(product_root).as_posix()}
            )
        )
    for destination in destinations.values():
        for previous in destination.parent.iterdir():
            if previous.is_file() and previous != destination:
                previous.unlink()
    config = {
        **existing_config,
        "operation": intake.operation.value,
        "product_id": product_id,
        "product_key": intake.product_key,
        "version": intake.version,
        "display_name": intake.display_name,
        "description": (
            intake.description
            if intake.description is not None
            else existing_config.get("description")
        ),
        "changeset_id": intake.changeset_id,
        "tables": existing_config.get("tables", []),
    }
    if existing_config:
        config["base_version"] = existing_config.get("version")
    product_root.mkdir(parents=True, exist_ok=True)
    (product_root / "product.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    manifest = IntakeManifest(
        issue_number=int(issue["number"]),
        product_key=intake.product_key,
        product_id=product_id,
        version=intake.version,
        files=downloaded,
    )
    (product_root / "intake-manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return manifest


def _parse_attachment(role: str, value: str) -> IntakeAttachment:
    match = _MARKDOWN_LINK_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"INVALID_ATTACHMENT_MARKDOWN: {role}")
    filename, url = match.groups()
    if Path(filename).name != filename or filename.startswith("."):
        raise AttachmentSecurityError(f"UNSAFE_ATTACHMENT_FILENAME: {filename}")
    validate_attachment_url(url)
    _validate_extension(role, Path(filename).suffix.lower())
    return IntakeAttachment(role=role, filename=filename, url=url)


def _validate_extension(role: str, extension: str) -> None:
    allowed = {
        "product_html": {".html", ".htm"},
        "semantic_document": {".docx", ".pdf"},
        "dictionary_excel": {".xlsx"},
    }[role]
    if extension not in allowed:
        raise AttachmentSecurityError(f"ATTACHMENT_EXTENSION_MISMATCH: {role}")


def _validate_content_headers(
    attachment: IntakeAttachment, headers: httpx.Headers, *, max_bytes: int
) -> None:
    content_length = headers.get("content-length")
    if content_length and int(content_length) > max_bytes:
        raise AttachmentSecurityError("ATTACHMENT_TOO_LARGE")
    content_type = headers.get("content-type", "").partition(";")[0].lower()
    allowed = {
        "product_html": {"text/html", "application/octet-stream", ""},
        "semantic_document": {
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/octet-stream",
            "",
        },
        "dictionary_excel": {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/octet-stream",
            "",
        },
    }[attachment.role]
    if content_type not in allowed:
        raise AttachmentSecurityError(f"ATTACHMENT_MIME_MISMATCH: {attachment.role}:{content_type}")


def _validate_downloaded_file(attachment: IntakeAttachment, path: Path) -> None:
    head = path.read_bytes()[:512]
    extension = Path(attachment.filename).suffix.lower()
    if extension in {".docx", ".xlsx"} and not head.startswith(b"PK\x03\x04"):
        raise AttachmentSecurityError(f"ATTACHMENT_MAGIC_MISMATCH: {attachment.role}")
    if extension in {".docx", ".xlsx"}:
        try:
            with zipfile.ZipFile(path) as archive:
                members = set(archive.namelist())
                expanded_size = sum(item.file_size for item in archive.infolist())
                compressed_size = max(sum(item.compress_size for item in archive.infolist()), 1)
        except zipfile.BadZipFile as error:
            raise AttachmentSecurityError(
                f"ATTACHMENT_CONTAINER_MISMATCH: {attachment.role}"
            ) from error
        required_member = "word/document.xml" if extension == ".docx" else "xl/workbook.xml"
        if "[Content_Types].xml" not in members or required_member not in members:
            raise AttachmentSecurityError(f"ATTACHMENT_CONTAINER_MISMATCH: {attachment.role}")
        if expanded_size > 200 * 1024 * 1024 or expanded_size / compressed_size > 100:
            raise AttachmentSecurityError(f"ATTACHMENT_ZIP_BOMB: {attachment.role}")
    if extension == ".pdf" and not head.startswith(b"%PDF"):
        raise AttachmentSecurityError(f"ATTACHMENT_MAGIC_MISMATCH: {attachment.role}")
    if extension in {".html", ".htm"} and b"<" not in head:
        raise AttachmentSecurityError(f"ATTACHMENT_MAGIC_MISMATCH: {attachment.role}")


def _canonical_filename(attachment: IntakeAttachment) -> str:
    extension = Path(attachment.filename).suffix.lower()
    return f"product{extension}" if attachment.role == "product_html" else f"semantic{extension}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare"])
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    maximum = int(os.environ.get("ARD_MAX_ATTACHMENT_BYTES", 50 * 1024 * 1024))
    manifest = prepare_issue_event(arguments.event, arguments.workspace, max_bytes=maximum)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
