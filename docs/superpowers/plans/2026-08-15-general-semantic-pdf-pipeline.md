# General Semantic PDF Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a general-purpose, evidence-preserving semantic PDF pipeline that reconstructs Korean spacing, reading order, and tables deterministically, asks a model only to choose bounded candidates, and publishes only fully verified documents.

**Architecture:** A new PDF-only path runs beside the current parser until cutover. It extracts immutable character and whitespace evidence, derives layout and semantic candidates, resolves unambiguous candidates deterministically, and sends only ambiguous candidate IDs to the existing multimodal provider boundary. A versioned canonical IR and validator become the publication authority; page/region decisions are content-addressed and reusable, while final promotion remains document-atomic.

**Tech Stack:** Python 3.12, Pydantic 2, pypdfium2 5.12.1, Docling 2.114.0, kiwipiepy 0.23.2, existing multimodal `LLMProvider`, pytest, Ruff, uv, GitHub Actions.

## Global Constraints

- Embedded PDF non-whitespace code points must never be added, deleted, substituted, or reordered.
- Source whitespace remains evidence, but output spacing is a separate, auditable boundary decision.
- A model may return only an allowlisted candidate ID and advisory confidence; it may not return source text, span IDs, ordering, or table geometry.
- Unambiguous regions make zero model calls.
- Every table candidate is a complete rectangular grid before adjudication.
- Page and region work may be cached, but only a globally `VERIFIED` document is automatically published.
- `REVIEW_REQUIRED` and `FAILED` never silently become publishable success.
- Default diagnostics contain hashes, counts, coordinates, and masked previews only; raw text and page images require protected opt-in.
- DOCX, HTML, dictionary, Registry, and Ossie behavior remains unchanged.
- The existing free-form repair path remains available only as a rollback path during stabilization.

---

## Planned file structure

New modules keep one responsibility each:

- `src/ard_ossie/semantic/evidence.py`: immutable atoms, extraction bundles, authoritative regions, and evidence invariants.
- `src/ard_ossie/semantic/evidence_sources.py`: embedded PDF/OCR adapters and mixed-region authority resolution.
- `src/ard_ossie/semantic/layout.py`: baseline lines, regions, repeated edges, columns, and reading-order constraints.
- `src/ard_ossie/semantic/candidates.py`: typed candidate payloads, IDs, scores, and candidate-set invariants.
- `src/ard_ossie/semantic/spacing.py`: geometry/rule/Kiwi whitespace lattice and candidate scoring.
- `src/ard_ossie/semantic/structure_candidates.py`: block, reading-order, continuation, and table candidates.
- `src/ard_ossie/semantic/adjudication.py`: deterministic selection, bounded provider calls, validation, retry, and trusted decision reuse.
- `src/ard_ossie/semantic/canonical.py`: canonical semantic IR, assembly, global invariants, and Markdown rendering.
- `src/ard_ossie/semantic/diagnostics.py`: privacy-safe manifest, evidence, candidate, decision, validation, and failure reports.
- `src/ard_ossie/semantic/pipeline_v2.py`: PDF orchestration and `legacy|shadow|candidate` mode switching.

Existing `semantic/parser.py` remains the public orchestration entrypoint, and the current
`semantic/repair.py` and `semantic/correction.py` remain untouched until the stabilization cleanup.

---

### Task 1: Define immutable evidence and authority models

**Files:**
- Create: `src/ard_ossie/semantic/evidence.py`
- Create: `tests/unit/semantic/test_evidence.py`

**Interfaces:**
- Consumes: existing `SourceBox`, `Sha256`, `StrictModel`, and `canonical_hash`.
- Produces: `AtomId`, `RegionId`, `HypothesisId`, `EvidenceAtom`, `RecognitionHypothesis`, `EvidenceRegion`, `ExtractedEvidence`, `EvidenceDocument`, `make_evidence_id()`, and `authoritative_non_whitespace()`.

- [ ] **Step 1: Write failing identity and invariant tests**

```python
def test_evidence_document_rejects_unknown_and_duplicate_atom_ownership() -> None:
    atom = character_atom("가", ordinal=0)
    duplicate_region = region(REGION_A, atom_ids=(atom.atom_id,))
    with pytest.raises(ValidationError, match="EVIDENCE_ATOM_OWNERSHIP_NOT_UNIQUE"):
        EvidenceDocument(
            schema_version="semantic-evidence-v2",
            source_hash=SOURCE_HASH,
            extraction_mode=EvidenceExtractionMode.PDF_EMBEDDED,
            page_count=1,
            parser_versions={"semantic_parser": "semantic-evidence-v2"},
            atoms=(atom,),
            regions=(duplicate_region, duplicate_region.model_copy(update={"region_id": REGION_B}),),
        )


def test_authoritative_non_whitespace_preserves_exact_code_points() -> None:
    document = evidence_document("데 이터\n시맨틱")
    assert authoritative_non_whitespace(document) == "데이터시맨틱"
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run --frozen pytest -q tests/unit/semantic/test_evidence.py
```

Expected: collection fails because `semantic.evidence` does not exist.

- [ ] **Step 3: Implement the evidence types and validators**

Define a v2-only extraction enum, content-addressed IDs, and frozen Pydantic models without changing
the existing public fidelity enum:

```python
class EvidenceExtractionMode(StrEnum):
    PDF_EMBEDDED = "pdf_embedded"
    PDF_OCR = "pdf_ocr"
    PDF_MIXED = "pdf_mixed"


AtomId = Annotated[str, StringConstraints(pattern=r"^atom_[0-9a-f]{16}$")]
RegionId = Annotated[str, StringConstraints(pattern=r"^region_[0-9a-f]{16}$")]
HypothesisId = Annotated[str, StringConstraints(pattern=r"^hyp_[0-9a-f]{16}$")]


class EvidenceAtom(ImmutableStrictModel):
    atom_id: AtomId
    ordinal: int = Field(ge=0)
    page: int = Field(ge=1)
    bbox: SourceBox | None = None
    text: str = Field(min_length=1, max_length=2)
    kind: Literal["character", "whitespace", "line_break"]
    authority: Literal["embedded", "ocr"]
    source_object: int = Field(ge=0)
    source_index: int = Field(ge=0)
    confidence: float = Field(default=1.0, ge=0, le=1)


class RecognitionHypothesis(ImmutableStrictModel):
    hypothesis_id: HypothesisId
    region_id: RegionId
    page: int = Field(ge=1)
    bbox: SourceBox
    text: str = Field(min_length=1)
    engine: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class EvidenceRegion(ImmutableStrictModel):
    region_id: RegionId
    page: int = Field(ge=1)
    bbox: SourceBox
    atom_ids: tuple[AtomId, ...] = ()
    hypothesis_ids: tuple[HypothesisId, ...] = ()
    authority: Literal["embedded", "ocr", "ambiguous"]
    requires_review: bool = False


def make_evidence_id(prefix: Literal["atom", "region", "hyp"], *parts: object) -> str:
    digest = canonical_hash([str(part) for part in parts])
    return f"{prefix}_{digest[:16]}"
```

