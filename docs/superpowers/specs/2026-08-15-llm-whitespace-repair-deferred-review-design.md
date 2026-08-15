# LLM Whitespace Repair and Deferred Review Design

## 1. Context

The current semantic PDF pipeline can ask an LLM only to select an existing candidate. When the
primary answer is below `minimum_model_confidence=0.80`, confidence recovery asks the same model
to reconsider the same closed candidate set. It cannot create a better rendering. A terminal
`review_required` decision then makes the entire canonical document non-publishable, so source
validation stops before Markdown generation and Draft PR writeback.

The live Issue #3 rerun exposed both limitations:

- GitHub Actions run: <https://github.com/kimohy/ard-ossie-provider/actions/runs/31858948735>
- failing set: `candidate_set_adf8c3c96f06d140`
- region: `region_05d599dd01a206bb`
- decision type: `spacing`
- primary: `candidate_dd108396f82597d3`, confidence `0.72`
- recovery: the same candidate, confidence `0.76`
- terminal code: `LLM_CONFIDENCE_RECOVERY_EXHAUSTED`
- global character coverage: `1.0`, with no missing or duplicate characters
- outcome: `review_required`, `publishable=false`, no application record

The candidate selected twice still contains identifier damage such as spaces adjacent to
underscores. Other candidates in the same set contain Korean word splits such as `식 별` and
`채 널`, punctuation joins, or dense line flattening. Repeating the vote cannot repair a set in
which every candidate has a material defect.

A second live choice in `candidate_set_6d08c750276170e3` also differs from the public approved golden
candidate while reporting high confidence. Therefore confidence and outcome flags alone are not
sufficient acceptance criteria: the exact applied rendering and protected-token integrity must
be checked.

## 2. Goals

- When a spacing decision is below the configured confidence threshold, let the LLM propose one
  new whitespace-only rendering instead of merely repeating a closed-set vote.
- Reject any proposal that changes a non-whitespace Unicode code point, its order, case, digit,
  or punctuation.
- Give generation and verification enough Korean morphology, identifier, line, and table-cell
  context to avoid the defects observed in Issue #3.
- Independently verify a generated proposal before treating it as a confident semantic repair.
- If semantic confidence remains low, apply a conservative deterministic fallback and continue
  document conversion and Draft PR writeback in a `review_pending` state.
- Persist a durable, privacy-conscious review artifact so a human can improve and replay the
  decision later.
- Keep release blocked until deferred review is resolved, without treating handled uncertainty
  as a conversion failure.
- Keep model calls and implementation scope bounded so the feedback loop remains fast.
- Use the exact Issue #3 failure as the first acceptance case, then generalize through invariants.

## 3. Non-goals

- The LLM may not rewrite wording, normalize spelling, replace punctuation, alter numbers, or
  reconstruct table geometry.
- This change does not lower the global confidence threshold.
- Two low-confidence answers do not become trusted merely because they agree.
- This change does not add a review web application or a broad GitHub-label taxonomy.
- It does not redesign PDF extraction, page segmentation, OCR, or non-PDF ingestion.
- It does not add tests for model defaults, getters, serialization trivia, or mocked branches
  that do not prove a document-conversion outcome.

## 4. Decision

Replace repeated low-confidence spacing selection with a bounded repair protocol. Preserve the
existing primary selection call because it avoids generation when one existing candidate is
already adequate.

```text
candidate set
  -> deterministic clear winner
      -> select, no LLM
  -> ambiguous
      -> primary closed-set LLM selection
          -> confidence >= 0.80
              -> select existing candidate
          -> confidence < 0.80 and decision type is spacing
              -> generate one whitespace-only repair candidate
              -> deterministic invariant validation
                  -> invalid: reject proposal
                  -> valid: one independent verification call
                      -> generator and verifier confidence >= 0.80
                         and verifier selects generated repair
                          -> apply repair
                      -> otherwise: reject proposal
              -> rejected or still uncertain
                  -> apply deterministic fallback
                  -> mark deferred review
          -> confidence < 0.80 and another decision type
              -> apply the safest valid deterministic candidate
              -> mark deferred review
  -> canonical assembly and global validation
      -> hard integrity failure: failed, stop publication
      -> no deferred decisions: verified
      -> deferred decisions: review_pending, continue Draft output
```

