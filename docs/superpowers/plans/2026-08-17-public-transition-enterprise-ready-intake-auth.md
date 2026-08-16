# Public Transition with Enterprise-Ready Intake Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the repository's current public GitHub contract and bootstrap-managed `main` protection while retaining the isolated attachment-token path needed for a later private GitHub Enterprise migration.

**Architecture:** Keep `ard-private-intake` and `ARD_ATTACHMENT_TOKEN` outside bootstrap ownership, and continue injecting the token only into the trusted intake and base-sync CLI steps. Make bootstrap converge only an exact public, unarchived, admin-accessible `main` repository, including public labels and branch protection. Decode paginated `gh api` output without `--slurp` so the installed GitHub CLI 2.45.0 can perform live reconciliation.

**Tech Stack:** Python 3.13, Pydantic, Typer, pytest, `gh` 2.45.0, GitHub Actions, YAML, Ruff, actionlint

## Global Constraints

- The current repository, Issues, attachment links, branches, pull requests, Actions metadata, artifacts, tags, and Releases are public; accept only synthetic, non-confidential content authorized for public publication.
- Retain the exact initial attachment allowlist `https://github.com/user-attachments/assets/<uuid>` and the existing credential-removal rules for storage redirects.
- `ARD_ATTACHMENT_TOKEN` remains a dedicated, expiring classic PAT with `repo` scope and is stored only in the `ard-private-intake` Environment.
- Only the trusted `intake` and `base_sync` CLI steps receive `ARD_ATTACHMENT_TOKEN`; processor `secrets: inherit` behavior remains unchanged.
- Bootstrap must not inspect or mutate `ard-private-intake`, its `main` deployment policy, or `ARD_ATTACHMENT_TOKEN`.
- Bootstrap requires the exact public, unarchived `kimohy/ard-ossie-provider` repository with default branch `main` and admin permission.
- Bootstrap manages `main` branch protection with required statuses `ard/changeset` and `ard/quality-gate`, strict current-base checks, PR and conversation-resolution requirements, admin enforcement, and force-push/deletion denial.
- The GitHub CLI adapter must use `--paginate` without `--slurp` and decode one or more sequential JSON documents.
- Do not print, log, commit, or paste any Secret value; verify Secret names and metadata only.
- Enterprise migration must revalidate repository features, attachment hosts and credentials, Environment behavior, reusable-workflow Secret behavior, and protection semantics before switching back to a private-content contract.

---

### Task 1: Restore public bootstrap convergence and GitHub CLI pagination compatibility

**Files:**
- Modify: `tests/unit/test_github_bootstrap_service.py`
- Modify: `tests/unit/test_github_cli_adapter.py`
- Modify: `src/ard_ossie/application/github_bootstrap.py`
- Modify: `src/ard_ossie/adapters/github_cli.py`

**Interfaces:**
- Consumes: `RepositoryState.public`, `GitHubPort.get_branch_protection("main")`, `GitHubPort.set_branch_protection("main", state)`, and `CommandResult.stdout` from `gh api --paginate`.
- Produces: `GitHubBootstrapService.plan(config) -> BootstrapPlan` containing `branch:main`; public label desired state; `GitHubCli._decode_paginated(result, code) -> dict[str, Any] | list[Any]`.

- [ ] **Step 1: Make the bootstrap tests express the current public contract**

Set `FakeGitHub.repository_state.public=True` by default. Replace the visibility and ownership tests with these assertions, and restore `branch:main` to the exact plan target list:

```python
def test_bootstrap_accepts_public_repository_and_converges_public_labels() -> None:
    github = FakeGitHub()
    service = GitHubBootstrapService(REPOSITORY, github)

    service.apply(service.plan(provider_config()), api_key="sentinel-key")

    assert github.labels["ard:submission"].description == "Public AI Ready Data submission"
    assert github.labels["ard:approved"].description == "Maintainer approved public ingestion"
    assert "ard-private-intake" not in github.environments
    assert set(github.environment_reads) == {"ard-llm", "production-linkage"}
    assert set(github.environment_secret_reads) == {"ard-llm"}


def test_bootstrap_rejects_private_repository_before_mutation() -> None:
    github = FakeGitHub()
    github.repository_state = replace(github.repository_state, public=False)

    with pytest.raises(WorkflowConfigurationError) as captured:
        GitHubBootstrapService(REPOSITORY, github).plan(provider_config())

    assert captured.value.code == "REPOSITORY_MISMATCH"
    assert github.labels == {}
    assert github.environments == {}
```