Validators must enforce unique IDs and ordinals, exactly one owner region for every selected atom,
known hypothesis references, page bounds, kind/text agreement (`line_break == "\n"`, whitespace
uses `str.isspace()`, character does not), and region authority consistency. `EvidenceDocument`
accepts only resolved `embedded|ocr` regions; `ExtractedEvidence` may retain `ambiguous` regions.

- [ ] **Step 4: Run the tests and verify GREEN**

Run the command from Step 2. Expected: all evidence and existing model tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ard_ossie/semantic/evidence.py tests/unit/semantic/test_evidence.py
git commit -m "feat: define semantic PDF evidence model"
```

---

### Task 2: Extract character-level embedded and regional OCR evidence

**Files:**
- Create: `src/ard_ossie/semantic/evidence_sources.py`
- Create: `tests/unit/semantic/test_evidence_sources.py`
- Modify: `src/ard_ossie/semantic/sources.py:620-784`
- Modify: `tests/unit/semantic/test_pdf_source.py:190-269`

**Interfaces:**
- Consumes: Task 1 evidence models, `SourceFile`, PDFium text pages, and Docling items.
- Produces: `extract_pdf_evidence(source, *, pdfium, ocr_document=None) -> ExtractedEvidence` and `resolve_evidence_authority(extracted, *, selected_hypotheses=None) -> EvidenceDocument`.

- [ ] **Step 1: Write failing character and text-object-boundary tests**

```python
def test_embedded_extraction_keeps_characters_but_not_text_object_semantics(tmp_path: Path) -> None:
    pdfium = FakePdfium(["데이터 시맨틱"], object_ranges=((0, 2), (2, 1), (3, 4)))
    extracted = extract_pdf_evidence(semantic_pdf_source(tmp_path), pdfium=pdfium)
    selected = resolve_evidence_authority(extracted)

    assert "".join(atom.text for atom in selected.atoms) == "데이터 시맨틱"
    assert [atom.source_object for atom in selected.atoms[:3]] == [0, 0, 1]
    assert len(selected.regions) == 1


def test_mixed_page_uses_nonoverlapping_ocr_region_without_duplicating_embedded_text() -> None:
    selected = resolve_evidence_authority(mixed_embedded_and_ocr_fixture())
    assert [region.authority for region in selected.regions] == ["embedded", "ocr"]
    assert authoritative_non_whitespace(selected) == "본문이미지표"
```

Add a CRLF mapping case proving that `\r\n` becomes one `line_break` atom whose `source_index`
points at the original CR while the following character retains its original PDF index.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_evidence_sources.py tests/unit/semantic/test_pdf_source.py
```

Expected: imports fail because the evidence adapter is absent.

- [ ] **Step 3: Implement embedded character extraction**

Move the new behavior to `evidence_sources.py` and leave `extract_pdf_native()` as the legacy
adapter. Walk each PDF text-object range, then emit one `EvidenceAtom` per normalized Unicode code
point. Use each PDF character's own `get_charbox(index)` instead of the union box for the complete
text object. Keep `source_object` and original `source_index`; normalize CRLF only by mapping both
source indices to one line-break atom.

Group characters into provisional regions by page, baseline overlap, and vertical distance. Use
these exact bounds:

```python
BASELINE_TOLERANCE = 0.35
LINE_GAP_MULTIPLIER = 1.75
OCR_DUPLICATE_OVERLAP = 0.60
OCR_SUPPLEMENT_OVERLAP = 0.20
```

The tolerances are relative to median non-degenerate character height, not absolute page units.

- [ ] **Step 4: Implement OCR hypothesis extraction and mixed authority resolution**

Convert each Docling text block or table cell into a `RecognitionHypothesis`. Resolution rules are
deterministic:

```python
if embedded_overlap >= OCR_DUPLICATE_OVERLAP:
    authority = "embedded"
elif embedded_overlap <= OCR_SUPPLEMENT_OVERLAP:
    authority = "ocr"
else:
    authority = "embedded"
    requires_review = True
```

For an OCR-selected region, materialize the selected recognizer text as character/whitespace atoms
with its confidence. `selected_hypotheses` maps region IDs to allowlisted hypothesis IDs. Without
that mapping, a competing OCR region uses the highest-confidence hypothesis only for a diagnostic
preview and remains review-required until Task 7 adjudication. Never merge an OCR string into an
overlapping embedded character sequence.

- [ ] **Step 5: Verify GREEN and legacy compatibility**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_evidence_sources.py tests/unit/semantic/test_pdf_source.py
```

Expected: new tests pass, and existing `extract_pdf_native()` assertions remain unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/ard_ossie/semantic/evidence_sources.py src/ard_ossie/semantic/sources.py tests/unit/semantic/test_evidence_sources.py tests/unit/semantic/test_pdf_source.py
git commit -m "feat: extract regional semantic PDF evidence"
```

---

### Task 3: Normalize lines, regions, repeated edges, and reading order

**Files:**
- Create: `src/ard_ossie/semantic/layout.py`
- Create: `tests/unit/semantic/test_layout.py`

**Interfaces:**
- Consumes: `EvidenceDocument` and optional existing `StructureDocument` hints.
- Produces: `LayoutLine`, `LayoutRegion`, `ReadingOrderEdge`, `LayoutDocument`, and `normalize_layout(evidence, hints) -> LayoutDocument`.

- [ ] **Step 1: Write failing normalization tests**

```python
def test_text_object_fragmentation_does_not_create_words_or_paragraphs() -> None:
    layout = normalize_layout(fragmented_korean_evidence(), StructureDocument(blocks=()))
    assert len(layout.lines) == 1
    assert len(layout.regions) == 1
    assert layout.regions[0].atom_ids == tuple(atom.atom_id for atom in fragmented_korean_evidence().atoms)


def test_two_columns_create_a_dag_without_cross_column_interleaving() -> None:
    layout = normalize_layout(two_column_evidence(), StructureDocument(blocks=()))
    assert topological_region_orders(layout) == (("left_1", "left_2", "right_1", "right_2"),)


def test_repeated_page_edge_text_is_an_exclusion_candidate() -> None:
    layout = normalize_layout(repeated_header_fixture(page_count=5), StructureDocument(blocks=()))
    assert {region.hint for region in layout.regions if region.repeated_edge} == {"page_header"}
```

