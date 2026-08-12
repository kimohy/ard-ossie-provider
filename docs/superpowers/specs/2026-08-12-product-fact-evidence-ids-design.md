# Product fact evidence ID design

**Status:** Approved

**Date:** 2026-08-12

**Scope:** Structured product-fact citations in the LLM extraction boundary

## 1. Context

The normalized data-product pipeline requires every LLM-derived product fact to cite an exact
`Evidence` object collected from the product HTML. The current structured response asks the model
to reproduce the complete source hash, role, locator, and excerpt. The validator correctly rejects
any reconstructed object that differs from the parser-produced evidence, but exact copying is a
probabilistic model task. GitHub Actions run `31562755748` reached the protected LLM processor and
failed with `LLM_PRODUCT_FACT_EVIDENCE_UNKNOWN` when a returned citation was outside that exact
evidence set.

Authentication, model access, source validation, and writeback all completed normally. The fault
is therefore at the structured-response boundary: an untrusted model is being asked to duplicate a
trusted compound object before the trusted validator can recognize it.

## 2. Goals

- Keep the existing fail-closed grounding boundary for product facts.
- Remove compound evidence copying from the model's responsibilities.
- Let trusted code recover the original parser-produced `Evidence` without approximation.
- Reject missing, unknown, and repeated evidence identifiers deterministically.
- Preserve the final `ProductIR`, quality audit, Markdown, and Ossie artifact formats.
- Leave semantic suggestion and metric citation contracts unchanged.

## 3. Non-goals

- Do not weaken exact product-HTML grounding or accept invented excerpts or locators.
- Do not infer or repair an unknown evidence reference.
- Do not broaden the change to semantic suggestions or metrics, whose current validation did not
  cause this failure.
- Do not persist evidence IDs in generated artifacts or treat them as stable across processing
  runs.
- Do not change product or table versioning.

## 4. Selected design

For each structured extraction request, trusted code enumerates the accepted
`product_document.evidence` collection in parser order. It assigns opaque request-local IDs using
the closed format `product-evidence-NNNNNN`, starting at one. Each prompt evidence entry retains
its existing source hash, role, locator, and excerpt and gains its assigned `evidence_id`.

The product-fact response schema replaces the compound `evidence` array with a non-empty
`evidence_ids` string array. Product fact suggestions carry only these IDs. Semantic suggestions
and metrics continue to return their existing compound evidence arrays.

Before product-fact policy validation, trusted code builds the same ID-to-`Evidence` catalog and
resolves every returned ID to the original object. The accepted `ProductFactIR` therefore contains
the parser-produced evidence, never a model-reconstructed copy.

## 5. Identifier contract

- IDs are deterministic within a request because they follow parser evidence order.
- IDs are unique by construction and have six decimal digits.
- IDs are opaque references; their numeric component has no semantic meaning.
- The prompt includes IDs only for accepted product evidence. Internally excluded AI-generated
  evidence is not assigned or exposed as citable product evidence.
- IDs are not written to the IR, audit, registry, Markdown, or Ossie outputs.

The response schema requires at least one ID for every product fact. The validator rejects a
repeated ID in one fact with `LLM_PRODUCT_FACT_EVIDENCE_ID_DUPLICATE` and an absent catalog entry
with the existing `LLM_PRODUCT_FACT_EVIDENCE_UNKNOWN` code. It does not select a nearby ID or
match on text.

## 6. Data flow

1. Docling returns ordered accepted and excluded product evidence.
2. Trusted pipeline code creates the request-local accepted-evidence catalog.
3. The user prompt serializes accepted product evidence with `evidence_id` alongside the original
   evidence fields.
4. The provider returns each product fact with `evidence_ids`.
5. Pydantic validates the closed structured response shape.
6. Trusted code rejects duplicate or unknown IDs and resolves known IDs to original `Evidence`
   objects.
7. Existing role, source hash, excerpt, AI-generated-content, confidence, deduplication, and
   singleton-conflict policies run against those resolved objects.
8. Accepted evidence continues into `ProductFactIR` and all existing artifacts unchanged.

## 7. Error handling and security

The catalog is derived only from trusted parser output for the current product document. The
model cannot submit a new locator, excerpt, role, or source hash through a product fact. Unknown
IDs and duplicates fail the run before any generated artifact is published. Missing
`evidence_ids`, an empty list, the legacy `evidence` property, or extra fields fail structured
schema/model validation.

The existing defensive checks on resolved evidence remain in place. They guard future refactors
and ensure a catalog can never make non-product, wrong-source, blank-excerpt, excluded, or
AI-generated evidence publishable.

## 8. Compatibility

This is an intentional provider-response contract change deployed together with its prompt,
schema, Pydantic model, resolver, and tests. There is no stored response format to migrate. The
input source files, `ProductIR`, public quality evidence, generated Markdown, Ossie model, and CLI
surface remain compatible.

## 9. Test strategy

- Schema tests require `evidence_ids`, reject the legacy `evidence` property, and preserve the
  existing compound evidence schema for suggestions and metrics.
- Prompt tests prove accepted product evidence receives deterministic IDs while excluded evidence
  is absent.
- Validation tests prove a known ID resolves to the exact original `Evidence` object.
- Validation tests prove unknown and repeated IDs fail with stable error codes.
- Existing product-fact policy tests are updated to express citations by ID and must retain their
  role, source, excerpt, AI-generated, ordering, confidence, and singleton behavior.
- Integration tests exercise a provider response using evidence IDs and verify that the public
  quality audit still contains full resolved evidence.
- Full pytest, Ruff, workflow-YAML, isolated model-schema, static verifier, and package-build gates
  run before publication.

## 10. Acceptance criteria

- A product fact can be accepted without the provider reproducing a locator, excerpt, role, or
  source hash.
- Every accepted fact resolves only to evidence that appeared in the current request catalog.
- Unknown, missing, empty, duplicate, excluded, or legacy product-fact citations fail closed.
- Semantic suggestions and metrics retain their current citation behavior.
- Generated product documentation and quality evidence contain the original full citations and no
  request-local IDs.
- Issue #3 can complete processing without `LLM_PRODUCT_FACT_EVIDENCE_UNKNOWN` caused by compound
  citation-copy drift.
