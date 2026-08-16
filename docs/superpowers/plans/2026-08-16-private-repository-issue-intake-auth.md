# Private Repository Issue Intake Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep `kimohy/ard-ossie-provider` private while approved Issue intake downloads private GitHub `user-attachments` with an isolated classic PAT.

**Architecture:** Store a dedicated bot's classic PAT only in the `ard-private-intake` Environment as `ARD_ATTACHMENT_TOKEN`. Only the mutually exclusive `intake` and `base_sync` jobs enter that Environment, and only their trusted CLI steps inject the Secret. Python requires the credential for exact `github.com` requests and strips auth before every allowed storage redirect.

**Tech Stack:** Python 3.12, httpx, pytest, PyYAML, GitHub Actions, GitHub Environments, GitHub CLI, Ruff, actionlint

## Global Constraints

- Implement `docs/superpowers/specs/2026-08-16-private-repository-issue-intake-auth-design.md`.
- Keep repository visibility `PRIVATE` and default branch `main`.
- Use a dedicated bot classic PAT with `repo` scope, explicit expiry, and access only to this repository.
- Store it only as `ARD_ATTACHMENT_TOKEN` in `ard-private-intake`, whose only branch policy is `main`.
- Never expose the token in arguments, Issue fields, inputs, files, logs, artifacts, results, PR text, shell history, or chat.
- Never fall back to `GH_TOKEN`; keep both processor `secrets: inherit` declarations unchanged.
- Preserve URL/redirect allowlists, size/type/magic/container/hash checks, atomic writes, and cleanup.
- Follow red-green-refactor and commit each task independently.
- Do not merge before the Environment, branch policy, and Secret metadata exist.

---

### Task 1: Require and isolate the attachment token

**Files:**
- Modify: `tests/unit/test_github_event.py`
- Modify: `src/ard_ossie/github_event.py:157-164,265-337`

**Interfaces:**
- Consumes: optional `attachment_token: str | None`, otherwise `ARD_ATTACHMENT_TOKEN`.
- Produces: `_resolve_attachment_token(value: str | None) -> str`, `_attachment_request_headers(url: str, token: str) -> dict[str, str]`, and an extended `download_attachment()`.

- [ ] **Step 1: Add a default token fixture and missing-token RED test**

```python
@pytest.fixture(autouse=True)
def private_attachment_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARD_ATTACHMENT_TOKEN", "fixture-attachment-token")
```

```python
def test_download_requires_attachment_token_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(404)

    monkeypatch.delenv("ARD_ATTACHMENT_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "must-not-be-used")
    attachment = IntakeAttachment(
        role="dictionary_excel", filename="dictionary.xlsx", url=FILE_ATTACHMENT_URL
    )
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(AttachmentSecurityError, match="ATTACHMENT_TOKEN_REQUIRED"),
    ):
        download_attachment(attachment, tmp_path / "dictionary.xlsx", client=client)
    assert called is False
```

- [ ] **Step 2: Add RED precedence and redirect assertions**

Modify the existing authenticated redirect test to set both `ARD_ATTACHMENT_TOKEN` and a decoy
`GH_TOKEN`, then call:

```python
result = download_attachment(
    attachment,
    target,
    client=client,
    attachment_token="explicit-attachment-token",
)
assert requests[0].headers["authorization"] == "Bearer explicit-attachment-token"
assert "authorization" not in requests[1].headers
```

Add a second invocation without the argument and require the Environment value, never `GH_TOKEN`.

- [ ] **Step 3: Verify RED**

Run:
`uv run --frozen pytest tests/unit/test_github_event.py::test_download_requires_attachment_token_before_network tests/unit/test_github_event.py::test_download_authenticates_github_without_leaking_credentials_to_storage -q`

Expected: current code reaches transport without a token and rejects the new keyword argument.

- [ ] **Step 4: Implement the minimal resolver**

