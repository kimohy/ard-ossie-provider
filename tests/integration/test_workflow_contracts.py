from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOWS = Path(".github/workflows")
PROCESSING_WORKFLOWS = tuple(
    WORKFLOWS / name
    for name in (
        "ard-issue-intake.yml",
        "ard-direct-signal.yml",
        "ard-direct-change.yml",
        "ard-process.yml",
        "ard-changeset.yml",
        "ard-release.yml",
        "ard-repository-change.yml",
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
    "workflow issue-route",
    "workflow issue-intake",
    "workflow issue-base-sync",
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
                assert any(f"ard {lifecycle}" in command for lifecycle in APPROVED_LIFECYCLES), (
                    f"{path.name}: unapproved lifecycle command"
                )
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
    validation = workflow["jobs"]["validate"]
    assert validation["permissions"] == {"contents": "read"}
    assert validation["environment"] == "ard-llm"
    assert "GH_TOKEN" not in str(validation)
    assert not any(name.startswith("ARD_") for name in validation["env"])
    validation_checkouts = [
        step for step in validation["steps"] if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert [step["with"]["path"] for step in validation_checkouts] == [
        "trusted",
        "candidate",
    ]
    assert validation_checkouts[0]["with"]["ref"] == (
        "${{ github.event.repository.default_branch }}"
    )
    assert validation_checkouts[1]["with"]["ref"] == "${{ inputs.expected_head }}"
    assert validation_checkouts[1]["with"]["persist-credentials"] == "false"
    validation_run = next(step for step in validation["steps"] if step.get("run"))
    assert validation_run["working-directory"] == "trusted"
    assert '--repository "$CANDIDATE_REPOSITORY"' in validation_run["run"]
    assert "--require-llm" in validation_run["run"]
    assert '--diagnostics-dir "$CANDIDATE_REPOSITORY/.ard/run/semantic-validate"' in validation_run[
        "run"
    ]
    validation_upload = next(
        step
        for step in validation["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert validation_upload["if"] == "always()"
    assert validation_upload["with"]["path"] == "candidate/.ard/run/semantic-validate"
    assert validation_upload["with"]["if-no-files-found"] == "warn"
    assert validation_run["env"]["ARD_LLM_PROFILE"] == "${{ vars.ARD_LLM_PROFILE }}"
    assert validation_run["env"]["ARD_LLM_API_KEY"] == "${{ secrets.ARD_LLM_API_KEY }}"
    assert validation_run["env"]["ARD_LLM_BASE_URL"] == (
        "${{ secrets.ARD_LLM_BASE_URL || vars.ARD_LLM_BASE_URL || 'https://api.openai.com/v1' }}"
    )
    assert validation_run["env"]["ARD_AZURE_OPENAI_ENDPOINT"] == (
        "${{ vars.ARD_AZURE_OPENAI_ENDPOINT }}"
    )
    assert validation_run["env"]["ARD_AZURE_OPENAI_API_KEY"] == (
        "${{ secrets.ARD_AZURE_OPENAI_API_KEY }}"
    )
    assert validation_run["env"]["ARD_GCP_PROJECT_ID"] == "${{ vars.ARD_GCP_PROJECT_ID }}"
    assert validation_run["env"]["ARD_VERTEX_CREDENTIALS_JSON"] == (
        "${{ secrets.ARD_VERTEX_CREDENTIALS_JSON }}"
    )
    assert all(
        "secrets." not in str(step)
        for step in validation["steps"]
        if step is not validation_run
    )

    job = workflow["jobs"]["process"]
    assert job["needs"] == "validate"
    assert job["environment"] == "ard-llm"
    assert job["permissions"] == {
        "actions": "write",
        "contents": "write",
        "pull-requests": "write",
        "statuses": "write",
    }
    text = (WORKFLOWS / "ard-process.yml").read_text(encoding="utf-8")
    assert "secrets.ARD_LLM_API_KEY" in text
    assert job["env"]["ARD_LLM_PROFILE"] == "${{ vars.ARD_LLM_PROFILE }}"
    assert job["env"]["ARD_LLM_BASE_URL"] == (
        "${{ secrets.ARD_LLM_BASE_URL || vars.ARD_LLM_BASE_URL || 'https://api.openai.com/v1' }}"
    )
    assert job["env"]["ARD_AZURE_OPENAI_ENDPOINT"] == ("${{ vars.ARD_AZURE_OPENAI_ENDPOINT }}")
    assert job["env"]["ARD_AZURE_OPENAI_API_KEY"] == ("${{ secrets.ARD_AZURE_OPENAI_API_KEY }}")
    assert job["env"]["ARD_GCP_PROJECT_ID"] == "${{ vars.ARD_GCP_PROJECT_ID }}"
    assert job["env"]["ARD_VERTEX_CREDENTIALS_JSON"] == (
        "${{ secrets.ARD_VERTEX_CREDENTIALS_JSON }}"
    )
    assert "ARD_LLM_MODEL" not in job["env"]
    assert "ARD_LLM_API_STYLE" not in job["env"]
    assert "ard workflow process" in text
    assert "ard workflow process-reconcile" in text
    assert "--expected-head" in text
    assert job["env"]["PROCESS_INVOCATION_ID"] == ("${{ github.run_id }}-${{ github.run_attempt }}")
    assert "retention-days: 30" in text
    assert "lfs: true" in text
    checkouts = [
        step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert [step["with"]["path"] for step in checkouts] == ["trusted", "candidate"]
    assert checkouts[0]["with"]["ref"] == ("${{ github.event.repository.default_branch }}")
    assert checkouts[0]["with"]["persist-credentials"] == "false"
    assert checkouts[1]["with"]["ref"] == "${{ inputs.expected_head }}"
    process_runs = [step for step in job["steps"] if step.get("run")]
    assert all(step["working-directory"] == "trusted" for step in process_runs)
    assert all('--repository "$CANDIDATE_REPOSITORY"' in step["run"] for step in process_runs)
    assert all('--invocation-id "$PROCESS_INVOCATION_ID"' in step["run"] for step in process_runs)
    assert all(step["env"].get("PYTHONSAFEPATH") == "1" for step in process_runs)
    assert "candidate/products/${{ inputs.product_key }}/quality" in text
    finalizer = workflow["jobs"]["finalize"]
    assert finalizer["if"] == "always()"
    assert "environment" not in finalizer
    assert "ard workflow finalize" in text
    assert_actions_are_sha_pinned(WORKFLOWS / "ard-process.yml")


def test_llm_smoke_workflow_is_manual_trusted_read_only_and_protected() -> None:
    workflow = load_workflow("ard-llm-smoke.yml")

    assert set(workflow["on"]) == {"workflow_dispatch"}
    profile = workflow["on"]["workflow_dispatch"]["inputs"]["profile"]
    assert profile["required"] == "true"
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["smoke"]
    assert job["environment"] == "ard-llm"
    assert job["permissions"] == {"contents": "read"}
    checkout = next(
        step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "main"
    assert checkout["with"]["persist-credentials"] == "false"
    run = next(step for step in job["steps"] if step.get("run"))
    assert run["run"].startswith("uv run --frozen ard llm smoke-test")
    assert "${{" not in run["run"]
    assert run["env"]["ARD_LLM_PROFILE"] == "${{ inputs.profile }}"
    assert "upload-artifact" not in str(workflow)
    assert_actions_are_sha_pinned(WORKFLOWS / "ard-llm-smoke.yml")


def test_direct_change_uses_read_only_signal_and_default_branch_coordinator() -> None:
    signal = load_workflow("ard-direct-signal.yml")
    assert set(signal["on"]) == {"push"}
    assert signal["on"]["push"]["branches-ignore"] == ["main"]
    assert signal["on"]["push"]["paths"] == ["products/*/sources/**"]
    assert signal["permissions"] == {"contents": "read"}
    assert all("run" not in step for step in signal["jobs"]["signal"]["steps"])
    assert "environment" not in signal["jobs"]["signal"]
    assert_actions_are_sha_pinned(WORKFLOWS / "ard-direct-signal.yml")

    workflow = load_workflow("ard-direct-change.yml")
    assert set(workflow["on"]) == {"workflow_run"}
    trigger = workflow["on"]["workflow_run"]
    assert trigger["workflows"] == ["ARD direct branch signal"]
    assert trigger["types"] == ["completed"]

    validation = workflow["jobs"]["validate"]
    guard = validation["if"]
    assert "github.event.workflow_run.conclusion == 'success'" in guard
    assert "github.event.workflow_run.event == 'push'" in guard
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in guard
    assert (
        "github.event.workflow_run.head_branch != github.event.repository.default_branch" in guard
    )
    assert validation["permissions"] == {"contents": "read"}
    assert validation["environment"] == "ard-llm"
    assert "GH_TOKEN" not in str(validation)
    assert not any(name.startswith("ARD_") for name in validation["env"])
    checkouts = [
        step for step in validation["steps"] if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert [step["with"]["path"] for step in checkouts] == ["trusted", "candidate"]
    assert checkouts[0]["with"]["ref"] == ("${{ github.event.repository.default_branch }}")
    assert checkouts[1]["with"]["ref"] == "${{ github.event.workflow_run.head_sha }}"
    assert checkouts[1]["with"]["persist-credentials"] == "false"
    validation_runs = [step for step in validation["steps"] if step.get("run")]
    assert all(step["working-directory"] == "trusted" for step in validation_runs)
    assert all('--repository "$CANDIDATE_REPOSITORY"' in step["run"] for step in validation_runs)
    source_check = next(step for step in validation_runs if step["id"] == "source_check")
    assert "--require-llm" in source_check["run"]
    assert source_check["env"]["ARD_LLM_PROFILE"] == "${{ vars.ARD_LLM_PROFILE }}"
    assert source_check["env"]["ARD_LLM_API_KEY"] == "${{ secrets.ARD_LLM_API_KEY }}"
    assert source_check["env"]["ARD_LLM_BASE_URL"] == (
        "${{ secrets.ARD_LLM_BASE_URL || vars.ARD_LLM_BASE_URL || 'https://api.openai.com/v1' }}"
    )
    assert source_check["env"]["ARD_AZURE_OPENAI_ENDPOINT"] == (
        "${{ vars.ARD_AZURE_OPENAI_ENDPOINT }}"
    )
    assert source_check["env"]["ARD_AZURE_OPENAI_API_KEY"] == (
        "${{ secrets.ARD_AZURE_OPENAI_API_KEY }}"
    )
    assert source_check["env"]["ARD_GCP_PROJECT_ID"] == "${{ vars.ARD_GCP_PROJECT_ID }}"
    assert source_check["env"]["ARD_VERTEX_CREDENTIALS_JSON"] == (
        "${{ secrets.ARD_VERTEX_CREDENTIALS_JSON }}"
    )
    assert all(
        "secrets." not in str(step)
        for step in validation["steps"]
        if step is not source_check
    )

    pull_request = workflow["jobs"]["pull_request"]
    assert pull_request["needs"] == "validate"
    assert pull_request["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
    }
    pr_run = next(step for step in pull_request["steps"] if step.get("run"))
    assert pr_run["working-directory"] == "trusted"
    assert '--repository "$CANDIDATE_REPOSITORY"' in pr_run["run"]

    process = workflow["jobs"]["process"]
    assert process["needs"] == ["validate", "pull_request"]
    assert process["uses"] == "./.github/workflows/ard-process.yml"
    assert process["with"]["allow_writeback"] == "true"
    assert process["with"]["expected_head"] == "${{ needs.validate.outputs.expected_head }}"
    assert process["secrets"] == "inherit"
    assert_actions_are_sha_pinned(WORKFLOWS / "ard-direct-change.yml")


def test_issue_intake_routes_existing_drafts_through_trusted_base_sync() -> None:
    workflow = load_workflow("ard-issue-intake.yml")

    assert workflow["on"]["issues"]["types"] == ["labeled"]
    route = workflow["jobs"]["route"]
    assert route["needs"] == "authorize"
    assert route["permissions"] == {
        "contents": "read",
        "issues": "read",
        "pull-requests": "read",
    }
    assert set(route["outputs"]) == {
        "mode",
        "base_sha",
        "branch",
        "product_key",
        "pr_number",
        "expected_head",
    }
    route_checkout = next(
        step for step in route["steps"] if step.get("uses", "").startswith("actions/checkout@")
    )
    assert route_checkout["with"] == {
        "ref": "${{ github.sha }}",
        "path": "trusted",
        "persist-credentials": "true",
    }
    route_run = next(step for step in route["steps"] if step.get("run"))
    assert route_run["working-directory"] == "trusted"
    assert route_run["env"]["PYTHONSAFEPATH"] == "1"
    assert '--repository "$TRUSTED_REPOSITORY"' in route_run["run"]

    intake = workflow["jobs"]["intake"]
    assert intake["needs"] == "route"
    assert intake["if"] == "needs.route.outputs.mode == 'intake'"

    base_sync = workflow["jobs"]["base_sync"]
    assert base_sync["needs"] == "route"
    assert base_sync["if"] == "needs.route.outputs.mode == 'base_sync'"
    assert base_sync["permissions"] == {
        "contents": "write",
        "issues": "read",
        "pull-requests": "read",
    }
    checkouts = [
        step for step in base_sync["steps"] if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert [step["with"]["path"] for step in checkouts] == ["trusted", "candidate"]
    assert checkouts[0]["with"] == {
        "ref": "${{ needs.route.outputs.base_sha }}",
        "path": "trusted",
        "persist-credentials": "false",
    }
    assert checkouts[1]["with"] == {
        "ref": "${{ needs.route.outputs.expected_head }}",
        "fetch-depth": "0",
        "lfs": "true",
        "path": "candidate",
        "persist-credentials": "true",
    }
    base_sync_run = next(step for step in base_sync["steps"] if step.get("run"))
    assert base_sync_run["working-directory"] == "trusted"
    assert base_sync_run["env"]["PYTHONSAFEPATH"] == "1"
    assert '--repository "$CANDIDATE_REPOSITORY"' in base_sync_run["run"]
    assert '--base-sha "$BASE_SHA"' in base_sync_run["run"]

    process = workflow["jobs"]["process"]
    assert process["uses"] == "./.github/workflows/ard-process.yml"
    assert process["needs"] == ["route", "intake", "base_sync"]
    assert process["if"] == (
        "always() && needs.route.result == 'success' && "
        "(needs.intake.result == 'success' || needs.base_sync.result == 'success')"
    )
    for name in ("branch", "product_key", "expected_head"):
        assert process["with"][name] == (
            f"${{{{ needs.intake.outputs.{name} || needs.base_sync.outputs.{name} }}}}"
        )
    assert process["with"]["pr_number"] == (
        "${{ fromJSON(needs.intake.outputs.pr_number || needs.base_sync.outputs.pr_number) }}"
    )
    assert process["secrets"] == "inherit"

    finalizer = workflow["jobs"]["finalize"]
    assert finalizer["needs"] == [
        "authorize",
        "route",
        "intake",
        "base_sync",
        "process",
    ]
    assert finalizer["if"].startswith("always()")
    assert "ARD_LLM_API_KEY" not in str(route)
    assert "ARD_LLM_API_KEY" not in str(base_sync)
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
        step for step in check["steps"] if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert [step["with"]["path"] for step in checkouts] == ["trusted", "candidate"]
    assert checkouts[0]["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkouts[1]["with"]["ref"] == ("refs/pull/${{ github.event.pull_request.number }}/head")
    repository_step = next(step for step in check["steps"] if step.get("run"))
    assert repository_step["working-directory"] == "trusted"
    assert '--repository "$CANDIDATE_REPOSITORY"' in repository_step["run"]
    assert "--verification-group static" in repository_step["run"]
    assert "publish-statuses" not in repository_step["run"]
    assert "repository-name" not in repository_step["run"]

    executable = workflow["jobs"]["executable"]
    assert executable["needs"] == "check"
    assert executable["if"] == "needs.check.outputs.code_only == 'true'"
    assert executable["permissions"] == {"contents": "read"}
    assert "GH_TOKEN" not in str(executable)
    assert executable["strategy"]["matrix"]["verification_group"] == [
        "model-schemas",
        "pytest",
        "wheel",
    ]
    executable_checkouts = [
        step for step in executable["steps"] if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert [step["with"]["path"] for step in executable_checkouts] == [
        "trusted",
        "candidate",
    ]
    assert executable_checkouts[1]["with"]["ref"] == (
        "refs/pull/${{ github.event.pull_request.number }}/head"
    )
    executable_run = next(step["run"] for step in executable["steps"] if step.get("run"))
    assert '--verification-group "$VERIFICATION_GROUP"' in executable_run
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
