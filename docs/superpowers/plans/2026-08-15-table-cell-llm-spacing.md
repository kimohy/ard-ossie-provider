# Table-Cell LLM Spacing Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair low-confidence whitespace inside proven table cells without allowing an LLM to alter characters, table structure, cell boundaries, or clean cells.

**Architecture:** Select table structure before spacing, using character ownership rather than whitespace quality as the proof. Build one row-major composite `SpacingCandidate` per affected table, hard-separate cells, allow mutations only inside suspicious cells, and reuse the existing generation/independent-verification/deferred-review pipeline. Canonical assembly projects selected boundaries back into cells.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, Kiwi 0.23.2, existing structured-output LLM providers

## Global Constraints

- Non-whitespace Unicode code points, order, case, punctuation, and atom ownership never change.
- Table dimensions, spans, coordinates, cell IDs, and block order never change.
- Cell boundaries are immutable hard breaks in the composite candidate.
- Clean-cell whitespace is immutable and validated after generation.
- Only suspicious-cell internal boundary indexes are mutable.
- Provider failure or confidence below `0.80` continues conversion through a deterministic fallback and records `deferred_review`.
- Raw Markdown/CommonMark escaping remains unchanged.
- Diagnostics do not persist unrestricted source text or provider output.
- Tests assert conversion results and invariants, not source-code strings or mock call existence.

---

### Task 1: Add Explicit Mutable-Boundary Scope

**Files:**
- Modify: `src/ard_ossie/semantic/candidates.py`
- Modify: `src/ard_ossie/semantic/spacing_repair.py`
- Modify: `src/ard_ossie/semantic/adjudication.py`
- Modify: `tests/unit/semantic/test_spacing_repair.py`
- Modify: `tests/unit/semantic/test_adjudication.py`

**Interfaces:**
- Extend `SpacingCandidate` and `GeneratedSpacingSnapshot` with `mutable_boundary_indexes: tuple[int, ...] | None = None`.
- Extend `make_spacing_candidate(..., mutable_boundary_indexes: tuple[int, ...] | None = None)`.
- `None` keeps existing non-table behavior; an explicit tuple is a strict mutation allowlist.

- [ ] **Step 1: Write failing boundary-scope tests**

Add a test helper that creates `AB\n가 나`, where boundary 1 is the hard cell separator and only boundary 2 is mutable:

```python
anchor = make_spacing_candidate(
    region_id=REGION_ID,
    rendered_text="AB\n가 나",
    character_sequence="AB가나",
    atom_ids=tuple(f"atom_{index:016x}" for index in range(4)),
    source_whitespace=((), (), ()),
    score=0.70,
    features={"table_cell_composite": 1.0},
    mutable_boundary_indexes=(2,),
)
```

Assert these observable contracts:

```python
assert build_generated_candidate(anchor, "AB\n가나", 0.92).rendered_text == "AB\n가나"
with pytest.raises(ValueError, match="SPACING_REPAIR_IMMUTABLE_BOUNDARY_MISMATCH"):
    build_generated_candidate(anchor, "A B\n가나", 0.92)
with pytest.raises(ValueError, match="SPACING_REPAIR_HARD_LINE_BOUNDARY_MISMATCH"):
    build_generated_candidate(anchor, "AB 가나", 0.92)
```

Also assert Pydantic rejects duplicate, out-of-range, negative, or hard-break indexes, and that diagnostic snapshot materialization/cache reuse preserves the exact allowlist.

- [ ] **Step 2: Run RED tests**

Run:

```bash
.venv/bin/pytest -q tests/unit/semantic/test_spacing_repair.py tests/unit/semantic/test_adjudication.py -k 'mutable_boundary or immutable_boundary or generated_snapshot'
```

Expected: construction fails because the model/factory do not accept `mutable_boundary_indexes`.

- [ ] **Step 3: Implement model and identity propagation**

In `SpacingCandidate.validate_spacing`, validate explicit indexes:

```python
if self.mutable_boundary_indexes is not None:
    indexes = self.mutable_boundary_indexes
    if tuple(sorted(set(indexes))) != indexes:
        raise ValueError("CANDIDATE_MUTABLE_BOUNDARIES_INVALID")
    if any(index < 0 or index >= len(self.boundaries) for index in indexes):
        raise ValueError("CANDIDATE_MUTABLE_BOUNDARIES_INVALID")
    if any(self.boundaries[index].state == "hard_break" for index in indexes):
        raise ValueError("CANDIDATE_MUTABLE_HARD_BOUNDARY")
```

