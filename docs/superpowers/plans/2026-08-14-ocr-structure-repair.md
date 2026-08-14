# OCR Semantic Structure Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair unresolved OCR document structure through the existing constrained LLM planner, retry one invalid semantic plan once, and expose safe actionable failure details without weakening fidelity gates.

**Architecture:** Keep OCR text and provenance in the immutable native span catalog, then let the LLM assign only supplied span IDs to validated semantic blocks after deterministic reconciliation. Remove the parser's OCR-only planner exclusion, run both semantic-plan attempts through the same validator, persist ordered attempt codes in the existing repair audit, and carry the resulting safe quality finding message through source-check and the CLI.

**Tech Stack:** Python 3.12, Pydantic v2, jsonschema Draft 2020-12, Typer, pytest, Ruff, uv, GitHub Actions

## Global Constraints

- Preserve every source span exactly once unless an exclusion has explicit validated evidence.
- The LLM may assign structure only; it must not add, delete, paraphrase, translate, merge, or split source text.
- Run semantic structure repair after deterministic reconciliation for every extraction mode, including ExtractionMode.OCR.
- Retry the first semantically invalid plan exactly once; never make a third semantic-plan attempt.
- Keep provider transient retries solely in LLMService; do not duplicate network retry policy in the semantic planner.
- Keep SEMANTIC_STRUCTURE_DEGRADED, coverage, visual-correction, and publication gates fail-closed.
- Never emit credentials, full prompts, source text, or provider response bodies in logs or findings.
- Render Markdown without raw HTML tags, `&#32;`, or `&#9;`.
- Do not add a repair-audit schema field: ordered validation_codes records failed attempts, and attempt count is derived from outcome plus those codes.
- Do not run the broad local test suite. Run only the named focused tests, Ruff on changed Python files, and git diff --check; required PR checks are the integrated gate.
- Keep the managed product PR draft until every Issue 3 acceptance criterion passes.

---

## File Map

- Modify src/ard_ossie/semantic/parser.py: invoke the existing repair planner for unresolved OCR spans.
- Modify src/ard_ossie/semantic/repair.py: perform one bounded semantic-plan retry, create compact feedback, and preserve per-attempt validation codes.
- Modify src/ard_ossie/pipeline.py: add safe structure-repair context to the hard quality finding while retaining its existing category.
- Modify src/ard_ossie/application/contracts.py: retain the safe workflow error message as a public attribute.
- Modify src/ard_ossie/application/source_check.py: propagate the selected quality finding's message and path into the workflow error.
- Modify src/ard_ossie/cli/workflow.py: write and print the detailed safe workflow error message instead of repeating only the code.
- Modify tests/integration/test_docling_pipeline.py: prove OCR enters structure repair and safe degraded diagnostics are specific.
- Modify tests/unit/semantic/test_repair.py: prove invalid-then-valid recovery, two-invalid bounded rejection, provider behavior, and retry payload limits.
- Modify tests/unit/test_source_check_service.py: prove a validation finding's safe details survive source-check.
- Modify tests/integration/test_workflow_direct_cli.py: prove CLI stderr and the result envelope expose safe details without secrets.
- No schema or generated catalog files change because the existing validation_codes field carries ordered attempt results.

### Task 1: Invoke structure repair for unresolved OCR spans

**Files:**
- Modify: src/ard_ossie/semantic/parser.py:226-273
- Test: tests/integration/test_docling_pipeline.py:503-535
- Test: tests/integration/test_docling_pipeline.py near test_full_page_ocr_preserves_paragraph_before_later_heading

**Interfaces:**
- Consumes: SemanticStructureRepairPlanner.repair(native, skeleton, unresolved_span_ids, *, trusted_record) -> RepairApplication
- Produces: _repair_and_degrade(...) -> tuple[ReconciledDocument, SemanticStructureRepairRecord | None] that calls the planner whenever unresolved_span_ids is non-empty and a planner exists, including for ExtractionMode.OCR.

- [ ] **Step 1: Add an OCR-native fixture with immutable page evidence**

Extend the existing controlled fixture helpers rather than loading a real OCR model:

~~~python
def controlled_ocr_native(texts: tuple[str, ...]) -> NativeDocument:
    spans = tuple(
        controlled_span(index, text).model_copy(
            update={
                "page": 1,
                "bbox": SourceBox(
                    left=0.05,
                    bottom=0.80 - index * 0.10,
                    right=0.95,
                    top=0.88 - index * 0.10,
                ),
            }
        )
        for index, text in enumerate(texts)
    )
    return NativeDocument(
        source_hash=CONTROLLED_HASH,
        extraction_mode=ExtractionMode.OCR,
        page_count=1,
        parser_versions={"ocr": "fixture-v1"},
        spans=spans,
        groups=(),
        tables=(),
    )
~~~

- [ ] **Step 2: Write the failing OCR repair regression**

Add a controlled public-parser test. Its empty skeleton leaves both spans unresolved; the fixed planner allocates them as a paragraph without changing their text:

~~~python
def test_unresolved_full_page_ocr_invokes_structure_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = controlled_ocr_native(("Semantics 문서", "개인정보"))
    span_ids = tuple(span.span_id for span in native.spans)
    plan = RepairPlan(
        blocks=[
            RepairBlock(
                kind="paragraph",
                order=0,
                span_ids=list(span_ids),
                heading_level=None,
                list_kind=None,
                list_depth=None,
                row_count=None,
                column_count=None,
                cells=[],
                exclusion_kind=None,
                confidence=1.0,
            )
        ]
    )
    planner = FixedRepairPlanner(
        plan,
        (ParagraphBlock(order=0, span_ids=span_ids),),
    )

    parsed = parse_controlled(tmp_path, monkeypatch, native, planner=planner)

    assert parsed.markdown == "Semantics 문서 개인정보\n"
    assert [item.excerpt for item in parsed.evidence] == ["Semantics 문서", "개인정보"]
    assert parsed.semantic_repair.outcome == "applied"
    assert parsed.semantic_repair.applied_orders == [0]
    assert parsed.semantic_fidelity.degraded_block_count == 0
    assert parsed.semantic_fidelity.source_text_coverage == 1.0
