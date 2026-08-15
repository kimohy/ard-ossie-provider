# Low-Confidence LLM Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover bounded semantic PDF decisions whose first LLM confidence is too low, apply only qualified same-candidate or two-of-three consensus, and persist a privacy-safe attempt and application audit.

**Architecture:** Extend the existing deterministic-first `CandidateAdjudicator` with immutable vote-attempt records and a three-phase `primary -> recovery -> tiebreak` state machine. Keep the final trusted cache key stable at the candidate request boundary while hashing every phase prompt independently. Extend semantic diagnostics with an application report that separates successful consensus from document-wide publication.

**Tech Stack:** Python 3.12, Pydantic 2, existing `LLMService` and closed candidate schema, pytest, Ruff, uv, GitHub Actions.

## Global Constraints

- Embedded PDF non-whitespace code points must never be added, deleted, substituted, or reordered.
- A model may return only an allowlisted candidate ID and advisory confidence; it may not return replacement text, source references, ordering, or table geometry.
- `minimum_model_confidence` remains `0.80`; this change must not lower or bypass it.
- A low-confidence decision receives at most two additional semantic vote phases.
- A changed recovery candidate requires a qualified two-of-three majority.
- Deterministic decisions make zero model calls.
- Cached recovered decisions make zero model calls and preserve their complete original audit.
- `REVIEW_REQUIRED` and `FAILED` never silently become publishable success.
- Canonical character, order and table invariants override every model consensus.
- Default diagnostics contain no raw prompt, response, source text or image bytes.
- DOCX, HTML, dictionary, Registry and Ossie behavior remains unchanged.

---

### Task 1: Add backward-compatible adjudication audit models

**Files:**
- Modify: `src/ard_ossie/semantic/adjudication.py`
- Modify: `tests/unit/semantic/test_adjudication.py`

**Interfaces:**
- Consumes: existing `CandidateId`, `Sha256`, `ValidationCode`, `ImmutableStrictModel`.
- Produces: `AdjudicationAttempt`, new `DecisionRecord` recovery fields, and content-addressed terminal decisions that include attempt history.

- [ ] **Step 1: Write failing audit-model tests**

Add imports for `AdjudicationAttempt` and `DecisionRecord`, then add:

```python
def test_decision_record_loads_legacy_payload_with_recovery_defaults() -> None:
    legacy = _legacy_decision_payload()

    decision = DecisionRecord.model_validate(legacy)

    assert decision.recovery_status == "not_needed"
    assert decision.attempts == ()
    assert decision.consensus_method == "none"
    assert decision.consensus_candidate_id is None
    assert decision.recovery_count == 0


def test_adjudication_attempt_is_closed_bounded_and_content_addressed() -> None:
    attempt = AdjudicationAttempt(
        attempt_index=1,
        phase="primary",
        request_hash="1" * 64,
        candidate_id="candidate_0000000000000001",
        confidence=0.70,
        status="low_confidence",
        validation_codes=("LLM_CONFIDENCE_TOO_LOW",),
        provider_retry_count=0,
        provider_repair_count=0,
    )

    assert attempt.model_dump(mode="json")["phase"] == "primary"
    with pytest.raises(ValidationError):
        AdjudicationAttempt.model_validate({**attempt.model_dump(), "attempt_index": 7})
```

`_legacy_decision_payload()` must contain exactly the fields accepted by the pre-change
`DecisionRecord`, proving trusted v1 JSON remains loadable.

- [ ] **Step 2: Run the tests and verify RED**

```bash
uv run --frozen pytest -q \
  tests/unit/semantic/test_adjudication.py::test_decision_record_loads_legacy_payload_with_recovery_defaults \
  tests/unit/semantic/test_adjudication.py::test_adjudication_attempt_is_closed_bounded_and_content_addressed
```

Expected: collection fails because `AdjudicationAttempt` and the recovery fields do not exist.

- [ ] **Step 3: Implement immutable audit fields**

In `adjudication.py`, set `PROMPT_VERSION = "semantic-candidate-adjudication-v2"` and add:

```python
class AdjudicationAttempt(ImmutableStrictModel):
    attempt_index: int = Field(ge=1, le=6)
    phase: Literal["primary", "recovery", "tiebreak"]
    request_hash: Sha256
    candidate_id: CandidateId | None = None
    confidence: float = Field(default=0.0, ge=0, le=1)
    status: Literal[
        "accepted",
        "low_confidence",
        "candidate_unknown",
        "provider_rejected",
    ]
    validation_codes: tuple[ValidationCode, ...] = Field(default=(), max_length=4)
    provider_retry_count: int = Field(default=0, ge=0, le=2)
    provider_repair_count: int = Field(default=0, ge=0, le=2)
```

Extend `AdjudicationPolicy` with `max_confidence_recovery_attempts=2` (`0..2`) and
`consensus_votes_required=2` (`2..2`). Extend `DecisionRecord` with defaulted fields:

```python
recovery_status: Literal["not_needed", "recovered", "review_required"] = "not_needed"
attempts: tuple[AdjudicationAttempt, ...] = Field(default=(), max_length=6)
consensus_method: Literal["none", "same_candidate", "two_of_three"] = "none"
consensus_candidate_id: CandidateId | None = None
recovery_count: int = Field(default=0, ge=0, le=2)
```

Add `"recovered"` to the `source` literal. Extend `_record()` to accept these fields and include
the ordered attempt request hashes, recovery status, consensus method and consensus candidate in
the terminal decision digest.

- [ ] **Step 4: Run focused tests and verify GREEN**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_adjudication.py
```

Expected: all existing and new adjudication model tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ard_ossie/semantic/adjudication.py tests/unit/semantic/test_adjudication.py
git commit -m "feat: record bounded adjudication attempts"
```

---

### Task 2: Implement low-confidence recovery and consensus

**Files:**
- Modify: `src/ard_ossie/semantic/adjudication.py`
- Modify: `src/ard_ossie/semantic/canonical.py`
- Modify: `tests/unit/semantic/test_adjudication.py`
- Modify: `tests/unit/semantic/test_canonical.py`

**Interfaces:**
- Consumes: Task 1 audit models, existing `LLMService`, closed `CandidateChoice` schema and candidate summaries.
- Produces: `_run_vote_phase(...)`, `_recovery_messages(...)`, `_attempt_request_hash(...)`, and terminal `recovered|review_required` decisions.

- [ ] **Step 1: Write the same-candidate recovery test**

Replace the old one-call low-confidence assertion with:

```python
def test_low_confidence_is_recovered_when_second_vote_matches() -> None:
    candidate_set = _spacing_set(0.80, 0.75)
    selected = candidate_set.candidates[0].candidate_id
    provider = RecordingProvider(
        [
            {"candidate_id": selected, "confidence": 0.70},
            {"candidate_id": selected, "confidence": 0.92},
        ]
    )

    decision = CandidateAdjudicator(provider).decide(candidate_set)

    assert decision.outcome == "selected"
    assert decision.source == "recovered"
    assert decision.selected_candidate_id == selected
    assert decision.recovery_status == "recovered"
    assert decision.consensus_method == "same_candidate"
    assert decision.recovery_count == 1
    assert [attempt.phase for attempt in decision.attempts] == ["primary", "recovery"]
    assert [attempt.confidence for attempt in decision.attempts] == [0.70, 0.92]
    assert decision.validation_codes == ("LLM_LOW_CONFIDENCE_RECOVERED",)
    assert len(provider.calls) == 2
```

- [ ] **Step 2: Run the same-candidate test and verify RED**

```bash
uv run --frozen pytest -q \
  tests/unit/semantic/test_adjudication.py::test_low_confidence_is_recovered_when_second_vote_matches
```

Expected: FAIL because the current adjudicator returns `review_required` after one call.

- [ ] **Step 3: Implement primary and recovery vote phases**

Create a private phase result that returns a valid `CandidateChoice`, its ordered attempts, or a
terminal provider-output rejection. `_run_vote_phase()` must use the existing schema correction
loop, assign contiguous attempt indexes and hash the exact phase messages:

