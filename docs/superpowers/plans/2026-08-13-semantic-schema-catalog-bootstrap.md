# Semantic Schema Catalog Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pre-authorize exactly the two semantic report schema references so PR #19 can add them atomically without weakening the trusted catalog boundary.

**Architecture:** Preserve the required `MODEL_SCHEMA_CATALOG` and add one trusted optional group containing both semantic report references. Static verification and the credential-free helper independently activate only the complete group, while the parent independently validates the nonce-bound receipt against candidate path presence.

**Tech Stack:** Python 3.12, Pydantic 2.12, JSON Schema Draft 2020-12, pytest 8.4, Ruff, GitHub Actions, uv

## Global Constraints

- Candidate code must never control or expand the trusted schema catalog.
- The static verifier must not import or execute candidate Python.
- The semantic fidelity and structure-repair references are one atomic group.
- The bootstrap must add no Pydantic model and no checked-in JSON Schema.
- Absent optional schemas keep current `main` fully valid.
- Present optional schemas must be compared with both candidate models in the credential-free helper.
- Receipt verification must be nonce-bound and independently derived by the trusted parent.
- Preserve all existing error codes and required status names.
- Follow RED-GREEN-REFACTOR for every behavior change.

---

### Task 1: Add trusted grouped schema-catalog activation

**Files:**
- Modify: `src/ard_ossie/application/model_schema_verification.py`
- Modify: `src/ard_ossie/application/repository_checks.py`
- Modify: `tests/unit/test_model_schema_verification.py`
- Modify: `tests/unit/test_repository_check_service.py`
- Modify: `docs/superpowers/specs/2026-08-13-semantic-schema-catalog-bootstrap-design.md`
- Modify: `docs/superpowers/plans/2026-08-13-semantic-schema-catalog-bootstrap.md`

**Interfaces:**
- Produces: `OPTIONAL_MODEL_SCHEMA_GROUPS: tuple[tuple[ModelSchemaReference, ...], ...]`
- Produces: `active_model_schema_catalog(repository: Path) -> tuple[ModelSchemaReference, ...]`
- Preserves: `MODEL_SCHEMA_CATALOG` as the required catalog
- Preserves: `verify_model_schemas(repository: Path) -> None`
- Preserves: `RepositoryVerificationTools.run(name: str) -> dict[str, object]`

- [ ] **Step 1: Add RED static-catalog tests**

Add tests that copy the current `schemas/` directory, then add two minimal
Draft 2020-12 JSON files at the exact semantic report paths and assert
`tools.run("schemas")` succeeds. Add parameterized partial-group cases that
create only one exact path and assert `SCHEMA_CATALOG_MISMATCH`. Keep the
existing unexpected-path test unchanged.

- [ ] **Step 2: Run the static tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-semantic-bootstrap-uv-cache uv run --frozen pytest -q \
  tests/unit/test_repository_check_service.py::test_static_schema_verifier_accepts_complete_preapproved_semantic_group \
  tests/unit/test_repository_check_service.py::test_static_schema_verifier_rejects_partial_preapproved_semantic_group
```

Expected: the complete pair fails with `SCHEMA_CATALOG_MISMATCH`; the new
optional-group interface is absent.

- [ ] **Step 3: Add RED helper and receipt tests**

Add tests that monkeypatch the required and optional catalogs to small fixture
models and schemas. Assert `active_model_schema_catalog()` returns only required
entries when the group is absent, returns required plus both entries when the
group is complete, and raises `ModelSchemaVerificationError` with
`SCHEMA_CATALOG_MISMATCH` for a partial group. Extend the repository-check
runner fixture so a receipt omitting present optional paths is rejected with
`REPOSITORY_MODEL_SCHEMAS_RECEIPT_INVALID`.

- [ ] **Step 4: Run the helper tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-semantic-bootstrap-uv-cache uv run --frozen pytest -q \
  tests/unit/test_model_schema_verification.py \
  tests/unit/test_repository_check_service.py -k 'model_schema or preapproved_semantic'
```

Expected: failures show the helper and parent still assume a fixed required-only
catalog.

- [ ] **Step 5: Implement the minimal trusted optional group**

In `model_schema_verification.py`, define the exact two-reference group and an
`active_model_schema_catalog(repository)` helper. Resolve the candidate schema
root through the existing safe candidate-root logic. For every group, include
all references when every schema exists, skip when none exists, and raise
`SCHEMA_CATALOG_MISMATCH` otherwise. Iterate the active catalog in
`verify_model_schemas()` and the receipt writer.

In `repository_checks.py`, use the trusted active-catalog derivation for both
static allowed-path comparison and parent receipt expectation. The parent must
derive the expected paths from `self.paths.root`; it must not accept a path list
declared only by the child.

- [ ] **Step 6: Run focused GREEN verification**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-semantic-bootstrap-uv-cache uv run --frozen pytest -q \
  tests/unit/test_model_schema_verification.py \
  tests/unit/test_repository_check_service.py
UV_CACHE_DIR=/tmp/ard-semantic-bootstrap-uv-cache uv run --frozen ruff check \
  src/ard_ossie/application/model_schema_verification.py \
  src/ard_ossie/application/repository_checks.py \
  tests/unit/test_model_schema_verification.py \
  tests/unit/test_repository_check_service.py
```

Expected: all focused tests pass and Ruff reports no errors.

- [ ] **Step 7: Run full bootstrap verification**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-semantic-bootstrap-uv-cache uv run --frozen pytest -q
UV_CACHE_DIR=/tmp/ard-semantic-bootstrap-uv-cache uv run --frozen ruff check .
UV_CACHE_DIR=/tmp/ard-semantic-bootstrap-uv-cache uv run --frozen ard workflow repository-check \
  --base-ref "$(git rev-parse main)" \
  --head-ref "$(git rev-parse HEAD)" \
  --head-sha "$(git rev-parse HEAD)" \
  --repository . \
  --verification-group static
```

Also run the focused model-schema and wheel integration gates and
`git diff --check`.

- [ ] **Step 8: Commit and publish the bootstrap PR**

Commit the spec/plan separately, then commit the reviewed implementation and
tests with terse messages. Push `agent/semantic-schema-catalog-bootstrap`, open
a non-draft PR to `main`, require the exact-head repository statuses to pass,
review the complete diff, and merge only after all required checks are green.
