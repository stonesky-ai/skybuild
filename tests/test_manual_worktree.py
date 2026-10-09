"""Owned worktree preparation preserves collisions and all existing work."""

import subprocess

import pytest

from skybuild.manual_assignment import AssignmentError
from skybuild.manual_worktree import WorktreeError, prepare_worktree
from test_manual_assignment import pinned  # noqa: F401


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def setup(pinned, tmp_path):
    repo, envelope = pinned
    base_ref = "refs/remotes/origin/dev-001"
    git(repo, "update-ref", base_ref, envelope["base_sha"])
    destination = tmp_path / "owned"

    def prepare(value=None):
        return prepare_worktree(envelope if value is None else value, repo, worker="wonko",
                                destination=destination, base_ref=base_ref)

    return repo, envelope, destination, prepare


def test_create_and_duplicate_delivery_are_exact_and_pristine(setup):
    repo, envelope, destination, prepare = setup
    first = prepare()
    assert first == prepare()
    assert first["prepared"] is True
    assert git(destination, "rev-parse", "HEAD") == envelope["base_sha"]
    assert git(destination, "symbolic-ref", "--short", "HEAD") == envelope["branch"]
    assert git(repo, "rev-parse", "HEAD") == envelope["base_sha"]


@pytest.mark.parametrize("kind", ["untracked", "staged", "ignored", "commit", "foreign_branch"])
def test_retry_preserves_work_and_requires_rescue(setup, kind):
    repo, envelope, destination, prepare = setup
    prepare()
    if kind == "foreign_branch":
        git(destination, "checkout", "-b", "feature/foreign")
    else:
        file = destination / "rescue.txt"
        file.write_text("valuable work\n")
        if kind == "ignored":
            (destination / ".gitignore").write_text("rescue.txt\n")
            git(destination, "add", ".gitignore")
            git(destination, "commit", "-m", "Ignore private work")
        if kind in {"staged", "commit"}:
            git(destination, "add", "rescue.txt")
        if kind == "commit":
            git(destination, "commit", "-m", "Unpublished work")
    head = git(destination, "rev-parse", "HEAD")
    status = git(destination, "status", "--porcelain", "--ignored")
    with pytest.raises(WorktreeError, match="preserve|rescue"):
        prepare()
    assert git(destination, "rev-parse", "HEAD") == head
    assert git(destination, "status", "--porcelain", "--ignored") == status


@pytest.mark.parametrize("collision", ["directory", "foreign_repo", "branch"])
def test_unowned_collision_is_preserved(setup, collision):
    repo, envelope, destination, prepare = setup
    if collision == "branch":
        git(repo, "branch", envelope["branch"])
    else:
        destination.mkdir()
        (destination / "keep").write_text("foreign work")
        if collision == "foreign_repo":
            subprocess.run(["git", "init", "--quiet", str(destination)], check=True)
    with pytest.raises(WorktreeError, match="ownership|owned"):
        prepare()
    if collision != "branch":
        assert (destination / "keep").read_text() == "foreign work"


def test_stale_base_blocks_create_and_retry(setup):
    repo, envelope, destination, prepare = setup
    prepare()
    git(repo, "commit", "--allow-empty", "-m", "New development base")
    git(repo, "update-ref", "refs/remotes/origin/dev-001", "HEAD")
    with pytest.raises(WorktreeError, match="base changed"):
        prepare()
    assert git(destination, "rev-parse", "HEAD") == envelope["base_sha"]


def test_forged_snapshot_and_foreign_origin_cannot_create(setup):
    repo, envelope, destination, prepare = setup
    with pytest.raises(AssignmentError):
        prepare({"verified": True})
    git(repo, "remote", "set-url", "--push", "origin", "https://github.com/stonesky-ai/skykeep.git")
    with pytest.raises(AssignmentError, match="origin"):
        prepare()
    assert not destination.exists()


def test_interrupted_reservation_retries_without_adopting_existing_branch(setup, monkeypatch):
    repo, envelope, destination, prepare = setup
    import skybuild.manual_worktree as module
    original = module._git

    def fail_creation(repo, *args):
        if args[:2] == ("worktree", "add"):
            raise AssignmentError("simulated interruption")
        return original(repo, *args)

    monkeypatch.setattr(module, "_git", fail_creation)
    with pytest.raises(AssignmentError, match="interruption"):
        prepare()
    monkeypatch.setattr(module, "_git", original)
    assert prepare()["prepared"]


def test_owned_branch_without_checkout_is_not_reset(setup):
    repo, envelope, destination, prepare = setup
    prepare()
    git(repo, "worktree", "remove", str(destination))
    with pytest.raises(WorktreeError, match="preserve"):
        prepare()
    assert git(repo, "rev-parse", envelope["branch"]) == envelope["base_sha"]


def test_ownership_record_cannot_change_destination(setup, tmp_path):
    repo, envelope, destination, prepare = setup
    prepare()
    with pytest.raises(WorktreeError, match="ownership changed"):
        prepare_worktree(envelope, repo, worker="wonko", destination=tmp_path / "other",
                         base_ref="refs/remotes/origin/dev-001")


def test_stale_base_cannot_create_a_checkout(setup):
    repo, envelope, destination, prepare = setup
    git(repo, "commit", "--allow-empty", "-m", "Advance development")
    git(repo, "update-ref", "refs/remotes/origin/dev-001", "HEAD")
    with pytest.raises(WorktreeError, match="base changed"):
        prepare()
    assert not destination.exists()


def test_concurrent_duplicate_preparers_share_one_worktree(setup):
    from concurrent.futures import ThreadPoolExecutor
    repo, envelope, destination, prepare = setup
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: prepare(), range(2)))
    assert results[0] == results[1]
    assert git(repo, "worktree", "list", "--porcelain").count(str(destination)) == 1


@pytest.mark.parametrize("git_marker", ["directory", "file"])
def test_destination_inside_foreign_checkout_is_refused_before_mutation(setup, tmp_path, git_marker):
    repo, envelope, destination, prepare = setup
    foreign = tmp_path / "skykeep"
    foreign.mkdir()
    if git_marker == "directory":
        subprocess.run(["git", "init", "--quiet", str(foreign)], check=True)
    else:
        (foreign / ".git").write_text("gitdir: /missing/foreign/metadata\n")
    keep = foreign / "keep.txt"
    keep.write_text("Foreign work must stay intact.\n")
    nested = foreign / "not-created" / "owned"
    with pytest.raises(WorktreeError, match="another Git checkout"):
        prepare_worktree(envelope, repo, worker="wonko", destination=nested,
                         base_ref="refs/remotes/origin/dev-001")
    assert not nested.parent.exists()
    assert keep.read_text() == "Foreign work must stay intact.\n"
    assert git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/" + envelope["branch"]) == ""
    assert not (repo / ".git" / "skybuild-manual-worktrees").exists()