The target list must end with:

```python
        "environment:production-linkage",
        "branch:main",
```

- [ ] **Step 2: Add branch-protection planning and apply assertions**

Replace the temporary Free-private exclusion test with:

```python
def test_bootstrap_plans_and_applies_public_branch_protection() -> None:
    github = FakeGitHub()
    service = GitHubBootstrapService(REPOSITORY, github)

    plan = service.plan(provider_config())
    branch_item = next(item for item in plan.items if item.target == "branch:main")
    result = service.apply(plan, api_key="sentinel-key")

    assert branch_item.action == "create"
    assert github.protection == BranchProtectionState(
        required_statuses=("ard:changeset", "ard/quality-gate"),
        strict=True,
        enforce_admins=True,
        required_approving_review_count=0,
        require_conversation_resolution=True,
        allow_force_pushes=False,
        allow_deletions=False,
        require_pull_request=True,
    )
    assert github.protection_writes == ["main"]
    assert any(mutation.resource == "branch_protection" for mutation in result.mutations)
```

In `test_apply_replans_noop_resources_after_confirmation_drift`, set `github.protection = None`, apply the previously confirmed plan, and assert that protection is recreated and a `branch_protection` mutation is returned. This proves apply replans immediately before mutation.

- [ ] **Step 3: Run the bootstrap tests and verify the temporary implementation fails**

Run:

```bash
uv run --frozen pytest tests/unit/test_github_bootstrap_service.py -q
```

Expected: failures show that private visibility is still accepted, public labels are not desired, and `branch:main` is absent.

- [ ] **Step 4: Restore the public bootstrap desired state**

In `src/ard_ossie/application/github_bootstrap.py`, set the first two labels to:

```python
    LabelState(
        name="ard:submission",
        color="1d76db",
        description="Public AI Ready Data submission",
    ),
    LabelState(
        name="ard:approved",
        color="0e8a16",
        description="Maintainer approved public ingestion",
    ),
```

After `environment:production-linkage` planning, read `main` protection and append:

```python
        current_protection = self.github.get_branch_protection("main")
        items.append(
            BootstrapItem(
                target="branch:main",
                action=(
                    "create"
                    if current_protection is None
                    else (
                        "noop"
                        if current_protection == _bootstrap_protection(current_protection)
                        else "update"
                    )
                ),
            )
        )
```

Before the postcondition plan in `apply`, restore:

```python
            current_protection = self.github.get_branch_protection("main")
            desired_protection = _bootstrap_protection(current_protection)
            if current_protection != desired_protection:
                mutations.append(
                    self.github.set_branch_protection("main", desired_protection)
                )
```

Change `_require_repository` to reject `not repository.public` and use the exact message `bootstrap requires the exact public main repository`.

- [ ] **Step 5: Verify the existing pagination regression tests cover `gh` 2.45.0**

Keep these two adapter test contracts:

```python
def test_paginated_api_avoids_unsupported_slurp_flag() -> None:
    runner = RecordingRunner([ok([])])
    assert GitHubCli(REPOSITORY, runner).list_labels() == {}
    assert "--paginate" in runner.requests[0].argv
    assert "--slurp" not in runner.requests[0].argv


def test_paginated_api_decodes_concatenated_pages() -> None:
    first_page = json.dumps([{"name": "ard:submission", "color": "1d76db", "description": "public"}])
    second_page = json.dumps([{"name": "ard:approved", "color": "0e8a16", "description": "approved"}])
    runner = RecordingRunner([CommandResult(returncode=0, stdout=f"{first_page}\n{second_page}\n", stderr="")])
    labels = GitHubCli(REPOSITORY, runner).list_labels()
    assert set(labels) == {"ard:submission", "ard:approved"}
```

The adapter must append only `--paginate`, parse sequential documents with `json.JSONDecoder().raw_decode`, preserve a single page as that page, and return multiple pages as a list.

- [ ] **Step 6: Run focused tests and formatting**

Run:

```bash
uv run --frozen pytest tests/unit/test_github_bootstrap_service.py tests/unit/test_github_cli_adapter.py -q
uv run --frozen ruff check src/ard_ossie/application/github_bootstrap.py src/ard_ossie/adapters/github_cli.py tests/unit/test_github_bootstrap_service.py tests/unit/test_github_cli_adapter.py
uv run --frozen ruff format --check src/ard_ossie/application/github_bootstrap.py src/ard_ossie/adapters/github_cli.py tests/unit/test_github_bootstrap_service.py tests/unit/test_github_cli_adapter.py
```

