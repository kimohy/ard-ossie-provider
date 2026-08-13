from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Iterable
from dataclasses import asdict, replace
from pathlib import Path

import pytest

import ard_ossie.adapters.github_cli as github_cli_module
from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.adapters.github_cli import GitHubCli
from ard_ossie.application.contracts import WorkflowSecurityError
from ard_ossie.ports.github import (
    ActionsPermissionState,
    BranchProtectionState,
    EnvironmentReviewer,
    EnvironmentState,
    GitHubConflict,
    GitHubTransientError,
    ReleaseAssetPayload,
)
from ard_ossie.ports.process import CommandRequest, CommandResult

REPOSITORY = "kimohy/ard-ossie-provider"
SHA = "a" * 40


def ok(payload: object = None) -> CommandResult:
    stdout = "" if payload is None else json.dumps(payload)
    return CommandResult(returncode=0, stdout=stdout, stderr="")


def not_found() -> CommandResult:
    return CommandResult(returncode=1, stdout="", stderr="HTTP 404: Not Found")


def fail_release_stage_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    real_temporary_directory = github_cli_module.tempfile.TemporaryDirectory

    class FailingCleanupDirectory:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._temporary = real_temporary_directory(*args, **kwargs)
            self.name = self._temporary.name

        def __enter__(self) -> str:
            return self.name

        def __exit__(self, *args: object) -> None:
            self.cleanup()

        def cleanup(self) -> None:
            self._temporary.cleanup()
            raise OSError("cannot clean private upload directory")

    monkeypatch.setattr(
        github_cli_module.tempfile,
        "TemporaryDirectory",
        FailingCleanupDirectory,
    )


class RecordingRunner:
    def __init__(self, results: Iterable[CommandResult]) -> None:
        self.results = iter(results)
        self.requests: list[CommandRequest] = []

    @property
    def argv(self) -> list[tuple[str, ...]]:
        return [request.argv for request in self.requests]

    def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        return next(self.results)


def pull_request_payload(number: int = 3) -> dict[str, object]:
    return {
        "number": number,
        "html_url": f"https://github.com/{REPOSITORY}/pull/{number}",
        "head": {"ref": "ard/example", "sha": SHA},
        "base": {"ref": "main"},
        "draft": True,
        "merged_at": None,
        "merge_commit_sha": "b" * 40,
    }


def test_repository_and_collaborator_responses_become_typed_snapshots() -> None:
    """Lifecycle policy must not depend on unstable raw GitHub dictionaries."""
    runner = RecordingRunner(
        [
            ok(
                {
                    "full_name": REPOSITORY,
                    "private": False,
                    "archived": False,
                    "default_branch": "main",
                    "permissions": {"admin": True, "maintain": True, "push": True},
                }
            ),
            ok({"permission": "maintain", "user": {"login": "reviewer"}}),
            ok([{"login": "reviewer", "role_name": "write"}]),
        ]
    )
    client = GitHubCli(REPOSITORY, runner)

    repository = client.repository()
    permission = client.collaborator_permission("reviewer")
    collaborators = client.list_collaborators()

    assert repository.full_name == REPOSITORY
    assert repository.public is True
    assert repository.permission == "admin"
    assert permission == "maintain"
    assert collaborators[0].login == "reviewer"
    assert collaborators[0].permission == "write"


def test_create_draft_pr_uses_json_stdin_and_returns_typed_state() -> None:
    """PR title and body must stay out of shell parsing and preserve the exact branch."""
    runner = RecordingRunner([ok(pull_request_payload())])

    result = GitHubCli(REPOSITORY, runner).create_draft_pr(
        "ard/example",
        "main",
        "data(example): update",
        "body with `content`",
    )

    assert result.number == 3
    assert result.head_branch == "ard/example"
    request = runner.requests[0]
    assert request.argv == (
        "gh",
        "api",
        "--method",
        "POST",
        f"repos/{REPOSITORY}/pulls",
        "--input",
        "-",
    )
    assert json.loads(request.stdin or "") == {
        "base": "main",
        "body": "body with `content`",
        "draft": True,
        "head": "ard/example",
        "title": "data(example): update",
    }


