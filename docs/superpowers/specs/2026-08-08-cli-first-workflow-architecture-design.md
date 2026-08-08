# CLI-First ARD Workflow Architecture Design

## 1. Status and objective

This design replaces business logic embedded in GitHub Actions YAML with a
layered, cross-platform `ard` CLI. GitHub Actions remains the event, permission,
runner, Environment, matrix, and artifact host. The CLI becomes the only execution
interface for ARD ingestion, parsing, modeling, validation, Git/Registry changes,
GitHub orchestration, changesets, and release processing.

The target repository is `kimohy/ard-ossie-provider`. This design extends the
approved ARD-to-Ossie architecture and makes the separately approved GitHub
bootstrap command part of the same CLI hierarchy.

The outcome must satisfy two use cases with the same application code:

1. a maintainer or developer can run and debug every lifecycle locally; and
2. GitHub Actions can run the lifecycle without reimplementing it in shell.

## 2. Scope and interpretation of “all modules through CLI”

Every externally useful capability has a stable CLI command. Internal pure
functions remain Python APIs and are composed behind those commands; exposing
every private function as a command would create an unstable and unusable
interface.

The CLI owns:

- HTML, Word/PDF, and Excel parsing;
- Product IR construction and normalization;
- OpenAI-compatible structured suggestions;
- ID resolution, duplicate handling, and numeric version checks;
- Registry and shared-table changesets;
- Markdown, dictionary JSON, and Apache Ossie JSON generation;
- quality, impact, version, and duplicate reports;
- Git and Git LFS operations;
- GitHub Issue, PR, status, label, Release, and repository-dispatch operations;
- release detection, verification, tags, bundles, and artifact hashes;
- repository code/data change classification and developer verification; and
- repository bootstrap and later review-protection enablement.

GitHub Actions YAML may own only:

- triggers, path filters, concurrency, and cancellation;
- job dependencies, matrices, and `if: always()` cleanup scheduling;
- least-privilege job permissions and Environment selection;
- pinned checkout, Python, uv, and upload-artifact Actions;
- one `uv run --frozen ard ...` command in each processing or finalizer step; and
- mapping CLI-produced step outputs into job or reusable-workflow outputs.

YAML processing blocks must not contain direct `git`, `gh`, `jq`, `awk`, `sed`,
inline Python, loops, case statements, or domain conditionals. Checkout and
artifact upload remain Actions because they are runner primitives rather than ARD
business logic.

## 3. Architecture and dependency direction

```text
GitHub Actions / local terminal
              |
              v
       CLI command adapters
              |
              v
   Application lifecycle services
              |
       +------+------+
       |             |
       v             v
  Domain modules    Ports
                      |
                      v
       Docling / Excel / OpenAI / Git / GitHub / Filesystem adapters
```

Dependencies point inward:

- domain modules know neither GitHub nor environment variables;
- application services depend on typed ports, not subprocess implementations;
- adapters implement external I/O and mutation;
- CLI commands validate user/event input, assemble dependencies, and render
  results; and
- workflow commands orchestrate application services but do not contain domain
  algorithms.

The target package structure is:

```text
src/ard_ossie/
├── cli/
│   ├── root.py
│   ├── parse.py
│   ├── model.py
│   ├── validate.py
│   ├── registry.py
│   ├── github.py
│   ├── release.py
│   └── workflow.py
├── application/
│   ├── intake.py
│   ├── processing.py
│   ├── changesets.py
│   ├── releases.py
│   └── repository_checks.py
├── ports/
│   ├── documents.py
│   ├── llm.py
│   ├── git.py
│   ├── github.py
│   └── filesystem.py
├── adapters/
│   ├── docling.py
│   ├── excel.py
│   ├── openai_compatible.py
│   ├── git_cli.py
│   ├── github_cli.py
│   └── filesystem.py
└── domain modules retained from the current package
```

The existing `ard_ossie.cli:app` entry point remains stable while its
implementation moves into the `cli/` package. Existing command names remain
compatibility aliases for one project release.

## 4. CLI hierarchy

### 4.1 Granular commands

Granular commands support local inspection, debugging, and selective reruns:

```text
ard parse product
ard parse semantic
ard parse dictionary
ard model build
ard validate product
ard validate registry
ard validate release
ard registry check
ard registry show
ard registry diff
ard impact table
ard changeset create
ard changeset ready
ard release plan
ard release build
ard github bootstrap
ard github enable-review-protection
```