Expected: all commands exit 0.

- [ ] **Step 7: Run the mutation-free live bootstrap plan**

Run:

```bash
uv run --frozen ard github bootstrap --repo kimohy/ard-ossie-provider --dry-run
```

Expected: exit 0; redacted output targets the exact public repository, includes `branch:main`, does not contain `ard-private-intake`, and does not request a Secret value.

- [ ] **Step 8: Commit the public bootstrap boundary**

```bash
git add src/ard_ossie/application/github_bootstrap.py src/ard_ossie/adapters/github_cli.py tests/unit/test_github_bootstrap_service.py tests/unit/test_github_cli_adapter.py
git commit -m "fix: restore public GitHub bootstrap"
```

---

### Task 2: Align the public submission contract, operating docs, and E2E fixture

**Files:**
- Modify: `.github/ISSUE_TEMPLATE/ard-content.yml`
- Modify: `README.md`
- Modify: `docs/github-actions-setup.md`
- Modify: `docs/github-enterprise-migration.md`
- Modify: `docs/next-steps.md`
- Modify: `docs/superpowers/specs/2026-08-08-github-cli-bootstrap-design.md`
- Modify: `docs/superpowers/specs/2026-08-08-ai-ready-data-ossie-architecture-design.md`
- Modify: `docs/superpowers/specs/2026-08-16-shared-table-changeset-e2e-design.md`
- Modify: `docs/superpowers/plans/2026-08-16-shared-table-changeset-e2e.md`
- Modify: `tests/e2e/test_approved_issue_to_release.py`
- Modify: `tests/fixtures/github/approved-issue.json`

**Interfaces:**
- Consumes: the approved public transition design and the retained `ARD_ATTACHMENT_TOKEN` E2E transport assertion.
- Produces: an Issue Form and active documentation that make public visibility explicit, plus an E2E scenario named and described as authenticated public Issue intake.

- [ ] **Step 1: Change the Issue Form to require public-publication authorization**

Set the form description and opening controls to:

```yaml
description: Submit one public-safe ARD product for validation and Ossie conversion
...
  - type: markdown
    attributes:
      value: |
        저장소, Issue, 첨부파일, 생성 결과와 Release는 누구나 볼 수 있습니다.
        공개 권한이 있는 합성·비기밀 데이터만 제출하고 비밀정보, 개인정보, 고객 데이터, 내부 문서, 접근 토큰을 첨부하지 마세요.
  - type: checkboxes
    id: public_authorization
    attributes:
      label: Public publication authorization
      options:
        - label: I am authorized to publish this synthetic, non-confidential content publicly.
          required: true
```

Do not change the operation, product identity, attachment, changeset, or change-reason fields.

- [ ] **Step 2: Restore public runtime wording in active operator docs**

Update `README.md`, `docs/github-actions-setup.md`, and `docs/next-steps.md` so they state:

- current GitHub.com operation is public and inputs/artifacts may be visible;
- only public-safe synthetic content is allowed;
- bootstrap manages public labels and `main` protection;
- `ard-private-intake` and its classic PAT remain separate rollout-managed Enterprise-readiness controls;
- only intake/base-sync trusted CLI steps receive the PAT;
- public GitHub attachments do not need the PAT today, but the path remains exercised;
- the two required statuses are enforced by public `main` protection;
- `ard github enable-review-protection` remains deferred until a non-owner writer exists.

Use the exact Secret provisioning command, without putting a value on the command line:

```bash
gh secret set ARD_ATTACHMENT_TOKEN \
  --repo kimohy/ard-ossie-provider \
  --env ard-private-intake
```

- [ ] **Step 3: Correct architecture and shared-table lifecycle wording**

In the architecture spec, describe repository mutations as using `GITHUB_TOKEN` and public Issue attachment downloads as exercising only the isolated `ARD_ATTACHMENT_TOKEN`; retain the approval actor permission check. Replace `private Issue` lifecycle wording with `public Issue` in the shared-table design and plan, including the seed and two update Issues. Do not change the underlying hashes, product IDs, version sequence, changeset behavior, or exact-head publication contract.

- [ ] **Step 4: Mark historical bootstrap policy and Enterprise boundaries accurately**

