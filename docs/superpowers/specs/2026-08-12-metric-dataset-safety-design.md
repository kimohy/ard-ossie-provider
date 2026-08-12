# Metric dataset safety design

**Status:** Approved

**Date:** 2026-08-12

**Scope:** Trusted validation, normalization, and publication policy for LLM-derived metrics

## 1. Context

PR #5 exposed two metric-safety defects even though its quality report was `PASS` with no
warnings. First, `Campaign Count` used an unqualified `campaign_id`, so the published expression
did not bind the column to an Ossie dataset. Second, `Modeled Efficiency` combined columns from
multiple datasets while the product had no declared relationships. The generated Ossie model
therefore represented a cross-dataset calculation without a trustworthy join path, cardinality,
or grain contract.

The current validator in `src/ard_ossie/pipeline.py` uses a regular expression that inspects only
already-qualified `table.column` tokens. Bare columns are ignored, and `_build_metrics` publishes
the original model expression unchanged. The relationship builder correctly trusts only physical
foreign keys from the submitted Dictionary, but metric validation does not use that safety
boundary. Metric exclusions also do not feed the quality warning collection.

## 2. Goals

- Require every model-proposed metric to declare the Ossie datasets it depends on.
- Validate dataset and column references against the current trusted Dictionary-derived tables.
- Publish only single-dataset metrics and fully qualify every referenced column in trusted code.
- Exclude multi-dataset metrics until the IR can represent join path, cardinality, and grain.
- Record every policy exclusion as a stable quality warning.
- Preserve the original model response, including excluded metrics, in `llm-suggestions.json`.
- Keep malformed, unsafe, or unknown references fail-closed before any promotion.
- Preserve stable metric IDs for accepted metrics and avoid registry entries for excluded metrics.

## 3. Non-goals

- Do not infer relationships from matching names, descriptions, or metric expressions.
- Do not synthesize joins or choose a grain for multi-dataset metrics.
- Do not publish a partially qualified or best-effort expression.
- Do not execute metric SQL or validate it against a live warehouse.
- Do not change Ossie 0.1.1, `MetricIR`, `MetricRecord`, product/table versioning, or public CLI
  arguments.
- Do not include the separate base-synchronization workflow fix in this PR.

## 4. Selected design

### 4.1 Structured response contract

`MetricSuggestion` gains a required, non-empty `dataset_names: list[str]` field. The OpenAI strict
JSON Schema requires the same array and rejects extra properties. The system prompt tells the
provider to use exact dataset names from the supplied Dictionary-derived table payload and to
list every dataset referenced by the expression.

This is an intentional provider-response contract change deployed atomically with the Pydantic
model, schema, prompt, trusted validator, and tests. There is no stored provider response to
migrate. A response using the old metric shape fails structured output validation.

### 4.2 Trusted SQL boundary

Add pure-Python `sqlglot==30.16.0` as a direct locked runtime dependency. PyPI lists this release
as compatible with Python 3.9 and later. Trusted code parses each metric as one SQL scalar
expression, traverses the AST, validates identifiers, rewrites eligible column nodes, and renders
a normalized expression. SQLGlot is used only as a parser and AST transformer; its execution
engine is never invoked.

The parser boundary accepts aggregate and scalar expressions such as `COUNT(*)`,
`COUNT(DISTINCT campaign_id)`, and `CASE` expressions. It rejects multiple statements, query or
subquery nodes, relation nodes, table- or set-valued functions, every statement root, and parsing
or tokenization failures. SQLGlot command-fallback warnings are suppressed at this boundary so
rejected provider SQL is never copied to application logs. Because SQLGlot is intentionally a
permissive transpiler rather than a complete database validator, the repository also enforces its
own closed AST and Dictionary-reference policies.

### 4.3 Dataset catalog and identifier rules

Trusted code builds a case-insensitive catalog from each `_TableDraft.locator.table_name` and its
Dictionary columns. The emitted expression always uses the catalog's canonical dataset and column
spelling. Two drafts with the same case-insensitive leaf table name make the provider catalog
ambiguous and fail before the provider is called with `METRIC_DATASET_NAME_AMBIGUOUS`. Dataset
names in one suggestion must be non-blank and unique case-insensitively.

