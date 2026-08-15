# LLM Whitespace Repair and Deferred Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert Issue #3 exactly by preserving invariant-proven table-cell text, repair genuinely unresolved whitespace with bounded LLM generation, and continue Draft output with durable review debt when confidence remains low.

**Architecture:** Build a deterministic table fast path from atom-level bbox allocation and exact per-cell character conservation before any spacing adjudication. Route only unresolved non-table or character-owned cell spacing through a generated-candidate/independent-verifier protocol; apply a source-spacing fallback as `deferred_review` when it remains uncertain. Carry `review_pending` through canonical assembly and product generation while keeping release verification strictly `verified`, and harden trusted audit identity at the same boundary.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, Ruff, existing `LLMService`, GitHub Actions, Issue #3 evidence replay fixture.

## Global Constraints

- Never lower `minimum_model_confidence=0.80`.
- Never change, insert, delete, normalize, or reorder non-whitespace code points in an LLM whitespace repair.
- Never send a flattened table region to LLM whitespace generation.
- An invariant-proven table requires complete grid validity, unique atom allocation, zero unassigned atoms, bbox cell agreement, and exact per-cell Unicode character multisets.
- A high-confidence rendering with whitespace adjacent to an underscore is defective and must not be selected.
- Model budget per unresolved spacing decision is at most primary selection + one generation + one independent verification, excluding bounded provider schema repair.
- `review_pending` may generate Markdown and update a Draft PR, but release remains restricted to exact `verified` status.
- Tests must assert document/canonical output or a security/audit boundary; do not add tests for trivial defaults, getters, or source text.
- Raw prompts, provider responses, page images, credentials, and unrestricted source text are not persisted by default.

---

## File Structure

- Modify `src/ard_ossie/semantic/structure_candidates.py`: build invariant-proven table candidates from per-atom bbox allocation.
- Modify `src/ard_ossie/semantic/candidates.py`: expose the proof predicate and preserve generated spacing-candidate identity.
- Create `src/ard_ossie/semantic/spacing_repair.py`: own whitespace schemas, deterministic invariants, fallback selection, generation, and verification.
- Modify `src/ard_ossie/semantic/adjudication.py`: invoke table proof fast path, spacing repair, deferred outcomes, and strict trusted-audit validation.
- Modify `src/ard_ossie/semantic/pipeline_v2.py`: omit redundant region spacing sets for proven tables.
- Modify `src/ard_ossie/semantic/canonical.py`: accept generated/fallback decisions, build table block text from cells, and emit `review_pending`.
- Modify `src/ard_ossie/semantic/diagnostics.py`: write `semantic-review.json` and accurate application/call telemetry.
- Modify `src/ard_ossie/pipeline.py`: treat `review_pending` as a quality warning, not a hard conversion failure.
- Modify `src/ard_ossie/release.py`: retain exact `verified` release requirement.
- Modify `scripts/verify_issue_3_semantic.py`: verify exact canonical table cells and text, not confidence/status proxies.
- Modify `tests/fixtures/semantic/issue-3-golden.json`: store exact approved cells for the two previously corrupted tables.
- Modify focused tests under `tests/unit/semantic/`, `tests/integration/`, and `tests/unit/test_processing_service.py`.

---

### Task 1: Make Issue #3 exact table output the acceptance oracle

**Files:**
- Modify: `tests/fixtures/semantic/issue-3-golden.json`
- Modify: `scripts/verify_issue_3_semantic.py:250-326`
- Modify: `tests/integration/test_semantic_pdf_regressions.py:7-57`

**Interfaces:**
- Consumes: `SemanticPipelineResult.canonical.blocks` and the public Issue #3 evidence fixture.
- Produces: golden key `exact_tables: dict[region_id, list[list[cell_text]]]` and `_canonical_table_rows(result, region_id) -> list[list[str]]`.

- [ ] **Step 1: Add hand-checked exact table goldens**

Add the complete 9x4 table for `region_05d599dd01a206bb` and complete 5x3 table for
`region_373d0a6138b7bdcb`. The latter must include these literal rows:

```json
[
  ["개체", "데이터 단위", "결합 기준"],
  ["Campaign", "campaign_id 당 1행", "campaign_id"],
  ["Creative", "creative_id 당 1행", "campaign_id"],
  ["Delivery", "event_date + campaign_id + creative_id 당 1행", "campaign_id, creative_id"],
  ["Outcome", "event_date + campaign_id 당 1행", "campaign_id"]
]
```

- [ ] **Step 2: Write the failing integration assertion**

```python
def _table_rows(result, region_id: str) -> list[list[str]]:
    block = next(item for item in result.canonical.blocks if item.region_id == region_id)
    rows = [["" for _ in range(block.column_count or 0)] for _ in range(block.row_count or 0)]
    for cell in block.cells:
        rows[cell.start_row][cell.start_column] = cell.text
    return rows

for region_id, expected_rows in golden["exact_tables"].items():
    assert _table_rows(result, region_id) == expected_rows
    block = next(item for item in result.canonical.blocks if item.region_id == region_id)
    assert block.text == "\n".join("\t".join(row) for row in expected_rows)
```

Remove assertions that require exactly two recovered spacing decisions. Recovery telemetry is not a correctness oracle.

- [ ] **Step 3: Run the replay test and verify the intended failure**

Run: `uv run pytest tests/integration/test_semantic_pdf_regressions.py::test_issue_3_replay_is_verified_without_korean_corruption -q`

Expected: FAIL on exact cell text or canonical table `block.text`, showing the current whole-line allocation corruption.

- [ ] **Step 4: Apply the same oracle in the standalone verifier**

Add `_canonical_table_rows()` to `scripts/verify_issue_3_semantic.py` and require every exact table to match. Return `exact_table_count` in the summary. Do not test the script by grepping its source; the integration test exercises it through the real replay result.

- [ ] **Step 5: Commit the red acceptance oracle**

```bash
git add tests/fixtures/semantic/issue-3-golden.json scripts/verify_issue_3_semantic.py tests/integration/test_semantic_pdf_regressions.py
git commit -m "test: require exact issue 3 table rendering"
```

---

### Task 2: Build invariant-proven table candidates at atom scope

**Files:**
- Modify: `src/ard_ossie/semantic/structure_candidates.py:191-310,412-650`
- Modify: `src/ard_ossie/semantic/candidates.py:145-235`
- Modify: `tests/unit/semantic/test_structure_candidates.py`

**Interfaces:**
- Consumes: `LayoutRegion`, `EvidenceDocument`, matching `StructureTable`, and cell bboxes.
- Produces: `is_invariant_proven_table(candidate: Candidate) -> TypeGuard[TableCandidate]`, `_invariant_hint_table_candidate(region, table_hint, atom_catalog) -> TableCandidate | None`, `_best_hint_cell(atom, cells) -> int | None`, `_order_atom_ids_for_hint(atom_ids, rendered_text, atom_catalog) -> tuple[AtomId, ...] | None`, and `_make_proven_table_candidate(region, table_hint, assignments, atom_catalog) -> TableCandidate | None`. The final candidate has proof features `atom_bbox_cell_agreement`, `cell_character_multiset`, and `structure_hint_text` all equal to `1.0`.

- [ ] **Step 1: Write a compact failing table-allocation test**

Create a 2x2 table fixture whose PDF layout exposes one line spanning both columns, while each character bbox lies inside its hinted cell. Assert:

```python
candidate_set = build_table_candidate_set(region, evidence, layout, hints)
proven = [item for item in candidate_set.candidates if is_invariant_proven_table(item)]

assert len(proven) == 1
assert [[cell.rendered_text for cell in proven[0].cells[:2]],
        [cell.rendered_text for cell in proven[0].cells[2:]]] == [
    ["항목", "정의"],
    ["creative_id", "광고 소재"],
]
assert set(proven[0].atom_ids) == set(region.atom_ids)
assert len(proven[0].atom_ids) == len(set(proven[0].atom_ids))
```

The production mutation this catches is assigning a complete `LayoutLine` to only one hinted cell.

- [ ] **Step 2: Run the focused test and observe RED**

Run: `uv run pytest tests/unit/semantic/test_structure_candidates.py -k invariant_proven -q`