def test_pr_lookup_rejects_malformed_merge_sha() -> None:
    """Release ancestry must never consume an unvalidated merge commit identifier."""
    payload = pull_request_payload()
    payload["merged_at"] = "2026-08-10T00:00:00Z"
    payload["merge_commit_sha"] = "not-a-sha"
    runner = RecordingRunner([ok(payload)])

    with pytest.raises(GitHubConflict, match="INVALID_GITHUB_SHA"):
        GitHubCli(REPOSITORY, runner).get_pr(3)


def test_find_open_pr_status_and_dispatches_use_exact_resources() -> None:
    """Retry decisions must query the exact head branch, status context, and dispatch target."""
    runner = RecordingRunner(
        [
            ok([pull_request_payload()]),
            ok({"statuses": [{"context": "ard/quality-gate", "state": "success"}]}),
            ok(),
            ok(),
        ]
    )
    client = GitHubCli(REPOSITORY, runner)

    assert client.find_open_pr("ard/example").number == 3
    assert client.get_status(SHA, "ard/quality-gate") == "success"
    workflow = client.dispatch_workflow("ard-process.yml", "main", {"product_key": "sales"})
    dispatch = client.repository_dispatch("ard_product_released", {"version": 1})

    assert workflow.action == "dispatch"
    assert dispatch.action == "dispatch"
    assert json.loads(runner.requests[2].stdin or "") == {
        "inputs": {"product_key": "sales"},
        "ref": "main",
    }
    assert json.loads(runner.requests[3].stdin or "") == {
        "client_payload": {"version": 1},
        "event_type": "ard_product_released",
    }


def test_set_status_targets_exact_repository_and_sha() -> None:
    """A status written to a moving branch instead of the exact SHA would be unsafe."""
    runner = RecordingRunner([ok({"id": 9})])
    client = GitHubCli(REPOSITORY, runner, token_env="GH_TOKEN")

    mutation = client.set_status(
        SHA,
        "ard/quality-gate",
        "success",
        "passed",
        "https://example.invalid/run",
    )

    assert mutation.target == f"{SHA}:ard/quality-gate"
    assert runner.requests[0].argv[4] == f"repos/{REPOSITORY}/statuses/{SHA}"
    assert json.loads(runner.requests[0].stdin or "")["context"] == "ard/quality-gate"


def test_upsert_comment_updates_existing_marker() -> None:
    """A retry must update one managed comment instead of adding duplicates."""
    runner = RecordingRunner(
        [
            ok([{"id": 7, "body": "<!-- ard:process --> old"}]),
            ok({"id": 7}),
        ]
    )

    mutation = GitHubCli(REPOSITORY, runner).upsert_pr_comment(3, "ard:process", "new")

    assert mutation.action == "update"
    assert "issues/comments/7" in " ".join(runner.argv[1])
    assert json.loads(runner.requests[1].stdin or "")["body"] == "<!-- ard:process -->\nnew"


def test_set_issue_labels_reconciles_only_the_requested_delta() -> None:
    """A finalizer must preserve unrelated labels while applying its owned delta."""
    runner = RecordingRunner(
        [
            ok({"labels": [{"name": "ard:processing"}, {"name": "customer-owned"}]}),
            ok([{"name": "ard:failed"}, {"name": "customer-owned"}]),
        ]
    )

    mutations = GitHubCli(REPOSITORY, runner).set_issue_labels(
        5,
        add={"ard:failed"},
        remove={"ard:processing"},
    )

    assert [mutation.action for mutation in mutations] == ["remove", "add"]
    assert json.loads(runner.requests[1].stdin or "") == {
        "labels": ["ard:failed", "customer-owned"]
    }


def test_matching_release_asset_is_a_noop(tmp_path: Path) -> None:
    """A release retry must not upload a second asset when GitHub reports the same digest."""
    asset = tmp_path / "bundle.zip"
    asset.write_bytes(b"bundle")
    digest = hashlib.sha256(b"bundle").hexdigest()
    runner = RecordingRunner(
        [
            ok(
                {
                    "id": 11,
                    "tag_name": "product/prd_example/v1",
                    "name": "Product v1",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": "bundle.zip",
                            "digest": f"sha256:{digest}",
                            "browser_download_url": "https://example.invalid/bundle.zip",
                        }
                    ],
                }
            )
        ]
    )

    mutation = GitHubCli(REPOSITORY, runner, paths=RepositoryPaths(tmp_path)).upsert_release(
        "product/prd_example/v1",
        "Product v1",
        asset,
        digest,
    )

    assert mutation.action == "noop"
    assert len(runner.requests) == 1