~~~

- [ ] **Step 3: Run the new test and verify the root cause**

Run:

~~~bash
uv run --frozen pytest -q tests/integration/test_docling_pipeline.py::test_unresolved_full_page_ocr_invokes_structure_repair
~~~

Expected: FAIL because parsed.semantic_repair is None and the OCR spans are emitted as degraded lossless blocks; this proves the planner was skipped before any retry work.

- [ ] **Step 4: Remove only the OCR planner exclusion**

Change the planner condition in _repair_and_degrade() to:

~~~python
    if unresolved and repair_planner is not None:
        try:
            application = repair_planner.repair(
                native,
                skeleton,
                unresolved,
                trusted_record=trusted_record,
            )
~~~

Leave _validate_native_table_repairs(), _applied_exclusions(), _enforce_native_tables(), coverage validation, and the lossless fallback unchanged. Keep the initial fallback reason as structure_unresolved for OCR and provider_unavailable for other modes.

- [ ] **Step 5: Run the OCR regression and directly adjacent parser cases**

Run:

~~~bash
uv run --frozen pytest -q \
  tests/integration/test_docling_pipeline.py::test_unresolved_full_page_ocr_invokes_structure_repair \
  tests/integration/test_docling_pipeline.py::test_full_page_ocr_preserves_paragraph_before_later_heading \
  tests/integration/test_docling_pipeline.py::test_unresolved_ordinary_span_after_failed_repair_is_audited_as_degraded \
  tests/integration/test_docling_pipeline.py::test_residual_ordinary_span_after_accepted_repair_is_structure_unresolved
~~~

Expected: 5 tests PASS, including the two parameterized failed-repair cases. No real OCR model is loaded.

- [ ] **Step 6: Commit the parser boundary fix**

~~~bash
git add src/ard_ossie/semantic/parser.py tests/integration/test_docling_pipeline.py
git commit -m "fix: repair unresolved OCR structure"
~~~

### Task 2: Retry one invalid semantic plan with bounded feedback

**Files:**
- Modify: src/ard_ossie/semantic/repair.py:40-45,190-274,390-428,603-660
- Test: tests/unit/semantic/test_repair.py:40-70,375-540,632-676

**Interfaces:**
- Consumes: _validate_plan(context, response) -> tuple[_ValidatedPlan | None, str | None, RepairPlan | None] and _repair_plan_payload(response) -> dict[str, object] | None.
- Produces: _retry_feedback(context, *, code, response, parsed_plan) -> dict[str, object] containing only validation_code, compact affected_span_ids, compact affected_orders, and a fixed instruction.
- Produces: _application(..., validation_codes: list[str] | None = None) -> RepairApplication, preserving the first failed-attempt code when retry succeeds.
- Produces: SemanticStructureRepairPlanner.repair(...) with at most two generate_structured() calls and ordered per-attempt validation codes.

- [ ] **Step 1: Make the recording provider return a response sequence**

Keep single-response tests source-compatible while supporting retry cases:

~~~python
class RecordingProvider:
    def __init__(
        self,
        response: object | None = None,
        *,
        responses: list[object] | None = None,
        error: Exception | None = None,
        capabilities_error: Exception | None = None,
    ) -> None:
        self.response = response
        self.responses = [deepcopy(item) for item in responses] if responses else None
        self.error = error
        self.capabilities_error = capabilities_error
        self.calls: list[dict[str, object]] = []
        self.capability_calls = 0

    def generate_structured(
        self,
        *,
        schema: dict[str, object],
        messages: list[dict[str, str]],
    ) -> object:
        self.calls.append({"schema": schema, "messages": deepcopy(messages)})
        if self.error is not None:
            raise self.error
        if self.responses is not None:
            assert self.responses
            return deepcopy(self.responses.pop(0))
        assert self.response is not None
        return deepcopy(self.response)
~~~

- [ ] **Step 2: Write the invalid-then-valid retry regression**

~~~python
def test_invalid_plan_retries_once_and_applies_valid_replacement() -> None:
    native, skeleton, span_ids = unresolved_fixture()
    missing = {
        "blocks": [
            repair_block(kind="paragraph", order=0, span_ids=list(span_ids[:-1]))
        ]
    }
    provider = RecordingProvider(
        responses=[repair_result(missing), repair_result(valid_table_plan(span_ids))]
    )

    application = SemanticStructureRepairPlanner(provider).repair(
        native, skeleton, span_ids, trusted_record=None
    )

    assert len(provider.calls) == 2
    assert application.record.outcome == "applied"
    assert application.record.validation_codes == ["SEMANTIC_REPAIR_MISSING_SPAN"]
    retry = json.loads(provider.calls[1]["messages"][-1]["content"])
    assert retry == {
        "repair_retry": {
            "affected_orders": [0],
            "affected_span_ids": [span_ids[-1]],
            "instruction": "Return one complete replacement plan using the original immutable spans.",
            "validation_code": "SEMANTIC_REPAIR_MISSING_SPAN",
        }
    }
~~~

- [ ] **Step 3: Write the two-invalid bounded rejection regression**

