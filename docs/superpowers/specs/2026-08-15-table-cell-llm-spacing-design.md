# Table-Cell LLM Spacing Repair Design

## Problem

The semantic PDF pipeline correctly preserves table grids and source characters, but Issue #3
still publishes malformed cell text such as `참 조`, `시 뮬레 이 션`,
`marketing_campaign.ca id mpaign_`, and `marketing_delivery.spe nd_units`.

The cause is architectural. `pipeline_v2.py` excludes every table region from spacing candidate
construction. Table adjudication chooses only among prebuilt `TableCandidate` layouts, while
canonical assembly prefers each selected cell's existing `rendered_text`. A low-confidence table
decision can therefore continue as deferred review, but no later stage is able to generate and
verify corrected cell whitespace.

## Scope

This change repairs whitespace inside character-owned table cells after table structure is
selected. It does not change extracted characters, table geometry, row or column spans, cell
ownership, block order, non-table spacing behavior, or CommonMark escaping.

The raw Markdown escape policy remains unchanged. It protects the document from source-authored
Markdown/HTML structure, and the escapes are consumed by CommonMark rendering.

## Decision

Use one bounded composite spacing decision per affected table, with hard cell boundaries and an
explicit allowlist of mutable character boundaries.

```text
evidence + structure hints
  -> build and select table structure
     -> exact bbox ownership + per-cell character conservation succeeds
        -> select structure deterministically even when cell spacing is unresolved
     -> inspect selected cells
        -> no deterministic defect or scorer disagreement: no spacing decision
        -> suspicious cells exist:
           -> build one row-major composite spacing candidate set
           -> mark every cell boundary as an immutable hard break
           -> mark only suspicious-cell internal boundaries mutable
           -> ordinary high-confidence candidate: apply
           -> low confidence or deterministic defect:
              -> one whitespace-only LLM generation
              -> deterministic boundary and character validation
              -> one independent LLM verification
              -> accepted: project changed boundaries into affected cells
              -> rejected/unavailable: apply deterministic fallback,
                 continue as review_pending, and persist review debt
```

One table-level decision preserves neighboring cell context while bounding model calls to at most
three per affected table. Per-cell calls would lose context and scale with cell count. A flattened
table-region call would permit whitespace to migrate between cells and is therefore prohibited.

## Structural Selection Before Spacing

`cell_spacing_integrity` is a repair-routing signal, not a table-structure proof. The invariant
table proof consists of:

- exact atom-to-cell bbox ownership;
- exact per-cell non-whitespace character multiset;
- exact structure-hint text character conservation.

The adjudicator may select the unique structure-proven table deterministically even when its
`cell_spacing_integrity` is `0.0`. This removes the current low-confidence table-layout calls for
Issue #3. If more than one distinct structure-proven candidate exists, ordinary bounded
adjudication remains in force.

The pipeline adjudicates table structure before building table spacing candidates. The selected
table is the only authority for cell order, atom ownership, and repair scope.

## Suspicious-Cell Routing

A cell enters the mutable scope when at least one of these conditions holds:

- deterministic validation finds a split protected token, identifier, date, URL, email, unit, or
  punctuation boundary;
- the Korean spacing scorer produces a character-conserving alternative and the source/hint has
  an existing fragmentation signal;
- the selected table reports unresolved cell spacing and a character-conserving scorer proposal
  disagrees with the cell rendering.

A scorer disagreement alone does not authorize a change in an otherwise clean table. Empty cells,
single-character cells, formulas without a defect, and cells whose proposals change characters
remain immutable.

Protected-token whitespace is also normalized by deterministic candidate construction. This
guarantees a defect-free fallback for known identifier/date/unit patterns without weakening the
global fallback validator.

## Composite Candidate Contract

The composite candidate concatenates non-empty cells in stable row-major coordinate order. Each
cell contributes its exact ordered non-whitespace atom IDs. Adjacent cells are separated by a
`hard_break` boundary; the separator is layout metadata and does not create a source character.

`SpacingCandidate` gains an optional immutable `mutable_boundary_indexes` tuple:

- `None` preserves the current non-table behavior, where every non-hard boundary may change.
- A table composite stores only internal boundary indexes belonging to suspicious cells.
- Every index is unique, in range, and cannot identify a hard-break cell boundary.
- All candidates in one set have the same character sequence, atom order, hard boundaries, and
  mutable-boundary allowlist.
- Candidate identity includes the allowlist so a decision cannot be replayed under a different
  repair scope.

`build_generated_candidate` rejects a generated rendering when:

- the non-whitespace Unicode sequence differs;
- any hard cell boundary differs;
- any boundary outside `mutable_boundary_indexes` differs from the anchor;
- a protected token is split;
- control whitespace is introduced.

Generation and verification requests expose the mutable indexes and bounded row-major cell
segments. Provider output remains a single rendered string, but it cannot expand its authority
beyond the supplied scope.

## Canonical Projection

When a table-scoped spacing decision exists, canonical assembly projects its boundary states into
each selected cell. It does not use the composite rendering as table text directly. The existing
canonical table builder then derives `block.text` from the projected cells.

Cells outside the mutable scope must have byte-identical rendered whitespace before and after the
decision. Cell IDs, coordinates, atom IDs, row/column counts, and source coverage must also remain
identical.

## Low-Confidence and Failure Behavior

An accepted generated candidate uses the existing `generated` decision path. Low confidence,
verification rejection, timeout, malformed provider output, or unavailable provider uses the
existing defect-free deterministic fallback and records `deferred_review`.

Deferred table spacing must not stop conversion or Draft PR writeback. The application report and
semantic review artifact record:

- table region and candidate-set IDs;
- selected fallback identity;
- generation and verification attempts;
- confidence values and validation codes;
- `review_pending` publication state.

No unrestricted cell text or provider response is added to public diagnostics; existing hashed or
bounded diagnostic snapshots remain authoritative. Release verification can continue to require
`verified`, while conversion itself continues with explicit review debt.

## Verification Strategy

Only conversion-relevant tests are added:

1. Candidate construction proves cell boundaries are hard, clean cells are immutable, and only
   suspicious-cell internal boundaries are mutable.
2. Generated-candidate validation rejects character mutation, cell-boundary movement, clean-cell
   edits, and protected-token splits.
3. Canonical projection changes only the selected cell whitespace while preserving grid and atom
   ownership exactly.
4. A low-confidence provider result applies the deterministic fallback, returns
   `deferred_review`, and leaves a complete audit record without stopping the pipeline.
5. Issue #3 evidence replay asserts corrected visible cells including `참조`, `시뮬레이션`,
   `marketing_campaign.campaign_id`, and `marketing_delivery.spend_units`; the two already exact
   golden tables remain byte-for-byte unchanged.
6. Issue #3 replay performs no table-layout model calls, uses at most three model calls per affected
   table, remains deterministic on cache replay, and preserves 100% character coverage.
7. After deterministic tests pass, the configured local LLM is exercised against the Issue #3
   evidence replay. The resulting canonical cell values, confidence decisions, and review artifacts
   are compared with the golden conversion contract; provider text is not persisted.

## Success Criteria

- No known Issue #3 table-cell fragmentation remains in canonical Markdown.
- All table grids, cell coordinates, atom allocations, characters, and source coverage are
  unchanged.
- Clean cells cannot be modified by LLM output.
- Low-confidence or failed repair never aborts document conversion.
- Deferred repair is visible and replayable through durable audit artifacts.
- Model calls are limited to affected tables and cached decisions require zero calls.
- Existing Markdown escaping and rendered CommonMark behavior remain unchanged.