Commands that produce files accept explicit input/output paths and default to
repository conventions. Read-only commands never mutate Git, GitHub, Registry, or
generated outputs.

### 4.2 Lifecycle commands used by Actions

```text
ard workflow issue-authorize
ard workflow issue-intake
ard workflow detect-product
ard workflow source-check
ard workflow process
ard workflow changeset
ard workflow repository-check
ard workflow release-detect
ard workflow release-product
ard workflow release-dispatch
ard workflow finalize
```

Each command accepts `--repository`, explicit identifiers when available, and an
optional `--event` JSON file. In GitHub Actions, `--event` defaults to
`GITHUB_EVENT_PATH`; locally it must be supplied when the command depends on an
event. Hidden GitHub context must not be inferred from a remote URL.

Mutating commands support a planning phase. Where a separate invocation would
introduce race conditions, one command computes a plan, displays or records it,
validates preconditions again, applies it, and verifies the result. Interactive
confirmation is required for maintainer bootstrap but disabled for CI lifecycle
commands whose authorization is the workflow permission and approved Environment.

## 5. Common execution contract

Every lifecycle command writes a versioned result envelope:

```json
{
  "schema_version": 1,
  "command": "workflow.process",
  "status": "success",
  "outputs": {},
  "artifacts": [],
  "findings": [],
  "mutations": [],
  "retryable": false
}
```

The default path is `.ard/run/<command>-result.json`. A command also:

- renders the same envelope to stdout with `--json`;
- writes declared scalar job outputs to the file identified by `GITHUB_OUTPUT`;
- writes a redacted Markdown summary to `GITHUB_STEP_SUMMARY`;
- records external mutations with resource type, stable target, action, and
  non-secret result identifier; and
- writes the result before returning a non-zero exit code whenever the filesystem
  remains writable.

Output files are written atomically. Multi-line GitHub outputs use the documented
delimiter form with collision-safe delimiters. Local execution does not require
GitHub output files. The `.ard/` runtime directory is ignored by Git and excluded
from every writeback allowlist; durable public quality reports continue to use the
product `quality/` directory.

No result, exception, summary, command argument, subprocess argument list, or
mutation journal may contain a GitHub token, LLM key, Authorization header, or
unredacted secret response.

## 6. Workflow-to-command mapping

| Workflow responsibility | CLI command |
|---|---|
| Verify `ard:approved` actor permission | `ard workflow issue-authorize` |
| Parse Issue form, validate/download attachments, create branch and Draft PR | `ard workflow issue-intake` |
| Detect exactly one changed product | `ard workflow detect-product` |
| Validate untrusted source without secrets or write credentials | `ard workflow source-check` |
| Parse, model, validate, promote, commit, and publish statuses | `ard workflow process` |
| Create/update shared-table coordination and tracking PRs | `ard workflow changeset` |
| Reject mixed code/ARD changes and run repository verification | `ard workflow repository-check` |
| Expand merged product/table/changeset release targets | `ard workflow release-detect` |
| Verify readiness, create tags/bundle/Release | `ard workflow release-product` |
| Emit approved downstream repository dispatch | `ard workflow release-dispatch` |
| Reconcile Issue labels, PR comment, and failure status | `ard workflow finalize` |

The Issue, direct-change, process, changeset, release, and repository-change YAML
files keep their existing separate security jobs. They become thin wrappers; the
design does not collapse different trust or permission levels into one job merely
to reduce YAML lines.

## 7. Product-processing transaction

`ard workflow process` performs these stages:

1. normalize event data and explicit arguments into a `WorkflowContext`;
2. verify repository, branch, PR number, expected head SHA, and changed-path scope;
3. validate source count, extension, byte limit, signature, hash, path, and symlink
   policy;
4. parse HTML and Word/PDF through Docling and Excel through the cell-preserving
   adapter;
5. construct canonical Product IR and source evidence;
6. obtain strict-schema suggestions from the configured OpenAI-compatible API;
7. validate suggestions and apply only allowed semantic enrichment;
8. resolve stable product, table, column, metric, relationship, and mapping IDs;
9. classify duplicates and enforce independent numeric `v1`–`v999` transitions;
10. compute shared-table impact and changeset readiness;
11. render product/semantic Markdown, dictionary JSON, and Ossie 0.1.1 JSON;
12. create detailed quality, duplicate, version, impact, and LLM suggestion reports;
13. validate schemas, references, IDs, hashes, and quality thresholds in staging;
14. atomically promote Registry, `generated/`, and `quality/` together;
15. commit and push allowed paths using Git/LFS adapters; and
16. publish exact-head `ard/quality-gate` and `ard/changeset` statuses.

