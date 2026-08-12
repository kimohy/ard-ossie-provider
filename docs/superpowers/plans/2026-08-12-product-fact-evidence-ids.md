# Product Fact Evidence IDs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace model-reconstructed product-fact evidence objects with request-local evidence IDs that trusted code resolves to the original parser evidence.

**Architecture:** Keep the existing single structured extraction request and all public output models. Add an ID-only product-fact response contract in `llm.py`; create one deterministic catalog in `pipeline.py` for prompt serialization and trusted resolution; then run the existing product-fact policy checks on resolved `Evidence` objects.

**Tech Stack:** Python 3.12, Pydantic v2, JSON Schema, pytest, Ruff, uv

## Global Constraints

- Preserve fail-closed exact product-HTML grounding.
- Use request-local IDs in the closed format `product-evidence-NNNNNN`, starting at one.
- Do not persist evidence IDs in generated artifacts, quality output, registry state, or IR.
- Do not change semantic suggestion or metric evidence contracts.
- Do not change product or table versioning.
- Follow strict RED-GREEN-REFACTOR cycles and keep each production change minimal.

---

### Task 1: Define the ID-only product-fact response contract

**Files:**
- Modify: `src/ard_ossie/llm.py:89-101,201-235`
- Test: `tests/unit/test_llm.py:92-171`

**Interfaces:**
- Produces: `ProductFactSuggestion.evidence_ids: list[str]`
- Produces: `semantic_extraction_schema()` product facts with required `evidence_ids`
- Preserves: `AISuggestion.evidence` and `MetricSuggestion.evidence`

- [x] **Step 1: Write failing schema and model tests**

Update `test_semantic_schema_requires_closed_product_facts` so the fact schema asserts:

```python
assert fact_schema["required"] == ["kind", "value", "confidence", "evidence_ids"]
assert fact_schema["properties"]["evidence_ids"] == {
    "type": "array",
    "minItems": 1,
    "items": {
        "type": "string",
        "pattern": r"^product-evidence-[0-9]{6}$",
    },
}
assert "evidence" not in fact_schema["properties"]
assert "evidence" in schema["properties"]["suggestions"]["items"]["properties"]
assert "evidence" in schema["properties"]["metrics"]["items"]["properties"]
```

Update the structured payload fixture and `test_product_fact_suggestion_normalizes_value_and_rejects_blank` to construct facts with:

```python
evidence_ids=["product-evidence-000001"]
```

Add a validation assertion that an empty `evidence_ids` list is rejected by Pydantic.

- [x] **Step 2: Run the focused tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen pytest tests/unit/test_llm.py -q
```

Expected: FAIL because the current schema and model still require compound `evidence`.

- [x] **Step 3: Implement the minimal response contract**

Change the model to:

```python
class ProductFactSuggestion(StrictModel):
    kind: ProductFactKind
    value: str
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1)
```

In `semantic_extraction_schema()`, replace the product-fact `evidence` property with:

```python
"evidence_ids": {
    "type": "array",
    "minItems": 1,
    "items": {
        "type": "string",
        "pattern": r"^product-evidence-[0-9]{6}$",
    },
},
```

Require `evidence_ids` and keep `additionalProperties: False`. Do not change the shared compound
evidence schema used by suggestions and metrics.

- [x] **Step 4: Run the focused tests and verify GREEN**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen pytest tests/unit/test_llm.py -q
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen ruff check src/ard_ossie/llm.py tests/unit/test_llm.py
```

Expected: both commands exit 0.

- [x] **Step 5: Commit Task 1**

```bash
git add src/ard_ossie/llm.py tests/unit/test_llm.py
git commit -m "refactor: cite product evidence by identifier"
```

### Task 2: Catalog and resolve trusted product evidence

**Files:**
- Modify: `src/ard_ossie/pipeline.py:963-1019,1068-1130`
- Modify: `tests/unit/test_pipeline.py:24-238`
- Modify: `tests/conftest.py:70-105`
- Modify: `tests/integration/test_cli_process.py:141-237`