def test_missing_release_is_created_before_asset_upload(tmp_path: Path) -> None:
    """Publication must create release metadata before uploading its immutable bundle."""
    asset = tmp_path / "bundle.zip"
    asset.write_bytes(b"bundle")
    digest = hashlib.sha256(b"bundle").hexdigest()
    runner = RecordingRunner([not_found(), ok({"id": 12}), ok()])

    mutation = GitHubCli(REPOSITORY, runner, paths=RepositoryPaths(tmp_path)).upsert_release(
        "product/prd_example/v1",
        "Product v1",
        asset,
        digest,
    )

    assert mutation.action == "upload"
    assert json.loads(runner.requests[1].stdin or "")["tag_name"] == "product/prd_example/v1"
    assert runner.requests[2].argv[:4] == (
        "gh",
        "release",
        "upload",
        "product/prd_example/v1",
    )


def test_release_upload_consumes_private_immutable_payload(tmp_path: Path) -> None:
    asset = tmp_path / "bundle.zip"
    payload = b"verified release bundle"
    asset.write_bytes(payload)

    class MutatingUploadRunner(RecordingRunner):
        uploaded = b""
        upload_directory_mode = 0
        upload_file_mode = 0

        def run(self, request: CommandRequest) -> CommandResult:
            if request.argv[:3] == ("gh", "release", "upload"):
                asset.write_bytes(b"replacement after adapter snapshot")
                upload = Path(request.argv[4])
                self.uploaded = upload.read_bytes()
                self.upload_directory_mode = stat.S_IMODE(upload.parent.stat().st_mode)
                self.upload_file_mode = stat.S_IMODE(upload.stat().st_mode)
            return super().run(request)

    runner = MutatingUploadRunner([not_found(), ok({"id": 12}), ok()])

    GitHubCli(REPOSITORY, runner, paths=RepositoryPaths(tmp_path)).upsert_release(
        "product/prd_example/v1",
        "Product v1",
        asset,
        hashlib.sha256(payload).hexdigest(),
    )

    assert runner.uploaded == payload
    assert runner.upload_directory_mode == 0o700
    assert runner.upload_file_mode == 0o600


def test_release_upload_staging_open_failure_precedes_remote_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"verified release bundle"
    runner = RecordingRunner([not_found(), ok({"id": 12})])

    def fail_open(*args: object, **kwargs: object) -> int:
        raise OSError("cannot create private upload file")

    monkeypatch.setattr(github_cli_module.os, "open", fail_open)

    with pytest.raises(GitHubTransientError) as exc_info:
        GitHubCli(REPOSITORY, runner).upsert_release(
            "product/prd_example/v1",
            "Product v1",
            ReleaseAssetPayload(name="bundle.zip", payload=payload),
            hashlib.sha256(payload).hexdigest(),
        )

    assert exc_info.value.code == "RELEASE_ASSET_STAGING_FAILED"
    assert runner.requests == []


def test_release_upload_staging_fsync_failure_precedes_remote_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"verified release bundle"
    runner = RecordingRunner([not_found(), ok({"id": 12})])

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("cannot sync private upload file")

    monkeypatch.setattr(github_cli_module.os, "fsync", fail_fsync)

    with pytest.raises(GitHubTransientError) as exc_info:
        GitHubCli(REPOSITORY, runner).upsert_release(
            "product/prd_example/v1",
            "Product v1",
            ReleaseAssetPayload(name="bundle.zip", payload=payload),
            hashlib.sha256(payload).hexdigest(),
        )

    assert exc_info.value.code == "RELEASE_ASSET_STAGING_FAILED"
    assert runner.requests == []