The command preserves the existing rule that validation hard errors leave the
previous Registry and generated artifacts intact while publishing a detailed
failure report. The Git commit is created only after a successful promotion and
path-scope verification.

## 8. Security and trust boundaries

### 8.1 Secret-free stages

Issue authorization, public attachment validation, fork handling, change
classification, and source preflight run without LLM secrets. Checkouts use
`persist-credentials: false` unless a later write is explicitly required.

### 8.2 LLM stage

Only a same-repository branch that passes source and changed-path checks can enter
the `ard-llm` Environment. The CLI reads provider configuration from the runner
environment and never accepts the API key as an argument. The job checks out the
validated expected head SHA, not a mutable branch tip.

LLM output is an untrusted suggestion. Strict JSON Schema, evidence references,
allowed-field checks, expression validation, and deterministic Registry logic
remain authoritative. The model cannot create or select stable IDs, approve a
duplicate, bypass a version decision, or publish a release.

### 8.3 Filesystem and subprocesses

All repository paths resolve below an explicit repository root. The filesystem
adapter rejects traversal, unexpected symlinks, device files, and writes outside
declared scopes. Subprocess adapters use argument arrays with `shell=False`, fixed
executables, bounded output, timeouts, and secret-aware redaction.

### 8.4 GitHub mutations

The GitHub adapter uses the runner-provided token and exact repository name. Every
write verifies the expected branch or head SHA immediately before mutation.
Commands use stable markers for managed comments and exact context names for
statuses. They do not approve their own PRs.

## 9. Idempotency, concurrency, and mutation journal

Lifecycle idempotency keys are:

- Issue intake: Issue number plus canonical source hashes;
- product processing: product ID, numeric version, and source-manifest hash;
- changeset readiness: changeset ID, product ID, version, PR number, and head SHA;
- release: product ID, numeric version, and merged commit SHA;
- GitHub comment: command-owned marker plus Issue/PR number; and
- status: repository, commit SHA, and status context.

The CLI treats an existing equivalent branch, commit, PR, comment, status, tag,
Release asset, or dispatch record as a successful no-op. An object with the same
stable identity but different immutable content is a conflict, not an overwrite.

GitHub Actions concurrency groups remain in YAML because scheduling is a platform
concern. The CLI still uses optimistic checks against branch tips, Registry base
versions, PR heads, and tag targets so local execution and retried jobs are safe.

The result envelope records each completed mutation. There is no automatic remote
rollback after an arbitrary GitHub partial failure. Recovery fixes the transient
cause and reruns the convergent command; conflicts require an explicit maintainer
decision or revert PR.

## 10. Exit codes and finalization

Stable process exit codes are:

| Exit code | Class | Retry expectation |
|---:|---|---|
| `0` | Success or equivalent no-op | None |
| `10` | Data, schema, or quality validation failure | Fix input |
| `20` | Configuration, dependency, or authentication failure | Fix runner/configuration |
| `30` | Transient provider or GitHub failure | Safe retry |
| `40` | Version, Registry, branch, head, or immutable-tag conflict | Rebase/review |
| `50` | Authorization, secret-boundary, path, or attachment security violation | Investigate |
| `70` | At least one remote mutation completed before a later failure | Inspect journal, then retry |

Typer parameter errors map to configuration/input failures without tracebacks.
Domain exceptions cross the CLI boundary as stable codes and concise messages;
full diagnostic detail is written only when non-secret and safe.

`if: always()` cleanup jobs invoke `ard workflow finalize`. The finalizer consumes
prior result envelopes and GitHub job results, then idempotently reconciles managed
Issue labels, PR summaries, and failure statuses. Finalization logic is not
duplicated in YAML.

## 11. Thin GitHub Actions contract

Processing jobs use pinned Actions for checkout, Python, uv, and artifacts. A
representative processing step is one command:

```yaml
- name: Process validated ARD product
  id: process
  run: >-
    uv run --frozen ard workflow process
    --repository "$GITHUB_WORKSPACE"
    --product-key "$PRODUCT_KEY"
    --branch "$BRANCH"
    --pr-number "$PR_NUMBER"
    --expected-head "$EXPECTED_HEAD"
```

