# Repository documentation refresh design

## Objective

Update the repository documentation after the Issue #3 semantic PDF work so that a new adopter can understand what the project does, while operators and contributors can reach precise policy and implementation details without treating historical plans as current behavior.

## Audience

The README serves a first-time adopter. It explains the product promise, inputs, outputs, safety guarantees, and shortest working path. Operators and pipeline contributors are secondary audiences served by linked documents.

## Information architecture

Use a layered documentation set:

1. `README.md` is the entry point and documentation index.
2. `docs/policy-and-governance.md` is the normative policy reference.
3. `docs/semantic-pdf-pipeline.md` explains the current semantic PDF architecture and report contracts.
4. `docs/github-actions-setup.md` remains the GitHub configuration and trust-boundary runbook.
5. `docs/operations/semantic-pdf-rollout.md` remains the incident, rollback, and acceptance runbook.
6. `docs/next-steps.md` records only unfinished work and clearly labels completed milestones.
7. `docs/superpowers/specs/` and `docs/superpowers/plans/` remain historical decision and implementation records. They do not override current code or normative documentation.

## Documentation authority

Current behavior must be derived from executable code, schemas, workflow definitions, tests, and verified Issue #3 artifacts. Documentation authority is ordered as follows:

1. schemas and executable validation invariants;
2. current CLI and workflow implementation;
3. normative policy and operations documents;
4. README summaries;
5. historical specifications and plans.

When a historical plan conflicts with current implementation, the current implementation and normative documents win. The historical file remains unchanged unless it contains a broken link or explicitly claims to describe current behavior.

## Policy coverage

The policy document must state these settled decisions:

- authoritative text comes from the complete embedded PDF text layer or one whole-document OCR path; page-level mixing is forbidden;
- source characters and hard boundaries are immutable through semantic repair;
- deterministic code owns physical schema, identifiers, versions, relationships, validation, and publication;
- LLMs may rank allowlisted candidates or generate whitespace-only repairs under explicit invariants;
- low-confidence decisions receive bounded recovery and independent verification;
- unresolved optional decisions do not stop conversion when a deterministic invariant-safe fallback exists;
- unresolved debt remains visible in decision, attempt, application, and validation audit records;
- unsafe optional metric SQL is excluded with a warning and does not invalidate an otherwise publishable document;
- raw source text, provider responses, credentials, and unrestricted generated candidates are not persisted in public diagnostics;
- publication requires global fidelity invariants even when an individual model decision is accepted;
- immutable tags, releases, and downstream dispatch use exact commits and idempotent retry contracts.

## Technical coverage

The pipeline document must describe the current sequence:

1. select embedded text or whole-document OCR authority;
2. create immutable atoms and source spans;
3. collect deterministic and model-assisted structure candidates;
4. adjudicate candidates with cache and audit validation;
5. apply bounded low-confidence recovery;
6. generate and independently verify whitespace-only repairs where needed;
7. choose an invariant-safe fallback when review remains unresolved;
8. assemble canonical blocks and derive heading levels from hierarchy;
9. validate coverage, ownership, ordering, tables, protected tokens, Markdown, and raw HTML;
10. publish generated artifacts and quality reports atomically;
11. release immutable bundles and dispatch downstream linkage idempotently.

The document must distinguish decision confidence from publication status and explain `selected`, `deferred_review`, `review_required`, `WARN`, and `FAIL` without implying that every warning blocks the workflow.

## README contract

The README will be rewritten as a concise entry point with:

- one-paragraph project definition;
- supported inputs and generated outputs;
- five or fewer guarantees a user can rely on;
- the GitHub Issue and direct-branch flows;
- a minimal local quick start;
- a compact explanation of deterministic and LLM responsibilities;
- links to policy, pipeline, setup, operations, roadmap, and historical architecture records;
- a short troubleshooting table for the most common status classes.

Detailed acceptance scripts and full environment matrices belong in linked documents, not in the README.

## Existing document updates

- `docs/github-actions-setup.md` will document the candidate pipeline variable, protected LLM execution, non-blocking review debt, release tag identity, retry semantics, and the distinction between historical failed runs and converged release state.
- `docs/operations/semantic-pdf-rollout.md` will replace stale assumptions that `review_required` always stops publication. It will define safe continuation, review-debt inspection, Issue #3 verification, and release recovery.
- `docs/next-steps.md` will mark Issue #3 conversion, heading repair, whitespace recovery, optional metric isolation, and v1 release as completed. Remaining items will be limited to genuine operational or product backlog.

## Validation

Documentation changes are complete only when:

- every non-historical documentation file is reachable from the README;
- documented CLI commands and environment variables exist in the current code;
- report names match the generated product tree and release bundle;
- Markdown links resolve locally;
- fenced commands pass syntax-oriented checks where practical;
- no credentials, provider payloads, or private diagnostic text are introduced;
- the Issue #3 example metrics match the verified artifacts;
- documentation lint, repository tests relevant to docs and CLI contracts, Ruff, and the repository change gate pass.

## Non-goals

- rewriting historical specifications or plans;
- changing pipeline behavior, schemas, or release policy;
- adding new runtime flags solely for documentation convenience;
- documenting provider-specific secrets beyond the reviewed environment-variable contract.
