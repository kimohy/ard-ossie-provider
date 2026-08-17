# Provenance-neutral Table Change Detection Design

## Goal

Detect table changes from published table content rather than workbook-wide provenance. When one
table in a multi-table dictionary changes, only that table may receive a new canonical hash and
version. Unchanged shared tables must retain their existing Registry records so a changeset remains
scoped to the tables that actually changed.

This is the first of two sequential recovery changes for the active shared-table production E2E.
Product-fact singleton handling is intentionally deferred to a separate design and PR.

## Production failure

Changeset `cst_01a00e64-ab27-7aeb-8d49-87eab21b7082` coordinates only table
`tbl_01a00585-94b8-7e49-ac43-97e00a165e26` (`marketing_campaign`). The approved workbook changes
one description cell in that table. Parsing the baseline and candidate workbooks shows no content
change in `marketing_creative`, `marketing_delivery`, or `marketing_outcome`.

The current table canonical hash includes each `ColumnIR.evidence` object. Every evidence object
contains the SHA-256 of the entire workbook. Changing one cell therefore changes the evidence hash
on every table, and `_build_table_records` classifies all four tables as changed. Protected
processing for product `500138302` consequently failed with
`CHANGESET_TABLE_NOT_INCLUDED` on the first unchanged table outside the changeset.

Evidence is required for audit and validation, but a source-container digest is not table content.
Using it as version authority makes a table version depend on unrelated cells in the same file.

## Selected approach

Use the product's published `generated/data-dictionary.json` from the exact repository default-
branch SHA as the trusted comparison baseline. Compare each current table with its baseline using a
versioned, provenance-neutral content projection. Preserve the existing Registry record when the
projection is unchanged; create a new Registry record and canonical hash only when it differs.

The trusted workflow must load the baseline through `GitPort.read_bytes_at(base_sha, path)`. It must
not read comparison authority from the pull-request worktree. `ProcessingService` already resolves
the repository default branch and pins its SHA for trusted semantic replay; the table baseline uses
that established authority.

This avoids an eager Registry migration. Existing provenance-sensitive hashes remain valid legacy
data until a table truly changes. The first real change writes the new provenance-neutral hash.

## Alternatives considered

### Change the global canonical hash and migrate all Registry tables

Excluding evidence globally would make every existing table hash differ at once. Rewriting all
Registry records would create unrelated release changes and a broad migration rollback surface.
This is not appropriate for an active production recovery.

### Expand the changeset to all tables in the workbook

This would accept the faulty classification by versioning three unchanged shared tables. It would
violate the approved E2E contract and make table versions reflect file packaging rather than table
content.

### Read the baseline from the pull-request worktree

Base sync normally resets derived outputs, but treating candidate files as comparison authority
would make the security contract depend on every caller having performed that reset correctly. An
explicit default-branch revision read is narrower and independently testable.

## Trusted baseline contract

`ProcessingService` reads
`products/<product-key>/generated/data-dictionary.json` from the exact default-branch SHA captured
after the PR base-branch check. The parsed baseline is passed explicitly to `process_product`; the
pipeline does not discover a trusted baseline from its local product directory.

Before it can influence version decisions, the pipeline validates all of the following:

1. the baseline is valid UTF-8 JSON with the expected strict structure and scalar types, including
   every nullable field that the renderer always emits;
2. its `product_id` matches the configured and existing Registry product;
3. its `product_version` equals the existing Registry product version;
4. table IDs are unique;
5. the baseline table-ID set exactly matches the product's trusted Registry mapping;
6. each baseline table version equals the mapped table version; and
7. each table ID, dataset name, and fully qualified source agree with the Registry locator.

An existing product without a readable or consistent baseline fails closed with a stable, redacted
validation or security code before Registry mutation. A create operation containing only new tables
has no prior baseline and retains the existing new-table path. A new product that links an already
registered shared table fails closed with `TABLE_BASELINE_REQUIRED` until an authoritative
owner-baseline contract is implemented; it cannot compare the Registry's legacy hash with the new
projection. Direct pipeline callers must pass an explicit baseline for an existing product. The
local CLI adapter may snapshot the pre-run local generated dictionary and pass it as local comparison
input, but protected workflow code must always override that convenience path with bytes read from
the trusted default-branch revision.

The baseline bytes are read before provider execution so the captured default-branch SHA remains the
comparison authority for the entire processing attempt. A later default-branch movement does not
silently replace that snapshot. Existing post-processing checks continue to reject PR identity,
base-branch, or head movement before writeback and statuses.