Validation order applies to every returned metric before confidence filtering:

1. Require a non-blank metric name, expression, and dataset list.
2. Reject duplicate or unknown declared datasets.
3. Parse exactly one safe scalar expression.
4. Inspect every AST column reference.
5. Reject a qualified reference whose dataset was not declared or whose column does not exist.
6. Reject an unqualified column that exists in none of the declared datasets.
7. Classify the metric as publishable or unsupported.

Stable provider-output errors remain `ProviderExecutionError(kind=OUTPUT)` and use these codes:

- `LLM_METRIC_NAME_OR_EXPRESSION_EMPTY`
- `LLM_METRIC_DATASET_EMPTY`
- `LLM_METRIC_DATASET_DUPLICATE`
- `LLM_METRIC_DATASET_UNKNOWN`
- `LLM_METRIC_SQL_UNSAFE`
- `LLM_METRIC_SQL_INVALID`
- `LLM_METRIC_REFERENCE_UNKNOWN`
- `LLM_METRIC_NAME_DUPLICATE`

These failures occur before candidate generation or registry/generated promotion.

### 4.4 Single-dataset publication

When `dataset_names` contains exactly one dataset, every AST `Column` must resolve to that
dataset. A bare column is rewritten to `dataset.column`; an already-qualified column is accepted
only when its qualifier matches the declared dataset. `COUNT(*)` remains valid because it has no
column reference. The normalized AST output, not the model's raw text, becomes
`MetricIR.expression` and the Ossie metric expression. Identifier quoting required by the source or
canonical Dictionary spelling is preserved, and the rendered expression is parsed through the
same closed scalar boundary once more before publication.

For example:

```text
dataset_names: [marketing_campaign]
input:  COUNT(DISTINCT campaign_id)
output: COUNT(DISTINCT marketing_campaign.campaign_id)
```

Only the normalized, accepted suggestions reach `_build_metrics`. Existing confidence,
duplicate-name, retired-ID, ID-reuse, evidence, and deterministic sorting policies remain in
force.

### 4.5 Multi-dataset exclusion

When `dataset_names` contains two or more valid datasets, trusted code parses and inspects the
expression but never publishes it. Qualified references must still name declared datasets and
real columns. An unqualified column must occur in at least one declared dataset; ambiguity does
not trigger inference because the metric is excluded regardless.

Each excluded metric produces one warning:

```text
code: METRIC_MULTI_DATASET_UNSUPPORTED
path: metrics.<metric name>
message: Metric uses multiple datasets and was excluded because join path, cardinality, and grain are not declared
```

Excluded metrics create no `MetricIR` and no new `MetricRecord`. They therefore do not affect the
product canonical hash or consume a metric ID; only the quality report records their exclusion.
They do not appear in generated Markdown or Ossie output. The pipeline does not consult
`relationships` to override this rule; even a physical FK is insufficient without an explicit
metric grain contract.

## 5. Data flow

1. Trusted parsing builds `_TableDraft` objects from the submitted Dictionary.
2. The LLM request supplies exact dataset names and columns and requires `dataset_names` per
   metric.
3. Structured schema and Pydantic validation produce the raw `SuggestionBatch`.
4. Existing evidence-source checks validate every metric citation.
5. A dedicated metric preparation function validates and parses every metric, returning
   normalized publishable suggestions plus exclusion findings.
6. `process_product` passes only publishable suggestions to `_build_metrics`.
7. Metric exclusion findings are appended to completeness warnings before warning policy is
   evaluated.
8. `--warnings-as-errors` converts any metric exclusion warning into the existing
   `WARNINGS_AS_ERRORS` hard failure before promotion. The direct `ard process` adapter must pass
   its existing option through to `process_product`; strict behavior may not be deferred until
   after a successful promotion.
9. `_write_quality` serializes the untouched raw `SuggestionBatch`; excluded expressions and
   `dataset_names` remain available for audit.
10. Generated artifacts and Registry receive only normalized accepted metrics.

The raw and publishable representations must be separate variables. Mutating
`SuggestionBatch.metrics` would erase the original expression or excluded metric from the audit
and is prohibited.