**Interfaces:**
- Produces: `_product_evidence_catalog(document: ParsedDocument) -> dict[str, Evidence]`
- Produces: `_product_prompt_payload(document: ParsedDocument) -> dict[str, object]`
- Consumes: `ProductFactSuggestion.evidence_ids`
- Preserves: `_validate_product_facts(...) -> list[ProductFactIR]`

- [x] **Step 1: Update helpers and add failing catalog/resolution tests**

Change the unit-test `product_fact` helper to accept IDs:

```python
def product_fact(
    kind: str,
    value: str,
    *,
    confidence: float = 0.9,
    evidence_ids: list[str] | None = None,
) -> ProductFactSuggestion:
    return ProductFactSuggestion(
        kind=kind,
        value=value,
        confidence=confidence,
        evidence_ids=evidence_ids or ["product-evidence-000001"],
    )
```

Represent wrong-role, wrong-source, blank-excerpt, and excluded-evidence cases by placing those
objects in the `ParsedDocument.evidence` catalog instead of constructing them in the model output.

Add these tests:

```python
def test_product_evidence_catalog_assigns_stable_request_local_ids() -> None:
    document = product_document()
    second = document.evidence[0].model_copy(
        update={"locator": {"document": "product-info/product.html", "item_index": 2}}
    )
    document = document.model_copy(update={"evidence": [document.evidence[0], second]})

    assert pipeline._product_evidence_catalog(document) == {
        "product-evidence-000001": document.evidence[0],
        "product-evidence-000002": second,
    }


def test_product_facts_reject_unknown_evidence_id() -> None:
    with pytest.raises(ValueError, match="^LLM_PRODUCT_FACT_EVIDENCE_UNKNOWN$"):
        pipeline._validate_product_facts(
            [product_fact("purpose", "주문 분석", evidence_ids=["product-evidence-999999"])],
            product_document(),
            configured_description=None,
        )


def test_product_facts_reject_duplicate_evidence_id() -> None:
    with pytest.raises(
        ValueError,
        match="^LLM_PRODUCT_FACT_EVIDENCE_ID_DUPLICATE$",
    ):
        pipeline._validate_product_facts(
            [
                product_fact(
                    "purpose",
                    "주문 분석",
                    evidence_ids=[
                        "product-evidence-000001",
                        "product-evidence-000001",
                    ],
                )
            ],
            product_document(),
            configured_description=None,
        )
```

Add a capturing fake provider test for `_extract_suggestions` that parses the user message and
asserts its first product evidence entry contains `evidence_id == "product-evidence-000001"`,
while `excluded_product_fact_evidence` is not serialized.

Change `NoisyPortalProductProvider` to find the prompt evidence entry for each fact and return its
`evidence_id` in `evidence_ids`. Extend the quality-audit assertion to require a full resolved
`evidence` object and to prove `evidence_ids` does not enter the public audit.

- [x] **Step 2: Run the pipeline tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen pytest tests/unit/test_pipeline.py tests/integration/test_cli_process.py::test_pipeline_normalizes_user_facts_and_excludes_portal_boilerplate -q
```

Expected: FAIL because the catalog/prompt helpers do not exist, `_validate_product_facts` still
reads `fact.evidence`, and the integration provider now returns only IDs.

- [x] **Step 3: Implement the catalog and prompt payload**

Add:

```python
def _product_evidence_catalog(document: ParsedDocument) -> dict[str, Evidence]:
    return {
        f"product-evidence-{position:06d}": evidence
        for position, evidence in enumerate(document.evidence, start=1)
    }


def _product_prompt_payload(document: ParsedDocument) -> dict[str, object]:
    payload = document.model_dump(mode="json")
    catalog = _product_evidence_catalog(document)
    payload["evidence"] = [
        {"evidence_id": evidence_id, **evidence.model_dump(mode="json")}
        for evidence_id, evidence in catalog.items()
    ]
    return payload