Include the allowlist in candidate ID payload, snapshot construction, snapshot materialization, and generated-candidate identity payload. Generated candidates copy the anchor allowlist.

- [ ] **Step 4: Enforce immutable boundaries during generation**

After constructing the generated candidate, compare every non-mutable boundary with the anchor:

```python
mutable = (
    set(range(len(anchor.boundaries)))
    if anchor.mutable_boundary_indexes is None
    else set(anchor.mutable_boundary_indexes)
)
if any(
    generated.boundaries[index].state != boundary.state
    for index, boundary in enumerate(anchor.boundaries)
    if index not in mutable
):
    raise ValueError("SPACING_REPAIR_IMMUTABLE_BOUNDARY_MISMATCH")
```

Require every candidate in one spacing set to carry the same explicit scope in `_validate_scope`. Add `mutable_boundary_indexes` to generation and verification request JSON.

- [ ] **Step 5: Run focused tests and commit**

```bash
.venv/bin/pytest -q tests/unit/semantic/test_spacing_repair.py tests/unit/semantic/test_adjudication.py
.venv/bin/ruff check src/ard_ossie/semantic/candidates.py src/ard_ossie/semantic/spacing_repair.py src/ard_ossie/semantic/adjudication.py tests/unit/semantic/test_spacing_repair.py tests/unit/semantic/test_adjudication.py
git add src/ard_ossie/semantic/candidates.py src/ard_ossie/semantic/spacing_repair.py src/ard_ossie/semantic/adjudication.py tests/unit/semantic/test_spacing_repair.py tests/unit/semantic/test_adjudication.py
git commit -m "feat: bound generated spacing mutations"
```

### Task 2: Build Table-Cell Composite Spacing Candidates

**Files:**
- Modify: `src/ard_ossie/semantic/spacing.py`
- Create: `tests/unit/semantic/test_table_spacing.py`

**Interfaces:**
- Produce `build_table_spacing_candidate_set(*, table: TableCandidate, evidence: EvidenceDocument, scorer: KoreanSpacingScorer) -> CandidateSet | None`.
- Reuse `make_spacing_candidate`; no new decision type is introduced.

- [ ] **Step 1: Write a failing real-candidate construction test**

Construct a 1x3 `TableCandidate` with exact atom ownership and cell renderings:

```python
("정상 셀", "시 뮬레 이 션 비용", "marketing_campaign.ca mpaign_id")
```

Use a deterministic scorer that returns `시뮬레이션 비용` for the broken Korean cell and leaves the clean cell unchanged. Assert:

```python
candidate_set = build_table_spacing_candidate_set(...)
assert candidate_set is not None
source = next(item for item in candidate_set.candidates if "source_spacing" in item.features)
repaired = next(item for item in candidate_set.candidates if "table_cell_repair" in item.features)
assert repaired.rendered_text == (
    "정상 셀\n시뮬레이션 비용\nmarketing_campaign.campaign_id"
)
assert [boundary.state for boundary in repaired.boundaries].count("hard_break") == 2
assert all(
    index not in repaired.mutable_boundary_indexes
    for index in clean_cell_internal_boundary_indexes
)
assert set(source.atom_ids) == set(table.atom_ids)
```

Add separate tests proving clean tables return `None`, character-mutating scorer proposals are ignored, and formula cells such as `COUNT(DISTINCT campaign_id) WHERE campaign_status = Active` are not densified.

- [ ] **Step 2: Run RED tests**

```bash
.venv/bin/pytest -q tests/unit/semantic/test_table_spacing.py
```

Expected: import failure because `build_table_spacing_candidate_set` does not exist.

- [ ] **Step 3: Implement stable composite construction**

In `spacing.py`:

- sort cells by `(start_row, start_column, end_row, end_column, cell_id)`;
- derive each cell's current text from `rendered_text`, falling back to its ordered source atoms;
- keep exact non-whitespace atom IDs in that same cell order;
- detect qualified-identifier-only cells whose whitespace-free text matches
  `^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$` and create the dense deterministic repair;
- accept scorer proposals only when character-conserving and free of `spacing_defect_codes`;
- mark a cell suspicious only with a fragmentation signal plus proposal disagreement, a
  deterministic spacing defect, or unresolved table spacing plus a valid disagreement;
- join cell renderings with `\n`, which becomes a hard boundary between the last atom of one cell
  and the first atom of the next;