Expected: FAIL because no proof predicate/candidate exists.

- [ ] **Step 3: Implement deterministic atom-to-cell allocation**

Add helpers with these signatures:

```python
PROVEN_TABLE_FEATURES = (
    "atom_bbox_cell_agreement",
    "cell_character_multiset",
    "structure_hint_text",
)

def _invariant_hint_table_candidate(
    region: LayoutRegion,
    table_hint: StructureTable,
    atom_catalog: dict[str, EvidenceAtom],
) -> TableCandidate | None:
    # Return None unless every atom is uniquely assigned and every cell's
    # assigned character Counter equals its hint character Counter.
    assignments: dict[int, list[AtomId]] = defaultdict(list)
    for atom_id in region.atom_ids:
        cell_index = _best_hint_cell(atom_catalog[atom_id], table_hint.cells)
        if cell_index is None:
            return None
        assignments[cell_index].append(atom_id)
    # Build TableCellCandidate values only after all per-cell checks pass.
    return _make_proven_table_candidate(region, table_hint, assignments, atom_catalog)

def _best_hint_cell(
    atom: EvidenceAtom,
    cells: tuple[StructureCell, ...],
) -> int | None:
    eligible = [
        (index, _box_overlap_area(atom.bbox, cell.bbox))
        for index, cell in enumerate(cells)
        if cell.bbox is not None and _box_overlap_area(atom.bbox, cell.bbox) > 0
    ]
    return max(eligible, key=lambda item: (item[1], -item[0]))[0] if eligible else None

def _order_atom_ids_for_hint(
    atom_ids: tuple[AtomId, ...],
    rendered_text: str,
    atom_catalog: dict[str, EvidenceAtom],
) -> tuple[AtomId, ...] | None:
    by_character: dict[str, deque[AtomId]] = defaultdict(deque)
    for atom_id in atom_ids:
        if not atom_catalog[atom_id].text.isspace():
            by_character[atom_catalog[atom_id].text].append(atom_id)
    ordered: list[AtomId] = []
    for character in rendered_text:
        if character.isspace():
            continue
        if not by_character[character]:
            return None
        ordered.append(by_character[character].popleft())
    if any(queue for queue in by_character.values()):
        return None
    return tuple(ordered)
```

Use maximum positive bbox overlap with stable cell-index tie-breaking. Compare `Counter` values for each cell before reordering IDs through per-code-point `deque`s. Allocate every whitespace atom exactly once to the overlapping cell, or to the cell of the nearest adjacent non-whitespace atom in region order. Return `None` on any missing/duplicate/unallocated atom or character mismatch.

- [ ] **Step 4: Add the candidate without deleting existing alternatives**

In `build_table_candidate_set`, append the proven candidate before sorting/truncating. Give it score `1.0` and the exact proof features; existing geometry and language candidates remain diagnostic alternatives.

In `candidates.py` add:

```python
def is_invariant_proven_table(candidate: Candidate) -> TypeGuard[TableCandidate]:
    return isinstance(candidate, TableCandidate) and all(
        candidate.features.get(name) == 1.0 for name in PROVEN_TABLE_FEATURES
    )
```

- [ ] **Step 5: Verify GREEN and run the Issue #3 proof count**

Run:

```bash
uv run pytest tests/unit/semantic/test_structure_candidates.py -k 'table or invariant_proven' -q
uv run python scripts/verify_issue_3_semantic.py --evidence tests/fixtures/semantic/issue-3-evidence.json --golden tests/fixtures/semantic/issue-3-golden.json
```

The standalone verifier may still fail on canonical text until Task 3, but a temporary diagnostic assertion must show 10 proven table candidates.

- [ ] **Step 6: Commit**

```bash
git add src/ard_ossie/semantic/candidates.py src/ard_ossie/semantic/structure_candidates.py tests/unit/semantic/test_structure_candidates.py
git commit -m "feat: conserve table hints at atom scope"
```

---

### Task 3: Make proven tables authoritative and remove redundant spacing decisions

