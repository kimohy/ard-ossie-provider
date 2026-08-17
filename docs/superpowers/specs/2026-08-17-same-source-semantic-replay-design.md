# Same-Source Semantic Replay Design

**Status:** Accepted

**Date:** 2026-08-17

## Problem

PR #49 processes the same semantic PDF as product `500138301` but selected a different Korean
spacing candidate. The source text and the verified existing output contain `정의서이며`; the new
output contains `정의 서이며`. Character coverage, global canonical invariants, and model
confidence still marked the result `verified` and `publishable` because whitespace-only changes do
not alter the conserved character sequence.

The processor already supports trusted decision reuse for an existing version of the same product.
A new product has no product-local quality history, so it adjudicates the identical source again and
can publish a different canonical semantic document.

## Goals

- Make identical semantic sources deterministic across products when their full semantic replay
  identities are compatible.
- Reuse only audit artifacts read from the trusted base revision.
- Make a second compatible product perform zero semantic adjudication model calls.
- Reject conflicting trusted baselines and compatible replays that produce different canonical
  Markdown.
- Preserve the current behavior when no compatible trusted baseline exists or the semantic policy
  identity changed.

## Non-goals

- Do not add a one-off Korean dictionary rule for `정의서`.
- Do not declare every same-source output identical across parser, prompt, provider, model, schema,
  or policy changes.
- Do not edit generated artifacts or quality reports by hand.
- Do not trust product artifacts from the candidate branch as replay authority.
- Do not merge or reprocess product PR #49 until the code fix is merged to `main`.

## Replay contract

A semantic replay identity consists of:

- the semantic source hash;
- the complete sorted set of decision identities, each containing decision type, region ID,
  candidate-set ID, and request hash; and
- the identities already committed to each request hash: evidence, prompt/schema, provider, model,
  adjudication policy, and bounded page-crop inputs.

Two products are replay-compatible only when this full identity matches. Product ID, product key,
and display metadata are intentionally excluded because they do not participate in semantic PDF
parsing.

For a compatible identity, canonical semantic Markdown must be byte-identical. Line wrapping,
spaces, and final newline are part of the contract.

## Trusted baseline catalog

`ProcessingService` builds a catalog from the exact remote base SHA before invoking the processor.
It reads `registry/indexes/product-keys.json` at that revision to enumerate products. For the current
product first, then other product keys in lexical order, it reads only revision-pinned artifacts:

- `generated/source-manifest.json`;
- `generated/data-semantic.md`;
- `quality/quality-report.json`;
- `quality/decision-report.json`; and
- `quality/validation-report.json`.

An entry is admitted only when all of the following hold:

- the source manifest records the current semantic source hash;
- `quality-report.json` records no hard errors and its generated/quality artifact hashes match the
  exact bytes read from the base revision;
- `validation-report.json` has `status=verified` and `publishable=true`;
- decision and validation report source hashes match the semantic source hash; and
- every artifact parses through its current strict model.

Missing artifacts make that product ineligible. A present but malformed, hash-mismatched, or
internally inconsistent candidate is a trust failure, not a silent skip. The candidate worktree is
never used to establish replay authority.

Catalog entries are grouped by full replay identity. If two verified base products have the same
identity but different canonical Markdown bytes, processing fails with
`SEMANTIC_REPLAY_BASELINE_CONFLICT`. Identical duplicates converge to one deterministic catalog
entry.

## Processing data flow

1. Securely scan the current candidate sources and obtain the semantic source hash.
2. Read and validate matching baseline entries from the exact base revision.
3. Pass the flattened trusted decisions from those entries to the existing `CandidateAdjudicator`.
4. Let the existing trusted-decision matcher revalidate candidate membership, request hash,
   provider/model identity, attempts, generated candidates, and deterministic invariants.
5. Assemble the canonical semantic document normally.
6. Compute the current full replay identity from the current decision report.
7. If the catalog contains that identity, compare the current canonical Markdown bytes with the
   catalog baseline.