The maximum semantic model budget for a low-confidence spacing decision is three calls: primary
selection, repair generation, and independent verification. This replaces the current redundant
recovery/tie-break sequence; it does not add an unbounded retry loop.

## 5. Repair contract

### 5.1 Generated candidate

The generation response is closed JSON containing:

- `rendered_text`: the complete proposed rendering for the bounded region;
- `confidence`: calibrated confidence from `0.0` to `1.0`;
- `repair_reasons`: a bounded list containing only `korean_morphology`,
  `identifier_integrity`, `punctuation_boundary`, `table_cell_boundary`, or `line_boundary`.

The generated rendering becomes a first-class `SpacingCandidate` with a deterministic ID derived
from its region, generator prompt version, source hash, and rendered text. It is not inserted into
the original extractor candidate set or silently represented as an existing candidate ID.

`DecisionRecord` carries an optional `generated_candidate` only for a spacing decision. Canonical
assembly may use it only when all repair invariants pass and its region and candidate-set identity
match the decision. Trusted cache loading repeats those checks rather than trusting serialized
flags.

### 5.2 Deterministic invariants

Before verification or application, the implementation must prove:

1. Removing Unicode whitespace from the source-bounded text and proposal produces exactly the
   same sequence of Unicode code points.
2. No code point was inserted, removed, reordered, case-folded, normalized, or replaced.
3. Protected identifiers remain byte-for-byte intact. Protected tokens include snake-case names,
   alphanumeric IDs, URLs, email-like values, date/time values, numeric units, and configured
   domain identifiers discovered in the source region.
4. The proposal does not move characters across extractor-provided table-cell boundaries.
5. The proposal does not merge or split structural line boundaries that were marked hard by the
   extractor.
6. The proposal contains no control character or unsupported line separator.
7. The proposal still satisfies all existing canonical source-coverage, missing-character,
   duplicate-character, and source-hash checks.

The first invariant is authoritative for character conservation. Protected-token and structural
checks prevent harmful whitespace changes that character conservation alone would permit.

### 5.3 Generation context

The generation prompt includes only the bounded region and evidence needed to decide spacing:

- exact source character sequence and current candidate renderings;
- compact boundary differences between candidates;
- protected identifier spans and token kinds;
- line IDs, table-cell IDs, and which boundaries are hard or soft;
- neighboring bounded text already allowed by the current evidence policy;
- Korean-specific instructions covering particles, compounds, punctuation, and mixed
  Korean/Latin identifiers;
- the exact whitespace-only contract and failure behavior.

The prompt must tell the model to preserve every non-whitespace code point, not to improve prose,
and to return low confidence when evidence is insufficient. It must not tell the model to raise
confidence or prefer the primary selection.

### 5.4 Independent verification

Verification uses a separate request and request hash. It receives the source region, generated
candidate, original candidates, protected spans, and structural boundary metadata. Its response
selects an allowlisted ID from the original candidates plus the generated candidate and returns
confidence and bounded reason codes.

A generated repair is applied as trusted semantic output only when:

- generation confidence is at least `minimum_model_confidence`;
- verifier confidence is at least `minimum_model_confidence`;
- the verifier selects the generated candidate; and
- deterministic invariants pass both before and after verification.

Model agreement never overrides an invariant failure.

## 6. Conservative fallback

When repair is rejected or remains uncertain, conversion uses a deterministic fallback so useful
work is not discarded:

1. Prefer the exact `source_spacing` candidate when it exists and passes character and structural
   invariants.
2. Otherwise choose the highest-scoring deterministic candidate that passes the same invariants,
   with stable candidate-ID ordering as the final tie-break.
3. Never synthesize another candidate and never choose a candidate with missing, duplicate,
   reordered, or cross-cell characters.

The fallback is recorded as `applied_fallback_pending_review`; it is not presented as a verified
repair. Its purpose is to keep the document usable and reviewable while preserving the safest
available source form.

