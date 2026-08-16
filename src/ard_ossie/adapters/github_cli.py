from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ard_ossie.application.contracts import MutationRecord
from ard_ossie.ports.filesystem import FileSystemPort
from ard_ossie.ports.github import (
    ActionsPermissionState,
    BranchProtectionState,
    CollaboratorState,
    EnvironmentReviewer,
    EnvironmentState,
    GitHubConflict,
    GitHubTransientError,
    LabelState,
    PullRequestState,
    ReleaseAssetPayload,
    ReleaseAssetState,
    ReleaseState,
    RepositoryState,
)
from ard_ossie.ports.process import CommandRequest, CommandResult, CommandRunner

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@*+-]*$")
_MARKER = re.compile(r"^[a-z0-9][a-z0-9:_-]*$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STATUS_STATES = {"error", "failure", "pending", "success"}


class GitHubCli:
    def __init__(
        self,
        repository: str,
        runner: CommandRunner,
        *,
        token_env: str = "GH_TOKEN",
        paths: FileSystemPort | None = None,
    ) -> None:
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError("repository must use owner/name form")
        self.repository_name = repository
        self.owner = repository.split("/", 1)[0]
        self.runner = runner
        self.token_env = token_env
        self.paths = paths

    def repository(self) -> RepositoryState:
        payload = self._api_json("GET", f"repos/{self.repository_name}")
        permissions = _mapping(payload.get("permissions"))
        permission = "read"
        for candidate, flag in (
            ("admin", "admin"),
            ("maintain", "maintain"),
            ("write", "push"),
            ("triage", "triage"),
        ):
            if permissions.get(flag) is True:
                permission = candidate
                break
        return RepositoryState(
            full_name=_string(payload, "full_name"),
            public=not bool(payload.get("private")),
            archived=bool(payload.get("archived")),
            default_branch=_string(payload, "default_branch"),
            permission=permission,
        )

    def branch_sha(self, branch: str) -> str:
        _validate_name(branch, "branch")
        payload = self._api_json(
            "GET",
            f"repos/{self.repository_name}/branches/{quote(branch, safe='')}",
        )
        commit = _mapping(payload.get("commit"))
        return _validated_sha(_string(commit, "sha"))

    def collaborator_permission(self, login: str) -> str:
        _validate_name(login, "login")
        payload = self._api_json(
            "GET",
            f"repos/{self.repository_name}/collaborators/{quote(login, safe='')}/permission",
        )
        response = _mapping(payload)
        permission = response.get("permission") or response.get("role_name")
        if not isinstance(permission, str) or not permission:
            raise GitHubConflict("INVALID_GITHUB_RESPONSE", "missing permission")
        return permission

    def list_collaborators(self) -> tuple[CollaboratorState, ...]:
        payload = self._api_json(
            "GET",
            f"repos/{self.repository_name}/collaborators?per_page=100",
            paginate=True,
        )
        return tuple(
            CollaboratorState(
                login=_string(item, "login"),
                permission=str(item.get("role_name") or _permission_from_flags(item)),
            )
            for item in _items(payload)
        )

    def user_reviewer(self, login: str) -> EnvironmentReviewer:
        _validate_name(login, "login")
        payload = self._api_json("GET", f"users/{quote(login, safe='')}")
        return EnvironmentReviewer(
            kind="User",
            id=int(payload["id"]),
            login=_string(payload, "login"),
        )

    def find_open_pr(self, branch: str) -> PullRequestState | None:
        _validate_name(branch, "branch")
        head = quote(f"{self.owner}:{branch}", safe="")
        payload = self._api_json(
            "GET",
            f"repos/{self.repository_name}/pulls?state=open&head={head}&per_page=100",
        )
        items = _items(payload)
        if not items:
            return None
        if len(items) > 1:
            raise GitHubConflict("MULTIPLE_OPEN_PULL_REQUESTS", branch)
        return _pull_request(items[0])

    def get_pr(self, number: int) -> PullRequestState:
        _positive(number, "pull request")
        return _pull_request(
            self._api_json("GET", f"repos/{self.repository_name}/pulls/{number}")
        )

    def create_draft_pr(
        self,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestState:
        _validate_name(branch, "branch")
        _validate_name(base, "base")
        payload = self._api_json(
            "POST",
            f"repos/{self.repository_name}/pulls",
            payload={"base": base, "body": body, "draft": True, "head": branch, "title": title},
        )
        return _pull_request(payload)

    def set_issue_labels(
        self,
        number: int,
        *,
        add: set[str],
        remove: set[str],
    ) -> list[MutationRecord]:
        _positive(number, "issue")
        current_payload = self._api_json("GET", f"repos/{self.repository_name}/issues/{number}")
        current = {_string(item, "name") for item in _items(current_payload.get("labels", []))}
        desired = (current - remove) | add
        if desired == current:
            return []
        self._api_json(
            "PUT",
            f"repos/{self.repository_name}/issues/{number}/labels",
            payload={"labels": sorted(desired)},
        )
        mutations = [
            MutationRecord(resource="label", target=f"issue:{number}:{name}", action="remove")
            for name in sorted(current - desired)
        ]
        mutations.extend(
            MutationRecord(resource="label", target=f"issue:{number}:{name}", action="add")
            for name in sorted(desired - current)
        )
        return mutations

    def upsert_pr_comment(self, number: int, marker: str, body: str) -> MutationRecord:
        _positive(number, "pull request")
        if not _MARKER.fullmatch(marker):
            raise ValueError("invalid managed comment marker")
        rendered = f"<!-- {marker} -->\n{body}"
        comments = _items(
            self._api_json(
                "GET",
                f"repos/{self.repository_name}/issues/{number}/comments?per_page=100",
                paginate=True,
            )
        )
        matches = [
            item
            for item in comments
            if str(item.get("body", "")).startswith(f"<!-- {marker} -->")
        ]
        if len(matches) > 1:
            raise GitHubConflict("MULTIPLE_MANAGED_COMMENTS", marker)
        if matches:
            comment_id = int(matches[0]["id"])
            if matches[0].get("body") == rendered:
                return MutationRecord(
                    resource="comment",
                    target=f"pr:{number}:{marker}",
                    action="noop",
                    result_id=str(comment_id),
                )
            self._api_json(
                "PATCH",
                f"repos/{self.repository_name}/issues/comments/{comment_id}",
                payload={"body": rendered},
            )
            action = "update"
        else:
            created = self._api_json(
                "POST",
                f"repos/{self.repository_name}/issues/{number}/comments",
                payload={"body": rendered},
            )
            comment_id = int(created["id"])
            action = "create"
        return MutationRecord(
            resource="comment",
            target=f"pr:{number}:{marker}",
            action=action,
            result_id=str(comment_id),
        )

    def set_status(
        self,
        sha: str,
        context: str,
        state: str,
        description: str,
        target_url: str,
    ) -> MutationRecord:
        sha = _validated_sha(sha)
        if state not in _STATUS_STATES:
            raise ValueError("invalid commit status state")
        self._api_json(
            "POST",
            f"repos/{self.repository_name}/statuses/{sha}",
            payload={
                "context": context,
                "description": description,
                "state": state,
                "target_url": target_url,
            },
        )
        return MutationRecord(resource="status", target=f"{sha}:{context}", action="set")

    def get_status(self, sha: str, context: str) -> str | None:
        sha = _validated_sha(sha)
        payload = self._api_json("GET", f"repos/{self.repository_name}/commits/{sha}/status")
        for status in _items(payload.get("statuses", [])):
            if status.get("context") == context:
                return str(status.get("state"))
        return None

    def dispatch_workflow(
        self,
        workflow: str,
        ref: str,
        inputs: dict[str, str],
    ) -> MutationRecord:
        _validate_name(workflow, "workflow")
        _validate_name(ref, "ref")
        self._api_json(
            "POST",
            f"repos/{self.repository_name}/actions/workflows/{quote(workflow, safe='')}/dispatches",
            payload={"inputs": dict(sorted(inputs.items())), "ref": ref},
            empty_ok=True,
        )
        return MutationRecord(resource="workflow", target=f"{workflow}:{ref}", action="dispatch")

    def get_release(self, tag: str) -> ReleaseState | None:
        _validate_name(tag, "tag")
        result = self._api(
            "GET",
            f"repos/{self.repository_name}/releases/tags/{quote(tag, safe='')}",
        )
        if _is_not_found(result):
            return None
        return _release(self._decode(result, "RELEASE_LOOKUP_FAILED"))

    def upsert_release(
        self,
        tag: str,
        title: str,
        asset: ReleaseAssetPayload | Path,
        sha256: str,
    ) -> MutationRecord:
        _validate_name(tag, "tag")
        if not _SHA256.fullmatch(sha256):
            raise ValueError("invalid release asset SHA-256")
        payload = self._release_asset_payload(asset, sha256)
        temporary, upload_path = self._stage_release_asset(payload)
        primary_error: BaseException | None = None
        try:
            existing = self.get_release(tag)
            if existing is not None:
                matching = [item for item in existing.assets if item.name == payload.name]
                if len(matching) > 1:
                    raise GitHubConflict("MULTIPLE_RELEASE_ASSETS", payload.name)
                if matching:
                    digest = matching[0].digest
                    if digest != sha256:
                        raise GitHubConflict("RELEASE_ASSET_CONFLICT", payload.name)
                release_id = existing.id
                metadata_changed = (
                    existing.title != title or existing.draft or existing.prerelease
                )
                if metadata_changed:
                    self._api_json(
                        "PATCH",
                        f"repos/{self.repository_name}/releases/{release_id}",
                        payload={"draft": False, "name": title, "prerelease": False},
                    )
                if matching:
                    return MutationRecord(
                        resource="release",
                        target=tag,
                        action="update" if metadata_changed else "noop",
                        result_id=str(existing.id),
                    )
            else:
                created = self._api_json(
                    "POST",
                    f"repos/{self.repository_name}/releases",
                    payload={
                        "draft": False,
                        "generate_release_notes": False,
                        "name": title,
                        "prerelease": False,
                        "tag_name": tag,
                    },
                )
                release_id = int(created["id"])
            upload = self._gh(
                "release",
                "upload",
                tag,
                str(upload_path),
                "--repo",
                self.repository_name,
                timeout_seconds=600,
            )
            self._require_success(upload, "RELEASE_UPLOAD_FAILED")
            return MutationRecord(
                resource="release",
                target=tag,
                action="upload",
                result_id=f"{release_id}:{sha256}",
            )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                self._cleanup_release_asset_stage(temporary)
            except GitHubTransientError as cleanup_error:
                if primary_error is None:
                    raise
                primary_error.add_note(str(cleanup_error))

    @staticmethod
    def _stage_release_asset(
        payload: ReleaseAssetPayload,
    ) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary: tempfile.TemporaryDirectory[str] | None = None
        try:
            temporary = tempfile.TemporaryDirectory(prefix="ard-release-upload-")
            os.chmod(temporary.name, 0o700)
            upload_path = Path(temporary.name) / payload.name
            descriptor = os.open(
                upload_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(payload.payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            return temporary, upload_path
        except OSError as error:
            if temporary is not None:
                with suppress(GitHubTransientError):
                    GitHubCli._cleanup_release_asset_stage(temporary)
            raise GitHubTransientError(
                "RELEASE_ASSET_STAGING_FAILED",
                "unable to stage the immutable release asset",
            ) from error

    @staticmethod
    def _cleanup_release_asset_stage(
        temporary: tempfile.TemporaryDirectory[str],
    ) -> None:
        try:
            temporary.cleanup()
        except OSError as error:
            raise GitHubTransientError(
                "RELEASE_ASSET_CLEANUP_FAILED",
                "unable to clean the private release asset stage",
            ) from error

    def _release_asset_payload(
        self,
        asset: ReleaseAssetPayload | Path,
        sha256: str,
    ) -> ReleaseAssetPayload:
        if isinstance(asset, ReleaseAssetPayload):
            payload = asset
        else:
            if self.paths is None:
                raise GitHubConflict(
                    "RELEASE_ASSET_PATH_POLICY_REQUIRED",
                    "release upload requires an explicit repository root",
                )
            path = self.paths.resolve_read(asset)
            if not path.is_file():
                raise GitHubConflict("RELEASE_ASSET_NOT_FOUND", path.name)
            try:
                payload = ReleaseAssetPayload(name=path.name, payload=path.read_bytes())
            except OSError as error:
                raise GitHubConflict("RELEASE_ASSET_NOT_FOUND", path.name) from error
        if hashlib.sha256(payload.payload).hexdigest() != sha256:
            raise GitHubConflict(
                "RELEASE_BUNDLE_CHANGED",
                "release asset bytes do not match the verified digest",
            )
        return payload

    def repository_dispatch(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> MutationRecord:
        _validate_name(event_type, "event type")
        self._api_json(
            "POST",
            f"repos/{self.repository_name}/dispatches",
            payload={"client_payload": payload, "event_type": event_type},
            empty_ok=True,
        )
        return MutationRecord(resource="repository_dispatch", target=event_type, action="dispatch")

    def list_labels(self) -> dict[str, LabelState]:
        payload = self._api_json(
            "GET",
            f"repos/{self.repository_name}/labels?per_page=100",
            paginate=True,
        )
        return {
            _string(item, "name"): LabelState(
                name=_string(item, "name"),
                color=_string(item, "color").casefold(),
                description=str(item.get("description") or ""),
            )
            for item in _items(payload)
        }

    def upsert_label(self, name: str, color: str, description: str) -> MutationRecord:
        _validate_name(name, "label")
        desired = LabelState(name=name, color=color.casefold().lstrip("#"), description=description)
        existing = self.list_labels().get(name)
        if existing == desired:
            return MutationRecord(resource="label", target=name, action="noop")
        if existing is None:
            self._api_json(
                "POST",
                f"repos/{self.repository_name}/labels",
                payload=asdict(desired),
            )
            action = "create"
        else:
            self._api_json(
                "PATCH",
                f"repos/{self.repository_name}/labels/{quote(name, safe='')}",
                payload={"color": desired.color, "description": description, "new_name": name},
            )
            action = "update"
        return MutationRecord(resource="label", target=name, action=action)

    def get_actions_permissions(self) -> ActionsPermissionState:
        payload = self._api_json(
            "GET",
            f"repos/{self.repository_name}/actions/permissions/workflow",
        )
        return ActionsPermissionState(
            default_workflow_permissions=_string(payload, "default_workflow_permissions"),
            can_approve_pull_request_reviews=bool(payload.get("can_approve_pull_request_reviews")),
        )

    def set_actions_permissions(self, state: ActionsPermissionState) -> MutationRecord:
        self._api_json(
            "PUT",
            f"repos/{self.repository_name}/actions/permissions/workflow",
            payload=asdict(state),
            empty_ok=True,
        )
        return MutationRecord(
            resource="actions_permissions",
            target=self.repository_name,
            action="set",
        )

    def get_environment(self, name: str) -> EnvironmentState | None:
        state, _ = self._environment_with_policy_ids(name)
        return state

    def upsert_environment(self, state: EnvironmentState) -> MutationRecord:
        current, policy_ids = self._environment_with_policy_ids(state.name)
        if current == state:
            return MutationRecord(
                resource="environment",
                target=f"environment:{state.name}",
                action="noop",
            )
        encoded_name = quote(state.name, safe="")
        self._api_json(
            "PUT",
            f"repos/{self.repository_name}/environments/{encoded_name}",
            payload={
                "deployment_branch_policy": {
                    "custom_branch_policies": True,
                    "protected_branches": False,
                },
                "prevent_self_review": state.prevent_self_review,
                "reviewers": [
                    {"id": reviewer.id, "type": reviewer.kind} for reviewer in state.reviewers
                ],
                "wait_timer": state.wait_timer,
            },
            empty_ok=True,
        )
        desired = set(state.branch_patterns)
        for pattern, policy_id in sorted(policy_ids.items()):
            if pattern not in desired:
                self._api_json(
                    "DELETE",
                    f"repos/{self.repository_name}/environments/{encoded_name}/deployment-branch-policies/{policy_id}",
                    empty_ok=True,
                )
        for pattern in sorted(desired - set(policy_ids)):
            self._api_json(
                "POST",
                f"repos/{self.repository_name}/environments/{encoded_name}/deployment-branch-policies",
                payload={"name": pattern, "type": "branch"},
            )
        return MutationRecord(
            resource="environment",
            target=f"environment:{state.name}",
            action="upsert",
        )

    def list_environment_secret_names(self, environment: str) -> frozenset[str]:
        _validate_name(environment, "environment")
        payload = self._api_json(
            "GET",
            f"repos/{self.repository_name}/environments/"
            f"{quote(environment, safe='')}/secrets?per_page=100",
            paginate=True,
        )
        return frozenset(
            _string(item, "name") for item in _paginated_items(payload, "secrets")
        )

    def set_environment_secret(
        self,
        environment: str,
        name: str,
        value: str,
    ) -> MutationRecord:
        _validate_name(environment, "environment")
        _validate_name(name, "secret")
        result = self._gh(
            "secret",
            "set",
            name,
            "--env",
            environment,
            "--repo",
            self.repository_name,
            stdin=value,
            extra_secrets=(value,),
        )
        self._require_success(result, "ENVIRONMENT_SECRET_SET_FAILED")
        return MutationRecord(
            resource="environment_secret",
            target=f"{environment}:{name}",
            action="set",
        )

    def set_variable(
        self,
        name: str,
        value: str,
        environment: str | None = None,
    ) -> MutationRecord:
        _validate_name(name, "variable")
        arguments = ["variable", "set", name, "--body", value, "--repo", self.repository_name]
        target = name
        if environment is not None:
            _validate_name(environment, "environment")
            arguments.extend(("--env", environment))
            target = f"{environment}:{name}"
        result = self._gh(*arguments)
        self._require_success(result, "VARIABLE_SET_FAILED")
        return MutationRecord(resource="variable", target=target, action="set")

    def list_variables(self, environment: str | None = None) -> dict[str, str]:
        if environment is None:
            endpoint = f"repos/{self.repository_name}/actions/variables?per_page=100"
        else:
            _validate_name(environment, "environment")
            endpoint = (
                f"repos/{self.repository_name}/environments/"
                f"{quote(environment, safe='')}/variables?per_page=100"
            )
        payload = self._api_json("GET", endpoint, paginate=True)
        return {
            _string(item, "name"): _string(item, "value")
            for item in _paginated_items(payload, "variables")
        }

    def get_branch_protection(self, branch: str) -> BranchProtectionState | None:
        _validate_name(branch, "branch")
        result = self._api(
            "GET",
            f"repos/{self.repository_name}/branches/{quote(branch, safe='')}/protection",
        )
        if _is_not_found(result):
            return None
        payload = self._decode(result, "BRANCH_PROTECTION_LOOKUP_FAILED")
        status_checks = _mapping(payload.get("required_status_checks"))
        reviews = _mapping(payload.get("required_pull_request_reviews"))
        return BranchProtectionState(
            required_statuses=tuple(
                sorted(str(item) for item in status_checks.get("contexts", []))
            ),
            strict=bool(status_checks.get("strict")),
            enforce_admins=_enabled(payload.get("enforce_admins")),
            required_approving_review_count=int(reviews.get("required_approving_review_count", 0)),
            require_conversation_resolution=_enabled(payload.get("required_conversation_resolution")),
            allow_force_pushes=_enabled(payload.get("allow_force_pushes")),
            allow_deletions=_enabled(payload.get("allow_deletions")),
            require_pull_request=payload.get("required_pull_request_reviews") is not None,
        )

    def set_branch_protection(
        self,
        branch: str,
        state: BranchProtectionState,
    ) -> MutationRecord:
        _validate_name(branch, "branch")
        self._api_json(
            "PUT",
            f"repos/{self.repository_name}/branches/{quote(branch, safe='')}/protection",
            payload={
                "allow_deletions": state.allow_deletions,
                "allow_force_pushes": state.allow_force_pushes,
                "enforce_admins": state.enforce_admins,
                "required_conversation_resolution": state.require_conversation_resolution,
                "required_pull_request_reviews": (
                    {
                        "dismiss_stale_reviews": True,
                        "require_code_owner_reviews": False,
                        "required_approving_review_count": state.required_approving_review_count,
                    }
                    if state.require_pull_request
                    else None
                ),
                "required_status_checks": {
                    "contexts": sorted(state.required_statuses),
                    "strict": state.strict,
                },
                "restrictions": None,
            },
            empty_ok=True,
        )
        return MutationRecord(resource="branch_protection", target=f"branch:{branch}", action="set")

    def _environment_with_policy_ids(
        self,
        name: str,
    ) -> tuple[EnvironmentState | None, dict[str, int]]:
        _validate_name(name, "environment")
        encoded_name = quote(name, safe="")
        result = self._api("GET", f"repos/{self.repository_name}/environments/{encoded_name}")
        if _is_not_found(result):
            return None, {}
        payload = self._decode(result, "ENVIRONMENT_LOOKUP_FAILED")
        reviewers: list[EnvironmentReviewer] = []
        prevent_self_review = False
        wait_timer = 0
        for rule in _items(payload.get("protection_rules", [])):
            if rule.get("type") == "wait_timer":
                wait_timer = int(rule.get("wait_timer", 0))
            if rule.get("type") == "required_reviewers":
                prevent_self_review = bool(rule.get("prevent_self_review"))
                for item in _items(rule.get("reviewers", [])):
                    reviewer = _mapping(item.get("reviewer"))
                    reviewers.append(
                        EnvironmentReviewer(
                            kind=str(item.get("type")),
                            id=int(reviewer["id"]),
                            login=str(
                                reviewer.get("login")
                                or reviewer.get("slug")
                                or reviewer["id"]
                            ),
                        )
                    )
        policy_ids: dict[str, int] = {}
        deployment = _mapping(payload.get("deployment_branch_policy"))
        if deployment.get("custom_branch_policies") is True:
            policies = self._api_json(
                "GET",
                f"repos/{self.repository_name}/environments/{encoded_name}/deployment-branch-policies?per_page=100",
                paginate=True,
            )
            for policy in _paginated_items(policies, "branch_policies"):
                policy_ids[_string(policy, "name")] = int(policy["id"])
        return (
            EnvironmentState(
                name=_string(payload, "name"),
                reviewers=tuple(sorted(reviewers, key=lambda item: (item.kind, item.id))),
                prevent_self_review=prevent_self_review,
                wait_timer=wait_timer,
                branch_patterns=tuple(sorted(policy_ids)),
            ),
            policy_ids,
        )

    def _api_json(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        paginate: bool = False,
        empty_ok: bool = False,
    ) -> dict[str, Any] | list[Any]:
        result = self._api(method, path, payload=payload, paginate=paginate)
        if empty_ok and result.returncode == 0 and not result.stdout.strip():
            return {}
        return self._decode(result, "GITHUB_API_FAILED")

    def _api(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        paginate: bool = False,
    ) -> CommandResult:
        arguments = ["api", "--method", method, path]
        if paginate:
            arguments.extend(("--paginate", "--slurp"))
        stdin = None
        if payload is not None:
            arguments.extend(("--input", "-"))
            stdin = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return self._gh(*arguments, stdin=stdin)

    def _gh(
        self,
        *arguments: str,
        stdin: str | None = None,
        extra_secrets: tuple[str, ...] = (),
        timeout_seconds: int = 60,
    ) -> CommandResult:
        environment: dict[str, str] = {}
        for key in ("PATH", "HOME", "XDG_CONFIG_HOME", "GH_HOST", "NO_COLOR"):
            if os.environ.get(key):
                environment[key] = os.environ[key]
        token = os.environ.get(self.token_env)
        secrets = tuple(item for item in (token, *extra_secrets) if item)
        if token:
            environment[self.token_env] = token
        return self.runner.run(
            CommandRequest(
                argv=("gh", *arguments),
                stdin=stdin,
                env=environment,
                timeout_seconds=timeout_seconds,
                secrets=secrets,
            )
        )

    @staticmethod
    def _decode(result: CommandResult, code: str) -> dict[str, Any] | list[Any]:
        GitHubCli._require_success(result, code)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise GitHubConflict("INVALID_GITHUB_JSON", str(error)) from None
        if not isinstance(payload, (dict, list)):
            raise GitHubConflict("INVALID_GITHUB_JSON", "expected object or array")
        return payload

    @staticmethod
    def _require_success(result: CommandResult, code: str) -> None:
        if result.returncode != 0:
            raise GitHubTransientError(code, result.stderr or result.stdout or "gh command failed")


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    flattened: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, list):
            flattened.extend(_items(item))
        elif isinstance(item, dict):
            flattened.append(item)
    return flattened


def _paginated_items(value: object, collection_key: str) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return _items(value.get(collection_key, []))
    if not isinstance(value, list):
        return []
    flattened: list[dict[str, Any]] = []
    for page in value:
        if isinstance(page, dict) and collection_key in page:
            flattened.extend(_items(page[collection_key]))
        elif isinstance(page, dict):
            flattened.append(page)
        elif isinstance(page, list):
            flattened.extend(_items(page))
    return flattened


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise GitHubConflict("INVALID_GITHUB_RESPONSE", f"missing {key}")
    return value


def _permission_from_flags(item: dict[str, Any]) -> str:
    permissions = _mapping(item.get("permissions"))
    for permission in ("admin", "maintain", "push", "triage", "pull"):
        if permissions.get(permission) is True:
            return {"push": "write", "pull": "read"}.get(permission, permission)
    return "read"


def _pull_request(payload: dict[str, Any]) -> PullRequestState:
    head = _mapping(payload.get("head"))
    base = _mapping(payload.get("base"))
    merge_sha = payload.get("merge_commit_sha")
    return PullRequestState(
        number=int(payload["number"]),
        head_branch=_string(head, "ref"),
        head_sha=_validated_sha(_string(head, "sha")),
        base_branch=_string(base, "ref"),
        draft=bool(payload.get("draft")),
        merged_at=str(payload["merged_at"]) if payload.get("merged_at") else None,
        merge_sha=_validated_sha(str(merge_sha)) if merge_sha else None,
        url=_string(payload, "html_url"),
    )


def _release(payload: dict[str, Any]) -> ReleaseState:
    assets = tuple(
        ReleaseAssetState(
            name=_string(item, "name"),
            digest=_release_digest(item.get("digest")),
            url=_string(item, "browser_download_url"),
        )
        for item in _items(payload.get("assets", []))
    )
    return ReleaseState(
        id=int(payload["id"]),
        tag=_string(payload, "tag_name"),
        title=_string(payload, "name"),
        draft=bool(payload.get("draft")),
        prerelease=bool(payload.get("prerelease")),
        assets=assets,
    )


def _release_digest(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    digest = value.removeprefix("sha256:").casefold()
    return digest if _SHA256.fullmatch(digest) else None


def _is_not_found(result: CommandResult) -> bool:
    return result.returncode != 0 and (
        "404" in result.stderr or "not found" in result.stderr.casefold()
    )


def _enabled(value: object) -> bool:
    if isinstance(value, dict):
        return bool(value.get("enabled"))
    return bool(value)


def _validated_sha(value: str) -> str:
    normalized = value.casefold()
    if not _SHA.fullmatch(normalized):
        raise GitHubConflict("INVALID_GITHUB_SHA", normalized[:80])
    return normalized


def _validate_name(value: str, kind: str) -> None:
    if not _NAME.fullmatch(value) or ".." in value or "//" in value:
        raise ValueError(f"invalid {kind}")


def _positive(value: int, kind: str) -> None:
    if value <= 0:
        raise ValueError(f"{kind} number must be positive")