- [ ] **Step 2: Run and verify RED**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_layout.py
```

Expected: module import fails.

- [ ] **Step 3: Implement deterministic layout normalization**

Define frozen types and one pure entrypoint:

```python
def normalize_layout(
    evidence: EvidenceDocument,
    hints: StructureDocument,
    *,
    page_edge_band: float = 0.10,
    repeat_ratio: float = 0.60,
) -> LayoutDocument:
    lines = _cluster_baselines(evidence.atoms)
    columns = _cluster_columns(lines)
    regions = _group_regions(lines, columns, hints)
    repeated = _mark_repeated_edges(regions, evidence.page_count, page_edge_band, repeat_ratio)
    edges = _reading_order_edges(repeated, columns)
    return LayoutDocument(lines=lines, regions=repeated, order_edges=edges)
```

Sort every geometry tie by `(page, top descending, left, first_atom_ordinal)`. Use Docling block
types only as `hint`; a hint cannot author text or override atom ownership. Reject cycles with
`READING_ORDER_CYCLE` and retain all source whitespace atom IDs in the owning line.

- [ ] **Step 4: Run and verify GREEN**

Run Step 2. Expected: all layout tests pass with stable ordering across repeated runs.

- [ ] **Step 5: Commit**

```bash
git add src/ard_ossie/semantic/layout.py tests/unit/semantic/test_layout.py
git commit -m "feat: normalize semantic PDF layout"
```

---

### Task 4: Build a Korean-aware, character-conserving whitespace lattice

**Files:**
- Create: `src/ard_ossie/semantic/candidates.py`
- Create: `src/ard_ossie/semantic/spacing.py`
- Create: `tests/unit/semantic/test_candidates.py`
- Create: `tests/unit/semantic/test_spacing.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: `EvidenceDocument`, `LayoutDocument`, and `kiwipiepy.Kiwi`.
- Produces: `CandidateId`, `SpacingBoundary`, `SpacingCandidate`, `CandidateSet`, `KoreanSpacingScorer`, `KiwiSpacingScorer`, and `build_spacing_candidate_set(region, evidence, layout, scorer) -> CandidateSet`.

- [ ] **Step 1: Add failing candidate and Korean spacing tests**

```python
def test_spacing_candidate_rejects_non_whitespace_character_change() -> None:
    with pytest.raises(ValidationError, match="CANDIDATE_CHARACTER_CONSERVATION_FAILED"):
        spacing_candidate(source="데이터시맨틱", rendered="데이터 의미 모델")


def test_kiwi_candidate_removes_pdf_inserted_korean_spaces() -> None:
    candidate_set = build_spacing_candidate_set(
        region=korean_region("데 이 터 시 맨 틱 모 델 을 구 성 한 다"),
        evidence=korean_evidence("데 이 터 시 맨 틱 모 델 을 구 성 한 다"),
        layout=korean_layout(),
        scorer=KiwiSpacingScorer(),
    )
    assert "데이터 시맨틱 모델을 구성한다" in {
        candidate.rendered_text for candidate in candidate_set.candidates
    }
    assert all(
        without_whitespace(candidate.rendered_text) == "데이터시맨틱모델을구성한다"
        for candidate in candidate_set.candidates
    )
```

Also cover Latin/number/unit boundaries (`CTR 10 %`), paired punctuation, headings, line-wrap
joining, and table-cell isolation.

- [ ] **Step 2: Add the morphology dependency and refresh the lock**

Add the exact runtime dependency:

```toml
"kiwipiepy==0.23.2",
```

Then run:

```bash
uv lock
uv run --frozen python -c "from kiwipiepy import Kiwi; print(Kiwi().space('데이터시맨틱'))"
```

Expected: the import succeeds on Python 3.12 and prints a non-empty Korean string.

- [ ] **Step 3: Run the new tests and verify RED**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_candidates.py tests/unit/semantic/test_spacing.py
```

Expected: imports fail because the candidate and spacing modules do not exist.

- [ ] **Step 4: Implement typed candidate sets and the spacing scorer**

Use discriminated frozen candidate models and deterministic IDs. A spacing boundary is always
between two consecutive non-whitespace atom IDs:

```python
class SpacingBoundary(ImmutableStrictModel):
    left_atom_id: AtomId
    right_atom_id: AtomId
    state: Literal["none", "space", "hard_break"]
    source_whitespace_atom_ids: tuple[AtomId, ...] = ()


class SpacingCandidate(ImmutableStrictModel):
    kind: Literal["spacing"] = "spacing"
    candidate_id: CandidateId
    region_id: RegionId
    rendered_text: str
    atom_ids: tuple[AtomId, ...]
    boundaries: tuple[SpacingBoundary, ...]
    score: float = Field(ge=0, le=1)
    features: dict[str, float]
```

Generate at most five unique candidates from source whitespace, normalized geometry, block policy,
`Kiwi.space(text, reset_whitespace=True)`, and `Kiwi.glue(line_chunks)`. Convert Kiwi output back to
boundary states only after exact non-whitespace equality succeeds. Rank by canonical
`(-score, candidate_id)` and preserve component feature scores for diagnostics.

- [ ] **Step 5: Run spacing tests and verify GREEN**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_candidates.py tests/unit/semantic/test_spacing.py
```

Expected: all tests pass, including exact character-conservation assertions.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/ard_ossie/semantic/candidates.py src/ard_ossie/semantic/spacing.py tests/unit/semantic/test_candidates.py tests/unit/semantic/test_spacing.py
git commit -m "feat: generate Korean spacing candidates"
```

---

### Task 5: Generate block, reading-order, and continuation candidates

**Files:**
- Create: `src/ard_ossie/semantic/structure_candidates.py`
- Create: `tests/unit/semantic/test_structure_candidates.py`
- Modify: `src/ard_ossie/semantic/candidates.py`

**Interfaces:**
- Consumes: evidence, layout, spacing candidate sets, and `StructureDocument` hints.
- Produces: `RecognitionCandidate`, `BlockCandidate`, `ReadingOrderCandidate`, `ContinuationCandidate`, `build_recognition_candidate_sets()`, `build_block_candidate_sets()`, `build_reading_order_candidate_set()`, and `build_continuation_candidate_sets()`.

- [ ] **Step 1: Write failing semantic-structure tests**

```python
def test_heading_candidate_uses_font_and_numbering_without_authoring_text() -> None:
    sets = build_block_candidate_sets(heading_fixture(), empty_structure_hints())
    heading = max(sets[0].candidates, key=lambda item: item.score)
    assert heading.block_kind == "heading"
    assert heading.heading_level == 2
    assert heading.atom_ids == heading_fixture().regions[0].atom_ids


