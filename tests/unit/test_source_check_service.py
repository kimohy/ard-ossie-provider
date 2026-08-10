from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.application.contracts import (
    WorkflowSecurityError,
    WorkflowStatus,
    WorkflowValidationError,
)
from ard_ossie.application.source_check import (
    DetectProductService,
    EnsureProductPrService,
    SourceCheckService,
)
from ard_ossie.ports.git import ChangedPaths
from ard_ossie.ports.github import PullRequestState
from tests.integration.test_cli_process import create_product_fixture

SHA = "a" * 40


class FakeGit:
    def __init__(self, paths: tuple[str, ...] = ()) -> None:
        self.paths = paths
        self.remote_sha = SHA

    def changed_paths(self, base_ref: str, head_ref: str = "HEAD") -> ChangedPaths:
        return ChangedPaths(merge_base="b" * 40, paths=tuple(Path(item) for item in self.paths))

    def current_sha(self) -> str:
        return SHA

    def remote_branch_sha(self, branch: str) -> str | None:
        return self.remote_sha


class FakeGitHub:
    def __init__(self) -> None:
        self.pull_request: PullRequestState | None = None
        self.created = 0

    def find_open_pr(self, branch: str) -> PullRequestState | None:
        return self.pull_request

    def create_draft_pr(
        self,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestState:
        self.created += 1
        self.pull_request = PullRequestState(
            number=9,
            head_branch=branch,
            head_sha=SHA,
            base_branch=base,
            draft=True,
            merged_at=None,
            merge_sha=None,
            url="https://example.invalid/pull/9",
        )
        return self.pull_request


@pytest.mark.parametrize(
    "paths,expected",
    [
        (("products/sales-order/sources/product.html",), "sales-order"),
        (("README.md",), None),
    ],
)
def test_detect_product(paths: tuple[str, ...], expected: str | None) -> None:
    result = DetectProductService(FakeGit(paths)).run("origin/main", "HEAD")

    assert result.outputs.get("product_key") == expected
    assert result.outputs["expected_head"] == SHA


def test_detect_product_rejects_multiple_products() -> None:
    git = FakeGit(
        (
            "products/a/sources/a.html",
            "products/b/sources/b.html",
        )
    )

    with pytest.raises(WorkflowValidationError, match="MULTIPLE_PRODUCTS_NOT_ALLOWED"):
        DetectProductService(git).run("origin/main", "HEAD")


def test_detect_product_allows_canonical_changeset_marker_with_sources() -> None:
    result = DetectProductService(
        FakeGit(
            (
                "products/sales-order/changesets/"
                "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2.json",
                "products/sales-order/product.yaml",
                "products/sales-order/sources/product/product.html",
            )
        )
    ).run("origin/main", "HEAD")

    assert result.outputs["product_key"] == "sales-order"


def test_detect_product_rejects_mixed_code_and_data() -> None:
    git = FakeGit(("README.md", "products/sales/sources/product.html"))

    with pytest.raises(WorkflowValidationError, match="MIXED_CODE_AND_ARD_CHANGES"):
        DetectProductService(git).run("origin/main", "HEAD")


def test_source_check_is_read_only_and_secret_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_product_fixture(tmp_path)
    registry = tmp_path / "registry"
    registry.mkdir()
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    result = SourceCheckService(RepositoryPaths(tmp_path)).run("sales-order", SHA)

    assert result.status is WorkflowStatus.SUCCESS
    assert result.outputs["expected_head"] == SHA
    assert result.outputs["source_count"] == 3
    assert {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and not path.is_relative_to(tmp_path / ".ard")
    } == before

    monkeypatch.setenv("ARD_LLM_API_KEY", "must-not-be-present")
    with pytest.raises(WorkflowSecurityError, match="SOURCE_CHECK_LLM_SECRET_PRESENT"):
        SourceCheckService(RepositoryPaths(tmp_path)).run("sales-order", SHA)


def test_source_check_requires_marker_and_product_config_binding(tmp_path: Path) -> None:
    product = create_product_fixture(tmp_path)
    (tmp_path / "registry").mkdir()
    marker = product / "changesets" / (
        "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2.json"
    )
    marker.parent.mkdir()
    marker.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkflowSecurityError, match="CHANGESET_BINDING_REQUIRED"):
        SourceCheckService(RepositoryPaths(tmp_path)).run("sales-order", SHA)


def test_source_check_accepts_exact_changeset_binding(tmp_path: Path) -> None:
    changeset_id = "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2"
    product = create_product_fixture(tmp_path)
    (tmp_path / "registry").mkdir()
    config_path = product / "product.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["changeset_id"] = changeset_id
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    marker = product / "changesets" / f"{changeset_id}.json"
    marker.parent.mkdir()
    marker.write_text(
        json.dumps(
            {
                "changeset_id": changeset_id,
                "product_id": config["product_id"],
                "status": "required",
            }
        ),
        encoding="utf-8",
    )

    result = SourceCheckService(RepositoryPaths(tmp_path)).run("sales-order", SHA)

    assert result.outputs["changeset_id"] == changeset_id


def test_ensure_product_pr_is_idempotent_for_exact_remote_head() -> None:
    git = FakeGit()
    github = FakeGitHub()
    service = EnsureProductPrService(git, github)

    first = service.run("feature/sales", "sales-order", SHA, base_branch="main")
    second = service.run("feature/sales", "sales-order", SHA, base_branch="main")

    assert first.outputs == second.outputs
    assert second.status is WorkflowStatus.NOOP
    assert github.created == 1


def test_ensure_product_pr_rejects_stale_remote_head() -> None:
    git = FakeGit()
    git.remote_sha = "c" * 40

    with pytest.raises(WorkflowSecurityError, match="DIRECT_BRANCH_HEAD_MISMATCH"):
        EnsureProductPrService(git, FakeGitHub()).run(
            "feature/sales",
            "sales-order",
            SHA,
            base_branch="main",
        )