```python
_ATTACHMENT_TOKEN_ENV = "ARD_ATTACHMENT_TOKEN"


def _resolve_attachment_token(value: str | None) -> str:
    token = value if value is not None else os.environ.get(_ATTACHMENT_TOKEN_ENV)
    if token is None or not token.strip():
        raise AttachmentSecurityError("ATTACHMENT_TOKEN_REQUIRED")
    return token


def _attachment_request_headers(url: str, token: str) -> dict[str, str]:
    if (urlsplit(url).hostname or "").lower() != "github.com":
        return {}
    return {"Authorization": f"Bearer {token}"}
```

Add `attachment_token: str | None = None` to `download_attachment`, resolve it immediately after
initial URL validation, and pass it to the header helper for each rebuilt request. Retain header
removal, `auth=None`, `follow_redirects=False`, closing, and all existing validation.

- [ ] **Step 5: Verify GREEN and commit**

Run `uv run --frozen pytest tests/unit/test_github_event.py -q`, then commit only the source and unit
test as `fix: require private attachment token`.

---

### Task 2: Restrict the Secret to two workflow steps

**Files:**
- Modify: `tests/integration/test_workflow_contracts.py:332-425`
- Modify: `tests/unit/test_workflow_secret_contract.py`
- Modify: `.github/workflows/ard-issue-intake.yml:94-181`

**Interfaces:**
- Consumes: `ard-private-intake` and `secrets.ARD_ATTACHMENT_TOKEN`.
- Produces: exactly two Environment-bound jobs and exactly two CLI-step Secret references.

- [ ] **Step 1: Add RED parsed-workflow assertions**

Extend the Issue workflow integration test:

```python
assert intake["environment"] == "ard-private-intake"
intake_run = next(step for step in intake["steps"] if step.get("id") == "intake")
assert intake_run["env"] == {
    "ARD_ATTACHMENT_TOKEN": "${{ secrets.ARD_ATTACHMENT_TOKEN }}",
}
assert base_sync["environment"] == "ard-private-intake"
assert base_sync_run["env"] == {
    "PYTHONSAFEPATH": "1",
    "ARD_ATTACHMENT_TOKEN": "${{ secrets.ARD_ATTACHMENT_TOKEN }}",
}
```

Add this method to `TestWorkflowSecretContract`:

```python
def test_attachment_secret_is_limited_to_private_intake_commands(self) -> None:
    issue_path = Path(".github/workflows/ard-issue-intake.yml")
    jobs = _workflow(issue_path)["jobs"]
    references: set[tuple[str, str]] = set()
    for job_name, job in jobs.items():
        if isinstance(job, dict):
            for step in job.get("steps", []):
                if isinstance(step, dict) and "ARD_ATTACHMENT_TOKEN" in str(step):
                    references.add((job_name, str(step.get("id"))))
    self.assertEqual(references, {("intake", "intake"), ("base_sync", "base_sync")})
    for path in _workflow_paths(ROOT):
        if path.relative_to(ROOT) != issue_path:
            self.assertNotIn("ARD_ATTACHMENT_TOKEN", path.read_text(encoding="utf-8"))
```

- [ ] **Step 2: Verify RED**

Run `uv run --frozen pytest tests/integration/test_workflow_contracts.py::test_issue_intake_routes_existing_drafts_through_trusted_base_sync tests/unit/test_workflow_secret_contract.py -q`.

Expected: Environment and Secret keys are absent.

- [ ] **Step 3: Apply the minimal workflow change**

Add `environment: ard-private-intake` to `intake` and `base_sync`. Add only this key to the intake
CLI step and alongside `PYTHONSAFEPATH` in the base-sync CLI step:

```yaml
ARD_ATTACHMENT_TOKEN: ${{ secrets.ARD_ATTACHMENT_TOKEN }}
```

Do not change job-level env, another workflow, reusable inputs, or either `secrets: inherit`.

- [ ] **Step 4: Verify GREEN and commit**

Run the two test files plus `actionlint .github/workflows/*.yml`. Commit the workflow and its two
tests as `ci: isolate private attachment credential`.

---

### Task 3: Replace the active public contract

