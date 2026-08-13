from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ard_ossie.adapters.filesystem import RepositoryPaths
from ard_ossie.application.contracts import (
    WorkflowConflict,
    WorkflowPartialError,
    WorkflowSecurityError,
)
from ard_ossie.application.release_publication import (
    ReleasePublicationRequest,
    ReleasePublicationService,
)
from ard_ossie.cli import release as release_cli
from ard_ossie.impact import build_changeset
from ard_ossie.models import (
    ProductRecord,
    ProductTableRef,
    TableLocator,
    TableRecord,
)
from ard_ossie.ports.github import (
    GitHubTransientError,
    PullRequestState,
    ReleaseAssetState,
    ReleaseState,
)
from ard_ossie.registry import Registry
from ard_ossie.release import ReleaseBlocked, build_release_bundle

CURRENT = "a" * 40
MERGE_SHA = "b" * 40
HEAD_SHA = "c" * 40
PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"
TABLE_ID = "tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c"
LINK_ID = "lnk_0198f6ca-2a11-78d1-8672-67d49e69f14d"
CHANGESET_ID = "cst_0198f6cf-c3d5-7fc8-9401-22fa7b330ec2"


class FakeGit:
    def __init__(self) -> None:
        self.sha = CURRENT
        self.tags: dict[str, str] = {}
        self.created: list[str] = []
        self.pushed: list[tuple[str, ...]] = []
        self.ancestors = {(MERGE_SHA, CURRENT)}

    def current_sha(self) -> str:
        return self.sha

    def tag_target(self, tag: str) -> str | None:
        return self.tags.get(tag)

    def create_annotated_tag(self, tag: str, target: str, message: str) -> None:
        assert target == CURRENT
        assert message == tag
        self.tags[tag] = target
        self.created.append(tag)

    def push_tags(self, tags: list[str]) -> None:
        self.pushed.append(tuple(tags))

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return (ancestor, descendant) in self.ancestors


class FakeGitHub:
    def __init__(self) -> None:
        self.pr = PullRequestState(
            number=7,
            head_branch=f"ard/{CHANGESET_ID}-sales-order",
            head_sha=HEAD_SHA,
            base_branch="main",
            draft=False,
            merged_at="2026-08-10T00:00:00Z",
            merge_sha=MERGE_SHA,
            url="https://example.invalid/pull/7",
        )
        self.release: ReleaseState | None = None
        self.fail_release = False
        self.upserts: list[tuple[str, str, Path, str]] = []

    def get_pr(self, number: int) -> PullRequestState:
        assert number == 7
        return self.pr

    def get_release(self, tag: str) -> ReleaseState | None:
        return self.release

    def upsert_release(self, tag: str, title: str, asset: Path, sha256: str):
        if self.fail_release:
            raise GitHubTransientError("RELEASE_UPLOAD_FAILED", "network")
        self.upserts.append((tag, title, asset, sha256))
        from ard_ossie.application.contracts import MutationRecord

        action = "noop" if self.release is not None else "upload"
        return MutationRecord(resource="release", target=tag, action=action)


def request(tmp_path: Path) -> ReleasePublicationRequest:
    return ReleasePublicationRequest(
        repository=tmp_path,
        product_key="sales-order",
        current=CURRENT,
        table_ids=[TABLE_ID],
        output=tmp_path / "dist",
    )