def test_cross_page_continuation_requires_edge_alignment_and_repeated_header() -> None:
    candidate_set = build_continuation_candidate_sets(cross_page_table_layout())[0]
    assert {(item.continue_previous, item.score) for item in candidate_set.candidates} == {
        (True, 0.90),
        (False, 0.55),
    }


def test_competing_ocr_hypotheses_become_allowlisted_recognition_candidates() -> None:
    candidate_set = build_recognition_candidate_sets(competing_ocr_bundle())[0]
    assert {item.hypothesis_id for item in candidate_set.candidates} == {
        OCR_HYPOTHESIS_A,
        OCR_HYPOTHESIS_B,
    }
    assert all(item.region_id == OCR_REGION for item in candidate_set.candidates)
```

Add list depth, caption, multi-column order, and a case where a Docling heading hint conflicts with
ordinary font/geometry and therefore cannot auto-win.

- [ ] **Step 2: Run and verify RED**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_structure_candidates.py
```

Expected: module import fails.

- [ ] **Step 3: Implement bounded structural candidates**

Extend the discriminated candidate union, then implement pure builders. Recognition candidates
copy only allowlisted `RecognitionHypothesis` IDs/text/confidence and never create a third
transcription. Candidate features are
limited to normalized geometry, font/style evidence, numbering/list markers, repeated-edge state,
Docling hint agreement, lexical shape, and source-order constraints. Emit no more than five
candidates per decision and reject any candidate whose atom IDs are unknown, duplicated, or not a
contiguous legal allocation for that region.

Reading-order candidates must be topological sorts of `LayoutDocument.order_edges`; enumerate only
ties between independent columns/regions and stop after five canonical orders.

- [ ] **Step 4: Run and verify GREEN**

Run Step 2. Expected: all structural candidate tests pass and repeated runs serialize identically.

- [ ] **Step 5: Commit**

```bash
git add src/ard_ossie/semantic/candidates.py src/ard_ossie/semantic/structure_candidates.py tests/unit/semantic/test_structure_candidates.py
git commit -m "feat: generate semantic structure candidates"
```

---

### Task 6: Reconstruct complete table grids deterministically

**Files:**
- Modify: `src/ard_ossie/semantic/structure_candidates.py`
- Modify: `src/ard_ossie/semantic/candidates.py`
- Create: `tests/unit/semantic/test_table_candidates.py`

**Interfaces:**
- Consumes: table `LayoutRegion`, evidence atoms, line geometry, and optional Docling table hints.
- Produces: `TableCellCandidate`, `TableCandidate`, and `build_table_candidate_set(region, evidence, layout, hints) -> CandidateSet`.

- [ ] **Step 1: Write failing grid tests**

```python
def test_borderless_table_forms_rectangular_grid_with_blank_cell() -> None:
    candidate_set = build_table_candidate_set(*borderless_table_fixture())
    selected = max(candidate_set.candidates, key=lambda item: item.score)
    assert (selected.row_count, selected.column_count) == (3, 3)
    assert {(cell.start_row, cell.start_column) for cell in selected.cells} == {
        (row, column) for row in range(3) for column in range(3)
    }
    assert selected.cells[5].atom_ids == ()


def test_merged_header_owns_atoms_once_and_covers_each_grid_coordinate() -> None:
    selected = highest_table_candidate(merged_header_fixture())
    assert selected.cells[0].end_column == 3
    assert_table_grid_complete(selected)
    assert allocations(selected).count(merged_header_atom_id()) == 1
```

Add page-spanning repeated headers, invalid overlaps, vertically split cell text, and a table whose
geometry is genuinely ambiguous and therefore yields two valid candidates.

- [ ] **Step 2: Run and verify RED**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_table_candidates.py
```

Expected: table candidate types and builder are absent.

- [ ] **Step 3: Implement grid inference and hard validation**

Derive row/column bands by clustering aligned character and line boxes. Candidate variants may
differ only at merged-cell and header boundaries supported by geometry or Docling hints. Expand
blank coordinates as explicit empty cells, but never duplicate merged-cell atom ownership.

Reuse the existing table limits from `semantic.models` and enforce this before returning a set:

```python
def assert_table_grid_complete(candidate: TableCandidate) -> None:
    occupied: dict[tuple[int, int], str] = {}
    for cell in candidate.cells:
        for row in range(cell.start_row, cell.end_row):
            for column in range(cell.start_column, cell.end_column):
                if (row, column) in occupied:
                    raise ValueError("TABLE_GRID_OVERLAP")
                occupied[(row, column)] = cell.cell_id
    expected = {
        (row, column)
        for row in range(candidate.row_count)
        for column in range(candidate.column_count)
    }
    if set(occupied) != expected:
        raise ValueError("TABLE_GRID_NOT_PARTITIONED")
```

- [ ] **Step 4: Run table and existing structure tests**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_table_candidates.py tests/unit/semantic/test_structure.py
```

Expected: new and legacy structure tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ard_ossie/semantic/candidates.py src/ard_ossie/semantic/structure_candidates.py tests/unit/semantic/test_table_candidates.py
git commit -m "feat: reconstruct semantic PDF table grids"
```

---

### Task 7: Add bounded model adjudication and trusted decision reuse

**Files:**
- Create: `src/ard_ossie/semantic/adjudication.py`
- Create: `tests/unit/semantic/test_adjudication.py`
- Modify: `src/ard_ossie/semantic/candidates.py`

**Interfaces:**
- Consumes: `CandidateSet`, optional PNG crop, `LLMProvider`, and trusted `DecisionRecord` values.
- Produces: `AdjudicationPolicy`, `CandidateChoice`, `DecisionRecord`, `DecisionReport`, `candidate_choice_schema()`, and `CandidateAdjudicator.decide()`.

- [ ] **Step 1: Write failing deterministic and provider-boundary tests**

```python
def test_clear_score_margin_selects_without_provider_call() -> None:
    provider = RecordingProvider()
    decision = CandidateAdjudicator(provider).decide(clear_candidate_set())
    assert decision.source == "deterministic"
    assert decision.selected_candidate_id == clear_candidate_set().candidates[0].candidate_id
    assert provider.calls == []


def test_ambiguous_request_contains_bounded_candidate_text_but_no_raw_catalog() -> None:
    provider = RecordingProvider(response={"candidate_id": CANDIDATE_B, "confidence": 0.91})
    decision = CandidateAdjudicator(provider).decide(ambiguous_spacing_set())
    request = provider.calls[0]
    assert decision.source == "model"
    assert set(request.candidate_ids) == {CANDIDATE_A, CANDIDATE_B}
    payload = json.dumps(request.messages, ensure_ascii=False)
    assert "데이터 시맨틱" in payload
    assert "atom_ids" not in payload
    assert "source_object" not in payload
    assert "original_page_catalog" not in payload