```python
def _attempt_request_hash(
    base_request_hash: Sha256,
    *,
    phase: str,
    messages: list[dict[str, str]],
    attempt_index: int,
) -> Sha256:
    return canonical_hash(
        {
            "base_request_hash": base_request_hash,
            "phase": phase,
            "attempt_index": attempt_index,
            "messages_hash": canonical_hash(messages),
        }
    )
```

Primary confidence below the policy threshold must create a `low_confidence` attempt and call the
recovery phase. `_recovery_messages()` must include `phase`, prior candidate ID/confidence/codes,
the same allowlisted candidate summaries, and the bounded Korean/identifier comparison rules from
the design. Add `"phase": "primary"` to the primary user request so providers and audit tooling
can distinguish every semantic vote. If the recovery candidate matches and is at least `0.80`,
return `source=recovered`, `consensus_method=same_candidate`.

- [ ] **Step 4: Run the same-candidate test and verify GREEN**

Run the command from Step 2. Expected: PASS with exactly two provider calls.

- [ ] **Step 5: Write failing disagreement and exhaustion tests**

```python
def test_disagreement_uses_high_confidence_two_of_three_tiebreak() -> None:
    candidate_set = _spacing_set(0.80, 0.75)
    first, second = [item.candidate_id for item in candidate_set.candidates]
    provider = RecordingProvider(
        [
            {"candidate_id": first, "confidence": 0.70},
            {"candidate_id": second, "confidence": 0.91},
            {"candidate_id": first, "confidence": 0.93},
        ]
    )

    decision = CandidateAdjudicator(provider).decide(candidate_set)

    assert decision.outcome == "selected"
    assert decision.selected_candidate_id == first
    assert decision.consensus_method == "two_of_three"
    assert decision.consensus_candidate_id == first
    assert decision.recovery_count == 2
    assert [item.phase for item in decision.attempts] == [
        "primary", "recovery", "tiebreak"
    ]


def test_second_low_confidence_vote_exhausts_recovery_without_tiebreak() -> None:
    candidate_set = _spacing_set(0.80, 0.75)
    selected = candidate_set.candidates[0].candidate_id
    provider = RecordingProvider(
        [
            {"candidate_id": selected, "confidence": 0.70},
            {"candidate_id": selected, "confidence": 0.74},
        ]
    )

    decision = CandidateAdjudicator(provider).decide(candidate_set)

    assert decision.outcome == "review_required"
    assert decision.selected_candidate_id is None
    assert decision.recovery_status == "review_required"
    assert decision.validation_codes == ("LLM_CONFIDENCE_RECOVERY_EXHAUSTED",)
    assert len(provider.calls) == 2


def test_tiebreak_without_majority_requires_review() -> None:
    candidate_set = _spacing_set(0.80, 0.75, 0.74)
    first, second, third = [item.candidate_id for item in candidate_set.candidates]
    provider = RecordingProvider(
        [
            {"candidate_id": first, "confidence": 0.70},
            {"candidate_id": second, "confidence": 0.91},
            {"candidate_id": third, "confidence": 0.94},
        ]
    )

    decision = CandidateAdjudicator(provider).decide(candidate_set)

    assert decision.outcome == "review_required"
    assert decision.validation_codes == ("LLM_CONSENSUS_NOT_REACHED",)
```

Update `_spacing_set` to accept a variadic score list while preserving all current call sites.

- [ ] **Step 6: Run the new tests and verify RED**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_adjudication.py -k 'disagreement or exhaustion or tiebreak'
```

Expected: FAIL because tie-break consensus and recovery exhaustion codes are absent.

- [ ] **Step 7: Implement tie-break and exact terminal rules**

When recovery returns a different high-confidence candidate, call an independent tie-break phase.
Count candidate IDs with `collections.Counter`; accept only when the tie-break belongs to the unique
two-vote majority and the tie-break confidence is at least the threshold. Otherwise return
`LLM_CONSENSUS_NOT_REACHED`. A second low-confidence vote returns
`LLM_CONFIDENCE_RECOVERY_EXHAUSTED` without a third call. Provider transient errors continue to
propagate and output failures remain `review_required` with all completed attempts retained.

Update canonical `model_call_count` to count current-run attempts for non-cache decisions:

```python
def _current_model_call_count(decision: DecisionRecord) -> int:
    if decision.source == "cache":
        return 0
    if decision.attempts:
        return sum(
            1 + attempt.provider_retry_count + attempt.provider_repair_count
            for attempt in decision.attempts
        )
    return int(decision.source in {"model", "provider", "recovered"})