def build_repository(tmp_path: Path) -> None:
    registry = Registry.load(tmp_path / "registry")
    registry.write_product(
        ProductRecord(product_id=PRODUCT_ID, product_key="sales-order", version=12)
    )
    registry.write_table(
        TableRecord(
            table_id=TABLE_ID,
            locator=TableLocator(
                source_system_id="erp",
                catalog="analytics",
                schema_name="sales",
                table_name="orders",
            ),
            version=7,
        )
    )
    registry.write_mappings(
        PRODUCT_ID,
        [
            ProductTableRef(
                link_id=LINK_ID,
                product_id=PRODUCT_ID,
                table_id=TABLE_ID,
                table_version=7,
                usage="SOURCE",
            )
        ],
    )
    changeset = build_changeset(
        [TABLE_ID],
        [PRODUCT_ID],
        changeset_id=CHANGESET_ID,
    )
    changeset.mark_ready(PRODUCT_ID, version=12, pr_number=7, head_sha=HEAD_SHA)
    registry.write_changeset(changeset)

    product = tmp_path / "products" / "sales-order"
    generated = product / "generated"
    quality = product / "quality"
    generated.mkdir(parents=True)
    quality.mkdir()
    generated_names = (
        "data-product.md",
        "data-semantic.md",
        "data-dictionary.json",
        "ossie-model.json",
        "source-manifest.json",
    )
    hashes: dict[str, str] = {}
    for name in generated_names:
        value = f"generated:{name}".encode()
        (generated / name).write_bytes(value)
        hashes[name] = hashlib.sha256(value).hexdigest()
    quality_hashes: dict[str, str] = {}
    for name in (
        "duplicate-report.json",
        "version-report.json",
        "impact-report.json",
        "llm-suggestions.json",
        "semantic-fidelity.json",
    ):
        payload: dict[str, object] = {"name": name}
        if name == "semantic-fidelity.json":
            payload = {
                "source_hash": "a" * 64,
                "extraction_mode": "docx_xml",
                "page_count": 0,
                "parser_versions": {"semantic": "test"},
                "status": "PASS",
                "heading_count": 0,
                "paragraph_count": 1,
                "list_item_count": 0,
                "table_count": 0,
                "row_count": 0,
                "cell_count": 0,
                "source_span_count": 1,
                "preserved_span_count": 1,
                "excluded_span_count": 0,
                "unmatched_span_count": 0,
                "duplicated_span_count": 0,
                "degraded_block_count": 0,
                "source_text_coverage": 1.0,
                "removed_elements": [],
                "degraded_blocks": [],
                "table_results": [],
            }
        value = json.dumps(payload).encode()
        (quality / name).write_bytes(value)
        quality_hashes[name] = hashlib.sha256(value).hexdigest()
    (quality / "quality-report.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "product_id": PRODUCT_ID,
                "product_version": 12,
                "completeness": 1,
                "hard_errors": [],
                "warnings": [],
                "artifact_hashes": hashes,
                "quality_artifact_hashes": quality_hashes,
            }
        ),
        encoding="utf-8",
    )
    (product / "product.yaml").write_text(
        f"changeset_id: {CHANGESET_ID}\n",
        encoding="utf-8",
    )


def service(tmp_path: Path, git: FakeGit, github: FakeGitHub, **kwargs):
    return ReleasePublicationService(
        RepositoryPaths(tmp_path),
        git,
        github,
        **kwargs,
    )


def test_publish_verifies_every_recorded_pr_head_and_ancestry(tmp_path: Path) -> None:
    build_repository(tmp_path)
    git = FakeGit()
    github = FakeGitHub()

    result = service(tmp_path, git, github).run(request(tmp_path))

    assert result.outputs["product_tag"] == f"product/{PRODUCT_ID}/v12"
    bundle = tmp_path / str(result.artifacts[0])
    assert result.outputs["artifact_sha256"] == hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert git.pushed == [
        (
            f"product/{PRODUCT_ID}/v12",
            f"table/{TABLE_ID}/v7",
        )
    ]
    assert github.upserts[0][0] == f"product/{PRODUCT_ID}/v12"


def test_publish_rejects_existing_tag_at_other_commit(tmp_path: Path) -> None:
    build_repository(tmp_path)
    git = FakeGit()
    git.tags[f"product/{PRODUCT_ID}/v12"] = "d" * 40
    github = FakeGitHub()

    with pytest.raises(WorkflowConflict, match="TAG_TARGET_CONFLICT"):
        service(tmp_path, git, github).run(request(tmp_path))

    assert git.created == []
    assert github.upserts == []


@pytest.mark.parametrize(
    ("pr_update", "code"),
    [
        ({"head_sha": "d" * 40}, "CHANGESET_HEAD_SHA_MISMATCH"),
        ({"merged_at": None}, "CHANGESET_PR_NOT_MERGED"),
        ({"merge_sha": "d" * 40}, "CHANGESET_MERGE_NOT_REACHABLE"),
    ],
)
def test_publish_rejects_unverified_readiness_pr(
    tmp_path: Path,
    pr_update: dict[str, str | None],
    code: str,
) -> None:
    build_repository(tmp_path)
    git = FakeGit()
    github = FakeGitHub()
    github.pr = PullRequestState(**{**github.pr.__dict__, **pr_update})

    with pytest.raises(WorkflowConflict, match=code):
        service(tmp_path, git, github).run(request(tmp_path))

    assert git.created == []


