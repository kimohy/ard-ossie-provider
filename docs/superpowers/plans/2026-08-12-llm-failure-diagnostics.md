# Safe LLM Failure Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface safe, actionable LLM failure codes and preserve the original non-partial process failure across reconciliation.

**Architecture:** Normalize OpenAI SDK and structured-output failures into typed provider errors at the LLM boundary, then map their failure kind onto existing workflow exit codes. Record the original numeric exit code in the trusted process envelope and have reconciliation replay non-partial failures while retaining existing post-commit recovery.

**Tech Stack:** Python 3.12, OpenAI Python SDK 2.46.0, httpx, Pydantic 2, Typer, pytest, GitHub Actions YAML

## Global Constraints

- Keep `chat_completions`, the configured model, and the protected `ard-llm` secret boundary unchanged.
- Never log or persist API keys, authorization headers, prompts, provider response bodies, or raw provider exception messages.
- Preserve exit codes 10 validation, 20 configuration, 30 transient, 40 conflict, 50 security, and 70 partial.
- Reconcile only `PROCESSING_POST_COMMIT_FAILED`; replay every other valid process failure with its original code and exit code.
- Do not refactor unrelated pipeline, workflow, release, or registry behavior.

---

### Task 1: Normalize provider and output failures

**Files:**
- Modify: `src/ard_ossie/llm.py`
- Modify: `src/ard_ossie/pipeline.py`
- Modify: `src/ard_ossie/application/processing.py`
- Test: `tests/unit/test_llm.py`
- Test: `tests/unit/test_processing_service.py`

**Interfaces:**
- Produces: `ProviderFailureKind` with `CONFIGURATION`, `TRANSIENT`, and `OUTPUT` values.
- Produces: `ProviderExecutionError(code: str, *, kind: ProviderFailureKind)` whose string representation contains only the safe code.
- Consumes: OpenAI SDK exception classes from version 2.46.0.

- [ ] **Step 1: Write provider exception mapping tests**

Add table-driven tests using real OpenAI SDK exception classes and synthetic
`httpx.Request`/`httpx.Response` objects. Hand-assert the expected code and kind
for authentication, permission, not-found, bad request, quota, rate limit,
timeout, connection, and server failures. Include sentinel strings in exception
messages/bodies and assert they are absent from `str(error)` and `repr(error)`.

- [ ] **Step 2: Run provider tests to verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen pytest tests/unit/test_llm.py -q
```

Expected: FAIL because `ProviderFailureKind` and normalized mappings do not exist.

- [ ] **Step 3: Implement minimal provider normalization**

Add the typed error and a private OpenAI exception mapper in `llm.py`. Wrap only
the remote SDK call, convert existing output parsing/schema errors to output-kind
errors, and never embed the caught exception text.

- [ ] **Step 4: Run provider tests to verify GREEN**

Run the command from Step 2. Expected: all `test_llm.py` tests pass.

- [ ] **Step 5: Write pipeline and workflow mapping tests**

Add cases proving that configuration, transient, and output provider kinds map
to exit codes 20, 30, and 10, respectively. Add a pipeline test proving a
semantic-output validation error preserves its safe `LLM_*` code.

- [ ] **Step 6: Run mapping tests to verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen pytest tests/unit/test_processing_service.py tests/integration/test_cli_process.py -q
```

Expected: FAIL because processing still maps every provider error to transient.

- [ ] **Step 7: Implement minimal pipeline and workflow mapping**

Preserve typed errors from `process_product()`, normalize suggestion-validation
errors as output failures, and switch `ProcessingService` on the failure kind to
raise the existing workflow configuration, transient, or validation error.

- [ ] **Step 8: Run mapping tests to verify GREEN**

Run the command from Step 6. Expected: all selected tests pass.

### Task 2: Replay non-partial process failures

**Files:**
- Modify: `src/ard_ossie/cli/workflow.py`
- Modify: `src/ard_ossie/application/processing.py`
- Test: `tests/integration/test_workflow_process_cli.py`
- Test: `tests/unit/test_processing_service.py`

**Interfaces:**
- Produces: trusted `failure_exit_code: int` and per-run `invocation_id` outputs.
- Consumes: a freshly initialized `.ard/run/workflow.process-result.json` with
  one safe finding code, a matching invocation, and a valid recorded exit code.

- [ ] **Step 1: Write failure-envelope and replay tests**

Test that a non-partial process failure writes `failure_exit_code`, that
`ProcessingReconcileService` re-raises the original code and exit code, and that
the existing partial envelope still reconciles statuses successfully. Test that
missing/invalid exit codes, invalid code syntax, wrong commands, and successful
results remain rejected. Also test stale-envelope invalidation, invocation
mismatch, partial exit 70 and exact partial finding shape.

- [ ] **Step 2: Run replay tests to verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen pytest tests/integration/test_workflow_process_cli.py tests/unit/test_processing_service.py -q
```

Expected: FAIL because failure exit metadata is absent and non-partial results
raise `PROCESSING_RECONCILE_RESULT_NOT_PARTIAL`.

- [ ] **Step 3: Implement trusted failure metadata and replay**

In `_publish`, invalidate the prior envelope, copy error outputs, overwrite
`failure_exit_code` with the trusted enum value, and bind process results to the
GitHub run/attempt invocation. Split result loading from partial detection. For
a valid non-partial process failure, validate its single safe code and recorded
exit, then raise `WorkflowError` with that code, exit, and retryability. Accept
partial reconciliation only for the exact partial shape and matching invocation.

- [ ] **Step 4: Run replay tests to verify GREEN**

Run the command from Step 2. Expected: all selected tests pass.

### Task 3: Verify and publish

**Files:**
- Verify all modified source, test, and documentation files.

**Interfaces:**
- Consumes: Tasks 1 and 2 completed.
- Produces: a reviewed PR whose exact head passes repository gates.

- [ ] **Step 1: Run focused and full verification**

```bash
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen pytest -q
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen ruff check .
UV_CACHE_DIR=/tmp/ard-uv-cache uv build --wheel
```

Also parse every `.github/workflows/*.yml` and `.yaml` with PyYAML and run
`actionlint` when installed. Expected: zero failures and zero lint errors.

- [ ] **Step 2: Review the diff against the design**

Check that raw caught exception strings never enter a provider error, result
output, finding, summary, or log; that only the approved files changed; and that
the original partial-publication behavior remains covered.

- [ ] **Step 3: Commit and open a PR**

Commit message:

```text
fix: preserve actionable LLM failures
```

Open a PR against `main` describing the observed run, redaction boundary,
failure mapping, reconcile replay, and verification commands.

- [ ] **Step 4: Verify remote gates and merge**

Require the repository's static, pytest, wheel, `ard/changeset`, and
`ard/quality-gate` conclusions plus resolved actionable review threads. Merge
only the exact reviewed head.

- [ ] **Step 5: Replay Issue #3**

Re-arm Issue #3 using the existing label workflow, approve `ard-llm` when the
job waits, and confirm either successful product publication or one specific
safe provider failure code with no reconcile masking.