def test_unknown_candidate_is_retried_once_then_requires_review() -> None:
    provider = SequenceProvider([
        {"candidate_id": "candidate_deadbeefdeadbeef", "confidence": 0.99},
        {"candidate_id": "candidate_deadbeefdeadbeef", "confidence": 0.99},
    ])
    decision = CandidateAdjudicator(provider).decide(ambiguous_spacing_set())
    assert decision.outcome == "review_required"
    assert decision.validation_codes == ("LLM_CANDIDATE_UNKNOWN", "LLM_CANDIDATE_UNKNOWN")
    assert len(provider.calls) == 2
```

Add tests for trusted-cache reuse, mismatched cache hashes, unavailable provider, transient provider
failure propagation, schema-invalid free text, and optional one-image multimodal requests.

- [ ] **Step 2: Run and verify RED**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_adjudication.py
```

Expected: module import fails.

- [ ] **Step 3: Implement decision contracts and cache keys**

Use these policy defaults:

```python
class AdjudicationPolicy(ImmutableStrictModel):
    auto_accept_score: float = 0.82
    auto_accept_margin: float = 0.12
    minimum_model_confidence: float = 0.80
    max_candidates: int = 5
    max_schema_attempts: int = 2
```

The request hash covers source/evidence/region/candidate-set/prompt/schema/provider/model hashes.
Reuse a trusted decision only when every hash matches and the selected candidate is still present.
The provider schema has exactly two required fields, `candidate_id` and `confidence`, with
`additionalProperties: false`.

- [ ] **Step 4: Implement deterministic selection and bounded calls**

Sort candidates canonically. Auto-select only when the best score reaches `0.82` and its margin to
the runner-up reaches `0.12`. Otherwise send candidate IDs, bounded candidate renderings, and
feature summaries; omit the raw page catalog, atom IDs, source-object boundaries, and unrelated
regions. Candidate renderings are required for language-level spacing comparison and OCR
hypothesis selection, but the response schema still permits only a candidate ID and confidence. A
page crop may be sent through `LLMService.generate_multimodal_structured` only for visual decision
types. One schema/allowlist failure receives one corrective request; an uncertain valid response
returns `review_required` without another call. Use `LLMService` so transport retry/backoff retains
the existing provider policy.

- [ ] **Step 5: Run adjudication and LLM contract tests**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_adjudication.py tests/unit/test_llm_service.py tests/unit/test_openai_adapters.py tests/unit/test_vertex_adapters.py
```

Expected: all selected tests pass; existing provider payload contracts remain unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/ard_ossie/semantic/adjudication.py src/ard_ossie/semantic/candidates.py tests/unit/semantic/test_adjudication.py
git commit -m "feat: add bounded semantic candidate adjudication"
```

---

### Task 8: Assemble, validate, and render the canonical semantic IR

**Files:**
- Create: `src/ard_ossie/semantic/canonical.py`
- Create: `tests/unit/semantic/test_canonical.py`
- Modify: `src/ard_ossie/semantic/render.py:111-244`
- Modify: `tests/unit/semantic/test_render.py`

**Interfaces:**
- Consumes: evidence, layout, candidate sets, and `DecisionRecord` values.
- Produces: `SemanticPipelineStatus`, `CanonicalBlock`, `CanonicalCell`, `CanonicalFigure`, `ExcludedEvidence`, `WhitespaceDisposition`, `CanonicalSemanticDocument`, `ValidationFinding`, `SemanticValidationReport`, `assemble_canonical()`, `validate_canonical()`, and `render_canonical_markdown()`.

- [ ] **Step 1: Write failing global-invariant tests**

```python
def test_canonical_validation_detects_one_deleted_embedded_character() -> None:
    document = canonical_document(text="데이터 시맨틱")
    corrupted = document.model_copy(update={"blocks": blocks_with_text("데이터 시맨")})
    report = validate_canonical(embedded_evidence("데이터 시맨틱"), corrupted)
    assert report.status is SemanticPipelineStatus.FAILED
    assert [finding.code for finding in report.findings] == ["INVARIANT_CHARACTER_LOSS"]


def test_low_confidence_decision_keeps_preview_but_blocks_publication() -> None:
    document, report = assemble_fixture_with_review_required_decision()
    assert render_canonical_markdown(document)
    assert report.status is SemanticPipelineStatus.REVIEW_REQUIRED
    assert report.publishable is False
```

Add exact atom allocation, whitespace disposition accounting, reading-order DAG, table grid,
cross-page continuation, raw HTML, and repeated canonical-hash tests.

- [ ] **Step 2: Run and verify RED**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_canonical.py tests/unit/semantic/test_render.py
```

Expected: canonical module is absent.

- [ ] **Step 3: Implement canonical types and assembly**

Canonical blocks store final text plus exact non-whitespace atom IDs. `CanonicalFigure` stores a
page/bounding-box reference and optional source-backed caption atom IDs; it never embeds page-image
bytes. `ExcludedEvidence` permits only repeated page headers, footers, and page numbers proven by
Task 3 frequency/edge evidence. Each source whitespace atom receives one disposition:

```python
class WhitespaceDisposition(ImmutableStrictModel):
    atom_id: AtomId
    outcome: Literal["retained", "normalized", "layout_only"]
    boundary_state: Literal["none", "space", "hard_break"]
    decision_id: str


class SemanticValidationReport(StrictModel):
    status: SemanticPipelineStatus
    publishable: bool
    source_hash: Sha256
    canonical_hash: Sha256
    findings: list[ValidationFinding]
    character_coverage: float = Field(ge=0, le=1)
    missing_atom_count: int = Field(ge=0)
    duplicate_atom_count: int = Field(ge=0)
    degraded_block_count: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
```

Assembly may choose the highest deterministic candidate for a diagnostic preview when a decision
requires review, but it must carry that state into validation.

- [ ] **Step 4: Implement authoritative validation and Markdown rendering**

Validate in stable code order: evidence references, allocation uniqueness, embedded character
sequence, whitespace accounting, reading order, table topology, decision status, then rendered
Markdown. For embedded evidence compare exact Python code points after removing `str.isspace()`;
do not NFC/NFKC-normalize the authoritative comparison.

Render headings, paragraphs, lists, GFM tables, and source-backed figure captions. A figure without
a caption is retained in canonical provenance but emits no invented text or image URL. Resolve final
text from canonical blocks, not from original `SourceSpan` boundaries. Reuse existing Markdown
escaping helpers and raw-HTML guard; keep the legacy renderer for DOCX and rollback mode.

- [ ] **Step 5: Run and verify GREEN**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_canonical.py tests/unit/semantic/test_render.py
```