def test_publish_rerun_reuses_tags_after_release_failure(tmp_path: Path) -> None:
    build_repository(tmp_path)
    git = FakeGit()
    github = FakeGitHub()
    github.fail_release = True

    with pytest.raises(WorkflowPartialError, match="RELEASE_PUBLICATION_PARTIAL"):
        service(tmp_path, git, github).run(request(tmp_path))

    assert len(git.created) == 2
    github.fail_release = False
    result = service(tmp_path, git, github).run(request(tmp_path))
    assert result.outputs["product_tag"] == f"product/{PRODUCT_ID}/v12"
    assert len(git.created) == 2


def test_publish_rejects_existing_release_asset_mismatch(tmp_path: Path) -> None:
    build_repository(tmp_path)
    git = FakeGit()
    github = FakeGitHub()
    github.release = ReleaseState(
        id=3,
        tag=f"product/{PRODUCT_ID}/v12",
        title="sales-order v12",
        draft=False,
        prerelease=False,
        assets=(
            ReleaseAssetState(
                name=f"{PRODUCT_ID}-v12.zip",
                digest="d" * 64,
                url="https://example.invalid/asset.zip",
            ),
        ),
    )

    with pytest.raises(WorkflowConflict, match="RELEASE_ASSET_CONFLICT"):
        service(tmp_path, git, github).run(request(tmp_path))

    assert git.created == []


def test_publish_reuses_existing_matching_release(tmp_path: Path) -> None:
    build_repository(tmp_path)
    bundle = build_release_bundle(
        tmp_path / "products" / "sales-order",
        tmp_path / "prebuilt" / f"{PRODUCT_ID}-v12.zip",
    )
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    git = FakeGit()
    git.tags = {
        f"product/{PRODUCT_ID}/v12": CURRENT,
        f"table/{TABLE_ID}/v7": CURRENT,
    }
    github = FakeGitHub()
    github.release = ReleaseState(
        id=3,
        tag=f"product/{PRODUCT_ID}/v12",
        title="sales-order v12",
        draft=False,
        prerelease=False,
        assets=(
            ReleaseAssetState(
                name=f"{PRODUCT_ID}-v12.zip",
                digest=digest,
                url="https://example.invalid/asset.zip",
            ),
        ),
    )

    result = service(tmp_path, git, github).run(request(tmp_path))

    assert result.mutations[-1].action == "noop"
    assert git.created == []
    assert git.pushed == []


def test_publish_rejects_missing_quality_before_tags(tmp_path: Path) -> None:
    build_repository(tmp_path)
    (tmp_path / "products" / "sales-order" / "quality" / "quality-report.json").unlink()
    git = FakeGit()

    with pytest.raises(WorkflowConflict, match="QUALITY_REPORT_MISSING"):
        service(tmp_path, git, FakeGitHub()).run(request(tmp_path))

    assert git.created == []


@pytest.mark.parametrize(
    ("update", "code"),
    [
        ({"status": "garbage"}, "QUALITY_REPORT_INVALID"),
        (
            {"product_id": "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a632"},
            "QUALITY_REPORT_PRODUCT_MISMATCH",
        ),
        ({"product_version": 11}, "QUALITY_REPORT_VERSION_MISMATCH"),
    ],
)
def test_publish_rejects_malformed_or_stale_quality_evidence_before_tags(
    tmp_path: Path,
    update: dict[str, object],
    code: str,
) -> None:
    build_repository(tmp_path)
    quality_path = (
        tmp_path / "products" / "sales-order" / "quality" / "quality-report.json"
    )
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality.update(update)
    quality_path.write_text(json.dumps(quality), encoding="utf-8")
    git = FakeGit()

    with pytest.raises(WorkflowConflict, match=code):
        service(tmp_path, git, FakeGitHub()).run(request(tmp_path))

    assert git.created == []