## 7. Decision and pipeline states

### 7.1 Decision outcome

Add `deferred_review` as a terminal decision outcome. It includes a selected fallback candidate
and is distinct from the current unresolved `review_required` outcome.

The relevant terminal outcomes are:

- `selected`: existing or generated candidate confidently selected;
- `deferred_review`: safe fallback applied, semantic review debt retained;
- `review_required`: no valid fallback can be applied;
- `failed`: integrity or schema state makes trustworthy assembly impossible.

The decision audit distinguishes:

- `applied_existing_candidate`;
- `applied_generated_repair`;
- `applied_fallback_pending_review`;
- `rejected_generated_repair`.

### 7.2 Canonical status

Add `review_pending` to `SemanticPipelineStatus`.

- `verified`: all decisions and global invariants passed; release-ready.
- `review_pending`: global integrity invariants passed and a valid canonical document was built,
  but one or more `deferred_review` decisions remain.
- `review_required`: no valid candidate exists for at least one region, so assembly cannot safely
  continue.
- `failed`: hard integrity, identity, or schema failure.

For compatibility, `review_pending` is `publishable=true` in the narrow sense that generated
content and quality artifacts may be written to a Draft PR. It is not release-ready. Release
validation continues to require the exact `verified` status, so deferred semantic debt cannot be
merged as an approved document.

### 7.3 Workflow behavior

The GitHub Actions workflow treats `review_pending` as a handled processing result:

- source validation exits successfully and passes the canonical artifact forward;
- Markdown generation and changeset creation continue;
- the product PR is created or updated and remains Draft;
- the Issue workflow completes instead of receiving `ard:failed` for semantic uncertainty;
- the PR summary states that manual semantic review is required;
- the product PR quality gate reports the review debt and remains non-release-ready;
- release validation still fails until a later trusted decision produces `verified`.

`review_required` and `failed` remain blocking because they lack a safe canonical output. Provider
configuration errors and exhausted transport retries remain workflow failures; they must not be
silently converted into semantic fallbacks.

## 8. Durable human-review record

Every `review_pending` result writes `quality/semantic-review.json` beside the generated product
quality artifacts. The artifact has a versioned schema and contains:

- source, evidence, candidate-set, region, prompt, schema, provider, and model hashes;
- decision type and terminal status;
- original candidate IDs, deterministic scores, and model confidences;
- generated candidate ID when present;
- generation and verification request hashes and bounded reason codes;
- invariant rejection codes;
- selected fallback ID and fallback policy version;
- whitespace-boundary edit positions or a compact boundary diff;
- masked previews governed by the existing diagnostics policy;
- the exact command and identities needed to replay the decision.

Raw prompts, raw provider responses, page images, credentials, and unrestricted source text are
not written by default. The canonical Markdown already exposes the applied fallback for human
review; the review artifact explains why it was used and how to reproduce the choice.

`application-report.json` records whether each decision was applied as an existing candidate,
generated repair, or pending-review fallback. Terminal summaries count generation calls,
verification calls, accepted repairs, rejected repairs, fallbacks, and deferred decisions.

A future human-approved decision can be stored through the existing trusted-decision mechanism,
keyed by the same source and candidate-set identities. On replay it must make zero LLM calls and
remove the corresponding review debt only after the trusted payload passes current invariants.

## 9. Cache and identity rules

Repair cache identity includes:

- source and evidence hashes;
- candidate-set and region IDs;
- generation and verification prompt versions;
- response schema versions;
- provider and model IDs;
- protected-token extraction policy version;
- structural-boundary policy version;
- exact generated candidate identity;
- deterministic invariant policy version.

A cache entry is reusable only when the full generated candidate and both audit phases are
present, all IDs match, and invariants pass again. A cached `deferred_review` decision may reuse
its fallback without a provider call, but it remains `review_pending`; caching must not promote it
to `verified`.

## 10. Error matrix

