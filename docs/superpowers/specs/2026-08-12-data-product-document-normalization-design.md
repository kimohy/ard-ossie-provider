# Data Product document normalization design

**Status:** Draft for review; design direction accepted

**Date:** 2026-08-12

**Scope:** `data-product.md` generation from the product HTML source

## 1. Context

The current pipeline converts the entire product HTML page with Docling and appends the
result below `## Parsed source`. In PR #5 this preserves useful submitted values, but it also
publishes portal navigation, buttons, authoring instructions, attachment controls, policy
notices, empty form labels, chatbot content, and other page chrome. The LLM currently proposes
only a product description, synonyms, table and column descriptions, and metrics; it does not
produce a normalized data-product document.

The pipeline already creates `generated/ossie-model.json` in the same processing run as
`data-product.md`. This design does not rename, defer, or otherwise change that Ossie artifact.

## 2. Goals

- Publish a concise, consistently structured `data-product.md`.
- Preserve only submitted facts that have evidence in the product HTML source or explicit
  product configuration.
- Remove portal boilerplate instead of copying or lightly filtering the full parsed page.
- Omit each unavailable fact and omit an entire section when it has no facts.
- Keep the output deterministic after structured facts have been accepted.
- Preserve the existing Ossie 0.1.1 compilation and artifact location.

## 3. Non-goals

- Do not redesign `data-semantic.md`, `data-dictionary.json`, or the Ossie 0.1.1 schema mapping.
- Do not infer missing ownership, access, freshness, SLA, quality, or usage information.
- Do not publish `미제공`, `N/A`, blank headings, empty tables, or placeholder text.
- Do not treat a field explicitly labeled as an automatically generated summary as user-authored
  documentation.
- Do not introduce product or table version changes solely because Markdown formatting changes.

## 4. Considered approaches

### 4.1 Rule-based filtering of parsed Markdown

Remove known button labels, notices, and menu text from the Docling output, then publish the
remaining Markdown. This is deterministic, but it is coupled to Korean portal wording and still
cannot reliably distinguish a field label, a field value, and authoring guidance.

### 4.2 Free-form LLM rewrite

Ask the LLM to rewrite the entire page as Markdown. This produces readable text, but section
shape, omission rules, and factual grounding cannot be enforced reliably.

### 4.3 Strict fact extraction plus deterministic rendering — selected

Extend the existing structured LLM response with evidence-backed product facts. Validate those
facts in code, build a typed product-document IR, and render it with a fixed template. The LLM
performs semantic selection and concise normalization; code controls the accepted vocabulary,
evidence boundary, section order, omission behavior, and final Markdown.

This approach best satisfies the requirement to organize user-entered content rather than merely
parse it.

## 5. Canonical product facts

The structured response accepts only the following fact kinds. A fact contains a kind, a concise
value, confidence, and one or more source evidence references.

| Section | Fact kinds | Multiplicity |
|---|---|---:|
| Overview | `description`, `purpose` | one each |
| Data source | `domain`, `data_type`, `storage_location` | one each |
| Data source | `source_system`, `source_name` | many |
| Tags | `tag` | many |
| Access and security | `access`, `security_classification` | one each |
| Ownership | `owner`, `contact`, `consumer` | many |
| Freshness and SLA | `refresh_schedule`, `freshness`, `sla` | one each |
| AI readiness and quality | `ai_readiness` | one |
| AI readiness and quality | `quality` | many |
| Constraints and notes | `constraint`, `related_link` | many |

Unknown kinds are invalid. Empty or whitespace-only values are invalid. Identical repeated facts
are deduplicated; conflicting singleton facts fail closed instead of selecting one silently.

## 6. Extraction and evidence contract

The existing provider call remains a single strict Structured Outputs request. Its response gains
a required `product_facts` array in addition to the current semantic suggestions and metrics.

The system instruction requires the provider to:

- extract only explicit submitted values from the product HTML;
- preserve meaning while normalizing whitespace and removing instructional phrasing;
- ignore navigation, search, menus, buttons, attachment actions and sizes, privacy notices,
  authoring hints, review-only empty fields, next/previous links, footer text, and chatbot content;
- ignore fields with no entered value;
- ignore page fields explicitly labeled as AI-generated summaries;
- return no fact when the source does not support it;
- cite product-HTML evidence for every returned fact.

HTML evidence must carry the product source hash and a non-empty excerpt. When Docling exposes an
HTML item without page provenance, the parser records the item index, hierarchy level, document
path, and a bounded text excerpt. This prevents the current document-only fallback from making
all page text share one undifferentiated evidence reference.

Validation requires every product fact to cite the product HTML source and rejects unknown source
hashes, absent excerpts, unknown kinds, malformed values, and conflicting singleton facts. Facts
below the existing confidence threshold are not included in the IR.

## 7. Typed IR and rendering

`ProductIR` receives an ordered collection of validated product facts. Raw
`product_document_markdown` is no longer a renderer input and `## Parsed source` is removed from
the template.

The renderer follows the canonical section order from Section 5. Within a section it follows the
canonical fact-kind order; repeatable values use a stable case-insensitive sort. It renders a
section only when at least one accepted fact belongs to it. The dataset table remains mandatory
because it comes from the validated dictionary and registry, not from optional product-page text.

For provider-free local processing, the document contains product identity, an explicit
configuration description when present, and the dataset table. It does not copy raw parsed HTML
as a fallback and does not synthesize missing sections.

## 8. Data flow

1. Docling parses the HTML and captures item-level evidence excerpts.
2. The existing LLM request returns semantic suggestions, metrics, and product facts under one
   strict schema.
3. The pipeline validates fact kind, value, confidence, source hash, excerpt, and singleton
   conflicts.
4. Accepted facts become typed `ProductIR` content.
5. Jinja renders only non-empty canonical sections and the validated dataset table.
6. The same run compiles and writes `generated/ossie-model.json` exactly as before.

The product's source hashes already represent source-content changes. A renderer-only pipeline
upgrade does not itself require a product version increment.

## 9. Error handling

- Malformed structured output retains the existing normalized provider-output failure path.
- Unsupported fact kinds, invalid evidence, or conflicting singleton facts fail the run instead
  of publishing potentially invented or ambiguous documentation.
- Low-confidence optional facts are omitted without creating placeholder sections.
- If no optional fact survives validation, the identity metadata and dataset table are still
  generated.
- The process remains atomic: validation failure does not replace previously generated artifacts.

## 10. Test strategy

- Schema tests prove `product_facts` is strict, required, and limited to known fact kinds.
- Parser tests prove HTML items without page provenance receive bounded item-level excerpts.
- Validation tests cover unknown kinds, missing excerpts, wrong source roles and hashes,
  deduplication, low-confidence omission, and singleton conflicts.
- Renderer golden tests cover a complete document and a sparse document; the sparse document
  must contain no empty headings or placeholders.
- A regression test feeds noisy portal-like Markdown and verifies that menu text, authoring hints,
  attachment controls, privacy notices, blank fields, AI-generated summary text, and chatbot text
  do not appear in `data-product.md`.
- Integration tests confirm that `data-product.md` and `ossie-model.json` are created together and
  that the Ossie model remains schema-valid.
- The full test, Ruff, workflow-YAML, and package-build gates run before publication.

## 11. Acceptance criteria

- `data-product.md` contains no `## Parsed source` section.
- The PR #5 product values are organized under the canonical sections.
- No portal navigation, buttons, instructional notes, attachment UI, privacy warning, empty form
  field, footer, or chatbot content is published.
- A missing value causes its fact and, when applicable, its whole section to be absent.
- Every LLM-derived product fact has product-HTML evidence with a non-empty excerpt.
- Dataset rows remain derived from the dictionary/registry and stable by table ID.
- `products/{product_key}/generated/ossie-model.json` continues to be written during the same
  processing run.
