# Semantic Heading Hierarchy Design

## Problem

The candidate semantic PDF pipeline currently interprets the numeric value at the
start of a heading as the Markdown heading level. This makes `1. 개요` an H1,
`2. 테이블 리스트` an H2, and later top-level sections H3 through H6. The section
number identifies sequence, not hierarchy, so the rendered outline becomes
progressively deeper even though these sections are peers.

## Scope

This change corrects numbered heading hierarchy only. Table-cell spacing repair
will follow separately. The renderer's conservative CommonMark escaping remains
unchanged because it prevents untrusted source text from creating Markdown or HTML
structure and does not expose backslashes in rendered output.

## Heading-Level Rule

Numbered headings derive their Markdown depth from the number of hierarchy
segments, not the value of any segment:

- `1.`, `2.`, `6.`, and `8.` are top-level document sections and render as H2.
- `3.1` renders as H3.
- `3.1.2` renders as H4.
- Deeper numbering is capped at H6.
- Repeated or continued headings such as `3. 핵심 업무 용어 (계속)` remain at
  the same level as the original section.
- Unnumbered headings keep the existing hint/geometry behavior.

H1 remains available for document titles. The heading classifier continues to
use source atoms unchanged; only structural metadata changes.

## Data Flow and Safety

`build_block_candidate_sets` recognizes a numbered heading and passes the matched
number to a pure heading-level helper. The helper counts non-empty dot-separated
segments and returns `min(6, segment_count + 1)`. Candidate adjudication, canonical
assembly, and Markdown rendering continue to consume the resulting level through
their existing interfaces.

No source character, block order, table structure, or Markdown escaping is
modified. Existing source-coverage and canonical validation remain the safety
boundary.

## Verification

Tests will verify behavior through real candidate construction rather than source
inspection:

1. Peer headings numbered `1`, `2`, `6`, and `8` all receive level 2.
2. Nested headings `3.1` and `3.1.2` receive levels 3 and 4.
3. Continued peer headings retain level 2.
4. The Issue #3 replay renders a flat H2 section sequence instead of increasing
   heading depth.
5. Existing CommonMark escaping and semantic pipeline tests remain unchanged and
   pass.

## Success Criteria

- Issue #3 has document-title H1 headings followed by peer H2 numbered sections.
- No top-level numbered section is mapped to its ordinal value.
- Nested numbering still produces a valid bounded hierarchy.
- The complete repository test and lint gates pass.