def test_release_upload_staging_write_failure_precedes_remote_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"verified release bundle"
    runner = RecordingRunner([not_found(), ok({"id": 12})])
    real_fdopen = github_cli_module.os.fdopen

    class FailingWriteStream:
        def __init__(self, descriptor: int) -> None:
            self._stream = real_fdopen(descriptor, "wb")

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            self._stream.close()

        def write(self, _payload: bytes) -> int:
            raise OSError("cannot write private upload file")

    def failing_fdopen(descriptor: int, _mode: str) -> FailingWriteStream:
        return FailingWriteStream(descriptor)

    monkeypatch.setattr(github_cli_module.os, "fdopen", failing_fdopen)

    with pytest.raises(GitHubTransientError) as exc_info:
        GitHubCli(REPOSITORY, runner).upsert_release(
            "product/prd_example/v1",
            "Product v1",
            ReleaseAssetPayload(name="bundle.zip", payload=payload),
            hashlib.sha256(payload).hexdigest(),
        )

    assert exc_info.value.code == "RELEASE_ASSET_STAGING_FAILED"
    assert runner.requests == []


def test_release_upload_cleanup_failure_is_a_deterministic_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"verified release bundle"
    runner = RecordingRunner([not_found(), ok({"id": 12}), ok()])
    fail_release_stage_cleanup(monkeypatch)

    with pytest.raises(GitHubTransientError) as exc_info:
        GitHubCli(REPOSITORY, runner).upsert_release(
            "product/prd_example/v1",
            "Product v1",
            ReleaseAssetPayload(name="bundle.zip", payload=payload),
            hashlib.sha256(payload).hexdigest(),
        )

    assert exc_info.value.code == "RELEASE_ASSET_CLEANUP_FAILED"
    assert runner.requests[-1].argv[:3] == ("gh", "release", "upload")


def test_release_upload_failure_remains_primary_when_cleanup_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"verified release bundle"
    runner = RecordingRunner(
        [
            not_found(),
            ok({"id": 12}),
            CommandResult(returncode=1, stdout="", stderr="upload unavailable"),
        ]
    )
    fail_release_stage_cleanup(monkeypatch)

    with pytest.raises(GitHubTransientError) as exc_info:
        GitHubCli(REPOSITORY, runner).upsert_release(
            "product/prd_example/v1",
            "Product v1",
            ReleaseAssetPayload(name="bundle.zip", payload=payload),
            hashlib.sha256(payload).hexdigest(),
        )

    assert exc_info.value.code == "RELEASE_UPLOAD_FAILED"
    assert any(
        "RELEASE_ASSET_CLEANUP_FAILED" in note
        for note in getattr(exc_info.value, "__notes__", ())
    )


def test_release_conflict_remains_primary_when_cleanup_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"verified release bundle"
    digest = hashlib.sha256(payload).hexdigest()
    runner = RecordingRunner(
        [
            ok(
                {
                    "id": 11,
                    "tag_name": "product/prd_example/v1",
                    "name": "Product v1",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": "bundle.zip",
                            "digest": f"sha256:{digest}",
                            "browser_download_url": "https://example.invalid/one.zip",
                        },
                        {
                            "name": "bundle.zip",
                            "digest": f"sha256:{digest}",
                            "browser_download_url": "https://example.invalid/two.zip",
                        },
                    ],
                }
            )
        ]
    )
    fail_release_stage_cleanup(monkeypatch)

    with pytest.raises(GitHubConflict) as exc_info:
        GitHubCli(REPOSITORY, runner).upsert_release(
            "product/prd_example/v1",
            "Product v1",
            ReleaseAssetPayload(name="bundle.zip", payload=payload),
            digest,
        )

    assert exc_info.value.code == "MULTIPLE_RELEASE_ASSETS"
    assert any(
        "RELEASE_ASSET_CLEANUP_FAILED" in note
        for note in getattr(exc_info.value, "__notes__", ())
    )


def test_matching_release_asset_reconciles_all_release_metadata(tmp_path: Path) -> None:
    """An asset match is a no-op only when title, draft, and prerelease also converge."""
    asset = tmp_path / "bundle.zip"
    asset.write_bytes(b"bundle")
    digest = hashlib.sha256(b"bundle").hexdigest()
    runner = RecordingRunner(
        [
            ok(
                {
                    "id": 11,
                    "tag_name": "product/prd_example/v1",
                    "name": "Old title",
                    "draft": True,
                    "prerelease": True,
                    "assets": [
                        {
                            "name": "bundle.zip",
                            "digest": f"sha256:{digest}",
                            "browser_download_url": "https://example.invalid/bundle.zip",
                        }
                    ],
                }
            ),
            ok({"id": 11}),
        ]
    )

    mutation = GitHubCli(
        REPOSITORY,
        runner,
        paths=RepositoryPaths(tmp_path),
    ).upsert_release("product/prd_example/v1", "Product v1", asset, digest)

    assert mutation.action == "update"
    assert json.loads(runner.requests[1].stdin or "") == {
        "draft": False,
        "name": "Product v1",
        "prerelease": False,
    }
    assert len(runner.requests) == 2


