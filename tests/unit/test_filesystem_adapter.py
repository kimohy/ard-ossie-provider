from __future__ import annotations

from pathlib import Path

import pytest

from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.ports.filesystem import PathPolicyError


def test_repository_paths_resolves_existing_read_below_root(tmp_path: Path) -> None:
    """A trusted source path should resolve to one canonical repository path."""
    root = tmp_path / "repo"
    source = root / "products" / "sales-order" / "sources" / "product.html"
    source.parent.mkdir(parents=True)
    source.write_text("source", encoding="utf-8")

    assert RepositoryPaths(root).resolve_read(source.relative_to(root)) == source.resolve()


def test_repository_paths_rejects_parent_escape(tmp_path: Path) -> None:
    """A parent traversal must never resolve into a writable path."""
    root = tmp_path / "repo"
    root.mkdir()

    with pytest.raises(PathPolicyError, match="PATH_OUTSIDE_REPOSITORY"):
        RepositoryPaths(root).resolve_write(Path("../secret"))


@pytest.mark.parametrize("operation", ["read", "write"])
def test_repository_paths_rejects_symlink_ancestor(tmp_path: Path, operation: str) -> None:
    """A symlink below the lexical root must not redirect reads or writes."""
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "file").write_text("secret", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)
    paths = RepositoryPaths(root)

    with pytest.raises(PathPolicyError, match="SYMLINK_NOT_ALLOWED"):
        if operation == "read":
            paths.resolve_read(Path("link/file"))
        else:
            paths.resolve_write(Path("link/file"))


def test_repository_paths_rejects_git_metadata_write(tmp_path: Path) -> None:
    """Workflow writeback must never modify repository control data."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)

    with pytest.raises(PathPolicyError, match="GIT_METADATA_WRITE_NOT_ALLOWED"):
        RepositoryPaths(root).resolve_write(Path(".git/config"))


def test_repository_paths_requires_existing_regular_read_target(tmp_path: Path) -> None:
    """A missing read target must not be mistaken for an empty source."""
    root = tmp_path / "repo"
    root.mkdir()

    with pytest.raises(PathPolicyError, match="READ_PATH_NOT_FOUND"):
        RepositoryPaths(root).resolve_read(Path("missing"))


def test_resolve_directory_allows_safe_missing_path_only_when_requested(
    tmp_path: Path,
) -> None:
    """Bootstrap callers may inspect an absent directory without creating it."""
    root = tmp_path / "repo"
    root.mkdir()

    resolved = RepositoryPaths(root).resolve_directory("registry", allow_missing=True)

    assert resolved == root / "registry"
    assert not resolved.exists()


def test_resolve_directory_requires_explicit_missing_path_opt_in(tmp_path: Path) -> None:
    """All existing callers retain fail-closed missing-directory behavior."""
    root = tmp_path / "repo"
    root.mkdir()

    with pytest.raises(PathPolicyError, match="READ_PATH_NOT_FOUND"):
        RepositoryPaths(root).resolve_directory("registry")


def test_resolve_directory_rejects_existing_regular_file(tmp_path: Path) -> None:
    """A file must never be interpreted as an empty registry directory."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "registry").write_text("not a directory", encoding="utf-8")

    with pytest.raises(PathPolicyError, match="READ_PATH_TYPE_NOT_ALLOWED"):
        RepositoryPaths(root).resolve_directory("registry", allow_missing=True)


def test_resolve_directory_rejects_symlink(tmp_path: Path) -> None:
    """Bootstrap support must retain the repository symlink boundary."""
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "registry").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathPolicyError, match="SYMLINK_NOT_ALLOWED"):
        RepositoryPaths(root).resolve_directory("registry", allow_missing=True)


@pytest.mark.parametrize(
    "candidate,allowed",
    [
        ("registry/products/prd_example.json", True),
        ("products/sales-order/product.yaml", True),
        ("products/sales-order/generated/data-product.md", True),
        ("products/sales-order/quality/quality-report.json", True),
        ("products/finance-order/generated/data-product.md", False),
        ("products/sales-order/sources/product.html", False),
        (".ard/run/result.json", False),
        ("README.md", False),
    ],
)
def test_writeback_scope_is_exact(
    tmp_path: Path,
    candidate: str,
    allowed: bool,
) -> None:
    """Only registry and the selected product's approved generated paths may be committed."""
    root = tmp_path / "repo"
    root.mkdir()

    assert RepositoryPaths(root).is_writeback_allowed(candidate, "sales-order") is allowed


@pytest.mark.parametrize(
    "candidate,allowed",
    [
        ("products/sales-order/product.yaml", True),
        ("products/sales-order/intake-manifest.json", True),
        ("products/sales-order/sources/product-info/product.html", True),
        ("products/sales-order/generated/data-product.md", False),
        ("products/other/sources/product.html", False),
        ("README.md", False),
    ],
)
def test_intake_scope_is_separate_from_processing_writeback(
    tmp_path: Path,
    candidate: str,
    allowed: bool,
) -> None:
    """Issue intake may commit sources but must not inherit processor output authority."""
    root = tmp_path / "repo"
    root.mkdir()

    assert RepositoryPaths(root).is_intake_write_allowed(candidate, "sales-order") is allowed


@pytest.mark.parametrize(
    "candidate,product_key,allowed",
    [
        ("registry/changesets/cst_example.json", None, True),
        ("registry/products/prd_example.json", None, False),
        ("products/sales-order/changesets/cst_example.json", "sales-order", True),
        ("products/sales-order/product.yaml", "sales-order", False),
        ("products/other/changesets/cst_example.json", "sales-order", False),
    ],
)
def test_changeset_scope_allows_only_central_or_one_tracking_marker(
    tmp_path: Path,
    candidate: str,
    product_key: str | None,
    allowed: bool,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    assert RepositoryPaths(root).is_changeset_write_allowed(
        candidate,
        "cst_example",
        product_key,
    ) is allowed