## Table content projection

The comparison projection represents the published dictionary contract for one table and excludes
version and provenance. It contains:

- immutable `table_id` and the Registry-bound four-part locator, including source-system ID;
- normalized dataset name and fully qualified published source;
- table description; and
- columns sorted by ordinal and ID, including column ID, ordinal, physical name, logical name, data
  type, nullability, primary-key flag, description, and optional foreign key, formula, and comment.

It excludes:

- `table_version`;
- all `Evidence` objects and their source hash, locator, excerpt, and role;
- product version and other product-level fields; and
- execution metadata already excluded by the canonical hashing contract.

The projection matches the information currently published by `render_dictionary_json`, augmented
with the source-system ID from the Registry locator after baseline-to-Registry validation. A helper
builds the same projection from both a current `_TableDraft` and a validated baseline table. Hashing
uses the existing normalized `canonical_hash` primitive after projection, so Unicode normalization,
ordering, and serialization remain deterministic.

The projection has an internal schema/version constant. Adding a new published table field later
requires an intentional projection-version update and compatibility design; silently changing the
hash contract is forbidden.

## Version and Registry behavior

For each table:

1. If no Registry table exists, use the existing create behavior and hash the new content
   projection.
2. If a Registry table exists, require its trusted baseline table and compare the two projections.
3. If they are equal, set `changed=false`, retain the current version, and reuse the existing
   `TableRecord` byte-for-model rather than rewriting its legacy hash or metadata.
4. If they differ, set `changed=true`, run the existing numeric version policy, and write a new
   record whose canonical hash is the current provenance-neutral projection hash.

The emitted `TableIR` always reflects the current validated source, including current evidence for
downstream audit. Reusing an unchanged Registry record affects only Registry/version state; it does
not reuse stale generated content or provenance.

Changeset membership checks consume the corrected `VersionDecision.changed` values. Therefore only
an actually changed existing shared table is required to appear in the changeset.

## Failure behavior and security

- A malformed baseline is reported through a stable redacted code; raw baseline content is never
  included in logs or workflow result envelopes.
- Missing baseline data for an existing mapped table cannot fall back to the legacy hash comparison,
  because that would recreate the workbook-wide false positive.
- A create operation that reuses an already registered table also cannot use that fallback and fails
  before provider execution or mutation until trusted owner-baseline lookup is supported.
- Extra, duplicate, or cross-product baseline tables cannot create identity or ownership authority.
- Candidate content cannot select the baseline revision, product identity, table identity, mapping,
  or current Registry version.
- The pipeline snapshots and validates the Registry before decisions and retains its existing
  atomic promotion and Registry-change checks.
- Evidence remains present in generated IR and quality artifacts; it is excluded only from the
  table version comparison.

## Test strategy

Unit coverage must first reproduce the failure:

- two otherwise identical multi-table parses use different workbook source hashes;
- only one table has a real description change;
- the changed table receives `changed=true` and the proposed version;
- every other table receives `changed=false`, retains its version, and preserves its existing
  Registry record/hash; and
- changeset validation does not report `CHANGESET_TABLE_NOT_INCLUDED` for unchanged tables.

Trusted-boundary coverage must prove:

- the protected baseline is read from the exact captured default-branch SHA rather than the
  candidate head;
- missing, malformed, duplicate-ID, wrong-product, wrong-version, wrong-locator, and unmapped-table
  baselines fail before provider execution or writeback as appropriate;
- create operations containing only new tables remain supported without a baseline, while a new
  product reusing a registered shared table fails closed before provider execution; and
- a post-processing PR base-branch, identity, or head movement still prevents publication.

Compatibility coverage must prove that an unchanged table with a legacy provenance-sensitive hash
keeps that exact hash, while a genuinely changed table writes the new deterministic hash. Existing
atomic-promotion, changeset, processing-service, CLI, static, and full-suite tests remain required.

## Rollout and E2E resumption

Land this design, implementation plan, code, and tests in one code/documentation-only PR based on the
current `main`. Do not include `products/**` or `registry/**` changes. Require the exact PR head to
pass both repository statuses and receive explicit merge approval.

After merge, implement and merge the separate product-fact singleton recovery PR. Retry Issues #57
and #58 only after both fixes are on `main`, avoiding another protected LLM run that cannot complete
the full coordinated path. Preserve the current failed runs and tracking heads as production
evidence.
