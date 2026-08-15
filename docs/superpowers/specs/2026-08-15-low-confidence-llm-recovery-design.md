# Low-Confidence LLM Recovery Design

## 1. Context

The bounded semantic PDF adjudicator selects among prevalidated candidates. It auto-selects a
clear deterministic winner and asks an LLM only when candidate scores are ambiguous. A model
response below `minimum_model_confidence=0.80` currently becomes `review_required` immediately.
The selected candidate ID is discarded, no confidence-recovery call occurs, and the decision
report records only the terminal low-confidence result.

The live Issue #3 validation demonstrated the gap. Source preservation and structural invariants
passed, 67 of 69 decisions were selected, and two Korean spacing decisions returned confidences
of `0.70` and `0.74`. Those two advisory confidence values blocked publication even though the
provider was available and returned allowlisted candidate IDs.

## 2. Goals

- Recover low-confidence bounded decisions with additional LLM adjudication.
- Preserve the rule that an LLM may choose only an allowlisted candidate ID and may not author
  replacement text, source references, ordering, or table geometry.
- Automatically apply a recovered candidate only when the configured consensus policy succeeds.
- Retain every primary, recovery, and tie-break attempt as a content-addressed audit record.
- Reuse a trusted recovered decision without making additional model calls.
- Keep deterministic decisions at zero model calls and retain current behavior for unavailable,
  transient, malformed, or persistently uncertain providers.
- Keep publication atomic and subordinate every model decision to canonical invariants.

## 3. Non-goals

- Lowering the global confidence threshold.
- Letting the model return corrected prose or a candidate outside the supplied allowlist.
- Publishing a document that remains ambiguous after the bounded recovery budget is exhausted.
- Adding a second provider configuration or requiring a different model for the tie-break call.
- Changing PDF evidence extraction, candidate generation, canonical rendering, or non-PDF flows.

## 4. Decision

Use a bounded three-vote recovery protocol only after a valid primary response is below the
minimum confidence threshold.

1. Record the primary response as vote 1, including its candidate ID and confidence.
2. Send a recovery request containing the same closed candidate set, the primary vote, the
   deterministic candidate scores and features, and a recovery-specific instruction to reassess
   the ambiguity without rewriting source content.
3. If vote 2 chooses the same candidate and meets `minimum_model_confidence`, accept that candidate.
4. If vote 2 chooses a different allowlisted candidate, make one independent tie-break request.
5. Accept the two-of-three majority candidate only when at least one vote for that candidate meets
   `minimum_model_confidence` and the terminal deciding vote is valid.
6. If vote 2 remains below threshold, or the tie-break does not produce a qualified majority,
   return `review_required`.
7. Canonical assembly and validation run unchanged. Any invariant failure prevents publication
   regardless of the consensus result.

The recovery budget is therefore at most two additional semantic vote phases per low-confidence
decision. Provider retries and closed-schema correction remain separately bounded. Ordinary model
decisions still use one vote phase, and deterministic decisions use none.

## 5. Adjudication model

### 5.1 Policy

`AdjudicationPolicy` gains:

- `max_confidence_recovery_attempts: int = 2`, constrained to `0..2`;
- `consensus_votes_required: int = 2`, fixed to two for the initial version.

`max_schema_attempts` remains responsible only for invalid structured output or an unknown
candidate ID. Confidence recovery is a separate budget, so provider-level retry counts and
semantic recovery counts cannot be conflated.

### 5.2 Attempt audit

Add an immutable `AdjudicationAttempt` model with:

- `attempt_index`: one-based order, maximum six, allowing two closed-schema attempts in each of
  the three semantic vote phases;
- `phase`: `primary`, `recovery`, or `tiebreak`;
- `request_hash`: hash of the exact phase, prompt version, candidate set, prior vote summaries,
  provider, model, evidence and optional crop;
- `candidate_id`: the returned allowlisted candidate, or `None` when output is rejected;
- `confidence`: advisory model confidence;
- `status`: `accepted`, `low_confidence`, `candidate_unknown`, or `provider_rejected`;
- `validation_codes`: bounded machine-readable codes;
- `provider_retry_count` and `provider_repair_count`.

No raw prompt, model prose, source text, or image is persisted in the default report. Exact prompt
identity remains verifiable through hashes. Candidate renderings remain governed by the existing
masked diagnostic policy.

### 5.3 Terminal decision audit

`DecisionRecord` gains backward-compatible fields with defaults:

- `recovery_status`: `not_needed`, `recovered`, or `review_required`;
- `attempts`: zero to six `AdjudicationAttempt` entries;
- `consensus_method`: `none`, `same_candidate`, or `two_of_three`;
- `consensus_candidate_id`: the majority candidate when consensus succeeds;
- `recovery_count`: the number of confidence-recovery calls.

`source` gains `recovered` for a newly recovered decision. Trusted reuse continues to report
`source=cache` while preserving the original attempts, consensus method, consensus candidate and
recovery count. The terminal `decision_id` includes the attempt hashes and consensus fields so two
different recovery histories cannot share an identity.

Existing trusted decision JSON remains loadable because every new field has a safe default.
Changing the adjudication prompt version invalidates incompatible request hashes.

## 6. Prompt and vote rules

The primary prompt remains closed and candidate-ID-only. Recovery prompts use a new prompt version
and include only:

- decision type, candidate-set ID and region ID;
- allowlisted candidate summaries already used by the primary call;
- prior candidate IDs, confidences and validation codes;
- an instruction to compare the exact differences among candidates;
- for spacing, an instruction to consider Korean morphology, particles, compounds, punctuation,
  identifiers and neighboring bounded context already present in candidate summaries; and
- the same optional single page crop supplied to the primary request.

The recovery model is not told to increase confidence and is not allowed to edit a candidate. It
must independently choose an allowlisted ID and report calibrated confidence. A tie-break prompt
states that prior votes disagree and asks for an independent selection; it does not identify a
preferred candidate.

Vote qualification rules are exact:

- unknown candidate IDs enter the existing schema-correction path and do not count as votes;
- a valid low-confidence primary response counts toward candidate agreement but cannot by itself
  authorize application;
- two matching votes authorize application only when the recovery vote is at least `0.80`;
- after disagreement, a two-of-three majority authorizes application only when the tie-break vote
  belongs to the majority and is at least `0.80`;
- no majority, a low-confidence terminal vote, exhausted schema correction, or provider output
  rejection produces `review_required`;
- transient provider failures continue to propagate to the workflow retry boundary.

## 7. Data flow

```text
bounded candidate set
  -> deterministic threshold
      -> clear winner: selected / no LLM audit attempts
      -> ambiguous: primary LLM vote
          -> confidence >= 0.80: selected / one attempt
          -> confidence < 0.80: recovery LLM vote
              -> same candidate and confidence >= 0.80: recovered
              -> different candidate: tie-break LLM vote
                  -> qualified 2-of-3 majority: recovered
                  -> otherwise: review_required
              -> confidence still < 0.80: review_required
  -> canonical assembly
  -> invariant validation
      -> verified: publishable
      -> invariant failure: failed, never published
```

## 8. Diagnostics and cache behavior

`decision-report.json` is the authoritative adjudication trail. It includes terminal decisions and
their ordered attempts. A new `application-report.json` records whether each recovered decision
was included in a canonical document that passed global validation. Each application entry
contains the decision ID, candidate-set and selected-candidate IDs, canonical hash, validation
status, application outcome (`applied` or `rejected_by_invariant`) and invariant finding codes.
This keeps model consensus separate from the later document-wide publication decision.

`failure-report.json` continues to list terminal validation codes. Recovered decisions add
`LLM_LOW_CONFIDENCE_RECOVERED` to their decision audit but not to failure codes, because recovery
succeeded. Unresolved decisions retain `LLM_CONFIDENCE_TOO_LOW`.

Diagnostic summaries add counts for:

- primary model calls;
- confidence-recovery calls;
- recovered decisions;
- tie-break decisions;
- unresolved low-confidence decisions.

Trusted cache reuse requires the same source, evidence, candidate set, prompt/schema, provider,
model and page-crop hashes. A cached recovered decision is accepted only when its selected and
consensus candidate IDs still belong to the current allowlist and its terminal outcome is
`selected`. Reuse preserves the complete original audit and makes zero provider calls.

## 9. Error handling

- Missing provider: unchanged `LLM_PROVIDER_UNAVAILABLE` review result, with no recovery attempt.
- Provider transient/configuration failure: propagate unchanged; workflow policy owns retry.
- Invalid JSON or closed-schema violation: use existing provider repair and schema-attempt limits.
- Unknown candidate: retry within the schema budget; if exhausted, review is required.
- Low-confidence recovery exhaustion: `LLM_CONFIDENCE_RECOVERY_EXHAUSTED` and review required.
- Tie-break without qualified majority: `LLM_CONSENSUS_NOT_REACHED` and review required.
- Cached audit mismatch: ignore the cache entry and perform a fresh primary adjudication.
- Canonical invariant failure: publication remains failed. The recovery audit is retained to show
  what was applied before the invariant rejected the document.

## 10. Testing strategy

Unit tests cover:

- a high-confidence primary decision with one attempt and no recovery;
- a low-confidence primary followed by a matching high-confidence recovery;
- a low-confidence primary, disagreeing recovery and high-confidence two-of-three tie-break;
- disagreement without a qualified majority;
- a second low-confidence response that exhausts recovery safely;
- schema correction during each phase without consuming the confidence budget incorrectly;
- transient provider failure propagation;
- trusted recovered-decision reuse with zero provider calls and preserved audit;
- cache rejection when any attempt/request identity is incompatible;
- closed schemas and bounded audit lengths.

Diagnostics tests prove raw prompts, raw responses, unmasked source text and page images are not
written by default. They also prove `application-report.json` distinguishes an applied recovered
decision from consensus that was later rejected by a canonical invariant. Integration tests prove
a recovered spacing decision can produce a globally `verified` canonical document, while failed
consensus remains non-publishable. The Issue #3 replay provider must exercise the recovery path
with the observed `0.70` and `0.74` primary confidence pattern rather than returning only `0.99`
happy-path responses.

## 11. Rollout and success criteria

1. Land the models, adjudication state machine, diagnostics and tests on a dedicated fix branch.
2. Run Ruff, model/schema verification and the full pytest suite.
3. Run the deterministic Issue #3 replay twice and confirm the second run makes zero model calls
   when trusted recovered decisions are supplied.
4. Merge through a reviewed PR.
5. Reapply the Issue #3 approval label and monitor protected validation, processing and finalization.

The change succeeds when Issue #3 either publishes through a fully audited recovered consensus or
remains in `review_required` with an exact attempt-by-attempt reason. It must never pass merely by
lowering confidence or bypassing canonical invariants.