| Condition | Applied output | Canonical status | Workflow |
| --- | --- | --- | --- |
| Primary selection confidence is high | Selected existing candidate | `verified` if globally clean | Continue |
| Generated spacing repair and verifier both pass | Generated candidate | `verified` if globally clean | Continue |
| Generated repair changes a character or protected token | Source-spacing fallback | `review_pending` | Continue Draft output |
| Generated repair or verifier remains below threshold | Source-spacing fallback | `review_pending` | Continue Draft output |
| Non-spacing semantic decision remains uncertain | Safest valid deterministic candidate | `review_pending` | Continue Draft output |
| No invariant-safe candidate exists | None | `review_required` | Stop before product writeback |
| Missing/duplicate/reordered character or source-hash mismatch | None | `failed` | Stop |
| Provider misconfiguration or exhausted transport retry | None | provider failure | Fail and retry operationally |
| Trusted cache audit or identity mismatch | Ignore cache, adjudicate fresh | Based on fresh result | Continue or fail normally |

## 11. Meaningful test strategy

Tests are organized around conversion contracts, not implementation trivia.

### 11.1 Issue #3 replay acceptance

Use the captured public Issue #3 candidate sets and replay responses to prove:

1. `candidate_set_adf8c3c96f06d140` does not stop after the observed `0.72` and `0.76`
   low-confidence pattern. It enters repair generation, preserves identifiers and every
   non-whitespace character, and records the exact applied rendering.
2. The `candidate_set_6d08c750276170e3` case compares the actual rendering with the approved
   expected text; a high confidence flag without correct identifier spacing is not sufficient to
   pass.
3. A character-changing or low-confidence generated proposal is rejected, the source-spacing
   fallback is applied, canonical Markdown is still produced, status is `review_pending`, and
   `semantic-review.json` contains actionable evidence.
4. Replaying a trusted accepted repair makes zero provider calls and reproduces the same canonical
   hash. Replaying a deferred fallback also makes zero calls but remains `review_pending`.

### 11.2 Compact general contract

One compact Korean table fixture combines Korean compounds, particles, punctuation,
`marketing_campaign`, `creative_id`, a date, and a numeric value. It proves the prompt schema,
identifier protection, cell boundaries, acceptance path, and fallback path without duplicating
dozens of shallow unit cases.

### 11.3 Workflow contract

An end-to-end workflow fixture proves that `review_pending` passes source-check output into
processing, writes Markdown and the durable review artifact, updates a Draft PR, and does not mark
the Issue as a failed conversion. A paired hard-integrity fixture proves that character loss still
blocks writeback.

Focused tests run first during development. Ruff, static/schema checks, and the full suite run only
at integration checkpoints and before merge. This keeps the inner loop fast while preserving the
final repository-wide safety check.

## 12. Implementation slices

1. Add the generated-candidate and repair-audit models plus deterministic whitespace, identifier,
   and structural invariants.
2. Replace low-confidence spacing re-voting with generation and verification; preserve bounded
   provider/schema retries.
3. Add deterministic fallback, `deferred_review`, and `review_pending` canonical assembly.
4. Carry `review_pending` through source-check, processing, Draft PR summary, quality status, and
   durable review artifacts.
5. Add the Issue #3 replay acceptance first, then the compact general contract and workflow hard
   failure pair.
6. Run focused checks, the full suite, a local Issue #3 replay, and pre-merge review.

Each slice is independently reviewable and should avoid unrelated refactoring.

## 13. Rollout and success criteria

After merge, reapply Issue #3 approval once and inspect the real run, Issue, and product PR.

The change succeeds when:

- the workflow completes conversion and advances PR #5 instead of stopping on handled semantic
  uncertainty;
- the exact Korean and identifier rendering is correct, or the conservative source-spacing form
  is visibly applied with `review_pending`;
- Issue #3 is not labeled as a failed conversion solely because semantic confidence was low;
- every generated or rejected repair is traceable through hashes, bounded reasons, and its exact
  application outcome;
- PR #5 remains Draft and non-release-ready while any deferred review exists;
- missing, duplicate, reordered, or mutated non-whitespace characters still fail closed; and
- trusted replay is deterministic and makes zero unnecessary LLM calls.

The first live acceptance targets Issue #3, but correctness is defined by the generic invariants
and state transitions above rather than by hard-coded document text.
