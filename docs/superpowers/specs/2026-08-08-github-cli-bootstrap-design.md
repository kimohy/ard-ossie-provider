# GitHub CLI Bootstrap Design

## 1. Status and scope

This design adds a cross-platform, maintainer-operated bootstrap command for
`kimohy/ard-ossie-provider`. It configures the GitHub repository without requiring
the web settings UI and without introducing a long-lived bootstrap token into
GitHub Actions.

The command configures only repository governance needed by the existing ARD
workflows:

- repository Actions defaults;
- five ARD Issue labels;
- the `ard-llm` and `production-linkage` deployment environments;
- OpenAI-compatible provider variables and the protected LLM API key;
- initial `main` branch protection; and
- the later transition from zero to one required approving review.

It does not merge PR #1, approve deployments, create an LLM credential, add a
GitHub collaborator, rotate an existing secret without confirmation, or manage
organization-wide policy.

## 2. Decisions

1. The feature is part of the existing Python CLI, not a Bash- or PowerShell-only
   script. Windows, WSL, Linux, and macOS run the same command through `uv`.
2. GitHub CLI (`gh`) supplies authentication and performs GitHub REST calls. The
   Python process never reads the GitHub access token.
3. The API key is collected with a hidden terminal prompt and passed to
   `gh secret set` over standard input. It is never accepted as a command-line
   option, written to disk, included in JSON output, or printed in an exception.
4. Initial branch protection requires a PR and both ARD status contexts but zero
   approvals. This permits the owner-authored bootstrap PR to merge while still
   blocking direct pushes.
5. A separate command enables one required approval only after it proves that an
   eligible non-owner collaborator is available.
6. The two project-specific environments are owned by this bootstrap contract.
   The command reports their current and desired state before reconciling them.
7. All external mutations are sequential. A failure stops later writes and emits
   a partial result; the command does not attempt unsafe rollback of GitHub state.

## 3. User interface

### 3.1 Bootstrap

```text
uv run ard github bootstrap --repo kimohy/ard-ossie-provider
uv run ard github bootstrap --repo kimohy/ard-ossie-provider --dry-run
```

Interactive input:

1. `ARD_LLM_BASE_URL`, default `https://api.openai.com/v1`;
2. `ARD_LLM_MODEL`, with no provider-specific default;
3. `ARD_LLM_API_STYLE`, default `chat_completions`;
4. `ARD_MAX_ATTACHMENT_BYTES`, default `52428800`;
5. one final confirmation after a redacted plan is displayed; and
6. `ARD_LLM_API_KEY`, using a hidden prompt immediately before the Secret write.

If `ARD_LLM_API_KEY` already exists in `ard-llm`, replacement defaults to `no`.
The operator can keep the existing secret while reconciling all non-secret state.
`--dry-run` never requests or changes a secret.

Normal output is concise human-readable progress. `--json` returns the same
redacted operation result as structured JSON. Each item has `resource`, `action`,
`status`, and an optional non-sensitive `message`. Secret records contain only the
secret name and `present`, `created`, `kept`, or `replaced` status.

### 3.2 Enable review protection

```text
uv run ard github enable-review-protection \
  --repo kimohy/ard-ossie-provider
```

The command lists collaborators and verifies at least one non-owner account has
`write`, `maintain`, or `admin` permission. If none exists, it exits without
changing branch protection. If one exists, it changes
`required_approving_review_count` from `0` to `1` while preserving the rest of the
bootstrap-owned protection contract.

## 4. Component boundaries

### 4.1 CLI adapter

The CLI adapter parses options, performs interactive prompting through injected
prompt/getpass functions, displays the redacted plan, and maps domain failures to
stable exit codes. It contains no GitHub request construction.

### 4.2 GitHub CLI transport

The transport wraps `subprocess.run` for `gh`. It exposes typed operations for
GET/PUT/POST/DELETE requests, labels, variables, and secrets. JSON request bodies
and sensitive values use standard input rather than shell interpolation.

The wrapper captures bounded stdout/stderr, checks exit status, and sanitizes
messages before returning them. Calls use `--repo` or an explicit REST path, so
execution does not depend on the current Git remote.

### 4.3 Desired-state planner

The planner reads repository metadata and current settings, then produces an
ordered list of `NOOP`, `CREATE`, `UPDATE`, or `BLOCKED` operations. It is pure and
can be tested with JSON fixtures. Applying the same desired state twice must make
the second plan contain only `NOOP`, except for an explicitly requested secret
replacement.

