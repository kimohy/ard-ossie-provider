# Source document fidelity design

**Status:** Approved direction; written specification for review

**Date:** 2026-08-12

**Scope:** Source-faithful `data-semantic.md` and `data-dictionary.json` generation

## 1. Context

PR #5 exposed two publication-boundary defects:

1. Docling's default PDF processing split Korean words from a valid embedded text layer, for
   example `개인정보` into `개 인정보` and `유효성` into `유 효 성`.
2. `data-semantic.md` appended generated Metrics and Relationships sections that did not exist in
   the semantic PDF. `data-dictionary.json` was rendered after LLM suggestions had mutated table
   or column descriptions, so it could also contain values not present in the Excel workbook.

The selected behavior keeps required system metadata such as stable IDs, numeric versions, and
source paths, while making each source-facing artifact an authoritative projection of its own
source document. Generated semantic enrichments remain available through the Ossie model and
audit outputs.

## 2. Goals

- Preserve the semantic PDF's embedded text without inserting or deleting whitespace inside words.
- Publish only semantic-source content in `data-semantic.md`.
- Publish only Excel-source table and column values in `data-dictionary.json`.
- Retain deterministic system-owned IDs, numeric versions, and source paths in the dictionary and
  other canonical artifacts.
- Keep accepted LLM metrics and deterministic relationships in `ossie-model.json` and audit
  reports, without copying them into source-facing documents.
- Preserve scanned-PDF support through a safe fallback when no usable embedded text layer exists.
- Keep processing atomic and deterministic.

## 3. Non-goals

- Do not change the Ossie 0.1.1 schema or remove metrics and relationships from the Ossie model.
- Do not infer new dictionary descriptions, relationships, joins, grain, or formulas.
- Do not repair PDF text with language-specific spacing heuristics or a word dictionary.
- Do not visually reproduce PDF pagination, columns, fonts, or table borders in Markdown.
- Do not change `data-product.md` behavior.
- Do not increment product or table versions solely because source-facing rendering is corrected.

## 4. Considered approaches

### 4.1 Set Docling `force_backend_text`

This would be the smallest configuration change, but the installed and locked Docling 2.114.0
standard PDF pipeline does not consume this option. The option is used by Docling's VLM pipeline,
which would add a model dependency and still would not provide a source-only guarantee. This
approach is rejected.

### 4.2 Post-process known Korean spacing errors

A replacement table could repair the reported words, but it would be incomplete, language
specific, and capable of changing intentional spaces. It treats symptoms after lossy extraction.
This approach is rejected.

### 4.3 Embedded-text-first parsing with Docling fallback — selected

For a semantic PDF, read every page's embedded text through `pypdfium2`. Use it only when every
page has non-empty programmatic text. Normalize line endings and page separators, but preserve all
internal characters and spaces. If any page lacks embedded text, fall back to the existing Docling
conversion so scanned PDFs retain OCR support.

The actual PR #5 PDF has embedded text on all five pages. Direct extraction preserves
`개인정보` twice and `유효성` once, with zero occurrences of the reported split forms.

## 5. Source authority boundaries

### 5.1 Semantic document

`ParsedDocument.markdown` is the sole content input for `data-semantic.md`. The renderer writes the
normalized source text and one final newline. It does not add a generated title, Metrics section,
Relationships section, metric expression, or relationship definition.

The same parsed semantic text remains available to the LLM as grounding and to `ProductIR` as
instructions for Ossie compilation. Thus enrichments can still be generated and validated, but
they are not back-written into the source-facing semantic document.

### 5.2 Dictionary workbook

The cell-preserving Excel adapter remains authoritative for table names, column names, logical
names, data types, nullability, keys, formulas, comments, and descriptions. LLM table or column
description suggestions may remain in the raw audit response, but the publication pipeline does
not apply them to canonical table or column IR.

`data-dictionary.json` continues to add only the system fields needed for governance:

- `product_id` and `product_version`;
- `table_id` and `table_version`;
- source path and stable column IDs.

All other dictionary values originate from the Excel adapter.

### 5.3 Generated semantics

Accepted metrics and deterministic relationships continue to flow into:

- `generated/ossie-model.json`;
- Registry metric and relationship records.

Raw LLM metric suggestions continue to flow into `quality/llm-suggestions.json`, including
suggestions that trusted validation excludes from publication. Relationship validation findings
remain in the quality report.

They do not flow into `data-semantic.md` or `data-dictionary.json`.

## 6. Components and data flow

1. `pypdfium2` becomes an explicit locked runtime dependency rather than an undeclared transitive
   import through Docling.
2. `DoclingParser` detects a semantic PDF.
3. The embedded-text extractor opens it locally, extracts text per page, and records page-level
   evidence with the existing source hash and source-relative path.
4. When every page has usable text, the parser returns the joined text without invoking Docling's
   PDF layout/OCR pipeline.
5. When embedded text is absent on any page, the parser delegates to the existing Docling path.
6. The Excel adapter builds source-faithful table drafts.
7. LLM suggestions are validated and audited, but table and column description suggestions do not
   mutate those drafts.
8. `render_semantic_markdown` writes only `ProductIR.instructions`.
9. `render_dictionary_json` writes system metadata plus the source-faithful table and column IR.
10. The Ossie compiler independently receives accepted metrics and relationships as before.

## 7. Error handling and security

- PDF extraction is local only and never invokes a remote service.
- An unreadable or malformed PDF follows the existing conversion failure path; no partial artifact
  is promoted.
- A PDF with an empty page-level text layer falls back as a whole to Docling rather than mixing two
  extraction orders in one document.
- Embedded text is treated as source content, not executable Markdown or provider output.
- Existing source signature, path, size, source-hash, provider-output, SQL-safety, and atomic
  promotion checks remain unchanged.
- No source-facing artifact is edited after its quality hash is calculated.

## 8. Determinism and versioning

- Convert CRLF and bare CR line endings to LF.
- Strip only page-edge whitespace; do not collapse internal whitespace.
- Join pages with exactly one blank line and write exactly one final newline.
- Keep page order and embedded text order returned by PDFium.
- Sort dictionary entities through the existing stable ID and ordinal rules.
- Rendering changes alone do not change a source hash and do not require a numeric version bump.

## 9. Test strategy

- Parser unit tests prove embedded text is selected only when every PDF page has text, preserves
  Korean words exactly, creates page-level evidence, and bypasses the Docling converter.
- Parser fallback tests prove any empty page delegates to the existing converter.
- Renderer tests prove `data-semantic.md` equals source text and contains no generated Metrics or
  Relationships sections even when the IR contains both.
- Pipeline tests provide LLM table and column description suggestions and prove the generated
  dictionary retains the literal Excel values while the raw suggestions remain auditable.
- Pipeline tests prove accepted metrics still appear in the Ossie model but not in semantic or
  dictionary artifacts.
- The actual PR #5 PDF is parsed during acceptance verification to assert `개인정보` and `유효성`
  are present and the split forms are absent.
- The full pytest, Ruff, schema, workflow, package, checksum, and secret-scan gates run before
  publication.

## 10. Acceptance criteria

- `data-semantic.md` contains the semantic PDF content and no generated Metrics or Relationships
  appendix.
- `data-semantic.md` contains `개인정보` and `유효성`, and does not contain `개 인정보` or
  `유 효 성`, for the current PR #5 input.
- Every non-system value in `data-dictionary.json` matches the Excel adapter output.
- Stable product, table, and column IDs; numeric versions; and source paths remain present.
- Accepted metrics and relationships remain schema-valid in `ossie-model.json` and auditable in
  quality outputs.
- No generated artifact or Registry record is edited manually.
- PR #5 is regenerated only after the processor fix is reviewed and merged into `main`.
