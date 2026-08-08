from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOWS = Path(".github/workflows")


def load_workflow(name: str) -> dict:
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def assert_actions_are_sha_pinned(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.search(r"\buses:\s*([^\s#]+)", line)
        if match is None or match.group(1).startswith("./"):
            continue
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", match.group(1)), line


def test_reusable_processor_has_writeback_quality_and_secret_contracts() -> None:
    workflow = load_workflow("ard-process.yml")
    call = workflow["on"]["workflow_call"]

    assert set(call["inputs"]) == {"branch", "product_key", "pr_number", "allow_writeback"}
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
    assert "ard/quality-gate" in text
    assert "retention-days: 30" in text
    assert "MULTIPLE_PRODUCTS_NOT_ALLOWED" in text
    assert "UNTRUSTED_BRANCH_PATH" in text
    assert "lfs: true" in text
    assert "products/${PRODUCT_KEY}/generated" in text
    assert "products/${PRODUCT_KEY}/quality" in text
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
    assert "secrets" not in process
    assert_actions_are_sha_pinned(WORKFLOWS / "ard-direct-change.yml")


def test_issue_intake_calls_processor_without_waiting_for_token_event() -> None:
    workflow = load_workflow("ard-issue-intake.yml")

    assert workflow["on"]["issues"]["types"] == ["labeled"]
    assert workflow["jobs"]["process"]["uses"] == "./.github/workflows/ard-process.yml"
    assert workflow["jobs"]["process"]["needs"] == "intake"
    assert "ARD_LLM_API_KEY" not in str(workflow["jobs"]["authorize"])
    assert_actions_are_sha_pinned(WORKFLOWS / "ard-issue-intake.yml")


def test_shared_changeset_serializes_registry_and_reconciles_pr_statuses() -> None:
    workflow = load_workflow("ard-changeset.yml")
    text = (WORKFLOWS / "ard-changeset.yml").read_text(encoding="utf-8")

    assert workflow["on"]["workflow_dispatch"]["inputs"]["mode"]["options"] == [
        "create",
        "ready",
    ]
    assert workflow["concurrency"]["group"] == "ard-registry-write"
    assert "ard/changeset" in text
    assert "ready_products" in text
    assert "Create one Draft tracking PR per required product" in text
    assert "head_sha" in workflow["on"]["workflow_dispatch"]["inputs"]
    assert "Publish coordination PR checks" in text
    assert_actions_are_sha_pinned(WORKFLOWS / "ard-changeset.yml")


def test_release_uses_numeric_id_tags_and_protected_linkage_dispatch() -> None:
    workflow = load_workflow("ard-release.yml")
    text = (WORKFLOWS / "ard-release.yml").read_text(encoding="utf-8")

    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["jobs"]["linkage"]["environment"] == "production-linkage"
    assert "product/${product_id}/v${version}" in text
    assert "table_tags" in text
    assert "ard_product_released" in text
    assert "TAG_TARGET_CONFLICT" in text
    assert "CHANGESET_PR_NOT_MERGED" in text
    assert "CHANGESET_HEAD_SHA_MISMATCH" in text
    assert "CHANGESET_MERGE_NOT_REACHABLE" in text
    assert_actions_are_sha_pinned(WORKFLOWS / "ard-release.yml")
