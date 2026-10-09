"""Safety checks for the branch-closeout helper, using only local Git repos."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import closeout_branches as closeout  # noqa: E402
from _repo_guard import RepoGuardError, verify_skybuild, verify_skybuild_remote  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True,
                          check=True).stdout.strip()


def repo_pair(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    git(work, "config", "user.email", "test@example.invalid")
    git(work, "config", "user.name", "Test")
    (work / "base.txt").write_text("base\n")
    git(work, "add", "base.txt")
    git(work, "commit", "-m", "base")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "main")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gh = fake_bin / "gh"
    gh.write_text("#!/bin/sh\nprintf '[]\\n'\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    return work


def feature(repo):
    git(repo, "switch", "-c", "feature")
    (repo / "feature.txt").write_text("feature\n")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-m", "feature")
    git(repo, "push", "-u", "origin", "feature")
    git(repo, "switch", "main")


def test_closeout_deletes_only_merged_branches(tmp_path, monkeypatch):
    repo = repo_pair(tmp_path, monkeypatch)
    feature(repo)
    git(repo, "merge", "--no-ff", "--no-edit", "feature")
    git(repo, "push", "origin", "main")
    head = git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(closeout, "verify_skybuild_remote", lambda *_: None)
    result = closeout.apply(repo, "origin", "main", head)
    assert result["deleted_remote"] == ["feature"]
    assert result["deleted_local"] == ["feature"]
    assert list(closeout.remote_heads(repo, "origin")) == ["main"]
    assert closeout.local_heads(repo) == ["main"]


def test_closeout_refuses_unmerged_branch(tmp_path, monkeypatch):
    repo = repo_pair(tmp_path, monkeypatch)
    feature(repo)
    head = git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(closeout, "verify_skybuild_remote", lambda *_: None)
    with pytest.raises(closeout.CloseoutError, match="not merged"):
        closeout.plan(repo, "origin", "main", head)
    assert "feature" in closeout.remote_heads(repo, "origin")


def test_repo_guard_refuses_a_different_origin(tmp_path, monkeypatch):
    repo = repo_pair(tmp_path, monkeypatch)
    (repo / "src/skybuild").mkdir(parents=True)
    (repo / "src/skybuild/store.py").write_text("")
    (repo / "docs/design").mkdir(parents=True)
    (repo / "docs/design/architecture.md").write_text("")
    with pytest.raises(RepoGuardError, match="fetch/push URLs"):
        verify_skybuild(repo)


def test_repo_guard_refuses_foreign_push_destination(tmp_path, monkeypatch):
    repo = repo_pair(tmp_path, monkeypatch)
    git(repo, "remote", "set-url", "origin", "https://github.com/stonesky-ai/skybuild.git")
    verify_skybuild_remote(repo, "origin")
    git(repo, "remote", "set-url", "--push", "origin", "https://github.com/stonesky-ai/skykeep.git")
    with pytest.raises(RepoGuardError, match="fetch/push URLs"):
        verify_skybuild_remote(repo, "origin")
    git(repo, "remote", "add", "skykeep", "https://github.com/stonesky-ai/skykeep.git")
    with pytest.raises(RepoGuardError, match="fetch/push URLs"):
        verify_skybuild_remote(repo, "skykeep")


def test_closeout_refuses_branch_advanced_after_plan(tmp_path, monkeypatch):
    repo = repo_pair(tmp_path, monkeypatch)
    feature(repo)
    git(repo, "merge", "--no-ff", "--no-edit", "feature")
    git(repo, "push", "origin", "main")
    head = git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(closeout, "verify_skybuild_remote", lambda *_: None)
    original_plan = closeout.plan

    def move_branch(*args):
        result = original_plan(*args)
        git(repo, "update-ref", "refs/heads/feature", head)
        return result

    monkeypatch.setattr(closeout, "plan", move_branch)
    with pytest.raises(closeout.CloseoutError, match="local branches"):
        closeout.apply(repo, "origin", "main", head)
    assert "feature" in closeout.remote_heads(repo, "origin")


def test_closeout_checks_advertised_commit_instead_of_stale_tracking_ref(tmp_path, monkeypatch):
    repo = repo_pair(tmp_path, monkeypatch)
    feature(repo)
    git(repo, "merge", "--no-ff", "--no-edit", "feature")
    git(repo, "push", "origin", "main")
    head = git(repo, "rev-parse", "HEAD")
    feature_head = git(repo, "rev-parse", "feature")
    tree = git(repo, "rev-parse", "feature^{tree}")
    advanced = subprocess.run(
        ["git", "commit-tree", tree, "-p", feature_head, "-m", "advance feature"],
        cwd=repo, text=True, capture_output=True, check=True,
    ).stdout.strip()
    original_remote_heads = closeout.remote_heads

    def advertised_heads(*args):
        heads = original_remote_heads(*args)
        heads["feature"] = advanced
        return heads

    monkeypatch.setattr(closeout, "verify_skybuild_remote", lambda *_: None)
    monkeypatch.setattr(closeout, "remote_heads", advertised_heads)
    with pytest.raises(closeout.CloseoutError, match="remote branch is not merged"):
        closeout.plan(repo, "origin", "main", head)
