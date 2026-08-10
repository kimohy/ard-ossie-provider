from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOWS = Path(".github/workflows")
PROCESSING_WORKFLOWS = tuple(
    WORKFLOWS / name
    for name in (
        "ard-issue-intake.yml",
        "ard-direct-change.yml",
        "ard-process.yml",
        "ard-changeset.yml",
        "ard-release.yml",
        "ard-repository-change.yml",
        "ard-initial-bootstrap.yml",
    )
)
FORBIDDEN_RUN_TOKENS = (
    "git ",
    "gh ",
    "jq ",
    "awk ",
    "sed ",
    "python ",
    "pytest",
    "ruff",
    "actionlint",
    "go ",
    "curl ",
    "wget ",
)
FORBIDDEN_SHELL_CONTROL = ("&&", "||", ";", "`", "$(`", "<(", ">(")
APPROVED_LIFECYCLES = (
    "workflow issue-authorize",
    "workflow issue-intake",
    "workflow detect-product",
    "workflow source-check",
    "workflow ensure-product-pr",
    "workflow process",
    "workflow process-reconcile",
    "workflow changeset",
    "workflow finalize",
    "workflow release-detect",
    "workflow release-product",
    "workflow release-dispatch",
    "workflow repository-check",
)


def load_workflow(name: str) -> dict:
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def assert_actions_are_sha_pinned(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.search(r"\buses:\s*([^\s#]+)", line)
        if match is None or match.group(1).startswith("./"):
            continue
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", match.group(1)), line


def test_processing_run_steps_only_invoke_ard_cli() -> None:
    for path in PROCESSING_WORKFLOWS:
        workflow = load_workflow(path.name)
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if "run" not in step:
                    continue
                command = step["run"].strip()
                assert command.startswith("uv run --frozen ard "), (
                    f"{path.name}: {step.get('name', '<unnamed>')}"
                )
                assert "${{" not in command, (
                    f"{path.name}: expressions must enter commands through env"
                )
                assert not any(token in command for token in FORBIDDEN_RUN_TOKENS), (
                    f"{path.name}: {step.get('name', '<unnamed>')}"
                )
                assert not any(token in command for token in FORBIDDEN_SHELL_CONTROL), (
                    f"{path.name}: shell control syntax is forbidden"
                )
                assert any(
                    f"ard {lifecycle}" in command for lifecycle in APPROVED_LIFECYCLES
                ), f"{path.name}: unapproved lifecycle command"
                assert step.get("id"), f"{path.name}: run step must expose an id"


def test_processing_workflows_use_locked_uv_setup_action() -> None:
    setup_uv = "astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b"
    for path in PROCESSING_WORKFLOWS:
        workflow = load_workflow(path.name)
        for job in workflow["jobs"].values():
            steps = job.get("steps", [])
            if not any("run" in step for step in steps):
                continue
            uv_steps = [step for step in steps if step.get("uses") == setup_uv]
            assert len(uv_steps) == 1, path.name
            assert uv_steps[0]["with"]["version"] == "0.11.33"


def test_reusable_processor_has_writeback_quality_and_secret_contracts() -> None:
    workflow = load_workflow("ard-process.yml")
    call = workflow["on"]["workflow_call"]

    assert set(call["inputs"]) == {
        "branch",
        "product_key",
        "pr_number",
        "expected_head",
        "allow_writeback",
    }
    assert call["inputs"]["expected_head"]["required"] == "true"
    assert workflow["concurrency"]["group"] == "ard-registry-write"
    assert workflow["jobs"]["process"]["environment"] == "ard-llm"
    job = workflow["jobs"]["process"]
    assert job["permissions"] == {
        "actions": "write",
        "contents": "write",
        "pull-requests": "write",
        "statuses": "write",
    }
    text = (WORKFLOWS / "ard-process.yml").read_text(encoding="utf-8")
    assert "secrets.ARD_LLM_API_KEY" in text
    assert "ard workflow process" in text
    assert "ard workflow process-reconcile" in text
    assert "--expected-head" in text
    assert "retention-days: 30" in text
    assert "lfs: true" in text
    checkout = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "${{ inputs.expected_head }}"
    assert "products/${{ inputs.product_key }}/quality" in text
    finalizer = workflow["jobs"]["finalize"]
    assert finalizer["if"] == "always()"
    assert "environment" not in finalizer
    assert "ard workflow finalize" in text
    assert_actions_are_sha_pinned(WORKFLOWS / "ard-process.yml")


def test_direct_change_never_gives_fork_prs_llm_secrets_or_writeback() -> None:
    workflow = load_workflow("ard-direct-change.yml")
    triggers = workflow["on"]

    assert "pull_request_target" not in triggers
    assert triggers["push"]["paths"] == ["products/*/sources/**"]
    assert triggers["pull_request"]["paths"] == ["products/*/sources/**"]
    assert "secrets.ARD_LLM_API_KEY" not in (WORKFLOWS / "ard-direct-change.yml").read_text()
    process = workflow["jobs"]["process"]
    assert "github.event_name == 'push'" in process["if"]
    assert process["with"]["allow_writeback"] == "true"
    assert process["with"]["expected_head"] == "${{ needs.detect.outputs.expected_head }}"
    assert "secrets" not in process
    source_check = workflow["jobs"]["source-check"]
    assert source_check["permissions"] == {"contents": "read"}
    assert "environment" not in source_check
    assert "persist-credentials: false" in (
        WORKFLOWS / "ard-direct-change.yml"
    ).read_text(encoding="utf-8")
    assert_actions_are_sha_pinned(WORKFLOWS / "ard-direct-change.yml")


def test_issue_intake_calls_processor_without_waiting_for_token_event() -> None:
    workflow = load_workflow("ard-issue-intake.yml")

    assert workflow["on"]["issues"]["types"] == ["labeled"]
    assert workflow["jobs"]["process"]["uses"] == "./.github/workflows/ard-process.yml"
    assert workflow["jobs"]["process"]["needs"] == "intake"
    assert workflow["jobs"]["process"]["with"]["expected_head"] == (
        "${{ needs.intake.outputs.expected_head }}"
    )
    assert "ARD_LLM_API_KEY" not in str(workflow["jobs"]["authorize"])
    assert workflow["jobs"]["finalize"]["if"].startswith("always()")
    assert_actions_are_sha_pinned(WORKFLOWS / "ard-issue-intake.yml")


def test_shared_changeset_serializes_registry_and_reconciles_pr_statuses() -> None:
    workflow = load_workflow("ard-changeset.yml")
    text = (WORKFLOWS / "ard-changeset.yml").read_text(encoding="utf-8")

    assert workflow["on"]["workflow_dispatch"]["inputs"]["mode"]["options"] == [
        "create",
        "ready",
    ]
    assert workflow["concurrency"]["group"] == "ard-registry-write"
    assert text.count("ard workflow changeset") == 3
    assert "--mode create" in text
    assert "--mode ready" in text
    assert "head_sha" in workflow["on"]["workflow_dispatch"]["inputs"]
    assert_actions_are_sha_pinned(WORKFLOWS / "ard-changeset.yml")


def test_release_uses_numeric_id_tags_and_protected_linkage_dispatch() -> None:
    workflow = load_workflow("ard-release.yml")
    text = (WORKFLOWS / "ard-release.yml").read_text(encoding="utf-8")

    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["jobs"]["linkage"]["environment"] == "production-linkage"
    assert "ard workflow release-detect" in text
    assert "ard workflow release-product" in text
    assert "ard workflow release-dispatch" in text
    assert "--table-ids" in text
    assert "production-linkage" in text
    assert "retention-days: 30" in text
    detect_checkout = next(
        step
        for step in workflow["jobs"]["detect"]["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert detect_checkout["with"]["fetch-depth"] == "0"
    assert_actions_are_sha_pinned(WORKFLOWS / "ard-release.yml")


def test_code_only_pull_requests_publish_the_same_required_statuses() -> None:
    workflow = load_workflow("ard-repository-change.yml")
    text = (WORKFLOWS / "ard-repository-change.yml").read_text(encoding="utf-8")

    assert "pull_request" not in workflow["on"]
    assert workflow["on"]["pull_request_target"]["types"] == [
        "opened",
        "synchronize",
        "reopened",
    ]
    assert workflow["on"]["pull_request_target"]["paths-ignore"] == [
        "products/**",
        "registry/**",
    ]
    assert "ard workflow repository-check" in text
    assert "ard workflow finalize" in text
    check = workflow["jobs"]["check"]
    assert check["permissions"] == {"contents": "read"}
    assert "GH_TOKEN" not in str(check)
    checkouts = [
        step
        for step in check["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert [step["with"]["path"] for step in checkouts] == ["trusted", "candidate"]
    assert checkouts[0]["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkouts[1]["with"]["ref"] == (
        "refs/pull/${{ github.event.pull_request.number }}/head"
    )
    repository_step = next(step for step in check["steps"] if step.get("run"))
    assert repository_step["working-directory"] == "trusted"
    assert "--repository \"$CANDIDATE_REPOSITORY\"" in repository_step["run"]
    assert "--verification-group static" in repository_step["run"]
    assert "publish-statuses" not in repository_step["run"]
    assert "repository-name" not in repository_step["run"]

    executable = workflow["jobs"]["executable"]
    assert executable["needs"] == "check"
    assert executable["if"] == "needs.check.outputs.code_only == 'true'"
    assert executable["permissions"] == {"contents": "read"}
    assert "GH_TOKEN" not in str(executable)
    assert executable["strategy"]["matrix"]["verification_group"] == [
        "pytest",
        "wheel",
    ]
    executable_checkouts = [
        step
        for step in executable["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert [step["with"]["path"] for step in executable_checkouts] == [
        "trusted",
        "candidate",
    ]
    assert executable_checkouts[1]["with"]["ref"] == (
        "refs/pull/${{ github.event.pull_request.number }}/head"
    )
    executable_run = next(
        step["run"] for step in executable["steps"] if step.get("run")
    )
    assert "--verification-group \"$VERIFICATION_GROUP\"" in executable_run
    assert "publish-statuses" not in executable_run
    assert "repository-name" not in executable_run

    finalizer = workflow["jobs"]["finalize"]
    assert finalizer["needs"] == ["check", "executable"]
    assert finalizer["if"] == "always()"
    assert finalizer["permissions"] == {"contents": "read", "statuses": "write"}
    assert "GH_TOKEN" in str(finalizer)
    finalizer_runs = [step["run"] for step in finalizer["steps"] if step.get("run")]
    assert len(finalizer_runs) == 2
    assert any("--publish-success-statuses" in run for run in finalizer_runs)
    assert any("--authoritative-statuses" in run for run in finalizer_runs)
    assert_actions_are_sha_pinned(WORKFLOWS / "ard-repository-change.yml")


def test_initial_bootstrap_gate_is_one_time_read_only_and_uses_trusted_cli() -> None:
    workflow = load_workflow("ard-initial-bootstrap.yml")
    text = (WORKFLOWS / "ard-initial-bootstrap.yml").read_text(encoding="utf-8")

    assert set(workflow["on"]) == {"pull_request"}
    assert workflow["on"]["pull_request"]["types"] == [
        "opened",
        "synchronize",
        "reopened",
    ]
    assert workflow["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in text
    assert "secrets." not in text
    assert "GH_TOKEN" not in text
    assert "statuses: write" not in text

    verification = workflow["jobs"]["verification"]
    guard = verification["if"]
    assert "github.event.pull_request.number == 1" in guard
    assert (
        "github.event.pull_request.base.sha == "
        "'c23333610cb1d27ff136910de010011b6c870f3a'"
    ) in guard
    assert "github.event.pull_request.head.repo.full_name == github.repository" in guard
    assert (
        "github.event.pull_request.head.ref == "
        "'agent/design-numeric-versions-actions'"
    ) in guard
    assert verification["permissions"] == {"contents": "read"}
    assert verification["strategy"]["fail-fast"] == "false"
    assert verification["strategy"]["matrix"]["verification_group"] == [
        "static",
        "pytest",
        "wheel",
    ]

    checkouts = [
        step
        for step in verification["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert [step["with"]["path"] for step in checkouts] == ["trusted", "candidate"]
    assert checkouts[0]["with"]["ref"] == (
        "cb79416c4585d383181e75e7f87579bbf368ca65"
    )
    assert checkouts[1]["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
    assert all(step["with"]["persist-credentials"] == "false" for step in checkouts)

    run_step = next(step for step in verification["steps"] if step.get("run"))
    assert run_step["working-directory"] == "trusted"
    assert "--repository \"$CANDIDATE_REPOSITORY\"" in run_step["run"]
    assert "--verification-group \"$VERIFICATION_GROUP\"" in run_step["run"]
    assert "publish-statuses" not in run_step["run"]
    assert "repository-name" not in run_step["run"]

    aggregate = workflow["jobs"]["aggregate"]
    assert aggregate["name"] == "ARD initial bootstrap aggregate"
    assert aggregate["needs"] == "verification"
    assert "always()" in aggregate["if"]
    assert "github.event.pull_request.number == 1" in aggregate["if"]
    assert aggregate["permissions"] == {"contents": "read"}
    assert aggregate["steps"][0]["uses"] == (
        "actions/github-script@ed597411d8f924073f98dfc5c65a23a2325f34cd"
    )
    assert aggregate["steps"][0]["env"] == {
        "VERIFICATION_RESULT": "${{ needs.verification.result }}"
    }
    assert "core.setFailed" in aggregate["steps"][0]["with"]["script"]
    assert aggregate["name"] not in {"ard/quality-gate", "ard/changeset"}
    assert_actions_are_sha_pinned(WORKFLOWS / "ard-initial-bootstrap.yml")