```

- [ ] **Step 8: Run adjudication and canonical tests and verify GREEN**

```bash
uv run --frozen pytest -q \
  tests/unit/semantic/test_adjudication.py \
  tests/unit/semantic/test_canonical.py
```

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add \
  src/ard_ossie/semantic/adjudication.py \
  src/ard_ossie/semantic/canonical.py \
  tests/unit/semantic/test_adjudication.py \
  tests/unit/semantic/test_canonical.py
git commit -m "feat: recover low-confidence semantic decisions"
```

---

### Task 3: Preserve recovered audits through trusted cache reuse

**Files:**
- Modify: `src/ard_ossie/semantic/adjudication.py`
- Modify: `tests/unit/semantic/test_adjudication.py`

**Interfaces:**
- Consumes: recovered `DecisionRecord` from Task 2.
- Produces: `_trusted_decision_matches(...)` and cache results that keep the original attempts and consensus metadata while reporting `source=cache`.

- [ ] **Step 1: Write failing trusted-recovery tests**

```python
def test_trusted_recovered_decision_reuses_full_audit_without_provider_call() -> None:
    candidate_set = _spacing_set(0.80, 0.75)
    selected = candidate_set.candidates[0].candidate_id
    first_provider = RecordingProvider(
        [
            {"candidate_id": selected, "confidence": 0.70},
            {"candidate_id": selected, "confidence": 0.92},
        ]
    )
    recovered = CandidateAdjudicator(first_provider).decide(candidate_set)
    reuse_provider = RecordingProvider()

    reused = CandidateAdjudicator(reuse_provider, trusted=(recovered,)).decide(candidate_set)

    assert reused.source == "cache"
    assert reused.selected_candidate_id == selected
    assert reused.recovery_status == "recovered"
    assert reused.consensus_method == "same_candidate"
    assert reused.consensus_candidate_id == selected
    assert reused.attempts == recovered.attempts
    assert reused.recovery_count == 1
    assert reuse_provider.calls == []


def test_invalid_trusted_consensus_is_ignored() -> None:
    candidate_set = _spacing_set(0.80, 0.75)
    selected = candidate_set.candidates[0].candidate_id
    recovered = CandidateAdjudicator(
        RecordingProvider(
            [
                {"candidate_id": selected, "confidence": 0.70},
                {"candidate_id": selected, "confidence": 0.92},
            ]
        )
    ).decide(candidate_set)
    invalid = recovered.model_copy(update={"consensus_candidate_id": "candidate_ffffffffffffffff"})
    provider = RecordingProvider(
        [{"candidate_id": selected, "confidence": 0.93}]
    )

    fresh = CandidateAdjudicator(provider, trusted=(invalid,)).decide(candidate_set)

    assert fresh.source != "cache"
    assert len(provider.calls) == 1
```

- [ ] **Step 2: Run the tests and verify RED**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_adjudication.py -k 'trusted_recovered or invalid_trusted_consensus'
```

Expected: the first test loses attempts/consensus and the second accepts an invalid cache entry.

- [ ] **Step 3: Implement strict trusted-decision validation and cache cloning**

Extract `_trusted_decision_matches()` from the inline generator. Require the existing identity
fields plus:

- selected candidate is in the current allowlist;
- recovered decisions have `recovery_status=recovered`;
- recovered consensus candidate equals the selected candidate and is allowlisted;
- attempt indexes are contiguous and every attempt request hash is present;
- the number of recovery/tie-break phases equals `recovery_count`;
- legacy selected decisions with no attempts remain reusable.

For a match, use `trusted.model_copy(update={"decision_id": new_cache_id, "source": "cache"})`
instead of reconstructing the record. Derive the cache decision ID from the trusted decision ID,
base request hash and `source=cache`, preserving all historical audit fields.

- [ ] **Step 4: Run adjudication tests and verify GREEN**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_adjudication.py
```