### 4.4 Sequential reconciler

The reconciler applies the approved plan in this order:

1. labels;
2. Actions workflow defaults;
3. environments and deployment branch policies;
4. non-secret variables;
5. optional LLM secret creation or replacement; and
6. `main` branch protection.

Branch protection is last because it can immediately constrain maintainers. A
failure returns every completed and pending item. Re-running safely continues from
the observed state.

## 5. Preconditions and authorization

Before planning any mutation, the command must prove all of the following:

- `gh --version` succeeds;
- `gh auth status` succeeds for `github.com`;
- `GET /repos/kimohy/ard-ossie-provider` identifies a public, non-archived
  repository with default branch `main`;
- the authenticated user has `admin` permission; and
- Actions are available for the repository.

The command rejects a repository inferred only from a local Git remote. The
explicit `OWNER/REPO` argument is required, defaults are not silently guessed, and
the displayed plan repeats the exact target before confirmation.

A fine-grained token used by `gh` needs repository Administration write access,
Actions variable/secret management access, Issues write access for labels, and
Metadata read access. A classic token needs the access required by the equivalent
GitHub endpoints. Authentication setup remains an operator responsibility.

## 6. Desired GitHub state

### 6.1 Labels

The command creates or updates these exact names with stable descriptions and
colors:

| Label | Color | Description |
|---|---:|---|
| `ard:submission` | `1D76DB` | Public AI Ready Data submission |
| `ard:approved` | `0E8A16` | Maintainer approved public ingestion |
| `ard:processing` | `FBCA04` | ARD ingestion and conversion in progress |
| `ard:failed` | `D93F0B` | ARD ingestion or conversion failed |
| `ard:pr-created` | `5319E7` | Draft product PR created |

`gh label create --force` gives create-or-update behavior. Other repository labels
are preserved.

### 6.2 Actions defaults

The repository workflow default remains read-only. The bootstrap sets:

```json
{
  "default_workflow_permissions": "read",
  "can_approve_pull_request_reviews": true
}
```

GitHub combines PR creation and approval capability in this setting. Existing
workflows request explicit job permissions and never submit an approval, so the
additional capability is not used. The setting is nevertheless security-relevant
and is reported explicitly in the plan.

### 6.3 `ard-llm` environment

The environment contract is:

- required reviewer: repository owner `kimohy`;
- `prevent_self_review: false`, because the owner is initially the only reviewer;
- `wait_timer: 0`;
- custom deployment branch policies enabled;
- allowed branch patterns: `main` and `ard/*`;
- Environment Secret: `ARD_LLM_API_KEY`;
- Environment or repository variables used by the workflow:
  `ARD_LLM_BASE_URL`, `ARD_LLM_MODEL`, `ARD_LLM_API_STYLE`, and
  `ARD_MAX_ATTACHMENT_BYTES`.

`main` is allowed because an Issue `labeled` event runs from the default branch
before processing the sanitized `ard/issue-*` branch. `ard/*` covers Issue-created,
changeset, and direct ARD branches. GitHub deployment wildcards do not match `/`,
so nested branches such as `ard/team/change` are intentionally rejected.

The bootstrap reconciles the two required patterns exactly after displaying any
existing drift. This prevents a stale broad pattern from weakening the secret
boundary.

### 6.4 `production-linkage` environment

The environment contract is:

- required reviewer: repository owner `kimohy`;
- `prevent_self_review: false` during the single-maintainer phase;
- `wait_timer: 0`;
- custom deployment branch policies enabled; and
- exact allowed branch pattern: `main`.

It contains no LLM secret. The release workflow reaches it only after tags,
release artifacts, and checksums have been produced.

### 6.5 Initial `main` branch protection

The bootstrap uses the branch-protection endpoint for the exact `main` branch:

- required status contexts: `ard/quality-gate`, `ard/changeset`;
- strict status checks enabled, requiring the head to include current `main`;
- pull request required;
- `required_approving_review_count: 0` initially;
- conversation resolution required;
- administrator enforcement enabled;
- force pushes and deletion disabled;
- no user/team push restriction list; and
- linear history not required because merge commits remain an allowed strategy.

With admin enforcement and a required PR, the owner cannot directly push to
`main`. The zero-review phase avoids deadlocking owner-authored PR #1. The later
review-protection command modifies only the approval count after collaborator
eligibility is established.

## 7. Secret handling and redaction

The following invariants are hard requirements:

- API keys are accepted only from `getpass`, never from an option or environment
  variable;