The example line wrapping is YAML presentation only; it contains no shell
condition or pipeline. Runtime bootstrap uses a pinned uv setup Action so the
workflow no longer runs `pip install` or `uv sync` scripts. `uv run --frozen`
creates or reuses the locked environment without changing `uv.lock`.

Repository verification is invoked through `ard workflow repository-check`. Its
tool adapter runs pytest, Ruff, actionlint, schema synchronization, wheel content,
Ossie checksum, and secret-pattern checks using pinned versions or verified cached
tools. Actions YAML does not install or orchestrate those tools with shell code.

Workflow contract tests fail if a processing `run:` block:

- does not begin with `uv run --frozen ard`;
- contains shell control flow, a pipe, command substitution, or redirection; or
- directly invokes `git`, `gh`, `jq`, `awk`, `sed`, Python, pytest, Ruff, Go, or
  actionlint.

## 12. Migration plan and compatibility

Migration is incremental so every commit leaves a runnable repository:

1. introduce result-envelope, `WorkflowContext`, exit-code, port, and adapter
   contracts;
2. split the Typer entry point into the `cli/` package without changing commands;
3. move Git, Git LFS, and GitHub calls into tested adapters;
4. expose parse, model, validate, Registry, and GitHub granular commands;
5. migrate Issue authorization/intake and its finalizer;
6. migrate direct-change detection/source checking;
7. migrate product processing and status/writeback;
8. migrate changeset coordination;
9. migrate release detection, product release, and downstream dispatch;
10. migrate repository verification and GitHub bootstrap;
11. reduce every workflow to the Thin Actions contract; and
12. remove the temporary compatibility adapters after one project release.

During migration, a workflow switches to its CLI lifecycle only when its unit,
adapter, workflow-contract, and E2E tests pass. Old and new implementations must
not both mutate the same event. Feature flags are not stored in the public data
model; the workflow commit itself selects the implementation.

## 13. Testing strategy

### 13.1 Unit and contract tests

- pure domain behavior and application-service orchestration;
- command parsing, help text, output envelope, and exit-code snapshots;
- exact Git/GitHub request and subprocess argument contracts;
- provider configuration and strict structured-output validation;
- idempotency plan computation and conflict classification;
- result redaction with sentinel secrets; and
- workflow YAML static policy enforcement.

### 13.2 Adapter integration tests

Temporary fake `git`, `git-lfs`, `gh`, actionlint, and OpenAI-compatible servers
record calls and inject timeouts, malformed output, partial successes, and stale
heads. Tests prove that secrets appear only in authorized request channels and
never in arguments or recorded outputs. Subprocess behavior is exercised on Linux
and Windows-compatible paths and quoting.

### 13.3 End-to-end tests

- approved Issue fixture to sanitized sources, Draft PR, generated artifacts, and
  checks;
- direct product change to the same processing result;
- duplicate product/table rejection and stable ID reuse;
- independent numeric product/table version transitions;
- shared-table changeset creation, readiness, and coordinated release;
- merged PR to immutable tags, bundle, GitHub Release, and approved dispatch;
- retry after GitHub/API/LFS partial failure; and
- code-only PR validation and mixed code/ARD rejection.

The existing pytest suite, Ruff, actionlint, wheel asset check, checked-in schema
synchronization, Apache Ossie checksum, and secret-pattern scan remain mandatory.

## 14. Acceptance criteria

1. Every ARD and repository lifecycle is executable locally through `ard`.
2. GitHub Actions processing steps invoke only `uv run --frozen ard ...`.
3. No workflow contains direct Git/GitHub/JQ/AWK/sed/inline-Python business logic.
4. Trust-separated jobs and Environment approval boundaries remain intact.
5. Local and CI executions produce the same versioned result envelope.
6. Every mutating command is exact-targeted, head-aware, journaled, and
   idempotent.
7. Product/table IDs, duplicate rules, numeric versions, changesets, and Ossie
   output remain behaviorally compatible with the approved architecture.
8. API keys and tokens never appear in files, arguments, logs, summaries,
   artifacts, commits, or PR comments.
9. Failed validation does not replace the last valid Registry/generated state.
10. Full verification and Issue-to-release E2E tests pass after all workflows are
    thinned.

## 15. Related specifications

- [AI Ready Data to Ossie architecture](./2026-08-08-ai-ready-data-ossie-architecture-design.md)
- [GitHub CLI bootstrap](./2026-08-08-github-cli-bootstrap-design.md)