Expected: all tests pass and cache reuse makes zero provider calls.

- [ ] **Step 5: Commit**

```bash
git add src/ard_ossie/semantic/adjudication.py tests/unit/semantic/test_adjudication.py
git commit -m "fix: preserve recovered decision audit on reuse"
```

---

### Task 4: Persist recovery application diagnostics

**Files:**
- Modify: `src/ard_ossie/semantic/diagnostics.py`
- Modify: `tests/unit/semantic/test_diagnostics.py`
- Modify: `tests/unit/test_release.py`

**Interfaces:**
- Consumes: `DecisionReport` with Task 2 recovery metadata and existing `SemanticValidationReport`.
- Produces: `DecisionApplicationRecord`, `ApplicationReport`, and `quality/application-report.json` included atomically with all semantic diagnostic reports.

- [ ] **Step 1: Write failing application-report and privacy tests**

Import `AdjudicationAttempt` and `DecisionRecord`. Extend `_diagnostics()` to accept decisions and
validation status. Add this exact fixture before the tests:

```python
def recovered_decision_fixture() -> DecisionRecord:
    selected = "candidate_0000000000000001"
    return DecisionRecord(
        decision_id="decision_0000000000000001",
        request_hash="1" * 64,
        source_hash=SOURCE_HASH,
        evidence_hash="2" * 64,
        candidate_set_id="candidate_set_0000000000000001",
        region_id="region_0000000000000001",
        decision_type="spacing",
        selected_candidate_id=selected,
        outcome="selected",
        source="recovered",
        confidence=0.92,
        provider="test",
        model="test",
        validation_codes=("LLM_LOW_CONFIDENCE_RECOVERED",),
        recovery_status="recovered",
        attempts=(
            AdjudicationAttempt(
                attempt_index=1,
                phase="primary",
                request_hash="3" * 64,
                candidate_id=selected,
                confidence=0.70,
                status="low_confidence",
                validation_codes=("LLM_CONFIDENCE_TOO_LOW",),
            ),
            AdjudicationAttempt(
                attempt_index=2,
                phase="recovery",
                request_hash="4" * 64,
                candidate_id=selected,
                confidence=0.92,
                status="accepted",
            ),
        ),
        consensus_method="same_candidate",
        consensus_candidate_id=selected,
        recovery_count=1,
    )
```

Then add:

```python
def test_recovered_decision_application_report_records_publication_outcome(tmp_path: Path) -> None:
    recovered = recovered_decision_fixture()
    write_semantic_diagnostics(
        tmp_path,
        _diagnostics(decisions=(recovered,), validation_status="verified"),
    )

    report = json.loads((tmp_path / "application-report.json").read_text())

    assert report["recovered_decision_count"] == 1
    assert report["confidence_recovery_attempt_count"] == 1
    assert report["tie_break_attempt_count"] == 0
    assert report["unresolved_low_confidence_count"] == 0
    assert report["applications"][0]["outcome"] == "applied"
    assert report["applications"][0]["selected_candidate_id"] == (
        recovered.selected_candidate_id
    )


@pytest.mark.parametrize(
    ("validation_status", "outcome"),
    [("review_required", "not_published"), ("failed", "rejected_by_invariant")],
)
def test_application_report_distinguishes_review_from_invariant_failure(
    tmp_path: Path,
    validation_status: str,
    outcome: str,
) -> None:
    write_semantic_diagnostics(
        tmp_path,
        _diagnostics(
            decisions=(recovered_decision_fixture(),),
            validation_status=validation_status,
        ),
    )

    report = json.loads((tmp_path / "application-report.json").read_text())
    assert report["applications"][0]["outcome"] == outcome


def test_default_application_report_contains_no_raw_prompt_response_or_source(
    tmp_path: Path,
) -> None:
    write_semantic_diagnostics(tmp_path, _diagnostics(decisions=(recovered_decision_fixture(),)))
    payload = (tmp_path / "application-report.json").read_text()
    assert "민감한 원문 데이터" not in payload
    assert "messages" not in payload
    assert "response" not in payload
```

Update the existing expected file-name set to include `application-report.json`.

