"""Scratch-Git checks for frozen, nonpublishing bundle preparation."""
from __future__ import annotations

import json
import fcntl
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_bundle as bundle


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.PIPE).strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-b", "dev-002")
    git(root, "config", "user.name", "Test author")
    git(root, "config", "user.email", "tests@localhost")
    (root / "shared.txt").write_text("base\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    git(root, "init", "--bare", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "origin", "dev-002")
    # Only repository identity is substituted: all preparation Git operations,
    # including fetch/ls-remote, run against a real, isolated bare test remote.
    monkeypatch.setattr(bundle, "verify_skybuild", lambda checkout: root if checkout == root else pytest.fail("wrong checkout"))
    monkeypatch.setattr(bundle, "available_memory_bytes", lambda: 16 * 1024**3)
    manifest = tmp_path / "manifest.json"
    policy = tmp_path / "policy.md"
    policy.write_text("Existing independent review and combined gate requirements.\n")
    values = {"schema": "skybuild.bundle-input.v1", "target_ref": "refs/heads/dev-002",
              "base_sha": base, "policy_evidence": "policy.md", "members": []}

    def member(name, filename=None, content="task\n"):
        git(root, "checkout", "-b", name, base)
        (root / (filename or name + ".txt")).write_text(content)
        git(root, "add", ".")
        git(root, "commit", "-m", name)
        sha = git(root, "rev-parse", "HEAD")
        git(root, "push", "origin", name)
        git(root, "checkout", "dev-002")
        review = tmp_path / (name + "-review.md")
        review.write_text(f"Independent review PASS for exact head {sha}.\n")
        values["members"].append({"task_id": name, "ref": "refs/heads/" + name, "head_sha": sha,
                                  "review": {"verdict": "pass", "head_sha": sha,
                                             "reviewer": "independent-session", "evidence": review.name}})
        save()
        return sha

    def save():
        manifest.write_text(json.dumps(values))

    save()
    return root, manifest, tmp_path / "prepared", values, member, save


def test_two_heads_included_without_shared_ref_changes_and_rerun(repository):
    root, manifest, output, values, member, _ = repository
    heads = [member("one"), member("two")]
    before = git(root, "show-ref")
    result = bundle.prepare(root, manifest, output)
    candidate = Path(result["candidate"])
    assert result["ok"] and not result["gated"] and not result["published"]
    assert result["candidate_tree"] == git(candidate, "rev-parse", "HEAD^{tree}")
    assert [item["head"] for item in result["included"]] == heads
    assert [item["changed_paths"] for item in result["included"]] == [["one.txt"], ["two.txt"]]
    for head in heads + [values["base_sha"]]:
        git(candidate, "merge-base", "--is-ancestor", head, "HEAD")
    assert git(candidate, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert git(root, "show-ref") == before
    assert git(root, "rev-parse", "HEAD") == values["base_sha"]
    assert bundle.prepare(root, manifest, output) == result


@pytest.mark.parametrize("which", ["member", "base"])
def test_remote_movement_refused_without_replacing_candidate(repository, which):
    root, manifest, output, values, member, _ = repository
    sha = member("one")
    result = bundle.prepare(root, manifest, output)
    ref = "refs/heads/one" if which == "member" else "refs/heads/dev-002"
    replacement = values["base_sha"] if which == "member" else sha
    git(root, "push", "--force", "origin", replacement + ":" + ref)
    with pytest.raises(bundle.PreparationError, match="Remote refs moved"):
        bundle.prepare(root, manifest, output)
    assert json.loads((output / "report.json").read_text()) == result
    assert git(Path(result["candidate"]), "rev-parse", "HEAD") == result["candidate_head"]


def test_movement_during_composition_retains_failure(repository, monkeypatch):
    root, manifest, output, values, member, _ = repository
    member("one")
    original = bundle.check_refs
    count = 0

    def moving_refs(checkout, inputs):
        nonlocal count
        count += 1
        if count == 3:
            git(root, "push", "--force", "origin", values["base_sha"] + ":refs/heads/one")
        original(checkout, inputs)

    monkeypatch.setattr(bundle, "check_refs", moving_refs)
    with pytest.raises(bundle.PreparationError, match="Remote refs moved"):
        bundle.prepare(root, manifest, output)
    report = json.loads((output / "report.json").read_text())
    assert not report["ok"] and len(report["included"]) == 1
    assert (output / "candidate" / "one.txt").is_file()


def test_conflict_preserves_index_diagnostics_and_shared_refs(repository):
    root, manifest, output, _, member, _ = repository
    member("one", "shared.txt", "one\n")
    member("two", "shared.txt", "two\n")
    before = git(root, "show-ref")
    with pytest.raises(bundle.PreparationError, match="CONFLICT"):
        bundle.prepare(root, manifest, output)
    report_bytes = (output / "report.json").read_bytes()
    report = json.loads(report_bytes)
    assert not report["ok"] and report["conflicts"] == ["shared.txt"]
    candidate = output / "candidate"
    assert "UU shared.txt" in git(candidate, "status", "--porcelain")
    assert git(root, "show-ref") == before
    with pytest.raises(bundle.PreparationError, match="Previous preparation failed"):
        bundle.prepare(root, manifest, output)
    assert (output / "report.json").read_bytes() == report_bytes


@pytest.mark.parametrize("change", ["verdict", "head", "evidence", "short-sha", "duplicate"])
def test_unreviewed_or_ambiguous_members_refused(repository, change):
    root, manifest, output, values, member, save = repository
    member("one")
    item = values["members"][0]
    if change == "verdict":
        item["review"]["verdict"] = "pending"
    elif change == "head":
        item["review"]["head_sha"] = values["base_sha"]
    elif change == "evidence":
        (manifest.parent / item["review"]["evidence"]).write_text("No exact review.\n")
    elif change == "short-sha":
        item["head_sha"] = item["head_sha"][:7]
        item["review"]["head_sha"] = item["head_sha"]
    else:
        values["members"].append(item)
    save()
    with pytest.raises(bundle.PreparationError):
        bundle.prepare(root, manifest, output)
    assert not output.exists()


@pytest.mark.parametrize("dirty", ["source", "candidate", "moved-candidate", "attached-candidate"])
def test_dirty_or_changed_checkout_refused(repository, dirty):
    root, manifest, output, values, member, _ = repository
    member("one")
    if dirty == "source":
        (root / "foreign.txt").write_text("other work")
    else:
        result = bundle.prepare(root, manifest, output)
        candidate = Path(result["candidate"])
        if dirty == "candidate":
            (candidate / "foreign.txt").write_text("other work")
        elif dirty == "moved-candidate":
            git(candidate, "checkout", "--detach", values["base_sha"])
        else:
            git(candidate, "checkout", "-b", "attached")
    with pytest.raises(bundle.PreparationError, match="clean|changed|detached"):
        bundle.prepare(root, manifest, output)


def test_changed_review_or_membership_cannot_reuse_output(repository):
    root, manifest, output, values, member, save = repository
    member("one")
    bundle.prepare(root, manifest, output)
    evidence = manifest.parent / values["members"][0]["review"]["evidence"]
    evidence.write_text(evidence.read_text() + "Revised evidence.\n")
    with pytest.raises(bundle.PreparationError, match="different frozen inputs"):
        bundle.prepare(root, manifest, output)
    member("two")
    save()
    with pytest.raises(bundle.PreparationError, match="different frozen inputs"):
        bundle.prepare(root, manifest, output)


def test_foreign_or_nested_output_never_adopted(repository):
    root, manifest, output, _, member, _ = repository
    member("one")
    output.mkdir()
    foreign = output / "foreign.txt"
    foreign.write_text("preserve")
    with pytest.raises(bundle.PreparationError, match="unowned"):
        bundle.prepare(root, manifest, output)
    assert foreign.read_text() == "preserve"
    with pytest.raises(bundle.PreparationError, match="isolated"):
        bundle.prepare(root, manifest, root / "nested")
    assert not (root / "nested").exists()


def test_low_memory_retains_failure_without_candidate(repository, monkeypatch):
    root, manifest, output, _, member, _ = repository
    member("one")
    monkeypatch.setattr(bundle, "available_memory_bytes", lambda: 8 * 1024**3 - 1)
    with pytest.raises(bundle.PreparationError, match="8 GiB reserve"):
        bundle.prepare(root, manifest, output)
    assert not (output / "candidate").exists()
    assert not json.loads((output / "report.json").read_text())["ok"]


def test_active_invocation_and_symlink_output_refused(repository):
    root, manifest, output, _, member, _ = repository
    member("one")
    result = bundle.prepare(root, manifest, output)
    with (output / "preparation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(bundle.PreparationError, match="already active"):
            bundle.prepare(root, manifest, output)
    link = output.parent / "linked-output"
    link.symlink_to(output, target_is_directory=True)
    with pytest.raises(bundle.PreparationError, match="symlink"):
        bundle.prepare(root, manifest, link)
    assert json.loads((output / "report.json").read_text()) == result


def test_changed_path_facts_preserve_unusual_filename(repository):
    root, manifest, output, _, member, _ = repository
    filename = " leading space\nand newline.txt"
    member("one", filename)
    result = bundle.prepare(root, manifest, output)
    assert result["included"][0]["changed_paths"] == [filename]


def test_repository_identity_guard_is_required(tmp_path):
    # The production guard is deliberately not patched for this test.
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    git(foreign, "init")
    with pytest.raises(bundle.RepoGuardError):
        bundle.prepare(foreign, tmp_path / "absent.json", tmp_path / "output")