```

Use `_product_prompt_payload(product_document)` in the extraction user message. Update the system
instruction so product facts must return supplied `evidence_id` values and must not reproduce
evidence objects.

- [x] **Step 4: Resolve IDs before applying existing policies**

At the beginning of `_validate_product_facts`, create the catalog. For every retained fact:

```python
if len(fact.evidence_ids) != len(set(fact.evidence_ids)):
    raise ValueError("LLM_PRODUCT_FACT_EVIDENCE_ID_DUPLICATE")
resolved_evidence: list[Evidence] = []
for evidence_id in fact.evidence_ids:
    evidence = evidence_catalog.get(evidence_id)
    if evidence is None:
        raise ValueError("LLM_PRODUCT_FACT_EVIDENCE_UNKNOWN")
    resolved_evidence.append(evidence)
```

Run all existing role, source, excerpt, excluded, exact-known, and AI-generated checks on
`resolved_evidence`. Construct `ProductFactIR(..., evidence=resolved_evidence)`. Keep confidence,
ordering, fact deduplication, singleton conflict, and configured-description behavior unchanged.

- [x] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen pytest tests/unit/test_pipeline.py tests/unit/test_llm.py tests/integration/test_cli_process.py::test_pipeline_normalizes_user_facts_and_excludes_portal_boilerplate -q
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen ruff check src/ard_ossie/llm.py src/ard_ossie/pipeline.py tests/unit/test_llm.py tests/unit/test_pipeline.py tests/integration/test_cli_process.py tests/conftest.py
```

Expected: both commands exit 0.

- [x] **Step 6: Commit Task 2**

```bash
git add src/ard_ossie/pipeline.py tests/unit/test_pipeline.py tests/integration/test_cli_process.py tests/conftest.py
git commit -m "fix: resolve product fact evidence identifiers"
```

### Task 3: Synchronize documentation and confirm end-to-end compatibility

**Files:**
- Modify: `docs/superpowers/specs/2026-08-12-data-product-document-normalization-design.md:82-115`
- Modify: `docs/superpowers/plans/2026-08-12-product-fact-evidence-ids.md`

**Interfaces:**
- Consumes: prompt product evidence entries with `evidence_id`
- Verifies: public `product_facts[*].evidence` still contains full resolved objects
- Verifies: public output contains no `evidence_ids`

- [x] **Step 1: Confirm the integration provider and public-output assertions**

Change `NoisyPortalProductProvider` to return:

```python
def evidence_id_for(text: str) -> str:
    item = next(
        item
        for item in product["evidence"]
        if text in str(item.get("excerpt") or "")
    )
    return str(item["evidence_id"])
```

Each product fact uses, for example:

```python
"evidence_ids": [evidence_id_for("주문 분석 지원")]
```

The Task 2 audit assertions must contain:

```python
assert audit["product_facts"][0]["evidence"][0]["excerpt"]
assert all("evidence_ids" not in fact for fact in audit["product_facts"])
```