- [ ] **Step 2: Run diagnostics tests and verify RED**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_diagnostics.py
```

Expected: FAIL because `application-report.json` is not written.

- [ ] **Step 3: Implement application audit models and serialization**

Add `application-report.json` to `DIAGNOSTIC_REPORT_NAMES`. Define:

```python
class DecisionApplicationRecord(ImmutableStrictModel):
    decision_id: DecisionId
    candidate_set_id: CandidateSetId
    selected_candidate_id: CandidateId
    canonical_hash: Sha256
    validation_status: Literal["verified", "review_required", "failed"]
    outcome: Literal["applied", "not_published", "rejected_by_invariant"]
    invariant_codes: tuple[str, ...] = ()


class ApplicationReport(ImmutableStrictModel):
    source_hash: Sha256
    primary_attempt_count: int = Field(ge=0)
    confidence_recovery_attempt_count: int = Field(ge=0)
    tie_break_attempt_count: int = Field(ge=0)
    recovered_decision_count: int = Field(ge=0)
    unresolved_low_confidence_count: int = Field(ge=0)
    applications: tuple[DecisionApplicationRecord, ...] = ()
```

Build application records only for `recovery_status=recovered`. Map `verified -> applied`,
`review_required -> not_published`, and `failed -> rejected_by_invariant`. Include validation
finding codes only for `failed`. Count attempts by phase from the audit records. Exclude
`LLM_LOW_CONFIDENCE_RECOVERED` when generating `failure_codes`, while retaining it in the
authoritative decision report.

- [ ] **Step 4: Update release fixture inventory and verify diagnostics/release GREEN**

Add `application-report.json` to `add_candidate_diagnostics()` in `tests/unit/test_release.py`.
Then run:

```bash
uv run --frozen pytest -q \
  tests/unit/semantic/test_diagnostics.py \
  tests/unit/test_release.py
```

Expected: all tests pass; partial diagnostic sets remain rejected by release verification.

- [ ] **Step 5: Commit**

```bash
git add \
  src/ard_ossie/semantic/diagnostics.py \
  tests/unit/semantic/test_diagnostics.py \
  tests/unit/test_release.py
git commit -m "feat: persist semantic recovery application audit"
```

---

### Task 5: Exercise Issue #3 recovery and bounded-call replay

**Files:**
- Modify: `scripts/verify_issue_3_semantic.py`
- Modify: `tests/integration/test_semantic_pdf_regressions.py`
- Modify: `tests/performance/test_semantic_pdf_performance.py`
- Modify: `docs/operations/semantic-pdf-rollout.md`

**Interfaces:**
- Consumes: Task 2 phase marker in LLM request payload and trusted recovered decisions.
- Produces: deterministic Issue #3 replay that reproduces the observed `0.70` and `0.74` primary confidences, recovers them, and proves zero-call reuse.

- [ ] **Step 1: Write failing Issue #3 recovery assertions**

Add to the existing regression test:

```python
    recovered = [
        decision
        for decision in result.decisions.decisions
        if decision.recovery_status == "recovered"
    ]
    assert len(recovered) == 2
    assert sorted(item.attempts[0].confidence for item in recovered) == [0.70, 0.74]
    assert all(item.consensus_method == "same_candidate" for item in recovered)
    assert all(item.validation_codes == ("LLM_LOW_CONFIDENCE_RECOVERED",) for item in recovered)
    assert repeated_provider.calls == 0
    assert all(
        repeated_decision.source == "cache"
        for repeated_decision in repeated.decisions.decisions
        if repeated_decision.candidate_set_id
        in {item.candidate_set_id for item in recovered}
    )
```

Change the performance assertion to audit exact calls:

```python
    audited_calls = sum(
        len(decision.attempts)
        for decision in result.decisions.decisions
        if decision.source != "cache"
    )
    assert provider.calls == audited_calls
    assert provider.calls <= 3 * ambiguous_decisions
```

- [ ] **Step 2: Run replay tests and verify RED**

```bash
uv run --frozen pytest -q \
  tests/integration/test_semantic_pdf_regressions.py \
  tests/performance/test_semantic_pdf_performance.py