- make every internal boundary of suspicious cells mutable and every clean-cell boundary immutable;
- emit a source candidate at score `0.45` and a repaired candidate below `0.82` when Korean judgment
  remains necessary; use score `0.95` only when every change is a deterministic qualified-identifier
  repair;
- return `None` if there is no mutable boundary or no character-conserving alternative.

Keep proposal count at two for predictable adjudication and bounded prompts.

- [ ] **Step 4: Run focused tests and commit**

```bash
.venv/bin/pytest -q tests/unit/semantic/test_table_spacing.py tests/unit/semantic/test_spacing.py
.venv/bin/ruff check src/ard_ossie/semantic/spacing.py tests/unit/semantic/test_table_spacing.py
git add src/ard_ossie/semantic/spacing.py tests/unit/semantic/test_table_spacing.py
git commit -m "feat: build scoped table cell spacing candidates"
```

### Task 3: Sequence Table Structure and Project Cell Spacing

**Files:**
- Modify: `src/ard_ossie/semantic/candidates.py`
- Modify: `src/ard_ossie/semantic/adjudication.py`
- Modify: `src/ard_ossie/semantic/pipeline_v2.py`
- Modify: `src/ard_ossie/semantic/canonical.py`
- Modify: `tests/unit/semantic/test_adjudication.py`
- Modify: `tests/unit/semantic/test_canonical.py`
- Modify: `tests/integration/test_semantic_pdf_regressions.py`

**Interfaces:**
- `is_invariant_proven_table` proves structure with `atom_bbox_cell_agreement`, `cell_character_multiset`, and `structure_hint_text`; `cell_spacing_integrity` routes repair only.
- Pipeline chooses table decisions before calling `build_table_spacing_candidate_set`.
- `_canonical_cells` gives selected spacing boundaries precedence over stale `cell.rendered_text`.

- [ ] **Step 1: Write failing structure and projection tests**

Add an adjudication test with proof features equal to `1.0` and `cell_spacing_integrity=0.0`; a provider that raises if called must still yield:

```python
assert decision.source == "deterministic"
assert decision.outcome == "selected"
```

Add a canonical test in which only the second cell boundary changes:

```python
assert canonical.blocks[0].cells[0].text == "정상 셀"
assert canonical.blocks[0].cells[1].text == "시뮬레이션"
assert canonical.blocks[0].row_count == 1
assert canonical.blocks[0].column_count == 2
assert canonical.blocks[0].atom_ids == original_atom_ids
```

Add an Issue #3 integration assertion that all table decisions are deterministic before testing
the final repaired strings.

- [ ] **Step 2: Run RED tests**

```bash
.venv/bin/pytest -q tests/unit/semantic/test_adjudication.py -k 'table and spacing_integrity'
.venv/bin/pytest -q tests/unit/semantic/test_canonical.py -k 'table and spacing'
```

Expected: the structure proof does not fast-path and canonical cells retain stale text.

- [ ] **Step 3: Decouple structure proof from spacing quality**

Remove `cell_spacing_integrity` from `INVARIANT_PROVEN_TABLE_FEATURES`. In the adjudicator, group
proven candidates by a structural signature of row/column dimensions and ordered cell coordinate/
atom allocations. Fast-path only when one distinct signature exists; choose the highest-scored
candidate within that signature. Multiple distinct signatures continue through ordinary bounded
adjudication instead of raising.

- [ ] **Step 4: Adjudicate tables before spacing**

In `parse_semantic_pdf_v2`:

1. build table sets after recognition/layout as now;
2. adjudicate each table set exactly once;
3. resolve each selected `TableCandidate` from its decision;
4. build table composite spacing sets only for resolved tables that need them;
5. build ordinary spacing sets only for non-table regions;
6. adjudicate composite/ordinary spacing, block, reading-order, and continuation sets;
7. assemble all candidate sets and decisions without duplicate table decisions.

Use the already computed `evidence_hash` for every phase so trusted decisions remain replayable.

- [ ] **Step 5: Project selected spacing into cells**

Change `_canonical_cells` to call `_project_cell_spacing` whenever a table spacing candidate exists;
otherwise retain the cell's existing `rendered_text` and current source fallback. Composite newlines
never become cell text because only intra-cell atom pairs are projected.

- [ ] **Step 6: Run focused tests and commit**