Expected: all canonical and legacy rendering tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/ard_ossie/semantic/canonical.py src/ard_ossie/semantic/render.py tests/unit/semantic/test_canonical.py tests/unit/semantic/test_render.py
git commit -m "feat: validate canonical semantic PDF IR"
```

---

### Task 9: Integrate the new PDF path in legacy, shadow, and candidate modes

**Files:**
- Create: `src/ard_ossie/semantic/pipeline_v2.py`
- Create: `tests/integration/test_semantic_pdf_v2.py`
- Modify: `src/ard_ossie/semantic/parser.py:62-158`
- Modify: `src/ard_ossie/docling_parser.py:20-83`
- Modify: `src/ard_ossie/pipeline.py:182-226,473-515`
- Modify: `src/ard_ossie/application/modeling.py:46-91`
- Modify: `src/ard_ossie/application/processing.py`
- Modify: `tests/integration/test_docling_pipeline.py`
- Modify: `tests/integration/test_cli_process.py`

**Interfaces:**
- Consumes: all Tasks 1-8 interfaces, existing converter injection, provider, and trusted decisions.
- Produces: `SemanticPipelineMode`, `SemanticPipelineResult`, `parse_semantic_pdf_v2()`, `canonical_fidelity_report()`, and mode-aware `parse_semantic_document()` behavior.

- [ ] **Step 1: Write failing mode-boundary integration tests**

```python
@pytest.mark.parametrize(
    ("mode", "expected_markdown_source"),
    [("legacy", "legacy"), ("shadow", "legacy"), ("candidate", "canonical")],
)
def test_pdf_pipeline_mode_controls_publication_not_docx(
    mode: str, expected_markdown_source: str
) -> None:
    result = parse_fixture(mode=mode)
    assert result.markdown == fixture_markdown(expected_markdown_source)
    assert result.pipeline_result is not None
    assert result.pipeline_result.mode is SemanticPipelineMode(mode)


def test_candidate_mode_never_invokes_free_form_repair() -> None:
    repair = ExplodingRepairPlanner()
    result = parse_fixture(mode="candidate", repair_planner=repair)
    assert result.pipeline_result is not None
    assert result.pipeline_result.validation.status == "VERIFIED"
    assert repair.calls == 0
```

Add tests proving DOCX ignores the PDF mode, shadow differences do not change published Markdown,
and a `REVIEW_REQUIRED` candidate-mode PDF is rejected by the existing hard quality gate.

- [ ] **Step 2: Run and verify RED**

```bash
uv run --frozen pytest -q tests/integration/test_semantic_pdf_v2.py tests/integration/test_docling_pipeline.py
```

Expected: `SemanticPipelineMode` and `pipeline_v2` are absent.

- [ ] **Step 3: Implement the PDF v2 orchestrator**

Use one explicit result type:

```python
class SemanticPipelineMode(StrEnum):
    LEGACY = "legacy"
    SHADOW = "shadow"
    CANDIDATE = "candidate"


@dataclass(frozen=True)
class SemanticPipelineResult:
    mode: SemanticPipelineMode
    markdown: str
    canonical: CanonicalSemanticDocument
    validation: SemanticValidationReport
    decisions: DecisionReport
    candidate_sets: tuple[CandidateSet, ...]
```

`parse_semantic_pdf_v2()` obtains ordinary Docling hints once, requests OCR evidence only for
missing/supplemental regions, builds and adjudicates recognition candidates, resolves authoritative
evidence, then normalizes layout and builds the remaining candidate sets. It adjudicates those
region by region before global assembly and validation. Crop rendering is lazy: call
`render_pdf_page_images()` only when an ambiguous visual decision actually needs a crop.

- [ ] **Step 4: Wire mode selection without changing non-PDF behavior**

Add a validated `semantic_pipeline_mode` argument through `process_product`, `ModelingService`,
`DoclingParser`, and `parse_semantic_document`; add
`SemanticParseResult.pipeline_result: SemanticPipelineResult | None`. In `shadow`, compute both
paths but return legacy
Markdown/fidelity; retain candidate diagnostics and a semantic diff summary. In `candidate`, return
canonical Markdown. `canonical_fidelity_report()` creates the backward-compatible
`SemanticFidelityReport` counts from canonical atom allocations while `ParsedDocument` also carries
the authoritative `SemanticValidationReport` as an excluded runtime field. Update
`_semantic_hard_findings` to prefer that report and skip legacy OCR-correction/repair requirements
only when a candidate validation report is present. Map `VERIFIED|REVIEW_REQUIRED|FAILED` to the
current publish gate without treating review as success. Default to `shadow` in Python until Task
11 cutover.

- [ ] **Step 5: Run focused integration tests**

```bash
uv run --frozen pytest -q tests/integration/test_semantic_pdf_v2.py tests/integration/test_docling_pipeline.py tests/integration/test_cli_process.py -k "semantic or pdf"
```

Expected: mode behavior passes and existing DOCX/HTML tests selected by the expression remain green.

- [ ] **Step 6: Commit**

```bash
git add src/ard_ossie/semantic/pipeline_v2.py src/ard_ossie/semantic/parser.py src/ard_ossie/docling_parser.py src/ard_ossie/pipeline.py src/ard_ossie/application/modeling.py src/ard_ossie/application/processing.py tests/integration/test_semantic_pdf_v2.py tests/integration/test_docling_pipeline.py tests/integration/test_cli_process.py
git commit -m "feat: integrate candidate semantic PDF pipeline"
```

---

### Task 10: Persist reusable decisions and always-upload privacy-safe diagnostics

**Files:**
- Create: `src/ard_ossie/semantic/diagnostics.py`
- Create: `tests/unit/semantic/test_diagnostics.py`
- Modify: `src/ard_ossie/docling_parser.py:20-83`
- Modify: `src/ard_ossie/pipeline.py:1114-1208,1938-2058`
- Modify: `src/ard_ossie/application/modeling.py:46-91`
- Modify: `src/ard_ossie/application/source_check.py:124-193`
- Modify: `src/ard_ossie/cli/workflow.py:183-204`
- Modify: `src/ard_ossie/application/processing.py:635-706`
- Modify: `src/ard_ossie/release.py:43-151,280-310`
- Modify: `src/ard_ossie/application/model_schema_verification.py:24-67`
- Create: `schemas/reports/semantic-validation.schema.json`
- Modify: `.github/workflows/ard-process.yml:66-84,188-198`
- Modify: `tests/unit/test_source_check_service.py`
- Modify: `tests/integration/test_atomic_promotion.py`
- Modify: `tests/integration/test_workflow_contracts.py`
- Modify: `tests/unit/test_release.py`
- Modify: `tests/unit/test_model_schema_verification.py`

**Interfaces:**
- Consumes: `SemanticPipelineResult`, candidate/decision reports, and secure repository paths.
- Produces: `SemanticDiagnostics`, `write_semantic_diagnostics()`, six named JSON reports, trusted decision loading, validation schema, and validation-job artifacts.

- [ ] **Step 1: Write failing redaction and artifact tests**

```python
def test_default_diagnostics_do_not_contain_source_text_or_image_bytes(tmp_path: Path) -> None:
    write_semantic_diagnostics(tmp_path, diagnostic_fixture(secret_text="민감한 원문"))
    payload = "".join(path.read_text() for path in tmp_path.glob("*.json"))
    assert "민감한 원문" not in payload
    assert "iVBOR" not in payload
    assert SOURCE_HASH in payload