**Files:**
- Modify: `.github/ISSUE_TEMPLATE/ard-content.yml`
- Modify: `README.md`
- Modify: `docs/github-actions-setup.md`
- Modify: `docs/superpowers/specs/2026-08-08-ai-ready-data-ossie-architecture-design.md`
- Modify: `docs/superpowers/plans/2026-08-08-repository-bootstrap.md`
- Modify: `docs/superpowers/plans/2026-08-08-cli-first-01-foundation.md`

**Interfaces:**
- Consumes: the approved private design and Task 2 runtime names.
- Produces: private consent, operating/rotation instructions, and historical supersession.

- [ ] **Step 1: Capture the current public contract before editing**

```text
rg -n -i "public ARD|공개 저장소 Issue|공개 수집|may be published publicly" \
  README.md .github/ISSUE_TEMPLATE/ard-content.yml docs/github-actions-setup.md \
  docs/superpowers/specs/2026-08-08-ai-ready-data-ossie-architecture-design.md
uv run --frozen python -c "import pathlib,yaml; form=yaml.safe_load(pathlib.Path('.github/ISSUE_TEMPLATE/ard-content.yml').read_text()); assert form['body'][1]['id'] == 'private_authorization'"
```

Expected: `rg` finds the superseded public language and the Python assertion fails on
`public_acknowledgement`. This is a one-time configuration/prose check, not a permanent source-text
test; durable automated tests remain focused on observable Python and workflow behavior.

- [ ] **Step 3: Update the Issue Form and README**

Use the exact description `Submit one private ARD product for validation and Ossie conversion`.
Use checkbox ID `private_authorization`, label `Private repository authorization`, and option
`I am authorized to submit this content to the private repository.`. State in Korean that data is
recorded in a private repository, authorization is required, and secrets/personal data/tokens remain
forbidden. Change README review step 2 to require repository-ingestion authority and policy review.

- [ ] **Step 4: Update operations, architecture, and history**

Document the isolated PAT exception, Environment/Secret names, dedicated bot, classic `repo` scope,
expiry, `main` policy, rotation, and private artifact/Release semantics in the setup guide. Replace
active public Issue/Release clauses in the architecture spec. Add this notice near the top of each
historical plan without erasing old steps:

```markdown
> **Superseded policy (2026-08-16):** Public visibility requirements in this historical plan are
> replaced by the private repository and Issue intake contract in
> `docs/superpowers/specs/2026-08-16-private-repository-issue-intake-auth-design.md`.
```

- [ ] **Step 5: Validate the private contract and commit**

Parse the Issue Form with PyYAML and require the exact private ID/label/required option. Run
`git diff --check`, search active docs for `public ARD`, `공개 저장소 Issue`, `공개 수집`, and
`may be published publicly`, and require no match. Commit the listed files as
`docs: adopt private repository intake policy`; do not add a persistent test that only freezes
human-facing prose.

---

### Task 4: Verify and publish the branch

**Files:**
- Verify: all Task 1-3 changes plus this plan and design

**Interfaces:**
- Consumes: branch based on main merge `bdc96c213b00ce62fa36c64fc4ecce7e5b186020`.
- Produces: exact reviewed head and green same-head CI.

- [ ] **Step 1: Run local verification**

Run, in order:

```text
uv run --frozen pytest tests/unit/test_github_event.py tests/unit/test_workflow_secret_contract.py tests/integration/test_workflow_contracts.py -q
uv run --frozen pytest -q
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
actionlint .github/workflows/*.yml
uv run --frozen pytest tests/unit/test_model_schema_verification.py -q
uv run --frozen pytest tests/integration/test_wheel_assets.py -q
git diff origin/main...HEAD --check
```

- [ ] **Step 2: Audit the complete diff**

Inspect `git diff origin/main...HEAD` and search for `ARD_ATTACHMENT_TOKEN`, `secrets: inherit`, and
`environment: ard-private-intake`. Require one production env read, two workflow Secret references,
two Environment jobs, unchanged inheritance, and no credential value/header logging.

- [ ] **Step 3: Push and open a focused PR**