**Files:**
- Modify: `src/ard_ossie/semantic/adjudication.py:138-220`
- Modify: `src/ard_ossie/semantic/pipeline_v2.py:130-190`
- Modify: `src/ard_ossie/semantic/canonical.py:153-335,520-601`
- Modify: `tests/unit/semantic/test_adjudication.py`
- Modify: `tests/unit/semantic/test_canonical.py`
- Modify: `tests/integration/test_semantic_pdf_regressions.py`

**Interfaces:**
- Consumes: `is_invariant_proven_table()`.
- Produces: `_table_plain_text(cells, row_count, column_count) -> str` and zero region-spacing decisions for proven table regions.

- [ ] **Step 1: Write failing tests for proof selection and canonical text**

```python
decision = CandidateAdjudicator(FailIfCalledProvider()).decide(candidate_set)
assert decision.source == "deterministic"
assert decision.selected_candidate_id == proven.candidate_id
```

And for canonical output:

```python
assert canonical.blocks[0].text == "개체\t데이터 단위\nCampaign\tcampaign_id 당 1행"
```

The tests fail if score-margin voting is used or if table `block.text` comes from flattened region spacing.

- [ ] **Step 2: Run RED tests**

Run: `uv run pytest tests/unit/semantic/test_adjudication.py tests/unit/semantic/test_canonical.py -k 'proven_table or table_plain_text' -q`

- [ ] **Step 3: Add the proof fast path**

Immediately after sorting candidates in `CandidateAdjudicator.decide`, select the unique invariant-proven table before the ordinary margin rule. Raise `ValueError("TABLE_PROOF_AMBIGUOUS")` if more than one different proven candidate appears.

- [ ] **Step 4: Reorder candidate construction in the pipeline**

Build block/table candidate sets before finalizing `spacing_sets`, compute:

```python
proven_table_regions = {
    candidate_set.region_id
    for candidate_set in table_sets
    if any(is_invariant_proven_table(item) for item in candidate_set.candidates)
}
spacing_sets = tuple(
    item for item in all_spacing_sets if item.region_id not in proven_table_regions
)
```

Keep recognition ordering unchanged. Include table sets in diagnostics and decisions as before.

- [ ] **Step 5: Derive canonical table block text from canonical cells**

Implement `_table_plain_text()` with a bounded grid, writing each non-spanning cell at its start coordinate and joining columns with tabs and rows with newlines. Set `CanonicalBlock.text` to this value when `kind == "table"`; paragraph/heading behavior is unchanged.

- [ ] **Step 6: Run the exact Issue #3 replay**

Run:

```bash
uv run pytest tests/integration/test_semantic_pdf_regressions.py::test_issue_3_replay_is_verified_without_korean_corruption -q
uv run python scripts/verify_issue_3_semantic.py --evidence tests/fixtures/semantic/issue-3-evidence.json --golden tests/fixtures/semantic/issue-3-golden.json
```

Expected: exact table assertions pass, status is `verified`, all 10 tables use proven candidates, and model calls decrease because 10 flattened spacing decisions disappear.

- [ ] **Step 7: Commit**

```bash
git add src/ard_ossie/semantic/adjudication.py src/ard_ossie/semantic/pipeline_v2.py src/ard_ossie/semantic/canonical.py tests/unit/semantic/test_adjudication.py tests/unit/semantic/test_canonical.py tests/integration/test_semantic_pdf_regressions.py scripts/verify_issue_3_semantic.py
git commit -m "fix: render proven tables without flattened spacing"
```

---

### Task 4: Define whitespace repair invariants and generated candidates

**Files:**
- Create: `src/ard_ossie/semantic/spacing_repair.py`
- Modify: `src/ard_ossie/semantic/adjudication.py:46-115`
- Create: `tests/unit/semantic/test_spacing_repair.py`

**Interfaces:**
- Produces: `spacing_repair_schema()`, `spacing_verification_schema(candidate_ids)`, `spacing_defect_codes(candidate)`, `build_generated_candidate(anchor, rendered_text, confidence)`, `fallback_spacing_candidate(candidate_set)`, `_generation_request(candidate_set, candidates, anchor) -> dict[str, object]`, `_verification_request(candidate_set, candidates, generated) -> dict[str, object]`, and stable `GENERATION_SYSTEM_CONTRACT` / `VERIFICATION_SYSTEM_CONTRACT` strings.
- Consumes later: `CandidateAdjudicator` in Task 5.