- [x] **Step 2: Run the focused integration test**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen pytest tests/integration/test_cli_process.py::test_pipeline_normalizes_user_facts_and_excludes_portal_boilerplate -q
```

Expected: PASS with Task 2 active, proving full resolved evidence is public and request-local IDs
are not.

- [x] **Step 3: Synchronize the canonical normalization design**

Amend the extraction and evidence-contract section to state that product facts return request-local
`evidence_ids`, trusted code resolves them to original parser evidence, and suggestions/metrics
retain compound citations. Link to
`docs/superpowers/specs/2026-08-12-product-fact-evidence-ids-design.md` for the complete amendment.

- [x] **Step 4: Run focused integration and documentation checks**

Run:

```bash
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen pytest tests/integration/test_cli_process.py -q
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen ruff check tests/integration/test_cli_process.py
git diff --check
```

Expected: all commands exit 0 and the audit includes full original evidence with no IDs.

- [x] **Step 5: Record execution evidence and commit Task 3**

Check off completed plan steps and append exact focused/full gate results under an `Execution
record` section. Then run:

```bash
git add docs/superpowers/specs/2026-08-12-data-product-document-normalization-design.md docs/superpowers/plans/2026-08-12-product-fact-evidence-ids.md
git commit -m "test: verify resolved product fact evidence"
```

## Execution record

- Clean baseline at `239099d`: `443 passed`.
- Task 1 RED: two expected failures proved the schema and Pydantic model still required compound
  product-fact evidence.
- Task 1 GREEN: `25 passed`; focused Ruff clean; commit `ff90128`.
- Task 2 RED: catalog, prompt serialization, trusted resolution, duplicate/unknown ID, and CLI
  integration cases failed at their missing production boundaries.
- Task 2 GREEN: `42 passed`; focused Ruff clean; commit `b9a5f58`.
- The first integration GREEN attempt exposed that the public LLM audit would persist request-local
  IDs. The audit writer was covered by the failing assertion, then changed to publish only resolved
  evidence-bearing LLM facts; configured authoritative facts remain outside the suggestion audit.
- Task 3 focused integration: `5 passed`; focused Ruff and `git diff --check` clean.
- Audit compatibility hardening RED: low-confidence unknown IDs were not rejected and resolved
  audit facts lost `confidence`; both regressions failed for the intended reasons.
- Audit compatibility hardening GREEN: `47 passed`, focused Ruff clean; commit `a2f0156`.
- Exact head `a2f0156`: `446 passed`, Ruff clean, seven workflow files parsed, sdist and wheel
  built, isolated `model-schemas` lifecycle successful, and network-independent schema catalog,
  Ossie checksum, and secret scan successful.
- Independent final production review: no Critical or Important findings; verdict `Ready`.

### Task 4: Full verification, publication, and Issue #3 replay

**Files:**
- Verify all changed files
- No production file is added in this task

**Interfaces:**
- Consumes: exact clean feature HEAD
- Produces: reviewed PR and successful Issue #3 processing evidence

- [x] **Step 1: Run the complete local gate**

Run the repository's trusted gate equivalents:

```bash
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen pytest -q
UV_CACHE_DIR=/tmp/ard-uv-cache uv run --frozen ruff check .
UV_CACHE_DIR=/tmp/ard-uv-cache uv build
git diff --check origin/main...HEAD
git status --short
```

Also parse all `.github/workflows/*.yml`, run the isolated `model-schemas` verifier group, and run
the network-independent schema-catalog, Ossie-checksum, and secret-scan verifiers using the same
CLI contracts already exercised by CI.

- [x] **Step 2: Review the exact diff**

Review `origin/main...HEAD` for Critical, Important, and Minor findings. Confirm product-fact IDs
cannot reach public outputs, unknown IDs fail closed, and suggestion/metric schemas are unchanged.
Apply any valid finding through a fresh RED-GREEN cycle and rerun the complete local gate.

- [ ] **Step 3: Publish a draft PR without rewriting remote history**

Reconfirm current `main`, rebase or merge only if it moved, rerun verification for any rewritten
commit, publish `fix/product-fact-evidence-ids`, and open a draft PR that references Actions run
`31562755748` and the stable failure code. Do not force-push.

- [ ] **Step 4: Require exact-head CI success and merge**

Confirm the PR head SHA, `ard/changeset`, `ard/quality-gate`, and all Actions jobs are successful.
Mark ready and squash-merge only with expected-head protection and an unchanged `main` base.

- [ ] **Step 5: Replay Issue #3 through the official label path**

Remove stale `ard:failed` and toggle only `ard:approved` to create a new `issues:labeled` event.
Monitor authorize, intake, validate, protected process, writeback, and finalize. If the protected
environment requests approval, stop and provide the exact run link for user approval.

- [ ] **Step 6: Validate PR #5 outputs**

Confirm PR #5 moves to a new exact head and contains generated product, semantic, dictionary,
Ossie, registry, and quality artifacts. Confirm both required commit statuses succeed, generated
product facts contain full original evidence, and the issue ends without `ard:failed`.
