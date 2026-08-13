# Semantic Schema Catalog Bootstrap Design

## Goal

Allow the already-reviewed semantic fidelity feature to add exactly
`reports/semantic-fidelity.schema.json` and
`reports/semantic-structure-repair.schema.json` atomically, without making the
trusted schema catalog candidate-controlled and without leaving `main` in an
inconsistent model/schema state.

## Confirmed failure

PR #19 head `c43a34e4ac6d4b632e7058e2f1da4ecc313988a4` failed the trusted static
repository verifier with `SCHEMA_CATALOG_MISMATCH`. The `pull_request_target`
job executed the verifier from `main` at
`8a06db084a28710478013447905fa76859b723ae`. That trusted catalog does not yet
contain the two new semantic report schema paths, so the static path-set check
correctly rejected the candidate before its credential-free model-schema job
could run.

## Security invariants

1. Candidate code never chooses or expands the trusted schema catalog.
2. The static verifier never imports or executes candidate Python.
3. Only the two exact semantic report references are pre-authorized.
4. The two semantic entries form one atomic group: both are absent or both are
   present. A partial group fails `SCHEMA_CATALOG_MISMATCH`.
5. When the group is present, the credential-free trusted helper imports and
   verifies both candidate models against the exact candidate schema bytes.
6. A nonce-bound receipt lists the exact required and present pre-authorized
   paths. Missing, extra, or partial receipt entries fail closed.
7. `main` remains valid before PR #19 merges because absent pre-authorized
   schemas are not required.
8. PR #19 promotes the two entries into its required `MODEL_SCHEMA_CATALOG`;
   after that feature merges, future trusted verification requires both paths.

## Architecture

Keep `MODEL_SCHEMA_CATALOG` as the required catalog. Add one trusted
`OPTIONAL_MODEL_SCHEMA_GROUPS` tuple containing a pair of
`ModelSchemaReference` values for the semantic fidelity and structure-repair
reports.

The static verifier parses all candidate JSON Schemas first. It accepts only
one of these exact non-Ossie path sets:

- the required catalog; or
- the required catalog plus the complete semantic group.

The trusted executable helper derives the same active catalog from schema-file
presence. It verifies every required model and, when the complete semantic
group exists, both semantic models. A partial group or inaccessible optional
path fails before candidate model import.

The parent verifier independently derives the expected receipt path list from
the candidate filesystem and compares the entire nonce-bound receipt. It never
trusts the child to declare which optional paths were active.

## Error handling

- Unexpected or partial schema paths: `SCHEMA_CATALOG_MISMATCH` in static
  verification.
- Partial optional group in the helper: `SCHEMA_CATALOG_MISMATCH`.
- Present optional schema with missing/wrong candidate model:
  `SCHEMA_MODEL_IMPORT_FAILED` or `SCHEMA_SYNCHRONIZATION_FAILED` through the
  existing stable helper boundary.
- Receipt path mismatch:
  `REPOSITORY_MODEL_SCHEMAS_RECEIPT_INVALID`.

## Testing

Use RED-GREEN-REFACTOR to prove:

1. static verification accepts the exact complete pre-authorized group;
2. static verification rejects either semantic schema alone and any unrelated
   new schema;
3. the helper skips the group when absent and verifies it when complete;
4. the parent expects optional paths only when the complete group is present;
5. a child receipt that omits present optional paths is rejected;
6. the full repository suite, Ruff, schema catalog, model-schema helper, wheel,
   workflow YAML, checksum, and secret-scan gates remain green.

## Rollout

1. Merge this bootstrap alone; it changes verifier code, tests, and this
   design/plan, but no Pydantic model or checked-in schema.
2. Merge the updated `main` into PR #19 and resolve its existing multi-provider
   LLM conflicts while retaining both feature sets.
3. Re-run PR #19 at the exact merged head. Trusted static verification accepts
   the complete pre-authorized pair, and the credential-free helper verifies
   both semantic models.
4. Keep PR #19 draft until the genuine workflow-created PDF/DOCX acceptance
   prerequisite is complete.
