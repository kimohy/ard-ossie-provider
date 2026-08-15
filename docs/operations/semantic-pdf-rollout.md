# Semantic PDF v2 rollout and rollback

## Runtime mode

The reusable processor validates and publishes PDFs with
`ARD_SEMANTIC_PDF_PIPELINE=candidate` by default. The accepted values are `legacy`, `shadow`, and
`candidate`; the CLI rejects every other value before processing starts. DOCX processing is not
affected by this setting.

- `candidate`: publish only the canonical candidate pipeline after `VERIFIED` validation.
- `shadow`: run both paths, retain diagnostics, and publish the legacy result.
- `legacy`: bypass the candidate path for immediate rollback.

The legacy implementation remains available during the stabilization window. Do not delete it as
part of a rollout response.

## Immediate rollback

Set the repository variable and rerun the failed workflow:

```bash
gh variable set ARD_SEMANTIC_PDF_PIPELINE --body legacy
gh run rerun RUN_ID
```

Rollback changes only the PDF semantic parser. It does not alter source attachments or previously
published commits. After the incident is understood, restore candidate mode with:

```bash
gh variable set ARD_SEMANTIC_PDF_PIPELINE --body candidate
```

## Evidence to retain

Keep both the validation and processing artifacts for the failed and rollback runs. The semantic
diagnostic bundle must contain its manifest, evidence summary, candidate report, decision report,
application report, validation report, and failure report. Record:

- workflow run URL, source hash, configuration hash, candidate canonical hash, and parser versions;
- extraction mode, page/atom/region/table counts, validation status, and invariant codes;
- decision type, candidate count, model/cache source, retries, and confidence without raw source
  text unless protected diagnostics were explicitly enabled; and
- the rollback run ID and whether legacy publication succeeded.

Open a defect referencing those artifacts. Never paste credentials, page images, or unmasked source
text into the issue.

## Low-confidence recovery audit

Candidate mode keeps the `0.80` minimum model-confidence threshold. A primary vote below that
threshold can receive at most one recovery vote and, only when valid votes disagree, one independent
tie-break vote. Interpret the terminal decision codes as follows:

- `LLM_LOW_CONFIDENCE_RECOVERED` means a bounded same-candidate or two-of-three consensus selected
  an allowlisted candidate. It is a successful audited recovery, not a validation failure.
- `LLM_CONFIDENCE_RECOVERY_EXHAUSTED` means the recovery vote was still below the threshold and the
  decision remains `review_required`.
- `LLM_CONSENSUS_NOT_REACHED` means the bounded votes conflicted without a qualified majority and the
  decision remains `review_required`.

Use `application-report.json` to distinguish decision recovery from document publication. Each
recovered decision has exactly one application outcome:

- `applied`: the canonical document also passed all global invariants and was verified for
  publication;
- `not_published`: the recovered candidate was assembled, but another unresolved decision kept the
  document in review;
- `rejected_by_invariant`: canonical validation failed, and `invariant_codes` records the overriding
  document-level failures.

Only `applied` can accompany globally verified publication. Consensus never overrides character
coverage, atom ownership, ordering, table-grid, raw-HTML, or other canonical invariants. Attempt
records contain phase-specific request hashes, candidate IDs, confidences, status codes, and retry
counts; default diagnostics do not retain raw prompts, responses, source text, or image bytes.

## Stabilization gate

Keep candidate mode enabled only while representative PDFs have 100% authoritative character
coverage, zero missing or duplicate atoms, valid table grids, no raw HTML, and stable repeated
canonical hashes. Track review rate, model calls, cache hits, stage latency, and peak memory for at
least 14 days and 20 varied PDFs before considering removal of the legacy path.