## 6. Quality and failure behavior

Metric exclusion findings join the existing warning list. A normal run with one or more excluded
multi-dataset metrics has `status=WARN`; its other valid metrics and generated artifacts may be
promoted. In strict mode, `WARNINGS_AS_ERRORS` makes the run `FAIL`, preserves detailed quality
evidence, and leaves the previous generated and Registry state unchanged.

Malformed SQL, unsafe ASTs, unknown datasets, and unknown columns are provider output failures,
not warnings. No normalization fallback, string replacement, closest-name match, or relationship
inference is allowed. An accepted and an excluded suggestion may not share a case-insensitive
metric name or split the primary name and aliases of an existing metric record; such a collision
fails closed rather than deleting or reassigning a stable metric identity.

## 7. Audit and compatibility

`quality/llm-suggestions.json` retains every original suggestion with its original expression,
confidence, evidence, status, and declared `dataset_names`. Excluded metrics remain in this file.
Accepted metrics are not overwritten with their normalized SQL in the audit. The public
`MetricIR`, generated Markdown, Ossie model, and Registry continue to use their existing schemas;
only accepted `MetricIR.expression` values become fully qualified.

Checked-in model schemas are regenerated only for the provider-facing `MetricSuggestion` shape if
the repository's schema generation covers it. Ossie and IR schemas must remain byte-for-byte
unchanged unless implementation evidence proves a currently generated schema necessarily embeds
the provider model.

## 8. Test strategy

- Schema/model tests require non-empty `dataset_names`, reject the legacy metric shape, and
  preserve the existing evidence contract.
- Unit tests verify single-dataset qualification for bare columns, already-qualified columns,
  `COUNT(*)`, nested functions, and `CASE` expressions.
- Unit tests reject duplicate/unknown datasets, unknown columns, undeclared qualifiers, multiple
  statements, queries/subqueries, mutation/command SQL, unsupported statement families,
  table-valued functions, tokenization failures, and parse errors without logging the raw rejected
  SQL.
- Unit tests reject ambiguous leaf dataset catalogs, verify reserved identifier quoting survives
  normalization, and protect existing metric identity from accepted/excluded name collisions.
- Integration tests prove duplicate-name and identity-collision failures remain classified as
  provider output and cannot escape the atomic provider-validation boundary as plain exceptions.
- Unit tests prove every raw metric is validated before confidence filtering.
- Unit tests verify multi-dataset metrics return exactly one stable warning and no publishable
  suggestion.
- Pipeline tests prove quality becomes `WARN`, excluded metrics create no IR/Registry record, and
  raw audit entries are preserved unchanged.
- Atomic-promotion tests prove `--warnings-as-errors` blocks generated and Registry promotion while
  retaining the detailed exclusion warning.
- Integration tests model the PR #5 result: `Campaign Count` is fully qualified against
  `marketing_campaign`, while `Modeled Efficiency` is excluded with
  `METRIC_MULTI_DATASET_UNSUPPORTED`.
- Full pytest, Ruff, workflow YAML parsing, isolated model-schema verification, schema catalog,
  Ossie checksum, secret scan, and sdist/wheel builds run before publication.

## 9. Acceptance criteria

- No published metric contains a bare column reference.
- Every published metric declares exactly one known dataset and references only its real columns.
- Multi-dataset metrics are absent from generated output and Registry and produce a quality
  warning.
- Invalid or unsafe SQL and unknown identifiers stop processing before promotion.
- `--warnings-as-errors` blocks promotion for an otherwise valid multi-dataset exclusion.
- `llm-suggestions.json` retains accepted and excluded raw metric responses unchanged.
- Existing product/table/relationship behavior and public schemas remain compatible.
- Reprocessing PR #5 produces a qualified `Campaign Count`, excludes `Modeled Efficiency`, and
  does not report `PASS` when an unsupported metric was removed.

## 10. References

- SQLGlot PyPI release and Python support: https://pypi.org/project/sqlglot/
- SQLGlot AST traversal and transformation: https://github.com/tobymao/sqlglot
- Existing source inventory: `docs/superpowers/specs/references/source-inventory.md`