- [ ] **Step 1: Write failing behavior tests**

Use literal expected values:

```python
def test_identifier_gap_is_a_defect_even_when_characters_are_conserved():
    candidate = spacing_candidate("marketing _campaign")
    assert spacing_defect_codes(candidate) == ("IDENTIFIER_WHITESPACE_SPLIT",)

def test_generated_candidate_rejects_character_mutation():
    with pytest.raises(ValueError, match="SPACING_REPAIR_CHARACTER_MISMATCH"):
        build_generated_candidate(anchor, "마케팅 캠페인!", confidence=0.91)

def test_fallback_prefers_valid_source_spacing():
    assert fallback_spacing_candidate(candidate_set).features["source_spacing"] == 0.45
```

Each test catches a wrong branch that would alter the generated document.

- [ ] **Step 2: Run RED**

Run: `uv run pytest tests/unit/semantic/test_spacing_repair.py -q`

- [ ] **Step 3: Implement the focused module**

Define immutable response models:

```python
class SpacingRepairProposal(ImmutableStrictModel):
    rendered_text: str
    confidence: float = Field(ge=0, le=1)
    repair_reasons: tuple[
        Literal[
            "korean_morphology",
            "identifier_integrity",
            "punctuation_boundary",
            "table_cell_boundary",
            "line_boundary",
        ],
        ...,
    ] = Field(max_length=5)

class SpacingVerification(ImmutableStrictModel):
    candidate_id: CandidateId
    confidence: float = Field(ge=0, le=1)
    validation_codes: tuple[ValidationCode, ...] = Field(max_length=4)
```

`build_generated_candidate` calls existing `make_spacing_candidate` with the anchor's region and atom IDs, score equal to bounded model confidence, and features `{"llm_generated_spacing": confidence}`. Enforce exact whitespace-stripped sequence, no control separators, unchanged hard line boundaries, and no whitespace adjacent to `_` before construction.

- [ ] **Step 4: Implement compact prompt builders**

Expose:

```python
def spacing_generation_messages(
    candidate_set: CandidateSet,
    candidates: tuple[SpacingCandidate, ...],
    anchor: SpacingCandidate,
) -> list[dict[str, str]]:
    request = _generation_request(candidate_set, candidates, anchor)
    return [
        {"role": "system", "content": GENERATION_SYSTEM_CONTRACT},
        {"role": "user", "content": _json(request)},
    ]

def spacing_verification_messages(
    candidate_set: CandidateSet,
    candidates: tuple[SpacingCandidate, ...],
    generated: SpacingCandidate,
) -> list[dict[str, str]]:
    request = _verification_request(candidate_set, candidates, generated)
    return [
        {"role": "system", "content": VERIFICATION_SYSTEM_CONTRACT},
        {"role": "user", "content": _json(request)},
    ]
```

Prompts include candidate boundary deltas, protected identifiers, Korean morphology/punctuation rules, and the exact non-whitespace sequence. Do not persist prompt content.

- [ ] **Step 5: Verify GREEN and commit**

```bash
uv run pytest tests/unit/semantic/test_spacing_repair.py -q
git add src/ard_ossie/semantic/spacing_repair.py src/ard_ossie/semantic/adjudication.py tests/unit/semantic/test_spacing_repair.py
git commit -m "feat: define bounded whitespace repair contract"
```

---

### Task 5: Replace low-confidence re-voting with generation and verification

**Files:**
- Modify: `src/ard_ossie/semantic/adjudication.py:46-976`
- Modify: `tests/unit/semantic/test_adjudication.py`
- Modify: `scripts/verify_issue_3_semantic.py:66-145`

**Interfaces:**
- Consumes: Task 4 repair schemas/builders.
- Produces: `DecisionRecord.generated_candidate`, attempt phases `generation` and `verification`, source `generated`, and outcome `deferred_review`.

- [ ] **Step 1: Write the high-confidence-defect RED test**

Configure a response queue:

```python
provider = RecordingProvider([
    {"candidate_id": damaged.candidate_id, "confidence": 0.91},
    {"rendered_text": "marketing_campaign 캠페인", "confidence": 0.92,
     "repair_reasons": ["identifier_integrity", "korean_morphology"]},
    {"candidate_id": generated_id, "confidence": 0.90, "validation_codes": []},
])
decision = CandidateAdjudicator(provider).decide(candidate_set)
assert decision.outcome == "selected"
assert decision.source == "generated"
assert decision.generated_candidate.rendered_text == "marketing_campaign 캠페인"
assert [item.phase for item in decision.attempts] == ["primary", "generation", "verification"]
```

The production mutation this catches is accepting `0.91` without checking identifier integrity.

- [ ] **Step 2: Write low-confidence/fallback RED tests**

One test supplies a valid proposal with generator/verifier confidence below `0.80`; another changes a punctuation character. Both must select the source-spacing candidate with `outcome="deferred_review"`, code `LLM_SPACING_REPAIR_DEFERRED`, and exactly three semantic calls for the low-confidence case or two calls when deterministic validation rejects before verification.

- [ ] **Step 3: Run and observe RED**

Run: `uv run pytest tests/unit/semantic/test_adjudication.py -k 'generated_spacing or deferred_spacing or high_confidence_identifier' -q`

- [ ] **Step 4: Implement the bounded state machine**

Replace the recovery/tiebreak branch only for spacing decisions:

```python
needs_repair = (
    primary.choice.confidence < policy.minimum_model_confidence
    or spacing_defect_codes(selected_primary)
)
```

Generate once, validate deterministically, then verify against original plus generated IDs once. For accepted generation, store the full generated candidate in the decision. For rejection, select `fallback_spacing_candidate()` and emit `deferred_review`. Non-spacing uncertainty uses its safest valid deterministic candidate and the same deferred outcome without text generation.

- [ ] **Step 5: Update replay provider parsing**

Make `ReplayCandidateProvider` respond to explicit `generation` and `verification` task payloads without hard-coding production text. Issue #3 proven table regions should not invoke these phases; a compact non-table fixture exercises them.

- [ ] **Step 6: Run focused adjudication and Issue #3 tests**

```bash
uv run pytest tests/unit/semantic/test_adjudication.py tests/unit/semantic/test_spacing_repair.py -q
uv run pytest tests/integration/test_semantic_pdf_regressions.py -q
```

- [ ] **Step 7: Commit**

```bash
git add src/ard_ossie/semantic/adjudication.py scripts/verify_issue_3_semantic.py tests/unit/semantic/test_adjudication.py
git commit -m "feat: generate verified spacing repairs"
```

---

### Task 6: Continue canonical conversion with deferred review debt

**Files:**
- Modify: `src/ard_ossie/semantic/canonical.py:40-153,336-480,520-538`
- Modify: `src/ard_ossie/semantic/pipeline_v2.py:62-216`
- Modify: `src/ard_ossie/semantic/diagnostics.py:27-298`
- Modify: `src/ard_ossie/pipeline.py:351-388,1080-1205`
- Modify: `src/ard_ossie/release.py:389-405`
- Modify: `tests/unit/semantic/test_canonical.py`
- Modify: `tests/unit/semantic/test_diagnostics.py`
- Modify: `tests/integration/test_semantic_pdf_v2.py`
- Modify: `tests/unit/test_release.py`

**Interfaces:**
- Produces: `SemanticPipelineStatus.REVIEW_PENDING`, `publication_status="review_pending"`, and `semantic-review.json`.
- Release continues to consume only `SemanticPipelineStatus.VERIFIED`.

- [ ] **Step 1: Write an end-to-end deferred conversion test**

Use a real candidate pipeline fixture whose provider returns low-confidence generation/verification. Assert observable output:

```python
assert result.validation.status == "review_pending"
assert result.validation.publishable is True
assert "marketing_campaign" in result.markdown
assert result.decisions.decisions[0].outcome == "deferred_review"
```

Pair it with a character-loss fixture that remains `failed` and `publishable=False`.

- [ ] **Step 2: Run RED**

Run: `uv run pytest tests/integration/test_semantic_pdf_v2.py -k 'review_pending or character_loss' -q`