def test_failed_validate_job_uploads_semantic_diagnostics() -> None:
    workflow = load_workflow("ard-process.yml")
    validation_steps = workflow["jobs"]["validate"]["steps"]
    upload = next(step for step in validation_steps if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["if"] == "always()"
    assert "candidate/.ard/run/semantic-validate" in upload["with"]["path"]
```

Add atomic-promotion tests for all reports and a trusted-decision tamper test proving a changed
candidate-set hash prevents reuse.

- [ ] **Step 2: Run and verify RED**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_diagnostics.py tests/integration/test_workflow_contracts.py tests/integration/test_atomic_promotion.py -k "semantic or diagnostic or quality"
```

Expected: diagnostics module and validation upload are absent.

- [ ] **Step 3: Implement privacy-safe reports**

Write these files on success and failure:

```text
manifest.json
evidence-summary.json
candidate-report.json
decision-report.json
validation-report.json
failure-report.json
```

Masked previews contain at most 24 characters, replace the middle 16 characters with `…`, and are
disabled entirely for values shorter than 8 characters. Reports include stable stage codes,
source/configuration hashes, page/region coordinates, counts, scores, attempts, cache outcomes, and
publication status. Raw mode writes to a separate `raw/` directory only when an explicit
`include_raw=True` reaches the writer; no environment variable alone may bypass the caller.

- [ ] **Step 4: Persist quality reports and trusted decisions atomically**

Add the six report names to `_QUALITY_DESTINATIONS` and `quality_artifact_hashes`. Register
`SemanticValidationReport` at `reports/semantic-validation.schema.json`, generate its schema from
`model_json_schema()`, and extend release verification so a missing or non-`VERIFIED` validation
report fails closed.

Extend trusted artifact loading to accept `decision-report.json` only after its SHA-256 matches the
quality report. Pass valid records to `CandidateAdjudicator`; never trust candidate text or geometry
from the prior artifact.

- [ ] **Step 5: Preserve validate-stage diagnostics outside temporary staging**

Add `--diagnostics-dir "$CANDIDATE_REPOSITORY/.ard/run/semantic-validate"` to `workflow source-check`.
Resolve that path through `FileSystemPort.resolve_write`, pass it into `ModelingService.validate`,
and have `process_product`/the PDF v2 parser write diagnostics before returning or raising.

Add a validation-job upload step immediately after source-check:

```yaml
- name: Upload semantic validation diagnostics
  if: always()
  uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
  with:
    name: ard-semantic-validate-${{ inputs.product_key }}-${{ github.run_id }}
    path: candidate/.ard/run/semantic-validate
    retention-days: 14
    if-no-files-found: warn
```

- [ ] **Step 6: Run schema, workflow, promotion, and release tests**

```bash
uv run --frozen pytest -q tests/unit/semantic/test_diagnostics.py tests/unit/test_source_check_service.py tests/integration/test_atomic_promotion.py tests/integration/test_workflow_contracts.py tests/unit/test_release.py tests/unit/test_model_schema_verification.py
uv run --frozen python -m ard_ossie.application.model_schema_verification --repository .
```

Expected: all tests pass and schema verification exits zero.

- [ ] **Step 7: Commit**

```bash
git add src/ard_ossie/semantic/diagnostics.py src/ard_ossie/docling_parser.py src/ard_ossie/pipeline.py src/ard_ossie/application/modeling.py src/ard_ossie/application/source_check.py src/ard_ossie/cli/workflow.py src/ard_ossie/application/processing.py src/ard_ossie/release.py src/ard_ossie/application/model_schema_verification.py schemas/reports/semantic-validation.schema.json .github/workflows/ard-process.yml tests/unit/semantic/test_diagnostics.py tests/unit/test_source_check_service.py tests/integration/test_atomic_promotion.py tests/integration/test_workflow_contracts.py tests/unit/test_release.py tests/unit/test_model_schema_verification.py
git commit -m "feat: persist semantic PDF diagnostics"
```

---

### Task 11: Add representative regression corpus, Issue #3 acceptance, and cutover gates

**Files:**
- Create: `tests/fixtures/semantic/issue-3-evidence.json`
- Create: `tests/fixtures/semantic/issue-3-golden.json`
- Create: `tests/fixtures/semantic/table-heavy-evidence.json`
- Create: `tests/unit/semantic/test_fixture_capture.py`
- Create: `tests/integration/test_semantic_pdf_regressions.py`
- Create: `tests/performance/test_semantic_pdf_performance.py`
- Modify: `scripts/verify_issue_3_semantic.py`
- Modify: `.github/workflows/ard-process.yml:39-42,98-115`
- Create: `docs/operations/semantic-pdf-rollout.md`

**Interfaces:**
- Consumes: the complete candidate pipeline, public Issue #3 attachment, and diagnostic reports.
- Produces: offline evidence replays, reviewed golden IR assertions, performance baselines, candidate-mode workflow default, and a rollback runbook.

- [ ] **Step 1: Write failing fixture-capture tests**

```python
def test_captured_evidence_is_replayable_and_contains_no_image_bytes(tmp_path: Path) -> None:
    destination = tmp_path / "evidence.json"
    capture_evidence(public_pdf_source(), destination, pdfium=FakePdfium.issue_3_shape())
    payload = json.loads(destination.read_text())
    replay = load_evidence_replay(destination)
    assert replay.source_hash == ISSUE_3_SOURCE_HASH
    assert "page_images" not in payload
    assert all("image_bytes" not in region for region in payload["regions"])
```

Run:

```bash
uv run --frozen pytest -q tests/unit/semantic/test_fixture_capture.py
```

Expected: import fails because capture/replay helpers are not present.

- [ ] **Step 2: Implement fixture capture and replay commands**

Extend `scripts/verify_issue_3_semantic.py` with `capture_evidence(source, destination)` and
`load_evidence_replay(path)`. Add mutually exclusive CLI modes `--capture-evidence OUTPUT PDF` and
`--evidence INPUT --golden GOLDEN`. Serialize through the Task 1 Pydantic models in canonical key
order, reject source hashes other than the supplied PDF hash, and exclude image bytes and model
credentials. Rerun Step 1 and expect PASS.

- [ ] **Step 3: Capture a deterministic public Issue #3 evidence replay**

Use the public attachment and verify it before deriving the JSON fixture:

```bash
mkdir -p .ard/run/fixture-capture
curl -fL https://github.com/user-attachments/files/30932950/Marketing.Insight.Data.Semantics.pdf -o .ard/run/fixture-capture/issue-3.pdf
printf '%s  %s\n' 'ca630eac7231e454a2398e2f1e25328490966ab1e110230f1c5eaba6ab367cf6' '.ard/run/fixture-capture/issue-3.pdf' | sha256sum -c -
uv run --frozen python scripts/verify_issue_3_semantic.py --capture-evidence tests/fixtures/semantic/issue-3-evidence.json .ard/run/fixture-capture/issue-3.pdf
```

The committed fixture contains atoms, regions, boxes, and structural hints but no page image bytes.
Its source hash must remain the exact hash above.

- [ ] **Step 4: Write the failing Issue #3 golden regression**

```python
def test_issue_3_replay_is_verified_without_korean_corruption() -> None:
    result = run_evidence_replay("issue-3-evidence.json", mode="candidate")
    repeated = run_evidence_replay("issue-3-evidence.json", mode="candidate")
    assert result.validation.status == "VERIFIED"
    assert result.validation.character_coverage == 1.0
    assert result.validation.missing_atom_count == 0
    assert result.validation.duplicate_atom_count == 0
    assert result.validation.degraded_block_count == 0
    assert result.validation.canonical_hash == repeated.validation.canonical_hash
    assert [block.text for block in result.canonical.blocks if block.kind == "heading"] == [
        "Marketing Insight Data Semantics",
        "1. 개요",
        "2. 테이블 리스트",
        "3. 핵심 업무 용어",
        "4. 관계 및 집계 단위",
        "5. 업무 규칙 (비즈니스 로직)",
        "6. 지표 정의서",
        "7. 활용 시 주의사항",
        "8. 품질 기준",
    ]
    assert [
        block.row_count - 1 for block in result.canonical.blocks if block.kind == "table"
    ] == [5, 4, 15, 4, 10, 13, 5, 5]
    assert "개인 식별자, 실제 계정, 실제 매체명" in result.markdown
    assert "Semantics 是州" not in result.markdown
    assert "号h" not in result.markdown
    assert "<pre" not in result.markdown
```

The golden JSON stores the exact source hash, heading list, table dimensions, selected spacing
boundaries, required phrases, and forbidden corruption strings. Determinism is asserted by the two
independent replay hashes above rather than copying an implementation-generated hash into expected
data. Review the generated Markdown against the PDF once before committing the golden structure.

- [ ] **Step 5: Add property-style mutation and table-heavy regressions**

Use deterministic seeds `0..99` to split the same Korean text at different PDF object boundaries,
insert whitespace atoms, and perturb boxes within `±0.002`. Every mutation must conserve the same
non-whitespace sequence and canonical Markdown. The table-heavy fixture must contain at least 700
atoms, eight tables, a merged header, a blank cell, and a page continuation; it must never produce
a model response proportional to atom count.

- [ ] **Step 6: Add bounded performance assertions**

Measure in-process stage counters rather than wall-clock-only thresholds:

```python
def test_embedded_table_heavy_budget() -> None:
    metrics = run_benchmark("table-heavy-evidence.json")
    assert metrics.model_calls <= metrics.ambiguous_region_count
    assert metrics.max_request_candidate_count <= 5
    assert metrics.max_response_schema_bytes <= 2_048
    assert metrics.recomputed_region_count == 0  # second run uses the trusted cache
```

Record wall time and peak memory as non-flaky benchmark output. Establish blocking thresholds only
after three CI samples; until then, block on call/count/schema bounds and report time/memory trends.

- [ ] **Step 7: Run the regression and performance gates**

```bash
uv run --frozen pytest -q tests/integration/test_semantic_pdf_regressions.py tests/performance/test_semantic_pdf_performance.py
uv run --frozen python scripts/verify_issue_3_semantic.py --evidence tests/fixtures/semantic/issue-3-evidence.json --golden tests/fixtures/semantic/issue-3-golden.json
```

Expected: Issue #3 is `VERIFIED`, exact headings and eight table row counts match, no known OCR
garbage or raw HTML appears, and bounded-call assertions pass.

- [ ] **Step 8: Switch Actions to candidate mode with immediate rollback**

Set both validate and process job environments to:

```yaml
ARD_SEMANTIC_PDF_PIPELINE: ${{ vars.ARD_SEMANTIC_PDF_PIPELINE || 'candidate' }}
```

Validate the environment value against `legacy|shadow|candidate` in application code. Document the
rollback command and evidence to collect in `docs/operations/semantic-pdf-rollout.md`: set the
repository variable to `legacy`, rerun the failed workflow, retain both diagnostic artifacts, and
open a defect with source/configuration hashes. Do not delete the legacy path in this commit.

- [ ] **Step 9: Run the complete local gate once**

```bash
uv run --frozen pytest -q
uv run --frozen ruff check src tests scripts
uv run --frozen python -m ard_ossie.application.model_schema_verification --repository .
git diff --check
```

Expected: the complete test suite, Ruff, schema verification, and whitespace checks pass.

- [ ] **Step 10: Commit**

```bash
git add tests/fixtures/semantic/issue-3-evidence.json tests/fixtures/semantic/issue-3-golden.json tests/fixtures/semantic/table-heavy-evidence.json tests/unit/semantic/test_fixture_capture.py tests/integration/test_semantic_pdf_regressions.py tests/performance/test_semantic_pdf_performance.py scripts/verify_issue_3_semantic.py .github/workflows/ard-process.yml docs/operations/semantic-pdf-rollout.md
git commit -m "test: gate general semantic PDF cutover"
```

---

## Post-merge stabilization gate

After the implementation branch lands on `main`, rerun Issue #3 once with candidate mode and retain
the validation/process diagnostic artifacts. Candidate mode remains enabled only if the live run
has `VERIFIED`, character coverage `1.0`, zero missing/duplicate/degraded evidence, the reviewed
heading/table golden result, no raw HTML, and a stable repeated canonical hash.

Keep `ARD_SEMANTIC_PDF_PIPELINE=legacy` as the immediate rollback for one stabilization window of
at least 14 days and at least 20 representative PDFs. Track status, extraction mode, table count,
review rate, model calls, tokens, cache hits, stage latency, and peak memory. Open a separate cleanup
change to delete `semantic/repair.py`, the free-form repair models/reports, and the legacy mode only
after the window has zero invariant regressions and no unresolved rollback. This delayed deletion is
intentional: removing the rollback path before operational evidence would violate the approved
design.