Create `.ard/run/private-intake-pr-body.md` with `apply_patch`, including design summary, credential
boundary, RED/GREEN evidence, full test count, and rollout gate. Push
`design/private-repository-intake-auth` and open a PR titled
`fix: support private issue attachment intake` against `main` using that body file.

```text
git push -u origin design/private-repository-intake-auth
gh pr create --repo kimohy/ard-ossie-provider --base main \
  --head design/private-repository-intake-auth \
  --title "fix: support private issue attachment intake" \
  --body-file .ard/run/private-intake-pr-body.md
```

- [ ] **Step 4: Review and require same-head CI**

Use `superpowers:requesting-code-review` against `origin/main...HEAD`. Resolve Critical/Important
findings with new RED/GREEN cycles. Record the exact SHA and require static/check, model-schemas,
pytest, wheel, and finalizer success at that same head.

---

### Task 5: Provision, merge, and retry Issue #46

**Files:**
- Update runtime-only `.ard/run/shared-table-e2e-state.json`

**Interfaces:**
- Consumes: dedicated bot PAT supplied locally, green PR, Issue #46, and recorded hashes.
- Produces: protected Environment, merged code, private metadata, successful intake, and resumed acceptance.

- [ ] **Step 1: Create the Environment before merge**

Use `gh api` to PUT `repos/kimohy/ard-ossie-provider/environments/ard-private-intake` with
`wait_timer=0`, `prevent_self_review=false`, protected branches false, and custom branch policies
true. POST one deployment branch policy with `name=main` and `type=branch`. Read it back and require
no reviewer/wait rule and exactly one `main` policy.

```text
gh api --method PUT repos/kimohy/ard-ossie-provider/environments/ard-private-intake \
  -F wait_timer=0 -F prevent_self_review=false \
  -F 'deployment_branch_policy[protected_branches]=false' \
  -F 'deployment_branch_policy[custom_branch_policies]=true'
gh api --method POST \
  repos/kimohy/ard-ossie-provider/environments/ard-private-intake/deployment-branch-policies \
  -f name=main -f type=branch
gh api repos/kimohy/ard-ossie-provider/environments/ard-private-intake
gh api repos/kimohy/ard-ossie-provider/environments/ard-private-intake/deployment-branch-policies
```

- [ ] **Step 2: Provision the PAT without exposing it**

The user runs locally:

```text
gh secret set ARD_ATTACHMENT_TOKEN --repo kimohy/ard-ossie-provider --env ard-private-intake
gh secret list --repo kimohy/ard-ossie-provider --env ard-private-intake
```

Paste only into the hidden prompt and require only the Secret name in output.

- [ ] **Step 3: Merge and update live labels**

Merge `design/private-repository-intake-auth` with `--match-head-commit` set to the exact reviewed
`git rev-parse HEAD`. Change `ard:submission` to `Private AI Ready Data submission` and
`ard:approved` to `Maintainer approved private ingestion`. Read back visibility `PRIVATE`, default
branch `main`, labels, Environment policy, and Secret metadata.

- [ ] **Step 4: Retry only Issue #46**

Remove and reapply `ard:approved` on Issue #46. Require authorization, route, intake, processor, and
finalizer success; use the existing `ard-llm` approval process if requested.

- [ ] **Step 5: Verify identity and convergence**

Require product `500138302` v1, original URLs, one managed Draft PR, successful quality/changeset
statuses, removal of `ard:failed`, no credential text, and these source hashes:

```text
HTML  b39248654c0cd9b6f3f28111a6c44036d86a2440a1d7dc2c9bfd7bd40281d7f9
PDF   ca630eac7231e454a2398e2f1e25328490966ab1e110230f1c5eaba6ab367cf6
XLSX  10310e99c8a76b4b030935c432e6f879ac4c56361ee4a6d52d6a17b2726c306a
```

- [ ] **Step 6: Record evidence and resume acceptance**

Update the ignored state JSON with PR/head/merge, CI and intake run/job IDs, Environment/Secret
metadata timestamps, Draft PR, hashes, and labels. Validate with `jq -e .` and resume the parent
shared-table plan only after all checks pass.