- the prompt does not echo input;
- the value is passed as bytes to `gh secret set ARD_LLM_API_KEY --env ard-llm`
  through standard input;
- subprocess command arrays, debug logs, exceptions, plans, and JSON output never
  contain the value;
- stdout/stderr sanitization replaces the exact in-memory secret if an unexpected
  child-process message includes it;
- no dotenv, temporary secret file, shell pipeline, or command interpolation is
  used; and
- the variable holding the secret is discarded immediately after the secret call.

GitHub CLI encrypts a Secret locally before uploading it. The command can list
secret names to determine presence, but it cannot and does not read the stored
value.

## 8. Drift, errors, and recovery

Expected failures use stable codes:

| Code | Meaning |
|---|---|
| `GH_CLI_NOT_FOUND` | GitHub CLI is unavailable |
| `GH_AUTH_REQUIRED` | No usable GitHub authentication |
| `REPOSITORY_MISMATCH` | Target is not the expected public/main repository |
| `ADMIN_PERMISSION_REQUIRED` | Authenticated principal cannot configure the repository |
| `INVALID_PROVIDER_CONFIG` | Base URL, model, API style, or size is invalid |
| `BOOTSTRAP_PARTIAL` | At least one earlier mutation succeeded before a later failure |
| `ELIGIBLE_REVIEWER_NOT_FOUND` | Review count cannot safely be raised to one |

The result names completed operations and the failed operation. Recovery is to
fix authorization or configuration and rerun. Because every non-secret operation
is convergent, rerun does not duplicate labels, reviewers, or branch policies.
The command never deletes environments, secrets, labels, or branch protection as
part of error recovery.

## 9. Test strategy

### 9.1 Unit tests

- parse and validate `OWNER/REPO`;
- compute `NOOP`, `CREATE`, `UPDATE`, and `BLOCKED` plans from fixtures;
- verify exact label, Environment, variable, and branch-protection payloads;
- verify `main` plus `ard/*` branch policy reasoning;
- reject review enforcement without an eligible non-owner collaborator;
- preserve all non-approval branch-protection fields when raising review count;
- prove redacted results never contain sentinel secret values; and
- verify rerunning against desired state produces only `NOOP`.

### 9.2 Integration tests with a fake `gh`

A temporary executable records argument arrays and serves deterministic JSON.
Tests cover successful bootstrap, dry-run with zero mutations, existing-secret
keep, explicit secret replacement, an API failure after partial progress, and
Windows-compatible subprocess invocation. The recorder must assert that the
sentinel secret appears only in the secret command's standard input and nowhere
else.

### 9.3 Repository contract tests

Existing workflow-contract tests are extended to assert that bootstrap desired
state matches workflow names, Environment names, variables, labels, branch naming,
and required status contexts. Ruff, the complete pytest suite, actionlint, wheel
asset checks, and secret-pattern scans remain mandatory before publication.

## 10. Acceptance criteria

1. A maintainer can run one cross-platform CLI command without opening GitHub
   Settings.
2. A dry run shows the exact target and redacted mutations and performs no writes.
3. The normal run creates or reconciles all five labels, both environments,
   provider variables, Actions defaults, and initial branch protection.
4. The API key is stored only as the `ard-llm` Environment Secret and is absent
   from local files, process arguments, logs, JSON output, commits, and test
   artifacts.
5. The second identical run is a no-op except for an operator-approved secret
   replacement.
6. PR #1 remains mergeable after becoming ready when both ARD statuses succeed,
   without requiring self-approval.
7. Direct pushes, force pushes, and deletion of `main` are blocked for the owner.
8. Review count cannot be raised to one until a qualified non-owner collaborator
   exists.
9. Once enabled, one approving review, current `main`, resolved conversations, and
   both ARD statuses are required for every merge.

## 11. References

- [GitHub deployment environments REST API](https://docs.github.com/en/rest/deployments/environments)
- [GitHub deployment branch policies REST API](https://docs.github.com/en/rest/deployments/branch-policies)
- [GitHub Actions permissions REST API](https://docs.github.com/en/rest/actions/permissions)
- [GitHub branch protection REST API](https://docs.github.com/en/rest/branches/branch-protection)
- [GitHub CLI `gh secret set`](https://cli.github.com/manual/gh_secret_set)
- [GitHub CLI `gh variable set`](https://cli.github.com/manual/gh_variable_set)
- [GitHub CLI `gh label create`](https://cli.github.com/manual/gh_label_create)