@pytest.mark.parametrize("asset_kind", ["outside", "symlink"])
def test_release_asset_must_be_a_regular_file_below_repository_root(
    tmp_path: Path,
    asset_kind: str,
) -> None:
    """Release publication must not upload host files or follow repository symlinks."""
    repository = tmp_path / "repo"
    repository.mkdir()
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"host data")
    asset = outside
    if asset_kind == "symlink":
        asset = repository / "bundle.zip"
        asset.symlink_to(outside)
    runner = RecordingRunner([])

    with pytest.raises(WorkflowSecurityError):
        GitHubCli(
            REPOSITORY,
            runner,
            paths=RepositoryPaths(repository),
        ).upsert_release("product/prd_example/v1", "Product v1", asset, "a" * 64)

    assert runner.requests == []


def test_label_reconciliation_lists_then_updates_exact_label() -> None:
    """Bootstrap must converge label properties without deleting unrelated labels."""
    existing = [{"name": "ard:approved", "color": "ffffff", "description": "old"}]
    runner = RecordingRunner([ok(existing), ok(existing), ok({"name": "ard:approved"})])
    client = GitHubCli(REPOSITORY, runner)

    assert client.list_labels()["ard:approved"].description == "old"
    mutation = client.upsert_label("ard:approved", "0e8a16", "Approved")

    assert mutation.action == "update"
    assert "labels/ard%3Aapproved" in runner.requests[2].argv[4]


def test_environment_secret_names_and_variable_scope_are_exact() -> None:
    """Bootstrap must distinguish protected environment resources from repository variables."""
    runner = RecordingRunner(
        [
            ok(
                [
                    {"total_count": 2, "secrets": [{"name": "ARD_LLM_API_KEY"}]},
                    {"total_count": 2, "secrets": [{"name": "SECOND_SECRET"}]},
                ]
            ),
            ok(),
            ok(),
        ]
    )
    client = GitHubCli(REPOSITORY, runner)

    assert client.list_environment_secret_names("ard-llm") == frozenset(
        {"ARD_LLM_API_KEY", "SECOND_SECRET"}
    )
    client.set_variable("ARD_LLM_MODEL", "gpt-example", "ard-llm")
    client.set_variable("GLOBAL_VALUE", "value")

    assert runner.requests[1].argv[-2:] == ("--env", "ard-llm")
    assert "--env" not in runner.requests[2].argv


def test_bootstrap_resolves_owner_reviewer_and_environment_variables() -> None:
    runner = RecordingRunner(
        [
            ok({"id": 11, "login": "kimohy"}),
            ok(
                [
                    {
                        "total_count": 1,
                        "variables": [{"name": "ARD_LLM_MODEL", "value": "gpt-example"}],
                    }
                ]
            ),
        ]
    )
    client = GitHubCli(REPOSITORY, runner)

    reviewer = client.user_reviewer("kimohy")
    variables = client.list_variables("ard-llm")

    assert reviewer == EnvironmentReviewer(kind="User", id=11, login="kimohy")
    assert variables == {"ARD_LLM_MODEL": "gpt-example"}
    assert "environments/ard-llm/variables" in runner.requests[1].argv[4]


