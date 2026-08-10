# Initial Bootstrap Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a one-time, read-only GitHub Actions gate that validates PR #1 before the permanent `pull_request_target` verifier exists on `main`.

**Architecture:** A new `pull_request` workflow is hard-bound to PR #1, base commit `c23333610cb1d27ff136910de010011b6c870f3a`, same-repository branch `agent/design-numeric-versions-actions`, and the immutable trusted verifier commit `cb79416c4585d383181e75e7f87579bbf368ca65`. A matrix runs `static`, `pytest`, and `wheel` in separate read-only runners; a read-only aggregate check fails unless the complete matrix succeeds. Its check names are bootstrap-specific so skipped jobs can never satisfy the permanent `ard/quality-gate` or `ard/changeset` requirements.

**Tech Stack:** GitHub Actions, YAML, Python 3.12, uv 0.11.33, pytest, actionlint

## Global Constraints

- The workflow must use `pull_request`, never `pull_request_target`.
- Every job has only `contents: read`; no secrets, `GH_TOKEN`, status writes, PR writes, or persisted checkout credentials.
- Trusted CLI code is checked out only from `cb79416c4585d383181e75e7f87579bbf368ca65`.
- Candidate code is checked out at the exact event head SHA.
- `static`, `pytest`, and `wheel` run in separate matrix runners through `ard workflow repository-check`.
- All third-party actions are pinned to full 40-character commit SHAs.
- The workflow is inert outside PR #1 at the exact initial base and branch.
- Bootstrap check names must not equal `ard/quality-gate` or `ard/changeset`.

---

### Task 1: One-time read-only bootstrap workflow

**Files:**
- Create: `.github/workflows/ard-initial-bootstrap.yml`
- Modify: `tests/integration/test_workflow_contracts.py`
- Modify: `docs/github-actions-setup.md`

**Interfaces:**
- Consumes: immutable verifier CLI at commit `cb79416c4585d383181e75e7f87579bbf368ca65`; PR event base/head metadata.
- Produces: isolated `static`, `pytest`, and `wheel` check runs plus one bootstrap-specific aggregate check.

- [ ] **Step 1: Write the failing workflow contract test**

Add `ard-initial-bootstrap.yml` to the thin-workflow contract and add a focused test that parses the workflow and asserts the exact event, PR/base/branch guard, read-only permissions, immutable verifier ref, exact candidate ref, credential-free checkouts, three verification groups, pinned actions, non-mutating aggregate, and bootstrap-only check names.

- [ ] **Step 2: Run the focused test to verify RED**

Run: `uv run --frozen pytest tests/integration/test_workflow_contracts.py -q`

Expected: failure because `.github/workflows/ard-initial-bootstrap.yml` does not exist.

- [ ] **Step 3: Add the minimal workflow and operator documentation**

Create the guarded workflow with one isolated verification matrix and one `always()` aggregate that uses pinned `actions/github-script` to fail on any non-success matrix result. Document why the bootstrap exists, its immutable verifier pin, its read-only boundary, and its post-merge inert behavior.

- [ ] **Step 4: Run focused GREEN verification**

Run: `uv run --frozen pytest tests/integration/test_workflow_contracts.py -q`

Expected: all workflow contract tests pass.

- [ ] **Step 5: Run repository-wide verification**

Run the full pytest suite, Ruff, official actionlint, sdist/wheel build, and the integrated static repository verifier from a disposable clone of the final commit.

Expected: every command exits 0 and the worktree remains clean.

- [ ] **Step 6: Publish and observe the bootstrap run**

Commit only the workflow, contract test, operator documentation, and this plan. Fast-forward the existing PR branch without force, verify the remote tree equals the locally verified tree, then inspect the Actions run and jobs for the new exact head. If a job fails, use its logs as the next TDD input before making any completion claim.
