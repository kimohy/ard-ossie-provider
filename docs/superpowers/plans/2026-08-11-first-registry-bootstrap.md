# First Registry Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow the first ARD product to validate against an absent `registry/` and let trusted processing create it atomically, without weakening path or symlink checks.

**Architecture:** Extend the filesystem port with one directory-specific resolver that can return a safe, absent path only when the caller opts in. Read-only modeling validation copies an existing registry or creates only an empty temporary staged registry; trusted processing passes the safe absent path to the existing atomic registry promotion logic. Existing files, symlinks, path escapes, and missing directories in non-bootstrap callers remain rejected.

**Tech Stack:** Python 3.12, pathlib, pytest, Typer, GitHub Actions, uv

## Global Constraints

- An absent `registry/` is treated as empty only when a caller explicitly sets `allow_missing=True`.
- Validation must not create the authoritative `registry/` directory.
- Trusted processing must use the existing `process_product` candidate-and-promotion path to create `registry/`.
- An existing regular file at `registry` must fail with `READ_PATH_TYPE_NOT_ALLOWED`.
- A symlink anywhere in the resolved registry path must fail with `SYMLINK_NOT_ALLOWED`.
- Existing products and an existing registry must retain their current behavior.

---

### Task 1: Safe first-registry validation and processing

**Files:**
- Modify: `src/ard_ossie/ports/filesystem.py`
- Modify: `src/ard_ossie/adapters/filesystem.py`
- Modify: `src/ard_ossie/application/modeling.py`
- Modify: `src/ard_ossie/application/source_check.py`
- Modify: `src/ard_ossie/application/processing.py`
- Modify: `tests/unit/test_filesystem_adapter.py`
- Modify: `tests/unit/test_source_check_service.py`
- Modify: `tests/unit/test_processing_service.py`
- Modify: `tests/e2e/test_approved_issue_to_release.py`

**Interfaces:**
- Consumes: `RepositoryPaths.resolve_read(path)` and `RepositoryPaths.resolve_write(path)` path-containment and symlink policies.
- Produces: `FileSystemPort.resolve_directory(path, *, allow_missing=False) -> Path`.
- Produces: `ModelingService.validate(product_path, registry_path)` support for a safe absent registry without authoritative writes.
- Produces: `ProcessingService.run(request)` support for a safe absent registry followed by normal atomic promotion.

- [ ] **Step 1: Write failing filesystem, validation, processing, and lifecycle tests**

Add tests that independently assert these literal outcomes:

```python
def test_resolve_directory_allows_safe_missing_path_only_when_requested(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    resolved = RepositoryPaths(root).resolve_directory("registry", allow_missing=True)
    assert resolved == root / "registry"
    assert not resolved.exists()


def test_source_check_treats_absent_registry_as_empty_without_creating_it(tmp_path: Path) -> None:
    create_product_fixture(tmp_path)
    result = SourceCheckService(RepositoryPaths(tmp_path)).run("sales-order", SHA)
    assert result.status is WorkflowStatus.SUCCESS
    assert not (tmp_path / "registry").exists()


def test_processing_creates_first_registry_through_atomic_promotion(tmp_path: Path) -> None:
    create_product_fixture(tmp_path)
    git = FakeGit()
    result = ProcessingService(
        RepositoryPaths(tmp_path), git, FakeGitHub(git), provider_factory=lambda: None
    ).run(request(tmp_path))
    assert result.status is WorkflowStatus.SUCCESS
    assert (tmp_path / "registry" / "products" / f"{PRODUCT_ID}.json").is_file()
```

Also remove the manual `(repository / "registry").mkdir()` bootstrap from the approved-Issue lifecycle test, and add filesystem cases proving missing-without-opt-in, regular-file, and symlink paths remain rejected.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-ossie-uv-cache uv run --frozen pytest -q \
  tests/unit/test_filesystem_adapter.py \
  tests/unit/test_source_check_service.py \
  tests/unit/test_processing_service.py \
  tests/e2e/test_approved_issue_to_release.py
```

Expected: failures caused by the absent `resolve_directory` API and the current `READ_PATH_NOT_FOUND` behavior for absent `registry/`.

- [ ] **Step 3: Implement the minimal safe directory resolver**

Add this protocol signature:

```python
def resolve_directory(
    self,
    path: str | Path,
    *,
    allow_missing: bool = False,
) -> Path: ...
```

Implement it by using `resolve_read` first, accepting only `READ_PATH_NOT_FOUND` when `allow_missing=True`, then resolving the absent path through `resolve_write`; finally require that any path which now exists is a directory. Do not create the path.

- [ ] **Step 4: Use the resolver in validation and processing**

Use `resolve_directory(..., allow_missing=True)` for read-only validation and trusted processing. Keep `model build` on `allow_missing=False`. In `_staged_state`, replace only the missing-registry copy with:

```python
if registry.exists():
    shutil.copytree(registry, staged_registry)
else:
    staged_registry.mkdir()
```

Do not add any direct `registry.mkdir()` call to source-check or processing.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the command from Step 2. Expected: all selected tests pass with no warnings or errors.

- [ ] **Step 6: Run complete verification**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-ossie-uv-cache uv run --frozen pytest -q
UV_CACHE_DIR=/tmp/ard-ossie-uv-cache uv run --frozen ruff check src tests
UV_CACHE_DIR=/tmp/ard-ossie-uv-cache uv build
```

Run the repository static verifier with its checksum-validated actionlint path, then verify the Git diff contains only the planned files.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/plans/2026-08-11-first-registry-bootstrap.md \
  src/ard_ossie/ports/filesystem.py src/ard_ossie/adapters/filesystem.py \
  src/ard_ossie/application/modeling.py src/ard_ossie/application/source_check.py \
  src/ard_ossie/application/processing.py tests/unit/test_filesystem_adapter.py \
  tests/unit/test_source_check_service.py tests/unit/test_processing_service.py \
  tests/e2e/test_approved_issue_to_release.py
git commit -m "fix: bootstrap the first registry safely"
```

After independent review, open a focused pull request, require the repository quality gate and changeset checks to pass on its exact head, merge it, and re-run Issue #3 until the managed PR publishes successful `ard/quality-gate` and `ard/changeset` statuses.