```bash
.venv/bin/pytest -q tests/unit/semantic/test_adjudication.py tests/unit/semantic/test_canonical.py tests/integration/test_semantic_pdf_regressions.py
.venv/bin/ruff check src/ard_ossie/semantic/candidates.py src/ard_ossie/semantic/adjudication.py src/ard_ossie/semantic/pipeline_v2.py src/ard_ossie/semantic/canonical.py
git add src/ard_ossie/semantic/candidates.py src/ard_ossie/semantic/adjudication.py src/ard_ossie/semantic/pipeline_v2.py src/ard_ossie/semantic/canonical.py tests/unit/semantic/test_adjudication.py tests/unit/semantic/test_canonical.py tests/integration/test_semantic_pdf_regressions.py
git commit -m "fix: repair spacing after table structure selection"
```

### Task 4: Prove Issue #3 Conversion and Local LLM Behavior

**Files:**
- Modify: `scripts/verify_issue_3_semantic.py`
- Modify: `tests/fixtures/semantic/issue-3-golden.json`
- Modify: `tests/integration/test_semantic_pdf_regressions.py`
- Modify: `tests/unit/semantic/test_diagnostics.py` only if an audit regression is uncovered

**Interfaces:**
- Golden contract gains explicit repaired table-cell strings and forbidden broken fragments.
- `ReplayCandidateProvider` identifies table-composite spacing candidates by their feature payload,
  selects the best offered candidate at low primary confidence, then returns/independently verifies
  its character-conserving rendering.

- [ ] **Step 1: Write failing Issue #3 visible-output assertions**

Add literal golden requirements for these cells:

```json
"required_repaired_table_cells": [
  "하나의 캠페인에는 여러 소재가 연결될 수 있으며 소재는 하나의 캠페인을 참조한다.",
  "성과 신호는 event_date, campaign_id 수준으로 집계한다.",
  "테이블 결합",
  "시뮬레이션 비용 합계",
  "marketing_campaign.campaign_id",
  "marketing_creative.creative_id",
  "marketing_delivery.impression_count",
  "marketing_delivery.engagement_count",
  "marketing_delivery.spend_units"
]
```

Add the known malformed fragments to `forbidden_strings`. Assert required values against the list
of actual canonical cell texts, not flattened Markdown substrings. Preserve the two existing exact
table assertions unchanged.

- [ ] **Step 2: Run RED Issue #3 replay**

```bash
.venv/bin/pytest -q tests/integration/test_semantic_pdf_regressions.py::test_issue_3_replay_is_verified_without_korean_corruption
```

Expected: current canonical cells do not match the repaired literals.

- [ ] **Step 3: Make replay generation exercise the real repair contract**

For a primary request containing `table_cell_composite`, select the highest-scored valid candidate
with confidence `0.70`. In generation, return that anchor rendering with confidence `0.99`; in
verification, select only the generated candidate at `0.99`. Existing deterministic validation,
not the fake provider, proves character and cell-boundary safety.

Extend `verify_evidence_replay` to require:

- every table decision source is deterministic or cache;
- at least one table-composite generation and verification occurred;
- cache replay makes zero calls;
- character coverage remains `1.0` with no missing or duplicate atoms;
- required repaired cells and forbidden fragments satisfy the golden contract.

Return affected table count, table-spacing generation count, and table-spacing verification count
in the verifier summary.

- [ ] **Step 4: Run deterministic Issue #3 acceptance**

```bash
.venv/bin/pytest -q tests/integration/test_semantic_pdf_regressions.py
.venv/bin/python scripts/verify_issue_3_semantic.py --evidence tests/fixtures/semantic/issue-3-evidence.json --golden tests/fixtures/semantic/issue-3-golden.json
```

Expected: verified output, corrected cells, exact existing tables unchanged, and zero cache calls.

- [ ] **Step 5: Run the configured local LLM experiment**

First inspect only whether the configured key variable is present; never print its value. Instantiate
the packaged `openai-compatible-default` profile through `LLMProviderFactory` and run the Issue #3
evidence replay with that provider. Persist only a bounded local summary containing counts,
confidence/status codes, canonical hash, and pass/fail against golden repaired cells. Do not commit
credentials, provider responses, prompts, or unrestricted cell text.

If the provider is unavailable, keep the deterministic implementation and record the exact bounded
failure code; do not weaken acceptance thresholds.

- [ ] **Step 6: Run repository validation and commit**

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
git diff --check
git add scripts/verify_issue_3_semantic.py tests/fixtures/semantic/issue-3-golden.json tests/integration/test_semantic_pdf_regressions.py tests/unit/semantic/test_diagnostics.py
git commit -m "test: verify issue 3 table cell spacing repair"
```

Do not stage `tests/unit/semantic/test_diagnostics.py` if it did not require a change.
