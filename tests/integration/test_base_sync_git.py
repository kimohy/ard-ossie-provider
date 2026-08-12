from __future__ import annotations

import subprocess
from pathlib import Path

from ard_ossie.adapters.git_cli import GitCli
from ard_ossie.adapters.subprocess import SubprocessRunner

BRANCH = "ard/issue-3-500138301"
PRODUCT_ID = "prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631"


def git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=check,
        text=True,
        capture_output=True,
    )


def write(repository: Path, relative: str, content: str) -> None:
    path = repository / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def commit_all(repository: Path, message: str) -> str:
    git(repository, "add", "--all", "--")
    git(repository, "commit", "-m", message, "--")
    return git(repository, "rev-parse", "HEAD").stdout.strip()


def exists_at(repository: Path, revision: str, relative: str) -> bool:
    return (
        git(repository, "cat-file", "-e", f"{revision}:{relative}", check=False).returncode
        == 0
    )


def test_merge_restore_commit_and_push_preserve_only_approved_input(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    candidate = tmp_path / "candidate"
    git(tmp_path, "init", "--bare", str(origin))
    git(tmp_path, "init", "--initial-branch=main", str(seed))
    git(seed, "config", "user.name", "Test Maintainer")
    git(seed, "config", "user.email", "maintainer@example.invalid")
    write(seed, "README.md", "base\n")
    write(
        seed,
        "products/500138301/product.yaml",
        "operation: create\nproduct_key: '500138301'\n",
    )
    write(
        seed,
        "products/500138301/intake-manifest.json",
        '{"product_key":"500138301"}\n',
    )
    write(
        seed,
        "products/500138301/sources/product.html",
        "approved source\n",
    )
    commit_all(seed, "initial base")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "--set-upstream", "origin", "main")
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")

    git(seed, "switch", "--create", BRANCH)
    write(
        seed,
        "products/500138301/generated/ossie-model.json",
        '{"derived":true}\n',
    )
    write(
        seed,
        f"registry/products/{PRODUCT_ID}.json",
        '{"derived":true}\n',
    )
    candidate_head = commit_all(seed, "processed product")
    git(seed, "push", "--set-upstream", "origin", BRANCH)

    git(seed, "switch", "main")
    write(seed, "src/trusted-change.py", "TRUSTED = True\n")
    write(
        seed,
        "products/500138301/product.yaml",
        "operation: update\nproduct_key: '500138301'\n",
    )
    base_sha = commit_all(seed, "advance trusted base")
    git(seed, "push", "origin", "main")

    git(tmp_path, "clone", str(origin), str(candidate))
    git(candidate, "switch", "--detach", candidate_head)
    adapter = GitCli(candidate, SubprocessRunner())
    merge = adapter.merge_revision(base_sha, "sync trusted main")
    assert (
        git(candidate, "show", "HEAD:products/500138301/product.yaml").stdout
        == "operation: update\nproduct_key: '500138301'\n"
    )
    adapter.restore_paths(
        candidate_head,
        [
            Path("products/500138301/product.yaml"),
            Path("products/500138301/intake-manifest.json"),
            Path("products/500138301/sources/product.html"),
        ],
    )
    preserved = adapter.commit_intake_paths("500138301", "preserve approved intake")
    adapter.restore_paths(
        base_sha,
        [
            Path("products/500138301/generated/ossie-model.json"),
            Path(f"registry/products/{PRODUCT_ID}.json"),
        ],
    )
    reset = adapter.commit_allowed_paths("500138301", "reset derived output")
    adapter.push(BRANCH)

    assert merge.created is True
    assert preserved.created is True
    assert reset.created is True
    final_sha = reset.sha
    assert git(candidate, "merge-base", "--is-ancestor", base_sha, final_sha).returncode == 0
    assert (
        git(
            candidate,
            "show",
            f"{final_sha}:products/500138301/product.yaml",
        ).stdout
        == "operation: create\nproduct_key: '500138301'\n"
    )
    assert (
        git(
            candidate,
            "show",
            f"{final_sha}:products/500138301/sources/product.html",
        ).stdout
        == "approved source\n"
    )
    assert exists_at(candidate, final_sha, "src/trusted-change.py")
    assert not exists_at(
        candidate,
        final_sha,
        "products/500138301/generated/ossie-model.json",
    )
    assert not exists_at(
        candidate,
        final_sha,
        f"registry/products/{PRODUCT_ID}.json",
    )
    assert git(candidate, "status", "--porcelain").stdout == ""
    assert (
        git(origin, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip()
        == final_sha
    )
    merge_parents = git(candidate, "rev-list", "--parents", "-n", "1", merge.sha)
    assert len(merge_parents.stdout.split()) == 3