def test_publish_rejects_incomplete_generated_hash_evidence_before_tags(
    tmp_path: Path,
) -> None:
    build_repository(tmp_path)
    quality_path = (
        tmp_path / "products" / "sales-order" / "quality" / "quality-report.json"
    )
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["artifact_hashes"].pop("ossie-model.json")
    quality_path.write_text(json.dumps(quality), encoding="utf-8")
    git = FakeGit()

    with pytest.raises(WorkflowConflict, match="QUALITY_ARTIFACT_HASH_SET_MISMATCH"):
        service(tmp_path, git, FakeGitHub()).run(request(tmp_path))

    assert git.created == []


def test_publish_rejects_non_object_quality_evidence_before_tags(
    tmp_path: Path,
) -> None:
    build_repository(tmp_path)
    quality_path = (
        tmp_path / "products" / "sales-order" / "quality" / "quality-report.json"
    )
    quality_path.write_text("[]", encoding="utf-8")
    git = FakeGit()

    with pytest.raises(WorkflowConflict, match="QUALITY_REPORT_INVALID"):
        service(tmp_path, git, FakeGitHub()).run(request(tmp_path))

    assert git.created == []


def test_publish_rejects_bundle_source_tampering_before_tags(tmp_path: Path) -> None:
    build_repository(tmp_path)
    git = FakeGit()
    github = FakeGitHub()

    def tampering_builder(product_root: Path, output: Path) -> Path:
        (product_root / "generated" / "ossie-model.json").write_text(
            "tampered",
            encoding="utf-8",
        )
        return build_release_bundle(product_root, output)

    with pytest.raises(WorkflowConflict, match="RELEASE_BUNDLE_HASH_MISMATCH"):
        service(
            tmp_path,
            git,
            github,
            bundle_builder=tampering_builder,
        ).run(request(tmp_path))

    assert git.created == []


def test_publish_rejects_bundle_mutation_after_hash_before_tags(tmp_path: Path) -> None:
    build_repository(tmp_path)
    bundle = tmp_path / "dist" / f"{PRODUCT_ID}-v12.zip"

    class MutatingGit(FakeGit):
        changed = False

        def tag_target(self, tag: str) -> str | None:
            if not self.changed:
                self.changed = True
                bundle.write_bytes(b"replacement payload after service hash")
            return super().tag_target(tag)

    class HashingGitHub(FakeGitHub):
        def upsert_release(self, tag: str, title: str, asset: object, sha256: str):
            payload = (
                asset.payload
                if hasattr(asset, "payload")
                else Path(asset).read_bytes()  # type: ignore[arg-type]
            )
            if hashlib.sha256(payload).hexdigest() != sha256:
                raise WorkflowConflict(
                    "RELEASE_BUNDLE_CHANGED",
                    "release bundle changed before upload",
                )
            return super().upsert_release(tag, title, asset, sha256)  # type: ignore[arg-type]

    git = MutatingGit()
    github = HashingGitHub()

    with pytest.raises(WorkflowConflict, match="RELEASE_BUNDLE_CHANGED"):
        service(tmp_path, git, github).run(request(tmp_path))

    assert git.created == []
    assert git.pushed == []
    assert github.upserts == []


def test_release_build_rejects_completed_zip_that_does_not_match_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_repository(tmp_path)
    real_builder = build_release_bundle

    def tampering_builder(product_root: Path, output: Path) -> Path:
        (product_root / "generated" / "ossie-model.json").write_text(
            "changed after plan resolution",
            encoding="utf-8",
        )
        return real_builder(product_root, output)

    monkeypatch.setattr(release_cli, "build_release_bundle", tampering_builder)

    with pytest.raises(ReleaseBlocked, match="RELEASE_BUNDLE_HASH_MISMATCH"):
        release_cli.release_build(
            PRODUCT_ID,
            registry=tmp_path / "registry",
            output=tmp_path / "dist",
            repository=tmp_path,
            table_id=[TABLE_ID],
        )

    assert not (tmp_path / "dist" / "release-plan.json").exists()
    assert not (tmp_path / "dist" / f"{PRODUCT_ID}-v12.zip").exists()


