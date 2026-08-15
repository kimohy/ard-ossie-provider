# Semantic Heading Hierarchy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make numbered semantic-PDF headings use numbering depth instead of ordinal value, so Issue #3 renders peer sections as H2.

**Architecture:** Keep heading recognition and downstream rendering unchanged. Replace only the level derivation in `structure_candidates.py`, then enforce the user-visible outline in both candidate-level tests and the existing Issue #3 evidence replay contract.

**Tech Stack:** Python 3.12, Pydantic semantic models, pytest, CommonMark Markdown renderer

## Global Constraints

- H1 remains reserved for document titles.
- Single-segment numbered headings render as H2 regardless of ordinal value.
- Each additional numeric segment increases the level by one, capped at H6.
- Continued headings retain the level implied by the same numeric prefix.
- Unnumbered heading behavior, source atoms, block order, tables, and Markdown escaping remain unchanged.
- Tests must exercise constructed candidates or rendered pipeline results; source-text inspection tests are excluded.

---

### Task 1: Derive Heading Level from Numbering Depth

**Files:**
- Modify: `tests/unit/semantic/test_structure_candidates.py:261`
- Modify: `tests/fixtures/semantic/issue-3-golden.json:2`
- Modify: `tests/integration/test_semantic_pdf_regressions.py:15`
- Modify: `src/ard_ossie/semantic/structure_candidates.py:568`

**Interfaces:**
- Consumes: `_HEADING_NUMBER` capture group containing a numeric prefix such as `"6"` or `"3.1.2"`.
- Produces: `_heading_level(number: str | None) -> int`, returning levels from 1 through 6.

- [ ] **Step 1: Write the failing candidate-construction test**

Replace the single-case heading test with literal cases that cover peer, nested, continued, and bounded headings:

```python
@pytest.mark.parametrize(
    ("text", "expected_level"),
    (
        ("1. 개요", 2),
        ("2. 테이블 리스트", 2),
        ("6. 지표 정의서", 2),
        ("8. 품질 기준", 2),
        ("3.1 세부 정의", 3),
        ("3.1.2 계산 규칙", 4),
        ("1.2.3.4.5.6 깊은 절", 6),
        ("3. 핵심 업무 용어 (계속)", 2),
    ),
)
def test_numbered_heading_level_follows_hierarchy_depth(
    text: str,
    expected_level: int,
) -> None:
    evidence, layout = _embedded_fixture(((1, text, BOX, None),))

    candidate_set = build_block_candidate_sets(
        evidence=evidence,
        layout=layout,
        hints=StructureDocument(blocks=()),
    )[0]
    heading = next(
        item for item in candidate_set.candidates if item.block_kind == "heading"
    )

    assert isinstance(heading, BlockCandidate)
    assert heading.heading_level == expected_level
    assert heading.atom_ids == layout.regions[0].atom_ids
```

Add `import pytest` to this test module. This catches the production mutation that maps the numeric value, or the raw segment count, directly to the Markdown level.

Add this literal field beside `headings` in `issue-3-golden.json`:

```json
"heading_levels": [1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]
```

In `test_issue_3_replay_is_verified_without_korean_corruption`, derive levels from the actual canonical result and compare them with the fixture:

```python
heading_levels = [
    block.heading_level
    for block in result.canonical.blocks
    if block.kind == "heading"
]

assert heading_levels == golden["heading_levels"]
```

This catches a full-pipeline regression even when heading text and Markdown rendering still succeed.

- [ ] **Step 2: Run the test and verify the intended failure**

Run both behavioral tests:

```bash
.venv/bin/pytest -q tests/unit/semantic/test_structure_candidates.py::test_numbered_heading_level_follows_hierarchy_depth
.venv/bin/pytest -q tests/integration/test_semantic_pdf_regressions.py::test_issue_3_replay_is_verified_without_korean_corruption
```

Expected: FAIL because `1. 개요` receives level 1, `6. 지표 정의서` receives level 6, `3.1 세부 정의` receives level 2, and Issue #3 has actual levels `[1, 1, 1, 2, 3, 3, 4, 5, 6, 6, 6, 6]`.

- [ ] **Step 3: Implement the minimal depth rule**

Change only `_heading_level`:

```python
def _heading_level(number: str | None) -> int:
    if number is None:
        return 1
    segment_count = sum(bool(segment) for segment in number.split("."))
    return min(6, segment_count + 1)
```

- [ ] **Step 4: Run the focused semantic candidate tests**

Run:

```bash
.venv/bin/pytest -q tests/unit/semantic/test_structure_candidates.py
.venv/bin/pytest -q tests/integration/test_semantic_pdf_regressions.py::test_issue_3_replay_is_verified_without_korean_corruption
```

Expected: all tests pass with no warnings.

- [ ] **Step 5: Commit the candidate behavior**

```bash
git add src/ard_ossie/semantic/structure_candidates.py \
  tests/unit/semantic/test_structure_candidates.py \
  tests/fixtures/semantic/issue-3-golden.json \
  tests/integration/test_semantic_pdf_regressions.py
git commit -m "fix: derive semantic heading levels from hierarchy"
```

### Task 2: Lock the Issue #3 User-Visible Outline

**Files:**
- Modify: `scripts/verify_issue_3_semantic.py:287`

**Interfaces:**
- Consumes: canonical heading blocks generated by the existing Issue #3 evidence replay.
- Produces: golden `heading_levels` contract and verifier output that reject ordinal-based heading escalation.

- [ ] **Step 1: Extend the standalone evidence verifier**

In `verify_evidence_replay`, collect the actual levels and require the same golden contract:

```python
heading_levels = [
    block.heading_level
    for block in result.canonical.blocks
    if block.kind == "heading"
]
_require(
    heading_levels == golden["heading_levels"],
    "EVIDENCE_REPLAY_HEADING_LEVELS",
)
```

Include `heading_levels` in the verifier's returned summary so action diagnostics expose the checked outline.

- [ ] **Step 2: Run Issue #3 and semantic regression tests**

Run:

```bash
.venv/bin/pytest -q tests/integration/test_semantic_pdf_regressions.py tests/unit/semantic/test_structure_candidates.py
.venv/bin/python scripts/verify_issue_3_semantic.py \
  --evidence tests/fixtures/semantic/issue-3-evidence.json \
  --golden tests/fixtures/semantic/issue-3-golden.json
```

Expected: pytest passes; the verifier returns `"status": "verified"` with twelve heading levels matching the golden contract.

- [ ] **Step 3: Run repository validation**

Run the repository's configured lint/type gates followed by the full pytest suite:

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
```

Expected: all checks pass with no new warnings or failures.

- [ ] **Step 4: Commit the Issue #3 verifier contract**

```bash
git add scripts/verify_issue_3_semantic.py
git commit -m "test: enforce issue 3 heading outline"
```