def test_environment_secret_value_exists_only_in_gh_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LLM key must not enter argv, output, mutations, or serialized exceptions."""
    monkeypatch.setenv("GH_TOKEN", "github-token")
    runner = RecordingRunner([ok()])

    mutation = GitHubCli(REPOSITORY, runner).set_environment_secret(
        "ard-llm",
        "ARD_LLM_API_KEY",
        "sentinel-key",
    )

    request = runner.requests[0]
    assert request.argv == (
        "gh",
        "secret",
        "set",
        "ARD_LLM_API_KEY",
        "--env",
        "ard-llm",
        "--repo",
        REPOSITORY,
    )
    assert request.stdin == "sentinel-key"
    assert set(request.secrets) == {"github-token", "sentinel-key"}
    assert "sentinel-key" not in " ".join(request.argv)
    assert "sentinel-key" not in mutation.model_dump_json()


def test_actions_permissions_round_trip_uses_exact_repository_endpoint() -> None:
    """Bootstrap must compare desired workflow permissions before mutating them."""
    current = ActionsPermissionState(
        default_workflow_permissions="read",
        can_approve_pull_request_reviews=False,
    )
    desired = replace(current, can_approve_pull_request_reviews=True)
    runner = RecordingRunner(
        [
            ok(asdict(current)),
            ok(),
        ]
    )
    client = GitHubCli(REPOSITORY, runner)

    assert client.get_actions_permissions() == current
    mutation = client.set_actions_permissions(desired)

    assert mutation.action == "set"
    assert runner.requests[1].argv[4] == f"repos/{REPOSITORY}/actions/permissions/workflow"
    assert json.loads(runner.requests[1].stdin or "") == asdict(desired)


def test_environment_snapshot_and_update_preserve_exact_reviewers_and_branches() -> None:
    """Protected jobs must not silently broaden reviewer or branch policy."""
    current_payload = {
        "name": "ard-llm",
        "protection_rules": [
            {"type": "wait_timer", "wait_timer": 0},
            {
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [{"type": "User", "reviewer": {"id": 12, "login": "kimohy"}}],
            },
            {
                "type": "branch_policy",
            },
        ],
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
    }
    desired = EnvironmentState(
        name="ard-llm",
        reviewers=(EnvironmentReviewer(kind="User", id=12, login="kimohy"),),
        prevent_self_review=True,
        wait_timer=0,
        branch_patterns=("ard/*", "main"),
    )
    current_policies = [
        {"branch_policies": [{"id": 7, "name": "main", "type": "branch"}]}
    ]
    runner = RecordingRunner(
        [
            ok(current_payload),
            ok(current_policies),
            ok(current_payload),
            ok(current_policies),
            ok(),
            ok({"id": 8, "name": "ard/*", "type": "branch"}),
        ]
    )
    client = GitHubCli(REPOSITORY, runner)

    assert client.get_environment("ard-llm").branch_patterns == ("main",)
    mutation = client.upsert_environment(desired)

    assert mutation.target == "environment:ard-llm"
    body = json.loads(runner.requests[4].stdin or "")
    assert body["reviewers"] == [{"id": 12, "type": "User"}]
    assert "branch_patterns" not in body
    assert json.loads(runner.requests[5].stdin or "") == {"name": "ard/*", "type": "branch"}


def test_branch_protection_snapshot_and_update_keep_fail_closed_settings() -> None:
    """Bootstrap must not drop an existing fail-closed protection setting."""
    desired = BranchProtectionState(
        required_statuses=("ard/changeset", "ard/quality-gate"),
        strict=True,
        enforce_admins=True,
        required_approving_review_count=0,
        require_conversation_resolution=True,
        allow_force_pushes=False,
        allow_deletions=False,
    )
    runner = RecordingRunner(
        [
            ok(
                {
                    "required_status_checks": {
                        "strict": True,
                        "contexts": ["ard/quality-gate", "ard/changeset"],
                    },
                    "enforce_admins": {"enabled": True},
                    "required_pull_request_reviews": {"required_approving_review_count": 0},
                    "required_conversation_resolution": {"enabled": True},
                    "allow_force_pushes": {"enabled": False},
                    "allow_deletions": {"enabled": False},
                }
            ),
            ok(),
        ]
    )
    client = GitHubCli(REPOSITORY, runner)

    snapshot = client.get_branch_protection("main")
    assert snapshot == desired
    assert snapshot.require_pull_request is True
    mutation = client.set_branch_protection("main", desired)

    assert mutation.target == "branch:main"
    body = json.loads(runner.requests[1].stdin or "")
    assert body["required_status_checks"]["contexts"] == [
        "ard/changeset",
        "ard/quality-gate",
    ]
    assert body["required_pull_request_reviews"]["required_approving_review_count"] == 0