```

Expected: FAIL because replay responses are always `0.99` and no recovered decisions exist.

- [ ] **Step 3: Make the replay provider reproduce the live low-confidence pattern**

In `ReplayCandidateProvider`, define:

```python
LOW_CONFIDENCE_PRIMARY = {
    "candidate_set_78620dc093a748fe": 0.70,
    "candidate_set_6d08c750276170e3": 0.74,
}
```

Read `phase` from the request. Return the mapped confidence only for a `primary` request; return
`0.99` for its recovery request while selecting the same deterministic best candidate. Continue
to reject multimodal calls in this public fixture. Extend the JSON result from
`verify_evidence_replay()` with `recovered_decision_count` and `recovery_model_calls`.

- [ ] **Step 4: Document operator interpretation**

In `docs/operations/semantic-pdf-rollout.md`, document:

- `LLM_LOW_CONFIDENCE_RECOVERED` as a successful audited recovery;
- `LLM_CONFIDENCE_RECOVERY_EXHAUSTED` as unresolved model uncertainty;
- `LLM_CONSENSUS_NOT_REACHED` as conflicting bounded votes;
- the `application-report.json` outcomes and the rule that only `applied` accompanies globally
  verified publication.

- [ ] **Step 5: Run replay and regression tests and verify GREEN**

```bash
uv run --frozen pytest -q \
  tests/integration/test_semantic_pdf_regressions.py \
  tests/performance/test_semantic_pdf_performance.py

uv run --frozen python scripts/verify_issue_3_semantic.py \
  --evidence tests/fixtures/semantic/issue-3-evidence.json \
  --golden tests/fixtures/semantic/issue-3-golden.json
```

Expected: verified output, two recovered decisions, zero cache model calls, at most five candidates
per request, full character coverage, and no Korean corruption.

- [ ] **Step 6: Commit**

```bash
git add \
  scripts/verify_issue_3_semantic.py \
  tests/integration/test_semantic_pdf_regressions.py \
  tests/performance/test_semantic_pdf_performance.py \
  docs/operations/semantic-pdf-rollout.md
git commit -m "test: replay low-confidence semantic recovery"
```

---

### Task 6: Run repository-wide verification

**Files:**
- Verify only; modify production or test files only if a failure proves a defect in Tasks 1-5.

**Interfaces:**
- Consumes: all previous tasks.
- Produces: a clean branch ready for review and protected Issue #3 validation.

- [ ] **Step 1: Run formatting and static analysis**

```bash
uv run --frozen ruff format --check src tests scripts
uv run --frozen ruff check src tests scripts
git diff --check
```

Expected: all commands exit zero.

- [ ] **Step 2: Run trusted schema and repository checks**

```bash
uv run --frozen python -m ard_ossie.application.model_schema_verification \
  --repository "$PWD"
uv run --frozen ard workflow repository-check \
  --repository "$PWD" \
  --verification-group static
```

Expected: both checks succeed without changing tracked files.

- [ ] **Step 3: Run the full test suite**

```bash
uv run --frozen pytest -q
```

Expected: all tests pass with no warnings introduced by this change.

- [ ] **Step 4: Run Issue #3 twice through trusted reuse**

```bash
uv run --frozen python scripts/verify_issue_3_semantic.py \
  --evidence tests/fixtures/semantic/issue-3-evidence.json \
  --golden tests/fixtures/semantic/issue-3-golden.json
```

Expected JSON includes `"status":"verified"`, `"recovered_decision_count":2`, and
`"cache_model_calls":0`.

- [ ] **Step 5: Review final diff and branch state**

```bash
git status --short --branch
git diff --stat origin/main...HEAD
git log --oneline origin/main..HEAD
```

Expected: clean worktree, only scoped recovery/audit changes, and intentional commits.

- [ ] **Step 6: Commit any verification-only documentation adjustment**

If Step 1-5 required no file change, do not create an empty commit. If an exact verification
command or expected metric changed, update only `docs/operations/semantic-pdf-rollout.md`, rerun
the affected verification, then commit:

```bash
git add docs/operations/semantic-pdf-rollout.md
git commit -m "docs: finalize semantic recovery verification"
```