At the top of `docs/superpowers/specs/2026-08-08-github-cli-bootstrap-design.md`, point current attachment and migration policy to:

```text
docs/superpowers/specs/2026-08-16-public-transition-enterprise-ready-intake-auth-design.md
```

Keep that historical design's public branch-protection behavior because it is current again. In `docs/github-enterprise-migration.md`, state that the current bootstrap intentionally accepts only public GitHub.com runtime and must not be used as a private Enterprise readiness probe. Require product-specific mutation-free read-back for private visibility, Environment features, attachment hosts, token type, branch/ruleset semantics, Actions, runner, API, LFS, and Release behavior.

- [ ] **Step 5: Rename the E2E scenario without weakening token coverage**

Rename:

```python
def test_approved_public_issue_with_attachment_auth_to_numeric_release_is_reproducible_and_traceable(
```

Keep:

```python
attachment_token = "e2e-attachment-token"
monkeypatch.setenv("ARD_ATTACHMENT_TOKEN", attachment_token)
with httpx.Client(transport=attachment_transport(attachment_token)) as client:
```

In `tests/fixtures/github/approved-issue.json`, change the reason to `Initial authenticated public Issue ingestion`.

- [ ] **Step 6: Validate YAML, public/private wording, workflow credential scope, and E2E behavior**

Run:

```bash
uv run --frozen python - <<'PY'
from pathlib import Path
import yaml

payload = yaml.safe_load(Path(".github/ISSUE_TEMPLATE/ard-content.yml").read_text())
assert payload["description"] == "Submit one public-safe ARD product for validation and Ossie conversion"
assert payload["body"][1]["id"] == "public_authorization"
PY
rg -n "private Issue|private ARD|GitHub Free private|private repository authorization" README.md .github/ISSUE_TEMPLATE/ard-content.yml docs/github-actions-setup.md docs/next-steps.md docs/superpowers/specs/2026-08-08-ai-ready-data-ossie-architecture-design.md docs/superpowers/specs/2026-08-16-shared-table-changeset-e2e-design.md docs/superpowers/plans/2026-08-16-shared-table-changeset-e2e.md
rg -n "ARD_ATTACHMENT_TOKEN" .github/workflows src tests docs/github-actions-setup.md
uv run --frozen pytest tests/e2e/test_approved_issue_to_release.py tests/integration/test_workflow_contracts.py -q
git diff --check
```

Expected: the first `rg` exits 1 with no stale current-runtime matches; the credential search shows exactly the documented trusted workflow scope; pytest and diff check exit 0.

- [ ] **Step 7: Commit the public contract**

```bash
git add .github/ISSUE_TEMPLATE/ard-content.yml README.md docs/github-actions-setup.md docs/github-enterprise-migration.md docs/next-steps.md docs/superpowers/specs/2026-08-08-github-cli-bootstrap-design.md docs/superpowers/specs/2026-08-08-ai-ready-data-ossie-architecture-design.md docs/superpowers/specs/2026-08-16-shared-table-changeset-e2e-design.md docs/superpowers/plans/2026-08-16-shared-table-changeset-e2e.md tests/e2e/test_approved_issue_to_release.py tests/fixtures/github/approved-issue.json
git commit -m "docs: restore public repository contract"
```

---

### Task 3: Verify, provision, converge GitHub, and publish the exact reviewed head

**Files:**
- Verify: `.github/workflows/ard-issue-intake.yml`
- Verify: `.github/workflows/ard-base-sync.yml`
- Verify: all changed Python, YAML, JSON, and Markdown files
- External state: `kimohy/ard-ossie-provider` repository settings, `ard-private-intake` Secret metadata, Environments, labels, and `main` protection

**Interfaces:**
- Consumes: the exact Task 1–2 branch head, repository admin access, a dedicated classic PAT entered through hidden local input, and the GitHub Actions checks for the exact PR head.
- Produces: a reviewed PR whose exact head passes local and remote verification, plus read-back evidence that public repository settings and isolated attachment Secret metadata match the design.

- [ ] **Step 1: Run the complete local verification matrix**

Run:

```bash
uv run --frozen pytest -q
git diff --name-only -z origin/main...HEAD -- '*.py' | xargs -0 uv run --frozen ruff check
git diff --name-only -z origin/main...HEAD -- '*.py' | xargs -0 uv run --frozen ruff format --check
uv run --frozen ard workflow repository-check
uv run --frozen python -m build
git diff --check
```

