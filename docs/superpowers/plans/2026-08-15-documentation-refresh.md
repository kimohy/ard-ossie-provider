# Repository Documentation Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the repository documentation accurately describe the shipped semantic PDF, LLM recovery, audit, GitHub processing, and immutable release behavior, with README as the first-time adopter entry point.

**Architecture:** Keep normative policy, technical pipeline, and operational procedures in separate focused documents. Make README a concise navigation and quick-start layer, preserve historical plans/specifications unchanged, and validate every current-behavior claim against code, workflow definitions, schemas, tests, and the verified Issue #3 artifacts.

**Tech Stack:** Markdown, Python 3.12, Typer CLI, GitHub Actions, JSON Schema, pytest, Ruff, GitHub CLI.

## Global Constraints

- Treat schemas and executable invariants as more authoritative than prose.
- Do not change runtime code, schemas, workflow behavior, or release policy in this documentation branch.
- Do not rewrite historical files under `docs/superpowers/specs/` or `docs/superpowers/plans/`, apart from this approved design and plan.
- Do not include credentials, raw provider responses, unmasked source text, or private diagnostic content.
- Distinguish a recoverable `WARN` or recorded review debt from a publication-blocking `FAIL`.
- Keep every non-historical documentation file reachable from `README.md`.

---

### Task 1: Build the current-behavior coverage map

**Files:**
- Read: `src/ard_ossie/semantic/*.py`
- Read: `src/ard_ossie/application/processing.py`
- Read: `src/ard_ossie/application/release_publication.py`
- Read: `src/ard_ossie/application/release_dispatch.py`
- Read: `.github/workflows/*.yml`
- Read: `schemas/reports/*.json`
- Read: `products/500138301/quality/*.json`
- Read: `README.md`
- Read: `docs/github-actions-setup.md`
- Read: `docs/next-steps.md`
- Read: `docs/operations/semantic-pdf-rollout.md`

**Interfaces:**
- Consumes: current `main` at or after `28f943db0afb28b820cf67818bd1b945c75c6765`.
- Produces: a working coverage map used by Tasks 2-5, with policy, reference, how-to, and explanation gaps identified.

- [ ] **Step 1: Inventory the public surface**

Record the exact pipeline modes, environment variables, CLI commands, decision states, validation outcomes, quality artifacts, release tags, and dispatch status contract found in current code.

- [ ] **Step 2: Verify Issue #3 facts**

Read the committed Issue #3 validation, semantic fidelity, decision, application, and quality reports. Record only aggregate facts used in documentation: publishability, coverage, missing/duplicate/degraded counts, heading and table counts, decision terminal states, and warning classes.

- [ ] **Step 3: Classify documentation gaps**

Create an internal matrix for these public surfaces: source authority, candidate adjudication, low-confidence recovery, whitespace generation, safe fallback, heading hierarchy, global validation, diagnostics privacy, optional metric rejection, GitHub trust boundary, release publication, and downstream dispatch.

- [ ] **Step 4: Confirm historical boundaries**

Verify that historical plans/specifications are linked as records and are not used as current normative documentation.

### Task 2: Write normative policy and governance documentation

**Files:**
- Create: `docs/policy-and-governance.md`

**Interfaces:**
- Consumes: Task 1 policy findings.
- Produces: the normative policy reference linked by all user-facing docs.

- [ ] **Step 1: Define policy authority and scope**

Add sections named `문서의 지위`, `Source authority`, `결정적 코드와 LLM의 권한`, `저신뢰와 검토 부채`, `게시 판정`, `감사와 개인정보`, and `버전·릴리스·재시도`.

- [ ] **Step 2: State the conversion-continuation rule**

Document that unresolved model judgment does not stop conversion when an invariant-safe deterministic fallback exists. Require the unresolved judgment and fallback application to remain auditable. State that no fallback may be published if every candidate violates deterministic invariants.

- [ ] **Step 3: State the LLM repair boundary**

Document allowlisted candidate selection, bounded low-confidence recovery, whitespace-only generation, independent verification, protected-token preservation, character-sequence equality, and global validation precedence.

- [ ] **Step 4: State optional suggestion policy**

Document that unsafe optional metric SQL is excluded and warned, while physical schema, identity, relationships, and mandatory fidelity failures remain blocking.

- [ ] **Step 5: Validate policy terminology**

Run:

```bash
rg -n "source authority|review_required|deferred_review|WARN|FAIL|LLM_METRIC_SQL_UNSAFE|immutable" docs/policy-and-governance.md
```

Expected: every required policy concept appears with an explicit outcome.

### Task 3: Write the semantic PDF pipeline reference

**Files:**
- Create: `docs/semantic-pdf-pipeline.md`

**Interfaces:**
- Consumes: Task 1 technical findings and Task 2 policy terms.
- Produces: current architecture, report reference, and operator-facing interpretation of pipeline states.

- [ ] **Step 1: Explain the end-to-end data flow**

Describe embedded-text versus whole-document OCR selection, immutable atoms/spans, candidate generation, adjudication, bounded recovery, generated spacing repair, canonical assembly, hierarchy-derived headings, invariant validation, atomic publication, release, and dispatch.

- [ ] **Step 2: Document state semantics**

Add a table covering `selected`, `deferred_review`, `review_required`, `PASS`, `WARN`, and `FAIL`. Explain which state controls a decision and which controls document publication.

- [ ] **Step 3: Document invariants**