- [ ] **Step 3: Implement canonical status semantics**

`_selected_candidate` first accepts a matching `generated_candidate`, otherwise the selected candidate ID. `validate_canonical` returns:

```python
if hard_findings:
    status, publishable = SemanticPipelineStatus.FAILED, False
elif unresolved_without_candidate:
    status, publishable = SemanticPipelineStatus.REVIEW_REQUIRED, False
elif deferred_decisions:
    status, publishable = SemanticPipelineStatus.REVIEW_PENDING, True
else:
    status, publishable = SemanticPipelineStatus.VERIFIED, True
```

- [ ] **Step 4: Add durable review diagnostics**

Add `semantic-review.json` to diagnostic report names only when deferred decisions exist, with version `semantic-review-v1`, source/candidate/decision IDs, request hashes, bounded codes, confidence values, fallback ID, compact boundary diff, and replay identity. Keep previews masked and never serialize raw provider responses or images.

Application outcomes become `applied_existing_candidate`, `applied_generated_repair`, and `applied_fallback_pending_review`. Counts are derived from complete attempts.

- [ ] **Step 5: Treat review pending as a warning in product quality**

In `_semantic_hard_findings`, return no hard finding for `REVIEW_PENDING`. In `_semantic_findings`, emit one warning per deferred decision code. Existing quality report then becomes `WARN`, processing continues, and the PR remains Draft.

- [ ] **Step 6: Prove release remains blocked**

Keep `_verify_candidate_validation_snapshot` unchanged except adding a regression assertion:

```python
with pytest.raises(ReleaseBlocked, match="SEMANTIC_VALIDATION_NOT_VERIFIED: review_pending"):
    build_release_bundle(product_root, bundle_path)
```

- [ ] **Step 7: Run focused tests and commit**

```bash
uv run pytest tests/unit/semantic/test_canonical.py tests/unit/semantic/test_diagnostics.py tests/integration/test_semantic_pdf_v2.py tests/unit/test_release.py -q
git add src/ard_ossie/semantic/canonical.py src/ard_ossie/semantic/pipeline_v2.py src/ard_ossie/semantic/diagnostics.py src/ard_ossie/pipeline.py src/ard_ossie/release.py tests/unit/semantic/test_canonical.py tests/unit/semantic/test_diagnostics.py tests/integration/test_semantic_pdf_v2.py tests/unit/test_release.py
git commit -m "feat: continue draft conversion with review debt"
```

---

### Task 7: Harden trusted decisions and make call telemetry exact

**Files:**
- Modify: `src/ard_ossie/semantic/adjudication.py:714-975`
- Modify: `src/ard_ossie/llm/service.py:90-160`
- Modify: `src/ard_ossie/semantic/canonical.py:480-490`
- Modify: `tests/unit/semantic/test_adjudication.py`
- Modify: `tests/unit/test_llm_service.py`

**Interfaces:**
- Produces: `_trusted_audit_matches_current_policy(decision, candidate_set, allowlist, policy, request_hash) -> bool`, complete decision identity, and aggregate retry/repair counts.

- [ ] **Step 1: Write the contradictory-cache RED test**

Mutate a valid `same_candidate` cached record so primary votes A and recovery votes B while summary claims A. Assert the provider is called fresh and the result source is not `cache`.

- [ ] **Step 2: Write policy-identity RED tests**

Reuse a valid recovery under `minimum_model_confidence=0.95` and under `max_confidence_recovery_attempts=0`. Both must miss cache. Changing any persisted attempt confidence/status/candidate/code must change `decision_id`.

- [ ] **Step 3: Write terminal output-repair telemetry RED test**

Feed three invalid structured outputs. Assert actual provider calls `3`, the terminal attempt `provider_repair_count == 2`, and canonical model call count `3`.

- [ ] **Step 4: Run RED**

Run: `uv run pytest tests/unit/semantic/test_adjudication.py tests/unit/test_llm_service.py -k 'trusted or policy or terminal_output or decision_identity' -q`

- [ ] **Step 5: Implement derived trust validation**