def test_release_build_promotes_the_verified_immutable_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_repository(tmp_path)
    verified_payload = b""
    real_verifier = release_cli.verify_release_bundle

    def mutate_after_verification(bundle: Path, expected: dict[str, str]) -> bytes:
        nonlocal verified_payload
        verified_payload = real_verifier(bundle, expected)
        bundle.write_bytes(b"replacement after verification")
        return verified_payload

    monkeypatch.setattr(release_cli, "verify_release_bundle", mutate_after_verification)

    release_cli.release_build(
        PRODUCT_ID,
        registry=tmp_path / "registry",
        output=tmp_path / "dist",
        repository=tmp_path,
        table_id=[TABLE_ID],
    )

    destination = tmp_path / "dist" / f"{PRODUCT_ID}-v12.zip"
    assert destination.read_bytes() == verified_payload
    assert destination.read_bytes() != b"replacement after verification"


def test_release_build_rejects_builder_returned_symlink_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_repository(tmp_path)
    real_builder = build_release_bundle

    def aliasing_builder(product_root: Path, output: Path) -> Path:
        real_builder(product_root, output)
        alias = output.with_suffix(".alias")
        alias.symlink_to(output.name)
        return alias

    monkeypatch.setattr(release_cli, "build_release_bundle", aliasing_builder)

    with pytest.raises(ReleaseBlocked, match="RELEASE_BUNDLE_OUTPUT_MISMATCH"):
        release_cli.release_build(
            PRODUCT_ID,
            registry=tmp_path / "registry",
            output=tmp_path / "dist",
            repository=tmp_path,
            table_id=[TABLE_ID],
        )

    assert not (tmp_path / "dist" / "release-plan.json").exists()
    assert not (tmp_path / "dist" / f"{PRODUCT_ID}-v12.zip").exists()


@pytest.mark.parametrize("replacement_kind", ["symlink", "directory"])
def test_release_build_rejects_nonregular_exact_builder_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    build_repository(tmp_path)
    real_builder = build_release_bundle

    def nonregular_builder(product_root: Path, output: Path) -> Path:
        alternate = output.with_suffix(".real")
        real_builder(product_root, alternate)
        output.unlink()
        if replacement_kind == "symlink":
            output.symlink_to(alternate.name)
        else:
            output.mkdir()
        return output

    monkeypatch.setattr(release_cli, "build_release_bundle", nonregular_builder)

    with pytest.raises(ReleaseBlocked, match="RELEASE_BUNDLE_OUTPUT_MISMATCH"):
        release_cli.release_build(
            PRODUCT_ID,
            registry=tmp_path / "registry",
            output=tmp_path / "dist",
            repository=tmp_path,
            table_id=[TABLE_ID],
        )

    assert not (tmp_path / "dist" / "release-plan.json").exists()
    assert not (tmp_path / "dist" / f"{PRODUCT_ID}-v12.zip").exists()


def test_publish_rejects_symlinked_public_source_before_bundle_or_tags(
    tmp_path: Path,
) -> None:
    build_repository(tmp_path)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside-secret", encoding="utf-8")
    source = (
        tmp_path
        / "products"
        / "sales-order"
        / "generated"
        / "data-product.md"
    )
    source.unlink()
    source.symlink_to(outside)
    git = FakeGit()

    with pytest.raises(WorkflowSecurityError, match="SYMLINK_NOT_ALLOWED"):
        service(tmp_path, git, FakeGitHub()).run(request(tmp_path))

    assert git.created == []
    assert not (tmp_path / "dist").exists()


def test_publish_rejects_symlinked_bundle_output_without_touching_target(
    tmp_path: Path,
) -> None:
    build_repository(tmp_path)
    outside = tmp_path / "outside-target.zip"
    outside.write_bytes(b"do-not-overwrite")
    output = tmp_path / "dist"
    output.mkdir()
    bundle = output / f"{PRODUCT_ID}-v12.zip"
    bundle.symlink_to(outside)
    git = FakeGit()

    with pytest.raises(WorkflowSecurityError, match="SYMLINK_NOT_ALLOWED"):
        service(tmp_path, git, FakeGitHub()).run(request(tmp_path))

    assert outside.read_bytes() == b"do-not-overwrite"
    assert git.created == []
