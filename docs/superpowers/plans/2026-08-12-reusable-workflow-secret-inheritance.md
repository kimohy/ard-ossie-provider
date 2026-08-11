# Reusable Workflow Secret Inheritance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Forward protected environment secrets across the two trusted reusable-workflow call boundaries so ARD processing receives `ARD_LLM_API_KEY`.

**Architecture:** The existing reusable processor remains the sole consumer of LLM secrets. Both trusted coordinators opt into GitHub's secret inheritance at their `jobs.process` call, while a parsed-YAML contract test fixes the caller and callee security boundary.

**Tech Stack:** GitHub Actions YAML, Python 3.12, PyYAML, unittest (pytest-collected in CI)

## Global Constraints

- Modify only the two trusted callers, one workflow contract test, and this change's design/plan documents.
- Keep `environment: ard-llm` on the reusable processor `process` job.
- Keep validation and finalize jobs free of `${{ secrets.* }}` references.
- Do not change Issue intake, source validation, processing, publication, or reconciliation code.

---

### Task 1: Enforce and repair reusable-workflow secret inheritance

**Files:**
- Create: `tests/unit/test_workflow_secret_contract.py`
- Modify: `.github/workflows/ard-issue-intake.yml`
- Modify: `.github/workflows/ard-direct-change.yml`

**Interfaces:**
- Consumes: GitHub reusable-workflow `jobs.<job_id>.secrets: inherit` syntax.
- Produces: Both trusted `process` calls forward repository/environment secrets to `ard-process.yml`; only its protected `process` job references them.

- [ ] **Step 1: Write the failing contract test**

Parse all three workflow files with `yaml.BaseLoader`. Assert both caller `process` jobs use
`./.github/workflows/ard-process.yml` and set `secrets` to `inherit`. Recursively inspect the
callee and assert only `jobs.process` contains secret-context expressions.

- [ ] **Step 2: Run the focused test to verify RED**

Run: `python -m unittest discover -s tests/unit -p 'test_workflow_secret_contract.py'`

Expected: both caller cases fail because `jobs.process.secrets` is absent.

- [ ] **Step 3: Apply the minimal workflow change**

Add this sibling key immediately after each reusable workflow `uses` entry:

```yaml
    secrets: inherit
```

- [ ] **Step 4: Run focused verification to verify GREEN**

Run: `python -m unittest discover -s tests/unit -p 'test_workflow_secret_contract.py'`

Expected: all contract tests pass.

- [ ] **Step 5: Publish and verify the exact change**

Create one commit from the exact `main` SHA, open a focused PR, and require the repository's
`static`, `pytest`, and `wheel` checks to succeed before merge. After merge, retry Issue #3,
approve `ard-llm` if GitHub requests it, and verify PR #5 receives generated artifacts and
successful `ard/quality-gate` plus `ard/changeset` statuses.