Include every outcome-affecting policy field in `_request_hash`. Recompute phase order, candidate allowlisting, threshold/status consistency, actual same-candidate or majority consensus, attempt request hashes, and terminal summary from attempts. Cache only if the derivation exactly matches.

Hash `attempt.model_dump(mode="json")` for every attempt into `_decision_id`, not only request hashes.

- [ ] **Step 6: Preserve and aggregate provider counters**

When `LLMService` exhausts output repair, attach consumed retry/repair counts to `ProviderExecutionError`. Copy them to the terminal attempt. Derive top-level decision totals by summing every phase instead of copying the last phase.

- [ ] **Step 7: Run focused tests and commit**

```bash
uv run pytest tests/unit/semantic/test_adjudication.py tests/unit/test_llm_service.py tests/unit/semantic/test_canonical.py -q
git add src/ard_ossie/semantic/adjudication.py src/ard_ossie/llm/service.py src/ard_ossie/semantic/canonical.py tests/unit/semantic/test_adjudication.py tests/unit/test_llm_service.py
git commit -m "fix: validate trusted semantic decision audits"
```

---

### Task 8: Verify local model behavior, workflow continuation, and merge readiness

**Files:**
- Modify if required by a failing contract: `tests/unit/test_processing_service.py`
- Modify if required by a failing contract: `tests/unit/test_finalize_service.py`
- Modify: `docs/superpowers/specs/2026-08-15-llm-whitespace-repair-deferred-review-design.md` only if implementation evidence changes a stated contract.

**Interfaces:**
- Consumes: all previous tasks.
- Produces: verified local Issue #3 output, full-suite evidence, and a merge-ready branch.

- [ ] **Step 1: Run focused quality checks**

```bash
uv run ruff check src/ard_ossie/semantic src/ard_ossie/llm tests/unit/semantic scripts/verify_issue_3_semantic.py
uv run pytest tests/unit/semantic tests/integration/test_semantic_pdf_regressions.py tests/integration/test_semantic_pdf_v2.py -q
```

- [ ] **Step 2: Run the real-model Issue #3 locally**

Load `.env.local` without printing it and run the candidate pipeline against
`/tmp/ard-semantic-plan.qJRelW/issue-3.pdf`. Assert:

- exact two target tables equal the golden rows;
- all identifier gap counts in canonical cells are zero;
- validation is `verified`, or `review_pending` only for a genuinely unrelated unresolved region;
- Markdown is generated in both states;
- no flattened proven-table spacing call occurs;
- the second run with trusted decisions makes zero avoidable model calls.

- [ ] **Step 3: Run workflow service contracts**

Add or adjust one processing/finalizer integration case so a `review_pending` product result writes artifacts, sets a Draft PR review summary, removes `ard:failed`, and does not claim release readiness. Do not assert GitHub mock call count unless the count is the public idempotency contract; assert resulting statuses, labels, PR state, and files.

- [ ] **Step 4: Run repository-wide verification**

```bash
uv run ruff check .
uv run pytest -q
git diff --check
git status --short
```

Expected: all tests pass, no secret appears in tracked/untracked repository files except ignored `.env.local`, and only intentional branch changes remain.

- [ ] **Step 5: Run pre-landing review and fix only reproducible findings**

Use `superpowers:requesting-code-review`, then the repository `review` skill. Reproduce every actionable finding before changing code. Repeat focused and full verification after fixes.

- [ ] **Step 6: Commit final integration adjustments**

```bash
git add src/ard_ossie tests scripts/verify_issue_3_semantic.py tests/fixtures/semantic/issue-3-golden.json docs/superpowers/specs/2026-08-15-llm-whitespace-repair-deferred-review-design.md docs/superpowers/plans/2026-08-15-llm-whitespace-repair-deferred-review.md
git commit -m "test: verify issue 3 semantic conversion"
```

- [ ] **Step 7: Hand off for merge and live Issue #3 rerun**

Use `superpowers:finishing-a-development-branch`. After merge, reapply `ard:approved` once, monitor the workflow, confirm PR #5 head advances, inspect the exact generated table cells, verify Issue #3 does not receive `ard:failed` for handled uncertainty, and leave the product PR Draft whenever `semantic-review.json` contains entries.