8. On a difference, add hard finding `SEMANTIC_SOURCE_REPLAY_MISMATCH`, write failure diagnostics,
   and stop before generated or Registry promotion.
9. If no compatible identity exists, continue through the existing adjudication and publication
   rules.

The catalog may contain older policy identities, but they cannot be reused because the existing
request-hash matcher rejects them. A legitimate semantic policy change therefore takes the normal
fresh-adjudication path instead of being frozen to an obsolete rendering.

## Components

### Trusted revision loader

Application-layer helpers own base-revision enumeration, artifact hash verification, and catalog
conflict detection. They use `GitPort.read_bytes_at` exclusively and return a typed, immutable
semantic replay catalog.

### Replay identity and catalog models

Semantic-layer models own replay identity derivation and canonical baseline grouping. Identity
derivation is deterministic and independent of product metadata.

### Pipeline invariant

`process_product` accepts the optional trusted catalog. It supplies catalog decisions to the
parser and evaluates canonical equality after semantic parsing. The invariant becomes an ordinary
hard quality finding so the failed run retains redacted decision, application, validation, and
failure reports.

## Error handling

- No eligible same-source baseline: existing behavior.
- Compatible baseline: trusted decisions are reused and canonical bytes must match.
- Baseline artifact missing: product is ineligible when the quality tree is wholly absent.
- Partial, malformed, hash-mismatched, or source-inconsistent quality tree:
  `SEMANTIC_REPLAY_TRUST_MISMATCH`.
- Same replay identity with different verified canonical bytes:
  `SEMANTIC_REPLAY_BASELINE_CONFLICT`.
- Compatible replay produces different canonical bytes:
  `SEMANTIC_SOURCE_REPLAY_MISMATCH`.
- Provider/model/prompt/schema/policy identity changed: trusted matcher rejects old decisions and
  the ordinary adjudication path runs.

Errors and diagnostics must not expose source text, raw prompts, provider responses, credentials,
or signed URLs.

## Testing strategy

Unit tests cover:

- deterministic replay identity independent of product metadata;
- exact-base product enumeration and artifact/hash validation;
- rejection of partial and tampered baseline trees;
- convergence of identical duplicate baselines;
- conflict detection for equal identities with different canonical bytes; and
- incompatible request identities remaining available only as nonmatching trusted inputs.

Integration tests cover:

- two products using the Issue #3 PDF source: the second product reuses the verified baseline,
  performs zero semantic adjudication provider calls, and emits byte-identical
  `data-semantic.md`;
- a provider that attempts to select the `정의 서이며` candidate cannot change the compatible
  replay output;
- a forced compatible-output mismatch produces `SEMANTIC_SOURCE_REPLAY_MISMATCH`, writes failure
  diagnostics, and does not promote generated or Registry state; and
- a changed prompt/provider/policy request identity follows fresh adjudication rather than the old
  baseline.

The complete test suite, Ruff, model/schema verification, and product workflow contract tests must
pass.

## Rollout

1. Implement the loader, typed catalog, replay identity, pipeline invariant, and tests on a focused
   code branch.
2. Run independent review and the full local gate.
3. Push the code branch, open a code-only PR, and wait for all protected checks.
4. Merge the code PR to `main`.
5. Remove and reapply `ard:approved` on Issue #46 so the trusted `base_sync` path merges current
   `main`, clears allowed derived output, and reprocesses PR #49.
6. Require the new PR #49 head to contain current `main`, produce `정의서이며`, and have successful
   exact-head `ard/quality-gate` and `ard/changeset` statuses before marking it ready.

## Success criteria

- Same source plus the same full replay identity always produces byte-identical canonical semantic
  Markdown across products.
- The second compatible product makes zero semantic adjudication model calls.
- The `정의 서이며` regression is caught automatically.
- Incompatible semantic policy identities are not silently frozen to old output.
- PR #49 is regenerated by the trusted workflow and becomes mergeable only on the corrected exact
  head.