~~~python
def test_two_invalid_plans_stop_after_one_retry_and_preserve_attempt_codes() -> None:
    native, skeleton, span_ids = unresolved_fixture()
    first = {
        "blocks": [
            repair_block(kind="paragraph", order=0, span_ids=list(span_ids[:-1]))
        ]
    }
    second = {
        "blocks": [
            repair_block(
                kind="paragraph",
                order=0,
                span_ids=[*span_ids, make_span_id("b" * 64, 0)],
            )
        ]
    }
    provider = RecordingProvider(responses=[repair_result(first), repair_result(second)])

    application = SemanticStructureRepairPlanner(provider).repair(
        native, skeleton, span_ids, trusted_record=None
    )

    assert len(provider.calls) == 2
    assert application.blocks == ()
    assert application.record.outcome == "rejected"
    assert application.record.validation_codes == [
        "SEMANTIC_REPAIR_MISSING_SPAN",
        "SEMANTIC_REPAIR_UNKNOWN_SPAN",
    ]
    assert application.record.plan is not None
~~~

- [ ] **Step 4: Run both regressions to establish RED**

Run:

~~~bash
uv run --frozen pytest -q \
  tests/unit/semantic/test_repair.py::test_invalid_plan_retries_once_and_applies_valid_replacement \
  tests/unit/semantic/test_repair.py::test_two_invalid_plans_stop_after_one_retry_and_preserve_attempt_codes
~~~

Expected: both FAIL because the current planner returns after its first invalid response.

- [ ] **Step 5: Add compact deterministic retry feedback**

Add helpers beside _request_payload(); never echo source text or the invalid response:

~~~python
def _plan_span_ids(plan: RepairPlan | None) -> list[str]:
    if plan is None:
        return []
    return [
        span_id
        for block in plan.blocks
        for span_id in (
            [item for cell in block.cells for item in cell.span_ids]
            if block.kind == "table"
            else block.span_ids
        )
    ]


def _retry_feedback(
    context: _RepairContext,
    *,
    code: str,
    response: object,
    parsed_plan: RepairPlan | None,
) -> dict[str, object]:
    allocations = _plan_span_ids(parsed_plan)
    allowed = set(context.ordered_span_ids)
    if code == _MISSING_SPAN:
        affected_ids = [item for item in context.ordered_span_ids if item not in allocations]
    elif code == _UNKNOWN_SPAN:
        affected_ids = list(dict.fromkeys(item for item in allocations if item not in allowed))
    elif code == _DUPLICATE_SPAN:
        payload = _repair_plan_payload(response)
        raw = _raw_allocations(payload) if _safe_allocation_payload(payload) else []
        affected_ids = list(dict.fromkeys(item for item in raw if raw.count(item) > 1))
    else:
        affected_ids = []
    affected_orders = (
        []
        if parsed_plan is None
        else list(dict.fromkeys(block.order for block in parsed_plan.blocks))
    )
    return {
        "repair_retry": {
            "validation_code": code,
            "affected_span_ids": affected_ids,
            "affected_orders": affected_orders,
            "instruction": (
                "Return one complete replacement plan using the original immutable spans."
            ),
        }
    }
~~~

Define the shape guard beside the helper so arbitrary provider output cannot make
retry construction raise:

~~~python
def _safe_allocation_payload(payload: object) -> bool:
    if not isinstance(payload, dict) or not isinstance(payload.get("blocks"), list):
        return False
    for block in payload["blocks"]:
        if not isinstance(block, dict) or not isinstance(block.get("kind"), str):
            return False
        if block["kind"] == "table":
            cells = block.get("cells")
            if not isinstance(cells, list):
                return False
            if any(
                not isinstance(cell, dict)
                or not isinstance(cell.get("span_ids"), list)
                or not all(isinstance(item, str) for item in cell["span_ids"])
                for cell in cells
            ):
                return False
        else:
            span_ids = block.get("span_ids")
            if not isinstance(span_ids, list) or not all(
                isinstance(item, str) for item in span_ids
            ):
                return False
    return True
~~~

If the shape is not safe, feedback uses an empty affected_span_ids list instead
of raising. The feedback helper must be total for arbitrary provider output.

- [ ] **Step 6: Implement exactly one semantic retry**

Bump REPAIR_PROMPT_VERSION from semantic-structure-repair-v1 to semantic-structure-repair-v2 so trusted v1 plans cannot bypass the new attempt semantics. After the first _validate_plan() call, append one compact user message and call the provider once more:

~~~python
        validated, first_code, first_plan = self._validate_plan(context, response)
        if validated is not None:
            return _application(
                context,
                validated,
                provider=provider,
                model=model,
                outcome="applied",
            )

        first_code = first_code or _SCHEMA_INVALID
        retry_messages = [
            *messages,
            {
                "role": "user",
                "content": json.dumps(
                    _retry_feedback(
                        context,
                        code=first_code,
                        response=response,
                        parsed_plan=first_plan,
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
        ]
        try:
            retry_response = self._provider.generate_structured(
                schema=context.schema,
                messages=retry_messages,
            )
        except ProviderExecutionError as error:
            if self.propagate_provider_errors:
                raise
            return _empty_application(
                context,
                provider=provider,
                model=model,
                outcome="degraded",
                validation_codes=[first_code],
                provider_error_code=error.code,
                plan=first_plan,
            )

        validated, second_code, second_plan = self._validate_plan(
            context, retry_response
        )
        if validated is None:
            return _empty_application(
                context,
                provider=provider,
                model=model,
                outcome="rejected",
                validation_codes=[first_code, second_code or _SCHEMA_INVALID],
                plan=second_plan,
            )
        return _application(
            context,
            validated,
            provider=provider,
            model=model,
            outcome="applied",
            validation_codes=[first_code],
        )
~~~

Extend _application() with validation_codes: list[str] | None = None and store validation_codes or []. Do not add a loop, sleep, backoff, or third call.

- [ ] **Step 7: Update affected fail-closed expectations**

The existing tests that supply one permanently invalid RecordingProvider.response now receive the same invalid response twice. Update only those assertions to require:

~~~python
assert len(provider.calls) == 2
assert application.record.validation_codes == [expected_code, expected_code]
~~~

For fixed-code tests such as schema-invalid and exclusion-invalid, expect the same code twice. Keep trusted-plan reuse at zero provider calls, valid first-attempt plans at one call, and provider execution errors at one call. In test_trusted_record_cannot_reuse_empty_non_table_span_allocation, expect two fresh provider calls after trusted reuse is rejected.

- [ ] **Step 8: Run the focused planner matrix**

Run:

~~~bash
uv run --frozen pytest -q \
  tests/unit/semantic/test_repair.py::test_valid_repair_uses_only_allowlisted_spans \
  tests/unit/semantic/test_repair.py::test_llm_service_llm_result_repair_plan_applies_blocks_and_audit_identity \
  tests/unit/semantic/test_repair.py::test_configured_profile_llm_service_applies_unresolved_semantic_repair \
  tests/unit/semantic/test_repair.py::test_invalid_plan_retries_once_and_applies_valid_replacement \
  tests/unit/semantic/test_repair.py::test_two_invalid_plans_stop_after_one_retry_and_preserve_attempt_codes \
  tests/unit/semantic/test_repair.py::test_invalid_repair_is_rejected_without_applied_blocks \
  tests/unit/semantic/test_repair.py::test_provider_error_propagates_when_fail_fast_requested \
  tests/unit/semantic/test_repair.py::test_matching_trusted_record_is_revalidated_and_reused_without_provider_call
~~~

Expected: focused cases PASS; the parameterized invalid matrix proves every validation class remains fail-closed with exactly two attempts.

- [ ] **Step 9: Add a strict source-check regression through `LLMResult.structured`**

In tests/unit/test_source_check_service.py, add a provider that returns a real
LLMResult for the repair schema and empty valid suggestions for the other
pipeline schema:

~~~python
class OcrRepairResultProvider:
    def health_check(self) -> bool:
        return True

    def capabilities(self) -> dict[str, str | bool]:
        return {
            "structured_output": "json_schema",
            "provider": "ocr-repair-test",
            "model": "ocr-repair-v1",
            "vision": True,
        }

    def generate_structured(self, *, schema, messages):
        if "blocks" not in schema.get("properties", {}):
            return {"suggestions": [], "metrics": [], "product_facts": []}
        request = json.loads(messages[1]["content"])
        span_ids = [item["span_id"] for item in request["unresolved_spans"]]
        return LLMResult(
            text="",
            structured={
                "blocks": [
                    {
                        "kind": "paragraph",
                        "order": 0,
                        "span_ids": span_ids,
                        "heading_level": None,
                        "list_kind": None,
                        "list_depth": None,
                        "row_count": None,
                        "column_count": None,
                        "cells": [],
                        "exclusion_kind": None,
                        "confidence": 0.99,
                    }
                ]
            },
            metadata=LLMMetadata(
                profile="ocr-source-check",
                provider="ocr-repair-test",
                model="ocr-repair-v1",
                elapsed_ms=1,
            ),
        )
~~~

Add an accepted correction stub with exactly one audited OCR page:

~~~python
class AcceptedOcrCorrectionPlanner:
    def correct(self, _source, native, **_kwargs):
        return OcrCorrectionApplication(
            document=native,
            audits=(
                OcrCorrectionPageAudit(
                    source_hash=native.source_hash,
                    page=1,
                    page_image_hash="1" * 64,
                    ocr_catalog_hash="2" * 64,
                    request_hash="3" * 64,
                    prompt_version="ocr-correction-test-v1",
                    prompt_hash="4" * 64,
                    schema_hash="5" * 64,
                    provider="ocr-repair-test",
                    model="ocr-repair-v1",
                    outcome="applied",
                    patches=[],
                ),
            ),
            warning_codes=(),
        )
~~~

The test dynamically binds native span IDs to the staged semantic source hash,
returns a static product HTML document, and otherwise uses the real Docling
parser, repair planner, fidelity builder, pipeline gates, and SourceCheckService:

~~~python
def test_source_check_accepts_repaired_ocr_from_llm_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_product_fixture(tmp_path)
    provider = OcrRepairResultProvider()

    def native_and_structure(source, **_kwargs):
        texts = ("Semantics 문서", "개인정보")
        spans = tuple(
            SourceSpan(
                span_id=make_span_id(source.sha256, index),
                ordinal=index,
                page=1,
                bbox=SourceBox(
                    left=0.05,
                    bottom=0.80 - index * 0.10,
                    right=0.95,
                    top=0.88 - index * 0.10,
                ),
                text=text,
                text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
            for index, text in enumerate(texts)
        )
        return (
            NativeDocument(
                source_hash=source.sha256,
                extraction_mode=ExtractionMode.OCR,
                page_count=1,
                parser_versions={"ocr": "fixture-v1"},
                spans=spans,
                groups=(),
                tables=(),
            ),
            StructureDocument(blocks=()),
        )

    class SourceCheckParser:
        def __init__(self) -> None:
            self.semantic = DoclingParser(
                structure_repair_planner=SemanticStructureRepairPlanner(
                    provider,
                    propagate_provider_errors=True,
                ),
                ocr_correction_planner=AcceptedOcrCorrectionPlanner(),
            )

        def parse(self, source):
            if source.role is SourceRole.PRODUCT_HTML:
                return ParsedDocument(
                    role=source.role,
                    source_hash=source.sha256,
                    markdown="# Sales Order\n\nOrder analytics.",
                )
            return self.semantic.parse(source)

    monkeypatch.setattr(semantic_parser, "_native_and_structure", native_and_structure)
    monkeypatch.setattr(
        pipeline_module,
        "_processing_parser",
        lambda **_kwargs: SourceCheckParser(),
    )

    result = SourceCheckService(
        RepositoryPaths(tmp_path),
        provider=provider,
    ).run("sales-order", SHA)

    assert result.status is WorkflowStatus.SUCCESS
~~~

Import hashlib, DoclingParser, ParsedDocument, SourceRole, LLMMetadata,
LLMResult, OcrCorrectionApplication, SemanticStructureRepairPlanner,
NativeDocument, OcrCorrectionPageAudit, SourceBox, SourceSpan, make_span_id,
StructureDocument, and ard_ossie.semantic.parser as semantic_parser.

- [ ] **Step 10: Run the strict source-check regression**

~~~bash
uv run --frozen pytest -q tests/unit/test_source_check_service.py::test_source_check_accepts_repaired_ocr_from_llm_result
~~~

Expected: PASS with WorkflowStatus.SUCCESS, a real LLMResult plan, complete OCR
page audit, coverage 1.0, and zero degraded blocks. No OCR engine or network is
used.

- [ ] **Step 11: Commit bounded semantic retry**

~~~bash
git add \
  src/ard_ossie/semantic/repair.py \
  tests/unit/semantic/test_repair.py \
  tests/unit/test_source_check_service.py
git commit -m "fix: retry invalid semantic structure plan once"
~~~

### Task 3: Preserve detailed safe structure diagnostics

**Files:**
- Modify: src/ard_ossie/pipeline.py:1087-1105
- Modify: src/ard_ossie/application/contracts.py:35-48
- Modify: src/ard_ossie/application/source_check.py:165-168
- Modify: src/ard_ossie/cli/workflow.py:454-490
- Test: tests/integration/test_docling_pipeline.py near semantic hard-finding tests
- Test: tests/unit/test_source_check_service.py:168-248
- Test: tests/integration/test_workflow_direct_cli.py:113-180

**Interfaces:**
- Consumes: ParsedDocument.semantic_fidelity, ParsedDocument.semantic_repair, and ValidationResult.findings.
- Produces: _semantic_repair_diagnostic(document: ParsedDocument) -> str with bounded metadata and no source text.
- Produces: WorkflowError.message: str and CLI result envelopes whose finding message matches the safe application error message.

- [ ] **Step 1: Write a hard-finding diagnostic regression**

Extend a controlled degraded OCR case with a rejected
SemanticStructureRepairRecord containing two attempt codes. Assert the most
specific last-attempt code is emitted first for source-check while the generic
publication category remains present:

~~~python
findings = _semantic_hard_findings(parsed, require_visual_correction=False)
specific = findings[0]
assert specific.code == "SEMANTIC_REPAIR_ORDER_INVALID"
assert "category=SEMANTIC_STRUCTURE_DEGRADED" in specific.message
assert "extraction_mode=ocr" in specific.message
assert "unresolved_spans=2" in specific.message
assert "pages=1" in specific.message
assert (
    "validation_codes=SEMANTIC_REPAIR_MISSING_SPAN,"
    "SEMANTIC_REPAIR_ORDER_INVALID"
) in specific.message
assert "provider=fake" in specific.message
assert "model=fake" in specific.message
assert "attempts=2" in specific.message
assert "Semantics 문서" not in specific.message
assert any(item.code == "SEMANTIC_STRUCTURE_DEGRADED" for item in findings)
~~~

Import _semantic_hard_findings inside the integration test to keep the public module namespace unchanged.

- [ ] **Step 2: Run the diagnostic regression to establish RED**

Run:

~~~bash
uv run --frozen pytest -q tests/integration/test_docling_pipeline.py::test_semantic_structure_degraded_finding_includes_safe_repair_diagnostics
~~~

Expected: FAIL because the current message contains only degradation reasons and block count.

- [ ] **Step 3: Build a safe aggregate diagnostic from audit metadata**

Add this private helper in pipeline.py and append it only to SEMANTIC_STRUCTURE_DEGRADED hard findings:

~~~python
def _semantic_repair_diagnostic(document: ParsedDocument) -> str:
    fidelity = document.semantic_fidelity
    repair = document.semantic_repair
    if fidelity is None:
        return ""
    unresolved_spans = sum(len(block.spans) for block in fidelity.degraded_blocks)
    pages = sorted(
        {
            span.page
            for block in fidelity.degraded_blocks
            for span in block.spans
            if span.page is not None
        }
    )
    codes = [] if repair is None else repair.validation_codes
    if repair is None:
        attempts = 0
        provider = "none"
        model = "none"
        applied = 0
        rejected = 0
    else:
        if repair.outcome == "reused":
            attempts = 0
        elif repair.outcome == "applied":
            attempts = len(codes) + 1
        elif repair.outcome == "degraded" and repair.provider_error_code and codes:
            attempts = len(codes) + 1
        else:
            attempts = len(codes) or 1
        provider = _safe_diagnostic_token(repair.provider)
        model = _safe_diagnostic_token(repair.model)
        applied = len(repair.applied_orders)
        rejected = len(repair.rejected_orders)
    return (
        f"; extraction_mode={fidelity.extraction_mode.value}"
        f"; unresolved_spans={unresolved_spans}"
        f"; pages={','.join(str(page) for page in pages) or 'unknown'}"
        f"; validation_codes={','.join(codes) or 'none'}"
        f"; provider={provider}; model={model}"
        f"; applied_blocks={applied}; rejected_blocks={rejected}"
        f"; attempts={attempts}"
    )
~~~

Add this bounded token helper beside `_semantic_repair_diagnostic()`; pipeline.py
already imports `re`:

~~~python
def _safe_diagnostic_token(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9._-]{1,80}", value) else "unknown"
~~~

The helper reads counts, enums, bounded provider/model names, pages, and codes
only. It must not read span text, prompts, credentials, or raw responses. When
repair.validation_codes is non-empty, prepend this specific hard finding:

~~~python
    repair = document.semantic_repair
    diagnostic = _semantic_repair_diagnostic(document)
    if fidelity.degraded_block_count > 0 and repair and repair.validation_codes:
        findings.append(
            QualityFinding(
                code=repair.validation_codes[-1],
                message=(
                    "Semantic structure repair validation failed; "
                    "category=SEMANTIC_STRUCTURE_DEGRADED"
                    f"{diagnostic}"
                ),
                path="quality.semantic-structure-repair.json",
            )
        )
~~~

After it, keep the existing SEMANTIC_STRUCTURE_DEGRADED finding and
generated.data-semantic.md path unchanged, appending the same diagnostic. This
preserves the publication category while SourceCheckService, which selects the
first hard finding, reports the last and most specific validation code.

- [ ] **Step 4: Write source-check message propagation regression**

Monkeypatch the imported ModelingService with a focused stub returning one
detailed failed quality finding:

~~~python
def test_source_check_preserves_quality_finding_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_product_fixture(tmp_path)

    class FailedModelingService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def validate(self, *_args: object, **_kwargs: object) -> ValidationResult:
            return ValidationResult(
                passed=False,
                findings=[
                    QualityFinding(
                        code="SEMANTIC_REPAIR_ORDER_INVALID",
                        message=(
                            "Semantic structure repair validation failed; "
                            "category=SEMANTIC_STRUCTURE_DEGRADED; "
                            "extraction_mode=ocr; "
                            "unresolved_spans=4; pages=1; validation_codes="
                            "SEMANTIC_REPAIR_MISSING_SPAN,"
                            "SEMANTIC_REPAIR_ORDER_INVALID; "
                            "provider=openai_compatible; model=gpt-5.6-terra; "
                            "applied_blocks=0; rejected_blocks=2; attempts=2"
                        ),
                        path="quality.semantic-structure-repair.json",
                    )
                ],
            )

    monkeypatch.setattr(
        source_check_module,
        "ModelingService",
        FailedModelingService,
    )

    with pytest.raises(WorkflowValidationError) as caught:
        SourceCheckService(RepositoryPaths(tmp_path)).run("sales-order", SHA)

    assert caught.value.code == "SEMANTIC_REPAIR_ORDER_INVALID"
    assert "validation_codes=SEMANTIC_REPAIR_MISSING_SPAN" in caught.value.message
    assert "category=SEMANTIC_STRUCTURE_DEGRADED" in caught.value.message
    assert "path=quality.semantic-structure-repair.json" in caught.value.message
~~~

Import ValidationResult from ard_ossie.application.modeling and QualityFinding
from ard_ossie.pipeline.

- [ ] **Step 5: Preserve the safe message through application contracts**

In WorkflowError.__init__(), retain the safe message separately from the combined exception string:

~~~python
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.retryable = retryable
~~~

In SourceCheckService.run(), replace the generic failure message with the selected finding's message and path:

~~~python
        if not validation.passed:
            if validation.findings:
                finding = validation.findings[0]
                location = f"; path={finding.path}" if finding.path else ""
                raise WorkflowValidationError(
                    finding.code,
                    f"{finding.message}{location}",
                )
            raise WorkflowValidationError(
                "MODEL_VALIDATION_FAILED",
                "staged product validation failed without a quality finding",
            )
~~~

- [ ] **Step 6: Write the CLI detail-and-secret regression**

Add a service stub that raises a known safe error and assert both stderr and the JSON envelope use its message:

~~~python
class FailingService:
    def run(self, *args, **kwargs) -> WorkflowResult:
        raise WorkflowValidationError(
            "SEMANTIC_REPAIR_MISSING_SPAN",
            (
                "category=SEMANTIC_STRUCTURE_DEGRADED; "
                "extraction_mode=ocr; unresolved_spans=4; "
                "validation_codes=SEMANTIC_REPAIR_MISSING_SPAN"
            ),
        )


def test_source_check_cli_publishes_safe_detailed_validation_message(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        workflow_cli,
        "_source_check_service",
        lambda paths, require_llm: FailingService(),
        raising=False,
    )
    monkeypatch.setenv("ARD_LLM_API_KEY", "must-not-appear")

    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "source-check",
            "--product-key",
            "sales-order",
            "--expected-head",
            "a" * 40,
            "--repository",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 10
    assert "SEMANTIC_REPAIR_MISSING_SPAN" in result.output
    assert "must-not-appear" not in result.output
    envelope = json.loads(
        (tmp_path / ".ard" / "run" / "workflow.source-check-result.json").read_text()
    )
    assert envelope["findings"][0]["code"] == "SEMANTIC_REPAIR_MISSING_SPAN"
    assert "category=SEMANTIC_STRUCTURE_DEGRADED" in envelope["findings"][0]["message"]
    assert "unresolved_spans=4" in envelope["findings"][0]["message"]
    assert "must-not-appear" not in json.dumps(envelope)
~~~

Import WorkflowValidationError in this test module.

- [ ] **Step 7: Publish the detailed message in _publish()**

Change only the WorkflowError branch:

~~~python
            findings=[{"code": error.code, "message": error.message}],
~~~

and:

~~~python
        typer.echo(f"{error.code}: {error.message}", err=True)
~~~

Leave the generic ValueError/TypeError sanitization branch unchanged because arbitrary exception strings are not a trusted public diagnostic source.

- [ ] **Step 8: Run the focused diagnostics tests**

Run:

~~~bash
uv run --frozen pytest -q \
  tests/integration/test_docling_pipeline.py::test_semantic_structure_degraded_finding_includes_safe_repair_diagnostics \
  tests/unit/test_source_check_service.py::test_source_check_preserves_quality_finding_details \
  tests/integration/test_workflow_direct_cli.py::test_source_check_cli_publishes_safe_detailed_validation_message \
  tests/integration/test_workflow_direct_cli.py::test_source_check_cli_maps_provider_factory_failure_to_configuration_exit
~~~

Expected: 4 tests PASS. The existing provider-configuration case confirms exit-code behavior remains unchanged.

- [ ] **Step 9: Commit safe diagnostics**

~~~bash
git add \
  src/ard_ossie/pipeline.py \
  src/ard_ossie/application/contracts.py \
  src/ard_ossie/application/source_check.py \
  src/ard_ossie/cli/workflow.py \
  tests/integration/test_docling_pipeline.py \
  tests/unit/test_source_check_service.py \
  tests/integration/test_workflow_direct_cli.py
git commit -m "fix: expose semantic repair diagnostics"
~~~

### Task 4: Run the bounded local verification gate

**Files:**
- Verify: all files changed in Tasks 1-3

**Interfaces:**
- Consumes: OCR planner invocation, two-attempt semantic validation, safe workflow diagnostics.
- Produces: a locally verified branch ready for required PR checks; no generated schema changes and no broad local test run.

- [ ] **Step 1: Run the complete affected semantic-repair module**

~~~bash
uv run --frozen pytest -q tests/unit/semantic/test_repair.py
~~~

Expected: all tests in this one focused module PASS.

- [ ] **Step 2: Run only affected integration selections**

~~~bash
uv run --frozen pytest -q \
  tests/integration/test_docling_pipeline.py -k \
  "unresolved_full_page_ocr or full_page_ocr_preserves_paragraph or failed_table_repair or provider_execution_failure or unresolved_ordinary_span or residual_ordinary_span or semantic_structure_degraded_finding" \
  tests/unit/test_source_check_service.py -k \
  "injects_provider or maps_provider_failure or preserves_quality_finding_details" \
  tests/integration/test_workflow_direct_cli.py -k \
  "source_check_cli"
~~~

Expected: selected tests PASS. Record the exact count in the implementation report; do not expand to the full repository suite.

- [ ] **Step 3: Run Ruff only on changed Python and test files**

~~~bash
uv run --frozen ruff check \
  src/ard_ossie/semantic/parser.py \
  src/ard_ossie/semantic/repair.py \
  src/ard_ossie/pipeline.py \
  src/ard_ossie/application/contracts.py \
  src/ard_ossie/application/source_check.py \
  src/ard_ossie/cli/workflow.py \
  tests/unit/semantic/test_repair.py \
  tests/integration/test_docling_pipeline.py \
  tests/unit/test_source_check_service.py \
  tests/integration/test_workflow_direct_cli.py
~~~

Expected: All checks passed!

- [ ] **Step 4: Verify diff hygiene and schema stability**

~~~bash
git diff --check origin/main...HEAD
git status --short
git diff --name-only origin/main...HEAD
~~~

Expected: no whitespace errors; only the design, plan, named source files, and named tests appear. No file under schemas/ or src/ard_ossie/schemas/ changes.

- [ ] **Step 5: Commit any focused verification correction**

If a focused test or Ruff required a code correction, stage only the affected
files from this exact allowlist and commit them:

~~~bash
git add \
  src/ard_ossie/semantic/parser.py \
  src/ard_ossie/semantic/repair.py \
  src/ard_ossie/pipeline.py \
  src/ard_ossie/application/contracts.py \
  src/ard_ossie/application/source_check.py \
  src/ard_ossie/cli/workflow.py \
  tests/unit/semantic/test_repair.py \
  tests/integration/test_docling_pipeline.py \
  tests/unit/test_source_check_service.py \
  tests/integration/test_workflow_direct_cli.py
git commit -m "test: complete OCR repair verification"
~~~

If the tree is already clean, do not create an empty commit.

### Task 5: Publish, merge, and perform one controlled Issue 3 run

**Files:**
- Inspect: .github/workflows/ard-issue-intake.yml
- Inspect: .github/workflows/ard-process.yml
- Inspect remotely: PR required checks, Issue 3 run, generated product PR artifacts
- Do not modify workflow files or repository environment secrets.

**Interfaces:**
- Consumes: the verified feature branch and GitHub's required PR status checks.
- Produces: one merged code fix, one controlled Issue 3 reprocessing run, an exact artifact verdict, and restored ard-llm reviewer protection.

- [ ] **Step 1: Push the focused branch and create a non-draft code PR**

~~~bash
git push -u origin fix/ocr-structure-repair
gh pr create \
  --base main \
  --head fix/ocr-structure-repair \
  --title "Fix OCR semantic structure repair" \
  --body-file docs/superpowers/specs/2026-08-14-ocr-structure-repair-design.md
~~~

The PR body must also state: no broad local suite was run by request; list exact focused test counts; SEMANTIC_STRUCTURE_DEGRADED remains hard; semantic retry is limited to one; no schema files changed.

- [ ] **Step 2: Wait for the required code PR checks once**

~~~bash
ocr_code_pr=$(gh pr view fix/ocr-structure-repair --json number --jq .number)
gh pr checks "$ocr_code_pr" --watch --interval 20
~~~

Expected: both required repository statuses finish pass. If either fails, obtain
the exact failed run from the check link and inspect only its failed jobs:

~~~bash
ocr_failed_run=$(
  gh pr checks "$ocr_code_pr" --json state,link \
    --jq '[.[] | select(.state != "SUCCESS")][0].link' \
  | sed -E 's#.*/runs/([0-9]+).*#\1#'
)
test -n "$ocr_failed_run"
gh run view "$ocr_failed_run" --log-failed
~~~

Fix the diagnosed cause, rerun the focused affected test, push once, and wait
for the new required checks. Do not repeatedly rerun an unchanged failure.

- [ ] **Step 3: Merge the code PR only after both required checks pass**

~~~bash
gh pr merge "$ocr_code_pr" --merge --delete-branch
~~~

Record the PR URL and merge SHA. Confirm origin/main contains that SHA before touching Issue 3.

- [ ] **Step 4: Temporarily remove only the environment reviewer rule**

Ask the user to open:

Repository Settings -> Environments -> ard-llm -> Deployment protection rules -> Required reviewers

Remove only reviewer kimohy. Keep environment secrets and the custom branch policy unchanged. Start a two-hour restoration timer immediately. Do not trigger Issue 3 until the user confirms this exact change.

- [ ] **Step 5: Trigger Issue 3 exactly once**

Capture the current run first, remove and re-add only ard:approved on Issue 3,
then accept only a newly created ARD issue intake run ID:

~~~bash
ocr_previous_run=$(
  gh run list --workflow "ARD issue intake" --event issues --limit 1 \
    --json databaseId --jq '.[0].databaseId // empty'
)
gh issue edit 3 --remove-label "ard:approved"
gh issue edit 3 --add-label "ard:approved"
ocr_issue_run=""
for ocr_poll in $(seq 1 12); do
  ocr_issue_run=$(
    gh run list --workflow "ARD issue intake" --event issues --limit 1 \
      --json databaseId --jq '.[0].databaseId // empty'
  )
  if test -n "$ocr_issue_run" && test "$ocr_issue_run" != "$ocr_previous_run"; then
    break
  fi
  sleep 5
done
test -n "$ocr_issue_run"
test "$ocr_issue_run" != "$ocr_previous_run"
~~~

Do not dispatch a manual duplicate run and do not touch other labels.

- [ ] **Step 6: Monitor the one run to a terminal state**

~~~bash
gh run watch "$ocr_issue_run" --interval 20 --exit-status
~~~

On failure, inspect only the failed job log once and record its exact code and detailed message. Do not rerun before a new code change. Restore reviewer kimohy immediately on terminal failure, work stoppage, or the two-hour timeout.

- [ ] **Step 7: Verify artifacts at the exact new product PR head**

Resolve the current managed PR and head SHA, then inspect files from that SHA rather than a moving checkout:

~~~bash
gh pr view 5 --json number,isDraft,headRefOid,url
ocr_product_head=$(gh pr view 5 --json headRefOid --jq .headRefOid)
gh api "repos/kimohy/ard-ossie-provider/contents/products/semantics/generated/data-semantic.md?ref=$ocr_product_head" --jq .content | base64 --decode
gh api "repos/kimohy/ard-ossie-provider/contents/products/semantics/quality/semantic-fidelity.json?ref=$ocr_product_head" --jq .content | base64 --decode
gh api "repos/kimohy/ard-ossie-provider/contents/products/semantics/quality/semantic-structure-repair.json?ref=$ocr_product_head" --jq .content | base64 --decode
gh api "repos/kimohy/ard-ossie-provider/contents/products/semantics/quality/quality-report.json?ref=$ocr_product_head" --jq .content | base64 --decode
~~~

Use the product path reported by the run if it differs from products/semantics;
do not guess silently.

- [ ] **Step 8: Apply the exact Issue 3 acceptance checklist**

Accept only if all conditions are true:

~~~text
data-semantic.md:
  contains: Semantics 문서, 개인정보, 유효성, 캠페인 기간
  excludes: 是州, 号h, 左叫, raw HTML tags, &#32;, &#9;

semantic-fidelity.json:
  page_count == 5
  source_text_coverage == 1.0
  ocr_correction_rejected_count == 0
  degraded_block_count == 0
  warning_codes == []
  status == "PASS"

semantic-structure-repair.json:
  outcome in {"applied", "reused"}
  provider_error_code is null
  rejected_orders == []

quality-report.json:
  hard_errors == []
  status == "PASS", or "WARN" only for an unrelated warning explained in the report
~~~

Keep PR 5 draft if any condition fails. Do not manually edit generated artifacts.

- [ ] **Step 9: Restore environment protection and report exact evidence**

Immediately restore reviewer kimohy at:

Repository Settings -> Environments -> ard-llm -> Deployment protection rules -> Required reviewers

Confirm the rule is visible again. Report the code PR URL, merge SHA, Issue 3 workflow run URL and conclusion, product PR exact head SHA, every acceptance metric, reviewer restoration time, and any remaining blocker. If the run failed, include the new detailed validation code and bounded metadata but no source text or provider response.