Cover exact character coverage, single ownership, order, hard line boundaries, table grids, protected email/URL/date/alphanumeric/unit tokens, Markdown-visible escapes, raw HTML, stable canonical hashes, and source/configuration binding.

- [ ] **Step 4: Document report contracts**

List every generated and quality artifact currently committed for Issue #3. Explain the purpose of candidate, decision, application, validation, failure, evidence, manifest, semantic fidelity, and optional structure-repair records.

- [ ] **Step 5: Add Issue #3 verified example**

Record the verified aggregate result: five pages, coverage `1.0`, zero unmatched/duplicated/degraded blocks, 12 headings with levels `[1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]`, 10 tables, 65 selected decisions, zero unresolved decisions, no visible escapes, and no raw HTML. Explain that the remaining warning is non-blocking audited LLM application or excluded unsafe optional SQL, not source loss.

### Task 4: Update operational and GitHub documentation

**Files:**
- Modify: `docs/github-actions-setup.md`
- Modify: `docs/operations/semantic-pdf-rollout.md`
- Modify: `docs/next-steps.md`

**Interfaces:**
- Consumes: Tasks 2-3 normative terms and report names.
- Produces: current setup, incident handling, rollback, release recovery, and roadmap guidance.

- [ ] **Step 1: Update GitHub configuration**

Document `ARD_SEMANTIC_PDF_PIPELINE`, protected `ard-llm` execution, credential-free validation, review-debt continuation, exact result envelopes, annotated-tag identity, immutable tag conflicts, exit `30` retry, exit `70` convergence, and path-filtered numeric release behavior.

- [ ] **Step 2: Update the rollout runbook**

Replace the assumption that every `review_required` blocks conversion. Add triage for invariant-safe fallback, no-safe-fallback failure, optional metric warnings, verifier execution, release tag failure, manual recovery requirements, and idempotency checks.

- [ ] **Step 3: Update next steps**

Move Issue #3 ingestion, general candidate pipeline, low-confidence recovery, table-cell spacing repair, heading hierarchy, optional metric isolation, verified v1 artifact, immutable tags, GitHub Release, and downstream dispatch into a completed section. Retain only uncompleted operational hardening and product backlog.

- [ ] **Step 4: Check setup names against code**

Run:

```bash
rg -o "ARD_[A-Z0-9_]+" README.md docs/github-actions-setup.md docs/operations/semantic-pdf-rollout.md | sort -u
rg -n "ARD_[A-Z0-9_]+" config src/ard_ossie .github/workflows
```

Expected: every documented runtime or repository variable has a current implementation or an explicit legacy-removal note.

### Task 5: Rewrite README as the adopter entry point

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: Tasks 2-4 documents.
- Produces: concise project overview, quick start, workflow summary, guarantees, troubleshooting, and complete documentation index.

- [ ] **Step 1: Replace the opening and feature inventory**

Lead with the user outcome, then list supported source documents, generated outputs, and the five core guarantees: lossless authority, deterministic ownership, bounded LLM assistance, auditable continuation, and immutable publication.

- [ ] **Step 2: Add concise processing flows**

Describe Issue approval and direct trusted-branch workflows without duplicating the setup runbook. State that merge closes the Issue only through `Closes #N`, and failed processing does not justify closing it.

- [ ] **Step 3: Add quick start and result interpretation**

Keep install/process/help/smoke commands. Add a short `PASS`/`WARN`/`FAIL` and exit-code table. Move long semantic acceptance scripts to the operations document.

- [ ] **Step 4: Add the documentation map**

Link policy, pipeline, GitHub setup, rollout/rollback, roadmap, architecture history, design records, and implementation plans. Label the last three as historical records.

- [ ] **Step 5: Check README scope**

Run:

```bash
wc -l README.md
rg -n "policy-and-governance|semantic-pdf-pipeline|github-actions-setup|semantic-pdf-rollout|next-steps" README.md
```

Expected: README remains an entry point rather than duplicating full policy or operations content, and links every current documentation page.

### Task 6: Validate, review, and publish the documentation change

**Files:**
- Verify: `README.md`
- Verify: `docs/policy-and-governance.md`
- Verify: `docs/semantic-pdf-pipeline.md`
- Verify: `docs/github-actions-setup.md`
- Verify: `docs/operations/semantic-pdf-rollout.md`
- Verify: `docs/next-steps.md`

**Interfaces:**
- Consumes: all documentation tasks.
- Produces: a clean documentation commit and reviewable PR.

- [ ] **Step 1: Validate local Markdown links**

Run a repository script that extracts relative Markdown links from current docs, ignores external URLs and anchors, and fails if the target path does not exist.

- [ ] **Step 2: Validate factual strings**

Compare documented artifact filenames, pipeline modes, CLI command names, environment variables, labels, status contexts, and exit codes against code and workflow definitions with `rg` and targeted pytest tests.

- [ ] **Step 3: Run repository verification**

Run:

```bash
uv run --frozen pytest -q tests/unit/test_issue_3_verifier.py tests/unit/semantic tests/integration/test_semantic_pdf_v2.py tests/integration/test_workflow_contracts.py
uv run --frozen ruff check .
git diff --check
```

Expected: all selected tests and checks pass.

- [ ] **Step 4: Review the documentation diff**

Check for contradictory policy statements, stale links, secrets, raw source payloads, claims unsupported by current tests, and historical files accidentally modified.

- [ ] **Step 5: Commit and publish**

Stage only the approved Markdown files, commit with a documentation-specific message, push `docs/semantic-pipeline-refresh`, open a PR, request independent review, wait for repository checks, and merge only when the gate is clean.