Expected: every command exits 0; repository-check includes Ruff, workflow YAML, checksum-validated actionlint, schema/catalog, and Secret scan checks.

- [ ] **Step 2: Request an independent code review and resolve findings**

Review the exact `origin/main...HEAD` diff for spec coverage, public/private wording, Secret containment, redirect credential stripping, error redaction, branch-protection convergence, pagination decoding, and tests. Apply only verified findings, rerun the focused tests after each correction, and repeat Step 1 after the final correction.

- [ ] **Step 3: Provision only the attachment Secret through hidden input**

Run locally and paste the PAT only into the hidden prompt:

```bash
gh secret set ARD_ATTACHMENT_TOKEN \
  --repo kimohy/ard-ossie-provider \
  --env ard-private-intake
```

Then read metadata only:

```bash
gh secret list --repo kimohy/ard-ossie-provider --env ard-private-intake
```

Expected: `ARD_ATTACHMENT_TOKEN` appears by name with an update timestamp; no value is displayed.

- [ ] **Step 4: Dry-run and apply public bootstrap convergence**

Run:

```bash
uv run --frozen ard github bootstrap --repo kimohy/ard-ossie-provider --dry-run
uv run --frozen ard github bootstrap --repo kimohy/ard-ossie-provider
uv run --frozen ard github bootstrap --repo kimohy/ard-ossie-provider --dry-run
```

Expected: the first plan includes current drift such as public labels, Environment reviewers, or `branch:main`; apply succeeds without requesting a replacement LLM Secret; the final plan contains only `noop` items. Bootstrap output never includes `ard-private-intake`.

- [ ] **Step 5: Read back exact public GitHub state without Secret values**

Run:

```bash
gh api repos/kimohy/ard-ossie-provider --jq '{full_name,visibility,private,archived,default_branch,permissions}'
gh api repos/kimohy/ard-ossie-provider/branches/main/protection --jq '{required_status_checks,required_pull_request_reviews,enforce_admins,required_conversation_resolution,allow_force_pushes,allow_deletions}'
gh api repos/kimohy/ard-ossie-provider/environments/ard-private-intake --jq '{name,protection_rules,deployment_branch_policy}'
gh api repos/kimohy/ard-ossie-provider/environments/ard-private-intake/deployment-branch-policies --jq '{total_count,branch_policies}'
gh secret list --repo kimohy/ard-ossie-provider --env ard-private-intake
```

Expected: visibility is `public`, repository is not archived, default branch is `main`, required statuses are exact, protection forbids force pushes/deletions, the intake Environment permits only `main`, and only Secret metadata is printed.

- [ ] **Step 6: Push the branch and open a reviewed PR**

Record the exact head, push it, and create a PR:

```bash
git rev-parse HEAD
git push --set-upstream origin design/private-repository-intake-auth
gh pr create --repo kimohy/ard-ossie-provider --base main --head design/private-repository-intake-auth --title "Secure Issue attachment intake and restore public policy" --body-file /tmp/ard-public-transition-pr.md
```

The PR body file must summarize the retained PAT boundary, public transition, bootstrap/pagination fix, verification commands, Secret metadata read-back, and Enterprise revalidation boundary without containing credentials.

- [ ] **Step 7: Require same-head CI and merge protection**

Run:

```bash
gh pr view --repo kimohy/ard-ossie-provider --json number,headRefOid,mergeStateStatus,reviewDecision,statusCheckRollup
gh pr checks --repo kimohy/ard-ossie-provider --watch
```

Compare `headRefOid` to the reviewed local `git rev-parse HEAD`. Merge only when every required check is successful and review is satisfied:

```bash
gh pr merge --repo kimohy/ard-ossie-provider --merge --match-head-commit "$(git rev-parse HEAD)"
```

- [ ] **Step 8: Retry the synthetic Issue #46 acceptance path after merge**

Remove and reapply `ard:approved` on Issue #46, then require successful authorization, routing, authenticated attachment intake, processing, finalization, exact source hashes, and the managed Draft PR. Confirm every attachment is synthetic and public-safe before triggering the workflow; do not use confidential Enterprise migration samples.

- [ ] **Step 9: Record final evidence**

Record the merged PR number, merge commit, successful workflow run URLs, Issue #46 run and Draft PR, exact source hashes, Secret name/update timestamp, public visibility, Environment `main` policy, and `main` protection summary in `docs/next-steps.md`. Do not record Secret values or signed attachment URLs.
